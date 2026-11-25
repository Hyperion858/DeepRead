/* DeepRead SPA — menu layout.
   Pages: #/ home, #/library materials, #/courses library, #/settings models, #/backup */
"use strict";

const $app = document.getElementById("app");
const state = {
  course: null,
  progress: null,
  tab: "read",
  abLoop: null,
  pollTimer: null,
};

/* ---------------- utils ---------------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function fmtDuration(sec) {
  sec = Math.floor(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}小时${m}分` : `${m}分${sec % 60}秒`;
}

function toast(msg, ms = 2600) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch {}
    throw new Error(detail || `HTTP ${r.status}`);
  }
  return r.json();
}

/* ---------------- layout shell ---------------- */
const NAV = [
  { hash: "#/",         key: "home",     icon: "🏠", label: "首页" },
  { hash: "#/library",  key: "library",  icon: "📥", label: "素材库" },
  { hash: "#/courses",  key: "courses",  icon: "📚", label: "课程库" },
  { hash: "#/settings", key: "settings", icon: "🧠", label: "模型配置" },
  { hash: "#/backup",   key: "backup",   icon: "💾", label: "备份与恢复" },
];

function shell(activeKey, contentHtml) {
  return `
  <div class="frame">
    <aside class="sidebar">
      <div class="logo"><b><span>Deep</span>Read</b><div class="logo-sub">英语精读工坊</div></div>
      <nav>
        ${NAV.map((n) => `
        <a href="${n.hash}" class="nav-item ${n.key === activeKey ? "active" : ""}">
          <span class="nav-icon">${n.icon}</span><span>${n.label}</span>
        </a>`).join("")}
      </nav>
      <div class="sidebar-foot">v0.1.0</div>
    </aside>
    <main class="main">${contentHtml}</main>
  </div>`;
}

/* ---------------- router ---------------- */
window.addEventListener("hashchange", route);

function route() {
  clearInterval(state.pollTimer);
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/course\/([\w-]+)/);
  if (m) return renderCourse(m[1]);
  if (hash.startsWith("#/library")) return renderLibrary();
  if (hash.startsWith("#/courses")) return renderCourses();
  if (hash.startsWith("#/settings")) return renderSettings();
  if (hash.startsWith("#/backup")) return renderBackup();
  return renderHome();
}

/* ================= Home ================= */
async function renderHome() {
  document.title = "DeepRead · 英语精读";
  let courses = [];
  let stats = { ready: 0, learning: 0, minutes: 0 };
  try {
    courses = await api("/api/courses");
    stats.ready = courses.filter((c) => c.stage === "ready").length;
    stats.learning = courses.filter((c) => c.learned_percent > 0).length;
    stats.minutes = Math.round(courses.reduce((a, c) => a + (c.duration || 0), 0) / 60);
  } catch {}
  const recent = courses.filter((c) => c.stage === "ready").slice(0, 3);
  $app.innerHTML = shell("home", `
    <div class="page">
      <h1>继续学习</h1>
      <div class="stat-row">
        <div class="stat"><div class="stat-n">${stats.ready}</div><div class="stat-l">课程</div></div>
        <div class="stat"><div class="stat-n">${stats.learning}</div><div class="stat-l">学习中</div></div>
        <div class="stat"><div class="stat-n">${stats.minutes}</div><div class="stat-l">素材分钟数</div></div>
      </div>
      ${recent.length ? `
      <div class="course-grid">
        ${recent.map((c) => `
        <div class="course-card" onclick="location.hash='#/course/${c.id}'">
          <div class="cc-title">${esc(c.title)}</div>
          <div class="cc-meta"><span class="badge ready">可学习</span><span>${fmtDuration(c.duration)}</span></div>
          <div class="progressbar"><div style="width:${c.learned_percent || 0}%"></div></div>
          <div class="cc-detail">已学 ${c.learned_percent || 0}%</div>
        </div>`).join("")}
      </div>` : `
      <div class="empty-tip">还没有课程。去 <a href="#/library">素材库</a> 上传视频或粘贴抖音链接，生成第一门精读课。</div>`}
      <div class="quick-actions">
        <a class="qa" href="#/library"><b>📥 添加素材</b><span>上传文件 / 抖音链接</span></a>
        <a class="qa" href="#/courses"><b>📚 课程库</b><span>全部课程与进度</span></a>
      </div>
    </div>`);
}

/* ================= Library (materials) ================= */
async function renderLibrary() {
  document.title = "素材库 · DeepRead";
  $app.innerHTML = shell("library", `
    <div class="page">
      <h1>素材库</h1>
      <div class="dropzone" id="dropzone">
        <div class="dz-icon">⬆️</div>
        <div>拖拽视频 / 音频文件到这里，或点击选择</div>
        <div class="dz-hint">支持 mp4 / mkv / mov / mp3 / wav / m4a 等</div>
        <input type="file" id="file-input" hidden accept="video/*,audio/*">
      </div>
      <div class="section-title">或粘贴视频链接（抖音 / B站 / YouTube 等yt-dlp支持的站点）</div>
      <div style="display:flex;gap:8px;max-width:640px">
        <input id="url-input" type="text" placeholder="https://v.douyin.com/..." style="flex:1;min-width:0">
        <button class="primary" id="url-go" style="white-space:nowrap;flex-shrink:0">下载素材</button>
      </div>
      <div class="cal-hint" style="margin-top:8px">
        素材是原始视频/音频。生成课程时会自动 OCR 识别画面硬字幕并与语音转写交叉校对。
      </div>

      <div class="section-title" style="margin-top:26px">素材列表</div>
      <div id="material-list"><div class="spinner"></div></div>
    </div>`);

  const dz = document.getElementById("dropzone");
  const fi = document.getElementById("file-input");
  dz.onclick = () => fi.click();
  fi.onchange = () => fi.files[0] && uploadMaterial(fi.files[0]);
  dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("dragover"); };
  dz.ondragleave = () => dz.classList.remove("dragover");
  dz.ondrop = (e) => {
    e.preventDefault(); dz.classList.remove("dragover");
    if (e.dataTransfer.files[0]) uploadMaterial(e.dataTransfer.files[0]);
  };
  document.getElementById("url-go").onclick = importUrl;
  document.getElementById("url-input").onkeydown = (e) => { if (e.key === "Enter") importUrl(); };

  await refreshMaterials();
  state.pollTimer = setInterval(refreshMaterials, 3000);
}

