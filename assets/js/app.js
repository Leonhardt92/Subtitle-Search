const summaryText = document.querySelector("#summaryText");
const resultsTitle = document.querySelector("#resultsTitle");
const resultList = document.querySelector("#resultList");
const searchInput = document.querySelector("#searchInput");
const searchHumorButton = document.querySelector("#searchHumorButton");
const searchWritingButton = document.querySelector("#searchWritingButton");
const semanticHumorButton = document.querySelector("#semanticHumorButton");
const semanticWritingButton = document.querySelector("#semanticWritingButton");
const playerDock = document.querySelector("#playerDock");
const playerDockVideo = document.querySelector("#playerDockVideo");
const playerDockHomeParent = playerDock ? playerDock.parentElement : null;
const playerDockHomeNextSibling = playerDock ? playerDock.nextElementSibling : null;
const PLAYER_STEP_SECONDS = 0.3;
const DEFAULT_VISIBLE_ITEMS = 5;

const state = {
  records: [],
  lastQuery: "",
  lastCategory: "humor",
  lastSearchMode: "keyword",
  expandedVideos: new Set(),
  feedbackStatus: new Map(),
  mediaAspects: new Map(),
};

const playerState = {
  record: null,
  videoId: null,
  videoPath: "",
  currentTime: 0,
  pendingSeek: null,
  shouldAutoplayAfterSeek: false,
  activeRecordId: null,
  copyStatusTimer: null,
};

async function fetchMeta() {
  const response = await fetch("./api/meta");

  if (!response.ok) {
    throw new Error(`Failed to load metadata: ${response.status}`);
  }

  return response.json();
}

function getCategoryLabel(category) {
  return category === "writing" ? "文笔" : "幽默";
}

async function searchRecords(query, category) {
  const params = new URLSearchParams({
    q: query,
    whole_word: "0",
    category,
  });
  const response = await fetch(`./api/search?${params.toString()}`);

  if (!response.ok) {
    throw new Error(`Failed to search subtitles: ${response.status}`);
  }

  const data = await response.json();
  return Array.isArray(data.records) ? data.records : [];
}

async function semanticSearchRecords(query, category) {
  const params = new URLSearchParams({
    q: query,
    category,
  });
  const response = await fetch(`./api/semantic-search?${params.toString()}`);

  if (!response.ok) {
    throw new Error(`Failed to run semantic search: ${response.status}`);
  }

  const data = await response.json();
  return Array.isArray(data.records) ? data.records : [];
}

async function submitSearchFeedback(payload) {
  const response = await fetch("./api/search-feedback", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `Failed to save feedback: ${response.status}`);
  }

  return data;
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function formatClock(totalSeconds) {
  const wholeSeconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(wholeSeconds / 3600);
  const minutes = Math.floor((wholeSeconds % 3600) / 60);
  const seconds = wholeSeconds % 60;

  if (hours > 0) {
    return [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":");
  }

  return [minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":");
}

function formatSecondsLabel(totalSeconds) {
  return `${Number(totalSeconds || 0).toFixed(3)}s`;
}

function formatSecondsCopyValue(totalSeconds) {
  return Number(totalSeconds || 0).toFixed(3);
}

function buildHighlightedHtml(text, query) {
  const escapedText = escapeHtml(text);
  const tokens = query
    .trim()
    .split(/\s+/)
    .map((token) => token.trim())
    .filter(Boolean);

  if (tokens.length === 0) {
    return escapedText;
  }

  const pattern = tokens.map((token) => escapeRegExp(token)).join("|");
  return escapedText.replace(new RegExp(`(${pattern})`, "giu"), "<mark>$1</mark>");
}

function renderEmpty(message) {
  dismissPlayerDock();
  resultList.innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
}

function getPlayerClockButton() {
  return document.querySelector("[data-player-clock]");
}

function getPlayerCopyStatusNode() {
  return document.querySelector("[data-player-copy-status]");
}

function setResultMediaAspectRatio(mediaNode, width, height) {
  if (!mediaNode || !Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return;
  }

  mediaNode.style.setProperty("--result-media-aspect", `${width} / ${height}`);
  mediaNode.classList.toggle("is-portrait", height > width);
  mediaNode.classList.toggle("is-landscape", width >= height);
}

