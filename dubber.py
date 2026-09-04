import os,sys,subprocess,asyncio,time,re,json,shutil
import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator,MyMemoryTranslator
import edge_tts
from pydub import AudioSegment

# ================= CONFIG =================

TTS_RATE="+8%"
MIN_RATE=4
MAX_RATE=12
FINAL_SPEED=1.10
ENABLE_FREEZE=True
SAFETY_GAP=0.03
SILENCE_THRESHOLD=-42
WHISPER_MODEL="tiny"

# CRF: 21=high quality,23=balanced,25=smaller
VIDEO_CRF="25"
VIDEO_PRESET="medium"
AUDIO_BITRATE="128k"

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

# ================= UTILITIES =================

def run_command(cmd,quiet=False):
    return subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
        check=True
    )

def get_duration(path):
    result=subprocess.check_output([
        "ffprobe","-v","error",
        "-show_entries","format=duration",
        "-of","default=noprint_wrappers=1:nokey=1",
        path
    ])
    return float(result.strip())

# ================= DOWNLOAD =================

def download_media(url):
    video_path="raw_source.mp4"
    audio_path="input_audio.wav"

    for path in (video_path,audio_path):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    opts={
        "format":"bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl":video_path,
        "merge_output_format":"mp4",
        "quiet":True,
        "no_warnings":True
    }

    print("📥 Downloading media...")

    with yt_dlp.YoutubeDL(opts) as ydl:
        meta=ydl.extract_info(url,download=True)
        metadata={
            "title":meta.get("title","Dubbed Video"),
            "description":meta.get("description",""),
            "tags":meta.get("tags",[])
        }

    with open("source_meta.json","w",encoding="utf-8") as f:
        json.dump(metadata,f,ensure_ascii=False,indent=2)

    print(f"TITLE_EMIT: {metadata['title']}")
    print("🎵 Extracting source audio...")

    run_command([
        "ffmpeg","-y",
        "-i",video_path,
        "-vn",
        "-acodec","pcm_s16le",
        "-ar","16000",
        "-ac","1",
        audio_path
    ],quiet=True)

    return video_path,audio_path,metadata

# ================= TRANSLATION =================

def contains_devanagari(text):
    return bool(re.search(r"[\u0900-\u097F]",text))

def is_error_page(text):
    t=text.lower()
    return any(x in t for x in (
        "server error","<!doctype","<html","captcha","unusual traffic"
    ))

def translate_text(text,target_lang):
    text=re.sub(r"[\r\n\t]+"," ",text).strip()

    if not text or len(text)<2:
        return text

    if target_lang=="hi":
        for _ in range(3):
            try:
                result=GoogleTranslator(
                    source="auto",
                    target="hi"
                ).translate(text)

                if result and not is_error_page(result) and contains_devanagari(result):
                    return result.strip()
            except Exception:
                time.sleep(.3)

        try:
            result=MyMemoryTranslator(
                source="auto",
                target="hi-IN"
            ).translate(text)

            if result and not is_error_page(result):
                return result.strip()
        except Exception:
            pass

        return ""

    for _ in range(3):
        try:
            result=GoogleTranslator(
                source="auto",
                target=target_lang
            ).translate(text)

            if result and not is_error_page(result):
                return result.strip()
        except Exception:
            time.sleep(.3)

    return text

# ================= WHISPER =================

def transcribe_and_translate(audio_path,target_lang):
    print("🧠 Loading Whisper...")

    model=WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8"
    )

    print("🎙️ Transcribing source audio...")

    raw_segments,info=model.transcribe(
        audio_path,
        language=None,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms":350
        }
    )

    print(f"🌍 Detected source language: {info.language}")

    raw=[]

    for segment in raw_segments:
        text=segment.text.strip()

        if not text:
            continue

        duration=segment.end-segment.start

        if duration<.15:
            continue

        raw.append({
            "start":float(segment.start),
            "end":float(segment.end),
            "text":text
        })

    if not raw:
        return []

    print(f"📝 Whisper found {len(raw)} speech segments.")

    segments=[]

    for i,source in enumerate(raw):
        start=source["start"]

        if i+1<len(raw):
            source_slot=raw[i+1]["start"]-start-SAFETY_GAP
        else:
            source_slot=source["end"]-start+2

        source_slot=max(.25,source_slot)

        translated=translate_text(
            source["text"],
            target_lang
        )

        if not translated:
            translated=source["text"]

        if i==0:
            print("TRANSLATION_PREVIEW: "+translated[:120])

        segments.append({
            "index":i,
            "start":start,
            "source_end":source["end"],
            "source_slot":source_slot,
            "source_text":source["text"],
            "translated_text":translated,
            "target_lang":target_lang
        })

    return segments

