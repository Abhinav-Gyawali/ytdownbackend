from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import tempfile
import threading
import time
from werkzeug.utils import secure_filename
import re
import json

app = Flask(__name__, static_folder='downloads')
CORS(app)  # Enable CORS for frontend access

# Configuration
DOWNLOAD_DIR = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Store download progress and status
download_progress = {}
download_status = {}

class ProgressHook:
    def __init__(self, download_id):
        self.download_id = download_id
    
    def __call__(self, d):
        if d['status'] == 'downloading':
            try:
                percent = d.get('_percent_str', '0%').replace('%', '')
                speed = d.get('_speed_str', 'N/A')
                eta = d.get('_eta_str', 'N/A')
                
                download_progress[self.download_id] = {
                    'status': 'downloading',
                    'percent': float(percent) if percent.replace('.', '').isdigit() else 0,
                    'speed': speed,
                    'eta': eta,
                    'filename': os.path.basename(d.get('filename', ''))
                }
            except:
                pass
        elif d['status'] == 'finished':
            filepath = d.get('filename', '')
            download_progress[self.download_id] = {
                'status': 'finished',
                'percent': 100,
                'filename': os.path.basename(filepath),
                'filepath': filepath,
                'download_url': f"/download-file/{os.path.basename(filepath)}" if filepath else None
            }

def sanitize_filename(filename):
    """Remove or replace invalid characters from filename"""
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    filename = re.sub(r'\s+', ' ', filename).strip()
    return filename[:200]  # Limit length

def download_audio_task(url, quality, download_id):
    """Download audio in background thread"""
    try:
        download_status[download_id] = {'status': 'starting', 'message': 'Initializing download...'}
        
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio',
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': quality if quality != 'best' else '192',
            }],
            'progress_hooks': [ProgressHook(download_id)],
            'extractaudio': True,
            'audioformat': 'mp3',
            'noplaylist': True,
            'quiet': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            download_status[download_id] = {'status': 'processing', 'message': 'Extracting audio...'}
            ydl.download([url])
            download_status[download_id] = {'status': 'completed', 'message': 'Audio download completed'}
            
    except Exception as e:
        error_msg = str(e)
        download_progress[download_id] = {
            'status': 'error',
            'error': error_msg
        }
        download_status[download_id] = {'status': 'error', 'message': f'Download failed: {error_msg}'}

def download_video_task(url, quality, download_id, format_id=None):
    """Download video in background thread"""
    try:
        download_status[download_id] = {'status': 'starting', 'message': 'Initializing download...'}
        
        if format_id:
            format_selector = format_id
        elif quality == 'best':
            format_selector = 'best[ext=mp4]/best[ext=webm]/best'
        elif quality == 'worst':
            format_selector = 'worst[ext=mp4]/worst[ext=webm]/worst'
        else:
            # Handle specific quality like 720p, 1080p, etc.
            quality_num = quality.replace('p', '') if 'p' in quality else quality
            format_selector = f'best[height<={quality_num}][ext=mp4]/best[height<={quality_num}][ext=webm]/best[height<={quality_num}]'
        
        ydl_opts = {
            'format': format_selector,
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
            'progress_hooks': [ProgressHook(download_id)],
            'noplaylist': True,
            'writesubtitles': False,
            'writeautomaticsub': False,
            'quiet': False,
        }
        
        with yt_dlp.YutubeDL(ydl_opts) as ydl:
            download_status[download_id] = {'status': 'processing', 'message': 'Downloading video...'}
            ydl.download([url])
            download_status[download_id] = {'status': 'completed', 'message': 'Video download completed'}
            
    except Exception as e:
        error_msg = str(e)
        download_progress[download_id] = {
            'status': 'error',
            'error': error_msg
        }
        download_status[download_id] = {'status': 'error', 'message': f'Download failed: {error_msg}'}

