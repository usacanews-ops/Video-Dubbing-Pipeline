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

    for path in (
        video_path,
        audio_path
    ):
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
        "title": info.get(
            "title",
            "Dubbed Video"
        ),
        "description": info.get(
            "description",
            ""
        ),
        "tags": info.get(
            "tags",
            []
        )
    }

    with open(
        "source_meta.json",
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            metadata,
            f,
            ensure_ascii=False,
            indent=2
        )

    print(
        "TITLE_EMIT: " +
        metadata["title"]
    )

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

    return (
        video_path,
        audio_path,
        metadata
    )


# ============================================================
# TEXT / LANGUAGE
# ============================================================

def contains_hindi(text):
    return bool(
        text and
        re.search(
            r"[\u0900-\u097F]",
            text
        )
    )


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

    return any(
        item in value
        for item in bad_values
    )


# ============================================================
# TRANSLATION
# ============================================================

def translate_text(
    text,
    target
):

    text = clean_text(text)

    if len(text) < 2:
        return text

    # Primary translator
    for attempt in range(3):

        try:

            result = GoogleTranslator(
                source="auto",
                target=target
            ).translate(text)

            if (
                result and
                not invalid_translation(result)
            ):

                if (
                    target != "hi" or
                    contains_hindi(result)
                ):
                    return result.strip()

        except Exception as e:

            print(
                f"⚠️ Translation retry "
                f"{attempt + 1}/3: {e}"
            )

            time.sleep(0.5)

    # Fallback translator
    try:

        result = MyMemoryTranslator(
            source="auto",
            target=target
        ).translate(text)

        if (
            result and
            not invalid_translation(result)
        ):

            if (
                target != "hi" or
                contains_hindi(result)
            ):
                return result.strip()

    except Exception as e:

        print(
            "⚠️ Fallback translation:",
            e
        )

    # CRITICAL:
    # Never return an empty dialogue.
    return text


# ============================================================
# NATURAL SPOKEN HINDI
# ============================================================

def naturalize_hindi(text):

    text = clean_text(text)

    if not NATURALIZE_HINDI:
        return text

    if not contains_hindi(text):
        return text

    api_key = os.environ.get(
        "GEMINI_API_KEY"
    )

    if not api_key:
        print(
            "ℹ️ GEMINI_API_KEY not set; "
            "using translated Hindi."
        )
        return text

    prompt = f"""
Rewrite the following Hindi for natural,
simple, everyday spoken Indian Hindi suitable
for video dubbing.

This is NOT a new translation.

Keep the exact meaning.

Rules:
- Use common everyday Hindi.
- Avoid difficult Sanskrit words.
- Avoid literary Hindi.
- Avoid unnecessarily formal Hindi.
- Make it sound like a normal Indian person speaking.
- Common English words are allowed where natural.
- Keep names, places, brands and numbers unchanged.
- Do not add information.
- Do not remove information.
- Return ONLY the rewritten Hindi.

Hindi:
{text}
"""

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 300
        }
    }

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/" +
        GEMINI_MODEL +
        ":generateContent"
    )

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8"),
        headers={
            "Content-Type":
                "application/json",
            "x-goog-api-key":
                api_key
        },
        method="POST"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            result = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

        candidates = result.get(
            "candidates",
            []
        )

        if not candidates:
            return text

        parts = (
            candidates[0]
            .get("content", {})
            .get("parts", [])
        )

        output = "".join(
            part.get(
                "text",
                ""
            )
            for part in parts
        ).strip()

        output = re.sub(
            r"^```(?:text|hindi)?\s*",
            "",
            output,
            flags=re.I
        )

        output = re.sub(
            r"\s*```$",
            "",
            output
        ).strip()

        if (
            output and
            contains_hindi(output) and
            len(output) < 3000
        ):
            return output

    except urllib.error.HTTPError as e:

        print(
            f"⚠️ Gemini HTTP error: "
            f"{e.code}"
        )

    except Exception as e:

        print(
            "⚠️ Gemini error:",
            e
        )

    return text


# ============================================================
# WHISPER TRANSCRIPTION
# ============================================================

