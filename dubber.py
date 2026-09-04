import os
import sys
import re
import json
import time
import math
import shutil
import asyncio
import subprocess
import urllib.request
import urllib.error

import yt_dlp
import edge_tts
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator
from pydub import AudioSegment


# ============================================================
# CONFIG
# ============================================================

FINAL_SPEED = 1.10

# Maximum individual TTS acceleration.
# If the sentence still does not fit, video freezes.
MAX_SENTENCE_SPEED = 1.12

TTS_RATE = "+5%"
WHISPER_MODEL = "tiny"

VIDEO_CRF = "27"
VIDEO_PRESET = "veryfast"
AUDIO_BITRATE = "128k"

TEMP_DIR = "temp_audio"

OUTPUT_VIDEO = "final_output.mp4"
OUTPUT_SRT = "subtitles.srt"

FREEZE_ENABLED = True

NATURALIZE_HINDI = True
GEMINI_MODEL = "gemini-2.5-flash"

TTS_RETRIES = 3


# ============================================================
# VOICES
# ============================================================

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


# ============================================================
# GENERAL HELPERS
# ============================================================

def run(cmd, quiet=False):
    print(
        "▶ " +
        " ".join(
            f'"{x}"' if " " in str(x) else str(x)
            for x in cmd
        )
    )

    subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
        check=True
    )


def get_duration(path):
    result = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path
        ],
        stderr=subprocess.STDOUT
    )

    return float(result.strip())


def size_mb(path):
    if not os.path.exists(path):
        return 0.0

    return os.path.getsize(path) / 1048576.0


def clean_text(text):
    return re.sub(
        r"\s+",
        " ",
        str(text or "")
    ).strip()


# ============================================================
# DOWNLOAD
# ============================================================

def download_media(url):
    video_path = "raw_source.mp4"
    audio_path = "input_audio.wav"

    for path in (video_path, audio_path):
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    print("📥 Downloading media...")

    options = {
        "format": "best[ext=mp4]/best",
        "outtmpl": video_path,
        "merge_output_format": "mp4",
        "noplaylist": True
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            url,
            download=True
        )

    metadata = {
        "title": info.get("title", "Dubbed Video"),
        "description": info.get("description", ""),
        "tags": info.get("tags", [])
    }

    with open("source_meta.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print("TITLE_EMIT: " + metadata["title"])
    print("🎵 Extracting source audio...")

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            audio_path
        ],
        quiet=True
    )

    return video_path, audio_path, metadata


# ============================================================
# TEXT / LANGUAGE
# ============================================================

def contains_hindi(text):
    return bool(text and re.search(r"[\u0900-\u097F]", text))


def invalid_translation(text):
    if not text:
        return True

    value = text.lower()
    bad_values = (
        "<html",
        "<!doctype",
        "captcha",
        "access denied",
        "server error",
        "unusual traffic"
    )

    return any(item in value for item in bad_values)


# ============================================================
# TRANSLATION & NATURALIZATION
# ============================================================

def naturalize_with_gemini(text, target_lang="hi"):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    lang_names = {
        "hi": "natural spoken Hindi",
        "pa": "natural spoken Punjabi",
        "bn": "natural spoken Bengali",
        "ta": "natural spoken Tamil",
        "te": "natural spoken Telugu",
        "mr": "natural spoken Marathi",
        "en": "conversational English",
    }
    target_desc = lang_names.get(target_lang, target_lang)

    prompt = (
        f"Translate and adapt the following sentence into {target_desc} for video dubbing.\n"
        "Rules:\n"
        "- Use common everyday conversational words.\n"
        "- Do not use overly formal or literary words.\n"
        "- Retain English brand names, technical terms, and numbers as commonly spoken.\n"
        "- Output ONLY the final spoken sentence. Do not add quotes, commentary, notes, or alternatives.\n\n"
        f"Text: {text}"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 350
        }
    }

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.loads(res.read().decode("utf-8"))
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        if parts:
            result = parts[0].get("text", "").strip()
            result = re.sub(r"^```(?:text|hindi)?\s*", "", result, flags=re.I)
            result = re.sub(r"\s*```$", "", result).strip()
            result = re.sub(r"^[\"']|[\"']$", "", result)
            return clean_text(result)
    except Exception as e:
        print(f"⚠️ Gemini processing error: {e}")

    return None


