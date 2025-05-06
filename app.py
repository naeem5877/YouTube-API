from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import time
import shutil
import sys
import logging
import json
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
COOKIE_FOLDER = os.path.join(os.getcwd(), "cookies")

# Ensure all directories exist
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)
os.makedirs(COOKIE_FOLDER, exist_ok=True)

# Main cookie file
MAIN_COOKIE_FILE = os.path.join(COOKIE_FOLDER, "main_cookie.txt")

# Look for cookie files in multiple locations
COOKIE_FILE_PATHS = [
    MAIN_COOKIE_FILE,
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
        logger.info(f"Found cookie file at: {COOKIE_FILE}")
        break

if not COOKIE_FILE:
    logger.warning("No cookie file found in any of the expected locations")

# Define yt-dlp version to use
YTDLP_VERSION = '2025.4.30'

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
                        logger.info(f"Cleaned up completed download: {file_path}")
                    except Exception as e:
                        logger.error(f"Error deleting {file_path}: {e}")
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
                        logger.info(f"Cleaned up old file: {file_path}")
                    except Exception as e:
                        logger.error(f"Error deleting {file_path}: {e}")

        time.sleep(300)  # Check every 5 minutes

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

def validate_cookie_file(cookie_path):
    """Validate the cookie file format"""
    try:
        if not os.path.exists(cookie_path):
            return False, "File does not exist"
            
        with open(cookie_path, 'r') as f:
            content = f.read().strip()
            
        if not content:
            return False, "Cookie file is empty"
            
        # Check for basic Netscape cookie file format header
        if not content.startswith("# Netscape") and not content.startswith("# HTTP"):
            # Try to detect if it's JSON format and convert it
            try:
                if content.startswith('{') or content.startswith('['):
                    json_data = json.loads(content)
                    # Convert JSON to Netscape format
                    with open(cookie_path, 'w') as f:
                        f.write("# Netscape HTTP Cookie File\n")
                        # Add basic format
                        for cookie in json_data:
                            domain = cookie.get('domain', '.youtube.com')
                            path = cookie.get('path', '/')
                            secure = 'TRUE' if cookie.get('secure', True) else 'FALSE'
                            expires = str(cookie.get('expirationDate', int(time.time() + 86400)))
                            name = cookie.get('name', '')
                            value = cookie.get('value', '')
                            if name and value:
                                f.write(f"{domain}\tTRUE\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
                    return True, "Converted JSON cookie to Netscape format"
            except json.JSONDecodeError:
                # Look for YouTube cookies in non-standard format
                if 'youtube' in content.lower() and ('=' in content or ':' in content):
                    # Try to extract in a simple format
                    with open(cookie_path, 'w') as f:
                        f.write("# Netscape HTTP Cookie File\n")
                        for line in content.split('\n'):
                            if '=' in line or ':' in line:
                                # Extract name-value pairs
                                sep = '=' if '=' in line else ':'
                                parts = line.split(sep, 1)
                                if len(parts) == 2:
                                    name = parts[0].strip()
                                    value = parts[1].strip()
                                    f.write(f".youtube.com\tTRUE\t/\tTRUE\t{int(time.time() + 86400)}\t{name}\t{value}\n")
                    return True, "Converted simple cookie format to Netscape format"
                
                return False, "Cookie file does not appear to be in Netscape or JSON format"
        
        # Check for critical YouTube cookies
        important_cookies = ['SID', 'HSID', 'SSID', 'APISID', 'SAPISID', '__Secure-3PAPISID']
        found_important = False
        
        for cookie in important_cookies:
            if cookie in content:
                found_important = True
                break
                
        if not found_important:
            return False, "No critical YouTube authentication cookies found"
            
        return True, "Cookie file appears valid"
        
    except Exception as e:
        return False, f"Error validating cookie file: {str(e)}"

def get_base_ydl_opts():
    """Return base yt-dlp options with enhanced cookie handling"""
    global COOKIE_FILE
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'no_check_certificate': True,  # Skip HTTPS certificate validation
        'rm_cache_dir': True,          # Clean cache directory
    }

    # Always use the main cookie file if it exists
    if os.path.exists(MAIN_COOKIE_FILE):
        COOKIE_FILE = MAIN_COOKIE_FILE
    
    # Add cookie file if exists with proper error handling
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        try:
            # Validate the cookie file
            valid, message = validate_cookie_file(COOKIE_FILE)
            
            if valid:
                logger.info(f"Using cookie file: {COOKIE_FILE} - {message}")
                ydl_opts['cookiefile'] = COOKIE_FILE
            else:
                logger.warning(f"Cookie file issue: {message}")
        except Exception as e:
            logger.error(f"Error reading cookie file: {e}")
    else:
        logger.warning(f"Cookie file not found at: {COOKIE_FILE}")
        
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

# Cookie management routes
@app.route('/api/cookie/upload', methods=['POST'])
def upload_cookie():
    """
    Upload a cookie file via API
    
    Parameters:
    - file: cookie file (form-data)
    - format: cookie format (optional, defaults to "netscape")
    
    Returns:
    - Status of the cookie upload
    """
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
        
    # Save the cookie file
    try:
        file.save(MAIN_COOKIE_FILE)
        
        # Validate the cookie file
        valid, message = validate_cookie_file(MAIN_COOKIE_FILE)
        
        if valid:
            global COOKIE_FILE
            COOKIE_FILE = MAIN_COOKIE_FILE
            return jsonify({
                "status": "success", 
                "message": f"Cookie file uploaded successfully. {message}"
            })
        else:
            return jsonify({
                "status": "warning",
                "message": f"Cookie file uploaded, but validation raised concerns: {message}"
            })
            
    except Exception as e:
        return jsonify({"error": f"Failed to save cookie file: {str(e)}"}), 500

@app.route('/api/cookie/text', methods=['POST'])
def upload_cookie_text():
    """
    Upload cookie data as text
    
    Parameters:
    - cookie_text: raw cookie text/data (JSON or form-data)
    
    Returns:
    - Status of the cookie upload
    """
    try:
        # Try to get from form data
        cookie_text = request.form.get('cookie_text')
        
        # If not in form data, try JSON
        if not cookie_text and request.is_json:
            data = request.get_json()
            cookie_text = data.get('cookie_text')
            
        # If still not found, check for raw data
        if not cookie_text:
            cookie_text = request.data.decode('utf-8')
            
        if not cookie_text:
            return jsonify({"error": "No cookie data provided"}), 400
            
        # Save the cookie text to file
        with open(MAIN_COOKIE_FILE, 'w') as f:
            f.write(cookie_text)
            
        # Validate the cookie file
        valid, message = validate_cookie_file(MAIN_COOKIE_FILE)
        
        if valid:
            global COOKIE_FILE
            COOKIE_FILE = MAIN_COOKIE_FILE
            return jsonify({
                "status": "success", 
                "message": f"Cookie data saved successfully. {message}"
            })
        else:
            return jsonify({
                "status": "warning",
                "message": f"Cookie data saved, but validation raised concerns: {message}"
            })
            
    except Exception as e:
        return jsonify({"error": f"Failed to save cookie data: {str(e)}"}), 500

@app.route('/api/cookie/status', methods=['GET'])
def cookie_status():
    """
    Check cookie file status
    
    Returns:
    - Information about the current cookie file
    """
    if not COOKIE_FILE or not os.path.exists(COOKIE_FILE):
        return jsonify({
            "status": "missing",
            "message": "No cookie file found"
        })
        
    try:
        valid, message = validate_cookie_file(COOKIE_FILE)
        
        with open(COOKIE_FILE, 'r') as f:
            # Read first 5 lines to get a preview (excluding sensitive data)
            preview_lines = []
            for i, line in enumerate(f):
                if i >= 5:
                    break
                # Mask actual cookie values for privacy
                if '\t' in line and line.count('\t') >= 6:
                    parts = line.split('\t')
                    if len(parts) >= 7:
                        preview_lines.append(f"{parts[0]}\t...\t{parts[5]}\t***MASKED***")
                else:
                    # For headers or non-standard lines
                    preview_lines.append(line.strip())
            
        return jsonify({
            "status": "valid" if valid else "invalid",
            "message": message,
            "path": COOKIE_FILE,
            "size_bytes": os.path.getsize(COOKIE_FILE),
            "last_modified": time.ctime(os.path.getmtime(COOKIE_FILE)),
            "preview": preview_lines
        })
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Error reading cookie file: {str(e)}",
            "path": COOKIE_FILE
        })

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
        logger.info(f"Extracting info for URL: {url}")
        
        # Check if cookie file is being used
        if 'cookiefile' in ydl_opts:
            logger.info(f"Using cookie file: {ydl_opts['cookiefile']}")
        else:
            logger.warning("No cookie file being used")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract full info with formats
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    logger.error("Info extraction returned None")
                    return jsonify({"error": "Could not extract video information"}), 500
            except Exception as e:
                error_message = str(e)
                logger.error(f"Error during info extraction: {error_message}")
                
                # Return more helpful error message
                if "Sign in to confirm you're not a bot" in error_message:
                    return jsonify({
                        "error": "YouTube requires authentication. Upload a valid cookie file using /api/cookie/upload",
                        "details": error_message,
                        "solution": "Upload valid YouTube cookies using POST to /api/cookie/upload or /api/cookie/text"
                    }), 401
                else:
                    return jsonify({"error": f"Error extracting video info: {error_message}"}), 500

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
        logger.error(f"Error merging files: {e}")
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

    # Download not found
    return jsonify({
        "download_id": download_id,
        "status": "not_found",
        "message": "Download not found. It may have been cleaned up or never existed."
    }), 404

