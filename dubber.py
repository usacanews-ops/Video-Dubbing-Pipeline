import os
import sys
import subprocess
import asyncio
import time
import re
import json
import shutil
import math

import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator
import edge_tts
from pydub import AudioSegment


# ============================================================
# AI DUBBING CONFIGURATION
# ============================================================

# ------------------------------------------------------------
# TTS SPEED
# ------------------------------------------------------------
#
# IMPORTANT:
# We intentionally keep TTS speed almost constant.
#
# The old system could make individual sentences extremely
# fast in order to fit them into the original video.
#
# This system does NOT do that.
#
# +8% means Edge TTS speaks slightly faster than normal.
#
# ------------------------------------------------------------

TTS_RATE = "+8%"

# Small optional adjustment range.
#
# The algorithm will choose between these rates if necessary,
# but it will NEVER use extreme compression.
#
MIN_RATE = 4
MAX_RATE = 12


# ------------------------------------------------------------
# VIDEO FREEZE
# ------------------------------------------------------------
#
# If dubbed speech is longer than the available visual slot,
# the last frame of that visual segment is frozen automatically.
#
# Example:
#
# Original visual slot = 4.0 seconds
# Dubbed speech        = 5.1 seconds
#
# Result:
#
# 4.0 sec normal video
# 1.1 sec frozen frame
#
# Then next visual segment starts.
#
# ------------------------------------------------------------

ENABLE_FREEZE = True


# ------------------------------------------------------------
# SMALL SAFETY GAP
# ------------------------------------------------------------

SAFETY_GAP = 0.03


# ------------------------------------------------------------
# SILENCE DETECTION
# ------------------------------------------------------------

SILENCE_THRESHOLD = -42


# ------------------------------------------------------------
# WHISPER MODEL
# ------------------------------------------------------------

WHISPER_MODEL = "tiny"


# ------------------------------------------------------------
# VIDEO ENCODING
# ------------------------------------------------------------

VIDEO_PRESET = "veryfast"
VIDEO_BITRATE = "2600k"
AUDIO_BITRATE = "192k"


# ============================================================
# EDGE TTS VOICES
# ============================================================

VOICE_MAP = {

    # Indian languages
    "hi": "hi-IN-MadhurNeural",
    "bn": "bn-IN-BashkarNeural",
    "ta": "ta-IN-ValluvarNeural",
    "te": "te-IN-MohanNeural",
    "mr": "mr-IN-ManoharNeural",
    "gu": "gu-IN-DhwaniNeural",
    "kn": "kn-IN-GaganNeural",
    "ml": "ml-IN-MidhunNeural",
    "pa": "pa-IN-OjasNeural",

    # English
    "en": "en-US-GuyNeural",

    # European
    "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural",
    "es": "es-ES-AlvaroNeural",
    "it": "it-IT-DiegoNeural",
    "pt": "pt-BR-AntonioNeural",
    "nl": "nl-NL-MaartenNeural",
    "pl": "pl-PL-MarekNeural",
    "tr": "tr-TR-AhmetNeural",

    # Slavic
    "ru": "ru-RU-DmitryNeural",
    "uk": "uk-UA-OstapNeural",

    # Asian
    "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural",
    "zh": "zh-CN-YunxiNeural",

    # Middle East
    "ar": "ar-SA-HamedNeural",
}


# ============================================================
# UTILITY
# ============================================================

def run_command(cmd, quiet=False):

    if quiet:

        return subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )

    return subprocess.run(
        cmd,
        check=True
    )


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
            file_path
        ]
    )

    return float(result.strip())


# ============================================================
# 1. DOWNLOAD MEDIA
# ============================================================

