#!/usr/bin/env python3
"""
YouTube Video Metadata Processor

This script processes YouTube video IDs from a text file, fetches metadata from
YouTube API and transcripts, uses AI to extract publication dates, and outputs
a structured CSV file ready for database import.

Requirements:
    - UV package manager (https://github.com/astral-sh/uv)
    - Environment variables: YOUTUBE_API_KEY, NVIDIA_API_KEY
    - Input file: video_ids.txt (format: VIDEO_ID,PLAYLIST_NAME per line)

Usage:
    uv venv
    uv pip install google-api-python-client youtube-transcript-api python-dotenv requests
    uv run process_youtube_videos.py
"""

import csv
import logging
import subprocess
import shutil
import time
import os
import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from datetime import timedelta
from dataclasses import dataclass

import requests
import boto3
from PIL import Image
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class UploadTracker:
    """Tracks successful and failed thumbnail uploads to avoid re-uploading."""

    def __init__(self, log_file: str = "logs/thumbnail_uploads.log"):
        """
        Initialize the upload tracker.

        Args:
            log_file: Path to the log file for tracking uploads
        """
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        self.log_file = log_file
        self.uploaded_files: Set[str] = self.load_tracker()
        self.failed_uploads: Dict[str, int] = {}

    def load_tracker(self) -> Set[str]:
        """Load previously uploaded video IDs from log file."""
        if os.path.exists(self.log_file):
            with open(self.log_file, 'r') as f:
                uploaded = set()
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        if entry.get("status") == "success":
                            uploaded.add(entry.get("video_id"))
                    except json.JSONDecodeError:
                        continue
                return uploaded
        return set()

    def log_upload(self, video_id: str, status: str, error: str = None):
        """
        Log an upload attempt.

        Args:
            video_id: YouTube video ID
            status: Upload status ("success" or "failed")
            error: Error message if failed
        """
        with open(self.log_file, 'a') as f:
            log_entry = {
                "video_id": video_id,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": status
            }
            if error:
                log_entry["error"] = error
            f.write(json.dumps(log_entry) + "\n")

        # Update in-memory tracker
        if status == "success":
            self.uploaded_files.add(video_id)

    def is_uploaded(self, video_id: str) -> bool:
        """Check if a video's thumbnail has already been uploaded."""
        return video_id in self.uploaded_files


