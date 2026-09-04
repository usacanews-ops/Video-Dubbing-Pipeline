import os,sys,subprocess,asyncio,time,re,json,shutil
import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator,MyMemoryTranslator
import edge_tts
from pydub import AudioSegment

# ================= CONFIG =================

TTS_RATE="+8%"
FINAL_SPEED=1.10
MAX_ATEMPO=1.12

VIDEO_CRF="27"
VIDEO_PRESET="veryfast"
AUDIO_BITRATE="128k"

SILENCE_THRESHOLD=-42
SAFETY_GAP=0.04
MAX_SENTENCE=8.0
FREEZE_ENABLED=True

WHISPER_MODEL="tiny"

# ---------- Hindi naturalization ----------

NATURALIZE_HINDI=True
GEMINI_MODEL="gemini-3.7-flash"

# Keep this reasonably low.
# Naturalization is done once per sentence.
GEMINI_MAX_OUTPUT=300

# ================= VOICES =================

VOICE_MAP={
    "hi":"hi-IN-MadhurNeural",
    "bn":"bn-IN-BashkarNeural",
    "ta":"ta-IN-ValluvarNeural",
    "te":"te-IN-MohanNeural",
    "mr":"mr-IN-ManoharNeural",
    "gu":"gu-IN-DhwaniNeural",
    "kn":"kn-IN-GaganNeural",
    "ml":"ml-IN-MidhunNeural",
    "pa":"pa-IN-OjasNeural",
    "en":"en-US-GuyNeural",
    "fr":"fr-FR-HenriNeural",
    "de":"de-DE-ConradNeural",
    "es":"es-ES-AlvaroNeural",
    "it":"it-IT-DiegoNeural",
    "pt":"pt-BR-AntonioNeural",
    "nl":"nl-NL-MaartenNeural",
    "pl":"pl-PL-MarekNeural",
    "tr":"tr-TR-AhmetNeural",
    "ru":"ru-RU-DmitryNeural",
    "uk":"uk-UA-OstapNeural",
    "ja":"ja-JP-KeitaNeural",
    "ko":"ko-KR-InJoonNeural",
    "zh":"zh-CN-YunxiNeural",
    "ar":"ar-SA-HamedNeural"
}

# ================= FFMPEG =================

def run(cmd,quiet=False):
    if quiet:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
    else:
        subprocess.run(cmd,check=True)

def duration(path):
    return float(
        subprocess.check_output([
            "ffprobe",
            "-v","error",
            "-show_entries","format=duration",
            "-of","default=noprint_wrappers=1:nokey=1",
            path
        ]).strip()
    )

# ================= DOWNLOAD =================