def download_media(url: str):

    video_path = "raw_source.mp4"
    audio_path = "input_audio.wav"

    for path in [
        video_path,
        audio_path
    ]:

        if os.path.exists(path):

            try:
                os.remove(path)
            except OSError:
                pass

    ydl_opts = {

        "format":
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",

        "outtmpl":
            video_path,

        "merge_output_format":
            "mp4",

        "quiet":
            True,

        "no_warnings":
            True,
    }

    print("📥 Downloading media...")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        meta = ydl.extract_info(
            url,
            download=True
        )

        meta_dict = {

            "title":
                meta.get(
                    "title",
                    "Dubbed Video"
                ),

            "description":
                meta.get(
                    "description",
                    ""
                ),

            "tags":
                meta.get(
                    "tags",
                    []
                ),
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

    print(
        f"TITLE_EMIT: {meta_dict['title']}"
    )

    # --------------------------------------------------------
    # Extract Whisper audio
    # --------------------------------------------------------

    print(
        "🎵 Extracting source audio..."
    )

    run_command(
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
            audio_path
        ],
        quiet=True
    )

    return (
        video_path,
        audio_path,
        meta_dict
    )


# ============================================================
# 2. TRANSLATION HELPERS
# ============================================================

def contains_devanagari(text: str) -> bool:

    return bool(
        re.search(
            r"[\u0900-\u097F]",
            text
        )
    )


