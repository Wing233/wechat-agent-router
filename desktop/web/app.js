"use strict";

/* ------------------------------------------------------------------ state */
const state = {
  token: "",
  bootstrap: null,
  config: null,
  agent: null,
  router: null,
  server: null,
  serverError: "",
  serverTried: false,
  serverLoading: false,
  serverTab: "overview",
  balance: null,
  page: "overview",
  logs: [],
  lastLogId: 0,
  logFilter: "all",
  logLevel: "all",
  logSearch: "",
  autoScroll: true,
  modal: null,
  initialPage: "overview",
  initialServerTab: "overview",
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ helpers */
function esc(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function icon(name, cls = "") {
  return `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
}

function short(text, limit = 60) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length > limit ? value.slice(0, limit - 1) + "…" : value;
}

function fmtTime(value, withDate = false) {
  if (!value) return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, "0");
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  if (!withDate) return time;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`;
}

function fmtDate(value) {
  if (!value) return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function splitCommand(text) {
  const tokens = [];
  let buffer = "";
  let quote = "";
  for (const char of String(text || "")) {
    if (quote) {
      if (char === quote) quote = "";
      else buffer += char;
    } else if (char === "'" || char === '"') {
      quote = char;
    } else if (/\s/.test(char)) {
      if (buffer) { tokens.push(buffer); buffer = ""; }
    } else {
      buffer += char;
    }
  }
  if (buffer) tokens.push(buffer);
  return tokens;
}

function agentState(agent) {
  if (!agent) {
    return { key: "stopped", label: "未启动", short: "未启动", icon: "power",
      desc: "本机 Agent 还没有运行。" };
  }
  if (agent.config_error) {
    return { key: "error", label: "配置错误", short: "配置错误", icon: "alert",
      desc: agent.config_error };
  }
  if (agent.status === "online") {
    return { key: "online", label: "在线", short: "在线", icon: "activity",
      desc: "已连接 Router，等待微信任务。" };
  }
  if (agent.status === "busy") {
    return { key: "busy", label: "正在执行任务", short: "执行中", icon: "zap",
      desc: agent.current ? agent.current.message : "本地 CLI 正在运行。" };
  }
  if (agent.status === "connecting") {
    return { key: "busy", label: "正在连接", short: "连接中", icon: "wifi",
      desc: "正在向 Router 注册本机 Agent。" };
  }
  if (agent.status === "error") {
    return { key: "error", label: "连接异常", short: "连接异常", icon: "alert",
      desc: agent.last_error || "无法连接 Router，正在自动重试。" };
  }
  return { key: "stopped", label: "已停止", short: "已停止", icon: "power",
    desc: "本机 Agent 已停止轮询。" };
}

function badge(text, kind = "muted") {
  return `<span class="badge badge-${kind}">${esc(text)}</span>`;
}

function emptyState(iconName, title, text) {
  return `<div class="empty"><div class="empty-icon">${icon(iconName)}</div><strong>${esc(title)}</strong><p>${esc(text)}</p></div>`;
}

function callout(kind, title, text, actions = "") {
  const icons = { info: "shield", warn: "alert", danger: "alert", ok: "check" };
  return `<div class="callout callout-${kind}">${icon(icons[kind] || "shield")}<div><strong>${esc(title)}</strong><p>${esc(text)}</p>${actions ? `<div class="callout-actions">${actions}</div>` : ""}</div></div>`;
}

/* ------------------------------------------------------------------ api */
async function api(path, options = {}) {
  const headers = { "X-Desktop-Token": state.token };
  let body;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const response = await fetch(path, {
    method: options.method || (body !== undefined ? "POST" : "GET"),
    headers,
    body,
  });
  const text = await response.text();
  let data = {};
  if (text) {
    try { data = JSON.parse(text); } catch { data = { detail: text }; }
  }
  if (!response.ok) {
    throw new Error(data.detail || data.error || `HTTP ${response.status}`);
  }
  return data;
}

function initToken() {
  const hash = location.hash.replace(/^#/, "");
  if (hash.includes("t=")) {
    const params = new URLSearchParams(hash);
    const token = params.get("t");
    if (token) {
      state.token = token;
      state.initialPage = params.get("page") || "overview";
      state.initialServerTab = params.get("tab") || "overview";
      sessionStorage.setItem("wechatAgentToken", token);
      history.replaceState(null, "", `#/${state.initialPage}`);
      return;
    }
  }
  state.token = sessionStorage.getItem("wechatAgentToken") || "";
}

/* ------------------------------------------------------------------ toast / modal */
function toast(message, kind = "ok") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  const iconName = kind === "error" ? "alert" : kind === "warn" ? "alert" : "check";
  node.innerHTML = `${icon(iconName)}<div>${esc(message)}</div>`;
  $("toast-root").appendChild(node);
  setTimeout(() => {
    node.classList.add("out");
    setTimeout(() => node.remove(), 200);
  }, kind === "error" ? 5200 : 3200);
}

function hideModal() {
  state.modal = null;
  $("modal").hidden = true;
  $("modal-body").innerHTML = "";
  $("modal-foot").innerHTML = "";
}

function closeModal() {
  const modal = state.modal;
  hideModal();
  if (modal && modal.onClose) modal.onClose();
}

function openModal({
  title, eyebrow = "CONFIG", body = "", saveText = "保存", wide = false,
  danger = false, onSubmit = null, onClose = null,
}) {
  state.modal = { onSubmit, onClose };
  $("modal-title").textContent = title;
  $("modal-eyebrow").textContent = eyebrow;
  $("modal-body").innerHTML = body;
  $("modal-body").scrollTop = 0;
  const card = document.querySelector(".modal-card");
  card.classList.toggle("wide", Boolean(wide));
  const foot = [];
  foot.push(`<button class="btn btn-ghost" type="button" data-action="modal-close">关闭</button>`);
  if (saveText && onSubmit) {
    foot.push(`<button class="btn ${danger ? "btn-danger" : "btn-primary"}" id="modal-save" type="submit" form="modal-form">${esc(saveText)}</button>`);
  }
  $("modal-foot").innerHTML = foot.join("");
  $("modal").hidden = false;
  setTimeout(() => {
    const focus = $("modal-body").querySelector("input, textarea, select");
    if (focus) focus.focus();
  }, 40);
}

function confirmAction(title, message, confirmText = "确认") {
  return new Promise((resolve) => {
    state.modal = {
      onSubmit: async () => { resolve(true); return true; },
      onClose: () => resolve(false),
    };
    $("modal-title").textContent = title;
    $("modal-eyebrow").textContent = "CONFIRM";
    $("modal-body").innerHTML = callout("danger", "请确认操作", message);
    $("modal-foot").innerHTML = `
      <button class="btn btn-ghost" type="button" data-action="modal-close">取消</button>
      <button class="btn btn-danger" type="button" data-action="modal-confirm">${esc(confirmText)}</button>`;
    $("modal").hidden = false;
  });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

/* ------------------------------------------------------------------ sidebar */
function updateSidebar() {
  const meta = agentState(state.agent);
  const dot = $("sidebar-dot");
  if (dot) dot.className = `status-dot ${meta.key}`;
  const label = $("sidebar-agent-label");
  if (label) label.textContent = meta.short;
  const note = $("sidebar-agent-meta");
  if (note) {
    const id = state.agent && state.agent.agent_id ? state.agent.agent_id : "本机 Agent";
    const router = state.router && state.router.ok ? "Router 已连接" : "Router 未连接";
    note.textContent = `${id} · ${router}`;
  }
}

function updateOverviewLive() {
  const meta = agentState(state.agent);
  const orb = $("ov-orb");
  if (orb) { orb.className = `status-orb ${meta.key}`; orb.innerHTML = icon(meta.icon); }
  const title = $("ov-title");
  if (title) title.textContent = meta.label;
  const sub = $("ov-sub");
  if (sub) sub.textContent = meta.desc;
  const router = state.router || {};
  const routerEl = $("ov-router");
  if (routerEl) routerEl.textContent = router.ok ? "已连接" : "未连接";
  const routerNote = $("ov-router-note");
  if (routerNote) routerNote.textContent = router.ok ? `${router.latency_ms || 0} ms 延迟` : (router.error || "尚未配置");
  const current = state.agent && state.agent.current;
  const task = $("ov-task");
  if (task) task.textContent = current ? "执行中" : "空闲";
  const taskNote = $("ov-task-note");
  if (taskNote) taskNote.textContent = current ? short(current.message, 42) : "等待微信指令";
  const done = $("ov-done");
  if (done) done.textContent = String((state.agent && state.agent.tasks_done) || 0);
  const note = $("ov-agent-note");
  if (note) {
    const agent = state.agent || {};
    note.textContent = `${agent.name || state.config?.name || "本地 Agent"} · 已完成 ${agent.tasks_done || 0} 个任务`;
  }
}

function updateLocalLive() {
  const meta = agentState(state.agent);
  const orb = $("local-orb");
  if (orb) { orb.className = `status-orb ${meta.key}`; orb.innerHTML = icon(meta.icon); }
  const title = $("local-title");
  if (title) title.textContent = meta.label;
  const sub = $("local-sub");
  if (sub) sub.textContent = meta.desc;
  const current = state.agent && state.agent.current;
  const task = $("local-task");
  if (task) task.textContent = current ? short(current.message, 60) : "空闲";
  const seen = $("local-seen");
  if (seen) seen.textContent = state.agent && state.agent.last_seen ? fmtTime(state.agent.last_seen) : "—";
  const done = $("local-done");
  if (done) done.textContent = String((state.agent && state.agent.tasks_done) || 0);
}

/* ------------------------------------------------------------------ logs */
function logRowHtml(item) {
  const level = String(item.level || "INFO").toUpperCase();
  const source = item.source || (item.logger ? "server" : "desktop");
  return `<div class="log-row"><span class="log-time">${esc(fmtTime(item.ts))}</span><span class="log-level ${esc(level)}">${esc(level)}</span><span class="log-source">${esc(source)}</span><span class="log-msg">${esc(item.message)}</span></div>`;
}

function filteredLogs() {
  const search = state.logSearch.trim().toLowerCase();
  return state.logs.filter((item) => {
    if (state.logFilter !== "all" && item.source !== state.logFilter) return false;
    if (state.logLevel !== "all" && String(item.level).toUpperCase() !== state.logLevel) return false;
    if (search && !String(item.message || "").toLowerCase().includes(search)) return false;
    return true;
  });
}

function updateLogRows() {
  const box = $("log-rows");
  if (!box) return;
  const rows = filteredLogs().slice(-600);
  box.innerHTML = rows.length
    ? rows.map(logRowHtml).join("")
    : `<div class="log-empty">没有匹配的日志</div>`;
  const count = $("log-count");
  if (count) count.textContent = `${rows.length} 条`;
  if (state.autoScroll) box.scrollTop = box.scrollHeight;
}

function updateRecentLogs() {
  const box = $("ov-logs");
  if (!box) return;
  const rows = state.logs.slice(-6);
  box.innerHTML = rows.length
    ? rows.map(logRowHtml).join("")
    : `<div class="log-empty">暂无日志</div>`;
}

async function loadLogs(initial = false) {
  try {
    const after = initial ? 0 : state.lastLogId;
    const data = await api(`/api/logs?after=${after}`);
    if (initial) {
      state.logs = [];
      state.lastLogId = 0;
    }
    const rows = data.logs || [];
    if (rows.length) {
      state.logs.push(...rows);
      state.lastLogId = rows[rows.length - 1].id;
      if (state.logs.length > 1400) state.logs = state.logs.slice(-1000);
    }
    if (state.page === "logs") updateLogRows();
    if (state.page === "overview") updateRecentLogs();
  } catch {
    /* silent: the status poll already surfaces most failures */
  }
}

/* ------------------------------------------------------------------ overview */
function serverSummaryCard() {
  const connected = Boolean(state.bootstrap?.server?.connected);
  if (!connected) {
    return `<section class="card server-summary">
      <div class="server-summary-head"><div><h2 class="card-title">服务器</h2><p class="card-sub">Router 与插件</p></div>${badge("未连接", "muted")}</div>
      <div class="server-host">${icon("server")} 连接后可在这里管理 Agent、插件和定时任务</div>
      <div class="summary-grid">
        <div class="summary-item"><strong>—</strong><span>在线 Agent</span></div>
        <div class="summary-item"><strong>—</strong><span>启用插件</span></div>
      </div>
      <button class="btn btn-secondary" data-action="go-server">${icon("server")}连接服务器</button>
    </section>`;
  }
  const server = state.server;
  if (!server) {
    return `<section class="card server-summary">
      <div class="server-summary-head"><div><h2 class="card-title">服务器</h2><p class="card-sub">${esc(state.bootstrap.server.url || "")}</p></div>${badge("读取中", "warn")}</div>
      <div class="summary-grid"><div class="summary-item"><strong>…</strong><span>正在读取</span></div><div class="summary-item"><strong>…</strong><span>服务器状态</span></div></div>
      <button class="btn" data-action="server-refresh">${icon("refresh")}刷新</button>
    </section>`;
  }
  const agents = server.agents || [];
  const online = agents.filter((item) => item.online).length;
  const plugins = (server.plugins || []).filter((item) => item.enabled).length;
  const errors = Object.keys(server.plugin_errors || {}).length;
  return `<section class="card server-summary">
    <div class="server-summary-head"><div><h2 class="card-title">服务器</h2><p class="card-sub">${esc(server._server_url || state.bootstrap.server.url || "")}</p></div>${badge(errors ? `${errors} 个错误` : "运行正常", errors ? "danger" : "ok")}</div>
    <div class="server-host">${icon("server")} ${esc(server.version || "Router")} · ${esc(server.tools?.length || 0)} 个工具</div>
    <div class="summary-grid">
      <div class="summary-item"><strong>${online} / ${agents.length}</strong><span>在线 Agent</span></div>
      <div class="summary-item"><strong>${plugins} / ${(server.plugins || []).length}</strong><span>启用插件</span></div>
      <div class="summary-item"><strong>${(server.jobs || []).length}</strong><span>定时任务</span></div>
      <div class="summary-item"><strong>${errors}</strong><span>插件错误</span></div>
    </div>
    <button class="btn btn-secondary" data-action="go-server">${icon("settings")}打开服务器管理</button>
  </section>`;
}

function renderOverview() {
  const cfg = state.config || {};
  const agent = state.agent || {};
  const meta = agentState(agent);
  const router = state.router || {};
  const current = agent.current;
  const setup = !cfg.router;
  return `
    ${setup ? callout("warn", "还差一步：填写 Router 地址",
      "本机 Agent 需要知道服务器地址才能接任务。去「本机 Agent」填写并保存。",
      `<button class="btn btn-sm btn-primary" data-action="go-local">${icon("cpu")}去配置</button>`) : ""}
    <div class="grid-hero">
      <section class="card hero-card">
        <div class="status-line">
          <div class="status-orb ${meta.key}" id="ov-orb">${icon(meta.icon)}</div>
          <div><h2 class="hero-title" id="ov-title">${esc(meta.label)}</h2><p class="hero-sub" id="ov-sub">${esc(meta.desc)}</p></div>
        </div>
        <div class="hero-metrics">
          <div class="metric"><span class="metric-label">Agent ID</span><strong class="metric-value mono" id="ov-agent-id">${esc(agent.agent_id || "未配置")}</strong><span class="metric-note" id="ov-agent-note">${esc(agent.name || cfg.name || "本地 Agent")} · 已完成 ${esc(agent.tasks_done || 0)} 个任务</span></div>
          <div class="metric"><span class="metric-label">Router</span><strong class="metric-value" id="ov-router">${router.ok ? "已连接" : "未连接"}</strong><span class="metric-note" id="ov-router-note">${router.ok ? `${esc(router.latency_ms || 0)} ms 延迟` : esc(router.error || "尚未配置")}</span></div>
          <div class="metric"><span class="metric-label">当前任务</span><strong class="metric-value" id="ov-task">${current ? "执行中" : "空闲"}</strong><span class="metric-note" id="ov-task-note">${current ? esc(short(current.message, 42)) : "等待微信指令"}</span></div>
        </div>
        <div class="hero-actions">
          ${agent.running
            ? `<button class="btn btn-secondary" data-action="agent-stop">${icon("stop")}停止 Agent</button>`
            : `<button class="btn btn-primary" data-action="agent-start">${icon("play")}启动 Agent</button>`}
          <button class="btn" data-action="agent-restart">${icon("refresh")}重启</button>
          <button class="btn btn-ghost" data-action="test-router">${icon("wifi")}测试 Router</button>
          <button class="btn btn-ghost" data-action="go-server">${icon("server")}服务器</button>
        </div>
      </section>
      ${serverSummaryCard()}
    </div>

    <div class="grid-2">
      <section class="card">
        <div class="card-head"><div><h2 class="card-title">本机运行环境</h2><p class="card-sub">引擎和工作区都是本机路径，服务器只传别名。</p></div><button class="btn btn-sm" data-action="go-local">${icon("edit")}修改</button></div>
        <div class="card-body">
          <dl class="kv">
            <dt>默认引擎</dt><dd>${esc(agent.default_engine || cfg.default_engine || "—")}</dd>
            <dt>默认工作区</dt><dd class="mono">${esc(cfg.workspaces?.[agent.default_workspace || cfg.default_workspace] || "—")}</dd>
            <dt>可用引擎</dt><dd class="chips">${(agent.engines || Object.keys(cfg.engines || {})).map((item) => `<span class="chip">${esc(item)}</span>`).join("") || "—"}</dd>
            <dt>可用工作区</dt><dd class="chips">${(agent.workspaces || Object.keys(cfg.workspaces || {})).map((item) => `<span class="chip">${esc(item)}</span>`).join("") || "—"}</dd>
            <dt>上次心跳</dt><dd>${agent.last_seen ? fmtTime(agent.last_seen) : "—"}</dd>
          </dl>
          <div class="hero-actions">
            <button class="btn btn-sm" data-action="go-engines">${icon("terminal")}管理引擎</button>
            <button class="btn btn-sm" data-action="go-workspaces">${icon("folder")}管理工作区</button>
            <button class="btn btn-sm btn-ghost" data-action="open-path" data-kind="config">${icon("folder")}打开配置目录</button>
          </div>
        </div>
      </section>
      <section class="card">
        <div class="card-head"><div><h2 class="card-title">最近活动</h2><p class="card-sub">本机 Agent 与桌面控制台</p></div><button class="btn btn-sm btn-ghost" data-action="go-logs">${icon("list")}全部日志</button></div>
        <div class="card-body" style="padding-top:10px"><div class="log-list" id="ov-logs">${state.logs.slice(-6).map(logRowHtml).join("") || `<div class="log-empty">暂无日志</div>`}</div></div>
      </section>
    </div>`;
}

/* ------------------------------------------------------------------ local */
function selectOptions(values, selected) {
  return values.map((item) => `<option value="${esc(item)}" ${item === selected ? "selected" : ""}>${esc(item)}</option>`).join("");
}

function renderLocal() {
  const cfg = state.config || {};
  const agent = state.agent || {};
  const meta = agentState(agent);
  const engines = Object.keys(cfg.engines || {});
  const workspaces = Object.keys(cfg.workspaces || {});
  const keyMasked = String(cfg.agent_key || "").includes("...");
  return `
    ${cfg._meta?.config_error ? callout("danger", "配置文件需要修复", cfg._meta.config_error) : ""}
    <section class="card">
      <div class="card-head">
        <div><h2 class="card-title">运行状态</h2><p class="card-sub">${esc(cfg.router || "尚未配置 Router")}</p></div>
        <div class="card-actions">
          ${agent.running
            ? `<button class="btn btn-sm btn-secondary" data-action="agent-stop">${icon("stop")}停止</button>`
            : `<button class="btn btn-sm btn-primary" data-action="agent-start">${icon("play")}启动</button>`}
          <button class="btn btn-sm" data-action="agent-restart">${icon("refresh")}重启</button>
          ${agent.current ? `<button class="btn btn-sm btn-danger" data-action="agent-cancel">${icon("x")}取消任务</button>` : ""}
        </div>
      </div>
      <div class="card-body">
        <div class="status-line">
          <div class="status-orb ${meta.key}" id="local-orb">${icon(meta.icon)}</div>
          <div><h3 class="hero-title" id="local-title">${esc(meta.label)}</h3><p class="hero-sub" id="local-sub">${esc(meta.desc)}</p></div>
        </div>
        <div class="hero-metrics">
          <div class="metric"><span class="metric-label">当前任务</span><strong class="metric-value" id="local-task">${agent.current ? esc(short(agent.current.message, 40)) : "空闲"}</strong><span class="metric-note">${agent.current ? `开始于 ${fmtTime(agent.current.started_at)}` : "等待微信指令"}</span></div>
          <div class="metric"><span class="metric-label">上次心跳</span><strong class="metric-value" id="local-seen">${agent.last_seen ? fmtTime(agent.last_seen) : "—"}</strong><span class="metric-note">在线判定 120 秒</span></div>
          <div class="metric"><span class="metric-label">完成任务</span><strong class="metric-value" id="local-done">${esc(agent.tasks_done || 0)}</strong><span class="metric-note">${agent.last_task ? `最近 ${fmtTime(agent.last_task.finished_at)} · ${agent.last_task.ok ? "成功" : "失败"}` : "还没有任务"}</span></div>
        </div>
      </div>
    </section>

    <form id="local-form">
      <section class="card">
        <div class="card-head"><div><h2 class="card-title">身份与连接</h2><p class="card-sub">Agent ID 和 Key 必须与服务器上的注册信息一致。</p></div></div>
        <div class="card-body">
          <div class="form-grid">
            <div class="field"><label>Agent ID</label><input class="input mono" name="agent_id" value="${esc(cfg.agent_id || "")}" placeholder="pc-home" autocomplete="off"><small>小写字母、数字、下划线或短横线，必须唯一。</small></div>
            <div class="field"><label>显示名称</label><input class="input" name="name" value="${esc(cfg.name || "")}" placeholder="家里电脑"></div>
            <div class="field span-2"><label>Router 地址</label><div class="input-group"><input class="input mono" name="router" value="${esc(cfg.router || "")}" placeholder="https://router.example.com" autocomplete="off"><button class="btn" type="button" data-action="test-router">${icon("wifi")}测试</button></div><small>本机主动出站轮询，不需要开放本地端口。</small></div>
            <div class="field span-2"><label>Agent Key</label><div class="input-group"><input class="input mono" id="agent-key-input" name="agent_key" type="password" value="${esc(cfg.agent_key || "")}" data-masked="${keyMasked ? "1" : "0"}" autocomplete="off"><button class="btn" type="button" data-action="toggle-key">${icon("eye")}显示</button><button class="btn" type="button" data-action="gen-key">${icon("refresh")}生成</button></div><small>留空或保留掩码表示不修改。Key 不会通过 API 明文返回。</small></div>
            <div class="field span-2"><label>描述</label><input class="input" name="description" value="${esc(cfg.description || "")}" placeholder="这台电脑的用途、可用引擎等"><small>会显示在服务器的 Agent 列表和微信路由提示里。</small></div>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card-head"><div><h2 class="card-title">默认执行位置</h2><p class="card-sub">微信里不指定引擎/工作区时使用这里的默认值。</p></div></div>
        <div class="card-body">
          <div class="form-grid">
            <div class="field"><label>默认引擎</label><select class="select" name="default_engine">${selectOptions(engines, cfg.default_engine)}</select></div>
            <div class="field"><label>默认工作区</label><select class="select" name="default_workspace">${selectOptions(workspaces, cfg.default_workspace)}</select></div>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card-head"><div><h2 class="card-title">运行参数</h2><p class="card-sub">一般保持默认即可。</p></div></div>
        <div class="card-body">
          <div class="form-grid cols-3">
            <div class="field"><label>同步等待（秒）</label><input class="input" type="number" name="wait_seconds" min="5" max="600" value="${esc(cfg.wait_seconds ?? 60)}"><small>超过这个时间改为异步通知。</small></div>
            <div class="field"><label>轮询间隔（秒）</label><input class="input" type="number" name="poll_seconds" min="1" max="60" value="${esc(cfg.poll_seconds ?? 2)}"></div>
            <div class="field"><label>任务超时（秒）</label><input class="input" type="number" name="task_timeout" min="60" max="86400" value="${esc(cfg.task_timeout ?? 1800)}"><small>超时会终止本地 CLI 进程树。</small></div>
          </div>
        </div>
      </section>

      <div class="form-actions">
        <button class="btn btn-ghost" type="button" data-action="go-workspaces">${icon("folder")}工作区</button>
        <button class="btn btn-ghost" type="button" data-action="go-engines">${icon("terminal")}引擎</button>
        <button class="btn btn-primary" type="submit">${icon("check")}保存并应用</button>
      </div>
    </form>`;
}

/* ------------------------------------------------------------------ engines */
function renderEngines() {
  const cfg = state.config || {};
  const checks = cfg._meta?.engine_checks || {};
  const engines = Object.entries(cfg.engines || {});
  return `
    <div class="section-head">
      <div><div class="section-title">${icon("terminal")}本地引擎</div><div class="section-note">命令会把微信任务文本作为最后一个参数追加。</div></div>
    </div>
    ${engines.length ? `<div class="engine-grid">${engines.map(([name, command]) => {
      const check = checks[name] || {};
      const isDefault = cfg.default_engine === name;
      return `<section class="card engine-card">
        <div class="engine-head">
          <div class="engine-icon">${icon("terminal")}</div>
          <div style="min-width:0"><div class="engine-name">${esc(name)} ${isDefault ? badge("默认", "brand") : ""}</div><div class="engine-command">${esc((command || []).join(" "))}</div></div>
        </div>
        <div class="engine-meta">${check.found ? badge("已找到", "ok") : badge("未找到", "danger")}<code>${esc(check.executable || command?.[0] || "—")}</code></div>
        <div class="engine-foot">
          <button class="btn btn-sm btn-secondary" data-action="test-engine" data-engine="${esc(name)}">${icon("play")}试跑</button>
          <button class="btn btn-sm" data-action="edit-engine" data-engine="${esc(name)}">${icon("edit")}编辑</button>
          <button class="btn btn-sm btn-ghost" data-action="delete-engine" data-engine="${esc(name)}">${icon("trash")}删除</button>
        </div>
      </section>`;
    }).join("")}</div>` : emptyState("terminal", "还没有引擎", "添加 OpenCode、Codex 或 Claude Code 的启动命令。")}
    <div style="margin-top:16px">${callout("info", "关于试跑", "试跑会真实调用一次本地 CLI，并让它只回复 OK。请确认工作区允许读取，避免在生产目录做实验。")}</div>`;
}

/* ------------------------------------------------------------------ workspaces */
function renderWorkspaces() {
  const cfg = state.config || {};
  const checks = cfg._meta?.workspace_checks || {};
  const workspaces = Object.entries(cfg.workspaces || {});
  return `
    <div class="section-head">
      <div><div class="section-title">${icon("folder")}工作区别名</div><div class="section-note">服务器只认识别名，真实路径只保存在这台电脑。</div></div>
    </div>
    ${workspaces.length ? `<div class="table-wrap"><table>
      <thead><tr><th>别名</th><th>真实路径</th><th>状态</th><th class="right">操作</th></tr></thead>
      <tbody>${workspaces.map(([alias, path]) => {
        const check = checks[alias] || {};
        return `<tr>
          <td><span class="cell-title mono">${esc(alias)}</span> ${cfg.default_workspace === alias ? badge("默认", "brand") : ""}</td>
          <td><span class="mono">${esc(path)}</span></td>
          <td>${check.exists ? badge("存在", "ok") : badge("不存在", "danger")}</td>
          <td><div class="row-actions">
            <button class="btn btn-sm btn-ghost" data-action="open-workspace" data-path="${esc(path)}">${icon("external")}打开</button>
            <button class="btn btn-sm" data-action="edit-workspace" data-alias="${esc(alias)}">${icon("edit")}编辑</button>
            <button class="btn btn-sm btn-ghost" data-action="delete-workspace" data-alias="${esc(alias)}">${icon("trash")}删除</button>
          </div></td>
        </tr>`;
      }).join("")}</tbody>
    </table></div>` : emptyState("folder", "还没有工作区", "添加一个绝对路径，本地 CLI 只允许在这里执行。")}
    <div style="margin-top:16px">${callout("warn", "路径白名单", "任务里传入绝对路径时，只有等于已配置工作区路径才会执行；其他路径会被拒绝。")}</div>`;
}

/* ------------------------------------------------------------------ server: connect */
function renderServerConnect() {
  const url = state.config?.router || state.bootstrap?.server?.url || "";
  return `<div class="server-connect"><section class="card"><div class="card-body">
    <div class="connect-illustration">${icon("server")}</div>
    <h2 class="connect-title">连接你的 Router</h2>
    <p class="connect-sub">连接后可以在这里管理 Agent、插件、定时任务和脱敏配置。服务令牌只在首次连接时提交。</p>
    <form id="server-connect-form">
      <div class="form-grid">
        <div class="field span-2"><label>服务器地址</label><input class="input mono" name="url" value="${esc(url)}" placeholder="https://router.example.com"></div>
        <div class="field span-2"><label>服务令牌</label><div class="input-group"><input class="input mono" id="server-token" type="password" name="token" placeholder="config.json 里的 service_token" autocomplete="off"><button class="btn" type="button" data-action="toggle-token">${icon("eye")}显示</button></div><small>令牌会用 Windows DPAPI 加密后保存在本机，API 不会回显。</small></div>
      </div>
      <div class="form-actions"><button class="btn btn-primary" type="submit">${icon("wifi")}连接并读取配置</button></div>
    </form>
    <div class="token-help" style="margin-top:14px">
      在服务器仓库目录读取令牌：
      <code>python -c "import json;print(json.load(open('config.json'))['service_token'])"</code>
      <div class="tiny muted" style="margin-top:6px">连接会同时把本机 Router 地址切换为上面的地址。</div>
    </div>
  </div></section></div>`;
}

/* ------------------------------------------------------------------ server: connected */
function serverStats(s) {
  const agents = s.agents || [];
  const plugins = s.plugins || [];
  const errors = Object.keys(s.plugin_errors || {}).length;
  return `<div class="stat-strip">
    <div class="stat"><div class="stat-label">${icon("cpu")}在线 Agent</div><div class="stat-value">${agents.filter((a) => a.online).length}<span class="muted" style="font-size:12px"> / ${agents.length}</span></div><div class="stat-note">${esc(s._server_url || "")}</div></div>
    <div class="stat"><div class="stat-label">${icon("package")}插件</div><div class="stat-value">${plugins.filter((p) => p.enabled).length}<span class="muted" style="font-size:12px"> / ${plugins.length}</span></div><div class="stat-note">${plugins.filter((p) => p.loaded).length} 个已加载</div></div>
    <div class="stat"><div class="stat-label">${icon("clock")}定时任务</div><div class="stat-value">${(s.jobs || []).length}</div><div class="stat-note">${(s.jobs || []).filter((j) => j.enabled).length} 个启用</div></div>
    <div class="stat"><div class="stat-label">${icon("alert")}错误</div><div class="stat-value">${errors}</div><div class="stat-note">${errors ? "查看插件页" : "运行正常"}</div></div>
  </div>`;
}

function renderServerOverviewTab(s) {
  const balance = state.balance;
  const balanceHtml = balance
    ? `<div class="callout callout-ok">${icon("database")}<div><strong>API 余额</strong><p>${balance.is_available ? "可用" : "不可用"} · ${(balance.balances || []).map((b) => `${esc(b.currency)} ${esc(b.total)}`).join(" / ") || "无数据"}</p></div></div>`
    : `<button class="btn btn-sm btn-secondary" data-action="balance">${icon("database")}查询 API 余额</button>`;
  const logs = (s.logs || []).slice(-10);
  const events = (s.events || []).slice(0, 8);
  return `<div class="grid-2">
    <section class="card"><div class="card-head"><div><h2 class="card-title">服务器信息</h2><p class="card-sub">Router 运行时</p></div></div><div class="card-body">
      <dl class="kv">
        <dt>地址</dt><dd class="mono">${esc(s._server_url || "—")}</dd>
        <dt>版本</dt><dd>${esc(s.version || "—")}</dd>
        <dt>时间</dt><dd>${esc(fmtTime(s.time, true))}</dd>
        <dt>工具</dt><dd>${esc((s.tools || []).length)} 个</dd>
        <dt>服务</dt><dd class="chips">${(s.services || []).map((x) => `<span class="chip">${esc(x)}</span>`).join("") || "—"}</dd>
        <dt>凭据</dt><dd class="chips">${(s.credentials || []).map((x) => `<span class="chip">${esc(x)}</span>`).join("") || "—"}</dd>
      </dl>
      <div class="divider"></div>
      ${balanceHtml}
    </div></section>
    <section class="card"><div class="card-head"><div><h2 class="card-title">插件事件</h2><p class="card-sub">最近 8 条</p></div><button class="btn btn-sm btn-ghost" data-action="server-refresh">${icon("refresh")}刷新</button></div><div class="card-body" style="padding-top:8px">
      ${events.length ? `<div class="table-wrap"><table style="min-width:0"><thead><tr><th>插件</th><th>动作</th><th>详情</th><th>时间</th></tr></thead><tbody>${events.map((e) => `<tr><td>${esc(e.plugin_id || e.plugin || "—")}</td><td>${badge(e.action || "—", "muted")}</td><td class="muted">${esc(short(e.detail || "", 34))}</td><td class="muted">${esc(fmtTime(e.created_at || e.ts))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("activity", "暂无事件", "插件加载、重载和配置修改会记录在这里。")}
    </div></section>
  </div>
  <section class="card" style="margin-top:16px"><div class="card-head"><div><h2 class="card-title">服务器日志</h2><p class="card-sub">最近 10 条，只通过服务令牌读取。</p></div></div><div class="card-body"><div class="log-list">${logs.length ? logs.map(logRowHtml).join("") : `<div class="log-empty">暂无日志</div>`}</div></div></section>`;
}

function renderServerAgentsTab(s) {
  const agents = s.agents || [];
  return `<div class="section-head"><div><div class="section-title">${icon("cpu")}已注册 Agent</div><div class="section-note">每台电脑一个唯一 ID 和 Key；在线状态来自出站轮询。</div></div><div class="card-actions"><button class="btn btn-sm btn-secondary" data-action="register-self">${icon("plus")}注册本机</button><button class="btn btn-sm" data-action="add-agent">${icon("plus")}手动添加</button></div></div>
  ${agents.length ? `<div class="table-wrap"><table><thead><tr><th>Agent</th><th>传输</th><th>在线</th><th>Key</th><th>运行时</th><th class="right">操作</th></tr></thead><tbody>${agents.map((a) => `<tr>
    <td><div class="cell-title">${esc(a.name || a.id)}</div><div class="cell-sub mono">${esc(a.id)}</div></td>
    <td>${a.transport === "poll" ? badge("轮询", "brand") : badge("HTTP", "muted")}</td>
    <td><span class="status-dot ${a.online ? "online" : "stopped"}"></span> <span class="tiny">${a.online ? "在线" : "离线"}</span>${a.last_seen ? `<div class="cell-sub">${esc(fmtTime(a.last_seen))}</div>` : ""}</td>
    <td><span class="mono tiny">${esc(a.api_key || "—")}</span></td>
    <td>${(a.runtime?.engines || []).length ? `<div class="chips">${a.runtime.engines.slice(0, 4).map((x) => `<span class="chip">${esc(x)}</span>`).join("")}${a.runtime.engines.length > 4 ? `<span class="chip">+${a.runtime.engines.length - 4}</span>` : ""}</div>` : `<span class="muted tiny">—</span>`}</td>
    <td><div class="row-actions"><button class="btn btn-sm" data-action="edit-agent" data-agent="${esc(a.id)}">${icon("edit")}编辑</button><button class="btn btn-sm btn-ghost" data-action="delete-agent" data-agent="${esc(a.id)}" ${a.id === "echo" ? "disabled" : ""}>${icon("trash")}删除</button></div></td>
  </tr>`).join("")}</tbody></table></div>` : emptyState("cpu", "还没有 Agent", "在每台电脑上运行桌面端，然后点「注册本机」。")}
  <div style="margin-top:16px">${callout("info", "多电脑怎么区分", "微信里说「在办公室电脑上改登录接口」时，LLM 按 Agent 描述选择；每台电脑的 ID 必须唯一。")}</div>`;
}

function renderServerPluginsTab(s) {
  const plugins = s.plugins || [];
  return `<div class="section-head"><div><div class="section-title">${icon("package")}插件</div><div class="section-note">启停状态会写回 config.json，重启后仍然有效。</div></div><button class="btn btn-sm btn-ghost" data-action="server-refresh">${icon("refresh")}刷新</button></div>
  ${plugins.length ? `<div class="plugin-grid">${plugins.map((p) => `<section class="card plugin-card">
    <div class="plugin-head"><div><div class="plugin-name">${esc(p.name || p.id)} ${badge(`v${p.version || "?"}`, "muted")}</div><div class="cell-sub mono">${esc(p.id)}</div></div><button class="switch ${p.enabled ? "on" : ""}" type="button" data-action="plugin-toggle" data-plugin="${esc(p.id)}" data-enabled="${p.enabled ? "true" : "false"}" aria-label="${p.enabled ? "停用" : "启用"}"></button></div>
    <div class="plugin-desc">${esc(p.description || "（无描述）")}</div>
    <div class="chips">${(p.capabilities || []).map((x) => `<span class="chip">${esc(x)}</span>`).join("") || `<span class="chip">tools</span>`}</div>
    ${p.error ? `<div class="callout callout-danger" style="padding:9px 10px">${icon("alert")}<div><strong>加载失败</strong><p>${esc(p.error)}</p></div></div>` : ""}
    <div class="plugin-meta"><span>${(p.tools || []).length} 工具 · ${(p.jobs || []).length} 任务</span><div class="card-actions">${p.enabled && !p.loaded ? badge("未加载", "warn") : ""}<button class="btn btn-sm" data-action="plugin-reload" data-plugin="${esc(p.id)}" ${p.loaded ? "" : "disabled"}>${icon("refresh")}重载</button></div></div>
  </section>`).join("")}</div>` : emptyState("package", "还没有插件", "插件目录里没有 manifest.json。")}`;
}

function renderServerJobsTab(s) {
  const jobs = s.jobs || [];
  return `<div class="section-head"><div><div class="section-title">${icon("clock")}定时任务</div><div class="section-note">修改间隔会热重载所属插件；停用的任务不能直接改间隔。</div></div><button class="btn btn-sm btn-ghost" data-action="server-refresh">${icon("refresh")}刷新</button></div>
  ${jobs.length ? `<div class="table-wrap"><table><thead><tr><th>任务</th><th>插件</th><th>间隔（分钟）</th><th>窗口（分钟）</th><th>下次运行</th><th>启用</th><th class="right">操作</th></tr></thead><tbody>${jobs.map((j) => `<tr data-job="${esc(j.id)}">
    <td><div class="cell-title mono">${esc(j.id)}</div></td><td>${badge(j.plugin || "—", "muted")}</td>
    <td><input class="input input-compact" type="number" min="1" max="10080" value="${esc(j.interval_minutes ?? "")}" data-job-interval ${j.enabled ? "" : "disabled"}></td>
    <td><input class="input input-compact" type="number" min="1" max="10080" value="${esc(j.window_minutes ?? "")}" data-job-window ${j.enabled ? "" : "disabled"}></td>
    <td class="muted">${j.next_run ? esc(fmtDate(j.next_run)) : "—"}</td>
    <td><button class="switch ${j.enabled ? "on" : ""}" type="button" data-action="job-toggle" data-job="${esc(j.id)}" data-enabled="${j.enabled ? "true" : "false"}" aria-label="启停任务"></button></td>
    <td><div class="row-actions"><button class="btn btn-sm" data-action="job-save" data-job="${esc(j.id)}" ${j.enabled ? "" : "disabled"}>保存</button><button class="btn btn-sm btn-secondary" data-action="job-run" data-job="${esc(j.id)}" ${j.enabled ? "" : "disabled"}>${icon("play")}立即运行</button></div></td>
  </tr>`).join("")}</tbody></table></div>` : emptyState("clock", "没有定时任务", "插件注册的 job 会显示在这里。")}`;
}

function renderServerConfigTab(s) {
  const cfg = s.config || {};
  const llm = cfg.llm || {};
  const hub = cfg.hub || {};
  return `<form data-config-form>
    <div class="grid-2">
      <section class="card"><div class="card-head"><div><h2 class="card-title">模型（LLM）</h2><p class="card-sub">DeepSeek 兼容接口；密钥留空表示不修改。</p></div></div><div class="card-body"><div class="form-grid">
        <div class="field"><label>Base URL</label><input class="input mono" name="llm.base_url" value="${esc(llm.base_url || "")}"></div>
        <div class="field"><label>模型</label><input class="input mono" name="llm.model" value="${esc(llm.model || "")}"></div>
        <div class="field span-2"><label>API Key</label><input class="input mono" name="llm.api_key" value="" placeholder="${esc(llm.api_key || "未配置")}" autocomplete="off"><small>只写不回显；保存后立即应用到新请求。</small></div>
        <div class="field span-2"><label>System Prompt</label><textarea class="textarea mono" name="llm.system_prompt" rows="5">${esc(llm.system_prompt || "")}</textarea></div>
      </div></div></section>
      <section class="card"><div class="card-head"><div><h2 class="card-title">微信通道（Hub）</h2><p class="card-sub">修改后由新的请求使用。</p></div></div><div class="card-body"><div class="form-grid">
        <div class="field span-2"><label>Hub Base URL</label><input class="input mono" name="hub.base_url" value="${esc(hub.base_url || "")}"></div>
        <div class="field span-2"><label>Channel Key</label><input class="input mono" name="hub.channel_key" value="" placeholder="${esc(hub.channel_key || "未配置")}" autocomplete="off"><small>只写不回显。</small></div>
        <div class="field span-2"><label>公开地址</label><input class="input mono" name="public_base_url" value="${esc(cfg.public_base_url || "")}"><small>用于生成登录链接，必须是外网可访问地址。</small></div>
        <div class="field"><label>轮询间隔（秒）</label><input class="input" type="number" min="1" max="60" name="poll_interval" value="${esc(cfg.poll_interval ?? 2)}"></div>
        <div class="field"><label>Agent 等待（秒）</label><input class="input" type="number" min="5" max="600" name="agent_wait_seconds" value="${esc(cfg.agent_wait_seconds ?? 60)}"></div>
      </div></div></section>
    </div>
    <div class="form-actions"><button class="btn btn-ghost" type="button" data-action="balance">${icon("database")}查询余额</button><button class="btn btn-primary" type="submit">${icon("check")}保存服务器配置</button></div>
  </form>`;
}

function renderServer() {
  if (!state.bootstrap?.server?.connected) return renderServerConnect();
  const s = state.server;
  if (!s) {
    if (state.serverError) {
      return `<div class="card"><div class="card-body">${callout("danger", "读取服务器失败", state.serverError, `<button class="btn btn-sm btn-secondary" data-action="server-refresh">${icon("refresh")}重试</button>`)}</div></div>`;
    }
    return `<div class="card"><div class="card-body">${emptyState("server", "正在读取服务器", "首次加载会读取配置、插件和任务列表。")}</div></div>`;
  }
  const tab = state.serverTab;
  const tabs = [["overview", "概览"], ["agents", "Agents"], ["plugins", "插件"], ["jobs", "任务"], ["config", "配置"]];
  let content = "";
  if (tab === "agents") content = renderServerAgentsTab(s);
  else if (tab === "plugins") content = renderServerPluginsTab(s);
  else if (tab === "jobs") content = renderServerJobsTab(s);
  else if (tab === "config") content = renderServerConfigTab(s);
  else content = renderServerOverviewTab(s);
  return `${serverStats(s)}
    <div class="tabs">${tabs.map(([key, label]) => `<button class="tab ${tab === key ? "active" : ""}" type="button" data-action="server-tab" data-tab="${key}">${esc(label)}</button>`).join("")}</div>
    ${content}`;
}

/* ------------------------------------------------------------------ logs page */
function renderLogs() {
  return `<section class="card"><div class="card-head"><div><h2 class="card-title">运行日志</h2><p class="card-sub">本机 Agent 与桌面控制台，最多保留 1000 条。</p></div><span class="badge badge-muted" id="log-count">${filteredLogs().length} 条</span></div><div class="card-body">
    <div class="log-toolbar">
      <select class="select" data-log-source><option value="all" ${state.logFilter === "all" ? "selected" : ""}>全部来源</option><option value="agent" ${state.logFilter === "agent" ? "selected" : ""}>本机 Agent</option><option value="desktop" ${state.logFilter === "desktop" ? "selected" : ""}>桌面控制台</option></select>
      <select class="select" data-log-level><option value="all" ${state.logLevel === "all" ? "selected" : ""}>全部级别</option><option value="INFO" ${state.logLevel === "INFO" ? "selected" : ""}>INFO</option><option value="WARN" ${state.logLevel === "WARN" ? "selected" : ""}>WARN</option><option value="ERROR" ${state.logLevel === "ERROR" ? "selected" : ""}>ERROR</option></select>
      <input class="input grow" data-log-search value="${esc(state.logSearch)}" placeholder="搜索日志…">
      <button class="btn btn-sm ${state.autoScroll ? "btn-secondary" : ""}" type="button" data-action="toggle-log-scroll" data-enabled="${state.autoScroll}">${icon("activity")}自动滚动</button>
      <button class="btn btn-sm" type="button" data-action="clear-logs">${icon("trash")}清空</button>
    </div>
    <div class="log-list" id="log-rows">${filteredLogs().slice(-600).map(logRowHtml).join("") || `<div class="log-empty">没有匹配的日志</div>`}</div>
  </div></section>`;
}

/* ------------------------------------------------------------------ settings */
function renderSettings() {
  const boot = state.bootstrap || {};
  const app = boot.app || {};
  const paths = boot.paths || {};
  const auto = Boolean(boot.autostart);
  const modeLabel = { auto: "自动（优先 WebView2）", webview: "WebView2 窗口", edge: "Edge 应用窗口", browser: "默认浏览器" }[boot.app?.window_mode] || boot.app?.window_mode || "自动";
  return `<div class="grid-2">
    <section class="card"><div class="card-head"><div><h2 class="card-title">启动与后台</h2><p class="card-sub">开机自启会用无窗口模式运行，再次双击会打开现有控制台。</p></div></div><div class="card-body">
      <div class="switch-row"><div class="switch-copy"><strong>开机自动启动</strong><small>在当前 Windows 用户下创建 Startup 快捷方式。</small></div><button class="switch ${auto ? "on" : ""}" type="button" data-action="toggle-autostart" data-enabled="${auto}" aria-label="开机自启"></button></div>
      <div class="switch-row"><div class="switch-copy"><strong>窗口承载</strong><small>本次启动使用：${esc(modeLabel)}</small></div><button class="btn btn-sm" data-action="open-path" data-kind="data">${icon("folder")}数据目录</button></div>
      <div class="switch-row"><div class="switch-copy"><strong>配置目录</strong><small class="mono">${esc(paths.config || "")}</small></div><button class="btn btn-sm" data-action="open-path" data-kind="config">${icon("folder")}打开</button></div>
      <div class="switch-row"><div class="switch-copy"><strong>日志文件</strong><small class="mono">${esc(paths.log || "")}</small></div><button class="btn btn-sm" data-action="open-path" data-kind="log">${icon("external")}打开</button></div>
    </div></section>

    <section class="card"><div class="card-head"><div><h2 class="card-title">安全模型</h2><p class="card-sub">本地接口只对本机开放。</p></div></div><div class="card-body">
      <div class="callout callout-ok">${icon("shield")}<div><strong>本地回环 + 每次启动随机令牌</strong><p>控制台只监听 127.0.0.1，API 需要 X-Desktop-Token；服务令牌用 Windows DPAPI 加密，不写入 git。</p></div></div>
      <div class="divider"></div>
      <dl class="kv"><dt>Agent Key</dt><dd>保存在 config.json，本地文件权限 600，git 已忽略。</dd><dt>服务令牌</dt><dd>DPAPI 加密后保存在 data/secrets.json。</dd><dt>远程管理</dt><dd>所有服务器接口都要求 X-Service-Token。</dd></dl>
    </div></section>
  </div>

  <section class="card" style="margin-top:16px"><div class="card-head"><div><h2 class="card-title">关于</h2><p class="card-sub">微信 Agent 桌面控制台</p></div></div><div class="card-body">
    <dl class="kv">
      <dt>版本</dt><dd>${esc(app.version || "1.0.0")} · 本机 Agent ${esc(state.agent?.version || "")}</dd>
      <dt>本地接口</dt><dd class="mono">http://127.0.0.1:${esc(location.port || "")}/</dd>
      <dt>数据目录</dt><dd class="mono">${esc(paths.data || "")}</dd>
      <dt>运行模式</dt><dd>${boot.app?.headless ? "后台服务（无窗口）" : "桌面窗口"}</dd>
    </dl>
    <div class="form-actions"><button class="btn btn-danger" type="button" data-action="quit">${icon("power")}退出整个程序</button></div>
  </div></section>`;
}

/* ------------------------------------------------------------------ page registry */
const PAGES = {
  overview: {
    title: "概览", eyebrow: "CONTROL ROOM",
    subtitle: "本机 Agent 与服务器状态一眼看清。",
    render: renderOverview,
    actions: () => `${state.agent?.running
      ? `<button class="btn btn-sm btn-secondary" data-action="agent-stop">${icon("stop")}停止</button>`
      : `<button class="btn btn-sm btn-primary" data-action="agent-start">${icon("play")}启动</button>`}<button class="btn btn-sm" data-action="refresh">${icon("refresh")}刷新</button>`,
    after: () => {
      updateRecentLogs();
      if (state.bootstrap?.server?.connected && !state.server && !state.serverTried) loadServer(true);
    },
  },
  local: {
    title: "本机 Agent", eyebrow: "LOCAL RUNTIME",
    subtitle: "身份、引擎、工作区和运行参数都在这里改。",
    render: renderLocal,
    actions: () => `<button class="btn btn-sm" type="submit" form="local-form">${icon("check")}保存并应用</button><button class="btn btn-sm btn-ghost" data-action="refresh">${icon("refresh")}刷新状态</button>`,
  },
  engines: {
    title: "引擎", eyebrow: "TOOLCHAIN",
    subtitle: "OpenCode、Codex、Claude Code，以及任何命令行程序。",
    render: renderEngines,
    actions: () => `<button class="btn btn-sm btn-primary" data-action="add-engine">${icon("plus")}添加引擎</button>`,
  },
  workspaces: {
    title: "工作区", eyebrow: "WORKSPACES",
    subtitle: "本地 CLI 只允许在已配置的目录里执行。",
    render: renderWorkspaces,
    actions: () => `<button class="btn btn-sm btn-primary" data-action="add-workspace">${icon("plus")}添加工作区</button>`,
  },
  server: {
    title: "服务器", eyebrow: "ROUTER",
    subtitle: "管理 Agent、插件、定时任务和脱敏配置。",
    render: renderServer,
    actions: () => state.bootstrap?.server?.connected
      ? `<button class="btn btn-sm" data-action="server-refresh">${icon("refresh")}刷新</button><button class="btn btn-sm btn-ghost" data-action="server-disconnect">${icon("x")}断开</button>`
      : "",
    after: () => {
      if (state.bootstrap?.server?.connected && !state.server && !state.serverTried) loadServer(true);
    },
  },
  logs: {
    title: "日志", eyebrow: "OBSERVABILITY",
    subtitle: "本地执行、轮询和配置操作的完整轨迹。",
    render: renderLogs,
    actions: () => `<button class="btn btn-sm" data-action="refresh-logs">${icon("refresh")}刷新</button><button class="btn btn-sm btn-ghost" data-action="clear-logs">${icon("trash")}清空</button>`,
    after: () => updateLogRows(),
  },
  settings: {
    title: "设置", eyebrow: "PREFERENCES",
    subtitle: "开机自启、文件位置和安全边界。",
    render: renderSettings,
    actions: () => "",
  },
};

function renderPage() {
  const meta = PAGES[state.page] || PAGES.overview;
  $("page-title").textContent = meta.title;
  $("page-eyebrow").textContent = meta.eyebrow;
  $("page-subtitle").textContent = meta.subtitle;
  $("top-actions").innerHTML = meta.actions ? meta.actions() : "";
  $("page").innerHTML = meta.render();
  document.querySelectorAll(".nav-item").forEach((link) => {
    link.classList.toggle("active", link.dataset.page === state.page);
  });
  if (meta.after) meta.after();
  updateSidebar();
}

function navigate(page) {
  state.page = PAGES[page] ? page : "overview";
  if (location.hash !== `#/${state.page}`) {
    history.replaceState(null, "", `#/${state.page}`);
  }
  renderPage();
}

/* ------------------------------------------------------------------ server load */
async function loadServer(render = true) {
  if (!state.bootstrap?.server?.connected) return;
  if (state.serverLoading) return;
  state.serverLoading = true;
  state.serverTried = true;
  try {
    const data = await api("/api/server/overview");
    state.server = data;
    state.serverError = "";
    if (render) renderPage();
    else if (state.page === "overview") renderPage();
  } catch (error) {
    state.serverError = error.message;
    if (render || state.page === "server") renderPage();
    else toast(error.message, "error");
  } finally {
    state.serverLoading = false;
  }
}

/* ------------------------------------------------------------------ actions */
async function saveLocalPatch(patch, message = "本机配置已保存") {
  const config = await api("/api/config", { method: "PUT", body: patch });
  state.config = config;
  if (state.bootstrap) state.bootstrap.config = config;
  toast(message);
  renderPage();
  setTimeout(refreshStatus, 700);
}

async function refreshStatus() {
  if (document.hidden) return;
  try {
    const data = await api("/api/status");
    state.agent = data.agent;
    state.router = data.router;
    if (state.bootstrap?.server) state.bootstrap.server.connected = data.server_connected;
    updateSidebar();
    if (state.page === "overview") updateOverviewLive();
    if (state.page === "local") updateLocalLive();
  } catch {
    /* transient; next tick retries */
  }
}

async function agentAction(action) {
  if ((action === "stop" || action === "restart") && state.agent?.current) {
    const ok = await confirmAction("中断当前任务？", "本机 CLI 正在执行任务，停止 Agent 会终止进程树。", "停止并继续");
    if (!ok) return;
  }
  const data = await api(`/api/agent/${action}`, { method: "POST" });
  if (data && data.status) state.agent = data;
  toast({ start: "Agent 已启动", stop: "Agent 已停止", restart: "Agent 已重启" }[action] || "已完成");
  renderPage();
  setTimeout(refreshStatus, 900);
}

async function testRouter() {
  toast("正在测试 Router…", "warn");
  const result = await api("/api/router/test");
  if (!result.ok) {
    openModal({
      title: "Router 连接失败", eyebrow: "DIAGNOSTICS", saveText: null,
      body: callout("danger", "无法连接", result.error || "未知错误") +
        `<div class="kv" style="margin-top:14px"><dt>地址</dt><dd class="mono">${esc(result.url || "")}</dd><dt>耗时</dt><dd>${esc(result.latency_ms || 0)} ms</dd></div>`,
    });
    return;
  }
  const data = result.data || {};
  openModal({
    title: "Router 连接正常", eyebrow: "DIAGNOSTICS", saveText: null,
    body: `<div class="callout callout-ok">${icon("check")}<div><strong>${esc(result.latency_ms || 0)} ms</strong><p>${esc(result.url || "")}</p></div></div>
      <div class="kv" style="margin-top:16px">
        <dt>Agent</dt><dd>${esc((data.agents || []).join(", ") || "—")}</dd>
        <dt>插件</dt><dd>${esc((data.plugins || []).join(", ") || "—")}</dd>
        <dt>工具</dt><dd>${esc((data.tools || []).length)} 个</dd>
        <dt>错误</dt><dd>${Object.keys(data.plugin_errors || {}).length ? esc(JSON.stringify(data.plugin_errors)) : "无"}</dd>
      </div>`,
  });
}

function showTestResult(result) {
  openModal({
    title: result.ok ? "引擎试跑成功" : "引擎试跑失败", eyebrow: "ENGINE TEST", wide: true, saveText: null,
    body: `<div class="kv">
        <dt>引擎</dt><dd class="mono">${esc(result.engine)}</dd>
        <dt>工作区</dt><dd class="mono">${esc(result.workspace)}</dd>
        <dt>退出码</dt><dd>${esc(result.exit_code)}</dd>
        <dt>耗时</dt><dd>${esc(result.seconds)} 秒</dd>
      </div><div class="divider"></div><div class="code-block">${esc(result.output || "（无输出）")}</div>`,
  });
}

function showKeyModal(key, agentId) {
  openModal({
    title: "Agent Key 已生成", eyebrow: "SECRET", saveText: null,
    body: `${callout("warn", "只显示一次", "复制并保存到这台电脑的 local-agent/config.json。关闭后服务器不会再次返回明文。")}
      <div class="copy-row" style="margin-top:14px"><code>${esc(key)}</code><button class="btn btn-sm" data-action="copy" data-copy="${esc(key)}">${icon("copy")}复制</button></div>
      <div class="tiny muted" style="margin-top:8px">Agent ID：<span class="mono">${esc(agentId)}</span></div>`,
  });
}

function engineModal(name, command) {
  const isEdit = Boolean(name);
  openModal({
    title: isEdit ? `编辑引擎 ${name}` : "添加引擎",
    body: `<form id="modal-form"><div class="form-grid">
      <div class="field span-2"><label>引擎名称</label><input class="input mono" name="engine_name" value="${esc(name || "")}" ${isEdit ? "readonly" : ""} placeholder="opencode"><small>微信里用这个名字指定引擎；不能再改名，删掉后重新添加即可。</small></div>
      <div class="field span-2"><label>启动命令</label><input class="input mono" name="engine_command" value="${esc((command || []).join(" "))}" placeholder="opencode run"><small>任务文本会作为最后一个参数追加。支持引号。</small></div>
      <div class="span-2">${callout("info", "示例", "opencode run · codex exec · claude -p")}</div>
    </div></form>`,
    onSubmit: async (form) => {
      const engineName = String(form.get("engine_name") || "").trim();
      const parts = splitCommand(String(form.get("engine_command") || ""));
      if (!engineName) throw new Error("请填写引擎名称");
      if (!parts.length) throw new Error("请填写启动命令");
      const engines = JSON.parse(JSON.stringify(state.config.engines || {}));
      engines[engineName] = parts;
      const patch = { engines };
      if (!state.config.default_engine || !engines[state.config.default_engine]) patch.default_engine = engineName;
      await saveLocalPatch(patch, "引擎已保存");
    },
  });
}

function workspaceModal(alias, path) {
  const isEdit = Boolean(alias);
  openModal({
    title: isEdit ? `编辑工作区 ${alias}` : "添加工作区",
    body: `<form id="modal-form"><div class="form-grid">
      <div class="field"><label>别名</label><input class="input mono" name="workspace_alias" value="${esc(alias || "")}" ${isEdit ? "readonly" : ""} placeholder="project"></div>
      <div class="field span-2"><label>真实路径</label><div class="input-group"><input class="input mono" id="workspace-path" name="workspace_path" value="${esc(path || "")}" placeholder="D:\\work\\project"><button class="btn" type="button" data-action="browse-folder" data-target="workspace-path">${icon("folder")}浏览</button></div><small>必须是绝对路径；可以现在不存在，但任务执行时会被拒绝。</small></div>
      <div class="span-2">${callout("warn", "路径白名单", "只有这里列出的目录才允许本地 CLI 执行，服务器无法传入任意路径。")}</div>
    </div></form>`,
    onSubmit: async (form) => {
      const workspaceAlias = String(form.get("workspace_alias") || "").trim();
      const workspacePath = String(form.get("workspace_path") || "").trim();
      if (!workspaceAlias || !workspacePath) throw new Error("请填写别名和路径");
      const workspaces = { ...(state.config.workspaces || {}) };
      workspaces[workspaceAlias] = workspacePath;
      const patch = { workspaces };
      if (!state.config.default_workspace || !workspaces[state.config.default_workspace]) patch.default_workspace = workspaceAlias;
      await saveLocalPatch(patch, "工作区已保存");
    },
  });
}

function serverAgentModal(agent) {
  const isEdit = Boolean(agent);
  const item = agent || {};
  openModal({
    title: isEdit ? `编辑 Agent ${item.id}` : "添加 Agent",
    wide: true,
    body: `<form id="modal-form"><div class="form-grid">
      <div class="field"><label>Agent ID</label><input class="input mono" name="agent_id" value="${esc(item.id || "")}" ${isEdit ? "readonly" : ""} placeholder="pc-office"></div>
      <div class="field"><label>显示名称</label><input class="input" name="agent_name" value="${esc(item.name || "")}" placeholder="办公室电脑"></div>
      <div class="field span-2"><label>描述</label><input class="input" name="agent_description" value="${esc(item.description || "")}" placeholder="办公室 Windows 电脑，可用 codex，默认工作区 D:\\work\\project"><small>微信路由会参考这段描述选择电脑。</small></div>
      <div class="field"><label>传输方式</label><select class="select" name="transport"><option value="poll" ${item.transport !== "http" ? "selected" : ""}>poll（出站轮询，推荐）</option><option value="http" ${item.transport === "http" ? "selected" : ""}>http（回调地址）</option></select></div>
      <div class="field"><label>API Key</label><input class="input mono" name="api_key" value="" placeholder="${esc(item.api_key || "留空自动生成")}" autocomplete="off"><small>编辑时留空表示不修改。</small></div>
      <div class="field span-2"><label>回调地址（仅 http）</label><input class="input mono" name="callback_url" value="${esc(item.callback_url || "")}" placeholder="http://192.168.1.10:9800/notify"></div>
      <div class="field span-2"><label>回调签名密钥（仅 http）</label><input class="input mono" name="callback_secret" value="" placeholder="${esc(item.callback_secret || "留空表示不修改")}" autocomplete="off"></div>
      <div class="span-2">${callout("info", "轮询模式不需要开放端口", "本地 Agent 会主动连服务器拉任务，适合 NAT 后面的家用电脑。")}</div>
    </div></form>`,
    onSubmit: async (form) => {
      const payload = {
        id: String(form.get("agent_id") || "").trim(),
        name: String(form.get("agent_name") || "").trim(),
        description: String(form.get("agent_description") || "").trim(),
        transport: String(form.get("transport") || "poll"),
      };
      const key = String(form.get("api_key") || "").trim();
      if (key && !key.includes("...")) payload.api_key = key;
      if (payload.transport === "http") {
        payload.callback_url = String(form.get("callback_url") || "").trim();
        const secret = String(form.get("callback_secret") || "").trim();
        if (secret && !secret.includes("...")) payload.callback_secret = secret;
      }
      const result = await api("/api/server/agents", { method: "POST", body: payload });
      state.server = null;
      state.serverTried = false;
      await loadServer(false);
      renderPage();
      toast("Agent 已保存");
      if (result.api_key_once) setTimeout(() => showKeyModal(result.api_key_once, payload.id), 220);
    },
  });
}

/* ------------------------------------------------------------------ submit handlers */
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form.id === "modal-form") {
    event.preventDefault();
    if (!state.modal || !state.modal.onSubmit) return;
    const button = $("modal-save");
    if (button) button.disabled = true;
    try {
      const result = await state.modal.onSubmit(new FormData(form));
      if (result !== false) hideModal();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (button) button.disabled = false;
    }
    return;
  }
  if (form.id === "local-form") {
    event.preventDefault();
    const data = new FormData(form);
    const payload = {
      router: String(data.get("router") || "").trim(),
      agent_id: String(data.get("agent_id") || "").trim(),
      name: String(data.get("name") || "").trim(),
      description: String(data.get("description") || "").trim(),
      agent_key: String(data.get("agent_key") || ""),
      default_engine: String(data.get("default_engine") || ""),
      default_workspace: String(data.get("default_workspace") || ""),
      wait_seconds: Number(data.get("wait_seconds")),
      poll_seconds: Number(data.get("poll_seconds")),
      task_timeout: Number(data.get("task_timeout")),
    };
    try {
      await saveLocalPatch(payload, "本机配置已保存并应用");
    } catch (error) {
      toast(error.message, "error");
    }
    return;
  }
  if (form.id === "server-connect-form") {
    event.preventDefault();
    const data = new FormData(form);
    try {
      const result = await api("/api/server/connect", {
        method: "POST",
        body: {
          url: String(data.get("url") || "").trim(),
          token: String(data.get("token") || "").trim(),
        },
      });
      state.server = result.overview;
      state.serverError = "";
      state.serverTried = true;
      if (state.bootstrap?.server) {
        state.bootstrap.server.connected = true;
        state.bootstrap.server.url = result.server.url;
      }
      if (result.config) {
        state.config = result.config;
        if (state.bootstrap) state.bootstrap.config = result.config;
      }
      if (result.agent) state.agent = result.agent;
      toast("已连接服务器");
      renderPage();
      refreshStatus();
    } catch (error) {
      toast(error.message, "error");
    }
    return;
  }
  if (form.matches("[data-config-form]")) {
    event.preventDefault();
    const data = new FormData(form);
    const updates = [];
    for (const [key, value] of data.entries()) {
      const text = String(value).trim();
      if (!text) continue;
      if (text.includes("...")) continue;
      updates.push([key, text]);
    }
    if (!updates.length) {
      toast("没有需要保存的修改", "warn");
      return;
    }
    try {
      for (const [key, value] of updates) {
        const parsed = ["poll_interval", "agent_wait_seconds"].includes(key) ? Number(value) : value;
        await api("/api/server/config", { method: "PATCH", body: { key, value: parsed } });
      }
      toast("服务器配置已保存");
      state.server = null;
      state.serverTried = false;
      await loadServer(true);
    } catch (error) {
      toast(error.message, "error");
    }
  }
});

/* ------------------------------------------------------------------ input / change */
document.addEventListener("input", (event) => {
  const target = event.target;
  if (target.matches("[data-log-search]")) {
    state.logSearch = target.value;
    updateLogRows();
  }
  if (target.id === "agent-key-input") {
    target.dataset.masked = "0";
  }
});

document.addEventListener("change", (event) => {
  const target = event.target;
  if (target.matches("[data-log-source]")) {
    state.logFilter = target.value;
    updateLogRows();
  }
  if (target.matches("[data-log-level]")) {
    state.logLevel = target.value;
    updateLogRows();
  }
});

/* ------------------------------------------------------------------ click actions */
document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  try {
    switch (action) {
      case "modal-close":
        closeModal();
        break;
      case "modal-confirm":
        if (state.modal && state.modal.onSubmit) {
          const result = await state.modal.onSubmit();
          if (result !== false) hideModal();
        }
        break;
      case "refresh":
        renderPage();
        refreshStatus();
        break;
      case "go-local": navigate("local"); break;
      case "go-engines": navigate("engines"); break;
      case "go-workspaces": navigate("workspaces"); break;
      case "go-server": navigate("server"); break;
      case "go-logs": navigate("logs"); break;
      case "agent-start": await agentAction("start"); break;
      case "agent-stop": await agentAction("stop"); break;
      case "agent-restart": await agentAction("restart"); break;
      case "agent-cancel": {
        const result = await api("/api/agent/cancel", { method: "POST" });
        toast(result.cancelled ? "已发送取消信号" : "当前没有运行中的任务", result.cancelled ? "ok" : "warn");
        setTimeout(refreshStatus, 700);
        break;
      }
      case "test-router": await testRouter(); break;
      case "toggle-key": {
        const input = $("agent-key-input");
        if (!input) break;
        const show = input.type === "password";
        input.type = show ? "text" : "password";
        target.innerHTML = `${icon(show ? "eye-off" : "eye")}${show ? "隐藏" : "显示"}`;
        break;
      }
      case "toggle-token": {
        const input = $("server-token");
        if (!input) break;
        const show = input.type === "password";
        input.type = show ? "text" : "password";
        target.innerHTML = `${icon(show ? "eye-off" : "eye")}${show ? "隐藏" : "显示"}`;
        break;
      }
      case "gen-key": {
        const bytes = new Uint8Array(20);
        crypto.getRandomValues(bytes);
        const key = "pc_" + Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
        const input = $("agent-key-input");
        if (input) {
          input.value = key;
          input.type = "text";
          input.dataset.masked = "0";
        }
        toast("已生成新 Key，保存后生效", "warn");
        break;
      }
      case "test-engine": {
        const engine = target.dataset.engine;
        toast(`正在试跑 ${engine}，可能需要几十秒…`, "warn");
        const result = await api("/api/agent/test-engine", { method: "POST", body: { engine } });
        showTestResult(result);
        break;
      }
      case "add-engine": engineModal("", []); break;
      case "edit-engine": {
        const name = target.dataset.engine;
        engineModal(name, state.config?.engines?.[name] || []);
        break;
      }
      case "delete-engine": {
        const name = target.dataset.engine;
        if (!await confirmAction("删除引擎", `确定删除引擎 ${name}？`, "删除")) break;
        const engines = { ...(state.config.engines || {}) };
        delete engines[name];
        if (!Object.keys(engines).length) { toast("至少保留一个引擎", "error"); break; }
        const patch = { engines };
        if (state.config.default_engine === name) patch.default_engine = Object.keys(engines)[0];
        await saveLocalPatch(patch, "引擎已删除");
        break;
      }
      case "add-workspace": workspaceModal("", ""); break;
      case "edit-workspace": {
        const alias = target.dataset.alias;
        workspaceModal(alias, state.config?.workspaces?.[alias] || "");
        break;
      }
      case "delete-workspace": {
        const alias = target.dataset.alias;
        if (!await confirmAction("删除工作区", `确定删除工作区别名 ${alias}？`, "删除")) break;
        const workspaces = { ...(state.config.workspaces || {}) };
        delete workspaces[alias];
        if (!Object.keys(workspaces).length) { toast("至少保留一个工作区", "error"); break; }
        const patch = { workspaces };
        if (state.config.default_workspace === alias) patch.default_workspace = Object.keys(workspaces)[0];
        await saveLocalPatch(patch, "工作区已删除");
        break;
      }
      case "browse-folder": {
        const input = $(target.dataset.target);
        const initial = input && input.value ? input.value : "";
        const result = await api(`/api/dialog/folder?initial=${encodeURIComponent(initial)}`);
        if (result.path && input) input.value = result.path;
        break;
      }
      case "open-workspace": {
        await api("/api/open-path", { method: "POST", body: { kind: "workspace", path: target.dataset.path } });
        break;
      }
      case "open-path": {
        await api("/api/open-path", { method: "POST", body: { kind: target.dataset.kind } });
        break;
      }
      case "server-tab":
        state.serverTab = target.dataset.tab || "overview";
        renderPage();
        break;
      case "server-refresh":
        state.server = null;
        state.serverError = "";
        state.serverTried = false;
        await loadServer(true);
        break;
      case "server-disconnect": {
        if (!await confirmAction("断开服务器", "断开后仍可在本机运行 Agent，但无法远程管理插件和任务。", "断开")) break;
        await api("/api/server/disconnect", { method: "POST" });
        if (state.bootstrap?.server) state.bootstrap.server.connected = false;
        state.server = null;
        state.serverTried = false;
        state.serverError = "";
        toast("已断开服务器");
        renderPage();
        break;
      }
      case "register-self": {
        const result = await api("/api/server/register-self", { method: "POST" });
        state.server = result.overview;
        if (result.agent) state.agent = result.agent;
        state.serverTried = true;
        toast("本机已注册到服务器");
        renderPage();
        refreshStatus();
        break;
      }
      case "add-agent": serverAgentModal(null); break;
      case "edit-agent": {
        const id = target.dataset.agent;
        const agent = (state.server?.agents || []).find((item) => item.id === id);
        if (agent) serverAgentModal(agent);
        break;
      }
      case "delete-agent": {
        const id = target.dataset.agent;
        if (!await confirmAction("删除 Agent", `确定删除 ${id}？服务器将不再接受它的任务。`, "删除")) break;
        await api(`/api/server/agents/${encodeURIComponent(id)}`, { method: "DELETE" });
        toast("Agent 已删除");
        state.server = null;
        state.serverTried = false;
        await loadServer(true);
        break;
      }
      case "plugin-toggle": {
        const id = target.dataset.plugin;
        const enabled = target.dataset.enabled === "true";
        if (enabled && !await confirmAction("停用插件", `停用 ${id} 后，它注册的工具和任务会立即消失。`, "停用")) break;
        await api(`/api/server/plugins/${encodeURIComponent(id)}/${enabled ? "disable" : "enable"}`, { method: "POST" });
        toast(enabled ? "插件已停用" : "插件已启用");
        state.server = null;
        state.serverTried = false;
        await loadServer(true);
        break;
      }
      case "plugin-reload": {
        const id = target.dataset.plugin;
        await api(`/api/server/plugins/${encodeURIComponent(id)}/reload`, { method: "POST" });
        toast("插件已重载");
        state.server = null;
        state.serverTried = false;
        await loadServer(true);
        break;
      }
      case "job-save": {
        const id = target.dataset.job;
        const row = target.closest("tr");
        const interval = Number(row?.querySelector("[data-job-interval]")?.value);
        const windowMinutes = Number(row?.querySelector("[data-job-window]")?.value);
        if (!interval || interval < 1) throw new Error("间隔必须大于 0");
        await api(`/api/server/jobs/${encodeURIComponent(id)}`, {
          method: "PATCH",
          body: { interval_minutes: interval, window_minutes: windowMinutes || interval },
        });
        toast("任务间隔已保存");
        state.server = null;
        state.serverTried = false;
        await loadServer(true);
        break;
      }
      case "job-toggle": {
        const id = target.dataset.job;
        const enabled = target.dataset.enabled === "true";
        await api(`/api/server/jobs/${encodeURIComponent(id)}`, { method: "PATCH", body: { enabled: !enabled } });
        toast(enabled ? "任务已停用" : "任务已启用");
        state.server = null;
        state.serverTried = false;
        await loadServer(true);
        break;
      }
      case "job-run": {
        const id = target.dataset.job;
        const result = await api(`/api/server/jobs/${encodeURIComponent(id)}/run`, { method: "POST" });
        openModal({
          title: `任务 ${id} 执行结果`, eyebrow: "JOB RUN", saveText: null,
          body: `<div class="kv"><dt>状态</dt><dd>${badge(result.status || "unknown", result.status === "ok" ? "ok" : "warn")}</dd><dt>耗时</dt><dd>${esc(result.seconds ?? "—")} 秒</dd></div><div class="divider"></div><div class="code-block">${esc(JSON.stringify(result.result, null, 2))}</div>`,
        });
        break;
      }
      case "balance": {
        const result = await api("/api/server/balance");
        state.balance = result;
        if (state.page === "server" && state.serverTab === "overview") {
          toast("余额已更新");
          renderPage();
        } else {
          const lines = (result.balances || []).map((b) => `<div class="summary-item"><strong>${esc(b.total)} ${esc(b.currency)}</strong><span>赠送 ${esc(b.granted)} · 充值 ${esc(b.topped_up)}</span></div>`).join("");
          openModal({
            title: "API 余额", eyebrow: "BALANCE", saveText: null,
            body: `<div class="callout ${result.is_available ? "callout-ok" : "callout-danger"}">${icon("database")}<div><strong>${result.is_available ? "账户可用" : "账户不可用"}</strong><p>来自 Router 的 LLM 账户。</p></div></div><div class="summary-grid">${lines || `<div class="summary-item"><strong>—</strong><span>没有余额数据</span></div>`}</div>`,
          });
        }
        break;
      }
      case "toggle-autostart": {
        const enabled = target.dataset.enabled === "true";
        const result = await api("/api/autostart", { method: "POST", body: { enabled: !enabled } });
        if (state.bootstrap) state.bootstrap.autostart = result.enabled;
        toast(result.enabled ? "已开启开机自启" : "已关闭开机自启");
        renderPage();
        break;
      }
      case "toggle-log-scroll":
        state.autoScroll = !state.autoScroll;
        renderPage();
        break;
      case "refresh-logs":
        await loadLogs(true);
        renderPage();
        break;
      case "clear-logs": {
        if (!await confirmAction("清空日志", "只清空界面中的内存日志，磁盘日志文件保留。", "清空")) break;
        await api("/api/logs/clear", { method: "POST" });
        state.logs = [];
        state.lastLogId = 0;
        toast("日志已清空");
        renderPage();
        break;
      }
      case "copy": {
        const ok = await copyText(target.dataset.copy || "");
        toast(ok ? "已复制" : "复制失败", ok ? "ok" : "error");
        break;
      }
      case "quit": {
        if (!await confirmAction("退出控制台", "本机 Agent 将停止轮询。若已开启开机自启，下次登录会自动恢复后台服务。", "退出")) break;
        await api("/api/quit", { method: "POST" });
        $("boot").classList.remove("hide");
        $("boot-sub").textContent = "控制台已退出，可以关闭窗口。";
        setTimeout(() => { try { window.close(); } catch { /* ignore */ } }, 400);
        break;
      }
      default:
        break;
    }
  } catch (error) {
    toast(error.message, "error");
  }
});

document.addEventListener("click", (event) => {
  if (event.target.id === "modal") closeModal();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("modal").hidden) closeModal();
});

