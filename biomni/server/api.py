"""Plan / step workspace API. Result endpoints always return HTTP 200 when the session exists."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from biomni.server.runtime import runtime


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runtime.boot()
    yield


app = FastAPI(title="Biomni Step API", lifespan=lifespan)


class PlanRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    session_id: str | None = None


class StepRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)


def _raise(exc: Exception):
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise exc


@app.get("/health")
def health():
    snap = runtime.global_status()
    return {"ok": snap["booted"], "state": snap["state"]}


@app.get("/status")
def status():
    snap = runtime.global_status()
    code = 202 if snap["busy"] else 200
    return JSONResponse(snap, status_code=code)


@app.post("/plan", status_code=202)
def plan(req: PlanRequest):
    try:
        return runtime.start_plan(req.prompt, req.session_id)
    except Exception as exc:
        _raise(exc)


@app.post("/step", status_code=202)
def step(req: StepRequest):
    try:
        return runtime.start_step(req.prompt, req.session_id)
    except Exception as exc:
        _raise(exc)


@app.get("/plan/result/{session_id}")
def plan_result(session_id: str, log: str | None = None):
    data = runtime.read_result("plan", session_id, full=log == "full")
    if data is None:
        raise HTTPException(status_code=404, detail=f"unknown session_id: {session_id}")
    return data


@app.get("/step/result/{session_id}")
def step_result(session_id: str, log: str | None = None):
    data = runtime.read_result("step", session_id, full=log == "full")
    if data is None:
        raise HTTPException(status_code=404, detail=f"unknown session_id: {session_id}")
    return data
