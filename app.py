import base64
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from collections import deque
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from google import genai
from google.auth.transport.requests import Request as GoogleRequest
from google.genai import types
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

GLOBAL_DEFAULTS = {
    "active_profile": "default",
    "storage_dir": "videos",
    "delete_after_publish": False,
    "keep_clips": False,
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.1:8b",
    "vision_model": "llama3.2-vision",
    "gemini_api_key": "",
    "veo_model": "veo-3.0-fast-generate-001",
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
    "upload_category_id": "24",
    "privacy_status": "private",
    "trending_query": "",
    "trending_days": 7,
    "category_id": "",
    "region_code": "US",
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

KIDS_VEO_SUFFIX = (
    " Child-friendly cartoon animation with cute non-human characters, soft bright colors, calm pacing, "
    "safe cheerful setting, nothing scary or dangerous, no realistic people, no logos, no flashing lights."
)

KIDS_VISION_PROMPT = """These are still frames from a short video intended for young children.
Mark it unsafe if ANY frame shows: realistic humans or children, violence, weapons, blood, injury, scary, creepy, or disturbing imagery, distorted or malformed faces or bodies, nudity or suggestive content, alcohol, tobacco, drugs, brand logos, readable text, or anything a parent would find inappropriate for a 4 year old.
Return JSON only: {"safe": true or false, "reason": "short explanation"}"""

KIDS_BANNED = re.compile(
    r"\b(kill\w*|blood\w*|bleed\w*|guns?|knife|knives|swords?|weapons?|bombs?|murder\w*|dead|die|dies|dying|death|"
    r"scary|terrif\w*|horror|creepy|nightmare\w*|sexy|kiss\w*|beer|wine|drunk|cigar\w*|smok\w*|drugs?|vape\w*|hate)\b",
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
stop_event = threading.Event()
runner = None
pending_flow = None
pending_profile = None
status = {"running": False, "stage": "idle"}


class Cancelled(Exception):
    pass


def check():
    if stop_event.is_set():
        raise Cancelled()


def set_stage(stage, log=True):
    status["stage"] = stage
    if log:
        logger.info("Stage: %s", stage)


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


def output_root(s):
    return resolve_storage(s["storage_dir"]) / "output"


def published_root(s):
    return resolve_storage(s["storage_dir"]) / "published"


def validate_storage(path_str):
    root = resolve_storage(path_str)
    try:
        (root / "output").mkdir(parents=True, exist_ok=True)
        (root / "published").mkdir(parents=True, exist_ok=True)
        probe = root / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise ValueError(f"Storage folder '{root}' is not writable: {e}")
    return root


def free_gb(path):
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def prepare_storage(s):
    root = validate_storage(s["storage_dir"])
    free = free_gb(root)
    if free is not None and free < MIN_FREE_GB:
        raise RuntimeError(f"Only {free:.1f} GB free at {root}, need at least {MIN_FREE_GB} GB")
    logger.info("Storage: %s (%.1f GB free)", root, free or 0)


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
            "views": int(i.get("statistics", {}).get("viewCount", 0)),
        }
        for i in items
    ]


def fetch_trending(s):
    yt = youtube_client(s["profile_id"])
    count = max(1, min(int(s["trending_count"]), 50))
    region = s["region_code"] or "US"
    query = s["trending_query"].strip()
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
        if s["category_id"]:
            params["videoCategoryId"] = s["category_id"]
        ids = [i["id"]["videoId"] for i in yt.search().list(**params).execute().get("items", []) if i.get("id", {}).get("videoId")]
        items = yt.videos().list(part="snippet,statistics", id=",".join(ids)).execute().get("items", []) if ids else []
        logger.info("Search '%s' returned %d videos from the last %s days (region %s)", query, len(items), s["trending_days"], region)
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
    return map_videos(items)


def ollama_json(s, prompt, temperature=0.9, model=None, images=None):
    url = f"{s['ollama_url'].rstrip('/')}/api/generate"
    model = model or s["ollama_model"]
    payload = {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {"temperature": temperature},
    }
    if images:
        payload["images"] = images
    started = datetime.now()
    r = requests.post(url, json=payload, timeout=900)
    if r.status_code == 404:
        raise RuntimeError(f"Ollama model '{model}' not found. Run: ollama pull {model}")
    r.raise_for_status()
    text = r.json().get("response", "")
    logger.debug("Ollama %s responded in %.1fs: %s", model, (datetime.now() - started).total_seconds(), text[:2000])
    return json.loads(text)


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
Identify the underlying topic and why it appeals to viewers. Then write a completely ORIGINAL {n * clip}-second YouTube Short.
Do not reuse the source's title, script, characters, jokes, branding, or channel identity.
Do not depict real people, celebrities, brands, logos, or copyrighted characters. Invent new characters.