def is_error_page(text: str) -> bool:

    lowered = text.lower()

    return any(
        x in lowered
        for x in [
            "server error",
            "<!doctype",
            "<html",
            "captcha",
            "unusual traffic"
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

    # --------------------------------------------------------
    # Hindi
    # --------------------------------------------------------

    if target_lang == "hi":

        for attempt in range(3):

            try:

                result = GoogleTranslator(
                    source="auto",
                    target="hi"
                ).translate(
                    clean_text
                )

                if (
                    result
                    and not is_error_page(result)
                    and contains_devanagari(result)
                ):

                    return result.strip()

            except Exception:

                time.sleep(0.3)

        # MyMemory fallback

        try:

            result = MyMemoryTranslator(
                source="auto",
                target="hi-IN"
            ).translate(
                clean_text
            )

            if (
                result
                and not is_error_page(result)
            ):

                return result.strip()

        except Exception:

            pass

        return ""

    # --------------------------------------------------------
    # Other languages
    # --------------------------------------------------------

    for attempt in range(3):

        try:

            result = GoogleTranslator(
                source="auto",
                target=target_lang
            ).translate(
                clean_text
            )

            if (
                result
                and not is_error_page(result)
            ):

                return result.strip()

        except Exception:

            time.sleep(0.3)

    return clean_text


# ============================================================
# 3. TRANSCRIPTION + TRANSLATION
# ============================================================

def transcribe_and_translate(
    audio_path: str,
    target_lang: str
):

    print(
        "🧠 Loading Whisper..."
    )

    model = WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8"
    )

    print(
        "🎙️ Transcribing source audio..."
    )

    raw_segments, info = model.transcribe(

        audio_path,

        language=None,

        vad_filter=True,

        vad_parameters={
            "min_silence_duration_ms": 350
        }
    )

    print(
        f"🌍 Detected source language: "
        f"{info.language}"
    )

    raw_list = []

    for segment in raw_segments:

        text = segment.text.strip()

        if not text:
            continue

        duration = (
            segment.end
            - segment.start
        )

        if duration < 0.15:
            continue

        raw_list.append(
            {
                "start":
                    float(segment.start),

                "end":
                    float(segment.end),

                "text":
                    text
            }
        )

    if not raw_list:

        return []

    print(
        f"📝 Whisper found "
        f"{len(raw_list)} speech segments."
    )

    segments = []

    first_preview = False

    for i, source in enumerate(raw_list):

        start = source["start"]

        # ----------------------------------------------------
        # Determine source visual/dialogue slot.
        #
        # The next speech start is used as the natural
        # boundary for this segment.
        # ----------------------------------------------------

        if i + 1 < len(raw_list):

            next_start = (
                raw_list[i + 1]["start"]
            )

            source_slot = (
                next_start
                - start
                - SAFETY_GAP
            )

        else:

            source_slot = (
                source["end"]
                - start
                + 2.0
            )

        source_slot = max(
            0.25,
            source_slot
        )

        translated = translate_text(
            source["text"],
            target_lang
        )

        if not translated:

            translated = source["text"]

        if not first_preview:

            print(
                "TRANSLATION_PREVIEW: "
                + translated[:120]
            )

            first_preview = True

        segments.append(
            {

                "index":
                    i,

                "start":
                    start,

                "source_end":
                    source["end"],

                "source_slot":
                    source_slot,

                "source_text":
                    source["text"],

                "translated_text":
                    translated,

                "target_lang":
                    target_lang
            }
        )

    return segments


# ============================================================
# 4. REMOVE TTS SILENCE
# ============================================================

def strip_dead_silence(
    input_file: str,
    output_file: str
):

    audio = AudioSegment.from_file(
        input_file
    )

    if len(audio) == 0:

        return

    start_trim = 0
    end_trim = len(audio)

    # --------------------------------------------------------
    # Find beginning of actual speech.
    # --------------------------------------------------------

    for pos in range(
        0,
        len(audio),
        10
    ):

        chunk = audio[
            pos:
            pos + 10
        ]

        if chunk.dBFS > SILENCE_THRESHOLD:

            start_trim = max(
                0,
                pos - 20
            )

            break

    # --------------------------------------------------------
    # Find end of actual speech.
    # --------------------------------------------------------

    for pos in range(
        len(audio) - 10,
        0,
        -10
    ):

        chunk = audio[
            pos:
            pos + 10
        ]

        if chunk.dBFS > SILENCE_THRESHOLD:

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
# 5. TTS RATE
# ============================================================

def rate_string(percent: int) -> str:

    if percent >= 0:

        return f"+{percent}%"

    return f"{percent}%"


# ============================================================
# 6. SYNTHESIZE ONE TTS CLIP
# ============================================================

async def synthesize_one(
    text: str,
    voice: str,
    rate: str,
    output_file: str
):

    communicator = edge_tts.Communicate(
        text,
        voice,
        rate=rate
    )

    await communicator.save(
        output_file
    )


# ============================================================
# 7. SYNTHESIZE ALL AUDIO
# ============================================================

async def synthesize_audio(
    segments,
    temp_dir="temp_audio"
):

    if os.path.exists(temp_dir):

        shutil.rmtree(
            temp_dir
        )

    os.makedirs(
        temp_dir,
        exist_ok=True
    )

    for i, seg in enumerate(segments):

        text = (
            seg
            .get(
                "translated_text",
                ""
            )
            .strip()
        )

        if not text:

            seg["clip_file"] = None

            continue

        target_lang = (
            seg
            .get(
                "target_lang",
                "hi"
            )
            .lower()
        )

        base_lang = (
            target_lang
            .split("-")[0]
        )

        voice = VOICE_MAP.get(
            base_lang
        )

        if not voice:

            print(
                f"⚠️ No configured Edge voice "
                f"for {target_lang}; "
                f"using English."
            )

            voice = "en-US-GuyNeural"

        raw_file = os.path.join(
            temp_dir,
            f"raw_{i}.mp3"
        )

        stripped_file = os.path.join(
            temp_dir,
            f"strip_{i}.wav"
        )

        # ----------------------------------------------------
        # Generate using constant speed.
        # ----------------------------------------------------

        print(
            f"🔊 Generating TTS "
            f"{i + 1}/{len(segments)} "
            f"at {TTS_RATE}"
        )

        try:

            await synthesize_one(
                text,
                voice,
                TTS_RATE,
                raw_file
            )

            strip_dead_silence(
                raw_file,
                stripped_file
            )

            duration = get_duration(
                stripped_file
            )

            seg["clip_file"] = (
                stripped_file
            )

            seg["tts_duration"] = (
                duration
            )

            print(
                f"   TTS duration: "
                f"{duration:.2f}s"
            )

        except Exception as e:

            print(
                f"⚠️ TTS error "
                f"on segment {i}: {e}"
            )

            seg["clip_file"] = None


# ============================================================
# 8. CREATE AUDIO TIMELINE
# ============================================================
#
# This is the heart of the synchronization system.
#
# We do NOT compress speech aggressively.
#
# We calculate:
#
#     original slot
#     versus
#     dubbed duration
#
# If dubbed duration is longer:
#
#     video must be extended.
#
# ============================================================

def calculate_dub_timeline(
    segments
):

    current_time = 0.0

    for i, seg in enumerate(segments):

        original_start = (
            seg["start"]
        )

        original_slot = (
            seg["source_slot"]
        )

        tts_duration = (
            seg.get(
                "tts_duration",
                0.0
            )
        )

        # ----------------------------------------------------
        # For the first segment, preserve the original opening
        # position.
        #
        # For later segments, the previous extended segment
        # determines their NEW position.
        # ----------------------------------------------------

        if i == 0:

            new_start = original_start

        else:

            new_start = current_time

        # ----------------------------------------------------
        # The dubbed sentence is allowed to use its natural
        # TTS duration.
        # ----------------------------------------------------

        speech_end = (
            new_start
            + tts_duration
        )

        # ----------------------------------------------------
        # Original visual segment end.
        #
        # We use source start + source slot.
        # ----------------------------------------------------

        original_end = (
            original_start
            + original_slot
        )

        # ----------------------------------------------------
        # The visual segment normally lasts the original slot.
        #
        # If TTS is longer, freeze is required.
        # ----------------------------------------------------

        visual_end = max(
            new_start
            + original_slot,
            speech_end
        )

        freeze_duration = max(
            0.0,
            speech_end
            - (
                new_start
                + original_slot
            )
        )

        seg["new_start"] = (
            new_start
        )

        seg["speech_end"] = (
            speech_end
        )

        seg["original_visual_end"] = (
            original_end
        )

        seg["visual_end"] = (
            visual_end
        )

        seg["freeze_duration"] = (
            freeze_duration
        )

        current_time = (
            visual_end
        )

        print(
            f"⏱ Segment {i + 1}: "
            f"start={new_start:.3f}s | "
            f"TTS={tts_duration:.3f}s | "
            f"original slot={original_slot:.3f}s | "
            f"freeze={freeze_duration:.3f}s | "
            f"next={current_time:.3f}s"
        )

    return segments


# ============================================================
# 9. BUILD MASTER DUBBED AUDIO
# ============================================================

def build_master_audio(
    segments,
    total_duration,
    output_file
):

    print(
        "\n🎧 Building extended dubbed "
        "audio timeline..."
    )

    master = AudioSegment.silent(
        duration=int(
            (
                total_duration
                + 0.5
            )
            * 1000
        ),
        frame_rate=44100
    )

    master = (
        master
        .set_frame_rate(44100)
        .set_channels(2)
    )

    for seg in segments:

        clip_file = (
            seg.get(
                "clip_file"
            )
        )

        if not clip_file:
            continue

        if not os.path.exists(
            clip_file
        ):
            continue

        clip = (
            AudioSegment
            .from_file(
                clip_file
            )
            .set_frame_rate(44100)
            .set_channels(2)
        )

        position = int(
            seg["new_start"]
            * 1000
        )

        # ----------------------------------------------------
        # Overlay at NEW timeline position.
        # ----------------------------------------------------

        master = master.overlay(
            clip,
            position=position
        )

        seg["final_start"] = (
            seg["new_start"]
        )

        seg["final_end"] = (
            seg["speech_end"]
        )

    master.export(
        output_file,
        format="wav"
    )

    return output_file


# ============================================================
# 10. GET VIDEO FPS
# ============================================================

def get_video_fps(
    video_file
):

    result = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=r_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_file
        ]
    ).decode().strip()

    if "/" in result:

        num, den = result.split(
            "/",
            1
        )

        try:

            return (
                float(num)
                /
                float(den)
            )

        except Exception:

            return 30.0

    try:

        return float(result)

    except Exception:

        return 30.0


