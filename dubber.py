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

SPEED_FACTOR = 1.12   # Uniform +12% speed increase on final video & audio
MAX_ATEMPO = 1.15     # Maximum compression allowed (Strict limit: <= 15%)

# ==========================================
# 1. 📥 Download Media & Extract Audio
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
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
    }

    print("📥 Downloading media stream...")
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

    # Extract 16kHz mono audio cleanly for Whisper
    print("🎵 Extracting synchronized 16kHz PCM audio...")
    subprocess.run([
        'ffmpeg', '-y', '-err_detect', 'ignore_err',
        '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        audio_path
    ], check=True)

    return video_path, audio_path, meta_dict

def get_duration(file_path: str) -> float:
    cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \"{file_path}\""
    return float(subprocess.check_output(cmd, shell=True).strip())

# ==========================================
# 2. 🛡️ Translation Engine
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

    for _ in range(3):
        try:
            res = GoogleTranslator(source='auto', target='hi').translate(clean_text)
            if res and not is_error_page(res) and contains_devanagari(res):
                time.sleep(random.uniform(0.1, 0.2))
                return res.strip()
        except Exception:
            time.sleep(0.25)

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
    print(f"🌍 Detected language: {info.language}")

    raw_list = []
    for s in raw_segments:
        t = s.text.strip()
        if t and (s.end - s.start >= 0.25):
            raw_list.append({"start": s.start, "end": s.end, "text": t})

    if not raw_list:
        return []

    # Merge tight fragments into full sentences to prevent fragmented translation bloat
    merged = [raw_list[0]]
    for s in raw_list[1:]:
        prev = merged[-1]
        if s["start"] - prev["end"] < 0.8:
            prev["end"] = max(prev["end"], s["end"])
            prev["text"] += " " + s["text"]
        else:
            merged.append(s)

    segments = []
    first_preview = False

    for i, s in enumerate(merged):
        next_start = merged[i + 1]["start"] if i + 1 < len(merged) else s["end"] + 4.0
        available_window = max(0.5, next_start - s["start"] - 0.05)

        if target_lang == "hi":
            translated = translate_to_hindi(s["text"])
            if not contains_devanagari(translated):
                translated = ""
        else:
            try:
                translated = GoogleTranslator(source='auto', target=target_lang).translate(s["text"])
            except Exception:
                translated = s["text"]

        if not first_preview and translated:
            words = translated.strip().split()
            short_p = " ".join(words[:5]) + ("..." if len(words) > 5 else "")
            print(f"TRANSLATION_PREVIEW: {short_p}")
            first_preview = True

        segments.append({
            "start": s["start"],
            "end": s["end"],
            "available_window": available_window,
            "translated_text": translated,
            "target_lang": target_lang
        })

    return segments

# ==========================================
# 3. ✂️ Speech Synthesis & Dead Air Removal
# ==========================================
def strip_dead_silence(input_wav: str, output_wav: str, threshold: int = -42, chunk: int = 10):
    """Trims synthetic silence padding added by TTS engines."""
    audio = AudioSegment.from_file(input_wav)
    start_trim = 0
    end_trim = len(audio)

    for i in range(0, len(audio), chunk):
        if audio[i:i + chunk].dBFS > threshold:
            start_trim = max(0, i - 15)
            break

    for i in range(len(audio) - chunk, 0, -chunk):
        if audio[i:i + chunk].dBFS > threshold:
            end_trim = min(len(audio), i + chunk + 15)
            break

    stripped = audio[start_trim:end_trim]
    stripped.export(output_wav, format="wav")

async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    for i, seg in enumerate(segments):
        raw_tts = os.path.join(temp_dir, f"raw_{i}.mp3")
        stripped = os.path.join(temp_dir, f"strip_{i}.wav")
        final_clip = os.path.join(temp_dir, f"clip_{i}.wav")

        text = seg["translated_text"].strip()
        lang = seg.get("target_lang", "hi")
        voice = "hi-IN-MadhurNeural" if lang == "hi" else "en-US-GuyNeural"

        if not text:
            seg["clip_file"] = None
            continue

        try:
            # Native +20% fast articulation
            communicate = edge_tts.Communicate(text, voice, rate="+20%")
            await communicate.save(raw_tts)

            strip_dead_silence(raw_tts, stripped)
            dur = get_duration(stripped)

            ratio = dur / seg["available_window"]
            if ratio > 1.0:
                # Cap compression strictly at 1.15x
                compress = min(ratio, MAX_ATEMPO)
                subprocess.run([
                    'ffmpeg', '-y', '-err_detect', 'ignore_err',
                    '-i', stripped,
                    '-filter:a', f'atempo={compress:.4f}',
                    final_clip
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                seg["clip_file"] = final_clip
            else:
                seg["clip_file"] = stripped

        except Exception as e:
            print(f"⚠️ Synthesis error on segment {i}: {e}")
            seg["clip_file"] = None

# ==========================================
# 4. 🎬 Strict Anchor Assembly (Zero Cumulative Lag)
# ==========================================
def render_dubbed_video(video_path: str, segments: list[dict], output_file: str):
    src_total_dur = get_duration(video_path)
    print(f"🎬 Assembling synchronized master audio track ({src_total_dur:.2f}s)...")

    master_track = AudioSegment.silent(duration=int((src_total_dur + 2.0) * 1000))
    audio_cursor = 0.0

    for seg in segments:
        if not seg.get("clip_file") or not os.path.exists(seg["clip_file"]):
            continue

        clip = AudioSegment.from_file(seg["clip_file"])
        clip_dur = len(clip) / 1000.0

        orig_start = seg["start"]

        # If previous dialogue bled over slightly, place back-to-back.
        # But if the scene had a pause, SNAP BACK to original scene timing immediately.
        start_time = max(orig_start, audio_cursor)
        if start_time - orig_start > 0.4:
            start_time = orig_start + 0.4

        master_track = master_track.overlay(clip, position=int(start_time * 1000))
        audio_cursor = start_time + clip_dur

        seg["final_start"] = start_time
        seg["final_end"] = start_time + clip_dur

    synced_audio_path = "synced_master.wav"
    # Cut exactly at video end to guarantee no trailing silence
    master_track = master_track[:int(src_total_dur * 1000)]
    master_track.export(synced_audio_path, format="wav")

    print(f"⚡ Applying uniform +12% ({SPEED_FACTOR}x) acceleration...")
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