function rememberMediaAspect(recordId, width, height) {
  if (!recordId || !Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return;
  }

  state.mediaAspects.set(String(recordId), { width, height });
}

function getMediaAspectStyle(recordId) {
  const aspect = state.mediaAspects.get(String(recordId));
  if (!aspect) {
    return "";
  }

  return ` style="--result-media-aspect: ${aspect.width} / ${aspect.height};"`;
}

function applyPreviewAspectRatios() {
  resultList.querySelectorAll("img.result-line-preview-image").forEach((image) => {
    const mediaNode = image.closest(".result-line-media");
    if (!mediaNode) {
      return;
    }

    const applyAspect = () => {
      if (image.naturalWidth > 0 && image.naturalHeight > 0) {
        setResultMediaAspectRatio(mediaNode, image.naturalWidth, image.naturalHeight);
        const recordId = image.closest("[data-record-id]")?.dataset.recordId;
        rememberMediaAspect(recordId, image.naturalWidth, image.naturalHeight);
      }
    };

    if (image.complete) {
      applyAspect();
      return;
    }

    image.addEventListener("load", applyAspect, { once: true });
  });
}

function groupRecordsByVideo(records) {
  const groups = new Map();

  for (const record of records) {
    if (!groups.has(record.videoTitle)) {
      groups.set(record.videoTitle, []);
    }

    groups.get(record.videoTitle).push(record);
  }

  return Array.from(groups.entries()).map(([videoTitle, items]) => ({
    videoTitle,
    items,
    videoId: items[0]?.videoId ?? "",
    videoPath: items[0]?.videoPath ?? "",
  }));
}

function mergeConsecutiveKeywordRecords(items) {
  if (items.length === 0) {
    return [];
  }

  const sorted = [...items].sort((left, right) => {
    const cueDelta = Number(left.cueIndex ?? 0) - Number(right.cueIndex ?? 0);
    if (cueDelta !== 0) {
      return cueDelta;
    }

    return Number(left.startSeconds ?? 0) - Number(right.startSeconds ?? 0);
  });

  const segments = [];

  for (const record of sorted) {
    const lastSegment = segments.at(-1);

    if (
      lastSegment &&
      Number.isFinite(record.cueIndex) &&
      Number(record.cueIndex) <= lastSegment.lastCueIndex + 1
    ) {
      lastSegment.records.push(record);
      lastSegment.lastCueIndex = Number(record.cueIndex);
      lastSegment.endSeconds = record.endSeconds;
      lastSegment.nextText = record.nextText || "";
      continue;
    }

    segments.push({
      id: record.id,
      videoId: record.videoId,
      videoTitle: record.videoTitle,
      videoPath: record.videoPath,
      startSeconds: record.startSeconds,
      endSeconds: record.endSeconds,
      prevStartSeconds: record.prevStartSeconds,
      prevText: record.prevText || "",
      nextText: record.nextText || "",
      records: [record],
      lastCueIndex: Number(record.cueIndex ?? 0),
    });
  }

  return segments;
}

function buildContextHtml(text, query, className) {
  if (!text) {
    return "";
  }

  return `<p class="${className}">${buildHighlightedHtml(text, query)}</p>`;
}

function formatDuration(items) {
  if (items.length === 0) {
    return "00:00";
  }

  const maxEnd = Math.max(...items.map((item) => item.endSeconds ?? item.startSeconds));
  return formatClock(maxEnd);
}

function buildMergedSegmentHtml(segment, query) {
  const hitLinesHtml = segment.records
    .map(
      (record) => `
        <button class="result-line-text result-line-copy-id" type="button" data-copy-subtitle-id="${record.id}">
          ${buildHighlightedHtml(record.text, query)}
        </button>
      `,
    )
    .join("");

  const mergedNoteHtml =
    segment.records.length > 1 ? `<p class="result-line-score">连续命中 ${segment.records.length} 句</p>` : "";

  return `
    ${buildContextHtml(segment.prevText, query, "result-line-context")}
    ${hitLinesHtml}
    ${buildContextHtml(segment.nextText, query, "result-line-context")}
    ${mergedNoteHtml}
  `;
}

function buildClipUrl(item) {
  if (item.records) {
    const params = new URLSearchParams({
      video_id: String(item.videoId),
      start: String(item.startSeconds),
      end: String(item.endSeconds),
    });
    return `./api/clip-range?${params.toString()}`;
  }

  return `./api/clip?subtitle_id=${encodeURIComponent(item.id)}`;
}

