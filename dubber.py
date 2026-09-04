import os, sys, re, json, time, math, shutil, asyncio, subprocess, urllib.request, urllib.error
import yt_dlp, edge_tts
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator
from pydub import AudioSegment

FINAL_SPEED = 1.10
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

VOICE_MAP = {
    "hi": "hi-IN-MadhurNeural", "bn": "bn-IN-BashkarNeural", "ta": "ta-IN-ValluvarNeural",
    "te": "te-IN-MohanNeural", "mr": "mr-IN-ManoharNeural", "gu": "gu-IN-DhwaniNeural",
    "kn": "kn-IN-GaganNeural", "ml": "ml-IN-MidhunNeural", "pa": "pa-IN-OjasNeural",
    "en": "en-US-GuyNeural", "fr": "fr-FR-HenriNeural", "de": "de-DE-ConradNeural",
    "es": "es-ES-AlvaroNeural", "it": "it-IT-DiegoNeural", "pt": "pt-BR-AntonioNeural",
    "nl": "nl-NL-MaartenNeural", "pl": "pl-PL-MarekNeural", "tr": "tr-TR-AhmetNeural",
    "ru": "ru-RU-DmitryNeural", "uk": "uk-UA-OstapNeural", "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural", "zh": "zh-CN-YunxiNeural", "ar": "ar-SA-HamedNeural"
}

def run(cmd, quiet=False):
    print("▶ " + " ".join(f'"{x}"' if " " in str(x) else str(x) for x in cmd))
    subprocess.run(cmd, stdout=subprocess.DEVNULL if quiet else None, stderr=subprocess.DEVNULL if quiet else None, check=True)

def get_duration(path):
    res = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path], stderr=subprocess.STDOUT)
    return float(res.strip())

def size_mb(path):
    return os.path.getsize(path) / 1048576.0 if os.path.exists(path) else 0.0

def clean_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()

def contains_hindi(text):
    return bool(text and re.search(r"[\u0900-\u097F]", text))

def invalid_translation(text):
    if not text: return True
    v = text.lower()
    bads = ("<html", "<!doctype", "captcha", "access denied", "server error", "unusual traffic", "invalid language", "languages are supported", "is an invalid language", "traceback", "exception")
    return any(b in v for b in bads)