/* ------------------------------------------------------------------ init */
function showBootError(message) {
  $("boot").classList.remove("hide");
  $("boot-sub").innerHTML = `<span style="color:var(--danger)">${esc(message)}</span>`;
  $("boot-sub").style.marginTop = "6px";
}

async function init() {
  initToken();
  if (!state.token) {
    showBootError("请从桌面 App 打开控制台（缺少本地访问令牌）。");
    return;
  }
  try {
    const boot = await api("/api/bootstrap");
    state.bootstrap = boot;
    state.config = boot.config;
    state.agent = boot.agent;
    state.router = boot.router;
    if ($("app-version")) $("app-version").textContent = `v${boot.app?.version || "1.0.0"}`;
    await loadLogs(true);
    const page = state.initialPage && PAGES[state.initialPage]
      ? state.initialPage
      : ((location.hash.replace(/^#\/?/, "").split("?")[0]) || "overview");
    state.page = PAGES[page] ? page : "overview";
    if (state.page === "server" && ["overview", "agents", "plugins", "jobs", "config"].includes(state.initialServerTab)) {
      state.serverTab = state.initialServerTab;
    }
    updateSidebar();
    renderPage();
    $("boot").classList.add("hide");
    setInterval(refreshStatus, 4000);
    setInterval(() => loadLogs(false), 3000);
    window.addEventListener("hashchange", () => {
      const next = (location.hash.replace(/^#\/?/, "").split("?")[0]) || "overview";
      if (PAGES[next] && next !== state.page) {
        state.page = next;
        renderPage();
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshStatus();
    });
  } catch (error) {
    showBootError(error.message);
  }
}

init();
