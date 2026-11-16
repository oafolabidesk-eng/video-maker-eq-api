import os
import subprocess
import uuid
import zipfile
from typing import List
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI()

class WeeklyBatchRequest(BaseModel):
    long_script: str
    short_scripts: List[str] = Field(..., min_length=1, max_length=7)
    long_x_pct: float = 0.50
    long_y_pct: float = 0.88
    short_x_pct: float = 0.45
    short_y_pct: float = 0.82

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

def render_single_video(text: str, video_type: str, x_pct: float, y_pct: float, output_filename: str):
    """Helper function that generates TTS audio, renders Manim, and merges them."""
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

@app.get("/make-video")
def make_video(
    background_tasks: BackgroundTasks,
    text: str,
    video_type: str = "short",
    x_pct: float = 0.45,
    y_pct: float = 0.82
):
    final_output = f"{uuid.uuid4()}_final.mp4"
    try:
        render_single_video(text, video_type, x_pct, y_pct, final_output)
        background_tasks.add_task(cleanup_files, final_output)
        return FileResponse(final_output, media_type="video/mp4", filename=f"{video_type}_video.mp4")
    except subprocess.CalledProcessError as e:
        cleanup_files(final_output)
        raise HTTPException(status_code=500, detail=f"Video generation failed: {e}")

@app.post("/make-weekly-batch")
def make_weekly_batch(payload: WeeklyBatchRequest, background_tasks: BackgroundTasks):
    batch_id = str(uuid.uuid4())[:8]
    zip_filename = f"weekly_videos_{batch_id}.zip"
    generated_videos = []

    try:
        # 1. Render the main 16:9 long-form video
        long_file = f"{batch_id}_00_long_form.mp4"
        render_single_video(
            text=payload.long_script,
            video_type="long",
            x_pct=payload.long_x_pct,
            y_pct=payload.long_y_pct,
            output_filename=long_file
        )
        generated_videos.append((long_file, "00_long_form.mp4"))

        # 2. Render up to 7 vertical 9:16 Shorts
        for idx, short_text in enumerate(payload.short_scripts, start=1):
            short_file = f"{batch_id}_0{idx}_short_day{idx}.mp4"
            render_single_video(
                text=short_text,
                video_type="short",
                x_pct=payload.short_x_pct,
                y_pct=payload.short_y_pct,
                output_filename=short_file
            )
            generated_videos.append((short_file, f"0{idx}_short_day{idx}.mp4"))

        # 3. Bundle all 8 videos into a single ZIP archive
        with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for disk_path, archive_name in generated_videos:
                zipf.write(disk_path, arcname=archive_name)

        # Clean up individual MP4s immediately; remove ZIP after download completes
        cleanup_files(*(path for path, _ in generated_videos))
        background_tasks.add_task(cleanup_files, zip_filename)

        return FileResponse(
            zip_filename,
            media_type="application/zip",
            filename="weekly_youtube_batch.zip"
        )

    except subprocess.CalledProcessError as e:
        cleanup_files(*(path for path, _ in generated_videos), zip_filename)
        raise HTTPException(status_code=500, detail=f"Batch video generation failed: {e}")
