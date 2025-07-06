from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import time
import json
import requests
from werkzeug.utils import secure_filename
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["http://localhost:3000", "https://vibedownloader.vercel.app", "https://vibedownloader.me", "https://www.vibedownloader.me", "https://ytapi.vibedownloader.me/"]}})

# Configuration - Use /tmp for ephemeral storage on Render
DOWNLOAD_FOLDER = "/tmp/downloads"
TEMP_FOLDER = "/tmp/temp"

# Create necessary directories
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# GoProxy Configuration - UPDATE THESE WITH YOUR CREDENTIALS
GOPROXY_CONFIG = {
    "username": os.environ.get('GOPROXY_USERNAME', 'ceu5962432'),  # Replace with your GoProxy username
    "password": os.environ.get('GOPROXY_PASSWORD', 'yfw6k4l2'),  # Replace with your GoProxy password
    "endpoint": "premium-residential.goproxy.io",  # GoProxy endpoint
    "port": 8001,  # Port for residential proxies
    "session_id": None,  # Will be generated for sticky sessions
    "country": "US",  # Default country (US, CA, UK, DE, NL, AU)
    "proxy_type": "residential"  # residential, datacenter, mobile
}

# Proxy rotation settings
PROXY_ROTATION = {
    "enabled": True,
    "rotation_interval": 300,  # 5 minutes
    "last_rotation": 0,
    "current_session": None,
    "max_retries": 3,
    "retry_delay": 2
}

# Dictionary to store download progress info
downloads_in_progress = {}
completed_downloads = {}

# Available proxy configurations
PROXY_CONFIGS = {
    "residential": {
        "endpoint": "premium-residential.goproxy.io",
        "port": 8001,
        "description": "Rotating Residential Proxies - Best for YouTube"
    },
    "static_residential": {
        "endpoint": "premium-residential.goproxy.io",
        "port": 8001,
        "description": "Static Residential Proxies - Fixed IP"
    },
    "datacenter": {
        "endpoint": "premium-datacenter.goproxy.io",
        "port": 8000,
        "description": "Datacenter Proxies - Cheaper but less reliable"
    },
    "mobile": {
        "endpoint": "premium-mobile.goproxy.io",
        "port": 8002,
        "description": "Mobile Proxies - Mobile network IPs"
    }
}

# Supported countries
SUPPORTED_COUNTRIES = [
    "US", "CA", "UK", "DE", "NL", "AU", "FR", "IT", "ES", "SE", "NO", "DK", 
    "FI", "BE", "CH", "AT", "IE", "PT", "LU", "IS", "GR", "CY", "MT", "LV", 
    "LT", "EE", "SI", "SK", "CZ", "HU", "PL", "RO", "BG", "HR", "RS", "JP", 
    "KR", "SG", "MY", "TH", "VN", "PH", "ID", "IN", "BR", "AR", "CL", "MX"
]

def generate_session_id():
    """Generate a random session ID for sticky sessions"""
    return str(uuid.uuid4())[:8]

def get_proxy_config(country="US", proxy_type="residential"):
    """Get proxy configuration based on country and type"""
    if proxy_type not in PROXY_CONFIGS:
        proxy_type = "residential"
    
    config = PROXY_CONFIGS[proxy_type]
    
    # Generate session ID for sticky sessions
    session_id = generate_session_id()
    
    # Format for GoProxy: username-session-sessionid-country-countrycode:password@endpoint:port
    if proxy_type == "static_residential":
        # Static residential uses different format
        proxy_auth = f"{GOPROXY_CONFIG['username']}-session-{session_id}:{GOPROXY_CONFIG['password']}"
    else:
        # Rotating residential and others
        proxy_auth = f"{GOPROXY_CONFIG['username']}-session-{session_id}-country-{country}:{GOPROXY_CONFIG['password']}"
    
    proxy_url = f"http://{proxy_auth}@{config['endpoint']}:{config['port']}"
    
    return {
        "proxy_url": proxy_url,
        "session_id": session_id,
        "country": country,
        "proxy_type": proxy_type,
        "endpoint": config['endpoint'],
        "port": config['port']
    }

