const state = {
  files: [],
  jobId: null,
  pollingTimer: null,
  taskTimer: null,
  busy: false,
  uploadPaused: false,
  resumeWaiters: [],
  resumeJobId: null,
  serverMaxUploadBytes: 25 * 1024 ** 3,
  uploadChunkBytes: 16 * 1024 ** 2,
  jobs: [],
  taskFilter: "all",
};

const $ = (selector) => document.querySelector(selector);
const elements = {
  dropzone: $("#dropzone"),
  fileInput: $("#fileInput"),
  folderInput: $("#folderInput"),
  chooseFilesButton: $("#chooseFilesButton"),
  chooseFolderButton: $("#chooseFolderButton"),
  fileSection: $("#fileSection"),
  fileList: $("#fileList"),
  fileCount: $("#fileCount"),
  clearFiles: $("#clearFiles"),
  uploadLimitGb: $("#uploadLimitGb"),
  resumeNotice: $("#resumeNotice"),
  resumeNoticeText: $("#resumeNoticeText"),
  cancelResumeButton: $("#cancelResumeButton"),
  startButton: $("#startButton"),
  startButtonLabel: $("#startButtonLabel"),
  startButtonReason: $("#startButtonReason"),
  taskPreset: $("#taskPreset"),
  qualityProfile: $("#qualityProfile"),
  maxImages: $("#maxImages"),
  maxImagesValue: $("#maxImagesValue"),
  previewImageCount: $("#previewImageCount"),
  videoSampling: $("#videoSampling"),
  intervalGroup: $("#intervalGroup"),
  videoCompressionEnabled: $("#videoCompressionEnabled"),
  compressionOptions: $("#compressionOptions"),
  compressionStateLabel: $("#compressionStateLabel"),
  compactModeButton: $("#compactModeButton"),
  runtimeDot: $("#runtimeDot"),
  runtimeTitle: $("#runtimeTitle"),
  runtimeText: $("#runtimeText"),
  preflightPanel: $("#preflightPanel"),
  preflightMetrics: $("#preflightMetrics"),
  preflightFiles: $("#preflightFiles"),
  preflightWarnings: $("#preflightWarnings"),
  preflightNotice: $("#preflightNotice"),
  compressionEstimate: $("#compressionEstimate"),
  confirmStartButton: $("#confirmStartButton"),
  cancelReadyButton: $("#cancelReadyButton"),
  progressPanel: $("#progressPanel"),
  progressState: $("#progressState"),
  progressPercent: $("#progressPercent"),
  progressBar: $("#progressBar"),
  progressStage: $("#progressStage"),
  progressMessage: $("#progressMessage"),
  uploadActions: $("#uploadActions"),
  pauseUploadButton: $("#pauseUploadButton"),
  cancelJobButton: $("#cancelJobButton"),
  eventList: $("#eventList"),
  resultActions: $("#resultActions"),
  downloadButton: $("#downloadButton"),
  compressedDownloads: $("#compressedDownloads"),
  newTaskButton: $("#newTaskButton"),
  errorBox: $("#errorBox"),
  taskFilters: $("#taskFilters"),
  taskList: $("#taskList"),
  taskCenterEmpty: $("#taskCenterEmpty"),
  refreshTasksButton: $("#refreshTasksButton"),
  toast: $("#toast"),
};

const AUDIO = new Set(["mp3", "wav", "m4a", "aac", "flac", "ogg", "opus", "wma", "aiff", "amr", "ape"]);
const VIDEO = new Set(["mp4", "mov", "mkv", "avi", "webm", "wmv", "flv", "m4v", "mpeg", "mpg", "ts", "mts", "m2ts", "3gp"]);
const IMAGE = new Set(["png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff", "avif", "heic"]);
const ARCHIVE = new Set(["zip", "7z", "tar", "gz", "tgz", "bz2", "xz"]);
const DOCUMENT = new Set(["pdf", "docx", "pptx", "xlsx", "xlsm", "csv", "tsv", "txt", "md", "html", "eml"]);

