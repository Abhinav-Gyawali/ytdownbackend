from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from urllib.parse import quote
import yt_dlp
from pathlib import Path
import os
import uuid
import asyncio
import subprocess
import zipfile
from typing import Optional
import json
import mimetypes
from collections import deque
import functools

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
COOKIES_FILE = os.path.join(os.getcwd(), "cookies.txt")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Store download progress
download_progress = {}

class FormatRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str

class ProgressResponse(BaseModel):
    progress: float
    eta: int
    speed: float

class SSEEvent:
    EVENTS = deque()
    
    @staticmethod
    def add_event(event: ProgressResponse):
        SSEEvent.EVENTS.append(event)

    @staticmethod
    def get_event():
        if len(SSEEvent.EVENTS) > 0:
            return SSEEvent.EVENTS.popleft()
        return None
        
    @staticmethod
    def count():
        return len(SSEEvent.EVENTS)

def get_ydl_opts(additional_opts: dict = None) -> dict:
    """Get yt-dlp options with cookie handling"""
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "cookiefile": "cookies.txt",
    }
    if additional_opts:
        base_opts.update(additional_opts)
    return base_opts


@app.post("/api/formats")
async def get_formats(request: FormatRequest):
    url = request.url
    
    if not url:
        raise HTTPException(status_code=400, detail="URL required")
    
    if "spotify.com" in url:
        return {
            "title": "Spotify Track",
            "url": url,
            "audio_formats": [
                {
                    "format_id": "spotify_mp3",
                    "ext": "mp3",
                    "abr": 82.48,
                    "filesize": 8374838,
                }
            ],
            "video_formats": [],
        }
    
    try:
        ydl_opts = get_ydl_opts({"format": "best"})
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get("formats", [])
            
            video_formats = []
            audio_formats = []
            
            seen_video = set()
            seen_audio = set()
            
            for f in formats:
                if f.get("vcodec") != "none" and f.get("vcodec") != None:
                    resolution = f.get("resolution", "unknown")
                    if resolution not in seen_video:
                        seen_video.add(resolution)
                        video_formats.append({
                            "format_id": f["format_id"],
                            "ext": f.get("ext"),
                            "resolution": resolution,
                            "filesize": f.get("filesize"),
                        })
                elif f.get("acodec") != "none" and f.get("acodec") != None:
                    abr = f.get("abr", 0)
                    if abr and abr not in seen_audio:
                        seen_audio.add(abr)
                        audio_formats.append({
                            "format_id": f["format_id"],
                            "ext": f.get("ext"),
                            "abr": abr,
                            "filesize": f.get("filesize"),
                        })
            
            video_formats.sort(key=lambda x: x.get("resolution", ""), reverse=True)
            audio_formats.sort(key=lambda x: x.get("abr", 0), reverse=True)
            
            return {
                "title": info.get("title"),
                "url": url,
                "video_formats": video_formats[:10],
                "audio_formats": audio_formats[:10],
            }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch formats: {str(e)}")


@app.post("/api/download")
async def initiate_download(request: DownloadRequest):
    url = request.url
    format_id = request.format_id
    
    if not url or not format_id:
        raise HTTPException(status_code=400, detail="Missing url or format_id")
    
    download_id = str(uuid.uuid4())
    
    # Initialize progress
    download_progress[download_id] = {
        "status": "starting",
        "message": "Initializing download..."
    }
    
    # Start download in background
    asyncio.create_task(process_download(download_id, url, format_id))
    
    return {
        "download_id": download_id,
        "progress_url": f"/api/progress/{download_id}",
        "message": "Download initiated"
    }

@app.get("/api/download/best/audio")
async def download_best_audio(url: str = Query(..., description="The URL to download audio from")):
    """Download best audio for a given URL"""
    download_id = str(uuid.uuid4())
    download_progress[download_id] = {"status": "starting", "message": "Downloading best audio..."}

    # Start download in background with progress tracking
    asyncio.create_task(process_ytdlp_download(download_id, url, "bestaudio/best"))
    
    return {"download_id": download_id, "progress_url": f"/api/progress/{download_id}"}

