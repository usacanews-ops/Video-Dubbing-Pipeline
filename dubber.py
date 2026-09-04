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

SPEED_FACTOR = 1.12  # Uniform +12% speed increase across the final video
MAX_ATEMPO = 1.15    # Maximum artificial audio compression (Strict Constraint)

# ==========================================
# 1. 📥 Download & CFR Video Sanitization
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

    # 🛡️ Force Constant Frame Rate (CFR) to fix 30-second VFR audio drift
    print("🧹 Sanitizing stream (Forcing CFR to guarantee zero A-V drift)...")
    subprocess.run([
        'ffmpeg', '-y', '-err_detect', 'ignore_err',
        '-i', dl_path,
        '-r', '30', '-vsync', '1', '-async', '1',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k', '-ar', '44100',
        video_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    if os.path.exists(dl_path):
        os.remove(dl_path)

    print("🎵 Extracting synchronized 16kHz PCM audio for Whisper...")
    subprocess.run([
        'ffmpeg', '-y', '-err_detect', 'ignore_err',
        '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        audio_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    return video_path, audio_path, meta_dict

def get_duration(file_path: str) -> float:
    cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \"{file_path}\""
    return float(subprocess.check_output(cmd, shell=True).strip())

# ==========================================
# 2. 🛡️ Safe Translation Engine
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
            time.sleep(0.2)

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
    
    raw_list = []
    for s in raw_segments:
        t = s.text.strip()
        if t and (s.end - s.start >= 0.25):
            raw_list.append({"start": s.start, "end": s.end, "text": t})

    if not raw_list:
        return []

    # Merge very close fragments to give TTS a larger continuous window
    merged = [raw_list[0]]
    for s in raw_list[1:]:
        prev = merged[-1]
        if s["start"] - prev["end"] < 0.4:
            prev["end"] = max(prev["end"], s["end"])
            prev["text"] += " " + s["text"]
        else:
            merged.append(s)

    segments = []
    for i, s in enumerate(merged):
        next_start = merged[i + 1]["start"] if i + 1 < len(merged) else s["end"] + 3.0
        available_window = max(0.5, next_start - s["start"] - 0.05)

        if target_lang == "hi":
            translated = translate_to_hindi(s["text"])
            # Prevents TTS from speaking English in Indian accent
            if not contains_devanagari(translated):
                translated = ""
        else:
            try:
                translated = GoogleTranslator(source='auto', target=target_lang).translate(s["text"])
            except:
                translated = s["text"]

        segments.append({
            "start": s["start"],
            "end": s["end"],
            "available_window": available_window,
            "translated_text": translated,
            "target_lang": target_lang
        })

    return segments

# ==========================================
# 3. ✂️ Silence Stripping & TTS Generation
# ==========================================
def strip_dead_silence(input_wav: str, output_wav: str, threshold: int = -40, chunk: int = 10):
    """Shaves invisible dead air off TTS generated files to save valuable timeline space."""
    audio = AudioSegment.from_file(input_wav)
    start_trim = 0
    end_trim = len(audio)

    for i in range(0, len(audio), chunk):
        if audio[i:i+chunk].dBFS > threshold:
            start_trim = max(0, i - 15)
            break
            
    for i in range(len(audio)-chunk, 0, -chunk):
        if audio[i:i+chunk].dBFS > threshold:
            end_trim = min(len(audio), i + chunk + 15)
            break

    stripped = audio[start_trim:end_trim]
    stripped.export(output_wav, format="wav")

async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    for i, seg in enumerate(segments):
        raw_tts_file = os.path.join(temp_dir, f"raw_{i}.mp3")
        stripped_file = os.path.join(temp_dir, f"strip_{i}.wav")
        final_clip_file = os.path.join(temp_dir, f"clip_{i}.wav")
        
        text = seg["translated_text"].strip()
        lang = seg.get("target_lang", "hi")
        voice = "hi-IN-MadhurNeural" if lang == "hi" else "en-US-GuyNeural"

        if not text:
            seg["clip_file"] = None
            continue

        try:
            # Native fast generation (+20%) prevents chipmunking
            communicate = edge_tts.Communicate(text, voice, rate="+20%")
            await communicate.save(raw_tts_file)
            
            # Strip dead air
            strip_dead_silence(raw_tts_file, stripped_file)
            natural_dur = get_duration(stripped_file)
            
            # Compress max 1.15x if it exceeds available scene window
            ratio = natural_dur / seg["available_window"]
            if ratio > 1.0:
                compress = min(ratio, MAX_ATEMPO)
                subprocess.run([
                    'ffmpeg', '-y', '-err_detect', 'ignore_err',
                    '-i', stripped_file, '-filter:a', f'atempo={compress:.4f}', final_clip_file
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                seg["clip_file"] = final_clip_file
            else:
                seg["clip_file"] = stripped_file

        except Exception as e:
            print(f"⚠️ Synthesis error segment {i}: {e}")
            seg["clip_file"] = None

# ==========================================
# 4. 🎬 Strict Anchored Assembly (Zero Lag)
# ==========================================
def render_dubbed_video(video_path: str, segments: list[dict], output_file: str):
    src_total_dur = get_duration(video_path)
    
    print("🎬 Building master audio track (Strict Timestamp Anchoring)...")
    master_track = AudioSegment.silent(duration=int((src_total_dur + 5.0) * 1000))
    
    audio_cursor = 0.0

    for seg in segments:
        if not seg.get("clip_file") or not os.path.exists(seg["clip_file"]):
            continue

        clip = AudioSegment.from_file(seg["clip_file"])
        clip_dur = len(clip) / 1000.0
        
        orig_start = seg["start"]
        
        # Place clip back-to-back if previous bled, but NEVER allow >0.5s drift from visual
        start_time = max(orig_start, audio_cursor)
        if start_time - orig_start > 0.5:
            start_time = orig_start + 0.5 

        master_track = master_track.overlay(clip, position=int(start_time * 1000))
        audio_cursor = start_time + clip_dur
        
        seg["final_start"] = start_time
        seg["final_end"] = start_time + clip_dur

    synced_audio_path = "synced_master.wav"
    
    # Trim master track exactly to video length to prevent dead tail
    master_track = master_track[:int(src_total_dur * 1000)]
    master_track.export(synced_audio_path, format="wav")

    print(f"⚡ Single-Pass Encode: Synchronous +12% ({SPEED_FACTOR}x) Acceleration...")
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
# 5. 📝 Accurate Subtitles (.srt)
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

            scaled_start = seg["final_start"] / SPEED_FACTOR
            scaled_end = seg["final_end"] / SPEED_FACTOR

            f.write(f"{idx}\n{format_timestamp(scaled_start)} --> {format_timestamp(scaled_end)}\n{txt}\n\n")
            idx += 1

if __name__ == "__main__":
    v_url = sys.argv[1]
    tgt_lang = sys.argv[2] if len(sys.argv) > 2 else "hi"

    v_file, a_file, meta = download_media(v_url)
    segs = transcribe_and_translate(a_file, target_lang=tgt_lang)
    asyncio.run(synthesize_audio(segs))

    render_dubbed_video(v_file, segs, "final_output.mp4")
    generate_srt(segs, "subtitles.srt")
    print("✅ Completed fast single-pass render with exact timeline matching.")
