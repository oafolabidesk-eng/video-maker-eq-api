import os
import subprocess
import uuid
import zipfile
from typing import List, Optional, Dict
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI()

# In-memory job tracker for n8n polling
JOBS: Dict[str, dict] = {}

class SingleVideoRequest(BaseModel):
    text: str
    video_type: str = "short"   # "short" or "long"
    x_pct: float = 0.45
    y_pct: float = 0.82
    curve_power: float = 2.0    # AI Agent can vary curve steepness
    curve_gain: float = 0.8     # AI Agent can vary curve height

class WeeklyBatchRequest(BaseModel):
    long_script: str
    short_scripts: List[str] = Field(..., min_length=1, max_length=7)
    long_x_pct: float = 0.50
    long_y_pct: float = 0.88
    short_x_pct: float = 0.45
    short_y_pct: float = 0.82
    curve_power: float = 2.0
    curve_gain: float = 0.8

def cleanup_files(*paths: str):
    for path in paths:
        if path and os.path.exists(path):
            os.remove(path)

def get_audio_duration(audio_path: str) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            audio_path
        ],
        capture_output=True,
        text=True,
        check=True
    )
    return max(3.5, float(result.stdout.strip()))

def render_single_video(
    text: str,
    video_type: str,
    x_pct: float,
    y_pct: float,
    curve_power: float,
    curve_gain: float,
    output_filename: str
):
    job_id = str(uuid.uuid4())
    manim_output = f"{job_id}_raw.mp4"
    audio_file = f"{job_id}.mp3"

    if video_type == "short":
        resolution = ["-r", "480,854"]
        folder_name = "854p15"
    else:
        resolution = ["-r", "854,480"]
        folder_name = "480p15"

    rendered_path = os.path.join("media", "videos", "mixing_scene", folder_name, manim_output)

    try:
        subprocess.run(["edge-tts", "--text", text, "--write-media", audio_file], check=True)
        audio_duration = get_audio_duration(audio_file)

        env = os.environ.copy()
        env["SCENE_TEXT"] = text
        env["VIDEO_TYPE"] = video_type
        env["TEXT_X_PCT"] = str(x_pct)
        env["TEXT_Y_PCT"] = str(y_pct)
        env["AUDIO_DURATION"] = str(audio_duration)
        env["CURVE_POWER"] = str(curve_power)
        env["CURVE_GAIN"] = str(curve_gain)

        subprocess.run(
            ["manim", "-ql", *resolution, "mixing_scene.py", "MixingGraph", "-o", manim_output],
            env=env,
            check=True
        )

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", rendered_path,
                "-i", audio_file,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                output_filename
            ],
            check=True
        )
    finally:
        cleanup_files(audio_file, rendered_path)

def process_single_job(job_id: str, req: SingleVideoRequest):
    output_file = f"{job_id}.mp4"
    try:
        render_single_video(
            req.text, req.video_type, req.x_pct, req.y_pct,
            req.curve_power, req.curve_gain, output_file
        )
        JOBS[job_id] = {"status": "completed", "file": output_file, "media_type": "video/mp4"}
    except Exception as e:
        cleanup_files(output_file)
        JOBS[job_id] = {"status": "failed", "error": str(e)}

def process_batch_job(job_id: str, req: WeeklyBatchRequest):
    zip_filename = f"{job_id}.zip"
    generated_videos = []
    try:
        long_file = f"{job_id}_00_long.mp4"
        render_single_video(
            req.long_script, "long", req.long_x_pct, req.long_y_pct,
            req.curve_power, req.curve_gain, long_file
        )
        generated_videos.append((long_file, "00_long_form.mp4"))

        for idx, short_text in enumerate(req.short_scripts, start=1):
            short_file = f"{job_id}_0{idx}_short.mp4"
            render_single_video(
                short_text, "short", req.short_x_pct, req.short_y_pct,
                req.curve_power, req.curve_gain, short_file
            )
            generated_videos.append((short_file, f"0{idx}_short_day{idx}.mp4"))

        with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for disk_path, archive_name in generated_videos:
                zipf.write(disk_path, arcname=archive_name)

        cleanup_files(*(path for path, _ in generated_videos))
        JOBS[job_id] = {"status": "completed", "file": zip_filename, "media_type": "application/zip"}
    except Exception as e:
        cleanup_files(*(path for path, _ in generated_videos), zip_filename)
        JOBS[job_id] = {"status": "failed", "error": str(e)}

# --- n8n Endpoints ---

@app.post("/jobs/single")
def create_single_job(req: SingleVideoRequest, background_tasks: BackgroundTasks):
    """n8n calls this with AI Agent output; returns job_id immediately."""
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "processing"}
    background_tasks.add_task(process_single_job, job_id, req)
    return {"job_id": job_id, "status": "processing"}

@app.post("/jobs/weekly-batch")
def create_batch_job(req: WeeklyBatchRequest, background_tasks: BackgroundTasks):
    """n8n calls this once a week with 1 long + 7 short scripts; returns job_id immediately."""
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "processing"}
    background_tasks.add_task(process_batch_job, job_id, req)
    return {"job_id": job_id, "status": "processing"}

@app.get("/jobs/{job_id}/status")
def check_job_status(job_id: str):
    """n8n polls this every 10 seconds until status == 'completed'."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, **job}

@app.get("/jobs/{job_id}/download")
def download_job_file(job_id: str, background_tasks: BackgroundTasks):
    """n8n downloads the finished MP4 or ZIP binary file and deletes it from Render."""
    job = JOBS.get(job_id)
    if not job or job.get("status") != "completed":
        raise HTTPException(status_code=400, detail="File not ready or job failed")

    file_path = job["file"]
    ext = "zip" if job["media_type"] == "application/zip" else "mp4"
    background_tasks.add_task(cleanup_files, file_path)
    JOBS.pop(job_id, None)

    return FileResponse(file_path, media_type=job["media_type"], filename=f"render_{job_id[:8]}.{ext}")
