import os
import sys
import subprocess
import asyncio
import time
import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator
import edge_tts
from pydub import AudioSegment
from deep_translator import GoogleTranslator, MyMemoryTranslator
# ==========================================
# 1. Download Media & Extract Audio
# ==========================================
def download_media(url: str) -> tuple[str, str]:
    video_path = "input_video.mp4"
    audio_path = "input_audio.wav"

    for path in [video_path, audio_path]:
        if os.path.exists(path):
            os.remove(path)

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': video_path,
        'quiet': False,
        'no_warnings': True,
    }

    print("Downloading video...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    print("Extracting audio track...")
    subprocess.run([
        'ffmpeg', '-y', '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        audio_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    return video_path, audio_path

def get_duration(file_path: str) -> float:
    cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {file_path}"
    return float(subprocess.check_output(cmd, shell=True).strip())

# ==========================================
# 2. Transcription & Translation
# ==========================================
def translate_text_robust(text: str, target_lang: str) -> str:
    """Attempts multiple free translation backends to bypass cloud IP blocks."""
    # Attempt 1: Google Translator
    try:
        res = GoogleTranslator(source='auto', target=target_lang).translate(text)
        if res and res.strip() and res.strip() != text.strip():
            return res
    except Exception as e:
        print(f"⚠️ Google Translate failed: {e}")

    # Attempt 2: MyMemory Translator (Different API provider)
    try:
        res = MyMemoryTranslator(source='en', target=target_lang).translate(text)
        if res and res.strip() and res.strip() != text.strip():
            return res
    except Exception as e:
        print(f"⚠️ MyMemory Translate failed: {e}")

    # Attempt 3: Retry with basic cleanup
    try:
        clean_text = text.replace('"', '').replace("'", "")
        res = GoogleTranslator(source='en', target=target_lang).translate(clean_text)
        if res and res.strip():
            return res
    except Exception:
        pass

    print(f"❌ All translators failed for: '{text[:30]}...'")
    return text

def transcribe_and_translate(audio_path: str, target_lang: str = "hi") -> list[dict]:
    print("🎙️ Transcribing audio with faster-whisper...")
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    raw_segments, _ = model.transcribe(audio_path, language="en", vad_filter=True)

    raw_list = []
    for s in raw_segments:
        text = s.text.strip()
        if text and (s.end - s.start >= 0.2):
            raw_list.append({"start": s.start, "end": s.end, "text": text})

    if not raw_list:
        print("⚠️ No speech detected in audio.")
        return []

    # Merge consecutive micro-segments (< 0.5s) for natural sentence flow
    merged = [raw_list[0]]
    for s in raw_list[1:]:
        prev = merged[-1]
        if s["start"] - prev["end"] < 0.5:
            prev["end"] = max(prev["end"], s["end"])
            prev["text"] += " " + s["text"]
        else:
            merged.append(s)

    segments = []
    print(f"🌐 Translating {len(merged)} dialogue chunks into '{target_lang}'...")
    for i, s in enumerate(merged, start=1):
        translated = translate_text_robust(s["text"], target_lang)
        print(f"[{i}/{len(merged)}] EN: {s['text']}")
        print(f"       -> HI: {translated}")

        segments.append({
            "orig_start": s["start"],
            "orig_end": s["end"],
            "translated_text": translated
        })

    return segments
# ==========================================
# 3. Speech Synthesis
# ==========================================
async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    print("Synthesizing voiceover clips...")

    for i, seg in enumerate(segments):
        audio_file = os.path.join(temp_dir, f"audio_{i}.mp3")
        seg["audio_file"] = audio_file

        communicate = edge_tts.Communicate(seg["translated_text"], "hi-IN-MadhurNeural", rate="+15%")
        await communicate.save(audio_file)
        seg["tts_dur"] = get_duration(audio_file)

# ==========================================
# 4. Global Timeline Sync & Render
# ==========================================
def render_dubbed_video(video_path: str, segments: list[dict], output_file: str):
    print("Aligning timeline and building non-overlapping audio master...")
    orig_total_dur = get_duration(video_path)

    # 1. Enforce zero-overlap sequential audio timeline
    timeline_cursor = 0.0
    for seg in segments:
        # Dialogue starts at scheduled time or right after previous dialogue ends
        start_time = max(seg["orig_start"], timeline_cursor)
        seg["new_start"] = start_time
        seg["new_end"] = start_time + seg["tts_dur"]
        
        # 0.1s padding prevents edge-to-edge audio collision
        timeline_cursor = seg["new_end"] + 0.1

    final_audio_dur = max(orig_total_dur, timeline_cursor)

    # 2. Build single master audio file
    master_audio = AudioSegment.silent(duration=int((final_audio_dur + 1.0) * 1000))
    for seg in segments:
        clip = AudioSegment.from_file(seg["audio_file"])
        pos_ms = int(seg["new_start"] * 1000)
        master_audio = master_audio.overlay(clip, position=pos_ms)

    master_audio_path = "master_output_audio.wav"
    master_audio.export(master_audio_path, format="wav")

    # 3. Calculate video speed stretch factor
    stretch_factor = final_audio_dur / orig_total_dur if orig_total_dur > 0 else 1.0
    print(f"Applying video stretch factor: {stretch_factor:.4f} (Original: {orig_total_dur:.1f}s -> Dubbed: {final_audio_dur:.1f}s)")

    # 4. Single-pass FFmpeg encoding (no trim scripts, zero risk of exit status 8)
    print("Rendering final MP4...")
    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        '-i', master_audio_path,
        '-filter_complex', f'[0:v]setpts={stretch_factor:.5f}*PTS,fps=30[vout]',
        '-map', '[vout]',
        '-map', '1:a',
        '-map_metadata', '-1',
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-b:v', '2500k',
        '-maxrate', '3000k',
        '-bufsize', '6000k',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-shortest',
        output_file
    ]
    subprocess.run(cmd, check=True)

# ==========================================
# 5. Subtitle (.srt) Generation
# ==========================================
def format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def generate_srt(segments: list[dict], output_file: str = "subtitles.srt"):
    print("Generating synchronized SRT file...")
    with open(output_file, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            start_str = format_timestamp(seg["new_start"])
            end_str = format_timestamp(seg["new_end"])
            f.write(f"{i}\n{start_str} --> {end_str}\n{seg['translated_text']}\n\n")

# ==========================================
# Entrypoint
# ==========================================
if __name__ == "__main__":
    url = sys.argv[1]
    target_lang = sys.argv[2] if len(sys.argv) > 2 else "hi"

    v_file, a_file = download_media(url)
    segs = transcribe_and_translate(a_file, target_lang=target_lang)
    asyncio.run(synthesize_audio(segs))
    
    render_dubbed_video(v_file, segs, "final_output.mp4")
    generate_srt(segs, "subtitles.srt")
    print("Process finished successfully.")
