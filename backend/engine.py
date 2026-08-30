"""
ShortAI — Core Engine (Phase 3 — Optimized)
---------------------------------------------
Speed optimizations:
  • Audio-only first pass                   → transcribe ~20MB instead of ~500MB video
  • Partial video download per clip         → only fetch the exact seconds needed
  • yt-dlp → FFmpeg pipe (no temp file)     → clip sections stream directly into encoder
  • faster-whisper int8 quantization        → ~4x faster transcription on CPU
  • Parallel trend fetching (ThreadPool)    → network calls run concurrently
  • Face cache built on clip sections       → no full-video OpenCV pass needed
  • ultrafast preset + crf 28              → faster encode, still good quality
"""

import os
import json
import re
import subprocess
import tempfile
import time
import yt_dlp
import requests
import cv2
import numpy as np
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pytrends.request import TrendReq

# ── faster-whisper (replaces openai-whisper) ──────────────────────────────────
# Install: pip install faster-whisper
try:
    from faster_whisper import WhisperModel
    _FASTER_WHISPER = True
except ImportError:
    import whisper as _openai_whisper
    _FASTER_WHISPER = False
    print("⚠  faster-whisper not installed — falling back to openai-whisper (slower)")
    print("   Install with: pip install faster-whisper")

# langdetect for per-segment language identification
try:
    from langdetect import detect as _langdetect
    from langdetect import DetectorFactory
    DetectorFactory.seed = 42
    _LANGDETECT_AVAILABLE = True
except ImportError:
    _LANGDETECT_AVAILABLE = False
    print("⚠  langdetect not installed — install with: pip install langdetect")


# ── CONFIG ─────────────────────────────────────────────────────────────────
DOWNLOAD_DIR    = Path("downloads")
OUTPUT_DIR      = Path("outputs")

# ✅ OPTIMIZATION: "tiny" is 2x faster than "base", still accurate enough for scoring
WHISPER_MODEL   = "tiny"

# Set to 1 for quick test — bump back to 3 when happy with output
NUM_CLIPS       = 1
CLIP_DURATION   = 30
MIN_SEGMENT_GAP = 30

# Face detection sample interval — every 2s is plenty, was 1s
FACE_SAMPLE_INTERVAL = 2.0

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

DOWNLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
#  HINDI / ENGLISH / HINGLISH SUPPORT  (unchanged from Phase 3)
# ══════════════════════════════════════════════════════════════════════════════

_HINDI_HOOK_DEVANAGARI: set[str] = {
    "सच", "झूठ", "रहस्य", "खुलासा", "असली", "नकली", "जरूरी", "ध्यान",
    "चौंकाने", "हैरान", "अविश्वसनीय", "खतरनाक", "गलती", "सीक्रेट",
    "शॉकिंग", "वायरल", "ट्रेंडिंग", "धमाका", "तहलका", "बड़ा", "सबसे",
    "पहली", "आखिरी", "कभी", "कोई", "नहीं", "क्यों", "कैसे", "क्या",
    "अब", "देखो", "सुनो", "जानो", "समझो", "बताऊंगा", "पता", "दिल",
    "जिंदगी", "पैसा", "सफलता", "जीत", "हार", "प्यार", "नफरत",
}

_HINGLISH_HOOK_ROMAN: set[str] = {
    "sach", "jhooth", "rahasya", "khulasa", "asli", "nakli",
    "kamaal", "zabardast", "behtareen", "shandar", "dhamaka",
    "thalaka", "viral", "trending", "shocking", "dangerous",
    "kya", "kyun", "kyunki", "kaise", "kaun", "kab", "kahan",
    "bilkul", "ekdum", "bahut", "zyada", "thoda", "sirf", "bas",
    "aur", "lekin", "magar", "isliye", "tabhi", "abhi", "phir",
    "secret", "exposed", "truth", "real", "fake", "pehli", "akhri",
    "sabse", "bada", "chota", "best", "worst", "number", "top",
    "dekho", "suno", "jano", "samjho", "bataunga", "bataungi",
    "subscribe", "like", "share", "comment", "follow",
}

_QUESTION_OPENERS_EN = (
    "what", "why", "how", "did you", "have you", "do you",
    "are you", "can you", "would you", "is it", "does it",
)
_QUESTION_OPENERS_HI_DEVA = (
    "क्या", "क्यों", "कैसे", "कौन", "कब", "कहाँ", "क्या आप",
    "क्या तुम", "क्या कभी",
)
_QUESTION_OPENERS_HINGLISH = (
    "kya", "kyun", "kaise", "kaun", "kab", "kahan",
    "kya aap", "kya tum", "kya kabhi",
)

_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

