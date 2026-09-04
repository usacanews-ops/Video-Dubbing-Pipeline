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


# ============================================================
# CONFIGURATION
# ============================================================

# IMPORTANT:
# Do NOT globally speed up the final video.
GLOBAL_SPEED = 1.0

# Maximum speed for an individual TTS clip.
# 1.0 = normal
# 1.25 = 25% faster
# 1.50 = 50% faster
# 1.75 = 75% faster
# 2.0 = double speed
MAX_TTS_SPEED = 1.85

# Small safety gap between two speech regions.
SAFETY_GAP = 0.03

# Silence removal threshold.
SILENCE_THRESHOLD = -42

# Whisper model.
WHISPER_MODEL = "tiny"


# ============================================================
# EDGE TTS VOICES
# ============================================================
#
# Add more languages here if required.
#
# The key is the language code used by your command line.
#
# Example:
# python dub.py URL hi
#
# python dub.py URL fr
# python dub.py URL es
# python dub.py URL de
#
# ============================================================

VOICE_MAP = {
    "hi": "hi-IN-MadhurNeural",

    "en": "en-US-GuyNeural",

    "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural",
    "es": "es-ES-AlvaroNeural",
    "it": "it-IT-DiegoNeural",
    "pt": "pt-BR-AntonioNeural",
    "ru": "ru-RU-DmitryNeural",

    "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural",
    "zh": "zh-CN-YunxiNeural",

    "ar": "ar-SA-HamedNeural",
    "bn": "bn-IN-BashkarNeural",
    "ta": "ta-IN-ValluvarNeural",
    "te": "te-IN-MohanNeural",
    "mr": "mr-IN-ManoharNeural",
    "gu": "gu-IN-DhwaniNeural",
    "kn": "kn-IN-GaganNeural",
    "ml": "ml-IN-MidhunNeural",
    "pa": "pa-IN-OjasNeural",

    "tr": "tr-TR-AhmetNeural",
    "nl": "nl-NL-MaartenNeural",
    "pl": "pl-PL-MarekNeural",
    "uk": "uk-UA-OstapNeural",
}


# ============================================================
# 1. MEDIA DOWNLOAD
# ============================================================

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
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": video_path,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }

    print("📥 Downloading media...")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        meta = ydl.extract_info(url, download=True)

        meta_dict = {
            "title": meta.get("title", "Dubbed Video"),
            "description": meta.get("description", ""),
            "tags": meta.get("tags", []),
        }

    with open(
        "source_meta.json",
        "w",
        encoding="utf-8"
    ) as mf:

        json.dump(
            meta_dict,
            mf,
            ensure_ascii=False,
            indent=2
        )

    print(f"TITLE_EMIT: {meta_dict['title']}")

    # Extract clean mono 16 kHz audio for Whisper.
    print("🎵 Extracting source audio...")

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            audio_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    return video_path, audio_path, meta_dict


# ============================================================
# 2. GET MEDIA DURATION
# ============================================================

def get_duration(file_path: str) -> float:

    result = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]
    )

    return float(result.strip())


# ============================================================
# 3. TRANSLATION
# ============================================================

def contains_devanagari(text: str) -> bool:

    return bool(
        re.search(r"[\u0900-\u097F]", text)
    )


def is_error_page(text: str) -> bool:

    lowered = text.lower()

    return any(
        sig in lowered
        for sig in [
            "server error",
            "<!doctype",
            "<html",
            "captcha",
            "unusual traffic",
        ]
    )


def translate_text(
    text: str,
    target_lang: str
) -> str:

    clean_text = re.sub(
        r"[\r\n\t]+",
        " ",
        text
    ).strip()

    if not clean_text:
        return ""

    if len(clean_text) < 2:
        return clean_text

    # ========================================================
    # Hindi
    # ========================================================

    if target_lang == "hi":

        for attempt in range(3):

            try:

                result = GoogleTranslator(
                    source="auto",
                    target="hi"
                ).translate(clean_text)

                if (
                    result
                    and not is_error_page(result)
                    and contains_devanagari(result)
                ):

                    return result.strip()

            except Exception:

                time.sleep(0.3)

        try:

            result = MyMemoryTranslator(
                source="auto",
                target="hi-IN"
            ).translate(clean_text)

            if result and not is_error_page(result):
                return result.strip()

        except Exception:
            pass

        return ""

    # ========================================================
    # Other languages
    # ========================================================

    for attempt in range(3):

        try:

            result = GoogleTranslator(
                source="auto",
                target=target_lang
            ).translate(clean_text)

            if result and not is_error_page(result):

                return result.strip()

        except Exception:

            time.sleep(0.3)

    return clean_text


