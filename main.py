from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from yt_dlp import YoutubeDL
import os

app = FastAPI()
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

class DownloadRequest(BaseModel):
    url: str

# Cleanup function to delete file after download
def cleanup_file(path: str):
    try:
        os.remove(path)
        print(f"Deleted {path}")
    except Exception as e:
        print(f"Error deleting {path}: {e}")

@app.post("/download-audio")
async def download_audio(data: DownloadRequest):
    url = data.url
    output_path = os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info).replace(".webm", ".mp3").replace(".m4a", ".mp3")
            basename = os.path.basename(filename)

            return {
                "message": "Download complete",
                "filename": basename,
                "url": f"/one-time-download/{basename}"
            }

    except Exception as e:
        return {"error": str(e)}

@app.get("/one-time-download/{filename}")
async def one_time_download(filename: str, background_tasks: BackgroundTasks):
    file_path = os.path.join(DOWNLOADS_DIR, filename)

    if not os.path.exists(file_path):
        return JSONResponse(content={"error": "File not found or already downloaded"}, status_code=404)

    background_tasks.add_task(cleanup_file, file_path)
    return FileResponse(path=file_path, filename=filename, media_type="audio/mpeg", background=background_tasks)