def translate_text(text, target):
    text = clean_text(text)
    if len(text) < 2:
        return text

    # Try Gemini directly if API key is present
    gemini_result = naturalize_with_gemini(text, target)
    if gemini_result and not invalid_translation(gemini_result):
        return gemini_result

    # GoogleTranslator fallback
    for attempt in range(3):
        try:
            result = GoogleTranslator(source="auto", target=target).translate(text)
            if result and not invalid_translation(result):
                return clean_text(result)
        except Exception:
            time.sleep(0.4)

    # MyMemory fallback
    mymemory_codes = {
        "hi": "hindi", "en": "english", "bn": "bengali", "ta": "tamil",
        "te": "telugu", "mr": "marathi", "gu": "gujarati", "kn": "kannada",
        "ml": "malayalam", "pa": "punjabi", "fr": "french", "de": "german",
        "es": "spanish", "it": "italian", "pt": "portuguese", "nl": "dutch",
        "pl": "polish", "tr": "turkish", "ru": "russian", "uk": "ukrainian",
        "ja": "japanese", "ko": "korean", "zh": "chinese", "ar": "arabic"
    }
    mm_target = mymemory_codes.get(target.lower(), target)

    try:
        result = MyMemoryTranslator(source="auto", target=mm_target).translate(text)
        if result and not invalid_translation(result):
            return clean_text(result)
    except Exception:
        pass

    print("⚠️ Translation fallback: using original text.")
    return text


# ============================================================
# WHISPER TRANSCRIPTION
# ============================================================

def transcribe(audio_path, target_lang):
    print("🧠 Loading Whisper...")

    model = WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8"
    )

    print("🎙️ Transcribing...")

    result, info = model.transcribe(
        audio_path,
        language=None,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300}
    )

    print("🌍 Detected language:", info.language)

    raw = []
    for item in result:
        text = clean_text(item.text)
        start = float(item.start)
        end = float(item.end)

        if not text or end <= start or (end - start < 0.20):
            continue

        raw.append({"start": start, "end": end, "text": text})

    if not raw:
        return []

    blocks = []
    current_text = []
    current_start = raw[0]["start"]
    current_end = raw[0]["end"]

    for i, item in enumerate(raw):
        current_text.append(item["text"])
        current_end = item["end"]

        punctuation = item["text"].rstrip().endswith((".", "!", "?", "।", "…"))
        gap = (raw[i + 1]["start"] - item["end"]) if (i + 1 < len(raw)) else 0
        long_sentence = (current_end - current_start >= 8.0)
        last_item = (i == len(raw) - 1)

        if punctuation or gap >= 0.70 or long_sentence or last_item:
            text = clean_text(" ".join(current_text))
            if text:
                blocks.append({
                    "start": current_start,
                    "end": current_end,
                    "text": text
                })
            current_text = []
            if i + 1 < len(raw):
                current_start = raw[i + 1]["start"]

    print(f"📝 Dialogue blocks: {len(blocks)}")

    segments = []
    for i, block in enumerate(blocks):
        start = block["start"]
        end = block["end"]

        if i + 1 < len(blocks):
            next_start = blocks[i + 1]["start"]
            slot = max(0.30, next_start - start)
        else:
            slot = max(0.30, end - start)

        print(f"Translating block {i + 1}/{len(blocks)}...")
        translated = translate_text(block["text"], target_lang)

        segments.append({
            "index": i,
            "source_start": start,
            "source_end": end,
            "slot": slot,
            "source_text": block["text"],
            "translated_text": translated,
            "target_lang": target_lang
        })

    return segments


# ============================================================
# EDGE TTS
# ============================================================

async def make_tts(text, voice, output):
    communicator = edge_tts.Communicate(text, voice, rate=TTS_RATE)
    await communicator.save(output)


def tts_with_retry(text, voice, output):
    for attempt in range(1, TTS_RETRIES + 1):
        try:
            if os.path.exists(output):
                os.remove(output)

            asyncio.run(make_tts(text, voice, output))

            if os.path.exists(output) and os.path.getsize(output) > 1000:
                if get_duration(output) > 0.05:
                    return True
        except Exception as e:
            print(f"⚠️ TTS {attempt}/{TTS_RETRIES}: {e}")

        time.sleep(0.5)

    return False


