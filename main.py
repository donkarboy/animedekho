"""
main.py --- FastAPI server for Render deployment
===================================================
Fix for 403: animedekho.app blocks datacenter IPs (Render/AWS).
Solution: full browser headers + rotating residential proxies.
Endpoints:
  GET / -> health check / usage info
  GET /extract?url={embed_url} -> returns JSON stream result
  POST /extract -> body: {"url": "..."} -> JSON stream result
Deploy on Render:
  - Build Command : pip install -r requirements.txt
  - Start Command : uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import os
import re
import random
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---- APP SETUP ---------------------------------------------------------------
app = FastAPI(
    title="AnimeDekho Stream Extractor API (Rotating Proxies)",
    description="Extracts HLS stream URLs from animedekho.app embed pages.",
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---- PROXY CONFIG ------------------------------------------------------------
# The list of residential proxies provided
PROXY_LIST = [
    "http://klgcswuo:zajxdew027s2@31.59.20.176:6754",
    "http://klgcswuo:zajxdew027s2@31.56.127.193:7684",
    "http://klgcswuo:zajxdew027s2@45.38.107.97:6014",
    "http://klgcswuo:zajxdew027s2@198.105.121.200:6462",
    "http://klgcswuo:zajxdew027s2@64.137.96.74:6641",
    "http://klgcswuo:zajxdew027s2@198.23.243.226:6361",
    "http://klgcswuo:zajxdew027s2@38.154.185.97:6370",
    "http://klgcswuo:zajxdew027s2@84.247.60.125:6095",
]

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
    # Shuffle proxies so we don't spam the same dead proxy repeatedly
    proxies_to_try = list(PROXY_LIST)
    random.shuffle(proxies_to_try)
    
    # Check if a custom Render PROXY_URL is set as a fallback
    env_proxy = os.environ.get("PROXY_URL", "")
    if env_proxy and env_proxy not in proxies_to_try:
        proxies_to_try.append(env_proxy)

    # Fallback to a direct connection if no proxies are defined
    if not proxies_to_try:
        proxies_to_try = [None]

    last_error = None

    # Rotate through available proxies until one succeeds
    for proxy_url in proxies_to_try:
        s = requests.Session()
        if proxy_url:
            s.proxies.update({"http": proxy_url, "https": proxy_url})
            
        try:
            # Step 1: Fetch embed page -> extract 32-char video hash
            resp = s.get(
                embed_url,
                headers={**BROWSER_HEADERS, "Referer": "https://www.google.com/"},
                timeout=12,  # Shortened timeout to rotate faster
                allow_redirects=True,
            )
            resp.raise_for_status()
            
            match = re.search(r'/video/([a-f0-9]{32})', resp.text)
            if not match:
                # If we got a 200 OK but no hash, we likely hit a Captcha/Block page. Try next proxy.
                raise ValueError(f"No video hash found (Page len: {len(resp.text)}). Likely a block page.")

            hash_ = match.group(1)
            cdn_video_url = f"https://as-cdn21.top/video/{hash_}"
            
            # Step 2: Hit CDN page -> sets fireplayer_player session cookie
            try:
                s.get(
                    cdn_video_url,
                    headers={**BROWSER_HEADERS, "Referer": embed_url, "Sec-Fetch-Site": "cross-site"},
                    timeout=10,
                )
            except Exception:
                pass # Best-effort; proceed even if this step fails

            # Step 3: POST to getVideo API -> signed m3u8 URL
            api_url = f"https://as-cdn21.top/player/index.php?data={hash_}&do=getVideo"
            
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
                timeout=15,
            )
            api_resp.raise_for_status()
            data = api_resp.json()
            
            stream_url = data.get("videoSource") or data.get("securedLink") or ""
            
            # If everything worked, return the final result
            return {
                "success": True,
                "input_url": embed_url,
                "video_hash": hash_,
                "cdn_video_page": cdn_video_url,
                "hls": data.get("hls", False),
                "stream_url": stream_url,
                "thumbnail": data.get("videoImage", ""),
                "download_links": data.get("downloadLinks", []),
                "proxy_used": proxy_url,
            }

        except Exception as e:
            # Save error but silently continue the loop to try the next proxy
            last_error = f"Proxy {proxy_url} failed: {e}"
            continue

    # If the loop finishes without returning, all proxies have failed
    raise HTTPException(
        status_code=502, 
        detail=f"All configured proxies failed. Last known error: {last_error}"
    )

# ---- ROUTES ------------------------------------------------------------------
@app.get("/", response_class=JSONResponse)
def root():
    return {
        "status": "ok",
        "service": "AnimeDekho Stream Extractor API",
        "version": "2.1.0",
        "proxies_configured": len(PROXY_LIST),
        "usage": {
            "GET": "/extract?url=https://animedekho.app/embed/31910/1-26",
            "POST": {"endpoint": "/extract", "body": {"url": "https://animedekho.app/embed/31910/1-26"}},
        }
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