elements.fileInput.addEventListener("change", (event) => {
  addFiles([...event.target.files]);
  event.target.value = "";
});
elements.folderInput.addEventListener("change", (event) => {
  addFiles([...event.target.files]);
  event.target.value = "";
});
elements.chooseFilesButton.addEventListener("click", (event) => {
  event.stopPropagation();
  elements.fileInput.click();
});
elements.chooseFolderButton.addEventListener("click", (event) => {
  event.stopPropagation();
  elements.folderInput.click();
});
elements.dropzone.addEventListener("click", (event) => {
  if (event.target === elements.dropzone || event.target.closest(".drop-icon, .drop-title, .drop-subtitle")) {
    elements.fileInput.click();
  }
});
elements.dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    elements.fileInput.click();
  }
});
for (const eventName of ["dragenter", "dragover"]) {
  elements.dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    event.stopPropagation();
    elements.dropzone.classList.add("is-dragging");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  elements.dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    event.stopPropagation();
    elements.dropzone.classList.remove("is-dragging");
  });
}
elements.dropzone.addEventListener("drop", async (event) => addFiles(await collectDroppedFiles(event.dataTransfer)));
elements.clearFiles.addEventListener("click", clearSelectedFiles);
elements.cancelResumeButton.addEventListener("click", exitResumeMode);
elements.maxImages.addEventListener("input", updateImageRange);
elements.uploadLimitGb.addEventListener("input", renderFiles);
elements.videoSampling.addEventListener("change", () => {
  elements.intervalGroup.hidden = elements.videoSampling.value !== "interval";
});
elements.videoCompressionEnabled.addEventListener("change", updateCompressionOptions);
elements.taskPreset.addEventListener("change", applyTaskPreset);
elements.qualityProfile.addEventListener("change", applyQualityProfile);
elements.startButton.addEventListener("click", startUploadAndPreflight);
elements.confirmStartButton.addEventListener("click", confirmProcessing);
elements.cancelReadyButton.addEventListener("click", cancelCurrentJob);
elements.pauseUploadButton.addEventListener("click", toggleUploadPause);
elements.cancelJobButton.addEventListener("click", cancelCurrentJob);
elements.newTaskButton.addEventListener("click", resetForNewTask);
elements.compactModeButton.addEventListener("click", toggleCompactMode);
elements.refreshTasksButton.addEventListener("click", loadJobs);
elements.taskFilters.addEventListener("click", changeTaskFilter);
elements.taskList.addEventListener("click", handleTaskAction);

async function collectDroppedFiles(dataTransfer) {
  const items = [...(dataTransfer.items || [])];
  const entries = items.map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) return [...dataTransfer.files];
  const collected = [];
  for (const entry of entries) await walkEntry(entry, "", collected);
  return collected;
}

async function walkEntry(entry, parentPath, collected) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    collected.push({ file, relativePath: `${parentPath}${file.name}` });
    return;
  }
  if (!entry.isDirectory) return;
  const reader = entry.createReader();
  let batch;
  do {
    batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    for (const child of batch) await walkEntry(child, `${parentPath}${entry.name}/`, collected);
  } while (batch.length);
}

function addFiles(incoming) {
  if (state.busy) return;
  let added = 0;
  for (const incomingItem of incoming) {
    const file = incomingItem?.file || incomingItem;
    if (!file || !file.name) continue;
    const relativePath = normalizeRelativePath(
      incomingItem?.relativePath || file.webkitRelativePath || file.name,
    );
    const duplicate = state.files.some(
      (item) => item.relativePath === relativePath && item.file.size === file.size && item.file.lastModified === file.lastModified,
    );
    if (!duplicate) {
      state.files.push({ file, relativePath });
      added += 1;
    }
  }
  renderFiles();
  if (!added && incoming.length) showToast("这些文件已经在列表里了");
}

function normalizeRelativePath(path) {
  return String(path || "").replaceAll("\\", "/").replace(/^\/+/, "");
}

function clearSelectedFiles() {
  if (state.busy) return;
  state.files = [];
  renderFiles();
}

function renderFiles() {
  elements.fileList.replaceChildren();
  const totalSize = state.files.reduce((sum, item) => sum + item.file.size, 0);
  const uploadLimit = currentUploadLimitBytes();
  const overLimit = totalSize > uploadLimit;
  elements.fileSection.classList.toggle("is-empty", state.files.length === 0);
  elements.fileSection.classList.toggle("is-over-limit", overLimit);
  elements.clearFiles.hidden = state.files.length === 0 || state.busy;
  elements.fileCount.textContent = state.files.length
    ? `${state.files.length} 个文件，${formatBytes(totalSize)}${overLimit ? `，超过 ${formatBytes(uploadLimit)} 上限` : ""}`
    : "还没有文件";

  state.files.slice(0, 200).forEach((item, index) => {
    const type = classifyFile(item.file.name);
    const row = document.createElement("li");
    row.className = "file-item";
    const badge = document.createElement("span");
    badge.className = `file-type ${type.className}`;
    badge.textContent = type.label;
    const details = document.createElement("div");
    details.className = "file-details";
    const name = document.createElement("strong");
    name.textContent = item.relativePath;
    name.title = item.relativePath;
    const meta = document.createElement("small");
    meta.textContent = `${type.description}，${formatBytes(item.file.size)}`;
    details.append(name, meta);
    const remove = document.createElement("button");
    remove.className = "remove-file";
    remove.type = "button";
    remove.disabled = state.busy;
    remove.setAttribute("aria-label", `移除 ${item.relativePath}`);
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      state.files.splice(index, 1);
      renderFiles();
    });
    row.append(badge, details, remove);
    elements.fileList.append(row);
  });
  if (state.files.length > 200) {
    const remainder = document.createElement("li");
    remainder.className = "file-overflow-note";
    remainder.textContent = `另有 ${state.files.length - 200} 个文件已加入，任务中会全部处理。`;
    elements.fileList.append(remainder);
  }

  const disabled = state.files.length === 0 || state.busy || overLimit;
  elements.startButton.disabled = disabled;
  elements.startButtonLabel.textContent = state.resumeJobId ? "继续缺失分片" : "上传并预检";
  if (state.busy) elements.startButtonReason.textContent = "当前任务尚未结束，可在任务中心查看进度";
  else if (!state.files.length) elements.startButtonReason.textContent = state.resumeJobId ? "请重新选择原文件或原文件夹" : "请先添加文件或文件夹";
  else if (overLimit) elements.startButtonReason.textContent = "文件总量超过本次上传上限";
  else if (state.resumeJobId) elements.startButtonReason.textContent = "只会补传服务器缺失的分片";
  else elements.startButtonReason.textContent = "上传完成后先显示时长、耗时、空间与输出预估";
}

