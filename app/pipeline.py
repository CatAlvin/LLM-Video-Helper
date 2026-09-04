from __future__ import annotations

import math
import shutil
import textwrap
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .extractors import UniversalExtractor, VisualAsset, select_indices
from .models import ProcessingOptions


@dataclass(frozen=True)
class PipelineResult:
    archive_path: Path
    compressed_files: list[Path]
    failed_files: list[str]


def run_pipeline(
    job_id: str,
    job_dir: Path,
    input_paths: list[Path],
    options: ProcessingOptions,
    update: Callable[[int, str, str], None],
) -> PipelineResult:
    update(5, "识别文件", "正在判断文件类型并规划处理路径")

    def extractor_status(message: str, fraction: float | None = None) -> None:
        # 内容提取阶段占总进度的 28%～72%；转写器会持续回传音频时间轴进度。
        progress = 28 if fraction is None else 28 + round(max(0.0, min(fraction, 1.0)) * 44)
        update(progress, "提取内容", message)

    extractor = UniversalExtractor(options, job_dir, extractor_status)
    sections, candidates, warnings, stats = extractor.extract(input_paths)

    update(78, "整理视觉信息", f"已获得 {len(candidates)} 个候选画面，正在筛选并生成拼图")
    result_dir = job_dir / "result"
    result_dir.mkdir(parents=True, exist_ok=True)
    sheets = create_contact_sheets(candidates, result_dir, options)

    update(90, "编排上下文", "正在生成带索引的 TXT 内容清单")
    text_path = result_dir / "context.txt"
    text_path.write_text(
        build_report(job_id, input_paths, options, sections, sheets, warnings, stats),
        encoding="utf-8-sig",
    )

    update(96, "生成结果包", "正在检查输出格式并打包")
    archive_path = job_dir / f"context_pack_{job_id}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(text_path, text_path.name)
        for sheet_path, _ in sheets:
            archive.write(sheet_path, sheet_path.name)

    working_dir = job_dir / "working"
    if working_dir.exists():
        shutil.rmtree(working_dir)
    update(100, "处理完成", f"已生成 1 个 TXT 和 {len(sheets)} 个 PNG")
    return PipelineResult(
        archive_path,
        list(extractor.kept_compressed_files),
        list(extractor.failed_files),
    )


def create_contact_sheets(
    candidates: list[VisualAsset],
    output_dir: Path,
    options: ProcessingOptions,
) -> list[tuple[Path, list[VisualAsset]]]:
    if not candidates or options.max_output_images == 0:
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    capacity = options.max_output_images * options.frames_per_sheet
    if len(candidates) > capacity:
        indices = select_indices(len(candidates), capacity)
        selected = [candidates[index] for index in indices]
    else:
        selected = candidates

    groups = [selected[index : index + options.frames_per_sheet] for index in range(0, len(selected), options.frames_per_sheet)]
    groups = groups[: options.max_output_images]
    sheets: list[tuple[Path, list[VisualAsset]]] = []
    for sheet_index, group in enumerate(groups, 1):
        target = output_dir / f"visual_{sheet_index:02d}.png"
        render_contact_sheet(group, target, options.output_image_width, sheet_index)
        sheets.append((target, group))
    return sheets


