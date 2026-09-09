import os
import sys
import subprocess
import asyncio
import time
import re
import json
import shutil
import urllib.request
import urllib.parse
import socket
import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator
import edge_tts
from pydub import AudioSegment

# Avoid unhandled infinite socket freezes on runner network stalls
socket.setdefaulttimeout(30.0)

# ================= CONFIG =================

TTS_RATE = "+15%"
FINAL_SPEED = 1.10

ENABLE_FREEZE = True
SAFETY_GAP = 0.03
SILENCE_THRESHOLD = -42
WHISPER_MODEL = "tiny"

FINAL_CRF = "22"
MAX_VIDEO_BITRATE = "2200k"
BUFFER_SIZE = "4400k"
AUDIO_BITRATE = "128k"

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

def get_system_font():
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

# ================= DOWNLOAD =================

def download_media(url, device_id="default"):
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

    print(f"📥 Downloading media for client device [{device_id}]...")
    with yt_dlp.YoutubeDL(opts) as ydl:
        meta = ydl.extract_info(url, download=True)
        metadata = {
            "title": meta.get("title", "Dubbed Video"),
            "description": meta.get("description", ""),
            "tags": meta.get("tags", []),
            "device_id": device_id
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

# ================= TRANSLATION ENGINE =================

def contains_devanagari(text):
    return bool(re.search(r"[\u0900-\u097F]", text))

def translate_gtx(text, target_lang="hi"):
    try:
        encoded = urllib.parse.quote(text)
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={target_lang}&dt=t&q={encoded}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            pieces = [part[0] for part in data[0] if part[0]]
            return "".join(pieces).strip()
    except Exception:
        return ""

def translate_text(text, target_lang):
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    if not text or len(text) < 2:
        return text

    res = translate_gtx(text, target_lang)
    if res and (target_lang != "hi" or contains_devanagari(res)):
        return res

    for _ in range(2):
        try:
            res = GoogleTranslator(source="auto", target=target_lang).translate(text)
            if res and (target_lang != "hi" or contains_devanagari(res)):
                return res.strip()
        except Exception:
            time.sleep(0.2)

    try:
        tgt = "hi-IN" if target_lang == "hi" else target_lang
        res = MyMemoryTranslator(source="auto", target=tgt).translate(text)
        if res and (target_lang != "hi" or contains_devanagari(res)):
            return res.strip()
    except Exception:
        pass

    return ""

# ================= WHISPER WITH CACHE & OFFLINE RESILIENCE =================

def load_resilient_whisper(model_size="tiny"):
    hf_token = os.environ.get("HF_TOKEN", None)
    if hf_token:
        hf_token = hf_token.strip() or None

    try:
        print("🔍 Checking local cache for Whisper model...")
        model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            cpu_threads=2,
            local_files_only=True
        )
        print("⚡ Successfully loaded Whisper from local cache.")
        return model
    except Exception:
        print("🌐 Local cache miss. Fetching Whisper from repository mirror...")

    for attempt in range(1, 4):
        try:
            model = WhisperModel(
                model_size,
                device="cpu",
                compute_type="int8",
                cpu_threads=2,
                download_root=os.path.expanduser("~/.cache/huggingface/hub"),
                local_files_only=False
            )
            print(f"✅ Model downloaded and initialized on attempt {attempt}.")
            return model
        except Exception as e:
            print(f"⚠️ Hugging Face connect attempt {attempt}/3 failed: {e}")
            time.sleep(attempt * 2)

    raise RuntimeError("Critical: Unable to initialize Whisper model due to network rate limits.")

def transcribe_and_translate(audio_path, target_lang):
    print("🧠 Loading Whisper...")
    model = load_resilient_whisper(WHISPER_MODEL)

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
    sem = asyncio.Semaphore(5)

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

# ================= HIGH-SPEED SLICING =================

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

    cmd = [
        "ffmpeg", "-y", "-err_detect", "ignore_err",
        "-i", video_file,
        "-an",
        "-vf", ",".join(filters),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "17",
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
    print("🎬 Slicing & freezing video parts...")

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

# ================= FINAL MUX & MOVING WATERMARK =================

def mux_final_video(extended_video, dubbed_audio, output_file):
    pct = int(round((FINAL_SPEED - 1.0) * 100))
    print(f"🎬 Applying moving logo, anti-fingerprint filters & +{pct}% speedup...")

    font_path = get_system_font()
    font_arg = f"fontfile='{font_path}':" if font_path else ""

    watermark_filter = (
        f"drawtext={font_arg}text='Movies Jet':fontsize=h/42:"
        f"fontcolor=white@0.30:bordercolor=black@0.18:borderw=1:"
        f"x='(w-tw)/2+((w-tw)/2.3)*sin(t*0.4)':"
        f"y='(h-th)/2+((h-th)/2.3)*cos(t*0.6)'"
    )

    video_filters = [
        "crop=w='trunc(in_w*0.96/2)*2':h='trunc(in_h*0.96/2)*2':x='(in_w-out_w)/2':y='(in_h-out_h)/2'",
        "eq=contrast=1.06:brightness=0.01:saturation=1.07:gamma=1.02",
        "noise=alls=3:allf=t",
        watermark_filter,
        f"setpts=PTS/{FINAL_SPEED:.4f}",
        "fps=30"
    ]
    video_filter_str = ",".join(video_filters)

    audio_filters = [
        f"atempo={FINAL_SPEED:.4f}",
        "equalizer=f=250:t=q:w=1:g=1.5",
        "equalizer=f=3500:t=q:w=1:g=1.2",
        "highpass=f=75",
        "lowpass=f=14000",
        "volume=1.05"
    ]
    audio_filter_str = ",".join(audio_filters)

    run_command([
        "ffmpeg", "-y",
        "-i", extended_video,
        "-i", dubbed_audio,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-vf", video_filter_str,
        "-af", audio_filter_str,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", FINAL_CRF,
        "-maxrate", MAX_VIDEO_BITRATE,
        "-bufsize", BUFFER_SIZE,
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
        print('Usage: python dubber.py "VIDEO_URL" [target_language] [device_id]')
        sys.exit(1)

    video_url = sys.argv[1]
    target_language = (sys.argv[2] if len(sys.argv) > 2 else "hi").lower()
    client_device_id = sys.argv[3] if len(sys.argv) > 3 else "default"

    print()
    print("=" * 55)
    print(f"AI DUBBING ENGINE [Client: {client_device_id}]")
    print("=" * 55)
    print(f"Target language : {target_language}")
    print(f"TTS Speech rate : {TTS_RATE}")
    print(f"Master speedup  : {FINAL_SPEED}x (+{int(round((FINAL_SPEED - 1.0) * 100))}%)")
    print(f"Video Quality   : CRF {FINAL_CRF}")
    print("=" * 55)
    print()

    t_start = time.time()

    try:
        video_file, audio_file, metadata = download_media(video_url, device_id=client_device_id)

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

        mux_final_video(extended_video, master_audio, "final_output.mp4")
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
        print(f"Final size        : {final_mb:.2f} MB")
        print(f"Original duration : {orig_dur:.2f}s")
        print(f"Final duration    : {final_dur:.2f}s")
        print("=" * 55)

        cleanup()

    except Exception as e:
        print()
        print("=" * 55)
        print("❌ DUBBING FAILED")
        print(f"Error: {e}")
        print("=" * 55)
        raise