@app.route('/api/get-file/<download_id>', methods=['GET'])
def get_file(download_id):
    """
    Get the downloaded file

    Path parameters:
    - download_id: ID of the completed download

    Returns:
    - Video file for download
    """
    # Check if download is completed
    if download_id in completed_downloads:
        info = completed_downloads[download_id]
        
        if info["status"] != "completed":
            return jsonify({
                "error": "Download failed or is still in progress", 
                "status": info["status"]
            }), 400
            
        file_path = info.get("file_path")
        
        if not file_path or not os.path.exists(file_path):
            return jsonify({
                "error": "File not found or has been cleaned up",
                "status": "file_missing"
            }), 404
            
        # Get video file name
        filename = os.path.basename(file_path)
        
        # Extract title from database if available
        title = None
        if "title" in info:
            title = info["title"]
            # Create a safe filename
            safe_title = secure_filename(title)
            if safe_title:
                filename = f"{safe_title}.mp4"
        
        # Send file for download
        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype='video/mp4'
        )
        
    # Download not found
    return jsonify({
        "error": "Download not found",
        "status": "not_found"
    }), 404

@app.route('/api/direct-download/<video_id>/<format_id>', methods=['GET'])
def direct_download(video_id, format_id):
    """
    Direct download of a specific format

    Path parameters:
    - video_id: YouTube video ID
    - format_id: Format ID to download

    Returns:
    - File download of the requested format
    """
    try:
        download_id = f"direct_{video_id}_{format_id}_{str(uuid.uuid4())[:8]}"
        url = f"https://www.youtube.com/watch?v={video_id}"
        
        # Set up options for direct download
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'format': format_id,
            'outtmpl': os.path.join(TEMP_FOLDER, f"{download_id}.%(ext)s"),
        })
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url)
            title = info.get('title', video_id)
            downloaded_file = ydl.prepare_filename(info)
            
            # Find the actual downloaded file (extension might be different)
            actual_file = downloaded_file
            if not os.path.exists(actual_file):
                # Get the file extension from the info
                ext = info.get('ext', 'mp4')
                # Try with correct extension
                actual_file = downloaded_file.rsplit(".", 1)[0] + f".{ext}"
                if not os.path.exists(actual_file):
                    # Try to find the file with different extensions
                    for ext in ['mp4', 'webm', 'mkv', 'm4a', 'mp3']:
                        candidate = downloaded_file.rsplit(".", 1)[0] + f".{ext}"
                        if os.path.exists(candidate):
                            actual_file = candidate
                            break
            
            if not os.path.exists(actual_file):
                return jsonify({
                    "error": "Download failed, file not found",
                    "status": "failed"
                }), 500
                
            # Create safe filename
            safe_title = secure_filename(title)
            if not safe_title:
                safe_title = video_id
                
            # Determine file extension
            file_ext = os.path.splitext(actual_file)[1]
            if not file_ext:
                file_ext = '.mp4'  # Default to mp4
                
            # Determine mimetype
            mimetype = 'video/mp4'
            if file_ext == '.mp3' or file_ext == '.m4a':
                mimetype = 'audio/mpeg'
            elif file_ext == '.webm':
                mimetype = 'video/webm'
                
            # Create download filename
            download_name = f"{safe_title}{file_ext}"
            
            # Send file
            return send_file(
                actual_file,
                as_attachment=True,
                download_name=download_name,
                mimetype=mimetype
            )
            
    except Exception as e:
        logger.error(f"Direct download error: {e}")
        return jsonify({
            "error": f"Download failed: {str(e)}",
            "status": "failed"
        }), 500