# ================= TTS =================

def strip_dead_silence(input_file,output_file):
    audio=AudioSegment.from_file(input_file)

    if len(audio)==0:
        return

    start_trim=0
    end_trim=len(audio)

    for pos in range(0,len(audio),10):
        chunk=audio[pos:pos+10]

        if chunk.dBFS>SILENCE_THRESHOLD:
            start_trim=max(0,pos-20)
            break

    for pos in range(len(audio)-10,0,-10):
        chunk=audio[pos:pos+10]

        if chunk.dBFS>SILENCE_THRESHOLD:
            end_trim=min(len(audio),pos+30)
            break

    stripped=audio if end_trim<=start_trim else audio[start_trim:end_trim]

    stripped=(
        stripped
        .set_frame_rate(44100)
        .set_channels(2)
    )

    stripped.export(output_file,format="wav")

async def synthesize_one(text,voice,rate,output_file):
    communicator=edge_tts.Communicate(
        text,
        voice,
        rate=rate
    )
    await communicator.save(output_file)

async def synthesize_audio(segments,temp_dir="temp_audio"):
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)

    os.makedirs(temp_dir,exist_ok=True)

    for i,seg in enumerate(segments):
        text=seg.get("translated_text","").strip()

        if not text:
            seg["clip_file"]=None
            continue

        target_lang=seg.get("target_lang","hi").lower()
        base_lang=target_lang.split("-")[0]
        voice=VOICE_MAP.get(base_lang,"en-US-GuyNeural")

        raw_file=os.path.join(
            temp_dir,
            f"raw_{i}.mp3"
        )

        stripped_file=os.path.join(
            temp_dir,
            f"strip_{i}.wav"
        )

        print(
            f"🔊 Generating TTS {i+1}/{len(segments)} "
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

            duration=get_duration(stripped_file)

            seg["clip_file"]=stripped_file
            seg["tts_duration"]=duration

            print(f"   TTS duration: {duration:.2f}s")

        except Exception as e:
            print(f"⚠️ TTS error on segment {i}: {e}")
            seg["clip_file"]=None
            seg["tts_duration"]=0

# ================= TIMELINE =================

def calculate_dub_timeline(segments):
    current_time=0

    for i,seg in enumerate(segments):
        original_start=seg["start"]
        original_slot=seg["source_slot"]
        tts_duration=seg.get("tts_duration",0)

        new_start=original_start if i==0 else current_time
        speech_end=new_start+tts_duration
        original_end=original_start+original_slot

        visual_end=max(
            new_start+original_slot,
            speech_end
        )

        freeze_duration=max(
            0,
            speech_end-(new_start+original_slot)
        )

        seg["new_start"]=new_start
        seg["speech_end"]=speech_end
        seg["original_visual_end"]=original_end
        seg["visual_end"]=visual_end
        seg["freeze_duration"]=freeze_duration

        current_time=visual_end

        print(
            f"⏱ Segment {i+1}: "
            f"start={new_start:.3f}s | "
            f"TTS={tts_duration:.3f}s | "
            f"slot={original_slot:.3f}s | "
            f"freeze={freeze_duration:.3f}s | "
            f"next={current_time:.3f}s"
        )

    return segments

# ================= MASTER AUDIO =================

def build_master_audio(segments,total_duration,output_file):
    print("🎧 Building dubbed audio timeline...")

    master=AudioSegment.silent(
        duration=int((total_duration+.5)*1000),
        frame_rate=44100
    )

    master=master.set_frame_rate(44100).set_channels(2)

    for seg in segments:
        clip_file=seg.get("clip_file")

        if not clip_file or not os.path.exists(clip_file):
            continue

        clip=(
            AudioSegment
            .from_file(clip_file)
            .set_frame_rate(44100)
            .set_channels(2)
        )

        position=int(seg["new_start"]*1000)

        master=master.overlay(
            clip,
            position=position
        )

        seg["final_start"]=seg["new_start"]
        seg["final_end"]=seg["speech_end"]

    master.export(
        output_file,
        format="wav"
    )

    return output_file

# ================= VIDEO PART =================

def create_video_part(
    video_file,
    source_start,
    source_end,
    freeze_duration,
    output_file
):
    duration=source_end-source_start

    if duration<=0:
        return False

    filters=[
        f"trim=start={source_start:.6f}:end={source_end:.6f}",
        "setpts=PTS-STARTPTS"
    ]

    if ENABLE_FREEZE and freeze_duration>.01:
        filters.append(
            "tpad="
            "stop_mode=clone:"
            f"stop_duration={freeze_duration:.6f}"
        )

    cmd=[
        "ffmpeg","-y",
        "-i",video_file,
        "-an",
        "-vf",",".join(filters),
        "-c:v","libx264",
        "-preset",VIDEO_PRESET,
        "-crf",VIDEO_CRF,
        "-pix_fmt","yuv420p",
        "-movflags","+faststart",
        output_file
    ]

    run_command(cmd,quiet=True)
    return True

# ================= VIDEO CONCAT =================

def concatenate_video_parts(parts,output_file):
    concat_file="video_concat.txt"

    with open(concat_file,"w",encoding="utf-8") as f:
        for part in parts:
            absolute=os.path.abspath(part).replace("\\","/")
            absolute=absolute.replace("'","'\\''")
            f.write(f"file '{absolute}'\n")

    run_command([
        "ffmpeg","-y",
        "-f","concat",
        "-safe","0",
        "-i",concat_file,
        "-an",
        "-c:v","libx264",
        "-preset",VIDEO_PRESET,
        "-crf",VIDEO_CRF,
        "-pix_fmt","yuv420p",
        "-movflags","+faststart",
        output_file
    ])

    if os.path.exists(concat_file):
        os.remove(concat_file)

# ================= EXTENDED VIDEO =================

def build_extended_video(video_file,segments,output_file):
    source_duration=get_duration(video_file)

    print("🎬 Building freeze-frame extended video...")

    temp_dir="temp_video_parts"

    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)

    os.makedirs(temp_dir)

    parts=[]
    previous_source_end=0

    for i,seg in enumerate(segments):
        source_start=seg["start"]

        if i==0 and source_start>.01:
            opening=os.path.join(
                temp_dir,
                "part_000_opening.mp4"
            )

            create_video_part(
                video_file,
                0,
                source_start,
                0,
                opening
            )

            parts.append(opening)

        if i+1<len(segments):
            source_end=segments[i+1]["start"]
        else:
            source_end=source_duration

        source_end=min(
            source_end,
            source_duration
        )

        if source_end<=source_start:
            continue

        freeze_duration=seg.get(
            "freeze_duration",
            0
        )

        part_file=os.path.join(
            temp_dir,
            f"part_{i+1:04d}.mp4"
        )

        print(
            f"🎞️ Segment {i+1}: "
            f"{source_start:.3f} → "
            f"{source_end:.3f} + "
            f"freeze {freeze_duration:.3f}s"
        )

        create_video_part(
            video_file,
            source_start,
            source_end,
            freeze_duration,
            part_file
        )

        parts.append(part_file)
        previous_source_end=source_end

    if not parts:
        raise RuntimeError("No video parts were created.")

    concatenate_video_parts(
        parts,
        output_file
    )

    return output_file

