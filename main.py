from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
import instaloader
import re
import logging
import os
import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from moviepy import VideoFileClip
from dotenv import load_dotenv
from proxy_manager import ProxyManager
import time
import random

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Instaloader Service")

# Ensure downloads directory exists
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Mount the downloads directory to serve files statically
app.mount("/downloads", StaticFiles(directory=DOWNLOADS_DIR), name="downloads")

class InstaRequest(BaseModel):
    url: HttpUrl

proxy_manager = ProxyManager()

async def cleanup_loop():
    """Background task to delete expired directories."""
    while True:
        logger.info("Running cleanup check...")
        try:
            now = datetime.now()
            # Iterate over directories in downloads
            for item in DOWNLOADS_DIR.iterdir():
                if item.is_dir():
                    expiry_file = item / "expiry_timestamp.txt"
                    if expiry_file.exists():
                        try:
                            with open(expiry_file, "r") as f:
                                content = f.read().strip()
                                expiry_time = datetime.fromisoformat(content)
                            
                            if now > expiry_time:
                                logger.info(f"Deleting expired directory: {item}")
                                shutil.rmtree(item)
                        except Exception as e:
                            logger.error(f"Error processing {item}: {e}")
        except Exception as e:
            logger.error(f"Error in cleanup loop: {e}")
        
        # Run every hour
        await asyncio.sleep(3600)

@app.on_event("startup")
async def startup_event():
    proxy_manager.fetch_proxies()
    asyncio.create_task(cleanup_loop())

@app.post("/insta")
def download_insta(request: Request, body: InstaRequest):
    url_str = str(body.url)
    logger.info(f"Received download request for URL: {url_str}")

    # Extract shortcode from URL
    # Matches /reel/SHORTCODE or /p/SHORTCODE
    match = re.search(r'instagram\.com/(?:reel|p)/([^/?#&]+)', url_str)
    if not match:
        logger.warning(f"Could not parse shortcode from URL: {url_str}")
        raise HTTPException(status_code=400, detail="Could not parse shortcode from URL. Ensure it is a valid Instagram post or reel URL.")
    
    shortcode = match.group(1)
    logger.info(f"Extracted shortcode: {shortcode}")

    max_retries = 20
    last_exception = None

    for attempt in range(max_retries):
        try:
            # Get proxy and user agent
            proxy = proxy_manager.get_proxy()
            user_agent = proxy_manager.get_user_agent()
            
            logger.info(f"Attempt {attempt + 1}/{max_retries} using proxy: {proxy.split('@')[1] if proxy else 'None'}")

            # Initialize Instaloader with custom dirname_pattern to save in downloads/ folder
            # dirname_pattern="{target}" is default, we change it to "downloads/{target}"
            # However, since we are running from the root, we can just use the absolute path or relative path in the pattern.
            # Instaloader replaces {target} with the target name passed to download_post.
            L = instaloader.Instaloader(
                dirname_pattern=str(DOWNLOADS_DIR / "{target}"),
                user_agent=user_agent,
                resume_prefix=None, # Disable resume files to prevent state leakage
                max_connection_attempts=1 # Fail fast to rotate proxy
            )

            # Configure Proxy if available
            if proxy:
                L.context._session.proxies = {"https": proxy, "http": proxy}
            
            # Get Post object from shortcode
            logger.info(f"Fetching metadata for shortcode: {shortcode}")
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            
            # Download the post
            # The target argument specifies the directory name for the download (inside downloads/ due to dirname_pattern)
            logger.info(f"Downloading post {shortcode}...")
            L.download_post(post, target=shortcode)
            
            # If successful, break the retry loop
            break

        except instaloader.exceptions.ConnectionException as e:
            error_msg = str(e)
            logger.error(f"Instaloader connection error on attempt {attempt + 1}: {error_msg}")
            last_exception = e
            # If it's the last attempt, we'll raise the error later
            if attempt < max_retries - 1:
                # Add a small random delay between retries to avoid hammering
                time.sleep(random.uniform(1, 3))
                continue
        except instaloader.exceptions.InstaloaderException as e:
            # Other instaloader exceptions (like 404) might not be recoverable by switching proxy
            logger.error(f"Instaloader error: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Instaloader error: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
    
    # If we exhausted retries and still have an exception
    if last_exception:
        error_msg = str(last_exception)
        if "401" in error_msg or "429" in error_msg or "403" in error_msg:
             raise HTTPException(status_code=429, detail="Rate limited by Instagram. Please try again later.")
        raise HTTPException(status_code=500, detail=f"Connection error after {max_retries} attempts: {error_msg}")

    try:
        target_path = DOWNLOADS_DIR / shortcode
        
        # Update/Create expiry file (1 hour from now)
        expiry_time = datetime.now() + timedelta(hours=1)
        with open(target_path / "expiry_timestamp.txt", "w") as f:
            f.write(expiry_time.isoformat())
            
        # Find video file
        video_files = list(target_path.glob("*.mp4"))
        if not video_files:
             logger.error(f"Video file not found in {target_path}")
             raise HTTPException(status_code=500, detail="Video file not found after download.")
        
        # Transcode video to ensure specific format
        input_path = video_files[0]
        temp_output_path = input_path.with_name(f"processed_{input_path.name}")
        
        logger.info(f"Transcoding video {input_path} to {temp_output_path}...")
        
        try:
            clip = VideoFileClip(str(input_path))
            fps = clip.fps if clip.fps else 30
            
            clip.write_videofile(
                str(temp_output_path),
                fps=fps,
                codec="libx264",
                audio_codec="aac",
                preset="ultrafast",
                audio_bitrate="128k",
                threads=0,
                bitrate="4000k",
                ffmpeg_params=[
                    "-pix_fmt", "yuv420p",
                    "-profile:v", "main",
                    "-level:v", "4.0",
                    "-movflags", "+faststart",
                    "-g", str(int(fps * 2)),
                    "-vf", f"fps={fps}",
                    "-maxrate", "4000k",
                    "-bufsize", "8000k"
                ],
                temp_audiofile=str(target_path / "_temp-audio.m4a"),
                remove_temp=True,
                logger=None
            )
            clip.close()
            
            # Replace original file with processed file
            input_path.unlink()
            temp_output_path.rename(input_path)
            logger.info("Transcoding complete.")
            
        except Exception as e:
            logger.error(f"Error during transcoding: {e}")
            raise HTTPException(status_code=500, detail=f"Transcoding failed: {str(e)}")

        video_filename = video_files[0].name
        # Construct the full URL to the video file
        # request.base_url ends with a slash, e.g., http://localhost:8000/
        video_url = f"{request.base_url}downloads/{shortcode}/{video_filename}"
        
        # Find title (caption) from .txt file
        # Exclude expiry_timestamp.txt
        title = ""
        txt_files = [f for f in target_path.glob("*.txt") if f.name != "expiry_timestamp.txt"]
        if txt_files:
            # Usually there's one, or maybe none if no caption
            try:
                with open(txt_files[0], "r", encoding="utf-8") as f:
                    title = f.read()
            except Exception as e:
                logger.warning(f"Could not read caption file: {e}")
        
        logger.info(f"Successfully processed {shortcode}")
        return {
            "data": {
                "play": video_url,
                "title": title
            }
        }

    except instaloader.exceptions.InstaloaderException as e:
        logger.error(f"Instaloader error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Instaloader error: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

