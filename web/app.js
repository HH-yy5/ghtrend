/* ============================================================
   GHTrend 桌面版 · 前端逻辑
   无框架、无构建步骤：原生 fetch + EventSource(SSE)
   ============================================================ */

const $ = (sel) => document.querySelector(sel);

const state = {
  lang: "",
  top: 20,
  minDelta: 0,
  data: null,
  sse: null,
  langsReady: false,
  linksReady: false,
  linkMode: localStorage.getItem("ghtrend.linkMode") || "",
  linkModes: [],
  busy: false,
  summarizing: false,
  // 收藏 / 视图
  view: localStorage.getItem("ghtrend.view") || "board",
  library: [],
  libLoaded: false,
  search: "",
  focusIndex: -1,
};

/** 最小转义。收藏里的标题、备注来自用户和 README，直接拼 HTML 会破坏结构。 */
const esc = (s) =>
  String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

/* ---------------- 小工具 ---------------- */

const fmt = (n) => Number(n || 0).toLocaleString("en-US");
const fmt1 = (n) =>
  Number(n || 0).toLocaleString("en-US", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", !!isError);
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("show"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => (el.hidden = true), 260);
  }, isError ? 5200 : 3000);
}

function setLive(mode) {
  const chip = $("#liveChip");
  const text = $("#liveText");
  chip.dataset.state = mode;
  text.textContent =
    mode === "on" ? "实时已连接" : mode === "off" ? "已断开，重连中" : "连接中";
}

/* ---------------- 渲染 ---------------- */

// 把 github.com 链接按用户选择的镜像重写。
// 数据拿到了却点不开项目，是大陆网络下最典型的"断在半路"。
// 把 github.com 链接按用户选择的镜像重写。
// 数据拿到了却点不开项目，是大陆网络下最典型的"断在半路"。
function applyLink(url, path) {
  const modes = state.linkModes || [];
  if (!modes.length) return url;
  let mode = modes.find((m) => m.id === state.linkMode) || modes[0];

  // auto：跟着抓取用的那条线走。连不上 github.com 的网络里，
  // 抓取必然走镜像，那网页也走同一个镜像才打得开。
  if (mode && mode.id === "auto") {
    const apiId = (state.api && state.api.id) || "";
    const direct = modes.find((m) => m.id === "direct");
    const same = modes.find((m) => m.id === apiId && m.id !== "auto");
    const proxy = modes.find((m) => m.id === "ghproxy");
    mode = apiId === "direct" && direct ? direct : (same || proxy || direct || mode);
  }

  if (!mode || !mode.template) return url;
  return mode.template.replace("{url}", url || "").replace("{path}", path || "");
}

function fillLinkModes(modes, defaultId) {
  if (state.linksReady || !modes || modes.length < 2) return;

  // 老版本把 direct / auto 存进了 localStorage。默认方案改过一次
  // （auto → direct），不升版本号的话老用户会一直被旧值卡住，
  // 表现就是"点了还是在加速站里出不来"。
  const LINK_VER = "3";
  try {
    if (localStorage.getItem("ghtrend.linkVer") !== LINK_VER) {
      localStorage.removeItem("ghtrend.linkMode");
      localStorage.setItem("ghtrend.linkVer", LINK_VER);
      state.linkMode = "";
    }
  } catch (err) { /* 隐私模式下 localStorage 不可用，忽略 */ }

  state.linkModes = modes;
  if (!state.linkMode || !modes.some((m) => m.id === state.linkMode)) {
    state.linkMode = defaultId || modes[0].id;
  }
  const sel = $("#selLink");
  sel.innerHTML = modes
    .map((m) => `<option value="${m.id}">${m.label}</option>`)
    .join("");
  sel.value = state.linkMode;
  $("#linkField").hidden = false;
  state.linksReady = true;
}