# ============================================================
# 4. TRANSCRIPTION
# ============================================================

def transcribe_and_translate(
    audio_path: str,
    target_lang: str = "hi"
) -> list[dict]:

    print("🧠 Loading Whisper...")

    model = WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8"
    )

    print("🎙️ Transcribing source audio...")

    raw_segments, info = model.transcribe(
        audio_path,
        language=None,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=350
        ),
    )

    print(
        f"🌍 Detected source language: {info.language}"
    )

    raw_list = []

    for segment in raw_segments:

        text = segment.text.strip()

        if not text:
            continue

        duration = segment.end - segment.start

        if duration < 0.15:
            continue

        raw_list.append(
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "text": text,
            }
        )

    if not raw_list:
        return []

    # ========================================================
    # IMPORTANT:
    #
    # We keep Whisper timing anchors.
    #
    # We do NOT allow translated audio to determine
    # the start time of the next sentence.
    # ========================================================

    print(
        f"📝 Found {len(raw_list)} speech segments."
    )

    segments = []

    first_preview = False

    for i, source in enumerate(raw_list):

        start = source["start"]

        # The next Whisper segment determines the END
        # of this segment's scheduling slot.
        if i + 1 < len(raw_list):

            next_start = raw_list[i + 1]["start"]

            available_slot = (
                next_start
                - start
                - SAFETY_GAP
            )

        else:

            available_slot = (
                source["end"]
                - start
                + 1.5
            )

        # Never permit a zero/negative slot.
        available_slot = max(
            0.25,
            available_slot
        )

        translated = translate_text(
            source["text"],
            target_lang
        )

        if not translated:
            translated = source["text"]

        if not first_preview:

            preview = translated[:100]

            print(
                f"TRANSLATION_PREVIEW: {preview}"
            )

            first_preview = True

        segments.append(
            {
                "index": i,
                "start": start,
                "source_end": source["end"],
                "available_slot": available_slot,
                "source_text": source["text"],
                "translated_text": translated,
                "target_lang": target_lang,
            }
        )

    return segments


# ============================================================
# 5. SILENCE REMOVAL
# ============================================================

def strip_dead_silence(
    input_file: str,
    output_file: str,
    threshold: int = SILENCE_THRESHOLD
):

    audio = AudioSegment.from_file(input_file)

    if len(audio) == 0:
        return

    start_trim = 0
    end_trim = len(audio)

    # Detect first audible portion.
    for pos in range(0, len(audio), 10):

        chunk = audio[pos:pos + 10]

        if chunk.dBFS > threshold:

            start_trim = max(
                0,
                pos - 20
            )

            break

    # Detect final audible portion.
    for pos in range(
        len(audio) - 10,
        0,
        -10
    ):

        chunk = audio[pos:pos + 10]

        if chunk.dBFS > threshold:

            end_trim = min(
                len(audio),
                pos + 30
            )

            break

    if end_trim <= start_trim:

        stripped = audio

    else:

        stripped = audio[
            start_trim:end_trim
        ]

    stripped = (
        stripped
        .set_frame_rate(44100)
        .set_channels(2)
    )

    stripped.export(
        output_file,
        format="wav"
    )


# ============================================================
# 6. FFMPEG ATEMPO FILTER
# ============================================================
#
# FFmpeg's atempo filter is safest when each individual
# filter remains between 0.5 and 2.0.
#
# Therefore we build a chain automatically.
#
# Example:
#
# 2.5x
#
# becomes:
#
# atempo=2.0,atempo=1.25
#
# ============================================================

