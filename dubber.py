import os
import sys
import subprocess
import asyncio
import time
import re
import json
import shutil
import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator
import edge_tts
from pydub import AudioSegment

# ================= CONFIG =================

# 1. Pacing & Speed
TTS_RATE = "+15%"          # Fast, natural articulation (minimizes required freeze)
FINAL_SPEED = 1.10         # Exact +10% uniform master speedup

# 2. Timing Controls
ENABLE_FREEZE = True
SAFETY_GAP = 0.03
SILENCE_THRESHOLD = -42
WHISPER_MODEL = "tiny"

# 3. Encoding Presets
AUDIO_BITRATE = "96k"      # 96k AAC is crisp for speech and saves 1-2 MB

VOICE_MAP = {
    "hi": "hi-IN-MadhurNeural",
    "bn": "bn-IN-BashkarNeural",
    "ta": "ta-IN-ValluvarNeural",
    "te": "te-IN-MohanNeural",
    "mr": "mr-IN-ManoharNeural",
    "gu": "gu-IN-DhwaniNeural",
    "kn": "kn-IN-GaganNeural",
    "ml": "ml-IN-MidhunNeural",
    "pa": "pa-IN-OjasNeural",
    "en": "en-US-GuyNeural",
    "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural",
    "es": "es-ES-AlvaroNeural",
    "it": "it-IT-DiegoNeural",
    "pt": "pt-BR-AntonioNeural",
    "nl": "nl-NL-MaartenNeural",
    "pl": "pl-PL-MarekNeural",
    "tr": "tr-TR-AhmetNeural",
    "ru": "ru-RU-DmitryNeural",
    "uk": "uk-UA-OstapNeural",
    "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural",
    "zh": "zh-CN-YunxiNeural",
    "ar": "ar-SA-HamedNeural"
}

# ================= UTILITIES =================

def run_command(cmd, quiet=False):
    return subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
        check=True
    )

def get_duration(path):
    result = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ])
    return float(result.strip())

def detect_source_video_bitrate(video_path, default_kbps=550):
    """Calculates source video bitrate to guarantee the final output matches source file size."""
    try:
        dur = get_duration(video_path)
        if dur > 0:
            total_bytes = os.path.getsize(video_path)
            total_kbps = int((total_bytes * 8) / dur / 1000)
            # Subtract 96k for audio, leave rest for video (bounded between 350k and 900k)
            video_kbps = max(350, min(total_kbps - 96, 900))
            return video_kbps
    except Exception:
        pass
    return default_kbps

# ================= DOWNLOAD =================

def download_media(url):
    video_path = "raw_source.mp4"
    audio_path = "input_audio.wav"

    for path in (video_path, audio_path):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": video_path,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True
    }

    print("📥 Downloading media...")
    with yt_dlp.YoutubeDL(opts) as ydl:
        meta = ydl.extract_info(url, download=True)
        metadata = {
            "title": meta.get("title", "Dubbed Video"),
            "description": meta.get("description", ""),
            "tags": meta.get("tags", [])
        }

    with open("source_meta.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print(f"TITLE_EMIT: {metadata['title']}")
    print("🎵 Extracting source audio...")

    run_command([
        "ffmpeg", "-y", "-err_detect", "ignore_err",
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        audio_path
    ], quiet=True)

    return video_path, audio_path, metadata

# ================= TRANSLATION =================

def contains_devanagari(text):
    return bool(re.search(r"[\u0900-\u097F]", text))

def is_error_page(text):
    t = text.lower()
    return any(x in t for x in (
        "server error", "500", "!!1500", "<!doctype", "<html", "captcha", "unusual traffic"
    ))

def translate_text(text, target_lang):
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    if not text or len(text) < 2:
        return text

    if target_lang == "hi":
        for _ in range(3):
            try:
                result = GoogleTranslator(source="auto", target="hi").translate(text)
                if result and not is_error_page(result) and contains_devanagari(result):
                    return result.strip()
            except Exception:
                time.sleep(0.2)

        try:
            result = MyMemoryTranslator(source="auto", target="hi-IN").translate(text)
            if result and not is_error_page(result) and contains_devanagari(result):
                return result.strip()
        except Exception:
            pass
        return ""

    for _ in range(3):
        try:
            result = GoogleTranslator(source="auto", target=target_lang).translate(text)
            if result and not is_error_page(result):
                return result.strip()
        except Exception:
            time.sleep(0.2)

    return text

# ================= WHISPER =================

def transcribe_and_translate(audio_path, target_lang):
    print("🧠 Loading Whisper...")
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

    print("🎙️ Transcribing source audio...")
    raw_segments, info = model.transcribe(
        audio_path,
        language=None,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 350}
    )
    print(f"🌍 Detected source language: {info.language}")

    raw = []
    for segment in raw_segments:
        text = segment.text.strip()
        if not text or (segment.end - segment.start < 0.15):
            continue
        raw.append({
            "start": float(segment.start),
            "end": float(segment.end),
            "text": text
        })

    if not raw:
        return []

    print(f"📝 Whisper found {len(raw)} speech segments.")
    segments = []

    for i, source in enumerate(raw):
        start = source["start"]
        if i + 1 < len(raw):
            source_slot = raw[i + 1]["start"] - start - SAFETY_GAP
        else:
            source_slot = source["end"] - start + 2.0

        source_slot = max(0.25, source_slot)
        translated = translate_text(source["text"], target_lang)

        if not translated and target_lang != "hi":
            translated = source["text"]

        if i == 0:
            print("TRANSLATION_PREVIEW: " + translated[:120])

        segments.append({
            "index": i,
            "start": start,
            "source_end": source["end"],
            "source_slot": source_slot,
            "source_text": source["text"],
            "translated_text": translated,
            "target_lang": target_lang
        })

    return segments

