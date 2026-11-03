# ClipZave

A small Flask web app for downloading publicly accessible Instagram Reels and posts that you own or have permission to save.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:10000.

## Render

- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Health check: `/health`

## Scope and responsible use

Only public Reel/post URLs are accepted. Private posts, Stories, login-protected media and restricted content are not supported. Use this only for content you created or have permission to download. Instagram and Meta are trademarks of their respective owners; ClipZave is independent and unaffiliated.
