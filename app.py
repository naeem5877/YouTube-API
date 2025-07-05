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
            # Delete files older than 30 minutes
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
    """Return base yt-dlp options with enhanced configuration"""
    ydl_opts = {
        'quiet': False,  # Set to False for debugging
        'no_warnings': False,  # Set to False for debugging
        'ignoreerrors': False,  # Don't ignore errors to see what's happening
        'writesubtitles': False,
        'writeautomaticsub': False,
        'extract_flat': False,
        # Add user agent to mimic a real browser
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        },
        # Add extractor arguments for better compatibility
        'extractor_args': {
            'youtube': {
                'skip': ['dash', 'hls'],  # Skip DASH and HLS formats that might cause issues
                'player_skip': ['js'],  # Skip JavaScript player
            }
        },
        # Enable IPv6 and other network options
        'prefer_ipv6': False,
        'socket_timeout': 30,
        'retries': 3,
        # Add output template to avoid special characters
        'restrictfilenames': True,
        'trim_filename': 200,
    }

    # Add cookie file if it exists
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

def format_filesize(size):
    """Convert filesize to human readable format"""
    if not size:
        return "Unknown"

    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

@app.route('/api/video-info', methods=['GET'])
def get_video_info():
    """Get video information including available formats"""
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "Missing video URL"}), 400

    try:
        ydl_opts = get_base_ydl_opts()
        # Add specific options for info extraction
        ydl_opts.update({
            'extract_flat': False,
            'dump_single_json': False,
            'simulate': True,  # Don't actually download
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                # If extraction fails, try with different options
                print(f"First extraction failed: {e}")
                ydl_opts.update({
                    'youtube_include_dash_manifest': False,
                    'format': 'best',
                    'extractor_args': {
                        'youtube': {
                            'skip': ['dash', 'hls'],
                            'player_skip': ['js'],
                        }
                    }
                })
                with yt_dlp.YoutubeDL(ydl_opts) as ydl2:
                    info = ydl2.extract_info(url, download=False)

            video_id = info.get('id')

            # Extract relevant information
            result = {
                "id": video_id,
                "title": info.get('title'),
                "description": info.get('description')[:1000] if info.get('description') else None,
                "duration": info.get('duration'),
                "view_count": info.get('view_count'),
                "like_count": info.get('like_count'),
                "upload_date": info.get('upload_date'),
                "thumbnails": info.get('thumbnails', []),
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

            # Try to extract channel profile picture
            for thumbnail in info.get('thumbnails', []):
                if 'url' in thumbnail and 'avatar' in thumbnail.get('id', ''):
                    result['channel']['profile_picture'] = thumbnail['url']
                    break

            # Extract and categorize formats
            formats = info.get('formats', [])

            # Audio-only formats
            audio_formats = []
            # Video formats (including video+audio combined)
            video_formats = []

            for fmt in formats:
                format_id = fmt.get('format_id')
                if not format_id:
                    continue

                # Check if it's audio-only
                if fmt.get('vcodec') == 'none' and fmt.get('acodec') != 'none':
                    audio_formats.append({
                        "format_id": format_id,
                        "ext": fmt.get('ext'),
                        "filesize": fmt.get('filesize'),
                        "filesize_human": format_filesize(fmt.get('filesize')),
                        "format_note": fmt.get('format_note', ''),
                        "abr": fmt.get('abr'),
                        "acodec": fmt.get('acodec'),
                        "quality": fmt.get('quality'),
                        "download_url": f"/api/direct-download/{video_id}/{format_id}"
                    })

                # Check if it's video (with or without audio)
                elif fmt.get('vcodec') != 'none':
                    # Determine quality label
                    height = fmt.get('height')
                    quality_label = "Unknown"
                    if height:
                        if height <= 144:
                            quality_label = "144p"
                        elif height <= 240:
                            quality_label = "240p"
                        elif height <= 360:
                            quality_label = "360p"
                        elif height <= 480:
                            quality_label = "480p"
                        elif height <= 720:
                            quality_label = "720p (HD)"
                        elif height <= 1080:
                            quality_label = "1080p (Full HD)"
                        elif height <= 1440:
                            quality_label = "1440p (2K)"
                        elif height <= 2160:
                            quality_label = "2160p (4K)"
                        elif height <= 4320:
                            quality_label = "4320p (8K)"
                        else:
                            quality_label = f"{height}p"

                    video_formats.append({
                        "format_id": format_id,
                        "ext": fmt.get('ext'),
                        "filesize": fmt.get('filesize'),
                        "filesize_human": format_filesize(fmt.get('filesize')),
                        "format_note": fmt.get('format_note', ''),
                        "width": fmt.get('width'),
                        "height": height,
                        "fps": fmt.get('fps'),
                        "vcodec": fmt.get('vcodec'),
                        "acodec": fmt.get('acodec'),
                        "quality_label": quality_label,
                        "has_audio": fmt.get('acodec') != 'none',
                        "download_url": f"/api/direct-download/{video_id}/{format_id}",
                        "resolution": f"{fmt.get('width', 0)}x{fmt.get('height', 0)}"
                    })

            # Sort formats by quality
            audio_formats.sort(key=lambda x: x.get('abr', 0) or 0, reverse=True)
            video_formats.sort(key=lambda x: x.get('height', 0) or 0, reverse=True)

            result["audio_formats"] = audio_formats
            result["video_formats"] = video_formats

            return jsonify(result)

    except Exception as e:
        print(f"Error in video-info: {e}")
        return jsonify({"error": f"Failed to extract video info: {str(e)}"}), 500

@app.route('/api/download', methods=['GET'])
def download_video():
    """Download a video with audio combined"""
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
        "message": "Download started. Video will be combined with audio automatically.",
        "note": "Files are temporarily stored and will be deleted after 30 minutes."
    })

def process_download(download_id, url, format_id=None, audio_id=None):
    """Process video download with audio combination"""
    downloads_in_progress[download_id] = {
        "status": "downloading",
        "progress": 0,
        "url": url,
        "start_time": time.time()
    }

    try:
        output_filename = f"{download_id}.%(ext)s"
        output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)

        # Configure yt-dlp options
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'outtmpl': output_path,
            'progress_hooks': [lambda d: update_progress(download_id, d)],
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'prefer_ffmpeg': True,
            'ffmpeg_location': '/usr/bin/ffmpeg',  # Common location on Linux
        })

        # Determine format selection with better audio handling
        if format_id and audio_id:
            # Specific video + specific audio
            ydl_opts['format'] = f"{format_id}+{audio_id}/best[height<=1080]"
        elif format_id:
            # Specific video + best audio - ensure audio is included
            ydl_opts['format'] = f"{format_id}+bestaudio[ext=m4a]/best[height<=1080]"
            # Add post-processor to ensure audio is merged
            ydl_opts['postprocessors'].append({
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            })
        elif audio_id:
            # Audio only
            ydl_opts['format'] = audio_id
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            # Best video + best audio with fallback
            ydl_opts['format'] = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'

        # Always merge to mp4 for video downloads
        if not audio_id or format_id:
            ydl_opts['merge_output_format'] = 'mp4'

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Find the downloaded file
        downloaded_file = None
        for file in os.listdir(DOWNLOAD_FOLDER):
            if file.startswith(download_id):
                downloaded_file = os.path.join(DOWNLOAD_FOLDER, file)
                break

        if downloaded_file and os.path.exists(downloaded_file):
            completed_downloads[download_id] = {
                "status": "completed",
                "url": url,
                "file_path": downloaded_file,
                "download_url": f"/api/get-file/{download_id}",
                "completion_time": time.time()
            }
        else:
            raise Exception("Download completed but file not found")

    except Exception as e:
        print(f"Download error: {e}")
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
                percent_str = d.get('_percent_str', '0%')
                if percent_str:
                    downloads_in_progress[download_id]['progress'] = float(percent_str.replace('%', '').strip())
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
    """Direct download with automatic audio combination"""
    audio_id = request.args.get('audio_id')
    custom_filename = request.args.get('filename')
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        # Create filename based on parameters
        if audio_id:
            filename = f"{video_id}_{format_id}_{audio_id}.%(ext)s"
        else:
            filename = f"{video_id}_{format_id}.%(ext)s"

        output_path = os.path.join(DOWNLOAD_FOLDER, filename)

        # Set up download options with better audio handling
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'outtmpl': output_path,
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'prefer_ffmpeg': True,
            'ffmpeg_location': '/usr/bin/ffmpeg',
        })

        # Get format information first
        with yt_dlp.YoutubeDL(get_base_ydl_opts()) as temp_ydl:
            info = temp_ydl.extract_info(url, download=False)
            target_format = None
            best_audio = None
            
            # Find the target format and best audio
            for fmt in info.get('formats', []):
                if fmt.get('format_id') == format_id:
                    target_format = fmt
                if fmt.get('vcodec') == 'none' and fmt.get('acodec') != 'none':
                    if not best_audio or (fmt.get('abr', 0) > best_audio.get('abr', 0)):
                        best_audio = fmt

        if not target_format:
            return jsonify({"error": "Format not found"}), 404

        # Determine format selection with enhanced audio handling
        if audio_id:
            # Specific audio requested
            ydl_opts['format'] = f"{format_id}+{audio_id}/best"
        elif target_format.get('vcodec') == 'none':
            # Audio only format
            ydl_opts['format'] = format_id
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            # Video format - ensure audio is combined
            if target_format.get('acodec') == 'none':
                # Video without audio - must combine with audio
                if best_audio:
                    ydl_opts['format'] = f"{format_id}+{best_audio['format_id']}/best"
                else:
                    ydl_opts['format'] = f"{format_id}+bestaudio/best"
            else:
                # Video with audio
                ydl_opts['format'] = format_id
            
            # Always merge to mp4 for video
            ydl_opts['merge_output_format'] = 'mp4'

        # Download the file
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url)

            # Get the actual title for filename
            if not custom_filename and info.get('title'):
                video_title = info.get('title')
                # Clean the title for use as filename
                video_title = ''.join(c for c in video_title if c.isalnum() or c in ' ._-')[:100]
                # Add extension based on format
                if target_format and target_format.get('vcodec') == 'none':
                    # Audio format
                    download_name = f"{video_title}.mp3"
                else:
                    # Video format
                    download_name = f"{video_title}.mp4"
            else:
                download_name = custom_filename if custom_filename else f"{video_id}.mp4"

        # Find the downloaded file
        downloaded_file = None
        for file in os.listdir(DOWNLOAD_FOLDER):
            if file.startswith(video_id) and format_id in file:
                downloaded_file = os.path.join(DOWNLOAD_FOLDER, file)
                break

        if downloaded_file and os.path.exists(downloaded_file):
            return send_file(downloaded_file, as_attachment=True, download_name=download_name)
        else:
            return jsonify({"error": "Download failed - file not found"}), 500

    except Exception as e:
        print(f"Direct download error: {e}")
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
        "version": "2.1.0",
        "platform": "render",
        "cookie_file_exists": os.path.exists(COOKIE_FILE),
        "ffmpeg_available": os.path.exists('/usr/bin/ffmpeg'),
        "storage_info": "Ephemeral storage - files deleted after 30 minutes",
        "features": [
            "All video qualities (144p to 8K)",
            "Audio-only formats",
            "Automatic video+audio combination",
            "Format caching",
            "Enhanced error handling"
        ]
    })

@app.route('/', methods=['GET'])
def root():
    """Root endpoint"""
    return jsonify({
        "message": "YouTube Downloader API v2.1",
        "description": "Supports all video qualities with guaranteed audio combination",
        "version": "2.1.0",
        "endpoints": [
            "/api/health",
            "/api/video-info",
            "/api/download",
            "/api/download-status/<id>",
            "/api/get-file/<id>",
            "/api/direct-download/<video_id>/<format_id>",
            "/api/upload-cookie"
        ],
        "features": [
            "144p to 8K video quality support",
            "Audio-only downloads",
            "Automatic video+audio combination",
            "Progress tracking",
            "File caching",
            "Enhanced error handling"
        ]
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
