from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from .hardware import gpu_status
from .media import format_seconds, probe_media
from .models import ProcessingOptions


MEDIA_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".amr", ".ape",
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg", ".ts",
    ".mts", ".m2ts", ".3gp",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg", ".ts",
    ".mts", ".m2ts", ".3gp",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif", ".heic"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".csv", ".tsv", ".txt", ".md", ".html", ".eml"}
MODEL_DOWNLOAD_BYTES = {
    "tiny": 75 * 1024**2,
    "base": 145 * 1024**2,
    "small": 466 * 1024**2,
    "medium": 1_530 * 1024**2,
    "large-v3": 3_100 * 1024**2,
}


def build_preflight(
    input_paths: list[Path],
    options: ProcessingOptions,
    relative_root: Path | None = None,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    total_bytes = 0
    total_duration = 0.0
    video_bytes = 0
    estimated_compressed_bytes = 0
    media_seconds = 0.0
    video_seconds = 0.0
    warnings: list[str] = []

    for path in input_paths:
        size = path.stat().st_size
        total_bytes += size
        suffix = path.suffix.lower()
        relative_name = (
            path.relative_to(relative_root).as_posix()
            if relative_root and path.is_relative_to(relative_root)
            else path.name
        )
        item: dict[str, Any] = {
            "name": relative_name,
            "size": size,
            "kind": classify_kind(suffix),
            "type": mimetypes.guess_type(path.name)[0] or "未知格式",
            "duration_seconds": 0,
            "duration": "",
            "details": [],
            "estimated_compressed_bytes": None,
        }
        if suffix in MEDIA_EXTENSIONS:
            try:
                data = probe_media(path)
                fmt = data.get("format", {})
                streams = data.get("streams", [])
                duration = float(fmt.get("duration") or 0)
                has_video = any(stream.get("codec_type") == "video" for stream in streams)
                has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
                total_duration += duration
                media_seconds += duration
                item["duration_seconds"] = duration
                item["duration"] = format_seconds(duration)
                item["kind"] = "视频" if has_video else "音频"
                if has_video:
                    video_bytes += size
                    video_seconds += duration
                    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
                    width = int(video_stream.get("width") or 0)
                    height = int(video_stream.get("height") or 0)
                    item["details"].append(f"{width or '?'} × {height or '?'}")
                    item["details"].append(str(video_stream.get("codec_name") or "未知视频编码").upper())
                    if options.video_compression_enabled:
                        estimate = estimate_compressed_size(size, duration, height, options)
                        item["estimated_compressed_bytes"] = estimate
                        estimated_compressed_bytes += estimate
                if has_audio:
                    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
                    item["details"].append(str(audio_stream.get("codec_name") or "未知音频编码").upper())
            except Exception as exc:
                item["details"].append("媒体信息读取失败")
                item["warning"] = str(exc)
                warnings.append(f"{relative_name}：无法读取完整媒体信息")
        files.append(item)

    runtime = gpu_status()
    use_gpu = options.processing_device != "cpu" and bool(runtime.get("available"))
    model_factor = {
        "tiny": 0.012,
        "base": 0.018,
        "small": 0.028,
        "medium": 0.055,
        "large-v3": 0.09,
    }[options.whisper_model]
    if not use_gpu:
        model_factor *= 8
    transcription_seconds = media_seconds * model_factor if options.transcription_enabled else 0
    compression_factor = 0.08 if use_gpu else 0.45
    compression_seconds = video_seconds * compression_factor if options.video_compression_enabled else 0
    other_seconds = max(8.0, total_bytes / (180 * 1024**2)) + len(files) * 1.5
    estimate_mid = transcription_seconds + compression_seconds + other_seconds
    estimate_min = max(5, round(estimate_mid * 0.65))
    estimate_max = max(estimate_min + 5, round(estimate_mid * 1.65))

    model_size = MODEL_DOWNLOAD_BYTES[options.whisper_model]
    model_cached = whisper_model_cached(options.whisper_model)
    model_download = 0 if model_cached or not options.transcription_enabled or media_seconds == 0 else model_size
    visual_working = options.max_output_images * options.output_image_width * 700
    compression_working = estimated_compressed_bytes if options.video_compression_enabled else 0
    disk_bytes = total_bytes + compression_working + visual_working + model_download
    output_estimate = 256 * 1024 + options.max_output_images * 1_800_000

    compression = None
    if options.video_compression_enabled and video_bytes:
        savings = (1 - estimated_compressed_bytes / video_bytes) * 100 if video_bytes else 0
        compression = {
            "original_bytes": video_bytes,
            "estimated_bytes": estimated_compressed_bytes,
            "estimated_savings_percent": round(savings, 1),
            "minimum_savings_percent": options.video_compression_min_savings_percent,
            "likely_kept": savings >= options.video_compression_min_savings_percent,
        }
        if savings < 0:
            warnings.append("按当前参数估算，压缩文件可能比原视频更大；收益保护会阻止保留它。")
        elif savings < options.video_compression_min_savings_percent:
            warnings.append(
                f"预计压缩收益低于 {options.video_compression_min_savings_percent}%，完成后不会保留压缩副本。"
            )

    return {
        "files": files,
        "summary": {
            "file_count": len(files),
            "total_bytes": total_bytes,
            "total_duration_seconds": round(total_duration, 2),
            "total_duration": format_seconds(total_duration) if total_duration else "无媒体时长",
            "estimated_seconds_min": estimate_min,
            "estimated_seconds_max": estimate_max,
            "estimated_disk_bytes": disk_bytes,
            "estimated_output_bytes": output_estimate,
            "output_text_files": 1,
            "output_image_files": options.max_output_images,
            "runtime": (runtime.get("name") or "NVIDIA GPU") if use_gpu else "CPU",
        },
        "model": {
            "name": options.whisper_model,
            "download_bytes": model_download,
            "model_size_bytes": model_size,
            "cached": model_cached,
            "needed": bool(options.transcription_enabled and media_seconds > 0),
        },
        "compression": compression,
        "warnings": warnings,
        "estimate_notice": "耗时、磁盘和压缩体积为本机预估值，实际结果会随编码、内容复杂度和磁盘速度变化。",
    }


def classify_kind(suffix: str) -> str:
    if suffix in VIDEO_EXTENSIONS:
        return "视频"
    if suffix in MEDIA_EXTENSIONS:
        return "音频"
    if suffix in IMAGE_EXTENSIONS:
        return "图片"
    if suffix in ARCHIVE_EXTENSIONS:
        return "压缩包"
    if suffix in DOCUMENT_EXTENSIONS:
        return "文档 / 数据"
    return "自动识别"


def estimate_compressed_size(
    original_bytes: int,
    duration: float,
    source_height: int,
    options: ProcessingOptions,
) -> int:
    if duration <= 0:
        return round(original_bytes * 0.65)
    target_height = min(source_height or options.video_compression_max_height or 1080, options.video_compression_max_height or source_height or 1080)
    baseline_mbps = {480: 0.9, 720: 1.8, 1080: 3.8, 1440: 6.5, 2160: 13.0}
    closest = min(baseline_mbps, key=lambda height: abs(height - target_height))
    quality_scale = 2 ** ((24 - options.video_compression_quality) / 6)
    preset_scale = {"fast": 1.12, "balanced": 1.0, "quality": 0.88}[options.video_compression_preset]
    video_mbps = baseline_mbps[closest] * quality_scale * preset_scale
    total_mbps = video_mbps + options.video_compression_audio_bitrate / 1000
    return max(1, round(duration * total_mbps * 1_000_000 / 8 * 1.03))


def whisper_model_cached(model_name: str) -> bool:
    cache_root = Path(os.getenv("HF_HOME", "")) if os.getenv("HF_HOME") else Path.home() / ".cache" / "huggingface"
    hub = cache_root / "hub"
    expected = f"models--Systran--faster-whisper-{model_name}"
    return (hub / expected).exists()
