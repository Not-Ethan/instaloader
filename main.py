from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
import re
import logging
import os
import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from moviepy import VideoFileClip
from dotenv import load_dotenv
import requests

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

    apify_token = os.getenv("APIFY_TOKEN")
    if not apify_token:
        logger.error("APIFY_TOKEN not set")
        raise HTTPException(status_code=500, detail="Server configuration error: APIFY_TOKEN not set")

    try:
        # Call Apify API
        apify_url = f"https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items?token={apify_token}"
        
        payload = {
            "addParentData": False,
            "directUrls": [url_str],
            "enhanceUserSearchWithFacebookPage": False,
            "isUserReelFeedURL": False,
            "isUserTaggedFeedURL": False,
            "resultsLimit": 200,
            "resultsType": "details",
            "searchLimit": 1,
            "searchType": "hashtag"
        }

        logger.info("Calling Apify API...")
        response = requests.post(apify_url, json=payload)
        
        if response.status_code != 201:
            logger.error(f"Apify API error: {response.status_code} - {response.text}")
            raise HTTPException(status_code=502, detail=f"Apify API error: {response.text}")

        data = response.json()
        if not data or not isinstance(data, list) or len(data) == 0:
             logger.error("No data returned from Apify")
             raise HTTPException(status_code=404, detail="Post not found or private")

        post_data = data[0]
        video_url = post_data.get("videoUrl")
        title = post_data.get("caption", "")
        owner_username = post_data.get("ownerUsername") or post_data.get("owner", {}).get("username")

        if not video_url:
            logger.error("No video URL found in Apify response")
            raise HTTPException(status_code=404, detail="No video found in post")

        # Download the video
        target_path = DOWNLOADS_DIR / shortcode
        target_path.mkdir(exist_ok=True)
        
        video_filename = f"{shortcode}.mp4"
        video_path = target_path / video_filename
        
        logger.info(f"Downloading video from {video_url}...")
        video_response = requests.get(video_url, stream=True)
        video_response.raise_for_status()
        
        with open(video_path, "wb") as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                f.write(chunk)

        # Save caption
        with open(target_path / f"{shortcode}.txt", "w", encoding="utf-8") as f:
            f.write(title)

        # Update/Create expiry file (1 hour from now)
        expiry_time = datetime.now() + timedelta(hours=1)
        with open(target_path / "expiry_timestamp.txt", "w") as f:
            f.write(expiry_time.isoformat())
            
        # Transcode video to ensure specific format
        input_path = video_path
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

            
            # Replace original file with processed file
            input_path.unlink()
            temp_output_path.rename(input_path)
            logger.info("Transcoding complete.")
            
        except Exception as e:
            logger.error(f"Error during transcoding: {e}")
            raise HTTPException(status_code=500, detail=f"Transcoding failed: {str(e)}")

        # Construct the full URL to the video file
        # request.base_url ends with a slash, e.g., http://localhost:8000/
        served_video_url = f"{request.base_url}downloads/{shortcode}/{video_filename}"
        
        logger.info(f"Successfully processed {shortcode}")
        return {
            "data": {
                "play": served_video_url,
                "title": title,
                "authorUsername": owner_username
            }
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {str(e)}")
        raise HTTPException(status_code=502, detail=f"External API error: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
