"""
Push transcoded video details to the NTV / Payload CMS.

Auth: header  ->  Authorization: users API-Key <CMS_API_KEY>
Endpoints:
    GET  {CMS_URL}/api/categories
    POST {CMS_URL}/api/categories   {"name": ...}
    POST {CMS_URL}/api/videos       {"videoId","title","category","resolutions",...}

Usage:
    from ntv_cms import NTVClient
    client = NTVClient(cms_url, api_key)
    client.push_video(video_id="abc", title="My Video", category="Satsang 2025",
                      duration="45:30", date="2025-01-01")
"""

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DEFAULT_RESOLUTIONS = ["1080p", "720p", "480p", "360p", "240p"]


def _session(retries: int = 3, backoff: float = 0.5,
             status=(500, 502, 503, 504)) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=retries, backoff_factor=backoff,
                  status_forcelist=status, allowed_methods=["GET", "POST", "PATCH"])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


class NTVClient:
    def __init__(self, cms_url: str, api_key: str):
        if not cms_url or not api_key:
            raise ValueError("cms_url and api_key are both required")
        self.cms_url = cms_url.rstrip("/")
        self.api_key = api_key
        self.session = _session()

    @property
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"users API-Key {self.api_key}",
        }

    # ---- categories ------------------------------------------------------- #
    def get_categories(self) -> dict:
        """Return {category_name: category_id}."""
        r = self.session.get(f"{self.cms_url}/api/categories",
                             headers=self._headers, timeout=30)
        r.raise_for_status()
        out = {}
        for cat in r.json().get("docs", []):
            name = (cat.get("name") or "").strip()
            if name and cat.get("id"):
                out[name] = cat["id"]
        return out

    def create_category(self, name: str):
        name = (name or "").strip()
        r = self.session.post(f"{self.cms_url}/api/categories",
                              json={"name": name}, headers=self._headers, timeout=30)
        if r.status_code == 409:  # already exists — look it up
            return self.get_categories().get(name)
        r.raise_for_status()
        return r.json().get("doc", {}).get("id")

    def ensure_category(self, name: str):
        """Return the id for a category, creating it if missing."""
        name = (name or "").strip()
        if not name:
            raise ValueError("category name is required")
        existing = self.get_categories()
        if name in existing:
            return existing[name]
        cid = self.create_category(name)
        logger.info("Created category '%s' -> %s", name, cid)
        return cid

    # ---- videos ----------------------------------------------------------- #
    def push_video(self, video_id: str, title: str, category,
                   resolutions=None, description: str = None,
                   duration: str = None, date: str = None) -> dict:
        """
        Create a video document in the CMS.

        `category` may be a name (str, auto-resolved/created) or an id (int).
        Returns {'success': bool, ...}.
        """
        resolutions = resolutions or DEFAULT_RESOLUTIONS
        category_id = category if isinstance(category, int) else self.ensure_category(category)

        payload = {
            "videoId": video_id,
            "title": title,
            "category": category_id,
            "resolutions": resolutions,
        }
        if description:
            payload["description"] = description
        if duration:
            payload["duration"] = duration
        if date:
            payload["date"] = date

        r = self.session.post(f"{self.cms_url}/api/videos", json=payload,
                              headers=self._headers, timeout=30)

        if r.status_code == 409:
            return {"success": False, "video_id": video_id,
                    "error": "already exists (409)"}
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            return {"success": False, "video_id": video_id,
                    "error": str(e), "detail": detail}

        return {"success": True, "video_id": video_id,
                "cms_id": r.json().get("doc", {}).get("id"), "response": r.json()}
