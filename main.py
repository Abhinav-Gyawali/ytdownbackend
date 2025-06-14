from fastapi import FastAPI, WebSocket, WebSocketDisconnect,Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from urllib.parse import quote
import yt_dlp
import os
import uuid
import asyncio
import subprocess

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Progress hook for yt_dlp
def make_hook(ws):
    async def _hook(d):
        if d['status'] in ['processing', 'finished']:
            update = {
                "event": "progress",
                "status": d['status'],
                "percent": d.get('_percent_str', '').strip(),
                "downloaded_bytes": d.get('downloaded_bytes', 0),
                "total_bytes": d.get('total_bytes', 0),
                "eta": d.get('eta'),
                "speed": d.get('speed')
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
        await ws.send_json({
            "event": "formats",
            "data":{
            "title": "Spotify Track",
            "audio_formats": [{"format_id": "spotify_mp3", "ext": "mp3"}],
            "video_formats": []
            }
        })
        return

    try:
        print("[handle_get_formats] Initializing yt_dlp with options")
        ydl_opts = {'quiet': True, 'format': 'best', 'nocheckcertificate': True}

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
                if f.get('vcodec') != 'none':
                    print(f"[handle_get_formats] -> Identified as video format")
                    video_formats.append({
                        "format_id": f['format_id'],
                        "ext": f.get("ext"),
                        "resolution": f.get("resolution"),
                        "filesize": f.get("filesize")
                    })
                elif f.get('acodec') != 'none':
                    print(f"[handle_get_formats] -> Identified as audio format")
                    audio_formats.append({
                        "format_id": f['format_id'],
                        "ext": f.get("ext"),
                        "abr": f.get("abr"),
                        "filesize": f.get("filesize")
                    })

            print(f"[handle_get_formats] Sending response with {len(video_formats)} video and {len(audio_formats)} audio formats")
            await ws.send_json({
                "event": "formats",
                "data":{
                "title": info.get("title"),
                "url" : url,
                "video_formats": video_formats,
                "audio_formats": audio_formats
                }
            })

    except Exception as e:
        print(f"[handle_get_formats] Exception occurred: {e}")
        await ws.send_json({"event": "error", "message": f"Failed to fetch formats: {str(e)}"})
        
async def handle_download(ws: WebSocket, data: dict):
    url = data.get("data").get("url")
    format_id = data.get("data").get("format_id")
    if not url or not format_id:
        await ws.send_json({"event": "error", "message": "Missing url or format_id"})
        return

    if "spotify.com" in url:
        try:
            result = subprocess.run(
                ["spotdl", url],
                cwd=DOWNLOAD_DIR,
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                raise Exception(result.stderr.strip())

            filename = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith(".mp3")][-1]
            await ws.send_json({
                "event": "done",
                
                "filename": filename,
                "filepath": f"/downloads/{filename}"
                
            })
        except Exception as e:
            await ws.send_json({"event": "error", "message": f"Spotify download failed: {str(e)}"})

    else:
        output_template = os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s')
        ydl_opts = {
            'format': format_id,
            'outtmpl': output_template,
            'nocheckcertificate': True,
            'quiet': True,
            'no_warnings': True
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                await ws.send_json({
                    "event": "done",
                    "title": info.get("title"),
                    "filename": os.path.basename(filepath),
                    "filepath": f"/downloads/{os.path.basename(filepath)}"
                })
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
            await ws.send_json({"event": "error", "message": f"Delete failed: {str(e)}"})
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
            media_type="application/octet-stream"
        )
    else:
        return StreamingResponse(
            file_generator(0, file_size - 1),
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            },
            media_type="application/octet-stream"
        )
        
@app.get("/health")
async def health_check():
    return {"status": "healthy"}