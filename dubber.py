import os
import sys
import subprocess
import asyncio
import time
import re
import json
import random
import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator
import edge_tts
from pydub import AudioSegment

SPEED_FACTOR = 1.12  # +12% speed increase across the entire video

# ==========================================
# 1. 📥 Download Media & Sanitize Streams
# ==========================================
def download_media(url: str) -> tuple[str, str, dict]:
    dl_path = "downloaded.mp4"
    video_path = "raw_source.mp4"
    audio_path = "input_audio.wav"

    for path in [dl_path, video_path, audio_path]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': dl_path,
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

    with open("source_meta.json", "w", encoding="utf-8") as mf:
        json.dump(meta_dict, mf, ensure_ascii=False)

    print(f"TITLE_EMIT: {meta_dict['title']}")

    # Clean bitstream and standardize container
    subprocess.run([
        'ffmpeg', '-y',
        '-err_detect', 'ignore_err',
        '-i', dl_path,
        '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '192k',
        '-ar', '44100',
        video_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    if os.path.exists(dl_path):
        os.remove(dl_path)

    # 16kHz mono audio for Whisper transcription
    subprocess.run([
        'ffmpeg', '-y',
        '-err_detect', 'ignore_err',
        '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        audio_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    return video_path, audio_path, meta_dict

def get_duration(file_path: str) -> float:
    cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \"{file_path}\""
    return float(subprocess.check_output(cmd, shell=True).strip())

# ==========================================
# 2. 🛡️ Enforced Hindi Translation
# ==========================================
def contains_devanagari(text: str) -> bool:
    """Checks if text contains Hindi (Devanagari) characters."""
    return bool(re.search(r'[\u0900-\u097F]', text))

def is_error_page(text: str) -> bool:
    lowered = text.lower()
    return any(sig in lowered for sig in [
        "server error", "500", "!!1500", "<!doctype", "<html", "captcha", "unusual traffic"
    ])

def translate_to_hindi(text: str) -> str:
    clean_text = re.sub(r'[\r\n\t]+', ' ', text).strip()
    if not clean_text or len(clean_text) < 2:
        return clean_text

    # Strategy 1: Google Translator
    for attempt in range(3):
        try:
            res = GoogleTranslator(source='auto', target='hi').translate(clean_text)
            if res and not is_error_page(res) and contains_devanagari(res):
                time.sleep(random.uniform(0.1, 0.2))
                return res.strip()
        except Exception:
            time.sleep(0.3)

    # Strategy 2: MyMemory Translator
    try:
        res = MyMemoryTranslator(source='en-US', target='hi-IN').translate(clean_text)
        if res and not is_error_page(res) and contains_devanagari(res):
            return res.strip()
    except Exception:
        pass

    # Strategy 3: Split into halves and translate
    words = clean_text.split()
    if len(words) > 2:
        try:
            mid = len(words) // 2
            p1 = GoogleTranslator(source='auto', target='hi').translate(" ".join(words[:mid]))
            p2 = GoogleTranslator(source='auto', target='hi').translate(" ".join(words[mid:]))
            combined = f"{p1} {p2}".strip()
            if contains_devanagari(combined):
                return combined
        except Exception:
            pass

    return ""

def transcribe_and_translate(audio_path: str, target_lang: str = "hi") -> list[dict]:
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    raw_segments, info = model.transcribe(audio_path, language=None, vad_filter=True)
    detected_lang = info.language
    print(f"🌍 Detected source language: {detected_lang}")

    raw_list = []
    for s in raw_segments:
        t = s.text.strip()
        if t and (s.end - s.start >= 0.25):
            raw_list.append({"start": s.start, "end": s.end, "text": t})

    if not raw_list:
        return []

    # Merge very close speech fragments (< 0.2s)
    merged = [raw_list[0]]
    for s in raw_list[1:]:
        prev = merged[-1]
        if s["start"] - prev["end"] < 0.2:
            prev["end"] = max(prev["end"], s["end"])
            prev["text"] += " " + s["text"]
        else:
            merged.append(s)

    segments = []
    first_preview_printed = False

    for i, s in enumerate(merged):
        # Determine maximum duration before next speaker starts
        next_start = merged[i + 1]["start"] if i + 1 < len(merged) else s["end"] + 4.0
        max_allowed_dur = max(s["end"] - s["start"], next_start - s["start"] - 0.1)

        if target_lang == "hi":
            translated = translate_to_hindi(s["text"])
        else:
            try:
                translated = GoogleTranslator(source='auto', target=target_lang).translate(s["text"])
            except Exception:
                translated = s["text"]

        # Ensure we do NOT pass untranslated English into Hindi TTS
        if target_lang == "hi" and not contains_devanagari(translated):
            translated = ""

        if not first_preview_printed and translated:
            words = translated.strip().split()
            short_preview = " ".join(words[:5]) + ("..." if len(words) > 5 else "")
            print(f"TRANSLATION_PREVIEW: {short_preview}")
            first_preview_printed = True

        segments.append({
            "start": s["start"],
            "end": s["end"],
            "max_allowed_dur": max_allowed_dur,
            "translated_text": translated,
            "target_lang": target_lang
        })

    return segments

# ==========================================
# 3. 🗣️ Speech Synthesis & Individual Speed Fit
# ==========================================
def speed_fit_clip(input_wav: str, max_dur: float, output_wav: str):
    """Speeds up clip with atempo only if it exceeds the space before the next line."""
    cur_dur = get_duration(input_wav)
    if cur_dur <= max_dur:
        return input_wav

    speed_ratio = cur_dur / max_dur
    # Limit max compression to 1.8x to preserve clarity
    speed_ratio = min(speed_ratio, 1.8)

    cmd = [
        'ffmpeg', '-y', '-i', input_wav,
        '-filter:a', f'atempo={speed_ratio:.3f}',
        output_wav
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return output_wav

async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    for i, seg in enumerate(segments):
        raw_tts_file = os.path.join(temp_dir, f"raw_{i}.mp3")
        final_clip_file = os.path.join(temp_dir, f"clip_{i}.wav")
        text = seg["translated_text"].strip()
        lang = seg.get("target_lang", "hi")

        voice = "hi-IN-MadhurNeural"
        if lang == "en":
            voice = "en-US-GuyNeural"
        elif lang == "es":
            voice = "es-ES-AlvaroNeural"

        if not text:
            seg["clip_file"] = None
            seg["duration"] = 0.0
            continue

        try:
            communicate = edge_tts.Communicate(text, voice, rate="+18%")
            await communicate.save(raw_tts_file)

            # Fit into slot so it never bleeds into the next dialogue
            speed_fit_clip(raw_tts_file, seg["max_allowed_dur"], final_clip_file)
            seg["clip_file"] = final_clip_file
            seg["duration"] = get_duration(final_clip_file)
        except Exception:
            seg["clip_file"] = None
            seg["duration"] = 0.0

# ==========================================
# 4. 🎬 Synchronized Timeline Assembly & +12% Speed
# ==========================================
def render_dubbed_video(video_path: str, segments: list[dict], output_file: str):
    src_total_dur = get_duration(video_path)
    print(f"🎬 Overlaying audio on anchored timeline (Source: {src_total_dur:.2f}s)...")

    # Construct single continuous audio timeline
    master_track = AudioSegment.silent(duration=int((src_total_dur + 2.0) * 1000))

    for seg in segments:
        if not seg.get("clip_file") or not os.path.exists(seg["clip_file"]):
            continue

        clip = AudioSegment.from_file(seg["clip_file"])
        # Anchored strictly to original start timestamp
        pos_ms = int(seg["start"] * 1000)
        master_track = master_track.overlay(clip, position=pos_ms)

    synced_audio_path = "synced_master.wav"
    master_track.export(synced_audio_path, format="wav")

    print(f"⚡ Encoding output with exact +12% ({SPEED_FACTOR}x) uniform speed increase...")
    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        '-i', synced_audio_path,
        '-filter_complex', f'[0:v]setpts=PTS/{SPEED_FACTOR},fps=30[v];[1:a]atempo={SPEED_FACTOR}[a]',
        '-map', '[v]',
        '-map', '[a]',
        '-map_metadata', '-1',
        '-map_chapters', '-1',
        '-fflags', '+bitexact',
        '-flags:v', '+bitexact',
        '-flags:a', '+bitexact',
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-b:v', '2600k',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-shortest',
        output_file
    ]
    subprocess.run(cmd, check=True)

    for temp_f in [synced_audio_path, "temp_audio"]:
        if os.path.isfile(temp_f):
            os.remove(temp_f)
        elif os.path.isdir(temp_f):
            import shutil
            shutil.rmtree(temp_f)

# ==========================================
# 5. 📝 Synchronized Subtitles (.srt)
# ==========================================
def format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def generate_srt(segments: list[dict], output_file: str = "subtitles.srt"):
    with open(output_file, "w", encoding="utf-8") as f:
        idx = 1
        for seg in segments:
            txt = seg.get("translated_text", "").strip()
            if not txt:
                continue

            # Scale timestamps by 1.12 to match the accelerated video
            scaled_start = seg["start"] / SPEED_FACTOR
            scaled_end = (seg["start"] + seg.get("duration", seg["end"] - seg["start"])) / SPEED_FACTOR

            start_str = format_timestamp(scaled_start)
            end_str = format_timestamp(scaled_end)
            f.write(f"{idx}\n{start_str} --> {end_str}\n{txt}\n\n")
            idx += 1

# ==========================================
# 🚀 Entrypoint
# ==========================================
if __name__ == "__main__":
    v_url = sys.argv[1]
    tgt_lang = sys.argv[2] if len(sys.argv) > 2 else "hi"

    v_file, a_file, meta = download_media(v_url)
    segs = transcribe_and_translate(a_file, target_lang=tgt_lang)
    asyncio.run(synthesize_audio(segs))

    render_dubbed_video(v_file, segs, "final_output.mp4")
    generate_srt(segs, "subtitles.srt")
    print("✅ Finished rendering dub with zero drift and +12% speed increase.")
