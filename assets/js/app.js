const summaryText = document.querySelector("#summaryText");
const resultsTitle = document.querySelector("#resultsTitle");
const resultList = document.querySelector("#resultList");
const searchInput = document.querySelector("#searchInput");
const searchHumorButton = document.querySelector("#searchHumorButton");
const searchWritingButton = document.querySelector("#searchWritingButton");
const semanticHumorButton = document.querySelector("#semanticHumorButton");
const semanticWritingButton = document.querySelector("#semanticWritingButton");

const state = {
  records: [],
  lastQuery: "",
  lastCategory: "humor",
  lastSearchMode: "keyword",
  openPlayers: new Map(),
  expandedVideos: new Set(),
  feedbackStatus: new Map(),
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
  resultList.innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
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

function buildVideoPreview(items, query) {
  const previews = items.slice(0, 2).map((item) => buildHighlightedHtml(item.text, query));
  return previews.join(" / ");
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
    state.openPlayers.clear();
    renderEmpty("Type in the search box above to search across your subtitle library.");
    return;
  }

  if (filtered.length === 0) {
    state.openPlayers.clear();
    renderEmpty("没有匹配结果，试试更短的关键词。");
    return;
  }

  const visibleTitles = new Set(groups.map((group) => group.videoTitle));

  for (const videoTitle of [...state.openPlayers.keys()]) {
    if (!visibleTitles.has(videoTitle)) {
      state.openPlayers.delete(videoTitle);
    }
  }

  resultList.innerHTML = groups
    .map((group) => {
      const openState = state.openPlayers.get(group.videoTitle);
      const isExpanded = state.expandedVideos.has(group.videoTitle);
      const visibleItems = isExpanded ? group.items : group.items.slice(0, 9);
      const matchesHtml = visibleItems
        .map((record) => {
          const activeClass = openState?.recordId === record.id ? " is-active" : "";
          const clipUrl = `./api/clip?subtitle_id=${encodeURIComponent(record.id)}`;
          const clipLabUrl = `./clip-lab.html?src=${encodeURIComponent(new URL(clipUrl, window.location.href).href)}`;
          const feedbackKey = `${state.lastSearchMode}:${state.lastCategory}:${state.lastQuery}:${record.id}`;
          const feedbackState = state.feedbackStatus.get(feedbackKey);
          const feedbackHtml =
            state.lastSearchMode === "semantic"
              ? `
                <div class="result-line-feedbacks">
                  <button
                    class="result-line-feedback${feedbackState?.kind === "useful" ? " is-saved" : ""}"
                    type="button"
                    data-feedback-subtitle-id="${record.id}"
                    data-feedback-video-id="${record.videoId}"
                    data-feedback-rank-index="${record.rankIndex ?? 0}"
                    data-feedback-score="${record.score ?? ""}"
                    data-feedback-kind="useful"
                  >
                    有用
                  </button>
                  <button
                    class="result-line-feedback result-line-feedback-bad${feedbackState?.kind === "bad" ? " is-saved" : ""}"
                    type="button"
                    data-feedback-subtitle-id="${record.id}"
                    data-feedback-video-id="${record.videoId}"
                    data-feedback-rank-index="${record.rankIndex ?? 0}"
                    data-feedback-score="${record.score ?? ""}"
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

          return `
            <div class="result-line${activeClass}">
              <div class="result-line-actions">
                <button class="result-line-time" type="button" data-record-id="${record.id}">
                  ${formatClock(record.startSeconds)}
                </button>
                <a class="result-line-download" href="${clipUrl}" target="_blank" rel="noreferrer">
                  Clip
                </a>
                <a class="result-line-download" href="${clipLabUrl}" target="_blank" rel="noreferrer">
                  Lab
                </a>
              </div>
              <div class="result-line-copy">
                ${buildContextHtml(record.prevText, query, "result-line-context")}
                <p class="result-line-text">${buildHighlightedHtml(record.text, query)}</p>
                ${buildContextHtml(record.nextText, query, "result-line-context")}
                ${record.score != null ? `<p class="result-line-score">相似度 ${Number(record.score).toFixed(3)}</p>` : ""}
                ${feedbackHtml}
              </div>
            </div>
          `;
        })
        .join("");
      const toggleHtml =
        group.items.length > 9
          ? `
            <button class="result-group-toggle" type="button" data-toggle-video="${escapeHtml(group.videoTitle)}">
              ${isExpanded ? "Show less" : `Show ${group.items.length - 9} more`}
            </button>
          `
          : "";

      return `
        <section class="result-group${openState ? " is-active" : ""}" data-video-title="${escapeHtml(group.videoTitle)}">
          <div class="result-group-header">
            <button class="result-group-play" type="button" data-record-id="${group.items[0].id}">Play</button>
            <div class="result-group-meta">
              <h3 class="result-group-title">${escapeHtml(group.videoTitle)}</h3>
              <div class="result-group-subtitle">
                <div>Video ID: ${escapeHtml(String(group.videoId))}</div>
                <div>Matches: ${group.items.length}</div>
                <div>Duration: ${formatDuration(group.items)}</div>
                <div>Preview: ${buildVideoPreview(group.items, query)}</div>
              </div>
            </div>
          </div>
          <div class="result-group-player-slot"></div>
          <div class="result-group-items">
            ${matchesHtml}
          </div>
          ${toggleHtml}
        </section>
      `;
    })
    .join("");

  restoreOpenPlayers();
}

function buildPlayerShell(videoTitle, videoPath) {
  const shell = document.createElement("div");
  shell.className = "inline-player-shell";
  shell.dataset.playerFor = videoTitle;
  shell.innerHTML = `
    <div class="inline-player-meta"></div>
    <div class="inline-player-actions">
      <button class="result-line-time" type="button" data-open-lab-video="${escapeHtml(videoTitle)}">当前播放进 Lab</button>
    </div>
    <video class="inline-player" controls preload="metadata" src="${escapeHtml(videoPath)}"></video>
  `;
  return shell;
}

function attachPlayerHandlers(video, openState) {
  video.addEventListener("loadedmetadata", () => {
    if (openState.pendingSeek == null) {
      return;
    }

    video.currentTime = openState.pendingSeek;
  });

  video.addEventListener("seeked", () => {
    if (openState.pendingSeek == null) {
      return;
    }

    openState.pendingSeek = null;

    if (openState.shouldAutoplayAfterSeek) {
      openState.shouldAutoplayAfterSeek = false;
      void video.play().catch(() => {});
    }
  });
}

function restoreOpenPlayers() {
  for (const [videoTitle, openState] of state.openPlayers.entries()) {
    const group = resultList.querySelector(`[data-video-title="${CSS.escape(videoTitle)}"]`);

    if (!group) {
      continue;
    }

    const slot = group.querySelector(".result-group-player-slot");

    if (!slot) {
      continue;
    }

    const shell = buildPlayerShell(videoTitle, openState.videoPath);
    const meta = shell.querySelector(".inline-player-meta");
    const video = shell.querySelector(".inline-player");
    meta.textContent = openState.cueText || "Select a timestamp to jump in this video.";
    attachPlayerHandlers(video, openState);
    slot.replaceWith(shell);
    openState.video = video;

    if (openState.pendingSeek == null && openState.currentTime != null) {
      video.addEventListener(
        "loadedmetadata",
        () => {
          video.currentTime = openState.currentTime;
          if (!openState.paused) {
            void video.play().catch(() => {});
          }
        },
        { once: true },
      );
    }
  }
}

function snapshotOpenPlayers() {
  for (const [videoTitle, openState] of state.openPlayers.entries()) {
    if (!openState.video) {
      continue;
    }

    openState.currentTime = openState.video.currentTime;
    openState.paused = openState.video.paused;
  }
}

function seekInlinePlayer(record) {
  snapshotOpenPlayers();
  const playbackStart = Number.isFinite(record.prevStartSeconds) ? record.prevStartSeconds : record.startSeconds;

  let openState = state.openPlayers.get(record.videoTitle);

  if (!openState) {
    openState = {
      videoId: record.videoId,
      videoPath: record.videoPath,
      recordId: null,
      cueText: "",
      currentTime: 0,
      paused: true,
      pendingSeek: null,
      shouldAutoplayAfterSeek: false,
      video: null,
    };
    state.openPlayers.set(record.videoTitle, openState);
  }

  openState.videoId = record.videoId;
  openState.videoPath = record.videoPath;
  openState.recordId = record.id;
  openState.cueText = `${formatClock(record.startSeconds)} ${record.text}`;
  openState.pendingSeek = playbackStart;
  openState.shouldAutoplayAfterSeek = true;
  openState.currentTime = playbackStart;
  openState.paused = false;

  renderResults();

  if (openState.video?.readyState >= 1) {
    openState.video.currentTime = playbackStart;
    void openState.video.play().catch(() => {});
  }
}

async function openClipLabForVideo(videoTitle) {
  const openState = state.openPlayers.get(videoTitle);
  if (!openState) {
    return;
  }

  if (openState.video) {
    openState.currentTime = openState.video.currentTime;
    openState.paused = openState.video.paused;
  }

  const currentTime = Number.isFinite(openState.currentTime) ? openState.currentTime : 0;
  if (!openState.videoId) {
    return;
  }

  try {
    const params = new URLSearchParams({
      video_id: String(openState.videoId),
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
}

async function runSearch(category, mode = "keyword") {
  const query = searchInput.value.trim();
  state.lastQuery = query;
  state.lastCategory = category;
  state.lastSearchMode = mode;
  state.openPlayers.clear();

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

  const openLabButton = event.target.closest("[data-open-lab-video]");

  if (openLabButton) {
    void openClipLabForVideo(openLabButton.dataset.openLabVideo);
    return;
  }

  const button = event.target.closest("[data-record-id]");

  if (!button) {
    return;
  }

  const record = state.records.find((item) => item.id === button.dataset.recordId);

  if (record) {
    seekInlinePlayer(record);
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