function renderStats(d) {
  const isDelta = d.mode === "delta";
  const cards = [
    {
      k: isDelta ? "今日 Star 增量合计" : "当前为新星榜模式",
      v: isDelta ? `+${fmt(d.stats.totalDelta)}` : "—",
      cls: isDelta ? "up" : "",
      hint: isDelta
        ? `上榜 ${d.stats.count} 个项目合计`
        : "本地只有 1 份快照，明天起可算日增",
    },
    {
      k: "上榜项目",
      v: fmt(d.stats.count),
      cls: "accent",
      hint: `筛选后 Top ${d.filters.top}`,
    },
    {
      k: "候选池规模",
      v: fmt(d.poolSize),
      cls: "",
      hint: `可比样本 ${fmt(d.sample)} 个`,
    },
    {
      k: "数据日期",
      v: d.date.slice(5),
      cls: "",
      hint: isDelta ? `基准 ${(d.baseline || "").slice(5)}` : "本地快照 1 份",
    },
  ];

  $("#stats").innerHTML = cards
    .map(
      (c) => `<div class="stat">
        <div class="k">${c.k}</div>
        <div class="v ${c.cls}">${c.v}</div>
        <div class="hint">${c.hint}</div>
      </div>`
    )
    .join("");
}

function fillLanguages(list) {
  if (state.langsReady) return;
  const sel = $("#selLang");
  const frag = document.createDocumentFragment();
  (list || []).forEach((l) => {
    const o = document.createElement("option");
    o.value = l.name;
    o.textContent = `${l.name}（${l.count}）`;
    frag.appendChild(o);
  });
  sel.appendChild(frag);
  state.langsReady = true;
}

function rowHTML(it, mode, daysGap) {
  const rankCls = it.rank <= 3 ? `top${it.rank}` : "";

  // 注意：这两行必须包在同一个 .dcol 里。
  // 直接把两个 div 并列输出会让 grid 多出一列，把「总 Star」挤到下一行。
  let inner = "";
  if (mode === "delta") {
    const val = daysGap > 1 ? it.perDay : it.delta;
    const cls = val > 0 ? "up" : val < 0 ? "down" : "flat";
    const text = val > 0 ? `+${fmt(val)}` : fmt(val);
    inner = `<div class="delta ${cls}">${text}</div>` +
      (daysGap > 1 ? `<div class="perday">日均</div>` : "");
  } else {
    inner = `<div class="delta up">${fmt1(it.velocity)}</div>
             <div class="perday">星/天</div>`;
  }

  const topics = (it.topics || []).slice(0, 4)
    .map((t) => `<span>${t}</span>`).join("");

  const href = it.href || it.url || "";
  let badge = "";
  if (it.plainSrc === "llm") {
    badge = `<span class="badge ai">AI 人话</span>`;
  } else if (it.plainSrc) {
    badge = `<span class="badge">规则标题</span>`;
  }

  const title = it.plainTitle || it.summary || it.name;
  const detail = it.plainDetail || it.summary || "";

  return `<div class="row" data-name="${it.name}">
    <div class="rank ${rankCls}">${it.rank}</div>
    <div class="body">
      <button class="ttl" type="button" aria-expanded="false">
        <span class="ttl-text">${title}</span>
        <span class="caret" aria-hidden="true">▾</span>
      </button>
      <div class="meta">
        <a class="repo" href="${href}" target="_blank" rel="noreferrer">${it.name}</a>
        ${it.language ? `<span class="lang">${it.language}</span>` : ""}
        ${badge}
        <button class="star-btn${it.starred ? " on" : ""}" type="button"
                data-star="${esc(it.name)}"
                title="${it.starred ? "取消收藏" : "收藏，之后能看到它涨了多少"}"
                aria-label="${it.starred ? "取消收藏" : "收藏"}">${it.starred ? "★" : "☆"}</button>
      </div>
      <div class="detail" hidden>
        <p class="plain">${detail}</p>
        ${topics ? `<div class="topics">${topics}</div>` : ""}
      </div>
      <div class="rowfoot">
        <a class="ghlink" href="${href}" target="_blank" rel="noreferrer">在 GitHub 查看 →</a>
      </div>
    </div>
    <div class="dcol">${inner}</div>
    <div class="stars"><b>${fmt(it.stars)}</b><small>总 Star</small></div>
  </div>`;
}

