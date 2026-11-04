import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from http.cookiejar import MozillaCookieJar
import zipfile
import shutil
import hmac

from flask import Flask, after_this_request, jsonify, make_response, render_template, request, send_file
from yt_dlp import YoutubeDL
import instaloader

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

ALLOWED_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
URL_RE = re.compile(r"^https?://", re.I)


def instagram_configuration_error():
    cookies_file = os.environ.get("INSTAGRAM_COOKIES_FILE", "").strip()
    username = os.environ.get("INSTAGRAM_USERNAME", "").strip()
    if bool(username) != bool(cookies_file):
        return "Set both INSTAGRAM_USERNAME and INSTAGRAM_COOKIES_FILE to enable Instagram session access."
    if cookies_file and not Path(cookies_file).is_file():
        return "The configured Instagram cookies file is missing from the server."
    if cookies_file:
        try:
            jar = MozillaCookieJar(cookies_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            cookie_names = {
                cookie.name for cookie in jar
                if cookie.domain.lstrip(".").lower() == "instagram.com"
                or cookie.domain.lstrip(".").lower().endswith(".instagram.com")
            }
            if not {"sessionid", "csrftoken"}.issubset(cookie_names):
                return "The Instagram cookies file must include a current sessionid and csrftoken."
        except (OSError, ValueError):
            return "The configured Instagram cookies file is invalid."
    return None


def youtube_dl_options(options):
    cookies_file = os.environ.get("INSTAGRAM_COOKIES_FILE", "").strip()
    if cookies_file:
        options["cookiefile"] = cookies_file
    return options


def create_instaloader(**options):
    loader = instaloader.Instaloader(**options)
    username = os.environ.get("INSTAGRAM_USERNAME", "").strip()
    cookies_file = os.environ.get("INSTAGRAM_COOKIES_FILE", "").strip()
    if username and cookies_file:
        jar = MozillaCookieJar(cookies_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        cookies = {
            cookie.name: cookie.value for cookie in jar
            if cookie.domain.lstrip(".").lower() == "instagram.com"
            or cookie.domain.lstrip(".").lower().endswith(".instagram.com")
        }
        loader.load_session(username, cookies)
    return loader


@app.before_request
def require_access_password():
    if request.endpoint == "health":
        return None

    expected_password = os.environ.get("CLIPZAVE_ACCESS_PASSWORD", "")
    if not expected_password:
        return jsonify(error="ClipZave access password is not configured."), 503

    auth = request.authorization
    if auth and auth.username == "clipzave" and hmac.compare_digest(auth.password or "", expected_password):
        return None

    response = make_response("Authentication required.", 401)
    response.headers["WWW-Authenticate"] = 'Basic realm="ClipZave", charset="UTF-8"'
    return response


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
    loader = create_instaloader(quiet=True)
    return instaloader.Post.from_shortcode(loader.context, shortcode)


def photo_nodes(post):
    nodes = list(post.get_sidecar_nodes()) if post.typename == "GraphSidecar" else [post]
    return [node for node in nodes if not node.is_video]


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/api/preview")
def preview():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not valid_instagram_url(url):
        return jsonify(error="Paste a valid Instagram Reel, post, or Story link."), 400
    config_error = instagram_configuration_error()
    if config_error:
        return jsonify(error=config_error), 503

    try:
        options = youtube_dl_options({"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "socket_timeout": 20})
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        is_story = urlparse(url).path.startswith("/stories/")
        if re.match(r"^/p/", urlparse(url).path):
            try:
                post = public_post(url)
                photos = photo_nodes(post)
                if len(photos) > 1:
                    return jsonify({
                        "title": (post.caption or "Instagram photo carousel")[:120],
                        "thumbnail": photos[0].display_url,
                        "duration": None,
                        "media_type": "Photo carousel",
                        "items": len(photos),
                        "images": [{"index": index, "url": photo.display_url} for index, photo in enumerate(photos)],
                    })
            except Exception:
                pass
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
                photos = photo_nodes(post)
                first_image = photos[0].display_url if photos else None
                result = {"title": (post.caption or "Instagram photo post")[:120], "thumbnail": first_image, "duration": None, "media_type": "Photo carousel" if len(photos) > 1 else "Photo", "items": len(photos)}
                if len(photos) > 1:
                    result["images"] = [{"index": index, "url": photo.display_url} for index, photo in enumerate(photos)]
                return jsonify(result)
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
    config_error = instagram_configuration_error()
    if config_error:
        return jsonify(error=config_error), 503

    selected_images = payload.get("selected_images")
    if selected_images is not None:
        if (
            not isinstance(selected_images, list)
            or not selected_images
            or any(type(index) is not int for index in selected_images)
            or len(set(selected_images)) != len(selected_images)
        ):
            return jsonify(error="Select at least one photo to download."), 400
        if not re.match(r"^/p/", urlparse(url).path):
            return jsonify(error="Photo selection is only available for Instagram photo carousels."), 400
        try:
            post = public_post(url)
            photos = photo_nodes(post)
        except Exception:
            return jsonify(error="Could not access the public photos in this carousel."), 422
        if len(photos) < 2:
            return jsonify(error="This post does not contain a photo carousel."), 400
        if any(index < 0 or index >= len(photos) for index in selected_images):
            return jsonify(error="The carousel changed. Preview it again and choose the photos to download."), 400

        image_dir = tempfile.mkdtemp(prefix="clipzave_selected_images_")
        try:
            loader = create_instaloader(
                dirname_pattern=image_dir, filename_pattern="{shortcode}_{mediaid}",
                download_pictures=True, download_videos=False, download_video_thumbnails=False,
                save_metadata=False, compress_json=False, post_metadata_txt_pattern="", quiet=True,
            )
            loader.download_post(post, target="post")
            downloaded_images = [
                path for path in Path(image_dir).rglob("*")
                if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
            archive = Path(image_dir) / "clipzave-selected-photos.zip"
            selected_paths = []
            for index in selected_images:
                media_id = str(photos[index].mediaid)
                matches = [path for path in downloaded_images if path.stem.endswith("_" + media_id)]
                if not matches:
                    raise RuntimeError("A selected carousel photo was not downloaded.")
                selected_paths.append((index, matches[0]))
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                for index, image_path in selected_paths:
                    zf.write(image_path, arcname=f"photo-{index + 1}{image_path.suffix.lower()}")

            @after_this_request
            def cleanup_selected_images(response):
                shutil.rmtree(image_dir, ignore_errors=True)
                return response

            return send_file(archive, as_attachment=True, download_name="clipzave-selected-photos.zip", mimetype="application/zip", max_age=0)
        except Exception:
            shutil.rmtree(image_dir, ignore_errors=True)
            return jsonify(error="Could not prepare the selected public photos. Instagram may be rate-limiting this request."), 422

    temp_dir = tempfile.mkdtemp(prefix="clipzave_")
    output_template = str(Path(temp_dir) / "%(title).80s-%(id)s.%(ext)s")
    options = youtube_dl_options({
        "outtmpl": output_template, "format": "best[ext=mp4]/best", "noplaylist": True,
        "quiet": True, "no_warnings": True, "restrictfilenames": True,
        "max_filesize": 100 * 1024 * 1024, "socket_timeout": 20,
    })
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
                loader = create_instaloader(
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