async function refreshMaterials() {
  const wrap = document.getElementById("material-list");
  if (!wrap) return;
  try {
    const list = await api("/api/materials");
    if (!list.length) {
      wrap.innerHTML = `<div class="panel-empty">还没有素材，上传文件或粘贴链接添加</div>`;
      return;
    }
    wrap.innerHTML = `<div class="table-scroll"><table class="bk-table">
      <tr><th>标题</th><th>类型</th><th>大小</th><th>来源</th><th>状态</th><th style="width:220px"></th></tr>` +
      list.map((m) => {
        const statusLabel = { ready: "就绪", downloading: "下载中", error: "下载失败" }[m.status || "ready"] || "就绪";
        const downloading = m.status === "downloading";
        return `
      <tr data-mid="${esc(m.id)}">
        <td class="mat-title-cell">
          <span class="mat-title">${esc(m.title)}</span>
          <button class="mat-rename" data-rename="${esc(m.id)}" title="重命名">✎</button>
          ${m.course_id ? `<a class="badge ready" style="text-decoration:none" href="#/course/${esc(m.course_id)}">已有课程 →</a>` : ""}
        </td>
        <td>${m.kind === "audio" ? "音频" : "视频"}</td>
        <td>${m.size_mb} MB</td>
        <td>${m.source_url ? `<a href="${esc(m.source_url)}" target="_blank" rel="noopener" style="color:var(--accent)">链接</a>` : "上传"}</td>
        <td>${downloading ? '<span class="spinner"></span> ' : ""}<span class="${m.status === "error" ? "flag-uncertain" : ""}">${statusLabel}${m.error ? " · " + esc(m.error.slice(0, 40)) : ""}</span></td>
        <td>
          ${!m.course_id && !downloading ? `<button class="primary" data-genc="${esc(m.id)}" ${m.media_exists ? "" : "disabled"}>生成课程</button>` : ""}
          ${m.media_exists && m.course_id ? `<button data-regen="${esc(m.course_id)}" title="重新生成课程">↻</button>` : ""}
          <button class="danger" data-del="${esc(m.id)}">删除</button>
        </td>
      </tr>`;
      }).join("") + `</table></div>`;

    wrap.querySelectorAll("[data-genc]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          const res = await api(`/api/materials/${btn.dataset.genc}/generate`, { method: "POST" });
          toast("课程生成已开始");
          location.hash = `#/course/${res.id}`;
        } catch (err) { toast(err.message); }
      };
    });
    wrap.querySelectorAll("[data-regen]").forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm("重新生成会覆盖这门课程的转写和精讲，继续？")) return;
        try {
          await api(`/api/courses/${btn.dataset.regen}/generate`, { method: "POST" });
          toast("重新生成已开始");
        } catch (err) { toast(err.message); }
      };
    });
    wrap.querySelectorAll("[data-del]").forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm("删除这个素材？媒体文件会一并删除。")) return;
        try {
          await api(`/api/materials/${btn.dataset.del}`, { method: "DELETE" });
          refreshMaterials();
        } catch (err) { toast(err.message); }
      };
    });
    wrap.querySelectorAll("[data-rename]").forEach((btn) => {
      btn.onclick = () => {
        const tr = btn.closest("tr");
        const mid = btn.dataset.rename;
        const span = tr.querySelector(".mat-title");
        const cur = span.textContent;
        const input = document.createElement("input");
        input.type = "text";
        input.value = cur;
        input.style.cssText = "max-width:280px";
        span.replaceWith(input);
        input.focus();
        const commit = async () => {
          const title = input.value.trim();
          if (title && title !== cur) {
            await api(`/api/materials/${mid}/rename`, { method: "POST", body: JSON.stringify({ title }) });
          }
          refreshMaterials();
        };
        input.onblur = commit;
        input.onkeydown = (e) => { if (e.key === "Enter") input.blur(); };
      };
    });
  } catch (err) {
    wrap.innerHTML = `<div class="panel-empty">加载失败: ${esc(err.message)}</div>`;
  }
}

async function uploadMaterial(file) {
  const fd = new FormData();
  fd.append("file", file);
  toast(`上传中: ${file.name}…`);
  try {
    await api("/api/materials/upload", { method: "POST", body: fd });
    toast("素材已上传");
    refreshMaterials();
  } catch (err) {
    toast(`上传失败: ${err.message}`);
  }
}

async function importUrl() {
  const input = document.getElementById("url-input");
  const url = input.value.trim();
  if (!url) { toast("请先粘贴链接"); return; }
  toast("开始下载素材…");
  try {
    await api("/api/materials/import", { method: "POST", body: JSON.stringify({ url }) });
    toast("下载已开始，完成后可在列表中生成课程");
    refreshMaterials();
  } catch (err) {
    toast(`导入失败: ${err.message}`);
  }
}

/* ================= Courses ================= */
async function renderCourses() {
  document.title = "课程库 · DeepRead";
  $app.innerHTML = shell("courses", `
    <div class="page">
      <h1>课程库</h1>
      <div class="course-grid" id="course-grid"><div class="spinner"></div></div>
    </div>`);
  await refreshCourses();
  state.pollTimer = setInterval(refreshCourses, 3000);
}

async function refreshCourses() {
  try {
    const list = await api("/api/courses");
    const grid = document.getElementById("course-grid");
    if (!grid) return;
    if (!list.length) {
      grid.innerHTML = `<div class="empty-tip" style="grid-column:1/-1">还没有课程，去 <a href="#/library">素材库</a> 添加</div>`;
      return;
    }
    grid.innerHTML = list.map((c) => {
      const stageLabel = {
        downloading: "下载中", uploaded: "待生成", transcribing: "转写", subtitles: "字幕",
        ocr: "OCR 字幕", aligning: "对齐", generating: "精讲", ready: "完成",
        error: "失败", cancelled: "已取消",
      }[c.stage] || c.stage;
      const working = ["downloading", "transcribing", "subtitles", "ocr", "aligning", "generating"].includes(c.stage);
      const badgeCls = c.stage === "ready" ? "ready" : c.stage === "error" ? "error" : working ? "working" : "";
      return `
      <div class="course-card" data-id="${c.id}">
        <div class="cc-title">${esc(c.title)}</div>
        <div class="cc-meta">
          <span class="badge ${badgeCls}">${stageLabel}</span>
          <span>${fmtDuration(c.duration)}</span>
          <span>${c.sentence_count ? c.sentence_count + " 句" : ""}</span>
        </div>
        ${working ? `<div class="progressbar"><div style="width:${Math.round((c.progress || 0) * 100)}%"></div></div>
                     <div class="cc-detail">${esc(c.detail || "")}</div>` : ""}
        ${c.stage === "ready" ? `<div class="progressbar"><div style="width:${c.learned_percent || 0}%"></div></div>
                                 <div class="cc-detail">已学 ${c.learned_percent || 0}%</div>` : ""}
        ${c.stage === "uploaded" ? `<button class="primary" data-gen="${c.id}">开始生成</button>` : ""}
        ${c.stage === "error" ? `<div class="cc-detail" style="color:var(--red)">${esc(c.detail || "")}</div>
                                 <button class="primary" data-gen="${c.id}">重试</button>` : ""}
        <button class="cc-del" data-del="${c.id}" title="删除">✕</button>
      </div>`;
    }).join("");

    grid.querySelectorAll(".course-card").forEach((card) => {
      card.onclick = (e) => {
        if (e.target.closest("[data-gen],[data-del]")) return;
        location.hash = `#/course/${card.dataset.id}`;
      };
    });
    grid.querySelectorAll("[data-gen]").forEach((btn) => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        try {
          await api(`/api/courses/${btn.dataset.gen}/generate`, { method: "POST" });
          toast("已开始生成");
          refreshCourses();
        } catch (err) { toast(err.message); }
      };
    });
    grid.querySelectorAll("[data-del]").forEach((btn) => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        if (!confirm("确定删除这门课程？\n只删除课程的学习内容（转写/精讲/进度），原始素材仍保留在素材库。")) return;
        await api(`/api/courses/${btn.dataset.del}`, { method: "DELETE" });
        refreshCourses();
      };
    });
  } catch (err) {
    const grid = document.getElementById("course-grid");
    if (grid) grid.innerHTML = `<div class="empty-tip" style="grid-column:1/-1">加载失败: ${esc(err.message)}</div>`;
  }
}

/* ================= Course page ================= */
async function renderCourse(cid) {
  document.title = "课程 · DeepRead";
  $app.innerHTML = shell("", `<div class="page"><div class="spinner"></div></div>`);
  try {
    const [course, progress] = await Promise.all([
      api(`/api/courses/${cid}`),
      api(`/api/courses/${cid}/progress`),
    ]);
    state.course = course;
    state.progress = progress.read ? progress : { read: [], cards: {} };
    if (!state.progress.read) state.progress.read = [];
    if (!state.progress.cards) state.progress.cards = {};
  } catch (err) {
    $app.innerHTML = shell("", `<div class="page" style="color:var(--red)">${esc(err.message)}</div>`);
    return;
  }
  drawCoursePage();
}

