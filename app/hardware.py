from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal


@dataclass(frozen=True)
class InferenceRuntime:
    device: Literal["cuda", "cpu"]
    compute_type: str
    label: str


@lru_cache(maxsize=1)
def gpu_status() -> dict:
    status = {
        "available": False,
        "device_count": 0,
        "name": None,
        "memory_total_mb": None,
        "memory_free_mb": None,
        "driver_version": None,
        "compute_types": [],
        "recommended_compute_type": None,
        "reason": None,
    }
    try:
        import ctranslate2

        device_count = ctranslate2.get_cuda_device_count()
        compute_types = sorted(ctranslate2.get_supported_compute_types("cuda")) if device_count else []
        status["device_count"] = device_count
        status["compute_types"] = compute_types
        status["recommended_compute_type"] = "float16" if "float16" in compute_types else None
        status["available"] = bool(device_count and status["recommended_compute_type"])
        if not device_count:
            status["reason"] = "CTranslate2 没有检测到 CUDA 设备"
        elif not status["recommended_compute_type"]:
            status["reason"] = "显卡不支持高效 FP16 推理"
    except Exception as exc:
        status["reason"] = f"CUDA 检测失败：{exc}"

    try:
        command = [
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if result.returncode == 0 and result.stdout.strip():
            name, driver, total, free = [part.strip() for part in result.stdout.splitlines()[0].split(",", 3)]
            status.update(
                {
                    "name": name,
                    "driver_version": driver,
                    "memory_total_mb": int(float(total)),
                    "memory_free_mb": int(float(free)),
                }
            )
    except (OSError, ValueError):
        pass
    return status


def select_inference_runtime(preference: Literal["auto", "cuda", "cpu"]) -> InferenceRuntime:
    if preference == "cpu":
        return cpu_runtime()
    gpu = gpu_status()
    if gpu["available"]:
        name = gpu.get("name") or "NVIDIA GPU"
        return InferenceRuntime("cuda", "float16", f"{name} · CUDA FP16")
    if preference == "cuda":
        raise RuntimeError(gpu.get("reason") or "CUDA GPU 当前不可用")
    return cpu_runtime()


def cpu_runtime() -> InferenceRuntime:
    return InferenceRuntime("cpu", "int8", "CPU · INT8")
