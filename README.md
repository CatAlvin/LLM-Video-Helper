# 知帧 ContextKit

将视频、音频、图片、文档、表格和压缩包整理成适合交给多模态 LLM 的上下文包。

## 版本与进度

- 当前版本：**1.1.0**
- 状态：可用的本地应用，支持分片上传、处理前预检、任务恢复和结果打包
- 技术栈：FastAPI、FFmpeg、faster-whisper、RapidOCR、Pillow

## 输出

- `context.txt`：来源、媒体信息、正文、表格、转写、OCR 与页码/时间戳。
- `visual_01.png` ～ `visual_10.png`：视频帧、PDF 页面和图片组成的视觉索引。
- 最终结果会打包为 ZIP，可直接提供给 ChatGPT、Claude、Gemini 或本地多模态模型。

## 支持内容

- 视频、音频和常见图片格式
- PDF、DOCX、PPTX、XLSX、CSV、TSV
- ZIP、7Z、TAR、GZ
- 文本、代码、HTML、EML 和字幕

## 使用方式

Windows 需要 Python 3.10+，并确保 `ffmpeg` 与 `ffprobe` 可在命令行使用。

最简单的方式是双击 `start.bat`，或运行：

```powershell
.\setup.ps1
.\start.ps1
```

打开 <http://127.0.0.1:8765>，选择文件与处理质量，查看预检结果后开始处理并下载 ZIP。

也可以手动启动：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

## AI 辅助

应用使用 Whisper 完成语音识别、RapidOCR 提取图片文字；预处理阶段不调用生成式模型改写或总结原内容。开发过程使用 AI 辅助需求拆解、实现和测试，最终工作流由作者确认。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```
