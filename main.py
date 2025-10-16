from fastapi import FastAPI, Request, HTTPException, Query
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
import json
import mimetypes
import re
from typing import Optional, Dict, Any

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

# Store download progress for SSE streaming
download_progress: Dict[str, Dict[str, Any]] = {}

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
    }
    if os.path.exists(COOKIES_FILE):
        base_opts["cookiefile"] = COOKIES_FILE
    
    if additional_opts:
        base_opts.update(additional_opts)
    return base_opts


def is_spotify_url(url: str) -> bool:
    """Check if URL is from Spotify"""
    return "spotify.com" in url


def is_playlist_url(url: str) -> bool:
    """Check if URL is a playlist"""
    playlist_indicators = [
        "playlist", "album", "list=", "sets/", 
        "/albums/", "/playlists/", "mix"
    ]
    return any(indicator in url.lower() for indicator in playlist_indicators)


@app.post("/api/formats")
async def get_formats(request: FormatRequest):
    """
    Fetch available formats for a given URL
    Returns format_id, extension, resolution/bitrate, and filesize
    """
    url = request.url
    
    if not url:
        raise HTTPException(status_code=400, detail="URL required")
    
    # Handle Spotify URLs
    if is_spotify_url(url):
        try:
            # Get Spotify track info using spotdl
            cmd = ["spotdl", url, "--print-errors", "--output", "{title}"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=DOWNLOAD_DIR
            )
            
            # Extract title from output or use default
            title = "Spotify Track"
            if result.stdout:
                lines = result.stdout.strip().split('\n')
                for line in lines:
                    if line and not line.startswith('['):
                        title = line
                        break
            
            return {
                "title": title,
                "url": url,
                "is_playlist": is_playlist_url(url),
                "audio_formats": [
                    {
                        "format_id": "spotify-mp3",
                        "ext": "mp3",
                        "abr": 320,
                        "acodec": "mp3",
                        "filesize": None,
                        "format_note": "Spotify Audio"
                    }
                ],
                "video_formats": []
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to fetch Spotify info: {str(e)}")
    
    # Handle other platforms with yt-dlp
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
                format_id = f.get("format_id")
                ext = f.get("ext")
                filesize = f.get("filesize")
                
                # Video formats
                if f.get("vcodec") not in ["none", None]:
                    resolution = f.get("resolution", f.get("height", "unknown"))
                    fps = f.get("fps")
                    vcodec = f.get("vcodec", "")[:20]
                    
                    # Create unique key for deduplication
                    key = f"{resolution}_{ext}_{fps}"
                    if key not in seen_video:
                        seen_video.add(key)
                        video_formats.append({
                            "format_id": format_id,
                            "ext": ext,
                            "resolution": resolution,
                            "fps": fps,
                            "vcodec": vcodec,
                            "filesize": filesize,
                            "format_note": f.get("format_note", "")
                        })
                
                # Audio formats
                elif f.get("acodec") not in ["none", None]:
                    abr = f.get("abr", 0)
                    acodec = f.get("acodec", "")[:20]
                    
                    if abr and abr not in seen_audio:
                        seen_audio.add(abr)
                        audio_formats.append({
                            "format_id": format_id,
                            "ext": ext,
                            "abr": round(abr, 2),
                            "acodec": acodec,
                            "filesize": filesize,
                            "format_note": f.get("format_note", "")
                        })
            
            # Sort by quality
            video_formats.sort(
                key=lambda x: (
                    int(re.search(r'\d+', str(x.get("resolution", "0"))).group() if re.search(r'\d+', str(x.get("resolution", "0"))) else 0),
                    x.get("fps", 0)
                ),
                reverse=True
            )
            audio_formats.sort(key=lambda x: x.get("abr", 0), reverse=True)
            
            return {
                "title": info.get("title", "Unknown"),
                "url": url,
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
                "is_playlist": "_type" in info and info["_type"] == "playlist",
                "video_formats": video_formats[:15],
                "audio_formats": audio_formats[:10],
            }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch formats: {str(e)}")


@app.post("/api/download")
async def initiate_download(request: DownloadRequest):
    """
    Initiate download with SSE progress streaming
    Supports: best-audio, best-video, or specific format_id
    Auto-zips playlists
    """
    url = request.url
    format_id = request.format_id
    
    if not url or not format_id:
        raise HTTPException(status_code=400, detail="Missing url or format_id")
    
    download_id = str(uuid.uuid4())
    
    # Initialize progress tracking
    download_progress[download_id] = {
        "status": "initializing",
        "message": "Starting download...",
        "progress": 0,
        "speed": None,
        "eta": None
    }
    
    # Start download in background
    asyncio.create_task(process_download(download_id, url, format_id))
    
    return {
        "download_id": download_id,
        "sse_url": f"/api/download/progress/{download_id}",
        "message": "Download initiated. Connect to SSE endpoint for progress."
    }


@app.get("/api/download/progress/{download_id}")
async def stream_download_progress(download_id: str):
    """
    Server-Sent Events endpoint for real-time download progress
    Streams: status, progress%, speed, eta, errors
    """
    
    async def event_generator():
        try:
            last_status = None
            
            while True:
                if download_id not in download_progress:
                    yield f"data: {json.dumps({'status': 'error', 'error': 'Download ID not found'})}\n\n"
                    break
                
                progress_data = download_progress[download_id]
                current_status = progress_data.get("status")
                
                # Send progress update
                yield f"data: {json.dumps(progress_data)}\n\n"
                
                # Check if download finished or errored
                if current_status in ["completed", "error"]:
                    # Send a few keep-alive messages
                    for _ in range(3):
                        await asyncio.sleep(0.3)
                        yield ": keepalive\n\n"
                    
                    # Cleanup after completion
                    if download_id in download_progress:
                        del download_progress[download_id]
                    break
                
                last_status = current_status
                await asyncio.sleep(0.5)
                
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
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*"
        }
    )