# ============================================================
# 11. GET VIDEO RESOLUTION
# ============================================================

def get_video_info(
    video_file
):

    result = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            video_file
        ]
    ).decode().strip()

    if "x" in result:

        width, height = result.split(
            "x"
        )

        return (
            int(width),
            int(height)
        )

    return (
        1920,
        1080
    )


# ============================================================
# 12. BUILD VIDEO SEGMENT
# ============================================================
#
# Each original segment is exported as a temporary video.
#
# If the dubbed sentence requires additional time:
#
#     normal video
#          +
#     frozen last frame
#
# This is exactly the behavior requested.
#
# ============================================================

def create_video_part(
    video_file,
    source_start,
    source_end,
    freeze_duration,
    output_file
):

    duration = (
        source_end
        - source_start
    )

    if duration <= 0:

        return False

    # --------------------------------------------------------
    # Normal video segment.
    # --------------------------------------------------------

    filters = [

        f"trim=start={source_start:.6f}:"
        f"end={source_end:.6f}",

        "setpts=PTS-STARTPTS"
    ]

    # --------------------------------------------------------
    # If required, clone the final frame.
    #
    # tpad stop_mode=clone creates a freeze-frame extension.
    # --------------------------------------------------------

    if (
        ENABLE_FREEZE
        and freeze_duration > 0.01
    ):

        filters.append(
            "tpad="
            "stop_mode=clone:"
            f"stop_duration={freeze_duration:.6f}"
        )

    filter_string = ",".join(
        filters
    )

    cmd = [

        "ffmpeg",
        "-y",

        "-i",
        video_file,

        "-an",

        "-vf",
        filter_string,

        "-r",
        "30",

        "-c:v",
        "libx264",

        "-preset",
        VIDEO_PRESET,

        "-b:v",
        VIDEO_BITRATE,

        "-pix_fmt",
        "yuv420p",

        output_file
    ]

    run_command(
        cmd,
        quiet=True
    )

    return True


