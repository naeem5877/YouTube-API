const express = require('express');
const cors = require('cors');
const axios = require('axios');
const { v4: uuidv4 } = require('uuid');

const app = express();
const PORT = process.env.PORT || 8080;

// Middleware
app.use(express.json());
app.use(cors({
  origin: [
    'http://localhost:3000',
    'https://vibedownloader.vercel.app',
    'https://vibedownloader.me',
    'https://www.vibedownloader.me',
    'https://ytapi.vibedownloader.me'
  ]
}));

// RapidAPI configuration
const RAPIDAPI_KEY = process.env.RAPIDAPI_KEY || 'a3e713dca1mshe25f9a91533a9e7p1e6e95jsn563f64fb72bd';
const RAPIDAPI_HOST = 'youtube-video-fast-downloader-24-7.p.rapidapi.com';

// Storage for download requests
const downloadRequests = new Map();

// Helper function to extract video ID from YouTube URL
const extractVideoId = (url) => {
  const regex = /(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/)([^"&?\/\s]{11})/;
  const match = url.match(regex);
  return match ? match[1] : null;
};

// Helper function to validate YouTube URL
const validateYouTubeURL = (url) => {
  return extractVideoId(url) !== null;
};

// Helper function to generate YouTube thumbnail URLs
const generateYouTubeThumbnails = (videoId) => {
  return [
    {
      url: `https://i.ytimg.com/vi/${videoId}/maxresdefault.jpg`,
      width: 1280,
      height: 720
    },
    {
      url: `https://i.ytimg.com/vi/${videoId}/hqdefault.jpg`,
      width: 480,
      height: 360
    },
    {
      url: `https://i.ytimg.com/vi/${videoId}/mqdefault.jpg`,
      width: 320,
      height: 180
    },
    {
      url: `https://i.ytimg.com/vi/${videoId}/default.jpg`,
      width: 120,
      height: 90
    }
  ];
};

// Helper function to get video info from RapidAPI
const getVideoInfoFromAPI = async (videoId) => {
  const options = {
    method: 'GET',
    url: `https://${RAPIDAPI_HOST}/get-video-info/${videoId}`,
    headers: {
      'x-rapidapi-key': RAPIDAPI_KEY,
      'x-rapidapi-host': RAPIDAPI_HOST
    }
  };

  try {
    const response = await axios.request(options);
    return response.data;
  } catch (error) {
    console.error('RapidAPI error:', error.response?.data || error.message);
    throw new Error(`Failed to fetch video info: ${error.response?.data?.message || error.message}`);
  }
};

// Helper function to request download from RapidAPI
const requestDownloadFromAPI = async (videoId, quality) => {
  const options = {
    method: 'GET',
    url: `https://${RAPIDAPI_HOST}/download_video/${videoId}`,
    params: { quality },
    headers: {
      'x-rapidapi-key': RAPIDAPI_KEY,
      'x-rapidapi-host': RAPIDAPI_HOST
    }
  };

  try {
    const response = await axios.request(options);
    return response.data;
  } catch (error) {
    console.error('Download request error:', error.response?.data || error.message);
    throw new Error(`Failed to request download: ${error.response?.data?.message || error.message}`);
  }
};

// Routes

// Health check
app.get('/api/health', async (req, res) => {
  res.json({
    status: 'ok',
    version: '3.0.0',
    platform: 'nodejs-serverless',
    rapidApiConnected: !!RAPIDAPI_KEY
  });
});

// Get video information
app.get('/api/video-info', async (req, res) => {
  const { url } = req.query;

  if (!url) {
    return res.status(400).json({ error: 'Missing video URL' });
  }

  try {
    if (!validateYouTubeURL(url)) {
      return res.status(400).json({ error: 'Invalid YouTube URL' });
    }

    const videoId = extractVideoId(url);
    const apiData = await getVideoInfoFromAPI(videoId);

    // Generate thumbnails using video ID
    const thumbnails = generateYouTubeThumbnails(videoId);

    // If API provides thumbnail data, use it, otherwise use generated ones
    let finalThumbnails = thumbnails;
    if (apiData.thumbnail && Array.isArray(apiData.thumbnail)) {
      finalThumbnails = apiData.thumbnail.map(thumb => ({
        url: thumb.url,
        width: thumb.width || null,
        height: thumb.height || null
      }));
    } else if (apiData.thumbnail && typeof apiData.thumbnail === 'object') {
      finalThumbnails = [{
        url: apiData.thumbnail.url,
        width: apiData.thumbnail.width || null,
        height: apiData.thumbnail.height || null
      }];
    }

    // Transform RapidAPI response to match original format
    const videoInfo = {
      id: videoId,
      title: apiData.title || 'Unknown Title',
      description: apiData.description || '', 
      duration: parseInt(apiData.lengthSeconds) || 0,
      view_count: parseInt(apiData.viewCount) || 0,
      upload_date: apiData.publishedTimeText || '', 
      thumbnails: finalThumbnails,
      channel: {
        id: apiData.channelId || '',
        name: apiData.author || 'Unknown Author',
        url: apiData.channelUrl || '',
        verified: apiData.channelVerified || false
      },
      canonical_url: apiData.canonicalUrl || `https://www.youtube.com/watch?v=${videoId}`,
      embed: apiData.embed || null,
      audio_formats: apiData.availableQuality
        ? apiData.availableQuality
            .filter(format => format.type === 'audio')
            .map(format => ({
              format_id: format.id.toString(),
              ext: format.mime && format.mime.includes('mp4') ? 'mp4' : 'webm',
              format_note: `${Math.round(format.bitrate / 1000)}kbps`,
              abr: Math.round(format.bitrate / 1000),
              filesize: parseInt(format.size) || null,
              download_url: `/api/direct-download/${videoId}/${format.id}`,
              quality_id: format.id
            }))
            .sort((a, b) => (b.abr || 0) - (a.abr || 0))
        : [],
      video_formats: apiData.availableQuality
        ? apiData.availableQuality
            .filter(format => format.type === 'video')
            .map(format => ({
              format_id: format.id.toString(),
              ext: format.mime && format.mime.includes('mp4') ? 'mp4' : 'webm',
              format_note: format.quality || 'unknown',
              width: null, // Not provided
              height: format.quality ? parseInt(format.quality.replace('p', '')) || null : null,
              fps: null, // Not provided
              vcodec: format.mime && format.mime.includes('av01') ? 'av01' : 
                      format.mime && format.mime.includes('avc1') ? 'avc1' :
                      format.mime && format.mime.includes('vp9') ? 'vp9' : 'unknown',
              acodec: 'none', // Video-only formats
              filesize: parseInt(format.size) || null,
              download_url: `/api/direct-download/${videoId}/${format.id}`,
              resolution: format.quality || 'unknown',
              quality_id: format.id,
              bitrate: format.bitrate || null
            }))
            .sort((a, b) => (b.height || 0) - (a.height || 0))
        : []
    };

    res.json(videoInfo);
  } catch (error) {
    console.error('Video info error:', error);
    res.status(500).json({ error: error.message });
  }
});

// Start download (now just prepares the download URL)
app.get('/api/download', async (req, res) => {
  const { url, format_id } = req.query;

  if (!url) {
    return res.status(400).json({ error: 'Missing video URL' });
  }

  if (!validateYouTubeURL(url)) {
    return res.status(400).json({ error: 'Invalid YouTube URL' });
  }

  try {
    const videoId = extractVideoId(url);
    const downloadId = uuidv4();

    // Store download request info
    downloadRequests.set(downloadId, {
      status: 'processing',
      videoId,
      formatId: format_id,
      url,
      startTime: Date.now()
    });

    // Start download process in background
    processDownloadRequest(downloadId, videoId, format_id);

    res.json({
      downloadId,
      status: 'processing',
      message: 'Download preparation started. Check status using the /api/download-status endpoint.'
    });
  } catch (error) {
    console.error('Download initiation error:', error);
    res.status(500).json({ error: error.message });
  }
});

// Process download request function
const processDownloadRequest = async (downloadId, videoId, formatId) => {
  try {
    const downloadRequest = downloadRequests.get(downloadId);
    if (!downloadRequest) return;

    downloadRequest.status = 'requesting';

    // Request download from RapidAPI
    const downloadData = await requestDownloadFromAPI(videoId, formatId);

    downloadRequest.status = 'ready';
    downloadRequest.downloadUrl = downloadData.file;
    downloadRequest.comment = downloadData.comment;
    downloadRequest.completionTime = Date.now();

  } catch (error) {
    console.error('Download processing error:', error);
    const downloadRequest = downloadRequests.get(downloadId);
    if (downloadRequest) {
      downloadRequest.status = 'failed';
      downloadRequest.error = error.message;
      downloadRequest.completionTime = Date.now();
    }
  }
};

// Check download status
app.get('/api/download-status/:downloadId', (req, res) => {
  const { downloadId } = req.params;

  if (!downloadRequests.has(downloadId)) {
    return res.status(404).json({ error: 'Download ID not found' });
  }

  const downloadInfo = downloadRequests.get(downloadId);

  const response = {
    downloadId,
    status: downloadInfo.status,
    url: downloadInfo.url
  };

  if (downloadInfo.status === 'ready') {
    response.downloadUrl = downloadInfo.downloadUrl;
    response.comment = downloadInfo.comment;
  }

  if (downloadInfo.error) {
    response.error = downloadInfo.error;
  }

  res.json(response);
});

// Get downloaded file (redirect to RapidAPI file URL)
app.get('/api/get-file/:downloadId', async (req, res) => {
  const { downloadId } = req.params;

  if (!downloadRequests.has(downloadId)) {
    return res.status(404).json({ error: 'Download not found' });
  }

  const downloadInfo = downloadRequests.get(downloadId);

  if (downloadInfo.status !== 'ready') {
    return res.status(404).json({ error: 'Download not ready' });
  }

  if (!downloadInfo.downloadUrl) {
    return res.status(404).json({ error: 'Download URL not available' });
  }

  // Redirect to the RapidAPI file URL
  res.redirect(downloadInfo.downloadUrl);
});

// Direct download endpoint
app.get('/api/direct-download/:videoId/:formatId', async (req, res) => {
  const { videoId, formatId } = req.params;
  const { filename } = req.query;

  try {
    // Request download from RapidAPI
    const downloadData = await requestDownloadFromAPI(videoId, formatId);

    if (!downloadData.file) {
      return res.status(500).json({ 
        error: 'Download file not ready',
        comment: downloadData.comment 
      });
    }

    // If filename is provided, try to set content disposition
    if (filename) {
      res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
    }

    // Redirect to the download URL provided by RapidAPI
    res.redirect(downloadData.file);

  } catch (error) {
    console.error('Direct download error:', error);
    res.status(500).json({ error: error.message });
  }
});

// Cleanup old download requests (run every hour)
const cleanupOldRequests = () => {
  const now = Date.now();
  const twoHours = 2 * 60 * 60 * 1000; // 2 hours in milliseconds

  for (const [id, info] of downloadRequests.entries()) {
    if (now - (info.completionTime || info.startTime) > twoHours) {
      downloadRequests.delete(id);
    }
  }
};

setInterval(cleanupOldRequests, 60 * 60 * 1000);

// Error handling middleware
app.use((error, req, res, next) => {
  console.error('Unhandled error:', error);
  res.status(500).json({ error: 'Internal server error' });
});

// Only start server in development
if (process.env.NODE_ENV !== 'production') {
  app.listen(PORT, '0.0.0.0', () => {
    console.log(`YouTube Downloader API running on port ${PORT}`);
    console.log(`Health check: http://localhost:${PORT}/api/health`);
    console.log(`RapidAPI Key configured: ${!!RAPIDAPI_KEY}`);
  });
}

module.exports = app;
