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

SPEED_FACTOR = 1.12  # Uniform +12% speed increase across the entire video
MAX_ATEMPO_COMPRESSION = 1.15  # Max audio compression to preserve quality

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

    # Clean bitstream to prevent AAC / Scalefactor Decoder Crashes
    print("🧹 Sanitizing source video streams...")
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

    # Extract 16kHz mono audio for Whisper transcription
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
# 2. 🛡️ Translation & Filtering
# ==========================================
def contains_devanagari(text: str) -> bool:
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

    for attempt in range(3):
        try:
            res = GoogleTranslator(source='auto', target='hi').translate(clean_text)
            if res and not is_error_page(res) and contains_devanagari(res):
                time.sleep(random.uniform(0.1, 0.2))
                return res.strip()
        except Exception:
            time.sleep(0.3)

    try:
        res = MyMemoryTranslator(source='en-US', target='hi-IN').translate(clean_text)
        if res and not is_error_page(res) and contains_devanagari(res):
            return res.strip()
    except Exception:
        pass

    return ""

def transcribe_and_translate(audio_path: str, target_lang: str = "hi") -> list[dict]:
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    raw_segments, info = model.transcribe(audio_path, language=None, vad_filter=True)
    print(f"🌍 Detected source language: {info.language}")

    raw_list = []
    for s in raw_segments:
        t = s.text.strip()
        if t and (s.end - s.start >= 0.25):
            raw_list.append({"start": s.start, "end": s.end, "text": t})

    if not raw_list:
        return []

    # Merge tight speech fragments (< 0.2s pause)
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
        # Calculate available window until the next person speaks
        next_start = merged[i + 1]["start"] if i + 1 < len(merged) else s["end"] + 3.0
        available_window = max(0.5, next_start - s["start"])

        if target_lang == "hi":
            translated = translate_to_hindi(s["text"])
        else:
            try:
                translated = GoogleTranslator(source='auto', target=target_lang).translate(s["text"])
            except Exception:
                translated = s["text"]

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
            "available_window": available_window,
            "translated_text": translated,
            "target_lang": target_lang
        })

    return segments

# ==========================================
# 3. 🗣️ Speech Synthesis & Strict 1.15x Fit
# ==========================================
def apply_atempo(input_wav: str, speed: float, output_wav: str):
    cmd = [
        'ffmpeg', '-y', '-err_detect', 'ignore_err',
        '-i', input_wav,
        '-filter:a', f'atempo={speed:.4f}',
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
            continue

        try:
            # 1. Native Fast Generation (+20% limits reliance on artificial atempo)
            communicate = edge_tts.Communicate(text, voice, rate="+20%")
            await communicate.save(raw_tts_file)
            natural_dur = get_duration(raw_tts_file)
            
            # 2. Strict Capped Compression Logic
            ratio = natural_dur / seg["available_window"]
            
            if ratio > 1.0:
                # Limit compression to exactly 1.15x
                compress_factor = min(ratio, MAX_ATEMPO_COMPRESSION)
                apply_atempo(raw_tts_file, compress_factor, final_clip_file)
                seg["clip_file"] = final_clip_file
            else:
                seg["clip_file"] = raw_tts_file

        except Exception as e:
            print(f"⚠️ Synthesis error on segment {i}: {e}")
            seg["clip_file"] = None

# ==========================================
# 4. 🎬 Blazing Fast Single-Pass Assembly
# ==========================================
def render_dubbed_video(video_path: str, segments: list[dict], output_file: str):
    src_total_dur = get_duration(video_path)
    
    print("🎬 Building master audio track (No frame freezes, preserving video timeline)...")
    
    # Create a blank canvas matching the video duration
    master_track = AudioSegment.silent(duration=int((src_total_dur + 5.0) * 1000))
    audio_cursor = 0.0

    for seg in segments:
        if not seg.get("clip_file") or not os.path.exists(seg["clip_file"]):
            continue

        clip = AudioSegment.from_file(seg["clip_file"])
        clip_dur = len(clip) / 1000.0
        
        # Smart Anchoring: Start at original timestamp, or shift slightly if previous clip bled over
        start_time = max(seg["start"], audio_cursor)
        
        master_track = master_track.overlay(clip, position=int(start_time * 1000))
        
        # Advance cursor to prevent voice overlap
        audio_cursor = start_time + clip_dur
        
        # Save exact timestamps for SRT Subtitles
        seg["final_start"] = start_time
        seg["final_end"] = audio_cursor

    synced_audio_path = "synced_master.wav"
    master_track.export(synced_audio_path, format="wav")

    print(f"⚡ Merging Audio/Video with uniform +12% ({SPEED_FACTOR}x) boost...")
    
    # Single ffmpeg command (100x faster than chunking)
    cmd = [
        'ffmpeg', '-y', '-err_detect', 'ignore_err',
        '-i', video_path,
        '-i', synced_audio_path,
        '-filter_complex', f'[0:v]setpts=PTS/{SPEED_FACTOR},fps=30[v];[1:a]atempo={SPEED_FACTOR}[a]',
        '-map', '[v]', '-map', '[a]',
        '-map_metadata', '-1', '-map_chapters', '-1',
        '-fflags', '+bitexact', '-flags:v', '+bitexact', '-flags:a', '+bitexact',
        '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '2600k',
        '-c:a', 'aac', '-b:a', '192k',
        '-shortest', output_file
    ]
    subprocess.run(cmd, check=True)

    if os.path.exists(synced_audio_path):
        os.remove(synced_audio_path)
    if os.path.exists("temp_audio"):
        import shutil
        shutil.rmtree("temp_audio")

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
            if not txt or "final_start" not in seg:
                continue

            # Scale timestamps by 1.12 to match the accelerated output video
            scaled_start = seg["final_start"] / SPEED_FACTOR
            scaled_end = seg["final_end"] / SPEED_FACTOR

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
    print("✅ Completed fast single-pass render with exact timeline matching.")
