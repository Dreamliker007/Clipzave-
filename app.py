import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse
import zipfile
import shutil

from flask import Flask, after_this_request, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL
import instaloader

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
    path = parsed.path
    return bool(
        re.match(r"^/(reel|p|tv)/[A-Za-z0-9_-]+/?$", path)
        or re.match(r"^/stories/[A-Za-z0-9._]+/[0-9]+/?$", path)
    )


def public_post(url: str):
    shortcode = urlparse(url).path.strip("/").split("/")[-1]
    loader = instaloader.Instaloader(quiet=True)
    return instaloader.Post.from_shortcode(loader.context, shortcode)


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/api/preview")
def preview():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not valid_instagram_url(url):
        return jsonify(error="Paste a valid Instagram Reel, post, or Story link."), 400

    try:
        options = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "socket_timeout": 20}
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        is_story = urlparse(url).path.startswith("/stories/")
        return jsonify({
            "title": info.get("title") or ("Instagram Story" if is_story else "Instagram media"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "media_type": "Story" if is_story else ("Video" if info.get("duration") or info.get("is_live") else "Instagram post"),
            "items": 1
        })
    except Exception:
        # Public photo posts and carousels may not expose a video stream.
        if re.match(r"^/p/", urlparse(url).path):
            try:
                post = public_post(url)
                if post.is_video:
                    return jsonify({"title": post.caption or "Instagram video", "thumbnail": post.url, "duration": None, "media_type": "Video", "items": 1})
                images = list(post.get_sidecar_nodes()) if post.typename == "GraphSidecar" else [post]
                first_image = images[0].display_url if images else None
                return jsonify({"title": (post.caption or "Instagram photo post")[:120], "thumbnail": first_image, "duration": None, "media_type": "Photo carousel" if len(images) > 1 else "Photo", "items": len(images)})
            except Exception:
                pass
        if urlparse(url).path.startswith("/stories/"):
            return jsonify(error="Could not access this Story. Instagram often requires an authorized session for Stories; private, expired, or login-protected Stories cannot be fetched here."), 422
        return jsonify(error="Could not preview this link. Instagram may be rate-limiting it or the post may not be publicly accessible."), 422


@app.post("/api/download")
def download():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not valid_instagram_url(url):
        return jsonify(error="Enter a valid Instagram Reel, post, or Story link."), 400

    temp_dir = tempfile.mkdtemp(prefix="clipzave_")
    output_template = str(Path(temp_dir) / "%(title).80s-%(id)s.%(ext)s")
    options = {
        "outtmpl": output_template, "format": "best[ext=mp4]/best", "noplaylist": True,
        "quiet": True, "no_warnings": True, "restrictfilenames": True,
        "max_filesize": 100 * 1024 * 1024, "socket_timeout": 20,
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
        @after_this_request
        def cleanup_video_files(response):
            shutil.rmtree(temp_dir, ignore_errors=True)
            return response
        return send_file(filepath, as_attachment=True, download_name=filepath.name, mimetype="application/octet-stream", max_age=0)
    except Exception as video_error:
        if re.search(r"no video|does not contain a video", str(video_error), re.I) and re.match(r"^/p/", urlparse(url).path):
            image_dir = tempfile.mkdtemp(prefix="clipzave_images_")
            try:
                shortcode = urlparse(url).path.strip("/").split("/")[1]
                loader = instaloader.Instaloader(
                    dirname_pattern=image_dir, filename_pattern="{shortcode}_{mediaid}",
                    download_pictures=True, download_videos=False, download_video_thumbnails=False,
                    save_metadata=False, compress_json=False, post_metadata_txt_pattern="", quiet=True,
                )
                post = instaloader.Post.from_shortcode(loader.context, shortcode)
                if not post.is_video:
                    loader.download_post(post, target="post")
                images = [p for p in Path(image_dir).rglob("*") if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
                if not images:
                    raise RuntimeError("No public images were returned for this post.")
                archive = Path(image_dir) / "clipzave-images.zip"
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                    for image_path in images:
                        zf.write(image_path, arcname=image_path.name)
                @after_this_request
                def cleanup_image_files(response):
                    shutil.rmtree(image_dir, ignore_errors=True)
                    return response
                return send_file(archive, as_attachment=True, download_name="clipzave-images.zip", mimetype="application/zip", max_age=0)
            except Exception:
                shutil.rmtree(image_dir, ignore_errors=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        if urlparse(url).path.startswith("/stories/"):
            return jsonify(error="Could not download this Story. Instagram may require an authorized session, or the Story may be private, expired, or restricted. ClipZave does not bypass Instagram access controls."), 422
        return jsonify(error="Could not fetch this public media. Instagram may require access or may be rate-limiting the request. Only publicly accessible content you own or have permission to save is supported."), 422


@app.get("/health")
def health():
    return {"status": "ok", "app": "ClipZave"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
