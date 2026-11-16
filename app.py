import os
import subprocess
import uuid
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

app = FastAPI()

def cleanup_files(*paths: str):
    for path in paths:
        if path and os.path.exists(path):
            os.remove(path)

def get_audio_duration(audio_path: str) -> float:
    """Measure exact MP3 duration using ffprobe so Manim matches the voiceover."""
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

@app.get("/make-video")
def make_video(
    background_tasks: BackgroundTasks,
    text: str,
    video_type: str = "short",  # "long" (16:9 widescreen) or "short" (9:16 vertical)
    x_pct: float = 0.45,        # Default horizontal text position (0.0 to 1.0)
    y_pct: float = 0.82         # Default vertical text position (0.0 to 1.0)
):
    job_id = str(uuid.uuid4())
    manim_output = f"{job_id}_raw.mp4"
    audio_file = f"{job_id}.mp3"
    final_output = f"{job_id}_final.mp4"

    # Match resolution and Manim's output folder name for Shorts (9:16) vs Long-form (16:9)
    if video_type == "short":
        resolution = ["-r", "480,854"]
        folder_name = "854p15"
    else:
        resolution = ["-r", "854,480"]
        folder_name = "480p15"

    rendered_path = os.path.join("media", "videos", "mixing_scene", folder_name, manim_output)

    try:
        # 1. Generate voiceover audio
        subprocess.run(
            ["edge-tts", "--text", text, "--write-media", audio_file],
            check=True
        )

        # 2. Get audio length so Manim animation matches the full narration
        audio_duration = get_audio_duration(audio_file)

        # 3. Pass all parameters safely to Manim
        env = os.environ.copy()
        env["SCENE_TEXT"] = text
        env["VIDEO_TYPE"] = video_type
        env["TEXT_X_PCT"] = str(x_pct)
        env["TEXT_Y_PCT"] = str(y_pct)
        env["AUDIO_DURATION"] = str(audio_duration)

        # 4. Render Manim scene
        subprocess.run(
            ["manim", "-ql", *resolution, "mixing_scene.py", "MixingGraph", "-o", manim_output],
            env=env,
            check=True
        )

        # 5. Merge video and audio
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", rendered_path,
                "-i", audio_file,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                final_output
            ],
            check=True
        )

        # Clean up intermediate files immediately; delete final video after transfer
        cleanup_files(audio_file, rendered_path)
        background_tasks.add_task(cleanup_files, final_output)

        return FileResponse(
            final_output,
            media_type="video/mp4",
            filename=f"{video_type}_{job_id[:8]}.mp4"
        )

    except subprocess.CalledProcessError as e:
        cleanup_files(audio_file, rendered_path, final_output)
        raise HTTPException(status_code=500, detail=f"Video generation failed: {e}")
