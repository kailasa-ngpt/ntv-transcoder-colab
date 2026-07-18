# NTV Transcoder — Colab

Custom **GPU FFmpeg** (NVENC + `scale_npp`) packaged to run in **Google Colab**. Give it public
video links, and it downloads → transcodes to multi-resolution **HLS** → uploads to **Cloudflare R2**
→ (optionally) pushes the video details to the **NTV / Payload CMS**.

> The bundled `ffmpeg` / `ffprobe` are **custom Linux x86-64 builds** with NVIDIA hardware
> acceleration. They require a **GPU Colab runtime** (T4 or L4).

---

## Open in Colab

**Option A — direct link** (Colab will ask you to authorize GitHub because this repo is private):

```
https://colab.research.google.com/github/kailasa-ngpt/ntv-transcoder-colab/blob/main/ntv_transcoder_colab.ipynb
```

**Option B — upload:** download `ntv_transcoder_colab.ipynb` and open it via *Colab → File → Upload notebook*.

Then: **Runtime → Change runtime type → GPU (T4 or L4)**.

---

## What you provide

1. **A GitHub token** (Colab Secret `GITHUB_TOKEN`, a PAT with read access) — so the private repo can be cloned.
2. **Cloudflare R2 credentials** (Colab Secrets, recommended):
   `CLOUDFLARE_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `BUCKET_NAME`.
3. **Public video links** — Google Drive (downloaded with `gdown`) or any yt-dlp-supported URL.
4. *(Optional)* **NTV CMS** `CMS_URL` + `CMS_API_KEY` to push each video's details.
5. *(Optional)* `CDN_BASE_URL` — the public host serving the bucket (for clean playback URLs).

Add all of these as **Colab Secrets** (🔑 icon in the left sidebar) so keys never live in the notebook.
See [`.env.example`](.env.example) for the full list.

---

## Notebook steps

| # | Cell | What it does |
|---|------|--------------|
| 1 | Setup | GPU check, clone repo, install custom `ffmpeg`/`ffprobe`, `pip install -r requirements.txt`, verify NVENC + `scale_npp` |
| 2 | Credentials | Load R2 / CMS creds from Colab Secrets (or inline fields) into the environment |
| 3 | Videos | **Paste one link per line** in `LINKS` — optionally `link \| video_id \| title \| category \| date` |
| 4 | Download | `gdown` for Drive links, `yt-dlp` otherwise → `workspace/<video_id>.mp4` |
| 5 | Transcode | `ott-transcoder.py` → HLS 240p–1080p → upload to R2; grabs duration + a `thumbnail.webp` |
| 6 | CMS push | *(optional)* create category if needed + `POST /api/videos` with the details |
| 7 | Summary | Prints the `master.m3u8` / per-resolution playback URLs |

Output layout in R2 (`<video_id>` is your key):

```
<video_id>/master.m3u8
<video_id>/1080p/playlist.m3u8 + segment_###.ts
<video_id>/720p/...  480p/...  360p/...  240p/...
<video_id>/thumbnail.webp
```

---

## Files

| File | Purpose |
|------|---------|
| `ntv_transcoder_colab.ipynb` | The Colab notebook (the thing you run) |
| `ffmpeg`, `ffprobe` | Custom NVENC/`scale_npp` Linux builds |
| `ott-transcoder.py` | Single-video HLS transcode + R2 upload (called per video) |
| `helpers.py` | Download (gdown/yt-dlp), ffprobe duration, thumbnail, R2 upload |
| `ntv_cms.py` | `NTVClient` — category sync + push video to the CMS |
| `process_youtube_videos.py` | Batch pipeline (rclone/gdrive → transcode → YouTube metadata → CSV) |
| `csv_to_cms_uploader.py` | Bulk CSV → CMS uploader |
| `docs/ffmpeg-build-steps.txt` | How the custom ffmpeg is loaded/rebuilt |

---

## No rclone needed for public links

Public links are fetched directly with `gdown` / `yt-dlp`. `rclone` is only needed to **search Drive**
for a file you don't have a direct link to — the notebook includes an optional, collapsed rclone cell
for that case (upload your own `rclone.conf` + service-account JSON at runtime; neither is committed).

---

## Security

- **No credentials are committed.** `.env`, `rclone.conf`, and service-account JSON are `.gitignore`d.
- Provide all keys via **Colab Secrets** at runtime.
- If any key was ever exposed, rotate it in Cloudflare / Google / the CMS.
