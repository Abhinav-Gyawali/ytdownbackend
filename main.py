from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
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
                        # Clean up after sending final event
                        await asyncio.sleep(1)
                        if download_id in download_progress:
                            del download_progress[download_id]
                        break
                
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            # Client disconnected
            pass
    
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
            "message": "Downloading from Spotify..."
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
    """Handle yt-dlp downloads with progress tracking"""
    
    def progress_hook(d):
        if d["status"] == "downloading":
            download_progress[download_id] = {
                "status": "downloading",
                "percent": d.get("_percent_str", "").strip(),
                "downloaded_bytes": d.get("downloaded_bytes", 0),
                "total_bytes": d.get("total_bytes", 0),
                "eta": d.get("eta"),
                "speed": d.get("speed"),
            }
        elif d["status"] == "finished":
            download_progress[download_id] = {
                "status": "processing",
                "message": "Processing file..."
            }
    
    output_template = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
    ydl_opts = get_ydl_opts({
        "format": format_id,
        "outtmpl": output_template,
        "progress_hooks": [progress_hook],
    })
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            filename = os.path.basename(filepath)
            
            download_progress[download_id] = {
                "status": "done",
                "title": info.get("title"),
                "filename": filename,
                "download_url": f"/downloads/{filename}",
            }
    except Exception as e:
        download_progress[download_id] = {
            "status": "error",
            "error": str(e)
        }


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
            media_type="application/octet-stream",
        )
    else:
        return StreamingResponse(
            file_generator(0, file_size - 1),
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            },
            media_type="application/octet-stream",
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

class DownloadRequest(BaseModel):
    url: str
    format_id: str


class DeleteRequest(BaseModel):
    filename: str


def get_ydl_opts(additional_opts: dict = None) -> dict:
    """Get yt-dlp options with cookie handling"""
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "cookiefile":"cookies.txt",
    }
    
    return base_opts


# WebSocket connection manager
@app.websocket("/ws/{download_id}")
async def websocket_endpoint(websocket: WebSocket, download_id: str):
    await websocket.accept()
    active_connections[download_id] = websocket
    print(f"WebSocket connected for download_id: {download_id}")
    
    try:
        # Keep connection alive and listen for close
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        print(f"WebSocket disconnected for download_id: {download_id}")
    finally:
        if download_id in active_connections:
            del active_connections[download_id]


async def send_progress(download_id: str, data: dict):
    """Send progress update to WebSocket if connected"""
    if download_id in active_connections:
        try:
            await active_connections[download_id].send_json(data)
        except Exception as e:
            print(f"Error sending progress for {download_id}: {e}")


# HTTP endpoint to fetch formats
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
            
            # Filter out duplicate formats and sort by quality
            seen_video = set()
            seen_audio = set()
            
            for f in formats:
                if f.get("vcodec") != "none" and f.get("vcodec") != None:
                    # Video format
                    resolution = f.get("resolution", "unknown")
                    if resolution not in seen_video:
                        seen_video.add(resolution)
                        video_formats.append({
                            "format_id": f["format_id"],
                            "ext": f.get("ext"),
                            "resolution": resolution,
                            "filesize": f.get("filesize"),
                            "fps": f.get("fps"),
                            "vcodec": f.get("vcodec"),
                        })
                elif f.get("acodec") != "none" and f.get("acodec") != None:
                    # Audio format
                    abr = f.get("abr", 0)
                    if abr and abr not in seen_audio:
                        seen_audio.add(abr)
                        audio_formats.append({
                            "format_id": f["format_id"],
                            "ext": f.get("ext"),
                            "abr": abr,
                            "filesize": f.get("filesize"),
                            "acodec": f.get("acodec"),
                        })
            
            # Sort formats
            video_formats.sort(key=lambda x: x.get("resolution", ""), reverse=True)
            audio_formats.sort(key=lambda x: x.get("abr", 0), reverse=True)
            
            return {
                "title": info.get("title"),
                "url": url,
                "video_formats": video_formats[:10],  # Limit to top 10
                "audio_formats": audio_formats[:10],
            }
    
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        if "Sign in" in error_msg or "bot" in error_msg:
            raise HTTPException(
                status_code=403, 
                detail="YouTube requires authentication. Please add cookies.txt file to the server. "
                       "See: https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"
            )
        raise HTTPException(status_code=500, detail=f"Failed to fetch formats: {error_msg}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch formats: {str(e)}")


# HTTP endpoint to initiate download
@app.post("/api/download")
async def initiate_download(request: DownloadRequest):
    url = request.url
    format_id = request.format_id
    
    if not url or not format_id:
        raise HTTPException(status_code=400, detail="Missing url or format_id")
    
    # Generate unique download ID
    download_id = str(uuid.uuid4())
    
    # Start download in background
    asyncio.create_task(process_download(download_id, url, format_id))
    
    return {
        "download_id": download_id,
        "websocket_url": f"/ws/{download_id}",
        "message": "Download initiated. Connect to WebSocket for progress updates."
    }


