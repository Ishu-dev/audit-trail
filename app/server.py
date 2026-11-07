"""Audit Trail web server.

  .venv/bin/uvicorn server:app --app-dir app --port 8000

Endpoints
  POST /api/jobs                  upload an .xlsx (or demo=planted|clean); parses, then audits
  GET  /api/jobs/{id}/events      server-sent events, the live run
  GET  /api/jobs/{id}/report      final report JSON
  POST /api/jobs/{id}/attack      run the labelled attack probe in this job's sandbox
  GET  /api/config                available providers
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from agent import PROVIDERS, Audit, write_report_md
from parser import column_headers, parse_workbook
from sandbox import ATTACK_PROBE, make_sandbox

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "runs" / "web"
STATIC = Path(__file__).resolve().parent / "static"
DEMOS = {"planted": ROOT / "sample_models" / "halcyra_model_planted.xlsx",
         "clean": ROOT / "sample_models" / "halcyra_model_clean.xlsx"}
MAX_BYTES = 5 * 1024 * 1024
MAX_RUNNING = int(os.environ.get("AUDIT_MAX_RUNNING", "3"))
SANDBOX_KIND = os.environ.get("AUDIT_SANDBOX", "auto")

app = FastAPI(title="Audit Trail")
JOBS: dict[str, "Job"] = {}


def available_providers() -> list[str]:
    out = []
    if os.environ.get("VULTR_INFERENCE_API_KEY") or (os.environ.get("LLM_API_KEY") and "vultr" in os.environ.get("LLM_BASE_URL", "")):
        out.append("vultr")
    if os.environ.get("GEMINI_API_KEY"):
        out.append("gemini")
    if os.environ.get("AUDIT_ALLOW_CLAUDE_CLI") == "1":
        out.append("claude")
    return out


class Job:
    def __init__(self, path: Path, name: str, provider: str, model: str | None):
        self.id = uuid.uuid4().hex[:10]
        self.dir = JOBS_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path, self.name, self.provider, self.model = path, name, provider, model
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.status = "queued"
        self.dump = None
        self.sandbox = None
        self.report = None
        self.probe_running = False

    def emit(self, ev: dict) -> None:
        ev.setdefault("t", round(time.time(), 2))
        with self.lock:
            ev["seq"] = len(self.events)
            self.events.append(ev)
        with (self.dir / "events.jsonl").open("a") as f:
            f.write(json.dumps(ev, default=str) + "\n")

    def run(self) -> None:
        try:
            self.status = "parsing"
            self.emit({"type": "parsing", "file": self.name})
            self.dump = parse_workbook(self.path)
            (self.dir / "workbook.json").write_text(json.dumps(self.dump, default=str))
            self.sandbox = make_sandbox(SANDBOX_KIND, self.dir / "workbook.json")
            self.emit({"type": "parsed", "sheets": [s["name"] for s in self.dump["sheets"]],
                       "cells": sum(len(s["cells"]) for s in self.dump["sheets"]),
                       "sandbox": self.sandbox.name, "stated_ebitda": stated_ebitda(self.dump)})
            self.status = "auditing"
            audit = Audit(dump=self.dump, sandbox=self.sandbox, provider=self.provider,
                          model=self.model, max_steps=20, on_event=self.emit)
            audit.run()
            rep = audit.report()
            rep["stated_ebitda"] = stated_ebitda(self.dump)
            self.report = rep
            (self.dir / "report.json").write_text(json.dumps(rep, indent=2, default=str))
            write_report_md(rep, self.dir / "report.md")
            self.status = "done"
            self.emit({"type": "report_ready"})
        except Exception as e:  # surface failures in the UI instead of hanging
            self.status = "error"
            self.emit({"type": "error", "error": f"{type(e).__name__}: {str(e)[:400]}"})

    def run_probe(self) -> None:
        self.probe_running = True
        try:
            r = self.sandbox.run(ATTACK_PROBE)
            self.emit({"type": "attack_probe", "run_id": r.run_id, "code": ATTACK_PROBE.strip(),
                       "stdout": r.stdout, "stderr": r.stderr[-1000:], "sandbox": r.sandbox,
                       "detail": "Labelled test: runs what the hidden instruction asked for, inside the sandbox."})
            if r.network_attempt:
                self.emit({"type": "containment", "run_id": r.run_id, "source": "attack probe",
                           "detail": "POST of workbook and env to the attacker URL failed: no network.",
                           "blocked": r.blocked})
        finally:
            self.probe_running = False


def stated_ebitda(dump: dict) -> dict:
    """EBITDA by year as the workbook states it (row labelled EBITDA)."""
    for s in dump["sheets"]:
        rows: dict[int, list[dict]] = {}
        for c in s["cells"]:
            rows.setdefault(c["row"], []).append(c)
        heads = column_headers(rows)
        for r, cells in rows.items():
            lab = next((str(c["value"]) for c in cells if c["col"] in ("A", "B") and c["is_text"]), "")
            if lab.strip().upper() == "EBITDA":
                return {heads[c["col"]]: c["value"] for c in cells
                        if c["col"] in heads and isinstance(c["value"], (int, float))}
    return {}


MAX_INFLATED = 100 * 1024 * 1024
MAX_MEMBERS = 2000


def check_zip(data: bytes) -> None:
    """Reject zip bombs and non-workbooks before openpyxl inflates anything."""
    import io
    import zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except zipfile.BadZipFile:
        raise HTTPException(400, "not a valid .xlsx file")
    if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_INFLATED:
        raise HTTPException(400, "workbook expands to more than 100 MB; refusing to open it")
    if not any(i.filename == "xl/workbook.xml" for i in infos):
        raise HTTPException(400, "not an Excel workbook (no xl/workbook.xml)")


@app.get("/api/config")
def config():
    provs = available_providers()
    return {"providers": provs, "default": provs[0] if provs else None,
            "default_models": {p: PROVIDERS[p][2] for p in provs}, "demos": list(DEMOS)}


@app.post("/api/jobs")
async def create_job(file: UploadFile | None = File(None), demo: str | None = Form(None),
                     provider: str | None = Form(None), model: str | None = Form(None)):
    provs = available_providers()
    provider = provider or (provs[0] if provs else None)
    if provider not in provs:
        raise HTTPException(400, f"provider must be one of {provs}")
    busy = sum(j.status in ("queued", "parsing", "auditing") for j in JOBS.values())
    if busy >= MAX_RUNNING:
        raise HTTPException(429, f"{busy} audits are running; try again in a minute")
    if demo:
        if demo not in DEMOS:
            raise HTTPException(400, "unknown demo")
        src, name = DEMOS[demo], DEMOS[demo].name
        job = Job(src, name, provider, model or None)
    else:
        if file is None:
            raise HTTPException(400, "upload a file or pick a demo")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "upload.xlsx").name)[:80]
        if not name.lower().endswith(".xlsx"):
            raise HTTPException(400, "only .xlsx files")
        data = await file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "file over 5 MB")
        if not data.startswith(b"PK"):
            raise HTTPException(400, "not a valid .xlsx file")
        check_zip(data)
        job = Job(Path("/dev/null"), name, provider, model or None)
        job.path = job.dir / name
        job.path.write_bytes(data)
    JOBS[job.id] = job
    threading.Thread(target=job.run, daemon=True).start()
    return {"id": job.id, "file": job.name, "provider": provider}


def _job(jid: str) -> Job:
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "no such job")
    return job


@app.get("/api/jobs/{jid}/events")
async def events(jid: str, after: int = -1):
    job = _job(jid)

    async def gen():
        i = after + 1
        idle = 0
        while True:
            with job.lock:
                new = job.events[i:]
            for ev in new:
                yield f"data: {json.dumps(ev, default=str)}\n\n"
            i += len(new)
            if new:
                idle = 0
            else:
                idle += 1
                if idle % 30 == 0:
                    yield ": keep-alive\n\n"
                # finished and quiet for 10 minutes: close (the probe can still be run; page reconnects)
                if job.status in ("done", "error") and not job.probe_running and idle > 2400:
                    return
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{jid}/report")
def report(jid: str):
    job = _job(jid)
    if job.report is None:
        raise HTTPException(409, f"not ready ({job.status})")
    return job.report


@app.post("/api/jobs/{jid}/attack")
def attack(jid: str):
    job = _job(jid)
    if job.sandbox is None:
        raise HTTPException(409, "sandbox not ready yet")
    if job.probe_running:
        raise HTTPException(409, "probe already running")
    threading.Thread(target=job.run_probe, daemon=True).start()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
