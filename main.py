import os
import shutil
import certifi
import asyncio
import tempfile
import instaloader
import queue
import uuid
import threading
import re
from fastapi import FastAPI, Query, HTTPException, BackgroundTasks, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from yt_dlp import YoutubeDL, DownloadError
from typing import Optional, Dict, Any, Union

# Set SSL Certificate for secure connections
os.environ["SSL_CERT_FILE"] = certifi.where()

app = FastAPI(title="Video Downloader API")

# Mount a static directory for serving the frontend HTML
app.mount("/static", StaticFiles(directory="static", html=True), name="static")

# Temporary directory for all downloads
TEMP_DIR = tempfile.mkdtemp()
print(f"Temporary directory created at: {TEMP_DIR}")

# Use an environment variable for the ffmpeg path for better portability and security
FFMPEG_PATH = "/Applications/ffmpeg/ffmpeg"

# A dictionary to hold progress queues for each unique download session
PROGRESS_QUEUES: Dict[str, queue.Queue] = {}
DOWNLOAD_FILES: Dict[str, str] = {}
DOWNLOAD_LOCKS: Dict[str, threading.Lock] = {}

# Instaloader setup (will be instantiated per-download for isolation)
L = None

@app.on_event("startup")
async def startup_event():
    """Startup event to create Instaloader instance"""
    global L
    # Instantiate Instaloader to use the session file for authentication
    L = instaloader.Instaloader(
        dirname_pattern=os.path.join(TEMP_DIR, '{profile}-{download_id}'),
        download_videos=True,
        download_pictures=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern=''
    )

    # Load the session from the file. The filename should match what you uploaded to Render.
    try:
        L.load_session_from_file('iwillfollow1million', 'iwillfollow1million.session')
        print("Instaloader session loaded successfully!")
    except FileNotFoundError:
        print("Instaloader session file not found. Instagram downloads may fail.")
    except Exception as e:
        print(f"Error loading Instaloader session: {e}")


@app.on_event("shutdown")
def cleanup_temp_dir():
    """Server band hone par temporary directory aur uske content ko saaf kare."""
    print(f"Cleaning up temporary directory: {TEMP_DIR}")
    shutil.rmtree(TEMP_DIR, ignore_errors=True)

@app.get("/", include_in_schema=False)
def root():
    """Root URL ko static frontend par redirect kare."""
    return RedirectResponse("/static/index.html")

def detect_platform(url: str) -> Optional[str]:
    """URL ke hisaab se platform detect kare."""
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    elif "instagram.com" in url:
        return "instagram"
    return None

