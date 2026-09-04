import os
import sys
import subprocess
import asyncio
import time
import re
import json
import random
import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator
import edge_tts
from pydub import AudioSegment

# ==========================================
# 1. 📥 Download Media & Extract Source Info
# ==========================================
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
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': video_path,
        'quiet': True,
        'no_warnings': True,
    }

    print("📥 Fetching source stream and metadata...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        meta = ydl.extract_info(url, download=True)
        meta_dict = {
            "title": meta.get("title", "Dubbed Video"),
            "description": meta.get("description", ""),
            "tags": meta.get("tags", [])
        }

    with open("source_meta.json", "w", encoding="utf-8") as mf:
        json.dump(meta_dict, mf, ensure_ascii=False)

    print(f"TITLE_EMIT: {meta_dict['title']}")

    subprocess.run([
        'ffmpeg', '-y', '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        audio_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    return video_path, audio_path, meta_dict

def get_duration(file_path: str) -> float:
    cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \"{file_path}\""
    return float(subprocess.check_output(cmd, shell=True).strip())

# ==========================================
# 2. 🛡️ Robust Translation & Filtering
# ==========================================
def is_invalid_output(text: str) -> bool:
    if not text:
        return True
    lowered = text.lower()
    error_signatures = [
        "server error", "500 (server error)", "!!1500", "please try again",
        "<!doctype", "<html", "<head>", "that's all we know", "403 forbidden",
        "unusual traffic", "captcha"
    ]
    return any(sig in lowered for sig in error_signatures)

def translate_text_enforced(text: str, source_lang: str, target_lang: str) -> str:
    clean_text = re.sub(r'[\r\n\t]+', ' ', text).strip()
    if not clean_text or len(clean_text) < 2:
        return clean_text

    if source_lang == target_lang:
        return clean_text

    for _ in range(2):
        try:
            res = GoogleTranslator(source='auto', target=target_lang).translate(clean_text)
            if res and not is_invalid_output(res) and res.strip().lower() != clean_text.lower():
                time.sleep(random.uniform(0.1, 0.25))
                return res.strip()
        except Exception:
            time.sleep(0.4)

    try:
        s_lang = source_lang if source_lang and source_lang != 'auto' else 'en'
        res = MyMemoryTranslator(source=s_lang, target=target_lang).translate(clean_text)
        if res and not is_invalid_output(res) and res.strip().lower() != clean_text.lower():
            return res.strip()
    except Exception:
        pass

    return clean_text

def transcribe_and_translate(audio_path: str, target_lang: str = "hi") -> list[dict]:
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    raw_segments, info = model.transcribe(audio_path, language=None, vad_filter=True)
    detected_lang = info.language

    raw_list = []
    for s in raw_segments:
        t = s.text.strip()
        if t and (s.end - s.start >= 0.25):
            raw_list.append({"start": s.start, "end": s.end, "text": t})

    if not raw_list:
        return []

    # Merge tight adjacent segments (< 0.2s)
    merged = [raw_list[0]]
    for s in raw_list[1:]:
        prev = merged[-1]
        if s["start"] - prev["end"] < 0.2:
            prev["end"] = max(prev["end"], s["end"])
            prev["text"] += " " + s["text"]
        else:
            merged.append(s)

    segments = []
    first_preview_printed = False

    for s in merged:
        translated = translate_text_enforced(s["text"], detected_lang, target_lang)

        if not first_preview_printed and translated and not is_invalid_output(translated):
            words = translated.strip().split()
            short_preview = " ".join(words[:5]) + ("..." if len(words) > 5 else "")
            print(f"TRANSLATION_PREVIEW: {short_preview}")
            first_preview_printed = True

        segments.append({
            "orig_start": s["start"],
            "orig_end": s["end"],
            "translated_text": translated,
            "target_lang": target_lang
        })

    return segments

# ==========================================
# 3. 🗣️ Speech Synthesis
# ==========================================
async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    for i, seg in enumerate(segments):
        audio_file = os.path.join(temp_dir, f"audio_{i}.mp3")
        text = seg["translated_text"].strip()
        lang = seg.get("target_lang", "hi")

        voice = "hi-IN-MadhurNeural"
        if lang == "en":
            voice = "en-US-GuyNeural"
        elif lang == "es":
            voice = "es-ES-AlvaroNeural"
        elif lang == "fr":
            voice = "fr-FR-HenriNeural"
        elif lang == "de":
            voice = "de-DE-ConradNeural"

        clean_tts_text = re.sub(r'[\[\]\(\)\{\}\<\>\"\'\*\#\@]', '', text).strip()
        orig_dur = max(0.4, seg["orig_end"] - seg["orig_start"])

        if is_invalid_output(clean_tts_text) or not clean_tts_text:
            silent = AudioSegment.silent(duration=int(orig_dur * 1000))
            silent.export(audio_file, format="mp3")
            seg["audio_file"] = audio_file
            seg["tts_dur"] = orig_dur
            continue

        seg["audio_file"] = audio_file
        try:
            communicate = edge_tts.Communicate(clean_tts_text, voice, rate="+15%")
            await communicate.save(audio_file)
            seg["tts_dur"] = get_duration(audio_file)
        except Exception:
            silent = AudioSegment.silent(duration=int(orig_dur * 1000))
            silent.export(audio_file, format="mp3")
            seg["tts_dur"] = orig_dur

# ==========================================
# 4. ❄️ Frame Freeze & Time-Synchronized Assembly
# ==========================================
def render_dubbed_video_with_freeze(video_path: str, segments: list[dict], output_file: str):
    total_src_duration = get_duration(video_path)
    os.makedirs("chunks", exist_ok=True)
    concat_list = []
    
    current_src_pos = 0.0
    accumulated_delay = 0.0

    print("🎬 Constructing time-synchronized timeline with dynamic last-frame freeze...")

    # Build intermediate chunks
    for i, seg in enumerate(segments):
        s_start = max(current_src_pos, seg["orig_start"])
        s_end = min(total_src_duration, seg["orig_end"])

        # 1. Play intervening gap (if any gap between previous dialogue and this one)
        if s_start > current_src_pos + 0.05:
            gap_dur = s_start - current_src_pos
            gap_file = f"chunks/gap_{i}.mp4"
            cmd = [
                'ffmpeg', '-y',
                '-ss', f"{current_src_pos:.3f}",
                '-i', video_path,
                '-t', f"{gap_dur:.3f}",
                '-f', 'lavfi', '-t', f"{gap_dur:.3f}", '-i', 'anullsrc=r=44100:cl=stereo',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '128k',
                '-shortest', gap_file
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            concat_list.append(gap_file)

        # 2. Main dialogue video slice
        dial_dur = max(0.2, s_end - s_start)
        tts_dur = seg["tts_dur"]
        
        # Calculate freeze duration if Hindi TTS takes longer than dialogue slot
        freeze_dur = max(0.0, tts_dur - dial_dur)

        # Record timeline anchors for SRT subtitles
        seg["dubbed_start"] = s_start + accumulated_delay
        seg["dubbed_end"] = s_end + accumulated_delay + freeze_dur
        accumulated_delay += freeze_dur

        # Render dialogue clip with TTS voice
        dial_file = f"chunks/dial_{i}.mp4"
        cmd = [
            'ffmpeg', '-y',
            '-ss', f"{s_start:.3f}",
            '-i', video_path,
            '-t', f"{dial_dur:.3f}",
            '-i', seg["audio_file"],
            '-map', '0:v:0',
            '-map', '1:a:0',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k',
            '-t', f"{dial_dur:.3f}",
            dial_file
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        concat_list.append(dial_file)

        # 3. If freeze required: extract the very last frame and hold it
        if freeze_dur >= 0.15:
            freeze_file = f"chunks/freeze_{i}.mp4"
            last_frame_img = f"chunks/last_frame_{i}.jpg"
            
            # Grab last frame
            subprocess.run([
                'ffmpeg', '-y',
                '-ss', f"{max(0.0, s_end - 0.05):.3f}",
                '-i', video_path,
                '-vframes', '1',
                '-q:v', '2',
                last_frame_img
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            # Hold the frame as video with the remainder of TTS audio
            remainder_audio = AudioSegment.from_file(seg["audio_file"])
            rem_ms = int(dial_dur * 1000)
            freeze_audio_seg = remainder_audio[rem_ms:] if len(remainder_audio) > rem_ms else AudioSegment.silent(duration=int(freeze_dur * 1000))
            freeze_wav = f"chunks/freeze_audio_{i}.wav"
            freeze_audio_seg.export(freeze_wav, format="wav")

            cmd_freeze = [
                'ffmpeg', '-y',
                '-loop', '1', '-t', f"{freeze_dur:.3f}", '-i', last_frame_img,
                '-i', freeze_wav,
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '192k',
                '-shortest', freeze_file
            ]
            subprocess.run(cmd_freeze, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            concat_list.append(freeze_file)

        current_src_pos = s_end

    # 4. Tail slice (if video extends past the last dialogue)
    if current_src_pos < total_src_duration:
        tail_dur = total_src_duration - current_src_pos
        tail_file = "chunks/tail.mp4"
        cmd = [
            'ffmpeg', '-y',
            '-ss', f"{current_src_pos:.3f}",
            '-i', video_path,
            '-t', f"{tail_dur:.3f}",
            '-f', 'lavfi', '-t', f"{tail_dur:.3f}", '-i', 'anullsrc=r=44100:cl=stereo',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',
            '-shortest', tail_file
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        concat_list.append(tail_file)

    # 5. Concatenate all slices into master output
    concat_txt = "chunks/concat_manifest.txt"
    with open(concat_txt, "w", encoding="utf-8") as f:
        for c in concat_list:
            f.write(f"file '{os.path.abspath(c)}'\n")

    print("🛡️ Merging slices, purging metadata, and encoding master reel...")
    subprocess.run([
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '0', '-i', concat_txt,
        '-map_metadata', '-1',
        '-map_chapters', '-1',
        '-fflags', '+bitexact',
        '-flags:v', '+bitexact',
        '-flags:a', '+bitexact',
        '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '2600k',
        '-c:a', 'aac', '-b:a', '192k',
        output_file
    ], check=True)

    # Clean temporary chunks directory
    try:
        import shutil
        shutil.rmtree("chunks")
    except Exception:
        pass

# ==========================================
# 5. 📝 Synchronized Subtitles (.srt)
# ==========================================
def format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def generate_srt(segments: list[dict], output_file: str = "subtitles.srt"):
    with open(output_file, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            start_str = format_timestamp(seg.get("dubbed_start", seg["orig_start"]))
            end_str = format_timestamp(seg.get("dubbed_end", seg["orig_end"]))
            txt = seg['translated_text']
            if is_invalid_output(txt):
                txt = ""
            f.write(f"{i}\n{start_str} --> {end_str}\n{txt}\n\n")

# ==========================================
# 🚀 Entrypoint
# ==========================================
if __name__ == "__main__":
    v_url = sys.argv[1]
    tgt_lang = sys.argv[2] if len(sys.argv) > 2 else "hi"

    v_file, a_file, meta = download_media(v_url)
    segs = transcribe_and_translate(a_file, target_lang=tgt_lang)
    asyncio.run(synthesize_audio(segs))

    render_dubbed_video_with_freeze(v_file, segs, "final_output.mp4")
    generate_srt(segs, "subtitles.srt")
    print("✅ Perfectly synchronized dubbed video rendered with frame-freeze protection.")