function saveProgress() {
  const p = state.progress || { read: [], cards: {} };
  const total = (state.course?.lesson?.sentences || []).length;
  const payload = {
    read: p.read,
    cards: p.cards,
    learned_percent: total ? Math.round((p.read.length / total) * 100) : 0,
  };
  const cid = state.course.meta.id;
  api(`/api/courses/${cid}/progress`, { method: "POST", body: JSON.stringify(payload) }).catch(() => {});
}

/* ------- calibration: global offset + per-sentence time overrides ------- */

function calibratedSentences() {
  const lesson = state.course?.lesson;
  const cal = state.course?.calibration || { global_offset: 0, overrides: {} };
  const off = parseFloat(cal.global_offset) || 0;
  const ovr = cal.overrides || {};
  return (lesson?.sentences || []).map((s) => {
    const o = ovr[String(s.id)];
    return {
      ...s,
      start: o ? o.start : s.start + off,
      end: o ? o.end : s.end + off,
    };
  });
}

async function saveCalibration() {
  const cal = state.course.calibration;
  await api(`/api/courses/${state.course.meta.id}/calibration`, {
    method: "POST",
    body: JSON.stringify({ global_offset: cal.global_offset, overrides: cal.overrides }),
  });
}

function drawCoursePage() {
  const { meta, status, lesson } = state.course;
  if (status.stage !== "ready") {
    const working = ["downloading", "transcribing", "subtitles", "ocr", "aligning", "generating"].includes(status.stage);
    $app.innerHTML = shell("", `
      <div class="page" style="text-align:center;padding-top:80px">
        <h2>${esc(meta.title)}</h2>
        <p>${working ? `<div class="spinner"></div><p>正在生成: ${esc(status.detail || status.stage)} (${Math.round((status.progress || 0) * 100)}%)</p>` : `课程尚未生成: ${esc(status.detail || status.stage)}`}
        </p>
        ${!working ? `<button class="primary" onclick="location.reload()">刷新</button>` : ""}
        <p><a href="#/courses">返回课程库</a></p>
      </div>`);
    if (working) {
      state.pollTimer = setInterval(async () => {
        const st = await api(`/api/courses/${meta.id}`).catch(() => null);
        if (st && st.status.stage === "ready") { clearInterval(state.pollTimer); renderCourse(meta.id); }
      }, 3000);
    }
    return;
  }

  const isAudio = meta.kind === "audio";
  const mediaUrl = `/media/${meta.id}/file`;
  const sentences = calibratedSentences();
  const rawSentences = lesson.sentences || [];

  $app.innerHTML = `
  <div class="course-page">
    <div class="cp-top">
      <a class="back" href="#/courses">←</a>
      <div class="cp-title">${esc(meta.title)}</div>
      <a href="#/courses" class="cp-home" title="返回课程库">📚</a>
    </div>
    <div class="cp-body">
      <div class="cp-left">
        <div class="player-wrap" id="player-wrap">
          ${isAudio
            ? `<div class="audio-ph" style="align-self:center"><div class="ph-wave">〰〰〰🎶〰〰〰</div>${esc(meta.title)}<br><audio id="player" src="${mediaUrl}" controls style="margin-top:14px"></audio></div>`
            : `<video id="player" src="${mediaUrl}" controls playsinline></video>`}
        </div>
        <div class="cp-controls">
          <button id="ctl-prev" title="上一句">⏮</button>
          <button id="ctl-ab" title="A-B 循环">A-B</button>
          <button id="ctl-follow" title="字幕跟随开关">不跟随</button>
          <button id="ctl-goto" title="跳到当前播放的句子">⤓ 当前句</button>
          <button id="ctl-cal" title="时间线校准（全局偏移/单句调整）">⏱ 校准</button>
          <div class="speed-group">
            ${["0.75", "1", "1.25", "1.5"].map((v) =>
              `<button data-speed="${v}" class="${v === "1" ? "on" : ""}">${v}x</button>`).join("")}
          </div>
        </div>
      </div>
      <div class="cp-right">
        <div class="tabs">
          <button data-tab="read" class="active">精读全文</button>
          <button data-tab="cards">生词卡</button>
          <button data-tab="express">表达·词汇</button>
          <button data-tab="hard">长难句</button>
          <button data-tab="shadow">跟读</button>
        </div>
        <div class="tab-panel" id="tab-panel"></div>
      </div>
    </div>
  </div>`;

  const player = document.getElementById("player");
  player.preload = "metadata";

  const vocabIndex = buildVocabIndex(lesson);

  // ---- tabs ----
  const panels = { read: renderReadTab, cards: renderCardsTab, express: renderExpressTab, hard: renderHardTab, shadow: renderShadowTab };
  document.querySelectorAll(".tabs button").forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.tab = btn.dataset.tab;
      panels[state.tab]();
    };
  });
  renderReadTab();

  // ---- playback sync ----
  let activeId = -1;
  let follow = false;
  player.addEventListener("timeupdate", () => {
    handleAbLoop(player);
    const t = player.currentTime;
    const idx = findSentenceAt(t, sentences);
    if (idx !== null && idx !== activeId) {
      activeId = idx;
      highlightSentence(idx);
      if (follow && state.tab === "read") {
        const el = document.querySelector(`.sent[data-idx="${idx}"]`);
        if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }
  });

  document.getElementById("ctl-prev").onclick = () => {
    const idx = findSentenceAt(player.currentTime, sentences);
    if (idx !== null && sentences[idx].start < player.currentTime - 0.5) {
      player.currentTime = sentences[idx].start;
    } else if (idx !== null && idx > 0) {
      player.currentTime = sentences[idx - 1].start;
    }
    player.play();
  };

  const followBtn = document.getElementById("ctl-follow");
  followBtn.onclick = () => {
    follow = !follow;
    followBtn.textContent = follow ? "跟随中" : "不跟随";
    followBtn.classList.toggle("on", follow);
    if (follow && activeId >= 0) {
      const el = document.querySelector(`.sent[data-idx="${activeId}"]`);
      if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  };

  document.getElementById("ctl-goto").onclick = () => {
    if (activeId >= 0) {
      const el = document.querySelector(`.sent[data-idx="${activeId}"]`);
      if (el) {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        el.classList.add("flash");
        setTimeout(() => el.classList.remove("flash"), 1200);
      }
    } else {
      toast("请先播放视频");
    }
  };

  document.getElementById("ctl-cal").onclick = () => {
    openCalibrationPanel({
      player,
      sentences,
      rawSentences,
      activeId,
      onApplied: () => {
        panels[state.tab]();
      },
    });
  };

  const abBtn = document.getElementById("ctl-ab");
  abBtn.onclick = () => {
    if (!state.abLoop) {
      state.abLoop = { a: player.currentTime };
      abBtn.textContent = "A✓-B";
      abBtn.classList.add("on");
      toast("已标记 A 点，再次点击标记 B 点开始循环");
    } else if (state.abLoop.b === undefined) {
      let b = player.currentTime;
      if (b <= state.abLoop.a) b = state.abLoop.a + 3;
      state.abLoop.b = b;
      abBtn.textContent = "循环中";
      player.currentTime = state.abLoop.a;
      player.play();
    } else {
      state.abLoop = null;
      abBtn.textContent = "A-B";
      abBtn.classList.remove("on");
    }
  };

  function handleAbLoop(pl) {
    if (state.abLoop && state.abLoop.b !== undefined && pl.currentTime >= state.abLoop.b) {
      pl.currentTime = state.abLoop.a;
    }
  }

  document.querySelectorAll("[data-speed]").forEach((btn) => {
    btn.onclick = () => {
      player.playbackRate = parseFloat(btn.dataset.speed);
      document.querySelectorAll("[data-speed]").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
    };
  });

  window.__jumpTo = (sec) => {
    player.currentTime = sec;
    player.play();
    document.querySelector(".cp-left").scrollIntoView({ behavior: "smooth" });
  };
  window.__playRange = (start, end, rate) => {
    player.currentTime = start;
    if (rate) player.playbackRate = rate;
    player.play();
    const stopAt = () => {
      if (player.currentTime >= end) { player.pause(); player.removeEventListener("timeupdate", stopAt); }
      else requestAnimationFrame(stopAt);
    };
    requestAnimationFrame(stopAt);
  };

  function highlightSentence(idx) {
    document.querySelectorAll(".sent.active").forEach((el) => el.classList.remove("active"));
    const el = document.querySelector(`.sent[data-idx="${idx}"]`);
    if (el) el.classList.add("active");
  }

  function renderReadTab() {
    const panel = document.getElementById("tab-panel");
    panel.innerHTML = sentences.map((s) => {
      const read = state.progress.read.includes(s.id);
      const flags = [];
      if (s.uncertain) flags.push(`<span class="s-flags flag-uncertain" title="字幕轨与转写不一致，建议对照原声确认">⚠ 不确定</span>`);
      if (s.source === "embedded" && !s.uncertain) flags.push(`<span class="s-flags flag-embedded" title="取自内嵌字幕轨">内嵌字幕</span>`);
      if (s.source === "OCR" && !s.uncertain) flags.push(`<span class="s-flags flag-embedded" title="取自画面 OCR 字幕">OCR 字幕</span>`);
      return `
      <div class="sent ${s.lang === "zh" ? "zh" : ""}" data-idx="${s.id}">
        <span class="s-time">${fmtTime(s.start)} → ${fmtTime(s.end)}${flags.join("")}${read ? " ✓" : ""}</span>
        <div class="s-text">${markVocab(s.text, s.lang, vocabIndex)}</div>
        ${s.translation ? `<div class="s-trans">${esc(s.translation)}</div>` : ""}
        ${s.grammar ? `<div class="s-grammar">语法: ${esc(s.grammar)}</div>` : ""}
      </div>`;
    }).join("");

    panel.querySelectorAll(".sent").forEach((el) => {
      el.onclick = (e) => {
        if (e.target.closest(".vmark")) return;
        const idx = parseInt(el.dataset.idx, 10);
        player.currentTime = sentences[idx].start;
        player.play();
        if (!state.progress.read.includes(idx)) {
          state.progress.read.push(idx);
          saveProgress();
        }
      };
    });
  }

  function renderCardsTab() {
    const panel = document.getElementById("tab-panel");
    const vocab = lesson.vocabulary || [];
    if (!vocab.length) { panel.innerHTML = `<div class="panel-empty">本课程暂无生词数据</div>`; return; }
    const order = shuffle([...vocab.keys()]);
    panel.innerHTML = `
      <div style="margin-bottom:12px;color:var(--ink-2);font-size:13px">
        点击卡片翻面 · 底部按钮标记认识/不认识 · 点击句号跳转原句
      </div>
      <div class="card-grid">${order.map((vi) => cardHtml(vocab[vi], vi)).join("")}</div>`;

    panel.querySelectorAll(".lcard").forEach((card) => {
      card.onclick = (e) => {
        if (e.target.closest("a,button")) return;
        card.classList.toggle("flipped");
      };
    });
    panel.querySelectorAll("[data-mark]").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const card = btn.closest(".lcard");
        const term = card.dataset.term;
        state.progress.cards[term] = btn.dataset.mark;
        saveProgress();
        card.classList.remove("flipped");
        btn.classList.add(btn.dataset.mark === "known" ? "mark-known" : "mark-unknown");
      };
    });
    panel.querySelectorAll(".jump").forEach((a) => {
      a.onclick = (e) => { e.preventDefault(); window.__jumpTo(sentences[+a.dataset.sid].start); };
    });
  }

  function cardHtml(v, vi) {
    const known = state.progress.cards[v.term];
    return `
    <div class="lcard" data-term="${esc(v.term)}">
      <div class="face front">
        <div class="term">${esc(v.term)}<span class="pos">${esc(v.pos || "")}</span></div>
        <div class="phon">${esc(v.phonetic || "")}</div>
        <div class="def" style="visibility:hidden"></div>
        <div class="lcard-actions">
          <button data-mark="unknown" class="${known === "unknown" ? "mark-unknown" : ""}">不认识</button>
          <button data-mark="known" class="${known === "known" ? "mark-known" : ""}">认识</button>
        </div>
        ${(v.sentenceIds || []).length ? `<a class="jump" data-sid="${v.sentenceIds[0]}" href="#">→ 原句 ${fmtTime(sentences[v.sentenceIds[0]]?.start)}</a>` : ""}
      </div>
      <div class="face back">
        <div class="def">${esc(v.definition || "")}</div>
        <div class="ex">${esc(v.example || "")}</div>
      </div>
    </div>`;
  }

  function renderExpressTab() {
    const panel = document.getElementById("tab-panel");
    const expr = lesson.expressions || [];
    const dom = lesson.domain || [];
    const cult = lesson.culture || [];
    const item = (x) => `
      <div class="hs-card">
        <div class="hs-orig">${esc(x.phrase || x.term || x.topic)}</div>
        <div class="hs-structure">${esc(x.meaning || x.explanation || x.note || "")}</div>
        ${x.usage ? `<div class="hs-breakdown">${esc(x.usage)}</div>` : ""}
        ${(x.sentenceIds || []).length ? `<div class="hs-jump"><a class="jump" data-sid="${x.sentenceIds[0]}" href="#">→ 原句</a></div>` : ""}
      </div>`;
    panel.innerHTML = `
      <div class="section-title">💬 口语表达</div>
      ${expr.length ? expr.map((x) => item(x)).join("") : `<div class="panel-empty">暂无</div>`}
      <div class="section-title">⚖ 行业词汇</div>
      ${dom.length ? dom.map((x) => item(x)).join("") : `<div class="panel-empty">暂无</div>`}
      <div class="section-title">🌍 文化背景</div>
      ${cult.length ? cult.map((x) => item(x)).join("") : `<div class="panel-empty">暂无</div>`}`;
    panel.querySelectorAll(".jump").forEach((a) => {
      a.onclick = (e) => { e.preventDefault(); window.__jumpTo(sentences[+a.dataset.sid].start); };
    });
  }

  function renderHardTab() {
    const panel = document.getElementById("tab-panel");
    const hs = lesson.hardSentences || [];
    if (!hs.length) { panel.innerHTML = `<div class="panel-empty">本课程暂无长难句拆解</div>`; return; }
    panel.innerHTML = hs.map((h) => {
      const s = sentences[h.sentenceId] || {};
      return `
      <div class="hs-card">
        <div class="hs-orig">${esc(s.text || "")}</div>
        ${s.translation ? `<div class="s-trans">${esc(s.translation)}</div>` : ""}
        <div class="hs-structure">结构: ${esc(h.structure || "")}</div>
        <div class="hs-breakdown">${esc(h.breakdown || "")}</div>
        <div class="hs-jump"><a class="jump" data-sid="${h.sentenceId}" href="#">→ 播放原句</a></div>
      </div>`;
    }).join("");
    panel.querySelectorAll(".jump").forEach((a) => {
      a.onclick = (e) => { e.preventDefault(); window.__jumpTo(sentences[+a.dataset.sid].start); };
    });
  }

  function renderShadowTab() {
    const panel = document.getElementById("tab-panel");
    const sh = (lesson.shadowing || []).filter((s) => sentences[s.sentenceId]);
    if (!sh.length) { panel.innerHTML = `<div class="panel-empty">本课程暂无跟读数据</div>`; return; }
    panel.innerHTML = `
      <div style="margin-bottom:12px;color:var(--ink-2);font-size:13px">
        🔁 A-B 循环 + 0.75x 慢速播放 → 暂停 → 影子跟读
      </div>` + sh.map((s) => {
        const sent = sentences[s.sentenceId];
        return `
      <div class="sh-row">
        <div class="sh-text">
          ${esc(sent.text)}
          <div class="sh-tip">${esc(s.tip || "")}</div>
        </div>
        <button data-play="${s.sentenceId}" title="原速播放">▶</button>
        <button data-slow="${s.sentenceId}" title="0.75x 慢速播放">🐢</button>
        <button data-ab="${s.sentenceId}" title="A-B 循环这句">🔁</button>
      </div>`;
      }).join("");

    panel.querySelectorAll("[data-play]").forEach((b) => {
      b.onclick = () => window.__playRange(sentences[+b.dataset.play].start, sentences[+b.dataset.play].end);
    });
    panel.querySelectorAll("[data-slow]").forEach((b) => {
      b.onclick = () => window.__playRange(sentences[+b.dataset.slow].start, sentences[+b.dataset.slow].end, 0.75);
    });
    panel.querySelectorAll("[data-ab]").forEach((b) => {
      b.onclick = () => {
        const s = sentences[+b.dataset.ab];
        state.abLoop = { a: s.start, b: s.end };
        document.getElementById("ctl-ab").textContent = "循环中";
        document.getElementById("ctl-ab").classList.add("on");
        window.__jumpTo(s.start);
      };
    });
  }
}

