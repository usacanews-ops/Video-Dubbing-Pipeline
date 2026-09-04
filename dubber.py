import os
import sys
import subprocess
import asyncio
import time
import re
import json
import shutil
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
MAX_ATEMPO = 1.12

TTS_RATE = "+8%"

VIDEO_CRF = "27"
VIDEO_PRESET = "veryfast"
AUDIO_BITRATE = "128k"

WHISPER_MODEL = "tiny"

SILENCE_THRESHOLD = -42
SAFETY_GAP = 0.04
MAX_SENTENCE = 8.0

FREEZE_ENABLED = True

NATURALIZE_HINDI = True
GEMINI_MODEL = "gemini-2.5-flash"

TEMP_DIR = "temp_audio"

OUTPUT_VIDEO = "final_output.mp4"
OUTPUT_SRT = "subtitles.srt"


# ============================================================
# VOICE MAP
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
# UTILITIES
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


def file_size_mb(path):
    if not os.path.exists(path):
        return 0

    return os.path.getsize(path) / 1048576


# ============================================================
# DOWNLOAD MEDIA
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
            except:
                pass

    print("📥 Downloading media...")

    ydl_opts = {
        "format":
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/best[ext=mp4]/best",
        "outtmpl": video_path,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True
    }

    with yt_dlp.YoutubeDL(
        ydl_opts
    ) as ydl:

        info = ydl.extract_info(
            url,
            download=True
        )

    meta = {
        "title":
            info.get(
                "title",
                "Dubbed Video"
            ),

        "description":
            info.get(
                "description",
                ""
            ),

        "tags":
            info.get(
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
            meta,
            f,
            ensure_ascii=False
        )

    print(
        f"TITLE_EMIT: {meta['title']}"
    )

    print(
        "🎵 Extracting audio for Whisper..."
    )

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
        meta
    )


# ============================================================
# TRANSLATION HELPERS
# ============================================================

def contains_devanagari(text):

    if not text:
        return False

    return bool(
        re.search(
            r"[\u0900-\u097F]",
            text
        )
    )


def bad_translation(text):

    if not text:
        return True

    lowered = text.lower()

    bad = (
        "<html",
        "<!doctype",
        "server error",
        "captcha",
        "unusual traffic"
    )

    return any(
        x in lowered
        for x in bad
    )


def translate_to_hindi(text):

    text = re.sub(
        r"[\r\n\t]+",
        " ",
        text
    ).strip()

    if len(text) < 2:
        return text

    # Google
    for attempt in range(3):

        try:

            result = GoogleTranslator(
                source="auto",
                target="hi"
            ).translate(text)

            if (
                result
                and not bad_translation(result)
                and contains_devanagari(result)
            ):

                return result.strip()

        except Exception:

            time.sleep(
                0.3
            )

    # MyMemory fallback
    try:

        result = MyMemoryTranslator(
            source="en-US",
            target="hi-IN"
        ).translate(text)

        if (
            result
            and not bad_translation(result)
            and contains_devanagari(result)
        ):

            return result.strip()

    except Exception:
        pass

    return ""


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

    for _ in range(3):

        try:

            result = GoogleTranslator(
                source="auto",
                target=target_lang
            ).translate(text)

            if (
                result
                and not bad_translation(result)
            ):

                return result.strip()

        except Exception:

            time.sleep(
                0.3
            )

    return text


# ============================================================
# NATURAL SPOKEN HINDI
# ============================================================