def transcribe(
    audio_path,
    target_lang
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
        "🎙️ Transcribing..."
    )

    result, info = model.transcribe(
        audio_path,
        language=None,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 300
        }
    )

    print(
        "🌍 Detected language:",
        info.language
    )

    raw = []

    for item in result:

        text = clean_text(
            item.text
        )

        start = float(
            item.start
        )

        end = float(
            item.end
        )

        if not text:
            continue

        if end <= start:
            continue

        if end - start < 0.20:
            continue

        raw.append(
            {
                "start": start,
                "end": end,
                "text": text
            }
        )

    if not raw:
        return []

    # --------------------------------------------------------
    # Group Whisper fragments into dialogue sentences.
    # --------------------------------------------------------

    blocks = []

    current_text = []
    current_start = raw[0]["start"]
    current_end = raw[0]["end"]

    for i, item in enumerate(raw):

        current_text.append(
            item["text"]
        )

        current_end = item["end"]

        punctuation = item[
            "text"
        ].rstrip().endswith(
            (
                ".",
                "!",
                "?",
                "।",
                "…"
            )
        )

        if i + 1 < len(raw):

            gap = (
                raw[i + 1]["start"]
                - item["end"]
            )

        else:

            gap = 0

        long_sentence = (
            current_end -
            current_start
            >= 8.0
        )

        last_item = (
            i == len(raw) - 1
        )

        if (
            punctuation or
            gap >= 0.70 or
            long_sentence or
            last_item
        ):

            text = clean_text(
                " ".join(
                    current_text
                )
            )

            if text:

                blocks.append(
                    {
                        "start":
                            current_start,
                        "end":
                            current_end,
                        "text":
                            text
                    }
                )

            current_text = []

            if i + 1 < len(raw):

                current_start = (
                    raw[i + 1]["start"]
                )

    print(
        f"📝 Dialogue blocks: "
        f"{len(blocks)}"
    )

    segments = []

    for i, block in enumerate(
        blocks
    ):

        start = block["start"]

        # ----------------------------------------------------
        # The next dialogue START is the hard timing anchor.
        # ----------------------------------------------------

        if i + 1 < len(blocks):

            next_start = (
                blocks[i + 1]["start"]
            )

            slot = (
                next_start -
                start
            )

        else:

            slot = (
                block["end"] -
                start
            )

        slot = max(
            0.30,
            slot
        )

        translated = translate_text(
            block["text"],
            target_lang
        )

        if not translated:
            translated = block["text"]

        if target_lang == "hi":

            print(
                f"🗣️ Natural Hindi "
                f"{i + 1}/{len(blocks)}"
            )

            translated = naturalize_hindi(
                translated
            )

            if not translated:
                translated = block["text"]

        segments.append(
            {
                "index": i,
                "source_start":
                    start,
                "source_end":
                    block["end"],
                "slot":
                    slot,
                "source_text":
                    block["text"],
                "translated_text":
                    translated,
                "target_lang":
                    target_lang
            }
        )

    return segments


# ============================================================
# EDGE TTS
# ============================================================

async def make_tts(
    text,
    voice,
    output
):

    communicator = edge_tts.Communicate(
        text,
        voice,
        rate=TTS_RATE
    )

    await communicator.save(
        output
    )


def tts_with_retry(
    text,
    voice,
    output
):

    for attempt in range(
        1,
        TTS_RETRIES + 1
    ):

        try:

            if os.path.exists(output):
                os.remove(output)

            asyncio.run(
                make_tts(
                    text,
                    voice,
                    output
                )
            )

            if (
                os.path.exists(output) and
                os.path.getsize(output) > 1000
            ):

                d = get_duration(
                    output
                )

                if d > 0.05:
                    return True

        except Exception as e:

            print(
                f"⚠️ TTS "
                f"{attempt}/{TTS_RETRIES}: "
                f"{e}"
            )

        time.sleep(0.5)

    return False


# ============================================================
# AUDIO CLEANUP
# ============================================================

