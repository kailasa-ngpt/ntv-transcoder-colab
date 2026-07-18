"""
CSV to Payload CMS Uploader
============================
This script reads a CSV file with video metadata and pushes each row to the Payload CMS API.
It automatically handles category creation by comparing CSV categories with backend categories.

CSV Format (Your existing format):
    id,title,description,dateOfVideo,categories,viewStatus,views,videoLength,tags,loves

Columns used by script:
    - id → videoId (required)
    - title → title (required)
    - categories → category (required)
    - description → description (optional)
    - videoLength → duration (optional)
    - dateOfVideo → date (optional)

Columns ignored:
    - viewStatus, views, tags, loves (not used by the CMS API)

Example:
    id,title,description,dateOfVideo,categories,viewStatus,views,videoLength,tags,loves
    video-123,Morning Session,Description here,2025-10-22,Nithyananda Satsang 2025,public,1500,45:30,meditation,250
    video-456,Evening Session,Another description,2025-10-23,New Category,public,800,32:15,yoga,120

Usage:
    python csv_to_cms_uploader.py --csv videos.csv
    python csv_to_cms_uploader.py --csv videos.csv --dry-run
"""

import csv
import logging
import time
import os
import argparse
from typing import Dict, List, Optional, Any, Set
from datetime import datetime
from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Load environment variables from .env file
load_dotenv()

# CMS Configuration - loaded from environment variables
CMS_URL = os.getenv("CMS_URL", "https://ntv-dev-cms.koogle.sk")
API_KEY = os.getenv("CMS_API_KEY", "")
CDN_BASE_URL = os.getenv("CDN_BASE_URL", "https://ntv-cdn-dev.koogle.sk")

