from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
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

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Store active WebSocket connections by download_id
active_connections = {}

# Store download tasks
download_tasks = {}


class FormatRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str


class DeleteRequest(BaseModel):
    filename: str


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
        ydl_opts = {
            "cookiefile": "cookies.txt",
            "quiet": True,
            "format": "best",
            "nocheckcertificate": True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get("formats", [])
            
            video_formats = []
            audio_formats = []
            
            for f in formats:
                if f.get("vcodec") != "none":
                    video_formats.append({
                        "format_id": f["format_id"],
                        "ext": f.get("ext"),
                        "resolution": f.get("resolution"),
                        "filesize": f.get("filesize"),
                    })
                elif f.get("acodec") != "none":
                    audio_formats.append({
                        "format_id": f["format_id"],
                        "ext": f.get("ext"),
                        "abr": f.get("abr"),
                        "filesize": f.get("filesize"),
                    })
            
            return {
                "title": info.get("title"),
                "url": url,
                "video_formats": video_formats,
                "audio_formats": audio_formats,
            }
    
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
    ydl_opts = {
        "cookiefile": "cookies.txt",
        "format": format_id,
        "outtmpl": output_template,
        "nocheckcertificate": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
    }
    
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
    return {"status": "healthy"}
