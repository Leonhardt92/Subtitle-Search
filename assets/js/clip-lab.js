const srcInput = document.querySelector("#srcInput");
const loadButton = document.querySelector("#loadButton");
const fitButton = document.querySelector("#fitButton");
const detectBlackBarsButton = document.querySelector("#detectBlackBarsButton");
const video = document.querySelector("#video");
const cropBox = document.querySelector("#cropBox");
const resizeHandle = document.querySelector("#resizeHandle");
const videoMeta = document.querySelector("#videoMeta");
const startInput = document.querySelector("#startInput");
const endInput = document.querySelector("#endInput");
const xInput = document.querySelector("#xInput");
const yInput = document.querySelector("#yInput");
const wInput = document.querySelector("#wInput");
const hInput = document.querySelector("#hInput");
const seekStartButton = document.querySelector("#seekStartButton");
const useCurrentButton = document.querySelector("#useCurrentButton");
const seekEndButton = document.querySelector("#seekEndButton");
const useCurrentEndButton = document.querySelector("#useCurrentEndButton");
const exportGifButton = document.querySelector("#exportGifButton");
const exportStatus = document.querySelector("#exportStatus");
const cropCanvas = document.querySelector("#cropCanvas");
const detectionCanvas = document.createElement("canvas");
const detectionCtx = detectionCanvas.getContext("2d", { willReadFrequently: true });

const ctx = cropCanvas.getContext("2d");
const url = new URL(window.location.href);
const initialSrc = url.searchParams.get("src");
const initialTime = Number(url.searchParams.get("t") || "");

const state = {
  dragMode: null,
  pointerStartX: 0,
  pointerStartY: 0,
  boxStart: { x: 0, y: 0, w: 320, h: 180 },
};

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function getVideoRect() {
  const rect = video.getBoundingClientRect();
  const videoWidth = video.videoWidth || 1;
  const videoHeight = video.videoHeight || 1;
  const elementRatio = rect.width / rect.height;
  const videoRatio = videoWidth / videoHeight;

  let drawWidth = rect.width;
  let drawHeight = rect.height;
  let offsetX = 0;
  let offsetY = 0;

  if (videoRatio > elementRatio) {
    drawHeight = rect.width / videoRatio;
    offsetY = (rect.height - drawHeight) / 2;
  } else {
    drawWidth = rect.height * videoRatio;
    offsetX = (rect.width - drawWidth) / 2;
  }

  return {
    left: rect.left + offsetX,
    top: rect.top + offsetY,
    width: drawWidth,
    height: drawHeight,
  };
}

function getCrop() {
  return {
    x: Number(xInput.value) || 0,
    y: Number(yInput.value) || 0,
    w: Math.max(1, Number(wInput.value) || 1),
    h: Math.max(1, Number(hInput.value) || 1),
  };
}

function setCrop(x, y, w, h) {
  const maxW = Math.max(1, video.videoWidth || w);
  const maxH = Math.max(1, video.videoHeight || h);
  const safeW = clamp(Math.round(w), 1, maxW);
  const safeH = clamp(Math.round(h), 1, maxH);
  const safeX = clamp(Math.round(x), 0, maxW - safeW);
  const safeY = clamp(Math.round(y), 0, maxH - safeH);
  xInput.value = safeX;
  yInput.value = safeY;
  wInput.value = safeW;
  hInput.value = safeH;
  updateCropBox();
  renderCropPreview();
  updateCommand();
}

function updateCropBox() {
  if (!video.videoWidth || !video.videoHeight) {
    cropBox.style.display = "none";
    return;
  }

  const crop = getCrop();
  const rect = getVideoRect();
  cropBox.style.display = "block";
  cropBox.style.left = `${(crop.x / video.videoWidth) * rect.width}px`;
  cropBox.style.top = `${(crop.y / video.videoHeight) * rect.height}px`;
  cropBox.style.width = `${(crop.w / video.videoWidth) * rect.width}px`;
  cropBox.style.height = `${(crop.h / video.videoHeight) * rect.height}px`;
}

function formatSeconds(value) {
  return Number((Number(value) || 0).toFixed(3));
}

function updateMeta() {
  if (!video.videoWidth || !video.videoHeight) {
    videoMeta.textContent = "还没加载视频。";
    return;
  }

  videoMeta.textContent =
    `源分辨率 ${video.videoWidth} × ${video.videoHeight} · 时长 ${formatSeconds(video.duration)}s · 当前时间 ${formatSeconds(video.currentTime)}s`;
}