# ============================================================
# 13. CONCATENATE VIDEO PARTS
# ============================================================

def concatenate_video_parts(
    parts,
    output_file
):

    concat_file = (
        "video_concat.txt"
    )

    with open(
        concat_file,
        "w",
        encoding="utf-8"
    ) as f:

        for part in parts:

            absolute = os.path.abspath(
                part
            ).replace(
                "\\",
                "/"
            )

            # Escape single quotes for concat demuxer.
            absolute = absolute.replace(
                "'",
                "'\\''"
            )

            f.write(
                f"file '{absolute}'\n"
            )

    cmd = [

        "ffmpeg",
        "-y",

        "-f",
        "concat",

        "-safe",
        "0",

        "-i",
        concat_file,

        "-an",

        "-c:v",
        "libx264",

        "-preset",
        VIDEO_PRESET,

        "-b:v",
        VIDEO_BITRATE,

        "-pix_fmt",
        "yuv420p",

        output_file
    ]

    run_command(
        cmd
    )

    if os.path.exists(
        concat_file
    ):

        os.remove(
            concat_file
        )


# ============================================================
# 14. BUILD COMPLETE EXTENDED VIDEO
# ============================================================
#
# This creates:
#
#     opening
#     sentence 1 video
#     freeze if required
#     sentence 2 video
#     freeze if required
#     sentence 3 video
#     ...
#     remaining source video
#
# ============================================================