# ================= FINAL MUX + SPEED =================

def mux_final_video(
    extended_video,
    dubbed_audio,
    output_file
):
    print(
        f"🎬 Applying {int((FINAL_SPEED-1)*100)}% "
        f"final speed increase..."
    )

    video_filter=f"setpts=PTS/{FINAL_SPEED:.4f}"
    audio_filter=f"atempo={FINAL_SPEED:.4f}"

    run_command([
        "ffmpeg","-y",
        "-i",extended_video,
        "-i",dubbed_audio,
        "-map","0:v:0",
        "-map","1:a:0",
        "-vf",video_filter,
        "-af",audio_filter,
        "-c:v","libx264",
        "-preset",VIDEO_PRESET,
        "-crf",VIDEO_CRF,
        "-pix_fmt","yuv420p",
        "-c:a","aac",
        "-b:a",AUDIO_BITRATE,
        "-movflags","+faststart",
        "-map_metadata","-1",
        "-map_chapters","-1",
        output_file
    ])

# ================= SRT =================

def format_timestamp(seconds):
    seconds=max(0,seconds)
    total_ms=int(round(seconds*1000))

    hours=total_ms//3600000
    total_ms%=3600000

    minutes=total_ms//60000
    total_ms%=60000

    secs=total_ms//1000
    millis=total_ms%1000

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d},"
        f"{millis:03d}"
    )