function classifyFile(filename) {
  const extension = filename.includes(".") ? filename.split(".").pop().toLowerCase() : "file";
  const label = extension.slice(0, 5) || "FILE";
  if (VIDEO.has(extension)) return { className: "video", label, description: "视频" };
  if (AUDIO.has(extension)) return { className: "audio", label, description: "音频" };
  if (IMAGE.has(extension)) return { className: "image", label, description: "图片" };
  if (ARCHIVE.has(extension)) return { className: "archive", label, description: "压缩包" };
  if (DOCUMENT.has(extension)) return { className: "document", label, description: "文档 / 数据" };
  return { className: "", label, description: "自动识别" };
}

function updateImageRange() {
  const value = Number(elements.maxImages.value);
  elements.maxImagesValue.value = value === 0 ? "不输出" : `${value} 张`;
  elements.maxImagesValue.textContent = value === 0 ? "不输出" : `${value} 张`;
  elements.previewImageCount.textContent = value === 0 ? "0" : `≤ ${value}`;
  elements.maxImages.style.background = `linear-gradient(to right, var(--accent) 0 ${value * 10}%, var(--line) ${value * 10}% 100%)`;
}

function updateCompressionOptions() {
  const enabled = elements.videoCompressionEnabled.checked;
  elements.compressionOptions.hidden = !enabled;
  elements.compressionStateLabel.textContent = `视频压缩：${enabled ? "开启" : "关闭"}`;
  elements.videoCompressionEnabled.setAttribute("aria-checked", String(enabled));
}

function applyTaskPreset() {
  const preset = elements.taskPreset.value;
  const presets = {
    general: { images: 6, frames: 12, pages: 12, sampling: "balanced", transcription: true, ocr: true },
    meeting: { images: 2, frames: 4, pages: 8, sampling: "balanced", transcription: true, ocr: true },
    course: { images: 8, frames: 18, pages: 20, sampling: "scene", transcription: true, ocr: true },
    video: { images: 10, frames: 24, pages: 8, sampling: "scene", transcription: true, ocr: true },
    research: { images: 6, frames: 12, pages: 30, sampling: "balanced", transcription: true, ocr: true },
    ocr: { images: 10, frames: 4, pages: 30, sampling: "balanced", transcription: false, ocr: true },
  };
  const values = presets[preset];
  elements.maxImages.value = values.images;
  $("#videoFrameCount").value = values.frames;
  $("#maxDocumentPages").value = values.pages;
  elements.videoSampling.value = values.sampling;
  $("#transcriptionEnabled").checked = values.transcription;
  $("#ocrImages").checked = values.ocr;
  elements.intervalGroup.hidden = values.sampling !== "interval";
  updateImageRange();
}

function applyQualityProfile() {
  const profile = elements.qualityProfile.value;
  const values = {
    fast: { model: "base", frames: 8, pages: 8 },
    balanced: { model: "small", frames: 12, pages: 12 },
    precise: { model: "large-v3", frames: 24, pages: 24 },
  }[profile];
  setRadio("whisperModel", values.model);
  $("#videoFrameCount").value = Math.max(Number($("#videoFrameCount").value), values.frames);
  $("#maxDocumentPages").value = Math.max(Number($("#maxDocumentPages").value), values.pages);
}

function setRadio(name, value) {
  const input = document.querySelector(`input[name="${name}"][value="${value}"]`);
  if (input) input.checked = true;
}

function currentUploadLimitGb() {
  const serverMaxGb = state.serverMaxUploadBytes / 1024 ** 3;
  const parsed = Number.parseFloat(elements.uploadLimitGb.value);
  return Math.max(0.1, Math.min(serverMaxGb, Number.isFinite(parsed) ? parsed : serverMaxGb));
}

function currentUploadLimitBytes() {
  return currentUploadLimitGb() * 1024 ** 3;
}

