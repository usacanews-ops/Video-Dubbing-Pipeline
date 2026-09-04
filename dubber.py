import os
import sys
import subprocess
import asyncio
import time
import re
import json
import random
import shutil
import yt_dlp
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator
import edge_tts
from pydub import AudioSegment

SPEED_FACTOR = 1.12  # Uniform +12% speed increase across the entire video
MAX_MILD_COMPRESSION = 1.15  # Tier 1 max compression
MAX_FASTEN_COMPRESSION = 1.25  # Tier 2 max compression

# ==========================================
# 1. 📥 Download Media & Sanitize Streams
# ==========================================
def download_media(url: str) -> tuple[str, str, dict]:
    dl_path = "downloaded.mp4"
    video_path = "raw_source.mp4"
    audio_path = "input_audio.wav"

    for path in [dl_path, video_path, audio_path]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': dl_path,
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

    # Clean bitstream and standardize container to prevent decoder crashes
    print("🧹 Sanitizing source video streams...")
    subprocess.run([
        'ffmpeg', '-y',
        '-err_detect', 'ignore_err',
        '-i', dl_path,
        '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '192k',
        '-ar', '44100',
        video_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    if os.path.exists(dl_path):
        os.remove(dl_path)

    # 16kHz mono audio for Whisper transcription
    subprocess.run([
        'ffmpeg', '-y',
        '-err_detect', 'ignore_err',
        '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        audio_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    return video_path, audio_path, meta_dict

def get_duration(file_path: str) -> float:
    cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \"{file_path}\""
    return float(subprocess.check_output(cmd, shell=True).strip())

# ==========================================
# 2. 🛡️ Enforced Translation & Filtering
# ==========================================
def contains_devanagari(text: str) -> bool:
    return bool(re.search(r'[\u0900-\u097F]', text))

def is_error_page(text: str) -> bool:
    lowered = text.lower()
    return any(sig in lowered for sig in [
        "server error", "500", "!!1500", "<!doctype", "<html", "captcha", "unusual traffic"
    ])

def translate_to_hindi(text: str) -> str:
    clean_text = re.sub(r'[\r\n\t]+', ' ', text).strip()
    if not clean_text or len(clean_text) < 2:
        return clean_text

    for attempt in range(3):
        try:
            res = GoogleTranslator(source='auto', target='hi').translate(clean_text)
            if res and not is_error_page(res) and contains_devanagari(res):
                time.sleep(random.uniform(0.1, 0.2))
                return res.strip()
        except Exception:
            time.sleep(0.3)

    try:
        res = MyMemoryTranslator(source='en-US', target='hi-IN').translate(clean_text)
        if res and not is_error_page(res) and contains_devanagari(res):
            return res.strip()
    except Exception:
        pass

    words = clean_text.split()
    if len(words) > 2:
        try:
            mid = len(words) // 2
            p1 = GoogleTranslator(source='auto', target='hi').translate(" ".join(words[:mid]))
            p2 = GoogleTranslator(source='auto', target='hi').translate(" ".join(words[mid:]))
            combined = f"{p1} {p2}".strip()
            if contains_devanagari(combined):
                return combined
        except Exception:
            pass

    return ""

def transcribe_and_translate(audio_path: str, target_lang: str = "hi") -> list[dict]:
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    raw_segments, info = model.transcribe(audio_path, language=None, vad_filter=True)
    print(f"🌍 Detected source language: {info.language}")

    raw_list = []
    for s in raw_segments:
        t = s.text.strip()
        if t and (s.end - s.start >= 0.25):
            raw_list.append({"start": s.start, "end": s.end, "text": t})

    if not raw_list:
        return []

    # Merge very close speech fragments (< 0.2s)
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

    for i, s in enumerate(merged):
        # Allow natural scene pause up to the next dialogue start
        next_start = merged[i + 1]["start"] if i + 1 < len(merged) else s["end"] + 3.0
        available_window = max(s["end"] - s["start"], next_start - s["start"] - 0.05)

        if target_lang == "hi":
            translated = translate_to_hindi(s["text"])
        else:
            try:
                translated = GoogleTranslator(source='auto', target=target_lang).translate(s["text"])
            except Exception:
                translated = s["text"]

        if target_lang == "hi" and not contains_devanagari(translated):
            translated = ""

        if not first_preview_printed and translated:
            words = translated.strip().split()
            short_preview = " ".join(words[:5]) + ("..." if len(words) > 5 else "")
            print(f"TRANSLATION_PREVIEW: {short_preview}")
            first_preview_printed = True

        segments.append({
            "start": s["start"],
            "end": s["end"],
            "available_window": available_window,
            "translated_text": translated,
            "target_lang": target_lang
        })

    return segments

# ==========================================
# 3. 🗣️ Speech Synthesis & Adaptive 1.15x / 1.25x Fit
# ==========================================
def apply_atempo(input_wav: str, speed: float, output_wav: str):
    cmd = [
        'ffmpeg', '-y', '-err_detect', 'ignore_err',
        '-i', input_wav,
        '-filter:a', f'atempo={speed:.4f}',
        output_wav
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return output_wav

async def synthesize_audio(segments: list[dict], temp_dir: str = "temp_audio"):
    os.makedirs(temp_dir, exist_ok=True)
    for i, seg in enumerate(segments):
        raw_tts_file = os.path.join(temp_dir, f"raw_{i}.mp3")
        final_clip_file = os.path.join(temp_dir, f"clip_{i}.wav")
        text = seg["translated_text"].strip()
        lang = seg.get("target_lang", "hi")

        voice = "hi-IN-MadhurNeural"
        if lang == "en":
            voice = "en-US-GuyNeural"
        elif lang == "es":
            voice = "es-ES-AlvaroNeural"

        if not text:
            seg["clip_file"] = None
            seg["clip_dur"] = 0.0
            seg["freeze_dur"] = 0.0
            continue

        try:
            communicate = edge_tts.Communicate(text, voice, rate="+15%")
            await communicate.save(raw_tts_file)
            natural_dur = get_duration(raw_tts_file)
            available_slot = seg["available_window"]

            # Ratio of TTS duration to available scene pause
            ratio = natural_dur / available_slot if available_slot > 0 else 1.0

            if ratio <= 1.0:
                # Fits within natural scene pause; no speedup or freeze needed
                seg["clip_file"] = raw_tts_file
                seg["clip_dur"] = natural_dur
                seg["freeze_dur"] = 0.0
            elif ratio <= MAX_MILD_COMPRESSION:
                # Fits within at most 1.15x compression
                apply_atempo(raw_tts_file, ratio, final_clip_file)
                seg["clip_file"] = final_clip_file
                seg["clip_dur"] = get_duration(final_clip_file)
                seg["freeze_dur"] = 0.0
            elif ratio <= MAX_FASTEN_COMPRESSION:
                # Fasten up to 1.25x without cutting words or freezing
                apply_atempo(raw_tts_file, ratio, final_clip_file)
                seg["clip_file"] = final_clip_file
                seg["clip_dur"] = get_duration(final_clip_file)
                seg["freeze_dur"] = 0.0
            else:
                # Exceeds 1.25x: cap speed at 1.25x and freeze the frame for the remainder
                apply_atempo(raw_tts_file, MAX_FASTEN_COMPRESSION, final_clip_file)
                fastened_dur = get_duration(final_clip_file)
                seg["clip_file"] = final_clip_file
                seg["clip_dur"] = fastened_dur
                seg["freeze_dur"] = max(0.0, fastened_dur - available_slot)

        except Exception as e:
            print(f"⚠️ Synthesis error on segment {i}: {e}")
            seg["clip_file"] = None
            seg["clip_dur"] = 0.0
            seg["freeze_dur"] = 0.0

# ==========================================
# 4. 🎬 Assembly: Dynamic Freeze + +12% Speed
# ==========================================
def render_dubbed_video(video_path: str, segments: list[dict], output_file: str):
    src_total_dur = get_duration(video_path)
    needs_any_freeze = any(s.get("freeze_dur", 0.0) >= 0.15 for s in segments)

    # -------------------------------------------------------------
    # Case A: No dialogue exceeded 1.25x (Fast single-pass overlay)
    # -------------------------------------------------------------
    if not needs_any_freeze:
        print("🎬 All dialogues fit cleanly within 1.25x speed. Rendering master timeline...")
        master_track = AudioSegment.silent(duration=int((src_total_dur + 2.0) * 1000))
        for seg in segments:
            if seg.get("clip_file") and os.path.exists(seg["clip_file"]):
                clip = AudioSegment.from_file(seg["clip_file"])
                pos_ms = int(seg["start"] * 1000)
                master_track = master_track.overlay(clip, position=pos_ms)

        synced_audio_path = "synced_master.wav"
        master_track.export(synced_audio_path, format="wav")

        print(f"⚡ Applying uniform +12% ({SPEED_FACTOR}x) acceleration...")
        cmd = [
            'ffmpeg', '-y', '-err_detect', 'ignore_err',
            '-i', video_path,
            '-i', synced_audio_path,
            '-filter_complex', f'[0:v]setpts=PTS/{SPEED_FACTOR},fps=30[v];[1:a]atempo={SPEED_FACTOR}[a]',
            '-map', '[v]', '-map', '[a]',
            '-map_metadata', '-1', '-map_chapters', '-1',
            '-fflags', '+bitexact', '-flags:v', '+bitexact', '-flags:a', '+bitexact',
            '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '2600k',
            '-c:a', 'aac', '-b:a', '192k',
            '-shortest', output_file
        ]
        subprocess.run(cmd, check=True)
        if os.path.exists(synced_audio_path):
            os.remove(synced_audio_path)
        return

    # -------------------------------------------------------------
    # Case B: One or more segments require last-frame freeze
    # -------------------------------------------------------------
    print("❄️ Freezing frame for extended dialogues while locking subsequent start times...")
    os.makedirs("chunks", exist_ok=True)
    concat_list = []
    current_src_pos = 0.0
    accumulated_freeze = 0.0

    for i, seg in enumerate(segments):
        s_start = max(current_src_pos, seg["start"])
        s_end = min(src_total_dur, seg["start"] + seg["available_window"])
        freeze_dur = seg.get("freeze_dur", 0.0)

        # Gap between previous and current dialogue
        if s_start > current_src_pos + 0.05:
            gap_dur = s_start - current_src_pos
            gap_file = f"chunks/gap_{i}.mp4"
            cmd = [
                'ffmpeg', '-y', '-err_detect', 'ignore_err',
                '-ss', f"{current_src_pos:.3f}", '-i', video_path,
                '-t', f"{gap_dur:.3f}",
                '-f', 'lavfi', '-t', f"{gap_dur:.3f}", '-i', 'anullsrc=r=44100:cl=stereo',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '128k',
                '-shortest', gap_file
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            concat_list.append(gap_file)

        slot_dur = max(0.2, s_end - s_start)
        seg["dubbed_start"] = s_start + accumulated_freeze
        seg["dubbed_end"] = s_end + accumulated_freeze + freeze_dur
        accumulated_freeze += freeze_dur

        dial_file = f"chunks/dial_{i}.mp4"
        audio_src = seg.get("clip_file")

        if freeze_dur < 0.15:
            # Normal segment within slot
            audio_input = ['-i', audio_src] if audio_src and os.path.exists(audio_src) else ['-f', 'lavfi', '-t', f"{slot_dur:.3f}", '-i', 'anullsrc=r=44100:cl=stereo']
            cmd = [
                'ffmpeg', '-y', '-err_detect', 'ignore_err',
                '-ss', f"{s_start:.3f}", '-i', video_path,
                '-t', f"{slot_dur:.3f}"
            ] + audio_input + [
                '-map', '0:v:0', '-map', '1:a:0',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '192k',
                '-t', f"{slot_dur:.3f}",
                dial_file
            ]
        else:
            # Freeze the last frame using tpad so audio plays out fully
            total_dial_dur = slot_dur + freeze_dur
            cmd = [
                'ffmpeg', '-y', '-err_detect', 'ignore_err',
                '-ss', f"{s_start:.3f}", '-i', video_path,
                '-t', f"{slot_dur:.3f}",
                '-i', audio_src,
                '-filter_complex', f'[0:v]tpad=stop_mode=clone:stop_duration={freeze_dur:.3f}[v]',
                '-map', '[v]', '-map', '1:a:0',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '192k',
                '-t', f"{total_dial_dur:.3f}",
                dial_file
            ]

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        concat_list.append(dial_file)
        current_src_pos = s_end

    # Tail slice if video continues after final dialogue
    if current_src_pos < src_total_dur:
        tail_dur = src_total_dur - current_src_pos
        tail_file = "chunks/tail.mp4"
        cmd = [
            'ffmpeg', '-y', '-err_detect', 'ignore_err',
            '-ss', f"{current_src_pos:.3f}", '-i', video_path,
            '-t', f"{tail_dur:.3f}",
            '-f', 'lavfi', '-t', f"{tail_dur:.3f}", '-i', 'anullsrc=r=44100:cl=stereo',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',
            '-shortest', tail_file
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        concat_list.append(tail_file)

    concat_txt = "chunks/manifest.txt"
    with open(concat_txt, "w", encoding="utf-8") as f:
        for c in concat_list:
            f.write(f"file '{os.path.abspath(c)}'\n")

    print(f"⚡ Merging slices with uniform +12% ({SPEED_FACTOR}x) boost...")
    subprocess.run([
        'ffmpeg', '-y', '-err_detect', 'ignore_err',
        '-f', 'concat', '-safe', '0', '-i', concat_txt,
        '-filter_complex', f'[0:v]setpts=PTS/{SPEED_FACTOR},fps=30[v];[0:a]atempo={SPEED_FACTOR}[a]',
        '-map', '[v]', '-map', '[a]',
        '-map_metadata', '-1', '-map_chapters', '-1',
        '-fflags', '+bitexact', '-flags:v', '+bitexact', '-flags:a', '+bitexact',
        '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '2600k',
        '-c:a', 'aac', '-b:a', '192k',
        output_file
    ], check=True)

    try:
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
        idx = 1
        for seg in segments:
            txt = seg.get("translated_text", "").strip()
            if not txt:
                continue

            raw_start = seg.get("dubbed_start", seg["start"])
            raw_end = seg.get("dubbed_end", seg["start"] + seg.get("clip_dur", seg["available_window"]))

            # Scale timestamps by 1.12 to match the accelerated video
            scaled_start = raw_start / SPEED_FACTOR
            scaled_end = raw_end / SPEED_FACTOR

            start_str = format_timestamp(scaled_start)
            end_str = format_timestamp(scaled_end)
            f.write(f"{idx}\n{start_str} --> {end_str}\n{txt}\n\n")
            idx += 1

# ==========================================
# 🚀 Entrypoint
# ==========================================
if __name__ == "__main__":
    v_url = sys.argv[1]
    tgt_lang = sys.argv[2] if len(sys.argv) > 2 else "hi"

    v_file, a_file, meta = download_media(v_url)
    segs = transcribe_and_translate(a_file, target_lang=tgt_lang)
    asyncio.run(synthesize_audio(segs))

    render_dubbed_video(v_file, segs, "final_output.mp4")
    generate_srt(segs, "subtitles.srt")
    print("✅ Dubbing completed with adaptive 1.15x/1.25x speed-fitting and freeze frame.")