function renderCropPreview() {
  const crop = getCrop();
  const canvasWidth = cropCanvas.width;
  const targetHeight = Math.round((crop.h / crop.w) * canvasWidth) || cropCanvas.height;
  cropCanvas.height = clamp(targetHeight, 120, 720);
  ctx.clearRect(0, 0, cropCanvas.width, cropCanvas.height);

  if (!video.videoWidth || video.readyState < 2) {
    ctx.fillStyle = "#111";
    ctx.fillRect(0, 0, cropCanvas.width, cropCanvas.height);
    return;
  }

  ctx.drawImage(video, crop.x, crop.y, crop.w, crop.h, 0, 0, cropCanvas.width, cropCanvas.height);
}

function updateCommand() {
  return;
}

function setStatus(message, link) {
  exportStatus.innerHTML = "";
  if (link) {
    exportStatus.innerHTML = `${message} <a class="download-link" href="${link}" download>下载 GIF</a>`;
    return;
  }
  exportStatus.textContent = message;
}

function fitCropToVideo() {
  if (!video.videoWidth || !video.videoHeight) {
    return;
  }
  setCrop(0, 0, video.videoWidth, video.videoHeight);
}

function detectBlackBarsOnCurrentFrame() {
  if (!video.videoWidth || video.readyState < 2) {
    setStatus("请先加载视频并停在想分析的那一帧。");
    return;
  }

  const maxDetectWidth = 960;
  const scale = Math.min(1, maxDetectWidth / video.videoWidth);
  detectionCanvas.width = Math.max(1, Math.round(video.videoWidth * scale));
  detectionCanvas.height = Math.max(1, Math.round(video.videoHeight * scale));
  detectionCtx.drawImage(video, 0, 0, detectionCanvas.width, detectionCanvas.height);

  const { data, width, height } = detectionCtx.getImageData(0, 0, detectionCanvas.width, detectionCanvas.height);
  const step = 1;
  const blackThreshold = 20;
  const rowCounts = new Array(height).fill(0);
  const colCounts = new Array(width).fill(0);

  for (let y = 0; y < height; y += step) {
    for (let x = 0; x < width; x += step) {
      const offset = (y * width + x) * 4;
      const r = data[offset];
      const g = data[offset + 1];
      const b = data[offset + 2];
      const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b;

      if (luminance > blackThreshold) {
        rowCounts[y] += 1;
        colCounts[x] += 1;
      }
    }
  }

  const minRowPixels = Math.max(8, Math.floor(width * 0.08));
  const minColPixels = Math.max(8, Math.floor(height * 0.08));
  const consecutiveRows = Math.max(6, Math.floor(height * 0.015));
  const consecutiveCols = Math.max(6, Math.floor(width * 0.015));

  function findLeadingEdge(counts, threshold, required) {
    let streak = 0;
    for (let index = 0; index < counts.length; index += 1) {
      if (counts[index] >= threshold) {
        streak += 1;
        if (streak >= required) {
          return index - required + 1;
        }
      } else {
        streak = 0;
      }
    }
    return -1;
  }

  function findTrailingEdge(counts, threshold, required) {
    let streak = 0;
    for (let index = counts.length - 1; index >= 0; index -= 1) {
      if (counts[index] >= threshold) {
        streak += 1;
        if (streak >= required) {
          return index + required - 1;
        }
      } else {
        streak = 0;
      }
    }
    return -1;
  }

  let minY = findLeadingEdge(rowCounts, minRowPixels, consecutiveRows);
  let maxY = findTrailingEdge(rowCounts, minRowPixels, consecutiveRows);
  let minX = findLeadingEdge(colCounts, minColPixels, consecutiveCols);
  let maxX = findTrailingEdge(colCounts, minColPixels, consecutiveCols);

  if (minX < 0 || minY < 0 || maxX < minX || maxY < minY) {
    setStatus("这一帧没有识别到明显的非黑内容。");
    return;
  }

  const denseRowPixels = Math.max(minRowPixels, Math.floor(width * 0.2));
  const denseBottom = findTrailingEdge(rowCounts, denseRowPixels, consecutiveRows);
  if (denseBottom >= minY && denseBottom < maxY) {
    maxY = denseBottom;
  }

  const pad = 4;
  minX = Math.max(0, minX - pad);
  minY = Math.max(0, minY - pad);
  maxX = Math.min(width - 1, maxX + pad);
  maxY = Math.min(height - 1, maxY + pad);

  const x = Math.round(minX / scale);
  const y = Math.round(minY / scale);
  const w = Math.round((maxX - minX + 1) / scale);
  const h = Math.round((maxY - minY + 1) / scale);

  setCrop(x, y, w, h);
  setStatus(`已按当前帧自动识别黑边：x=${x}, y=${y}, w=${w}, h=${h}`);
}