The Short has exactly {n} scenes of {clip} seconds each. Each scene has:
"visual": a detailed, self-contained cinematic shot description for an AI video model (subject, setting, action, camera, lighting). Repeat key character and setting details in every scene so they stay consistent.
"narration": one spoken line of at most {words} words.

Scene 1 must hook the viewer in the first 2 seconds. The final scene must deliver a payoff.

Return JSON only in this shape:
{{"title": "under 70 characters", "style": "one sentence visual style applied to every scene", "description": "2 to 3 sentence YouTube description", "scenes": [{{"visual": "...", "narration": "..."}}]}}"""
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
            data["description"] = str(data.get("description", ""))
            logger.info("Script ready: '%s' (%d scenes)", data["title"], n)
            return data
        except Cancelled:
            raise
        except Exception as e:
            last = e
            logger.warning("Script attempt %d failed: %s", attempt, e)
    raise RuntimeError(f"Could not generate a valid script: {last}")


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
{niche_line}{kids_line}
Title: {script['title']}
Description: {script['description']}
Narration: {narration}

Return JSON only: {{"tags": ["15 to 25 tags, most specific first"]}}"""
    for attempt in range(1, 3):
        check()
        try:
            data = ollama_json(s, prompt)
            raw = data.get("tags", [])
            if isinstance(raw, str):
                raw = raw.split(",")
            tags = clean_tags(raw)
            if tags:
                return tags
        except Cancelled:
            raise
        except Exception as e:
            logger.warning("Tag attempt %d failed: %s", attempt, e)
    logger.warning("No tags generated, uploading without tags")
    return []


def kids_rule_issues(script):
    issues = []
    texts = [script["title"], script["description"], script["style"]]
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
    prompt = f"""You are a strict compliance reviewer for YouTube videos that are made for kids, applying COPPA and YouTube's quality principles for kids content.
Check this Short against every rule below. Reject it if any rule is broken or anything could upset, endanger, or mislead a young child.

{KIDS_RULES}
Title: {script['title']}
Description: {script['description']}
Visual style: {script['style']}
Scenes:
{scenes}

Return JSON only: {{"pass": true or false, "issues": ["each specific problem"]}}"""
    try:
        data = ollama_json(s, prompt, temperature=0.1)
    except Cancelled:
        raise
    except Exception as e:
        return False, [f"compliance reviewer error: {e}"]
    issues = [str(i) for i in data.get("issues", []) if str(i).strip()] if isinstance(data.get("issues"), list) else []
    return truthy(data.get("pass")), issues


def produce_script(s, src):
    kids = bool(s["made_for_kids"])
    feedback = []
    attempts = 4 if kids else 1
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
        ok, llm_issues = kids_llm_review(s, script)
        if not ok:
            issues += llm_issues or ["reviewer rejected the script without details"]
        if not issues:
            logger.info("Kids compliance review passed on attempt %d", attempt)
            return script, tags, {"kids_checks": True, "script_review": "passed", "attempts": attempt, "dropped_tags": dropped}
        logger.warning("Kids compliance review failed (attempt %d/%d): %s", attempt, attempts, "; ".join(issues))
        feedback = issues
    raise RuntimeError(f"Script failed kids compliance review after {attempts} attempts")


def kids_vision_check(s, clip_path):
    model = s["vision_model"].strip()
    if not model:
        raise RuntimeError("Made for kids requires a vision model in Settings to check generated clips")
    clip = int(s["clip_seconds"])
    images = []
    for idx, t in enumerate((0.5, clip / 2, max(0.5, clip - 0.5))):
        frame = clip_path.with_name(f"{clip_path.stem}_f{idx}.jpg")
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t), "-i", str(clip_path), "-frames:v", "1", "-vf", "scale=512:-2", str(frame)],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not frame.exists():
            raise RuntimeError(f"Could not extract frame at {t}s for vision check")
        images.append(base64.b64encode(frame.read_bytes()).decode())
        frame.unlink(missing_ok=True)
    data = ollama_json(s, KIDS_VISION_PROMPT, temperature=0.1, model=model, images=images)
    if not truthy(data.get("safe")):
        raise ValueError(f"Vision check rejected {clip_path.name}: {data.get('reason', 'no reason given')}")
    logger.info("Vision check passed for %s", clip_path.name)


