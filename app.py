from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import time
import json
from werkzeug.utils import secure_filename
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["http://localhost:3000", "https://vibedownloader.vercel.app", "https://vibedownloader.me", "https://www.vibedownloader.me", "https://ytapi.vibedownloader.me/"]}})

# Configuration - Use /tmp for ephemeral storage on Render
DOWNLOAD_FOLDER = "/tmp/downloads"
COOKIE_FILE = "/tmp/cookie.txt"
TEMP_FOLDER = "/tmp/temp"

# Create necessary directories
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# Dictionary to store download progress info
downloads_in_progress = {}
completed_downloads = {}

# Modified cleanup function for Render (more conservative)
def cleanup_old_files():
    while True:
        try:
            now = time.time()
            # Delete files older than 30 minutes (more aggressive due to limited storage)
            for folder in [DOWNLOAD_FOLDER, TEMP_FOLDER]:
                if os.path.exists(folder):
                    for filename in os.listdir(folder):
                        file_path = os.path.join(folder, filename)
                        if os.path.isfile(file_path) and now - os.path.getmtime(file_path) > 1800:  # 30 minutes
                            try:
                                os.remove(file_path)
                            except Exception as e:
                                print(f"Error deleting {file_path}: {e}")

            # Clean up completed downloads dictionary
            to_remove = []
            for download_id, info in completed_downloads.items():
                if now - info.get("completion_time", 0) > 1800:  # 30 minutes
                    to_remove.append(download_id)

            for download_id in to_remove:
                completed_downloads.pop(download_id, None)

        except Exception as e:
            print(f"Cleanup error: {e}")
        
        time.sleep(900)  # Check every 15 minutes

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