def build_atempo_filter(speed: float) -> str:

    speed = max(
        0.25,
        min(speed, 8.0)
    )

    filters = []

    while speed > 2.0:

        filters.append(
            "atempo=2.0"
        )

        speed /= 2.0

    while speed < 0.5:

        filters.append(
            "atempo=0.5"
        )

        speed /= 0.5

    filters.append(
        f"atempo={speed:.6f}"
    )

    return ",".join(filters)


# ============================================================
# 7. FIT ONE TTS CLIP TO ITS FIXED SLOT
# ============================================================

def fit_audio_to_slot(
    input_wav: str,
    output_wav: str,
    target_duration: float
):

    actual_duration = get_duration(
        input_wav
    )

    if actual_duration <= 0:
        return

    # If already shorter, don't slow it down.
    if actual_duration <= target_duration:

        shutil.copyfile(
            input_wav,
            output_wav
        )

        return

    # Required speed.
    speed = (
        actual_duration
        / target_duration
    )

    # Cap extreme compression.
    speed = min(
        speed,
        MAX_TTS_SPEED
    )

    print(
        f"      ⏩ TTS {actual_duration:.2f}s "
        f"→ slot {target_duration:.2f}s "
        f"({speed:.2f}x)"
    )

    filter_chain = build_atempo_filter(
        speed
    )

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_wav,
            "-filter:a",
            filter_chain,
            "-ar",
            "44100",
            "-ac",
            "2",
            output_wav,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


# ============================================================
# 8. TTS SYNTHESIS
# ============================================================

async def synthesize_audio(
    segments: list[dict],
    temp_dir: str = "temp_audio"
):

    if os.path.exists(temp_dir):

        shutil.rmtree(temp_dir)

    os.makedirs(
        temp_dir,
        exist_ok=True
    )

    for i, seg in enumerate(segments):

        text = (
            seg
            .get("translated_text", "")
            .strip()
        )

        target_lang = (
            seg
            .get("target_lang", "hi")
            .lower()
        )

        if not text:

            seg["clip_file"] = None

            continue

        # Remove region such as hi-IN.
        base_lang = target_lang.split("-")[0]

        voice = VOICE_MAP.get(
            base_lang
        )

        if not voice:

            print(
                f"⚠️ No Edge voice configured "
                f"for '{target_lang}'. "
                f"Using English voice."
            )

            voice = "en-US-GuyNeural"

        raw_tts = os.path.join(
            temp_dir,
            f"raw_{i}.mp3"
        )

        stripped = os.path.join(
            temp_dir,
            f"strip_{i}.wav"
        )

        fitted = os.path.join(
            temp_dir,
            f"clip_{i}.wav"
        )

        try:

            print(
                f"🔊 TTS {i + 1}/{len(segments)}"
            )

            communicate = edge_tts.Communicate(
                text,
                voice,
                rate="+8%"
            )

            await communicate.save(
                raw_tts
            )

            # Remove leading/trailing synthetic silence.
            strip_dead_silence(
                raw_tts,
                stripped
            )

            actual_duration = get_duration(
                stripped
            )

            slot = max(
                0.25,
                seg["available_slot"]
            )

            # =================================================
            # If speech fits:
            # use it unchanged.
            #
            # If speech is longer:
            # accelerate ONLY THIS CLIP.
            # =================================================

            if actual_duration > slot:

                required_speed = (
                    actual_duration / slot
                )

                if required_speed <= MAX_TTS_SPEED:

                    fit_audio_to_slot(
                        stripped,
                        fitted,
                        slot
                    )

                else:

                    # We still use the maximum permitted
                    # speed rather than allowing this clip
                    # to push the following clips.
                    print(
                        f"      ⚠️ Required speed "
                        f"{required_speed:.2f}x > "
                        f"maximum {MAX_TTS_SPEED:.2f}x"
                    )

                    filter_chain = build_atempo_filter(
                        MAX_TTS_SPEED
                    )

                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            stripped,
                            "-filter:a",
                            filter_chain,
                            "-ar",
                            "44100",
                            "-ac",
                            "2",
                            fitted,
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=True,
                    )

                seg["clip_file"] = fitted

            else:

                seg["clip_file"] = stripped

            final_duration = get_duration(
                seg["clip_file"]
            )

            seg["audio_duration"] = (
                final_duration
            )

            print(
                f"      ✓ final audio: "
                f"{final_duration:.2f}s"
            )

        except Exception as e:

            print(
                f"⚠️ TTS error on segment "
                f"{i}: {e}"
            )

            seg["clip_file"] = None