function buildThumbnailUrl(item) {
  const primaryRecord = item.records ? item.records[0] : item;
  return `./api/thumbnail?subtitle_id=${encodeURIComponent(primaryRecord.id)}`;
}

function showCopiedFeedback(element, subtitleId) {
  const note = document.createElement("span");
  note.className = "result-line-feedback-note";
  note.textContent = `已复制 ID ${subtitleId}`;

  const container = element.closest(".result-line-copy") || element.parentElement;
  if (!container) {
    return;
  }

  const previous = container.querySelector(".result-line-feedback-note.is-copy-note");
  if (previous) {
    previous.remove();
  }

  note.classList.add("is-copy-note");
  container.appendChild(note);

  window.setTimeout(() => {
    if (note.isConnected) {
      note.remove();
    }
  }, 1600);
}

function renderResults() {
  const query = searchInput.value.trim();
  state.lastQuery = query;
  const filtered = query ? state.records : [];
  const groups = groupRecordsByVideo(filtered);
  const categoryLabel = getCategoryLabel(state.lastCategory);
  const searchModeLabel = state.lastSearchMode === "semantic" ? "语义搜索" : "关键词搜索";

  resultsTitle.textContent = query ? `${searchModeLabel} · ${categoryLabel}` : "Results";
  summaryText.textContent = query
    ? `在${categoryLabel}中${searchModeLabel}得到 ${filtered.length} 条结果，来自 ${groups.length} 个视频`
    : "Enter a search query to begin";

  if (!query) {
    renderEmpty("Type in the search box above to search across your subtitle library.");
    return;
  }

  if (filtered.length === 0) {
    renderEmpty("没有匹配结果，试试更短的关键词。");
    return;
  }

  resultList.innerHTML = groups
    .map((group) => {
      const isExpanded = state.expandedVideos.has(group.videoTitle);
      const displayItems =
        state.lastSearchMode === "keyword" ? mergeConsecutiveKeywordRecords(group.items) : group.items;
      const visibleItems = isExpanded ? displayItems : displayItems.slice(0, DEFAULT_VISIBLE_ITEMS);
      const matchesHtml = visibleItems
        .map((item) => {
          const primaryRecord = item.records ? item.records[0] : item;
          const clipUrl = buildClipUrl(item);
          const clipLabUrl = `./clip-lab.html?src=${encodeURIComponent(new URL(clipUrl, window.location.href).href)}`;
          const thumbnailUrl = buildThumbnailUrl(item);
          const isPlayerActive = playerState.activeRecordId === primaryRecord.id;
          const feedbackKey = `${state.lastSearchMode}:${state.lastCategory}:${state.lastQuery}:${primaryRecord.id}`;
          const feedbackState = state.feedbackStatus.get(feedbackKey);
          const feedbackHtml =
            state.lastSearchMode === "semantic"
              ? `
                <div class="result-line-feedbacks">
                  <button
                    class="result-line-feedback${feedbackState?.kind === "useful" ? " is-saved" : ""}"
                    type="button"
                    data-feedback-subtitle-id="${primaryRecord.id}"
                    data-feedback-video-id="${primaryRecord.videoId}"
                    data-feedback-rank-index="${primaryRecord.rankIndex ?? 0}"
                    data-feedback-score="${primaryRecord.score ?? ""}"
                    data-feedback-kind="useful"
                  >
                    有用
                  </button>
                  <button
                    class="result-line-feedback result-line-feedback-bad${feedbackState?.kind === "bad" ? " is-saved" : ""}"
                    type="button"
                    data-feedback-subtitle-id="${primaryRecord.id}"
                    data-feedback-video-id="${primaryRecord.videoId}"
                    data-feedback-rank-index="${primaryRecord.rankIndex ?? 0}"
                    data-feedback-score="${primaryRecord.score ?? ""}"
                    data-feedback-kind="bad"
                  >
                    很差
                  </button>
                  ${
                    feedbackState
                      ? `<span class="result-line-feedback-note">${escapeHtml(feedbackState.message || "已记录")}</span>`
                      : ""
                  }
                </div>
              `
              : "";

          const mediaStyle = isPlayerActive ? getMediaAspectStyle(primaryRecord.id) : "";

          return `
            <div class="result-line">
              <div class="result-line-media"${mediaStyle} data-record-id="${primaryRecord.id}">
                ${
                  isPlayerActive
                    ? `<div class="result-line-player-slot" data-player-slot="${primaryRecord.id}"></div>`
                    : `
                      <button class="result-line-preview-button" type="button" data-record-id="${primaryRecord.id}" aria-label="点击播放" title="点击播放">
                        <img
                          class="result-line-preview-image"
                          src="${escapeHtml(thumbnailUrl)}"
                          alt=""
                          loading="lazy"
                          decoding="async"
                          onerror="this.closest('.result-line-media').classList.add('is-error'); this.remove();"
                        />
                      </button>
                    `
                }
              </div>
              <div class="result-line-actions">
                <div class="result-line-actions-main">
                  <button class="result-line-time" type="button" data-record-id="${primaryRecord.id}">
                    ${formatClock(primaryRecord.startSeconds)}
                  </button>
                  <a class="result-line-download result-line-download-clip" href="${clipUrl}" target="_blank" rel="noreferrer" aria-label="Clip" title="Clip">
                    ↓
                  </a>
                  <a class="result-line-download result-line-download-lab" href="${clipLabUrl}" target="_blank" rel="noreferrer" aria-label="Lab" title="Lab">
                    ✂
                  </a>
                </div>
                ${
                  isPlayerActive
                    ? `
                      <div class="result-line-actions-player">
                        <button class="result-line-player-step" type="button" data-player-step="-0.3" title="后退 0.3 秒" aria-label="后退 0.3 秒">⏪</button>
                        <button class="result-line-player-step" type="button" data-player-step="0.3" title="快进 0.3 秒" aria-label="快进 0.3 秒">⏩</button>
                        <button class="result-line-player-clock" type="button" data-copy-player-clock data-player-clock aria-label="点击复制当前秒数" title="点击复制当前秒数" aria-live="off"></button>
                        <span class="result-line-player-copy-status" data-player-copy-status aria-live="polite"></span>
                      </div>
                    `
                    : ""
                }
              </div>
              <div class="result-line-copy">
                ${
                  item.records
                    ? buildMergedSegmentHtml(item, query)
                    : `
                      ${buildContextHtml(primaryRecord.prevText, query, "result-line-context")}
                      <button class="result-line-text result-line-copy-id" type="button" data-copy-subtitle-id="${primaryRecord.id}">
                        ${buildHighlightedHtml(primaryRecord.text, query)}
                      </button>
                      ${buildContextHtml(primaryRecord.nextText, query, "result-line-context")}
                    `
                }
                ${primaryRecord.score != null ? `<p class="result-line-score">相似度 ${Number(primaryRecord.score).toFixed(3)}</p>` : ""}
                ${feedbackHtml}
              </div>
            </div>
          `;
        })
        .join("");
      const toggleHtml =
        displayItems.length > DEFAULT_VISIBLE_ITEMS
          ? `
            <button class="result-group-toggle" type="button" data-toggle-video="${escapeHtml(group.videoTitle)}">
              ${isExpanded ? "Show less" : `Show ${displayItems.length - DEFAULT_VISIBLE_ITEMS} more`}
            </button>
          `
          : "";

      return `
        <section class="result-group" data-video-title="${escapeHtml(group.videoTitle)}">
          <div class="result-group-header">
            <button class="result-group-play" type="button" data-record-id="${displayItems[0].id}">Play</button>
            <div class="result-group-meta">
              <h3 class="result-group-title">${escapeHtml(group.videoTitle)}</h3>
              <div class="result-group-subtitle">
                <div>Video ID: ${escapeHtml(String(group.videoId))}</div>
                <div>Matches: ${group.items.length}</div>
                ${state.lastSearchMode === "keyword" ? `<div>Segments: ${displayItems.length}</div>` : ""}
                <div>Duration: ${formatDuration(group.items)}</div>
              </div>
            </div>
          </div>
          <div class="result-group-items">
            ${matchesHtml}
          </div>
          ${toggleHtml}
        </section>
      `;
    })
    .join("");

  syncEmbeddedPlayer();
  applyPreviewAspectRatios();
}

