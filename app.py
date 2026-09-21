import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
LOGS = DATA / "logs"
PROFILES_DIR = DATA / "profiles"
for d in (DATA, LOGS, PROFILES_DIR):
    d.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DATA / "settings.json"
STATE_FILE = DATA / "state.json"
CLIENT_SECRET = BASE / "client_secret.json"
LOG_FILE = LOGS / "app.log"
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8000/auth/callback")
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
MASK = "********"
MIN_FREE_GB = 2
LATIN_LANGS = {"en", "es", "fr", "de", "it", "pt", "nl", "sv", "no", "da", "fi", "pl", "cs", "ro", "hu", "tr", "id", "ms", "vi", "tl"}
REVIEW_MODES = ("advisory", "block", "off")
ENGINES = ("local", "veo")
TTS_ENGINES = ("auto", "kokoro", "piper", "say", "espeak")
SUGGESTED_TEXT_MODELS = ["llama3.1:8b", "llama3.2:3b", "qwen2.5:7b", "mistral:7b"]
SUGGESTED_VISION_MODELS = ["qwen2.5vl:7b", "llava:7b", "llama3.2-vision", "moondream"]
SMALL_VISION_MODELS = ("moondream", "llava-phi3", "bakllava")

GLOBAL_DEFAULTS = {
    "active_profile": "default",
    "storage_dir": "videos",
    "models_dir": "",
    "temp_dir": "",
    "delete_after_publish": False,
    "keep_clips": False,
    "video_engine": "local",
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.1:8b",
    "vision_model": "qwen2.5vl:7b",
    "ollama_timeout": 300,
    "sd_model": "stabilityai/sdxl-turbo",
    "sd_steps": 4,
    "sd_guidance": 2,
    "sd_width": 704,
    "sd_height": 1216,
    "tts_engine": "auto",
    "tts_voice": "af_heart",
    "tts_rate": 170,
    "piper_model": "",
    "auto_install": True,
    "gemini_api_key": "",
    "veo_model": "veo-3.1-fast-generate-preview",
    "aspect_ratio": "9:16",
    "target_seconds": 60,
    "clip_seconds": 8,
    "error_cooldown_minutes": 5,
}

PROFILE_DEFAULTS = {
    "name": "Default",
    "channel_niche": "",
    "made_for_kids": False,
    "kids_manual_review": True,
    "kids_llm_review": "advisory",
    "ai_disclosure": True,
    "upload_category_id": "24",
    "privacy_status": "private",
    "trending_query": "",
    "trending_queries": "",
    "trending_days": 7,
    "category_id": "",
    "region_code": "US",
    "source_language": "en",
    "trending_count": 25,
    "videos_per_day": 0,
    "minutes_between_videos": 0,
}

KIDS_RULES = """The audience is young children under 13. Follow every one of these rules:
Use simple, warm, positive language a 5 year old understands.
Characters are cartoon animals, creatures, or objects. Never realistic humans and never real children.
No violence, weapons, injuries, blood, death, dangerous acts children could imitate, scary or disturbing imagery, bullying, romance, kissing, gross-out humor, alcohol, tobacco, drugs, or adult themes.
No brand names, real products, toys for sale, or product placement.
Never ask viewers to comment, like, subscribe, share personal information, visit a website, or leave YouTube.
No clickbait. The title honestly describes the story, uses no ALL CAPS words, and has at most one exclamation mark.
Include a gentle lesson or positive message such as kindness, sharing, curiosity, or courage.
Visuals are bright, colorful, calm, friendly animation in safe, cheerful settings, with no flashing or strobing light.
"""

REVIEW_RULES = {
    "violence": "violence, weapons, injuries, blood, death, or dangerous acts a child could copy",
    "scary": "scary, creepy, or disturbing imagery or events",
    "humans": "realistic humans or real children as characters",
    "adult": "romance, kissing, alcohol, tobacco, drugs, or other adult themes",
    "commercial": "brand names, real products, toys for sale, or product placement",
    "contact": "asking viewers to comment, like, subscribe, share personal information, visit a website, or leave YouTube",
    "negative": "bullying, meanness, or a message that teaches bad behavior",
}

KIDS_VEO_SUFFIX = (
    " Flat 2D cartoon animation, never photorealistic, correct anatomy with the right number of limbs. Child-friendly cartoon animation with cute non-human characters, soft bright colors, calm pacing, "
    "safe cheerful setting, nothing scary or dangerous, no realistic people, no logos, no flashing lights."
)

CARTOON_SUFFIX = (
    ", flat 2D cartoon illustration, hand drawn animation style, simple stylized cartoon characters, "
    "correct anatomy with the right number of limbs, one head per character, exactly two ears and two eyes per animal, clean clear shapes, "
    "not photorealistic, not a photograph, no realistic people"
)

KIDS_IMAGE_SUFFIX = ", children's picture book illustration, cute cartoon characters, bright soft colors, cheerful and safe, gentle"

IMAGE_NEGATIVE = (
    "photorealistic, photograph, photo, realistic human, real person, lifelike face, hyperrealistic, 3d render, "
    "extra limbs, extra legs, extra arms, extra heads, two heads, multiple heads, duplicated body, duplicate character, "
    "extra ears, three ears, four ears, extra eyes, three eyes, extra tails, extra noses, extra mouths, "
    "fused limbs, missing limbs, malformed limbs, deformed, mutated, disfigured, bad anatomy, wrong anatomy, "
    "malformed hands, extra fingers, distorted face, blurry, low quality, text, letters, watermark, logo, signature, "
    "scary, creepy, horror, violence, weapon, blood, gore, nsfw"
)

KIDS_VISION_PROMPT = """Look carefully at this image from a video for young children.
First describe what you actually see in the image.
Then decide: it is unsafe if it shows photorealistic or lifelike people, realistic children, any character with the wrong number of limbs, legs, arms, or heads, duplicated or merged bodies, deformed or distorted faces, violence, weapons, blood, injury, scary or creepy imagery, nudity, alcohol, tobacco, drugs, brand logos, or anything a parent would find inappropriate for a 4 year old.
Count the heads, ears, eyes, legs, arms, and tails on each animal or person before deciding. Any extra ear, eye, limb, or tail makes it unsafe.
Reply with JSON only, filling in your own words: {"description": "what you see", "safe": true or false, "reason": "why"}"""

VISION_TEXT_PROMPT = """Look at this image, which is meant for young children.
Answer with one word, safe or unsafe, then a short reason.
Answer unsafe if it shows photorealistic or lifelike people, any character with the wrong number of heads, ears, eyes, limbs, or tails, duplicated or merged bodies, violence, weapons, blood, injury, scary or creepy imagery, distorted faces, nudity, alcohol, tobacco, drugs, brand logos, or anything a parent would find inappropriate for a 4 year old."""

KIDS_BANNED = re.compile(
    r"\b(kill\w*|blood\w*|bleed\w*|guns?|knife|knives|swords?|weapons?|bombs?|murder\w*|dead|die|dies|dying|death|"
    r"scary|spooky|terrif\w*|horror|creepy|nightmare\w*|ghost\w*|haunt\w*|zombie\w*|demon\w*|devil\w*|bhoot|"
    r"sexy|(?<!sun-)(?<!sun )kiss\w*|beer|wine|drunk|cigar\w*|smok\w*|drugs?|vape\w*|hate)\b",
    re.I,
)
KIDS_CONTACT = re.compile(
    r"(https?://|www\.|\S+@\S+\.\w+|@\w{3,}|\bcomments?\b|\bsubscrib\w*|\blike and\b|\blink in\b|"
    r"\byour (name|address|phone|school|age|birthday)\b|\bdm\b|\bmessage us\b)",
    re.I,
)

log_buffer = deque(maxlen=3000)
log_lock = threading.Lock()
log_seq = 0


class BufferHandler(logging.Handler):
    def emit(self, record):
        global log_seq
        line = self.format(record)
        with log_lock:
            log_seq += 1
            log_buffer.append((log_seq, record.levelname, line))


logger = logging.getLogger("shorts")
logger.setLevel(logging.DEBUG)
logger.propagate = False
for _old in list(logger.handlers):
    logger.removeHandler(_old)
    try:
        _old.close()
    except Exception:
        pass
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
for _h in (
    RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
    logging.StreamHandler(),
    BufferHandler(),
):
    _h.setFormatter(_fmt)
    logger.addHandler(_h)