# ============================================================
# 9. BUILD PERFECTLY ANCHORED MASTER AUDIO
# ============================================================

def render_dubbed_video(
    video_path: str,
    segments: list[dict],
    output_file: str
):

    source_duration = get_duration(
        video_path
    )

    print(
        f"\n🎬 Source duration: "
        f"{source_duration:.3f}s"
    )

    print(
        "🎯 Building fixed-anchor dubbed audio..."
    )

    # Create exact-length silent master.
    master = AudioSegment.silent(
        duration=int(
            source_duration * 1000
        ),
        frame_rate=44100
    )

    master = (
        master
        .set_frame_rate(44100)
        .set_channels(2)
    )

    for seg in segments:

        clip_file = seg.get(
            "clip_file"
        )

        if not clip_file:
            continue

        if not os.path.exists(
            clip_file
        ):
            continue

        # ====================================================
        # CRITICAL FIX
        #
        # NEVER:
        #
        #     max(seg["start"], audio_cursor)
        #
        # The source timestamp is authoritative.
        # ====================================================

        start_time = max(
            0.0,
            seg["start"]
        )

        clip = (
            AudioSegment
            .from_file(clip_file)
            .set_frame_rate(44100)
            .set_channels(2)
        )

        clip_duration = (
            len(clip) / 1000.0
        )

        # Do not allow audio to extend beyond video.
        remaining = (
            source_duration
            - start_time
        )

        if remaining <= 0:
            continue

        if clip_duration > remaining:

            clip = clip[
                :int(remaining * 1000)
            ]

            clip_duration = (
                len(clip) / 1000.0
            )

        # ====================================================
        # FIXED POSITION.
        #
        # Previous audio does NOT influence this position.
        # ====================================================

        position_ms = int(
            start_time * 1000
        )

        master = master.overlay(
            clip,
            position=position_ms
        )

        seg["final_start"] = (
            start_time
        )

        seg["final_end"] = (
            start_time + clip_duration
        )

        print(
            f"   Segment {seg['index'] + 1}: "
            f"{start_time:.3f}s → "
            f"{start_time + clip_duration:.3f}s"
        )

    # Make absolutely certain master length
    # equals source video duration.

    master = master[
        :int(source_duration * 1000)
    ]

    master_path = "synced_master.wav"

    master.export(
        master_path,
        format="wav"
    )

    # ========================================================
    # IMPORTANT:
    #
    # Video is NOT accelerated.
    # Audio is NOT globally accelerated.
    #
    # Each individual speech clip was already fitted.
    # ========================================================

    print(
        "\n🎬 Encoding final synchronized video..."
    )

    cmd = [
        "ffmpeg",
        "-y",

        "-i",
        video_path,

        "-i",
        master_path,

        # Keep original video timing.
        "-map",
        "0:v:0",

        # Use generated dubbed audio.
        "-map",
        "1:a:0",

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-b:v",
        "2600k",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        # Ensure exact source duration.
        "-t",
        f"{source_duration:.3f}",

        "-map_metadata",
        "-1",

        "-map_chapters",
        "-1",

        output_file,
    ]

    subprocess.run(
        cmd,
        check=True
    )

    if os.path.exists(
        master_path
    ):
        os.remove(
            master_path
        )

    if os.path.exists(
        "temp_audio"
    ):
        shutil.rmtree(
            "temp_audio"
        )

    print(
        f"\n✅ Final video created: "
        f"{output_file}"
    )


# ============================================================
# 10. SRT
# ============================================================