def build_extended_video(
    video_file,
    segments,
    output_file
):

    source_duration = get_duration(
        video_file
    )

    print(
        "\n🎬 Building freeze-frame "
        "extended video..."
    )

    temp_video_dir = (
        "temp_video_parts"
    )

    if os.path.exists(
        temp_video_dir
    ):

        shutil.rmtree(
            temp_video_dir
        )

    os.makedirs(
        temp_video_dir
    )

    parts = []

    # --------------------------------------------------------
    # We create pieces based on ORIGINAL source locations.
    # --------------------------------------------------------

    previous_source_end = 0.0

    for i, seg in enumerate(
        segments
    ):

        source_start = (
            seg["start"]
        )

        # ----------------------------------------------------
        # Opening section before first speech.
        # ----------------------------------------------------

        if (
            i == 0
            and source_start > 0.01
        ):

            opening_file = os.path.join(
                temp_video_dir,
                "part_000_opening.mp4"
            )

            print(
                f"🎞️ Opening: "
                f"0.000 → "
                f"{source_start:.3f}"
            )

            create_video_part(
                video_file,
                0.0,
                source_start,
                0.0,
                opening_file
            )

            parts.append(
                opening_file
            )

        # ----------------------------------------------------
        # Determine original visual end.
        #
        # Prefer next speech start.
        # ----------------------------------------------------

        if i + 1 < len(segments):

            source_end = (
                segments[i + 1]["start"]
            )

        else:

            source_end = source_duration

        source_end = min(
            source_end,
            source_duration
        )

        if source_end <= source_start:

            continue

        freeze_duration = (
            seg.get(
                "freeze_duration",
                0.0
            )
        )

        part_file = os.path.join(
            temp_video_dir,
            f"part_{i + 1:04d}.mp4"
        )

        print(
            f"🎞️ Segment {i + 1}: "
            f"{source_start:.3f} → "
            f"{source_end:.3f} "
            f"+ freeze "
            f"{freeze_duration:.3f}s"
        )

        create_video_part(
            video_file,
            source_start,
            source_end,
            freeze_duration,
            part_file
        )

        parts.append(
            part_file
        )

        previous_source_end = (
            source_end
        )

    # --------------------------------------------------------
    # If Whisper didn't produce speech near the end,
    # the final source portion is still preserved.
    # --------------------------------------------------------

    if (
        previous_source_end
        < source_duration - 0.01
        and not segments
    ):

        final_part = os.path.join(
            temp_video_dir,
            "part_final.mp4"
        )

        create_video_part(
            video_file,
            0.0,
            source_duration,
            0.0,
            final_part
        )

        parts.append(
            final_part
        )

    if not parts:

        raise RuntimeError(
            "No video parts were created."
        )

    # --------------------------------------------------------
    # Concatenate all visual pieces.
    # --------------------------------------------------------

    concatenate_video_parts(
        parts,
        output_file
    )

    return output_file


# ============================================================
# 15. FINAL AUDIO + VIDEO MUX
# ============================================================

def mux_final_video(
    extended_video,
    dubbed_audio,
    output_file
):

    print(
        "\n🎬 Muxing dubbed audio..."
    )

    cmd = [

        "ffmpeg",
        "-y",

        "-i",
        extended_video,

        "-i",
        dubbed_audio,

        "-map",
        "0:v:0",

        "-map",
        "1:a:0",

        "-c:v",
        "copy",

        "-c:a",
        "aac",

        "-b:a",
        AUDIO_BITRATE,

        "-shortest",

        "-map_metadata",
        "-1",

        "-map_chapters",
        "-1",

        output_file
    ]

    run_command(
        cmd
    )


# ============================================================
# 16. FORMAT SRT TIMESTAMP
# ============================================================

def format_timestamp(
    seconds
):

    seconds = max(
        0.0,
        seconds
    )

    total_ms = int(
        round(
            seconds * 1000
        )
    )

    hours = (
        total_ms
        // 3600000
    )

    total_ms %= 3600000

    minutes = (
        total_ms
        // 60000
    )

    total_ms %= 60000

    secs = (
        total_ms
        // 1000
    )

    millis = (
        total_ms
        % 1000
    )

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d},"
        f"{millis:03d}"
    )


# ============================================================
# 17. GENERATE SYNCHRONIZED SRT
# ============================================================

def generate_srt(
    segments,
    output_file="subtitles.srt"
):

    print(
        "\n📝 Creating synchronized subtitles..."
    )

    with open(
        output_file,
        "w",
        encoding="utf-8"
    ) as f:

        subtitle_index = 1

        for seg in segments:

            text = (
                seg
                .get(
                    "translated_text",
                    ""
                )
                .strip()
            )

            if not text:
                continue

            start = (
                seg
                .get(
                    "final_start",
                    seg["new_start"]
                )
            )

            end = (
                seg
                .get(
                    "final_end",
                    seg["speech_end"]
                )
            )

            if end <= start:

                end = (
                    start
                    + 0.5
                )

            f.write(
                f"{subtitle_index}\n"
                f"{format_timestamp(start)} --> "
                f"{format_timestamp(end)}\n"
                f"{text}\n\n"
            )

            subtitle_index += 1


# ============================================================
# 18. CLEANUP
# ============================================================