state_lock = threading.RLock()
settings_lock = threading.RLock()
pipe_lock = threading.Lock()
pipeline = {"id": None, "obj": None}
kokoro = {"obj": None}
stop_event = threading.Event()
runner = None
pending_flow = None
pending_profile = None
status = {"running": False, "stage": "idle", "step": 0, "steps": 0, "job": "", "started": None}


class Cancelled(Exception):
    pass


class VisionUnavailable(Exception):
    pass


def check():
    if stop_event.is_set():
        raise Cancelled()


def set_stage(stage, log=True, step=None, steps=None):
    status["stage"] = stage
    if step is not None:
        status["step"] = step
    if steps is not None:
        status["steps"] = steps
    if log:
        logger.info("Stage: %s", stage)


def reset_progress(job=""):
    status["step"] = 0
    status["steps"] = 0
    status["job"] = job
    status["started"] = datetime.now().timestamp() if job else None


def truthy(v):
    return v is True or str(v).strip().lower() in ("true", "yes", "1", "pass", "safe")


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def coerce(defaults, key, value):
    d = defaults[key]
    if isinstance(d, bool):
        return bool(value)
    if isinstance(d, int):
        return int(value)
    return str(value).strip()


def resolve_storage(path_str):
    p = Path(str(path_str or "videos").strip().strip('"')).expanduser()
    return p if p.is_absolute() else BASE / p


def resolve_sub(s, key, fallback):
    raw = str(s.get(key, "")).strip().strip('"')
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else BASE / p
    return resolve_storage(s["storage_dir"]) / fallback


def output_root(s):
    return resolve_storage(s["storage_dir"]) / "output"


def published_root(s):
    return resolve_storage(s["storage_dir"]) / "published"


def writable(root):
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise ValueError(f"Folder '{root}' is not writable: {e}")
    return root


def validate_storage(path_str):
    root = writable(resolve_storage(path_str))
    (root / "output").mkdir(parents=True, exist_ok=True)
    (root / "published").mkdir(parents=True, exist_ok=True)
    return root


def free_gb(path):
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def venv_python():
    cand = BASE / ".venv" / "bin" / "python"
    return str(cand) if cand.exists() else sys.executable


def pip_install(*args):
    cmd = [venv_python(), "-m", "pip", "install", *args]
    logger.info("Installing %s (this can take a few minutes)", " ".join(args))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        logger.warning("Install failed: %s", (r.stderr or r.stdout).strip()[-500:])
        return False
    logger.info("Installed %s", " ".join(args))
    return True


def have_module(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def ensure_module(s, module, package):
    if have_module(module):
        return True
    if not s.get("auto_install", True):
        logger.warning("%s is missing and auto install is off", package)
        return False
    set_stage(f"installing {package}")
    return pip_install(package) and have_module(module)


def voices_dir(s):
    return resolve_sub(s, "models_dir", "models") / "voices"


def find_piper_voice(s):
    configured = s["piper_model"].strip()
    if configured and Path(configured).expanduser().exists():
        return str(Path(configured).expanduser())
    vdir = voices_dir(s)
    if vdir.exists():
        for f in sorted(vdir.glob("*.onnx")):
            return str(f)
    return ""


def ensure_piper_voice(s):
    voice = find_piper_voice(s)
    if voice:
        return voice
    if not s.get("auto_install", True):
        return ""
    vdir = voices_dir(s)
    vdir.mkdir(parents=True, exist_ok=True)
    base = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium"
    set_stage("downloading narration voice")
    try:
        for suffix in (".onnx", ".onnx.json"):
            dest = vdir / f"en_US-lessac-medium{suffix}"
            if dest.exists():
                continue
            logger.info("Downloading narration voice%s", suffix)
            with requests.get(base + suffix, stream=True, timeout=(10, 600)) as r:
                r.raise_for_status()
                with open(dest, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        check()
                        fh.write(chunk)
        logger.info("Narration voice ready")
    except Cancelled:
        raise
    except Exception as e:
        logger.warning("Could not download the Piper voice: %s", e)
        return ""
    return find_piper_voice(s)


def piper_binary():
    for cand in (BASE / ".venv" / "bin" / "piper", Path("/opt/homebrew/bin/piper")):
        if cand.exists():
            return str(cand)
    return shutil.which("piper") or ""


KOKORO_FILES = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}


def kokoro_assets(s):
    vdir = voices_dir(s)
    vdir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, url in KOKORO_FILES.items():
        dest = vdir / name
        if not dest.exists() or dest.stat().st_size < 1_000_000:
            set_stage(f"downloading narration model {name}")
            logger.info("Downloading %s", name)
            tmp = dest.with_suffix(dest.suffix + ".part")
            with requests.get(url, stream=True, timeout=(10, 1200)) as r:
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        check()
                        fh.write(chunk)
            os.replace(tmp, dest)
            logger.info("Downloaded %s", name)
        paths[name] = str(dest)
    return paths


def kokoro_speak(s, text, out_wav):
    import soundfile as sf
    from kokoro_onnx import Kokoro
    with pipe_lock:
        if kokoro["obj"] is None:
            assets = kokoro_assets(s)
            logger.info("Loading narration model")
            kokoro["obj"] = Kokoro(assets["kokoro-v1.0.onnx"], assets["voices-v1.0.bin"])
            logger.info("Narration model ready")
    voice = s["tts_voice"].strip() or "af_heart"
    if not re.fullmatch(r"[a-z]{2}_[a-z_]+", voice):
        voice = "af_heart"
    samples, rate = kokoro["obj"].create(text, voice=voice, speed=0.95, lang="en-us")
    sf.write(str(out_wav), samples, rate)


def apply_paths(s):
    models = writable(resolve_sub(s, "models_dir", "models"))
    temp = writable(resolve_sub(s, "temp_dir", "tmp"))
    os.environ["HF_HOME"] = str(models)
    os.environ["HF_HUB_CACHE"] = str(models / "hub")
    os.environ["TORCH_HOME"] = str(models / "torch")
    os.environ["TMPDIR"] = str(temp)
    tempfile.tempdir = str(temp)
    return models, temp


def prepare_storage(s):
    root = validate_storage(s["storage_dir"])
    free = free_gb(root)
    if free is not None and free < MIN_FREE_GB:
        raise RuntimeError(f"Only {free:.1f} GB free at {root}, need at least {MIN_FREE_GB} GB")
    models, temp = apply_paths(s)
    logger.info("Storage: %s (%.1f GB free) | Models: %s | Temp: %s", root, free or 0, models, temp)


def finish_files(s, job_dir, job_id):
    try:
        if s["delete_after_publish"]:
            shutil.rmtree(job_dir)
            logger.info("Deleted local files for job %s after publishing", job_id)
            return "deleted after publish"
        dest = published_root(s) / s["profile_id"] / job_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(job_dir), str(dest))
        logger.info("Moved job %s to %s", job_id, dest)
        return str(dest)
    except Exception as e:
        logger.error("Video was uploaded but moving or deleting local files failed: %s", e)
        return str(job_dir)


def pdir(pid):
    return PROFILES_DIR / pid


def valid_pid(pid):
    return bool(re.fullmatch(r"[a-z0-9-]+", pid or "")) and (pdir(pid) / "profile.json").exists()


def slugify(name):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "profile"
    slug, i = base, 2
    while pdir(slug).exists():
        slug = f"{base}-{i}"
        i += 1
    return slug


def load_profile(pid):
    p = dict(PROFILE_DEFAULTS)
    p.update({k: v for k, v in read_json(pdir(pid) / "profile.json", {}).items() if k in PROFILE_DEFAULTS})
    return p


def save_profile(pid, p):
    pdir(pid).mkdir(parents=True, exist_ok=True)
    write_json(pdir(pid) / "profile.json", p)


def profile_channel(pid):
    if not (pdir(pid) / "token.json").exists():
        return None
    return read_json(pdir(pid) / "channel.json", {}).get("title")


def list_profiles():
    return [
        {"id": d.name, "name": load_profile(d.name)["name"], "channel": profile_channel(d.name)}
        for d in sorted(PROFILES_DIR.iterdir())
        if (d / "profile.json").exists()
    ]


def load_global():
    with settings_lock:
        g = dict(GLOBAL_DEFAULTS)
        g.update({k: v for k, v in read_json(SETTINGS_FILE, {}).items() if k in GLOBAL_DEFAULTS})
        if not valid_pid(g["active_profile"]):
            profiles = list_profiles()
            g["active_profile"] = profiles[0]["id"] if profiles else "default"
        return g