@app.get("/api/download/best/video")
async def download_best_video(url: str = Query(..., description="The URL to download video from")):
    """Download best video for a given URL"""
    download_id = str(uuid.uuid4())
    download_progress[download_id] = {"status": "starting", "message": "Downloading best video..."}

    # Start download in background with progress tracking
    asyncio.create_task(process_ytdlp_download(download_id, url, "bestvideo+bestaudio/best"))
    
    return {"download_id": download_id, "progress_url": f"/api/progress/{download_id}"}
    
@app.get("/api/progress/{download_id}")
async def stream_progress(download_id: str):
    """Stream progress updates using Server-Sent Events"""
    
    async def event_generator():
        try:
            while True:
                if download_id in download_progress:
                    progress_data = download_progress[download_id]
                    
                    # Send data as SSE
                    yield f"data: {json.dumps(progress_data)}\n\n"
                    
                    # If download is done or error, send final event and break
                    if progress_data.get("status") in ["done", "error"]:
                        # Keep connection alive briefly, then cleanup
                        for _ in range(4):
                            yield ":\n\n"  # SSE comment line keeps connection alive
                            await asyncio.sleep(0.5)
                        
                        # Cleanup
                        if download_id in download_progress:
                            del download_progress[download_id]
                        break
                    
                    # Wait before next update
                    await asyncio.sleep(0.5)
                else:
                    # Download ID not found
                    yield f"data: {json.dumps({'status': 'error', 'error': 'Download ID not found'})}\n\n"
                    break
                    
        except asyncio.CancelledError:
            # Client disconnected
            if download_id in download_progress:
                del download_progress[download_id]
        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'error': str(e)})}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


async def process_download(download_id: str, url: str, format_id: str):
    """Process download and update progress"""
    
    try:
        if "spotify.com" in url:
            await process_spotify_download(download_id, url)
        else:
            await process_ytdlp_download(download_id, url, format_id)
    except Exception as e:
        download_progress[download_id] = {
            "status": "error",
            "error": str(e)
        }


async def process_spotify_download(download_id: str, url: str):
    """Handle Spotify downloads"""
    
    try:
        download_progress[download_id] = {
            "status": "downloading",
            "message": "Downloading from Spotify...",
            "percent": "0%",
            "eta": None,
            "speed": None
        }
        
        cmd = [
            "spotdl",
            url,
            "--format",
            "mp3",
            "--bitrate",
            "disable",
            "--cookie-file",
            "cookies.txt",
        ]
        
        result = subprocess.run(
            cmd,
            cwd=DOWNLOAD_DIR,
            capture_output=True,
            text=True,
            timeout=300,
        )
        
        if result.returncode != 0:
            raise Exception(result.stderr.strip() or "Unknown spotdl error")
        
        mp3_files = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith(".mp3")]
        
        if not mp3_files:
            raise Exception("No files downloaded")
        
        if len(mp3_files) == 1:
            filename = mp3_files[0]
            download_progress[download_id] = {
                "status": "done",
                "filename": filename,
                "download_url": f"/downloads/{filename}",
            }
        else:
            zip_filename = f"spotify_{download_id[:8]}.zip"
            zip_path = os.path.join(DOWNLOAD_DIR, zip_filename)
            
            download_progress[download_id] = {
                "status": "processing",
                "message": f"Creating ZIP with {len(mp3_files)} files..."
            }
            
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for mp3_file in mp3_files:
                    mp3_path = os.path.join(DOWNLOAD_DIR, mp3_file)
                    zipf.write(mp3_path, mp3_file)
                    os.remove(mp3_path)
            
            download_progress[download_id] = {
                "status": "done",
                "filename": zip_filename,
                "download_url": f"/downloads/{zip_filename}",
            }
    
    except Exception as e:
        download_progress[download_id] = {
            "status": "error",
            "error": f"Spotify download failed: {str(e)}"
        }


