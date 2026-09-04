from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import shutil
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .errors import JobCancelled
from .hardware import gpu_status
from .media import executable_available
from .models import JobView, ProcessingOptions, UploadInitRequest
from .pipeline import run_pipeline
from .preflight import build_preflight
from .store import JobStore
from .uploads import DEFAULT_CHUNK_SIZE, UploadManager


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
JOBS_DIR = BASE_DIR / "workspace" / "jobs"
GIB = 1024 * 1024 * 1024
MAX_UPLOAD_BYTES = min(int(os.getenv("CONTEXTKIT_MAX_UPLOAD_BYTES", str(25 * GIB))), 25 * GIB)

app = FastAPI(
    title="知帧 · 多模态上下文整理器",
    version="1.1.0",
    description="将多媒体和文档整理为 LLM 易读的 TXT 与 PNG 上下文包。",
)
store = JobStore(JOBS_DIR)
uploads = UploadManager(JOBS_DIR, DEFAULT_CHUNK_SIZE)


@app.get("/api/health")
def health() -> dict:
    modules = {
        "Whisper 转写": "faster_whisper",
        "图片 OCR": "rapidocr_onnxruntime",
        "PDF": "pypdf",
        "Office 文档": "docx",
        "演示文稿": "pptx",
        "工作簿": "openpyxl",
        "7Z": "py7zr",
    }
    features = {label: importlib.util.find_spec(module) is not None for label, module in modules.items()}
    return {
        "status": "ready" if executable_available("ffmpeg") else "limited",
        "ffmpeg": executable_available("ffmpeg") and executable_available("ffprobe"),
        "features": features,
        "gpu": gpu_status(),
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "upload_chunk_bytes": DEFAULT_CHUNK_SIZE,
        "resumable_uploads": True,
        "local_only": True,
    }


@app.get("/api/jobs", response_model=list[JobView])
def list_jobs() -> list[dict]:
    return store.list_recent()


@app.post("/api/uploads", response_model=JobView, status_code=201)
def initialize_upload(payload: UploadInitRequest) -> dict:
    selected_limit = min(MAX_UPLOAD_BYTES, int(payload.options.upload_limit_gb * GIB))
    total_size = sum(file.size for file in payload.files)
    if total_size > selected_limit:
        raise HTTPException(
            status_code=413,
            detail=(
                f"本次文件超过所选的 {selected_limit / GIB:g} GB 上限"
                if selected_limit < MAX_UPLOAD_BYTES
                else f"本次文件超过服务器允许的 {MAX_UPLOAD_BYTES / GIB:g} GB 上限"
            ),
        )

    job_id = secrets.token_hex(6)
    job_dir = JOBS_DIR / job_id
    try:
        upload_status = uploads.initialize(job_id, payload.files)
        files = [
            {
                "name": item.name,
                "relative_path": item.relative_path or item.name,
                "size": item.size,
                "content_type": item.content_type,
            }
            for item in payload.files
        ]
        job = store.create(
            job_id,
            files,
            state="uploading",
            stage="等待上传",
            message=f"将以 {DEFAULT_CHUNK_SIZE // 1024**2} MB 分片上传，并逐块校验 SHA-256",
            upload=upload_status,
            initial_event="已建立可暂停、可恢复的分片上传会话",
        )
        (job_dir / "options.json").write_text(payload.options.model_dump_json(indent=2), encoding="utf-8")
        return job
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


@app.get("/api/uploads/{job_id}")
def get_upload_status(job_id: str) -> dict:
    job = require_job(job_id)
    try:
        status = uploads.get_status(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"job": job, "upload": status}