def clean_audio(
    source,
    output
):

    audio = AudioSegment.from_file(
        source
    )

    if len(audio) <= 20:

        audio.export(
            output,
            format="wav"
        )

        return

    # Only trim beginning/end.
    # Internal silence is preserved.
    try:

        audio = audio.strip_silence(
            silence_len=60,
            silence_thresh=-48,
            padding=25
        )

    except Exception:
        pass

    audio = (
        audio
        .set_frame_rate(44100)
        .set_channels(2)
    )

    audio.export(
        output,
        format="wav"
    )


def speed_audio(
    source,
    output,
    factor
):

    factor = max(
        1.0,
        min(
            float(factor),
            2.0
        )
    )

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

def prepare_tts(
    segments
):

    if os.path.exists(
        TEMP_DIR
    ):
        shutil.rmtree(
            TEMP_DIR
        )

    os.makedirs(
        TEMP_DIR,
        exist_ok=True
    )

    for i, segment in enumerate(
        segments
    ):

        text = clean_text(
            segment[
                "translated_text"
            ]
        )

        if not text:

            text = clean_text(
                segment[
                    "source_text"
                ]
            )

            segment[
                "translated_text"
            ] = text

        lang = (
            segment[
                "target_lang"
            ]
            .lower()
            .split("-")[0]
        )

        voice = VOICE_MAP.get(
            lang,
            "en-US-GuyNeural"
        )

        raw = os.path.join(
            TEMP_DIR,
            f"{i:04d}_raw.mp3"
        )

        clean = os.path.join(
            TEMP_DIR,
            f"{i:04d}_clean.wav"
        )

        fitted = os.path.join(
            TEMP_DIR,
            f"{i:04d}_fit.wav"
        )

        print(
            f"🔊 TTS "
            f"{i + 1}/{len(segments)}"
        )

        success = tts_with_retry(
            text,
            voice,
            raw
        )

        # ----------------------------------------------------
        # SECOND FALLBACK:
        # Use original transcription if translated TTS fails.
        # ----------------------------------------------------

        if not success:

            source_text = clean_text(
                segment[
                    "source_text"
                ]
            )

            if source_text:

                print(
                    "   ↳ TTS fallback "
                    "to source text"
                )

                success = tts_with_retry(
                    source_text,
                    voice,
                    raw
                )

        if not success:

            print(
                f"❌ TTS failed: "
                f"block {i + 1}"
            )

            segment[
                "clip_file"
            ] = None

            segment[
                "tts_duration"
            ] = 0.0

            segment[
                "tts_failed"
            ] = True

            continue

        try:

            clean_audio(
                raw,
                clean
            )

            original_duration = (
                get_duration(clean)
            )

            slot = max(
                0.30,
                float(
                    segment["slot"]
                )
            )

            # Required acceleration.
            required = (
                original_duration /
                slot
            )

            factor = max(
                1.0,
                required
            )

            factor = min(
                factor,
                MAX_SENTENCE_SPEED
            )

            if factor > 1.001:

                speed_audio(
                    clean,
                    fitted,
                    factor
                )

                final_file = fitted

                final_duration = (
                    get_duration(
                        fitted
                    )
                )

            else:

                final_file = clean

                final_duration = (
                    original_duration
                )

            if final_duration <= 0.05:

                raise RuntimeError(
                    "TTS clip is empty."
                )

            # ------------------------------------------------
            # Store ALL timing information explicitly.
            # ------------------------------------------------

            segment[
                "clip_file"
            ] = final_file

            segment[
                "tts_duration"
            ] = final_duration

            segment[
                "audio_speed"
            ] = factor

            segment[
                "tts_failed"
            ] = False

            # Extra time required after max TTS speed.
            segment[
                "freeze_duration"
            ] = max(
                0.0,
                final_duration - slot
            )

            print(
                f"   {original_duration:.2f}s "
                f"→ {final_duration:.2f}s | "
                f"slot {slot:.2f}s | "
                f"speed {factor:.3f}x"
            )

        except Exception as e:

            print(
                f"❌ Audio processing "
                f"failed for block "
                f"{i + 1}: {e}"
            )

            segment[
                "clip_file"
            ] = None

            segment[
                "tts_duration"
            ] = 0.0

            segment[
                "tts_failed"
            ] = True


