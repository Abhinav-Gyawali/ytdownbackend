import os
import yt_dlp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

app = FastAPI()

# ---- Utility: build yt-dlp options dynamically ----
def get_ydl_opts(download_path=None):
    cookies_path = "cookies.txt" if os.path.exists("cookies.txt") else None

    ydl_opts = {
        "quiet": True,
        "format": "best",
        "nocheckcertificate": True,
    }

    if cookies_path:
        ydl_opts["cookiefile"] = cookies_path

    if download_path:
        ydl_opts["outtmpl"] = os.path.join(download_path, "%(title)s.%(ext)s")

    return ydl_opts


# ---- Handlers ----
async def handle_get_formats(websocket: WebSocket, url: str):
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)

            formats = []
            for f in info.get("formats", []):
                formats.append({
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "resolution": f.get("resolution") or f"{f.get('width')}x{f.get('height')}",
                    "filesize": f.get("filesize"),
                })

            await websocket.send_json({"status": "success", "formats": formats})

    except Exception as e:
        error_message = str(e)
        if "Sign in to confirm" in error_message:
            error_message = (
                "❌ This video requires authentication. "
                "Please provide a valid cookies.txt file."
            )
        await websocket.send_json({"status": "error", "message": error_message})


async def handle_download(websocket: WebSocket, url: str, format_id: str):
    try:
        output_dir = "downloads"
        os.makedirs(output_dir, exist_ok=True)

        ydl_opts = get_ydl_opts(download_path=output_dir)
        ydl_opts["format"] = format_id

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        await websocket.send_json({
            "status": "success",
            "file": file_path
        })

    except Exception as e:
        error_message = str(e)
        if "Sign in to confirm" in error_message:
            error_message = (
                "❌ Download failed: this video requires authentication. "
                "Provide a valid cookies.txt file."
            )
        await websocket.send_json({"status": "error", "message": error_message})


# ---- WebSocket endpoint ----
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "get_formats":
                await handle_get_formats(websocket, data["url"])

            elif action == "download":
                await handle_download(websocket, data["url"], data["format_id"])

            else:
                await websocket.send_json({"status": "error", "message": "Unknown action"})

    except WebSocketDisconnect:
        print("Client disconnected")


@app.get("/health")
async def health_check():
    return {"status": "healthy"}

# ---- Run app ----
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