function renderBoard(d) {
  state.focusIndex = -1;   // 重画后旧的键盘焦点位置已经没有意义

  if (!d.items.length) {
    $("#board").innerHTML = `<div class="state-block">
      <div class="icon">🔍</div>
      <h2>没有符合条件的项目</h2>
      <p>当前候选池 ${fmt(d.poolSize)} 个仓库，可能是筛选条件过窄。
      试试把语言改回「全部语言」，或把最小增量清零。</p>
      <button class="btn" data-act="reset">清除筛选条件</button>
    </div>`;
    return;
  }

  const head = `<div class="board-head">
    <div>#</div>
    <div>项目</div>
    <div class="r">${d.mode === "delta"
      ? (d.daysGap > 1 ? "日均增量" : "24h 增量")
      : "爆发速度"}</div>
    <div class="r c-stars">总 Star</div>
  </div>`;

  $("#board").innerHTML =
    head + d.items
      .map((it) => rowHTML({ ...it, href: applyLink(it.url, it.name) },
                           d.mode, d.daysGap))
      .join("");
}

function renderEmpty(msg, withButton) {
  $("#stats").innerHTML = `
    <div class="stat"><div class="k">状态</div><div class="v">—</div><div class="hint">等待数据</div></div>
    <div class="stat"><div class="k">候选池</div><div class="v">—</div><div class="hint">尚未抓取</div></div>
    <div class="stat"><div class="k">上榜项目</div><div class="v">—</div><div class="hint">—</div></div>
    <div class="stat"><div class="k">数据日期</div><div class="v">—</div><div class="hint">—</div></div>`;

  $("#board").innerHTML = `<div class="state-block">
    <div class="icon">📡</div>
    <h2>今天还没有数据</h2>
    <p>${msg}</p>
    ${withButton ? `<button class="btn primary" data-act="refresh">开始抓取</button>` : ""}
  </div>`;
}

function render(d) {
  if (!d || !d.ok) {
    const msg =
      d && d.reason === "no_snapshot"
        ? "点下面的按钮抓取一次，大约需要 1 分钟。抓取期间这里会显示实时进度。"
        : (d && d.message) || "本地服务返回了异常结果。";
    renderEmpty(msg, true);
    $("#metaLine").textContent = "尚未抓取数据";
    return;
  }

  renderStats(d);
  fillLanguages(d.languages);
  state.api = d.api || {};                         // 必须先于 renderBoard：applyLink 要用它选线路
  fillLinkModes(d.linkModes, d.defaultLinkMode);   // 必须先于 renderBoard
  renderBoard(d);

  // 当前走的是哪条线路 —— 走了第三方镜像要给用户一个明确提示
  const api = d.api || {};
  const apiChip = $("#apiChip");
  if (api.base) {
    const thirdParty = String(api.base).indexOf("api.github.com") === -1;
    apiChip.hidden = false;
    apiChip.textContent =
      `线路：${api.label || "未知"}` + (api.latencyMs ? ` ${api.latencyMs}ms` : "");
    apiChip.className = "chip" + (thirdParty ? " warn" : " muted");
    apiChip.title = api.base;
  }

  const isDelta = d.mode === "delta";
  let meta = isDelta
    ? `${d.daysGap > 1 ? `过去 ${d.daysGap} 天日均` : "过去 24 小时"} star 增量 · 基准 ${
        d.baseline
      } · ${fmt(d.sample)} 个仓库可比`
    : `新星榜（star ÷ 项目年龄）· ${fmt(d.poolSize)} 个候选仓库`;
  // 断档是"日均"失真的直接原因，必须显式说明 —— 否则用户会把 5 天均值当成昨天的增量
  if (isDelta && d.daysGap > 1) meta += ` ｜ 中间缺 ${d.daysGap - 1} 天，已按日均折算`;
  $("#metaLine").textContent = meta;
  setLibCount(d.libraryCount || 0);

  const chip = $("#modeChip");
  chip.hidden = false;
  chip.textContent = isDelta ? "日增榜" : "新星榜";
  chip.className = "chip" + (isDelta ? "" : " warn");

  $("#timeChip").textContent = `生成于 ${(d.generatedAt || "").slice(11)}`;
  $("#footMeta").textContent = d.note ? d.note : `共 ${fmt(d.poolSize)} 个候选仓库`;

  // AI 人话没启用时，标题只能靠规则生成，得让用户知道能升级
  const sumChip = $("#sumChip");
  if (!d.llmEnabled) {
    sumChip.hidden = false;
    sumChip.className = "chip muted";
    sumChip.textContent = "AI 人话未启用";
    sumChip.title = "在 config.json 配置 llm 后，每行标题会自动变成看得懂的人话";
  }

  maybeSummarize(d);
}

