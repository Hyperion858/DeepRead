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

// server stores UTC ISO timestamps; display as China Standard Time
function fmtCNTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
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
  { hash: "#/backup",   key: "backup",   icon: "💾", label: "备份恢复" },
  { hash: "#/logs",     key: "logs",     icon: "📋", label: "日志" },
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
  if (hash.startsWith("#/logs")) return renderLogs();
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
  highlightFromHash();
  state.pollTimer = setInterval(refreshMaterials, 3000);
}

// read ?hl=<id> from the hash and flash-highlight that row/card
function highlightFromHash() {
  const m = location.hash.match(/[?&]hl=([\w-]+)/);
  if (!m) return;
  const id = m[1];
  requestAnimationFrame(() => {
    const el = document.querySelector(`[data-mid="${id}"], [data-id="${id}"]`);
    if (el) {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      el.classList.add("hl-flash");
      setTimeout(() => el.classList.remove("hl-flash"), 2500);
    }
    // clean the query from the hash so refreshes don't re-highlight
    history.replaceState(null, "", location.pathname + location.hash.split("?")[0]);
  });
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
      <tr><th>标题</th><th>类型</th><th>大小</th><th>添加时间</th><th>来源</th><th>状态</th><th style="width:220px"></th></tr>` +
      list.map((m) => {
        const statusLabel = { ready: "就绪", downloading: "下载中", error: "下载失败" }[m.status || "ready"] || "就绪";
        const downloading = m.status === "downloading";
        return `
      <tr data-mid="${esc(m.id)}">
        <td class="mat-title-cell">
          ${m.course_id ? `<a class="badge ready" style="text-decoration:none;vertical-align:middle;margin-right:8px" href="#/course/${esc(m.course_id)}">已有课程 →</a>` : ""}<span class="mat-title">${esc(m.title)}</span>
          <button class="mat-rename" data-rename="${esc(m.id)}" title="重命名">✎ 改名</button>
          ${m.media_exists ? `<a class="mat-play" data-play="${esc(m.id)}" href="#" title="播放">▶ 播放</a>` : ""}
        </td>
        <td>${m.kind === "audio" ? "音频" : "视频"}</td>
        <td>${m.size_mb} MB</td>
        <td>${fmtCNTime(m.created_at)}</td>
        <td>${m.source_url ? `<a href="${esc(m.source_url)}" target="_blank" rel="noopener" style="color:var(--accent)">链接</a>` : "上传"}</td>
        <td>${downloading ? '<span class="spinner"></span> ' : ""}<span class="${m.status === "error" ? "flag-uncertain" : ""}">${statusLabel}${m.error ? " · " + esc(m.error.slice(0, 40)) : ""}</span></td>
        <td>
          ${m.course_id && m.media_exists ? `<a class="badge ready" style="text-decoration:none" href="#/course/${esc(m.course_id)}?hl=material">查看课程 →</a>` : ""}
          ${!m.course_id && !downloading ? `<button class="primary" data-genc="${esc(m.id)}" ${m.media_exists ? "" : "disabled"}>生成课程</button>` : ""}
          ${m.media_exists && m.course_id ? `<button data-regen="${esc(m.course_id)}" title="重新生成课程">↻</button>` : ""}
          <button class="danger" data-del="${esc(m.id)}">删除</button>
        </td>
      </tr>`;
      }).join("") + `</table></div>`;

    wrap.querySelectorAll("[data-genc]").forEach((btn) => {
      btn.onclick = () => {
        api(`/api/materials/${btn.dataset.genc}/generate`, {
          method: "POST", body: JSON.stringify({ channels: ["embedded", "ocr", "asr"] }),
        })
          .then((res) => { toast("课程生成已开始"); location.hash = `#/course/${res.id}`; })
          .catch((err) => toast(err.message));
      };
    });
    wrap.querySelectorAll("[data-play]").forEach((a) => {
      a.onclick = (e) => { e.preventDefault(); openMaterialPlayer(a.dataset.play); };
    });
    wrap.querySelectorAll("[data-regen]").forEach((btn) => {
      btn.onclick = () => {
        confirmDialog("重新生成课程", "将用当前配置重新转写并覆盖这门课程的精讲内容，继续？", () => {
          api(`/api/courses/${btn.dataset.regen}/generate`, {
            method: "POST", body: JSON.stringify({ channels: ["embedded", "ocr", "asr"] }),
          })
            .then(() => toast("重新生成已开始"))
            .catch((err) => toast(err.message));
        });
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
  highlightFromHash();
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
      <div class="course-card" data-id="${c.id}" data-mid="${esc(c.material_id || "")}">
        <div class="cc-title">${esc(c.title)}</div>
        <div class="cc-meta">
          <span class="badge ${badgeCls}">${stageLabel}</span>
          <span>${fmtDuration(c.duration)}</span>
          <span>${c.sentence_count ? c.sentence_count + " 句" : ""}</span>
          <span title="添加时间">${fmtCNTime(c.created_at)}</span>
        </div>
        ${working ? `<div class="progressbar"><div style="width:${Math.round((c.progress || 0) * 100)}%"></div></div>
                     <div class="cc-detail">${esc(c.detail || "")}</div>` : ""}
        ${c.stage === "ready" ? `<div class="progressbar"><div style="width:${c.learned_percent || 0}%"></div></div>
                                 <div class="cc-detail">已学 ${c.learned_percent || 0}%</div>` : ""}
        ${c.stage === "uploaded" ? `<button class="primary" data-gen="${c.id}">开始生成</button>` : ""}
        ${c.stage === "error" ? `<div class="cc-detail" style="color:var(--red)">${esc(c.detail || "")}</div>
                                 <button class="primary" data-gen="${c.id}">重试</button>` : ""}
        ${c.material_id ? `<a class="cc-viewmat" href="#/library?hl=${esc(c.material_id)}" title="查看素材" onclick="event.stopPropagation()">📥 素材</a>` : ""}        <button class="cc-del" data-del="${c.id}" title="删除">✕</button>
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
  const cal = state.course?.calibration || { global_offset: 0, overrides: {}, text_edits: {} };
  const off = parseFloat(cal.global_offset) || 0;
  const ovr = cal.overrides || {};
  const edits = cal.text_edits || {};
  return (lesson?.sentences || []).map((s) => {
    const o = ovr[String(s.id)];
    return {
      ...s,
      text: edits[String(s.id)] ?? s.text,          // manual text correction
      start: o ? o.start : s.start + off,
      end: o ? o.end : s.end + off,
      edited: edits[String(s.id)] !== undefined,
    };
  });
}

async function saveCalibration() {
  const cal = state.course.calibration;
  await api(`/api/courses/${state.course.meta.id}/calibration`, {
    method: "POST",
    body: JSON.stringify({
      global_offset: cal.global_offset,
      overrides: cal.overrides,
      text_edits: cal.text_edits || {},
    }),
  });
}

function drawCoursePage() {
  const { meta, status, lesson } = state.course;
  if (status.stage !== "ready") {
    const working = ["downloading", "transcribing", "subtitles", "ocr", "aligning", "generating", "channels"].includes(status.stage);
    const pct = Math.round((status.progress || 0) * 100);
    const stageNames = {
      downloading: "下载视频", uploaded: "等待生成", transcribing: "语音转写",
      subtitles: "内嵌字幕", ocr: "OCR 硬字幕", channels: "通道处理",
      aligning: "对齐融合", generating: "翻译与精讲",
    };
    const stageOrder = ["downloading", "uploaded", "transcribing", "subtitles", "ocr", "channels", "aligning", "generating"];
    const cur = stageNames[status.stage] || status.stage;
    const curIdx = stageOrder.indexOf(status.stage);
    $app.innerHTML = shell("", `
      <div class="page" style="max-width:640px;margin:0 auto;padding-top:clamp(40px,10vh,100px)">
        <div class="gen-card">
          <h2 style="margin:0 0 6px;text-align:left">${esc(meta.title)}</h2>
          <div class="gen-sub">${meta.kind === "audio" ? "音频" : "视频"}课程 · 生成中</div>
          <div class="progressbar gen-bar"><div style="width:${pct}%"></div></div>
          <div class="gen-status">
            <span class="spinner"></span>
            <span><b>${cur}</b> — ${esc(status.detail || "")} <b class="gen-pct">${pct}%</b></span>
          </div>
          ${working ? `
          <div class="gen-steps">
            ${stageOrder.filter((s) => s !== "uploaded").map((s, i) => `
              <span class="gen-step ${i < curIdx ? "done" : s === status.stage ? "now" : ""}">${stageNames[s]}</span>`).join(" → ")}
          </div>` : `
          <div class="form-actions" style="justify-content:center">
            <button class="primary" onclick="location.reload()">刷新状态</button>
          </div>`}
          <div style="text-align:center;margin-top:18px"><a href="#/courses" class="gen-back">← 返回课程库</a></div>
        </div>
      </div>`);
    if (working) {
      state.pollTimer = setInterval(async () => {
        const st = await api(`/api/courses/${meta.id}`).catch(() => null);
        if (st && st.status.stage === "ready") { clearInterval(state.pollTimer); renderCourse(meta.id); }
        else if (st) {
          // live-update in place without re-rendering the whole shell
          const bar = document.querySelector(".gen-bar > div");
          const pctEl = document.querySelector(".gen-pct");
          const stEl = document.querySelector(".gen-status span:last-child");
          if (bar) bar.style.width = Math.round((st.status.progress || 0) * 100) + "%";
          if (pctEl) pctEl.textContent = Math.round((st.status.progress || 0) * 100) + "%";
          if (stEl) stEl.innerHTML = `<b>${stageNames[st.status.stage] || st.status.stage}</b> — ${esc(st.status.detail || "")} <b class="gen-pct">${Math.round((st.status.progress || 0) * 100)}%</b>`;
        }
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

  // Playback state must exist before any tab body renders: the calibration
  // toolbar inside each tab reads activeId, and a TDZ ReferenceError there
  // aborts drawCoursePage (no highlight, no controls wired).
  let activeId = -1;
  let follow = false;

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
  window.__playRange = (start, end, rate) => playRange(start, end, rate);

  function playRange(start, end, rate) {
    if (player.__rangeTick) player.removeEventListener("timeupdate", player.__rangeTick);
    player.playbackRate = rate || 1;
    let armed = false;
    const tick = () => {
      if (player.__rangeTick !== tick) return;
      if (!armed || player.currentTime < end) return;
      player.pause();
      player.removeEventListener("timeupdate", tick);
      player.__rangeTick = null;
    };
    const arm = () => { armed = true; };
    player.__rangeTick = tick;
    player.currentTime = start;
    const started = player.play();
    if (started && typeof started.catch === "function") started.catch(() => {});
    player.addEventListener("timeupdate", tick);
    // Arm the stop check only once the seek has landed. Reading currentTime
    // before that can still return the pre-seek position, which made the first
    // click of a shadowing sentence pause immediately.
    if (player.seeking) player.addEventListener("seeked", arm, { once: true });
    else arm();
    setTimeout(() => { if (player.__rangeTick === tick) arm(); }, 400);
  }

  function highlightSentence(idx) {
    document.querySelectorAll(".sent.active").forEach((el) => el.classList.remove("active"));
    const el = document.querySelector(`.sent[data-idx="${idx}"]`);
    if (el) el.classList.add("active");
  }

  /* ---- per-sentence calibration / text correction (shared by every tab) ---- */
  function calibrateSentence(sid) {
    const raw = rawSentences.find((s) => s.id === sid);
    if (!raw) { toast("找不到这句的原始数据"); return; }
    const cal = state.course.calibration;
    const t = player.currentTime;
    cal.overrides[String(sid)] = {
      start: Math.max(0, t),
      end: Math.max(0, t + (raw.end - raw.start)),
    };
    saveCalibration().catch((e) => toast("保存失败: " + e.message));
    const list = calibratedSentences();
    sentences.splice(0, sentences.length, ...list);
    panels[state.tab]();
    toast(`第 ${sid + 1} 句已对齐到 ${fmtTime(t)} ✓`);
  }

  function saveSentenceText(sid, value) {
    const cal = state.course.calibration;
    if (!cal.text_edits) cal.text_edits = {};
    const val = (value || "").trim();
    if (val) cal.text_edits[String(sid)] = val;
    else delete cal.text_edits[String(sid)];   // empty -> revert
    saveCalibration().catch((e) => toast("保存失败: " + e.message));
    const list = calibratedSentences();
    sentences.splice(0, sentences.length, ...list);
    panels[state.tab]();
    toast(val ? `第 ${sid + 1} 句文本已修正 ✓` : `第 ${sid + 1} 句已还原 ✓`);
  }

  function sentenceActionsHtml(sid) {
    return `
      <div class="s-actions">
        <div class="s-tools">
          <button class="s-cal" data-cal="${sid}" title="把这句对齐到当前播放位置">⏱ 校准此句</button>
          <button class="s-edit" data-edit="${sid}" title="手动修正这句的文字">✎ 修正文字</button>
        </div>
        <div class="s-editbox" style="display:none">
          <textarea rows="2" placeholder="修正这句话的文本（合并拆错的句子、改错别字）">${esc(sentences[sid]?.text || "")}</textarea>
          <div style="display:flex;gap:8px;margin-top:6px">
            <button class="primary edit-save" data-save="${sid}">保存</button>
            <button class="edit-cancel">取消</button>
          </div>
        </div>
      </div>`;
  }

  function wireSentenceActions(root) {
    root.querySelectorAll("[data-cal]").forEach((btn) => {
      btn.onclick = (e) => { e.stopPropagation(); calibrateSentence(+btn.dataset.cal); };
    });
    root.querySelectorAll("[data-edit]").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const box = btn.closest(".s-actions").querySelector(".s-editbox");
        box.style.display = box.style.display === "none" ? "block" : "none";
      };
    });
    root.querySelectorAll(".s-actions .edit-cancel").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        btn.closest(".s-actions").querySelector(".s-editbox").style.display = "none";
      };
    });
    root.querySelectorAll(".s-actions .edit-save").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const ta = btn.closest(".s-actions").querySelector("textarea");
        saveSentenceText(+btn.dataset.save, ta.value);
      };
    });
  }

  function renderReadTab() {
    const panel = document.getElementById("tab-panel");
    panel.innerHTML = sentences.map((s) => {
      const read = state.progress.read.includes(s.id);
      const flags = [];
      if (s.edited) flags.push(`<span class="s-flags flag-embedded" title="已手动修正">✎ 已修正</span>`);
      if (s.uncertain) flags.push(`<span class="s-flags flag-uncertain" title="字幕轨与转写不一致，建议对照原声确认">⚠ 不确定</span>`);
      if (s.source === "embedded" && !s.uncertain) flags.push(`<span class="s-flags flag-embedded" title="取自内嵌字幕轨">内嵌字幕</span>`);
      if (s.source === "OCR" && !s.uncertain) flags.push(`<span class="s-flags flag-embedded" title="取自画面 OCR 字幕">OCR 字幕</span>`);
      if (s.source === "arbitrated" && !s.uncertain) flags.push(`<span class="s-flags flag-embedded" title="字幕与转写由模型仲裁合并">已仲裁</span>`);
      return `
      <div class="sent ${s.lang === "zh" ? "zh" : ""}" data-idx="${s.id}">
        <span class="s-time">${fmtTime(s.start)} → ${fmtTime(s.end)}${flags.join("")}${read ? " ✓" : ""}</span>
        <div class="s-text">${markVocab(s.text, s.lang, vocabIndex)}</div>
        ${s.translation ? `<div class="s-trans">${esc(s.translation)}</div>` : ""}
        ${s.grammar ? `<div class="s-grammar">语法: ${esc(s.grammar)}</div>` : ""}
        ${sentenceActionsHtml(s.id)}
      </div>`;
    }).join("");

    panel.querySelectorAll(".sent").forEach((el) => {
      el.onclick = (e) => {
        if (e.target.closest(".vmark") || e.target.closest(".s-actions")) return;
        const idx = parseInt(el.dataset.idx, 10);
        player.currentTime = sentences[idx].start;
        player.play();
        if (!state.progress.read.includes(idx)) {
          state.progress.read.push(idx);
          saveProgress();
        }
      };
    });
    wireSentenceActions(panel);
    // re-apply the highlight after a re-render, otherwise it disappears on
    // tab switches / text edits while playback continues
    if (activeId >= 0) highlightSentence(activeId);
  }

  function renderCardsTab() {
    const panel = document.getElementById("tab-panel");
    const vocab = lesson.vocabulary || [];
    if (!vocab.length) { panel.innerHTML = `<div class="panel-empty">本课程暂无生词数据</div>`; return; }
    const order = shuffle([...vocab.keys()]);
    panel.innerHTML = `
      <div style="margin-bottom:12px;color:var(--ink-2);font-size:13px">
        点击卡片翻面 · 底部按钮标记认识/不认识
      </div>
      <div class="card-grid">${order.map((vi) => cardHtml(vocab[vi], vi)).join("")}</div>`;

    panel.querySelectorAll(".lcard").forEach((card) => {
      card.onclick = (e) => {
        if (e.target.closest("a,button,.s-actions")) return;
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
    panel.querySelectorAll("[data-speak]").forEach((b) => {
      b.onclick = (e) => { e.stopPropagation(); speak(b.dataset.speak); };
    });
    panel.querySelectorAll(".jump").forEach((a) => {
      a.onclick = (e) => { e.preventDefault(); window.__jumpTo(sentences[+a.dataset.sid].start); };
    });
    wireSentenceActions(panel);
  }

  // speak an English word/phrase with the browser's built-in TTS
  function speak(text) {
    if (!("speechSynthesis" in window) || !text) return;
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-US";
    u.rate = 0.9;
    speechSynthesis.speak(u);
  }

  function cardHtml(v, vi) {
    const known = state.progress.cards[v.term];
    return `
    <div class="lcard" data-term="${esc(v.term)}">
      <div class="face front">
        <div class="term">${esc(v.term)}<span class="pos">${esc(v.pos || "")}</span>
          <button class="speak-btn" data-speak="${esc(v.term)}" title="朗读发音">🔊</button></div>
        <div class="phon">${esc(v.phonetic || "")}</div>
        <div class="def" style="visibility:hidden"></div>
        <div class="lcard-actions">
          <button data-mark="unknown" class="${known === "unknown" ? "mark-unknown" : ""}">不认识</button>
          <button data-mark="known" class="${known === "known" ? "mark-known" : ""}">认识</button>
        </div>
      </div>
      <div class="face back">
        <div class="def">${esc(v.definition || "")}</div>
        <div class="ex">${markVocab(v.example || "", "en", vocabIndex)}</div>
      </div>
    </div>`;
  }

  function renderExpressTab() {
    const panel = document.getElementById("tab-panel");
    const expr = lesson.expressions || [];
    const dom = lesson.domain || [];
    const cult = lesson.culture || [];
    const item = (x) => {
      const sid = (x.sentenceIds || [])[0];
      const s = sid !== undefined && sentences[sid] ? sentences[sid] : null;
      const phrase = x.phrase || x.term || x.topic || "";
      return `
      <div class="hs-card">
        <div class="hs-orig">${esc(phrase)}${phrase ? ` <button class="speak-btn" data-speak="${esc(phrase)}" title="朗读发音">🔊</button>` : ""}</div>
        <div class="hs-structure">${esc(x.meaning || x.explanation || x.note || "")}</div>
        ${x.usage ? `<div class="hs-breakdown">${esc(x.usage)}</div>` : ""}
        ${s ? `<div class="hs-jump"><span class="hs-snip">${esc(s.text.slice(0, 60))}</span></div>` : ""}
      </div>`;
    };
    panel.innerHTML = `
      <div class="section-title">💬 口语表达</div>
      ${expr.length ? expr.map((x) => item(x)).join("") : `<div class="panel-empty">暂无</div>`}
      <div class="section-title">⚖ 行业词汇</div>
      ${dom.length ? dom.map((x) => item(x)).join("") : `<div class="panel-empty">暂无</div>`}
      <div class="section-title">🌍 文化背景</div>
      ${cult.length ? cult.map((x) => item(x)).join("") : `<div class="panel-empty">暂无</div>`}`;
    panel.querySelectorAll("[data-speak]").forEach((b) => {
      b.onclick = (e) => { e.stopPropagation(); speak(b.dataset.speak); };
    });
    panel.querySelectorAll(".jump").forEach((a) => {
      a.onclick = (e) => { e.preventDefault(); window.__jumpTo(sentences[+a.dataset.sid].start); };
    });
    wireSentenceActions(panel);
  }

  function renderHardTab() {
    const panel = document.getElementById("tab-panel");
    const hs = lesson.hardSentences || [];
    if (!hs.length) { panel.innerHTML = `<div class="panel-empty">本课程暂无长难句拆解</div>`; return; }
    panel.innerHTML = hs.map((h) => {
      const s = sentences[h.sentenceId] || {};
      return `
      <div class="hs-card">
        ${s.start !== undefined ? `<div class="hs-time">⏱ ${fmtTime(s.start)} → ${fmtTime(s.end)}</div>` : ""}
        <div class="hs-orig">${markVocab(s.text || "", "en", vocabIndex)}</div>
        ${s.translation ? `<div class="s-trans">${esc(s.translation)}</div>` : ""}
        <div class="hs-structure">结构: ${esc(h.structure || "")}</div>
        <div class="hs-breakdown">${esc(h.breakdown || "")}</div>
        <div class="hs-jump"><a class="jump jump-btn" data-sid="${h.sentenceId}" href="#">▶ 播放原句</a></div>
        ${sentences[h.sentenceId] ? sentenceActionsHtml(h.sentenceId) : ""}
      </div>`;
    }).join("");
    panel.querySelectorAll(".jump").forEach((a) => {
      a.onclick = (e) => { e.preventDefault(); window.__jumpTo(sentences[+a.dataset.sid].start); };
    });
    wireSentenceActions(panel);
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
        <div class="sh-main">
          <div class="sh-text">
            ${esc(sent.text)}
            <div class="sh-tip">${esc(s.tip || "")}</div>
          </div>
          <div class="hs-time">⏱ ${fmtTime(sent.start)} → ${fmtTime(sent.end)}</div>
          ${sentenceActionsHtml(s.sentenceId)}
        </div>
        <div class="sh-btns">
          <button data-play="${s.sentenceId}" title="原速播放">▶</button>
          <button data-slow="${s.sentenceId}" title="0.75x 慢速播放">🐢</button>
          <button data-ab="${s.sentenceId}" title="A-B 循环这句">🔁</button>
        </div>
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
    wireSentenceActions(panel);
  }
}

/* ---------------- centered confirm dialog ---------------- */
function confirmDialog(title, message, onOk) {
  document.querySelectorAll(".cal-overlay").forEach((el) => el.remove());
  const overlay = document.createElement("div");
  overlay.className = "cal-overlay";
  overlay.innerHTML = `
    <div class="cal-panel confirm-panel" style="width:min(380px,92%)">
      <div class="cal-head"><b>${esc(title)}</b><button class="cal-close">✕</button></div>
      <div class="confirm-msg">${esc(message)}</div>
      <div class="form-actions" style="margin-top:16px">
        <button class="primary confirm-ok" style="flex:1">确定</button>
        <button class="cal-close" style="flex-shrink:0">取消</button>
      </div>
    </div>`;
  overlay.querySelector(".confirm-ok").onclick = () => { overlay.remove(); onOk(); };
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay || e.target.closest(".cal-close")) overlay.remove();
  });
  document.body.appendChild(overlay);
}