def test_proxy_connection(proxy_config):
    """Test proxy connection"""
    try:
        proxies = {
            "http": proxy_config["proxy_url"],
            "https": proxy_config["proxy_url"]
        }
        
        # Test with httpbin
        response = requests.get("http://httpbin.org/ip", proxies=proxies, timeout=15)
        if response.status_code == 200:
            ip_info = response.json()
            print(f"Proxy working - IP: {ip_info.get('origin')} - Country: {proxy_config['country']} - Type: {proxy_config['proxy_type']}")
            return True
        else:
            print(f"Proxy test failed - Status: {response.status_code}")
            return False
    except Exception as e:
        print(f"Proxy test failed: {e}")
        return False

def rotate_proxy_if_needed():
    """Rotate proxy if rotation is enabled and interval has passed"""
    if not PROXY_ROTATION["enabled"]:
        return
    
    current_time = time.time()
    if current_time - PROXY_ROTATION["last_rotation"] > PROXY_ROTATION["rotation_interval"]:
        # Generate new session for rotation
        new_config = get_proxy_config(GOPROXY_CONFIG["country"], GOPROXY_CONFIG["proxy_type"])
        if test_proxy_connection(new_config):
            PROXY_ROTATION["current_session"] = new_config
            PROXY_ROTATION["last_rotation"] = current_time
            print(f"Proxy rotated to new session: {new_config['session_id']}")
            return True
        else:
            print("Proxy rotation failed - keeping current session")
            return False
    return True

def get_base_ydl_opts(country="US", proxy_type="residential"):
    """Return base yt-dlp options with proxy configuration"""
    
    # Rotate proxy if needed
    rotate_proxy_if_needed()
    
    # Get current proxy config
    if PROXY_ROTATION["current_session"]:
        proxy_config = PROXY_ROTATION["current_session"]
    else:
        proxy_config = get_proxy_config(country, proxy_type)
        if test_proxy_connection(proxy_config):
            PROXY_ROTATION["current_session"] = proxy_config
            PROXY_ROTATION["last_rotation"] = time.time()
        else:
            print("Warning: Proxy connection failed, proceeding without proxy")
            proxy_config = None
    
    ydl_opts = {
        'quiet': False,
        'no_warnings': False,
        'ignoreerrors': False,
        'writesubtitles': False,
        'writeautomaticsub': False,
        'extract_flat': False,
        # Enhanced headers to avoid detection
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        },
        # Extractor arguments for better compatibility
        'extractor_args': {
            'youtube': {
                'skip': ['dash', 'hls'],
                'player_skip': ['js'],
                'player_client': ['android', 'web'],
            }
        },
        # Network options
        'prefer_ipv6': False,
        'socket_timeout': 30,
        'retries': 5,
        'retry_sleep': 2,
        'restrictfilenames': True,
        'trim_filename': 200,
        # Avoid rate limiting
        'sleep_interval': 1,
        'max_sleep_interval': 5,
        'sleep_interval_requests': 1,
        # Additional options
        'nocheckcertificate': True,
        'ignoreerrors': True,
        'no_color': True,
    }
    
    # Add proxy if available
    if proxy_config:
        ydl_opts['proxy'] = proxy_config["proxy_url"]
        print(f"Using proxy: {proxy_config['proxy_type']} - {proxy_config['country']} - Session: {proxy_config['session_id']}")
    else:
        print("No proxy configured - using direct connection")
    
    return ydl_opts

def get_verification_status(channel_data):
    """Check if channel is verified based on badges in channel data"""
    if not channel_data:
        return False
    
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

