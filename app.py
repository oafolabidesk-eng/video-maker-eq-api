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

@app.get("/make-video")
def make_video(background_tasks: BackgroundTasks, text: str, video_type: str = "short"):
    job_id = str(uuid.uuid4())
    manim_output = f"{job_id}_raw.mp4"
    audio_file = f"{job_id}.mp3"
    final_output = f"{job_id}_final.mp4"
    rendered_path = os.path.join("media", "videos", "mixing_scene", "480p15", manim_output)

    env = os.environ.copy()
    env["SCENE_TEXT"] = text
    env["VIDEO_TYPE"] = video_type

    try:
        # 1. Generate audio
        subprocess.run(["edge-tts", "--text", text, "--write-media", audio_file], check=True)

        # 2. Run Manim
        subprocess.run(["manim", "-ql", "mixing_scene.py", "MixingGraph", "-o", manim_output], env=env, check=True)

        # 3. Merge video and audio
        subprocess.run(
            ["ffmpeg", "-y", "-i", rendered_path, "-i", audio_file, "-c:v", "copy", "-c:a", "aac", "-shortest", final_output],
            check=True
        )

        # Remove intermediate files right away
        cleanup_files(audio_file, rendered_path)

        # Schedule final_output deletion only after FileResponse finishes sending
        background_tasks.add_task(cleanup_files, final_output)

        return FileResponse(final_output, media_type="video/mp4", filename="video.mp4")

    except subprocess.CalledProcessError as e:
        # Clean up any partial files if a command fails
        cleanup_files(audio_file, rendered_path, final_output)
        raise HTTPException(status_code=500, detail=f"Video generation failed: {e}")