# Validate required environment variables
if not API_KEY:
    raise ValueError("CMS_API_KEY environment variable is required but not set in .env file")

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('cms_upload.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def create_session_with_retries(
    retries: int = 3,
    backoff_factor: float = 0.5,
    status_forcelist: tuple = (500, 502, 503, 504)
) -> requests.Session:
    """
    Create a requests session with automatic retry logic.

    Args:
        retries: Number of retry attempts
        backoff_factor: Exponential backoff factor
        status_forcelist: HTTP status codes that should trigger a retry

    Returns:
        Configured requests.Session object
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=["POST", "GET", "PATCH"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def load_csv_data(csv_file_path: str) -> List[Dict[str, Any]]:
    """
    Load all video data from CSV file.

    CSV Format: id,title,description,dateOfVideo,categories,viewStatus,views,videoLength,tags,loves

    Args:
        csv_file_path: Path to the CSV file

    Returns:
        List of dictionaries containing video metadata
    """
    videos = []
    
    try:
        with open(csv_file_path, 'r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            
            for row_num, row in enumerate(reader, start=2):  # Start at 2 (1 is header)
                # Map CSV columns to required fields
                video_id = row.get('id', '').strip()
                title = row.get('title', '').strip()
                category = row.get('categories', '').strip()
                description = row.get('description', '').strip()
                date_raw = row.get('dateOfVideo', '').strip()
                duration = row.get('videoLength', '').strip()
                
                # Validate required fields
                if not video_id:
                    logger.warning(f"Row {row_num}: Missing id, skipping")
                    continue
                
                if not title:
                    logger.warning(f"Row {row_num}: Missing title for video {video_id}, skipping")
                    continue
                
                if not category:
                    logger.warning(f"Row {row_num}: Missing categories for video {video_id}, skipping")
                    continue
                
                # Convert date format from YYYY_MM_DD to YYYY-MM-DD
                date_formatted = date_raw.replace('_', '-') if date_raw else ''
                
                # Validate date format (should be YYYY-MM-DD)
                if date_formatted:
                    try:
                        # Try to parse the date to validate it
                        datetime.strptime(date_formatted, '%Y-%m-%d')
                    except ValueError:
                        logger.warning(f"Row {row_num}: Invalid date format '{date_raw}' for video {video_id}, will send as-is")
                
                video_data = {
                    'videoId': video_id,
                    'category': category,
                    'title': title,
                    'description': description,
                    'duration': duration,
                    'date': date_formatted
                }
                
                logger.debug(f"Row {row_num}: Loaded video {video_id} - category: {category}, duration: {duration}, date: {date_formatted}")
                videos.append(video_data)
        
        logger.info(f"Loaded {len(videos)} valid videos from CSV file")
        return videos
    
    except FileNotFoundError:
        logger.error(f"CSV file not found: {csv_file_path}")
        return []
    except Exception as e:
        logger.error(f"Error reading CSV file: {str(e)}")
        return []


def extract_unique_categories(videos: List[Dict[str, Any]]) -> Set[str]:
    """
    Extract unique category names from video data.

    Args:
        videos: List of video dictionaries

    Returns:
        Set of unique category names
    """
    categories = set()
    for video in videos:
        if video.get('category'):
            categories.add(video['category'])
    
    logger.info(f"Found {len(categories)} unique categories in CSV: {sorted(categories)}")
    return categories


def get_all_categories() -> Dict[str, Any]:
    """
    Fetch all categories from the CMS API.

    Returns:
        Dictionary with success status and category data
        Format: {
            'success': True/False,
            'categories': {category_name: category_id, ...},
            'error': error_message (if failed)
        }
    """
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'users API-Key {API_KEY}'
    }

    url = f"{CMS_URL}/api/categories"

    logger.info("Fetching all categories from CMS")

    session = create_session_with_retries()

    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        result = response.json()

        # Extract categories into a name->id mapping
        categories = {}
        if 'docs' in result:
            for cat in result['docs']:
                category_name = cat.get('name', '').strip()
                category_id = cat.get('id')
                if category_name and category_id:
                    categories[category_name] = category_id

        logger.info(f"Successfully fetched {len(categories)} categories from CMS: {sorted(categories.keys())}")

        return {
            'success': True,
            'categories': categories,
            'response': result
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch categories: {e}")
        return {
            'success': False,
            'categories': {},
            'error': str(e)
        }


def create_category(name: str, description: str = "") -> Dict[str, Any]:
    """
    Create a new category in the CMS.

    Args:
        name: Name of the category to create
        description: Optional description for the category

    Returns:
        Dictionary with success status and category data
    """
    if not name or not name.strip():
        return {
            'success': False,
            'error': 'Category name cannot be empty'
        }

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'users API-Key {API_KEY}'
    }

    url = f"{CMS_URL}/api/categories"

    payload = {
        'name': name.strip(),
    }

    if description:
        payload['description'] = description

    logger.info(f"Creating new category: '{name}'")

    session = create_session_with_retries()

    try:
        response = session.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()

        result = response.json()
        category_id = result.get('doc', {}).get('id')

        logger.info(f"[OK] Successfully created category '{name}' with ID: {category_id}")

        return {
            'success': True,
            'category_id': category_id,
            'category_name': name,
            'response': result
        }

    except requests.exceptions.HTTPError as e:
        if response.status_code == 409:
            logger.warning(f"Category '{name}' already exists in CMS")
            return {
                'success': False,
                'category_name': name,
                'error': 'Conflict - Category already exists',
                'status_code': response.status_code
            }

        logger.error(f"Failed to create category '{name}': {e}")
        return {
            'success': False,
            'category_name': name,
            'error': str(e),
            'status_code': response.status_code
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to create category '{name}': {e}")
        return {
            'success': False,
            'category_name': name,
            'error': str(e)
        }


def sync_categories(csv_categories: Set[str], cms_categories: Dict[str, int]) -> Dict[str, int]:
    """
    Compare CSV categories with CMS categories and create missing ones.

    Args:
        csv_categories: Set of category names from CSV
        cms_categories: Dictionary mapping category names to IDs from CMS

    Returns:
        Updated dictionary mapping all category names to IDs
    """
    logger.info("=" * 60)
    logger.info("CATEGORY SYNCHRONIZATION")
    logger.info("=" * 60)
    
    # Find categories that need to be created
    missing_categories = csv_categories - set(cms_categories.keys())
    
    if missing_categories:
        logger.info(f"Found {len(missing_categories)} new categories to create:")
        for cat in sorted(missing_categories):
            logger.info(f"  - {cat}")
    else:
        logger.info("All CSV categories already exist in CMS")
    
    # Create missing categories
    updated_categories = cms_categories.copy()
    
    for category_name in sorted(missing_categories):
        result = create_category(category_name)
        
        if result['success']:
            updated_categories[category_name] = result['category_id']
        else:
            logger.error(f"Failed to create category '{category_name}': {result.get('error')}")
        
        # Brief delay between category creations
        time.sleep(0.5)
    
    logger.info("=" * 60)
    logger.info(f"Category sync complete. Total categories: {len(updated_categories)}")
    logger.info("=" * 60)
    
    return updated_categories


def push_video_to_cms(
    video_data: Dict[str, Any],
    category_id: int,
    resolutions: List[str] = None
) -> Dict[str, Any]:
    """
    Push video data to Payload CMS API.

    Args:
        video_data: Dictionary containing video metadata
        category_id: The ID of the category for this video
        resolutions: List of available resolutions

    Returns:
        API response dictionary
    """
    if resolutions is None:
        resolutions = ['1080p', '720p', '480p', '360p', '240p']

    # Prepare payload
    payload = {
        'videoId': video_data['videoId'],
        'title': video_data['title'],
        'category': category_id,
        'resolutions': resolutions
    }

    # Add optional fields if present and not empty
    if video_data.get('description'):
        payload['description'] = video_data['description']

    if video_data.get('duration'):
        payload['duration'] = video_data['duration']

    if video_data.get('date'):
        payload['date'] = video_data['date']

    # Prepare headers
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'users API-Key {API_KEY}'
    }

    # API endpoint
    url = f"{CMS_URL}/api/videos"

    logger.info(f"Pushing video: {video_data['videoId']} - {video_data['title']}")
    logger.info(f"Payload: videoId={video_data['videoId']}, category={category_id}, duration={video_data.get('duration')}, date={video_data.get('date')}")
    logger.debug(f"Full payload: {payload}")

    session = create_session_with_retries()

    try:
        response = session.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()

        result = response.json()
        logger.info(f"[OK] Successfully created video: {video_data['videoId']}")

        return {
            'success': True,
            'video_id': video_data['videoId'],
            'cms_id': result.get('doc', {}).get('id'),
            'message': result.get('message', 'Video successfully created'),
            'response': result
        }

    except requests.exceptions.HTTPError as e:
        # Try to get detailed error message from response
        try:
            error_detail = response.json()
            logger.error(f"[ERROR] API Response: {error_detail}")
        except:
            logger.error(f"[ERROR] Response text: {response.text}")
        
        logger.error(f"[ERROR] Payload sent: {payload}")
        
        if response.status_code == 409:
            logger.warning(f"[SKIP] Video {video_data['videoId']} already exists in CMS")
            return {
                'success': False,
                'video_id': video_data['videoId'],
                'error': 'Conflict - Video already exists',
                'status_code': response.status_code
            }

        logger.error(f"[FAIL] HTTP error pushing video {video_data['videoId']}: {e}")
        return {
            'success': False,
            'video_id': video_data['videoId'],
            'error': str(e),
            'status_code': response.status_code
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"[FAIL] Request error pushing video {video_data['videoId']}: {e}")
        return {
            'success': False,
            'video_id': video_data['videoId'],
            'error': str(e)
        }

    except Exception as e:
        logger.error(f"[FAIL] Unexpected error pushing video {video_data['videoId']}: {e}")
        return {
            'success': False,
            'video_id': video_data['videoId'],
            'error': str(e)
        }


def process_csv_to_cms(
    csv_file_path: str,
    resolutions: List[str] = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Main function to process CSV file and push all videos to CMS.

    Args:
        csv_file_path: Path to the CSV file
        resolutions: List of available resolutions
        dry_run: If True, only simulate the process without making changes

    Returns:
        Summary dictionary with results
    """
    logger.info("=" * 60)
    logger.info("CSV TO CMS UPLOADER - STARTING")
    logger.info("=" * 60)
    logger.info(f"CSV File: {csv_file_path}")
    logger.info(f"Dry Run: {dry_run}")
    logger.info("=" * 60)

    # Step 1: Load CSV data
    logger.info("\n[STEP 1] Loading CSV data...")
    videos = load_csv_data(csv_file_path)
    
    if not videos:
        logger.error("No valid videos found in CSV. Exiting.")
        return {
            'success': False,
            'error': 'No valid videos found in CSV'
        }
    
        logger.info(f"[OK] Loaded {len(videos)} videos from CSV")

    # Step 2: Extract unique categories from CSV
    logger.info("\n[STEP 2] Extracting categories from CSV...")
    csv_categories = extract_unique_categories(videos)

    # Step 3: Fetch existing categories from CMS
    logger.info("\n[STEP 3] Fetching existing categories from CMS...")
    cms_result = get_all_categories()
    
    if not cms_result['success']:
        logger.error(f"Failed to fetch categories from CMS: {cms_result.get('error')}")
        return {
            'success': False,
            'error': 'Failed to fetch categories from CMS'
        }
    
    cms_categories = cms_result['categories']

    # Step 4: Sync categories (create missing ones)
    if dry_run:
        logger.info("\n[STEP 4] DRY RUN - Would create the following categories:")
        missing = csv_categories - set(cms_categories.keys())
        if missing:
            for cat in sorted(missing):
                logger.info(f"  - {cat}")
        else:
            logger.info("  No new categories needed")
        category_mapping = cms_categories
    else:
        logger.info("\n[STEP 4] Synchronizing categories...")
        category_mapping = sync_categories(csv_categories, cms_categories)

    # Step 5: Push videos to CMS
    logger.info("\n[STEP 5] Pushing videos to CMS...")
    logger.info("=" * 60)
    
    results = []
    successful = 0
    failed = 0
    skipped = 0

    for idx, video in enumerate(videos, start=1):
        logger.info(f"\nProcessing video {idx}/{len(videos)}: {video['videoId']}")
        
        # Get category ID
        category_name = video['category']
        category_id = category_mapping.get(category_name)
        
        if not category_id:
            logger.error(f"[ERROR] Category '{category_name}' not found in mapping. Skipping video.")
            results.append({
                'success': False,
                'video_id': video['videoId'],
                'error': f'Category not found: {category_name}'
            })
            skipped += 1
            continue
        
        if dry_run:
            logger.info(f"DRY RUN - Would push video: {video['videoId']} to category '{category_name}' (ID: {category_id})")
            results.append({
                'success': True,
                'video_id': video['videoId'],
                'dry_run': True
            })
            successful += 1
        else:
            # Push video to CMS
            result = push_video_to_cms(video, category_id, resolutions)
            results.append(result)
            
            if result['success']:
                successful += 1
            else:
                failed += 1
            
            # Brief delay between requests to avoid rate limiting
            time.sleep(0.5)

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("UPLOAD COMPLETE - SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total videos processed: {len(videos)}")
    logger.info(f"Successful: {successful}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Skipped: {skipped}")
    logger.info("=" * 60)

    return {
        'success': True,
        'total': len(videos),
        'successful': successful,
        'failed': failed,
        'skipped': skipped,
        'results': results
    }


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description='Upload videos from CSV to Payload CMS',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example CSV format (your existing format):
    id,title,description,dateOfVideo,categories,viewStatus,views,videoLength,tags,loves
    video-123,Morning Session,Description,2025-10-22,Nithyananda Satsang 2025,public,1500,45:30,meditation,250
    video-456,Evening Session,Another description,2025-10-23,New Category,public,800,32:15,yoga,120

The script uses: id, title, categories (required) and description, videoLength, dateOfVideo (optional)
The script ignores: viewStatus, views, tags, loves

Example usage:
    python csv_to_cms_uploader.py --csv videos.csv
    python csv_to_cms_uploader.py --csv videos.csv --dry-run
    python csv_to_cms_uploader.py --csv videos.csv --resolutions 1080p 720p 480p
        """
    )
    
    parser.add_argument(
        '--csv',
        required=True,
        help='Path to the CSV file containing video metadata'
    )
    
    parser.add_argument(
        '--resolutions',
        nargs='+',
        default=['1080p', '720p', '480p', '360p', '240p'],
        help='List of resolutions (default: 1080p 720p 480p 360p 240p)'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Simulate the process without making any changes'
    )
    
    args = parser.parse_args()
    
    # Process the CSV
    result = process_csv_to_cms(
        csv_file_path=args.csv,
        resolutions=args.resolutions,
        dry_run=args.dry_run
    )
    
    # Exit with appropriate code
    if result['success'] and result.get('failed', 0) == 0:
        exit(0)
    else:
        exit(1)


if __name__ == "__main__":
    main()
