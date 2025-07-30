import os
import shutil
import certifi
import tempfile
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from yt_dlp import YoutubeDL

os.environ["SSL_CERT_FILE"] = certifi.where()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static", html=True), name="static")

DOWNLOAD_DIR = "download"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

@app.get("/")
def root():
    return RedirectResponse("/static/index.html")

def detect_platform(url: str):
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    elif "instagram.com" in url:
        return "instagram"
    else:
        return None

def get_temp_filename(info, format: str):
    base = info.get('title', 'download').replace(' ', '_')
    ext = 'mp3' if format == 'audio' else 'mp4'
    return f"{base}.{ext}"

@app.get("/info")
def get_video_info(url: str):
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(400, detail="Unsupported URL")

    options = {
        'quiet': True,
        'skip_download': True,
        'noplaylist': True,
    }

    with YoutubeDL(options) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "filesize": info.get("filesize") or info.get("filesize_approx"),
                "platform": platform
            }
        except Exception as e:
            raise HTTPException(500, detail=f"Failed to fetch video info: {str(e)}")

@app.get("/download")
def download_video(url: str = Query(...)):
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(status_code=400, detail="Unsupported URL")

    filename = None
    options = {
        'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
        'quiet': True,
        'noplaylist': True,
        'format': 'best[ext=mp4]/best',
    }

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

    if not filename or not os.path.exists(filename):
        raise HTTPException(status_code=500, detail="File not found after download.")

    return FileResponse(
        filename,
        media_type="application/octet-stream",
        filename=os.path.basename(filename),
    )