// 后端逐条生成完一条，就就地更新那一行的标题与详情
function updateRow(d) {
  if (!d || !d.name) return;
  const row = document.querySelector(`.row[data-name="${d.name}"]`);
  if (!row) return;

  const t = row.querySelector(".ttl-text");
  if (t && d.title) t.textContent = d.title;

  const p = row.querySelector(".detail .plain");
  if (p && d.detail) p.textContent = d.detail;

  if (d.src === "llm") {
    const meta = row.querySelector(".meta");
    const old = meta && meta.querySelector(".badge");
    if (old) old.remove();
    if (meta) {
      const b = document.createElement("span");
      b.className = "badge ai";
      b.textContent = "AI 人话";
      meta.appendChild(b);
    }
  }
}

function maybeSummarize(d) {
  if (!d || !d.llmEnabled || !d.summaryPending) return;
  if (state.summarizing) return;
  if (d.summaryJob && d.summaryJob.state === "running") {
    state.summarizing = true;
    return;
  }
  state.summarizing = true;

  const params = new URLSearchParams({ top: state.top });
  if (state.lang) params.set("language", state.lang);
  if (state.minDelta > 0) params.set("min_delta", state.minDelta);

  fetch(`/api/summarize?${params.toString()}`, { method: "POST" })
    .then((r) => r.json())
    .then((res) => { if (!res.ok) state.summarizing = false; })
    .catch(() => { state.summarizing = false; });
}

/* ---------------- 收藏 / 视图切换 ---------------- */

function setLibCount(n) {
  const el = $("#libCount");
  el.textContent = n > 0 ? String(n) : "";
  el.hidden = !(n > 0);
}

function setView(view) {
  state.view = view;
  try {
    localStorage.setItem("ghtrend.view", view);
  } catch (err) { /* ignore */ }
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("is-active", t.dataset.view === view);
  });
  $("#viewBoard").hidden = view !== "board";
  $("#viewLibrary").hidden = view !== "library";
  if (view === "library") loadLibrary();
}

async function loadLibrary() {
  try {
    const res = await fetch("/api/library", { cache: "no-store" });
    const d = await res.json();
    state.library = d.items || [];
    state.libLoaded = true;
    setLibCount(d.count || 0);
    renderLibrary();
  } catch (err) {
    $("#libList").innerHTML = `<div class="state-block">
      <div class="icon">!</div>
      <h2>读不到收藏</h2>
      <p>本地服务可能已经退出了。重新双击 GHTrend.exe 再试。</p>
    </div>`;
  }
}

