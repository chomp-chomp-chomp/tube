import base64
import os
import re
import uuid
import threading
import time
from pathlib import Path
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, send_file, abort
)
from dotenv import load_dotenv
import yt_dlp

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))

PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "./cookies.txt"))
MAX_ACTIVE_DOWNLOADS = int(os.environ.get("MAX_DOWNLOADS", "3"))
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "500"))

# On startup: write cookies from COOKIES_BASE64 env var if present.
# This survives Render free-plan restarts (no persistent disk) because
# the env var is always available, while an uploaded file would be lost.
_cookies_b64 = os.environ.get("COOKIES_BASE64", "").strip()
if _cookies_b64:
    try:
        COOKIES_FILE.write_bytes(base64.b64decode(_cookies_b64))
    except Exception as _e:
        print(f"Warning: could not decode COOKIES_BASE64: {_e}")

# In-memory job tracker: job_id -> {"status", "progress", "filename", "error"}
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

# Semaphore caps concurrent downloads so the server isn't overwhelmed
_dl_semaphore = threading.Semaphore(MAX_ACTIVE_DOWNLOADS)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cookie_opts() -> dict:
    """Return cookiefile opt if a non-empty cookies.txt exists."""
    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0:
        return {"cookiefile": str(COOKIES_FILE)}
    return {}


def _sanitize(name: str) -> str:
    """Remove characters that are unsafe in filenames."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)


# ---------------------------------------------------------------------------
# Routes – auth
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == PASSWORD:
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "Incorrect password."
        time.sleep(1)  # slow brute-force attempts
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Routes – main
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/info", methods=["POST"])
@login_required
def video_info():
    """Return basic video metadata so the user can preview before downloading."""
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided."}), 400

    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            **_cookie_opts(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return jsonify({
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", ""),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/download", methods=["POST"])
@login_required
def start_download():
    """Kick off a background download job and return a job_id."""
    url = request.json.get("url", "").strip()
    fmt = request.json.get("format", "mp4")    # "mp4" or "mp3"
    quality = request.json.get("quality", "best")  # "best" | "1080" | "720" | "480"

    if not url:
        return jsonify({"error": "No URL provided."}), 400
    if fmt not in ("mp4", "mp3"):
        return jsonify({"error": "Invalid format."}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "progress": 0, "filename": None, "error": None}

    t = threading.Thread(target=_download_worker, args=(job_id, url, fmt, quality), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
@login_required
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    return jsonify(job)


@app.route("/file/<job_id>")
@login_required
def serve_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        abort(404)
    filepath = DOWNLOAD_DIR / job["filename"]
    if not filepath.exists():
        abort(404)
    return send_file(
        filepath,
        as_attachment=True,
        download_name=job["filename"],
    )


# ---------------------------------------------------------------------------
# Routes – settings
# ---------------------------------------------------------------------------

@app.route("/settings")
@login_required
def settings():
    has_cookies = COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0
    cookies_size = COOKIES_FILE.stat().st_size if has_cookies else 0
    msg = request.args.get("msg", "")
    env_cookies = bool(os.environ.get("COOKIES_BASE64", "").strip())
    return render_template(
        "settings.html",
        has_cookies=has_cookies,
        cookies_size=cookies_size,
        msg=msg,
        env_cookies=env_cookies,
        max_file_size_mb=MAX_FILE_SIZE_MB,
        max_downloads=MAX_ACTIVE_DOWNLOADS,
    )


@app.route("/settings/cookies", methods=["POST"])
@login_required
def upload_cookies():
    f = request.files.get("cookies")
    if not f or not f.filename:
        return redirect(url_for("settings", msg="no_file"))
    content = f.read()
    if len(content) > 10 * 1024 * 1024:  # 10 MB sanity limit for a cookie file
        return redirect(url_for("settings", msg="too_large"))
    COOKIES_FILE.write_bytes(content)
    return redirect(url_for("settings", msg="saved"))


@app.route("/settings/cookies/clear", methods=["POST"])
@login_required
def clear_cookies():
    try:
        COOKIES_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return redirect(url_for("settings", msg="cleared"))


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _make_progress_hook(job_id):
    def hook(d):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            if d["status"] == "downloading":
                pct = d.get("_percent_str", "0%").strip().replace("%", "")
                try:
                    job["progress"] = float(pct)
                except ValueError:
                    pass
                job["status"] = "downloading"
            elif d["status"] == "finished":
                job["progress"] = 99
                job["status"] = "processing"
    return hook


def _download_worker(job_id: str, url: str, fmt: str, quality: str):
    # Block here if too many downloads are already running.
    # The job stays in "queued" state (visible in the UI) until a slot opens.
    _dl_semaphore.acquire()
    try:
        with jobs_lock:
            jobs[job_id]["status"] = "starting"

        uid = job_id[:8]
        max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
        base_opts = {
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_make_progress_hook(job_id)],
            "max_filesize": max_bytes,
            **_cookie_opts(),
        }

        if fmt == "mp3":
            ydl_opts = {
                **base_opts,
                "format": "bestaudio/best",
                "outtmpl": str(DOWNLOAD_DIR / f"%(title)s [{uid}].%(ext)s"),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            }
        else:
            if quality == "best":
                fmt_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            else:
                fmt_str = (
                    f"bestvideo[height<={quality}][ext=mp4]"
                    f"+bestaudio[ext=m4a]/best[height<={quality}][ext=mp4]/best"
                )
            ydl_opts = {
                **base_opts,
                "format": fmt_str,
                "outtmpl": str(DOWNLOAD_DIR / f"%(title)s [{uid}].%(ext)s"),
                "merge_output_format": "mp4",
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = _sanitize(info.get("title", "video"))
            ext = "mp3" if fmt == "mp3" else "mp4"
            filename = f"{title} [{uid}].{ext}"

        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["filename"] = filename

    except Exception as exc:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(exc)
    finally:
        _dl_semaphore.release()


# ---------------------------------------------------------------------------
# Cleanup old jobs (runs every 30 min in background)
# ---------------------------------------------------------------------------

def _cleanup_jobs():
    """Remove completed/errored jobs and their files after 1 hour."""
    while True:
        time.sleep(1800)
        with jobs_lock:
            stale = [jid for jid, j in jobs.items()
                     if j["status"] in ("done", "error")]
        for jid in stale:
            with jobs_lock:
                job = jobs.pop(jid, None)
            if job and job.get("filename"):
                fp = DOWNLOAD_DIR / job["filename"]
                try:
                    fp.unlink(missing_ok=True)
                except OSError:
                    pass


threading.Thread(target=_cleanup_jobs, daemon=True).start()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=host, port=port, debug=debug)