# Main endpoint that matches your Java code's base URL
@app.route('/download-audio', methods=['POST'])
def download_audio():
    """Main audio download endpoint with synchronous response"""
    try:
        # Get and validate request data
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
        
        url = data.get('url')
        format_id = data.get('format_id')  # Get format_id from request
        timestamp = data.get('timestamp', '2025-06-05 09:50:46')  # Use provided timestamp
        user = data.get('user', 'Abhinav-Gyawali')  # Use provided user
        
        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        if not format_id:
            return jsonify({'success': False, 'error': 'Format ID is required'}), 400
            
        print(f"Download requested by {user} at {timestamp}")
        print(f"URL: {url}")
        print(f"Format ID: {format_id}")
        
        # Setup download options
        output_template = f'downloads/{user}_{int(time.time())}_%(title)s.%(ext)s'
        
        ydl_opts = {
            'format': format_id,
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'progress_hooks': [lambda d: print(f"Download progress: {d.get('status')} - {d.get('_percent_str', '0%')}")],
            'nocheckcertificate': True,
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        try:
            # Perform the download
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                print("Starting download...")
                info = ydl.extract_info(url, download=True)
                
                # Get downloaded file path
                if info.get('requested_downloads'):
                    downloaded_file = info['requested_downloads'][0]['filepath']
                else:
                    downloaded_file = ydl.prepare_filename(info)
                
                print(f"Download completed: {downloaded_file}")
                
                # Get file size
                file_size = os.path.getsize(downloaded_file) if os.path.exists(downloaded_file) else 0
                
                # Log the successful download
                log_message = (
                    f"Download completed:\n"
                    f"User: {user}\n"
                    f"Timestamp: {timestamp}\n"
                    f"Format: {format_id}\n"
                    f"File: {os.path.basename(downloaded_file)}\n"
                    f"Size: {file_size} bytes"
                )
                print(log_message)
                
                return jsonify({
                    'success': True,
                    'message': 'Download completed successfully',
                    'details': {
                        'title': info.get('title'),
                        'format': format_id,
                        'filename': os.path.basename(downloaded_file),
                        'file_size': file_size,
                        'timestamp': timestamp,
                        'user': user
                    }
                })
                
        except Exception as e:
            error_message = f"Download failed: {str(e)}"
            print(error_message)
            return jsonify({
                'success': False,
                'error': error_message,
                'details': {
                    'timestamp': timestamp,
                    'user': user,
                    'format': format_id
                }
            }), 400
            
    except Exception as e:
        error_message = f"Server error: {str(e)}"
        print(error_message)
        return jsonify({
            'success': False,
            'error': error_message
        }), 500

# Additional endpoints for your Java app to use
@app.route('/download-audio/video', methods=['POST'])
def download_video():
    """Video download endpoint"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
        
        url = data.get('url')
        quality = data.get('quality', 'best')  # best, worst, 720p, 1080p, etc.
        format_id = data.get('format_id')  # specific format ID
        
        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        
        # Generate unique download ID
        download_id = f"video_{int(time.time() * 1000)}"
        
        # Initialize progress tracking
        download_progress[download_id] = {'status': 'queued', 'percent': 0}
        download_status[download_id] = {'status': 'queued', 'message': 'Download queued'}
        
        # Start download in background
        thread = threading.Thread(target=download_video_task, args=(url, quality, download_id, format_id))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'success': True,
            'download_id': download_id,
            'message': 'Video download started',
            'status_url': f'/status/{download_id}',
            'progress_url': f'/progress/{download_id}'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/download-audio/info', methods=['POST'])
def get_video_info():
    """Get video information"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
        
        url = data.get('url')
        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            return jsonify({
                'success': True,
                'info': {
                    'title': info.get('title', 'Unknown'),
                    'uploader': info.get('uploader', 'Unknown'),
                    'duration': info.get('duration', 0),
                    'view_count': info.get('view_count', 0),
                    'upload_date': info.get('upload_date', ''),
                    'description': (info.get('description', '')[:300] + '...') if info.get('description') else '',
                    'thumbnail': info.get('thumbnail', ''),
                    'webpage_url': info.get('webpage_url', url)
                }
            })
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/download-audio/qualities', methods=['POST'])
def get_qualities():
    """Get available qualities for a video"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
        
        url = data.get('url')
        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Get available video qualities
            video_qualities = []
            audio_qualities = []
            
            # Collect unique video qualities
            heights = set()
            for f in info.get('formats', []):
                if f.get('vcodec') and f.get('vcodec') != 'none' and f.get('height'):
                    heights.add(f.get('height'))
            
            # Create quality options
            for height in sorted(heights, reverse=True):
                video_qualities.append({
                    'quality': f"{height}p",
                    'label': f"{height}p",
                    'height': height
                })
            
            # Add best/worst options
            if heights:
                video_qualities.insert(0, {'quality': 'best', 'label': 'Best Quality', 'height': max(heights)})
                video_qualities.append({'quality': 'worst', 'label': 'Worst Quality', 'height': min(heights)})
            
            # Audio quality options
            audio_qualities = [
                {'quality': 'best', 'label': 'Best Quality (320kbps)', 'bitrate': '320'},
                {'quality': '192', 'label': 'High Quality (192kbps)', 'bitrate': '192'},
                {'quality': '128', 'label': 'Standard Quality (128kbps)', 'bitrate': '128'},
                {'quality': '96', 'label': 'Good Quality (96kbps)', 'bitrate': '96'},
                {'quality': '64', 'label': 'Low Quality (64kbps)', 'bitrate': '64'}
            ]
            
            return jsonify({
                'success': True,
                'video_qualities': video_qualities,
                'audio_qualities': audio_qualities,
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0),
                'has_video': len([f for f in info.get('formats', []) if f.get('vcodec') != 'none']) > 0,
                'has_audio': len([f for f in info.get('formats', []) if f.get('acodec') != 'none']) > 0
            })
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400
        
@app.route('/download-audio/formats', methods=['POST'])
def get_formats():
    """Get detailed format information"""
    try:
        data = request.get_json()
        print(f"Received request data: {data}")
        
        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
        
        url = data.get('url')
        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        
        print(f"Processing URL: {url}")
        
        # Updated yt-dlp options
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'format': 'bestvideo+bestaudio/best',  # Request best quality formats
            'youtube_include_dash_manifest': True,  # Include DASH manifests
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'no_color': True,
            'prefer_free_formats': True
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                print(f"Video title: {info.get('title')}")
                all_formats = info.get('formats', [])
                print(f"Available formats: {len(all_formats)}")
                
                # Print all available formats for debugging
                print("All available format IDs:")
                for f in all_formats:
                    print(f"Format ID: {f.get('format_id')} - "
                          f"vcodec: {f.get('vcodec')} - "
                          f"acodec: {f.get('acodec')}")
                
            except Exception as e:
                print(f"Error extracting info: {str(e)}")
                return jsonify({
                    'success': False, 
                    'error': f'Error extracting video info: {str(e)}'
                }), 400
            
            video_formats = []
            audio_formats = []
            
            for f in all_formats:
                # Skip sponsorblock formats
                if f.get('format_id', '').startswith('sb'):
                    continue
                    
                try:
                    format_info = {
                        'format_id': f.get('format_id'),
                        'ext': f.get('ext'),
                        'filesize': f.get('filesize'),
                        'filesize_approx': f.get('filesize_approx'),
                        'tbr': f.get('tbr')
                    }
                    
                    # Video formats (including combined formats)
                    if f.get('vcodec') and f.get('vcodec') not in ['none', None]:
                        format_info.update({
                            'resolution': f.get('resolution', f"{f.get('height', '?')}p"),
                            'width': f.get('width'),
                            'height': f.get('height'),
                            'fps': f.get('fps'),
                            'vcodec': f.get('vcodec'),
                            'acodec': f.get('acodec', 'none')
                        })
                        video_formats.append(format_info)
                        print(f"Added video format: {format_info['format_id']} - {format_info['resolution']}")
                    
                    # Audio-only formats
                    elif f.get('acodec') and f.get('acodec') not in ['none', None]:
                        format_info.update({
                            'acodec': f.get('acodec'),
                            'abr': f.get('abr'),
                            'asr': f.get('asr'),
                            'resolution': 'audio only'
                        })
                        audio_formats.append(format_info)
                        print(f"Added audio format: {format_info['format_id']} - {format_info.get('abr')}kbps")
                    
                except Exception as e:
                    print(f"Error processing format: {str(e)}")
                    continue
            
            print(f"Total video formats: {len(video_formats)}")
            print(f"Total audio formats: {len(audio_formats)}")
            
            response_data = {
                'success': True,
                'video_formats': sorted(video_formats, key=lambda x: x.get('height', 0) or 0, reverse=True),
                'audio_formats': sorted(audio_formats, key=lambda x: x.get('abr', 0) or 0, reverse=True),
                'title': info.get('title', 'Unknown')
            }
            
            print(f"Sending response: {response_data}")
            return jsonify(response_data)
            
    except Exception as e:
        print(f"Global error: {str(e)}")
        return jsonify({
            'success': False, 
            'error': f'Server error: {str(e)}'
        }), 400
            
# Status and progress endpoints
@app.route('/status/<download_id>', methods=['GET'])
def get_status(download_id):
    """Get download status"""
    status = download_status.get(download_id, {'status': 'not_found', 'message': 'Download not found'})
    return jsonify(status)

@app.route('/progress/<download_id>', methods=['GET'])
def get_progress(download_id):
    """Get download progress"""
    progress = download_progress.get(download_id, {'status': 'not_found'})
    return jsonify(progress)

# File management endpoints
@app.route('/download-audio/files', methods=['GET'])
def list_files():
    """List all downloaded files"""
    try:
        files = []
        for filename in os.listdir(DOWNLOAD_DIR):
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if os.path.isfile(filepath):
                stat = os.stat(filepath)
                files.append({
                    'filename': filename,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                    'download_url': f'/download-file/{filename}'
                })
        
        return jsonify({'success': True, 'files': files})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/download-audio/delete', methods=['POST'])
def delete_file():
    """Delete a specific file"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
        
        filename = data.get('filename')
        if not filename:
            return jsonify({'success': False, 'error': 'Filename is required'}), 400
        
        safe_filename = secure_filename(filename)
        filepath = os.path.join(DOWNLOAD_DIR, safe_filename)
        
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        os.remove(filepath)
        return jsonify({'success': True, 'message': 'File deleted successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
        
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)