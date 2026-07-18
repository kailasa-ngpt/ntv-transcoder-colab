#!/usr/bin/env python3

import os
import subprocess
import boto3
from botocore.config import Config
import uuid
import shutil
import time
import json
import sys
from pathlib import Path
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Set, List
import psutil
import multiprocessing
from dataclasses import dataclass
import concurrent.futures

@dataclass
class UploadJob:
    file_path: str
    key: str
    content_type: str
    resolution: str

class UploadTracker:
    def __init__(self, video_id: str):
        self.video_id = video_id
        self.tracker_dir = os.path.join('processing', video_id)
        os.makedirs(self.tracker_dir, exist_ok=True)
        self.tracker_file = os.path.join(self.tracker_dir, 'upload_tracker.json')
        self.uploaded_files: Set[str] = self.load_tracker()
        self.failed_uploads: Dict[str, int] = {}

    def load_tracker(self) -> Set[str]:
        if os.path.exists(self.tracker_file):
            with open(self.tracker_file, 'r') as f:
                data = json.load(f)
                self.failed_uploads = data.get('failed_uploads', {})
                return set(data.get('uploaded_files', []))
        return set()

    def save_tracker(self):
        with open(self.tracker_file, 'w') as f:
            json.dump({
                'uploaded_files': list(self.uploaded_files),
                'failed_uploads': self.failed_uploads
            }, f)

    def mark_uploaded(self, key: str):
        self.uploaded_files.add(key)
        if key in self.failed_uploads:
            del self.failed_uploads[key]
        self.save_tracker()

    def mark_failed(self, key: str):
        self.failed_uploads[key] = self.failed_uploads.get(key, 0) + 1
        self.save_tracker()

    def is_uploaded(self, key: str) -> bool:
        return key in self.uploaded_files

    def get_failed_uploads(self) -> List[str]:
        return [key for key, attempts in self.failed_uploads.items() 
                if attempts < 3 and key not in self.uploaded_files]

    def cleanup(self):
        if os.path.exists(self.tracker_file):
            os.remove(self.tracker_file)