# ============================================================
# BUILD AUDIO TIMELINE
# ============================================================

def calculate_timeline(
    segments,
    source_duration
):

    output_duration = (
        source_duration
    )

    for segment in segments:

        start = float(
            segment[
                "source_start"
            ]
        )

        clip_duration = float(
            segment.get(
                "tts_duration",
                0.0
            )
        )

        segment[
            "audio_start"
        ] = start

        segment[
            "audio_end"
        ] = (
            start +
            clip_duration
        )

        if clip_duration > 0:

            output_duration = max(
                output_duration,
                segment[
                    "audio_end"
                ]
            )

    return output_duration


# ============================================================
# MASTER AUDIO
# ============================================================

def build_master_audio(
    segments,
    duration,
    output
):

    print(
        "🎧 Building master audio..."
    )

    total_ms = int(
        math.ceil(
            (duration + 0.25) *
            1000
        )
    )

    master = (
        AudioSegment
        .silent(
            duration=total_ms,
            frame_rate=44100
        )
        .set_channels(2)
    )

    for i, segment in enumerate(
        segments
    ):

        clip_path = segment.get(
            "clip_file"
        )

        if (
            not clip_path or
            not os.path.exists(
                clip_path
            )
        ):

            print(
                f"⚠️ No audio for "
                f"block {i + 1}"
            )

            continue

        try:

            clip = (
                AudioSegment
                .from_file(
                    clip_path
                )
                .set_frame_rate(44100)
                .set_channels(2)
            )

            position = int(
                round(
                    segment[
                        "audio_start"
                    ] * 1000
                )
            )

            if position < 0:
                position = 0

            # ------------------------------------------------
            # Overlay is intentional.
            #
            # It prevents a sentence from replacing/cutting
            # another sentence if Whisper timings overlap
            # slightly.
            # ------------------------------------------------

            master = master.overlay(
                clip,
                position=position
            )

        except Exception as e:

            print(
                f"⚠️ Could not insert "
                f"audio block {i + 1}: "
                f"{e}"
            )

    master.export(
        output,
        format="wav"
    )

    print(
        "✅ Master audio created"
    )


# ============================================================
# VIDEO RENDERING
# ============================================================