def render_contact_sheet(assets: list[VisualAsset], target: Path, width: int, sheet_number: int) -> None:
    count = len(assets)
    if count <= 2:
        columns = 1 if count == 1 else 2
    elif count <= 6:
        columns = 2
    else:
        columns = 3
    rows = math.ceil(count / columns)
    gutter = 18
    outer = 26
    header_height = 66
    label_height = 48
    cell_width = (width - outer * 2 - gutter * (columns - 1)) // columns
    image_height = int(cell_width * 0.62)
    cell_height = image_height + label_height
    height = outer * 2 + header_height + rows * cell_height + (rows - 1) * gutter

    canvas = Image.new("RGB", (width, height), "#F5F2EA")
    draw = ImageDraw.Draw(canvas)
    title_font = load_font(28, bold=True)
    label_font = load_font(17)
    meta_font = load_font(14)
    draw.text((outer, outer), f"视觉上下文 · {sheet_number:02d}", font=title_font, fill="#17201D")
    draw.text((width - outer, outer + 7), f"{count} 个画面", font=meta_font, fill="#66716C", anchor="ra")

    for index, asset in enumerate(assets):
        row, column = divmod(index, columns)
        x = outer + column * (cell_width + gutter)
        y = outer + header_height + row * (cell_height + gutter)
        draw.rounded_rectangle((x, y, x + cell_width, y + cell_height), radius=10, fill="#FFFFFF", outline="#D8D9D2", width=1)
        try:
            with Image.open(asset.path) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                fitted = ImageOps.contain(image, (cell_width - 2, image_height - 2), Image.Resampling.LANCZOS)
                image_area = Image.new("RGB", (cell_width - 2, image_height - 2), "#E8E9E4")
                paste_x = (image_area.width - fitted.width) // 2
                paste_y = (image_area.height - fitted.height) // 2
                image_area.paste(fitted, (paste_x, paste_y))
                canvas.paste(image_area, (x + 1, y + 1))
        except Exception:
            draw.text((x + 16, y + 16), "画面无法渲染", font=label_font, fill="#A0463B")
        label = ellipsize(asset.label, max(18, int(cell_width / 17)))
        draw.text((x + 14, y + image_height + 13), label, font=label_font, fill="#26332E")
    canvas.save(target, "PNG", optimize=True)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def ellipsize(value: str, length: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= length:
        return normalized
    return normalized[: max(1, length - 1)] + "…"


def build_report(
    job_id: str,
    sources: list[Path],
    options: ProcessingOptions,
    sections: list[tuple[str, str]],
    sheets: list[tuple[Path, list[VisualAsset]]],
    warnings: list[str],
    stats: dict,
) -> str:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "知帧 · 多模态上下文包",
        "=" * 72,
        f"任务编号：{job_id}",
        f"生成时间：{timestamp}",
        f"输入文件：{len(sources)} 个",
        f"输出文件：1 个 TXT + {len(sheets)} 个 PNG",
        "",
        "[给 LLM 的使用说明]",
        "1. 先上传本文件 context.txt，它包含来源、技术信息、正文、转写和视觉索引。",
        "2. 再同时上传 visual_*.png（如有）；图片标题与下方“视觉文件索引”一一对应。",
        "3. 可直接向模型说明：‘请综合 TXT 与所有视觉图片回答，引用来源文件名和时间戳/页码，不要把自动 OCR 当作绝对事实。’",
        "4. 本工具只做确定性的抽取与整理，不会擅自用生成式模型改写原始内容。",
        "",
        "[快速索引]",
        f"扫描到的文件（含压缩包成员）：{stats.get('files_seen', 0)}",
        f"成功完成解析：{stats.get('files_extracted', 0)}",
        f"语音转写数量：{stats.get('transcripts', 0)}",
        f"视觉候选数量：{stats.get('visual_candidates', 0)}",
        f"展开的压缩包数量：{stats.get('archives', 0)}",
        f"生成的压缩视频数量：{stats.get('compressed_videos', 0)}",
        "",
        "输入来源：",
    ]
    for path in sources:
        lines.append(f"- {path.name}")

    lines.extend(["", "[处理配置]"])
    config_rows = [
        ("本次上传上限", f"{options.upload_limit_gb:g} GB"),
        ("语音转写", "开启" if options.transcription_enabled else "关闭"),
        ("处理设备", options.processing_device),
        ("Whisper 模型", options.whisper_model),
        ("识别语言", options.language),
        ("转写时间戳", "保留" if options.include_timestamps else "不保留"),
        ("视频取帧策略", options.video_sampling),
        ("视频目标帧数", str(options.video_frame_count)),
        ("视频压缩", "开启" if options.video_compression_enabled else "关闭"),
        ("视频压缩设备", options.video_compression_device),
        ("视频压缩最高高度", "保留原分辨率" if options.video_compression_max_height == 0 else f"{options.video_compression_max_height}p"),
        ("视频压缩质量", str(options.video_compression_quality)),
        ("视频压缩速度", options.video_compression_preset),
        ("压缩后解析来源", options.video_parse_source),
        ("压缩视频保留", "保留" if options.video_keep_compressed else "解析后删除"),
        ("压缩收益保护", f"至少节省 {options.video_compression_min_savings_percent}% 才保留"),
        ("输出 PNG 上限", str(options.max_output_images)),
        ("每张拼图画面数", str(options.frames_per_sheet)),
        ("OCR", "开启" if options.ocr_images else "关闭"),
        ("文档页面预览", "开启" if options.render_documents else "关闭"),
        ("压缩包递归深度", str(options.archive_depth)),
    ]
    lines.extend(f"{label}：{value}" for label, value in config_rows)

    lines.extend(["", "[视觉文件索引]"])
    if not sheets:
        lines.append("本任务未生成 PNG（无视觉内容，或用户将输出图片上限设为 0）。")
    for sheet_path, assets in sheets:
        lines.append(f"{sheet_path.name}：")
        for index, asset in enumerate(assets, 1):
            lines.append(f"  {index}. {asset.label}｜类型：{asset.kind}｜来源：{asset.source}")

    if warnings:
        lines.extend(["", "[处理提示 / 需要人工复核]"])
        lines.extend(f"- {warning}" for warning in warnings)

    lines.extend(["", "=" * 72, "以下为逐文件抽取结果", "=" * 72])
    for number, (title, body) in enumerate(sections, 1):
        lines.extend(["", f"[{number:03d}] {title}", "-" * 72, body.strip()])

    lines.extend(
        [
            "",
            "=" * 72,
            "抽取结束",
            "提示：OCR、语音识别和复杂文档布局可能存在误差；重要结论请回看时间戳、页码与对应 PNG。",
        ]
    )
    return "\n".join(lines) + "\n"