/* ---------------- calibration panel ---------------- */
function openCalibrationPanel({ player, sentences, rawSentences, activeId, onApplied }) {
  document.querySelectorAll(".cal-overlay").forEach((el) => el.remove());
  const cal = state.course.calibration;
  const overlay = document.createElement("div");
  overlay.className = "cal-overlay";
  overlay.innerHTML = `
    <div class="cal-panel">
      <div class="cal-head">
        <b>⏱ 时间线校准</b>
        <button class="cal-close" title="关闭">✕</button>
      </div>

      <div class="section-title" style="margin-top:8px">全局偏移（所有句子整体平移）</div>
      <div class="cal-row">
        <button class="cal-shift" data-d="-0.5">−0.5s</button>
        <button class="cal-shift" data-d="-0.1">−0.1s</button>
        <input class="cal-global" type="number" step="0.1" value="${esc(String(cal.global_offset || 0))}" style="width:90px;text-align:center">
        <button class="cal-shift" data-d="0.1">+0.1s</button>
        <button class="cal-shift" data-d="0.5">+0.5s</button>
        <button class="cal-reset" title="清除全局偏移">归零</button>
      </div>
      <div class="cal-hint">字幕整体比声音慢 → 加负值；快 → 加正值。改动即时生效并自动保存。</div>
      <div style="margin:8px 0">
        <button class="cal-pick-start"> ◀ 取当前播放点为参考</button>
        <span class="cal-ref" style="font-size:12px;color:var(--ink-2)">未设置</span>
      </div>
      <div class="cal-apply-global" style="display:none">
        <button class="primary cal-align-here">将选中句子对齐到该播放点</button>
        <span style="font-size:12px;color:var(--ink-2)">（选中下面任一句后可用）</span>
      </div>

      <div class="section-title">单句调整（点击句子选中，可设起点/终点或微调）</div>
      <div class="cal-list"></div>
    </div>`;

  const globalInput = overlay.querySelector(".cal-global");
  const listEl = overlay.querySelector(".cal-list");
  const refEl = overlay.querySelector(".cal-ref");
  const applyWrap = overlay.querySelector(".cal-apply-global");
  let refPoint = null;
  let selId = null;

  function renderList() {
    const items = sentences.map((s) => {
      const o = cal.overrides[String(s.id)];
      const isSel = selId === s.id;
      return `
      <div class="cal-item ${isSel ? "sel" : ""}" data-sid="${s.id}">
        <div class="ci-main" data-act="sel">
          <span class="s-time">${fmtTime(s.start)} → ${fmtTime(s.end)}</span>
          <span class="ci-text">${esc(s.text.slice(0, 40))}</span>
          ${o ? '<span class="badge">已调</span>' : ""}
        </div>
        ${isSel ? `
        <div class="ci-edit">
          <label>起点 <input class="ci-start" type="number" step="0.1" value="${s.start.toFixed(2)}"></label>
          <label>终点 <input class="ci-end" type="number" step="0.1" value="${s.end.toFixed(2)}"></label>
          <button class="ci-set-start" data-act="set-start">起点=播放点</button>
          <button class="ci-set-end" data-act="set-end">终点=播放点</button>
          <button class="ci-nudge" data-act="nudge-l">◀ 0.5s</button>
          <button class="ci-nudge" data-act="nudge-r">0.5s ▶</button>
          <button class="ci-clear" data-act="clear" ${o ? "" : "disabled"}>还原</button>
          <button class="primary ci-play" data-act="play">▶ 试听</button>
        </div>` : ""}
      </div>`;
    }).join("");
    listEl.innerHTML = items || `<div class="panel-empty">无句子</div>`;
  }

  function persist() {
    globalInput.value = Number(cal.global_offset || 0).toFixed(1);
    saveCalibration().then(() => {}).catch((e) => toast("保存失败: " + e.message));
  }

  overlay.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act],[data-d],.cal-close,.cal-reset,.cal-pick-start,.cal-align-here");
    if (!btn) {
      if (e.target === overlay) close();
      return;
    }
    if (btn.classList.contains("cal-close")) return close();
    if (btn.classList.contains("cal-reset")) { cal.global_offset = 0; persist(); return; }
    if (btn.classList.contains("cal-pick-start")) {
      refPoint = player.currentTime;
      refEl.textContent = `参考点 ${refPoint.toFixed(2)}s`;
      return;
    }
    if (btn.classList.contains("cal-align-here")) {
      if (refPoint === null || selId === null) { toast("先取参考点并选中句子"); return; }
      const raw = rawSentences.find((s) => s.id === selId);
      const dur = (raw.end - raw.start);
      cal.overrides[String(selId)] = {
        start: Math.max(0, refPoint),
        end: Math.max(0, refPoint + dur),
      };
      persist();
      refreshSentences();
      renderList();
      return;
    }
    if (btn.dataset.d !== undefined) {
      cal.global_offset = Math.round(((parseFloat(cal.global_offset) || 0) + parseFloat(btn.dataset.d)) * 10) / 10;
      persist();
      return;
    }

    const act = btn.dataset.act;
    const item = btn.closest(".cal-item");
    const sid = item ? parseInt(item.dataset.sid, 10) : null;
    if (act === "sel") {
      selId = sid;
      applyWrap.style.display = "block";
      renderList();
      return;
    }
    if (act === "play") {
      const s = sentences.find((x) => x.id === sid);
      player.currentTime = s.start;
      player.play();
      return;
    }
    if (act === "clear") {
      delete cal.overrides[String(sid)];
      persist();
      refreshSentences();
      renderList();
      return;
    }
    const raw = rawSentences.find((s) => s.id === sid);
    const dur = raw.end - raw.start;
    if (act === "set-start") {
      const st = player.currentTime;
      cal.overrides[String(sid)] = { start: Math.max(0, st), end: Math.max(0, st + dur) };
    } else if (act === "set-end") {
      const st = cal.overrides[String(sid)]?.start ?? raw.start;
      cal.overrides[String(sid)] = { start: st, end: Math.max(st + 0.3, player.currentTime) };
    } else if (act === "nudge-l" || act === "nudge-r") {
      const cur = cal.overrides[String(sid)] || { start: raw.start, end: raw.end };
      const d = act === "nudge-l" ? -0.5 : 0.5;
      cal.overrides[String(sid)] = {
        start: Math.max(0, cur.start + d),
        end: Math.max(0.3, cur.end + d),
      };
    }
    persist();
    refreshSentences();
    renderList();
  });

  globalInput.addEventListener("change", () => {
    cal.global_offset = parseFloat(globalInput.value) || 0;
    persist();
    refreshSentences();
  });
  listEl.addEventListener("change", (e) => {
    const inp = e.target;
    if (!inp.classList.contains("ci-start") && !inp.classList.contains("ci-end")) return;
    const sid = parseInt(inp.closest(".cal-item").dataset.sid, 10);
    const raw = rawSentences.find((s) => s.id === sid);
    const cur = cal.overrides[String(sid)] || { start: raw.start, end: raw.end };
    let start = cur.start, end = cur.end;
    if (inp.classList.contains("ci-start")) start = Math.max(0, parseFloat(inp.value) || 0);
    else end = Math.max(start + 0.3, parseFloat(inp.value) || 0);
    cal.overrides[String(sid)] = { start, end };
    persist();
    refreshSentences();
    renderList();
  });

  function refreshSentences() {
    const list = calibratedSentences();
    sentences.splice(0, sentences.length, ...list);
    if (onApplied) onApplied();
  }

  function close() {
    overlay.remove();
    refreshSentences();
  }

  renderList();
  document.body.appendChild(overlay);
}

