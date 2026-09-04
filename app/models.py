from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ProcessingOptions(BaseModel):
    upload_limit_gb: float = Field(default=25.0, ge=0.1, le=25.0)

    task_preset: Literal["general", "meeting", "course", "video", "research", "ocr"] = "general"
    quality_profile: Literal["fast", "balanced", "precise"] = "balanced"

    transcription_enabled: bool = True
    processing_device: Literal["auto", "cuda", "cpu"] = "auto"
    whisper_model: Literal["tiny", "base", "small", "medium", "large-v3"] = "small"
    language: str = "auto"
    include_timestamps: bool = True

    video_sampling: Literal["balanced", "scene", "interval"] = "balanced"
    video_frame_count: int = Field(default=12, ge=1, le=60)
    frame_interval_seconds: int = Field(default=30, ge=2, le=3600)
    scene_threshold: float = Field(default=0.32, ge=0.1, le=0.8)

    video_compression_enabled: bool = False
    video_compression_device: Literal["auto", "cuda", "cpu"] = "auto"
    video_compression_max_height: int = Field(default=1080, ge=0, le=4320)
    video_compression_quality: int = Field(default=24, ge=18, le=35)
    video_compression_preset: Literal["fast", "balanced", "quality"] = "balanced"
    video_compression_audio_bitrate: int = Field(default=128, ge=32, le=320)
    video_parse_source: Literal["original", "compressed"] = "compressed"
    video_keep_compressed: bool = False
    video_compression_min_savings_percent: int = Field(default=10, ge=0, le=80)

    max_output_images: int = Field(default=6, ge=0, le=10)
    frames_per_sheet: int = Field(default=6, ge=1, le=9)
    output_image_width: int = Field(default=1600, ge=800, le=2400)

    ocr_images: bool = True
    render_documents: bool = True
    max_document_pages: int = Field(default=12, ge=1, le=50)
    archive_depth: int = Field(default=2, ge=0, le=4)
    max_archive_files: int = Field(default=100, ge=1, le=500)
    table_max_rows: int = Field(default=300, ge=10, le=5000)
    table_max_columns: int = Field(default=40, ge=5, le=200)
    include_metadata: bool = True

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        value = value.strip().lower()
        if value == "auto":
            return value
        if not (2 <= len(value) <= 10 and value.replace("-", "").isalpha()):
            raise ValueError("语言应为 auto 或 zh、en、ja 这样的语言代码")
        return value


class JobView(BaseModel):
    id: str
    state: Literal["uploading", "ready", "queued", "processing", "completed", "failed", "cancelled"]
    progress: int = 0
    stage: str = "等待处理"
    message: str = ""
    created_at: str
    updated_at: str
    files: list[dict] = []
    events: list[dict] = []
    result_name: str | None = None
    compressed_files: list[dict] = []
    failed_files: list[str] = []
    preflight: dict | None = None
    upload: dict | None = None
    cancel_requested: bool = False
    source_job_id: str | None = None
    error: str | None = None


class UploadFileSpec(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    relative_path: str = Field(default="", max_length=1000)
    size: int = Field(ge=0)
    content_type: str = Field(default="", max_length=200)
    last_modified: int = Field(default=0, ge=0)


class UploadInitRequest(BaseModel):
    files: list[UploadFileSpec] = Field(min_length=1, max_length=10_000)
    options: ProcessingOptions = Field(default_factory=ProcessingOptions)