function loadVideo() {
  const src = srcInput.value.trim();
  if (!src) {
    return;
  }
  video.src = src;
  video.load();
}

function beginDrag(event, mode) {
  if (!video.videoWidth || !video.videoHeight) {
    return;
  }

  event.preventDefault();
  state.dragMode = mode;
  state.pointerStartX = event.clientX;
  state.pointerStartY = event.clientY;
  state.boxStart = getCrop();
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", endDrag, { once: true });
}

function onPointerMove(event) {
  if (!state.dragMode) {
    return;
  }

  const rect = getVideoRect();
  const scaleX = video.videoWidth / rect.width;
  const scaleY = video.videoHeight / rect.height;
  const dx = (event.clientX - state.pointerStartX) * scaleX;
  const dy = (event.clientY - state.pointerStartY) * scaleY;

  if (state.dragMode === "move") {
    setCrop(state.boxStart.x + dx, state.boxStart.y + dy, state.boxStart.w, state.boxStart.h);
    return;
  }

  setCrop(state.boxStart.x, state.boxStart.y, state.boxStart.w + dx, state.boxStart.h + dy);
}

function endDrag() {
  state.dragMode = null;
  window.removeEventListener("pointermove", onPointerMove);
}

cropBox.addEventListener("pointerdown", (event) => {
  if (event.target === resizeHandle) {
    return;
  }
  beginDrag(event, "move");
});

resizeHandle.addEventListener("pointerdown", (event) => {
  beginDrag(event, "resize");
});

[startInput, endInput, xInput, yInput, wInput, hInput, srcInput].forEach((element) => {
  element.addEventListener("input", () => {
    if (element === xInput || element === yInput || element === wInput || element === hInput) {
      updateCropBox();
      renderCropPreview();
    }
    updateCommand();
  });
});

loadButton.addEventListener("click", loadVideo);
fitButton.addEventListener("click", fitCropToVideo);
detectBlackBarsButton.addEventListener("click", detectBlackBarsOnCurrentFrame);
seekStartButton.addEventListener("click", () => {
  video.currentTime = formatSeconds(startInput.value);
});
useCurrentButton.addEventListener("click", () => {
  startInput.value = formatSeconds(video.currentTime);
  updateCommand();
});
seekEndButton.addEventListener("click", () => {
  video.currentTime = formatSeconds(endInput.value);
});
useCurrentEndButton.addEventListener("click", () => {
  endInput.value = formatSeconds(video.currentTime);
  updateCommand();
});
exportGifButton.addEventListener("click", async () => {
  const start = formatSeconds(startInput.value);
  const end = formatSeconds(endInput.value);
  const crop = getCrop();
  if (end <= start) {
    setStatus("结束秒数必须大于开始秒数。");
    return;
  }

  setStatus("正在生成 GIF...");
  try {
    const response = await fetch("./api/clip-lab/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        src: srcInput.value.trim(),
        start,
        end,
        x: crop.x,
        y: crop.y,
        w: crop.w,
        h: crop.h,
      }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `Export failed: ${response.status}`);
    }
    setStatus("GIF 已生成。", data.url);
  } catch (error) {
    setStatus(String(error));
  }
});

video.addEventListener("loadedmetadata", () => {
  if (!(Number(wInput.value) > 0) || !(Number(hInput.value) > 0)) {
    fitCropToVideo();
  } else {
    updateCropBox();
  }
  if (Number.isFinite(initialTime)) {
    const clampedTime = clamp(initialTime, 0, video.duration || initialTime);
    video.currentTime = clampedTime;
    startInput.value = formatSeconds(clampedTime);
  }
  endInput.value = formatSeconds(video.duration);
  updateMeta();
  renderCropPreview();
  updateCommand();
  setStatus("视频已加载，可以直接裁剪并导出 GIF。");
});

video.addEventListener("timeupdate", () => {
  updateMeta();
  renderCropPreview();
});

window.addEventListener("resize", updateCropBox);

if (initialSrc) {
  srcInput.value = initialSrc;
  loadVideo();
} else {
  srcInput.value = "http://127.0.0.1:4173/clips/示例.mp4";
  updateCommand();
}