def render_video(
    source,
    segments,
    source_duration,
    output
):

    print(
        "🎬 Rendering video..."
    )

    # --------------------------------------------------------
    # Every dialogue start becomes a video boundary.
    # --------------------------------------------------------

    starts = sorted(
        set(
            max(
                0.0,
                min(
                    source_duration,
                    float(
                        s["source_start"]
                    )
                )
            )
            for s in segments
        )
    )

    filters = []
    labels = []

    label_number = 0

    def new_label():

        nonlocal label_number

        label = (
            f"v{label_number}"
        )

        label_number += 1

        return label

    # --------------------------------------------------------
    # No dialogue.
    # --------------------------------------------------------

    if not starts:

        label = new_label()

        filters.append(
            "[0:v]"
            "setpts=PTS-STARTPTS"
            f"[{label}]"
        )

        labels.append(
            f"[{label}]"
        )

    else:

        # ----------------------------------------------------
        # Video before first dialogue.
        # ----------------------------------------------------

        first = starts[0]

        if first > 0.001:

            label = new_label()

            filters.append(
                "[0:v]"
                f"trim=start=0:"
                f"end={first:.6f},"
                "setpts=PTS-STARTPTS"
                f"[{label}]"
            )

            labels.append(
                f"[{label}]"
            )

        # ----------------------------------------------------
        # Dialogue sections.
        # ----------------------------------------------------

        for i, start in enumerate(
            starts
        ):

            if i + 1 < len(starts):

                end = starts[i + 1]

            else:

                end = source_duration

            if end <= start:
                continue

            # Find corresponding segment.
            segment = None

            for item in segments:

                if abs(
                    float(
                        item[
                            "source_start"
                        ]
                    ) - start
                ) < 0.02:

                    segment = item
                    break

            freeze = 0.0

            if segment:

                freeze = float(
                    segment.get(
                        "freeze_duration",
                        0.0
                    )
                )

            label = new_label()

            section = (
                "[0:v]"
                f"trim=start={start:.6f}:"
                f"end={end:.6f},"
                "setpts=PTS-STARTPTS"
            )

            # ------------------------------------------------
            # Freeze final frame when speech is longer than
            # the available source dialogue slot.
            # ------------------------------------------------

            if (
                FREEZE_ENABLED and
                freeze > 0.01
            ):

                section += (
                    ",tpad="
                    "stop_mode=clone:"
                    f"stop_duration="
                    f"{freeze:.6f}"
                )

            section += (
                f"[{label}]"
            )

            filters.append(
                section
            )

            labels.append(
                f"[{label}]"
            )

    # --------------------------------------------------------
    # Concatenate sections.
    # --------------------------------------------------------

    if len(labels) == 1:

        filters.append(
            labels[0] +
            "setpts=PTS-STARTPTS"
            "[joined]"
        )

    else:

        filters.append(
            "".join(labels) +
            f"concat=n={len(labels)}:"
            "v=1:a=0,"
            "setpts=PTS-STARTPTS"
            "[joined]"
        )

    # --------------------------------------------------------
    # Final 10% speed increase.
    # --------------------------------------------------------

    filters.append(
        "[joined]"
        f"setpts=PTS/{FINAL_SPEED:.6f}"
        "[vout]"
    )

    filter_complex = ";".join(
        filters
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            source,
            "-filter_complex",
            filter_complex,
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

    if not os.path.exists(
        output
    ):

        raise RuntimeError(
            "Video rendering failed."
        )


# ============================================================
# FINAL AUDIO + VIDEO
# ============================================================

def mux_final(
    video,
    audio,
    output
):

    print(
        "🎧 Muxing final MP4..."
    )

    temporary = (
        output +
        ".tmp.mp4"
    )

    if os.path.exists(
        temporary
    ):
        os.remove(
            temporary
        )

    # --------------------------------------------------------
    # Video has already been accelerated.
    # Apply the SAME acceleration to audio.
    # --------------------------------------------------------

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

    if not os.path.exists(
        temporary
    ):

        raise RuntimeError(
            "Final MP4 was not generated."
        )

    if os.path.exists(
        output
    ):
        os.remove(
            output
        )

    os.replace(
        temporary,
        output
    )


# ============================================================
# SRT
# ============================================================

def timestamp(seconds):

    milliseconds = max(
        0,
        int(
            round(
                seconds * 1000
            )
        )
    )

    hours = (
        milliseconds //
        3600000
    )

    milliseconds %= 3600000

    minutes = (
        milliseconds //
        60000
    )

    milliseconds %= 60000

    seconds_value = (
        milliseconds //
        1000
    )

    milliseconds %= 1000

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{seconds_value:02d},"
        f"{milliseconds:03d}"
    )


def write_srt(
    segments,
    output
):

    print(
        "📝 Writing SRT..."
    )

    counter = 0

    with open(
        output,
        "w",
        encoding="utf-8"
    ) as f:

        for segment in segments:

            text = clean_text(
                segment.get(
                    "translated_text",
                    ""
                )
            )

            if not text:
                continue

            counter += 1

            start = (
                float(
                    segment[
                        "source_start"
                    ]
                )
                / FINAL_SPEED
            )

            duration = max(
                0.30,
                float(
                    segment.get(
                        "tts_duration",
                        0.30
                    )
                )
            )

            end = (
                float(
                    segment[
                        "source_start"
                    ]
                )
                +
                duration
            ) / FINAL_SPEED

            f.write(
                str(counter) +
                "\n"
            )

            f.write(
                timestamp(start) +
                " --> " +
                timestamp(end) +
                "\n"
            )

            f.write(
                text +
                "\n\n"
            )

    print(
        f"✅ SRT: {counter} subtitles"
    )


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

                shutil.rmtree(
                    path
                )

            elif os.path.exists(path):

                os.remove(
                    path
                )

        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

