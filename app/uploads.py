from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from .models import UploadFileSpec


DEFAULT_CHUNK_SIZE = 16 * 1024 * 1024


class UploadManager:
    """Persist resumable upload manifests and verified random-access chunks."""

    def __init__(self, root: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> None:
        self.root = root
        self.chunk_size = chunk_size
        self._lock = threading.RLock()

    def initialize(self, job_id: str, specs: list[UploadFileSpec]) -> dict[str, Any]:
        job_dir = self.root / job_id
        parts_dir = job_dir / "upload_parts"
        input_dir = job_dir / "input"
        parts_dir.mkdir(parents=True, exist_ok=False)
        input_dir.mkdir(parents=True, exist_ok=False)

        used_paths: set[str] = set()
        files = []
        for index, spec in enumerate(specs):
            file_id = f"f{index + 1:05d}"
            relative_path = unique_relative_path(
                sanitize_relative_path(spec.relative_path or spec.name),
                used_paths,
            )
            used_paths.add(relative_path.lower())
            files.append(
                {
                    "id": file_id,
                    "name": Path(relative_path).name,
                    "original_name": spec.name,
                    "relative_path": relative_path,
                    "size": spec.size,
                    "content_type": spec.content_type,
                    "last_modified": spec.last_modified,
                    "total_chunks": math.ceil(spec.size / self.chunk_size) if spec.size else 0,
                    "uploaded": {},
                }
            )

        manifest = {
            "version": 1,
            "job_id": job_id,
            "chunk_size": self.chunk_size,
            "complete": False,
            "files": files,
        }
        self._save(job_id, manifest)
        return self.public_status(manifest)

    def get_status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self.public_status(self._load(job_id))

    def write_chunk(
        self,
        job_id: str,
        file_id: str,
        chunk_index: int,
        data: bytes,
        expected_sha256: str,
    ) -> dict[str, Any]:
        with self._lock:
            manifest = self._load(job_id)
            if manifest.get("complete"):
                raise RuntimeError("上传已经完成，不能再写入分片")
            file_entry = find_file(manifest, file_id)
            total_chunks = int(file_entry["total_chunks"])
            if chunk_index < 0 or chunk_index >= total_chunks:
                raise ValueError("分片编号超出范围")

            offset = chunk_index * int(manifest["chunk_size"])
            expected_size = min(int(manifest["chunk_size"]), int(file_entry["size"]) - offset)
            if len(data) != expected_size:
                raise ValueError(f"分片大小不正确，应为 {expected_size} 字节")
            digest = hashlib.sha256(data).hexdigest()
            if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256 or ""):
                raise ValueError("缺少有效的 SHA-256 分片校验值")
            if digest.lower() != expected_sha256.lower():
                raise ValueError("分片 SHA-256 校验失败，请重试该分片")

            part_path = self.root / job_id / "upload_parts" / f"{file_id}.part"
            mode = "r+b" if part_path.exists() else "w+b"
            with part_path.open(mode) as output:
                output.seek(offset)
                output.write(data)
                output.flush()
            file_entry["uploaded"][str(chunk_index)] = digest
            self._save(job_id, manifest)
            return self.public_status(manifest)

    def complete(self, job_id: str) -> tuple[list[Path], list[dict[str, Any]], dict[str, Any]]:
        with self._lock:
            manifest = self._load(job_id)
            if manifest.get("complete"):
                return self._completed_files(job_id, manifest)

            job_dir = self.root / job_id
            input_dir = job_dir / "input"
            input_paths: list[Path] = []
            saved_files: list[dict[str, Any]] = []
            for file_entry in manifest["files"]:
                expected = int(file_entry["total_chunks"])
                uploaded = file_entry.get("uploaded", {})
                if len(uploaded) != expected or any(str(index) not in uploaded for index in range(expected)):
                    raise RuntimeError(f"{file_entry['relative_path']} 仍有未上传分片")

                target = (input_dir / file_entry["relative_path"]).resolve()
                if not target.is_relative_to(input_dir.resolve()):
                    raise RuntimeError("上传目标路径无效")
                target.parent.mkdir(parents=True, exist_ok=True)
                part_path = job_dir / "upload_parts" / f"{file_entry['id']}.part"
                if int(file_entry["size"]) == 0:
                    target.touch()
                else:
                    if not part_path.is_file() or part_path.stat().st_size != int(file_entry["size"]):
                        raise RuntimeError(f"{file_entry['relative_path']} 的本地分片文件不完整")
                    part_path.replace(target)
                input_paths.append(target)
                saved_files.append(
                    {
                        "name": file_entry["name"],
                        "relative_path": file_entry["relative_path"],
                        "size": file_entry["size"],
                        "content_type": file_entry["content_type"],
                    }
                )

            parts_dir = job_dir / "upload_parts"
            try:
                parts_dir.rmdir()
            except OSError:
                pass
            manifest["complete"] = True
            self._save(job_id, manifest)
            return input_paths, saved_files, self.public_status(manifest)

    def _completed_files(
        self,
        job_id: str,
        manifest: dict[str, Any],
    ) -> tuple[list[Path], list[dict[str, Any]], dict[str, Any]]:
        input_dir = self.root / job_id / "input"
        paths = [input_dir / item["relative_path"] for item in manifest["files"]]
        files = [
            {
                "name": item["name"],
                "relative_path": item["relative_path"],
                "size": item["size"],
                "content_type": item["content_type"],
            }
            for item in manifest["files"]
        ]
        return paths, files, self.public_status(manifest)

    def public_status(self, manifest: dict[str, Any]) -> dict[str, Any]:
        files = []
        uploaded_bytes = 0
        total_bytes = 0
        chunk_size = int(manifest["chunk_size"])
        for item in manifest["files"]:
            indices = sorted(int(index) for index in item.get("uploaded", {}))
            size = int(item["size"])
            file_uploaded = sum(min(chunk_size, max(0, size - index * chunk_size)) for index in indices)
            uploaded_bytes += file_uploaded
            total_bytes += size
            files.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "relative_path": item["relative_path"],
                    "size": size,
                    "last_modified": item["last_modified"],
                    "total_chunks": item["total_chunks"],
                    "uploaded_chunks": indices,
                    "uploaded_bytes": file_uploaded,
                }
            )
        return {
            "chunk_size": chunk_size,
            "complete": bool(manifest.get("complete")),
            "uploaded_bytes": uploaded_bytes,
            "total_bytes": total_bytes,
            "progress": round(uploaded_bytes / total_bytes * 100) if total_bytes else 100,
            "files": files,
        }

    def _manifest_path(self, job_id: str) -> Path:
        return self.root / job_id / "upload_manifest.json"

    def _load(self, job_id: str) -> dict[str, Any]:
        path = self._manifest_path(job_id)
        if not path.is_file():
            raise FileNotFoundError("没有找到上传会话")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, job_id: str, manifest: dict[str, Any]) -> None:
        path = self._manifest_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


def find_file(manifest: dict[str, Any], file_id: str) -> dict[str, Any]:
    for item in manifest["files"]:
        if item["id"] == file_id:
            return item
    raise KeyError("没有找到该上传文件")


def sanitize_relative_path(raw_path: str) -> str:
    normalized = raw_path.replace("\\", "/").strip("/ ")
    parts = []
    for part in PurePosixPath(normalized).parts:
        if part in {"", ".", ".."}:
            continue
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part).rstrip(". ")[:180]
        if cleaned:
            parts.append(cleaned)
    return "/".join(parts[-20:]) or "unnamed_file"


def unique_relative_path(relative_path: str, used_paths: set[str]) -> str:
    candidate = relative_path
    path = PurePosixPath(relative_path)
    counter = 2
    while candidate.lower() in used_paths:
        candidate = str(path.with_name(f"{path.stem}_{counter}{path.suffix}"))
        counter += 1
    return candidate
