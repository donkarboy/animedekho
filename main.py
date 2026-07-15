"""
main.py  —  FastAPI server for Render deployment
=================================================
Endpoints:
  GET  /                          → health check / usage info
  GET  /extract?url={embed_url}   → returns JSON stream result
  POST /extract                   → body: {"url": "..."} → JSON stream result

Deploy on Render:
  - Build Command : pip install -r requirements.txt
  - Start Command : uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import re
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── APP SETUP ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="AnimeDekho Stream Extractor API",
    description="Extracts HLS stream URLs from animedekho.app embed pages.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── CORE EXTRACTOR ────────────────────────────────────────────────────────────

def get_stream(embed_url: str) -> dict:
    s = requests.Session()

    # Step 1: Fetch embed page → extract 32-char video hash
    try:
        resp = s.get(embed_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Embed page request timed out.")
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Embed page error: {e}")

    match = re.search(r'/video/([a-f0-9]{32})', resp.text)
    if not match:
        raise HTTPException(
            status_code=422,
            detail="Could not find video hash in embed page. URL may be invalid or page structure changed."
        )

    hash_ = match.group(1)
    cdn_video_url = f"https://as-cdn21.top/video/{hash_}"

    # Step 2: Hit CDN video page → sets fireplayer_player session cookie
    try:
        s.get(cdn_video_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    except Exception:
        pass  # Cookie step best-effort; continue anyway

    # Step 3: POST to getVideo API → signed m3u8 URL
    api_url = f"https://as-cdn21.top/player/index.php?data={hash_}&do=getVideo"
    try:
        api_resp = s.post(
            api_url,
            data={"hash": hash_, "r": embed_url},
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": cdn_video_url,
                "Origin": "https://as-cdn21.top",
            },
            timeout=20,
        )
        api_resp.raise_for_status()
        data = api_resp.json()
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="getVideo API request timed out.")
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"getVideo API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse API response: {e}")

    stream_url = data.get("videoSource") or data.get("securedLink") or ""

    return {
        "success": True,
        "input_url": embed_url,
        "video_hash": hash_,
        "cdn_video_page": cdn_video_url,
        "hls": data.get("hls", False),
        "stream_url": stream_url,
        "thumbnail": data.get("videoImage", ""),
        "download_links": data.get("downloadLinks", []),
    }


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=JSONResponse)
def root():
    """Health check + API usage info."""
    return {
        "status": "ok",
        "service": "AnimeDekho Stream Extractor API",
        "version": "1.0.0",
        "usage": {
            "GET":  "/extract?url=https://animedekho.app/embed/31910/1-26",
            "POST": {
                "endpoint": "/extract",
                "body": {"url": "https://animedekho.app/embed/31910/1-26"},
            },
        },
        "example_response": {
            "success": True,
            "input_url": "https://animedekho.app/embed/31910/1-26",
            "video_hash": "39059724f73a9969845dfe4146c5660e",
            "cdn_video_page": "https://as-cdn21.top/video/39059724f73a9969845dfe4146c5660e",
            "hls": True,
            "stream_url": "https://as-cdn21.top/cdn/hls/.../master.m3u8?md5=...&expires=...",
            "thumbnail": "https://as-cdn22.top/p/....jpg",
            "download_links": [],
        },
    }


@app.get("/extract", response_class=JSONResponse)
def extract_get(
    url: str = Query(..., description="animedekho.app embed URL", example="https://animedekho.app/embed/31910/1-26")
):
    """
    GET /extract?url=https://animedekho.app/embed/31910/1-26

    Returns JSON with the HLS stream URL and metadata.
    """
    if not url.strip():
        raise HTTPException(status_code=400, detail="url parameter is required.")
    return get_stream(url.strip())


class ExtractBody(BaseModel):
    url: str

    model_config = {
        "json_schema_extra": {
            "example": {"url": "https://animedekho.app/embed/31910/1-26"}
        }
    }


@app.post("/extract", response_class=JSONResponse)
def extract_post(body: ExtractBody):
    """
    POST /extract
    Body: {"url": "https://animedekho.app/embed/31910/1-26"}

    Returns JSON with the HLS stream URL and metadata.
    """
    if not body.url.strip():
        raise HTTPException(status_code=400, detail="url field is required.")
    return get_stream(body.url.strip())


# ── LOCAL DEV ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)