async def process_download(download_id: str, url: str, format_id: str):
    """Route download to appropriate handler"""
    try:
        if is_spotify_url(url):
            await process_spotify_download(download_id, url)
        else:
            await process_ytdlp_download(download_id, url, format_id)
    except Exception as e:
        download_progress[download_id] = {
            "status": "error",
            "error": str(e),
            "message": f"Download failed: {str(e)}"
        }


async def process_spotify_download(download_id: str, url: str):
    """Handle Spotify downloads with spotdl"""
    
    def blocking_spotify_download():
        try:
            download_progress[download_id] = {
                "status": "downloading",
                "message": "Downloading from Spotify...",
                "progress": 10,
            }
            
            cmd = [
                "spotdl",
                url,
                "--format", "mp3",
                "--bitrate", "320",
                "--output", "{title}",
            ]
            
            if os.path.exists(COOKIES_FILE):
                cmd.extend(["--cookie-file", COOKIES_FILE])
            
            # Track files before download
            files_before = set(os.listdir(DOWNLOAD_DIR))
            
            download_progress[download_id]["progress"] = 30
            
            result = subprocess.run(
                cmd,
                cwd=DOWNLOAD_DIR,
                capture_output=True,
                text=True,
                timeout=600
            )
            
            download_progress[download_id]["progress"] = 80
            
            if result.returncode != 0:
                error_msg = result.stderr.strip() or "Spotify download failed"
                return {"status": "error", "error": error_msg}
            
            # Find newly downloaded files
            files_after = set(os.listdir(DOWNLOAD_DIR))
            new_files = list(files_after - files_before)
            mp3_files = [f for f in new_files if f.endswith(".mp3")]
            
            if not mp3_files:
                return {"status": "error", "error": "No files downloaded"}
            
            download_progress[download_id] = {
                "status": "processing",
                "message": "Processing downloads...",
                "progress": 90
            }
            
            # Single file - return directly
            if len(mp3_files) == 1:
                filename = mp3_files[0]
                filepath = os.path.join(DOWNLOAD_DIR, filename)
                filesize = os.path.getsize(filepath)
                
                return {
                    "status": "completed",
                    "filename": filename,
                    "download_url": f"/downloads/{quote(filename)}",
                    "filesize": filesize,
                    "mimetype": "audio/mpeg",
                    "progress": 100,
                    "message": "Download complete!"
                }
            
            # Multiple files - create ZIP
            else:
                zip_filename = f"spotify_download_{download_id[:8]}.zip"
                zip_path = os.path.join(DOWNLOAD_DIR, zip_filename)
                
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                    for mp3_file in mp3_files:
                        mp3_path = os.path.join(DOWNLOAD_DIR, mp3_file)
                        zipf.write(mp3_path, mp3_file)
                        os.remove(mp3_path)  # Clean up individual files
                
                filesize = os.path.getsize(zip_path)
                
                return {
                    "status": "completed",
                    "filename": zip_filename,
                    "download_url": f"/downloads/{quote(zip_filename)}",
                    "filesize": filesize,
                    "mimetype": "application/zip",
                    "file_count": len(mp3_files),
                    "progress": 100,
                    "message": f"Downloaded {len(mp3_files)} tracks as ZIP"
                }
                
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": "Download timeout exceeded"}
        except Exception as e:
            return {"status": "error", "error": str(e)}
    
    # Run in executor to avoid blocking
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, blocking_spotify_download)
    download_progress[download_id] = result