def get_base_ydl_opts():
    """Return base yt-dlp options with cookie file if exists"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        # Add format selection to reduce file sizes
        'format': 'best[height<=720]',  # Limit to 720p max to save resources
    }

    if os.path.exists(COOKIE_FILE):
        ydl_opts['cookiefile'] = COOKIE_FILE

    return ydl_opts

def get_verification_status(channel_data):
    """Check if channel is verified based on badges in channel data"""
    badges = channel_data.get('badges', [])
    for badge in badges:
        if badge and isinstance(badge, dict) and 'verified' in badge.get('type', '').lower():
            return True
    return False

@app.route('/api/video-info', methods=['GET'])
def get_video_info():
    """Get video information including available formats"""
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "Missing video URL"}), 400

    try:
        ydl_opts = get_base_ydl_opts()

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            video_id = info.get('id')

            # Extract relevant information
            result = {
                "id": video_id,
                "title": info.get('title'),
                "description": info.get('description')[:500] if info.get('description') else None,  # Limit description
                "duration": info.get('duration'),
                "view_count": info.get('view_count'),
                "like_count": info.get('like_count'),
                "upload_date": info.get('upload_date'),
                "thumbnails": info.get('thumbnails', [])[:3],  # Limit thumbnails
                "channel": {
                    "id": info.get('channel_id'),
                    "name": info.get('channel', info.get('uploader')),
                    "url": info.get('channel_url'),
                    "profile_picture": None,
                    "verified": get_verification_status(info)
                },
                "audio_formats": [],
                "video_formats": []
            }

            # Extract audio formats (limit to reasonable quality)
            audio_formats = []
            for format in info.get('formats', []):
                if (format.get('vcodec') == 'none' and 
                    format.get('acodec') != 'none' and 
                    format.get('abr', 0) <= 128):  # Limit audio bitrate
                    audio_formats.append({
                        "format_id": format.get('format_id'),
                        "ext": format.get('ext'),
                        "filesize": format.get('filesize'),
                        "format_note": format.get('format_note'),
                        "abr": format.get('abr'),
                        "download_url": f"/api/direct-download/{video_id}/{format.get('format_id')}"
                    })

            result["audio_formats"] = audio_formats[:5]  # Limit number of formats

            # Extract video formats (limit to reasonable quality)
            video_formats = []
            for format in info.get('formats', []):
                if (format.get('vcodec') != 'none' and 
                    format.get('height', 0) <= 720):  # Limit to 720p max
                    video_formats.append({
                        "format_id": format.get('format_id'),
                        "ext": format.get('ext'),
                        "filesize": format.get('filesize'),
                        "format_note": format.get('format_note'),
                        "width": format.get('width'),
                        "height": format.get('height'),
                        "fps": format.get('fps'),
                        "vcodec": format.get('vcodec'),
                        "acodec": format.get('acodec'),
                        "download_url": f"/api/direct-download/{video_id}/{format.get('format_id')}",
                        "resolution": f"{format.get('width', 0)}x{format.get('height', 0)}"
                    })

            result["video_formats"] = video_formats[:5]  # Limit number of formats

            return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/download', methods=['GET'])
def download_video():
    """Download a video with resource limits"""
    url = request.args.get('url')
    format_id = request.args.get('format_id')
    audio_id = request.args.get('audio_id')

    if not url:
        return jsonify({"error": "Missing video URL"}), 400

    download_id = str(uuid.uuid4())

    # Start download in background
    thread = threading.Thread(
        target=process_download,
        args=(download_id, url, format_id, audio_id)
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        "download_id": download_id,
        "status": "processing",
        "message": "Download started. Check status using the /api/download-status endpoint.",
        "note": "Files are temporarily stored and will be deleted after 30 minutes."
    })

def process_download(download_id, url, format_id=None, audio_id=None):
    """Process video download with resource limits"""
    downloads_in_progress[download_id] = {
        "status": "downloading",
        "progress": 0,
        "url": url,
        "start_time": time.time()
    }

    try:
        output_filename = f"{download_id}.mp4"
        output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)

        # Configure yt-dlp options with resource limits
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'outtmpl': os.path.join(TEMP_FOLDER, f"{download_id}_%(title)s.%(ext)s"),
            'progress_hooks': [lambda d: update_progress(download_id, d)],
            'format': 'best[height<=720]',  # Limit quality
        })

        # Simplified download logic
        if format_id:
            ydl_opts.update({
                'format': f"{format_id}+bestaudio/best",
                'merge_output_format': 'mp4',
            })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url)
            downloaded_file = ydl.prepare_filename(info)

            # Find the actual downloaded file
            actual_file = None
            for ext in ['mp4', 'webm', 'mkv']:
                candidate = downloaded_file.rsplit(".", 1)[0] + f".{ext}"
                if os.path.exists(candidate):
                    actual_file = candidate
                    break

            if actual_file and os.path.exists(actual_file):
                # Move to downloads folder
                os.rename(actual_file, output_path)

        # Update download info
        completed_downloads[download_id] = {
            "status": "completed",
            "url": url,
            "file_path": output_path,
            "download_url": f"/api/get-file/{download_id}",
            "completion_time": time.time()
        }

    except Exception as e:
        completed_downloads[download_id] = {
            "status": "failed",
            "url": url,
            "error": str(e),
            "completion_time": time.time()
        }

    finally:
        # Remove from in-progress
        if download_id in downloads_in_progress:
            downloads_in_progress.pop(download_id)

def update_progress(download_id, d):
    """Update download progress information"""
    if download_id in downloads_in_progress:
        if d['status'] == 'downloading':
            try:
                downloads_in_progress[download_id]['progress'] = float(d.get('_percent_str', '0%').replace('%', ''))
            except:
                pass
        elif d['status'] == 'finished':
            downloads_in_progress[download_id]['status'] = 'processing'
            downloads_in_progress[download_id]['progress'] = 100

@app.route('/api/download-status/<download_id>', methods=['GET'])
def check_download_status(download_id):
    """Check the status of a download"""
    if download_id in downloads_in_progress:
        return jsonify({
            "download_id": download_id,
            "status": downloads_in_progress[download_id]["status"],
            "progress": downloads_in_progress[download_id]["progress"],
            "url": downloads_in_progress[download_id]["url"]
        })

    if download_id in completed_downloads:
        return jsonify({
            "download_id": download_id,
            "status": completed_downloads[download_id]["status"],
            "url": completed_downloads[download_id]["url"],
            "download_url": completed_downloads[download_id].get("download_url"),
            "error": completed_downloads[download_id].get("error")
        })

    return jsonify({"error": "Download ID not found"}), 404

@app.route('/api/get-file/<download_id>', methods=['GET'])
def get_downloaded_file(download_id):
    """Get a downloaded file"""
    if download_id in completed_downloads and completed_downloads[download_id]["status"] == "completed":
        file_path = completed_downloads[download_id]["file_path"]

        if os.path.exists(file_path):
            filename = os.path.basename(file_path)
            return send_file(file_path, as_attachment=True, download_name=filename)

    return jsonify({"error": "File not found"}), 404

@app.route('/api/direct-download/<video_id>/<format_id>', methods=['GET'])
def direct_download(video_id, format_id):
    """Direct download with resource limits"""
    custom_filename = request.args.get('filename')
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        filename = f"{video_id}_{format_id}.mp4"
        output_path = os.path.join(DOWNLOAD_FOLDER, filename)

        # Check if file already exists
        if os.path.exists(output_path):
            download_name = custom_filename if custom_filename else f"{video_id}.mp4"
            return send_file(output_path, as_attachment=True, download_name=download_name)

        # Download with limits
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'format': f"{format_id}+bestaudio/best[height<=720]",
            'outtmpl': output_path,
            'merge_output_format': 'mp4',
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url)
            
            if not custom_filename and info.get('title'):
                video_title = info.get('title')
                video_title = ''.join(c for c in video_title if c.isalnum() or c in ' ._-')[:50]  # Limit length
                download_name = f"{video_title}.mp4"
            else:
                download_name = custom_filename if custom_filename else f"{video_id}.mp4"

        if os.path.exists(output_path):
            return send_file(output_path, as_attachment=True, download_name=download_name)
        else:
            return jsonify({"error": "Download failed"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/upload-cookie', methods=['POST'])
def upload_cookie():
    """Upload cookie.txt file"""
    if 'cookie_file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['cookie_file']

    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    try:
        file.save(COOKIE_FILE)
        return jsonify({"success": True, "message": "Cookie file uploaded successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        "version": "1.0.0",
        "platform": "render",
        "cookie_file_exists": os.path.exists(COOKIE_FILE),
        "storage_info": "Ephemeral storage - files deleted after 30 minutes"
    })

@app.route('/', methods=['GET'])
def root():
    """Root endpoint"""
    return jsonify({
        "message": "YouTube Downloader API",
        "version": "1.0.0",
        "endpoints": [
            "/api/health",
            "/api/video-info",
            "/api/download",
            "/api/download-status/<id>",
            "/api/get-file/<id>",
            "/api/direct-download/<video_id>/<format_id>"
        ]
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