# ============================================================
# AUDIO CLEANUP
# ============================================================

def clean_audio(source, output):
    audio = AudioSegment.from_file(source)

    if len(audio) <= 20:
        audio.export(output, format="wav")
        return

    try:
        audio = audio.strip_silence(
            silence_len=60,
            silence_thresh=-48,
            padding=25
        )
    except Exception:
        pass

    audio = audio.set_frame_rate(44100).set_channels(2)
    audio.export(output, format="wav")


def speed_audio(source, output, factor):
    factor = max(1.0, min(float(factor), 2.0))
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            source,
            "-filter:a",
            f"atempo={factor:.6f}",
            "-ar",
            "44100",
            "-ac",
            "2",
            output
        ],
        quiet=True
    )


# ============================================================
# PREPARE TTS
# ============================================================

def prepare_tts(segments):
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)

    os.makedirs(TEMP_DIR, exist_ok=True)

    for i, segment in enumerate(segments):
        text = clean_text(segment["translated_text"])
        if not text:
            text = clean_text(segment["source_text"])
            segment["translated_text"] = text

        lang = segment["target_lang"].lower().split("-")[0]
        voice = VOICE_MAP.get(lang, "en-US-GuyNeural")

        raw = os.path.join(TEMP_DIR, f"{i:04d}_raw.mp3")
        clean = os.path.join(TEMP_DIR, f"{i:04d}_clean.wav")
        fitted = os.path.join(TEMP_DIR, f"{i:04d}_fit.wav")

        print(f"🔊 TTS {i + 1}/{len(segments)}")

        success = tts_with_retry(text, voice, raw)

        # Fallback to source text
        if not success:
            source_text = clean_text(segment["source_text"])
            if source_text:
                print("   ↳ TTS fallback to source text")
                success = tts_with_retry(source_text, voice, raw)

        if not success:
            print(f"❌ TTS failed: block {i + 1}")
            segment["clip_file"] = None
            segment["tts_duration"] = 0.0
            segment["tts_failed"] = True
            continue

        try:
            clean_audio(raw, clean)
            original_duration = get_duration(clean)
            slot = max(0.30, float(segment["slot"]))

            required = original_duration / slot
            factor = max(1.0, min(required, MAX_SENTENCE_SPEED))

            if factor > 1.001:
                speed_audio(clean, fitted, factor)
                final_file = fitted
                final_duration = get_duration(fitted)
            else:
                final_file = clean
                final_duration = original_duration

            if final_duration <= 0.05:
                raise RuntimeError("TTS clip is empty.")

            segment["clip_file"] = final_file
            segment["tts_duration"] = final_duration
            segment["audio_speed"] = factor
            segment["tts_failed"] = False

            print(
                f"   {original_duration:.2f}s → {final_duration:.2f}s | "
                f"slot {slot:.2f}s | speed {factor:.3f}x"
            )

        except Exception as e:
            print(f"❌ Audio processing failed for block {i + 1}: {e}")
            segment["clip_file"] = None
            segment["tts_duration"] = 0.0
            segment["tts_failed"] = True


# ============================================================
# SYNCHRONIZATION & MASTER AUDIO
# ============================================================

def calculate_timeline(segments, source_duration):
    """
    Prevents overlap: dynamically pushes next segments forward if speech
    is longer than available slot and calculates exact video frame freezes.
    """
    current_time = 0.0
    accumulated_delay = 0.0

    for seg in segments:
        orig_start = float(seg["source_start"])
        slot = float(seg["slot"])
        clip_dur = float(seg.get("tts_duration", 0.0))

        target_start = max(orig_start + accumulated_delay, current_time)
        current_delay = target_start - orig_start
        accumulated_delay = max(accumulated_delay, current_delay)

        seg["audio_start"] = target_start
        seg["audio_end"] = target_start + clip_dur
        current_time = seg["audio_end"] + 0.05

        extra_time = clip_dur - slot
        if extra_time > 0.05 and FREEZE_ENABLED:
            seg["freeze_duration"] = extra_time
            accumulated_delay += extra_time
        else:
            seg["freeze_duration"] = 0.0

    total_duration = max(source_duration + accumulated_delay, current_time)
    return total_duration


