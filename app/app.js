/* DeepRead SPA — hash routing: #/ library, #/settings, #/course/<id> */
"use strict";

const $app = document.getElementById("app");
const state = {
  course: null,      // full course payload {meta, status, source, lesson}
  progress: null,    // {read:[ids], cards:{termId: 'known'|'unknown'}}
  tab: "read",
  abLoop: null,      // {a: sec, b: sec}
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

/* ---------------- router ---------------- */
window.addEventListener("hashchange", route);

function route() {
  clearInterval(state.pollTimer);
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/course\/([\w-]+)/);
  if (m) return renderCourse(m[1]);
  if (hash.startsWith("#/settings")) return renderSettings();
  return renderLibrary();
}

/* ================= Library ================= */
async function renderLibrary() {
  document.title = "DeepRead · 英语精读";
  $app.innerHTML = `
    <div class="library">
      <div class="lib-head">
        <h1><span>Deep</span>Read</h1>
        <div class="lib-actions">
          <button id="btn-settings">⚙ 设置</button>
        </div>
      </div>
      <div class="dropzone" id="dropzone">
        <div class="dz-icon">⬆️</div>
        <div>拖拽视频 / 音频文件到这里，或点击选择</div>
        <div class="dz-hint">支持 mp4 / mkv / mov / mp3 / wav / m4a 等 · 内嵌字幕会被自动提取并与转写融合</div>
        <input type="file" id="file-input" hidden accept="video/*,audio/*">
      </div>
      <div class="course-grid" id="course-grid"><div class="spinner"></div></div>
    </div>`;

  document.getElementById("btn-settings").onclick = () => (location.hash = "#/settings");
  const dz = document.getElementById("dropzone");
  const fi = document.getElementById("file-input");
  dz.onclick = () => fi.click();
  fi.onchange = () => fi.files[0] && uploadFile(fi.files[0]);
  dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("dragover"); };
  dz.ondragleave = () => dz.classList.remove("dragover");
  dz.ondrop = (e) => {
    e.preventDefault(); dz.classList.remove("dragover");
    if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]);
  };

  await refreshCourses();
  // poll while any course is generating
  state.pollTimer = setInterval(refreshCourses, 3000);
}

async function refreshCourses() {
  try {
    const list = await api("/api/courses");
    const grid = document.getElementById("course-grid");
    if (!grid) return;
    if (!list.length) {
      grid.innerHTML = `<div class="empty-tip" style="grid-column:1/-1">还没有课程，上传第一个视频开始吧</div>`;
      return;
    }
    grid.innerHTML = list.map((c) => {
      const stageLabel = {
        uploaded: "待生成", transcribing: "转写", subtitles: "字幕", aligning: "对齐",
        generating: "精讲", ready: "完成", error: "失败", cancelled: "已取消",
      }[c.stage] || c.stage;
      const working = ["transcribing", "subtitles", "aligning", "generating"].includes(c.stage);
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
        ${c.stage === "ready" ? `<div class="cc-detail">已学 ${c.learned_percent || 0}%</div>` : ""}
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
        if (!confirm("确定删除这门课程？媒体文件也会一并删除。")) return;
        await api(`/api/courses/${btn.dataset.del}`, { method: "DELETE" });
        refreshCourses();
      };
    });
  } catch (err) {
    const grid = document.getElementById("course-grid");
    if (grid) grid.innerHTML = `<div class="empty-tip" style="grid-column:1/-1">加载失败: ${esc(err.message)}</div>`;
  }
}

async function uploadFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  toast(`上传中: ${file.name}…`);
  try {
    const res = await api("/api/courses/upload", { method: "POST", body: fd });
    toast("上传完成，开始生成…");
    await api(`/api/courses/${res.id}/generate`, { method: "POST" }).catch(() => {});
    refreshCourses();
  } catch (err) {
    toast(`上传失败: ${err.message}`);
  }
}