def save_global(g):
    with settings_lock:
        write_json(SETTINGS_FILE, g)


def load_settings():
    g = load_global()
    s = dict(g)
    s.update(load_profile(g["active_profile"]))
    s["profile_id"] = g["active_profile"]
    return s


def migrate():
    if any((d / "profile.json").exists() for d in PROFILES_DIR.iterdir()):
        return
    old = read_json(SETTINGS_FILE, {})
    p = dict(PROFILE_DEFAULTS)
    p.update({k: v for k, v in old.items() if k in PROFILE_DEFAULTS})
    save_profile("default", p)
    for f in ("token.json", "channel.json"):
        src = DATA / f
        if src.exists():
            os.replace(src, pdir("default") / f)
    g = dict(GLOBAL_DEFAULTS)
    g.update({k: v for k, v in old.items() if k in GLOBAL_DEFAULTS})
    g["active_profile"] = "default"
    save_global(g)
    st = read_json(STATE_FILE, None)
    if st and st.get("published") and all(isinstance(v, int) for v in st["published"].values()):
        st["published"] = {"default": st["published"]}
        write_json(STATE_FILE, st)
    logger.info("Created Default profile from existing settings")


def load_state():
    with state_lock:
        st = read_json(STATE_FILE, {})
        st.setdefault("processed", [])
        st.setdefault("published", {})
        st.setdefault("history", [])
        return st


def save_state(st):
    with state_lock:
        write_json(STATE_FILE, st)


def update_history(job_id, **fields):
    with state_lock:
        st = load_state()
        for h in st["history"]:
            if h.get("job_id") == job_id:
                h.update(fields)
        save_state(st)


def published_today(pid):
    return load_state()["published"].get(pid, {}).get(date.today().isoformat(), 0)


def load_creds(pid):
    tf = pdir(pid) / "token.json"
    if not tf.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(tf), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        tf.write_text(creds.to_json(), encoding="utf-8")
    return creds


def youtube_client(pid):
    creds = load_creds(pid)
    if not creds:
        raise RuntimeError(f"No YouTube channel linked to profile '{load_profile(pid)['name']}'")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def map_videos(items):
    return [
        {
            "id": i["id"],
            "title": i["snippet"]["title"],
            "channel": i["snippet"].get("channelTitle", ""),
            "description": i["snippet"].get("description", "")[:1500],
            "tags": i["snippet"].get("tags", [])[:20],
            "language": (i["snippet"].get("defaultAudioLanguage") or i["snippet"].get("defaultLanguage") or "").lower(),
            "views": int(i.get("statistics", {}).get("viewCount", 0)),
        }
        for i in items
    ]


def mostly_latin(text):
    stripped = re.sub(r"#\S+|https?://\S+|@\S+", " ", str(text))
    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return False
    latin = sum(1 for c in letters if ord(c) < 0x250)
    return latin / len(letters) >= 0.95


def filter_language(videos, lang, strict=False):
    if not lang:
        return videos
    kept = []
    for v in videos:
        if v["language"] and not v["language"].startswith(lang):
            continue
        if strict and not v["language"]:
            continue
        if lang in LATIN_LANGS and not mostly_latin(v["title"]):
            continue
        kept.append(v)
    if len(kept) < len(videos):
        logger.info("Skipped %d source videos not in language '%s'", len(videos) - len(kept), lang)
    return kept


def profile_queries(s):
    extra = [q.strip() for q in re.split(r"[,\n]", s.get("trending_queries", "")) if q.strip()]
    main = s["trending_query"].strip()
    queries = ([main] if main else []) + extra
    return queries


def fetch_trending(s, query=None):
    yt = youtube_client(s["profile_id"])
    count = max(1, min(int(s["trending_count"]), 50))
    region = s["region_code"] or "US"
    lang = s["source_language"].strip().lower()
    query = (query if query is not None else s["trending_query"]).strip()
    if query:
        after = (datetime.now(timezone.utc) - timedelta(days=max(1, int(s["trending_days"])))).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "part": "id",
            "q": query,
            "type": "video",
            "order": "viewCount",
            "publishedAfter": after,
            "safeSearch": "strict" if s["made_for_kids"] else "moderate",
            "regionCode": region,
            "maxResults": count,
        }
        if lang:
            params["relevanceLanguage"] = lang
        if s["category_id"]:
            params["videoCategoryId"] = s["category_id"]
        ids = [i["id"]["videoId"] for i in yt.search().list(**params).execute().get("items", []) if i.get("id", {}).get("videoId")]
        items = yt.videos().list(part="snippet,statistics", id=",".join(ids)).execute().get("items", []) if ids else []
        logger.info("Search '%s' returned %d videos from the last %s days (region %s, language %s)", query, len(items), s["trending_days"], region, lang or "any")
    else:
        params = {
            "part": "snippet,statistics",
            "chart": "mostPopular",
            "regionCode": region,
            "maxResults": count,
        }
        if s["category_id"]:
            params["videoCategoryId"] = s["category_id"]
        items = yt.videos().list(**params).execute().get("items", [])
        logger.info("Fetched %d trending chart videos (region %s, category %s)", len(items), region, s["category_id"] or "all")
    kept = filter_language(map_videos(items), lang, strict=bool(s["made_for_kids"]))
    if not kept and s["made_for_kids"]:
        logger.info("No sources declared language '%s', falling back to title matching", lang)
        kept = filter_language(map_videos(items), lang)
    return kept


def ollama_tags(s):
    try:
        r = requests.get(f"{s['ollama_url'].rstrip('/')}/api/tags", timeout=10)
        r.raise_for_status()
        return sorted(m["name"] for m in r.json().get("models", []) if m.get("name"))
    except Exception as e:
        logger.debug("Could not list Ollama models: %s", e)
        return []


def ollama_has(s, model):
    installed = ollama_tags(s)
    return model in installed or f"{model}:latest" in installed


def ollama_pull(s, model):
    model = (model or "").strip()
    if not model:
        raise RuntimeError("No model name given")
    if ollama_has(s, model):
        return False
    logger.info("Model '%s' is not installed, pulling it now (this can take a while)", model)
    url = f"{s['ollama_url'].rstrip('/')}/api/pull"
    last = ""
    with requests.post(url, json={"model": model, "stream": True}, stream=True, timeout=(10, 3600)) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            check()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("error"):
                raise RuntimeError(f"Could not pull '{model}': {msg['error']}")
            st = msg.get("status", "")
            total, done = msg.get("total"), msg.get("completed")
            if total and done:
                pct = int(done * 100 / total)
                line_txt = f"{st} {pct}%"
                if line_txt != last and pct % 10 == 0:
                    logger.info("Pulling %s: %s", model, line_txt)
                    last = line_txt
            elif st and st != last:
                logger.info("Pulling %s: %s", model, st)
                last = st
    logger.info("Model '%s' is ready", model)
    return True


def ensure_local_deps(s):
    if not ensure_module(s, "torch", "torch") or not ensure_module(s, "diffusers", "diffusers accelerate safetensors transformers".split()[0]):
        raise RuntimeError("Could not install the image packages. Run ./install-local.sh")
    for module, package in (("accelerate", "accelerate"), ("safetensors", "safetensors"), ("transformers", "transformers")):
        ensure_module(s, module, package)
    want = s["tts_engine"] if s["tts_engine"] in TTS_ENGINES else "auto"
    if want in ("auto", "kokoro") and not have_module("kokoro_onnx"):
        if ensure_module(s, "kokoro_onnx", "kokoro-onnx"):
            ensure_module(s, "soundfile", "soundfile")
        else:
            logger.warning("Kokoro could not be installed, trying Piper instead")
            if not piper_binary():
                ensure_module(s, "piper", "piper-tts")
            ensure_piper_voice(s)
    if want == "piper":
        if not piper_binary():
            ensure_module(s, "piper", "piper-tts")
        ensure_piper_voice(s)
    logger.info("Narration engine: %s", pick_tts(s))


def ensure_models(s, engine):
    needed = [s["ollama_model"].strip()]
    if s["made_for_kids"] and s["vision_model"].strip():
        needed.append(s["vision_model"].strip())
    for m in needed:
        if m and not ollama_has(s, m):
            set_stage(f"downloading model {m}")
            ollama_pull(s, m)