def build_master_audio(segments, total_duration, output):
    print("🎧 Building master audio...")

    total_ms = int(math.ceil((total_duration + 1.0) * 1000))
    master = AudioSegment.silent(duration=total_ms, frame_rate=44100).set_channels(2)

    for i, segment in enumerate(segments):
        clip_path = segment.get("clip_file")
        if not clip_path or not os.path.exists(clip_path):
            continue

        try:
            clip = AudioSegment.from_file(clip_path).set_frame_rate(44100).set_channels(2)
            position = int(round(segment["audio_start"] * 1000))
            if position < 0:
                position = 0
            master = master.overlay(clip, position=position)
        except Exception as e:
            print(f"⚠️ Could not insert audio block {i + 1}: {e}")

    master.export(output, format="wav")
    print("✅ Master audio created")


# ============================================================
# VIDEO RENDERING
# ============================================================

def render_video(source, segments, source_duration, output):
    print("🎬 Rendering synchronized video...")

    filters = []
    labels = []
    label_number = 0

    def new_label():
        nonlocal label_number
        lbl = f"v{label_number}"
        label_number += 1
        return lbl

    prev_end = 0.0

    for seg in segments:
        start = float(seg["source_start"])
        end = float(seg["source_end"])
        freeze = float(seg.get("freeze_duration", 0.0))

        if start > prev_end + 0.01:
            lbl = new_label()
            filters.append(
                f"[0:v]trim=start={prev_end:.6f}:end={start:.6f},"
                f"setpts=PTS-STARTPTS[{lbl}]"
            )
            labels.append(f"[{lbl}]")

        lbl = new_label()
        chunk = (
            f"[0:v]trim=start={start:.6f}:end={end:.6f},"
            f"setpts=PTS-STARTPTS"
        )
        if FREEZE_ENABLED and freeze > 0.02:
            chunk += f",tpad=stop_mode=clone:stop_duration={freeze:.6f}"
        chunk += f"[{lbl}]"

        filters.append(chunk)
        labels.append(f"[{lbl}]")
        prev_end = end

    if prev_end < source_duration - 0.01:
        lbl = new_label()
        filters.append(
            f"[0:v]trim=start={prev_end:.6f}:end={source_duration:.6f},"
            f"setpts=PTS-STARTPTS[{lbl}]"
        )
        labels.append(f"[{lbl}]")

    if not labels:
        lbl = new_label()
        filters.append(f"[0:v]setpts=PTS-STARTPTS[{lbl}]")
        labels.append(f"[{lbl}]")

    if len(labels) == 1:
        filters.append(f"{labels[0]}setpts=PTS-STARTPTS[joined]")
    else:
        concat_inputs = "".join(labels)
        filters.append(f"{concat_inputs}concat=n={len(labels)}:v=1:a=0,setpts=PTS-STARTPTS[joined]")

    filters.append(f"[joined]setpts=PTS/{FINAL_SPEED:.6f}[vout]")

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            source,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            VIDEO_PRESET,
            "-crf",
            VIDEO_CRF,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            output
        ],
        quiet=False
    )

    if not os.path.exists(output):
        raise RuntimeError("Video rendering failed.")


# ============================================================
# FINAL AUDIO + VIDEO
# ============================================================

def mux_final(video, audio, output):
    print("🎧 Muxing final MP4...")

    temporary = output + ".tmp.mp4"
    if os.path.exists(temporary):
        os.remove(temporary)

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            video,
            "-i",
            audio,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-af",
            f"atempo={FINAL_SPEED:.6f}",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            AUDIO_BITRATE,
            "-movflags",
            "+faststart",
            "-shortest",
            temporary
        ],
        quiet=False
    )

    if not os.path.exists(temporary):
        raise RuntimeError("Final MP4 was not generated.")

    if os.path.exists(output):
        os.remove(output)

    os.replace(temporary, output)


# ============================================================
# SRT
# ============================================================