def download_media(url):

    video="raw_source.mp4"
    audio="input_audio.wav"

    for p in (video,audio):
        if os.path.exists(p):
            try:
                os.remove(p)
            except:
                pass

    print("📥 Downloading media...")

    opts={
        "format":
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl":video,
        "merge_output_format":"mp4",
        "quiet":True,
        "no_warnings":True
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info=ydl.extract_info(
            url,
            download=True
        )

    meta={
        "title":info.get(
            "title",
            "Dubbed Video"
        ),
        "description":info.get(
            "description",
            ""
        ),
        "tags":info.get(
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
        "🎵 Extracting audio..."
    )

    run([
        "ffmpeg","-y",
        "-i",video,
        "-vn",
        "-ac","1",
        "-ar","16000",
        "-c:a","pcm_s16le",
        audio
    ],True)

    return video,audio,meta

# ================= TRANSLATION =================

def contains_devanagari(text):
    return bool(
        re.search(
            r"[\u0900-\u097F]",
            text
        )
    )

def bad_translation(text):

    if not text:
        return True

    t=text.lower()

    return any(
        x in t
        for x in (
            "<html",
            "<!doctype",
            "server error",
            "captcha",
            "unusual traffic"
        )
    )

def translate_hindi(text):

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

        except:
            time.sleep(.25)

    try:

        result=MyMemoryTranslator(
            source="auto",
            target="hi-IN"
        ).translate(text)

        if (
            result
            and not bad_translation(result)
            and contains_devanagari(result)
        ):
            return result.strip()

    except:
        pass

    return ""

def translate(text,target):

    if target=="hi":
        return translate_hindi(text)

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
                target=target
            ).translate(text)

            if (
                result
                and not bad_translation(result)
            ):
                return result.strip()

        except:
            time.sleep(.25)

    return text

# ================= GEMINI NATURAL HINDI =================

def naturalize_hindi(text):

    if not NATURALIZE_HINDI:
        return text

    api_key=os.environ.get(
        "GEMINI_API_KEY"
    )

    if not api_key:
        print(
            "⚠️ GEMINI_API_KEY not found. "
            "Using translated Hindi."
        )
        return text

    if not text or not contains_devanagari(text):
        return text

    prompt="""You are a professional Indian Hindi dubbing writer.

Rewrite the following Hindi translation into NATURAL, SIMPLE, SPOKEN INDIAN HINDI suitable for a video voice-over.

This is NOT a request for a literal translation.

Rules:
1. Keep the exact meaning.
2. Do not add facts or information.
3. Do not remove important information.
4. Use everyday Hindi that ordinary Indian viewers easily understand.
5. Avoid highly Sanskritized, literary or bookish Hindi.
6. Prefer common spoken words.
7. Use natural Hindi sentence structure.
8. Shorter sentences are preferred when the meaning remains unchanged.
9. You may naturally use commonly spoken English words when Indians normally use them.
10. Keep names, places, organizations, brands, numbers and dates accurate.
11. Do not translate proper names unnecessarily.
12. Do not use quotation marks unless they are genuinely part of the sentence.
13. Do not add explanations.
14. Return ONLY the rewritten Hindi text.
15. This text will be spoken by a TTS voice, so it must sound natural when spoken aloud.

Examples:

Formal:
"घटना के पश्चात उसे गिरफ्तार किया गया।"

Natural:
"घटना के बाद उसे गिरफ्तार कर लिया गया।"

Formal:
"सरकार ने नवीन योजना की घोषणा की।"

Natural:
"सरकार ने एक नई योजना का ऐलान किया।"

Formal:
"इस परिस्थिति के परिप्रेक्ष्य में प्रशासन ने महत्वपूर्ण निर्णय लिया।"

Natural:
"इस स्थिति को देखते हुए प्रशासन ने एक अहम फैसला लिया।"

Hindi text to rewrite:
""" + text

    payload={
        "contents":[
            {
                "parts":[
                    {
                        "text":prompt
                    }
                ]
            }
        ],
        "generationConfig":{
            "maxOutputTokens":
                GEMINI_MAX_OUTPUT
        }
    }

    try:

        import urllib.request
        import urllib.error

        url=(
            "https://generativelanguage.googleapis.com/"
            "v1beta/models/"
            +GEMINI_MODEL+
            ":generateContent"
        )

        data=json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8")

        request=urllib.request.Request(
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

        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            result=json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

        candidates=result.get(
            "candidates",
            []
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
            r"^```(?:hindi|text)?\s*",
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
            and len(output)<max(
                3000,
                len(text)*4
            )
        ):
            return output

    except Exception as e:

        print(
            f"⚠️ Hindi naturalization failed: {e}"
        )

    return text

# ================= WHISPER =================

def transcribe_and_translate(
    audio_path,
    target_lang="hi"
):

    print(
        "🧠 Loading Whisper..."
    )

    model=WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8"
    )

    print(
        "🎙️ Transcribing..."
    )

    raw_segments,info=model.transcribe(
        audio_path,
        language=None,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms":350
        }
    )

    print(
        f"🌍 Detected language: "
        f"{info.language}"
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

    # ---------- semantic grouping ----------

    blocks=[]
    current=[]
    current_start=raw[0]["start"]
    current_end=raw[0]["end"]

    endings=(
        ".",
        "!",
        "?",
        "।",
        "…"
    )

    for i,s in enumerate(raw):

        current.append(
            s["text"]
        )

        current_end=s["end"]

        terminal=s[
            "text"
        ].rstrip().endswith(
            endings
        )

        acoustic_gap=False

        if i+1<len(raw):

            gap=(
                raw[i+1]["start"]
                -s["end"]
            )

            acoustic_gap=gap>=.8

        too_long=(
            current_end-current_start
            >=MAX_SENTENCE
        )

        last=i+1==len(raw)

        if (
            terminal
            or acoustic_gap
            or too_long
            or last
        ):

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
                current_start=(
                    raw[i+1]["start"]
                )

    print(
        f"📝 {len(blocks)} "
        "semantic sentences."
    )

    segments=[]

    for i,b in enumerate(blocks):

        if i+1<len(blocks):

            next_start=(
                blocks[i+1]["start"]
            )

            available=max(
                .5,
                next_start
                -b["start"]
                -SAFETY_GAP
            )

        else:

            available=max(
                .5,
                b["end"]
                -b["start"]
                +2
            )

        translated=translate(
            b["text"],
            target_lang
        )

        if not translated:
            translated=b["text"]

        # ---------- NATURAL HINDI ----------

        if (
            target_lang=="hi"
            and NATURALIZE_HINDI
        ):

            print(
                f"🗣️ Naturalizing "
                f"Hindi {i+1}/{len(blocks)}"
            )

            natural=naturalize_hindi(
                translated
            )

            if natural:
                translated=natural

        if i==0:

            print(
                "TRANSLATION_PREVIEW: "
                +translated[:180]
            )

        segments.append({

            "index":i,

            "start":
                b["start"],

            "end":
                b["end"],

            "available_slot":
                available,

            "translated_text":
                translated,

            "target_lang":
                target_lang
        })

    return segments

# ================= SILENCE =================

def strip_dead_silence(
    source,
    output,
    threshold=SILENCE_THRESHOLD
):

    audio=AudioSegment.from_file(
        source
    )

    if not audio:
        return

    start=0
    end=len(audio)
    step=10

    for p in range(
        0,
        len(audio),
        step
    ):

        chunk=audio[
            p:p+step
        ]

        if chunk.dBFS>threshold:

            start=max(
                0,
                p-15
            )

            break

    for p in range(
        len(audio)-step,
        0,
        -step
    ):

        chunk=audio[
            p:p+step
        ]

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

# ================= TTS =================

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

def change_speed(
    source,
    output,
    factor
):

    run([
        "ffmpeg","-y",
        "-i",source,
        "-filter:a",
        f"atempo={factor:.5f}",
        "-ar","44100",
        "-ac","2",
        output
    ],True)

async def synthesize_audio(
    segments,
    temp_dir="temp_audio"
):

    if os.path.exists(temp_dir):
        shutil.rmtree(
            temp_dir
        )

    os.makedirs(
        temp_dir
    )

    for i,seg in enumerate(
        segments
    ):

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
            temp_dir,
            f"raw_{i}.mp3"
        )

        clean=os.path.join(
            temp_dir,
            f"clean_{i}.wav"
        )

        fitted=os.path.join(
            temp_dir,
            f"fit_{i}.wav"
        )

        print(
            f"🔊 TTS {i+1}/"
            f"{len(segments)}"
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

            original_duration=duration(
                clean
            )

            available=seg[
                "available_slot"
            ]

            ratio=(
                original_duration
                /available
            )

            # Controlled compression.
            if ratio>1.0:

                factor=min(
                    ratio,
                    MAX_ATEMPO
                )

                change_speed(
                    clean,
                    fitted,
                    factor
                )

                final_file=fitted

                final_duration=(
                    original_duration
                    /factor
                )

                seg[
                    "audio_speed"
                ]=factor

            else:

                final_file=clean

                final_duration=(
                    original_duration
                )

                seg[
                    "audio_speed"
                ]=1.0

            seg[
                "clip_file"
            ]=final_file

            seg[
                "tts_duration"
            ]=final_duration

            print(
                f"   TTS="
                f"{original_duration:.2f}s "
                f"final="
                f"{final_duration:.2f}s "
                f"speed="
                f"{seg['audio_speed']:.3f}x"
            )

        except Exception as e:

            print(
                f"⚠️ TTS error {i}: {e}"
            )

            seg[
                "clip_file"
            ]=None

            seg[
                "tts_duration"
            ]=0

# ================= TIMELINE =================

def build_timeline(segments):

    cursor=0.0

    for i,seg in enumerate(
        segments
    ):

        original_start=(
            seg["start"]
        )

        available=(
            seg["available_slot"]
        )

        speech=(
            seg["tts_duration"]
        )

        start=max(
            original_start,
            cursor
        )

        speech_end=(
            start+speech
        )

        normal_end=(
            start+available
        )

        freeze=max(
            0.0,
            speech_end-normal_end
        )

        if not FREEZE_ENABLED:
            freeze=0.0

        visual_end=max(
            normal_end+freeze,
            speech_end
        )

        seg[
            "final_start"
        ]=start

        seg[
            "speech_end"
        ]=speech_end

        seg[
            "freeze_duration"
        ]=freeze

        seg[
            "visual_end"
        ]=visual_end

        cursor=visual_end

        print(
            f"⏱ {i+1}: "
            f"start={start:.2f} "
            f"speech={speech:.2f} "
            f"slot={available:.2f} "
            f"freeze={freeze:.2f} "
            f"next={cursor:.2f}"
        )

    return segments

# ================= MASTER AUDIO =================

def create_master_audio(
    segments,
    total_duration,
    output
):

    print(
        "🎧 Building synchronized audio..."
    )

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
            .from_file(
                clip_file
            )
            .set_frame_rate(
                44100
            )
            .set_channels(
                2
            )
        )

        position=int(
            seg["final_start"]
            *1000
        )

        master=master.overlay(
            clip,
            position=position
        )

    master.export(
        output,
        format="wav"
    )

# ================= ONE-PASS VIDEO =================

def render_video(
    source,
    segments,
    output
):

    source_duration=duration(
        source
    )

    print(
        "🎬 Rendering video in "
        "ONE encode..."
    )

    filters=[]
    labels=[]

    # ---------- opening ----------

    fir