def download_media(url):
    video_path, audio_path = "raw_source.mp4", "input_audio.wav"
    for p in (video_path, audio_path):
        if os.path.exists(p):
            try: os.remove(p)
            except Exception: pass
    print("📥 Downloading media...")
    opts = {"format": "best[ext=mp4]/best", "outtmpl": video_path, "merge_output_format": "mp4", "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    meta = {"title": info.get("title", "Dubbed Video"), "description": info.get("description", ""), "tags": info.get("tags", [])}
    with open("source_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("TITLE_EMIT: " + meta["title"])
    print("🎵 Extracting source audio...")
    run(["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio_path], quiet=True)
    return video_path, audio_path, meta

def naturalize_with_gemini(text, target_lang="hi"):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: return None
    lang_names = {"hi": "natural spoken Hindi (Devanagari script only)", "pa": "natural spoken Punjabi (Gurmukhi script only)", "bn": "natural spoken Bengali", "ta": "natural spoken Tamil", "te": "natural spoken Telugu", "mr": "natural spoken Marathi", "en": "conversational English"}
    target_desc = lang_names.get(target_lang, target_lang)
    prompt = f"Translate and adapt into {target_desc} for video dubbing.\nRules:\n- Simple everyday spoken phrasing.\n- Retain brand names, technical terms, and numbers as commonly spoken.\n- Return ONLY the translated sentence. No English notes or quotes.\n\nSentence: {text}"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2, "maxOutputTokens": 350}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.loads(res.read().decode("utf-8"))
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        if parts:
            result = parts[0].get("text", "").strip()
            result = re.sub(r"^```(?:text|hindi)?\s*", "", result, flags=re.I)
            result = re.sub(r"\s*```$", "", result).strip()
            result = clean_text(re.sub(r"^[\"']|[\"']$", "", result))
            if target_lang == "hi" and not contains_hindi(result): return None
            return result
    except Exception as e:
        print(f"⚠️ Gemini translation error: {e}")
    return None

def translate_text(text, target, source_lang="en"):
    text = clean_text(text)
    if len(text) < 2: return text
    gem_res = naturalize_with_gemini(text, target)
    if gem_res and not invalid_translation(gem_res): return gem_res

    src, tgt = (source_lang or "en").lower(), (target or "hi").lower()
    for _ in range(3):
        try:
            res = GoogleTranslator(source=src, target=tgt).translate(text)
            if res and not invalid_translation(res):
                cl = clean_text(res)
                if tgt != "hi" or contains_hindi(cl): return cl
        except Exception:
            time.sleep(0.4)

    mm_codes = {"hi": "hindi", "en": "english", "bn": "bengali", "ta": "tamil", "te": "telugu", "mr": "marathi", "gu": "gujarati", "kn": "kannada", "ml": "malayalam", "pa": "punjabi", "fr": "french", "de": "german", "es": "spanish", "it": "italian", "pt": "portuguese", "nl": "dutch", "pl": "polish", "tr": "turkish", "ru": "russian", "uk": "ukrainian", "ja": "japanese", "ko": "korean", "zh": "chinese", "ar": "arabic"}
    try:
        res = MyMemoryTranslator(source=mm_codes.get(src, "english"), target=mm_codes.get(tgt, "hindi")).translate(text)
        if res and not invalid_translation(res):
            cl = clean_text(res)
            if tgt != "hi" or contains_hindi(cl): return cl
    except Exception: pass
    print("⚠️ Translation fallback: using original text.")
    return text

def transcribe(audio_path, target_lang):
    print("🧠 Loading Whisper...")
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    print("🎙️ Transcribing...")
    result, info = model.transcribe(audio_path, language=None, vad_filter=True, vad_parameters={"min_silence_duration_ms": 300})
    detected_lang = info.language or "en"
    print(f"🌍 Detected language: {detected_lang}")

    raw = []
    for item in result:
        txt = clean_text(item.text)
        st, ed = float(item.start), float(item.end)
        if not txt or ed <= st or (ed - st < 0.20): continue
        raw.append({"start": st, "end": ed, "text": txt})
    if not raw: return []

    blocks, cur_txt, cur_st, cur_ed = [], [], raw[0]["start"], raw[0]["end"]
    for i, item in enumerate(raw):
        cur_txt.append(item["text"])
        cur_ed = item["end"]
        punct = item["text"].rstrip().endswith((".", "!", "?", "।", "…"))
        gap = (raw[i + 1]["start"] - item["end"]) if (i + 1 < len(raw)) else 0
        if punct or gap >= 0.70 or (cur_ed - cur_st >= 8.0) or (i == len(raw) - 1):
            t = clean_text(" ".join(cur_txt))
            if t: blocks.append({"start": cur_st, "end": cur_ed, "text": t})
            cur_txt = []
            if i + 1 < len(raw): cur_st = raw[i + 1]["start"]

    print(f"📝 Dialogue blocks: {len(blocks)}")
    segments = []
    for i, b in enumerate(blocks):
        slot = max(0.30, (blocks[i + 1]["start"] - b["start"]) if (i + 1 < len(blocks)) else (b["end"] - b["start"]))
        print(f"Translating block {i + 1}/{len(blocks)}...")
        translated = translate_text(b["text"], target_lang, source_lang=detected_lang)
        segments.append({
            "index": i, "source_start": b["start"], "source_end": b["end"], "slot": slot,
            "source_text": b["text"], "translated_text": translated, "target_lang": target_lang
        })
    return segments

async def make_tts(text, voice, output):
    await edge_tts.Communicate(text, voice, rate=TTS_RATE).save(output)

def tts_with_retry(text, voice, output):
    for attempt in range(1, TTS_RETRIES + 1):
        try:
            if os.path.exists(output): os.remove(output)
            asyncio.run(make_tts(text, voice, output))
            if os.path.exists(output) and os.path.getsize(output) > 1000 and get_duration(output) > 0.05:
                return True
        except Exception as e:
            print(f"⚠️ TTS {attempt}/{TTS_RETRIES}: {e}")
        time.sleep(0.5)
    return False

def clean_audio(source, output):
    audio = AudioSegment.from_file(source)
    if len(audio) > 20:
        try: audio = audio.strip_silence(silence_len=60, silence_thresh=-48, padding=25)
        except Exception: pass
    audio.set_frame_rate(44100).set_channels(2).export(output, format="wav")

def speed_audio(source, output, factor):
    f = max(1.0, min(float(factor), 2.0))
    run(["ffmpeg", "-y", "-i", source, "-filter:a", f"atempo={f:.6f}", "-ar", "44100", "-ac", "2", output], quiet=True)

def prepare_tts(segments):
    if os.path.exists(TEMP_DIR): shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)

    for i, seg in enumerate(segments):
        txt = clean_text(seg["translated_text"])
        if not txt or invalid_translation(txt):
            txt = clean_text(seg["source_text"])
            seg["translated_text"] = txt

        voice = VOICE_MAP.get(seg["target_lang"].lower().split("-")[0], "en-US-GuyNeural")
        raw, cln, fit = os.path.join(TEMP_DIR, f"{i:04d}_raw.mp3"), os.path.join(TEMP_DIR, f"{i:04d}_clean.wav"), os.path.join(TEMP_DIR, f"{i:04d}_fit.wav")
        print(f"🔊 TTS {i + 1}/{len(segments)}")

        success = tts_with_retry(txt, voice, raw)
        if not success and clean_text(seg["source_text"]):
            print("   ↳ TTS fallback to source text")
            success = tts_with_retry(clean_text(seg["source_text"]), voice, raw)

        if not success:
            print(f"❌ TTS failed: block {i + 1}")
            seg["clip_file"], seg["tts_duration"], seg["tts_failed"] = None, 0.0, True
            continue

        try:
            clean_audio(raw, cln)
            orig_dur = get_duration(cln)
            slot = max(0.30, float(seg["slot"]))
            factor = max(1.0, min(orig_dur / slot, MAX_SENTENCE_SPEED))

            if factor > 1.001:
                speed_audio(cln, fit, factor)
                final_file, final_dur = fit, get_duration(fit)
            else:
                final_file, final_dur = cln, orig_dur

            if final_dur <= 0.05: raise RuntimeError("TTS clip is empty.")
            seg["clip_file"], seg["tts_duration"], seg["audio_speed"], seg["tts_failed"] = final_file, final_dur, factor, False
            print(f"   {orig_dur:.2f}s → {final_dur:.2f}s | slot {slot:.2f}s | speed {factor:.3f}x")
        except Exception as e:
            print(f"❌ Audio processing failed for block {i + 1}: {e}")
            seg["clip_file"], seg["tts_duration"], seg["tts_failed"] = None, 0.0, True

def calculate_timeline(segments, source_duration):
    cur_time, acc_delay = 0.0, 0.0
    for seg in segments:
        st, slot = float(seg["source_start"]), float(seg["slot"])
        dur = float(seg.get("tts_duration", 0.0))
        tgt_st = max(st + acc_delay, cur_time)
        acc_delay = max(acc_delay, tgt_st - st)
        seg["audio_start"], seg["audio_end"] = tgt_st, tgt_st + dur
        cur_time = seg["audio_end"] + 0.05

        extra = dur - slot
        if extra > 0.05 and FREEZE_ENABLED:
            seg["freeze_duration"] = extra
            acc_delay += extra
        else:
            seg["freeze_duration"] = 0.0
    return max(source_duration + acc_delay, cur_time)

def build_master_audio(segments, total_duration, output):
    print("🎧 Building master audio...")
    master = AudioSegment.silent(duration=int(math.ceil((total_duration + 1.0) * 1000)), frame_rate=44100).set_channels(2)
    for i, seg in enumerate(segments):
        cp = seg.get("clip_file")
        if not cp or not os.path.exists(cp): continue
        try:
            clip = AudioSegment.from_file(cp).set_frame_rate(44100).set_channels(2)
            master = master.overlay(clip, position=max(0, int(round(seg["audio_start"] * 1000))))
        except Exception as e:
            print(f"⚠️ Audio overlay error block {i + 1}: {e}")
    master.export(output, format="wav")
    print("✅ Master audio created")

def render_video(source, segments, source_duration, output):
    print("🎬 Rendering synchronized video...")
    filters, labels, l_num, prev_end = [], [], 0, 0.0

    def new_lbl():
        nonlocal l_num
        lbl = f"v{l_num}"
        l_num += 1
        return lbl

    for seg in segments:
        st, ed, frz = float(seg["source_start"]), float(seg["source_end"]), float(seg.get("freeze_duration", 0.0))
        if st > prev_end + 0.01:
            lbl = new_lbl()
            filters.append(f"[0:v]trim=start={prev_end:.6f}:end={st:.6f},setpts=PTS-STARTPTS[{lbl}]")
            labels.append(f"[{lbl}]")

        lbl = new_lbl()
        chunk = f"[0:v]trim=start={st:.6f}:end={ed:.6f},setpts=PTS-STARTPTS"
        if FREEZE_ENABLED and frz > 0.02:
            chunk += f",tpad=stop_mode=clone:stop_duration={frz:.6f}"
        filters.append(chunk + f"[{lbl}]")
        labels.append(f"[{lbl}]")
        prev_end = ed

    if prev_end < source_duration - 0.01:
        lbl = new_lbl()
        filters.append(f"[0:v]trim=start={prev_end:.6f}:end={source_duration:.6f},setpts=PTS-STARTPTS[{lbl}]")
        labels.append(f"[{lbl}]")

    if not labels:
        lbl = new_lbl()
        filters.append(f"[0:v]setpts=PTS-STARTPTS[{lbl}]")
        labels.append(f"[{lbl}]")

    if len(labels) == 1:
        filters.append(f"{labels[0]}setpts=PTS-STARTPTS[joined]")
    else:
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0,setpts=PTS-STARTPTS[joined]")

    filters.append(f"[joined]setpts=PTS/{FINAL_SPEED:.6f}[vout]")
    run([
        "ffmpeg", "-y", "-i", source, "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-an", "-c:v", "libx264", "-preset", VIDEO_PRESET,
        "-crf", VIDEO_CRF, "-pix_fmt", "yuv420p", "-movflags", "+faststart", output
    ], quiet=False)

    if not os.path.exists(output): raise RuntimeError("Video rendering failed.")

def mux_final(video, audio, output):
    print("🎧 Muxing final MP4...")
    tmp = output + ".tmp.mp4"
    if os.path.exists(tmp): os.remove(tmp)
    run([
        "ffmpeg", "-y", "-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0",
        "-af", f"atempo={FINAL_SPEED:.6f}", "-c:v", "copy", "-c:a", "aac",
        "-b:a", AUDIO_BITRATE, "-movflags", "+faststart", "-shortest", tmp
    ], quiet=False)
    if not os.path.exists(tmp): raise RuntimeError("Final MP4 was not generated.")
    if os.path.exists(output): os.remove(output)
    os.replace(tmp, output)

def timestamp(sec):
    ms = max(0, int(round(sec * 1000)))
    h, ms = ms // 3600000, ms % 3600000
    m, ms = ms // 60000, ms % 60000
    return f"{h:02d}:{m:02d}:{ms // 1000:02d},{ms % 1000:03d}"

def write_srt(segments, output):
    print("📝 Writing SRT...")
    cnt = 0
    with open(output, "w", encoding="utf-8") as f:
        for seg in segments:
            txt = clean_text(seg.get("translated_text", ""))
            if not txt: continue
            cnt += 1
            st, ed = float(seg["audio_start"]) / FINAL_SPEED, float(seg["audio_end"]) / FINAL_SPEED
            f.write(f"{cnt}\n{timestamp(st)} --> {timestamp(ed)}\n{txt}\n\n")
    print(f"✅ SRT: {cnt} subtitles")

def cleanup():
    for p in (TEMP_DIR, "raw_source.mp4", "input_audio.wav", "synced_master.wav", "extended_video.mp4"):
        try:
            if os.path.isdir(p): shutil.rmtree(p)
            elif os.path.exists(p): os.remove(p)
        except Exception: pass

def main():
    if len(sys.argv) < 2:
        print('Usage: python dubber.py "VIDEO_URL" [language]')
        sys.exit(1)

    url = sys.argv[1]
    tgt_lang = sys.argv[2].lower() if len(sys.argv) > 2 else "hi"
    started = time.time()

    print("\n" + "=" * 65 + "\n                 AI VIDEO DUBBER\n" + "=" * 65)
    print(f"Target: {tgt_lang} | Final speed: {FINAL_SPEED} | Freeze: {FREEZE_ENABLED}\n" + "=" * 65 + "\n")

    try:
        src_v, src_a, meta = download_media(url)
        src_dur = get_duration(src_v)
        print(f"Source duration: {src_dur:.2f}s | Size: {size_mb(src_v):.2f} MB")

        segments = transcribe(src_a, tgt_lang)
        if not segments: raise RuntimeError("No speech detected.")

        prepare_tts(segments)
        out_dur = calculate_timeline(segments, src_dur)
        print(f"Timeline duration: {out_dur:.2f}s")

        build_master_audio(segments, out_dur, "synced_master.wav")
        write_srt(segments, OUTPUT_SRT)
        render_video(src_v, segments, src_dur, "extended_video.mp4")
        mux_final("extended_video.mp4", "synced_master.wav", OUTPUT_VIDEO)

        if not os.path.exists(OUTPUT_VIDEO): raise RuntimeError("final_output.mp4 not created.")

        fails = sum(1 for s in segments if s.get("tts_failed", False))
        print("\n" + "=" * 65 + "\n                    COMPLETE\n" + "=" * 65)
        print(f"Final size: {size_mb(OUTPUT_VIDEO):.2f} MB | Final duration: {get_duration(OUTPUT_VIDEO):.2f}s")
        print(f"TTS failures: {fails} | Time taken: {(time.time() - started) / 60:.2f} min")
        print("🎬 final_output.mp4 | 📝 subtitles.srt\n" + "=" * 65)

        cleanup()

    except Exception as e:
        print("\n" + "=" * 65 + "\n                    FAILED\n" + "=" * 65)
        print(f"❌ {e}\nTemporary files retained.\n" + "=" * 65)
        raise

if __name__ == "__main__":
    main()
    
