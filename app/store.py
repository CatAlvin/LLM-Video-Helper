from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._load_existing()

    def _load_existing(self) -> None:
        for state_path in self.root.glob("*/state.json"):
            try:
                job = json.loads(state_path.read_text(encoding="utf-8"))
                if job.get("state") in {"queued", "processing"}:
                    job["state"] = "failed"
                    job["stage"] = "任务已中断"
                    job["error"] = "应用上次关闭时任务尚未完成，请重新提交。"
                    job["updated_at"] = now_iso()
                self._jobs[job["id"]] = job
                self._persist(job)
            except (OSError, ValueError, KeyError):
                continue

    def create(
        self,
        job_id: str,
        files: list[dict[str, Any]],
        *,
        state: str = "queued",
        stage: str = "文件已接收",
        message: str = "等待处理引擎启动",
        upload: dict[str, Any] | None = None,
        source_job_id: str | None = None,
        initial_event: str = "文件已安全保存到本地任务目录",
    ) -> dict[str, Any]:
        timestamp = now_iso()
        job = {
            "id": job_id,
            "state": state,
            "progress": 0,
            "stage": stage,
            "message": message,
            "created_at": timestamp,
            "updated_at": timestamp,
            "files": files,
            "events": [{"time": timestamp, "text": initial_event}],
            "result_name": None,
            "compressed_files": [],
            "failed_files": [],
            "preflight": None,
            "upload": upload,
            "cancel_requested": False,
            "source_job_id": source_job_id,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._persist(job)
        return deepcopy(job)

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            job = self._jobs[job_id]
            event = changes.pop("event", None)
            if event and (not job["events"] or job["events"][-1]["text"] != event):
                job["events"].append({"time": now_iso(), "text": event})
                job["events"] = job["events"][-30:]
            job.update(changes)
            job["updated_at"] = now_iso()
            self._persist(job)
            return deepcopy(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return deepcopy(job) if job else None

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item["created_at"], reverse=True)
            return deepcopy(jobs[:limit])

    def remove(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def _persist(self, job: dict[str, Any]) -> None:
        path = self.root / job["id"] / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