def timestamp(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours = milliseconds // 3600000
    milliseconds %= 3600000
    minutes = milliseconds // 60000
    milliseconds %= 60000
    seconds_value = milliseconds // 1000
    milliseconds %= 1000

    return f"{hours:02d}:{minutes:02d}:{seconds_value:02d},{milliseconds:03d}"


def write_srt(segments, output):
    print("📝 Writing SRT...")

    counter = 0
    with open(output, "w", encoding="utf-8") as f:
        for segment in segments:
            text = clean_text(segment.get("translated_text", ""))
            if not text:
                continue

            counter += 1
            start = float(segment["audio_start"]) / FINAL_SPEED
            end = float(segment["audio_end"]) / FINAL_SPEED

            f.write(str(counter) + "\n")
            f.write(f"{timestamp(start)} --> {timestamp(end)}\n")
            f.write(text + "\n\n")

    print(f"✅ SRT: {counter} subtitles")


# ============================================================
# CLEANUP
# ============================================================

def cleanup():
    paths = (
        TEMP_DIR,
        "raw_source.mp4",
        "input_audio.wav",
        "synced_master.wav",
        "extended_video.mp4"
    )

    for path in paths:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

def main():
    if len(sys.argv) < 2:
        print('Usage: python dubber.py "VIDEO_URL" [language]')
        sys.exit(1)

    url = sys.argv[1]
    target_lang = sys.argv[2].lower() if len(sys.argv) > 2 else "hi"

    started = time.time()

    print()
    print("=" * 65)
    print("                 AI VIDEO DUBBER")
    print("=" * 65)
    print("Target language:", target_lang)
    print("Final speed:", FINAL_SPEED)
    print("Max sentence speed:", MAX_SENTENCE_SPEED)
    print("Freeze:", FREEZE_ENABLED)
    print("=" * 65)
    print()

    try:
        # 1. DOWNLOAD
        source_video, source_audio, metadata = download_media(url)
        source_duration = get_duration(source_video)

        print(f"Source duration: {source_duration:.2f}s")
        print(f"Source size: {size_mb(source_video):.2f} MB")

        # 2. TRANSCRIBE
        segments = transcribe(source_audio, target_lang)
        if not segments:
            raise RuntimeError("No speech was detected.")

        # 3. TTS
        prepare_tts(segments)

        # 4. TIMELINE & OVERLAP FIX
        output_duration = calculate_timeline(segments, source_duration)
        print(f"Timeline duration: {output_duration:.2f}s")

        # 5. AUDIO
        master_audio = "synced_master.wav"
        build_master_audio(segments, output_duration, master_audio)

        # 6. SUBTITLES
        write_srt(segments, OUTPUT_SRT)

        # 7. VIDEO
        intermediate_video = "extended_video.mp4"
        render_video(source_video, segments, source_duration, intermediate_video)

        # 8. FINAL MP4
        mux_final(intermediate_video, master_audio, OUTPUT_VIDEO)

        # 9. VERIFY
        if not os.path.exists(OUTPUT_VIDEO):
            raise RuntimeError("final_output.mp4 was not created.")

        final_size = size_mb(OUTPUT_VIDEO)
        final_duration = get_duration(OUTPUT_VIDEO)
        failures = sum(1 for s in segments if s.get("tts_failed", False))
        elapsed = time.time() - started

        print()
        print("=" * 65)
        print("                    COMPLETE")
        print("=" * 65)
        print(f"Source size : {size_mb(source_video):.2f} MB")
        print(f"Final size  : {final_size:.2f} MB")
        print(f"Source time : {source_duration:.2f}s")
        print(f"Final time  : {final_duration:.2f}s")
        print(f"Speed       : {FINAL_SPEED:.2f}x")
        print(f"TTS failures: {failures}")
        print(f"Processing  : {elapsed / 60:.2f} minutes")
        print()
        print("🎬 final_output.mp4")
        print("📝 subtitles.srt")
        print("=" * 65)

        cleanup()

    except Exception as e:
        print()
        print("=" * 65)
        print("                    FAILED")
        print("=" * 65)
        print("❌", str(e))
        print()
        print("Temporary files have been kept.")
        print("=" * 65)
        raise


if __name__ == "__main__":
    main()
