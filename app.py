to upload cookie "curl -X POST -F "cookie_file=@cookie.txt" https://ytapi.vibedownloader.me/api/upload-cookie""

from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import time
import json
from werkzeug.utils import secure_filename
from flask_cors import CORS  # Import the CORS package

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["http://localhost:3000", "https://vibedownloader.vercel.app", "https://vibedownloader.me", "https://www.vibedownloader.me", "https://ytapi.vibedownloader.me/"]}})  # Enable CORS for all routes

# Configuration
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), "downloads")
COOKIE_FILE = os.path.join(os.getcwd(), "cookie.txt")
TEMP_FOLDER = os.path.join(os.getcwd(), "temp")

# Create necessary directories
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# Dictionary to store download progress info
downloads_in_progress = {}
completed_downloads = {}

# Function to clean up old files periodically
def cleanup_old_files():
    while True:
        now = time.time()
        # Delete files older than 2 hours
        for folder in [DOWNLOAD_FOLDER, TEMP_FOLDER]:
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                if os.path.isfile(file_path) and now - os.path.getmtime(file_path) > 7200:  # 2 hours
                    try:
                        os.remove(file_path)
                    except Exception as e:
                        print(f"Error deleting {file_path}: {e}")

        # Clean up completed downloads dictionary
        to_remove = []
        for download_id, info in completed_downloads.items():
            if now - info.get("completion_time", 0) > 7200:  # 2 hours
                to_remove.append(download_id)

        for download_id in to_remove:
            completed_downloads.pop(download_id, None)

        time.sleep(3600)  # Check every hour

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