class VideoProcessor:
    def __init__(self, video_path: str, video_id: str):
        self.video_path = video_path
        self.video_id = video_id
        self.output_dir = os.path.join('processing', video_id)
        os.makedirs(self.output_dir, exist_ok=True)
        self.master_playlist_content = "#EXTM3U\n#EXT-X-VERSION:3\n"
        self.config = self.load_config()
        self.hls_segment_time = int(os.getenv('HLS_SEGMENT_TIME', '6'))
        self.audio_bitrate = os.getenv('AUDIO_BITRATE', '128k')
        self.processed_resolutions = set()
        self.ffmpeg_path = 'ffmpeg'
        self.hw_accel_config = self._get_hw_accel_config()
        self.load_processing_state()

    def load_config(self):
        return {
            "240p": {"width": 426, "height": 240, "bitrate": "250K"},
            "360p": {"width": 640, "height": 360, "bitrate": "500K"},
            "480p": {"width": 854, "height": 480, "bitrate": "1M"},
            "720p": {"width": 1280, "height": 720, "bitrate": "2M"},
            "1080p": {"width": 1920, "height": 1080, "bitrate": "4M"}
        }

    def _get_hw_accel_config(self) -> Dict:
        """
        Detects available FFmpeg hardware acceleration and returns config.
        Prioritizes NVIDIA > Apple VideoToolbox > CPU.
        """
        # Check for NVIDIA NVENC - verify actual GPU access, not just nvidia-smi
        try:
            result = subprocess.run(['nvidia-smi'], capture_output=True, text=True, check=False)
            if result.returncode == 0:
                # Test if ffmpeg can actually access NVENC
                test_cmd = [
                    self.ffmpeg_path, '-hide_banner',
                    '-f', 'lavfi', '-i', 'nullsrc=s=256x256:d=1',
                    '-c:v', 'h264_nvenc', '-f', 'null', '-'
                ]
                test_result = subprocess.run(test_cmd, capture_output=True, text=True, check=False)

                if test_result.returncode == 0:
                    print("Found NVIDIA GPU with working NVENC hardware acceleration.")
                    return {"hwaccel": "cuda", "vcodec": "h264_nvenc"}
                else:
                    print("NVIDIA GPU detected but NVENC not accessible (likely Docker GPU passthrough issue).")
                    print("Falling back to CPU transcoding. To enable GPU, run container with --gpus all")
        except FileNotFoundError:
            pass  # nvidia-smi not found

        # Check for Apple VideoToolbox on macOS
        if sys.platform == "darwin":
            try:
                # Check if ffmpeg was compiled with videotoolbox support
                result = subprocess.run([self.ffmpeg_path, '-codecs'], capture_output=True, text=True, check=False)
                if 'h264_videotoolbox' in result.stdout:
                    print("Found Apple VideoToolbox hardware acceleration.")
                    return {"hwaccel": "videotoolbox", "vcodec": "h264_videotoolbox"}
            except FileNotFoundError:
                pass  # ffmpeg not found, will fail later

        print("No compatible GPU hardware acceleration found. Using CPU transcoding.")
        return {}

    def process_resolution(self, resolution: str, config: Dict):
        output_path = os.path.join(self.output_dir, resolution)
        os.makedirs(output_path, exist_ok=True)

        ffmpeg_command = [
            self.ffmpeg_path,
        ]

        # Add hardware acceleration flags if available
        if self.hw_accel_config:
            ffmpeg_command.extend(['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'])

        ffmpeg_command.extend([
            '-i', self.video_path, # Input file must come after hwaccel flags
            '-vf', f'scale_npp={config["width"]}:{config["height"]}',
            '-c:v', 'h264',
            '-b:v', config['bitrate'],
            '-c:a', 'aac',
            '-b:a', self.audio_bitrate,
            '-f', 'hls',
            '-hls_time', str(self.hls_segment_time),
            '-hls_playlist_type', 'vod',
            '-hls_segment_type', 'mpegts',
            '-threads', '0',
            '-g', '48',
            '-sc_threshold', '0',
            '-hls_segment_filename', os.path.join(output_path, f'segment_%03d.ts'),
            os.path.join(output_path, 'playlist.m3u8')
        ])

        # Add encoder-specific options based on hardware acceleration
        if self.hw_accel_config:
            # Use NVENC codec
            ffmpeg_command[ffmpeg_command.index('-c:v') + 1] = self.hw_accel_config['vcodec']

            # Add NVENC-specific preset (valid values: default, slow, medium, fast, hp, hq, bd, ll, llhq, llhp, lossless, losslesshp)
            preset_index = ffmpeg_command.index('-hls_segment_filename')
            ffmpeg_command.insert(preset_index, '-preset')
            ffmpeg_command.insert(preset_index + 1, 'fast')

            # Add NVENC-specific tune for high quality
            ffmpeg_command.insert(preset_index + 2, '-tune')
            ffmpeg_command.insert(preset_index + 3, 'hq')
        else:
            # Use software encoder presets
            preset_index = ffmpeg_command.index('-hls_segment_filename')
            ffmpeg_command.insert(preset_index, '-preset')
            ffmpeg_command.insert(preset_index + 1, 'faster')
            ffmpeg_command.insert(preset_index + 2, '-tune')
            ffmpeg_command.insert(preset_index + 3, 'fastdecode')

        try:
            process = subprocess.Popen(
                ffmpeg_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            
            print(f"Started processing {resolution}")
            stdout, stderr = process.communicate()
            
            if process.returncode != 0:
                print(f"Error processing {resolution}:\n{stderr}")
                return None

            print(f"✓ Completed {resolution}")

            bandwidth = int(config['bitrate'].replace('K', '000').replace('M', '000000'))
            self.master_playlist_content += f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={config['width']}x{config['height']}\n"
            self.master_playlist_content += f"{resolution}/playlist.m3u8\n"

            return output_path
        except Exception as e:
            print(f"Error processing {resolution}: {str(e)}")
            return None

    def process_video(self):
        processed_paths = []
        resolutions_to_process = [
            (resolution, config) 
            for resolution, config in self.config.items() 
            if resolution not in self.processed_resolutions
        ]

        if not resolutions_to_process:
            print("All resolutions already processed")
            return processed_paths

        print(f"Processing {len(resolutions_to_process)} resolutions in parallel...")
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_resolution = {
                executor.submit(self.process_resolution, resolution, config): resolution
                for resolution, config in resolutions_to_process
            }

            for future in as_completed(future_to_resolution):
                resolution = future_to_resolution[future]
                try:
                    output_path = future.result()
                    if output_path:
                        processed_paths.append(output_path)
                        self.processed_resolutions.add(resolution)
                        self.save_processing_state()
                    else:
                        print(f"❌ Failed to process {resolution}")
                except Exception as e:
                    print(f"❌ Error processing {resolution}: {str(e)}")

        if processed_paths:
            self.create_master_playlist()

        return processed_paths

    def load_processing_state(self):
        state_file = os.path.join(self.output_dir, "processing_state.json")
        if os.path.exists(state_file):
            with open(state_file, "r") as f:
                state = json.load(f)
                self.processed_resolutions = set(state.get("processed_resolutions", []))
                self.master_playlist_content = state.get("master_playlist_content", self.master_playlist_content)

    def save_processing_state(self):
        state_file = os.path.join(self.output_dir, "processing_state.json")
        state = {
            "processed_resolutions": list(self.processed_resolutions),
            "master_playlist_content": self.master_playlist_content
        }
        with open(state_file, "w") as f:
            json.dump(state, f)

    def create_master_playlist(self):
        master_playlist_path = os.path.join(self.output_dir, "master.m3u8")
        with open(master_playlist_path, "w") as f:
            f.write(self.master_playlist_content)
        return master_playlist_path

class AutoScalingVideoProcessor(VideoProcessor):
    def __init__(self, video_path: str, video_id: str):
        self.total_cores = multiprocessing.cpu_count()
        self.calculate_optimal_workers()
        super().__init__(video_path, video_id)
        
    def calculate_optimal_workers(self):
        cpu_usage = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        memory_available_gb = memory.available / (1024 * 1024 * 1024)
        
        if self.total_cores <= 8:
            self.max_workers = max(1, self.total_cores - 1)
        else:
            reserved_cores = min(4, self.total_cores // 8)
            self.max_workers = self.total_cores - reserved_cores
        
        if cpu_usage > 80:
            self.max_workers = max(1, int(self.max_workers * 0.7))
        
        max_workers_by_memory = int(memory_available_gb / 2)
        self.max_workers = min(self.max_workers, max_workers_by_memory)
        
        print(f"Using {self.max_workers} workers for processing")
    
    def process_video(self):
        self.calculate_optimal_workers()
        if self.hw_accel_config:
            self.max_workers = len(self.config)
        return super().process_video()

class ParallelUploadHandler:
    def __init__(self):
        self.calculate_optimal_workers()
        self.max_retries = 3
        self.retry_delay = 2
        
    def calculate_optimal_workers(self):
        total_cores = psutil.cpu_count()
        cpu_usage = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        memory_available_gb = memory.available / (1024 * 1024 * 1024)
        
        if total_cores <= 4:
            self.max_workers = 2
        else:
            self.max_workers = max(2, total_cores // 4)
        
        if cpu_usage > 80:
            self.max_workers = max(2, int(self.max_workers * 0.7))
        
        max_workers_by_memory = int(memory_available_gb / 1)
        self.max_workers = min(self.max_workers, max_workers_by_memory)
        
        self.max_workers = 32
        print(f"\nUpload System Info:")
        print(f"- Upload Workers: {self.max_workers}")

    def _upload_file(self, r2_client, job: UploadJob, bucket_name: str, upload_tracker) -> tuple[bool, str]:
        if upload_tracker.is_uploaded(job.key):
            return True, job.key

        for attempt in range(self.max_retries):
            try:
                with open(job.file_path, 'rb') as f:
                    r2_client.put_object(
                        Bucket=bucket_name,
                        Key=job.key,
                        Body=f,
                        ContentType=job.content_type
                    )
                upload_tracker.mark_uploaded(job.key)
                return True, job.key
            except Exception as e:
                if attempt < self.max_retries - 1:
                    delay = self.retry_delay * (2 ** attempt)
                    print(f"Retry {attempt + 1}/{self.max_retries} for {os.path.basename(job.key)} in {delay}s")
                    time.sleep(delay)
                else:
                    return False, job.key
        return False, job.key

    def parallel_upload(self, r2_client, jobs: List[UploadJob], bucket_name: str, upload_tracker) -> List[str]:
        failed_uploads = []
        resolution_jobs = {}
        
        for job in jobs:
            if job.resolution not in resolution_jobs:
                resolution_jobs[job.resolution] = []
            resolution_jobs[job.resolution].append(job)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for resolution, res_jobs in resolution_jobs.items():
                print(f"\nUploading {resolution} files...")
                
                future_to_job = {
                    executor.submit(
                        self._upload_file,
                        r2_client,
                        job,
                        bucket_name,
                        upload_tracker
                    ): job for job in res_jobs
                }
                
                resolution_failed = False
                for future in concurrent.futures.as_completed(future_to_job):
                    job = future_to_job[future]
                    try:
                        success, key = future.result()
                        if not success:
                            failed_uploads.append(key)
                            resolution_failed = True
                            print(f"❌ Failed to upload after {self.max_retries} attempts: {os.path.basename(job.key)}")
                    except Exception as e:
                        failed_uploads.append(job.key)
                        resolution_failed = True
                        print(f"❌ Error uploading {os.path.basename(job.key)}: {str(e)}")
                
                if not resolution_failed:
                    print(f"✓ Completed uploading {resolution}")
        
        if failed_uploads:
            print(f"\n❌ Total failed uploads: {len(failed_uploads)}")
        
        return failed_uploads

def log_completed_video(video_id: str, playlist_url: str):
    """
    Log successfully completed video processing to a file
    
    Args:
        video_id (str): The ID of the processed video
        playlist_url (str): The source playlist URL
    """
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "processed_videos.log")
    
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    
    log_entry = {
        "video_id": video_id,
        "playlist_url": playlist_url,
        "timestamp": timestamp
    }
    
    with open(log_file, "a") as f:
        json.dump(log_entry, f)
        f.write("\n")
    
    print(f"✓ Added video {video_id} to processing log")

def is_video_processed(video_id: str) -> bool:
    """
    Check if a video has been successfully processed before
    
    Args:
        video_id (str): The ID of the video to check
        
    Returns:
        bool: True if video was successfully processed, False otherwise
    """
    log_file = os.path.join("logs", "processed_videos.log")
    if not os.path.exists(log_file):
        return False
        
    with open(log_file, "r") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("video_id") == video_id:
                    return True
            except json.JSONDecodeError:
                continue
    
    return False

def process_single_video(video_path: str, video_id: str):
    """
    Processes a single video file for transcoding and upload.

    Args:
        video_path (str): Path to the source video file.
        video_id (str): The unique ID for the video.
    """
    r2 = boto3.client('s3',
        endpoint_url=f"https://{os.getenv('CLOUDFLARE_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=os.getenv('R2_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('R2_SECRET_ACCESS_KEY'),
        region_name='auto',
        config=Config(max_pool_connections=64)
    )
    upload_handler = ParallelUploadHandler()

    try:
        print(f"\nProcessing video: {video_id}")
        upload_tracker = UploadTracker(video_id)
        processor = AutoScalingVideoProcessor(video_path, video_id)
        processed_paths = processor.process_video()

        # If no resolutions were processed, it's a failure.
        if not processed_paths and not processor.processed_resolutions:
            print(f"❌ No video resolutions were processed for {video_id}. Aborting.")
            exit(1)

        # Prepare upload jobs
        upload_jobs = []

        # Add jobs for each resolution
        for path in processed_paths:
            resolution = os.path.basename(path)
            files = os.listdir(path)

            print(f"\nPreparing {resolution} files ({len(files)} files) for upload:")
            for file in files:
                file_path = os.path.join(path, file)
                key = f"{video_id}/{resolution}/{file}"
                content_type = 'application/x-mpegURL' if file.endswith('.m3u8') else 'video/MP2T'

                upload_jobs.append(UploadJob(
                    file_path=file_path,
                    key=key,
                    content_type=content_type,
                    resolution=resolution
                ))

        # Add master playlist job
        master_playlist_path = os.path.join(processor.output_dir, "master.m3u8")
        if os.path.exists(master_playlist_path):
            upload_jobs.append(UploadJob(
                file_path=master_playlist_path,
                key=f"{video_id}/master.m3u8",
                content_type='application/x-mpegURL',
                resolution='master'
            ))

        # Perform parallel upload
        print(f"\nStarting parallel upload for {video_id}:")
        failed_uploads = upload_handler.parallel_upload(
            r2,
            upload_jobs,
            os.getenv('BUCKET_NAME'),
            upload_tracker
        )

        # Handle failed uploads
        if failed_uploads:
            print(f"\nRetrying failed uploads for {video_id}:")
            retry_jobs = [job for job in upload_jobs if job.key in failed_uploads]
            failed_uploads = upload_handler.parallel_upload(
                r2,
                retry_jobs,
                os.getenv('BUCKET_NAME'),
                upload_tracker
            )

        # Cleanup logic
        if failed_uploads:
            # Don't remove the source video if uploads fail, but log the error
            print(f"⚠ Some files failed to upload for {video_id}. Keeping processed files for later retry.")
            print(f"Files are located in: {processor.output_dir}")
            failed_files_path = os.path.join(processor.output_dir, "failed_uploads.json")
            with open(failed_files_path, "w") as f:
                json.dump({
                    "failed_uploads": failed_uploads,
                    "video_id": video_id,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "processing_dir": processor.output_dir
                }, f, indent=2)
            # Exit with an error code to signal failure to the main script
            exit(1)
        else:
            shutil.rmtree(processor.output_dir)
            # The main script will handle deleting the source video
            print(f"✓ Completed processing and uploading {video_id}")
            # Log only successful completion
            log_completed_video(video_id, "N/A")

    except Exception as e:
        print(f"Error processing {video_id}: {str(e)}")
        exit(1)

def main():
    parser = argparse.ArgumentParser(description="Transcode and upload a video to HLS format.")
    parser.add_argument('--video-path', required=True, help="Path to the source video file.")
    parser.add_argument('--video-id', required=True, help="Unique ID for the video.")
    # Deprecated arguments, kept for potential backward compatibility but not used
    parser.add_argument('--playlist-file', help="[DEPRECATED] Path to the playlist file.")
    parser.add_argument('--cookies-file', help="[DEPRECATED] Path to the cookies file.")

    args = parser.parse_args()

    if not os.path.exists(args.video_path):
        print(f"Error: Video file not found at {args.video_path}")
        exit(1)

    process_single_video(args.video_path, args.video_id)

if __name__ == "__main__":
    main()
