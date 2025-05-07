from flask import Flask, request, jsonify, send_file, Response
import yt_dlp
import os
import uuid
import threading
import time
import json
from werkzeug.utils import secure_filename
import logging

# Set up logging
logging.basicConfig(level=logging.INFO,
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

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

# Max request timeout (in seconds) - critical for preventing worker timeouts
REQUEST_TIMEOUT = 25  # Keep requests under 30 seconds to avoid platform timeouts

# Function to clean up old files periodically
def cleanup_old_files():
    while True:
        try:
            now = time.time()
            # Delete files older than 30 minutes to save space
            for folder in [DOWNLOAD_FOLDER, TEMP_FOLDER]:
                for filename in os.listdir(folder):
                    file_path = os.path.join(folder, filename)
                    if os.path.isfile(file_path) and now - os.path.getmtime(file_path) > 1800:  # 30 minutes
                        try:
                            os.remove(file_path)
                            logger.info(f"Cleaned up old file: {file_path}")
                        except Exception as e:
                            logger.error(f"Error deleting {file_path}: {e}")

            # Clean up completed downloads dictionary
            to_remove = []
            for download_id, info in completed_downloads.items():
                if now - info.get("completion_time", 0) > 1800:  # 30 minutes
                    to_remove.append(download_id)

            for download_id in to_remove:
                completed_downloads.pop(download_id, None)
                logger.info(f"Cleaned up completed download record: {download_id}")

            time.sleep(3600)  # Check every hour
        except Exception as e:
            logger.error(f"Error in cleanup thread: {e}")
            time.sleep(3600)  # Still sleep on error

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

def get_base_ydl_opts():
    """Return base yt-dlp options with cookie file if exists"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'socket_timeout': 10,  # Lower socket timeout to avoid hanging
    }

    if os.path.exists(COOKIE_FILE):
        ydl_opts['cookiefile'] = COOKIE_FILE

    return ydl_opts

def get_verification_status(info_dict):
    """Improved check if channel is verified based on badges or other indicators"""
    # Check different possible locations for verification status
    if info_dict.get('verified', False):
        return True
    
    # Check channel badges if available
    badges = info_dict.get('badges', [])
    if badges:
        for badge in badges:
            if badge and isinstance(badge, dict) and 'verified' in str(badge).lower():
                return True
    
    # Check for verification in channel name or description
    channel_name = info_dict.get('channel', info_dict.get('uploader', ''))
    if channel_name and '✓' in channel_name:
        return True
        
    return False

def get_subscriber_count(info_dict):
    """Extract subscriber count from channel information"""
    # Try different possible locations for subscriber count
    subscriber_count = info_dict.get('channel_follower_count')
    
    if not subscriber_count:
        subscriber_count = info_dict.get('subscriber_count')
    
    # Some versions report it under channel_is_subscribed
    if not subscriber_count:
        channel_info = info_dict.get('channel')
        if isinstance(channel_info, dict):
            subscriber_count = channel_info.get('subscriber_count')
    
    return subscriber_count

@app.route('/api/video-info', methods=['GET'])
def get_video_info():
    """
    Get video information including available formats
    
    This endpoint is optimized to return quickly to avoid worker timeouts
    """
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "Missing video URL"}), 400

    try:
        ydl_opts = get_base_ydl_opts()
        
        # These options make extraction faster
        ydl_opts.update({
            'extract_flat': False,
            'skip_download': True,
            'timeout': 10,  # Timeout for connections
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Use a timeout to prevent hanging
            info = ydl.extract_info(url, download=False)
            if not info:
                return jsonify({"error": "Could not extract video information"}), 500
                
            video_id = info.get('id')

            # Extract only necessary information to make response lighter
            result = {
                "id": video_id,
                "title": info.get('title'),
                "description": info.get('description', '')[:500] + ('...' if info.get('description', '') and len(info.get('description', '')) > 500 else ''),  # Truncate long descriptions
                "duration": info.get('duration'),
                "view_count": info.get('view_count'),
                "like_count": info.get('like_count'),
                "upload_date": info.get('upload_date'),
                "channel": {
                    "id": info.get('channel_id'),
                    "name": info.get('channel', info.get('uploader')),
                    "url": info.get('channel_url'),
                    "verified": get_verification_status(info),
                    "subscriber_count": get_subscriber_count(info)
                },
                "audio_formats": [],
                "video_formats": []
            }
            
            # Add main thumbnail only
            if info.get('thumbnails') and len(info.get('thumbnails')) > 0:
                result['thumbnail'] = info.get('thumbnails')[-1].get('url')
            else:
                result['thumbnail'] = None

            # Try to extract channel profile picture if available
            for thumbnail in info.get('thumbnails', []):
                if 'url' in thumbnail and ('avatar' in thumbnail.get('id', '') or 'avatar' in thumbnail.get('url', '')):
                    result['channel']['profile_picture'] = thumbnail['url']
                    break
            
            if 'channel' in result and 'profile_picture' not in result['channel']:
                result['channel']['profile_picture'] = None

            # Extract audio formats - limit to most common ones to reduce response size
            audio_formats = []
            for format in info.get('formats', []):
                if format.get('vcodec') == 'none' and format.get('acodec') != 'none':
                    # Only include formats with reasonable quality
                    if format.get('abr', 0) > 48:  # Skip very low quality
                        audio_formats.append({
                            "format_id": format.get('format_id'),
                            "ext": format.get('ext'),
                            "filesize": format.get('filesize'),
                            "format_note": format.get('format_note'),
                            "abr": format.get('abr'),
                            "download_url": f"/api/direct-download/{video_id}/{format.get('format_id')}"
                        })

            # Sort audio formats by quality (descending)
            audio_formats.sort(key=lambda x: x.get('abr', 0), reverse=True)
            
            # Limit to top 5 audio formats
            result["audio_formats"] = audio_formats[:5]

            # Extract video formats with direct download links
            video_formats = []
            common_resolutions = [2160, 1440, 1080, 720, 480, 360, 240, 144]  # Focus on common resolutions
            
            format_by_resolution = {}  # To track the best format for each resolution
            
            for format in info.get('formats', []):
                if format.get('vcodec') != 'none':
                    height = format.get('height', 0)
                    
                    # Skip non-standard resolutions unless we don't have any formats yet
                    if height not in common_resolutions and len(format_by_resolution) > 0:
                        continue
                    
                    # Track the best format for each resolution
                    if height not in format_by_resolution or format.get('tbr', 0) > format_by_resolution[height].get('tbr', 0):
                        format_by_resolution[height] = {
                            "format_id": format.get('format_id'),
                            "ext": format.get('ext'),
                            "filesize": format.get('filesize'),
                            "format_note": format.get('format_note'),
                            "width": format.get('width'),
                            "height": format.get('height'),
                            "fps": format.get('fps'),
                            "vcodec": format.get('vcodec'),
                            "acodec": format.get('acodec'),
                            "tbr": format.get('tbr'),
                            "download_url": f"/api/direct-download/{video_id}/{format.get('format_id')}",
                            "resolution": f"{format.get('width', 0)}x{format.get('height', 0)}"
                        }
            
            # Get the best format for each resolution
            for height, format_info in format_by_resolution.items():
                video_formats.append(format_info)
            
            # Sort video formats by height (descending)
            video_formats.sort(key=lambda x: x.get('height', 0), reverse=True)
            
            result["video_formats"] = video_formats

            return jsonify(result)

    except Exception as e:
        logger.error(f"Error getting video info: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/download', methods=['GET'])
def download_video():
    """
    Start a download in the background to avoid worker timeouts
    """
    url = request.args.get('url')
    format_id = request.args.get('format_id')
    audio_id = request.args.get('audio_id')

    if not url:
        return jsonify({"error": "Missing video URL"}), 400

    download_id = str(uuid.uuid4())

    # Start download in background thread
    thread = threading.Thread(
        target=process_download,
        args=(download_id, url, format_id, audio_id)
    )
    thread.daemon = True
    thread.start()

    # Return immediately with download ID
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
        # First get video info to use title in filename
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'quiet': True,
            'skip_download': True,
        })
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                video_title = info.get('title', 'video')
            except Exception as e:
                logger.error(f"Error getting video info in process_download: {str(e)}")
                video_title = f"download_{download_id}"
            
            # Clean title for safe filename
            video_title = ''.join(c for c in video_title if c.isalnum() or c in ' ._-')
            video_title = video_title.strip()
            
        output_filename = f"VibeDownloader - {video_title}.mp4"
        output_path = os.path.join(DOWNLOAD_FOLDER, f"{download_id}_{output_filename}")

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
                if video_path and audio_path:
                    merge_video_audio(video_path, audio_path, output_path)

                    # Clean up temp files
                    try:
                        if os.path.exists(video_path):
                            os.remove(video_path)
                        if os.path.exists(audio_path):
                            os.remove(audio_path)
                    except Exception as e:
                        logger.error(f"Error removing temp files: {str(e)}")
                else:
                    raise Exception("Failed to download video or audio")

            else:
                # Download specific format and merge with best audio
                ydl_opts.update({
                    'format': f"{format_id}+bestaudio",
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
        else:
            # Download best quality and merge
            ydl_opts.update({
                'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',  # Limit to 1080p to avoid huge downloads
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
            "filename": output_filename,
            "completion_time": time.time()
        }
        
        logger.info(f"Download completed: {download_id} - {output_filename}")

    except Exception as e:
        logger.error(f"Download failed {download_id}: {str(e)}")
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
    try:
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
            
            if not os.path.exists(actual_file):
                logger.error(f"Downloaded file not found: {downloaded_file}")
                return None
                
            return actual_file
    except Exception as e:
        logger.error(f"Error downloading specific format: {str(e)}")
        return None

def merge_video_audio(video_path, audio_path, output_path):
    """Merge video and audio files using ffmpeg"""
    import subprocess

    try:
        command = [
            'ffmpeg', '-i', video_path, '-i', audio_path,
            '-c:v', 'copy', '-c:a', 'aac', '-strict', 'experimental',
            '-y',  # Overwrite output file if it exists
            output_path
        ]

        process = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        logger.info(f"Merged files successfully: {output_path}")
        return True
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout merging files")
        return False
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
                pass
        elif d['status'] == 'finished':
            downloads_in_progress[download_id]['status'] = 'processing'
            downloads_in_progress[download_id]['progress'] = 100

@app.route('/api/download-status/<download_id>', methods=['GET'])
def check_download_status(download_id):
    """
    Check the status of a download
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
            "download_url": completed_downloads[download_id].get("download_url"),
            "filename": completed_downloads[download_id].get("filename"),
            "error": completed_downloads[download_id].get("error")
        })

    return jsonify({"error": "Download ID not found"}), 404