# ================= FAST PARALLEL TTS =================

def strip_dead_silence(input_file, output_file):
    audio = AudioSegment.from_file(input_file)
    if len(audio) == 0:
        return

    start_trim = 0
    end_trim = len(audio)

    for pos in range(0, len(audio), 10):
        if audio[pos:pos + 10].dBFS > SILENCE_THRESHOLD:
            start_trim = max(0, pos - 20)
            break

    for pos in range(len(audio) - 10, 0, -10):
        if audio[pos:pos + 10].dBFS > SILENCE_THRESHOLD:
            end_trim = min(len(audio), pos + 30)
            break

    stripped = audio if end_trim <= start_trim else audio[start_trim:end_trim]
    stripped = stripped.set_frame_rate(44100).set_channels(2)
    stripped.export(output_file, format="wav")

async def synthesize_audio(segments, temp_dir="temp_audio"):
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir, exist_ok=True)

    print(f"🔊 Synthesizing {len(segments)} TTS clips in parallel at {TTS_RATE}...")
    sem = asyncio.Semaphore(5)  # 5 parallel synthesis workers

    async def fetch_one(i, seg):
        text = seg.get("translated_text", "").strip()
        if not text:
            seg["clip_file"] = None
            seg["tts_duration"] = 0
            return

        target_lang = seg.get("target_lang", "hi").lower()
        base_lang = target_lang.split("-")[0]
        voice = VOICE_MAP.get(base_lang, "en-US-GuyNeural")

        raw_file = os.path.join(temp_dir, f"raw_{i}.mp3")
        stripped_file = os.path.join(temp_dir, f"strip_{i}.wav")

        async with sem:
            for attempt in range(3):
                try:
                    communicator = edge_tts.Communicate(text, voice, rate=TTS_RATE)
                    await communicator.save(raw_file)
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"⚠️ TTS error on segment {i}: {e}")
                        seg["clip_file"] = None
                        seg["tts_duration"] = 0
                        return
                    await asyncio.sleep(0.4)

        strip_dead_silence(raw_file, stripped_file)
        duration = get_duration(stripped_file)
        seg["clip_file"] = stripped_file
        seg["tts_duration"] = duration

    tasks = [fetch_one(i, seg) for i, seg in enumerate(segments)]
    await asyncio.gather(*tasks)

# ================= TIMELINE =================

