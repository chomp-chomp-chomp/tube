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

# In-memory job tracker: job_id -> {"status", "progress", "filename", "error"}
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


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
# Routes
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
        ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
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
    fmt = request.json.get("format", "mp4")   # "mp4" or "mp3"
    quality = request.json.get("quality", "best")  # "best" | "1080" | "720" | "480" | "360"

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


def _sanitize(name: str) -> str:
    """Remove characters that are unsafe in filenames."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def _download_worker(job_id: str, url: str, fmt: str, quality: str):
    try:
        with jobs_lock:
            jobs[job_id]["status"] = "starting"

        # Build a unique output filename template
        uid = job_id[:8]

        if fmt == "mp3":
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": str(DOWNLOAD_DIR / f"%(title)s [{uid}].%(ext)s"),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [_make_progress_hook(job_id)],
            }
        else:
            # MP4 video
            if quality == "best":
                fmt_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            else:
                fmt_str = (
                    f"bestvideo[height<={quality}][ext=mp4]"
                    f"+bestaudio[ext=m4a]/best[height<={quality}][ext=mp4]/best"
                )
            ydl_opts = {
                "format": fmt_str,
                "outtmpl": str(DOWNLOAD_DIR / f"%(title)s [{uid}].%(ext)s"),
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [_make_progress_hook(job_id)],
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


# ---------------------------------------------------------------------------
# Cleanup old jobs (runs every 30 min in background)
# ---------------------------------------------------------------------------

def _cleanup_jobs():
    """Remove completed/errored jobs older than 1 hour and their files."""
    while True:
        time.sleep(1800)
        cutoff = time.time() - 3600
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
