"""Session workspace on disk. result.json is the source of truth."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any, Literal

JobType = Literal["plan", "step"]

ROOT = Path(__file__).resolve().parents[2] / "sessions"
TEXT_TYPES = {"md", "txt", "json", "csv", "tsv", "html", "xml", "yaml", "yml"}
MAX_FILE_BYTES = 5 * 1024 * 1024


def session_dir(session_id: str) -> Path:
    return ROOT / session_id


def job_dir(session_id: str, job_type: JobType) -> Path:
    return session_dir(session_id) / job_type


def result_path(session_id: str, job_type: JobType) -> Path:
    return job_dir(session_id, job_type) / "result.json"


def files_dir(session_id: str, job_type: JobType) -> Path:
    return job_dir(session_id, job_type) / "files"


def exists(session_id: str) -> bool:
    return session_dir(session_id).is_dir()


def ensure_session(session_id: str) -> Path:
    path = session_dir(session_id)
    files_dir(session_id, "plan").mkdir(parents=True, exist_ok=True)
    files_dir(session_id, "step").mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_result(session_id: str, job_type: JobType, data: dict[str, Any]) -> dict[str, Any]:
    ensure_session(session_id)
    payload = {
        "session_id": session_id,
        "type": job_type,
        "status": data.get("status", "running"),
        "prompt": data.get("prompt", ""),
        "description": data.get("description", ""),
        "result": data.get("result"),
        "error": data.get("error"),
        "log": list(data.get("log") or []),
    }
    write_json(result_path(session_id, job_type), payload)
    return payload


def update_result(session_id: str, job_type: JobType, **fields: Any) -> dict[str, Any]:
    current = read_json(result_path(session_id, job_type)) or {
        "session_id": session_id,
        "type": job_type,
        "status": "running",
        "prompt": "",
        "description": "",
        "result": None,
        "error": None,
        "log": [],
    }
    log_entry = fields.pop("log_entry", None)
    current.update(fields)
    if log_entry:
        log = list(current.get("log") or [])
        log.append(str(log_entry))
        current["log"] = log
    return write_result(session_id, job_type, current)


def content_type_of(name: str) -> str:
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix:
        return suffix
    guessed, _ = mimetypes.guess_type(name)
    return (guessed or "bin").split("/")[-1]


def read_outputs(session_id: str, job_type: JobType) -> list[dict[str, str]]:
    folder = files_dir(session_id, job_type)
    if not folder.is_dir():
        return []
    outputs: list[dict[str, str]] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            outputs.append(
                {
                    "name": path.name,
                    "content_type": content_type_of(path.name),
                    "content": "",
                    "error": f"file larger than {MAX_FILE_BYTES} bytes",
                }
            )
            continue
        kind = content_type_of(path.name)
        item: dict[str, str] = {"name": path.name, "content_type": kind}
        if kind in TEXT_TYPES:
            item["content"] = data.decode("utf-8", errors="replace")
        else:
            import base64

            item["encoding"] = "base64"
            item["content"] = base64.b64encode(data).decode("ascii")
        outputs.append(item)
    return outputs


def load_result(session_id: str, job_type: JobType, full: bool = False) -> dict[str, Any] | None:
    data = read_json(result_path(session_id, job_type))
    if data is None:
        if not exists(session_id):
            return None
        payload: dict[str, Any] = {
            "session_id": session_id,
            "type": job_type,
            "status": "done",
            "prompt": "",
            "description": "",
            "result": None,
            "error": None,
            "output": [],
        }
        if full:
            payload["log"] = []
        return payload
    data["output"] = read_outputs(session_id, job_type)
    if full:
        data["log"] = list(data.get("log") or [])
    else:
        data.pop("log", None)
    return data