def naturalize_hindi(text):

    if not NATURALIZE_HINDI:
        return text

    if not text:
        return text

    api_key = os.environ.get(
        "GEMINI_API_KEY"
    )

    if not api_key:

        print(
            "⚠️ GEMINI_API_KEY not set."
            " Using normal Hindi translation."
        )

        return text

    if not contains_devanagari(text):
        return text

    prompt = """
You are a professional Indian Hindi dubbing writer.

Rewrite the following Hindi translation into natural, simple,
spoken Indian Hindi for a video voice-over.

IMPORTANT:
This is NOT a new translation.
Preserve the meaning of the existing Hindi.

Rules:

- Use everyday Hindi understood by ordinary Indian viewers.
- Avoid difficult Sanskrit words.
- Avoid overly formal, literary or bookish Hindi.
- Prefer simple conversational Hindi.
- Make it sound natural when spoken aloud.
- Keep the original meaning exactly.
- Do not add information.
- Do not remove important information.
- Keep names, places, organizations, brands and numbers unchanged.
- Common English words may be used where Indians naturally use them.
- Do not add explanations.
- Do not add headings.
- Do not add quotation marks.
- Return ONLY the final Hindi sentence.

Examples:

Formal:
घटना के पश्चात उसे गिरफ्तार किया गया।

Natural:
घटना के बाद उसे गिरफ्तार कर लिया गया।

Formal:
सरकार ने नवीन योजना की घोषणा की।

Natural:
सरकार ने एक नई योजना का ऐलान किया।

Formal:
इस परिस्थिति के परिप्रेक्ष्य में प्रशासन ने महत्वपूर्ण निर्णय लिया।

Natural:
इस स्थिति को देखते हुए प्रशासन ने एक अहम फैसला लिया।

Now rewrite this Hindi:

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
            "temperature": 0.25,
            "maxOutputTokens": 300
        }
    }

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )

    data = json.dumps(
        payload,
        ensure_ascii=False
    ).encode(
        "utf-8"
    )

    request = urllib.request.Request(
        url,
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
            part.get("text", "")
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
            output
            and contains_devanagari(output)
            and len(output) < 3000
        ):

            return output

    except urllib.error.HTTPError as e:

        try:
            detail = e.read().decode(
                "utf-8",
                errors="ignore"
            )

            print(
                "⚠️ Gemini HTTP error:",
                e.code,
                detail[:500]
            )

        except:
            print(
                "⚠️ Gemini HTTP error:",
                e.code
            )

    except Exception as e:

        print(
            "⚠️ Gemini naturalization error:",
            e
        )

    # VERY IMPORTANT:
    # Never allow naturalization failure
    # to stop the dubbing process.
    return text


# ============================================================
# WHISPER + TRANSLATION
# ============================================================

def transcribe_and_translate(
    audio_path,
    target_lang="hi"
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

    raw_segments, info = model.transcribe(
        audio_path,
        language=None,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 350
        }
    )

    print(
        f"🌍 Detected language: "
        f"{info.language}"
    )

    raw = []

    for s in raw_segments:

        text = s.text.strip()

        if not text:
            continue

        if (
            s.end - s.start
            < 0.2
        ):
            continue

        raw.append({
            "start":
                float(s.start),

            "end":
                float(s.end),

            "text":
                text
        })

    if not raw:
        return []

    # --------------------------------------------------------
    # Semantic sentence grouping
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

        current_end = (
            segment["end"]
        )

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

            acoustic_gap = (
                gap >= 0.8
            )

        too_long = (
            current_end
            - current_start
            >= MAX_SENTENCE
        )

        last = (
            i + 1 == len(raw)
        )

        if (
            terminal
            or acoustic_gap
            or too_long
            or last
        ):

            text = " ".join(
                current
            ).strip()

            if text:

                blocks.append({
                    "start":
                        current_start,

                    "end":
                        current_end,

                    "text":
                        text
                })

            current = []

            if i + 1 < len(raw):

                current_start = (
                    raw[i + 1]["start"]
                )

    print(
        f"📝 {len(blocks)} "
        "semantic sentences."
    )

    segments = []

    for i, block in enumerate(blocks):

        if i + 1 < len(blocks):

            available = max(
                0.5,
                blocks[i + 1]["start"]
                - block["start"]
                - SAFETY_GAP
            )

        else:

            available = max(
                0.5,
                block["end"]
                - block["start"]
                + 2.0
            )

        # ----------------------------------------------------
        # First translation
        # ----------------------------------------------------

        translated = translate_text(
            block["text"],
            target_lang
        )

        if not translated:

            translated = block["text"]

        # ----------------------------------------------------
        # Natural spoken Hindi
        # ----------------------------------------------------

        if (
            target_lang == "hi"
            and NATURALIZE_HINDI
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

        if i == 0:

            print(
                "TRANSLATION_PREVIEW: "
                + translated[:250]
            )

        segments.append({

            "index":
                i,

            "start":
                float(block["start"]),

            "end":
                float(block["end"]),

            "available_slot":
                float(available),

            "translated_text":
                translated,

            "target_lang":
                target_lang
        })

    return segments


# ============================================================
# REMOVE TTS SILENCE
# ============================================================

def strip_dead_silence(
    source,
    output,
    threshold=SILENCE_THRESHOLD
):

    audio = AudioSegment.from_file(
        source
    )

    if not audio:
        return

    start = 0
    end = len(audio)

    step = 10

    # Beginning
    for p in range(
        0,
        len(audio),
        step
    ):

        chunk = audio[
            p:p + step
        ]

        if (
            chunk.dBFS
            > threshold
        ):

            start = max(
                0,
                p - 15
            )

            break

    # End
    for p in range(
        len(audio) - step,
        0,
        -step
    ):

        chunk = audio[
            p:p + step
        ]

        if (
            chunk.dBFS
            > threshold
        ):

            end = min(
                len(audio),
                p + step + 15
            )

            break

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

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            source,
            "-filter:a",
            f"atempo={factor:.5f}",
            "-ar",
            "44100",
            "-ac",
            "2",
            output
        ],
        quiet=True
    )


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


async def synthesize_audio(
    segments,
    temp_dir=TEMP_DIR
):

    if os.path.exists(
        temp_dir
    ):

        shutil.rmtree(
            temp_dir
        )

    os.makedirs(
        temp_dir,
        exist_ok=True
    )

    for i, seg in enumerate(
        segments
    ):

        text = (
            seg["translated_text"]
            .strip()
        )

        if not text:

            seg["clip_file"] = None
            seg["tts_duration"] = 0

            continue

        lang = (
            seg.get(
                "target_lang",
                "hi"
            )
            .lower()
            .split("-")[0]
        )

        voice = VOICE_MAP.get(
            lang,
            "en-US-GuyNeural"
        )

        raw = os.path.join(
            temp_dir,
            f"raw_{i}.mp3"
        )

        clean = os.path.join(
            temp_dir,
            f"clean_{i}.wav"
        )

        fitted = os.path.join(
            temp_dir,
            f"fit_{i}.wav"
        )

        print(
            f"🔊 TTS "
            f"{i + 1}/{len(segments)}"
        )

        try:

            await generate_tts(
                text,
                voice,
                raw
            )

            strip_dead_silence(
                raw,
                clean
            )

            original_duration = (
                get_duration(clean)
            )

            available = (
                seg["available_slot"]
            )

            ratio = (
                original_duration
                / available
            )

            if ratio > 1.0:

                factor = min(
                    ratio,
                    MAX_ATEMPO
                )

                change_audio_speed(
                    clean,
                    fitted,
                    factor
                )

                final_file = fitted

                final_duration = (
                    original_duration
                    / factor
                )

                seg[
                    "audio_speed"
                ] = factor

            else:

                final_file = clean

                final_duration = (
  
