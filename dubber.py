import os
import sys
import subprocess
import asyncio
import time
import re
import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator, LingueeTranslator
import edge_tts
from pydub import AudioSegment

# ==========================================
# 1. 📥 Download Media & Strip Input Stream
# ==========================================
def download_media(url: str) -> tuple[str, str]:
    video_path = "input_video.mp4"
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
        'quiet': False,
        'no_warnings': True,
    }

    print("📥 Downloading video stream...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    print("🎵 Extracting raw 16kHz PCM audio for AI processing...")
    subprocess.run([
        'ffmpeg', '-y', '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        audio_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    return video_path, audio_path

def get_duration(file_path: str) -> float:
    cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \"{file_path}\""
    return float(subprocess.check_output(cmd, shell=True).strip())

# ==========================================
# 2. 🎙️ Transcription, Detection & Forced Translation
# ==========================================
def translate_text_enforced(text: str, source_lang: str, target_lang: str) -> str:
    clean_text = re.sub(r'[\r\n\t]+', ' ', text).strip()
    if not clean_text or len(clean_text) < 2:
        return clean_text

    if source_lang == target_lang:
        return clean_text

    # Attempt 1: Google Translator
    try:
        res = GoogleTranslator(source='auto', target=target_lang).translate(clean_text)
        if res and res.strip().lower() != clean_text.lower():
            return res.strip()
    except Exception as e:
        print(f"⚠️ Google Translate failed: {e}")

    # Attempt 2: MyMemory Translator
    try:
        res = MyMemoryTranslator(source=source_lang if source_lang != 'auto' else 'en', target=target_lang).translate(clean_text)
        if res and res.strip().lower() != clean_text.lower():
            return res.strip()
    except Exception as e:
        print(f"⚠️ MyMemory Translate failed: {e}")

    # Attempt 3: Sentence decomposition
    words = clean_text.split()
    if len(words) > 3:
        try:
            half = len(words) // 2
            part1 = GoogleTranslator(source='auto', target=target_lang).translate(" ".join(words[:half]))
            part2 = GoogleTranslator(source='auto', target=target_lang).translate(" ".join(words[half:]))
            combined = f"{part1} {part2}".strip()
            if combined and combined.lower() != clean_text.lower():
                return combined
        except Exception:
            pass

    print(f"⚠️ Warning: Retaining original string for: '{clean_text[:30]}'")
    return clean_text

def transcribe_and_translate(audio_path: str, target_lang: str = "hi") -> list[dict]:
    print("🎙️ Transcribing and auto-detecting spoken language...")
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    
    # language=None enables Whisper's native auto-detection
    raw_segments, info = model.transcribe(audio_path, language=None, vad_filter=True)
    detected_lang = info.language
    print(f"🌍 Detected source language: {detected_lang} (Probability: {info.language_probability:.2f})")

    raw_list = []
    for s in raw_segments:
        text = s.text.strip()
        if text and (s.end - s.start >= 0.25):
            raw_list.append({"start": s.start, "end": s.end, "text": text})

    if not raw_list:
        print("⚠️ No dialogue detected in video.")
        return []

    # Merge adjacent segments (< 0.5s pause) for smoother sentence synthesis
    merged = [raw_list[0]]
    for s in raw_list[1:]:
        prev = merged[-1]
        if s["start"] - prev["end"] < 0.5:
            prev["end"] = max(prev["end"], s["end"])
            prev["text"] += " " + s["text"]
        else:
            merged.append(s)

    segments = []
    print(f"🌐 Translating {len(merged)} dialogues ({detected_lang} -> {target_lang})...")
    first_preview_printed = False

    for i, s in enumerate(merged, start=1):
        translated = translate_text_enforced(s["text"], detected_lang, target_lang)

        # Emit Preview Tag for Android App Log Interceptor
        if not first_preview_printed and translated:
            print(f"TRANSLATION_PREVIEW: {translated}")
            first_preview_printed = True

        print(f"[{i}/{len(merged)}] SRC: {s['text']}")
        print(f"       -> TARGET: {translated}")

        segments.append({
            "orig_start": s["start"],
            "orig_end": s["end"],
            "translated_text": translated,
            "target_lang": target_lang
        })

    return segments

# ==========================================
# 3. 🗣️ Speech Synthesis & Voice Replacement
# ==========================================
async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    print("🗣️ Generating new AI narration voiceover...")

    for i, seg in enumerate(segments):
        audio_file = os.path.join(temp_dir, f"audio_{i}.mp3")
        text = seg["translated_text"].strip()
        lang = seg.get("target_lang", "hi")

        # Select natural Neural voices depending on chosen language
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

        if not clean_tts_text or len(clean_tts_text) < 2:
            silent = AudioSegment.silent(duration=500)
            silent.export(audio_file, format="mp3")
            seg["audio_file"] = audio_file
            seg["tts_dur"] = 0.5
            continue

        seg["audio_file"] = audio_file
        success = False

        for attempt in range(3):
            try:
                rate = "+15%" if lang != "en" else "+5%"
                communicate = edge_tts.Communicate(clean_tts_text, voice, rate=rate)
                await communicate.save(audio_file)
                if os.path.exists(audio_file) and os.path.getsize(audio_file) > 100:
                    seg["tts_dur"] = get_duration(audio_file)
                    success = True
                    break
            except Exception as e:
                print(f"⚠️ TTS retry {attempt + 1}: {e}")
                await asyncio.sleep(1.0)

        if not success:
            fallback_dur = max(0.5, seg["orig_end"] - seg["orig_start"])
            silent = AudioSegment.silent(duration=int(fallback_dur * 1000))
            silent.export(audio_file, format="mp3")
            seg["tts_dur"] = fallback_dur

# ==========================================
# 4. 🎬 Video Assembly & Total Metadata Stripping
# ==========================================
def render_dubbed_video(video_path: str, segments: list[dict], output_file: str):
    print("🎬 Stitching clean master audio track...")
    orig_total_dur = get_duration(video_path)

    # Prevent speech collisions by enforcing non-overlapping timestamps
    timeline_cursor = 0.0
    for seg in segments:
        start_time = max(seg["orig_start"], timeline_cursor)
        seg["new_start"] = start_time
        seg["new_end"] = start_time + seg["tts_dur"]
        timeline_cursor = seg["new_end"] + 0.15

    final_audio_dur = max(orig_total_dur, timeline_cursor)

    master_audio = AudioSegment.silent(duration=int((final_audio_dur + 1.5) * 1000))
    for seg in segments:
        clip = AudioSegment.from_file(seg["audio_file"])
        pos_ms = int(seg["new_start"] * 1000)
        master_audio = master_audio.overlay(clip, position=pos_ms)

    master_audio_path = "master_output_audio.wav"
    master_audio.export(master_audio_path, format="wav")

    stretch_factor = final_audio_dur / orig_total_dur if orig_total_dur > 0 else 1.0

    print("🛡️ Rendering MP4 & Purging 100% metadata/EXIF/signatures...")
    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        '-i', master_audio_path,
        '-filter_complex', f'[0:v]setpts={stretch_factor:.5f}*PTS,fps=30[vout]',
        '-map', '[vout]',
        '-map', '1:a',
        # Complete metadata and vendor stripping
        '-map_metadata', '-1',
        '-map_chapters', '-1',
        '-fflags', '+bitexact',
        '-flags:v', '+bitexact',
        '-flags:a', '+bitexact',
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-b:v', '2600k',
        '-maxrate', '3200k',
        '-bufsize', '6400k',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-shortest',
        output_file
    ]
    subprocess.run(cmd, check=True)

# ==========================================
# 5. 📝 Synchronized Subtitle (.srt) Generation
# ==========================================
def format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def generate_srt(segments: list[dict], output_file: str = "subtitles.srt"):
    print("📝 Generating synchronized SRT captions...")
    with open(output_file, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            start_str = format_timestamp(seg["new_start"])
            end_str = format_timestamp(seg["new_end"])
            f.write(f"{i}\n{start_str} --> {end_str}\n{seg['translated_text']}\n\n")

# ==========================================
# 🚀 Entrypoint
# ==========================================
if __name__ == "__main__":
    url = sys.argv[1]
    target_lang = sys.argv[2] if len(sys.argv) > 2 else "hi"

    v_file, a_file = download_media(url)
    segs = transcribe_and_translate(a_file, target_lang=target_lang)
    asyncio.run(synthesize_audio(segs))

    render_dubbed_video(v_file, segs, "final_output.mp4")
    generate_srt(segs, "subtitles.srt")
    print("🎉 Dubbing and metadata sanitization completed!")
