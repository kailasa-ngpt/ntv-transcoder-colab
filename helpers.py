"""
Helper utilities for the NTV transcoder Colab notebook.

- download_video(): public Google Drive -> gdown, anything else -> yt-dlp
- get_duration():   ffprobe -> (seconds, "MM:SS" / "H:MM:SS")
- make_thumbnail(): grab a frame -> jpg -> webp
- upload_file_to_r2(): put a single object into the Cloudflare R2 bucket
"""

import os
import re
import subprocess
from pathlib import Path


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #
def extract_drive_id(url: str):
    """Return the Google Drive file id from common share-URL forms, else None."""
    if not url:
        return None
    for pattern in (
        r"/file/d/([a-zA-Z0-9_-]{20,})",
        r"[?&]id=([a-zA-Z0-9_-]{20,})",
        r"/d/([a-zA-Z0-9_-]{20,})",
    ):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def is_drive_url(url: str) -> bool:
    url = url or ""
    return "drive.google.com" in url or "docs.google.com" in url


def download_video(link: str, dest_path: str) -> str:
    """
    Download a *public* video to dest_path.

    Google Drive links use gdown; everything else uses yt-dlp.
    Returns the local path. Raises on failure / empty output.
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and dest.stat().st_size > 0:
        print(f"[skip] already downloaded: {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
        return str(dest)

    if is_drive_url(link):
        file_id = extract_drive_id(link)
        if not file_id:
            raise ValueError(f"Could not parse a Google Drive file id from: {link}")
        import gdown  # imported lazily so the module loads even before pip install

        print(f"[gdown] downloading Drive file id={file_id} ...")
        gdown.download(id=file_id, output=str(dest), quiet=False)
    else:
        print(f"[yt-dlp] downloading {link} ...")
        subprocess.run(
            ["yt-dlp", "-f", "bv*+ba/b", "--merge-output-format", "mp4",
             "-o", str(dest), link],
            check=True,
        )

    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"Download failed or produced an empty file: {dest}")
    print(f"[ok] downloaded {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return str(dest)


# --------------------------------------------------------------------------- #
# ffprobe / thumbnails
# --------------------------------------------------------------------------- #
def get_duration(video_path: str):
    """Return (seconds: float, human: str) using ffprobe. ('' human on failure)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True,
    )
    try:
        seconds = float(out.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0, ""
    h, m, s = int(seconds // 3600), int((seconds % 3600) // 60), int(seconds % 60)
    human = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return seconds, human


def make_thumbnail(video_path: str, out_path: str, at_seconds: float = 5.0,
                   width: int = 1280):
    """Grab one frame -> jpg -> webp. Returns the webp path, or None on failure."""
    jpg = str(Path(out_path).with_suffix(".jpg"))
    dur, _ = get_duration(video_path)
    ss = min(at_seconds, dur / 2) if dur else at_seconds

    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(ss),
         "-i", video_path, "-frames:v", "1", "-vf", f"scale={width}:-2", "-y", jpg],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not os.path.exists(jpg):
        print(f"[warn] thumbnail extract failed: {r.stderr[:200]}")
        return None
    try:
        from PIL import Image

        webp = str(Path(out_path).with_suffix(".webp"))
        Image.open(jpg).save(webp, "WEBP", quality=85)
        os.remove(jpg)
        return webp
    except Exception as e:
        print(f"[warn] webp convert failed ({e}); keeping jpg")
        return jpg


# --------------------------------------------------------------------------- #
# Cloudflare R2
# --------------------------------------------------------------------------- #
def r2_client_from_env():
    """Build a boto3 S3 client for Cloudflare R2 from environment variables."""
    import boto3
    from botocore.config import Config

    account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(max_pool_connections=32),
    )


def upload_file_to_r2(local_path: str, key: str, content_type: str, bucket: str = None) -> str:
    """Upload one file to R2 under `key`. Returns the key."""
    bucket = bucket or os.environ.get("BUCKET_NAME", "ntv-ott")
    client = r2_client_from_env()
    with open(local_path, "rb") as f:
        client.put_object(Bucket=bucket, Key=key, Body=f, ContentType=content_type)
    print(f"[ok] uploaded r2://{bucket}/{key}")
    return key