@app.put("/api/uploads/{job_id}/files/{file_id}/chunks/{chunk_index}")
async def upload_chunk(
    job_id: str,
    file_id: str,
    chunk_index: int,
    request: Request,
    x_chunk_sha256: str = Header(default=""),
) -> dict:
    job = require_job(job_id)
    if job["state"] != "uploading" or job.get("cancel_requested"):
        raise HTTPException(status_code=409, detail="该上传会话已暂停、取消或完成")
    content_length = int(request.headers.get("content-length") or 0)
    if content_length > DEFAULT_CHUNK_SIZE:
        raise HTTPException(status_code=413, detail="单个上传分片超过服务器限制")
    data = await request.body()
    if len(data) > DEFAULT_CHUNK_SIZE:
        raise HTTPException(status_code=413, detail="单个上传分片超过服务器限制")
    try:
        status = uploads.write_chunk(job_id, file_id, chunk_index, data, x_chunk_sha256)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (KeyError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    updated = store.update(
        job_id,
        progress=status["progress"],
        stage="分片上传中",
        message=f"已安全接收 {status['uploaded_bytes']:,} / {status['total_bytes']:,} 字节",
        upload=status,
    )
    return {"job": updated, "upload": status}


@app.post("/api/uploads/{job_id}/complete", response_model=JobView)
def complete_upload(job_id: str) -> dict:
    job = require_job(job_id)
    if job["state"] != "uploading":
        if job["state"] == "ready":
            return job
        raise HTTPException(status_code=409, detail="该任务不在可完成上传的状态")
    try:
        input_paths, saved_files, upload_status = uploads.complete(job_id)
        options = read_options(JOBS_DIR / job_id)
        preflight = build_preflight(input_paths, options, JOBS_DIR / job_id / "input")
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return store.update(
        job_id,
        state="ready",
        progress=0,
        stage="预检完成",
        message="已读取文件信息，请确认耗时、空间与输出预估后开始处理",
        files=saved_files,
        upload=upload_status,
        preflight=preflight,
        event="全部分片通过 SHA-256 校验，处理前预检已完成",
    )


@app.post("/api/jobs", response_model=JobView, status_code=202)
async def create_job(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    options: str = Form("{}"),
) -> dict:
    """Compatibility endpoint for small clients that still use one multipart request."""
    if not files:
        raise HTTPException(status_code=400, detail="请至少选择一个文件")
    try:
        parsed_options = ProcessingOptions.model_validate(json.loads(options))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=f"处理参数无效：{exc}") from exc

    job_id = secrets.token_hex(6)
    job_dir = JOBS_DIR / job_id
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=False)
    saved_files: list[dict] = []
    input_paths: list[Path] = []
    total_size = 0
    selected_upload_limit = min(MAX_UPLOAD_BYTES, int(parsed_options.upload_limit_gb * GIB))
    try:
        for upload in files:
            filename = unique_filename(input_dir, sanitize_filename(upload.filename or "unnamed_file"))
            target = input_dir / filename
            size = 0
            with target.open("wb") as output:
                while chunk := await upload.read(4 * 1024 * 1024):
                    size += len(chunk)
                    total_size += len(chunk)
                    if total_size > selected_upload_limit:
                        raise HTTPException(status_code=413, detail="本次上传超过所选或服务器允许的上限")
                    output.write(chunk)
            await upload.close()
            input_paths.append(target)
            saved_files.append(
                {"name": filename, "relative_path": filename, "size": size, "content_type": upload.content_type or ""}
            )
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    job = store.create(job_id, saved_files)
    (job_dir / "options.json").write_text(parsed_options.model_dump_json(indent=2), encoding="utf-8")
    background_tasks.add_task(process_job, job_id, job_dir, input_paths, parsed_options)
    return job


@app.post("/api/jobs/{job_id}/start", response_model=JobView, status_code=202)
def start_ready_job(job_id: str, background_tasks: BackgroundTasks) -> dict:
    job = require_job(job_id)
    if job["state"] != "ready":
        raise HTTPException(status_code=409, detail="任务尚未完成预检，或已经开始处理")
    job_dir = JOBS_DIR / job_id
    input_paths = job_input_paths(job_id, job)
    if not input_paths:
        raise HTTPException(status_code=410, detail="任务的源文件已不存在")
    options = read_options(job_dir)
    queued = store.update(
        job_id,
        state="queued",
        progress=0,
        stage="等待处理",
        message="预检已确认，正在启动本地处理引擎",
        cancel_requested=False,
        error=None,
        event="用户已确认预检结果，任务进入处理队列",
    )
    background_tasks.add_task(process_job, job_id, job_dir, input_paths, options)
    return queued


@app.get("/api/jobs/{job_id}", response_model=JobView)
def get_job(job_id: str) -> dict:
    return require_job(job_id)


@app.post("/api/jobs/{job_id}/cancel", response_model=JobView)
def cancel_job(job_id: str) -> dict:
    job = require_job(job_id)
    if job["state"] in {"completed", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="该任务已经结束")
    if job["state"] == "processing":
        return store.update(
            job_id,
            cancel_requested=True,
            stage="正在取消",
            message="会在当前安全处理点停止",
            event="已收到取消请求",
        )
    return store.update(
        job_id,
        state="cancelled",
        cancel_requested=True,
        stage="已取消",
        message="源文件仍保留在本机，可重新处理或清理任务",
        event="任务已由用户取消",
    )


