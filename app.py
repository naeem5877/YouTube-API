from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import time
import shutil
import sys
import logging
from werkzeug.utils import secure_filename

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger('youtube-api')

app = Flask(__name__)

# Configuration
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), "downloads")
TEMP_FOLDER = os.path.join(os.getcwd(), "temp")

# Look for cookie file in multiple locations
COOKIE_FILE_PATHS = [
    os.path.join(os.getcwd(), "cookie.txt"),
    os.path.join(os.getcwd(), "cookies.txt"),
    "/app/cookie.txt",
    "/app/cookies.txt"
]

# Find the first available cookie file
COOKIE_FILE = None
for path in COOKIE_FILE_PATHS:
    if os.path.exists(path) and os.path.isfile(path):
        COOKIE_FILE = path
        print(f"Found cookie file at: {COOKIE_FILE}")
        break

if not COOKIE_FILE:
    print("Warning: No cookie file found in any of the expected locations")

# Define yt-dlp version to use
YTDLP_VERSION = '2025.4.30'

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
        
        # Keep track of which users are downloading
        active_download_ids = set(downloads_in_progress.keys())
        active_file_paths = set()
        
        # Collect all active file paths to avoid deleting in-use files
        for download_id in active_download_ids:
            info = downloads_in_progress.get(download_id, {})
            temp_path = os.path.join(TEMP_FOLDER, f"{download_id}_*")
            active_file_paths.add(temp_path)
        
        # Check completed downloads and remove old ones
        to_remove = []
        for download_id, info in completed_downloads.items():
            # Only delete files that are no longer active and older than 30 minutes
            if download_id not in active_download_ids and now - info.get("completion_time", 0) > 1800:  # 30 minutes
                file_path = info.get("file_path")
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        print(f"Cleaned up completed download: {file_path}")
                    except Exception as e:
                        print(f"Error deleting {file_path}: {e}")
                to_remove.append(download_id)

        # Remove tracked completed downloads
        for download_id in to_remove:
            completed_downloads.pop(download_id, None)
            
        # Delete old files in download folder (older than 30 minutes)
        for folder in [DOWNLOAD_FOLDER, TEMP_FOLDER]:
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                
                # Skip files that are currently being used
                if any(file_path.startswith(active_path) for active_path in active_file_paths):
                    continue
                    
                if os.path.isfile(file_path) and now - os.path.getmtime(file_path) > 1800:  # 30 minutes
                    try:
                        os.remove(file_path)
                        print(f"Cleaned up old file: {file_path}")
                    except Exception as e:
                        print(f"Error deleting {file_path}: {e}")

        time.sleep(300)  # Check every 5 minutes

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

