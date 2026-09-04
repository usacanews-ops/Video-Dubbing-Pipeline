import os
import sys
import re
import json
import time
import shutil
import asyncio
import subprocess
import urllib.request
import urllib.error

import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator
import edge_tts
from pydub import AudioSegment


# ============================================================
# CONFIGURATION
# ============================================================

FINAL_SPEED = 1.10

# Maximum amount by which an individual Hindi sentence may
# be compressed to fit its original dialogue slot.
MAX_SENTENCE_SPEED = 1.12

# Edge TTS base rate.
TTS_RATE = "+5%"

WHISPER_MODEL = "tiny"

VIDEO_CRF = "27"
VIDEO_PRESET = "veryfast"
AUDIO_BITRATE = "128k"

TEMP_DIR = "temp_audio"

OUTPUT_VIDEO = "final_output.mp4"
OUTPUT_SRT = "subtitles.srt"

FREEZE_ENABLED = True

# Natural spoken Hindi conversion.
NATURALIZE_HINDI = True

GEMINI_MODEL = "gemini-2.5-flash"

# Small gaps below this value are considered part of the
# dialogue timeline and are filled automatically.
MAX_GAP_FILL = 0.35

# Number of TTS retries.
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
# COMMAND HELPERS
# ============================================================

def run(cmd, quiet=False):
    print(
        "▶ " +
        " ".join(
            f'"{x}"' if " " in str(x) else str(x)
            for x in cmd
        )
    )

    if quiet:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
    else:
        subprocess.run(
            cmd,
            check=True
        )


def get_duration(path):
    result = subprocess.check_output(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ],
        stderr=subprocess.STDOUT
    )

    return float(result.strip())


def file_size_mb(path):
    if not os.path.exists(path):
        return 0

    return os.path.getsize(path) / 1048576


# ============================================================
# DOWNLOAD
# ============================================================

