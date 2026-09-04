import os
import sys
import subprocess
import asyncio
import time
import re
import json
import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator
import edge_tts
from pydub import AudioSegment

# ==========================================
# 1. 📥 Download Media & Extract Source Info
# ==========================================
def download_media(url: str) -> tuple[str, str, dict]:
    video_path = "raw_source.mp4"
    audio_path = "input_audio.wav"

    for path in [video_path, audio_path]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': video_path,
        'quiet': True,
        'no_warnings': True,
    }

    print("📥 Fetching source stream and metadata...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        meta = ydl.extract_info(url, download=True)
        meta_dict = {
            "title": meta.get("title", "Dubbed Video"),
            "description": meta.get("description", ""),
            "tags": meta.get("tags", [])
        }

    # Save metadata JSON for the cloud artifact release
    with open("source_meta.json", "w", encoding="utf-8") as mf:
        json.dump(meta_dict, mf, ensure_ascii=False)

    print(f"TITLE_EMIT: {meta_dict['title']}")

    # Extract 16kHz Mono Audio for transcription
    subprocess.run([
        'ffmpeg', '-y', '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        audio_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    return video_path, audio_path, meta_dict

def get_duration(file_path: str) -> float:
    cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \"{file_path}\""
    return float(subprocess.check_output(cmd, shell=True).strip())

# ==========================================
# 2. 🎙️ Auto-Detection & Forced Translation
# ==========================================
def translate_text_enforced(text: str, source_lang: str, target_lang: str) -> str:
    clean_text = re.sub(r'[\r\n\t]+', ' ', text).strip()
    if not clean_text or len(clean_text) < 2:
        return clean_text

    if source_lang == target_lang:
        return clean_text

    # Attempt 1: Google Translate
    try:
        res = GoogleTranslator(source='auto', target=target_lang).translate(clean_text)
        if res and res.strip().lower() != clean_text.lower():
            return res.strip()
    except Exception:
        pass

    # Attempt 2: MyMemory Translate
    try:
        res = MyMemoryTranslator(source=source_lang if source_lang != 'auto' else 'en', target=target_lang).translate(clean_text)
        if res and res.strip().lower() != clean_text.lower():
            return res.strip()
    except Exception:
        pass

    return clean_text

def transcribe_and_translate(audio_path: str, target_lang: str = "hi") -> list[dict]:
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    raw_segments, info = model.transcribe(audio_path, language=None, vad_filter=True)
    detected_lang = info.language

    raw_list = []
    for s in raw_segments:
        t = s.text.strip()
        if t and (s.end - s.start >= 0.25):
            raw_list.append({"start": s.start, "end": s.end, "text": t})

    if not raw_list:
        return []

    # Merge tight adjacent segments
    merged = [raw_list[0]]
    for s in raw_list[1:]:
        prev = merged[-1]
        if s["start"] - prev["end"] < 0.4:
            prev["end"] = max(prev["end"], s["end"])
            prev["text"] += " " + s["text"]
        else:
            merged.append(s)

    segments = []
    first_preview_printed = False

    for s in merged:
        translated = translate_text_enforced(s["text"], detected_lang, target_lang)

        if not first_preview_printed and translated:
            words = translated.strip().split()
            short_preview = " ".join(words[:5]) + ("..." if len(words) > 5 else "")
            print(f"TRANSLATION_PREVIEW: {short_preview}")
            first_preview_printed = True

        segments.append({
            "orig_start": s["start"],
            "orig_end": s["end"],
            "translated_text": translated,
            "target_lang": target_lang
        })

    return segments

# ==========================================
# 3. 🗣️ Speech Synthesis (+18% Snappy Rate)
# ==========================================
async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    for i, seg in enumerate(segments):
        audio_file = os.path.join(temp_dir, f"audio_{i}.mp3")
        text = seg["translated_text"].strip()
        lang = seg.get("target_lang", "hi")

        voice = "hi-IN-MadhurNeural"
        if lang == "en":
            voice = "en-US-GuyNeural"
        elif lang == "es":
            voice = "es-ES-AlvaroNeural"
        elif lang == "fr":
            voice = "fr-FR-HenriNeural"
        elif lang == "de":
            voice = "de-DE-ConradNeural"

        clean_tts_text = re.sub(r'[\[\]\(\)\{\}\<\>\"\'\*\#\@]', '', text).strip()
        if not clean_tts_text:
            silent = AudioSegment.silent(duration=400)
            silent.export(audio_file, format="mp3")
            seg["audio_file"] = audio_file
            seg["tts_dur"] = 0.4
            continue

        seg["audio_file"] = audio_file
        # Faster rate (+18%) keeps video energetic and crisp
        try:
            communicate = edge_tts.Communicate(clean_tts_text, voice, rate="+18%")
            await communicate.save(audio_file)
            seg["tts_dur"] = get_duration(audio_file)
        except Exception:
            dur = max(0.4, seg["orig_end"] - seg["orig_start"])
            silent = AudioSegment.silent(duration=int(dur * 1000))
            silent.export(audio_file, format="mp3")
            seg["tts_dur"] = dur

# ==========================================
# 4. 🎬 +12% Video Speed Up & Total Scrub
# ==========================================
def render_dubbed_video(video_path: str, segments: list[dict], output_file: str):
    orig_total_dur = get_duration(video_path)

    # Calculate tight timeline with minimal inter-speech gaps
    timeline_cursor = 0.0
    for seg in segments:
        start_time = max(seg["orig_start"], timeline_cursor)
        seg["new_start"] = start_time
        seg["new_end"] = start_time + seg["tts_dur"]
        timeline_cursor = seg["new_end"] + 0.1

    final_audio_dur = max(orig_total_dur, timeline_cursor)
    master_audio = AudioSegment.silent(duration=int((final_audio_dur + 1.5) * 1000))

    for seg in segments:
        clip = AudioSegment.from_file(seg["audio_file"])
        master_audio = master_audio.overlay(clip, position=int(seg["new_start"] * 1000))

    master_audio_path = "temp_master_audio.wav"
    master_audio.export(master_audio_path, format="wav")

    # Overall Video Speed increase: 1.12x (12% faster video and audio)
    # setpts=PTS/1.12 increases video speed; atempo=1.12 increases audio speed
    speed_factor = 1.12
    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        '-i', master_audio_path,
        '-filter_complex', f'[0:v]setpts=PTS/{speed_factor},fps=30[v];[1:a]atempo={speed_factor}[a]',
        '-map', '[v]',
        '-map', '[a]',
        '-map_metadata', '-1',
        '-map_chapters', '-1',
        '-fflags', '+bitexact',
        '-flags:v', '+bitexact',
        '-flags:a', '+bitexact',
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-b:v', '2500k',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-shortest',
        output_file
    ]
    subprocess.run(cmd, check=True)

# ==========================================
# 5. 📝 Subtitle (.srt) Generation
# ==========================================
def format_timestamp(seconds: float) -> str:
    # Scale timestamps down by 1.12 to match sped-up video
    seconds = seconds / 1.12
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def generate_srt(segments: list[dict], output_file: str = "subtitles.srt"):
    with open(output_file, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            start_str = format_timestamp(seg["new_start"])
            end_str = format_timestamp(seg["new_end"])
            f.write(f"{i}\n{start_str} --> {end_str}\n{seg['translated_text']}\n\n")

if __name__ == "__main__":
    v_url = sys.argv[1]
    tgt_lang = sys.argv[2] if len(sys.argv) > 2 else "hi"

    v_file, a_file, meta = download_media(v_url)
    segs = transcribe_and_translate(a_file, target_lang=tgt_lang)
    asyncio.run(synthesize_audio(segs))

    render_dubbed_video(v_file, segs, "final_output.mp4")
    generate_srt(segs, "subtitles.srt")
