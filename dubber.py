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
    print("🎙️ Transcribing audio with faster-whisper...")
    model = WhisperModel("small", device="cpu", compute_type="int8")
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

    # Sanitize overlapping timestamps (Whisper sometimes bleeds them)
    for i in range(1, len(segments)):
        if segments[i]['start'] < segments[i-1]['end']:
            segments[i]['start'] = segments[i-1]['end']
            if segments[i]['end'] <= segments[i]['start']:
                segments[i]['end'] = segments[i]['start'] + 0.1

    return segments

# ==========================================
# 3. 🗣️ Speech Synthesis (Natural Speed)
# ==========================================
async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    print("🗣️ Synthesizing normal-speed voiceovers...")

    for i, seg in enumerate(segments):
        audio_file = os.path.join(temp_dir, f"audio_{i}.mp3")
        seg["audio_file"] = audio_file

        # No forced speed increase; let it speak naturally
        communicate = edge_tts.Communicate(seg["translated_text"], "hi-IN-MadhurNeural")
        await communicate.save(audio_file)

        seg["tts_dur"] = get_duration(audio_file)

# ==========================================
# 4. 🎬 Dynamic Video Stretching & Assembly
# ==========================================
def build_ffmpeg_timeline(video_path: str, segments: list[dict], output_file: str):
    print("🎬 Calculating dynamic timeline (slowing video to match audio)...")
    total_video_dur = get_duration(video_path)
    
    filter_lines = []
    video_concat_inputs = []
    part_idx = 0
    current_new_time = 0.0
    last_orig_end = 0.0

    for i, seg in enumerate(segments):
        start = seg['start']
        end = seg['end']
        tts_dur = seg['tts_dur']
        orig_dur = end - start

        # 1. Keep gap before segment at normal speed
        if start > last_orig_end:
            gap_dur = start - last_orig_end
            filter_lines.append(f"[0:v]trim=start={last_orig_end}:end={start},setpts=PTS-STARTPTS,fps=30[vpart{part_idx}];")
            video_concat_inputs.append(f"[vpart{part_idx}]")
            part_idx += 1
            current_new_time += gap_dur

        # 2. Process Dialogue Segment (Slow down video if TTS is longer)
        factor = 1.0
        if tts_dur > orig_dur:
            factor = tts_dur / orig_dur  # Calculate slow-motion factor
            actual_seg_dur = tts_dur
        else:
            actual_seg_dur = orig_dur

        filter_lines.append(f"[0:v]trim=start={start}:end={end},setpts={factor}*(PTS-STARTPTS),fps=30[vpart{part_idx}];")
        video_concat_inputs.append(f"[vpart{part_idx}]")
        part_idx += 1

        # Save the new timestamps for the SRT file
        seg['new_start'] = current_new_time
        seg['new_end'] = current_new_time + tts_dur

        current_new_time += actual_seg_dur
        last_orig_end = end

    # Add remaining video at the end
    if last_orig_end < total_video_dur:
        filter_lines.append(f"[0:v]trim=start={last_orig_end}:end={total_video_dur},setpts=PTS-STARTPTS,fps=30[vpart{part_idx}];")
        video_concat_inputs.append(f"[vpart{part_idx}]")
        part_idx += 1

    # Concatenate all video parts
    concat_v = "".join(video_concat_inputs)
    filter_lines.append(f"{concat_v}concat=n={part_idx}:v=1:a=0[vout];")

    # Map audio files to new timestamps
    audio_inputs = []
    for i, seg in enumerate(segments):
        start_ms = int(seg['new_start'] * 1000)
        input_idx = i + 1  # 0 is video
        filter_lines.append(f"[{input_idx}:a]adelay={start_ms}|{start_ms}[a{i}];")
        audio_inputs.append(f"[a{i}]")

    amix_str = "".join(audio_inputs)
    filter_lines.append(f"{amix_str}amix=inputs={len(audio_inputs)}:normalize=0[aout];")

    # Save filter script locally
    script_path = "filter_script.txt"
    with open(script_path, "w") as f:
        f.write("\n".join(filter_lines))

    # Run FFmpeg compilation
    print("✨ Mixing audio and encoding final MP4...")
    cmd = ['ffmpeg', '-y', '-i', video_path]
    for seg in segments:
        cmd.extend(['-i', seg['audio_file']])

    cmd.extend([
        '-filter_complex_script', script_path,
        '-map', '[vout]',
        '-map', '[aout]',
        '-map_metadata', '-1',
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-c:a', 'aac',
        output_file
    ])
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
            start_str = format_timestamp(seg["new_start"])
            end_str = format_timestamp(seg["new_end"])
            f.write(f"{i}\n{start_str} --> {end_str}\n{seg['translated_text']}\n\n")

if __name__ == "__main__":
    url = sys.argv[1]
    target_lang = sys.argv[2] if len(sys.argv) > 2 else "hi"

    v_file, a_file = download_media(url)
    segs = transcribe_and_translate(a_file, target_lang=target_lang)
    asyncio.run(synthesize_audio(segs))
    
    # Render stretched video timeline first
    build_ffmpeg_timeline(v_file, segs, "final_output.mp4")
    
    # Generate SRT last, so it reads the new timestamps calculated during video stretch
    generate_srt(segs, "subtitles.srt")
