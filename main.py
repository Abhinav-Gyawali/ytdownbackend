from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from urllib.parse import quote
import yt_dlp
import os
import uuid
import asyncio
import subprocess
import os
import subprocess
import zipfile

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# Progress hook for yt_dlp
def make_hook(ws):
    async def _hook(d):
        if d["status"] in ["processing", "finished"]:
            update = {
                "event": "progress",
                "status": d["status"],
                "percent": d.get("_percent_str", "").strip(),
                "downloaded_bytes": d.get("downloaded_bytes", 0),
                "total_bytes": d.get("total_bytes", 0),
                "eta": d.get("eta"),
                "speed": d.get("speed"),
            }
            try:
                await ws.send_json(update)
            except:
                pass

    return _hook


@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    print("WebSocket connection attempt...")
    await ws.accept()
    print("WebSocket connected.")

    try:
        while True:
            print("Waiting to receive data from WebSocket...")
            data = await ws.receive_json()
            print(f"Received data: {data}")

            action = data.get("type")
            print(f"Action received: {action}")

            if action == "fetch_formats":
                print("Calling handle_get_formats...")
                await handle_get_formats(ws, data)

            elif action == "download":
                print("Calling handle_download...")
                await handle_download(ws, data)

            elif action == "delete":
                print("Calling handle_delete...")
                await handle_delete(ws, data)

            else:
                print("Invalid action received.")
                await ws.send_json({"event": "error", "message": "Invalid action"})

    except WebSocketDisconnect:
        print("WebSocket disconnected.")
    except Exception as e:
        print(f"Exception occurred: {e}")
        await ws.send_json({"event": "error", "message": str(e)})