async def process_ytdlp_download(download_id: str, url: str, format_id: str):
    """Handle yt-dlp downloads with progress tracking"""
    
    def progress_hook(d):
        """Called by yt-dlp during download"""
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            
            progress = int((downloaded / total * 100) if total > 0 else 0)
            
            speed = d.get("speed")
            speed_str = None
            if speed:
                if speed > 1024 * 1024:
                    speed_str = f"{speed / (1024 * 1024):.2f} MB/s"
                elif speed > 1024:
                    speed_str = f"{speed / 1024:.2f} KB/s"
                else:
                    speed_str = f"{speed:.0f} B/s"
            
            download_progress[download_id] = {
                "status": "downloading",
                "progress": progress,
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "eta": d.get("eta"),
                "speed": speed_str,
                "message": f"Downloading... {progress}%"
            }
            
        elif d["status"] == "finished":
            download_progress[download_id] = {
                "status": "processing",
                "progress": 95,
                "message": "Processing file..."
            }
    
    def blocking_ytdlp_download():
        try:
            # Handle format selection
            if format_id == "best-audio":
                fmt = "bestaudio/best"
            elif format_id == "best-video":
                fmt = "bestvideo+bestaudio/best"
            else:
                fmt = format_id
            
            output_template = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
            
            ydl_opts = get_ydl_opts({
                "format": fmt,
                "outtmpl": output_template,
                "progress_hooks": [progress_hook],
                "merge_output_format": "mp4",  # Ensure merged files are mp4
            })
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                
                # Check if it's a playlist
                if "_type" in info and info["_type"] == "playlist":
                    entries = info.get("entries", [])
                    
                    download_progress[download_id] = {
                        "status": "processing",
                        "progress": 90,
                        "message": f"Creating ZIP for {len(entries)} files..."
                    }
                    
                    # Find downloaded files
                    downloaded_files = []
                    for entry in entries:
                        if entry:
                            filename = ydl.prepare_filename(entry)
                            if os.path.exists(filename):
                                downloaded_files.append(filename)
                    
                    # Create ZIP
                    zip_filename = f"playlist_{download_id[:8]}.zip"
                    zip_path = os.path.join(DOWNLOAD_DIR, zip_filename)
                    
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                        for file_path in downloaded_files:
                            zipf.write(file_path, os.path.basename(file_path))
                            os.remove(file_path)  # Clean up
                    
                    filesize = os.path.getsize(zip_path)
                    
                    return {
                        "status": "completed",
                        "filename": zip_filename,
                        "download_url": f"/downloads/{quote(zip_filename)}",
                        "filesize": filesize,
                        "mimetype": "application/zip",
                        "file_count": len(downloaded_files),
                        "progress": 100,
                        "message": f"Downloaded {len(downloaded_files)} files as ZIP"
                    }
                
                # Single file
                else:
                    filepath = ydl.prepare_filename(info)
                    filename = os.path.basename(filepath)
                    filesize = os.path.getsize(filepath)
                    
                    # Detect mimetype
                    mimetype, _ = mimetypes.guess_type(filepath)
                    if not mimetype:
                        mimetype = "application/octet-stream"
                    
                    return {
                        "status": "completed",
                        "title": info.get("title"),
                        "filename": filename,
                        "download_url": f"/downloads/{quote(filename)}",
                        "filesize": filesize,
                        "mimetype": mimetype,
                        "progress": 100,
                        "message": "Download complete!"
                    }
                    
        except Exception as e:
            return {"status": "error", "error": str(e)}
    
    # Run in executor
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, blocking_ytdlp_download)
    download_progress[download_id] = result


@app.get("/downloads/{filename:path}")
async def download_file(filename: str, request: Request):
    """
    Stream file download with range support (pause/resume)
    """
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    file_size = os.path.getsize(file_path)
    range_header = request.headers.get("range")
    
    # Detect MIME type
    mimetype, _ = mimetypes.guess_type(file_path)
    if not mimetype:
        mimetype = "application/octet-stream"
    
    def file_generator(start: int, end: int):
        """Generate file chunks for streaming"""
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            chunk_size = 8192
            
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)
    
    # Handle range requests (for pause/resume)
    if range_header:
        try:
            range_str = range_header.strip().replace("bytes=", "")
            byte1, byte2 = range_str.split("-")
            byte1 = int(byte1)
            byte2 = int(byte2) if byte2 else file_size - 1
        except Exception:
            raise HTTPException(status_code=416, detail="Invalid Range header")
        
        return StreamingResponse(
            file_generator(byte1, byte2),
            status_code=206,
            headers={
                "Content-Range": f"bytes {byte1}-{byte2}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(byte2 - byte1 + 1),
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": mimetype,
            },
            media_type=mimetype,
        )
    
    # Full file download
    return StreamingResponse(
        file_generator(0, file_size - 1),
        headers={
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": mimetype,
        },
        media_type=mimetype,
    )