@app.route('/api/version', methods=['GET'])
def get_version():
    """Get API and yt-dlp version information"""
    try:
        return jsonify({
            "api_version": "1.0.0",
            "yt_dlp_version": YTDLP_VERSION,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "cookie_file": COOKIE_FILE is not None,
            "cookie_file_path": COOKIE_FILE
        })
    except Exception as e:
        return jsonify({
            "error": f"Error getting version info: {str(e)}"
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Simple health check endpoint"""
    return jsonify({
        "status": "ok",
        "timestamp": time.time()
    })

# Simple static page for the API documentation
@app.route('/', methods=['GET'])
def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>YouTube Download API</title>
        <style>
            body { font-family: Arial, sans-serif; line-height: 1.6; margin: 0; padding: 20px; color: #333; max-width: 800px; margin: 0 auto; }
            h1 { color: #e62117; }
            h2 { margin-top: 20px; color: #444; }
            code { background: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-family: monospace; }
            pre { background: #f4f4f4; padding: 10px; border-radius: 5px; overflow-x: auto; }
            .endpoint { margin-bottom: 20px; border-bottom: 1px solid #eee; padding-bottom: 20px; }
            .method { font-weight: bold; color: #e62117; }
        </style>
    </head>
    <body>
        <h1>YouTube Download API</h1>
        <p>API for extracting YouTube video information and downloading videos using yt-dlp.</p>
        
        <h2>Endpoints:</h2>
        
        <div class="endpoint">
            <h3><span class="method">GET</span> /api/video-info</h3>
            <p>Get video information including available formats</p>
            <p><strong>Query params:</strong> <code>url</code> (YouTube video URL)</p>
        </div>
        
        <div class="endpoint">
            <h3><span class="method">GET</span> /api/download</h3>
            <p>Download a video and combine with best audio</p>
            <p><strong>Query params:</strong></p>
            <ul>
                <li><code>url</code> (YouTube video URL)</li>
                <li><code>format_id</code> (Optional) Specific video format ID</li>
                <li><code>audio_id</code> (Optional) Specific audio format ID</li>
            </ul>
        </div>
        
        <div class="endpoint">
            <h3><span class="method">GET</span> /api/download-status/{download_id}</h3>
            <p>Check the status of a download</p>
        </div>
        
        <div class="endpoint">
            <h3><span class="method">GET</span> /api/get-file/{download_id}</h3>
            <p>Get the downloaded file</p>
        </div>
        
        <div class="endpoint">
            <h3><span class="method">GET</span> /api/direct-download/{video_id}/{format_id}</h3>
            <p>Direct download of a specific format</p>
        </div>
        
        <div class="endpoint">
            <h3><span class="method">POST</span> /api/cookie/upload</h3>
            <p>Upload a cookie file via API</p>
        </div>
        
        <div class="endpoint">
            <h3><span class="method">POST</span> /api/cookie/text</h3>
            <p>Upload cookie data as text</p>
        </div>
        
        <div class="endpoint">
            <h3><span class="method">GET</span> /api/cookie/status</h3>
            <p>Check cookie file status</p>
        </div>
        
        <div class="endpoint">
            <h3><span class="method">GET</span> /api/version</h3>
            <p>Get API and yt-dlp version information</p>
        </div>
        
        <div class="endpoint">
            <h3><span class="method">GET</span> /health</h3>
            <p>Simple health check endpoint</p>
        </div>
    </body>
    </html>
    """

if __name__ == '__main__':
    # Get port from environment or use default
    port = int(os.environ.get('PORT', 5000))
    
    # Start the Flask app
    app.run(host='0.0.0.0', port=port, debug=False)
