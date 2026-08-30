"""
ShortAI — FastAPI Server (Optimized)
--------------------------------------
Endpoints:
  POST /generate          → accepts YouTube URL, runs engine, returns clip metadata
  GET  /clips/{filename}  → serves the actual video file
  GET  /status/{job_id}   → check job processing status
  GET  /health            → health check

No changes needed here — all speed improvements are inside engine.py.
This file is included for completeness so you can drop both files together.
"""

import os
import uuid
import json
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine import generate_shorts

# ── APP SETUP ───────────────────────────────────────────────────────────────
app = FastAPI(title="ShortAI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)
app.mount("/clips", StaticFiles(directory="outputs"), name="clips")

FRONTEND_DIR = Path("../frontend")
if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

jobs: dict = {}


# ── MODELS ───────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    url: str
    platform: Optional[str] = "youtube_shorts"
    num_clips: Optional[int] = 3        # was 5 — matches new engine default
    clip_duration: Optional[int] = 30   # was 45 — matches new engine default


class JobStatus(BaseModel):
    job_id: str
    status: str
    step: Optional[str]
    progress: int
    clips: Optional[list]
    error: Optional[str]


# ── BACKGROUND TASK ─────────────────────────────────────────────────────────

def run_engine(job_id: str, url: str, num_clips: int, clip_duration: int):
    try:
        jobs[job_id]["status"]   = "processing"
        jobs[job_id]["step"]     = "Starting pipeline..."
        jobs[job_id]["progress"] = 5

        result = generate_shorts(
            url=url,
            num_clips=num_clips,
            clip_duration=clip_duration,
            progress_callback=lambda step, pct: update_job(job_id, step, pct),
        )

        clips_out = []
        for i, (path, meta) in enumerate(zip(result["output_paths"], result["clips_meta"])):
            filename = Path(path).name
            clips_out.append({
                "clip_index":  i + 1,
                "filename":    filename,
                "url":         f"/clips/{filename}",
                "start":       meta["start"],
                "end":         meta["end"],
                "duration":    meta["duration"],
                "score":       meta["score"],
                "anchor_text": meta["anchor_text"],
                "type":        get_clip_type(i),
                "type_label":  get_clip_type_label(i),
            })

        jobs[job_id].update({
            "status":          "done",
            "step":            "Complete!",
            "progress":        100,
            "clips":           clips_out,
            "title":           result["title"],
            "duration":        result["duration"],
            "elapsed_seconds": result.get("elapsed_seconds"),
            "lang_breakdown":  result.get("lang_breakdown", {}),
        })

    except Exception as e:
        jobs[job_id].update({
            "status":   "error",
            "step":     "Failed",
            "progress": 0,
            "error":    str(e),
        })
        print(f"[ERROR] Job {job_id} failed: {e}")


def update_job(job_id: str, step: str, progress: int):
    if job_id in jobs:
        jobs[job_id]["step"]     = step
        jobs[job_id]["progress"] = progress


def get_clip_type(index: int) -> str:
    return ["hook", "emotion", "edu", "trend", "viral"][index % 5]


def get_clip_type_label(index: int) -> str:
    return ["Hook", "Emotion", "Educational", "Trending", "Viral"][index % 5]


# ── ROUTES ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "message": "ShortAI API is running"}


@app.post("/generate")
async def generate(req: GenerateRequest, background_tasks: BackgroundTasks):
    if "youtube.com" not in req.url and "youtu.be" not in req.url:
        raise HTTPException(status_code=400, detail="Please provide a valid YouTube URL")

    job_id = str(uuid.uuid4())[:8]

    jobs[job_id] = {
        "job_id":   job_id,
        "status":   "queued",
        "step":     "Queued...",
        "progress": 0,
        "clips":    None,
        "error":    None,
        "url":      req.url,
    }

    background_tasks.add_task(run_engine, job_id, req.url, req.num_clips, req.clip_duration)
    return {"job_id": job_id, "status": "queued"}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


@app.get("/clips/{filename}")
def serve_clip(filename: str):
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Clip not found")
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    if job_id in jobs:
        del jobs[job_id]
    return {"deleted": job_id}