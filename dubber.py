import os,sys,subprocess,asyncio,time,re,json,shutil,random
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
    return float(subprocess.check_output([
        "ffprobe","-v","error",
        "-show_entries","format=duration",
        "-of","default=noprint_wrappers=1:nokey=1",
        path
    ]).strip())

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
        "format":"bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl":video,
        "merge_output_format":"mp4",
        "quiet":True,
        "no_warnings":True
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info=ydl.extract_info(url,download=True)

    meta={
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
            meta,
            f,
            ensure_ascii=False
        )

    print(f"TITLE_EMIT: {meta['title']}")
    print("🎵 Extracting audio...")

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

    return any(x in t for x in (
        "<html",
        "<!doctype",
        "server error",
        "captcha",
        "unusual traffic"
    ))

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

            if result and not bad_translation(result):
                return result.strip()

        except:
            time.sleep(.25)

    return text

# ================= WHISPER =================

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
        f"🌍 Detected language: {info.language}"
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
        current.append(s["text"])
        current_end=s["end"]

        terminal=s["text"].rstrip().endswith(
            endings
        )

        acoustic_gap=False

        if i+1<len(raw):
            gap=raw[i+1]["start"]-s["end"]
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
            text=" ".join(current).strip()

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
        f"📝 {len(blocks)} semantic sentences."
    )

    segments=[]

    preview=False

    for i,b in enumerate(blocks):

        if i+1<len(blocks):
            next_start=blocks[i+1]["start"]

            available=max(
                .5,
                next_start-b["start"]-SAFETY_GAP
            )
        else:
            available=max(
                .5,
                b["end"]-b["start"]+2
            )

        translated=translate(
            b["text"],
            target_lang
        )

        if not translated:
            translated=b["text"]

        if not preview:
            print(
                "TRANSLATION_PREVIEW: "
                +translated[:120]
            )
            preview=True

        segments.append({
            "index":i,
            "start":b["start"],
            "end":b["end"],
            "available_slot":available,
            "translated_text":translated,
            "target_lang":target_lang
        })

    return segments

# ================= SILENCE =================

def strip_dead_silence(
    source,
    output,
    threshold=SILENCE_THRESHOLD
):
    audio=AudioSegment.from_file(source)

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
        chunk=audio[p:p+step]

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

    await communicate.save(output)

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
        shutil.rmtree(temp_dir)

    os.makedirs(temp_dir)

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
            f"🔊 TTS {i+1}/{len(segments)}"
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

            # Never make speech excessively fast.
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

                seg["audio_speed"]=factor

            else:
                final_file=clean
                final_duration=original_duration
                seg["audio_speed"]=1.0

            seg["clip_file"]=final_file
            seg["tts_duration"]=final_duration

            print(
                f"   original={original_duration:.2f}s "
                f"final={final_duration:.2f}s "
                f"speed={seg['audio_speed']:.3f}x"
            )

        except Exception as e:
            print(
                f"⚠️ TTS error {i}: {e}"
            )

            seg["clip_file"]=None
            seg["tts_duration"]=0

# ================= TIMELINE =================

def build_timeline(segments):
    cursor=0.0

    for i,seg in enumerate(segments):

        original_start=seg["start"]
        available=seg["available_slot"]
        speech=seg["tts_duration"]

        # Normally anchor to source position.
        # After a freeze, never move backwards.
        start=max(
            original_start,
            cursor
        )

        speech_end=start+speech

        normal_end=start+available

        freeze=max(
            0.0,
            speech_end-normal_end
        )

        if not FREEZE_ENABLED:
            freeze=0.0

        visual_end=(
            normal_end+freeze
            if freeze>0
            else normal_end
        )

        visual_end=max(
            visual_end,
            speech_end
        )

        seg["final_start"]=start
        seg["speech_end"]=speech_end
        seg["freeze_duration"]=freeze
        seg["visual_end"]=visual_end

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
    print("🎧 Building synchronized audio...")

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
            or not os.path.exists(clip_file)
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

# ================= ONE-PASS VIDEO =================

