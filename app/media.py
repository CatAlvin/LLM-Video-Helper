from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .hardware import InferenceRuntime, cpu_runtime, select_inference_runtime
from .models import ProcessingOptions


_MODEL_CACHE: dict[tuple[str, str, str], object] = {}
_MODEL_LOCK = threading.Lock()


@dataclass(frozen=True)
class VideoCompressionResult:
    path: Path
    encoder: str
    original_bytes: int
    compressed_bytes: int

    @property
    def reduction_percent(self) -> float:
        if self.original_bytes <= 0:
            return 0.0
        return (1 - self.compressed_bytes / self.original_bytes) * 100


def executable_available(name: str) -> bool:
    return shutil.which(name) is not None


@lru_cache(maxsize=16)
def ffmpeg_encoder_available(name: str) -> bool:
    if not executable_available("ffmpeg"):
        return False
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.returncode == 0 and re.search(rf"\b{re.escape(name)}\b", result.stdout) is not None


def format_seconds(seconds: float | int | None) -> str:
    if seconds is None:
        return "未知"
    total = max(0, int(float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def probe_media(path: Path) -> dict:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "FFprobe 无法识别该文件")
    return json.loads(result.stdout)


def media_summary(data: dict) -> tuple[str, bool, bool, float]:
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    duration = float(fmt.get("duration") or 0)
    has_video = any(item.get("codec_type") == "video" for item in streams)
    has_audio = any(item.get("codec_type") == "audio" for item in streams)
    lines = [
        f"容器格式：{fmt.get('format_long_name') or fmt.get('format_name') or '未知'}",
        f"时长：{format_seconds(duration)}",
        f"文件大小：{human_size(int(fmt.get('size') or 0))}",
        f"平均码率：{round(int(fmt.get('bit_rate') or 0) / 1000)} kb/s" if fmt.get("bit_rate") else "平均码率：未知",
    ]
    for index, stream in enumerate(streams, 1):
        kind = stream.get("codec_type", "stream")
        if kind == "video":
            frame_rate = stream.get("avg_frame_rate", "0/0")
            try:
                numerator, denominator = frame_rate.split("/")
                fps = round(float(numerator) / float(denominator), 2) if float(denominator) else 0
            except (ValueError, ZeroDivisionError):
                fps = 0
            lines.append(
                f"视频轨 {index}：{stream.get('codec_name', '未知')}，"
                f"{stream.get('width', '?')}×{stream.get('height', '?')}，{fps or '?'} fps"
            )
        elif kind == "audio":
            lines.append(
                f"音频轨 {index}：{stream.get('codec_name', '未知')}，"
                f"{stream.get('sample_rate', '?')} Hz，{stream.get('channels', '?')} 声道"
            )
        elif kind == "subtitle":
            lines.append(f"字幕轨 {index}：{stream.get('codec_name', '未知')}，语言 {stream.get('tags', {}).get('language', '未知')}")
    tags = fmt.get("tags") or {}
    if tags:
        safe_tags = [f"{key}={value}" for key, value in tags.items() if len(str(value)) < 500]
        if safe_tags:
            lines.append("容器标签：" + "；".join(safe_tags))
    return "\n".join(lines), has_video, has_audio, duration


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def extract_audio(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "无法从文件中提取音频")


def compress_video(
    source: Path,
    target: Path,
    options: ProcessingOptions,
    status: Callable[[str, float | None], None],
) -> VideoCompressionResult:
    """Create an MP4/H.264 derivative, preferring NVENC when requested and available."""
    target.parent.mkdir(parents=True, exist_ok=True)
    device = options.video_compression_device
    if device == "cuda" and not ffmpeg_encoder_available("h264_nvenc"):
        raise RuntimeError("当前 FFmpeg 未提供 NVIDIA NVENC 编码器，无法强制使用 GPU 压缩")

    attempts = ["cuda", "cpu"] if device == "auto" else [device]
    last_error = ""
    for attempt in attempts:
        if attempt == "cuda" and not ffmpeg_encoder_available("h264_nvenc"):
            continue
        encoder_label = "NVIDIA NVENC" if attempt == "cuda" else "CPU libx264"
        status(f"正在使用 {encoder_label} 压缩 {source.name}", None)
        command = build_video_compression_command(source, target, options, attempt)
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if result.returncode == 0 and target.is_file():
            compressed_bytes = target.stat().st_size
            status(
                f"视频压缩完成 · {human_size(source.stat().st_size)} → {human_size(compressed_bytes)} · {encoder_label}",
                None,
            )
            return VideoCompressionResult(target, encoder_label, source.stat().st_size, compressed_bytes)
        last_error = result.stderr.strip() or f"{encoder_label} 编码失败"
        target.unlink(missing_ok=True)
        if device == "auto" and attempt == "cuda":
            status(f"GPU 视频编码不可用，正在自动回退 CPU：{last_error}", None)

    raise RuntimeError(last_error or "视频压缩失败")


def build_video_compression_command(
    source: Path,
    target: Path,
    options: ProcessingOptions,
    device: str,
) -> list[str]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-map_metadata",
        "0",
    ]
    if options.video_compression_max_height:
        command.extend(["-vf", f"scale=-2:'min({options.video_compression_max_height},ih)'"])

    if device == "cuda":
        preset = {"fast": "p3", "balanced": "p5", "quality": "p7"}[options.video_compression_preset]
        command.extend(
            [
                "-c:v",
                "h264_nvenc",
                "-preset",
                preset,
                "-tune",
                "hq",
                "-rc",
                "vbr",
                "-cq",
                str(options.video_compression_quality),
                "-b:v",
                "0",
            ]
        )
    else:
        preset = {"fast": "veryfast", "balanced": "medium", "quality": "slow"}[options.video_compression_preset]
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                preset,
                "-crf",
                str(options.video_compression_quality),
            ]
        )
    command.extend(
        [
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            f"{options.video_compression_audio_bitrate}k",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )
    return command


def transcribe_audio(
    source: Path,
    options: ProcessingOptions,
    status: Callable[[str, float | None], None],
) -> str:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("未安装 faster-whisper，无法执行语音转写") from exc

    runtime = select_inference_runtime(options.processing_device)
    try:
        model = load_whisper_model(WhisperModel, options.whisper_model, runtime, status)
        return run_whisper_transcription(model, source, options, runtime, status)
    except Exception as exc:
        if runtime.device != "cuda":
            raise
        cache_key = (options.whisper_model, runtime.device, runtime.compute_type)
        with _MODEL_LOCK:
            _MODEL_CACHE.pop(cache_key, None)
        if options.processing_device == "cuda":
            raise RuntimeError(f"GPU 转写失败：{exc}") from exc
        status(f"GPU 初始化或推理失败，正在自动回退 CPU：{exc}", 0.0)
        fallback = cpu_runtime()
        model = load_whisper_model(WhisperModel, options.whisper_model, fallback, status)
        return run_whisper_transcription(model, source, options, fallback, status)


def load_whisper_model(
    model_class: object,
    model_name: str,
    runtime: InferenceRuntime,
    status: Callable[[str, float | None], None],
) -> object:
    cache_key = (model_name, runtime.device, runtime.compute_type)
    with _MODEL_LOCK:
        model = _MODEL_CACHE.get(cache_key)
        if model is None:
            status(f"正在加载 Whisper {model_name} 到 {runtime.label}；首次使用会自动下载", None)
            model = model_class(model_name, device=runtime.device, compute_type=runtime.compute_type)
            _MODEL_CACHE[cache_key] = model
    return model


def run_whisper_transcription(
    model: object,
    source: Path,
    options: ProcessingOptions,
    runtime: InferenceRuntime,
    status: Callable[[str, float | None], None],
) -> str:
    status(f"正在使用 {runtime.label} 识别语音并整理时间轴", 0.0)
    language = None if options.language == "auto" else options.language
    segments, info = model.transcribe(
        str(source),
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=True,
    )
    lines = [
        f"计算设备：{runtime.label}",
        f"识别语言：{getattr(info, 'language', options.language)}",
        f"语言置信度：{getattr(info, 'language_probability', 0):.1%}",
        "",
    ]
    count = 0
    started_at = time.monotonic()
    last_progress = -1
    duration = float(getattr(info, "duration", 0) or 0)
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        count += 1
        if options.include_timestamps:
            lines.append(f"[{format_seconds(segment.start)} → {format_seconds(segment.end)}] {text}")
        else:
            lines.append(text)
        if duration > 0:
            fraction = max(0.0, min(float(segment.end) / duration, 0.99))
            percentage = int(fraction * 100)
            if percentage >= last_progress + 2:
                elapsed = time.monotonic() - started_at
                remaining = elapsed * (1 - fraction) / fraction if fraction >= 0.01 else None
                message = f"已转写 {format_seconds(segment.end)} / {format_seconds(duration)}"
                if remaining is not None:
                    message += f" · 预计剩余约 {format_seconds(remaining)}"
                status(message, fraction)
                last_progress = percentage
    if count == 0:
        lines.append("（未检测到可识别语音，可能是静音、纯音乐或语音过短。）")
    status(f"语音转写完成 · 共整理 {count} 个片段", 1.0)
    return "\n".join(lines)


def choose_timestamps(duration: float, options: ProcessingOptions, source: Path) -> list[float]:
    count = options.video_frame_count
    if duration <= 0:
        return [0]
    if options.video_sampling == "interval":
        values = [float(item) for item in range(0, math.ceil(duration), options.frame_interval_seconds)]
        return values[:count] or [0]
    if options.video_sampling == "scene":
        scene_values = detect_scene_changes(source, options.scene_threshold)
        if scene_values:
            return evenly_select(scene_values, count)
    # 首尾各留少量缓冲，避免大量抽到片头黑屏或片尾空帧。
    start = min(1.0, duration * 0.02)
    end = max(start, duration - min(1.0, duration * 0.02))
    if count == 1:
        return [duration / 2]
    return [start + (end - start) * index / (count - 1) for index in range(count)]


def detect_scene_changes(source: Path, threshold: float) -> list[float]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(source),
        "-vf",
        f"select='gt(scene,{threshold})',showinfo",
        "-an",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    values = [float(value) for value in re.findall(r"pts_time:([0-9.]+)", result.stderr)]
    return values


def evenly_select(values: list[float], count: int) -> list[float]:
    if len(values) <= count:
        return values
    if count == 1:
        return [values[len(values) // 2]]
    indices = [round(index * (len(values) - 1) / (count - 1)) for index in range(count)]
    return [values[index] for index in indices]


def extract_video_frames(
    source: Path,
    output_dir: Path,
    duration: float,
    options: ProcessingOptions,
) -> list[tuple[Path, str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[Path, str]] = []
    for index, timestamp in enumerate(choose_timestamps(duration, options, source), 1):
        target = output_dir / f"frame_{index:03d}.png"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({options.output_image_width},iw)':-2",
            str(target),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if completed.returncode == 0 and target.exists():
            results.append((target, f"{source.name} · {format_seconds(timestamp)}"))
    return results