function libCardHTML(r) {
  const href = applyLink(r.url, r.name);
  const delta = r.deltaSinceAdd;
  let numLine;
  if (delta !== null && delta !== undefined) {
    numLine = `<small class="up">收藏后 ${delta >= 0 ? "+" : ""}${fmt(delta)}</small>`;
  } else if (r.stale) {
    numLine = `<small class="stale">今天没抓到，数字未更新</small>`;
  } else {
    numLine = `<small class="stale">暂无对比</small>`;
  }
  const note = r.note || "";
  return `<div class="lib-card" data-name="${esc(r.name)}">
    <button class="star-btn on" type="button" data-star="${esc(r.name)}"
            title="取消收藏" aria-label="取消收藏">★</button>
    <div>
      <div class="lib-title">${esc(r.title || r.name)}</div>
      <div class="lib-meta">
        <a href="${href}" target="_blank" rel="noreferrer">${esc(r.name)}</a>
        ${r.language ? `<span>${esc(r.language)}</span>` : ""}
        <span>收藏于 ${(r.addedAt || "").slice(5, 10) || "—"}</span>
      </div>
      <div class="lib-note${note ? "" : " empty"}" data-note="${esc(r.name)}">${
        note ? esc(note) : "点击写个备注，记一下为什么收藏"
      }</div>
    </div>
    <div class="lib-num">
      <b>${fmt(r.stars)}</b>
      ${numLine}
    </div>
  </div>`;
}

function renderLibrary() {
  const q = state.search.trim().toLowerCase();
  const rows = state.library.filter((r) => {
    if (!q) return true;
    return [r.name, r.title, r.note, r.language]
      .join(" ").toLowerCase().includes(q);
  });

  if (!rows.length) {
    const hasAny = state.library.length > 0;
    $("#libList").innerHTML = `<div class="state-block">
      <div class="icon">☆</div>
      <h2>${hasAny ? "没有匹配的收藏" : "还没有收藏"}</h2>
      <p>${hasAny
        ? "换个关键词试试。项目名、标题、备注都能搜。"
        : "在榜单里点项目名右边的星标就会收藏到这里。收藏时会记下当时的 star，之后能看到它涨了多少。"}</p>
      ${hasAny
        ? ""
        : `<button class="btn primary" type="button" data-act="toBoard">去榜单看看</button>`}
    </div>`;
    return;
  }

  $("#libList").innerHTML = rows.map(libCardHTML).join("");
}

async function toggleStar(name, btn) {
  const row = (state.data && state.data.items || []).find((x) => x.name === name);
  const payload = { name };
  if (row) {
    payload.stars = row.stars;
    payload.url = row.url;
    payload.title = row.plainTitle || "";
    payload.language = row.language || "";
  }
  try {
    const res = await fetch("/api/library/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await res.json();
    if (!d.ok) {
      toast(d.message || "收藏失败", true);
      return;
    }
    setLibCount(d.count || 0);
    if (btn) {
      btn.classList.toggle("on", !!d.starred);
      btn.textContent = d.starred ? "★" : "☆";
      btn.title = d.starred ? "取消收藏" : "收藏，之后能看到它涨了多少";
    }
    toast(d.starred ? "已收藏" : "已取消收藏");
    if (state.view === "library") loadLibrary();
  } catch (err) {
    toast("无法连接本地服务", true);
  }
}

function editNote(name) {
  const card = document.querySelector(`.lib-card[data-name="${CSS.escape(name)}"]`);
  if (!card) return;
  const box = card.querySelector(".lib-note");
  if (!box || box.querySelector("input")) return;

  const item = state.library.find((r) => r.name === name) || {};
  const cur = item.note || "";

  box.classList.remove("empty");
  box.textContent = "";
  const inp = document.createElement("input");
  inp.className = "note-input";
  inp.value = cur;
  inp.maxLength = 120;
  inp.placeholder = "例如：做数据看板时找接口用";
  box.appendChild(inp);
  inp.focus();
  inp.select();

  let done = false;
  const commit = async (save) => {
    if (done) return;
    done = true;
    const val = save ? inp.value.trim() : cur;
    box.textContent = val || "点击写个备注，记一下为什么收藏";
    box.classList.toggle("empty", !val);
    state.library.forEach((r) => { if (r.name === name) r.note = val; });
    if (!save) return;
    try {
      await fetch("/api/library/note", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, note: val }),
      });
      toast("备注已保存");
    } catch (err) {
      toast("备注没保存成功", true);
    }
  };

  inp.addEventListener("blur", () => commit(true), { once: true });
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); inp.blur(); }
    if (e.key === "Escape") { e.preventDefault(); commit(false); }
  });
}

/* ---------------- 数据加载 ---------------- */