function getPlaybackStart(record) {
  return Number.isFinite(record.prevStartSeconds) ? record.prevStartSeconds : record.startSeconds;
}

function updatePlayerClock() {
  const playerClockButton = getPlayerClockButton();
  if (!playerClockButton || !playerDockVideo) {
    return;
  }

  const currentTime = Number.isFinite(playerDockVideo.currentTime) ? playerDockVideo.currentTime : playerState.currentTime;
  const seconds = Number.isFinite(currentTime) ? currentTime : 0;
  playerClockButton.textContent = formatSecondsLabel(seconds);
  playerClockButton.dataset.copyValue = formatSecondsCopyValue(seconds);
  playerState.currentTime = seconds;
}

function copyPlayerClock() {
  const playerClockButton = getPlayerClockButton();
  if (!playerClockButton) {
    return;
  }

  const value = playerClockButton.dataset.copyValue || playerClockButton.textContent || "";
  if (!value) {
    return;
  }

  const writeClipboard = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
        return true;
      }

      const textarea = document.createElement("textarea");
      textarea.value = value;
      textarea.setAttribute("readonly", "true");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.select();
      const copied = document.execCommand("copy");
      textarea.remove();
      return copied;
    } catch (error) {
      console.error(error);
      return false;
    }
  };

  void writeClipboard().then((copied) => {
    const statusNode = getPlayerCopyStatusNode();
    if (!statusNode) {
      return;
    }

    statusNode.textContent = copied ? "已复制" : "复制失败";
    statusNode.classList.toggle("is-error", !copied);

    if (playerState.copyStatusTimer) {
      window.clearTimeout(playerState.copyStatusTimer);
    }

    playerState.copyStatusTimer = window.setTimeout(() => {
      const liveStatusNode = getPlayerCopyStatusNode();
      if (liveStatusNode) {
        liveStatusNode.textContent = "";
        liveStatusNode.classList.remove("is-error");
      }
      playerState.copyStatusTimer = null;
    }, 1000);
  });
}