def main():

    if len(sys.argv) < 2:

        print(
            'Usage: python dubber.py '
            '"VIDEO_URL" [language]'
        )

        sys.exit(1)

    url = sys.argv[1]

    target_lang = (
        sys.argv[2].lower()
        if len(sys.argv) > 2
        else "hi"
    )

    started = time.time()

    print()
    print("=" * 65)
    print("                 AI VIDEO DUBBER")
    print("=" * 65)
    print(
        "Target language:",
        target_lang
    )
    print(
        "Final speed:",
        FINAL_SPEED
    )
    print(
        "Max sentence speed:",
        MAX_SENTENCE_SPEED
    )
    print(
        "Freeze:",
        FREEZE_ENABLED
    )
    print(
        "Natural Hindi:",
        NATURALIZE_HINDI
    )
    print("=" * 65)
    print()

    try:

        # ====================================================
        # 1. DOWNLOAD
        # ====================================================

        (
            source_video,
            source_audio,
            metadata
        ) = download_media(
            url
        )

        source_duration = get_duration(
            source_video
        )

        print(
            f"Source duration: "
            f"{source_duration:.2f}s"
        )

        print(
            f"Source size: "
            f"{size_mb(source_video):.2f} MB"
        )

        # ====================================================
        # 2. TRANSCRIBE
        # ====================================================

        segments = transcribe(
            source_audio,
            target_lang
        )

        if not segments:

            raise RuntimeError(
                "No speech was detected."
            )

        # ====================================================
        # 3. TTS
        # ====================================================

        prepare_tts(
            segments
        )

        # ====================================================
        # 4. TIMELINE
        # ====================================================

        output_duration = (
            calculate_timeline(
                segments,
                source_duration
            )
        )

        print(
            f"Timeline duration: "
            f"{output_duration:.2f}s"
        )

        # ====================================================
        # 5. AUDIO
        # ====================================================

        master_audio = (
            "synced_master.wav"
        )

        build_master_audio(
            segments,
            output_duration,
            master_audio
        )

        # ====================================================
        # 6. SUBTITLES
        # ====================================================

        write_srt(
            segments,
            OUTPUT_SRT
        )

        # ====================================================
        # 7. VIDEO
        # ====================================================

        intermediate_video = (
            "extended_video.mp4"
        )

        render_video(
            source_video,
            segments,
            source_duration,
            intermediate_video
        )

        # ====================================================
        # 8. FINAL MP4
        # ====================================================

        mux_final(
            intermediate_video,
            master_audio,
            OUTPUT_VIDEO
        )

        # ====================================================
        # 9. VERIFY
        # ====================================================

        if not os.path.exists(
            OUTPUT_VIDEO
        ):

            raise RuntimeError(
                "final_output.mp4 "
                "was not created."
            )

        final_size = size_mb(
            OUTPUT_VIDEO
        )

        final_duration = get_duration(
            OUTPUT_VIDEO
        )

        failures = sum(
            1
            for s in segments
            if s.get(
                "tts_failed",
                False
            )
        )

        elapsed = (
            time.time() -
            started
        )

        print()
        print("=" * 65)
        print("                    COMPLETE")
        print("=" * 65)
        print(
            f"Source size : "
            f"{size_mb(source_video):.2f} MB"
        )
        print(
            f"Final size  : "
            f"{final_size:.2f} MB"
        )
        print(
            f"Source time : "
            f"{source_duration:.2f}s"
        )
        print(
            f"Final time  : "
            f"{final_duration:.2f}s"
        )
        print(
            f"Speed       : "
            f"{FINAL_SPEED:.2f}x"
        )
        print(
            f"TTS failures: "
            f"{failures}"
        )
        print(
            f"Processing  : "
            f"{elapsed / 60:.2f} minutes"
        )
        print()
        print(
            "🎬 final_output.mp4"
        )
        print(
            "📝 subtitles.srt"
        )
        print("=" * 65)

        cleanup()

    except Exception as e:

        print()
        print("=" * 65)
        print("                    FAILED")
        print("=" * 65)
        print(
            "❌",
            str(e)
        )
        print()
        print(
            "Temporary files have been kept."
        )
        print("=" * 65)

        raise


if __name__ == "__main__":
    main()
