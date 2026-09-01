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
# 2. 🎙️ Transcription & Translation
# ==========================================
def transcribe_and_translate(audio_path: str, target_lang: str = "hi") -> list[dict]:
    print("🎙️ Transcribing audio with faster-whisper...")
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    raw_segments, _ = model.transcribe(audio_path, language="en", vad_filter=True)

    translator = GoogleTranslator(source='en', target=target_lang)
    raw_list = []

    for s in raw_segments:
        text = s.text.strip()
        if text and (s.end - s.start >= 0.2):
            raw_list.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})

    if not raw_list:
        return []

    # Merge consecutive micro-segments (< 0.3s apart) to prevent 0-length video slices
    merged = [raw_list[0]]
    for s in raw_list[1:]:
        prev = merged[-1]
        if s["start"] - prev["end"] < 0.3:
            prev["end"] = max(prev["end"], s["end"])
            prev["text"] += " " + s["text"]
        else:
            merged.append(s)

    # Translate merged blocks
    segments = []
    print(f"🌐 Translating {len(merged)} segments into '{target_lang}'...")
    for s in merged:
        translated = s["text"]
        for _ in range(3):
            try:
                res = translator.translate(s["text"])
                if res:
                    translated = res
                    break
            except Exception:
                time.sleep(1)

        segments.append({
            "orig_start": s["start"],
            "orig_end": s["end"],
            "orig_dur": s["end"] - s["start"],
            "translated_text": translated
        })

    return segments

# ==========================================
# 3. 🗣️ Speech Synthesis (+15% Rate)
# ==========================================
async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    print("🗣️ Synthesizing voiceovers (Speed +15%)...")

    for i, seg in enumerate(segments):
        audio_file = os.path.join(temp_dir, f"audio_{i}.mp3")
        seg["audio_file"] = audio_file

        communicate = edge_tts.Communicate(seg["translated_text"], "hi-IN-MadhurNeural", rate="+15%")
        await communicate.save(audio_file)

        seg["tts_dur"] = get_duration(audio_file)

# ==========================================
# 4. 🎬 Sequential Timeline Engine (Zero Overlap & Valid Filters)
# ==========================================
def build_ffmpeg_timeline(video_path: str, segments: list[dict], output_file: str):
    print("🎬 Building robust video timeline...")
    total_video_dur = get_duration(video_path)

    filter_lines = []
    video_concat_inputs = []
    part_idx = 0

    current_timeline_time = 0.0
    last_orig_end = 0.0

    for i, seg in enumerate(segments):
        orig_start = seg["orig_start"]
        orig_end = seg["orig_end"]
        tts_dur = seg["tts_dur"]
        orig_dur = max(0.1, orig_end - orig_start)

        # 1. Non-speech gap (only if gap is >= 0.1s to prevent 0-length filter errors)
        if orig_start - last_orig_end >= 0.1:
            gap_dur = orig_start - last_orig_end
            filter_lines.append(f"[0:v]trim=start={last_orig_end:.2f}:end={orig_start:.2f},setpts=PTS-STARTPTS,fps=30[vpart{part_idx}];")
            video_concat_inputs.append(f"[vpart{part_idx}]")
            part_idx += 1
            current_timeline_time += gap_dur

        # 2. Speech segment: ensure video covers full audio + 0.1s safety padding
        actual_seg_dur = max(orig_dur, tts_dur + 0.1)
        stretch_factor = actual_seg_dur / orig_dur

        filter_lines.append(f"[0:v]trim=start={orig_start:.2f}:end={orig_end:.2f},setpts={stretch_factor:.4f}*(PTS-STARTPTS),fps=30[vpart{part_idx}];")
        video_concat_inputs.append(f"[vpart{part_idx}]")
        part_idx += 1

        # Strict non-overlapping placement
        seg["new_start"] = current_timeline_time
        seg["new_end"] = current_timeline_time + tts_dur

        current_timeline_time += actual_seg_dur
        last_orig_end = orig_end

    # Trailing tail
    if total_video_dur - last_orig_end >= 0.1:
        filter_lines.append(f"[0:v]trim=start={last_orig_end:.2f}:end={total_video_dur:.2f},setpts=PTS-STARTPTS,fps=30[vpart{part_idx}];")
        video_concat_inputs.append(f"[vpart{part_idx}]")
        part_idx += 1

    concat_v = "".join(video_concat_inputs)
    filter_lines.append(f"{concat_v}concat=n={part_idx}:v=1:a=0[vout];")

    script_path = "filter_script.txt"
    with open(script_path, "w") as f:
        f.write("\n".join(filter_lines))

    # 🎛️ Audio Assembly
    print("🎧 Pre-rendering audio master track...")
    total_audio_ms = int((current_timeline_time + 2.0) * 1000)
    master_audio = AudioSegment.silent(duration=total_audio_ms)

    for seg in segments:
        seg_audio = AudioSegment.from_file(seg["audio_file"])
        start_ms = int(seg["new_start"] * 1000)
        master_audio = master_audio.overlay(seg_audio, position=start_ms)

    master_audio_path = "master_output_audio.wav"
    master_audio.export(master_audio_path, format="wav")

    # 🚀 Encode MP4
    print("✨ Rendering final synchronized video...")
    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        '-i', master_audio_path,
        '-filter_complex_script', script_path,
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
        output_file
    ]
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
    
    build_ffmpeg_timeline(v_file, segs, "final_output.mp4")
    generate_srt(segs, "subtitles.srt")
    print("🎉 Pipeline completed successfully!")