async def process_download(download_id: str, url: str, format_id: str):
    """Process download and send progress via WebSocket"""
    
    try:
        if "spotify.com" in url:
            await process_spotify_download(download_id, url)
        else:
            await process_ytdlp_download(download_id, url, format_id)
    except Exception as e:
        await send_progress(download_id, {
            "event": "error",
            "error": str(e)
        })


async def process_spotify_download(download_id: str, url: str):
    """Handle Spotify downloads"""
    print(f"🎵 Starting Spotify download for {download_id}")
    
    try:
        await send_progress(download_id, {
            "event": "progress",
            "status": "starting",
            "message": "Starting Spotify download..."
        })
        
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
        
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        
        result = subprocess.run(
            cmd,
            cwd=DOWNLOAD_DIR,
            capture_output=True,
            text=True,
            timeout=300,
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Unknown spotdl error"
            raise Exception(error_msg)
        
        # Get downloaded MP3 files
        mp3_files = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith(".mp3")]
        
        if not mp3_files:
            raise Exception("No files downloaded")
        
        if len(mp3_files) == 1:
            filename = mp3_files[0]
            await send_progress(download_id, {
                "event": "done",
                "filename": filename,
                "download_url": f"/downloads/{filename}",
            })
        else:
            # Multiple files - create ZIP
            zip_filename = f"spotify_download_{download_id[:8]}_{len(mp3_files)}_tracks.zip"
            zip_path = os.path.join(DOWNLOAD_DIR, zip_filename)
            
            await send_progress(download_id, {
                "event": "progress",
                "status": "zipping",
                "message": f"Creating ZIP archive with {len(mp3_files)} files..."
            })
            
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for mp3_file in mp3_files:
                    mp3_path = os.path.join(DOWNLOAD_DIR, mp3_file)
                    zipf.write(mp3_path, mp3_file)
                    os.remove(mp3_path)
            
            await send_progress(download_id, {
                "event": "done",
                "filename": zip_filename,
                "download_url": f"/downloads/{zip_filename}",
            })
    
    except Exception as e:
        await send_progress(download_id, {
            "event": "error",
            "error": f"Spotify download failed: {str(e)}"
        })


async def process_ytdlp_download(download_id: str, url: str, format_id: str):
    """Handle yt-dlp downloads with progress tracking"""
    
    def progress_hook(d):
        if d["status"] == "downloading":
            asyncio.create_task(send_progress(download_id, {
                "event": "progress",
                "status": "downloading",
                "percent": d.get("_percent_str", "").strip(),
                "downloaded_bytes": d.get("downloaded_bytes", 0),
                "total_bytes": d.get("total_bytes", 0),
                "eta": d.get("eta"),
                "speed": d.get("speed"),
            }))
        elif d["status"] == "finished":
            asyncio.create_task(send_progress(download_id, {
                "event": "progress",
                "status": "processing",
                "message": "Processing file..."
            }))
    
    output_template = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
    ydl_opts = get_ydl_opts({
        "format": format_id,
        "outtmpl": output_template,
        "progress_hooks": [progress_hook],
    })
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            filename = os.path.basename(filepath)
            
            await send_progress(download_id, {
                "event": "done",
                "title": info.get("title"),
                "filename": filename,
                "download_url": f"/downloads/{filename}",
            })
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        if "Sign in" in error_msg or "bot" in error_msg:
            await send_progress(download_id, {
                "event": "error",
                "error": "YouTube requires authentication. Please contact the server administrator to add cookies.txt"
            })
        else:
            await send_progress(download_id, {
                "event": "error",
                "error": error_msg
            })
    except Exception as e:
        await send_progress(download_id, {
            "event": "error",
            "error": str(e)
        })


@app.delete("/api/files/{filename}")
async def delete_file(filename: str):
    """Delete a file from the downloads directory"""
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
            media_type="application/octet-stream",
        )
    else:
        return StreamingResponse(
            file_generator(0, file_size - 1),
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            },
            media_type="application/octet-stream",
        )


VIDEO_EXTS = {"mp4", "mkv", "webm"}
AUDIO_EXTS = {"mp3", "m4a", "aac", "wav"}


@app.get("/files")
def get_available_files():
    result_list = []
    DIR = Path(DOWNLOAD_DIR)
    if not DIR.exists() or not DIR.is_dir():
        return JSONResponse(
            content={"error": "Download directory not found"}, status_code=404
        )

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
        "cookies": cookies_status,
        "cookies_path": COOKIES_FILE
    }


@app.get("/")
async def root():
    return {
        "message": "YouTube/Spotify Downloader API",
        "docs": "/docs",
        "health": "/health"
    }