def download_media(url):

    video_path = "raw_source.mp4"
    audio_path = "input_audio.wav"

    for path in [
        video_path,
        audio_path
    ]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass

    print("📥 Downloading media...")

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": video_path,
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
            ensure_ascii=False
        )

    print(
        "TITLE_EMIT: " +
        metadata["title"]
    )

    print("🎵 Extracting audio...")

    run(
        [
            "ffmpeg",
            "-y",
            "-i", video_path,
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
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
# TEXT HELPERS
# ============================================================

def contains_devanagari(text):
    return bool(
        text and
        re.search(
            r"[\u0900-\u097F]",
            text
        )
    )


def bad_translation(text):

    if not text:
        return True

    value = text.lower()

    bad = [
        "<html",
        "<!doctype",
        "server error",
        "captcha",
        "unusual traffic",
        "access denied"
    ]

    return any(
        x in value
        for x in bad
    )


# ============================================================
# TRANSLATION
# ============================================================

def translate_to_hindi(text):

    text = re.sub(
        r"[\r\n\t]+",
        " ",
        text
    ).strip()

    if len(text) < 2:
        return text

    for attempt in range(3):

        try:

            result = GoogleTranslator(
                source="auto",
                target="hi"
            ).translate(text)

            if (
                result and
                not bad_translation(result) and
                contains_devanagari(result)
            ):
                return result.strip()

        except Exception as e:

            print(
                f"⚠️ Hindi translation retry "
                f"{attempt + 1}/3"
            )

            time.sleep(0.5)

    try:

        result = MyMemoryTranslator(
            source="en-US",
            target="hi-IN"
        ).translate(text)

        if (
            result and
            not bad_translation(result) and
            contains_devanagari(result)
        ):
            return result.strip()

    except Exception:
        pass

    # IMPORTANT:
    # Never return an empty string.
    return text


def translate_text(
    text,
    target_lang
):

    if target_lang == "hi":
        return translate_to_hindi(text)

    text = re.sub(
        r"[\r\n\t]+",
        " ",
        text
    ).strip()

    if len(text) < 2:
        return text

    for attempt in range(3):

        try:

            result = GoogleTranslator(
                source="auto",
                target=target_lang
            ).translate(text)

            if (
                result and
                not bad_translation(result)
            ):
                return result.strip()

        except Exception:

            print(
                f"⚠️ Translation retry "
                f"{attempt + 1}/3"
            )

            time.sleep(0.5)

    # IMPORTANT:
    # Never create an empty dialogue.
    return text


# ============================================================
# NATURAL SPOKEN HINDI
# ============================================================

def naturalize_hindi(text):

    if not NATURALIZE_HINDI:
        return text

    if not text:
        return text

    if not contains_devanagari(text):
        return text

    api_key = os.environ.get(
        "GEMINI_API_KEY"
    )

    if not api_key:
        print(
            "⚠️ GEMINI_API_KEY unavailable. "
            "Keeping translated Hindi."
        )
        return text

    prompt = """
You are an expert Indian Hindi dubbing writer.

Rewrite the following Hindi into very natural,
simple, everyday spoken Indian Hindi suitable
for a video voice-over.

This is NOT a new translation.

Keep exactly the same meaning.

IMPORTANT RULES:

- Use common everyday Hindi.
- Avoid difficult Sanskrit words.
- Avoid literary Hindi.
- Avoid highly formal Hindi.
- Use words ordinary Indian viewers naturally understand.
- It should sound natural when spoken.
- Common English words are allowed when Indians normally use them.
- Keep names, places, brands, organizations and numbers unchanged.
- Do not add information.
- Do not remove information.
- Do not explain anything.
- Return ONLY the final Hindi sentence.

Hindi:
""" + text

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
            "temperature": 0.20,
            "maxOutputTokens": 300
        }
    }

    api_url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )

    data = json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    request = urllib.request.Request(
        api_url,
        data=data,
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

            raw = response.read()

        result = json.loads(
            raw.decode("utf-8")
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
            p.get("text", "")
            for p in parts
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
            contains_devanagari(output) and
            len(output) < 3000
        ):
            return output

    except urllib.error.HTTPError as e:

        try:
            detail = e.read().decode(
                "utf-8",
                errors="ignore"
            )

            print(
                f"⚠️ Gemini HTTP {e.code}: "
                f"{detail[:250]}"
            )

        except:
            print(
                f"⚠️ Gemini HTTP {e.code}"
            )

    except Exception as e:

        print(
            "⚠️ Gemini error:",
            e
        )

    # Never return blank.
    return text


# ============================================================
# WHISPER
# ============================================================

def transcribe_and_translate(
    audio_path,
    target_lang="hi"
):

    print("🧠 Loading Whisper...")

    model = WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8"
    )

    print("🎙️ Transcribing...")

    raw_segments, info = model.transcribe(
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

    for s in raw_segments:

        text = s.text.strip()

        if not text:
            continue

        start = float(s.start)
        end = float(s.end)

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
    # Build natural sentence blocks.
    #
    # IMPORTANT:
    # We preserve the original Whisper start/end positions.
    # These positions remain the authoritative timeline.
    # --------------------------------------------------------

    blocks = []

    current = []
    current_start = raw[0]["start"]
    current_end = raw[0]["end"]

    endings = (
        ".",
        "!",
        "?",
        "।",
        "…"
    )

    for i, segment in enumerate(raw):

        current.append(
            segment["text"]
        )

        current_end = segment["end"]

        terminal = (
            segment["text"]
            .rstrip()
            .endswith(endings)
        )

        acoustic_gap = False

        if i + 1 < len(raw):

            gap = (
                raw[i + 1]["start"]
                - segment["end"]
            )

            acoustic_gap = gap >= 0.70

        sentence_length = (
            current_end -
            current_start
        )

        too_long = sentence_length >= 8.0

        last = (
            i + 1 ==
            len(raw)
        )

        if (
            terminal or
            acoustic_gap or
            too_long or
            last
        ):

            text = " ".join(
                current
            ).strip()

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

            current = []

            if i + 1 < len(raw):

                current_start = (
                    raw[i + 1]["start"]
                )

    print(
        f"📝 {len(blocks)} dialogue blocks"
    )

    segments = []

    for i, block in enumerate(blocks):

        start = block["start"]

        # ----------------------------------------------------
        # ORIGINAL dialogue slot
        #
        # This is deliberately based on the NEXT dialogue
        # start, not merely the Whisper end.
        #
        # This prevents artificial gaps.
        # ----------------------------------------------------

        if i + 1 < len(blocks):

            next_start = (
                blocks[i + 1]["start"]
            )

            slot = max(
                0.30,
                next_start - start
            )

        else:

            slot = max(
                0.50,
                block["end"] - start
            )

        translated = translate_text(
            block["text"],
            target_lang
        )

        if not translated:
            translated = block["text"]

        if (
            target_lang == "hi" and
            NATURALIZE_HINDI
        ):

            print(
                f"🗣️ Natural Hindi "
                f"{i + 1}/{len(blocks)}"
            )

            natural = naturalize_hindi(
                translated
            )

            if natural:
                translated = natural

        segments.append(
            {
                "index": i,
                "source_start": start,
                "source_end":
                    block["end"],
                "slot": slot,
                "source_text":
                    block["text"],
                "translated_text":
                    translated,
                "target_lang":
                    target_lang
            }
        )

    if segments:

        print(
            "TRANSLATION_PREVIEW:",
            segments[0]["translated_text"][:250]
        )

    return segments


# ============================================================
# TTS
# ============================================================

async def generate_tts(
    text,
    voice,
    output
):

    communicate = edge_tts.Communicate(
        text,
        voice,
        rate=TTS_RATE
    )

    await communicate.save(
        output
    )


def generate_tts_with_retry(
    text,
    voice,
    output
):

    for attempt in range(
        TTS_RETRIES
    ):

        try:

            if os.path.exists(output):
                os.remove(output)

            asyncio.run(
                generate_tts(
                    text,
                    voice,
                    output
                )
            )

            if (
                os.path.exists(output) and
                os.path.getsize(output) > 1000
            ):

                duration = get_duration(
                    output
                )

                if duration > 0.05:
                    return True

        except Exception as e:

            print(
                f"⚠️ TTS attempt "
                f"{attempt + 1}/"
                f"{TTS_RETRIES} failed: {e}"
            )

        time.sleep(0.5)

    return False


# ============================================================
# SILENCE REMOVAL
# ============================================================

def strip_dead_silence(
    source,
    output,
    threshold=-42
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

    # --------------------------------------------------------
    # Remove only dead silence from beginning/end.
    # Do NOT remove internal silence.
    #
    # Internal silence is important for natural speech.
    # --------------------------------------------------------

    start = 0
    end = len(audio)

    step = 10

    found_start = False

    for p in range(
        0,
        len(audio),
        step
    ):

        chunk = audio[
            p:p + step
        ]

        if chunk.dBFS > threshold:

            start = max(
                0,
                p - 20
            )

            found_start = True
            break

    if not found_start:
        start = 0

    found_end = False

    for p in range(
        len(audio) - step,
        0,
        -step
    ):

        chunk = audio[
            p:p + step
        ]

        if chunk.dBFS > threshold:

            end = min(
                len(audio),
                p + step + 20
            )

            found_end = True
            break

    if not found_end:
        end = len(audio)

    if end <= start:
        start = 0
        end = len(audio)

    audio = audio[
        start:end
    ]

    audio = (
        audio
        .set_frame_rate(44100)
        .set_channels(2)
    )

    audio.export(
        output,
        format="wav"
    )


# ============================================================
# AUDIO SPEED
# ============================================================

def change_audio_speed(
    source,
    output,
    factor
):

    factor = max(
        0.5,
        min(
            factor,
            2.0
        )
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-i", source,
            "-filter:a",
            f"atempo={factor:.6f}",
            "-ar", "44100",
            "-ac", "2",
            output
        ],
        quiet=True
    )


# ============================================================
# PREPARE TTS CLIPS
# ============================================================

def prepare_tts_clips(
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

    for i, seg in enumerate(
        segments
    ):

        text = seg[
            "translated_text"
        ].strip()

        if not text:

            # FALLBACK:
            # Never leave a dialogue blank.
            text = seg[
                "source_text"
            ].strip()

            seg[
                "translated_text"
  