def ollama_call(s, prompt, temperature=0.9, model=None, images=None, num_predict=2048, as_json=True):
    url = f"{s['ollama_url'].rstrip('/')}/api/generate"
    model = model or s["ollama_model"]
    timeout = max(30, int(s["ollama_timeout"]))
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "5m",
        "options": {"temperature": temperature, "num_predict": num_predict, "num_ctx": 8192},
    }
    if as_json:
        payload["format"] = "json"
    if images:
        payload["images"] = images
    started = datetime.now()
    logger.debug("Waiting on Ollama %s (timeout %ds)", model, timeout)
    try:
        r = requests.post(url, json=payload, timeout=(10, timeout))
    except requests.exceptions.ReadTimeout:
        raise RuntimeError(f"Ollama model '{model}' did not respond within {timeout}s. Check 'ollama ps' and free RAM, or use a smaller model")
    except requests.exceptions.ConnectionError:
        raise RuntimeError(f"Cannot reach Ollama at {s['ollama_url']}. Is it running?")
    if r.status_code == 404:
        logger.warning("Ollama model '%s' is missing, pulling it now", model)
        ollama_pull(s, model)
        r = requests.post(url, json=payload, timeout=(10, timeout))
    r.raise_for_status()
    text = r.json().get("response", "")
    logger.debug("Ollama %s responded in %.1fs: %s", model, (datetime.now() - started).total_seconds(), text[:2000])
    return json.loads(text) if as_json else text


def ollama_json(s, prompt, temperature=0.9, model=None, images=None, num_predict=2048):
    return ollama_call(s, prompt, temperature=temperature, model=model, images=images, num_predict=num_predict, as_json=True)


def write_script(s, src, feedback=None):
    clip = int(s["clip_seconds"])
    n = max(1, int(s["target_seconds"]) // clip)
    kids = bool(s["made_for_kids"])
    words = max(8, int(clip * (2.0 if kids else 2.3)))
    niche = s["channel_niche"].strip()
    niche_block = (
        f"This channel's niche: {niche}\nThe Short must fit this niche even if the trending source does not. Borrow only the general theme or what makes it appealing.\n"
        if niche else ""
    )
    kids_block = KIDS_RULES if kids else ""
    feedback_block = (
        "A previous draft was rejected for these problems. Do not repeat them:\n" + "\n".join(f"- {f}" for f in feedback) + "\n"
        if feedback else ""
    )
    prompt = f"""You are a short-form video writer. This video is currently popular on YouTube:
Title: {src['title']}
Channel: {src['channel']}
Description: {src['description']}
Tags: {', '.join(src['tags'])}

{niche_block}{kids_block}{feedback_block}
Identify the underlying topic and why it appeals to viewers. Then write a completely ORIGINAL {n * clip}-second YouTube Short in English.
Do not reuse the source's title, script, characters, jokes, branding, or channel identity.
Do not depict real people, celebrities, brands, logos, or copyrighted characters. Invent new characters.

First invent the cast. "characters" is one sentence naming each character with fixed, concrete visual details (species, color, size, clothing, one distinctive feature) that never change.
Every character is a cartoon: a cartoon animal, a cartoon creature, or a simple cartoon person. Never describe anyone as realistic, lifelike, photorealistic, or human-looking.

The Short has exactly {n} scenes of {clip} seconds each. Each scene has:
"visual": a detailed, self-contained shot description of one still image (setting, action, mood, lighting). Name the characters but do not re-describe their appearance, that comes from "characters". Never mention on-screen text or words.
"narration": one spoken line of at most {words} words.

Scene 1 must hook the viewer in the first 2 seconds. The final scene must deliver a payoff.

Return JSON only in this shape:
{{"title": "under 70 characters", "characters": "one sentence describing every character's fixed appearance", "style": "one sentence visual style applied to every scene", "description": "2 to 3 sentence YouTube description", "scenes": [{{"visual": "...", "narration": "..."}}]}}"""
    last = None
    for attempt in range(1, 4):
        check()
        try:
            data = ollama_json(s, prompt)
            scenes = [
                sc for sc in data.get("scenes", [])
                if isinstance(sc, dict) and str(sc.get("visual", "")).strip()
            ]
            if len(scenes) < n:
                raise ValueError(f"expected {n} scenes, got {len(scenes)}")
            if not str(data.get("title", "")).strip():
                raise ValueError("missing title")
            data["scenes"] = [{"visual": str(sc.get("visual", "")), "narration": str(sc.get("narration", ""))} for sc in scenes[:n]]
            data["title"] = str(data["title"])
            data["style"] = str(data.get("style", ""))
            data["characters"] = str(data.get("characters", ""))
            data["description"] = str(data.get("description", ""))
            logger.info("Script ready: '%s' (%d scenes)", data["title"], n)
            return data
        except Cancelled:
            raise
        except Exception as e:
            last = e
            logger.warning("Script attempt %d failed: %s", attempt, e)
    raise RuntimeError(f"Could not generate a valid script: {last}")


_english_words = None


def english_words():
    global _english_words
    if _english_words is None:
        words = set()
        for f in ("/usr/share/dict/words", "/usr/share/dict/american-english", "/usr/share/dict/british-english"):
            try:
                words |= {w.strip().lower() for w in open(f, encoding="utf-8", errors="ignore") if w.strip()}
            except OSError:
                continue
        _english_words = words
    return _english_words


def is_english_word(w, extra):
    w = w.lower().strip("'")
    if not w or w.isdigit() or w in extra:
        return True
    vocab = english_words()
    if not vocab:
        return True
    if w in vocab:
        return True
    for suffix in ("s", "es", "'s", "ers", "er", "ing", "ed", "ies", "ly"):
        if w.endswith(suffix) and len(w) > len(suffix) + 2:
            stem = w[: -len(suffix)]
            if stem in vocab or stem + "e" in vocab or (suffix == "ies" and stem + "y" in vocab):
                return True
    return False


def english_tags(tags, script):
    extra = set(re.findall(r"[a-z]+", script_text(script).lower()))
    extra |= {
        "youtube", "shorts", "kid", "kids", "kiddie", "preschool", "preschooler", "preschoolers", "toddler", "toddlers",
        "storytime", "storybook", "bedtime", "cartoon", "cartoons", "animated", "animation", "teamwork", "video", "videos",
        "online", "sel", "abc", "abcs", "diy", "fun", "3d", "2d", "short", "film",
    }
    kept, dropped = [], []
    for tag in tags:
        if not tag.isascii():
            dropped.append(tag)
            continue
        words = re.findall(r"[A-Za-z']+|\d+", tag)
        if words and all(is_english_word(w, extra) for w in words):
            kept.append(tag)
        else:
            dropped.append(tag)
    return kept, dropped


def clean_tags(tags):
    out, seen, total = [], set(), 0
    for t in tags:
        t = re.sub(r"[<>#\"]", "", str(t)).strip()
        if not t or len(t) > 100 or t.lower() in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if total + cost > 480:
            break
        out.append(t)
        seen.add(t.lower())
        total += cost
    return out


def make_tags(s, script):
    narration = " ".join(sc["narration"] for sc in script["scenes"])
    niche = s["channel_niche"].strip()
    niche_line = f"Channel niche: {niche}\n" if niche else ""
    kids_line = "The audience is young children. Tags must be child-appropriate and describe kids content.\n" if s["made_for_kids"] else ""
    prompt = f"""Generate YouTube tags for this Short.
Include specific tags for its exact subject plus broader category, genre, and audience tags that help discovery.
Every tag must accurately describe the video. No unrelated trending terms, no names of other creators, people, brands, or copyrighted characters.
Write every tag in plain English words only.
{niche_line}{kids_line}
Title: {script['title']}
Description: {script['description']}
Narration: {narration}

Return JSON only: {{"tags": ["15 to 25 tags, most specific first"]}}"""
    for attempt in range(1, 3):
        check()
        try:
            data = ollama_json(s, prompt, num_predict=512)
            raw = data.get("tags", [])
            if isinstance(raw, str):
                raw = raw.split(",")
            tags = clean_tags(raw)
            if s["source_language"].strip().lower() in ("", "en"):
                tags, dropped = english_tags(tags, script)
                if dropped:
                    logger.info("Dropped non-English tags: %s", ", ".join(dropped))
            if tags:
                return tags
        except Cancelled:
            raise
        except Exception as e:
            logger.warning("Tag attempt %d failed: %s", attempt, e)
    logger.warning("No tags generated, uploading without tags")
    return []


def normalize_text(t):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(t).lower())).strip()


def script_text(script):
    parts = [script["title"], script["description"], script["style"], script.get("characters", "")]
    for sc in script["scenes"]:
        parts += [sc["visual"], sc["narration"]]
    return " ".join(parts)