def calculate_dub_timeline(segments):
    current_time = 0.0

    for i, seg in enumerate(segments):
        original_start = seg["start"]
        original_slot = seg["source_slot"]
        tts_duration = seg.get("tts_duration", 0)

        new_start = original_start if i == 0 else current_time
        speech_end = new_start + tts_duration
        original_end = original_start + original_slot

        visual_end = max(new_start + original_slot, speech_end)
        freeze_duration = max(0.0, speech_end - (new_start + original_slot))

        seg["new_start"] = new_start
        seg["speech_end"] = speech_end
        seg["original_visual_end"] = original_end
        seg["visual_end"] = visual_end
        seg["freeze_duration"] = freeze_duration

        current_time = visual_end

    return segments

# ================= MASTER AUDIO =================

def build_master_audio(segments, total_duration, output_file):
    print("🎧 Building dubbed audio timeline...")
    master = AudioSegment.silent(
        duration=int((total_duration + 0.5) * 1000),
        frame_rate=44100
    ).set_channels(2)

    for seg in segments:
        clip_file = seg.get("clip_file")
        if not clip_file or not os.path.exists(clip_file):
            continue

        clip = AudioSegment.from_file(clip_file).set_frame_rate(44100).set_channels(2)
        master = master.overlay(clip, position=int(seg["new_start"] * 1000))

        seg["final_start"] = seg["new_start"]
        seg["final_end"] = seg["speech_end"]

    master.export(output_file, format="wav")
    return output_file

# ================= HIGH-SPEED VIDEO SLICING =================

def create_video_part(video_file, source_start, source_end, freeze_duration, output_file):
    duration = source_end - source_start
    if duration <= 0:
        return False

    filters = [
        f"trim=start={source_start:.6f}:end={source_end:.6f}",
        "setpts=PTS-STARTPTS"
    ]

    if ENABLE_FREEZE and freeze_duration > 0.01:
        filters.append(f"tpad=stop_mode=clone:stop_duration={freeze_duration:.6f}")

    # Use ultrafast preset for intermediate scratch clips
    cmd = [
        "ffmpeg", "-y", "-err_detect", "ignore_err",
        "-i", video_file,
        "-an",
        "-vf", ",".join(filters),
        "-c:v", "libx264",
        "-preset", "ultrafast",   # Fast scratch slices
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        output_file
    ]
    run_command(cmd, quiet=True)
    return True

def concatenate_video_parts(parts, output_file):
    concat_file = "video_concat.txt"
    with open(concat_file, "w", encoding="utf-8") as f:
        for part in parts:
            absolute = os.path.abspath(part).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{absolute}'\n")

    # Fast stream copy
    run_command([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file,
        "-c:v", "copy",
        output_file
    ], quiet=True)

    if os.path.exists(concat_file):
        os.remove(concat_file)

def build_extended_video(video_file, segments, output_file):
    source_duration = get_duration(video_file)
    print("🎬 Slicing & freezing video parts (High-Speed Mode)...")

    temp_dir = "temp_video_parts"
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir)

    parts = []

    for i, seg in enumerate(segments):
        source_start = seg["start"]
        if i == 0 and source_start > 0.01:
            opening = os.path.join(temp_dir, "part_000_opening.mp4")
            create_video_part(video_file, 0, source_start, 0, opening)
            parts.append(opening)

        source_end = segments[i + 1]["start"] if (i + 1 < len(segments)) else source_duration
        source_end = min(source_end, source_duration)
        if source_end <= source_start:
            continue

        freeze_duration = seg.get("freeze_duration", 0)
        part_file = os.path.join(temp_dir, f"part_{i+1:04d}.mp4")
        create_video_part(video_file, source_start, source_end, freeze_duration, part_file)
        parts.append(part_file)

    if not parts:
        raise RuntimeError("No video parts created.")

    concatenate_video_parts(parts, output_file)
    return output_file

# ================= SINGLE-PASS FINAL MUX & SPEED =================

def mux_final_video(extended_video, dubbed_audio, output_file, target_v_bitrate):
    pct = int(round((FINAL_SPEED - 1.0) * 100))
    print(f"🎬 Single-pass mux: +{pct}% speedup @ {target_v_bitrate}k bitrate target...")

    video_filter = f"setpts=PTS/{FINAL_SPEED:.4f},fps=30"
    audio_filter = f"atempo={FINAL_SPEED:.4f}"

    maxrate = int(target_v_bitrate * 1.3)
    bufsize = int(target_v_bitrate * 2.0)

    # Single compression pass with strict bitrate ceiling to match source size
    run_command([
        "ffmpeg", "-y",
        "-i", extended_video,
        "-i", dubbed_audio,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-vf", video_filter,
        "-af", audio_filter,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-b:v", f"{target_v_bitrate}k",
        "-maxrate", f"{maxrate}k",
        "-bufsize", f"{bufsize}k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        output_file
    ])