@app.route('/api/get-file/<download_id>', methods=['GET'])
def get_downloaded_file(download_id):
    """
    Get a downloaded file
    """
    if download_id in completed_downloads and completed_downloads[download_id]["status"] == "completed":
        file_path = completed_downloads[download_id]["file_path"]

        if os.path.exists(file_path):
            filename = completed_downloads[download_id].get("filename", "download.mp4")
            return send_file(file_path, as_attachment=True, download_name=filename)

    return jsonify({"error": "File not found"}), 404

@app.route('/api/direct-download/<video_id>/<format_id>', methods=['GET'])
def direct_download(video_id, format_id):
    """
    Direct download endpoint that streams the response
    """
    audio_id = request.args.get('audio_id')
    custom_filename = request.args.get('filename')
    url = f"https://www.youtube.com/watch?v={video_id}"

    # Check if a cached version exists
    cache_filename = f"{video_id}_{format_id}"
    if audio_id:
        cache_filename += f"_{audio_id}"
    cache_filename += ".mp4"
    
    cache_path = os.path.join(DOWNLOAD_FOLDER, cache_filename)
    
    if os.path.exists(cache_path):
        # First get video info to make a better filename
        try:
            ydl_opts = get_base_ydl_opts()
            ydl_opts.update({
                'quiet': True,
                'skip_download': True,
            })
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                video_title = info.get('title', video_id)
                video_title = ''.join(c for c in video_title if c.isalnum() or c in ' ._-')
                video_title = video_title.strip()
        except:
            video_title = video_id
        
        download_name = custom_filename if custom_filename else f"VibeDownloader - {video_title}.mp4"
        return send_file(cache_path, as_attachment=True, download_name=download_name)
    
    # If we're here, we need to start the download but without blocking
    download_id = str(uuid.uuid4())
    
    # Create a status for this request
    downloads_in_progress[download_id] = {
        "status": "pending",
        "progress": 0,
        "url": url,
        "start_time": time.time(),
        "format_id": format_id,
        "audio_id": audio_id,
        "video_id": video_id,
        "cache_path": cache_path,
    }
    
    # Start a background download
    thread = threading.Thread(
        target=process_direct_download,
        args=(download_id, url, format_id, audio_id, cache_path, video_id)
    )
    thread.daemon = True
    thread.start()
    
    # Return a status page that will check and redirect when ready
    response_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Download in Progress</title>
        <meta http-equiv="refresh" content="5;url=/api/direct-download/{video_id}/{format_id}?audio_id={audio_id if audio_id else ''}">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; text-align: center; }}
            .progress-container {{ width: 80%; margin: 20px auto; background-color: #f3f3f3; border-radius: 5px; }}
            .progress-bar {{ height: 30px; background-color: #4CAF50; width: 0%; border-radius: 5px; transition: width 0.5s; }}
            .status {{ margin: 20px 0; }}
        </style>
        <script>
            function checkStatus() {{
                fetch('/api/download-status/{download_id}')
                    .then(response => response.json())
                    .then(data => {{
                        if (data.status === "completed") {{
                            window.location.href = "/api/get-file/{download_id}";
                        }} else if (data.status === "failed") {{
                            document.getElementById('status').innerHTML = "Download failed: " + (data.error || "Unknown error");
                            document.getElementById('progress-bar').style.width = "100%";
                            document.getElementById('progress-bar').style.backgroundColor = "#f44336";
                        }} else {{
                            document.getElementById('status').innerHTML = "Download in progress: " + data.progress.toFixed(1) + "%";
                            document.getElementById('progress-bar').style.width = data.progress + "%";
                            setTimeout(checkStatus, 1000);
                        }}
                    }});
            }}
            window.onload = function() {{
                checkStatus();
            }};
        </script>
    </head>
    <body>
        <h1>Preparing Your Download</h1>
        <div class="progress-container">
            <div class="progress-bar" id="progress-bar"></div>
        </div>
        <div class="status" id="status">Starting download...</div>
        <p>Please wait while we prepare your download. This page will automatically refresh.</p>
    </body>
    </html>
    """
    
    return Response(response_html, mimetype='text/html')

def process_direct_download(download_id, url, format_id, audio_id, cache_path, video_id):
    """Process a direct download in the background"""
    try:
        # Set up download options
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'progress_hooks': [lambda d: update_progress(download_id, d)],
        })

        # Set format string
        if audio_id:
            format_str = f"{format_id}+{audio_id}"
        else:
            format_str = f"{format_id}+bestaudio"
            
        ydl_opts.update({
            'format': format_str,
            'outtmpl': cache_path,
            'merge_output_format': 'mp4',
        })

        # Download the file
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url)
            
        # Get video title for a better filename
        try:
            ydl_opts = get_base_ydl_opts()
            ydl_opts.update({
                'quiet': True,
                'skip_download': True,
            })
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                video_title = info.get('title', video_id)
                video_title = ''.join(c for c in video_title if c.isalnum() or c in ' ._-')
                video_title = video_title.strip()
        except:
            video_title = video_id
            
        download_name = f"VibeDownloader - {video_title}.mp4"
        
        # Update downloads info
        completed_downloads[download_id] = {
            "status": "completed",
            "url": url,
            "file_path": cache_path,
            "filename": download_name,
            "completion_time": time.time(),
            "download_url": f"/api/get-file/{download_id}",
        }
        
        logger.info(f"Direct download completed: {download_id} - {download_name}")
        
    except Exception as e:
        logger.error(f"Direct download failed {download_id}: {str(e)}")
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

@app.route('/api/upload-cookie', methods=['POST'])
def upload_cookie():
    """
    Upload cookie.txt file
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

@app.route('/cookie.txt', methods=['GET'])
def get_cookie_file():
    """
    Return cookie.txt file content or empty placeholder if not exists
    """
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, 'r') as f:
            content = f.read()
        return content, 200, {'Content-Type': 'text/plain'}
    else:
        return "# No cookie file uploaded", 404, {'Content-Type': 'text/plain'}

@app.route('/api/health', methods=['GET'])
def health_check():
    """Simple health check endpoint"""
    return jsonify({
        "status": "ok",
        "version": "1.2.0",
        "cookie_file_exists": os.path.exists(COOKIE_FILE),
        "downloads_folder_size_mb": get_folder_size(DOWNLOAD_FOLDER) / (1024 * 1024),
        "temp_folder_size_mb": get_folder_size(TEMP_FOLDER) / (1024 * 1024),
        "active_downloads": len(downloads_in_progress),
        "completed_downloads": len(completed_downloads),
        "uptime_seconds": time.time() - app.config.get("start_time", time.time())
    })

def get_folder_size(folder_path):
    """Calculate total size of a folder in bytes"""
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(folder_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.exists(fp):
                total_size += os.path.getsize(fp)
    return total_size

@app.route('/api/clean-cache', methods=['POST'])
def clean_cache():
    """Manually clean cached files to free up space"""
    try:
        # Count files before cleaning
        files_before = len(os.listdir(DOWNLOAD_FOLDER)) + len(os.listdir(TEMP_FOLDER))
        size_before = get_folder_size(DOWNLOAD_FOLDER) + get_folder_size(TEMP_FOLDER)
        
        # Get active file paths to avoid deleting in-use files
        active_files = []
        for download_info in downloads_in_progress.values():
            if "cache_path" in download_info:
                active_files.append(download_info["cache_path"])
        
        for download_info in completed_downloads.values():
            if "file_path" in download_info and time.time() - download_info.get("completion_time", 0) < 300:  # Keep files completed in last 5 minutes
                active_files.append(download_info["file_path"])
        
        # Clean download folder
        files_deleted = 0
        for filename in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, filename)
            if file_path not in active_files and os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                    files_deleted += 1
                except Exception as e:
                    logger.error(f"Error deleting {file_path}: {e}")
        
        # Clean temp folder
        for filename in os.listdir(TEMP_FOLDER):
            file_path = os.path.join(TEMP_FOLDER, filename)
            if file_path not in active_files and os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                    files_deleted += 1
                except Exception as e:
                    logger.error(f"Error deleting {file_path}: {e}")
        
        # Calculate savings
        size_after = get_folder_size(DOWNLOAD_FOLDER) + get_folder_size(TEMP_FOLDER)
        size_saved = size_before - size_after
        
        return jsonify({
            "success": True,
            "message": f"Cache cleaned successfully. Deleted {files_deleted} files.",
            "space_saved_mb": size_saved / (1024 * 1024),
            "current_size_mb": size_after / (1024 * 1024)
        })
    except Exception as e:
        logger.error(f"Error cleaning cache: {e}")
        return jsonify({"error": f"Error cleaning cache: {str(e)}"}), 500

@app.route('/api/list-downloads', methods=['GET'])
def list_downloads():
    """List all in-progress and completed downloads"""
    in_progress = []
    for download_id, info in downloads_in_progress.items():
        in_progress.append({
            "download_id": download_id,
            "status": info.get("status", "unknown"),
            "progress": info.get("progress", 0),
            "url": info.get("url", ""),
            "elapsed_seconds": time.time() - info.get("start_time", time.time())
        })
    
    completed = []
    for download_id, info in completed_downloads.items():
        completed.append({
            "download_id": download_id,
            "status": info.get("status", "unknown"),
            "url": info.get("url", ""),
            "filename": info.get("filename", ""),
            "download_url": info.get("download_url", ""),
            "completion_time": info.get("completion_time", 0),
            "age_seconds": time.time() - info.get("completion_time", time.time())
        })
    
    return jsonify({
        "in_progress": in_progress,
        "completed": completed
    })

@app.route('/api/cancel-download/<download_id>', methods=['POST'])
def cancel_download(download_id):
    """Cancel an in-progress download"""
    if download_id in downloads_in_progress:
        # Mark as cancelled in the completion dictionary
        completed_downloads[download_id] = {
            "status": "cancelled",
            "url": downloads_in_progress[download_id].get("url", ""),
            "error": "Download cancelled by user",
            "completion_time": time.time()
        }
        
        # Remove from in-progress
        downloads_in_progress.pop(download_id)
        
        return jsonify({
            "success": True,
            "message": f"Download {download_id} cancelled successfully"
        })
    
    return jsonify({"error": "Download not found or already completed"}), 404

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get detailed stats about the service"""
    # Calculate average download time for completed downloads
    download_times = []
    success_count = 0
    failure_count = 0
    
    for info in completed_downloads.values():
        if info.get("status") == "completed":
            success_count += 1
        elif info.get("status") == "failed":
            failure_count += 1
    
    # Get memory usage
    import psutil
    try:
        process = psutil.Process(os.getpid())
        memory_usage = process.memory_info().rss / (1024 * 1024)  # Convert to MB
    except:
        memory_usage = 0
    
    return jsonify({
        "service": {
            "version": "1.2.0",
            "uptime_seconds": time.time() - app.config.get("start_time", time.time()),
            "memory_usage_mb": memory_usage
        },
        "storage": {
            "downloads_folder_size_mb": get_folder_size(DOWNLOAD_FOLDER) / (1024 * 1024),
            "temp_folder_size_mb": get_folder_size(TEMP_FOLDER) / (1024 * 1024),
            "cookie_file_exists": os.path.exists(COOKIE_FILE)
        },
        "downloads": {
            "active": len(downloads_in_progress),
            "completed_success": success_count,
            "completed_failed": failure_count,
            "total_completed": len(completed_downloads)
        }
    })

@app.route('/', methods=['GET'])
def index():
    """Serve a simple frontend for the API"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>VibeDownloader API</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
            }
            h1 {
                color: #2a65a0;
                border-bottom: 2px solid #eee;
                padding-bottom: 10px;
            }
            .endpoint {
                background: #f9f9f9;
                border-left: 4px solid #2a65a0;
                padding: 15px;
                margin-bottom: 20px;
            }
            .method {
                font-weight: bold;
                color: #0a5e0a;
            }
            .url {
                font-family: monospace;
                background-color: #eee;
                padding: 2px 5px;
                border-radius: 3px;
            }
            .description {
                margin-top: 10px;
            }
            footer {
                margin-top: 40px;
                color: #777;
                font-size: 0.9em;
                text-align: center;
                border-top: 1px solid #eee;
                padding-top: 20px;
            }
        </style>
    </head>
    <body>
        <h1>VibeDownloader API</h1>
        <p>Welcome to the VibeDownloader API. Below are the available endpoints:</p>
        
        <div class="endpoint">
            <div><span class="method">GET</span> <span class="url">/api/video-info?url={video_url}</span></div>
            <div class="description">Get video information including available formats.</div>
        </div>
        
        <div class="endpoint">
            <div><span class="method">GET</span> <span class="url">/api/download?url={video_url}&format_id={format_id}&audio_id={audio_id}</span></div>
            <div class="description">Start a video download. Format ID and audio ID are optional.</div>
        </div>
        
        <div class="endpoint">
            <div><span class="method">GET</span> <span class="url">/api/download-status/{download_id}</span></div>
            <div class="description">Check the status of a download.</div>
        </div>
        
        <div class="endpoint">
            <div><span class="method">GET</span> <span class="url">/api/get-file/{download_id}</span></div>
            <div class="description">Download a completed file.</div>
        </div>
        
        <div class="endpoint">
            <div><span class="method">GET</span> <span class="url">/api/direct-download/{video_id}/{format_id}?audio_id={audio_id}</span></div>
            <div class="description">Direct download of a video with specified format. Audio ID is optional.</div>
        </div>
        
        <div class="endpoint">
            <div><span class="method">POST</span> <span class="url">/api/upload-cookie</span></div>
            <div class="description">Upload a cookie.txt file for authenticated downloads.</div>
        </div>
        
        <div class="endpoint">
            <div><span class="method">GET</span> <span class="url">/api/health</span></div>
            <div class="description">Health check endpoint.</div>
        </div>
        
        <div class="endpoint">
            <div><span class="method">POST</span> <span class="url">/api/clean-cache</span></div>
            <div class="description">Clean cached files to free up space.</div>
        </div>
        
        <div class="endpoint">
            <div><span class="method">GET</span> <span class="url">/api/list-downloads</span></div>
            <div class="description">List all in-progress and completed downloads.</div>
        </div>
        
        <div class="endpoint">
            <div><span class="method">POST</span> <span class="url">/api/cancel-download/{download_id}</span></div>
            <div class="description">Cancel an in-progress download.</div>
        </div>
        
        <div class="endpoint">
            <div><span class="method">GET</span> <span class="url">/api/stats</span></div>
            <div class="description">Get detailed stats about the service.</div>
        </div>
        
        <footer>
            VibeDownloader API v1.2.0
        </footer>
    </body>
    </html>
    """

# Store start time for uptime tracking
app.config["start_time"] = time.time()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