def get_base_ydl_opts():
    """Return base yt-dlp options with cookie file if exists"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
    }

    if os.path.exists(COOKIE_FILE):
        ydl_opts['cookiefile'] = COOKIE_FILE

    return ydl_opts

def get_verification_status(channel_data):
    """Check if channel is verified based on badges in channel data"""
    # This is a simplified check and might need adjustment
    badges = channel_data.get('badges', [])
    for badge in badges:
        if badge and isinstance(badge, dict) and 'verified' in badge.get('type', '').lower():
            return True
    return False

@app.route('/api/video-info', methods=['GET'])
def get_video_info():
    """
    Get video information including available formats

    Query parameters:
    - url: YouTube video URL

    Returns:
    - Video information including title, thumbnail, channel info, and available formats with direct download links
    """
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
                "description": info.get('description'),
                "duration": info.get('duration'),
                "view_count": info.get('view_count'),
                "like_count": info.get('like_count'),
                "upload_date": info.get('upload_date'),
                "thumbnails": info.get('thumbnails', []),
                "channel": {
                    "id": info.get('channel_id'),
                    "name": info.get('channel', info.get('uploader')),
                    "url": info.get('channel_url'),
                    "profile_picture": None,  # Will be updated if available
                    "verified": get_verification_status(info)
                },
                "audio_formats": [],
                "video_formats": []
            }

            # Try to extract channel profile picture if available
            for thumbnail in info.get('thumbnails', []):
                if 'url' in thumbnail and 'avatar' in thumbnail.get('id', ''):
                    result['channel']['profile_picture'] = thumbnail['url']
                    break

            # Extract audio formats
            audio_formats = []
            for format in info.get('formats', []):
                if format.get('vcodec') == 'none' and format.get('acodec') != 'none':
                    audio_formats.append({
                        "format_id": format.get('format_id'),
                        "ext": format.get('ext'),
                        "filesize": format.get('filesize'),
                        "format_note": format.get('format_note'),
                        "abr": format.get('abr'),
                        "download_url": f"/api/direct-download/{video_id}/{format.get('format_id')}"
                    })

            result["audio_formats"] = audio_formats

            # Extract video formats with direct download links
            video_formats = []
            for format in info.get('formats', []):
                if format.get('vcodec') != 'none':
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

            result["video_formats"] = video_formats

            return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/download', methods=['GET'])
def download_video():
    """
    Download a video and combine with best audio

    Query parameters:
    - url: YouTube video URL
    - format_id: (Optional) Specific video format ID to download
    - audio_id: (Optional) Specific audio format ID to download

    Returns:
    - Download ID to check status
    """
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
        "message": "Download started. Check status using the /api/download-status endpoint."
    })

def process_download(download_id, url, format_id=None, audio_id=None):
    """Process video download and merging in background"""
    downloads_in_progress[download_id] = {
        "status": "downloading",
        "progress": 0,
        "url": url,
        "start_time": time.time()
    }

    try:
        output_filename = f"{download_id}.mp4"
        output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)

        # Configure yt-dlp options
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'outtmpl': os.path.join(TEMP_FOLDER, f"{download_id}_%(title)s.%(ext)s"),
            'progress_hooks': [lambda d: update_progress(download_id, d)],
        })

        # If specific format requested
        if format_id:
            if audio_id:
                # Download video and audio separately and merge
                video_path = download_specific_format(url, format_id, f"{download_id}_video")
                audio_path = download_specific_format(url, audio_id, f"{download_id}_audio")

                # Merge video and audio
                merge_video_audio(video_path, audio_path, output_path)

                # Clean up temp files
                if os.path.exists(video_path):
                    os.remove(video_path)
                if os.path.exists(audio_path):
                    os.remove(audio_path)

            else:
                # Download specific format and merge with best audio
                ydl_opts.update({
                    'format': f"{format_id}+bestaudio",
                    'merge_output_format': 'mp4',
                    'final_filepath': output_path
                })

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url)
                    downloaded_file = ydl.prepare_filename(info)

                    # Move to downloads folder with proper name
                    if downloaded_file and os.path.exists(downloaded_file):
                        os.rename(downloaded_file, output_path)
                    elif downloaded_file and os.path.exists(downloaded_file.rsplit(".", 1)[0] + ".mp4"):
                        os.rename(downloaded_file.rsplit(".", 1)[0] + ".mp4", output_path)
        else:
            # Download best quality and merge
            ydl_opts.update({
                'format': 'bestvideo+bestaudio',
                'merge_output_format': 'mp4',
            })

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url)
                downloaded_file = ydl.prepare_filename(info)

                # Move to downloads folder with proper name
                if downloaded_file and os.path.exists(downloaded_file):
                    os.rename(downloaded_file, output_path)
                elif downloaded_file and os.path.exists(downloaded_file.rsplit(".", 1)[0] + ".mp4"):
                    os.rename(downloaded_file.rsplit(".", 1)[0] + ".mp4", output_path)

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

def download_specific_format(url, format_id, prefix):
    """Download specific format and return file path"""
    temp_path = os.path.join(TEMP_FOLDER, f"{prefix}.mp4")

    ydl_opts = get_base_ydl_opts()
    ydl_opts.update({
        'format': format_id,
        'outtmpl': os.path.join(TEMP_FOLDER, f"{prefix}.%(ext)s"),
    })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url)
        downloaded_file = ydl.prepare_filename(info)

        # Find the actual downloaded file (extension might be different)
        actual_file = downloaded_file
        if not os.path.exists(actual_file):
            # Try to find the file with different extensions
            for ext in ['mp4', 'webm', 'mkv', 'm4a', 'mp3']:
                candidate = downloaded_file.rsplit(".", 1)[0] + f".{ext}"
                if os.path.exists(candidate):
                    actual_file = candidate
                    break

    return actual_file

def merge_video_audio(video_path, audio_path, output_path):
    """Merge video and audio files using ffmpeg"""
    import subprocess

    try:
        command = [
            'ffmpeg', '-i', video_path, '-i', audio_path,
            '-c:v', 'copy', '-c:a', 'aac', '-strict', 'experimental',
            output_path
        ]

        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception as e:
        print(f"Error merging files: {e}")
        return False

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
    """
    Check the status of a download

    Path parameters:
    - download_id: ID of the download to check

    Returns:
    - Download status information
    """
    # Check if download is in progress
    if download_id in downloads_in_progress:
        return jsonify({
            "download_id": download_id,
            "status": downloads_in_progress[download_id]["status"],
            "progress": downloads_in_progress[download_id]["progress"],
            "url": downloads_in_progress[download_id]["url"]
        })

    # Check if download is completed
    if download_id in completed_downloads:
        return jsonify({
            "download_id": download_id,
            "status": completed_downloads[download_id]["status"],
            "url": completed_downloads[download_id]["url"],
            "download_url": completed_downloads[download_id].get("download_url")
        })

    return jsonify({"error": "Download ID not found"}), 404

@app.route('/api/get-file/<download_id>', methods=['GET'])
def get_downloaded_file(download_id):
    """
    Get a downloaded file

    Path parameters:
    - download_id: ID of the download to get

    Returns:
    - The downloaded file
    """
    if download_id in completed_downloads and completed_downloads[download_id]["status"] == "completed":
        file_path = completed_downloads[download_id]["file_path"]

        if os.path.exists(file_path):
            filename = os.path.basename(file_path)
            return send_file(file_path, as_attachment=True, download_name=filename)

    return jsonify({"error": "File not found"}), 404

@app.route('/api/direct-download/<video_id>/<format_id>', methods=['GET'])
def direct_download(video_id, format_id):
    """
    Direct download endpoint that combines video with best audio and sends the file

    Path parameters:
    - video_id: YouTube video ID
    - format_id: Format ID to download

    Query parameters:
    - audio_id: (Optional) Specific audio format ID
    - filename: (Optional) Custom filename for the download

    Returns:
    - The downloaded file directly to the browser
    """
    audio_id = request.args.get('audio_id')
    custom_filename = request.args.get('filename')
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        # Create a unique filename based on video ID and format
        filename = f"{video_id}_{format_id}"
        if audio_id:
            filename += f"_{audio_id}"
        filename += ".mp4"

        output_path = os.path.join(DOWNLOAD_FOLDER, filename)

        # Check if file already exists (cached)
        if os.path.exists(output_path):
            download_name = custom_filename if custom_filename else f"{video_id}.mp4"
            return send_file(output_path, as_attachment=True, download_name=download_name)

        # Set up download options
        ydl_opts = get_base_ydl_opts()

        # Add progress hooks
        download_id = str(uuid.uuid4())
        downloads_in_progress[download_id] = {
            "status": "downloading",
            "progress": 0,
            "url": url,
            "start_time": time.time()
        }

        ydl_opts.update({
            'progress_hooks': [lambda d: update_progress(download_id, d)],
        })

        # Always combine with best audio if format is video-only
        ydl_opts.update({
            'format': f"{format_id}+bestaudio" if not audio_id else f"{format_id}+{audio_id}",
            'outtmpl': output_path,
            'merge_output_format': 'mp4',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
        })

        # Download the file
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url)

            # Get the actual title for a better filename if not provided
            if not custom_filename and info.get('title'):
                video_title = info.get('title')
                # Clean the title for use as a filename
                video_title = ''.join(c for c in video_title if c.isalnum() or c in ' ._-')
                download_name = f"{video_title}.mp4"
            else:
                download_name = custom_filename if custom_filename else f"{video_id}.mp4"

        # Update downloads info and remove from in-progress
        if download_id in downloads_in_progress:
            downloads_in_progress.pop(download_id)

        completed_downloads[download_id] = {
            "status": "completed",
            "url": url,
            "file_path": output_path,
            "completion_time": time.time()
        }

        # Check if download was successful
        if os.path.exists(output_path):
            return send_file(output_path, as_attachment=True, download_name=download_name)
        else:
            return jsonify({"error": "Download failed"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/upload-cookie', methods=['POST'])
def upload_cookie():
    """
    Upload cookie.txt file

    Form data:
    - cookie_file: The cookie.txt file

    Returns:
    - Success or error message
    """
    if 'cookie_file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['cookie_file']

    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    try:
        filename = secure_filename(file.filename)
        file_path = os.path.join(os.getcwd(), "cookie.txt")
        file.save(file_path)
        return jsonify({"success": True, "message": "Cookie file uploaded successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Simple health check endpoint"""
    return jsonify({
        "status": "ok",
        "version": "1.0.0",
        "cookie_file_exists": os.path.exists(COOKIE_FILE)
    })

if __name__ == '__main__':
    # For Replit, use this configuration
    app.run(host='0.0.0.0', port=8080)