/* ================= Settings ================= */
function renderSettings() {
  document.title = "设置 · DeepRead";
  $app.innerHTML = `
    <div class="settings">
      <h1>API 设置</h1>
      <p style="color:var(--ink-2);font-size:13.5px">配置 OpenAI 兼容中转 API。API Key 保存在服务器本地 data/config.json，不会上传。</p>
      <div class="form-row"><label>Base URL（如 https://api.example.com/v1）</label>
        <input id="cfg-base" type="text" placeholder="https://.../v1"></div>
      <div class="form-row"><label>API Key</label>
        <input id="cfg-key" type="password" placeholder="sk-..."></div>
      <div class="form-row"><label>转写模型（/v1/audio/transcriptions）</label>
        <input id="cfg-asr" type="text" placeholder="whisper-1"></div>
      <div class="form-row"><label>Chat 模型（翻译 + 精讲生成）</label>
        <input id="cfg-chat" type="text" placeholder="gpt-4o-mini"></div>
      <div class="form-actions">
        <button class="primary" id="cfg-save">保存</button>
        <button id="cfg-test">测试连接</button>
        <button id="cfg-back">返回</button>
      </div>
      <div class="test-result" id="test-result"></div>
    </div>`;

  document.getElementById("cfg-back").onclick = () => (location.hash = "#/");
  api("/api/config").then((cfg) => {
    document.getElementById("cfg-base").value = cfg.base_url || "";
    document.getElementById("cfg-key").value = cfg.api_key || "";
    document.getElementById("cfg-asr").value = cfg.transcribe_model || "";
    document.getElementById("cfg-chat").value = cfg.chat_model || "";
  });

  document.getElementById("cfg-save").onclick = async () => {
    try {
      await api("/api/config", {
        method: "PUT",
        body: JSON.stringify({
          base_url: document.getElementById("cfg-base").value.trim(),
          api_key: document.getElementById("cfg-key").value.trim(),
          transcribe_model: document.getElementById("cfg-asr").value.trim(),
          chat_model: document.getElementById("cfg-chat").value.trim(),
        }),
      });
      toast("已保存");
    } catch (err) { toast(err.message); }
  };

  document.getElementById("cfg-test").onclick = async () => {
    const el = document.getElementById("test-result");
    el.className = "test-result"; el.textContent = "测试中…";
    // save first so the server tests the current values
    document.getElementById("cfg-save").click();
    try {
      const res = await api("/api/config/test", { method: "POST" });
      const parts = [];
      parts.push(res.chat ? "✓ chat 端点可用" : "✗ chat 端点不可用");
      parts.push(res.transcribe ? "✓ 转写端点可用" : "✗ 转写端点不可用");
      el.className = "test-result " + (res.chat || res.transcribe ? "ok" : "fail");
      el.textContent = parts.join("　") + (res.detail ? ` · ${res.detail}` : "");
    } catch (err) {
      el.className = "test-result fail";
      el.textContent = "连接失败: " + err.message;
    }
  };
}

/* ================= Course page ================= */
async function renderCourse(cid) {
  document.title = "课程 · DeepRead";
  $app.innerHTML = `<div style="padding:60px;text-align:center"><div class="spinner"></div></div>`;
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
    $app.innerHTML = `<div style="padding:60px;text-align:center;color:var(--red)">${esc(err.message)}</div>`;
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

function drawCoursePage() {
  const { meta, status, lesson } = state.course;
  if (status.stage !== "ready") {
    const working = ["transcribing", "subtitles", "aligning", "generating"].includes(status.stage);
    $app.innerHTML = `
      <div style="padding:60px;text-align:center">
        <h2>${esc(meta.title)}</h2>
        <p>${working ? `<div class="spinner"></div><p>正在生成: ${esc(status.detail || status.stage)} (${Math.round((status.progress || 0) * 100)}%)</p>` : `课程尚未生成: ${esc(status.detail || status.stage)}`}
        </p>
        ${!working ? `<button class="primary" onclick="location.reload()">刷新</button>` : ""}
        <p><a href="#/">返回课程库</a></p>
      </div>`;
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
  const sentences = lesson.sentences || [];

  $app.innerHTML = `
  <div class="course-page">
    <div class="cp-top">
      <button class="back" id="btn-back">←</button>
      <div class="cp-title">${esc(meta.title)}</div>
      <button id="btn-settings2">⚙</button>
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
          <button id="ctl-follow" class="on" title="字幕跟随">跟随</button>
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

  // vocabulary marks over sentence text
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

  document.getElementById("btn-back").onclick = () => (location.hash = "#/");
  document.getElementById("btn-settings2").onclick = () => (location.hash = "#/settings");

  // ---- playback sync ----
  let activeId = -1;
  player.addEventListener("timeupdate", () => {
    handleAbLoop(player);
    const t = player.currentTime;
    const idx = findSentenceAt(t, sentences);
    if (idx !== null && idx !== activeId) {
      activeId = idx;
      highlightSentence(idx);
      if (document.getElementById("ctl-follow").classList.contains("on")) {
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

  document.getElementById("ctl-follow").onclick = (e) => e.target.classList.toggle("on");

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

  // expose a global jump for card/link clicks
  window.__jumpTo = (sec) => {
    player.currentTime = sec;
    player.play();
    document.querySelector(".cp-left").scrollIntoView({ behavior: "smooth" });
  };
  window.__playRange = (start, end, rate) => {
    player.currentTime = start;
    if (rate) player.playbackRate = rate;
    player.play();
    // stop at end if not already looping
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
    const item = (x, cls) => `
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

/* ---------------- helpers ---------------- */
function findSentenceAt(t, sentences) {
  for (let i = 0; i < sentences.length; i++) {
    if (t >= sentences[i].start && t < sentences[i].end + 0.5) return i;
  }
  // after last sentence
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
    if (!/[一-鿿]/.test(v.term)) idx.push(v);  // only mark English terms
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