def run_sync_in_threadpool(func, *args, **kwargs):
    """Sync function ko async context mein chalane ke liye."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, func, *args, **kwargs)

def ytdlp_progress_hook(d: Dict[str, Any], download_id: str):
    """
    yt-dlp download progress ke liye hook.
    Har download ke liye uski alag queue mein progress message daalta hai.
    """
    download_queue = PROGRESS_QUEUES.get(download_id)
    if not download_queue:
        return

    if d['status'] == 'downloading':
        p = d.get('_percent_str', 'N/A')
        p = re.sub(r'\x1b\[[0-9;]*[mGKHF]', '', p)
        total = d.get('_total_bytes_str', 'N/A')
        speed = d.get('_speed_str', 'N/A')
        total = total.replace('MiB', 'MB')
        speed = speed.replace('MiB', 'MB')
        message = f"Downloading: {p} of {total} "
        download_queue.put(message)
    elif d['status'] == 'finished':
        download_queue.put("Merging video and audio...")

@app.get("/info", summary="Video ki jaankari (info) fetch kare.")
async def get_video_info(url: str = Query(..., description="Video URL")):
    """
    Video URL se uska title, thumbnail, duration aur available formats fetch kare.
    """
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported URL.")

    options = {'quiet': True, 'skip_download': True, 'noplaylist': True}
    
    try:
        info_dict = await run_sync_in_threadpool(
            lambda: YoutubeDL(options).extract_info(url, download=False)
        )
    except DownloadError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Video info fetch karne mein fail: {e}")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Server error: {e}")

    if not info_dict:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch video info.")
    
    available_formats = []

    if 'formats' in info_dict:
        for f in info_dict['formats']:
            ext = f.get('ext', '').lower()
            if 'acodec' in f and 'vcodec' in f:  # Combined video/audio
                label = f"MP4 Video ({f.get('height', 'unknown')}p)" if f.get('height') else "MP4 Video (Best Quality)"
                filesize = f.get('filesize') or f.get('filesize_approx')
                available_formats.append({
                    "id": f.get('format_id', 'best'),
                    "label": label,
                    "ext": ext,
                    "filesize": filesize
                })
            elif 'acodec' in f and f['acodec'] != 'none':  # Audio only
                label = f"M4A Audio ({f.get('abr', 'unknown')}kbps)"
                filesize = f.get('filesize') or f.get('filesize_approx')
                available_formats.append({
                    "id": f.get('format_id', 'bestaudio'),
                    "label": label,
                    "ext": 'm4a',
                    "filesize": filesize
                })
    else:
        # Fallback for simple cases, e.g., Instagram reels
        available_formats.append({
            "id": "best",
            "label": "MP4 Video (Best Quality)",
            "ext": "mp4",
            "filesize": info_dict.get('filesize')
        })

    return {
        "title": info_dict.get("title"),
        "thumbnail": info_dict.get("thumbnail"),
        "duration": info_dict.get("duration"),
        "platform": platform,
        "formats": available_formats
    }

def download_with_yt_dlp(url: str, format_id: str, download_id: str, temp_dir_path: str):
    """Video ko download karne ka sync function."""
    platform = detect_platform(url)

    options = {
        'outtmpl': os.path.join(temp_dir_path, "%(title)s.%(ext)s"),
        'quiet': False,
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'ffmpeg_location': FFMPEG_PATH,
        'progress_hooks': [lambda d: ytdlp_progress_hook(d, download_id)],
    }
    
    if platform == "youtube":
        options['format'] = format_id if format_id else 'bestvideo+bestaudio/best'
    elif platform == "instagram":
        options['format'] = 'best'
    else:
        options['format'] = 'best'
    
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            PROGRESS_QUEUES[download_id].put("Download complete!")
            DOWNLOAD_FILES[download_id] = filename
    except Exception as e:
        error_message = f"ERROR: Download failed. {e}"
        PROGRESS_QUEUES[download_id].put(error_message)
        raise

@app.get("/start-download", summary="Video download shuru kare.")
async def start_download(
    url: str = Query(..., description="Video URL"),
    format_id: str = Query(..., description="Selected format ID")
):
    """
    Download process ko shuru kare aur ek unique download_id return kare.
    """
    download_id = str(uuid.uuid4())
    
    download_temp_dir = os.path.join(TEMP_DIR, download_id)
    os.makedirs(download_temp_dir, exist_ok=True)

    PROGRESS_QUEUES[download_id] = queue.Queue()
    DOWNLOAD_LOCKS[download_id] = threading.Lock()

    asyncio.get_event_loop().run_in_executor(
        None, download_with_yt_dlp, url, format_id, download_id, download_temp_dir
    )

    return {"download_id": download_id, "message": "Download process started."}

async def progress_streamer(download_id: str):
    """Real-time progress updates ko server-sent events ke roop mein stream kare."""
    download_queue = PROGRESS_QUEUES.get(download_id)
    if not download_queue:
        yield "data: ERROR: Invalid download ID.\n\n"
        return

    while True:
        try:
            message = download_queue.get(timeout=300)
            yield f"data: {message}\n\n"
            if message.startswith("ERROR:") or message == "Download complete!":
                break
        except queue.Empty:
            yield "data: KEEP_ALIVE\n\n"
        await asyncio.sleep(1)

    del PROGRESS_QUEUES[download_id]
    if download_id in DOWNLOAD_LOCKS:
        del DOWNLOAD_LOCKS[download_id]

@app.get("/stream-progress/{download_id}", summary="Download progress stream kare.")
async def stream_progress_updates(download_id: str):
    """Streams real-time progress updates to the client for a specific download ID."""
    return StreamingResponse(progress_streamer(download_id), media_type="text/event-stream")

@app.get("/get-file/{download_id}", summary="Download ki hui file return kare.")
async def get_downloaded_file(download_id: str, background_tasks: BackgroundTasks):
    """
    Download complete hone par file ko return kare aur baad mein use delete kare.
    """
    if download_id not in DOWNLOAD_FILES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found or download not complete.")
    
    filepath = DOWNLOAD_FILES.get(download_id)
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File path not valid.")

    download_temp_dir = os.path.dirname(filepath)
    background_tasks.add_task(shutil.rmtree, download_temp_dir, ignore_errors=True)
    
    del DOWNLOAD_FILES[download_id]

    return FileResponse(
        filepath,
        media_type="application/octet-stream",
        filename=os.path.basename(filepath)
    )