def get_base_ydl_opts():
    """Return base yt-dlp options with enhanced cookie handling"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'no_check_certificate': True,  # Skip HTTPS certificate validation
        'rm_cache_dir': True,          # Clean cache directory
    }

    # Add cookie file if exists with proper error handling
    if os.path.exists(COOKIE_FILE):
        try:
            # Check if cookie file is readable
            with open(COOKIE_FILE, 'r') as f:
                cookie_content = f.read()
                
            # Only use cookie file if it has content
            if cookie_content.strip():
                print(f"Using cookie file: {COOKIE_FILE}")
                ydl_opts['cookiefile'] = COOKIE_FILE
            else:
                print("Cookie file exists but is empty")
        except Exception as e:
            print(f"Error reading cookie file: {e}")
    else:
        print(f"Cookie file not found at: {COOKIE_FILE}")
        
    return ydl_opts

def get_verification_status(info):
    """Check if channel is verified based on channel data"""
    # Check for channel verification badges in different possible locations
    if info.get('channel_is_verified'):
        return True

    # Check in channel badges if available
    badges = info.get('badges', [])
    for badge in badges:
        if badge and isinstance(badge, dict):
            badge_type = badge.get('type', '').lower()
            if 'verified' in badge_type or 'official' in badge_type:
                return True

    # Check in uploader badges if available
    uploader_badges = info.get('uploader_badges', [])
    if isinstance(uploader_badges, list):
        for badge in uploader_badges:
            if badge and isinstance(badge, str) and ('verified' in badge.lower() or 'official' in badge.lower()):
                return True

    return False

def get_channel_profile_picture(info):
    """Extract channel profile picture from video info"""
    # Try multiple possible locations for channel avatar
    
    # Check in channel thumbnails
    if info.get('channel_thumbnails'):
        for thumbnail in info.get('channel_thumbnails', []):
            if thumbnail and isinstance(thumbnail, dict) and 'url' in thumbnail:
                return thumbnail['url']

    # Look for uploader_thumbnail
    if info.get('uploader_thumbnail'):
        return info.get('uploader_thumbnail')

    # Check regular thumbnails for avatar
    for thumbnail in info.get('thumbnails', []):
        if thumbnail and isinstance(thumbnail, dict):
            thumbnail_id = thumbnail.get('id', '')
            if isinstance(thumbnail_id, str) and ('avatar' in thumbnail_id or 'channel' in thumbnail_id):
                return thumbnail['url']

    return None

@app.route('/api/video-info', methods=['GET'])
def get_video_info():
    """
    Get video information including available formats

    Query parameters:
    - url: YouTube video URL

    Returns:
    - Video information including title, thumbnails, channel info, and available formats
    """
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "Missing video URL"}), 400

    try:
        # Ensure we're using the specified yt-dlp version
        ydl_opts = get_base_ydl_opts()
        ydl_opts['extract_flat'] = False  # Ensure we get full info
        
        # Add verbose logging for debugging
        print(f"Extracting info for URL: {url}")
        
        # Check if cookie file is being used
        if 'cookiefile' in ydl_opts:
            print(f"Using cookie file: {ydl_opts['cookiefile']}")
        else:
            print("No cookie file being used")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract full info with formats
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    print("Info extraction returned None")
                    return jsonify({"error": "Could not extract video information"}), 500
            except Exception as e:
                print(f"Error during info extraction: {str(e)}")
                
                # Return more helpful error message
                if "Sign in to confirm you're not a bot" in str(e):
                    return jsonify({
                        "error": "YouTube requires authentication. The cookie file may be invalid or expired.",
                        "details": str(e)
                    }), 401
                else:
                    return jsonify({"error": f"Error extracting video info: {str(e)}"}), 500

            video_id = info.get('id')

            # Get clean title
            title = info.get('title', '').strip()
            if not title:
                title = info.get('fulltitle', '').strip()

            # Get only 4 high quality thumbnails
            thumbnails = info.get('thumbnails', [])
            selected_thumbnails = []
            
            if thumbnails:
                # Sort thumbnails by resolution (if width/height available)
                sorted_thumbnails = sorted(
                    [t for t in thumbnails if t.get('width') and t.get('height')],
                    key=lambda x: (x.get('width', 0) * x.get('height', 0)),
                    reverse=True
                )
                
                # Take top 4 thumbnails
                selected_thumbnails = sorted_thumbnails[:4]
                
                # If fewer than 4 sorted thumbnails, add others until we have 4
                if len(selected_thumbnails) < 4 and len(thumbnails) > len(selected_thumbnails):
                    for t in thumbnails:
                        if t not in selected_thumbnails:
                            selected_thumbnails.append(t)
                            if len(selected_thumbnails) >= 4:
                                break

            # Extract relevant information
            result = {
                "id": video_id,
                "title": title,
                "description": info.get('description'),
                "duration": info.get('duration'),
                "view_count": info.get('view_count'),
                "like_count": info.get('like_count'),
                "upload_date": info.get('upload_date'),
                "thumbnails": selected_thumbnails,
                "channel": {
                    "id": info.get('channel_id'),
                    "name": info.get('channel', info.get('uploader')),
                    "url": info.get('channel_url'),
                    "profile_picture": get_channel_profile_picture(info),
                    "verified": get_verification_status(info)
                },
                "audio_formats": [],
                "video_formats": []
            }

            # Extract audio formats
            audio_formats = []
            for format in info.get('formats', []):
                if format.get('vcodec') == 'none' and format.get('acodec') != 'none':
                    # Skip formats with no filesize information
                    if not format.get('filesize') and not format.get('filesize_approx'):
                        continue

                    audio_formats.append({
                        "format_id": format.get('format_id'),
                        "ext": format.get('ext'),
                        "filesize": format.get('filesize') or format.get('filesize_approx'),
                        "format_note": format.get('format_note'),
                        "abr": format.get('abr'),
                        "download_url": f"/api/direct-download/{video_id}/{format.get('format_id')}"
                    })

            result["audio_formats"] = audio_formats

            # Extract only available video formats with direct download links
            video_formats = []
            for format in info.get('formats', []):
                if format.get('vcodec') != 'none':
                    # Skip formats with no filesize information
                    if not format.get('filesize') and not format.get('filesize_approx'):
                        continue

                    # Skip formats that are likely unavailable
                    if format.get('format_note', '').lower() == 'none':
                        continue

                    video_formats.append({
                        "format_id": format.get('format_id'),
                        "ext": format.get('ext'),
                        "filesize": format.get('filesize') or format.get('filesize_approx'),
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
                        shutil.move(downloaded_file, output_path)
                    elif downloaded_file and os.path.exists(downloaded_file.rsplit(".", 1)[0] + ".mp4"):
                        shutil.move(downloaded_file.rsplit(".", 1)[0] + ".mp4", output_path)
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
                    shutil.move(downloaded_file, output_path)
                elif downloaded_file and os.path.exists(downloaded_file.rsplit(".", 1)[0] + ".mp4"):
                    shutil.move(downloaded_file.rsplit(".", 1)[0] + ".mp4", output_path)

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
                # Use the newer progress format in case _percent_str is not available
                try:
                    if d.get('total_bytes') and d.get('downloaded_bytes'):
                        progress = (d.get('downloaded_bytes') / d.get('total_bytes')) * 100
                        downloads_in_progress[download_id]['progress'] = progress
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

@app.route('/api/health', methods=['GET'])
def health_check():
    """Enhanced health check endpoint - Important for Koyeb to consider service healthy"""
    # Always return status code 200 for health checks to keep service running
    cookie_status = "not_found"
    cookie_content = ""
    
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, 'r') as f:
                content = f.read(100)  # Just read the first 100 chars to check
                cookie_content = f"{len(content)} characters" if content else "empty"
                cookie_status = "valid" if content else "empty"
        except Exception as e:
            cookie_status = f"error: {str(e)}"
    
    # Check environment
    app_dir = os.getcwd()
    files_in_dir = os.listdir(app_dir)[:10]  # List first 10 files
    
    return jsonify({
        "status": "ok",
        "version": "1.0.0",
        "ytdlp_version": YTDLP_VERSION,
        "cookie_file": {
            "path": COOKIE_FILE,
            "exists": COOKIE_FILE and os.path.exists(COOKIE_FILE),
            "status": cookie_status,
            "content_preview": cookie_content
        },
        "environment": {
            "app_directory": app_dir,
            "files": files_in_dir,
            "python_version": sys.version
        },
        "downloads_active": len(downloads_in_progress),
        "downloads_completed": len(completed_downloads),
        "storage_usage": {
            "downloads_folder_mb": get_folder_size_mb(DOWNLOAD_FOLDER),
            "temp_folder_mb": get_folder_size_mb(TEMP_FOLDER)
        }
    })

def get_folder_size_mb(folder_path):
    """Calculate folder size in MB"""
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(folder_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.exists(fp):
                total_size += os.path.getsize(fp)
    return round(total_size / (1024 * 1024), 2)  # Convert to MB

# Add a root route for easy checking if service is up
@app.route('/', methods=['GET'])
def root():
    """Root endpoint to verify the API is running"""
    return jsonify({
        "status": "running",
        "message": "YouTube Downloader API is active",
        "endpoints": {
            "health": "/api/health",
            "video_info": "/api/video-info?url=VIDEO_URL",
            "download": "/api/download?url=VIDEO_URL&format_id=FORMAT_ID",
            "download_status": "/api/download-status/DOWNLOAD_ID",
            "get_file": "/api/get-file/DOWNLOAD_ID",
            "direct_download": "/api/direct-download/VIDEO_ID/FORMAT_ID"
        }
    })

# For Koyeb deployment
if __name__ == '__main__':
    # Get port from environment variable or use 8080 as default
    port = int(os.environ.get('PORT', 8080))
    
    # Log startup information
    logger.info(f"Starting YouTube Downloader API on port {port}")
    logger.info(f"Python version: {sys.version}")
    logger.info(f"Cookie file path: {COOKIE_FILE}")
    logger.info(f"Cookie file exists: {COOKIE_FILE and os.path.exists(COOKIE_FILE)}")
    
    # Run the app
    app.run(host='0.0.0.0', port=port)