function seekPlayerByDelta(deltaSeconds) {
  if (!playerDockVideo) {
    return;
  }

  const currentTime = Number.isFinite(playerDockVideo.currentTime)
    ? playerDockVideo.currentTime
    : Number.isFinite(playerState.currentTime)
      ? playerState.currentTime
      : 0;
  const duration = Number.isFinite(playerDockVideo.duration) ? playerDockVideo.duration : null;
  const nextTime =
    duration == null ? Math.max(0, currentTime + deltaSeconds) : Math.min(duration, Math.max(0, currentTime + deltaSeconds));

  playerState.currentTime = nextTime;
  playerState.pendingSeek = nextTime;

  if (playerDockVideo.readyState >= 1) {
    playerDockVideo.currentTime = nextTime;
  }

  updatePlayerClock();
}

function attachPlayerHandlers() {
  if (!playerDockVideo) {
    return;
  }

  playerDockVideo.addEventListener("loadedmetadata", () => {
    if (playerState.pendingSeek == null) {
      updatePlayerClock();
      return;
    }

    try {
      playerDockVideo.currentTime = playerState.pendingSeek;
    } catch (error) {
      console.error(error);
    }
    updatePlayerClock();
  });

  playerDockVideo.addEventListener("seeked", () => {
    if (playerState.pendingSeek == null) {
      updatePlayerClock();
      return;
    }

    playerState.pendingSeek = null;
    updatePlayerClock();

    if (playerState.shouldAutoplayAfterSeek) {
      playerState.shouldAutoplayAfterSeek = false;
      void playerDockVideo.play().catch(() => {});
    }
  });

  playerDockVideo.addEventListener("timeupdate", () => {
    updatePlayerClock();
  });
}

