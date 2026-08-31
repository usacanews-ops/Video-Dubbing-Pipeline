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

# ==========================================
# 2. 🎙️ Transcription & Resilient Translation
# ==========================================
def transcribe_and_translate(audio_path: str, target_lang: str = "hi") -> list[dict]:
    print("🎙️ Transcribing audio with faster-whisper...")
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio_path, language="en", vad_filter=True)

    translator = GoogleTranslator(source='en', target=target_lang)
    transcript = []

    print(f"🌐 Translating segments into '{target_lang}'...")
    for s in segments:
        text = s.text.strip()
        if not text:
            continue

        translated = text  # Default to original English if translation completely fails
        
        # Retry loop to handle GitHub Actions IP rate-limiting
        for attempt in range(3):
            try:
                result = translator.translate(text)
                if result:
                    translated = result
                    break  # Success, exit the retry loop
            except Exception as e:
                print(f"⚠️ Translation attempt {attempt + 1} failed for: '{text[:20]}...' | Retrying in 2s...")
                time.sleep(2)  # Wait 2 seconds before retrying to clear the rate limit

        transcript.append({
            "start": s.start,
            "end": s.end,
            "duration": s.end - s.start,
            "translated_text": translated
        })

    print(f"✅ Prepared {len(transcript)} subtitle blocks.")
    return transcript

# ==========================================
# 3. 🗣️ Speech Synthesis & Dynamic Sync
# ==========================================
async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    print("🗣️ Synthesizing voiceover audio with edge-tts...")

    for i, seg in enumerate(segments):
        raw_file = os.path.join(temp_dir, f"raw_{i}.mp3")
        aligned_file = os.path.join(temp_dir, f"aligned_{i}.wav")
        seg["audio_file"] = aligned_file

        communicate = edge_tts.Communicate(seg["translated_text"], "hi-IN-MadhurNeural")
        await communicate.save(raw_file)

        probe_cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {raw_file}"
        gen_duration = float(subprocess.check_output(probe_cmd, shell=True).strip())
        target_duration = seg["duration"]

        speed_factor = max(0.7, min(gen_duration / target_duration, 2.0)) if target_duration > 0 else 1.0

        subprocess.run([
            'ffmpeg', '-y', '-i', raw_file,
            '-filter:a', f'atempo={speed_factor}',
            '-ar', '44100', '-ac', '2',
            aligned_file
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

# ==========================================
# 4. 🎬 Video Assembly
# ==========================================
def merge_video(video_path: str, segments: list[dict], output_file: str, video_speed: float):
    print("🎬 Mixing audio channels and compiling MP4...")
    filter_inputs = []
    filter_complex = ""

    for i, seg in enumerate(segments):
        filter_inputs.extend(['-i', seg["audio_file"]])
        start_ms = int(seg["start"] * 1000)
        filter_complex += f"[{i+1}:a]adelay={start_ms}|{start_ms}[a{i}];"

    inputs_str = "".join([f"[a{i}]" for i in range(len(segments))])
    filter_complex += f"{inputs_str}amix=inputs={len(segments)}:normalize=0[aout]"

    pts_factor = 1.0 / video_speed

    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        *filter_inputs,
        '-filter_complex', filter_complex,
        '-map', '0:v',
        '-map', '[aout]',
        '-vf', f'setpts={pts_factor}*PTS',
        '-map_metadata', '-1',
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-c:a', 'aac',
        output_file
    ]
    subprocess.run(cmd, check=True)
    print(f"✨ Rendering complete: {output_file}")

if __name__ == "__main__":
    url = sys.argv[1]
    target_lang = sys.argv[2] if len(sys.argv) > 2 else "hi"
    speed = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0

    v_file, a_file = download_media(url)
    segs = transcribe_and_translate(a_file, target_lang=target_lang)
    asyncio.run(synthesize_audio(segs))
    merge_video(v_file, segs, "final_output.mp4", speed)