function collectOptions() {
  return {
    upload_limit_gb: currentUploadLimitGb(),
    task_preset: elements.taskPreset.value,
    quality_profile: elements.qualityProfile.value,
    transcription_enabled: $("#transcriptionEnabled").checked,
    processing_device: document.querySelector('input[name="processingDevice"]:checked').value,
    whisper_model: document.querySelector('input[name="whisperModel"]:checked').value,
    language: $("#language").value,
    include_timestamps: $("#includeTimestamps").checked,
    video_sampling: elements.videoSampling.value,
    video_frame_count: boundedNumber("#videoFrameCount", 1, 60),
    frame_interval_seconds: boundedNumber("#frameInterval", 2, 3600),
    scene_threshold: Number($("#sceneThreshold").value),
    video_compression_enabled: elements.videoCompressionEnabled.checked,
    video_compression_device: $("#videoCompressionDevice").value,
    video_compression_max_height: Number($("#videoCompressionMaxHeight").value),
    video_compression_quality: boundedNumber("#videoCompressionQuality", 18, 35),
    video_compression_preset: $("#videoCompressionPreset").value,
    video_compression_audio_bitrate: boundedNumber("#videoCompressionAudioBitrate", 32, 320),
    video_parse_source: $("#videoParseSource").value,
    video_keep_compressed: $("#videoKeepCompressed").value === "true",
    video_compression_min_savings_percent: boundedNumber("#videoCompressionMinSavings", 0, 80),
    max_output_images: Number(elements.maxImages.value),
    frames_per_sheet: Number($("#framesPerSheet").value),
    output_image_width: 1600,
    ocr_images: $("#ocrImages").checked,
    render_documents: $("#renderDocuments").checked,
    max_document_pages: boundedNumber("#maxDocumentPages", 1, 50),
    archive_depth: Number($("#archiveDepth").value),
    max_archive_files: 100,
    table_max_rows: boundedNumber("#tableRows", 10, 5000),
    table_max_columns: 40,
    include_metadata: $("#includeMetadata").checked,
  };
}

function boundedNumber(selector, min, max) {
  const input = $(selector);
  const parsed = Number.parseInt(input.value, 10);
  const value = Math.max(min, Math.min(max, Number.isFinite(parsed) ? parsed : min));
  input.value = value;
  return value;
}

async function startUploadAndPreflight() {
  if (!state.files.length || state.busy) return;
  const totalSize = state.files.reduce((sum, item) => sum + item.file.size, 0);
  if (totalSize > currentUploadLimitBytes()) {
    showToast("文件总量超过本次上传上限");
    renderFiles();
    return;
  }
  state.busy = true;
  state.uploadPaused = false;
  renderFiles();
  elements.preflightPanel.hidden = true;
  elements.progressPanel.hidden = false;
  elements.resultActions.hidden = true;
  elements.errorBox.hidden = true;
  elements.uploadActions.hidden = false;
  elements.pauseUploadButton.textContent = "暂停上传";
  elements.progressPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });

  try {
    let uploadStatus;
    if (state.resumeJobId) {
      const response = await apiFetch(`/api/uploads/${state.resumeJobId}`);
      state.jobId = state.resumeJobId;
      uploadStatus = response.upload;
      updateProgress(uploadStatus.progress, "正在恢复上传", "正在核对已完成分片");
    } else {
      const job = await apiFetch("/api/uploads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          files: state.files.map((item) => ({
            name: item.file.name,
            relative_path: item.relativePath,
            size: item.file.size,
            content_type: item.file.type || "",
            last_modified: item.file.lastModified || 0,
          })),
          options: collectOptions(),
        }),
      });
      state.jobId = job.id;
      uploadStatus = job.upload;
      localStorage.setItem("contextkitActiveUpload", job.id);
      renderJob(job);
    }
    await uploadMissingChunks(state.jobId, uploadStatus);
    updateProgress(100, "正在预检", "上传已校验，正在读取媒体时长与资源预估");
    const readyJob = await apiFetch(`/api/uploads/${state.jobId}/complete`, { method: "POST" });
    localStorage.removeItem("contextkitActiveUpload");
    exitResumeMode(false);
    renderJob(readyJob);
    await loadJobs();
  } catch (error) {
    state.busy = false;
    elements.uploadActions.hidden = true;
    showFailure(`${error.message || "上传失败"}。上传会话仍保留，可在任务中心重新选择原文件并继续。`);
    renderFiles();
    await loadJobs();
  }
}

async function uploadMissingChunks(jobId, uploadStatus) {
  const localFiles = matchLocalFiles(uploadStatus.files);
  for (const remoteFile of uploadStatus.files) {
    const uploaded = new Set(remoteFile.uploaded_chunks || []);
    if (uploaded.size === remoteFile.total_chunks) continue;
    const local = localFiles.get(remoteFile.id);
    if (!local) throw new Error(`没有重新选择到 ${remoteFile.relative_path}`);
    for (let index = 0; index < remoteFile.total_chunks; index += 1) {
      if (uploaded.has(index)) continue;
      await waitIfPaused();
      const start = index * uploadStatus.chunk_size;
      const chunk = local.file.slice(start, Math.min(local.file.size, start + uploadStatus.chunk_size));
      const digest = await sha256Hex(chunk);
      let response;
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        try {
          response = await apiFetch(`/api/uploads/${jobId}/files/${remoteFile.id}/chunks/${index}`, {
            method: "PUT",
            headers: { "X-Chunk-SHA256": digest, "Content-Type": "application/octet-stream" },
            body: chunk,
          });
          break;
        } catch (error) {
          if (attempt === 3) throw error;
          updateProgress(
            uploadStatus.progress,
            "分片重试中",
            `${remoteFile.relative_path} 的第 ${index + 1} 块校验或传输失败，正在第 ${attempt + 1} 次尝试`,
          );
          await delay(attempt * 500);
        }
      }
      uploadStatus = response.upload;
      updateProgress(
        uploadStatus.progress,
        state.uploadPaused ? "上传已暂停" : "正在分片上传",
        `${formatBytes(uploadStatus.uploaded_bytes)} / ${formatBytes(uploadStatus.total_bytes)}，当前 ${remoteFile.relative_path}`,
      );
    }
  }
}