@app.get("/api/files")
async def list_available_files():
    """
    List all downloaded files available for download
    Returns: name, size, type (video/audio/other), mimetype
    """
    VIDEO_EXTS = {"mp4", "mkv", "webm", "avi", "mov", "flv"}
    AUDIO_EXTS = {"mp3", "m4a", "aac", "wav", "flac", "ogg", "opus"}
    
    files_list = []
    download_dir = Path(DOWNLOAD_DIR)
    
    if not download_dir.exists():
        return []
    
    for file_path in download_dir.iterdir():
        if file_path.is_file():
            name = file_path.name
            size = file_path.stat().st_size
            ext = file_path.suffix[1:].lower()
            
            # Determine file type
            if ext in VIDEO_EXTS:
                file_type = "video"
            elif ext in AUDIO_EXTS:
                file_type = "audio"
            elif ext == "zip":
                file_type = "archive"
            else:
                file_type = "other"
            
            # Get mimetype
            mimetype, _ = mimetypes.guess_type(str(file_path))
            
            files_list.append({
                "name": name,
                "size": size,
                "type": file_type,
                "mimetype": mimetype or "application/octet-stream",
                "download_url": f"/downloads/{quote(name)}",
                "extension": ext
            })
    
    # Sort by newest first
    files_list.sort(key=lambda x: x["name"], reverse=True)
    
    return files_list


@app.delete("/api/files/{filename:path}")
async def delete_file(filename: str):
    """
    Delete a downloaded file to free up server storage
    """
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    
    # Security check - ensure file is within DOWNLOAD_DIR
    if not os.path.abspath(file_path).startswith(os.path.abspath(DOWNLOAD_DIR)):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    try:
        file_size = os.path.getsize(file_path)
        os.remove(file_path)
        
        return {
            "success": True,
            "message": "File deleted successfully",
            "filename": filename,
            "freed_bytes": file_size
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")


@app.delete("/api/files")
async def delete_all_files():
    """
    Delete all downloaded files (cleanup endpoint)
    """
    download_dir = Path(DOWNLOAD_DIR)
    
    if not download_dir.exists():
        return {"success": True, "deleted_count": 0, "freed_bytes": 0}
    
    deleted_count = 0
    freed_bytes = 0
    errors = []
    
    for file_path in download_dir.iterdir():
        if file_path.is_file():
            try:
                file_size = file_path.stat().st_size
                file_path.unlink()
                deleted_count += 1
                freed_bytes += file_size
            except Exception as e:
                errors.append({"file": file_path.name, "error": str(e)})
    
    return {
        "success": True,
        "deleted_count": deleted_count,
        "freed_bytes": freed_bytes,
        "errors": errors if errors else None
    }


@app.get("/health")
async def health_check():
    """
    Health check endpoint
    Verifies: API status, cookies.txt availability, download directory
    """
    cookies_exists = os.path.exists(COOKIES_FILE)
    download_dir_exists = os.path.exists(DOWNLOAD_DIR)
    
    # Check available disk space
    stat = os.statvfs(DOWNLOAD_DIR)
    free_space_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
    
    # Count files in download directory
    file_count = len([f for f in Path(DOWNLOAD_DIR).iterdir() if f.is_file()]) if download_dir_exists else 0
    
    return {
        "status": "healthy",
        "cookies": "available" if cookies_exists else "missing",
        "cookies_path": COOKIES_FILE if cookies_exists else None,
        "download_dir": "ready" if download_dir_exists else "missing",
        "free_space_gb": round(free_space_gb, 2),
        "files_count": file_count,
        "active_downloads": len(download_progress)
    }


@app.get("/")
async def root():
    """API root with available endpoints"""
    return {
        "name": "Enhanced Media Downloader API",
        "version": "2.0",
        "endpoints": {
            "formats": "/api/formats (POST)",
            "download": "/api/download (POST)",
            "progress": "/api/download/progress/{download_id} (GET SSE)",
            "files": "/api/files (GET)",
            "delete": "/api/files/{filename} (DELETE)",
            "health": "/health (GET)",
            "docs": "/docs"
        },
        "features": [
            "YouTube/Spotify downloads",
            "SSE progress streaming",
            "Playlist auto-zipping",
            "Resume/pause support",
            "File management"
        ]
    }