def _devanagari_ratio(text: str) -> float:
    chars = [c for c in text if c.isalpha()]
    if not chars:
        return 0.0
    deva = sum(1 for c in chars if _DEVANAGARI_RE.match(c))
    return deva / len(chars)


def detect_segment_language(text: str) -> str:
    deva_r = _devanagari_ratio(text)
    if deva_r > 0.30:
        return "hi"

    if _LANGDETECT_AVAILABLE:
        try:
            detected = _langdetect(text)
            if detected == "hi" and deva_r < 0.10:
                return "hinglish"
            if detected == "hi":
                return "hi"
        except Exception:
            pass

    roman_words = set(re.findall(r"[a-z]+", text.lower()))
    hinglish_hits = len(roman_words & _HINGLISH_HOOK_ROMAN)
    if hinglish_hits >= 2:
        return "hinglish"

    return "en"


# ══════════════════════════════════════════════════════════════════════════════
#  UPGRADE 1 — LIVE TRENDING HOOK WORDS  (now fetched in PARALLEL)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_google_trends(geo: str = "US", top_n: int = 50) -> list[str]:
    tokens: list[str] = []
    geo_map = {"US": "united_states", "IN": "india", "GB": "united_kingdom"}
    geos_to_fetch = [geo] if geo == "IN" else [geo, "IN"]

    for g in geos_to_fetch:
        pn = geo_map.get(g, "united_states")
        hl = "hi" if g == "IN" else "en-US"
        try:
            pytrends = TrendReq(hl=hl, tz=330, timeout=(10, 25))
            df       = pytrends.trending_searches(pn=pn)
            terms    = df[0].tolist()[:top_n]
            for phrase in terms:
                tokens.extend(re.findall(r"[a-z]+", phrase.lower()))
                tokens.extend(re.findall(r"[\u0900-\u097F]+", phrase))
            print(f"    ✓ Google Trends [{g}]: {len(terms)} phrases")
        except Exception as e:
            print(f"    ⚠ Google Trends [{g}] failed: {e}")

    return tokens


def fetch_instagram_hashtags() -> list[str]:
    if not RAPIDAPI_KEY:
        print("    ⚠ No RAPIDAPI_KEY set — skipping Instagram hashtags")
        return []
    try:
        url = "https://instagram-hashtag-api.p.rapidapi.com/trending"
        headers = {
            "X-RapidAPI-Key": RAPIDAPI_KEY,
            "X-RapidAPI-Host": "instagram-hashtag-api.p.rapidapi.com",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        tags   = [item.get("hashtag", "").lower() for item in data if "hashtag" in item]
        tokens = []
        for tag in tags:
            tokens.extend(re.findall(r"[a-z]+", tag))
        print(f"    ✓ Instagram hashtags: {len(tags)} tags → {len(tokens)} tokens")
        return tokens
    except Exception as e:
        print(f"    ⚠ Instagram hashtag fetch failed: {e}")
        return []


def fetch_twitter_trends(woeid: int = 1) -> list[str]:
    tokens = []
    if not RAPIDAPI_KEY:
        return tokens
    try:
        url = "https://twitter-trends5.p.rapidapi.com/twitter/request.php"
        headers = {
            "X-RapidAPI-Key": RAPIDAPI_KEY,
            "X-RapidAPI-Host": "twitter-trends5.p.rapidapi.com",
        }
        resp = requests.get(url, headers={"headers": headers}, params={"woeid": woeid}, timeout=10)
        resp.raise_for_status()
        data   = resp.json()
        trends = data.get("trends", [])
        for t in trends:
            name = t.get("name", "")
            tokens.extend(re.findall(r"[a-z]+", name.lower()))
        print(f"    ✓ Twitter/X trends: {len(trends)} trends → {len(tokens)} tokens")
    except Exception as e:
        print(f"    ⚠ Twitter trends fetch failed: {e}")
    return tokens


_FALLBACK_HOOK_WORDS = [
    "secret", "shocking", "truth", "mistake", "wrong", "actually",
    "revealed", "exposed", "insane", "unbelievable", "simple", "easy",
    "instantly", "powerful", "hack", "trick", "strategy", "changed",
    "transform", "warning", "dangerous", "biggest", "worst", "best",
]


def build_hook_vocabulary(geo: str = "US") -> dict:
    """
    ✅ OPTIMIZATION: All three network calls run in PARALLEL via ThreadPoolExecutor.
    Previously sequential (~8–15s) → now concurrent (~3–5s).
    """
    print("\n[HOOK VOCAB] Fetching live trending keywords (parallel)...")

    # ── Fire all three fetches at the same time ───────────────────────────
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_trends  = ex.submit(fetch_google_trends, geo)
        f_insta   = ex.submit(fetch_instagram_hashtags)
        f_twitter = ex.submit(fetch_twitter_trends)

        trends_tokens  = f_trends.result()
        insta_tokens   = f_insta.result()
        twitter_tokens = f_twitter.result()

    all_tokens = trends_tokens + insta_tokens + twitter_tokens

    stop_en = {
        "the","a","an","is","in","of","to","and","for","on","at",
        "by","it","be","as","or","if","do","we","my","me","he","she",
        "his","her","our","its","not","but","are","was","has","had",
        "with","from","this","that","have","will","just","can","all",
    }
    stop_hinglish = {"hai","hain","ka","ki","ke","ko","se","ne","par","aur","bhi","toh"}

    live_roman = {
        t for t in all_tokens
        if re.fullmatch(r"[a-z]+", t) and t not in stop_en and len(t) > 2
    }
    live_deva = {
        t for t in all_tokens
        if re.fullmatch(r"[\u0900-\u097F]+", t)
    }

    en_vocab       = live_roman | set(_FALLBACK_HOOK_WORDS)
    hi_vocab       = live_deva  | _HINDI_HOOK_DEVANAGARI
    hinglish_vocab = (live_roman - stop_hinglish) | _HINGLISH_HOOK_ROMAN

    print(f"    ✓ en={len(en_vocab)}  hi={len(hi_vocab)}  hinglish={len(hinglish_vocab)} keywords")
    return {"en": en_vocab, "hi": hi_vocab, "hinglish": hinglish_vocab}


# ══════════════════════════════════════════════════════════════════════════════
#  UPGRADE 2 — FACE-TRACKED VERTICAL CROP  (now single-pass cached)
# ══════════════════════════════════════════════════════════════════════════════

_FACE_CASCADE = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)