def cleanup_old_files():
    """Clean up old files and completed downloads"""
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
                                print(f"Deleted old file: {filename}")
                            except Exception as e:
                                print(f"Error deleting {file_path}: {e}")

            # Clean up completed downloads dictionary
            to_remove = []
            for download_id, info in completed_downloads.items():
                if now - info.get("completion_time", 0) > 1800:  # 30 minutes
                    to_remove.append(download_id)

            for download_id in to_remove:
                completed_downloads.pop(download_id, None)
                print(f"Cleaned up completed download: {download_id}")

        except Exception as e:
            print(f"Cleanup error: {e}")

        time.sleep(900)  # Check every 15 minutes

# API Routes

@app.route('/api/set-proxy', methods=['POST'])
def set_proxy_config():
    """Set proxy configuration"""
    data = request.get_json()
    
    if not data:
        return jsonify({"error": "No data provided"}), 400
    
    country = data.get('country', 'US').upper()
    proxy_type = data.get('proxy_type', 'residential').lower()
    
    if country not in SUPPORTED_COUNTRIES:
        return jsonify({"error": f"Invalid country code. Supported: {', '.join(SUPPORTED_COUNTRIES[:10])}..."}), 400
    
    if proxy_type not in PROXY_CONFIGS:
        return jsonify({"error": f"Invalid proxy type. Supported: {', '.join(PROXY_CONFIGS.keys())}"}), 400
    
    # Update global config
    GOPROXY_CONFIG["country"] = country
    GOPROXY_CONFIG["proxy_type"] = proxy_type
    
    # Test new proxy configuration
    test_config = get_proxy_config(country, proxy_type)
    if test_proxy_connection(test_config):
        PROXY_ROTATION["current_session"] = test_config
        PROXY_ROTATION["last_rotation"] = time.time()
        
        return jsonify({
            "success": True,
            "message": f"Proxy configuration updated successfully",
            "config": {
                "country": country,
                "proxy_type": proxy_type,
                "session_id": test_config["session_id"],
                "endpoint": test_config["endpoint"]
            }
        })
    else:
        return jsonify({"error": "Failed to connect with new proxy configuration"}), 500

@app.route('/api/proxy-status', methods=['GET'])
def get_proxy_status():
    """Get current proxy status"""
    current_session = PROXY_ROTATION.get("current_session")
    
    if current_session:
        return jsonify({
            "status": "active",
            "country": current_session["country"],
            "proxy_type": current_session["proxy_type"],
            "session_id": current_session["session_id"],
            "endpoint": current_session["endpoint"],
            "rotation_enabled": PROXY_ROTATION["enabled"],
            "last_rotation": PROXY_ROTATION["last_rotation"],
            "next_rotation": PROXY_ROTATION["last_rotation"] + PROXY_ROTATION["rotation_interval"]
        })
    else:
        return jsonify({
            "status": "inactive",
            "message": "No active proxy session"
        })

@app.route('/api/test-proxy', methods=['POST'])
def test_proxy():
    """Test proxy connection"""
    data = request.get_json() or {}
    country = data.get('country', GOPROXY_CONFIG["country"])
    proxy_type = data.get('proxy_type', GOPROXY_CONFIG["proxy_type"])
    
    test_config = get_proxy_config(country, proxy_type)
    
    if test_proxy_connection(test_config):
        return jsonify({
            "success": True,
            "message": "Proxy connection successful",
            "config": {
                "country": country,
                "proxy_type": proxy_type,
                "session_id": test_config["session_id"],
                "endpoint": test_config["endpoint"]
            }
        })
    else:
        return jsonify({
            "success": False,
            "message": "Proxy connection failed"
        })

@app.route('/api/available-proxies', methods=['GET'])
def get_available_proxies():
    """Get available proxy types and countries"""
    return jsonify({
        "proxy_types": PROXY_CONFIGS,
        "countries": SUPPORTED_COUNTRIES,
        "recommended": {
            "proxy_type": "residential",
            "countries": ["US", "CA", "UK", "DE", "NL", "AU"]
        }
    })