/* ================= Settings (model config) ================= */
function renderSettings() {
  document.title = "模型配置 · DeepRead";
  $app.innerHTML = shell("settings", `
    <div class="page" style="max-width:760px">
      <h1>模型配置</h1>
      <p style="color:var(--ink-2);font-size:13.5px;margin-top:-8px">
        添加多个中转 API（OpenAI 兼容），随时切换。转写 / Chat / OCR 可使用不同的 API。Key 保存在服务器本地。
      </p>

      <div class="cfg-card">
        <div class="cfg-card-head">
          <span class="cfg-card-title">🔌 API 提供商</span>
          <button id="prov-add" class="primary" style="padding:6px 14px">+ 添加</button>
        </div>
        <div id="prov-list"></div>
      </div>

      <div class="cfg-card">
        <div class="cfg-card-head"><span class="cfg-card-title">🎯 角色分配</span></div>

        <div class="role-grid">
          <div class="role-block">
            <div class="role-name">📝 翻译与精讲 <i>Chat</i></div>
            <div class="form-row"><select id="role-chat"></select></div>
            <div class="form-row" style="display:flex;gap:8px">
              <select id="cfg-chat" style="flex:1;min-width:0"></select>
              <button id="cfg-refresh-chat" style="flex-shrink:0" title="获取模型列表">↻</button>
            </div>
          </div>
          <div class="role-block">
            <div class="role-name">🎙 语音转写 <i>ASR</i></div>
            <div class="form-row"><select id="role-asr"></select></div>
            <div class="form-row" style="display:flex;gap:8px">
              <select id="cfg-asr" style="flex:1;min-width:0"></select>
              <button id="cfg-refresh-asr" style="flex-shrink:0" title="获取模型列表">↻</button>
            </div>
            <input id="cfg-asr-custom" type="text" placeholder="手动输入模型名（覆盖上面的选择）">
          </div>
          <div class="role-block">
            <div class="role-name">🖼 画面字幕 OCR <i>Vision</i></div>
            <div class="form-row"><select id="role-vision"></select></div>
            <div class="form-row" style="display:flex;gap:8px">
              <input id="cfg-vision" type="text" placeholder="如 qwen3.5-ocr（留空用 Chat 模型）" style="flex:1;min-width:0">
              <button id="cfg-refresh-vision" style="flex-shrink:0" title="获取模型列表">↻</button>
            </div>
          </div>
        </div>

        <div class="auto-switch-box">
          <label class="switch-row">
            <input type="checkbox" id="cfg-autoswitch">
            <span class="switch-ui"></span>
            <div class="switch-text">
              <b>自动切换可用模型</b>
              <small>模型报错（免费额度用尽、下线等）时，自动按下面列表的顺序尝试下一个。关闭则只用选定模型。</small>
            </div>
          </label>
          <div id="model-priority" style="display:none;margin-top:12px"></div>
        </div>
      </div>

      <div class="form-actions">
        <button class="primary" id="cfg-save" style="padding:9px 26px">保存配置</button>
        <button id="cfg-test">测试连接</button>
      </div>
      <div class="test-result" id="test-result"></div>
    </div>`);

  let providers = [];
  let cfgCache = null;
  let modelCache = {};          // provider_id -> {models, asr_models}
  let priorityModels = [];      // failover priority list (working copy)

  async function init() {
    cfgCache = await api("/api/config");
    providers = (cfgCache.providers || []).map((p) => ({ ...p }));
    drawProviders();
    drawRoles();
    document.getElementById("role-asr").value = cfgCache.asr_provider || "";
    document.getElementById("role-chat").value = cfgCache.chat_provider || "";
    document.getElementById("role-vision").value = cfgCache.vision_provider || "";
    document.getElementById("cfg-vision").value = cfgCache.vision_model || "";
    if (cfgCache.asr_model) {
      fillAsrSelect([cfgCache.asr_model], cfgCache.asr_model);
      document.getElementById("cfg-asr-custom").value = "";
    }
    if (cfgCache.chat_model) fillChatSelect([cfgCache.chat_model], cfgCache.chat_model);
    // auto-switch state + priority list
    priorityModels = (cfgCache.chat_models || []).slice();
    if (cfgCache.chat_model && !priorityModels.includes(cfgCache.chat_model)) {
      priorityModels.unshift(cfgCache.chat_model);
    }
    const asw = document.getElementById("cfg-autoswitch");
    asw.checked = !!cfgCache.auto_model_switch;
    drawPriority();
    asw.onchange = drawPriority;
  }

  function drawPriority() {
    const wrap = document.getElementById("model-priority");
    const on = document.getElementById("cfg-autoswitch").checked;
    wrap.style.display = on ? "block" : "none";
    if (!on) return;
    wrap.innerHTML = `
      <div class="cal-hint" style="margin:0 0 8px">模型优先级（从上到下依次尝试）。▲ 上移，✕ 移除：</div>
      <div class="prio-list" id="prio-list"></div>
      <div style="display:flex;gap:8px;margin-top:8px">
        <select id="prio-add-sel" style="flex:1;min-width:0"></select>
        <button id="prio-add-btn" style="flex-shrink:0">+ 加入列表</button>
      </div>`;
    const list = document.getElementById("prio-list");
    const sel = document.getElementById("prio-add-sel");
    const pid = document.getElementById("role-chat").value;
    const known = [...(modelCache[pid]?.models || [])];
    const cur = document.getElementById("cfg-chat").value;
    if (cur && !known.includes(cur)) known.unshift(cur);
    for (const m of priorityModels) {
      if (!known.includes(m)) known.push(m);
    }
    sel.innerHTML = known.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("") ||
      `<option value="">（先点 ↻ 获取模型）</option>`;
    list.innerHTML = priorityModels.map((m, i) => `
      <div class="prio-item" draggable="true" data-i="${i}">
        <span class="prio-idx">${i + 1}</span>
        <span class="prio-name">${esc(m)}</span>
        <button class="prio-up" data-up="${i}" title="上移">▲</button>
        <button class="prio-del" data-del="${i}" title="移除">✕</button>
      </div>`).join("") || `<div class="panel-empty" style="padding:12px">列表为空：从下面选择加入</div>`;

    list.querySelectorAll("[data-del]").forEach((b) => {
      b.onclick = () => { priorityModels.splice(+b.dataset.del, 1); drawPriority(); };
    });
    list.querySelectorAll("[data-up]").forEach((b) => {
      b.onclick = () => {
        const i = +b.dataset.up;
        if (i > 0) {
          [priorityModels[i - 1], priorityModels[i]] = [priorityModels[i], priorityModels[i - 1]];
          drawPriority();
        }
      };
    });
    let dragIdx = null;
    list.querySelectorAll(".prio-item").forEach((el) => {
      el.ondragstart = () => { dragIdx = +el.dataset.i; el.classList.add("dragging"); };
      el.ondragend = () => { el.classList.remove("dragging"); };
      el.ondragover = (e) => {
        e.preventDefault();
        const i = +el.dataset.i;
        if (dragIdx === null || dragIdx === i) return;
        const [m] = priorityModels.splice(dragIdx, 1);
        priorityModels.splice(i, 0, m);
        dragIdx = i;
        drawPriority();
      };
    });
    document.getElementById("prio-add-btn").onclick = () => {
      const v = sel.value;
      if (v && !priorityModels.includes(v)) { priorityModels.push(v); drawPriority(); }
    };
  }

  function drawProviders() {
    const wrap = document.getElementById("prov-list");
    if (!providers.length) {
      wrap.innerHTML = `<div class="panel-empty" style="padding:16px">还没有 API，点击下方添加</div>`;
    } else {
      wrap.innerHTML = providers.map((p, i) => `
        <div class="prov-card" data-pi="${i}">
          <div style="display:flex;gap:10px;align-items:center;margin-bottom:8px">
            <input class="p-name" value="${esc(p.name)}" placeholder="名称（如 beichenapi）" style="flex:1">
            <button class="danger p-del" title="删除">✕</button>
          </div>
          <div class="form-row" style="margin-bottom:8px"><input class="p-url" value="${esc(p.base_url)}" placeholder="Base URL（https://.../v1）"></div>
          <div class="form-row" style="margin-bottom:0"><input class="p-key" type="password" value="${esc(p.api_key)}" placeholder="API Key（sk-...）"></div>
        </div>`).join("");
    }
    wrap.querySelectorAll(".p-del").forEach((btn) => {
      btn.onclick = () => {
        const i = +btn.closest("[data-pi]").dataset.pi;
        if (!confirm(`删除 API「${providers[i].name}」？`)) return;
        providers.splice(i, 1);
        drawProviders(); drawRoles();
      };
    });
  }

  function collectProviders() {
    document.querySelectorAll("#prov-list [data-pi]").forEach((card) => {
      const i = +card.dataset.pi;
      if (providers[i]) {
        providers[i].name = card.querySelector(".p-name").value.trim() || `API ${i + 1}`;
        providers[i].base_url = card.querySelector(".p-url").value.trim();
        providers[i].api_key = card.querySelector(".p-key").value.trim();
      }
    });
  }

  function drawRoles() {
    collectProviders();
    const opts = () => providers.map((p, i) =>
      `<option value="${esc(p.id)}">${esc(p.name || `API ${i + 1}`)}</option>`).join("");
    const asrSel = document.getElementById("role-asr");
    const chatSel = document.getElementById("role-chat");
    const visSel = document.getElementById("role-vision");
    const prevAsr = asrSel.value, prevChat = chatSel.value, prevVis = visSel.value;
    asrSel.innerHTML = opts() || `<option value="">（请先添加 API）</option>`;
    chatSel.innerHTML = opts() || `<option value="">（请先添加 API）</option>`;
    visSel.innerHTML = `<option value="">（用 Chat API）</option>` + (opts() || "");
    if (prevAsr && providers.some((p) => p.id === prevAsr)) asrSel.value = prevAsr;
    if (prevChat && providers.some((p) => p.id === prevChat)) chatSel.value = prevChat;
    if (prevVis && providers.some((p) => p.id === prevVis)) visSel.value = prevVis;
    asrSel.onchange = () => fillAsrSelect([], "");
    chatSel.onchange = () => fillChatSelect([], "");
  }

  function fillAsrSelect(models, selected) {
    const sel = document.getElementById("cfg-asr");
    const keepCustom = document.getElementById("cfg-asr-custom").value.trim();
    sel.innerHTML = models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    if (selected && ![...sel.options].some((o) => o.value === selected)) {
      sel.insertAdjacentHTML("beforeend", `<option value="${esc(selected)}">${esc(selected)}</option>`);
    }
    if (keepCustom && ![...sel.options].some((o) => o.value === keepCustom)) {
      sel.insertAdjacentHTML("beforeend", `<option value="${esc(keepCustom)}">${esc(keepCustom)}</option>`);
    }
    sel.value = selected || keepCustom || sel.value || "";
  }

  function fillChatSelect(models, selected) {
    const sel = document.getElementById("cfg-chat");
    sel.innerHTML = models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    if (selected && ![...sel.options].some((o) => o.value === selected)) {
      sel.insertAdjacentHTML("beforeend", `<option value="${esc(selected)}">${esc(selected)}</option>`);
    }
    sel.value = selected || sel.value || "";
  }

  async function fetchModels(providerId) {
    if (modelCache[providerId]) return modelCache[providerId];
    const res = await api(`/api/config/models?provider_id=${encodeURIComponent(providerId)}`);
    modelCache[providerId] = res;
    return res;
  }

  document.getElementById("prov-add").onclick = async () => {
    collectProviders();
    providers.push({ id: "", name: "", base_url: "", api_key: "" });
    drawProviders(); drawRoles();
  };

  document.getElementById("cfg-refresh-asr").onclick = async () => {
    await saveConfigQuiet();
    const pid = document.getElementById("role-asr").value;
    if (!pid) { toast("请先添加并保存 API"); return; }
    toast("获取模型列表中…");
    try {
      const res = await fetchModels(pid);
      fillAsrSelect(res.asr_models || [], "");
      toast(`获取到 ${(res.asr_models || res.models || []).length} 个模型`);
    } catch (err) { toast("获取失败: " + err.message); }
  };

  document.getElementById("cfg-refresh-chat").onclick = async () => {
    await saveConfigQuiet();
    const pid = document.getElementById("role-chat").value;
    if (!pid) { toast("请先添加并保存 API"); return; }
    toast("获取模型列表中…");
    try {
      const res = await fetchModels(pid);
      fillChatSelect(res.models || [], "");
      toast(`获取到 ${(res.models || []).length} 个模型`);
    } catch (err) { toast("获取失败: " + err.message); }
  };

  async function saveConfigQuiet() {
    collectProviders();
    const custom = document.getElementById("cfg-asr-custom").value.trim();
    const asrVal = custom || document.getElementById("cfg-asr").value;
    try {
      await api("/api/config", {
        method: "PUT",
        body: JSON.stringify({
          providers,
          asr_provider: document.getElementById("role-asr").value,
          chat_provider: document.getElementById("role-chat").value,
          vision_provider: document.getElementById("role-vision").value,
          asr_model: asrVal === "__custom__" ? "" : asrVal,
          chat_model: document.getElementById("cfg-chat").value,
          vision_model: document.getElementById("cfg-vision").value.trim(),
          auto_model_switch: document.getElementById("cfg-autoswitch").checked,
          chat_models: priorityModels,
        }),
      });
      cfgCache = await api("/api/config");
      providers = (cfgCache.providers || []).map((p) => ({ ...p }));
    } catch (err) { toast(err.message); }
  }

  document.getElementById("cfg-save").onclick = async () => {
    await saveConfigQuiet();
    const custom = document.getElementById("cfg-asr-custom").value.trim();
    const asrVal = custom || document.getElementById("cfg-asr").value;
    if (!providers.length) { toast("请至少添加一个 API"); return; }
    if (!asrVal) { toast("请选择转写模型"); return; }
    if (!document.getElementById("cfg-chat").value) { toast("请选择 Chat 模型"); return; }
    await saveConfigQuiet();
    toast("已保存");
  };

  document.getElementById("cfg-test").onclick = async () => {
    const el = document.getElementById("test-result");
    el.className = "test-result"; el.textContent = "测试中…";
    await saveConfigQuiet();
    try {
      const res = await api("/api/config/test", { method: "POST", body: JSON.stringify({}) });
      const parts = [];
      parts.push(res.chat ? "✓ Chat 可用" : "✗ Chat 不可用");
      parts.push(res.transcribe ? "✓ 转写端点可达" : "✗ 转写端点不可用");
      el.className = "test-result " + (res.chat || res.transcribe ? "ok" : "fail");
      el.textContent = parts.join("　") + (res.detail ? ` · ${res.detail}` : "");
    } catch (err) {
      el.className = "test-result fail";
      el.textContent = "连接失败: " + err.message;
    }
  };

  init();
}