def kids_rule_issues(script):
    issues = []
    texts = [script["title"], script["description"], script["style"], script.get("characters", "")]
    texts += [f"{sc['visual']} {sc['narration']}" for sc in script["scenes"]]
    for t in texts:
        for m in KIDS_BANNED.finditer(t):
            issues.append(f"banned word '{m.group(0)}'")
        for m in KIDS_CONTACT.finditer(t):
            issues.append(f"contact info or call to action '{m.group(0)}'")
    title = script["title"]
    if re.search(r"\b[A-Z]{4,}\b", title):
        issues.append("ALL CAPS word in title")
    if title.count("!") > 1 or title.count("?") > 1:
        issues.append("clickbait punctuation in title")
    return sorted(set(issues))


def kids_llm_review(s, script):
    scenes = "\n".join(f"{i}. Visual: {sc['visual']}\n   Narration: {sc['narration']}" for i, sc in enumerate(script["scenes"], 1))
    rules = "\n".join(f"{k}: {v}" for k, v in REVIEW_RULES.items())
    prompt = f"""You are a careful reviewer for a YouTube channel of cartoon stories for young children.
Check the script below against each rule. A rule is only broken if specific words in the script clearly break it.
Do not judge capitalization, punctuation, target age, art style, or anything not listed in the rules. Those are checked elsewhere.

Rules (id: what is not allowed):
{rules}

Title: {script['title']}
Description: {script['description']}
Visual style: {script['style']}
Scenes:
{scenes}

For every broken rule, copy the exact words from the script that break it.
Return JSON only: {{"violations": [{{"rule": "rule id", "quote": "exact words copied from the script", "reason": "short explanation"}}]}}
Return {{"violations": []}} if no rule is broken."""
    try:
        data = ollama_json(s, prompt, temperature=0.1, num_predict=1024)
    except Cancelled:
        raise
    except Exception as e:
        return [f"compliance reviewer error: {e}"], 0
    raw = data.get("violations", [])
    if not isinstance(raw, list):
        return ["compliance reviewer returned an invalid response"], 0
    haystack = normalize_text(script_text(script))
    verified, ignored = [], 0
    for v in raw:
        if not isinstance(v, dict):
            ignored += 1
            continue
        rule = str(v.get("rule", "")).strip().lower()
        quote = normalize_text(v.get("quote", ""))
        if rule not in REVIEW_RULES or len(quote) < 3 or quote not in haystack:
            ignored += 1
            logger.debug("Ignored unverified reviewer finding: %s", v)
            continue
        verified.append(f"{rule}: '{v.get('quote')}' ({v.get('reason', '')})")
    return verified, ignored


def review_mode(s):
    return s["kids_llm_review"] if s["kids_llm_review"] in REVIEW_MODES else "advisory"


def produce_script(s, src):
    kids = bool(s["made_for_kids"])
    feedback = []
    attempts = 4 if kids else 1
    mode = review_mode(s)
    for attempt in range(1, attempts + 1):
        check()
        set_stage("writing script")
        script = write_script(s, src, feedback)
        set_stage("generating tags")
        tags = make_tags(s, script)
        if not kids:
            logger.info("Tags: %s", ", ".join(tags))
            return script, tags, {"kids_checks": False}
        dropped = [t for t in tags if KIDS_BANNED.search(t) or KIDS_CONTACT.search(t)]
        tags = [t for t in tags if t not in dropped]
        if dropped:
            logger.info("Dropped tags that failed kids filters: %s", ", ".join(dropped))
        logger.info("Tags: %s", ", ".join(tags))
        set_stage("kids compliance review")
        issues = kids_rule_issues(script)
        notes, ignored = ([], 0) if mode == "off" else kids_llm_review(s, script)
        if ignored:
            logger.info("Ignored %d reviewer findings that did not quote the script or match a rule", ignored)
        if mode == "block":
            issues += notes
        if not issues:
            if notes:
                logger.warning("Reviewer notes (advisory, check during manual review): %s", "; ".join(notes))
            logger.info("Kids compliance review passed on attempt %d (LLM review: %s)", attempt, mode)
            return script, tags, {
                "kids_checks": True,
                "script_review": "passed",
                "attempts": attempt,
                "dropped_tags": dropped,
                "reviewer_mode": mode,
                "reviewer_notes": notes,
                "reviewer_ignored_findings": ignored,
            }
        logger.warning("Kids compliance review failed (attempt %d/%d): %s", attempt, attempts, "; ".join(issues))
        feedback = issues
    raise RuntimeError(f"Script failed kids compliance review after {attempts} attempts")


def unload_pipeline():
    with pipe_lock:
        if pipeline["obj"] is None:
            return
        pipeline["obj"] = None
        pipeline["id"] = None
        try:
            import gc
            import torch
            gc.collect()
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        logger.debug("Released image model from memory")


INCONCLUSIVE = ("don't know", "do not know", "not sure", "cannot", "can't", "unable", "no image", "what you see", "short explanation")


VISION_QUESTIONS = [
    ("extra ears", "Look closely at every animal and character. Does any single animal or character have more than two ears? Answer only yes or no."),
    ("extra eyes", "Does any single animal or character have more than two eyes? Answer only yes or no."),
    ("extra heads", "Does any single animal or character have more than one head, or are two bodies merged together? Answer only yes or no."),
    ("extra limbs", "Count the legs and arms on each animal and character. Does any of them have more legs or arms than that kind of creature should have? Answer only yes or no."),
    ("realistic person", "Is there a realistic, photograph-like human being in this image, rather than a cartoon? Answer only yes or no."),
    ("unsuitable", "Is there anything scary, violent, creepy, or inappropriate for a 4 year old child in this image? Answer only yes or no."),
]


