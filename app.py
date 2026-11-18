import glob
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager, closing
from typing import List, Dict, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Persistent job store (SQLite, shared safely across workers on one host)
# ---------------------------------------------------------------------------

JOBS_DB = os.environ.get("JOBS_DB", "jobs.db")
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_HOURS", "24")) * 3600
HEARTBEAT_INTERVAL = 10      # seconds between heartbeats from a live process
STALE_AFTER = 45             # "processing" job with no heartbeat this long = dead
MAINTENANCE_INTERVAL = 600   # seconds between expiry / orphan sweeps
INSTANCE_ID = uuid.uuid4().hex  # unique per running process

_OUTPUT_FILE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(\.mp4|\.zip|_.+\.mp4)$"
)
_TEMP_FILE_RE = re.compile(r"^(temp_[0-9a-f]{32}.*|concat_[0-9a-f]{32}\.txt)$")


class JobStore:
    """Durable job tracker. Survives restarts and is visible to all workers."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._conn()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id     TEXT PRIMARY KEY,
                    type       TEXT NOT NULL,
                    status     TEXT NOT NULL,
                    file       TEXT,
                    error      TEXT,
                    owner      TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def create(self, job_id: str, job_type: str) -> None:
        now = time.time()
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT INTO jobs (job_id, type, status, owner, created_at, updated_at) "
                "VALUES (?, ?, 'processing', ?, ?, ?)",
                (job_id, job_type, INSTANCE_ID, now, now),
            )

    def update(self, job_id: str, **fields) -> None:
        allowed = {"status", "file", "error"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
        with closing(self._conn()) as conn, conn:
            conn.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id = ?",
                (*fields.values(), time.time(), job_id),
            )

    def get(self, job_id: str) -> Optional[dict]:
        self.fail_stale()
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT job_id, type, status, file, error, created_at, updated_at "
                "FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def claim_for_download(self, job_id: str) -> bool:
        """Atomically delete a finished job; only one caller can win."""
        with closing(self._conn()) as conn, conn:
            cur = conn.execute(
                "DELETE FROM jobs WHERE job_id = ? AND status = 'completed'",
                (job_id,),
            )
            return cur.rowcount == 1

    def heartbeat(self) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "UPDATE jobs SET updated_at = ? WHERE owner = ? AND status = 'processing'",
                (time.time(), INSTANCE_ID),
            )

    def fail_stale(self) -> int:
        """Mark 'processing' jobs whose worker stopped heartbeating as failed."""
        cutoff = time.time() - STALE_AFTER
        with closing(self._conn()) as conn, conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'failed', "
                "error = 'Job interrupted (server restarted or worker died). Please resubmit.', "
                "updated_at = ? WHERE status = 'processing' AND updated_at < ?",
                (time.time(), cutoff),
            )
            return cur.rowcount

    def purge_expired(self) -> List[str]:
        """Delete finished/failed jobs older than the TTL; return their output files."""
        cutoff = time.time() - JOB_TTL_SECONDS
        with closing(self._conn()) as conn, conn:
            rows = conn.execute(
                "SELECT job_id, file FROM jobs "
                "WHERE status IN ('completed', 'failed') AND updated_at < ?",
                (cutoff,),
            ).fetchall()
            conn.executemany(
                "DELETE FROM jobs WHERE job_id = ?", [(r["job_id"],) for r in rows]
            )
        return [r["file"] for r in rows if r["file"]]


def sweep_orphan_files() -> None:
    """Remove stale outputs and temp files left behind by crashes or restarts."""
    now = time.time()
    try:
        names = os.listdir(".")
    except OSError:
        return
    for name in names:
        is_output = bool(_OUTPUT_FILE_RE.match(name))
        is_temp = bool(_TEMP_FILE_RE.match(name))
        if not (is_output or is_temp):
            continue
        max_age = JOB_TTL_SECONDS if is_output else 3600
        try:
            if os.path.isfile(name) and now - os.path.getmtime(name) > max_age:
                os.remove(name)
        except OSError:
            pass


def run_maintenance(store: "JobStore") -> None:
    store.fail_stale()
    for path in store.purge_expired():
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    sweep_orphan_files()


def _background_loop(store: "JobStore", stop: threading.Event) -> None:
    last_maintenance = 0.0
    while not stop.wait(HEARTBEAT_INTERVAL):
        try:
            store.heartbeat()
            if time.time() - last_maintenance > MAINTENANCE_INTERVAL:
                run_maintenance(store)
                last_maintenance = time.time()
        except Exception:
            pass  # never let the housekeeping thread die


JOBS = JobStore(JOBS_DB)


@asynccontextmanager
async def lifespan(_: FastAPI):
    run_maintenance(JOBS)
    stop = threading.Event()
    thread = threading.Thread(
        target=_background_loop, args=(JOBS, stop), daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()


app = FastAPI(
    title="Multi-Plugin Vocal Chain & Weekly YouTube Generator API",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SingleVideoRequest(BaseModel):
    text: str
    visual_mode: str = "auto"
    video_type: str = "short"  # "short" = 9:16, "long" = 16:9
    voice: str = "en-US-ChristopherNeural"
    x_pct: float = 0.45
    y_pct: float = 0.87
    curve_power: float = 2.0
    curve_gain: float = 0.85
    plugin_title: Optional[str] = None
    knob_labels: Optional[List[str]] = None
    x_labels: Optional[List[str]] = None
    y_labels: Optional[List[str]] = None


class VideoChapter(BaseModel):
    long_script: str  # flows naturally inside the multi-minute 16:9 video
    short_script: Optional[str] = None  # punchy standalone script for the daily 9:16 Short
    visual_mode: str = "auto"
    plugin_title: Optional[str] = None
    knob_labels: Optional[List[str]] = None
    x_labels: Optional[List[str]] = None
    y_labels: Optional[List[str]] = None


class WeeklyChainBatchRequest(BaseModel):
    chapters: List[VideoChapter] = Field(..., min_length=1, max_length=12)
    voice: str = "en-US-ChristopherNeural"
    long_x_pct: float = 0.50
    long_y_pct: float = 0.89
    short_x_pct: float = 0.45
    short_y_pct: float = 0.87
    curve_power: float = 2.0
    curve_gain: float = 0.85


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def run_cmd(cmd: List[str], env: Optional[dict] = None) -> str:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, env=env
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n{stderr}"
        )


VISUAL_KEYWORDS = [
    ("flanger", ["flanger", "flanging", "comb", "jet"]),
    ("chorus", ["chorus", "doubler", "detune", "unison"]),
    ("phaser", ["phaser", "phase", "notch sweep"]),
    ("deesser", ["de-esser", "deesser", "de esser", "sibilance", "harsh s"]),
    ("imager", ["stereo", "width", "imager", "mid side", "mid-side", "panning"]),
    ("reverb", ["reverb", "room", "hall", "plate", "decay", "tail"]),
    ("delay", ["delay", "echo", "tap", "feedback", "ping pong"]),
    ("limiter", ["limiter", "limiting", "brickwall", "ceiling", "lufs"]),
    (
        "compressor",
        ["compressor", "compression", "sidechain", "threshold", "ratio", "attack", "release"],
    ),
    (
        "saturation",
        ["saturation", "saturate", "harmonic", "warmth", "tube", "tape", "drive"],
    ),
    ("eq", ["eq", "equalizer", "frequency", "hz", "khz", "low cut", "high pass"]),
]


def resolve_visual_mode(requested_mode: str, script_text: str) -> str:
    if requested_mode != "auto":
        return requested_mode.lower()
    text = script_text.lower()
    for mode, keywords in VISUAL_KEYWORDS:
        if any(k in text for k in keywords):
            return mode
    return "universal"


def get_audio_duration(audio_path: str) -> float:
    stdout = run_cmd([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ])
    return max(3.5, float(stdout.strip()))


def find_manim_output(output_name: str) -> str:
    matches = glob.glob(
        os.path.join("media", "videos", "mixing_scene", "*", output_name)
    )
    if not matches:
        raise FileNotFoundError(f"Manim output not found: {output_name}")
    return matches[0]


def cleanup_files(*paths: Optional[str]) -> None:
    for path in paths:
        if not path:
            continue
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Rendering & concatenation
# ---------------------------------------------------------------------------

def render_single_video(
    text: str,
    visual_mode: str,
    video_type: str,
    voice: str,
    x_pct: float,
    y_pct: float,
    curve_power: float,
    curve_gain: float,
    output_path: str,
    plugin_title: Optional[str] = None,
    knob_labels: Optional[List[str]] = None,
    x_labels: Optional[List[str]] = None,
    y_labels: Optional[List[str]] = None,
) -> str:
    if not os.path.exists("mixing_scene.py"):
        raise FileNotFoundError("mixing_scene.py not found in the working directory")

    uid = uuid.uuid4().hex
    audio_path = f"temp_{uid}.mp3"
    vtt_path = f"temp_{uid}.vtt"
    manim_name = f"temp_{uid}_silent.mp4"
    manim_path: Optional[str] = None

    try:
        run_cmd([
            "edge-tts",
            "--voice", voice,
            f"--text={text}",
            "--write-media", audio_path,
            "--write-subtitles", vtt_path,
        ])

        duration = get_audio_duration(audio_path)
        resolved_mode = resolve_visual_mode(visual_mode, text)

        env = os.environ.copy()
        env["SCENE_TEXT"] = text
        env["VTT_PATH"] = os.path.abspath(vtt_path)
        env["VISUAL_MODE"] = resolved_mode
        env["VIDEO_TYPE"] = video_type
        env["TEXT_X_PCT"] = str(x_pct)
        env["TEXT_Y_PCT"] = str(y_pct)
        env["AUDIO_DURATION"] = str(duration)
        env["CURVE_POWER"] = str(curve_power)
        env["CURVE_GAIN"] = str(curve_gain)
        if plugin_title:
            env["CUSTOM_TITLE"] = plugin_title
        if knob_labels and len(knob_labels) == 3:
            env["CUSTOM_KNOBS"] = "|".join(knob_labels)
        if x_labels and len(x_labels) == 6:
            env["CUSTOM_X_LABELS"] = "|".join(x_labels)
        if y_labels and len(y_labels) == 5:
            env["CUSTOM_Y_LABELS"] = "|".join(y_labels)

        resolution = "480,854" if video_type == "short" else "854,480"

        run_cmd([
            "manim", "-ql", "--disable_caching",
            "-r", resolution,
            "-o", manim_name,
            "mixing_scene.py", "MixingGraph",
        ], env=env)

        manim_path = find_manim_output(manim_name)

        run_cmd([
            "ffmpeg", "-y",
            "-i", manim_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            output_path,
        ])

        return output_path
    finally:
        cleanup_files(audio_path, vtt_path, manim_path)


def concat_videos(video_paths: List[str], merged_output: str) -> None:
    list_file = f"concat_{uuid.uuid4().hex}.txt"
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for p in video_paths:
                escaped = os.path.abspath(p).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")
        run_cmd([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            merged_output,
        ])
    finally:
        cleanup_files(list_file)


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

def process_single_job(job_id: str, req: SingleVideoRequest) -> None:
    output_path = f"{job_id}.mp4"
    try:
        render_single_video(
            text=req.text,
            visual_mode=req.visual_mode,
            video_type=req.video_type,
            voice=req.voice,
            x_pct=req.x_pct,
            y_pct=req.y_pct,
            curve_power=req.curve_power,
            curve_gain=req.curve_gain,
            output_path=output_path,
            plugin_title=req.plugin_title,
            knob_labels=req.knob_labels,
            x_labels=req.x_labels,
            y_labels=req.y_labels,
        )
        JOBS.update(job_id, status="completed", file=output_path)
    except Exception as e:
        cleanup_files(output_path)
        JOBS.update(job_id, status="failed", error=str(e))


def process_batch_job(job_id: str, req: WeeklyChainBatchRequest) -> None:
    long_paths: List[str] = []
    short_paths: List[str] = []
    merged_path = "00_long_form_full.mp4"
    zip_path = f"{job_id}.zip"
    # Unique merged filename on disk to avoid collisions between concurrent jobs
    merged_disk_path = f"{job_id}_{merged_path}"

    try:
        for idx, chapter in enumerate(req.chapters, start=1):
            long_out = f"{job_id}_long_chapter_{idx:02d}.mp4"
            render_single_video(
                text=chapter.long_script,
                visual_mode=chapter.visual_mode,
                video_type="long",
                voice=req.voice,
                x_pct=req.long_x_pct,
                y_pct=req.long_y_pct,
                curve_power=req.curve_power,
                curve_gain=req.curve_gain,
                output_path=long_out,
                plugin_title=chapter.plugin_title,
                knob_labels=chapter.knob_labels,
                x_labels=chapter.x_labels,
                y_labels=chapter.y_labels,
            )
            long_paths.append(long_out)

            if idx <= 7:
                short_out = f"{job_id}_{idx:02d}_short_day{idx}.mp4"
                render_single_video(
                    text=chapter.short_script or chapter.long_script,
                    visual_mode=chapter.visual_mode,
                    video_type="short",
                    voice=req.voice,
                    x_pct=req.short_x_pct,
                    y_pct=req.short_y_pct,
                    curve_power=req.curve_power,
                    curve_gain=req.curve_gain,
                    output_path=short_out,
                    plugin_title=chapter.plugin_title,
                    knob_labels=chapter.knob_labels,
                    x_labels=chapter.x_labels,
                    y_labels=chapter.y_labels,
                )
                short_paths.append(short_out)

        concat_videos(long_paths, merged_disk_path)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(merged_disk_path, arcname=merged_path)
            for i, p in enumerate(long_paths, start=1):
                zf.write(p, arcname=f"long_chapter_{i:02d}.mp4")
            for i, p in enumerate(short_paths, start=1):
                zf.write(p, arcname=f"{i:02d}_short_day{i}.mp4")

        JOBS.update(job_id, status="completed", file=zip_path)
    except Exception as e:
        cleanup_files(zip_path)
        JOBS.update(job_id, status="failed", error=str(e))
    finally:
        cleanup_files(merged_disk_path, *long_paths, *short_paths)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "has_mixing_scene": os.path.exists("mixing_scene.py")}


@app.get("/voices")
def list_voices():
    try:
        output = run_cmd(["edge-tts", "--list-voices"])
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    male: List[str] = []
    female: List[str] = []
    for line in output.splitlines():
        if "en-" not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        voice_name = parts[0]
        gender = parts[1].lower()
        if gender == "male":
            male.append(voice_name)
        elif gender == "female":
            female.append(voice_name)

    return {
        "total_english_voices": len(male) + len(female),
        "male": male,
        "female": female,
    }


@app.post("/jobs/single")
def create_single_job(req: SingleVideoRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    JOBS.create(job_id, "single")
    background_tasks.add_task(process_single_job, job_id, req)
    return {"job_id": job_id, "status": "processing"}


@app.post("/jobs/weekly-batch")
def create_weekly_batch_job(
    req: WeeklyChainBatchRequest, background_tasks: BackgroundTasks
):
    job_id = str(uuid.uuid4())
    JOBS.create(job_id, "weekly-batch")
    background_tasks.add_task(process_batch_job, job_id, req)
    return {"job_id": job_id, "status": "processing"}


@app.get("/jobs/{job_id}/status")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs/{job_id}/download")
def download_job(job_id: str, background_tasks: BackgroundTasks):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] == "processing":
        raise HTTPException(status_code=409, detail="Job is still processing")
    if job["status"] == "failed":
        raise HTTPException(status_code=500, detail=job.get("error") or "Job failed")

    file_path = job.get("file")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Output file not found")

    # Atomic claim so concurrent downloads can't both remove the file.
    if not JOBS.claim_for_download(job_id):
        raise HTTPException(status_code=404, detail="Job not found")

    media_type = "application/zip" if file_path.endswith(".zip") else "video/mp4"
    background_tasks.add_task(cleanup_files, file_path)
    return FileResponse(
        file_path, media_type=media_type, filename=os.path.basename(file_path)
    )