/* ================= Backup ================= */
function renderBackup() {
  document.title = "备份与恢复 · DeepRead";
  $app.innerHTML = shell("backup", `
    <div class="page" style="max-width:720px">
      <h1>备份与恢复</h1>
      <div class="form-row" style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
        <label style="display:flex;gap:6px;align-items:center;margin:0">
          <input type="checkbox" id="bk-enabled" style="width:auto"> 每日自动备份
        </label>
        <label style="margin:0;display:flex;gap:6px;align-items:center">时间
          <input type="time" id="bk-time" style="width:110px"></label>
        <label style="margin:0;display:flex;gap:6px;align-items:center">保留数量
          <input type="number" id="bk-keep" min="1" max="90" style="width:70px"></label>
        <label style="display:flex;gap:6px;align-items:center;margin:0">
          <input type="checkbox" id="bk-media" style="width:auto"> 包含媒体文件（体积大）
        </label>
        <button id="bk-save-settings">保存设置</button>
        <button id="bk-now" class="primary">立即备份</button>
      </div>
      <div class="form-row" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <label style="margin:0;font-size:13px;color:var(--ink-2)">恢复：选择备份 zip 上传</label>
        <input type="file" id="bk-restore-file" accept=".zip" style="width:auto">
        <button id="bk-restore" class="danger">恢复（覆盖当前数据）</button>
      </div>
      <div class="cal-hint" style="margin-bottom:8px">
        备份包含 API 配置、全部课程（转写/精讲/进度/校准）；媒体文件可选。
        换部署环境：新机器 docker compose up 后，在此上传备份恢复，再按提示补传媒体。
      </div>
      <div id="bk-list"></div>
      <div id="bk-missing" style="margin-top:10px"></div>
    </div>`);

  let cfgCache = null;
  let providers = [];

  async function loadBackupSettings() {
    cfgCache = await api("/api/config");
    document.getElementById("bk-enabled").checked = !!cfgCache.backup_enabled;
    document.getElementById("bk-time").value = cfgCache.backup_time || "04:00";
    document.getElementById("bk-keep").value = cfgCache.backup_keep || 3;
    document.getElementById("bk-media").checked = cfgCache.backup_include_media !== false;
  }

  async function refreshBackups() {
    const listEl = document.getElementById("bk-list");
    try {
      const items = await api("/api/backups");
      if (!items.length) {
        listEl.innerHTML = `<div class="panel-empty" style="padding:14px">暂无备份</div>`;
        return;
      }
      listEl.innerHTML = `<div class="table-scroll"><table class="bk-table"><tr><th>时间</th><th>类型</th><th>大小</th><th></th></tr>` +
        items.map((b) => `
        <tr>
          <td>${esc((b.created_at || "").replace("T", " ").slice(0, 19))}</td>
          <td>${b.auto ? "自动" : "手动"}${b.has_media ? ' <span class="badge">含媒体</span>' : ""}</td>
          <td>${b.size_mb} MB</td>
          <td>
            <a href="/api/backups/${esc(b.file)}/download" download>下载</a>
            <button class="danger bk-del" data-f="${esc(b.file)}" style="padding:2px 8px;font-size:12px">删除</button>
          </td>
        </tr>`).join("") + `</table></div>`;
      listEl.querySelectorAll(".bk-del").forEach((btn) => {
        btn.onclick = async () => {
          if (!confirm("删除这个备份？")) return;
          await api(`/api/backups/${btn.dataset.f}`, { method: "DELETE" });
          refreshBackups();
        };
      });
    } catch (err) {
      listEl.innerHTML = `<div class="panel-empty" style="padding:14px">加载失败: ${esc(err.message)}</div>`;
    }
  }

  document.getElementById("bk-save-settings").onclick = async () => {
    try {
      await api("/api/config", {
        method: "PUT",
        body: JSON.stringify({
          backup_enabled: document.getElementById("bk-enabled").checked,
          backup_time: document.getElementById("bk-time").value || "04:00",
          backup_keep: parseInt(document.getElementById("bk-keep").value, 10) || 3,
          backup_include_media: document.getElementById("bk-media").checked,
        }),
      });
      toast("备份设置已保存");
    } catch (err) { toast(err.message); }
  };

  document.getElementById("bk-now").onclick = async () => {
    try {
      await api("/api/backups/create", { method: "POST" });
      toast("备份完成");
      refreshBackups();
    } catch (err) { toast(err.message); }
  };

  document.getElementById("bk-restore").onclick = async () => {
    const fi = document.getElementById("bk-restore-file");
    if (!fi.files[0]) { toast("请先选择备份 zip"); return; }
    if (!confirm("恢复会覆盖当前的 API 配置和所有课程数据，确定继续？")) return;
    const fd = new FormData();
    fd.append("file", fi.files[0]);
    try {
      const res = await api("/api/backups/restore", { method: "POST", body: fd });
      let msg = `恢复完成：${res.courses} 门课程${res.config ? " + API 配置" : ""}`;
      if (res.media_restored) msg += `；媒体 ${res.media_restored} 个`;
      if (res.missing_media?.length) {
        msg += `；${res.missing_media.length} 个课程缺媒体（见下方）`;
        renderMissingMedia(res.missing_media);
      } else {
        document.getElementById("bk-missing").innerHTML = "";
      }
      toast(msg, 5000);
      loadBackupSettings();
      refreshBackups();
    } catch (err) { toast("恢复失败: " + err.message, 5000); }
  };

  function renderMissingMedia(missing) {
    document.getElementById("bk-missing").innerHTML = `
      <div class="section-title">缺媒体文件的课程（重新上传后自动关联）</div>` +
      missing.map((m) => `
      <div class="hs-card" style="padding:10px 14px">
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <span style="flex:1">${esc(m.title)}</span>
          <span class="cal-hint">需要文件名: ${esc(m.media_file)}</span>
          <label style="margin:0">
            <input type="file" class="mm-file" data-cid="${esc(m.course_id)}" style="width:auto">
          </label>
        </div>
      </div>`).join("");
    document.querySelectorAll(".mm-file").forEach((inp) => {
      inp.onchange = async () => {
        if (!inp.files[0]) return;
        const fd = new FormData();
        fd.append("file", inp.files[0]);
        fd.append("course_id", inp.dataset.cid);
        try {
          await api("/api/backups/media", { method: "POST", body: fd });
          toast("媒体已关联");
          inp.closest(".hs-card").style.display = "none";
        } catch (err) { toast(err.message); }
      };
    });
  }

  loadBackupSettings();
  refreshBackups();
}

/* ---------------- helpers ---------------- */
function findSentenceAt(t, sentences) {
  for (let i = 0; i < sentences.length; i++) {
    if (t >= sentences[i].start && t < sentences[i].end + 0.5) return i;
  }
  if (sentences.length && t >= sentences[sentences.length - 1].end) return sentences.length - 1;
  return null;
}

function shuffle(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

function buildVocabIndex(lesson) {
  const idx = [];
  for (const v of lesson.vocabulary || []) {
    if (!v.term || v.term.length < 2) continue;
    if (!/[一-鿿]/.test(v.term)) idx.push(v);
  }
  idx.sort((a, b) => b.term.length - a.term.length);
  return idx;
}

function markVocab(text, lang, vocabIndex) {
  if (lang !== "en" || !vocabIndex.length) return esc(text);
  let html = esc(text);
  for (const v of vocabIndex) {
    const term = esc(v.term);
    const re = new RegExp(`(?<![\\w${term}])(${term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})(?![\\w])`, "gi");
    html = html.replace(re, (m) =>
      `<span class="vmark" data-term="${term}">${m}<span class="vpop">${esc(v.definition || "")}</span></span>`);
  }
  return html;
}

route();