async function loadBoard() {
  const params = new URLSearchParams({ top: state.top });
  if (state.lang) params.set("language", state.lang);
  if (state.minDelta > 0) params.set("min_delta", state.minDelta);

  try {
    const res = await fetch(`/api/board?${params.toString()}`, { cache: "no-store" });
    const data = await res.json();
    state.data = data;
    render(data);
  } catch (e) {
    renderEmpty("无法连接本地服务。服务可能已经退出，请重新双击「打开界面.bat」。", false);
    $("#metaLine").textContent = "本地服务未响应";
  }
}

/* ---------------- 抓取进度（SSE） ---------------- */

function showProgress(text) {
  const box = $("#progress");
  box.hidden = false;
  $("#progText").textContent = text || "抓取中…";
}

function hideProgress() {
  $("#progress").hidden = true;
}

function setBusy(busy) {
  state.busy = busy;
  const btn = $("#btnRefresh");
  btn.disabled = busy;
  btn.textContent = busy ? "抓取中…" : "抓取最新数据";
}

async function doRefresh() {
  if (state.busy) return;
  setBusy(true);
  showProgress("正在启动抓取…");
  try {
    const res = await fetch("/api/refresh", { method: "POST" });
    const d = await res.json();
    if (!d.ok) {
      toast(d.message || "无法启动抓取", true);
      setBusy(false);
      hideProgress();
    }
  } catch (e) {
    toast("无法连接本地服务", true);
    setBusy(false);
    hideProgress();
  }
}

function connectSSE() {
  if (state.sse) {
    try { state.sse.close(); } catch (e) { /* ignore */ }
  }
  const es = new EventSource("/api/events");
  state.sse = es;

  es.addEventListener("hello", (ev) => {
    setLive("on");
    try {
      const d = JSON.parse(ev.data);
      if (d.job && d.job.state === "running") {
        setBusy(true);
        showProgress(d.job.step);
      }
    } catch (e) { /* ignore */ }
  });

  es.addEventListener("progress", (ev) => {
    try {
      // 服务端把引擎的日志行原样推过来 —— 这是真实的抓取进度，不是假动画
      showProgress(JSON.parse(ev.data).step);
    } catch (e) { /* ignore */ }
  });

  es.addEventListener("job", (ev) => {
    let d = {};
    try { d = JSON.parse(ev.data); } catch (e) { return; }

    if (d.state === "running") {
      setBusy(true);
      showProgress(d.step);
    } else if (d.state === "done") {
      hideProgress();
      setBusy(false);
      toast(`抓取完成，共 ${fmt(d.count)} 个仓库`);
      loadBoard();
    } else if (d.state === "error") {
      hideProgress();
      setBusy(false);
      toast(d.error || "抓取失败", true);
    }
  });

  // 数据更新广播：将来加"每日定时推送"时，服务端 broadcast 一下这里就会自动刷新
  es.addEventListener("board-updated", () => {
    state.langsReady = false;
    loadBoard();
  });

  // 人话总结：每生成一条就推一条，标题会一条条"长出来"
  es.addEventListener("summary", (ev) => {
    let d = {};
    try { d = JSON.parse(ev.data); } catch (err) { return; }
    updateRow(d);
  });

  es.addEventListener("summary-job", (ev) => {
    let d = {};
    try { d = JSON.parse(ev.data); } catch (err) { return; }
    const chip = $("#sumChip");
    if (d.state === "running") {
      chip.hidden = false;
      chip.className = "chip";
      chip.textContent = `人话总结 ${d.done}/${d.total || "…"}`;
      chip.title = "正在逐条读取项目说明并改写成大白话";
    } else {
      chip.hidden = true;
      state.summarizing = false;
      if (d.state === "done" && d.done > 0) {
        toast(`已生成 ${d.done} 条人话总结`);
      }
    }
  });

  es.addEventListener("library", (ev) => {
    let d = {};
    try { d = JSON.parse(ev.data); } catch (err) { return; }
    setLibCount(d.count || 0);
    if (state.view === "library") loadLibrary();
  });

  es.onopen = () => setLive("on");
  es.onerror = () => setLive("off"); // EventSource 会自动重连，不用手写重连逻辑
}

