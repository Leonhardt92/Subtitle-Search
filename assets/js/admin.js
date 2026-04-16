const adminSummary = document.querySelector("#adminSummary");
const adminLog = document.querySelector("#adminLog");
const newVideoCategoryInput = document.querySelector("#newVideoCategoryInput");
const newVideoTitleInput = document.querySelector("#newVideoTitleInput");
const newVideoSourceUrlInput = document.querySelector("#newVideoSourceUrlInput");
const subtitleIdInput = document.querySelector("#subtitleIdInput");
const videoIdInput = document.querySelector("#videoIdInput");
const cueIndexInput = document.querySelector("#cueIndexInput");
const startSecondsInput = document.querySelector("#startSecondsInput");
const endSecondsInput = document.querySelector("#endSecondsInput");
const textInput = document.querySelector("#textInput");
const createVideoButton = document.querySelector("#createVideoButton");
const loadSubtitleButton = document.querySelector("#loadSubtitleButton");
const saveSubtitleButton = document.querySelector("#saveSubtitleButton");
const syncVideoButton = document.querySelector("#syncVideoButton");
const generateClipButton = document.querySelector("#generateClipButton");
const generateEmbeddingButton = document.querySelector("#generateEmbeddingButton");

function setLog(message) {
  adminLog.textContent = message;
}

function readForm() {
  return {
    subtitleId: subtitleIdInput.value.trim(),
    videoId: videoIdInput.value.trim(),
    cueIndex: cueIndexInput.value.trim(),
    startSeconds: startSecondsInput.value.trim(),
    endSeconds: endSecondsInput.value.trim(),
    text: textInput.value,
  };
}

function fillForm(record) {
  subtitleIdInput.value = record.id ?? "";
  videoIdInput.value = record.videoId ?? "";
  cueIndexInput.value = record.cueIndex ?? "";
  startSecondsInput.value = record.startSeconds ?? "";
  endSecondsInput.value = record.endSeconds ?? "";
  textInput.value = record.text ?? "";
  adminSummary.textContent = `当前视频：${record.videoTitle} · video_id=${record.videoId}`;
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `Request failed: ${response.status}`);
  }
  return data;
}

async function loadSubtitle() {
  const subtitleId = subtitleIdInput.value.trim();
  if (!subtitleId) {
    setLog("请先输入 subtitle_id。");
    return;
  }

  setLog("正在读取字幕...");
  try {
    const data = await requestJson(`./api/admin/subtitle?subtitle_id=${encodeURIComponent(subtitleId)}`);
    fillForm(data.record);
    setLog(JSON.stringify(data.record, null, 2));
  } catch (error) {
    setLog(String(error));
  }
}

async function createVideo() {
  const title = newVideoTitleInput.value.trim();
  const category = newVideoCategoryInput.value;
  const sourceUrl = newVideoSourceUrlInput.value.trim();

  if (!title) {
    setLog("请先填写新增视频标题。");
    return;
  }

  setLog("正在新增视频并同步...");
  try {
    const data = await requestJson("./api/admin/video", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, category, sourceUrl }),
    });
    videoIdInput.value = data.record.id;
    subtitleIdInput.value = "";
    adminSummary.textContent = data.duplicateWarning
      ? `已新增视频：${data.record.title} · video_id=${data.record.id} · 检测到疑似重复视频`
      : `已新增视频：${data.record.title} · video_id=${data.record.id}`;
    setLog(JSON.stringify(data, null, 2));
  } catch (error) {
    setLog(String(error));
  }
}

async function saveSubtitle() {
  const payload = readForm();
  if (!payload.videoId || !payload.cueIndex || !payload.startSeconds || !payload.endSeconds || !payload.text.trim()) {
    setLog("video_id、cue_index、start_seconds、end_seconds、text 都要填。");
    return;
  }

  setLog("正在保存字幕...");
  try {
    const data = await requestJson("./api/admin/subtitle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    subtitleIdInput.value = data.subtitleId;
    adminSummary.textContent = `已保存 subtitle_id=${data.subtitleId} · video_id=${data.videoId}`;
    setLog(JSON.stringify(data, null, 2));
  } catch (error) {
    setLog(String(error));
  }
}

async function generateForVideo(action) {
  const videoId = videoIdInput.value.trim();
  if (!videoId) {
    setLog("请先填写 video_id。");
    return;
  }

  setLog(`正在生成 ${action}...`);
  try {
    const data = await requestJson("./api/admin/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, videoId }),
    });
    setLog(`${JSON.stringify(data, null, 2)}`);
  } catch (error) {
    setLog(String(error));
  }
}

createVideoButton.addEventListener("click", () => {
  void createVideo();
});

loadSubtitleButton.addEventListener("click", () => {
  void loadSubtitle();
});

saveSubtitleButton.addEventListener("click", () => {
  void saveSubtitle();
});

syncVideoButton.addEventListener("click", () => {
  void generateForVideo("sync");
});

generateClipButton.addEventListener("click", () => {
  void generateForVideo("clip");
});

generateEmbeddingButton.addEventListener("click", () => {
  void generateForVideo("embedding");
});