def render_video(
    source,
    segments,
    output
):
    source_duration=duration(source)

    print(
        "🎬 Rendering synchronized video "
        "in ONE encode..."
    )

    filters=[]
    labels=[]

    # ---------- opening ----------
    first_start=segments[0]["start"]

    if first_start>.01:

        filters.append(
            "[0:v]"
            f"trim=start=0:end={first_start:.6f},"
            "setpts=PTS-STARTPTS"
            "[v0]"
        )

        labels.append("[v0]")

    label_number=1

    # ---------- sentence video ----------
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

        label=f"v{label_number}"

        part=(
            "[0:v]"
            f"trim=start={start:.6f}:end={end:.6f},"
            "setpts=PTS-STARTPTS"
        )

        freeze=seg.get(
            "freeze_duration",
            0
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

        label_number+=1

    if not labels:
        raise RuntimeError(
            "No video segments available."
        )

    joined="".join(labels)

    filters.append(
        f"{joined}"
        f"concat=n={len(labels)}:v=1:a=0,"
        "format=yuv420p[v]"
    )

    # IMPORTANT:
    # Apply the 10% speed increase HERE,
    # during the SAME video encode.
    filters.append(
        f"[v]setpts=PTS/{FINAL_SPEED:.5f}[vout]"
    )

    filter_complex=";".join(filters)

    run([
        "ffmpeg","-y",
        "-i",source,
        "-filter_complex",filter_complex,
        "-map","[vout]",
        "-an",
        "-c:v","libx264",
        "-preset",VIDEO_PRESET,
        "-crf",VIDEO_CRF,
        "-pix_fmt","yuv420p",
        "-movflags","+faststart",
        output
    ])

# ================= FINAL MUX =================

def mux_audio(
    video,
    audio,
    output
):
    print("🎧 Encoding audio and muxing...")

    # Audio gets the exact same 10% speed change
    # as the video.
    audio_filter=f"atempo={FINAL_SPEED:.5f}"

    run([
        "ffmpeg","-y",
        "-i",video,
        "-i",audio,
        "-map","0:v:0",
        "-map","1:a:0",
        "-af",audio_filter,
        "-c:v","copy",
        "-c:a","aac",
        "-b:a",AUDIO_BITRATE,
        "-movflags","+faststart",
        "-map_metadata","-1",
        "-map_chapters","-1",
        "-shortest",
        output
    ])

# ================= SRT =================

def timestamp(seconds):
    ms=int(
        round(
            max(0,seconds)*1000
        )
    )

    h=ms//3600000
    ms%=3600000

    m=ms//60000
    ms%=60000

    s=ms//1000
    ms%=1000

    return (
        f"{h:02d}:"
        f"{m:02d}:"
        f"{s:02d},"
        f"{ms:03d}"
    )

def generate_srt(
    segments,
    output="subtitles.srt"
):
    print("📝 Creating subtitles...")

    with open(
        output,
        "w",
        encoding="utf-8"
    ) as f:

        index=1

        for seg in segments:

            text=seg[
                "translated_text"
            ].strip()

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

            f.write(
                f"{index}\n"
                f"{timestamp(start)} --> "
                f"{timestamp(end)}\n"
                f"{text}\n\n"
            )

            index+=1

# ================= CLEANUP =================

def cleanup():

    for directory in (
        "temp_audio",
    ):
        if os.path.exists(directory):
            try:
                shutil.rmtree(directory)
            except:
                pass

    for file in (
        "raw_source.mp4",
        "input_audio.wav",
        "synced_master.wav",
        "extended_video.mp4"
    ):
        if os.path.exists(file):
            try:
                os.remove(file)
            except:
                pass

# ================= MAIN =================

if __name__=="__main__":

    if len(sys.argv)<2:
        print(
            'Usage: python dubber.py "VIDEO_URL" [language]'
        )
        print(
            'Example: python dubber.py "https://..." hi'
        )
        sys.exit(1)

    url=sys.argv[1]

    target=(
        sys.argv[2]
        if len(sys.argv)>2
        else "hi"
    ).lower()

    print()
    print("="*60)
    print("AI DUBBER - OPTIMIZED SYNCHRONIZATION")
    print("="*60)
    print(
        f"Target language : {target}"
    )
    print(
        f"TTS rate        : {TTS_RATE}"
    )
    print(
        f"Max TTS stretch : {MAX_ATEMPO}x"
    )
    print(
        f"Final speed     : {FINAL_SPEED}x"
    )
    print(
        f"Video CRF       : {VIDEO_CRF}"
    )
    print(
        f"Video preset    : {VIDEO_PRESET}"
    )
    print(
        f"Audio bitrate   : {AUDIO_BITRATE}"
    )
    print(
        f"Freeze frames   : {FREEZE_ENABLED}"
    )
    print("="*60)
    print()

    started=time.time()

    try:

        # 1. Download
        video,audio,meta=download_media(
            url
        )

        source_duration=duration(
            video
        )

        source_size=(
            os.path.getsize(video)
            /1048576
        )

        print(
            f"📦 Source: "
            f"{source_size:.2f} MB | "
            f"{source_duration:.2f}s"
        )

        # 2. Whisper + translation
        segments=(
            transcribe_and_translate(
                audio,
                target
            )
        )

        if not segments:
            raise RuntimeError(
                "No speech segments detected."
            )

        # 3. TTS
        asyncio.run(
            synthesize_audio(
                segments
            )
        )

        # 4. Build non-colliding timeline
        build_timeline(
            segments
        )

        timeline_duration=max(
            source_duration,
            max(
                (
                    s["visual_end"]
                    for s in segments
                ),
                default=0
            )
        )

        print(
            f"📏 Timeline duration: "
            f"{timeline_duration:.2f}s"
        )

        # 5. Master audio
        master_audio="synced_master.wav"

        create_master_audio(
            segments,
            timeline_duration,
            master_audio
        )

        # 6. ONE video encode
        intermediate_video=(
            "extended_video.mp4"
        )

        render_video(
            video,
            segments,
            intermediate_video
        )

        # 7. Final audio mux.
        # Video is COPIED here, not encoded again.
        mux_audio(
            intermediate_video,
            master_audio,
            "final_output.mp4"
        )

        # 8. Subtitles
        generate_srt(
            segments,
            "subtitles.srt"
        )

        # 9. Results
        final_size=(
            os.path.getsize(
                "final_output.mp4"
            )/1048576
        )

        final_duration=duration(
            "final_output.mp4"
        )

        elapsed=(
            time.time()-started
        )

        print()
        print("="*60)
        print("✅ DUBBING COMPLETE")
        print("="*60)
        print(
            f"Source size     : "
            f"{source_size:.2f} MB"
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
            f"Final speed     : "
            f"{FINAL_SPEED}x"
        )
        print(
            f"Processing time : "
            f"{elapsed/60:.2f} minutes"
        )
        print()
        print(
            "🎬 final_output.mp4"
        )
        print(
            "📝 subtitles.srt"
        )
        print("="*60)

        cleanup()

    except Exception as e:

        print()
        print("="*60)
        print("❌ DUBBING FAILED")
        print("="*60)
        print(str(e))
        print("="*60)

        raise