/* ---------------- material video player dialog ---------------- */
function openMaterialPlayer(mid) {
  document.querySelectorAll(".cal-overlay").forEach((el) => el.remove());
  const overlay = document.createElement("div");
  overlay.className = "cal-overlay";
  overlay.innerHTML = `
    <div class="cal-panel" style="width:min(720px,96%);padding:14px">
      <div class="cal-head"><b>播放素材</b><button class="cal-close">✕</button></div>
      <video class="mat-player" src="/media/${encodeURIComponent(mid)}/material"
             controls autoplay style="width:100%;max-height:70vh;border-radius:8px;background:#000;margin-top:10px"></video>
    </div>`;
  const video = overlay.querySelector("video");
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay || e.target.closest(".cal-close")) {
      video.pause();
      video.src = "";
      overlay.remove();
    }
  });
  document.body.appendChild(overlay);
}

/* ================= Settings (model config) ================= */
function normBase(url) { return (url || "").replace(/\/+$/, ""); }

function renderSettings() {
  document.title = "模型配置 · DeepRead";
  // state declared BEFORE the template — the template interpolates these
  let selSection = "providers";   // "providers" | "roles" | "tuning"
  let expandedProvider = -1;      // index of the provider row expanded inline; -1 = all collapsed
  let providers = [];
  let cfgCache = null;
  let modelCache = {};            // provider_id -> {models, asr_models}
  $app.innerHTML = shell("settings", `
    <div class="page settings-split">
      <h1>模型配置</h1>
      <div class="set-tabs">
        <button class="set-fixed ${selSection === "providers" ? "on" : ""}" data-sec="providers">API 提供商</button>
        <button class="set-fixed ${selSection === "roles" ? "on" : ""}" data-sec="roles">模型分配</button>
        <button class="set-fixed ${selSection === "tuning" ? "on" : ""}" data-sec="tuning">生成参数</button>
      </div>
      <section class="set-main">
          <div id="stab-providers" class="stab"></div>
          <div id="stab-roles" class="stab" style="display:none">
            <div class="cfg-card">
              <div class="cfg-card-head"><span class="cfg-card-title">🎯 模型分配</span></div>
              <div class="role-grid">
                <div class="role-block">
                  <div class="role-name">📝 翻译与精讲 <i>Chat</i></div>
                  <div class="form-row"><select id="role-chat"></select></div>
                  <div class="mp-slot" id="mp-chat"></div>
                  <div class="probe-row">
                    <div class="probe-actions">
                      <button id="cfg-refresh-chat" title="获取这个 API 的模型列表">↻ 获取模型</button>
                    </div>
                  </div>
                </div>
                <div class="role-block">
                  <div class="role-name">🎙 语音转写 <i>ASR</i></div>
                  <div class="form-row"><select id="role-asr"></select></div>
                  <div class="mp-slot" id="mp-asr"></div>
                  <div class="probe-row">
                    <div class="probe-actions">
                      <button id="cfg-refresh-asr" title="获取这个 API 的模型列表">↻ 获取模型</button>
                    </div>
                  </div>
                </div>
                <div class="role-block">
                  <div class="role-name">🖼 画面字幕 OCR <i>Vision</i></div>
                  <div class="form-row"><select id="role-vision"></select></div>
                  <div class="mp-slot" id="mp-vision"></div>
                  <div class="probe-row">
                    <div class="probe-actions">
                      <button id="cfg-refresh-vision" title="获取这个 API 的模型列表">↻ 获取模型</button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <div id="stab-tuning" class="stab" style="display:none">
            <div class="cfg-card">
              <div class="cfg-card-head"><span class="cfg-card-title">🎛 生成参数</span></div>
              <div class="tune-grid">
              <div class="tune-section">
                <div class="tune-section-title">字幕识别（OCR）</div>
                <div class="tune-rows">
                  <div class="tune-row">
                    <div class="tune-label">字幕区域高度：<small>画面底部保留比例，字幕不在底部时调大（0.05-0.5）</small></div>
                    <div class="tune-ctl"><input type="number" id="cfg-ocr-crop" min="0.05" max="0.5" step="0.01"></div>
                  </div>
                  <div class="tune-row">
                    <div class="tune-label">OCR 并发数：<small>每帧图片识别并发（3-8）</small></div>
                    <div class="tune-ctl"><input type="number" id="cfg-ocr-workers" min="1" max="12"></div>
                  </div>
                </div>
              </div>
              <div class="tune-section">
                <div class="tune-section-title">语音转写（ASR）</div>
                <div class="tune-rows">
                  <div class="tune-row">
                    <div class="tune-label">转写并发数：<small>音频分段转写并发（2-6）</small></div>
                    <div class="tune-ctl"><input type="number" id="cfg-asr-workers" min="1" max="8"></div>
                  </div>
                </div>
              </div>
              <div class="tune-section">
                <div class="tune-section-title">融合与精讲</div>
                <div class="tune-rows">
                  <div class="tune-row">
                    <div class="tune-label">翻译并发数：<small>翻译批次并发（1-6）</small></div>
                    <div class="tune-ctl"><input type="number" id="cfg-tr-workers" min="1" max="8"></div>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <div class="form-actions set-savebar" id="savebar">
            <button class="primary" id="cfg-save" style="padding:9px 26px">保存配置</button>
            <button id="cfg-test">测试连接</button>
          </div>
          <div class="test-result" id="test-result"></div>
        </section>
      <section class="run-log">
        <div class="run-log-head">
          <span>运行日志</span>
          <button id="run-log-clear" title="清空日志">清空</button>
        </div>
        <div class="run-log-body" id="run-log">
          <div class="run-log-empty">测试连接 / 获取模型时，这里会打印过程日志。</div>
        </div>
      </section>
    </div>`);

  /* ---- run log (bottom panel) ---- */
  function logLine(level, text) {
    const el = document.getElementById("run-log");
    if (!el) return;
    const empty = el.querySelector(".run-log-empty");
    if (empty) empty.remove();
    const row = document.createElement("div");
    row.className = "run-log-row " + (level || "info");
    const t = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    row.textContent = `[${t}] ${text}`;
    el.appendChild(row);
    while (el.children.length > 400) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
  }

  const runLogClearBtn = document.getElementById("run-log-clear");
  if (runLogClearBtn) {
    runLogClearBtn.onclick = () => {
      document.getElementById("run-log").innerHTML = `<div class="run-log-empty">日志已清空</div>`;
    };
  }

  /* ---- last connection-test result per provider (keyed by base url) ---- */
  function loadProvStatuses() {
    try { return JSON.parse(localStorage.getItem("dr.provStatus") || "{}"); } catch { return {}; }
  }
  let provStatuses = loadProvStatuses();

  function provStatus(p) {
    const st = provStatuses[normBase(p.base_url)];
    if (!st) return { cls: "", text: "未测试" };
    return st.ok
      ? { cls: "ok", text: `✓ 已连通 · ${st.count} 个模型` }
      : { cls: "fail", text: "✗ 连接失败" };
  }

  function setProvStatus(p, val) {
    provStatuses[normBase(p.base_url)] = { ...val, at: Date.now() };
    try { localStorage.setItem("dr.provStatus", JSON.stringify(provStatuses)); } catch {}
  }

  async function init() {
    cfgCache = await api("/api/config");
    providers = (cfgCache.providers || []).map((p) => ({ ...p }));
    drawProviders();
    drawRoles();
    document.getElementById("role-asr").value = cfgCache.asr_provider || "";
    document.getElementById("role-chat").value = cfgCache.chat_provider || "";
    document.getElementById("role-vision").value = cfgCache.vision_provider || "";
    // restore the saved multi-selection, then reuse saved probe results
    initPickedFromConfig();
    showSavedOrProbed("asr");
    showSavedOrProbed("vision");
    setBrowse("chat", []);
    drawAllPickers();
    // pipeline tuning
    document.getElementById("cfg-ocr-crop").value = cfgCache.ocr_bottom_crop || 0.22;
    document.getElementById("cfg-ocr-workers").value = cfgCache.ocr_workers || 6;
    document.getElementById("cfg-asr-workers").value = cfgCache.asr_workers || 4;
    document.getElementById("cfg-tr-workers").value = cfgCache.translate_workers || 3;
    selSection = "providers";
    expandedProvider = -1;
    drawProviders();
    drawSection();
  }

  function drawSection() {
    document.getElementById("stab-providers").style.display = selSection === "providers" ? "block" : "none";
    document.getElementById("stab-roles").style.display = selSection === "roles" ? "block" : "none";
    document.getElementById("stab-tuning").style.display = selSection === "tuning" ? "block" : "none";
    // save bar only makes sense for roles/tuning (providers have per-card save)
    document.getElementById("savebar").style.display = selSection === "providers" ? "none" : "flex";
    document.querySelectorAll(".set-fixed").forEach((el) => {
      el.classList.toggle("on", selSection === el.dataset.sec);
    });
  }

  function drawProviders() {
    const wrap = document.getElementById("stab-providers");
    if (!wrap) return;

    // ---- provider cards (collapsed by default; click to expand) ----
    if (!providers.length) expandedProvider = -1;
    const rolesFor = (i) => {
      const pid = providers[i].id;
      const assigned = [];
      if (pid) {
        if (document.getElementById("role-chat")?.value === pid) assigned.push("精讲");
        if (document.getElementById("role-asr")?.value === pid) assigned.push("转写");
        if (document.getElementById("role-vision")?.value === pid) assigned.push("OCR");
      }
      return assigned;
    };
    wrap.innerHTML = `
      <div class="prov-head">
        <span class="prov-head-title">API 提供商</span>
        <button id="prov-add" class="primary" style="padding:6px 16px">＋ 添加</button>
      </div>
      ${providers.length ? `<div class="prov-grid">${providers.map((p, i) => {
        const st = provStatus(p);
        const roles = rolesFor(i);
        const open = expandedProvider === i;
        return `
        <div class="prov-card ${open ? "open" : ""}" data-pi="${i}">
          <div class="prov-card-hit" data-toggle="${i}">
            <div class="prov-card-head">
              <span class="prov-dot ${st.cls}" title="${st.cls === "ok" ? "上次测试通过" : st.cls === "fail" ? "上次测试失败" : "未测试"}"></span>
              <span class="prov-card-name" title="${esc(p.name || `API ${i + 1}`)}">${esc(p.name || `API ${i + 1}`)}</span>
              <span class="prov-status ${st.cls}" title="${st.text}">${st.text}</span>
            </div>
            <div class="prov-card-url" title="${esc(p.base_url)}">${esc((p.base_url || "").replace(/^https?:\/\//, ""))}</div>
            <div class="prov-card-roles" title="${roles.length ? roles.join(" · ") : "未分配"}">用于：${roles.length ? roles.join(" · ") : "未分配"}</div>
          </div>
          <div class="prov-card-actions">
            <button class="prov-test" data-test="${i}" title="测试这个 API 的连接">测试</button>
            <button data-edit="${i}" title="编辑这个 API">编辑</button>
            <button class="danger p-del" data-del="${i}" title="删除">✕</button>
          </div>
          <div class="prov-body" data-body="${i}">
            <div class="form-row"><label>名称</label>
              <input class="p-name" value="${esc(p.name)}" placeholder="名称（如 阿里云百炼）"></div>
            <div class="form-row"><label>Base URL</label>
              <input class="p-url" value="${esc(p.base_url)}" placeholder="https://.../v1"></div>
            <div class="form-row"><label>API Key</label>
              <input class="p-key" type="password" value="${esc(p.api_key)}" placeholder="sk-..."></div>
            <div class="cal-hint">Key 保存在服务器本地 data/config.json。转写 / Chat / OCR 可分别指向不同提供商（见上方「模型分配」）。</div>
            <div class="form-actions">
              <button class="primary prov-done" data-done="${i}" style="padding:8px 22px">确定</button>
              <button class="prov-test" data-test="${i}">测试连接</button>
            </div>
          </div>
        </div>`;
      }).join("")}</div>`
        : `<div class="panel-empty" style="padding:24px">还没有 API，点击右上「＋ 添加」</div>`}`;

    wrap.querySelectorAll("[data-toggle]").forEach((el) => {
      el.onclick = () => {
        const i = +el.dataset.toggle;
        collectProviders();
        expandedProvider = expandedProvider === i ? -1 : i;
        drawProviders();
      };
    });
    wrap.querySelectorAll("[data-edit]").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        collectProviders();
        expandedProvider = +btn.dataset.edit;
        drawProviders();
        const nameInput = wrap.querySelector(`.prov-body[data-body="${expandedProvider}"] .p-name`);
        if (nameInput) nameInput.focus();
      };
    });
    wrap.querySelectorAll(".p-del").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const i = +btn.dataset.del;
        if (!confirm(`删除 API「${providers[i].name}」？`)) return;
        providers.splice(i, 1);
        if (expandedProvider === i) expandedProvider = -1;
        else if (expandedProvider > i) expandedProvider -= 1;
        drawProviders(); drawRoles();
      };
    });
    wrap.querySelectorAll("[data-done]").forEach((btn) => {
      btn.onclick = async () => {
        collectProviders();
        const p = providers[expandedProvider];
        if (p && !p.base_url) { toast("请填写 Base URL"); return; }
        const ok = await saveProviders();
        if (!ok) return;
        expandedProvider = -1;
        drawProviders();
        logLine("ok", "✓ 已保存 API 配置");
        toast("已保存");
      };
    });
    // per-provider connectivity check (uses the values currently in the fields)
    wrap.querySelectorAll("[data-test]").forEach((btn) => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        collectProviders();
        const i = +btn.dataset.test;
        const p = providers[i];
        if (!p || !p.base_url) { toast("请填写 Base URL"); return; }
        const label = p.name || `API ${i + 1}`;
        const card = btn.closest(".prov-card");
        const statusEl = card ? card.querySelector(".prov-status") : null;
        const dotEl = card ? card.querySelector(".prov-dot") : null;
        btn.disabled = true;
        if (statusEl) { statusEl.textContent = "测试中…"; statusEl.className = "prov-status"; }
        logLine("info", `测试连接「${label}」 ${p.base_url}`);
        try {
          const res = await api("/api/config/test_provider", {
            method: "POST",
            body: JSON.stringify({ provider_id: p.id, base_url: p.base_url, api_key: p.api_key }),
          });
          const ok = !!res.ok;
          setProvStatus(p, { ok, count: res.count || 0 });
          if (statusEl) {
            statusEl.textContent = ok ? `✓ 已连通 · ${res.count} 个模型` : "✗ 连接失败";
            statusEl.className = "prov-status " + (ok ? "ok" : "fail");
          }
          if (dotEl) dotEl.className = "prov-dot " + (ok ? "ok" : "fail");
          logLine(ok ? "ok" : "fail", ok ? `✓ 连接成功 · ${res.count} 个模型` : `✗ ${res.detail || "连接失败"}`);
        } catch (err) {
          setProvStatus(p, { ok: false, count: 0 });
          if (statusEl) { statusEl.textContent = "✗ 连接失败"; statusEl.className = "prov-status fail"; }
          if (dotEl) dotEl.className = "prov-dot fail";
          logLine("fail", "✗ " + err.message);
        } finally {
          btn.disabled = false;
        }
      };
    });
    const addBtn = wrap.querySelector("#prov-add");
    if (addBtn) addBtn.onclick = () => {
      collectProviders();
      providers.push({ id: "", name: "", base_url: "", api_key: "" });
      expandedProvider = providers.length - 1;
      drawProviders(); drawRoles();
      const nameInput = wrap.querySelector(`.prov-body[data-body="${expandedProvider}"] .p-name`);
      if (nameInput) nameInput.focus();
    };
    drawRoles();
  }

  // Persist just the provider list (ids for new entries come back from the server).
  async function saveProviders() {
    try {
      await api("/api/config", { method: "PUT", body: JSON.stringify({ providers }) });
      cfgCache = await api("/api/config");
      providers = (cfgCache.providers || []).map((p) => ({ ...p }));
      return true;
    } catch (err) {
      toast("保存失败: " + err.message);
      return false;
    }
  }

  function collectProviders() {
    if (expandedProvider < 0 || !providers[expandedProvider]) return;
    const body = document.querySelector(`#stab-providers .prov-body[data-body="${expandedProvider}"]`);
    if (!body) return;
    providers[expandedProvider].name = body.querySelector(".p-name").value.trim() || `API ${expandedProvider + 1}`;
    providers[expandedProvider].base_url = body.querySelector(".p-url").value.trim();
    providers[expandedProvider].api_key = body.querySelector(".p-key").value.trim();
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
    asrSel.onchange = () => showSavedOrProbed("asr");
    chatSel.onchange = () => { setBrowse("chat", []); };
    visSel.onchange = () => showSavedOrProbed("vision");
  }

  /* ---- multi-select model picker (ordered list; first = primary) ---- */
  const picked = { chat: [], asr: [], vision: [] };   // working selection per role
  let browse = { chat: [], asr: [], vision: [] };     // models known for the current provider

  function healthKey(pid, model) { return `${pid || ""}||${model || ""}`; }

  function healthFor(pid, model) {
    return (cfgCache?.model_health || {})[healthKey(pid, model)] || null;
  }

  function setPicked(role, list) {
    picked[role] = [...new Set((list || []).filter(Boolean))];
  }

  const modelFilter = { chat: "", asr: "", vision: "" };   // per-role browse search

  function drawPicker(role) {
    const slot = document.getElementById(`mp-${role}`);
    if (!slot) return;
    const pid = document.getElementById(role === "chat" ? "role-chat" : role === "asr" ? "role-asr" : "role-vision")?.value || "";
    const chosen = picked[role];
    const f = (modelFilter[role] || "").toLowerCase();
    const avail = (browse[role] || [])
      .filter((m) => !chosen.includes(m))
      .filter((m) => !f || m.toLowerCase().includes(f));
    const rows = chosen.length ? chosen.map((m, i) => {
      const bad = healthFor(pid, m);
      return `
        <div class="mp-item ${bad ? "bad" : ""}" data-role="${role}" data-model="${esc(m)}">
          <span class="mp-idx">${i + 1}</span>
          <span class="mp-name" title="${esc(m)}">${esc(m)}</span>
          ${bad ? `<span class="mp-bad" title="${esc(bad.message || "上次调用失败")}">⚠ 报错</span>` : ""}
          <button class="mp-up" data-up="${i}" title="上移" ${i === 0 ? "disabled" : ""}>▲</button>
          <button class="mp-del" data-del="${i}" title="移除">✕</button>
        </div>`;
    }).join("") : `<div class="mp-empty">还没有选择模型 — 用下面的列表添加</div>`;
    const badCount = chosen.filter((m) => healthFor(pid, m)).length;
    const shownTotal = (browse[role] || []).length - chosen.length;
    slot.innerHTML = `
      <div class="mp-list">${rows}</div>
      ${badCount ? `<div class="mp-warn">⚠ 有 ${badCount} 个已选模型上次调用报错，仍按顺序尝试；连续失败会继续换下一个</div>` : ""}
      <div class="mp-add-row">
        <input type="text" class="mp-search" placeholder="搜索模型（${shownTotal} 个可选）" value="${esc(modelFilter[role] || "")}">
        <select class="mp-add-sel">
          <option value="" selected>${f ? `匹配 ${avail.length} 个` : "选择模型…"}</option>
          ${avail.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("")}
        </select>
        <button class="mp-add" disabled>＋ 添加</button>
        <button class="mp-manual-add" title="手动输入模型名（官方目录滞后的新模型也能加）">＋ 手动添加</button>
      </div>
      <div class="mp-hint">可多选，按顺序尝试：第一个是主模型，报错时自动换下一个。</div>`;

    slot.querySelectorAll("[data-up]").forEach((btn) => {
      btn.onclick = () => {
        const i = +btn.dataset.up;
        if (i > 0) {
          [chosen[i - 1], chosen[i]] = [chosen[i], chosen[i - 1]];
          drawPicker(role);
        }
      };
    });
    slot.querySelectorAll("[data-del]").forEach((btn) => {
      btn.onclick = () => {
        chosen.splice(+btn.dataset.del, 1);
        drawPicker(role);
      };
    });
    const addSel = slot.querySelector(".mp-add-sel");
    const addBtn = slot.querySelector(".mp-add");
    const search = slot.querySelector(".mp-search");
    if (search) {
      // keep focus and caret position across redraws
      const pos = search.selectionStart;
      search.oninput = () => {
        modelFilter[role] = search.value;
        drawPicker(role);
        const again = slot.querySelector(".mp-search");
        if (again) { again.focus(); again.setSelectionRange(pos, pos); }
      };
      search.onkeydown = (e) => {
        if (e.key === "Enter" && avail.length === 1) {
          // quick add: exactly one match → add it
          if (!chosen.includes(avail[0])) chosen.push(avail[0]);
          modelFilter[role] = "";
          drawPicker(role);
        }
      };
    }
    if (addSel && addBtn) {
      addSel.onchange = () => { addBtn.disabled = !addSel.value; };
      addBtn.onclick = () => {
        if (addSel.value && !chosen.includes(addSel.value)) chosen.push(addSel.value);
        modelFilter[role] = "";
        drawPicker(role);
      };
    }
    // manual add: for models the provider's /models directory lags behind on
    const manualBtn = slot.querySelector(".mp-manual-add");
    if (manualBtn) {
      manualBtn.onclick = () => {
        const name = prompt("输入模型名称（按 API 文档的模型 Code，如 qwen-audio-3.1-asr-flash-message）：");
        const clean = (name || "").trim();
        if (clean && !chosen.includes(clean)) chosen.push(clean);
        if (clean) toast(`已添加 ${clean}（保存配置后生效）`);
      };
    }
  }

  function drawAllPickers() { ["chat", "asr", "vision"].forEach(drawPicker); }

  // Show whatever was saved before this page load (selection is stored in config).
  function initPickedFromConfig() {
    setPicked("chat", (cfgCache?.chat_models || []).length ? cfgCache.chat_models : [cfgCache?.chat_model]);
    setPicked("asr", (cfgCache?.asr_models || []).length ? cfgCache.asr_models : [cfgCache?.asr_model]);
    setPicked("vision", (cfgCache?.vision_models || []).length ? cfgCache.vision_models : [cfgCache?.vision_model]);
  }

  // Fill the "add" list for a role with the provider model list.
  // Models that last failed a real call are dropped so the list stays usable.
  function setBrowse(role, models) {
    const provSel = role === "asr" ? "role-asr" : role === "vision" ? "role-vision" : "role-chat";
    const pid = document.getElementById(provSel)?.value || "";
    const healthy = (models || []).filter((m) => !healthFor(pid, m) || picked[role].includes(m));
    browse[role] = [...new Set([...healthy, ...picked[role]])];
    drawPicker(role);
  }

  // No cached list yet: keep the saved selection, browse starts empty until
  // 获取模型 runs for the selected provider.
  function showSavedOrProbed(role) {
    setBrowse(role, []);
  }

  async function fetchModels(providerId) {
    if (modelCache[providerId]) return modelCache[providerId];
    const res = await api(`/api/config/models?provider_id=${encodeURIComponent(providerId)}`);
    modelCache[providerId] = res;
    return res;
  }

  // left-nav fixed sections (API 提供商 / 模型分配 / 生成参数)
  document.querySelectorAll(".set-fixed").forEach((el) => {
    el.onclick = () => {
      collectProviders();
      selSection = el.dataset.sec;
      expandedProvider = -1;
      drawProviders();
      drawSection();
    };
  });

  // unified "获取模型" for all three roles: clear the session cache, fetch
  // the provider's full model list, and fill the role's browse picker
  async function refreshRoleModels(role) {
    const sel = document.getElementById(role === "asr" ? "role-asr" : role === "vision" ? "role-vision" : "role-chat");
    const pid = sel?.value;
    if (!pid) { toast("请先添加并保存 API"); return; }
    await saveConfigQuiet();
    const btn = document.getElementById(role === "asr" ? "cfg-refresh-asr"
      : role === "vision" ? "cfg-refresh-vision" : "cfg-refresh-chat");
    if (btn) btn.disabled = true;
    const prov = providers.find((p) => p.id === pid);
    logLine("info", `—— 获取模型 @ ${prov ? prov.name || pid : pid} ——`);
    try {
      delete modelCache[pid];   // always re-fetch; nothing is cached
      const res = await fetchModels(pid);
      setBrowse(role, res.models || []);
      const n = (res.models || []).length;
      logLine("ok", `✓ 获取到 ${n} 个模型`);
      toast(`获取到 ${n} 个模型`);
    } catch (err) {
      logLine("fail", "✗ 获取失败: " + err.message);
      toast("获取失败: " + err.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  document.getElementById("cfg-refresh-asr").onclick = () => refreshRoleModels("asr");
  document.getElementById("cfg-refresh-chat").onclick = () => refreshRoleModels("chat");
  document.getElementById("cfg-refresh-vision").onclick = () => refreshRoleModels("vision");

  async function saveConfigQuiet() {
    collectProviders();
    try {
      await api("/api/config", {
        method: "PUT",
        body: JSON.stringify({
          providers,
          asr_provider: document.getElementById("role-asr").value,
          chat_provider: document.getElementById("role-chat").value,
          vision_provider: document.getElementById("role-vision").value,
          chat_models: picked.chat,
          asr_models: picked.asr,
          vision_models: picked.vision,
          asr_model: picked.asr[0] || "",
          chat_model: picked.chat[0] || "",
          vision_model: picked.vision[0] || "",
          ocr_workers: parseInt(document.getElementById("cfg-ocr-workers").value, 10) || 6,
          ocr_bottom_crop: parseFloat(document.getElementById("cfg-ocr-crop").value) || 0.22,
          asr_workers: parseInt(document.getElementById("cfg-asr-workers").value, 10) || 4,
          translate_workers: parseInt(document.getElementById("cfg-tr-workers").value, 10) || 3,
        }),
      });
      cfgCache = await api("/api/config");
      providers = (cfgCache.providers || []).map((p) => ({ ...p }));
    } catch (err) { toast(err.message); }
  }

  document.getElementById("cfg-save").onclick = async () => {
    if (!providers.length) { toast("请至少添加一个 API"); return; }
    if (!picked.asr.length) { toast("请选择转写模型"); return; }
    if (!picked.chat.length) { toast("请选择 Chat 模型"); return; }
    await saveConfigQuiet();
    drawAllPickers();
    toast("已保存");
  };

  document.getElementById("cfg-test").onclick = async () => {
    const el = document.getElementById("test-result");
    el.className = "test-result"; el.textContent = "测试中…";
    logLine("info", "—— 测试连接（当前保存的配置）——");
    await saveConfigQuiet();
    try {
      const res = await api("/api/config/test", { method: "POST", body: JSON.stringify({}) });
      const parts = [];
      parts.push(res.chat ? "✓ Chat 可用" : "✗ Chat 不可用");
      parts.push(res.transcribe ? "✓ 转写端点可达" : "✗ 转写端点不可用");
      el.className = "test-result " + (res.chat || res.transcribe ? "ok" : "fail");
      el.textContent = parts.join("　") + (res.detail ? ` · ${res.detail}` : "");
      logLine(res.chat ? "ok" : "fail", `Chat：${res.chat ? "可用" : "不可用"}`);
      logLine(res.transcribe ? "ok" : "fail", `转写端点：${res.transcribe ? "可达" : "不可用"}`);
      if (res.detail) logLine("warn", res.detail);
    } catch (err) {
      el.className = "test-result fail";
      el.textContent = "连接失败: " + err.message;
      logLine("fail", "✗ 连接失败: " + err.message);
    }
  };

  init();
}

/* ================= Backup ================= */
function renderBackup() {
  document.title = "备份恢复 · DeepRead";
  $app.innerHTML = shell("backup", `
    <div class="page page-narrow">
      <h1>备份恢复</h1>

      <div class="cfg-card">
        <div class="cfg-card-head"><span class="cfg-card-title">⏰ 自动备份</span>
          <button id="bk-now" class="primary" style="padding:6px 16px">立即备份</button>
        </div>
        <div class="bk-settings-grid">
          <label class="switch-row">
            <input type="checkbox" id="bk-enabled">
            <span class="switch-ui"></span>
            <div class="switch-text"><b>每日自动备份</b><small>每天在设定时间自动创建备份</small></div>
          </label>
          <div class="bk-field">
            <label>备份时间</label>
            <input type="time" id="bk-time">
          </div>
          <div class="bk-field">
            <label>保留数量（份）</label>
            <input type="number" id="bk-keep" min="1" max="90">
          </div>
          <label class="switch-row" style="grid-column:1/-1">
            <input type="checkbox" id="bk-media">
            <span class="switch-ui"></span>
            <div class="switch-text"><b>包含媒体文件</b><small>备份原始视频/音频（每份备份可达数百 MB）；关闭则只备份数据，恢复后需补传媒体</small></div>
          </label>
        </div>
        <div class="form-actions" style="margin-top:12px">
          <button id="bk-save-settings" class="primary">保存设置</button>
        </div>
      </div>

      <div class="cfg-card">
        <div class="cfg-card-head"><span class="cfg-card-title">♻ 恢复</span></div>
        <div class="bk-restore-row">
          <input type="file" id="bk-restore-file" accept=".zip" style="flex:1;min-width:0">
          <button id="bk-restore" class="danger" style="flex-shrink:0;white-space:nowrap">恢复（覆盖数据）</button>
        </div>
        <div class="cal-hint" style="margin-top:8px">
          备份包含 API 配置与全部课程/素材数据。换部署环境：新机器 docker compose up 后在此上传备份，再按提示补传媒体即可。
        </div>
      </div>

      <div class="cfg-card">
        <div class="cfg-card-head"><span class="cfg-card-title">🗄 备份列表</span></div>
        <div id="bk-list"></div>
        <div id="bk-missing" style="margin-top:10px"></div>
      </div>
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

/* ================= Logs ================= */
function renderLogs() {
  document.title = "日志 · DeepRead";
  $app.innerHTML = shell("logs", `
    <div class="page">
      <h1>日志</h1>

      <div class="cfg-card">
        <div class="cfg-card-head"><span class="cfg-card-title">📋 查询</span>
          <a href="/api/logs/runtime/download" download style="font-size:12.5px">下载运行日志</a>
        </div>
        <div class="log-filter-row">
          <div class="log-tabs">
            <button data-cat="all" class="active">全部</button>
            <button data-cat="pipeline">课程生成</button>
            <button data-cat="material">素材</button>
            <button data-cat="action">用户操作</button>
            <button data-cat="runtime">运行日志</button>
          </div>
          <input id="log-kw" type="text" placeholder="关键字搜索（消息/详情/课程ID）" style="flex:1;min-width:180px">
          <select id="log-level" style="width:110px;flex-shrink:0">
            <option value="">全部级别</option>
            <option value="info">info</option>
            <option value="warn">warn</option>
            <option value="error">error</option>
          </select>
          <button id="log-refresh" style="flex-shrink:0">查询</button>
        </div>
        <div id="log-list" class="log-list"><div class="spinner"></div></div>
        <div class="cal-hint" style="margin-top:8px">自动刷新：生成过程中每 5 秒更新；手动点「查询」立即刷新</div>
      </div>
    </div>`);

  let curCat = "all";
  let autoTimer = null;

  async function refresh() {
    const kw = document.getElementById("log-kw").value.trim();
    const level = document.getElementById("log-level").value;
    const listEl = document.getElementById("log-list");
    try {
      const items = await api(`/api/logs?category=${curCat}&keyword=${encodeURIComponent(kw)}&level=${level}&limit=300`);
      if (!items.length) {
        listEl.innerHTML = `<div class="panel-empty">暂无日志</div>`;
        return;
      }
      const levelTag = (lv) => lv === "error" ? `<span class="log-lv err">ERROR</span>`
        : lv === "warn" ? `<span class="log-lv warn">WARN</span>`
        : "";
      const catTag = (c) => ({ runtime: "运行", pipeline: "生成", action: "操作", material: "素材" }[c] || c);
      listEl.innerHTML = items.map((e) => `
        <div class="log-row ${e.level === "error" ? "log-err" : e.level === "warn" ? "log-warn" : ""}">
          <span class="log-ts">${esc((e.ts || "").replace("T", " ").slice(0, 19))}</span>
          <span class="log-cat">${catTag(e.category)}</span>
          ${levelTag(e.level)}
          <span class="log-msg">${esc(e.message)}${e.course_id ? ` <a href="#/course/${esc(e.course_id)}" style="color:var(--accent);font-size:12px">课程</a>` : ""}</span>
          ${e.detail ? `<div class="log-detail">${esc(e.detail)}</div>` : ""}
        </div>`).join("");
    } catch (err) {
      listEl.innerHTML = `<div class="panel-empty">加载失败: ${esc(err.message)}</div>`;
    }
  }

  document.querySelectorAll(".log-tabs button").forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll(".log-tabs button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      curCat = btn.dataset.cat;
      refresh();
    };
  });
  document.getElementById("log-refresh").onclick = refresh;
  document.getElementById("log-kw").onkeydown = (e) => { if (e.key === "Enter") refresh(); };
  document.getElementById("log-level").onchange = refresh;

  refresh();
  state.pollTimer = setInterval(refresh, 5000);
}

/* ---------------- helpers ---------------- */
function findSentenceAt(t, sentences) {
  // Sentences are contiguous (end_i === start_i+1), so a "+0.5s tail" must not
  // win over a sentence that has already started — otherwise the highlight
  // lags one sentence behind for the first half second of every line.
  let last = null;
  for (let i = 0; i < sentences.length; i++) {
    const s = sentences[i];
    if (s.start > t) break;
    last = i;
    if (t < s.end) return i;
  }
  if (last === null) return null;
  // Inside a short pause after a sentence: keep the highlight there.
  if (t - sentences[last].end <= 0.5) return last;
  const lastIdx = sentences.length - 1;
  return lastIdx >= 0 && t >= sentences[lastIdx].end ? lastIdx : null;
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
    // word-char class is fixed; the term itself only goes into the escaped group.
    // (embedding the term in the char class breaks on hyphens/spaces -> Range out of order)
    try {
      const re = new RegExp(`(?<![\\w'’-])(${term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})(?![\\w'’-])`, "gi");
      html = html.replace(re, (m) =>
        `<span class="vmark" data-term="${term}">${m}<span class="vpop">${esc(v.definition || "")}</span></span>`);
    } catch {
      // pathological term: fall back to plain escaped occurrence highlight
      const safe = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      html = html.replace(new RegExp(safe, "gi"), (m) =>
        `<span class="vmark" data-term="${term}">${m}<span class="vpop">${esc(v.definition || "")}</span></span>`);
    }
  }
  return html;
}

route();
