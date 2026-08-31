import os
import sys
import subprocess
import asyncio
import time
import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator
import edge_tts

# ==========================================
# 1. 📥 Download Video & Extract Audio
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

    print("📥 Downloading video...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    print("🎵 Extracting audio track...")
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
# 2. 🎙️ Transcription & Resilient Translation
# ==========================================
def transcribe_and_translate(audio_path: str, target_lang: str = "hi") -> list[dict]:
    print("🎙️ Transcribing audio with faster-whisper (tiny)...")
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    raw_segments, _ = model.transcribe(audio_path, language="en", vad_filter=True)

    translator = GoogleTranslator(source='en', target=target_lang)
    segments = []

    print(f"🌐 Translating segments into '{target_lang}'...")
    for s in raw_segments:
        text = s.text.strip()
        if not text:
            continue

        translated = text
        for attempt in range(3):
            try:
                result = translator.translate(text)
                if result:
                    translated = result
                    break
            except Exception:
                time.sleep(2)

        segments.append({
            "start": s.start,
            "end": s.end,
            "translated_text": translated
        })

    return segments

# ==========================================
# 3. 🗣️ Speech Synthesis
# ==========================================
async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    print("🗣️ Synthesizing voiceovers...")

    for i, seg in enumerate(segments):
        audio_file = os.path.join(temp_dir, f"audio_{i}.mp3")
        seg["audio_file"] = audio_file

        communicate = edge_tts.Communicate(seg["translated_text"], "hi-IN-MadhurNeural")
        await communicate.save(audio_file)
        seg["tts_dur"] = get_duration(audio_file)

# ==========================================
# 4. 🎬 Lightweight Stable Video Assembly
# ==========================================
def assemble_video(video_path: str, segments: list[dict], output_file: str):
    print("🎬 Assembling final video timeline safely...")
    
    filter_inputs = []
    filter_complex = ""

    for i, seg in enumerate(segments):
        filter_inputs.extend(['-i', seg['audio_file']])
        start_ms = int(seg['start'] * 1000)
        # Delay each TTS audio clip to match its original subtitle start timestamp
        filter_complex += f"[{i+1}:a]adelay={start_ms}|{start_ms}[a{i}];"

    audio_labels = "".join([f"[a{i}]" for i in range(len(segments))])
    # Mix all audio tracks together cleanly without cutting the video stream into pieces
    filter_complex += f"{audio_labels}amix=inputs={len(segments)}:normalize=0[aout]"

    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        *filter_inputs,
        '-filter_complex', filter_complex,
        '-map', '0:v',
        '-map', '[aout]',
        '-map_metadata', '-1',
        '-c:v', 'libx264',
        '-preset', 'ultrafast',
        '-c:a', 'aac',
        output_file
    ]
    
    print("✨ Running lightweight FFmpeg compilation...")
    subprocess.run(cmd, check=True)

# ==========================================
# 5. 📝 Subtitle (.srt) Generation
# ==========================================
def format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def generate_srt(segments: list[dict], output_file: str = "subtitles.srt"):
    print("📝 Generating synchronized SRT file...")
    with open(output_file, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            start_str = format_timestamp(seg["start"])
            end_str = format_timestamp(seg["end"])
            f.write(f"{i}\n{start_str} --> {end_str}\n{seg['translated_text']}\n\n")

if __name__ == "__main__":
    url = sys.argv[1]
    target_lang = sys.argv[2] if len(sys.argv) > 2 else "hi"

    v_file, a_file = download_media(url)
    segs = transcribe_and_translate(a_file, target_lang=target_lang)
    asyncio.run(synthesize_audio(segs))
    
    assemble_video(v_file, segs, "final_output.mp4")
    generate_srt(segs, "subtitles.srt")
