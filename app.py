import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

ALLOWED_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
URL_RE = re.compile(r"^https?://", re.I)


def valid_instagram_url(value: str) -> bool:
    if not value or not URL_RE.match(value):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        return False
    # Only public post/reel URLs; private profiles and story endpoints are not supported.
    return bool(re.match(r"^/(reel|p|tv)/[A-Za-z0-9_-]+/?$", parsed.path))


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/api/download")
def download():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()

    if not valid_instagram_url(url):
        return jsonify(error="Enter a valid public Instagram Reel or post link. Stories and private content are not supported."), 400

    temp_dir = tempfile.mkdtemp(prefix="clipzave_")
    output_template = str(Path(temp_dir) / "%(title).80s-%(id)s.%(ext)s")
    options = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "max_filesize": 100 * 1024 * 1024,
        "socket_timeout": 20,
    }

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = Path(ydl.prepare_filename(info))
        if not filepath.exists():
            candidates = list(Path(temp_dir).glob("*"))
            if not candidates:
                raise RuntimeError("The media file was not created.")
            filepath = candidates[0]
        return send_file(
            filepath,
            as_attachment=True,
            download_name=filepath.name,
            mimetype="application/octet-stream",
            max_age=0,
        )
    except Exception:
        return jsonify(error="Could not fetch this media. It may be private, unavailable, age-restricted, or blocked by Instagram. Try a public post or Reel you own or have permission to download."), 422


@app.get("/health")
def health():
    return {"status": "ok", "app": "ClipZave"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