class YouTubeVideoProcessor:
    """Handles fetching and processing YouTube video metadata."""

    def __init__(
        self,
        youtube_api_key: str,
        nvidia_api_key: str,
        cloudflare_video_base_url: str,
        cloudflare_thumbnail_base_url: str,
        thumbnails_dir: str = "thumbnails",
        workspace_dir: str = "workspace",
        r2_config: Optional[Dict] = None,
        rclone_config_path: Optional[str] = None
    ):
        """
        Initialize the processor with API keys and configuration.

        Args:
            youtube_api_key: YouTube Data API v3 key
            nvidia_api_key: NVIDIA API key for date extraction
            cloudflare_video_base_url: Base URL for video files
            cloudflare_thumbnail_base_url: Base URL for thumbnails
            thumbnails_dir: Directory to save downloaded thumbnails
            workspace_dir: Directory for temporary video files
            r2_config: Cloudflare R2 configuration dict
            rclone_config_path: Path to the rclone.conf file
        """
        self.youtube_api_key = youtube_api_key
        self.nvidia_api_key = nvidia_api_key
        self.cloudflare_video_base_url = cloudflare_video_base_url
        self.cloudflare_thumbnail_base_url = cloudflare_thumbnail_base_url
        self.thumbnails_dir = thumbnails_dir
        self.workspace_dir = workspace_dir
        self.rclone_config_path = rclone_config_path
        self.youtube_service = build('youtube', 'v3', developerKey=youtube_api_key)

        # Create directories if they don't exist
        Path(self.thumbnails_dir).mkdir(parents=True, exist_ok=True)
        Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)
        logger.info(f"Thumbnails will be saved to: {Path(self.thumbnails_dir).absolute()}")
        logger.info(f"Workspace directory set to: {Path(self.workspace_dir).absolute()}")

        # Initialize R2 client if config provided
        self.r2_client = None
        self.upload_tracker = None
        if r2_config:
            self.r2_client = boto3.client(
                's3',
                endpoint_url=f"https://{r2_config['account_id']}.r2.cloudflarestorage.com",
                aws_access_key_id=r2_config['access_key'],
                aws_secret_access_key=r2_config['secret_key'],
                region_name='auto'
            )
            self.bucket_name = r2_config['bucket_name']
            self.upload_tracker = UploadTracker()
            logger.info("Cloudflare R2 client initialized")

    def read_video_ids(self, input_file: str) -> List[Tuple[str, str]]:
        """
        Read video IDs and playlist names from input file.

        Args:
            input_file: Path to the input text file

        Returns:
            List of tuples (video_id, playlist_name)
        """
        video_data = []
        seen_ids = set()

        try:
            with open(input_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue

                    parts = line.split(',', 1)
                    video_id = parts[0].strip()
                    playlist_name = parts[1].strip() if len(parts) > 1 else "Unknown"

                    # Validate video ID format (11 characters for YouTube)
                    if len(video_id) != 11:
                        logger.warning(f"Line {line_num}: Invalid video ID format '{video_id}', skipping")
                        continue

                    # Skip duplicates
                    if video_id in seen_ids:
                        logger.warning(f"Line {line_num}: Duplicate video ID '{video_id}', skipping")
                        continue

                    seen_ids.add(video_id)
                    video_data.append((video_id, playlist_name))

            logger.info(f"Loaded {len(video_data)} unique video IDs from {input_file}")
            return video_data

        except FileNotFoundError:
            logger.error(f"Input file not found: {input_file}")
            raise
        except Exception as e:
            logger.error(f"Error reading input file: {e}")
            raise

    def _list_remote_files(self, video_id: str, rclone_remote: str) -> List[Tuple[str, int]]:
        """
        List all files in the remote folder for a specific video ID.

        Args:
            video_id: The YouTube video ID to search for
            rclone_remote: The rclone remote name (e.g., 'gdrive')

        Returns:
            List of tuples (file_path, file_size) for files found
        """
        command = [
            'rclone', 'ls',
            '--config', self.rclone_config_path,
            f"{rclone_remote}:{video_id}",
            '--progress'
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode != 0:
                logger.warning(f"Rclone ls failed for {video_id}. Error: {result.stderr.strip()}")
                return []

            # Parse the output
            # Format: "    SIZE_IN_BYTES path/to/file.mp4"
            files = []
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if not line or 'Transferred:' in line or 'Elapsed time:' in line:
                    continue

                parts = line.split(None, 1)  # Split on whitespace, max 2 parts
                if len(parts) == 2:
                    try:
                        size = int(parts[0])
                        file_path = parts[1]
                        if file_path.endswith('.mp4'):
                            files.append((file_path, size))
                    except ValueError:
                        continue

            logger.info(f"Found {len(files)} video files for {video_id}")
            return files

        except FileNotFoundError:
            logger.error("`rclone` command not found. Please ensure it is installed and in your PATH.")
            return []
        except Exception as e:
            logger.error(f"Error listing remote files: {e}")
            return []

    def _select_best_file(self, files: List[Tuple[str, int]], video_id: str) -> Optional[str]:
        """
        Select the best file from the list, prioritizing the Videos folder.

        Args:
            files: List of tuples (file_path, file_size)
            video_id: The exact video ID to match

        Returns:
            Selected file path or None if no match found
        """
        if not files:
            return None

        # Filter files that contain the exact video ID
        matching_files = [
            (path, size) for path, size in files
            if video_id in path
        ]

        if not matching_files:
            logger.warning(f"No files found with exact video ID: {video_id}")
            return None

        # Prioritize files in the "Videos" folder
        videos_folder_files = [
            path for path, _ in matching_files
            if path.startswith('Videos/')
        ]

        if videos_folder_files:
            selected = videos_folder_files[0]
            logger.info(f"Selected file from Videos folder: {selected}")
            return selected

        # Fallback to any other folder
        selected = matching_files[0][0]
        logger.info(f"Selected file from alternate folder: {selected}")
        return selected

    def fetch_video_from_gdrive(self, video_id: str) -> Optional[str]:
        """
        Fetch a video file from Google Drive using rclone.

        Args:
            video_id: The YouTube video ID, used as the filename.

        Returns:
            Local file path to the downloaded video or None if failed.
        """
        if not self.rclone_config_path:
            logger.error("Rclone config path not provided. Cannot fetch video.")
            return None

        # The remote name 'gdrive' is taken from your rclone.conf file.
        rclone_remote = 'gdrive'

        local_dest_path = Path(self.workspace_dir)
        local_video_path = local_dest_path / f"{video_id}.mp4"

        # Source videos are stored as flat files named by video ID inside
        # category folders on the drive. Try each known location in priority
        # order (uploaded videos first, then live-stream recordings).
        candidate_paths = [
            f"Videos/{video_id}.mp4",
            f"Streams/{video_id}.mp4",
        ]

        for remote_rel_path in candidate_paths:
            remote_file_path = f"{rclone_remote}:{remote_rel_path}"
            logger.info(f"Trying to fetch '{remote_file_path}'...")

            command = [
                'rclone', 'copyto',
                '--config', self.rclone_config_path,
                remote_file_path,
                str(local_video_path),
                '--progress'
            ]

            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    check=False  # We check the returncode manually
                )

                if result.returncode == 0 and local_video_path.exists():
                    logger.info(f"✓ Successfully downloaded video to {local_video_path}")
                    return str(local_video_path)

                logger.warning(
                    f"Not found at '{remote_file_path}' "
                    f"(rclone exit {result.returncode}). {result.stderr.strip()[:200]}"
                )

            except FileNotFoundError:
                logger.error("`rclone` command not found. Please ensure it is installed and in your PATH.")
                return None
            except Exception as e:
                logger.error(f"An unexpected error occurred during rclone execution: {e}")
                return None

        logger.error(f"No source file found for video ID '{video_id}' in {candidate_paths}")
        return None

    def transcode_and_upload_video(self, video_id: str, source_video_path: str) -> bool:
        """
        Calls the ott-transcoder.py script to process and upload the video.

        Args:
            video_id: The YouTube video ID.
            source_video_path: The local path to the downloaded source video.

        Returns:
            True if transcoding and upload were successful, False otherwise.
        """
        logger.info(f"Starting transcoding for {video_id}...")
        transcoder_script_path = Path(__file__).parent / 'ott-transcoder.py'

        if not transcoder_script_path.exists():
            logger.error(f"Transcoder script not found at {transcoder_script_path}")
            return False

        command = [
            'python3', str(transcoder_script_path),
            '--video-path', source_video_path,
            '--video-id', video_id
        ]

        try:
            # We use Popen to stream output in real-time
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in iter(process.stdout.readline, ''):
                logger.info(f"[Transcoder]: {line.strip()}")
            process.wait()
            return process.returncode == 0
        except Exception as e:
            logger.error(f"An error occurred while running the transcoder: {e}")
            return False

    def is_video_complete_in_r2(self, video_id: str) -> bool:
        if not self.r2_client:
            return False
        keys=[f"{video_id}/master.m3u8"]+[f"{video_id}/{r}/playlist.m3u8" for r in ("240p","360p","480p","720p","1080p")]
        try:
            for k in keys: self.r2_client.head_object(Bucket=self.bucket_name, Key=k)
            return True
        except Exception:
            return False

    def fetch_youtube_metadata_batch(
        self,
        video_ids: List[str],
        max_retries: int = 3
    ) -> Dict[str, Dict]:
        """
        Fetch YouTube metadata for multiple videos in a single API call.

        Args:
            video_ids: List of video IDs (max 50)
            max_retries: Maximum number of retry attempts

        Returns:
            Dictionary mapping video_id to metadata dict
        """
        if not video_ids:
            return {}

        # YouTube API supports up to 50 IDs per request
        video_ids = video_ids[:50]

        for attempt in range(max_retries):
            try:
                request = self.youtube_service.videos().list(
                    part='snippet,statistics,contentDetails',
                    id=','.join(video_ids)
                )
                response = request.execute()

                results = {}
                for item in response.get('items', []):
                    video_id = item['id']
                    snippet = item.get('snippet', {})
                    statistics = item.get('statistics', {})
                    content_details = item.get('contentDetails', {})

                    results[video_id] = {
                        'title': snippet.get('title', 'Not Available'),
                        'description': snippet.get('description', 'Not Available'),
                        'views': int(statistics.get('viewCount', 0)),
                        'likes': int(statistics.get('likeCount', 0)),
                        'duration': content_details.get('duration', 'PT0S')
                    }

                return results

            except HttpError as e:
                if e.resp.status == 403:
                    logger.error("YouTube API quota exceeded or forbidden")
                    break
                elif e.resp.status in [429, 500, 503]:
                    # Rate limit or server error - retry with backoff
                    wait_time = (2 ** attempt) * 1
                    logger.warning(f"API error {e.resp.status}, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"YouTube API error: {e}")
                    break
            except Exception as e:
                logger.error(f"Unexpected error fetching YouTube metadata: {e}")
                break

        # Return empty results for failed videos
        return {vid: self._get_default_metadata() for vid in video_ids}

    def _get_default_metadata(self) -> Dict:
        """Return default metadata for failed API calls."""
        return {
            'title': 'Not Available',
            'description': 'Not Available',
            'views': 0,
            'likes': 0,
            'duration': 'PT0S'
        }

    def fetch_transcript(self, video_id: str) -> str:
        """
        Fetch transcript for a video using youtube-transcript-api.

        Args:
            video_id: YouTube video ID

        Returns:
            Full transcript text or error message
        """
        try:
            # Initialize the API
            ytt_api = YouTubeTranscriptApi()

            # Fetch transcript - try English and auto-generated
            transcript_data = ytt_api.fetch(video_id, languages=['en', 'en-US', 'en-GB'])

            # Extract text from FetchedTranscriptSnippet objects using .text attribute
            transcript_text = ' '.join([snippet.text for snippet in transcript_data])
            return transcript_text
        except (TranscriptsDisabled, NoTranscriptFound):
            logger.debug(f"Transcript not available for video {video_id}")
            return "Transcript Not Available"
        except VideoUnavailable:
            logger.warning(f"Video {video_id} is unavailable")
            return "Transcript Not Available"
        except Exception as e:
            logger.warning(f"Error fetching transcript for {video_id}: {e}")
            return "Transcript Not Available"

    def download_thumbnail(
        self,
        video_id: str,
        thumbnail_url: str,
        max_retries: int = 3
    ) -> Optional[str]:
        """
        Download thumbnail from YouTube and save to local directory.

        Args:
            video_id: YouTube video ID
            thumbnail_url: URL to the thumbnail image (from YouTube API)
            max_retries: Maximum number of retry attempts

        Returns:
            Local file path to saved thumbnail or None if failed
        """
        # Try different quality thumbnails in order of preference
        thumbnail_urls = [
            f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",  # 1280x720
            f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",      # 480x360
            f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",      # 320x180
            f"https://i.ytimg.com/vi/{video_id}/default.jpg"         # 120x90
        ]

        local_path = Path(self.thumbnails_dir) / f"{video_id}.jpg"

        # Try each quality level
        for url in thumbnail_urls:
            for attempt in range(max_retries):
                try:
                    response = requests.get(url, timeout=10, stream=True)

                    if response.status_code == 200:
                        # Save thumbnail to file
                        with open(local_path, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                f.write(chunk)

                        logger.debug(f"Downloaded thumbnail for {video_id} from {url}")
                        return str(local_path)

                    elif response.status_code == 404:
                        # This quality not available, try next
                        break

                except requests.exceptions.Timeout:
                    logger.warning(f"Timeout downloading thumbnail for {video_id}, attempt {attempt + 1}/{max_retries}")
                    time.sleep(1)
                except Exception as e:
                    logger.warning(f"Error downloading thumbnail for {video_id}: {e}")
                    break

        logger.error(f"Failed to download thumbnail for {video_id}")
        return None

    def convert_to_webp_and_upload(
        self,
        video_id: str,
        jpg_path: str,
        max_retries: int = 3,
        retry_delay: int = 2
    ) -> bool:
        """
        Convert JPG thumbnail to WebP and upload to Cloudflare R2.

        Args:
            video_id: YouTube video ID
            jpg_path: Path to the downloaded JPG thumbnail
            max_retries: Maximum number of retry attempts
            retry_delay: Base delay between retries in seconds

        Returns:
            True if upload successful, False otherwise
        """
        if not self.r2_client:
            logger.warning("R2 client not initialized, skipping upload")
            return False

        if not self.upload_tracker:
            logger.warning("Upload tracker not initialized, skipping upload")
            return False

        # Check if already uploaded
        if self.upload_tracker.is_uploaded(video_id):
            logger.info(f"Skipping {video_id}: Already uploaded")
            return True

        if not os.path.exists(jpg_path):
            logger.error(f"Thumbnail file not found: {jpg_path}")
            self.upload_tracker.log_upload(video_id, "failed", "Thumbnail file not found")
            return False

        webp_path = None
        try:
            # Convert JPG to WebP
            webp_path = jpg_path.replace('.jpg', '.webp')
            img = Image.open(jpg_path)
            img.save(webp_path, 'WEBP', quality=85)
            logger.debug(f"Converted {jpg_path} to WebP")

            # Upload to R2 with retry logic
            key = f"{video_id}/thumbnail.webp"

            for attempt in range(max_retries):
                try:
                    with open(webp_path, 'rb') as f:
                        self.r2_client.put_object(
                            Bucket=self.bucket_name,
                            Key=key,
                            Body=f,
                            ContentType='image/webp'
                        )
                    logger.info(f"✓ Uploaded thumbnail to R2: {key}")

                    # Log successful upload
                    self.upload_tracker.log_upload(video_id, "success")

                    # Clean up WebP file after successful upload
                    if os.path.exists(webp_path):
                        os.remove(webp_path)
                    return True

                except Exception as e:
                    if attempt < max_retries - 1:
                        delay = retry_delay * (2 ** attempt)
                        logger.warning(f"Retry {attempt + 1}/{max_retries} for {video_id} in {delay}s: {str(e)}")
                        time.sleep(delay)
                    else:
                        logger.error(f"❌ Failed to upload {video_id} after {max_retries} attempts: {str(e)}")
                        # Log failed upload
                        self.upload_tracker.log_upload(video_id, "failed", str(e))
                        # Clean up WebP file
                        if os.path.exists(webp_path):
                            os.remove(webp_path)
                        return False

        except Exception as e:
            error_msg = f"Error converting thumbnail: {str(e)}"
            logger.error(f"{error_msg} for {video_id}")
            # Log failed upload
            self.upload_tracker.log_upload(video_id, "failed", error_msg)
            # Clean up WebP file if conversion succeeded but something else failed
            if webp_path and os.path.exists(webp_path):
                os.remove(webp_path)
            return False

        return False

    def extract_date_with_ai(
        self,
        title: str,
        description: str,
        transcript: str,
        max_retries: int = 3
    ) -> str:
        """
        Use NVIDIA API to extract publication date from video content.

        Args:
            title: Video title
            description: Video description
            transcript: Video transcript (truncated if too long)
            max_retries: Maximum number of retry attempts

        Returns:
            Date in YYYY-MM-DD format or "Date Not Found"
        """
        # Truncate transcript to avoid token limits
        max_transcript_chars = 2000
        truncated_transcript = transcript[:max_transcript_chars]

        # Construct prompt
        content = f"""Title: {title}

Description: {description}

Transcript (first {max_transcript_chars} chars): {truncated_transcript}

Extract the most relevant date mentioned in this text, preferring event or publication dates over others. Return ONLY the date in YYYY-MM-DD format or 'Date Not Found' if none exists. Do not include any explanation or additional text."""

        payload = {
            "model": "deepseek-ai/deepseek-v3.1",
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ],
            "temperature": 0.2,
            "top_p": 0.7,
            "max_tokens": 100,
            "seed": 42,
            "stream": False
        }

        headers = {
            "Authorization": f"Bearer {self.nvidia_api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

        for attempt in range(max_retries):
            try:
                response = requests.post(
                    "https://integrate.api.nvidia.com/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=30
                )

                if response.status_code == 200:
                    result = response.json()
                    date_text = result['choices'][0]['message']['content'].strip()

                    # Clean up response (remove quotes, extra whitespace)
                    date_text = date_text.strip('"\'').strip()

                    # Validate format (basic check)
                    if len(date_text) == 10 and date_text.count('-') == 2:
                        return date_text
                    elif 'not found' in date_text.lower():
                        return "Date Not Found"
                    else:
                        logger.debug(f"Unexpected date format from AI: {date_text}")
                        return "Date Not Found"

                elif response.status_code == 429:
                    wait_time = (2 ** attempt) * 2
                    logger.warning(f"NVIDIA API rate limit, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.warning(f"NVIDIA API error {response.status_code}: {response.text}")
                    break

            except requests.exceptions.Timeout:
                logger.warning(f"NVIDIA API timeout, attempt {attempt + 1}/{max_retries}")
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error calling NVIDIA API: {e}")
                break

        return "Date Not Found"

    def convert_duration_to_readable(self, iso_duration: str) -> str:
        """
        Convert ISO 8601 duration to human-readable format (MM:SS or HH:MM:SS).

        Args:
            iso_duration: Duration in ISO 8601 format (e.g., "PT15M30S")

        Returns:
            Human-readable duration (e.g., "15:30")
        """
        try:
            # Parse ISO 8601 duration
            import re
            pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
            match = re.match(pattern, iso_duration)

            if not match:
                return iso_duration

            hours = int(match.group(1) or 0)
            minutes = int(match.group(2) or 0)
            seconds = int(match.group(3) or 0)

            if hours > 0:
                return f"{hours}:{minutes:02d}:{seconds:02d}"
            else:
                return f"{minutes}:{seconds:02d}"

        except Exception as e:
            logger.warning(f"Error converting duration '{iso_duration}': {e}")
            return iso_duration

    def cleanup_workspace(self, source_video_path: Optional[str], local_thumbnail_path: Optional[str]):
        """
        Deletes temporary files from the workspace and thumbnails directories.

        Args:
            source_video_path: Path to the source video file to delete.
            local_thumbnail_path: Path to the local thumbnail file to delete.
        """
        logger.info(f"Cleaning up temporary files for video...")
        if source_video_path and os.path.exists(source_video_path):
            try:
                os.remove(source_video_path)
                logger.info(f"✓ Cleaned up source video: {source_video_path}")
            except OSError as e:
                logger.warning(f"Failed to delete source video {source_video_path}: {e}")

        if local_thumbnail_path and os.path.exists(local_thumbnail_path):
            try:
                os.remove(local_thumbnail_path)
                logger.info(f"✓ Cleaned up local thumbnail: {local_thumbnail_path}")
            except OSError as e:
                logger.warning(f"Failed to delete local thumbnail {local_thumbnail_path}: {e}")

    def process_videos(
        self,
        video_data: List[Tuple[str, str]],
        output_file: str,
        batch_size: int = 50
    ):
        """
        Process all videos and write to CSV incrementally.

        Args:
            video_data: List of (video_id, playlist_name) tuples
            output_file: Path to output CSV file
            batch_size: Number of videos to process per YouTube API batch
        """
        total_videos = len(video_data)

        # Define CSV fieldnames
        fieldnames = [
            'video_id',
            'master_link',
            'thumbnail_link',
            'link_1080p',
            'link_720p',
            'video_title',
            'description',
            'transcript',
            'date_published',
            'views',
            'likes',
            'duration',
            'playlist_name'
        ]

        # Open CSV file and write header
        csv_file = open(output_file, 'w', newline='', encoding='utf-8')
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        csv_file.flush()  # Ensure header is written immediately

        logger.info(f"Created CSV file: {output_file}")

        # Process in batches for YouTube API efficiency
        for batch_start in range(0, total_videos, batch_size):
            batch_end = min(batch_start + batch_size, total_videos)
            batch = video_data[batch_start:batch_end]

            # Fetch YouTube metadata for batch
            video_ids = [vid for vid, _ in batch]
            metadata_map = self.fetch_youtube_metadata_batch(video_ids)

            # Process each video individually for transcript and AI
            for idx, (video_id, playlist_name) in enumerate(batch, start=batch_start + 1):
                logger.info(f"Processing video {idx}/{total_videos}: {video_id}")

                source_video_path = None
                if self.is_video_complete_in_r2(video_id):
                    logger.info(f"{video_id} already complete in R2 - skipping transcode, metadata only")
                else:
                    stale_dir = os.path.join('processing', video_id)
                    if os.path.isdir(stale_dir):
                        shutil.rmtree(stale_dir, ignore_errors=True)
                    source_video_path = self.fetch_video_from_gdrive(video_id)
                    if not source_video_path:
                        logger.warning(f"Skipping video {video_id} due to download failure.")
                        continue
                    transcode_success = self.transcode_and_upload_video(video_id, source_video_path)
                    if not transcode_success:
                        logger.error(f"Transcoding failed for {video_id}. Skipping metadata processing.")
                        continue

                # Get metadata
                metadata = metadata_map.get(video_id, self._get_default_metadata())

                # Construct URLs with new domain structure
                base_domain = "ntv-cms.nithyananda.ai"
                master_link = f"https://{base_domain}/{video_id}/master.m3u8"
                thumbnail_link = f"https://{base_domain}/{video_id}/thumbnail.webp"
                link_1080p = f"https://{base_domain}/{video_id}/1080p/playlist.m3u8"
                link_720p = f"https://{base_domain}/{video_id}/720p/playlist.m3u8"

                # Fetch transcript
                transcript = self.fetch_transcript(video_id)

                # Download and save thumbnail to local folder
                local_thumbnail_path = self.download_thumbnail(video_id, thumbnail_link)

                # Convert to WebP and upload to R2
                if local_thumbnail_path and self.r2_client:
                    self.convert_to_webp_and_upload(video_id, local_thumbnail_path)

                # Extract date using AI
                date_published = self.extract_date_with_ai(
                    metadata['title'],
                    metadata['description'],
                    transcript
                )

                # Convert duration to readable format
                duration_readable = self.convert_duration_to_readable(metadata['duration'])

                # Aggregate data
                video_record = {
                    'video_id': video_id,
                    'master_link': master_link,
                    'thumbnail_link': thumbnail_link,  # Remote URL from ntv-cms
                    'link_1080p': link_1080p,
                    'link_720p': link_720p,
                    'video_title': metadata['title'],
                    'description': metadata['description'],
                    'transcript': transcript,
                    'date_published': date_published,
                    'views': metadata['views'],
                    'likes': metadata['likes'],
                    'duration': duration_readable,
                    'playlist_name': playlist_name
                }

                # Write row immediately to CSV
                writer.writerow(video_record)
                csv_file.flush()  # Ensure data is written to disk immediately
                logger.info(f"Written video {idx}/{total_videos} to CSV: {video_id}")

                # Final Step: Cleanup local files
                self.cleanup_workspace(source_video_path, local_thumbnail_path)

                # Small delay to avoid hammering APIs
                time.sleep(0.5)

        # Close CSV file
        csv_file.close()
        logger.info(f"Successfully completed processing {total_videos} videos")


def load_env_from_md(file_path: str):
    """
    Load environment variables from a markdown file with KEY=VALUE format.
    Ignores comments and empty lines.

    Args:
        file_path: Path to the environment file.
    """
    try:
        with open(file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    if key and value:
                        os.environ[key] = value
                        logger.debug(f"Loaded env var from {file_path}: {key}")
    except FileNotFoundError:
        logger.warning(f"Environment file not found: {file_path}. Relying on pre-set environment variables.")

def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description='Process YouTube video IDs and generate metadata CSV'
    )
    parser.add_argument(
        '--input',
        default='video_ids.txt',
        help='Input text file with video IDs (default: video_ids.txt)'
    )
    parser.add_argument(
        '--output',
        default='youtube_video_data.csv',
        help='Output CSV file (default: youtube_video_data.csv)'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=50,
        help='Batch size for YouTube API calls (default: 50, max: 50)'
    )
    parser.add_argument(
        '--thumbnails-dir',
        default='thumbnails',
        help='Directory to save downloaded thumbnails (default: thumbnails)'
    )
    parser.add_argument(
        '--workspace-dir',
        default='workspace',
        help='Directory for temporary video files (default: workspace)'
    )
    parser.add_argument(
        '--rclone-config',
        default='rclone.conf',
        help='Path to the rclone.conf file (default: rclone.conf)'
    )
    parser.add_argument(
        '--env-file',
        default='.env',
        help='Path to the environment file (default: .env)'
    )

    args = parser.parse_args()

    # Load environment variables
    load_env_from_md(args.env_file)
    
    # Validate required environment variables
    youtube_api_key = os.getenv('YOUTUBE_API_KEY')
    nvidia_api_key = os.getenv('NVIDIA_API_KEY')

    # Construct Cloudflare R2 URLs from environment
    cloudflare_domain = os.getenv('CLOUDFLARE_DOMAIN', 'https://5be98fe813fcf5f08db3eb1de5c62c51.r2.cloudflarestorage.com')
    bucket_name = os.getenv('BUCKET_NAME', 'ntv-ott')

    # URLs follow the pattern: {domain}/{bucket}/{video_id}/file
    cloudflare_video_base = f"{cloudflare_domain}/{bucket_name}"
    cloudflare_thumbnail_base = f"{cloudflare_domain}/{bucket_name}"

    if not youtube_api_key:
        logger.error("YOUTUBE_API_KEY environment variable not set")
        return 1

    if not nvidia_api_key:
        logger.error("NVIDIA_API_KEY environment variable not set")
        return 1

    # Get R2 configuration
    r2_config = {
        'account_id': os.getenv('CLOUDFLARE_ACCOUNT_ID'),
        'access_key': os.getenv('R2_ACCESS_KEY_ID'),
        'secret_key': os.getenv('R2_SECRET_ACCESS_KEY'),
        'bucket_name': os.getenv('BUCKET_NAME')
    }

    # Validate R2 config
    if not all(r2_config.values()):
        logger.warning("R2 configuration incomplete, thumbnail upload will be skipped")
        r2_config = None

    # Initialize processor
    processor = YouTubeVideoProcessor(
        youtube_api_key=youtube_api_key,
        nvidia_api_key=nvidia_api_key,
        cloudflare_video_base_url=cloudflare_video_base,
        cloudflare_thumbnail_base_url=cloudflare_thumbnail_base,
        thumbnails_dir=args.thumbnails_dir,
        workspace_dir=args.workspace_dir,
        r2_config=r2_config,
        rclone_config_path=args.rclone_config
    )

    try:
        # Read input file
        video_data = processor.read_video_ids(args.input)

        if not video_data:
            logger.warning("No valid video IDs found in input file")
            return 1
        

        # Process videos and write to CSV incrementally
        logger.info(f"Starting processing of {len(video_data)} videos...")
        processor.process_videos(video_data, args.output, batch_size=args.batch_size)

        logger.info("Processing complete!")
        return 0

    except KeyboardInterrupt:
        logger.warning("Processing interrupted by user")
        return 1
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    exit(main())
