from __future__ import annotations

import tempfile
import unittest
import io
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from PIL import Image
from pydantic import ValidationError

from app.extractors import UniversalExtractor, VisualAsset, copy_with_limit, safe_member_path, select_indices
from app.hardware import InferenceRuntime, select_inference_runtime
from app.models import ProcessingOptions
from app.media import _MODEL_CACHE, VideoCompressionResult, transcribe_audio
from app.pipeline import create_contact_sheets
from app.preflight import estimate_compressed_size


class CoreTests(unittest.TestCase):
    def test_defaults_stay_within_output_contract(self) -> None:
        options = ProcessingOptions()
        self.assertLessEqual(options.max_output_images, 10)
        self.assertGreaterEqual(options.frames_per_sheet, 1)
        self.assertEqual(options.whisper_model, "small")
        self.assertEqual(options.processing_device, "auto")
        self.assertEqual(options.upload_limit_gb, 25.0)
        self.assertFalse(options.video_compression_enabled)
        self.assertEqual(options.video_parse_source, "compressed")
        self.assertEqual(options.video_compression_min_savings_percent, 10)

    def test_upload_limit_cannot_exceed_server_product_limit(self) -> None:
        with self.assertRaises(ValidationError):
            ProcessingOptions(upload_limit_gb=25.1)

    def test_auto_device_prefers_cuda_fp16(self) -> None:
        fake_gpu = {"available": True, "name": "NVIDIA GeForce RTX 4090"}
        with patch("app.hardware.gpu_status", return_value=fake_gpu):
            runtime = select_inference_runtime("auto")
        self.assertEqual(runtime.device, "cuda")
        self.assertEqual(runtime.compute_type, "float16")

    def test_archive_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "archive"
            root.mkdir()
            with self.assertRaises(RuntimeError):
                safe_member_path(root, "../../outside.txt")
            valid = safe_member_path(root, "folder/readme.txt")
            self.assertTrue(valid.is_relative_to(root))

    def test_index_selection_covers_beginning_and_end(self) -> None:
        self.assertEqual(select_indices(100, 3), [0, 50, 99])
        self.assertEqual(select_indices(2, 10), [0, 1])

    def test_stream_decompression_limit_is_enforced(self) -> None:
        source = io.BytesIO(b"0123456789")
        destination = io.BytesIO()
        with self.assertRaises(RuntimeError):
            copy_with_limit(source, destination, 5)

    def test_transcription_reports_timeline_progress(self) -> None:
        class FakeModel:
            def transcribe(self, *_args, **_kwargs):
                segments = iter(
                    [
                        SimpleNamespace(start=0.0, end=25.0, text=" 第一段"),
                        SimpleNamespace(start=25.0, end=50.0, text=" 第二段"),
                        SimpleNamespace(start=50.0, end=100.0, text=" 第三段"),
                    ]
                )
                return segments, SimpleNamespace(language="zh", language_probability=0.99, duration=100.0)

        updates = []
        cache_key = ("tiny", "cpu", "int8")
        previous = _MODEL_CACHE.get(cache_key)
        _MODEL_CACHE[cache_key] = FakeModel()
        try:
            result = transcribe_audio(
                Path("unused.wav"),
                ProcessingOptions(whisper_model="tiny", processing_device="cpu"),
                lambda message, fraction=None: updates.append((message, fraction)),
            )
        finally:
            if previous is None:
                _MODEL_CACHE.pop(cache_key, None)
            else:
                _MODEL_CACHE[cache_key] = previous

        fractions = [fraction for _, fraction in updates if fraction is not None]
        self.assertEqual(fractions[0], 0.0)
        self.assertEqual(fractions[-1], 1.0)
        self.assertTrue(any(0 < fraction < 1 for fraction in fractions))
        self.assertIn("第一段", result)

    def test_auto_device_falls_back_to_cpu_after_gpu_error(self) -> None:
        class BrokenGpuModel:
            def transcribe(self, *_args, **_kwargs):
                raise RuntimeError("CUDA test failure")

        class WorkingCpuModel:
            def transcribe(self, *_args, **_kwargs):
                segment = SimpleNamespace(start=0.0, end=2.0, text=" CPU 回退成功")
                info = SimpleNamespace(language="zh", language_probability=1.0, duration=2.0)
                return iter([segment]), info

        gpu_key = ("tiny", "cuda", "float16")
        cpu_key = ("tiny", "cpu", "int8")
        previous_gpu = _MODEL_CACHE.get(gpu_key)
        previous_cpu = _MODEL_CACHE.get(cpu_key)
        _MODEL_CACHE[gpu_key] = BrokenGpuModel()
        _MODEL_CACHE[cpu_key] = WorkingCpuModel()
        updates = []
        try:
            with patch(
                "app.media.select_inference_runtime",
                return_value=InferenceRuntime("cuda", "float16", "Test GPU · CUDA FP16"),
            ):
                result = transcribe_audio(
                    Path("unused.wav"),
                    ProcessingOptions(whisper_model="tiny", processing_device="auto"),
                    lambda message, fraction=None: updates.append((message, fraction)),
                )
        finally:
            for key, previous in ((gpu_key, previous_gpu), (cpu_key, previous_cpu)):
                if previous is None:
                    _MODEL_CACHE.pop(key, None)
                else:
                    _MODEL_CACHE[key] = previous

        self.assertIn("CPU 回退成功", result)
        self.assertIn("计算设备：CPU · INT8", result)
        self.assertTrue(any("回退 CPU" in message for message, _ in updates))

    def test_contact_sheets_never_exceed_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = []
            for index in range(20):
                path = root / f"source_{index}.png"
                Image.new("RGB", (320, 180), (index * 7 % 255, 80, 110)).save(path)
                assets.append(VisualAsset(path, f"画面 {index}", "sample.mp4", "视频帧"))
            options = ProcessingOptions(max_output_images=2, frames_per_sheet=4, output_image_width=800)
            sheets = create_contact_sheets(assets, root / "result", options)
            self.assertEqual(len(sheets), 2)
            self.assertTrue(all(path.exists() for path, _ in sheets))
            self.assertLessEqual(sum(len(items) for _, items in sheets), 8)

    def test_compression_estimate_changes_with_quality(self) -> None:
        high_quality = ProcessingOptions(video_compression_quality=18)
        small_file = ProcessingOptions(video_compression_quality=32)
        source_size = 2 * 1024**3
        high_estimate = estimate_compressed_size(source_size, 3600, 1080, high_quality)
        small_estimate = estimate_compressed_size(source_size, 3600, 1080, small_file)
        self.assertGreater(high_estimate, small_estimate)

    def test_low_savings_compressed_copy_is_not_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            input_dir.mkdir()
            source = input_dir / "sample.mp4"
            source.write_bytes(b"0" * 1000)
            media_data = {
                "format": {"duration": "1", "size": "1000"},
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 320, "height": 180}],
            }

            def fake_compress(_source, target, _options, _status):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"1" * 950)
                return VideoCompressionResult(target, "test", 1000, 950)

            options = ProcessingOptions(
                transcription_enabled=False,
                max_output_images=0,
                video_compression_enabled=True,
                video_keep_compressed=True,
                video_compression_min_savings_percent=10,
            )
            extractor = UniversalExtractor(options, root, lambda *_args: None)
            with (
                patch("app.extractors.shutil.which", return_value="available"),
                patch("app.extractors.probe_media", return_value=media_data),
                patch("app.extractors.compress_video", side_effect=fake_compress),
            ):
                extractor.extract([source])

            self.assertEqual(extractor.kept_compressed_files, [])
            self.assertTrue(any("低于 10% 门槛" in warning for warning in extractor.warnings))
            self.assertFalse(any((root / "compressed").glob("*.mp4")))


if __name__ == "__main__":
    unittest.main()