def cleanup():

    directories = [

        "temp_audio",
        "temp_video_parts"
    ]

    files = [

        "raw_source.mp4",
        "input_audio.wav",
        "synced_master.wav",
        "extended_video.mp4",
        "video_concat.txt"
    ]

    for directory in directories:

        if os.path.exists(
            directory
        ):

            try:

                shutil.rmtree(
                    directory
                )

            except Exception:

                pass

    for file in files:

        if os.path.exists(
            file
        ):

            try:

                os.remove(
                    file
                )

            except Exception:

                pass


# ============================================================
# 19. MAIN
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
            'python dub.py "https://..." en'
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
        "=================================================="
    )
    print(
        "          AI DUBBING ENGINE"
    )
    print(
        "=================================================="
    )
    print(
        f"Target language       : {target_language}"
    )
    print(
        f"TTS base rate         : {TTS_RATE}"
    )
    print(
        f"Allowed rate range    : "
        f"+{MIN_RATE}% to +{MAX_RATE}%"
    )
    print(
        f"Freeze-frame extension: {ENABLE_FREEZE}"
    )
    print(
        "=================================================="
    )
    print()

    try:

        # ====================================================
        # STEP 1
        # ====================================================

        video_file, audio_file, metadata = (
            download_media(
                video_url
            )
        )

        # ====================================================
        # STEP 2
        # ====================================================

        segments = (
            transcribe_and_translate(
                audio_file,
                target_language
            )
        )

        if not segments:

            raise RuntimeError(
                "No speech segments were detected."
            )

        # ====================================================
        # STEP 3
        # ====================================================

        asyncio.run(
            synthesize_audio(
                segments
            )
        )

        # ====================================================
        # STEP 4
        #
        # Calculate new extended timeline.
        # ====================================================

        print(
            "\n⏱️ Calculating dubbing timeline..."
        )

        calculate_dub_timeline(
            segments
        )

        # ====================================================
        # STEP 5
        #
        # Calculate total final duration.
        # ====================================================

        final_duration = max(

            get_duration(
                video_file
            ),

            max(
                (
                    seg["visual_end"]
                    for seg in segments
                ),
                default=0.0
            )
        )

        print(
            f"\n📏 Final timeline duration: "
            f"{final_duration:.3f}s"
        )

        # ====================================================
        # STEP 6
        #
        # Build master audio.
        # ====================================================

        master_audio = (
            "synced_master.wav"
        )

        build_master_audio(
            segments,
            final_duration,
            master_audio
        )

        # ====================================================
        # STEP 7
        #
        # Build extended/frozen video.
        # ====================================================

        extended_video = (
            "extended_video.mp4"
        )

        build_extended_video(
            video_file,
            segments,
            extended_video
        )

        # ====================================================
        # STEP 8
        #
        # Mux audio and video.
        # ====================================================

        mux_final_video(
            extended_video,
            master_audio,
            "final_output.mp4"
        )

        # ====================================================
        # STEP 9
        #
        # SRT
        # ====================================================

        generate_srt(
            segments,
            "subtitles.srt"
        )

        # ====================================================
        # STEP 10
        # ====================================================

        actual_final_duration = (
            get_duration(
                "final_output.mp4"
            )
        )

        print()
        print(
            "=================================================="
        )
        print(
            "              ✅ DUBBING COMPLETE"
        )
        print(
            "=================================================="
        )
        print(
            f"Original duration : "
            f"{get_duration(video_file):.2f}s"
        )
        print(
            f"Final duration    : "
            f"{actual_final_duration:.2f}s"
        )
        print(
            f"Extended by       : "
            f"{max(0, actual_final_duration - get_duration(video_file)):.2f}s"
        )
        print()
        print(
            "Video : final_output.mp4"
        )
        print(
            "SRT   : subtitles.srt"
        )
        print(
            "=================================================="
        )

        # ----------------------------------------------------
        # Remove temporary files.
        # ----------------------------------------------------

        cleanup()

    except Exception as e:

        print()
        print(
            "=================================================="
        )
        print(
            "❌ DUBBING FAILED"
        )
        print(
            "=================================================="
        )
        print(
            f"Error: {e}"
        )

        raise
