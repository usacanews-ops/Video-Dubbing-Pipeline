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
# CONFIG
# ============================================================

FINAL_SPEED = 1.10
MAX_TTS_SPEED = 1.12
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
# COMMAND
# ============================================================

def run(cmd,quiet=False):
    print("▶ "+" ".join(f'"{x}"' if " " in str(x) else str(x) for x in cmd))

    if quiet:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
    else:
        subprocess.run(cmd,check=True)


# ============================================================
# FILE HELPERS
# ============================================================

def get_duration(path):
    result=subprocess.check_output([
        "ffprobe",
        "-v","error",
        "-show_entries","format=duration",
        "-of","default=noprint_wrappers=1:nokey=1",
        path
    ],stderr=subprocess.STDOUT)

    return float(result.strip())


def file_size_mb(path):
    if not os.path.exists(path):
        return 0

    return os.path.getsize(path)/1048576


# ============================================================
# DOWNLOAD
# ============================================================

def download_media(url):

    video_path="raw_source.mp4"
    audio_path="input_audio.wav"

    for path in [video_path,audio_path]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass

    print("📥 Downloading media...")

    # More compatible with Facebook share URLs.
    ydl_opts={
        "format":"best[ext=mp4]/best",
        "outtmpl":video_path,
        "merge_output_format":"mp4",
        "quiet":False,
        "no_warnings":False,
        "noplaylist":True
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info=ydl.extract_info(url,download=True)

    metadata={
        "title":info.get("title","Dubbed Video"),
        "description":info.get("description",""),
        "tags":info.get("tags",[])
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
        "TITLE_EMIT: "
        + metadata["title"]
    )

    print("🎵 Extracting audio...")

    run([
        "ffmpeg",
        "-y",
        "-i",video_path,
        "-vn",
        "-ac","1",
        "-ar","16000",
        "-c:a","pcm_s16le",
        audio_path
    ],quiet=True)

    return video_path,audio_path,metadata


# ============================================================
# TRANSLATION
# ============================================================

def contains_devanagari(text):
    return bool(
        text and re.search(
            r"[\u0900-\u097F]",
            text
        )
    )


def bad_translation(text):

    if not text:
        return True

    text=text.lower()

    bad=[
        "<html",
        "<!doctype",
        "server error",
        "captcha",
        "unusual traffic"
    ]

    return any(x in text for x in bad)


def translate_to_hindi(text):

    text=re.sub(
        r"[\r\n\t]+",
        " ",
        text
    ).strip()

    if len(text)<2:
        return text

    for _ in range(3):
        try:
            result=GoogleTranslator(
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
            time.sleep(.3)

    try:
        result=MyMemoryTranslator(
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


def translate_text(text,target_lang):

    if target_lang=="hi":
        return translate_to_hindi(text)

    text=re.sub(
        r"[\r\n\t]+",
        " ",
        text
    ).strip()

    if len(text)<2:
        return text

    for _ in range(3):
        try:
            result=GoogleTranslator(
                source="auto",
                target=target_lang
            ).translate(text)

            if result and not bad_translation(result):
                return result.strip()

        except Exception:
            time.sleep(.3)

    return text


# ============================================================
# NATURAL SPOKEN HINDI
# ============================================================

def naturalize_hindi(text):

    if not NATURALIZE_HINDI:
        return text

    if not text or not contains_devanagari(text):
        return text

    api_key=os.environ.get("GEMINI_API_KEY")

    if not api_key:
        print(
            "⚠️ GEMINI_API_KEY unavailable. "
            "Using normal Hindi."
        )
        return text

    prompt="""
You are an expert Indian Hindi dubbing writer.

Rewrite the Hindi below into very natural, simple,
spoken Indian Hindi suitable for a video voice-over.

This is NOT a new translation.

Keep exactly the same meaning.

Rules:
- Use everyday Hindi.
- Avoid difficult Sanskrit words.
- Avoid literary or bookish Hindi.
- Avoid unnecessarily formal Hindi.
- Use words ordinary Indian viewers naturally understand.
- Make it sound natural when spoken aloud.
- Keep names, places, brands, organizations and numbers.
- Common English words are allowed where Indians naturally use them.
- Do not add information.
- Do not remove information.
- Do not explain anything.
- Return ONLY the rewritten Hindi.

Hindi text:
""" + text

    payload={
        "contents":[{
            "parts":[{
                "text":prompt
            }]
        }],
        "generationConfig":{
            "temperature":0.25,
            "maxOutputTokens":300
        }
    }

    url=(
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )

    data=json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    request=urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type":"application/json",
            "x-goog-api-key":api_key
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:
            raw=response.read()

        result=json.loads(
            raw.decode("utf-8")
        )

        candidates=result.get(
            "candidates",[]
        )

        if not candidates:
            return text

        parts=(
            candidates[0]
            .get("content",{})
            .get("parts",[])
        )

        output="".join(
            p.get("text","")
            for p in parts
        ).strip()

        output=re.sub(
            r"^```(?:text|hindi)?\s*",
            "",
            output,
            flags=re.I
        )

        output=re.sub(
            r"\s*```$",
            "",
            output
        ).strip()

        if (
            output
            and contains_devanagari(output)
            and len(output)<3000
        ):
            return output

    except urllib.error.HTTPError as e:

        try:
            detail=e.read().decode(
                "utf-8",
                errors="ignore"
            )

            print(
                f"⚠️ Gemini HTTP {e.code}: "
                f"{detail[:300]}"
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

    return text


# ============================================================
# WHISPER + SENTENCES
# ============================================================

def transcribe_and_translate(
    audio_path,
    target_lang="hi"
):

    print("🧠 Loading Whisper...")

    model=WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8"
    )

    print("🎙️ Transcribing...")

    raw_segments,info=model.transcribe(
        audio_path,
        language=None,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms":350
        }
    )

    print(
        "🌍 Detected language:",
        info.language
    )

    raw=[]

    for s in raw_segments:

        text=s.text.strip()

        if not text:
            continue

        if s.end-s.start<.2:
            continue

        raw.append({
            "start":float(s.start),
            "end":float(s.end),
            "text":text
        })

    if not raw:
        return []

    blocks=[]
    current=[]
    current_start=raw[0]["start"]
    current_end=raw[0]["end"]

    endings=(
        ".","!","?",
        "।","…"
    )

    for i,segment in enumerate(raw):

        current.append(
            segment["text"]
        )

        current_end=segment["end"]

        terminal=segment["text"].rstrip().endswith(endings)

        acoustic_gap=False

        if i+1<len(raw):

            gap=(
                raw[i+1]["start"]
                -segment["end"]
            )

            acoustic_gap=gap>=.8

        too_long=(
            current_end-current_start
            >=MAX_SENTENCE
        )

        last=i+1==len(raw)

        if terminal or acoustic_gap or too_long or last:

            text=" ".join(
                current
            ).strip()

            if text:
                blocks.append({
                    "start":current_start,
                    "end":current_end,
                    "text":text
                })

            current=[]

            if i+1<len(raw):
                current_start=raw[i+1]["start"]

    print(
        f"📝 {len(blocks)} sentences"
    )

    segments=[]

    for i,block in enumerate(blocks):

        if i+1<len(blocks):
            available=max(
                .5,
                blocks[i+1]["start"]
                -block["start"]
                -SAFETY_GAP
            )
        else:
            available=max(
                .5,
                block["end"]
                -block["start"]
                +2
            )

        translated=translate_text(
            block["text"],
            target_lang
        )

        if not translated:
            translated=block["text"]

        if (
            target_lang=="hi"
            and NATURALIZE_HINDI
        ):
            print(
                f"🗣️ Natural Hindi "
                f"{i+1}/{len(blocks)}"
            )

            translated=naturalize_hindi(
                translated
            )

        if i==0:
            print(
                "TRANSLATION_PREVIEW:",
                translated[:250]
            )

        segments.append({
            "index":i,
            "start":float(block["start"]),
            "end":float(block["end"]),
            "available_slot":float(available),
            "translated_text":translated,
            "target_lang":target_lang
        })

    return segments


# ============================================================
# REMOVE SILENCE
# ============================================================

def strip_dead_silence(
    source,
    output,
    threshold=SILENCE_THRESHOLD
):

    audio=AudioSegment.from_file(
        source
    )

    start=0
    end=len(audio)
    step=10

    for p in range(
        0,
        len(audio),
        step
    ):
        chunk=audio[p:p+step]

        if chunk.dBFS>threshold:
            start=max(0,p-15)
            break

    for p in range(
        len(audio)-step,
        0,
        -step
    ):
        chunk=audio[p:p+step]

        if chunk.dBFS>threshold:
            end=min(
                len(audio),
                p+step+15
            )
            break

    audio=audio[start:end]

    audio=(
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

    run([
        "ffmpeg",
        "-y",
        "-i",source,
        "-filter:a",
        f"atempo={factor:.5f}",
        "-ar","44100",
        "-ac","2",
        output
    ],quiet=True)


# ============================================================
# TTS
# ============================================================

async def generate_tts(
    text,
    voice,
    output
):

    communicate=edge_tts.Communicate(
        text,
        voice,
        rate=TTS_RATE
    )

    await communicate.save(
        output
    )


async def synthesize_audio(
    segments
):

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)

    os.makedirs(
        TEMP_DIR,
        exist_ok=True
    )

    for i,seg in enumerate(segments):

        text=seg[
            "translated_text"
        ].strip()

        if not text:
            seg["clip_file"]=None
            seg["tts_duration"]=0
            continue

        lang=(
            seg.get(
                "target_lang",
                "hi"
            )
            .lower()
            .split("-")[0]
        )

        voice=VOICE_MAP.get(
            lang,
            "en-US-GuyNeural"
        )

        raw=os.path.join(
            TEMP_DIR,
            f"raw_{i}.mp3"
        )

        clean=os.path.join(
            TEMP_DIR,
            f"clean_{i}.wav"
        )

        fitted=os.path.join(
            TEMP_DIR,
            f"fit_{i}.wav"
        )

        print(
            f"🔊 TTS "
            f"{i+1}/{len(segments)}"
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

            original_duration=get_duration(
                clean
            )

            available=seg[
                "available_slot"
            ]

            ratio=(
                original_duration
                /available
            )

            if ratio>1:

                factor=min(
                    ratio,
                    MAX_TTS_SPEED
                )

                change_audio_speed(
                    clean,
                    fitted,
                    factor
                )

                final_file=fitted

                final_duration=(
                    original_duration
                    /factor
                )

            else:

                final_file=clean

                final_duration=(
                    original_duration
                )

                factor=1

            seg["clip_file"]=final_file
            seg["tts_duration"]=final_duration
            seg["audio_speed"]=factor

            print(
                f"   {original_duration:.2f}s"
                f" → {final_duration:.2f}s"
                f" @ {factor:.3f}x"
            )

        except Exception as e:

            print(
                f"⚠️ TTS failed "
                f"{i+1}: {e}"
            )

            seg["clip_file"]=None
            seg["tts_duration"]=0


# ============================================================
# TIMELINE
# ============================================================

def build_timeline(segments):

    cursor=0

    for seg in segments:

        original_start=seg["start"]
        available=seg["available_slot"]
        speech=seg.get(
            "tts_duration",
            0
        )

        final_start=max(
            original_start,
            cursor
        )

        speech_end=(
            final_start
            +speech
        )

        normal_end=(
            final_start
            +available
        )

        freeze_duration=max(
            0,
            speech_end-normal_end
        )

        if not FREEZE_ENABLED:
            freeze_duration=0

        final_end=max(
            normal_end+freeze_duration,
            speech_end
        )

        seg["final_start"]=final_start
        seg["speech_end"]=speech_end
        seg["freeze_duration"]=freeze_duration
        seg["final_end"]=final_end

        cursor=final_end

    return segments


# ============================================================
# MASTER AUDIO
# ============================================================

def create_master_audio(
    segments,
    total_duration,
    output
):

    print("🎧 Building master audio...")

    master=AudioSegment.silent(
        duration=int(
            (total_duration+.5)*1000
        ),
        frame_rate=44100
    ).set_channels(2)

    for seg in segments:

        clip_file=seg.get(
            "clip_file"
        )

        if (
            not clip_file
            or not os.path.exists(
                clip_file
            )
        ):
            continue

        clip=(
            AudioSegment
            .from_file(clip_file)
            .set_frame_rate(44100)
            .set_channels(2)
        )

        position=int(
            seg["final_start"]*1000
        )

        master=master.overlay(
            clip,
            position=position
        )

    master.export(
        output,
        format="wav"
    )


# ============================================================
# VIDEO FILTER
# ============================================================

def build_video_filter(
    source_duration,
    segments
):

    filters=[]
    labels=[]
    label_id=0

    # --------------------------------------------------------
    # Before first dialogue
    # --------------------------------------------------------

    first_start=segments[0]["start"]

    if first_start>.01:

        label=f"v{label_id}"
        label_id+=1

        filters.append(
            "[0:v]"
            f"trim=start=0:end={first_start:.6f},"
            "setpts=PTS-STARTPTS"
            f"[{label}]"
        )

        labels.append(
            f"[{label}]"
        )

    # --------------------------------------------------------
    # Dialogue sections
    # --------------------------------------------------------

    for i,seg in enumerate(segments):

        start=seg["start"]

        if i+1<len(segments):
            end=segments[i+1]["start"]
        else:
            end=source_duration

        end=min(
            end,
            source_duration
        )

        if end<=start:
            continue

        label=f"v{label_id}"
        label_id+=1

        part=(
            "[0:v]"
            f"trim=start={start:.6f}:end={end:.6f},"
            "setpts=PTS-STARTPTS"
        )

        freeze=float(
            seg.get(
                "freeze_duration",
                0
            )
        )

        if (
            FREEZE_ENABLED
            and freeze>.01
        ):

            part+=(
                ",tpad="
                "stop_mode=clone:"
                f"stop_duration={freeze:.6f}"
            )

        part+=f"[{label}]"

        filters.append(part)
        labels.append(f"[{label}]")

    if not labels:
        raise RuntimeError(
            "No video sections generated."
        )

    filters.append(
        "".join(labels)
        +f"concat=n={len(labels)}:v=1:a=0,"
        "fps=30,"
        "format=yuv420p"
        "[joined]"
    )

    # Final 10% speed increase
    filters.append(
        "[joined]"
        f"setpts=PTS/{FINAL_SPEED:.6f}"
        "[vout]"
    )

    return ";".join(filters)


# ============================================================
# RENDER VIDEO
# ============================================================

def render_video(
    source,
    segments,
    output
):

    source_duration=get_duration(
        source
    )

    print("🎬 Rendering video...")

    filter_complex=build_video_filter(
        source_duration,
        segments
    )

    command=[
        "ffmpeg",
        "-y",
        "-i",source,
        "-filter_complex",
        filter_complex,
        "-map","[vout]",
        "-an",
        "-c:v","libx264",
        "-preset",VIDEO_PRESET,
        "-crf",VIDEO_CRF,
        "-pix_fmt","yuv420p",
        "-movflags","+faststart",
        output
    ]

    run(command,quiet=False)

    if not os.path.exists(output):
        raise RuntimeError(
            "Video MP4 was not created."
        )

    print(
        f"✅ Intermediate video: "
        f"{file_size_mb(output):.2f} MB"
    )


# ============================================================
# MUX AUDIO
# ============================================================

def mux_audio(
    video,
    audio,
    output
):

    print("🎧 Adding dubbed audio...")

    temp=output+".tmp.mp4"

    if os.path.exists(temp):
        os.remove(temp)

    command=[
        "ffmpeg",
        "-y",
        "-i",video,
        "-i",audio,
        "-map","0:v:0",
        "-map","1:a:0",
        "-af",
        f"atempo={FINAL_SPEED:.6f}",
        "-c:v","copy",
        "-c:a","aac",
        "-b:a",AUDIO_BITRATE,
        "-movflags","+faststart",
        "-map_metadata","-1",
        "-map_chapters","-1",
        temp
    ]

    run(command,quiet=False)

    if not os.path.exists(temp):
        raise RuntimeError(
            "Final MP4 was not created."
        )

    if os.path.exists(output):
        os.remove(output)

    os.replace(
        temp,
        output
    )

    print(
        f"✅ Final MP4: "
        f"{file_size_mb(output):.2f} MB"
    )


# ============================================================
# SRT
# ============================================================

def format_timestamp(seconds):

    milliseconds=int(
        round(
            max(0,seconds)*1000
        )
    )

    hours=milliseconds//3600000
    milliseconds%=3600000

    minutes=milliseconds//60000
    milliseconds%=60000

    secs=milliseconds//1000
    milliseconds%=1000

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d},"
        f"{milliseconds:03d}"
    )


def generate_srt(
    segments,
    output=OUTPUT_SRT
):

    print("📝 Creating SRT...")

    count=0

    with open(
        output,
        "w",
        encoding="utf-8"
    ) as f:

        for seg in segments:

            text=seg.get(
                "translated_text",
                ""
            ).strip()

            if not text:
                continue

            start=(
                seg["final_start"]
                /FINAL_SPEED
            )

            end=(
                seg["speech_end"]
                /FINAL_SPEED
            )

            if end<=start:
                end=start+.5

            count+=1

            f.write(
                f"{count}\n"
            )

            f.write(
                format_timestamp(start)
                +" --> "
                +format_timestamp(end)
                +"\n"
            )

            f.write(
                text
                +"\n\n"
            )

    print(
        f"✅ SRT created: "
        f"{output}"
    )

    print(
        f"   Subtitles: {count}"
    )


# ============================================================
# CLEANUP
# ============================================================

def cleanup():

    print("🧹 Cleaning temporary files...")

    for directory in [
        TEMP_DIR
    ]:

        if os.path.exists(directory):

            try:
                shutil.rmtree(directory)
            except:
                pass

    for file in [
        "raw_source.mp4",
        "input_audio.wav",
        "synced_master.wav",
        "extended_video.mp4"
    ]:

        if os.path.exists(file):

            try:
                os.remove(file)
            except:
                pass


# ============================================================
# MAIN
# ============================================================

def main():

    if len(sys.argv)<2:

        print(
            'Usage: python dubber.py '
            '"VIDEO_URL" [language]'
        )

        sys.exit(1)

    url=sys.argv[1]

    target_lang=(
        sys.argv[2]
        if len(sys.argv)>2
        else "hi"
    ).lower()

    print()
    print("="*65)
    print("             AI VIDEO DUBBER")
    print("="*65)

    print(
        "Target language:",
        target_lang
    )

    print(
        "Natural Hindi:",
        NATURALIZE_HINDI
    )

    print(
        "Video speed:",
        FINAL_SPEED
    )

    print(
        "Freeze frames:",
        FREEZE_ENABLED
    )

    print("="*65)
    print()

    started=time.time()

    try:

        # ----------------------------------------------------
        # 1. DOWNLOAD
        # ----------------------------------------------------

        (
            video,
            source_audio,
            metadata
        )=download_media(url)

        source_duration=get_duration(
            video
        )

        print(
            f"Source: "
            f"{file_size_mb(video):.2f} MB"
        )

        print(
            f"Duration: "
            f"{source_duration:.2f}s"
        )

        # ----------------------------------------------------
        # 2. TRANSCRIPTION
        # ----------------------------------------------------

        segments=transcribe_and_translate(
            source_audio,
            target_lang
        )

        if not segments:
            raise RuntimeError(
                "No speech detected."
            )

        # ----------------------------------------------------
        # 3. TTS
        # ----------------------------------------------------

        asyncio.run(
            synthesize_audio(
                segments
            )
        )

        # ----------------------------------------------------
        # 4. TIMELINE
        # ----------------------------------------------------

        build_timeline(
            segments
        )

        timeline_duration=max(
            source_duration,
            max(
                (
                    s["final_end"]
                    for s in segments
                ),
                default=0
            )
        )

        print(
            f"Timeline: "
            f"{timeline_duration:.2f}s"
        )

        # ----------------------------------------------------
        # 5. MASTER AUDIO
        # ----------------------------------------------------

        master_audio="synced_master.wav"

        create_master_audio(
            segments,
            timeline_duration,
            master_audio
        )

        # ----------------------------------------------------
        # 6. SRT
        #
        # Create this BEFORE rendering.
        # ----------------------------------------------------

        generate_srt(
            segments,
            OUTPUT_SRT
        )

        # ----------------------------------------------------
        # 7. VIDEO
        # ----------------------------------------------------

        intermediate_video=(
            "extended_video.mp4"
        )

        render_video(
            video,
            segments,
            intermediate_video
        )

        # ----------------------------------------------------
        # 8. FINAL MUX
        # ----------------------------------------------------

        mux_audio(
            intermediate_video,
            master_audio,
            OUTPUT_VIDEO
        )

        # ----------------------------------------------------
        # 9. VERIFY
        # ----------------------------------------------------

        if not os.path.exists(
            OUTPUT_VIDEO
        ):
            raise RuntimeError(
                "final_output.mp4 missing."
            )

        if not os.path.exists(
            OUTPUT_SRT
        ):
            raise RuntimeError(
                "subtitles.srt missing."
            )

        final_duration=get_duration(
            OUTPUT_VIDEO
        )

        final_size=file_size_mb(
            OUTPUT_VIDEO
        )

        elapsed=time.time()-started

        print()
        print("="*65)
        print("                ✅ COMPLETE")
        print("="*65)

        print(
            f"Source size     : "
            f"{file_size_mb(video):.2f} MB"
        )

        print(
            f"Final size      : "
            f"{final_size:.2f} MB"
        )

        print(
            f"Source duration : "
            f"{source_duration:.2f}s"
        )

        print(
            f"Final duration  : "
            f"{final_duration:.2f}s"
        )

        print(
            f"Video speed     : "
            f"{FINAL_SPEED}x"
        )

        print(
            f"Processing time : "
            f"{elapsed/60:.2f} minutes"
        )

        print()
        print("🎬 final_output.mp4")
        print("📝 subtitles.srt")
        print("="*65)

        cleanup()

    except Exception as e:

        print()
        print("="*65)
        print("                ❌ FAILED")
        print("="*65)

        print(
            str(e)
        )

        print()
        print(
            "Temporary files were kept "
            "for troubleshooting."
        )

        print("="*65)

        raise


if __name__=="__main__":
    main()