/* ---------------- 事件绑定 ---------------- */

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

document.addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) {
    setView(tab.dataset.view);
    return;
  }

  const starBtn = e.target.closest("[data-star]");
  if (starBtn) {
    e.preventDefault();
    toggleStar(starBtn.dataset.star, starBtn);
    return;
  }

  const noteBox = e.target.closest("[data-note]");
  if (noteBox && !noteBox.querySelector("input")) {
    editNote(noteBox.dataset.note);
    return;
  }

  // 点「人话标题」展开 / 收起详情
  const ttl = e.target.closest(".ttl");
  if (ttl) {
    const row = ttl.closest(".row");
    const detail = row && row.querySelector(".detail");
    if (detail) {
      detail.hidden = !detail.hidden;
      row.classList.toggle("open", !detail.hidden);
      ttl.setAttribute("aria-expanded", detail.hidden ? "false" : "true");
    }
    return;
  }

  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "refresh") doRefresh();
  if (act === "toBoard") setView("board");
  if (act === "reset") {
    $("#selLang").value = "";
    $("#inpDelta").value = "";
    state.lang = "";
    state.minDelta = 0;
    loadBoard();
  }
});

$("#btnRefresh").addEventListener("click", doRefresh);

$("#btnQuit").addEventListener("click", async () => {
  if (!confirm("退出后本地服务会一起关闭。确定吗？")) return;
  try { await fetch("/api/quit", { method: "POST" }); } catch (e) { /* ignore */ }
  document.body.innerHTML = `<div class="state-block" style="padding-top:120px">
    <div class="icon">👋</div>
    <h2>已退出</h2>
    <p>可以关掉这个窗口了。<br>下次双击「打开界面.bat」重新打开。</p>
  </div>`;
});

$("#selLang").addEventListener("change", (e) => {
  state.lang = e.target.value;
  loadBoard();
});

$("#selTop").addEventListener("change", (e) => {
  state.top = Number(e.target.value);
  loadBoard();
});

$("#selLink").addEventListener("change", (e) => {
  state.linkMode = e.target.value;
  try { localStorage.setItem("ghtrend.linkMode", state.linkMode); } catch (err) { /* ignore */ }
  // 只重画列表即可，不需要重新请求后端
  if (state.data && state.data.ok) renderBoard(state.data);
});

$("#inpDelta").addEventListener(
  "input",
  debounce((e) => {
    const v = Number(e.target.value);
    state.minDelta = Number.isFinite(v) && v > 0 ? Math.floor(v) : 0;
    loadBoard();
  }, 420)
);

$("#inpSearch").addEventListener(
  "input",
  debounce((e) => {
    state.search = e.target.value || "";
    if (state.view === "library") renderLibrary();
  }, 160)
);

// 键盘：j / k 上下移动，Enter 展开详情。焦点在输入框里时不拦截。
document.addEventListener("keydown", (e) => {
  if (state.view !== "board") return;
  if (e.target.closest("input, select, textarea")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  const rows = [...document.querySelectorAll("#board .row")];
  if (!rows.length) return;

  if (e.key === "j" || e.key === "k") {
    e.preventDefault();
    const step = e.key === "j" ? 1 : -1;
    let i = state.focusIndex < 0 ? 0 : state.focusIndex + step;
    i = Math.max(0, Math.min(i, rows.length - 1));
    state.focusIndex = i;
    rows.forEach((r, idx) => r.classList.toggle("focused", idx === i));
    rows[i].scrollIntoView({ block: "nearest" });
  } else if (e.key === "Enter" && state.focusIndex >= 0) {
    const ttl = rows[state.focusIndex] && rows[state.focusIndex].querySelector(".ttl");
    if (ttl) {
      e.preventDefault();
      ttl.click();
    }
  }
});

/* ---------------- 启动 ---------------- */

renderEmpty("正在读取本地数据…", false);
connectSSE();
setView(state.view);
loadBoard();