function openClipLabForCurrentVideo() {
  if (!playerState.record || !playerState.videoId) {
    return;
  }

  const currentTime = Number.isFinite(playerDockVideo?.currentTime) ? playerDockVideo.currentTime : playerState.currentTime;

  void (async () => {
    try {
      const params = new URLSearchParams({
        video_id: String(playerState.videoId),
        time: currentTime.toFixed(3),
      });
      const response = await fetch(`./api/subtitle-at?${params.toString()}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `Failed to resolve subtitle: ${response.status}`);
      }

      const subtitleId = data.record?.subtitleId;
      const labTime = Number(data.record?.labTime ?? 0);
      const clipUrl = new URL(`./api/clip?subtitle_id=${encodeURIComponent(String(subtitleId))}`, window.location.href).href;
      const clipLabUrl = `./clip-lab.html?src=${encodeURIComponent(clipUrl)}&t=${encodeURIComponent(labTime.toFixed(3))}`;
      window.open(clipLabUrl, "_blank", "noopener,noreferrer");
    } catch (error) {
      console.error(error);
    }
  })();
}

function openPlayerForRecord(record) {
  if (!playerDock || !playerDockVideo) {
    return;
  }

  const playbackStart = getPlaybackStart(record);
  const desiredSrc = new URL(record.videoPath, window.location.href).href;
  const currentSrc = playerDockVideo.currentSrc || "";

  playerState.record = record;
  playerState.activeRecordId = record.id;
  playerState.videoId = record.videoId;
  playerState.videoPath = record.videoPath;
  playerState.currentTime = playbackStart;
  playerState.pendingSeek = playbackStart;
  playerState.shouldAutoplayAfterSeek = true;

  if (playerDock) {
    playerDock.setAttribute("hidden", "");
    playerDock.hidden = true;
  }

  renderResults();

  if (currentSrc !== desiredSrc) {
    playerDockVideo.src = record.videoPath;
    playerDockVideo.load();
  }

  updatePlayerClock();

  if (playerDockVideo.readyState >= 1 && currentSrc === desiredSrc) {
    try {
      playerDockVideo.currentTime = playbackStart;
    } catch (error) {
      console.error(error);
    }
    void playerDockVideo.play().catch(() => {});
  }
}

function syncEmbeddedPlayer() {
  if (!playerDock || !playerDockHomeParent || !playerDockVideo) {
    return;
  }

  const activeId = playerState.activeRecordId;
  if (!activeId) {
    restorePlayerDockHome();
    playerDock.setAttribute("hidden", "");
    playerDock.hidden = true;
    return;
  }

  const slot = resultList.querySelector(`[data-player-slot="${CSS.escape(activeId)}"]`);
  if (!slot) {
    restorePlayerDockHome();
    playerDock.setAttribute("hidden", "");
    playerDock.hidden = true;
    return;
  }

  if (playerDock.parentElement !== slot) {
    slot.replaceWith(playerDock);
  }

  playerDock.removeAttribute("hidden");
  playerDock.hidden = false;
  updatePlayerClock();
}

function restorePlayerDockHome() {
  if (!playerDock || !playerDockHomeParent) {
    return;
  }

  if (playerDock.parentElement !== playerDockHomeParent) {
    playerDockHomeParent.insertBefore(playerDock, playerDockHomeNextSibling || null);
  }
}

function dismissPlayerDock() {
  if (playerDockVideo) {
    playerDockVideo.pause();
  }

  playerState.activeRecordId = null;
  playerState.record = null;
  playerState.videoId = null;
  playerState.videoPath = "";
  playerState.pendingSeek = null;
  playerState.shouldAutoplayAfterSeek = false;

  if (playerState.copyStatusTimer) {
    window.clearTimeout(playerState.copyStatusTimer);
    playerState.copyStatusTimer = null;
  }

  restorePlayerDockHome();

  if (playerDock) {
    playerDock.setAttribute("hidden", "");
    playerDock.hidden = true;
  }
}

async function runSearch(category, mode = "keyword") {
  const query = searchInput.value.trim();
  state.lastQuery = query;
  state.lastCategory = category;
  state.lastSearchMode = mode;

  if (!query) {
    state.records = [];
    renderResults();
    return;
  }

  resultsTitle.textContent = `${mode === "semantic" ? "语义搜索" : "关键词搜索"} · ${getCategoryLabel(category)}`;
  summaryText.textContent = mode === "semantic" ? "正在做语义搜索..." : "正在搜索...";

  try {
    state.records =
      mode === "semantic"
        ? await semanticSearchRecords(query, category)
        : await searchRecords(query, category);
    state.records = state.records.map((record, index) => ({
      ...record,
      rankIndex: index,
    }));
    renderResults();
  } catch (error) {
    console.error(error);
    state.records = [];
    summaryText.textContent = "搜索失败";
    renderEmpty(
      mode === "semantic"
        ? "语义搜索失败，请检查本地 embedding 环境和模型是否已准备好。"
        : "后端搜索接口调用失败，请检查 Python 服务是否正常启动。",
    );
  }
}

resultList.addEventListener("click", (event) => {
  const clockButton = event.target.closest("[data-copy-player-clock]");
  if (clockButton) {
    copyPlayerClock();
    return;
  }

  const stepButton = event.target.closest("[data-player-step]");
  if (stepButton) {
    const deltaSeconds = Number(stepButton.dataset.playerStep || 0);
    if (Number.isFinite(deltaSeconds)) {
      seekPlayerByDelta(deltaSeconds);
    }
    return;
  }

  const openLabButton = event.target.closest("[data-open-lab-video]");
  if (openLabButton) {
    openClipLabForCurrentVideo();
    return;
  }

  const copyButton = event.target.closest("[data-copy-subtitle-id]");
  if (copyButton) {
    const subtitleId = copyButton.dataset.copySubtitleId;
    if (subtitleId) {
      void navigator.clipboard.writeText(subtitleId).then(() => {
        showCopiedFeedback(copyButton, subtitleId);
      }).catch((error) => {
        console.error(error);
      });
    }
    return;
  }

  const feedbackButton = event.target.closest("[data-feedback-subtitle-id]");

  if (feedbackButton) {
    const subtitleId = feedbackButton.dataset.feedbackSubtitleId;
    const videoId = feedbackButton.dataset.feedbackVideoId;
    const kind = feedbackButton.dataset.feedbackKind;
    const rankIndex = feedbackButton.dataset.feedbackRankIndex;
    const score = feedbackButton.dataset.feedbackScore;
    const feedbackKey = `${state.lastSearchMode}:${state.lastCategory}:${state.lastQuery}:${subtitleId}`;

    void (async () => {
      try {
        const data = await submitSearchFeedback({
          query: state.lastQuery,
          category: state.lastCategory,
          searchMode: state.lastSearchMode,
          subtitleId: Number(subtitleId),
          videoId: Number(videoId),
          rankIndex: rankIndex === "" ? null : Number(rankIndex),
          score: score === "" ? null : Number(score),
          feedback: kind,
        });
        const count =
          kind === "useful"
            ? `有用 ${Number(data.usefulCount ?? 0)}`
            : `很差 ${Number(data.badCount ?? 0)}`;
        state.feedbackStatus.set(feedbackKey, {
          kind,
          message: `已记录 · ${count}`,
        });
        renderResults();
      } catch (error) {
        console.error(error);
      }
    })();
    return;
  }

  const toggleButton = event.target.closest("[data-toggle-video]");

  if (toggleButton) {
    const videoTitle = toggleButton.dataset.toggleVideo;

    if (state.expandedVideos.has(videoTitle)) {
      state.expandedVideos.delete(videoTitle);
    } else {
      state.expandedVideos.add(videoTitle);
    }

    renderResults();
    return;
  }

  const button = event.target.closest("[data-record-id]");

  if (!button) {
    return;
  }

  const recordId = button.dataset.recordId;

  const record = state.records.find((item) => item.id === recordId);
  if (record) {
    openPlayerForRecord(record);
  }
});

searchHumorButton.addEventListener("click", () => {
  void runSearch("humor", "keyword");
  searchInput.focus();
});

searchWritingButton.addEventListener("click", () => {
  void runSearch("writing", "keyword");
  searchInput.focus();
});

semanticHumorButton.addEventListener("click", () => {
  void runSearch("humor", "semantic");
  searchInput.focus();
});

semanticWritingButton.addEventListener("click", () => {
  void runSearch("writing", "semantic");
  searchInput.focus();
});

searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    void runSearch(state.lastCategory, state.lastSearchMode);
  }
});

async function main() {
  try {
    dismissPlayerDock();
    attachPlayerHandlers();
    const data = await fetchMeta();
    const videoCount = Number(data.metadata?.video_count ?? 0);
    const subtitleCount = Number(data.metadata?.subtitle_count ?? 0);
    summaryText.textContent = `Ready. ${videoCount} videos, ${subtitleCount} subtitle cues indexed.`;
    renderEmpty("Type in the search box above to search across your subtitle library.");
  } catch (error) {
    console.error(error);
    renderEmpty("无法连接 Python 后端，请先运行 npm run serve。");
    summaryText.textContent = "Backend unavailable";
  }
}

main();
