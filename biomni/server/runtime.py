"""Single-process workspace runtime. Plan uses one LLM call; step uses go_stream()."""

from __future__ import annotations

import json
import re
import threading
import uuid
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from biomni.agent import A1
from biomni.server import store

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_SOLUTION = re.compile(r"<solution>(.*?)</solution>", re.DOTALL)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_MSG_BANNER = re.compile(
    r"[-=]{2,}\s*[A-Za-z]+(?:\s+[A-Za-z]+)*\s+Message\s*[-=]{2,}\s*",
    re.IGNORECASE,
)


def _message_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"text", "output_text", "redacted_text"}:
                parts.append(str(block.get("text") or block.get("content") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content or "")


def _extract_solution(text: str) -> str:
    match = _SOLUTION.search(text or "")
    return match.group(1).strip() if match else (text or "").strip()


def _parse_plan_steps(text: str) -> list[dict[str, str]]:
    payload = _extract_solution(text)
    blob = None
    fenced = _JSON_FENCE.search(payload)
    if fenced:
        blob = fenced.group(1)
    else:
        start = payload.find("{")
        end = payload.rfind("}")
        if start != -1 and end > start:
            blob = payload[start : end + 1]
    if not blob:
        raise ValueError("Planner did not return JSON steps")

    data = json.loads(blob)
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Planner JSON has no steps")

    steps: list[dict[str, str]] = []
    for i, item in enumerate(raw_steps, start=1):
        if isinstance(item, str):
            steps.append({"id": f"s{i}", "title": item.strip(), "why": ""})
            continue
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            continue
        steps.append(
            {
                "id": str(item.get("id") or f"s{i}"),
                "title": title,
                "why": str(item.get("why") or item.get("reason") or "").strip(),
            }
        )
    if not steps:
        raise ValueError("Planner JSON steps were empty after parsing")
    return steps


def _strip_message_banner(text: str) -> str:
    cleaned = _ANSI.sub("", str(text or ""))
    cleaned = _MSG_BANNER.sub("", cleaned)
    return cleaned.strip()


def _describe(text: str) -> str:
    return _strip_message_banner(text) or "working"


class Runtime:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._job_lock = threading.Lock()
        self.agent: A1 | None = None
        self.booted = False
        self.busy = False
        self.job_type: str | None = None
        self.session_id: str | None = None

    def boot(self) -> None:
        agent = A1(path="./data", expected_data_lake_files=[])
        with self._lock:
            self.agent = agent
            self.booted = True

    def global_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": "busy" if self.busy else ("idle" if self.booted else "booting"),
                "busy": self.busy,
                "type": self.job_type,
                "session_id": self.session_id,
                "booted": self.booted,
            }

    def start_plan(self, prompt: str, session_id: str | None = None) -> dict[str, str]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        session_id = (session_id or "").strip() or None
        with self._lock:
            if not self.booted or self.agent is None:
                raise RuntimeError("agent is still booting")
            if self.busy:
                raise RuntimeError("a job is already running")
            if session_id:
                if not store.exists(session_id):
                    raise FileNotFoundError(f"unknown session_id: {session_id}")
            else:
                session_id = f"s_{uuid.uuid4().hex[:12]}"
            store.ensure_session(session_id)
            store.write_result(
                session_id,
                "plan",
                {
                    "status": "running",
                    "prompt": prompt,
                    "description": "planning",
                    "result": None,
                    "error": None,
                    "log": ["planning"],
                },
            )
            self.busy = True
            self.job_type = "plan"
            self.session_id = session_id
        threading.Thread(target=self._run_plan, args=(session_id, prompt), daemon=True).start()
        return {"session_id": session_id}

    def start_step(self, prompt: str, session_id: str) -> dict[str, str]:
        prompt = (prompt or "").strip()
        session_id = (session_id or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        if not session_id:
            raise ValueError("session_id is required")
        with self._lock:
            if not self.booted or self.agent is None:
                raise RuntimeError("agent is still booting")
            if self.busy:
                raise RuntimeError("a job is already running")
            if not store.exists(session_id):
                raise FileNotFoundError(f"unknown session_id: {session_id}")
            store.ensure_session(session_id)
            store.write_result(
                session_id,
                "step",
                {
                    "status": "running",
                    "prompt": prompt,
                    "description": "starting step",
                    "result": None,
                    "error": None,
                    "log": ["starting step"],
                },
            )
            self.busy = True
            self.job_type = "step"
            self.session_id = session_id
        threading.Thread(target=self._run_step, args=(session_id, prompt), daemon=True).start()
        return {"session_id": session_id}

    def read_result(self, job_type: store.JobType, session_id: str, full: bool = False) -> dict[str, Any] | None:
        return store.load_result(session_id, job_type, full=full)

    def _finish(self) -> None:
        with self._lock:
            self.busy = False
            self.job_type = None

    def _run_plan(self, session_id: str, prompt: str) -> None:
        if not self._job_lock.acquire(blocking=False):
            store.update_result(
                session_id,
                "plan",
                status="done",
                error="another job holds the process",
                log_entry="another job holds the process",
            )
            self._finish()
            return
        try:
            assert self.agent is not None
            store.update_result(
                session_id,
                "plan",
                description="retrieving relevant resources",
                log_entry="retrieving relevant resources",
            )
            if self.agent.use_tool_retriever:
                selected = self.agent._prepare_resources_for_retrieval(prompt)
                if selected:
                    self.agent.update_system_prompt_with_selected_resources(selected)

            prior_file = store.files_dir(session_id, "plan") / "plan.json"
            archived = json.loads(prior_file.read_text(encoding="utf-8")) if prior_file.is_file() else None

            store.update_result(session_id, "plan", description="writing plan", log_entry="writing plan")
            response = self.agent.llm.invoke(
                [
                    SystemMessage(content=self.agent.system_prompt),
                    HumanMessage(content=self._plan_prompt(prompt, archived)),
                ]
            )
            text = _message_text(response)
            steps = _parse_plan_steps(text)
            result = {"steps": steps}
            files = store.files_dir(session_id, "plan")
            files.mkdir(parents=True, exist_ok=True)
            (files / "plan.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            (files / "plan.md").write_text(self._plan_markdown(prompt, steps), encoding="utf-8")
            store.update_result(
                session_id,
                "plan",
                status="done",
                description="plan ready",
                result=result,
                error=None,
                log_entry="plan ready",
            )
        except Exception as exc:
            store.update_result(
                session_id,
                "plan",
                status="done",
                error=str(exc),
                description="plan failed",
                log_entry="plan failed",
            )
        finally:
            self._job_lock.release()
            self._finish()

    def _run_step(self, session_id: str, prompt: str) -> None:
        if not self._job_lock.acquire(blocking=False):
            store.update_result(
                session_id,
                "step",
                status="done",
                error="another job holds the process",
                log_entry="another job holds the process",
            )
            self._finish()
            return
        try:
            assert self.agent is not None
            plan_doc = store.read_json(store.result_path(session_id, "plan"))
            prev_step = store.read_json(store.result_path(session_id, "step"))
            out_dir = store.files_dir(session_id, "step")
            out_dir.mkdir(parents=True, exist_ok=True)
            store.update_result(session_id, "step", description="running step", log_entry="running step")
            last_text = ""
            last_cleaned = ""
            for event in self.agent.go_stream(self._step_prompt(prompt, session_id, plan_doc, prev_step, out_dir)):
                last_text = str(event.get("output") or "")
                last_cleaned = _strip_message_banner(last_text)
                fields: dict[str, Any] = {"description": _describe(last_cleaned)}
                if last_cleaned:
                    fields["log_entry"] = last_cleaned
                store.update_result(session_id, "step", **fields)
            summary = _extract_solution(last_cleaned or last_text)
            store.update_result(
                session_id,
                "step",
                status="done",
                description="step finished",
                result={"summary": summary},
                error=None,
                log_entry="step finished",
            )
        except Exception as exc:
            store.update_result(
                session_id,
                "step",
                status="done",
                error=str(exc),
                description="step failed",
                log_entry="step failed",
            )
        finally:
            self._job_lock.release()
            self._finish()

    @staticmethod
    def _plan_prompt(prompt: str, previous_result: Any) -> str:
        previous = ""
        if previous_result:
            previous = (
                "\nPrevious plan JSON (revise it according to the new prompt, keep useful steps):\n"
                f"{json.dumps(previous_result, ensure_ascii=False, indent=2)}\n"
            )
        return f"""You are only allowed to plan. Do not run tools. Do not write or execute code.
Do not use <execute> tags.

User prompt:
{prompt}
{previous}
Output JSON only in <solution>:
{{
  "steps": [
    {{"id": "s1", "title": "one concrete action", "why": "why this step is needed"}}
  ]
}}

Rules:
- 3 to 8 steps
- titles must be concrete and scoped to a single action
- do not start any analysis in this turn
"""

    @staticmethod
    def _plan_markdown(prompt: str, steps: list[dict[str, str]]) -> str:
        lines = ["# Plan\n", f"{prompt}\n"]
        for item in steps:
            why = f" — {item['why']}" if item.get("why") else ""
            lines.append(f"- **{item['id']}**: {item['title']}{why}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _step_prompt(
        prompt: str,
        session_id: str,
        plan_doc: dict[str, Any] | None,
        prev_step: dict[str, Any] | None,
        out_dir,
    ) -> str:
        plan_block = "none"
        if plan_doc and plan_doc.get("result"):
            plan_block = json.dumps(plan_doc["result"], ensure_ascii=False, indent=2)
        prev_block = "none"
        if prev_step and prev_step.get("result") and prev_step.get("status") == "done":
            prev_block = json.dumps(prev_step["result"], ensure_ascii=False, indent=2)
        return f"""Complete ONLY the approved work in the user prompt. Do not start unrelated later steps.

Session: {session_id}
Save any generated files into this directory:
{out_dir}

Current plan:
{plan_block}

Previous step result:
{prev_block}

User prompt:
{prompt}

When this work is done, put a concise summary in <solution>.
"""


runtime = Runtime()