async def process_ytdlp_download(download_id: str, url: str, format_id: str):
    """Handle yt-dlp downloads with progress tracking - runs in executor to avoid blocking"""
    
    def progress_hook(d):
        """This runs in the thread pool, but we can safely update the dict"""
        if d["status"] == "downloading":
            # Calculate percentage
            total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded_bytes = d.get("downloaded_bytes", 0)
            
            if total_bytes > 0:
                percent = (downloaded_bytes / total_bytes) * 100
                percent_str = f"{percent:.1f}%"
            else:
                percent_str = d.get("_percent_str", "0%").strip()
            
            # Format speed
            speed = d.get("speed")
            speed_str = None
            if speed:
                if speed > 1024 * 1024:
                    speed_str = f"{speed / (1024 * 1024):.2f} MB/s"
                elif speed > 1024:
                    speed_str = f"{speed / 1024:.2f} KB/s"
                else:
                    speed_str = f"{speed:.2f} B/s"
            
            # Update progress - safe because we're just updating a dict
            download_progress[download_id] = {
                "status": "downloading",
                "percent": percent_str,
                "downloaded_bytes": downloaded_bytes,
                "total_bytes": total_bytes,
                "eta": d.get("eta"),
                "speed": speed_str,
                "speed_bytes": speed,
            }
        elif d["status"] == "finished":
            download_progress[download_id] = {
                "status": "processing",
                "message": "Processing file...",
                "percent": "100%"
            }
    
    def blocking_download():
        """The actual blocking download operation"""
        output_template = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
        ydl_opts = get_ydl_opts({
            "format": format_id,
            "outtmpl": output_template,
            "progress_hooks": [progress_hook],  # This is the key - progress_hook gets called by yt-dlp
        })
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                filename = os.path.basename(filepath)
                
                return {
                    "status": "done",
                    "title": info.get("title"),
                    "filename": filename,
                    "download_url": f"/downloads/{filename}",
                    "percent": "100%"
                }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }
    
    # Run the blocking download in a thread pool executor
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, blocking_download)
    download_progress[download_id] = result


@app.delete("/api/files/{filename}")
async def delete_file(filename: str):
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    
    try:
        os.remove(filepath)
        return {"message": "File deleted successfully", "filename": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")

@app.get("/downloads/{filename:path}")
async def download(filename: str, request: Request):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    file_size = os.path.getsize(file_path)
    range_header = request.headers.get("range")

    # Detect MIME type
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "application/octet-stream"

    def file_generator(start: int, end: int):
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(8192, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)

    if range_header:
        try:
            range_str = range_header.strip().split("=")[-1]
            byte1, byte2 = range_str.split("-")
            byte1 = int(byte1)
            byte2 = int(byte2) if byte2 else file_size - 1
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid Range header")

        return StreamingResponse(
            file_generator(byte1, byte2),
            status_code=206,
            headers={
                "Content-Range": f"bytes {byte1}-{byte2}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(byte2 - byte1 + 1),
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            },
            media_type=mime_type,
        )
    else:
        return StreamingResponse(
            file_generator(0, file_size - 1),
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            },
            media_type=mime_type,
        )

VIDEO_EXTS = {"mp4", "mkv", "webm"}
AUDIO_EXTS = {"mp3", "m4a", "aac", "wav"}


@app.get("/files")
def get_available_files():
    result_list = []
    DIR = Path(DOWNLOAD_DIR)
    if not DIR.exists() or not DIR.is_dir():
        return []

    for file in DIR.iterdir():
        if file.is_file():
            name = file.name
            size = file.stat().st_size
            ext = file.suffix[1:].lower()

            if ext in VIDEO_EXTS:
                file_type = "video"
            elif ext in AUDIO_EXTS:
                file_type = "audio"
            else:
                file_type = "others"

            result_list.append({"name": name, "size": size, "type": file_type})

    return result_list


@app.get("/health")
async def health_check():
    cookies_status = "OK" if os.path.exists(COOKIES_FILE) else "MISSING"
    return {
        "status": "healthy",
        "cookies": cookies_status
    }


@app.get("/")
async def root():
    return {
        "message": "YouTube/Spotify Downloader API",
        "docs": "/docs",
        "health": "/health"
                        }
