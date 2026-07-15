"""
main.py  ---  FastAPI server for Render deployment
===================================================
Fix for 403: animedekho.app blocks datacenter IPs (Render/AWS).
Solution: full browser headers + optional HTTP proxy via PROXY_URL env var.

Endpoints:
  GET  /                          -> health check / usage info
  GET  /extract?url={embed_url}   -> returns JSON stream result
  POST /extract                   -> body: {"url": "..."} -> JSON stream result

Deploy on Render:
  - Build Command : pip install -r requirements.txt
  - Start Command : uvicorn main:app --host 0.0.0.0 --port $PORT

  Optional env var in Render dashboard -> Environment:
  PROXY_URL = http://user:pass@host:port   <- residential proxy fixes 403
              (free tier: webshare.io gives 10 free residential IPs)
"""

import os
import re
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---- APP SETUP ---------------------------------------------------------------

app = FastAPI(
    title="AnimeDekho Stream Extractor API",
    description="Extracts HLS stream URLs from animedekho.app embed pages.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---- PROXY CONFIG ------------------------------------------------------------
# Set PROXY_URL in Render dashboard > Environment Variables
# e.g.  http://user:pass@residential-proxy-host:port
PROXY_URL = os.environ.get("PROXY_URL", "")
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# ---- FULL BROWSER HEADERS (Chrome 124 on Windows) ---------------------------
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Cache-Control": "max-age=0",
    "DNT": "1",
}


# ---- CORE EXTRACTOR ----------------------------------------------------------

def get_stream(embed_url: str) -> dict:
    s = requests.Session()
    if PROXIES:
        s.proxies.update(PROXIES)

    # Step 1: Fetch embed page -> extract 32-char video hash
    try:
        resp = s.get(
            embed_url,
            headers={**BROWSER_HEADERS, "Referer": "https://www.google.com/"},
            timeout=25,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Embed page request timed out.")
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status == 403:
            raise HTTPException(
                status_code=403,
                detail=(
                    "animedekho.app returned 403 — Render server IP is blocked by the site. "
                    "Fix: add PROXY_URL env var in Render dashboard (Environment tab) pointing "
                    "to a residential proxy (free: webshare.io). "
                    f"Proxy currently configured: {'YES' if PROXY_URL else 'NO'}"
                ),
            )
        raise HTTPException(status_code=502, detail=f"Embed page HTTP {status}: {e}")
    except requests.exceptions.ConnectionError as e:
        raise HTTPException(status_code=502, detail=f"Connection error: {e}")

    match = re.search(r'/video/([a-f0-9]{32})', resp.text)
    if not match:
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not find video hash in embed page. "
                "URL may be invalid or page structure changed. "
                f"Page length received: {len(resp.text)} chars"
            ),
        )

    hash_ = match.group(1)
    cdn_video_url = f"https://as-cdn21.top/video/{hash_}"

    # Step 2: Hit CDN page -> sets fireplayer_player session cookie
    try:
        s.get(
            cdn_video_url,
            headers={**BROWSER_HEADERS, "Referer": embed_url, "Sec-Fetch-Site": "cross-site"},
            timeout=20,
        )
    except Exception:
        pass  # best-effort; proceed even if this fails

    # Step 3: POST to getVideo API -> signed m3u8 URL
    api_url = f"https://as-cdn21.top/player/index.php?data={hash_}&do=getVideo"
    try:
        api_resp = s.post(
            api_url,
            data={"hash": hash_, "r": embed_url},
            headers={
                "User-Agent": BROWSER_HEADERS["User-Agent"],
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://as-cdn21.top",
                "Referer": cdn_video_url,
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "Sec-CH-UA": BROWSER_HEADERS["Sec-CH-UA"],
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": BROWSER_HEADERS["Sec-CH-UA-Platform"],
            },
            timeout=20,
        )
        api_resp.raise_for_status()
        data = api_resp.json()
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="getVideo API timed out.")
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"getVideo API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse getVideo response: {e}")

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
        "proxy_used": bool(PROXY_URL),
    }


# ---- ROUTES ------------------------------------------------------------------

@app.get("/", response_class=JSONResponse)
def root():
    return {
        "status": "ok",
        "service": "AnimeDekho Stream Extractor API",
        "version": "2.0.0",
        "proxy_configured": bool(PROXY_URL),
        "usage": {
            "GET":  "/extract?url=https://animedekho.app/embed/31910/1-26",
            "POST": {"endpoint": "/extract", "body": {"url": "https://animedekho.app/embed/31910/1-26"}},
        },
        "fix_403": (
            "animedekho.app blocks datacenter IPs. "
            "Add PROXY_URL env var in Render dashboard pointing to a residential proxy. "
            "Free option: webshare.io (10 free residential IPs after signup)."
        ),
    }


@app.get("/extract", response_class=JSONResponse)
def extract_get(
    url: str = Query(..., description="animedekho.app embed URL",
                     example="https://animedekho.app/embed/31910/1-26")
):
    if not url.strip():
        raise HTTPException(status_code=400, detail="url parameter is required.")
    return get_stream(url.strip())


class ExtractBody(BaseModel):
    url: str
    model_config = {
        "json_schema_extra": {"example": {"url": "https://animedekho.app/embed/31910/1-26"}}
    }


@app.post("/extract", response_class=JSONResponse)
def extract_post(body: ExtractBody):
    if not body.url.strip():
        raise HTTPException(status_code=400, detail="url field is required.")
    return get_stream(body.url.strip())


# ---- LOCAL DEV ---------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)