@app.route('/api/video-info', methods=['GET'])
def get_video_info():
    """Get video information including available formats"""
    url = request.args.get('url')
    country = request.args.get('country', GOPROXY_CONFIG["country"])
    proxy_type = request.args.get('proxy_type', GOPROXY_CONFIG["proxy_type"])
    
    if not url:
        return jsonify({"error": "Missing video URL"}), 400

    try:
        ydl_opts = get_base_ydl_opts(country, proxy_type)
        ydl_opts.update({
            'extract_flat': False,
            'dump_single_json': False,
            'simulate': True,
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                print(f"First extraction failed: {e}")
                # Try with fallback options
                ydl_opts.update({
                    'youtube_include_dash_manifest': False,
                    'format': 'best',
                    'retries': 3,
                    'extractor_args': {
                        'youtube': {
                            'skip': ['dash', 'hls'],
                            'player_skip': ['js'],
                            'player_client': ['android'],
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
                "video_formats": [],
                "proxy_info": {
                    "country": country,
                    "proxy_type": proxy_type,
                    "session_id": PROXY_ROTATION["current_session"]["session_id"] if PROXY_ROTATION["current_session"] else None
                }
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
    country = request.args.get('country', GOPROXY_CONFIG["country"])
    proxy_type = request.args.get('proxy_type', GOPROXY_CONFIG["proxy_type"])

    if not url:
        return jsonify({"error": "Missing video URL"}), 400

    download_id = str(uuid.uuid4())

    # Start download in background
    thread = threading.Thread(
        target=process_download,
        args=(download_id, url, format_id, audio_id, country, proxy_type)
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        "download_id": download_id,
        "status": "processing",
        "message": "Download started. Video will be combined with audio automatically.",
        "note": "Files are temporarily stored and will be deleted after 30 minutes.",
        "proxy_info": {
            "country": country,
            "proxy_type": proxy_type
        }
    })

def process_download(download_id, url, format_id=None, audio_id=None, country="US", proxy_type="residential"):
    """Process video download with audio combination"""
    downloads_in_progress[download_id] = {
        "status": "downloading",
        "progress": 0,
        "url": url,
        "start_time": time.time(),
        "proxy_info": {
            "country": country,
            "proxy_type": proxy_type
        }
    }

    try:
        output_filename = f"{download_id}.%(ext)s"
        output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)

        # Configure yt-dlp options
        ydl_opts = get_base_ydl_opts(country, proxy_type)
        ydl_opts.update({
            'outtmpl': output_path,
            'progress_hooks': [lambda d: update_progress(download_id, d)],
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'prefer_ffmpeg': True,
            'ffmpeg_location': '/usr/bin/ffmpeg',
        })

        # Determine format selection with better audio handling
        if format_id and audio_id:
            # Specific video + specific audio
            ydl_opts['format'] = f"{format_id}+{audio_id}/best[height<=1080]"
        elif format_id:
            # Specific video + best audio
            ydl_opts['format'] = f"{format_id}+bestaudio[ext=m4a]/best[height<=1080]"
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
                "completion_time": time.time(),
                "proxy_info": {
                    "country": country,
                    "proxy_type": proxy_type
                }
            }
        else:
            raise Exception("Download completed but file not found")

    except Exception as e:
        print(f"Download error: {e}")
        completed_downloads[download_id] = {
            "status": "failed",
            "url": url,
            "error": str(e),
            "completion_time": time.time(),
            "proxy_info": {
                "country": country,
                "proxy_type": proxy_type
            }
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
            "url": downloads_in_progress[download_id]["url"],
            "proxy_info": downloads_in_progress[download_id].get("proxy_info", {})
        })

    if download_id in completed_downloads:
        return jsonify({
            "download_id": download_id,
            "status": completed_downloads[download_id]["status"],
            "url": completed_downloads[download_id]["url"],
            "download_url": completed_downloads[download_id].get("download_url"),
            "error": completed_downloads[download_id].get("error"),
            "proxy_info": completed_downloads[download_id].get("proxy_info", {})
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
    country = request.args.get('country', GOPROXY_CONFIG["country"])
    proxy_type = request.args.get('proxy_type', GOPROXY_CONFIG["proxy_type"])
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        # Create filename based on parameters
        if audio_id:
            filename = f"{video_id}_{format_id}_{audio_id}.%(ext)s"
        else:
            filename = f"{video_id}_{format_id}.%(ext)s"

        output_path = os.path.join(DOWNLOAD_FOLDER, filename)

        # Set up download options with better audio handling
                ydl_opts = get_base_ydl_opts(country, proxy_type)
        ydl_opts.update({
            'outtmpl': output_path,
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'prefer_ffmpeg': True,
            'ffmpeg_location': '/usr/bin/ffmpeg',
        })

        # Determine format selection
        if audio_id:
            # Video + specific audio
            ydl_opts['format'] = f"{format_id}+{audio_id}/best[height<=1080]"
            ydl_opts['merge_output_format'] = 'mp4'
        elif format_id.startswith('140') or 'audio' in format_id:
            # Audio only format
            ydl_opts['format'] = format_id
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            # Video format - try to combine with best audio
            ydl_opts['format'] = f"{format_id}+bestaudio[ext=m4a]/best[height<=1080]"
            ydl_opts['merge_output_format'] = 'mp4'

        # Download the file
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Find the downloaded file
        downloaded_file = None
        for file in os.listdir(DOWNLOAD_FOLDER):
            if file.startswith(f"{video_id}_{format_id}"):
                downloaded_file = os.path.join(DOWNLOAD_FOLDER, file)
                break

        if downloaded_file and os.path.exists(downloaded_file):
            # Use custom filename if provided
            if custom_filename:
                file_ext = os.path.splitext(downloaded_file)[1]
                safe_filename = secure_filename(custom_filename) + file_ext
                return send_file(downloaded_file, as_attachment=True, download_name=safe_filename)
            else:
                return send_file(downloaded_file, as_attachment=True)
        else:
            return jsonify({"error": "Download failed - file not found"}), 500

    except Exception as e:
        print(f"Direct download error: {e}")
        return jsonify({"error": f"Download failed: {str(e)}"}), 500

@app.route('/api/playlist-info', methods=['GET'])
def get_playlist_info():
    """Get playlist information"""
    url = request.args.get('url')
    country = request.args.get('country', GOPROXY_CONFIG["country"])
    proxy_type = request.args.get('proxy_type', GOPROXY_CONFIG["proxy_type"])
    
    if not url:
        return jsonify({"error": "Missing playlist URL"}), 400

    try:
        ydl_opts = get_base_ydl_opts(country, proxy_type)
        ydl_opts.update({
            'extract_flat': True,
            'dump_single_json': False,
            'simulate': True,
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if 'entries' not in info:
                return jsonify({"error": "No playlist entries found"}), 400

            # Extract playlist information
            playlist_info = {
                "id": info.get('id'),
                "title": info.get('title'),
                "description": info.get('description'),
                "uploader": info.get('uploader'),
                "uploader_id": info.get('uploader_id'),
                "uploader_url": info.get('uploader_url'),
                "playlist_count": info.get('playlist_count'),
                "view_count": info.get('view_count'),
                "entries": []
            }

            # Extract video entries
            for entry in info['entries']:
                if entry:
                    playlist_info['entries'].append({
                        "id": entry.get('id'),
                        "title": entry.get('title'),
                        "duration": entry.get('duration'),
                        "url": entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}",
                        "thumbnail": entry.get('thumbnail'),
                        "uploader": entry.get('uploader'),
                        "view_count": entry.get('view_count')
                    })

            return jsonify(playlist_info)

    except Exception as e:
        print(f"Error in playlist-info: {e}")
        return jsonify({"error": f"Failed to extract playlist info: {str(e)}"}), 500

@app.route('/api/search', methods=['GET'])
def search_videos():
    """Search for videos"""
    query = request.args.get('query')
    max_results = int(request.args.get('max_results', 10))
    country = request.args.get('country', GOPROXY_CONFIG["country"])
    proxy_type = request.args.get('proxy_type', GOPROXY_CONFIG["proxy_type"])
    
    if not query:
        return jsonify({"error": "Missing search query"}), 400

    try:
        search_url = f"ytsearch{max_results}:{query}"
        
        ydl_opts = get_base_ydl_opts(country, proxy_type)
        ydl_opts.update({
            'extract_flat': True,
            'dump_single_json': False,
            'simulate': True,
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)

            if 'entries' not in info:
                return jsonify({"error": "No search results found"}), 400

            search_results = []
            for entry in info['entries']:
                if entry:
                    search_results.append({
                        "id": entry.get('id'),
                        "title": entry.get('title'),
                        "duration": entry.get('duration'),
                        "url": entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}",
                        "thumbnail": entry.get('thumbnail'),
                        "uploader": entry.get('uploader'),
                        "view_count": entry.get('view_count'),
                        "upload_date": entry.get('upload_date')
                    })

            return jsonify({
                "query": query,
                "max_results": max_results,
                "results_count": len(search_results),
                "results": search_results
            })

    except Exception as e:
        print(f"Error in search: {e}")
        return jsonify({"error": f"Search failed: {str(e)}"}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": time.time(),
        "proxy_status": {
            "enabled": PROXY_ROTATION["enabled"],
            "current_session": PROXY_ROTATION["current_session"]["session_id"] if PROXY_ROTATION["current_session"] else None,
            "country": GOPROXY_CONFIG["country"],
            "proxy_type": GOPROXY_CONFIG["proxy_type"]
        },
        "downloads": {
            "in_progress": len(downloads_in_progress),
            "completed": len(completed_downloads)
        }
    })

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get server statistics"""
    return jsonify({
        "downloads_in_progress": len(downloads_in_progress),
        "completed_downloads": len(completed_downloads),
        "proxy_rotation_enabled": PROXY_ROTATION["enabled"],
        "current_proxy_session": PROXY_ROTATION["current_session"]["session_id"] if PROXY_ROTATION["current_session"] else None,
        "proxy_config": {
            "country": GOPROXY_CONFIG["country"],
            "proxy_type": GOPROXY_CONFIG["proxy_type"]
        },
        "supported_formats": ["mp4", "webm", "mp3", "m4a"],
        "max_download_quality": "4K (2160p)",
        "server_uptime": time.time() - (PROXY_ROTATION.get("server_start_time", time.time()))
    })

@app.route('/api/clear-downloads', methods=['POST'])
def clear_downloads():
    """Clear completed downloads and files"""
    try:
        # Clear completed downloads
        cleared_count = len(completed_downloads)
        completed_downloads.clear()
        
        # Remove files from download folder
        files_removed = 0
        for folder in [DOWNLOAD_FOLDER, TEMP_FOLDER]:
            if os.path.exists(folder):
                for filename in os.listdir(folder):
                    file_path = os.path.join(folder, filename)
                    if os.path.isfile(file_path):
                        try:
                            os.remove(file_path)
                            files_removed += 1
                        except Exception as e:
                            print(f"Error removing {file_path}: {e}")

        return jsonify({
            "success": True,
            "message": f"Cleared {cleared_count} downloads and {files_removed} files"
        })

    except Exception as e:
        return jsonify({"error": f"Failed to clear downloads: {str(e)}"}), 500

@app.route('/api/cancel-download/<download_id>', methods=['POST'])
def cancel_download(download_id):
    """Cancel a download in progress"""
    if download_id in downloads_in_progress:
        downloads_in_progress[download_id]["status"] = "cancelled"
        return jsonify({
            "success": True,
            "message": f"Download {download_id} cancelled"
        })
    else:
        return jsonify({"error": "Download not found or already completed"}), 404

@app.route('/', methods=['GET'])
def index():
    """Simple index page"""
    return """
    <html>
    <head>
        <title>YouTube Downloader API</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            .endpoint { margin: 20px 0; padding: 15px; background: #f5f5f5; border-radius: 5px; }
            .method { color: #007bff; font-weight: bold; }
            .path { color: #28a745; font-weight: bold; }
            code { background: #e9ecef; padding: 2px 4px; border-radius: 3px; }
        </style>
    </head>
    <body>
        <h1>YouTube Downloader API</h1>
        <p>A powerful YouTube downloader API with proxy support and format selection.</p>
        
        <div class="endpoint">
            <span class="method">GET</span> <span class="path">/api/video-info</span>
            <p>Get video information and available formats</p>
            <p>Parameters: <code>url</code>, <code>country</code> (optional), <code>proxy_type</code> (optional)</p>
        </div>
        
        <div class="endpoint">
            <span class="method">GET</span> <span class="path">/api/download</span>
            <p>Download video with optional format selection</p>
            <p>Parameters: <code>url</code>, <code>format_id</code> (optional), <code>audio_id</code> (optional)</p>
        </div>
        
        <div class="endpoint">
            <span class="method">GET</span> <span class="path">/api/direct-download/{video_id}/{format_id}</span>
            <p>Direct download with automatic audio combination</p>
            <p>Parameters: <code>audio_id</code> (optional), <code>filename</code> (optional)</p>
        </div>
        
        <div class="endpoint">
            <span class="method">GET</span> <span class="path">/api/playlist-info</span>
            <p>Get playlist information</p>
            <p>Parameters: <code>url</code></p>
        </div>
        
        <div class="endpoint">
            <span class="method">GET</span> <span class="path">/api/search</span>
            <p>Search for videos</p>
            <p>Parameters: <code>query</code>, <code>max_results</code> (optional, default: 10)</p>
        </div>
        
        <div class="endpoint">
            <span class="method">POST</span> <span class="path">/api/set-proxy</span>
            <p>Configure proxy settings</p>
            <p>Body: <code>{"country": "US", "proxy_type": "residential"}</code></p>
        </div>
        
        <div class="endpoint">
            <span class="method">GET</span> <span class="path">/api/proxy-status</span>
            <p>Get current proxy status</p>
        </div>
        
        <div class="endpoint">
            <span class="method">GET</span> <span class="path">/api/health</span>
            <p>Health check endpoint</p>
        </div>
        
        <div class="endpoint">
            <span class="method">GET</span> <span class="path">/api/stats</span>
            <p>Get server statistics</p>
        </div>
        
        <p><strong>Features:</strong></p>
        <ul>
            <li>Proxy rotation with GoProxy integration</li>
            <li>Multiple format support (MP4, WebM, MP3, M4A)</li>
            <li>Automatic audio-video combination</li>
            <li>Playlist support</li>
            <li>Video search functionality</li>
            <li>Real-time download progress</li>
            <li>Automatic file cleanup</li>
        </ul>
    </body>
    </html>
    """

if __name__ == '__main__':
    # Initialize proxy rotation start time
    PROXY_ROTATION["server_start_time"] = time.time()
    
    # Start cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_old_files)
    cleanup_thread.daemon = True
    cleanup_thread.start()
    
    # Test initial proxy connection
    initial_config = get_proxy_config(GOPROXY_CONFIG["country"], GOPROXY_CONFIG["proxy_type"])
    if test_proxy_connection(initial_config):
        PROXY_ROTATION["current_session"] = initial_config
        PROXY_ROTATION["last_rotation"] = time.time()
        print(f"✅ Initial proxy connection successful - {initial_config['proxy_type']} - {initial_config['country']}")
    else:
        print("⚠️  Initial proxy connection failed - running without proxy")
    
    print("🚀 YouTube Downloader API starting...")
    print("📡 Proxy rotation enabled" if PROXY_ROTATION["enabled"] else "📡 Proxy rotation disabled")
    print("🌍 Default country:", GOPROXY_CONFIG["country"])
    print("🔄 Proxy type:", GOPROXY_CONFIG["proxy_type"])
    
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