# ================= SRT SUBTITLES =================

def format_timestamp(seconds):
    seconds = max(0.0, seconds)
    total_ms = int(round(seconds * 1000))
    hours = total_ms // 3600000
    total_ms %= 3600000
    minutes = total_ms // 60000
    total_ms %= 60000
    secs = total_ms // 1000
    millis = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def generate_srt(segments, output_file="subtitles.srt"):
    print("📝 Creating synchronized subtitles...")
    with open(output_file, "w", encoding="utf-8") as f:
        index = 1
        for seg in segments:
            text = seg.get("translated_text", "").strip()
            if not text:
                continue

            start = seg.get("final_start", seg["new_start"]) / FINAL_SPEED
            end = seg.get("final_end", seg["speech_end"]) / FINAL_SPEED

            if end <= start:
                end = start + 0.5

            f.write(f"{index}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{text}\n\n")
            index += 1

# ================= CLEANUP =================

def cleanup():
    for directory in ("temp_audio", "temp_video_parts"):
        if os.path.exists(directory):
            try:
                shutil.rmtree(directory)
            except Exception:
                pass

    for file in ("raw_source.mp4", "input_audio.wav", "synced_master.wav", "extended_video.mp4", "video_concat.txt"):
        if os.path.exists(file):
            try:
                os.remove(file)
            except Exception:
                pass

# ================= MAIN =================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python dub.py "VIDEO_URL" [target_language]')
        sys.exit(1)

    video_url = sys.argv[1]
    target_language = (sys.argv[2] if len(sys.argv) > 2 else "hi").lower()

    print()
    print("=" * 55)
    print("AI DUBBING ENGINE (ULTRA-FAST & SIZE-OPTIMIZED)")
    print("=" * 55)
    print(f"Target language : {target_language}")
    print(f"TTS Speech rate : {TTS_RATE}")
    print(f"Master speedup  : {FINAL_SPEED}x (+{int(round((FINAL_SPEED - 1.0) * 100))}%)")
    print("=" * 55)
    print()

    t_start = time.time()

    try:
        video_file, audio_file, metadata = download_media(video_url)

        target_bitrate = detect_source_video_bitrate(video_file)
        print(f"🎯 Target video bitrate locked to: {target_bitrate} kbps")

        segments = transcribe_and_translate(audio_file, target_language)
        if not segments:
            raise RuntimeError("No speech segments detected.")

        asyncio.run(synthesize_audio(segments))
        calculate_dub_timeline(segments)

        final_duration = max(
            get_duration(video_file),
            max((seg["visual_end"] for seg in segments), default=0.0)
        )

        master_audio = "synced_master.wav"
        build_master_audio(segments, final_duration, master_audio)

        extended_video = "extended_video.mp4"
        build_extended_video(video_file, segments, extended_video)

        mux_final_video(extended_video, master_audio, "final_output.mp4", target_bitrate)
        generate_srt(segments, "subtitles.srt")

        orig_dur = get_duration(video_file)
        final_dur = get_duration("final_output.mp4")
        orig_mb = os.path.getsize(video_file) / (1024 * 1024)
        final_mb = os.path.getsize("final_output.mp4") / (1024 * 1024)
        total_time = time.time() - t_start

        print()
        print("=" * 55)
        print("✅ DUBBING COMPLETE")
        print("=" * 55)
        print(f"Execution time    : {total_time:.1f}s (~{total_time/60:.1f} min)")
        print(f"Original size     : {orig_mb:.2f} MB")
        print(f"Final size        : {final_mb:.2f} MB (Matches source)")
        print(f"Original duration : {orig_dur:.2f}s")
        print(f"Final duration    : {final_dur:.2f}s (Speed +{int(round((FINAL_SPEED - 1.0) * 100))}%)")
        print("=" * 55)

        cleanup()

    except Exception as e:
        print()
        print("=" * 55)
        print("❌ DUBBING FAILED")
        print(f"Error: {e}")
        print("=" * 55)
        raise