def vision_image(s, clip_path):
    src = clip_path.with_suffix(".png")
    out = clip_path.with_name(f"{clip_path.stem}_check.jpg")
    if src.exists():
        cmd = ["ffmpeg", "-y", "-i", str(src), "-vf", "scale=-2:1024", "-q:v", "3", str(out)]
    else:
        cmd = ["ffmpeg", "-y", "-ss", str(int(s["clip_seconds"]) / 2), "-i", str(clip_path), "-frames:v", "1", "-vf", "scale=-2:1024", "-q:v", "3", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError("Could not prepare an image for the vision check")
    data = base64.b64encode(out.read_bytes()).decode()
    out.unlink(missing_ok=True)
    return data


def yes_no(text):
    low = text.strip().lower()
    m = re.match(r"^\W*(yes|no)\b", low)
    if m:
        return m.group(1) == "yes"
    has_yes = re.search(r"\byes\b", low)
    has_no = re.search(r"\bno\b", low)
    if has_yes and not has_no:
        return True
    if has_no and not has_yes:
        return False
    return None


def kids_vision_check(s, clip_path):
    model = s["vision_model"].strip()
    if not model:
        raise RuntimeError("Made for kids requires a vision model in Settings to check generated clips")
    images = [vision_image(s, clip_path)]
    problems = []
    unclear = []
    try:
        desc = ollama_call(s, "Describe this image in one short sentence.", temperature=0.1, model=model, images=images, num_predict=80, as_json=False).strip()
        for key, question in VISION_QUESTIONS:
            check()
            answer = None
            for _ in range(2):
                reply = ollama_call(s, question, temperature=0.0, model=model, images=images, num_predict=10, as_json=False)
                answer = yes_no(reply)
                if answer is not None:
                    break
            if answer is None:
                unclear.append(key)
            elif answer:
                problems.append(key)
    except Cancelled:
        raise
    except Exception as e:
        raise VisionUnavailable(f"vision model '{model}' could not run: {e}") from e
    if problems:
        raise ValueError(f"Vision check rejected {clip_path.name}: {', '.join(problems)} ({desc[:120]})")
    if unclear:
        raise VisionUnavailable(f"vision model '{model}' gave no clear answer about {', '.join(unclear)}")
    logger.info("Vision check passed for %s: %s", clip_path.name, desc[:160])


def load_pipeline(s):
    with pipe_lock:
        model = s["sd_model"].strip()
        if pipeline["id"] == model and pipeline["obj"] is not None:
            return pipeline["obj"]
        try:
            import torch
            from diffusers import AutoPipelineForText2Image
        except ImportError:
            raise RuntimeError("Image packages are missing. Turn on auto install in Settings or run ./install-local.sh")
        if torch.backends.mps.is_available():
            device, dtype = "mps", torch.float16
        elif torch.cuda.is_available():
            device, dtype = "cuda", torch.float16
        else:
            device, dtype = "cpu", torch.float32
        logger.info("Loading image model %s on %s (first run downloads several GB)", model, device)
        pipe = AutoPipelineForText2Image.from_pretrained(model, torch_dtype=dtype, variant="fp16" if dtype == torch.float16 else None)
        pipe = pipe.to(device)
        pipe.set_progress_bar_config(disable=True)
        pipeline["id"] = model
        pipeline["obj"] = pipe
        logger.info("Image model ready")
        return pipe


def generate_image(s, prompt, path):
    pipe = load_pipeline(s)
    kwargs = {
        "prompt": prompt[:900],
        "num_inference_steps": max(1, int(s["sd_steps"])),
        "guidance_scale": float(s["sd_guidance"]),
        "width": max(512, int(s["sd_width"]) // 64 * 64),
        "height": max(512, int(s["sd_height"]) // 64 * 64),
    }
    if kwargs["guidance_scale"] < 1.1:
        kwargs["guidance_scale"] = 1.5
    kwargs["negative_prompt"] = IMAGE_NEGATIVE
    started = datetime.now()
    image = pipe(**kwargs).images[0]
    image.save(str(path))
    logger.info("Generated %s in %.1fs", path.name, (datetime.now() - started).total_seconds())


def pick_tts(s):
    engine = s["tts_engine"] if s["tts_engine"] in TTS_ENGINES else "auto"
    if engine != "auto":
        return engine
    if have_module("kokoro_onnx"):
        return "kokoro"
    if find_piper_voice(s) and piper_binary():
        return "piper"
    return "say" if sys.platform == "darwin" else "espeak"


def system_speak(s, text, out_wav):
    if sys.platform == "darwin":
        aiff = out_wav.with_suffix(".aiff")
        cmd = ["say", "-r", str(int(s["tts_rate"])), "-o", str(aiff), text]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"say failed: {r.stderr.strip()[:200]}")
        conv = subprocess.run(["ffmpeg", "-y", "-i", str(aiff), "-ar", "44100", "-ac", "2", str(out_wav)], capture_output=True, text=True)
        aiff.unlink(missing_ok=True)
        if conv.returncode != 0:
            raise RuntimeError("Could not convert narration audio")
    else:
        r = subprocess.run(["espeak-ng", "-v", "en-us", "-s", str(int(s["tts_rate"])), "-w", str(out_wav), text], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"espeak-ng failed: {r.stderr.strip()[:200]}")


def tts_speak(s, text, out_wav):
    text = text.strip() or "..."
    engine = pick_tts(s)
    try:
        if engine == "kokoro":
            kokoro_speak(s, text, out_wav)
        elif engine == "piper":
            voice = ensure_piper_voice(s)
            binary = piper_binary()
            if not voice or not binary:
                raise RuntimeError("Piper is not available")
            r = subprocess.run([binary, "--model", voice, "--output_file", str(out_wav)], input=text, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"piper failed: {r.stderr.strip()[:200]}")
        else:
            system_speak(s, text, out_wav)
    except Cancelled:
        raise
    except Exception as e:
        if engine in ("kokoro", "piper"):
            logger.warning("%s narration failed (%s), using the system voice for this scene", engine, e)
            system_speak(s, text, out_wav)
        else:
            raise
    if not out_wav.exists() or out_wav.stat().st_size < 1000:
        raise RuntimeError("Narration audio was empty")


def media_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def render_scene(s, image, audio, out, aspect):
    w, h = (1080, 1920) if aspect == "9:16" else (1920, 1080)
    dur = max(float(s["clip_seconds"]), media_duration(audio) + 0.6)
    frames = int(dur * 30)
    vf = (
        f"[0:v]scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
        f"zoompan=z='min(zoom+0.0004,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps=30,"
        f"format=yuv420p[v];[1:a]apad,atrim=0:{dur:.2f},asetpts=N/SR/TB[a]"
    )
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(image), "-i", str(audio),
        "-filter_complex", vf, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-t", f"{dur:.2f}", str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        logger.error("ffmpeg output:\n%s", r.stderr[-3000:])
        raise RuntimeError("Could not render scene video")
    logger.info("Rendered %s (%.1fs)", out.name, dur)


def make_clip_local(s, scene, script, path, kids):
    image = path.with_suffix(".png")
    audio = path.with_suffix(".wav")
    style = script["style"].strip()
    cast = script.get("characters", "").strip()
    cast_part = f" Characters: {cast}" if cast else ""
    prompt = f"{scene['visual']}{cast_part} {style}{KIDS_IMAGE_SUFFIX if kids else ''}{CARTOON_SUFFIX}"
    logger.debug("Image prompt: %s", prompt)
    generate_image(s, prompt, image)
    tts_speak(s, scene["narration"], audio)
    render_scene(s, image, audio, path, s["aspect_ratio"])
    if not s["keep_clips"]:
        audio.unlink(missing_ok=True)
    return image


def make_clip_veo(s, scene, script, path, kids):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=s["gemini_api_key"])
    orientation = "Vertical" if s["aspect_ratio"] == "9:16" else "Widescreen"
    prompt = (
        f"{scene['visual']} Style: {script['style']}. {orientation} short-form video. "
        f"A narrator's voiceover says: \"{scene['narration']}\" "
        f"No on-screen text, captions, or subtitles.{KIDS_VEO_SUFFIX if kids else ''}"
    )
    logger.debug("Veo prompt: %s", prompt)
    op = client.models.generate_videos(
        model=s["veo_model"],
        prompt=prompt,
        config=types.GenerateVideosConfig(aspect_ratio=s["aspect_ratio"]),
    )
    waited = 0
    while not op.done:
        check()
        if waited > 900:
            raise TimeoutError("Veo generation timed out after 15 minutes")
        stop_event.wait(10)
        waited += 10
        op = client.operations.get(op)
    err = getattr(op, "error", None)
    if err:
        raise RuntimeError(f"Veo error: {err}")
    vids = op.response.generated_videos if op.response else None
    if not vids:
        raise RuntimeError("Veo returned no video (prompt may have been blocked by safety filters)")
    v = vids[0]
    client.files.download(file=v.video)
    v.video.save(str(path))
    logger.info("Saved clip %s", path.name)
    return None


def concat_clips(clips, out, aspect):
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{c.resolve().as_posix()}'\n" for c in clips), encoding="utf-8")
    w, h = (1080, 1920) if aspect == "9:16" else (1920, 1080)
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps=30",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out),
    ]
    logger.debug("ffmpeg: %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        logger.error("ffmpeg output:\n%s", r.stderr[-3000:])
        raise RuntimeError(f"ffmpeg failed with code {r.returncode}")
    lst.unlink(missing_ok=True)
    logger.info("Final video: %s", out)


def upload_video(s, path, script, tags):
    yt = youtube_client(s["profile_id"])
    kids = bool(s["made_for_kids"])
    title = re.sub(r"[<>]", "", script["title"]).strip()[:90] or "Untitled"
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"
    hashtags = []
    for t in tags[:3]:
        h = re.sub(r"[^A-Za-z0-9]", "", t)
        if h:
            hashtags.append("#" + h)
    description = re.sub(r"[<>]", "", f"{script['description']}\n\n#Shorts {' '.join(hashtags)}").strip()[:4500]
    privacy = s["privacy_status"]
    held = kids and s["kids_manual_review"]
    if held:
        privacy = "private"
        logger.info("Holding made-for-kids video as private for manual review")
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": s["upload_category_id"] or "24",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": kids,
            "containsSyntheticMedia": bool(s["ai_disclosure"]),
        },
    }
    logger.info(
        "Uploading to %s as category %s, made for kids: %s, AI disclosure: %s",
        profile_channel(s["profile_id"]), body["snippet"]["categoryId"], kids, bool(s["ai_disclosure"]),
    )
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        check()
        prog, resp = req.next_chunk()
        if prog:
            logger.info("Upload %d%%", int(prog.progress() * 100))
    logger.info("Uploaded: https://youtube.com/shorts/%s (%s)", resp["id"], privacy)
    return resp["id"], held