def format_timestamp(
    seconds: float
) -> str:

    if seconds < 0:
        seconds = 0

    hours = int(
        seconds // 3600
    )

    minutes = int(
        (seconds % 3600) // 60
    )

    secs = int(
        seconds % 60
    )

    millis = int(
        round(
            (seconds - int(seconds))
            * 1000
        )
    )

    if millis >= 1000:

        millis -= 1000
        secs += 1

    if secs >= 60:

        secs -= 60
        minutes += 1

    if minutes >= 60:

        minutes -= 60
        hours += 1

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d},"
        f"{millis:03d}"
    )


def generate_srt(
    segments: list[dict],
    output_file: str = "subtitles.srt"
):

    with open(
        output_file,
        "w",
        encoding="utf-8"
    ) as f:

        subtitle_index = 1

        for seg in segments:

            text = (
                seg
                .get("translated_text", "")
                .strip()
            )

            if not text:
                continue

            # Use ORIGINAL source timing.
            #
            # This is important because the video itself
            # is no longer globally accelerated.

            start = seg.get(
                "final_start",
                seg["start"]
            )

            end = seg.get(
                "final_end",
                seg["source_end"]
            )

            # Avoid zero-length subtitles.
            if end <= start:
                end = start + 0.5

            f.write(
                f"{subtitle_index}\n"
                f"{format_timestamp(start)} --> "
                f"{format_timestamp(end)}\n"
                f"{text}\n\n"
            )

            subtitle_index += 1


# ============================================================
# 11. CLEANUP
# ============================================================

def cleanup_files():

    files = [
        "raw_source.mp4",
        "input_audio.wav",
    ]

    for file in files:

        if os.path.exists(file):

            try:
                os.remove(file)
            except OSError:
                pass


# ============================================================
# 12. MAIN
# ============================================================

if __name__ == "__main__":

    if len(sys.argv) < 2:

        print(
            "Usage:"
        )

        print(
            'python dub.py "VIDEO_URL" [target_language]'
        )

        print()
        print(
            "Examples:"
        )

        print(
            'python dub.py "https://..." hi'
        )

        print(
            'python dub.py "https://..." fr'
        )

        print(
            'python dub.py "https://..." es'
        )

        sys.exit(1)

    video_url = sys.argv[1]

    target_language = (
        sys.argv[2]
        if len(sys.argv) > 2
        else "hi"
    ).lower()

    print()
    print(
        "============================================"
    )
    print(
        "        SYNCHRONIZED AI DUBBING"
    )
    print(
        "============================================"
    )
    print(
        f"Target language: {target_language}"
    )
    print(
        f"Global video speed: {GLOBAL_SPEED}x"
    )
    print(
        f"Maximum individual TTS speed: "
        f"{MAX_TTS_SPEED}x"
    )
    print(
        "============================================"
    )
    print()

    try:

        # ----------------------------------------------------
        # Download
        # ----------------------------------------------------

        video_file, audio_file, metadata = (
            download_media(video_url)
        )

        # ----------------------------------------------------
        # Transcribe + translate
        # ----------------------------------------------------

        segments = (
            transcribe_and_translate(
                audio_file,
                target_language
            )
        )

        if not segments:

            raise RuntimeError(
                "No speech segments detected."
            )

        print(
            f"\n📝 Processing "
            f"{len(segments)} segments..."
        )

        # ----------------------------------------------------
        # TTS
        # ----------------------------------------------------

        asyncio.run(
            synthesize_audio(
                segments
            )
        )

        # ----------------------------------------------------
        # Render
        # ----------------------------------------------------

        render_dubbed_video(
            video_file,
            segments,
            "final_output.mp4"
        )

        # ----------------------------------------------------
        # Subtitles
        # ----------------------------------------------------

        generate_srt(
            segments,
            "subtitles.srt"
        )

        print()
        print(
            "============================================"
        )
        print(
            "✅ DUBBING COMPLETED"
        )
        print(
            "============================================"
        )
        print(
            "Video : final_output.mp4"
        )
        print(
            "SRT   : subtitles.srt"
        )
        print(
            "============================================"
        )

    except Exception as e:

        print()
        print(
            "❌ DUBBING FAILED"
        )

        print(
            f"Error: {e}"
        )

        raise