@app.post("/api/jobs/{job_id}/retry", response_model=JobView, status_code=202)
def retry_job(job_id: str, background_tasks: BackgroundTasks, failed_only: bool = False) -> dict:
    source_job = require_job(job_id)
    if source_job["state"] not in {"completed", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="只有已结束的任务可以重新处理")
    failed_names = set(source_job.get("failed_files") or [])
    if failed_only and not failed_names:
        raise HTTPException(status_code=409, detail="该任务没有可单独重试的失败文件")

    selected_files = [
        item
        for item in source_job.get("files", [])
        if not failed_only
        or item.get("relative_path", item.get("name")) in failed_names
        or item.get("name") in failed_names
    ]
    if not selected_files:
        raise HTTPException(status_code=410, detail="未找到可重新处理的源文件")

    new_id = secrets.token_hex(6)
    new_dir = JOBS_DIR / new_id
    new_input_dir = new_dir / "input"
    new_input_dir.mkdir(parents=True, exist_ok=False)
    new_paths: list[Path] = []
    cloned_files: list[dict] = []
    try:
        source_input_dir = JOBS_DIR / job_id / "input"
        for item in selected_files:
            relative_path = item.get("relative_path") or item["name"]
            source = (source_input_dir / relative_path).resolve()
            if not source.is_relative_to(source_input_dir.resolve()) or not source.is_file():
                continue
            target = (new_input_dir / relative_path).resolve()
            if not target.is_relative_to(new_input_dir.resolve()):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
            new_paths.append(target)
            cloned_files.append({**item, "size": target.stat().st_size})
        if not new_paths:
            raise RuntimeError("源文件已不存在")

        source_options = JOBS_DIR / job_id / "options.json"
        target_options = new_dir / "options.json"
        shutil.copy2(source_options, target_options)
        options = read_options(new_dir)
        job = store.create(
            new_id,
            cloned_files,
            state="queued",
            stage="等待重新处理",
            message="正在复用本机源文件启动新任务",
            source_job_id=job_id,
            initial_event="已从历史任务创建可独立追踪的重试任务",
        )
        background_tasks.add_task(process_job, new_id, new_dir, new_paths, options)
        return job
    except Exception as exc:
        shutil.rmtree(new_dir, ignore_errors=True)
        raise HTTPException(status_code=410, detail=f"无法创建重试任务：{exc}") from exc


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    job = require_job(job_id)
    if job["state"] != "completed" or not job.get("result_name"):
        raise HTTPException(status_code=409, detail="任务尚未生成可下载结果")
    result_path = JOBS_DIR / job_id / job["result_name"]
    if not result_path.is_file():
        raise HTTPException(status_code=410, detail="结果文件已不存在")
    return FileResponse(result_path, media_type="application/zip", filename=job["result_name"])


@app.get("/api/jobs/{job_id}/compressed/{filename}")
def download_compressed_video(job_id: str, filename: str) -> FileResponse:
    job = require_job(job_id)
    allowed = {item.get("name") for item in job.get("compressed_files", [])}
    if filename not in allowed:
        raise HTTPException(status_code=404, detail="没有找到该压缩视频")
    compressed_dir = (JOBS_DIR / job_id / "compressed").resolve()
    target = (compressed_dir / filename).resolve()
    if not target.is_relative_to(compressed_dir) or not target.is_file():
        raise HTTPException(status_code=410, detail="压缩视频已不存在")
    return FileResponse(target, media_type="video/mp4", filename=filename)


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str) -> None:
    job = require_job(job_id)
    if job["state"] in {"uploading", "queued", "processing"}:
        raise HTTPException(status_code=409, detail="请先取消正在上传或处理的任务，再执行清理")
    job_dir = (JOBS_DIR / job_id).resolve()
    if not job_dir.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=400, detail="任务路径无效")
    shutil.rmtree(job_dir, ignore_errors=True)
    store.remove(job_id)


def process_job(job_id: str, job_dir: Path, input_paths: list[Path], options: ProcessingOptions) -> None:
    def update(progress: int, stage: str, message: str) -> None:
        current = store.get(job_id)
        if not current or current.get("cancel_requested") or current.get("state") == "cancelled":
            raise JobCancelled("用户取消了任务")
        store.update(
            job_id,
            state="processing" if progress < 100 else "completed",
            progress=max(0, min(progress, 100)),
            stage=stage,
            message=message,
            event=message,
        )

    try:
        result = run_pipeline(job_id, job_dir, input_paths, options, update)
        compressed_files = [
            {"name": path.name, "size": path.stat().st_size}
            for path in result.compressed_files
            if path.is_file()
        ]
        store.update(
            job_id,
            state="completed",
            progress=100,
            stage="处理完成",
            message="上下文包已经可以下载",
            result_name=result.archive_path.name,
            compressed_files=compressed_files,
            failed_files=result.failed_files,
            cancel_requested=False,
            event="结果已完成校验并打包",
        )
    except JobCancelled:
        store.update(
            job_id,
            state="cancelled",
            stage="已取消",
            message="任务已在安全处理点停止，源文件仍保留在本机",
            cancel_requested=True,
            event="处理已安全停止",
        )
    except Exception as exc:
        store.update(
            job_id,
            state="failed",
            stage="处理失败",
            message="处理过程中遇到无法恢复的错误",
            error=f"{type(exc).__name__}：{exc}",
            event="任务已停止，请查看错误信息",
        )


def require_job(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="没有找到该任务")
    return job


def read_options(job_dir: Path) -> ProcessingOptions:
    try:
        return ProcessingOptions.model_validate_json((job_dir / "options.json").read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise RuntimeError(f"任务参数无法读取：{exc}") from exc


def job_input_paths(job_id: str, job: dict) -> list[Path]:
    input_dir = (JOBS_DIR / job_id / "input").resolve()
    paths = []
    for item in job.get("files", []):
        relative_path = item.get("relative_path") or item.get("name")
        if not relative_path:
            continue
        target = (input_dir / relative_path).resolve()
        if target.is_relative_to(input_dir) and target.is_file():
            paths.append(target)
    return paths


def sanitize_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.rstrip(". ")
    return name[:180] or "unnamed_file"


def unique_filename(directory: Path, filename: str) -> str:
    path = Path(filename)
    candidate = filename
    counter = 2
    while (directory / candidate).exists():
        candidate = f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    return candidate


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