def generate_clip(client, s, prompt, path):
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
            "containsSyntheticMedia": True,
        },
    }
    logger.info("Uploading to %s as category %s, made for kids: %s", profile_channel(s["profile_id"]), body["snippet"]["categoryId"], kids)
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
    if not s["gemini_api_key"]:
        raise RuntimeError("Gemini API key is not set in Settings")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg was not found on PATH")
    kids = bool(s["made_for_kids"])
    if kids and not s["vision_model"].strip():
        raise RuntimeError("Made for kids requires a vision model in Settings (e.g. llama3.2-vision)")
    prepare_storage(s)
    logger.info("Profile: %s | Niche: %s | Made for kids: %s | Upload category: %s", s["name"], s["channel_niche"] or "none", kids, s["upload_category_id"])
    if kids:
        logger.info("Kids compliance active: script rules, word filters, LLM review, frame vision checks, made-for-kids flag%s", ", manual review hold" if s["kids_manual_review"] else "")
    set_stage("fetching trending videos")
    trending = fetch_trending(s)
    state = load_state()
    candidates = [v for v in sorted(trending, key=lambda v: -v["views"]) if v["id"] not in state["processed"]]
    if not candidates:
        logger.warning("No unprocessed trending videos, checking again in 30 minutes")
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
            "source_id": src["id"],
            "source_title": src["title"],
            "status": "running",
            "location": str(job_dir),
        }] + st["history"])[:200]
        save_state(st)
    logger.info("Job %s: source %s '%s' (%s views)", job_id, src["id"], src["title"], f"{src['views']:,}")
    logger.info("Working folder: %s", job_dir)
    compliance = {}
    finalized = False
    try:
        script, tags, compliance = produce_script(s, src)
        update_history(job_id, title=script["title"])
        write_json(job_dir / "script.json", {"profile": s["name"], "source": src, "script": script, "tags": tags})
        client = genai.Client(api_key=s["gemini_api_key"])
        orientation = "Vertical" if s["aspect_ratio"] == "9:16" else "Widescreen"
        suffix = KIDS_VEO_SUFFIX if kids else ""
        clips = []
        vision_log = []
        n = len(script["scenes"])
        for i, scene in enumerate(script["scenes"], 1):
            check()
            set_stage(f"generating clip {i}/{n}")
            path = job_dir / f"clip_{i:02d}.mp4"
            prompt = (
                f"{scene['visual']} Style: {script['style']}. {orientation} short-form video. "
                f"A narrator's voiceover says: \"{scene['narration']}\" "
                f"No on-screen text, captions, or subtitles.{suffix}"
            )
            for attempt in range(1, 4):
                try:
                    generate_clip(client, s, prompt, path)
                    if kids:
                        set_stage(f"vision check clip {i}/{n}")
                        kids_vision_check(s, path)
                        vision_log.append({"clip": i, "attempt": attempt, "result": "passed"})
                    break
                except Cancelled:
                    raise
                except Exception as e:
                    logger.warning("Clip %d attempt %d failed: %s", i, attempt, e)
                    if kids:
                        vision_log.append({"clip": i, "attempt": attempt, "result": str(e)[:300]})
                    path.unlink(missing_ok=True)
                    if attempt == 3:
                        raise
                    stop_event.wait(20)
            clips.append(path)
        if kids:
            compliance["vision_checks"] = vision_log
        set_stage("stitching video")
        final = job_dir / "final.mp4"
        concat_clips(clips, final, s["aspect_ratio"])
        set_stage("uploading to YouTube")
        vid, held = upload_video(s, final, script, tags)
        compliance["made_for_kids_flag"] = kids
        compliance["synthetic_media_flag"] = True
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
            for c in clips:
                c.unlink(missing_ok=True)
        set_stage("moving files")
        location = finish_files(s, job_dir, job_id)
        update_history(job_id, status="review" if held else "published", youtube_id=vid, location=location)
        set_stage("job complete")
    except Cancelled:
        update_history(job_id, status="cancelled")
        logger.warning("Job %s cancelled", job_id)
        raise
    except Exception as e:
        update_history(job_id, status="failed", error=str(e)[:300])
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
        "profile": s["name"],
        "made_for_kids": s["made_for_kids"],
        "published_today": published_today(s["profile_id"]),
        "videos_per_day": s["videos_per_day"],
        "channel": profile_channel(s["profile_id"]),
        "storage": str(root),
        "storage_free_gb": round(free, 1) if free is not None else None,
        "delete_after_publish": s["delete_after_publish"],
        "history": st["history"][:25],
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
            root = validate_storage(g["storage_dir"])
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
    import uvicorn
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found on PATH, video stitching will fail")
    uvicorn.run(app, host="127.0.0.1", port=8000)