async def handle_get_formats(ws: WebSocket, data: dict):
    print("[handle_get_formats] Function called")
    url = data.get("data").get("url")
    print(f"[handle_get_formats] URL received: {url}")

    if not url:
        print("[handle_get_formats] No URL provided")
        await ws.send_json({"event": "error", "message": "URL required"})
        return

    if "spotify.com" in url:
        print("[handle_get_formats] Spotify URL detected")
        await ws.send_json(
            {
                "event": "formats",
                "data": {
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
                },
            }
        )
        return

    try:
        print("[handle_get_formats] Initializing yt_dlp with options")
        ydl_opts = {
            "cookiefile": "cookies.txt",
            "quiet": True,
            "format": "best",
            "nocheckcertificate": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print("[handle_get_formats] Extracting info with yt_dlp")
            info = ydl.extract_info(url, download=False)
            print("[handle_get_formats] Info extracted successfully")

            formats = info.get("formats", [])
            print(f"[handle_get_formats] Total formats found: {len(formats)}")

            video_formats = []
            audio_formats = []

            for f in formats:
                print(f"[handle_get_formats] Processing format: {f.get('format_id')}")
                if f.get("vcodec") != "none":
                    print(f"[handle_get_formats] -> Identified as video format")
                    video_formats.append(
                        {
                            "format_id": f["format_id"],
                            "ext": f.get("ext"),
                            "resolution": f.get("resolution"),
                            "filesize": f.get("filesize"),
                        }
                    )
                elif f.get("acodec") != "none":
                    print(f"[handle_get_formats] -> Identified as audio format")
                    audio_formats.append(
                        {
                            "format_id": f["format_id"],
                            "ext": f.get("ext"),
                            "abr": f.get("abr"),
                            "filesize": f.get("filesize"),
                        }
                    )

            print(
                f"[handle_get_formats] Sending response with {len(video_formats)} video and {len(audio_formats)} audio formats"
            )
            await ws.send_json(
                {
                    "event": "formats",
                    "data": {
                        "title": info.get("title"),
                        "url": url,
                        "video_formats": video_formats,
                        "audio_formats": audio_formats,
                    },
                }
            )

    except Exception as e:
        print(f"[handle_get_formats] Exception occurred: {e}")
        await ws.send_json(
            {"event": "error", "message": f"Failed to fetch formats: {str(e)}"}
        )


async def handle_download(ws: WebSocket, data: dict):
    url = data.get("data").get("url")
    format_id = data.get("data").get("format_id")

    if not url or not format_id:
        return await ws.send_json(
            {"event": "error", "message": "Missing url or format_id"}
        )
    if "spotify.com" in url:
        print(f"🎵 Starting Spotify download for URL: {url}")
        try:
            # Print the command being executed
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
            print(f"📋 Executing command: {' '.join(cmd)}")
            print(f"📁 Working directory: {DOWNLOAD_DIR}")

            # Check if download directory exists
            if not os.path.exists(DOWNLOAD_DIR):
                print(f"⚠️  Download directory doesn't exist, creating: {DOWNLOAD_DIR}")
                os.makedirs(DOWNLOAD_DIR, exist_ok=True)

            # Print files before download
            files_before = os.listdir(DOWNLOAD_DIR)
            print(f"📂 Files in directory before download: {files_before}")

            print("⏳ Starting download...")
            result = subprocess.run(
                cmd,
                cwd=DOWNLOAD_DIR,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
            )

            # Print subprocess results
            print(f"🔍 Command return code: {result.returncode}")
            if result.stdout:
                print(f"📤 STDOUT: {result.stdout}")
            if result.stderr:
                print(f"⚠️  STDERR: {result.stderr}")

            if result.returncode != 0:
                error_msg = result.stderr.strip() or "Unknown spotdl error"
                print(
                    f"❌ SpotDL command failed with code {result.returncode}: {error_msg}"
                )
                raise Exception(error_msg)

            # Print files after download
            files_after = os.listdir(DOWNLOAD_DIR)
            print(f"📂 Files in directory after download: {files_after}")

            # Get all downloaded MP3 files
            mp3_files = [f for f in files_after if f.endswith(".mp3")]
            print(f"🎶 Found {len(mp3_files)} MP3 files: {mp3_files}")

            if not mp3_files:
                print("❌ No MP3 files found after download")
                raise Exception("No files downloaded")

            if len(mp3_files) == 1:
                # Single file
                filename = mp3_files[0]
                file_path = os.path.join(DOWNLOAD_DIR, filename)
                file_size = os.path.getsize(file_path)

                print(
                    f"✅ Single file download completed: {filename} ({file_size:,} bytes)"
                )

                await ws.send_json(
                    {
                        "event": "done",
                        "filename": filename,
                        "filepath": f"/downloads/{filename}",
                    }
                )

            else:
                # Multiple files - create ZIP
                print(
                    f"📦 Multiple files detected ({len(mp3_files)}), creating ZIP archive"
                )

                zip_filename = f"spotify_download_{len(mp3_files)}_tracks.zip"
                zip_path = os.path.join(DOWNLOAD_DIR, zip_filename)

                print(f"🗜️  Creating ZIP file: {zip_path}")

                total_size_before_zip = 0
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                    for i, mp3_file in enumerate(mp3_files, 1):
                        mp3_path = os.path.join(DOWNLOAD_DIR, mp3_file)
                        file_size = os.path.getsize(mp3_path)
                        total_size_before_zip += file_size

                        print(
                            f"📁 Adding to ZIP ({i}/{len(mp3_files)}): {mp3_file} ({file_size:,} bytes)"
                        )
                        zipf.write(mp3_path, mp3_file)

                        # Delete original MP3 file after adding to ZIP
                        os.remove(mp3_path)
                        print(f"🗑️  Deleted original file: {mp3_file}")

                zip_size = os.path.getsize(zip_path)
                compression_ratio = (
                    (1 - zip_size / total_size_before_zip) * 100
                    if total_size_before_zip > 0
                    else 0
                )

                print(f"✅ ZIP creation completed: {zip_filename}")
                print(f"📊 Original total size: {total_size_before_zip:,} bytes")
                print(f"📊 ZIP size: {zip_size:,} bytes")
                print(f"📊 Compression ratio: {compression_ratio:.1f}%")

                await ws.send_json(
                    {
                        "event": "done",
                        "filename": zip_filename,
                        "filepath": f"/downloads/{zip_filename}",
                    }
                )

        except subprocess.TimeoutExpired:
            error_msg = "Download timeout (exceeded 5 minutes)"
            print(f"⏰ {error_msg}")
            await ws.send_json(
                {"event": "error", "error": f"Spotify download failed: {error_msg}"}
            )

        except FileNotFoundError as e:
            error_msg = "SpotDL not found - please install with: pip install spotdl"
            print(f"❌ FileNotFoundError: {error_msg}")
            await ws.send_json(
                {"event": "error", "error": f"Spotify download failed: {error_msg}"}
            )

        except PermissionError as e:
            error_msg = (
                f"Permission denied accessing download directory: {DOWNLOAD_DIR}"
            )
            print(f"🔒 PermissionError: {error_msg}")
            await ws.send_json(
                {"event": "error", "error": f"Spotify download failed: {error_msg}"}
            )

        except Exception as e:
            error_msg = str(e)
            print(f"💥 Unexpected error: {error_msg}")
            print(f"🔍 Error type: {type(e).__name__}")

            # Print additional debug info
            try:
                files_current = os.listdir(DOWNLOAD_DIR)
                print(f"📂 Current files in directory: {files_current}")
            except:
                print("❌ Could not list directory contents")

            await ws.send_json(
                {"event": "error", "error": f"Spotify download failed: {error_msg}"}
            )

        print("🏁 Spotify download process completed")

    else:
        output_template = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
        ydl_opts = {
            "cookiefile": "cookies.txt",
            "format": format_id,
            "outtmpl": output_template,
            "nocheckcertificate": True,
            "quiet": True,
            "no_warnings": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                await ws.send_json(
                    {
                        "event": "done",
                        "title": info.get("title"),
                        "filename": os.path.basename(filepath),
                        "filepath": f"/downloads/{os.path.basename(filepath)}",
                    }
                )
        except Exception as e:
            await ws.send_json({"event": "error", "message": str(e)})


async def handle_delete(ws: WebSocket, data: dict):
    print(data)
    filename = data.get("data").get("filename")
    if not filename:
        await ws.send_json({"event": "error", "message": "Filename required"})
        return

    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.isfile(filepath):
        try:
            os.remove(filepath)
            await ws.send_json({"event": "deleted", "filename": filename})
        except Exception as e:
            await ws.send_json(
                {"event": "error", "message": f"Delete failed: {str(e)}"}
            )
    else:
        await ws.send_json({"event": "error", "message": "File not found"})


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


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
