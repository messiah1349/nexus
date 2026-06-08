"use strict";

const PATH = window.location.pathname;
let activeProjectId = null;
let activeProjectName = null;

// ---------- Generic helpers ----------

async function api(method, url, body) {
  const opts = { method, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers = { "content-type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (r.status === 401) {
    window.location = "/login";
    throw new Error("unauthorized");
  }
  if (!r.ok) {
    const detail = await r.text().catch(() => r.statusText);
    throw new Error(`${r.status}: ${detail}`);
  }
  if (r.status === 204) return null;
  return r.json();
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------- Auth page ----------

async function initAuthPage() {
  const cfg = await api("GET", "/api/auth/config");
  const host = document.getElementById("telegram-widget-host");

  if (cfg.telegram_bot_username) {
    // Telegram Login Widget — the script auto-renders inside `host` and
    // sends the user to /auth/telegram/callback on confirm.
    const script = document.createElement("script");
    script.async = true;
    script.src = "https://telegram.org/js/telegram-widget.js?22";
    script.dataset.telegramLogin = cfg.telegram_bot_username;
    script.dataset.size = "large";
    script.dataset.authUrl = `${window.location.origin}/auth/telegram/callback`;
    script.dataset.requestAccess = "write";
    host.appendChild(script);
  } else {
    host.innerHTML =
      '<p class="muted small">Telegram Login Widget not configured (set TELEGRAM_BOT_USERNAME).</p>';
  }

  if (cfg.dev_auth_enabled) {
    document.getElementById("dev-login-wrap").hidden = false;
    document.getElementById("dev-login-form").addEventListener("submit", onDevLogin);
  }
}

async function onDevLogin(e) {
  e.preventDefault();
  const err = document.getElementById("dev-login-error");
  err.hidden = true;
  const telegramId = parseInt(document.getElementById("dev-tg-id").value, 10);
  const displayName = document.getElementById("dev-name").value || null;
  if (!telegramId) {
    err.textContent = "telegram_id required";
    err.hidden = false;
    return;
  }
  try {
    await api("POST", "/auth/dev", {
      telegram_id: telegramId,
      display_name: displayName,
    });
    window.location = "/app";
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
}

// ---------- Chat page ----------

async function initChatPage() {
  document.getElementById("logout").addEventListener("click", onLogout);
  document.getElementById("chat-form").addEventListener("submit", onSubmit);
  document.getElementById("chat-input").addEventListener("keydown", (e) => {
    // Enter to send; Shift+Enter for newline.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      document.getElementById("chat-form").requestSubmit();
    }
  });
  document.getElementById("end-session").addEventListener("click", onEndSession);

  await loadProjects();
}

async function loadProjects() {
  const projects = await api("GET", "/api/projects");
  const list = document.getElementById("project-list");
  const empty = document.getElementById("no-projects");
  list.innerHTML = "";
  if (projects.length === 0) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  for (const p of projects) {
    const li = document.createElement("li");
    li.dataset.projectId = p.id;
    li.innerHTML = `${escapeHtml(p.name)}<span class="domain">${escapeHtml(p.domain)}</span>`;
    li.addEventListener("click", () => selectProject(p));
    list.appendChild(li);
  }
}

async function selectProject(p) {
  activeProjectId = p.id;
  activeProjectName = p.name;
  for (const li of document.querySelectorAll("#project-list li")) {
    li.classList.toggle("active", li.dataset.projectId === p.id);
  }
  document.getElementById("chat-title").textContent = p.name;
  document.getElementById("chat-input").disabled = false;
  document.getElementById("send-button").disabled = false;
  document.getElementById("end-session").hidden = false;
  await renderHistory();
  document.getElementById("chat-input").focus();
}

async function renderHistory() {
  const m = document.getElementById("messages");
  m.innerHTML = "";
  const history = await api(
    "GET",
    `/api/projects/${activeProjectId}/messages?limit=50`,
  );
  for (const msg of history) {
    appendMessage(msg.role, msg.content);
  }
}

function appendMessage(role, content) {
  const m = document.getElementById("messages");
  const div = document.createElement("div");
  div.className = `message ${role}`;
  div.innerHTML = `<span class="role">${escapeHtml(role)}</span>${escapeHtml(content)}`;
  m.appendChild(div);
  m.scrollTop = m.scrollHeight;
}

function setStatus(text, isError = false) {
  const el = document.getElementById("chat-status");
  if (!text) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.textContent = text;
  el.classList.toggle("error", isError);
}

async function onSubmit(e) {
  e.preventDefault();
  if (!activeProjectId) return;
  const input = document.getElementById("chat-input");
  const button = document.getElementById("send-button");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  appendMessage("user", text);
  input.disabled = true;
  button.disabled = true;
  setStatus("Coach is thinking…");
  try {
    const reply = await api("POST", `/api/projects/${activeProjectId}/messages`, {
      text,
    });
    appendMessage("assistant", reply.content);
    setStatus("");
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    input.disabled = false;
    button.disabled = false;
    input.focus();
  }
}

async function onLogout() {
  try {
    await api("POST", "/auth/logout");
  } finally {
    window.location = "/login";
  }
}

async function onEndSession() {
  if (!activeProjectId) return;
  if (!confirm(`End and summarize the current session in '${activeProjectName}'?`)) {
    return;
  }
  setStatus("Summarizing…");
  try {
    const result = await api("POST", `/api/projects/${activeProjectId}/end`);
    if (result.summary) {
      appendMessage("assistant", `[Session ended]\n\n${result.summary}`);
    }
    setStatus("");
  } catch (e) {
    setStatus(e.message, true);
  }
}

// ---------- Bootstrap ----------

if (PATH === "/login") {
  initAuthPage().catch((e) => console.error(e));
} else if (PATH === "/app") {
  initChatPage().catch((e) => console.error(e));
}