def run_job(s):
    engine = s["video_engine"] if s["video_engine"] in ENGINES else "local"
    if engine == "veo" and not s["gemini_api_key"]:
        raise RuntimeError("Gemini API key is not set in Settings")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg was not found on PATH")
    kids = bool(s["made_for_kids"])
    if kids and not s["vision_model"].strip():
        raise RuntimeError("Made for kids requires a vision model in Settings (e.g. llama3.2-vision)")
    prepare_storage(s)
    ensure_models(s, engine)
    if engine == "local":
        ensure_local_deps(s)
    if engine == "local":
        logger.info("Image model: %s at %sx%s", s["sd_model"], s["sd_width"], s["sd_height"])
    logger.info("Profile: %s | Engine: %s | Niche: %s | Made for kids: %s | Upload category: %s", s["name"], engine, s["channel_niche"] or "none", kids, s["upload_category_id"])
    if kids:
        logger.info(
            "Kids compliance active: script rules, word filters, LLM review (%s), frame vision checks, made-for-kids flag%s",
            review_mode(s),
            ", manual review hold" if s["kids_manual_review"] else "",
        )
    set_stage("fetching trending videos")
    state = load_state()
    queries = profile_queries(s) or [""]
    candidates = []
    for q in queries:
        check()
        trending = fetch_trending(s, q)
        found = [v for v in sorted(trending, key=lambda v: -v["views"]) if v["id"] not in state["processed"]]
        if kids:
            safe = [v for v in found if not KIDS_BANNED.search(v["title"])]
            if len(safe) < len(found):
                logger.info("Skipped %d source videos with themes unsuitable for kids", len(found) - len(safe))
            found = safe
        if found:
            candidates = found
            break
        if len(queries) > 1:
            logger.info("Nothing new for '%s', trying the next query", q or "trending chart")
    if not candidates:
        logger.warning("No usable trending videos, checking again in 30 minutes")
        set_stage("waiting for new trending videos")
        stop_event.wait(1800)
        return
    src = candidates[0]
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    job_dir = output_root(s) / s["profile_id"] / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with state_lock:
        st = load_state()
        st["processed"] = (st["processed"] + [src["id"]])[-2000:]
        st["history"] = ([{
            "job_id": job_id,
            "profile": s["name"],
            "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "started_ts": int(datetime.now().timestamp()),
            "source_id": src["id"],
            "source_title": src["title"],
            "status": "running",
            "location": str(job_dir),
        }] + st["history"])[:200]
        save_state(st)
    scenes_n = max(1, int(s["target_seconds"]) // max(1, int(s["clip_seconds"])))
    reset_progress(job_id)
    set_stage(status["stage"], log=False, step=1, steps=3 + scenes_n * 2 + 2)
    logger.info("Job %s: source %s '%s' (%s views)", job_id, src["id"], src["title"], f"{src['views']:,}")
    logger.info("Working folder: %s", job_dir)
    compliance = {}
    finalized = False
    try:
        script, tags, compliance = produce_script(s, src)
        compliance["engine"] = engine
        update_history(job_id, title=script["title"], notes="; ".join(compliance.get("reviewer_notes", []))[:400])
        write_json(job_dir / "script.json", {"profile": s["name"], "source": src, "script": script, "tags": tags})
        clips = []
        extras = []
        vision_log = []
        n = len(script["scenes"])

        def build_scene(i, scene, path):
            for attempt in range(1, 4):
                check()
                try:
                    if engine == "local":
                        extra = make_clip_local(s, scene, script, path, kids)
                    else:
                        extra = make_clip_veo(s, scene, script, path, kids)
                    if extra and extra not in extras:
                        extras.append(extra)
                    return
                except Cancelled:
                    raise
                except Exception as e:
                    logger.warning("Scene %d attempt %d failed: %s", i, attempt, e)
                    path.unlink(missing_ok=True)
                    if attempt == 3:
                        raise
                    stop_event.wait(10)

        for i, scene in enumerate(script["scenes"], 1):
            set_stage(f"generating scene {i}/{n}", step=2 + i)
            path = job_dir / f"clip_{i:02d}.mp4"
            build_scene(i, scene, path)
            clips.append(path)

        if kids:
            pending = list(range(1, n + 1))
            for rnd in range(1, 4):
                unload_pipeline()
                rejected = []
                for i in pending:
                    check()
                    set_stage(f"vision check scene {i}/{n}", step=2 + n + i)
                    try:
                        kids_vision_check(s, clips[i - 1])
                        vision_log.append({"clip": i, "round": rnd, "result": "passed"})
                    except Cancelled:
                        raise
                    except VisionUnavailable as e:
                        if s["kids_manual_review"]:
                            logger.warning("Scene %d: %s. Continuing, check this video manually before publishing", i, e)
                            vision_log.append({"clip": i, "round": rnd, "result": f"skipped: {e}"[:300]})
                        else:
                            raise RuntimeError(f"{e}. Frames were not verified, so nothing was uploaded")
                    except Exception as e:
                        logger.warning("Scene %d rejected (round %d): %s", i, rnd, e)
                        vision_log.append({"clip": i, "round": rnd, "result": str(e)[:300]})
                        rejected.append(i)
                if not rejected:
                    break
                if rnd == 3:
                    raise RuntimeError(f"Scenes {rejected} still failed the vision check after 3 rounds")
                for i in rejected:
                    set_stage(f"regenerating scene {i}/{n}", step=2 + i)
                    clips[i - 1].unlink(missing_ok=True)
                    build_scene(i, script["scenes"][i - 1], clips[i - 1])
                pending = rejected
        if kids:
            compliance["vision_checks"] = vision_log
        set_stage("stitching video", step=3 + n * 2)
        final = job_dir / "final.mp4"
        concat_clips(clips, final, s["aspect_ratio"])
        set_stage("uploading to YouTube", step=4 + n * 2)
        vid, held = upload_video(s, final, script, tags)
        compliance["made_for_kids_flag"] = kids
        compliance["synthetic_media_flag"] = bool(s["ai_disclosure"])
        compliance["held_for_review"] = held
        compliance["youtube_id"] = vid
        with state_lock:
            st = load_state()
            key = date.today().isoformat()
            pub = st["published"].setdefault(s["profile_id"], {})
            pub[key] = pub.get(key, 0) + 1
            st["published"][s["profile_id"]] = dict(sorted(pub.items())[-60:])
            save_state(st)
        write_json(job_dir / "compliance.json", compliance)
        finalized = True
        if not s["keep_clips"]:
            for c in clips + extras:
                c.unlink(missing_ok=True)
        set_stage("moving files")
        location = finish_files(s, job_dir, job_id)
        update_history(job_id, status="review" if held else "published", youtube_id=vid, location=location, finished_ts=int(datetime.now().timestamp()))
        set_stage("job complete", step=status["steps"])
    except Cancelled:
        update_history(job_id, status="cancelled", finished_ts=int(datetime.now().timestamp()))
        logger.warning("Job %s cancelled", job_id)
        raise
    except Exception as e:
        update_history(job_id, status="failed", error=str(e)[:300], finished_ts=int(datetime.now().timestamp()))
        raise
    finally:
        if compliance and not finalized and job_dir.exists():
            write_json(job_dir / "compliance.json", compliance)


def run_loop():
    status["running"] = True
    logger.info("Autopilot started")
    try:
        while not stop_event.is_set():
            s = load_settings()
            limit = int(s["videos_per_day"])
            if limit > 0 and published_today(s["profile_id"]) >= limit:
                if status["stage"] != "daily limit reached":
                    logger.info("Profile '%s' reached its daily limit of %d, waiting for tomorrow", s["name"], limit)
                set_stage("daily limit reached", log=False)
                stop_event.wait(60)
                continue
            try:
                run_job(s)
            except Cancelled:
                break
            except Exception as e:
                logger.exception("Job failed: %s", e)
                mins = max(1, int(s["error_cooldown_minutes"]))
                set_stage(f"cooling down {mins} min after error")
                stop_event.wait(mins * 60)
                continue
            gap = int(s["minutes_between_videos"])
            if gap > 0 and not stop_event.is_set():
                set_stage(f"waiting {gap} min before next video")
                stop_event.wait(gap * 60)
    finally:
        status["running"] = False
        reset_progress()
        set_stage("idle")
        logger.info("Autopilot stopped")


migrate()
app = FastAPI()


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/status")
def api_status():
    s = load_settings()
    st = load_state()
    root = resolve_storage(s["storage_dir"])
    free = free_gb(root) if root.exists() else None
    return {
        "running": bool(runner and runner.is_alive()),
        "stage": status["stage"],
        "step": status["step"],
        "steps": status["steps"],
        "job": status["job"],
        "elapsed": int(datetime.now().timestamp() - status["started"]) if status["started"] else 0,
        "profile": s["name"],
        "engine": s["video_engine"],
        "made_for_kids": s["made_for_kids"],
        "published_today": published_today(s["profile_id"]),
        "videos_per_day": s["videos_per_day"],
        "channel": profile_channel(s["profile_id"]),
        "storage": str(root),
        "storage_free_gb": round(free, 1) if free is not None else None,
        "delete_after_publish": s["delete_after_publish"],
        "history": st["history"][:25],
        "now": int(datetime.now().timestamp()),
    }


@app.get("/api/settings")
def get_settings():
    g = load_global()
    if g["gemini_api_key"]:
        g["gemini_api_key"] = MASK
    return {
        "global": g,
        "profile": load_profile(g["active_profile"]),
        "active": g["active_profile"],
        "profiles": list_profiles(),
        "storage_resolved": str(resolve_storage(g["storage_dir"])),
        "models_resolved": str(resolve_sub(g, "models_dir", "models")),
        "temp_resolved": str(resolve_sub(g, "temp_dir", "tmp")),
        "installed_models": ollama_tags(g),
        "suggested_text_models": SUGGESTED_TEXT_MODELS,
        "suggested_vision_models": SUGGESTED_VISION_MODELS,
    }


@app.post("/api/settings")
async def post_settings(request: Request):
    body = await request.json()
    with settings_lock:
        g = load_global()
        old_storage = g["storage_dir"]
        pid = g["active_profile"]
        p = load_profile(pid)
        try:
            for k, v in body.get("global", {}).items():
                if k not in GLOBAL_DEFAULTS or k == "active_profile" or (k == "gemini_api_key" and v == MASK):
                    continue
                g[k] = coerce(GLOBAL_DEFAULTS, k, v)
            for k, v in body.get("profile", {}).items():
                if k in PROFILE_DEFAULTS:
                    p[k] = coerce(PROFILE_DEFAULTS, k, v)
            g["storage_dir"] = g["storage_dir"].strip().strip('"') or "videos"
            if g["video_engine"] not in ENGINES:
                g["video_engine"] = "local"
            if g["tts_engine"] not in TTS_ENGINES:
                g["tts_engine"] = "auto"
            p["source_language"] = p["source_language"].lower()[:5]
            if p["kids_llm_review"] not in REVIEW_MODES:
                p["kids_llm_review"] = "advisory"
            root = validate_storage(g["storage_dir"])
            writable(resolve_sub(g, "models_dir", "models"))
            writable(resolve_sub(g, "temp_dir", "tmp"))
        except (TypeError, ValueError) as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        if not p["name"]:
            p["name"] = pid
        save_global(g)
        save_profile(pid, p)
    if g["storage_dir"] != old_storage:
        logger.info("Storage folder set to %s (applies to new jobs)", root)
    logger.info("Settings saved for profile '%s'", p["name"])
    return {"ok": True}


@app.post("/api/profiles")
async def create_profile(request: Request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"detail": "Profile name is required"}, status_code=400)
    with settings_lock:
        g = load_global()
        p = load_profile(g["active_profile"])
        p["name"] = name
        pid = slugify(name)
        save_profile(pid, p)
        g["active_profile"] = pid
        save_global(g)
    logger.info("Created profile '%s' and made it active", name)
    return {"ok": True, "id": pid}