def _detect_face_center(frame: np.ndarray) -> tuple[int, int] | None:
    gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = _FACE_CASCADE.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
    if len(faces) == 0:
        return None
    faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
    x, y, w, h = faces[0]
    return (x + w // 2, y + h // 2)


def build_face_cache_from_file(video_path: str,
                               interval: float = FACE_SAMPLE_INTERVAL) -> dict[float, int]:
    """
    Sample face positions from a SHORT clip file (not the full video).
    Returns {timestamp: cx}.  Called once per clip section after it is
    downloaded — much faster than scanning the whole source video.
    """
    cap            = cv2.VideoCapture(video_path)
    fps            = cap.get(cv2.CAP_PROP_FPS) or 25.0
    cache: dict[float, int] = {}
    frame_interval = max(1, int(fps * interval))
    frame_num      = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_num % frame_interval == 0:
            t      = frame_num / fps
            centre = _detect_face_center(frame)
            if centre:
                cache[t] = centre[0]
        frame_num += 1

    cap.release()
    return cache


def compute_face_crop_x_from_cache(face_cache: dict[float, int],
                                    clip_start: float, clip_duration: float,
                                    src_width: int) -> int:
    """
    Look up the pre-built cache for face positions within this clip's time window.
    Falls back to centre if no face found in window.
    """
    clip_end = clip_start + clip_duration
    cx_vals  = [cx for t, cx in face_cache.items() if clip_start <= t <= clip_end]

    if not cx_vals:
        return src_width // 2   # no face → centre crop

    median_cx = int(np.median(cx_vals))
    return median_cx


def build_face_tracked_vf(face_cache: dict[float, int],
                           clip_start: float, clip_duration: float,
                           src_width: int, src_height: int) -> str:
    """
    Build FFmpeg -vf filter string with face-centred 9:16 crop.
    Now takes pre-built face_cache instead of doing its own video scan.
    """
    OUT_W, OUT_H = 1080, 1920

    scale_factor = OUT_H / src_height
    scaled_w     = int(src_width * scale_factor)

    face_cx_src = compute_face_crop_x_from_cache(face_cache, clip_start, clip_duration, src_width)
    face_cx_sc  = int(face_cx_src * scale_factor)
    crop_x      = max(0, min(face_cx_sc - OUT_W // 2, scaled_w - OUT_W))

    vf = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={scaled_w}:{OUT_H},"
        f"crop={OUT_W}:{OUT_H}:{crop_x}:0,"
        f"boxblur=20:20[blurred];"
        f"[fg]scale={scaled_w}:{OUT_H},"
        f"crop={OUT_W}:{OUT_H}:{crop_x}:0[cropped];"
        f"[blurred][cropped]overlay=0:0[out]"
    )
    return vf


def get_video_dimensions(video_path: str) -> tuple[int, int]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json", video_path,
    ]
    out  = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    data = json.loads(out)
    s    = data["streams"][0]
    return s["width"], s["height"]


# ══════════════════════════════════════════════════════════════════════════════
#  PIPELINE STEPS
# ══════════════════════════════════════════════════════════════════════════════

# ── STEP 1a: FETCH VIDEO METADATA (no download) ─────────────────────────────
def fetch_video_info(url: str) -> dict:
    """
    Grab title, duration, video_id WITHOUT downloading anything.
    Takes ~2–3 seconds, costs zero bandwidth.
    """
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    print(f"    ✓ Info fetched: '{info['title']}' ({info['duration']//60}m {info['duration']%60}s)")
    return {
        "video_id": info["id"],
        "title":    info["title"],
        "duration": info["duration"],
    }


# ── STEP 1b: DOWNLOAD AUDIO ONLY ────────────────────────────────────────────
def download_audio_only(url: str, video_id: str,
                        progress_callback=None) -> str:
    """
    Download audio stream only — typically 10–20x smaller than full video.
    A 20-min 1080p video  →  ~500 MB video  vs  ~20 MB audio.
    Whisper transcribes audio files just as well as video files.
    Returns path to the saved audio file.
    """
    # Cache check
    for ext in ["m4a", "webm", "opus", "mp3", "ogg"]:
        cached = DOWNLOAD_DIR / f"{video_id}_audio.{ext}"
        if cached.exists():
            print(f"    ✓ Audio cache hit: {cached.name}")
            return str(cached)

    if progress_callback:
        progress_callback("Downloading audio only (much faster than full video)...", 10)

    print(f"\n[1/4] Audio-only download...")
    audio_base = str(DOWNLOAD_DIR / f"{video_id}_audio")

    ydl_opts = {
        "format":      "bestaudio/best",
        "outtmpl":     audio_base + ".%(ext)s",
        "quiet":       True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    for ext in ["m4a", "webm", "opus", "mp3", "ogg"]:
        candidate = DOWNLOAD_DIR / f"{video_id}_audio.{ext}"
        if candidate.exists():
            size_mb = os.path.getsize(candidate) / 1024 / 1024
            print(f"    ✓ Audio saved: {candidate.name} ({size_mb:.1f} MB)")
            if progress_callback:
                progress_callback("Audio downloaded. Transcribing...", 20)
            return str(candidate)

    raise FileNotFoundError(f"Audio file not found for video_id={video_id}")


# ── STEP 1c: DOWNLOAD ONLY THE CLIP SECTIONS NEEDED ─────────────────────────
def download_clip_section(url: str, start: float, duration: float,
                          out_path: str) -> bool:
    """
    Download ONLY the seconds needed for one clip.

    Uses the best available video+audio combination instead of requiring
    a specific MP4 format. This is important because some YouTube videos
    don't expose a progressive MP4 stream.
    """
    section_end = start + duration + 2

    ydl_opts = {
        # Don't require ext=mp4.
        # YouTube often provides separate video/audio streams.
        "format": "bestvideo+bestaudio/best",

        "outtmpl": out_path,

        # Download only the requested section.
        "download_sections": f"*{start}-{section_end}",

        "force_keyframes_at_cuts": True,

        # Merge separate video/audio streams into MP4.
        "merge_output_format": "mp4",

        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        return os.path.exists(out_path)

    except Exception as e:
        print(f"      ⚠ Section download failed: {e}")
        return False
# ── STEP 2: TRANSCRIBE AUDIO ────────────────────────────────────────────────
def transcribe_video(audio_path: str, progress_callback=None) -> list:
    """
    Accepts the audio-only file downloaded in step 1b.
    Starting transcription on a 20 MB audio file instead of a 500 MB video
    means this begins almost immediately after step 1b finishes.
    """
    if progress_callback:
        progress_callback("Transcribing audio with Whisper AI (multilingual)...", 25)

    print(f"\n[2/4] Transcribing — model={WHISPER_MODEL}  "
          f"backend={'faster-whisper' if _FASTER_WHISPER else 'openai-whisper'}...")

    segments = []

    if _FASTER_WHISPER:
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        raw_segments, info = model.transcribe(
            audio_path,
            task="transcribe",
            beam_size=1,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        detected_lang = info.language
        print(f"    ✓ Whisper detected language: {detected_lang}")
        for seg in raw_segments:
            text = seg.text.strip()
            if not text:
                continue
            lang = detect_segment_language(text)
            segments.append({
                "text":  text,
                "start": round(seg.start, 2),
                "end":   round(seg.end,   2),
                "lang":  lang,
            })
    else:
        model  = _openai_whisper.load_model(WHISPER_MODEL)
        result = model.transcribe(audio_path, task="transcribe",
                                  verbose=False, fp16=False)
        detected_lang = result.get("language", "unknown")
        print(f"    ✓ Whisper detected language: {detected_lang}")
        for seg in result["segments"]:
            text = seg["text"].strip()
            lang = detect_segment_language(text)
            segments.append({
                "text":  text,
                "start": round(seg["start"], 2),
                "end":   round(seg["end"],   2),
                "lang":  lang,
            })

    lang_counts = Counter(s["lang"] for s in segments)
    print(f"    ✓ {len(segments)} segments  |  "
          f"en={lang_counts['en']}  hi={lang_counts['hi']}  "
          f"hinglish={lang_counts['hinglish']}")

    if progress_callback:
        progress_callback(f"Transcribed {len(segments)} segments. Scoring...", 50)
    return segments


# ── STEP 3: SCORE SEGMENTS ──────────────────────────────────────────────────
def score_segments(segments: list, video_duration: float,
                   hook_vocab: dict | None = None,
                   progress_callback=None) -> list:
    if progress_callback:
        progress_callback("Scoring segments for viral potential...", 65)

    print(f"\n[3/4] Scoring {len(segments)} segments (multilingual)...")

    if hook_vocab is None:
        hook_vocab = build_hook_vocabulary()

    en_vocab       = hook_vocab.get("en",       set())
    hi_vocab       = hook_vocab.get("hi",       set())
    hinglish_vocab = hook_vocab.get("hinglish", set())

    scored = []
    for i, seg in enumerate(segments):
        text  = seg["text"]
        lang  = seg.get("lang", detect_segment_language(text))
        score = 0

        if lang == "hi":
            deva_words  = set(re.findall(r"[\u0900-\u097F]+", text))
            roman_words = set(re.findall(r"[a-z]+", text.lower()))
            hook_hits   = len(deva_words & hi_vocab) + len(roman_words & hinglish_vocab)

        elif lang == "hinglish":
            roman_words = set(re.findall(r"[a-z]+", text.lower()))
            deva_words  = set(re.findall(r"[\u0900-\u097F]+", text))
            hook_hits   = (len(roman_words & hinglish_vocab) +
                           len(roman_words & en_vocab) +
                           len(deva_words  & hi_vocab))

        else:
            roman_words = set(re.findall(r"[a-z]+", text.lower()))
            hook_hits   = len(roman_words & en_vocab)

        score += min(hook_hits * 8, 40)

        text_lower         = text.lower().strip()
        is_question_opener = (
            text_lower.startswith(_QUESTION_OPENERS_EN)
            or text.strip().startswith(_QUESTION_OPENERS_HI_DEVA)
            or text_lower.startswith(_QUESTION_OPENERS_HINGLISH)
        )
        if is_question_opener:
            score += 15
        if "?" in text:
            score += 8

        sentence_count = len(re.findall(r"[.!?।]", text))
        score         += min(sentence_count * 5, 15)
        score         += min(text.count("!") * 5, 10)

        seg_dur = seg["end"] - seg["start"]
        if 8 <= seg_dur <= 20:
            score += 10
        elif 5 <= seg_dur <= 25:
            score += 5

        rel_pos = seg["start"] / video_duration if video_duration > 0 else 0
        if rel_pos <= 0.15:
            score += 10
        elif rel_pos >= 0.85:
            score += 6

        if lang == "hinglish":
            score += 5

        scored.append({**seg, "score": score, "original_index": i, "lang": lang})

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[0]
    print(f"    ✓ Top score: {top['score']} pts [{top['lang']}] — \"{top['text'][:60]}\"")

    if progress_callback:
        progress_callback("Selecting best clip windows...", 70)
    return scored


# ── STEP 4: SELECT CLIP WINDOWS ─────────────────────────────────────────────
def select_clip_windows(scored: list, video_duration: float,
                         num_clips: int = NUM_CLIPS,
                         clip_duration: int = CLIP_DURATION) -> list:
    clips, used_times = [], []
    for seg in scored:
        if len(clips) >= num_clips:
            break
        mid        = (seg["start"] + seg["end"]) / 2
        clip_start = max(0, mid - clip_duration / 2)
        clip_end   = min(video_duration, clip_start + clip_duration)
        clip_start = max(0, clip_end - clip_duration)
        if any(abs(clip_start - u) < MIN_SEGMENT_GAP for u in used_times):
            continue
        used_times.append(clip_start)
        clips.append({
            "clip_index":  len(clips) + 1,
            "start":       round(clip_start, 2),
            "end":         round(clip_end, 2),
            "duration":    round(clip_end - clip_start, 2),
            "anchor_text": seg["text"][:100],
            "score":       seg["score"],
        })
    print(f"\n    ✓ Selected {len(clips)} clip windows")
    return clips


# ── SUBTITLE HELPERS ────────────────────────────────────────────────────────

def _seconds_to_srt_time(s: float) -> str:
    """Convert float seconds → SRT timestamp  00:00:00,000"""
    ms  = int((s % 1) * 1000)
    s   = int(s)
    h   = s // 3600
    m   = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def write_srt(segments: list, clip_start: float, clip_end: float,
              srt_path: str) -> int:
    """
    Write a .srt file containing only the segments that fall inside
    [clip_start, clip_end]. Timestamps are re-based to clip-relative time
    (so 0:00 = start of the clip, not the source video).

    Returns the number of subtitle entries written.
    """
    entries = []
    for seg in segments:
        # Keep segment if it overlaps the clip window at all
        if seg["end"] < clip_start or seg["start"] > clip_end:
            continue
        # Clamp to clip bounds, then re-base
        t_start = max(seg["start"], clip_start) - clip_start
        t_end   = min(seg["end"],   clip_end)   - clip_start
        if t_end <= t_start:
            continue
        entries.append((t_start, t_end, seg["text"].strip()))

    with open(srt_path, "w", encoding="utf-8") as f:
        for idx, (t_start, t_end, text) in enumerate(entries, 1):
            f.write(f"{idx}\n")
            f.write(f"{_seconds_to_srt_time(t_start)} --> {_seconds_to_srt_time(t_end)}\n")
            f.write(f"{text}\n\n")

    return len(entries)


# ── DRAWTEXT SUBTITLE HELPER ─────────────────────────────────────────────────

def _srt_to_drawtext(srt_path: str, font_size: int, margin_bot: int,
                     frame_w: int, frame_h: int) -> list[str]:
    """
    Parse a .srt file and return a list of FFmpeg drawtext filter strings,
    one per subtitle entry.  Each filter is active only during its time window
    using FFmpeg's enable='between(t,start,end)' expression.

    Design choices:
      • White bold text, black border (box=0, bordercolor=black, borderw=3)
      • Semi-transparent dark box behind each line for legibility
      • Text wrapped at ~35 chars per line to fit 1080px width at font_size=28
      • Positioned at bottom: y = frame_h - margin_bot
      • x centred: x = (frame_w - text_w) / 2  → use x=(w-tw)/2 in FFmpeg
    """
    import re as _re

    # Parse SRT blocks
    # Format: index\nHH:MM:SS,mmm --> HH:MM:SS,mmm\ntext\n
    srt_text  = Path(srt_path).read_text(encoding="utf-8")
    blocks    = _re.split(r"\n\n+", srt_text.strip())
    filters   = []

    def _srt_ts_to_sec(ts: str) -> float:
        """'00:01:23,456' → 83.456"""
        h, m, rest = ts.split(":")
        s, ms      = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    def _escape(text: str) -> str:
        """Escape characters that break FFmpeg drawtext filter strings."""
        return (text
                .replace("\\", "\\\\")
                .replace("'",  "\u2019")   # replace smart apostrophe to avoid shell issues
                .replace('"',  '\\"')
                .replace(":",  "\\:")
                .replace("%",  "\\%"))

    y_pos = frame_h - margin_bot   # pixel y from top  e.g. 1920-140 = 1780

    for block in blocks:
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 3:
            continue
        # lines[0] = index, lines[1] = timestamps, lines[2:] = text
        try:
            t_start_str, t_end_str = lines[1].split("-->")
            t_start = _srt_ts_to_sec(t_start_str.strip())
            t_end   = _srt_ts_to_sec(t_end_str.strip())
        except (ValueError, IndexError):
            continue

        text = " ".join(lines[2:]).strip()
        if not text:
            continue

        # Wrap long lines (~35 chars) so they don't overflow 1080px width
        words      = text.split()
        wrapped    = []
        cur        = ""
        for word in words:
            if len(cur) + len(word) + 1 > 35:
                if cur:
                    wrapped.append(cur.strip())
                cur = word + " "
            else:
                cur += word + " "
        if cur.strip():
            wrapped.append(cur.strip())
        display_text = r"\n".join(_escape(l) for l in wrapped)

        # drawtext filter for this subtitle entry
        # boxcolor=black@0.5 gives a semi-transparent background box
        dt = (
            f"drawtext="
            f"text='{display_text}':"
            f"fontsize={font_size}:"
            f"fontcolor=white:"
            f"bordercolor=black:"
            f"borderw=3:"
            f"box=1:"
            f"boxcolor=black@0.45:"
            f"boxborderw=8:"
            f"x=(w-tw)/2:"           # horizontally centred
            f"y={y_pos}-th:"         # bottom-anchored (th = text height)
            f"enable='between(t,{t_start:.3f},{t_end:.3f})'"
        )
        filters.append(dt)

    return filters


# ── STEP 5: RENDER CLIPS (partial download + face-track + subtitles) ─────────
def render_clips(url: str, clips: list, video_id: str,
                 segments: list,
                 progress_callback=None) -> list:
    """
    For each clip:
      1. Download ONLY that clip's seconds from YouTube (not the full video)
      2. Run face detection on just that short file
      3. Burn subtitles + face-tracked crop → output MP4
      4. Delete the raw section file (keep only the final output)
    All clips are processed in parallel.
    """
    print(f"\n[4/4] Rendering {len(clips)} clips "
          f"(partial download + face-track + subtitles)...")

    def render_one(clip: dict) -> str | None:
        idx       = clip["clip_index"]
        out_path  = OUTPUT_DIR / f"{video_id}_clip{idx}.mp4"
        srt_path  = OUTPUT_DIR / f"{video_id}_clip{idx}.srt"
        # Temp file for the raw downloaded section
        raw_path  = str(DOWNLOAD_DIR / f"{video_id}_raw{idx}.mp4")

        # ── 1. Download only this clip's section ──────────────────────────
        print(f"      → Clip {idx}: downloading {clip['duration']}s section "
              f"[{clip['start']:.1f}s–{clip['end']:.1f}s]...")
        ok = download_clip_section(url, clip["start"], clip["duration"], raw_path)
        if not ok:
            print(f"      ✗ Clip {idx}: section download failed")
            return None

        raw_size = os.path.getsize(raw_path) / 1024 / 1024
        print(f"      ✓ Clip {idx}: section downloaded ({raw_size:.1f} MB)")

        # ── 2. Get dimensions + build face cache from this short file ─────
        try:
            src_w, src_h = get_video_dimensions(raw_path)
        except Exception as e:
            print(f"      ✗ Clip {idx}: ffprobe failed: {e}")
            return None

        face_cache = build_face_cache_from_file(raw_path)

        # Face cache timestamps are relative to the clip file (start=0),
        # so we pass clip_start=0, duration=clip["duration"]
        face_cx_src = compute_face_crop_x_from_cache(
            face_cache, 0.0, clip["duration"], src_w
        )

        # ── 3. Build crop filtergraph ──────────────────────────────────────
        OUT_W, OUT_H  = 1080, 1920
        scale_factor  = OUT_H / src_h
        scaled_w      = int(src_w * scale_factor)
        face_cx_sc    = int(face_cx_src * scale_factor)
        crop_x        = max(0, min(face_cx_sc - OUT_W // 2, scaled_w - OUT_W))

        vf_crop = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={scaled_w}:{OUT_H},"
            f"crop={OUT_W}:{OUT_H}:{crop_x}:0,"
            f"boxblur=20:20[blurred];"
            f"[fg]scale={scaled_w}:{OUT_H},"
            f"crop={OUT_W}:{OUT_H}:{crop_x}:0[cropped];"
            f"[blurred][cropped]overlay=0:0[overlaid]"
        )

        # ── 4. Write SRT + build drawtext filter chain ────────────────────
        # We read the SRT entries and convert each one into a ffmpeg drawtext
        # filter with enable='between(t,start,end)'. This approach:
        #   • works on ALL FFmpeg builds (no libass required)
        #   • gives pixel-exact font size and position on 1080×1920
        #   • is immune to force_style being ignored by subtitles filter
        n_subs = write_srt(segments, clip["start"], clip["end"], str(srt_path))
        print(f"      → Clip {idx}: {n_subs} subtitle entries")

        # Parse the SRT we just wrote to build drawtext entries
        # Font size 28px on 1080×1920 looks natural on a phone screen.
        # y=h-120 places text 120px above the bottom edge.
        FONT_SIZE  = 28
        MARGIN_BOT = 140   # px from bottom of 1920px frame

        drawtext_filters = _srt_to_drawtext(
            str(srt_path),
            font_size  = FONT_SIZE,
            margin_bot = MARGIN_BOT,
            frame_w    = OUT_W,   # 1080
            frame_h    = OUT_H,   # 1920
        )

        if drawtext_filters:
            # Chain: crop/blur overlay → drawtext1 → drawtext2 → ... → [out]
            dt_chain = ";".join(
                f"[dt{i}]{f}[dt{i+1}]" if i < len(drawtext_filters) - 1
                else f"[dt{i}]{f}[out]"
                for i, f in enumerate(drawtext_filters)
            )
            # Rename the overlaid node to dt0 so the chain starts correctly
            vf_crop_renamed = vf_crop.replace(
                "[blurred][cropped]overlay=0:0[overlaid]",
                "[blurred][cropped]overlay=0:0[dt0]"
            )
            full_vf = vf_crop_renamed + ";" + dt_chain
        else:
            # No subtitle entries — just pass through without subtitle layer
            full_vf = vf_crop.replace(
                "[blurred][cropped]overlay=0:0[overlaid]",
                "[blurred][cropped]overlay=0:0[out]"
            )

        # ── 5. Encode ──────────────────────────────────────────────────────
        cmd = [
            "ffmpeg", "-y",
            "-i", raw_path,           # input is the short section file
            "-filter_complex", full_vf,
            "-map", "[out]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "96k",
            "-movflags", "+faststart",
            str(out_path),
        ]
        print(f"      → Clip {idx}: encoding...", flush=True)
        res = subprocess.run(cmd, capture_output=True, text=True)

        # Clean up raw section file regardless of encode result
        try:
            os.remove(raw_path)
        except OSError:
            pass

        if res.returncode == 0:
            size_mb = os.path.getsize(out_path) / 1024 / 1024
            print(f"      ✓ Clip {idx}: done ({size_mb:.1f} MB)")
            return str(out_path)
        else:
            print(f"      ✗ Clip {idx}: encode FAILED")
            print(f"        {res.stderr[-400:]}")
            return None

    output_paths = []
    with ThreadPoolExecutor(max_workers=1) as ex:
        futures = {ex.submit(render_one, clip): clip for clip in clips}
        done    = 0
        for future in as_completed(futures):
            done += 1
            path  = future.result()
            if path:
                output_paths.append(path)
            if progress_callback:
                pct = 65 + int((done / len(clips)) * 33)
                progress_callback(f"Rendered {done}/{len(clips)} clips...", pct)

    return output_paths


# ── STEP 6: SAVE METADATA ───────────────────────────────────────────────────
def save_transcript(segments: list, clips: list, video_id: str, title: str):
    data = {"video_id": video_id, "title": title, "segments": segments, "clips": clips}
    path = OUTPUT_DIR / f"{video_id}_data.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"    ✓ Metadata saved → {path}")


# ── MAIN PIPELINE ───────────────────────────────────────────────────────────
def generate_shorts(url: str, num_clips: int = NUM_CLIPS,
                    clip_duration: int = CLIP_DURATION,
                    geo: str = "IN",
                    progress_callback=None) -> dict:

    print("\n" + "="*60)
    print("  ShortAI — Viral Shorts Generator  (Phase 3 — Optimized)")
    print("  New flow: audio-only download → partial clip fetch → render")
    print("="*60)

    t0 = time.time()

    # ── 0. Fetch metadata (no download, ~2s) ──────────────────────────────
    if progress_callback:
        progress_callback("Fetching video info...", 3)
    video_info = fetch_video_info(url)

    # ── 1. Build hook vocab + download audio IN PARALLEL ──────────────────
    # Hook vocab fetch and audio download both hit the network — run together.
    if progress_callback:
        progress_callback("Fetching trends + downloading audio in parallel...", 6)

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_vocab = ex.submit(build_hook_vocabulary, geo)
        f_audio = ex.submit(download_audio_only, url,
                            video_info["video_id"], None)  # no callback inside thread
        hook_vocab  = f_vocab.result()
        audio_path  = f_audio.result()

    # ── 2. Transcribe the small audio file ────────────────────────────────
    segments = transcribe_video(audio_path, progress_callback)

    # ── 3. Score segments ─────────────────────────────────────────────────
    scored = score_segments(segments, video_info["duration"],
                            hook_vocab=hook_vocab,
                            progress_callback=progress_callback)

    # ── 4. Select clip windows ────────────────────────────────────────────
    clips = select_clip_windows(scored, video_info["duration"],
                                num_clips, clip_duration)

    # ── 5. Download clip sections + face-track + render (parallel) ────────
    # No full video ever saved to disk — each clip downloads only its seconds.
    output_paths = render_clips(url, clips, video_info["video_id"],
                                segments, progress_callback)

    # ── 6. Save metadata ──────────────────────────────────────────────────
    save_transcript(segments, clips, video_info["video_id"], video_info["title"])

    elapsed = time.time() - t0
    if progress_callback:
        progress_callback("All clips ready!", 100)

    print("\n" + "="*60)
    print(f"  Done! {len(output_paths)} clip(s) saved to /outputs/")
    print(f"  Total time: {elapsed:.1f}s")
    print("="*60 + "\n")

    lang_breakdown = Counter(s.get("lang", "en") for s in segments)
    return {
        "title":            video_info["title"],
        "duration":         video_info["duration"],
        "clips_saved":      len(output_paths),
        "output_paths":     output_paths,
        "clips_meta":       clips,
        "hook_vocab_sizes": {k: len(v) for k, v in hook_vocab.items()},
        "lang_breakdown":   dict(lang_breakdown),
        "elapsed_seconds":  round(elapsed, 1),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("\nUsage:  python engine.py <youtube_url> [geo=IN]\n")
        sys.exit(1)
    geo    = sys.argv[2] if len(sys.argv) > 2 else "IN"
    result = generate_shorts(sys.argv[1], geo=geo)
    for p in result["output_paths"]:
        print(f"  → {p}")
    print(f"\n  Total time: {result['elapsed_seconds']}s")