function matchLocalFiles(remoteFiles) {
  const result = new Map();
  const used = new Set();
  for (const remote of remoteFiles) {
    let index = state.files.findIndex(
      (item, itemIndex) => !used.has(itemIndex) && item.relativePath === remote.relative_path && item.file.size === remote.size,
    );
    if (index < 0) {
      index = state.files.findIndex(
        (item, itemIndex) => !used.has(itemIndex) && item.file.name === remote.name && item.file.size === remote.size,
      );
    }
    if (index >= 0) {
      used.add(index);
      result.set(remote.id, state.files[index]);
    }
  }
  return result;
}

async function sha256Hex(blob) {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function toggleUploadPause() {
  state.uploadPaused = !state.uploadPaused;
  elements.pauseUploadButton.textContent = state.uploadPaused ? "继续上传" : "暂停上传";
  if (state.uploadPaused) updateProgress(Number(elements.progressPercent.textContent.replace("%", "")), "上传已暂停", "已完成分片安全保留，继续时不会重传");
  else {
    for (const resolve of state.resumeWaiters.splice(0)) resolve();
    updateProgress(Number(elements.progressPercent.textContent.replace("%", "")), "正在继续上传", "从下一个缺失分片继续");
  }
}

function waitIfPaused() {
  if (!state.uploadPaused) return Promise.resolve();
  return new Promise((resolve) => state.resumeWaiters.push(resolve));
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function confirmProcessing() {
  if (!state.jobId) return;
  elements.confirmStartButton.disabled = true;
  try {
    const job = await apiFetch(`/api/jobs/${state.jobId}/start`, { method: "POST" });
    elements.preflightPanel.hidden = true;
    elements.progressPanel.hidden = false;
    renderJob(job);
    pollJob();
    await loadJobs();
  } catch (error) {
    showToast(error.message || "无法开始处理");
  } finally {
    elements.confirmStartButton.disabled = false;
  }
}

async function pollJob() {
  clearTimeout(state.pollingTimer);
  if (!state.jobId) return;
  try {
    const job = await apiFetch(`/api/jobs/${state.jobId}`, { cache: "no-store" });
    renderJob(job);
    if (["uploading", "queued", "processing"].includes(job.state)) {
      state.pollingTimer = setTimeout(pollJob, 1000);
    }
  } catch (error) {
    state.pollingTimer = setTimeout(pollJob, 2500);
    elements.progressMessage.textContent = "暂时无法读取状态，正在自动重连";
  }
}

function renderJob(job) {
  const labels = {
    uploading: "正在上传", ready: "等待确认", queued: "等待中", processing: "正在处理",
    completed: "已经完成", failed: "处理失败", cancelled: "已取消",
  };
  state.jobId = job.id;
  elements.progressState.textContent = labels[job.state] || job.state;
  updateProgress(job.progress || 0, job.stage || "准备处理", job.message || "");
  elements.eventList.replaceChildren();
  for (const event of (job.events || []).slice(-7)) {
    const item = document.createElement("li");
    item.textContent = event.text;
    elements.eventList.append(item);
  }

  elements.uploadActions.hidden = job.state !== "uploading";
  if (job.state === "ready") {
    state.busy = true;
    elements.progressPanel.hidden = true;
    renderPreflight(job);
  } else if (["uploading", "queued", "processing"].includes(job.state)) {
    state.busy = true;
    elements.preflightPanel.hidden = true;
    elements.progressPanel.hidden = false;
  } else if (job.state === "completed") {
    state.busy = false;
    elements.preflightPanel.hidden = true;
    elements.progressPanel.hidden = false;
    elements.resultActions.hidden = false;
    elements.downloadButton.href = `/api/jobs/${job.id}/download`;
    renderCompressedDownloads(job);
    elements.errorBox.hidden = true;
    renderFiles();
  } else if (["failed", "cancelled"].includes(job.state)) {
    state.busy = false;
    elements.preflightPanel.hidden = true;
    elements.progressPanel.hidden = false;
    showFailure(job.error || job.message || (job.state === "cancelled" ? "任务已取消" : "处理失败"));
  }
}

function renderPreflight(job) {
  const preflight = job.preflight || {};
  const summary = preflight.summary || {};
  elements.preflightPanel.hidden = false;
  elements.preflightMetrics.replaceChildren();
  const metrics = [
    ["文件", `${summary.file_count || 0} 个`],
    ["媒体时长", summary.total_duration || "无"],
    ["预计耗时", formatDurationRange(summary.estimated_seconds_min, summary.estimated_seconds_max)],
    ["预计磁盘", formatBytes(summary.estimated_disk_bytes || 0)],
    ["首次模型下载", preflight.model?.download_bytes ? formatBytes(preflight.model.download_bytes) : "无需下载"],
    ["预计输出", `1 TXT，最多 ${summary.output_image_files || 0} PNG`],
  ];
  for (const [label, value] of metrics) {
    const wrapper = document.createElement("div");
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = label;
    detail.textContent = value;
    wrapper.append(term, detail);
    elements.preflightMetrics.append(wrapper);
  }

  const compression = preflight.compression;
  elements.compressionEstimate.hidden = !compression;
  if (compression) {
    const savings = Number(compression.estimated_savings_percent || 0);
    elements.compressionEstimate.classList.toggle("warning", savings < compression.minimum_savings_percent);
    elements.compressionEstimate.textContent = savings >= 0
      ? `压缩预估：${formatBytes(compression.original_bytes)} → ${formatBytes(compression.estimated_bytes)}，约节省 ${savings.toFixed(1)}%。最低保留门槛为 ${compression.minimum_savings_percent}%。`
      : `压缩预估：可能增加 ${Math.abs(savings).toFixed(1)}%。收益保护会阻止保留该压缩副本。`;
  }

  elements.preflightFiles.replaceChildren();
  for (const file of (preflight.files || []).slice(0, 8)) {
    const row = document.createElement("li");
    const name = document.createElement("strong");
    const meta = document.createElement("span");
    name.textContent = file.name;
    meta.textContent = [file.kind, formatBytes(file.size), file.duration, ...(file.details || [])].filter(Boolean).join("，");
    row.append(name, meta);
    elements.preflightFiles.append(row);
  }
  if ((preflight.files || []).length > 8) {
    const more = document.createElement("li");
    more.textContent = `另有 ${preflight.files.length - 8} 个文件已完成预检`;
    elements.preflightFiles.append(more);
  }
  const warnings = preflight.warnings || [];
  elements.preflightWarnings.hidden = warnings.length === 0;
  elements.preflightWarnings.textContent = warnings.join(" ");
  elements.preflightNotice.textContent = preflight.estimate_notice || "预估值会随内容和磁盘速度变化。";
  elements.preflightPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  renderFiles();
}

function renderCompressedDownloads(job) {
  elements.compressedDownloads.replaceChildren();
  const files = job.compressed_files || [];
  elements.compressedDownloads.hidden = files.length === 0;
  for (const file of files) {
    const link = document.createElement("a");
    link.className = "secondary-button compressed-download-button";
    link.href = `/api/jobs/${job.id}/compressed/${encodeURIComponent(file.name)}`;
    link.download = file.name;
    link.textContent = `下载压缩视频：${file.name}，${formatBytes(file.size)}`;
    elements.compressedDownloads.append(link);
  }
}

function updateProgress(percent, stage, message) {
  const normalized = Math.max(0, Math.min(100, Number(percent) || 0));
  elements.progressPercent.textContent = `${normalized}%`;
  elements.progressBar.style.width = `${normalized}%`;
  elements.progressStage.textContent = stage;
  elements.progressMessage.textContent = message;
}

function showFailure(message) {
  clearTimeout(state.pollingTimer);
  elements.progressState.textContent = "需要处理";
  elements.progressStage.textContent = "这次没有完成";
  elements.progressMessage.textContent = "源文件和已上传分片仍保留在本机。";
  elements.errorBox.textContent = message;
  elements.errorBox.hidden = false;
  elements.resultActions.hidden = true;
  elements.uploadActions.hidden = true;
  renderFiles();
}

async function cancelCurrentJob() {
  if (!state.jobId) return;
  try {
    const job = await apiFetch(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
    state.uploadPaused = false;
    for (const resolve of state.resumeWaiters.splice(0)) resolve();
    renderJob(job);
    await loadJobs();
  } catch (error) {
    showToast(error.message || "无法取消任务");
  }
}

function resetForNewTask() {
  clearTimeout(state.pollingTimer);
  state.busy = false;
  state.jobId = null;
  state.files = [];
  exitResumeMode(false);
  elements.progressPanel.hidden = true;
  elements.preflightPanel.hidden = true;
  elements.resultActions.hidden = true;
  elements.errorBox.hidden = true;
  elements.compressedDownloads.hidden = true;
  elements.compressedDownloads.replaceChildren();
  renderFiles();
  elements.dropzone.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function loadJobs() {
  try {
    state.jobs = await apiFetch("/api/jobs", { cache: "no-store" });
    renderTaskCenter();
  } catch {
    elements.taskCenterEmpty.textContent = "暂时无法读取任务记录，请确认本地服务仍在运行。";
    elements.taskCenterEmpty.hidden = false;
  }
}

function renderTaskCenter() {
  elements.taskList.replaceChildren();
  const filtered = state.jobs.filter((job) => {
    if (state.taskFilter === "active") return ["uploading", "ready", "queued", "processing"].includes(job.state);
    if (state.taskFilter === "completed") return job.state === "completed";
    if (state.taskFilter === "failed") return ["failed", "cancelled"].includes(job.state);
    return true;
  });
  elements.taskCenterEmpty.hidden = filtered.length > 0;
  for (const job of filtered) elements.taskList.append(createTaskCard(job));
}

function createTaskCard(job) {
  const card = document.createElement("article");
  card.className = `task-card state-${job.state}`;
  const header = document.createElement("div");
  header.className = "task-card-main";
  const content = document.createElement("div");
  const title = document.createElement("h3");
  const names = (job.files || []).map((file) => file.relative_path || file.name);
  title.textContent = names.length ? `${names[0]}${names.length > 1 ? ` 等 ${names.length} 个文件` : ""}` : `任务 ${job.id}`;
  const meta = document.createElement("p");
  meta.textContent = `${job.stage || "等待处理"}，更新于 ${formatLocalTime(job.updated_at)}`;
  content.append(title, meta);
  const badge = document.createElement("span");
  badge.className = `state-badge ${job.state}`;
  badge.textContent = ({ uploading: "上传中", ready: "待确认", queued: "等待中", processing: "处理中", completed: "已完成", failed: "失败", cancelled: "已取消" })[job.state] || job.state;
  header.append(content, badge);
  card.append(header);
  if (["uploading", "queued", "processing"].includes(job.state)) {
    const progress = document.createElement("div");
    progress.className = "task-progress";
    const bar = document.createElement("span");
    bar.style.width = `${job.progress || 0}%`;
    progress.append(bar);
    card.append(progress);
  }
  const actions = document.createElement("div");
  actions.className = "task-actions";
  if (job.state === "uploading") {
    actions.append(taskButton("选择文件恢复", "resume-files", job.id), taskButton("选择文件夹恢复", "resume-folder", job.id), taskButton("取消", "cancel", job.id));
  } else if (job.state === "ready") {
    actions.append(taskButton("查看预检", "open", job.id), taskButton("开始处理", "start", job.id), taskButton("取消", "cancel", job.id));
  } else if (["queued", "processing"].includes(job.state)) {
    actions.append(taskButton("查看进度", "open", job.id), taskButton("取消", "cancel", job.id));
  } else if (job.state === "completed") {
    actions.append(taskLink("下载", `/api/jobs/${job.id}/download`), taskButton("重新处理", "retry", job.id));
    if ((job.failed_files || []).length) actions.append(taskButton("只重试失败文件", "retry-failed", job.id));
    actions.append(taskButton("清理", "delete", job.id, true));
  } else {
    actions.append(taskButton("重新处理", "retry", job.id));
    if ((job.failed_files || []).length) actions.append(taskButton("只重试失败文件", "retry-failed", job.id));
    actions.append(taskButton("清理", "delete", job.id, true));
  }
  card.append(actions);
  return card;
}

function taskButton(label, action, jobId, danger = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.dataset.action = action;
  button.dataset.jobId = jobId;
  if (danger) button.className = "danger-link";
  return button;
}

function taskLink(label, href) {
  const link = document.createElement("a");
  link.textContent = label;
  link.href = href;
  return link;
}

async function handleTaskAction(event) {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const { action, jobId } = target.dataset;
  try {
    if (action === "resume-files" || action === "resume-folder") {
      beginResume(jobId, action === "resume-folder");
      return;
    }
    if (action === "open") {
      const job = await apiFetch(`/api/jobs/${jobId}`);
      state.jobId = jobId;
      renderJob(job);
      if (["queued", "processing"].includes(job.state)) pollJob();
      return;
    }
    if (action === "start") {
      state.jobId = jobId;
      const job = await apiFetch(`/api/jobs/${jobId}/start`, { method: "POST" });
      renderJob(job);
      pollJob();
    } else if (action === "cancel") {
      const job = await apiFetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
      if (state.jobId === jobId) renderJob(job);
    } else if (action === "retry" || action === "retry-failed") {
      const job = await apiFetch(`/api/jobs/${jobId}/retry?failed_only=${action === "retry-failed"}`, { method: "POST" });
      state.jobId = job.id;
      renderJob(job);
      pollJob();
    } else if (action === "delete") {
      if (!window.confirm("清理后将删除该任务的源文件、结果与记录，且无法恢复。确定继续吗？")) return;
      await apiFetch(`/api/jobs/${jobId}`, { method: "DELETE" });
      if (state.jobId === jobId) resetForNewTask();
    }
    await loadJobs();
  } catch (error) {
    showToast(error.message || "任务操作失败");
  }
}

function beginResume(jobId, useFolderPicker) {
  state.busy = false;
  state.resumeJobId = jobId;
  state.files = [];
  elements.resumeNotice.hidden = false;
  elements.resumeNoticeText.textContent = "请重新选择原文件。文件内容不会重复上传，只会补传服务器缺失的分片。";
  renderFiles();
  elements.dropzone.scrollIntoView({ behavior: "smooth", block: "center" });
  if (useFolderPicker) elements.folderInput.click();
  else elements.fileInput.click();
}

function exitResumeMode(render = true) {
  state.resumeJobId = null;
  elements.resumeNotice.hidden = true;
  if (render) renderFiles();
}

function changeTaskFilter(event) {
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  state.taskFilter = button.dataset.filter;
  for (const item of elements.taskFilters.querySelectorAll("button")) item.classList.toggle("active", item === button);
  renderTaskCenter();
}

function toggleCompactMode() {
  const compact = !document.body.classList.contains("compact-home");
  document.body.classList.toggle("compact-home", compact);
  elements.compactModeButton.textContent = compact ? "显示介绍" : "隐藏介绍";
  elements.compactModeButton.setAttribute("aria-pressed", String(compact));
  localStorage.setItem("contextkitCompactHome", compact ? "1" : "0");
}

function restoreCompactMode() {
  if (localStorage.getItem("contextkitCompactHome") !== "1") return;
  document.body.classList.add("compact-home");
  elements.compactModeButton.textContent = "显示介绍";
  elements.compactModeButton.setAttribute("aria-pressed", "true");
}

async function checkRuntime() {
  try {
    const data = await apiFetch("/api/health", { cache: "no-store" });
    if (Number.isFinite(data.max_upload_bytes) && data.max_upload_bytes > 0) {
      state.serverMaxUploadBytes = data.max_upload_bytes;
      state.uploadChunkBytes = data.upload_chunk_bytes || state.uploadChunkBytes;
      const serverMaxGb = data.max_upload_bytes / 1024 ** 3;
      elements.uploadLimitGb.max = serverMaxGb.toFixed(1).replace(/\.0$/, "");
      if (Number(elements.uploadLimitGb.value) > serverMaxGb) elements.uploadLimitGb.value = elements.uploadLimitGb.max;
      renderFiles();
    }
    const missing = Object.entries(data.features || {}).filter(([, ready]) => !ready).map(([name]) => name);
    if (!data.ffmpeg) missing.unshift("FFmpeg");
    const gpu = data.gpu || {};
    $("#cudaDeviceOption").disabled = !gpu.available;
    if (gpu.available) {
      const shortName = (gpu.name || "NVIDIA GPU").replace(/^NVIDIA GeForce\s+/i, "");
      $("#gpuDeviceName").textContent = shortName;
      $("#gpuDeviceMeta").textContent = "CUDA FP16";
    } else {
      $("#gpuDeviceName").textContent = "GPU 不可用";
      $("#gpuDeviceMeta").textContent = "将使用 CPU";
    }
    if (missing.length) {
      elements.runtimeDot.className = "runtime-dot limited";
      elements.runtimeTitle.textContent = "部分能力尚未安装";
      elements.runtimeText.textContent = `缺少：${missing.join("、")}。普通文本仍可处理。`;
    } else if (gpu.available) {
      elements.runtimeDot.className = "runtime-dot";
      elements.runtimeTitle.textContent = `GPU 加速已就绪：${(gpu.name || "NVIDIA GPU").replace(/^NVIDIA GeForce\s+/i, "")}`;
      const total = gpu.memory_total_mb ? `${(gpu.memory_total_mb / 1024).toFixed(0)} GB 显存` : "CUDA FP16";
      elements.runtimeText.textContent = `Whisper 默认使用 CUDA FP16，${total}。分片上传与预检已启用。`;
    } else {
      elements.runtimeDot.className = "runtime-dot";
      elements.runtimeTitle.textContent = "CPU 处理能力已就绪";
      elements.runtimeText.textContent = `GPU 暂不可用：${gpu.reason || "未检测到兼容设备"}。`;
    }
  } catch {
    elements.runtimeDot.className = "runtime-dot limited";
    elements.runtimeTitle.textContent = "无法读取运行状态";
    elements.runtimeText.textContent = "请确认本地服务仍在运行，然后刷新页面。";
  }
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 204) return null;
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) throw new Error(readApiError(payload, `服务器返回 ${response.status}`));
  return payload;
}

function readApiError(payload, fallback) {
  if (!payload) return fallback;
  if (typeof payload === "string") return payload;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) return payload.detail.map((item) => item.msg).join("；");
  return fallback;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** index;
  return `${value >= 100 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

function formatDurationRange(minimum, maximum) {
  if (!Number.isFinite(minimum) || !Number.isFinite(maximum)) return "等待估算";
  return `${formatDuration(minimum)} 至 ${formatDuration(maximum)}`;
}

function formatDuration(seconds) {
  if (seconds < 60) return `约 ${Math.max(1, Math.round(seconds))} 秒`;
  if (seconds < 3600) return `约 ${Math.round(seconds / 60)} 分钟`;
  return `约 ${(seconds / 3600).toFixed(1)} 小时`;
}

function formatLocalTime(value) {
  if (!value) return "未知时间";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

let toastTimer;
function showToast(message) {
  clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.add("show");
  toastTimer = setTimeout(() => elements.toast.classList.remove("show"), 3200);
}

restoreCompactMode();
updateImageRange();
updateCompressionOptions();
renderFiles();
checkRuntime();
loadJobs();
state.taskTimer = setInterval(loadJobs, 5000);