@app.post("/api/profiles/activate")
async def activate_profile(request: Request):
    pid = (await request.json()).get("id", "")
    if not valid_pid(pid):
        return JSONResponse({"detail": "Unknown profile"}, status_code=400)
    with settings_lock:
        g = load_global()
        g["active_profile"] = pid
        save_global(g)
    suffix = " (applies from the next video)" if runner and runner.is_alive() else ""
    logger.info("Active profile is now '%s'%s", load_profile(pid)["name"], suffix)
    return {"ok": True}


@app.post("/api/profiles/delete")
async def delete_profile(request: Request):
    pid = (await request.json()).get("id", "")
    if not valid_pid(pid):
        return JSONResponse({"detail": "Unknown profile"}, status_code=400)
    profiles = list_profiles()
    if len(profiles) <= 1:
        return JSONResponse({"detail": "Cannot delete the only profile"}, status_code=400)
    with settings_lock:
        g = load_global()
        if pid == g["active_profile"] and runner and runner.is_alive():
            return JSONResponse({"detail": "Stop the autopilot before deleting the active profile"}, status_code=400)
        name = load_profile(pid)["name"]
        shutil.rmtree(pdir(pid))
        if g["active_profile"] == pid:
            g["active_profile"] = next(p["id"] for p in profiles if p["id"] != pid)
            save_global(g)
    logger.info("Deleted profile '%s' (its videos on disk were not touched)", name)
    return {"ok": True}


@app.post("/api/start")
def api_start():
    global runner
    if runner and runner.is_alive():
        return {"ok": True, "detail": "already running"}
    stop_event.clear()
    runner = threading.Thread(target=run_loop, daemon=True)
    runner.start()
    return {"ok": True}


@app.post("/api/stop")
def api_stop():
    stop_event.set()
    if runner and runner.is_alive():
        set_stage("stopping")
    return {"ok": True}


@app.post("/api/models/pull")
async def api_pull_model(request: Request):
    model = str((await request.json()).get("model", "")).strip()
    if not model:
        return JSONResponse({"detail": "No model name given"}, status_code=400)
    g = load_global()
    try:
        pulled = ollama_pull(g, model)
    except Exception as e:
        logger.exception("Model pull failed")
        return JSONResponse({"detail": str(e)}, status_code=400)
    return {"ok": True, "pulled": pulled, "models": ollama_tags(g)}


@app.post("/api/history/clear")
def api_clear_history():
    with state_lock:
        st = load_state()
        count = len(st["history"])
        if count:
            st["history"] = []
            save_state(st)
    if count:
        logger.info("Cleared %d jobs from the list", count)
    return {"ok": True, "cleared": count}


@app.get("/api/trending")
def api_trending():
    try:
        return {"items": fetch_trending(load_settings())}
    except Exception as e:
        logger.exception("Trending preview failed")
        return JSONResponse({"detail": str(e)}, status_code=400)


@app.get("/api/logs")
def api_logs(since: int = 0):
    with log_lock:
        return {"lines": [x for x in log_buffer if x[0] > since]}


@app.get("/api/logs/download")
def api_logs_download():
    return FileResponse(LOG_FILE, filename="app.log")


@app.get("/auth/start")
def auth_start():
    global pending_flow, pending_profile
    if not CLIENT_SECRET.exists():
        return JSONResponse({"detail": "client_secret.json not found next to app.py"}, status_code=400)
    flow = Flow.from_client_secrets_file(str(CLIENT_SECRET), scopes=SCOPES, redirect_uri=REDIRECT_URI)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    pending_flow = flow
    pending_profile = load_global()["active_profile"]
    return RedirectResponse(url)


@app.get("/auth/callback")
def auth_callback(request: Request):
    global pending_flow, pending_profile
    if pending_flow is None or not valid_pid(pending_profile):
        return RedirectResponse("/")
    pid = pending_profile
    try:
        pending_flow.fetch_token(authorization_response=str(request.url))
        (pdir(pid) / "token.json").write_text(pending_flow.credentials.to_json(), encoding="utf-8")
        items = youtube_client(pid).channels().list(part="snippet", mine=True).execute().get("items", [])
        title = items[0]["snippet"]["title"] if items else "Linked (no channel found)"
        write_json(pdir(pid) / "channel.json", {"title": title})
        logger.info("Linked YouTube channel '%s' to profile '%s'", title, load_profile(pid)["name"])
    except Exception as e:
        logger.exception("OAuth callback failed")
        return JSONResponse({"detail": str(e)}, status_code=400)
    finally:
        pending_flow = None
        pending_profile = None
    return RedirectResponse("/")


@app.post("/auth/unlink")
def auth_unlink():
    pid = load_global()["active_profile"]
    (pdir(pid) / "token.json").unlink(missing_ok=True)
    (pdir(pid) / "channel.json").unlink(missing_ok=True)
    logger.info("Unlinked YouTube channel from profile '%s'", load_profile(pid)["name"])
    return {"ok": True}


if __name__ == "__main__":
    import socket
    import uvicorn
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 8000))
    except OSError:
        print("Shorts Autopilot is already running at http://localhost:8000")
        print("Stop it first with: pkill -f app.py")
        raise SystemExit(1)
    finally:
        probe.close()
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found on PATH, video stitching will fail")
    uvicorn.run(app, host="127.0.0.1", port=8000)