def generate_srt(segments,output_file="subtitles.srt"):
    print("📝 Creating synchronized subtitles...")

    with open(
        output_file,
        "w",
        encoding="utf-8"
    ) as f:
        index=1

        for seg in segments:
            text=seg.get(
                "translated_text",
                ""
            ).strip()

            if not text:
                continue

            start=seg.get(
                "final_start",
                seg["new_start"]
            )

            end=seg.get(
                "final_end",
                seg["speech_end"]
            )

            # Final output is 10% faster,
            # therefore subtitle timestamps must also
            # be compressed by the same factor.
            start/=FINAL_SPEED
            end/=FINAL_SPEED

            if end<=start:
                end=start+.5

            f.write(
                f"{index}\n"
                f"{format_timestamp(start)} --> "
                f"{format_timestamp(end)}\n"
                f"{text}\n\n"
            )

            index+=1

# ================= CLEANUP =================

def cleanup():
    for directory in (
        "temp_audio",
        "temp_video_parts"
    ):
        if os.path.exists(directory):
            try:
                shutil.rmtree(directory)
            except Exception:
                pass

    for file in (
        "raw_source.mp4",
        "input_audio.wav",
        "synced_master.wav",
        "extended_video.mp4",
        "video_concat.txt"
    ):
        if os.path.exists(file):
            try:
                os.remove(file)
            except Exception:
                pass

# ================= MAIN =================

if __name__=="__main__":
    if len(sys.argv)<2:
        print('Usage: python dub.py "VIDEO_URL" [target_language]')
        print('Example: python dub.py "https://..." hi')
        sys.exit(1)

    video_url=sys.argv[1]
    target_language=(
        sys.argv[2]
        if len(sys.argv)>2
        else "hi"
    ).lower()

    print()
    print("="*55)
    print("AI DUBBING ENGINE")
    print("="*55)
    print(f"Target language: {target_language}")
    print(f"TTS speed: {TTS_RATE}")
    print(f"Final speed: {FINAL_SPEED}x")
    print(f"Video CRF: {VIDEO_CRF}")
    print(f"Video preset: {VIDEO_PRESET}")
    print(f"Audio bitrate: {AUDIO_BITRATE}")
    print(f"Freeze frames: {ENABLE_FREEZE}")
    print("="*55)
    print()

    try:
        video_file,audio_file,metadata=download_media(
            video_url
        )

        segments=transcribe_and_translate(
            audio_file,
            target_language
        )

        if not segments:
            raise RuntimeError(
                "No speech segments were detected."
            )

        asyncio.run(
            synthesize_audio(
                segments
            )
        )

        calculate_dub_timeline(
            segments
        )

        final_duration=max(
            get_duration(video_file),
            max(
                (
                    seg["visual_end"]
                    for seg in segments
                ),
                default=0
            )
        )

        master_audio="synced_master.wav"

        build_master_audio(
            segments,
            final_duration,
            master_audio
        )

        extended_video="extended_video.mp4"

        build_extended_video(
            video_file,
            segments,
            extended_video
        )

        mux_final_video(
            extended_video,
            master_audio,
            "final_output.mp4"
        )

        generate_srt(
            segments,
            "subtitles.srt"
        )

        original_duration=get_duration(
            video_file
        )

        final_output_duration=get_duration(
            "final_output.mp4"
        )

        original_size=os.path.getsize(
            video_file
        )/1024/1024

        final_size=os.path.getsize(
            "final_output.mp4"
        )/1024/1024

        print()
        print("="*55)
        print("✅ DUBBING COMPLETE")
        print("="*55)
        print(f"Original duration : {original_duration:.2f}s")
        print(f"Final duration    : {final_output_duration:.2f}s")
        print(
            f"Original size     : {original_size:.2f} MB"
        )
        print(
            f"Final size        : {final_size:.2f} MB"
        )
        print(
            f"Final speed       : {FINAL_SPEED}x"
        )
        print()
        print("Video: final_output.mp4")
        print("SRT  : subtitles.srt")
        print("="*55)

        cleanup()

    except Exception as e:
        print()
        print("="*55)
        print("❌ DUBBING FAILED")
        print("="*55)
        print(f"Error: {e}")
        print("="*55)
        raise
