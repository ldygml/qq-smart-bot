let csrf = "";
let instances = [];
let currentInstanceId = null;
let sending = false;
let busyTimer = null;
let busyStartedAt = 0;

const messagesElement = document.querySelector("#chat-messages");
const input = document.querySelector("#chat-input");
const sendButton = document.querySelector("#chat-send");
const instanceSelect = document.querySelector("#chat-instance");
const errorElement = document.querySelector("#chat-error");

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (csrf && options.method && options.method !== "GET") {
    headers["X-CSRF-Token"] = csrf;
  }
  const response = await fetch(path, {...options, headers});
  if (response.status === 401) {
    location.href = "/login";
    throw new Error("登录已失效");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    if (typeof detail === "string") throw new Error(detail);
    if (detail) throw new Error(JSON.stringify(detail));
    throw new Error(`HTTP ${response.status}`);
  }
  return data;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[char]));
}

function setText(selector, value) {
  const element = document.querySelector(selector);
  if (element) element.textContent = value;
}

function selectedInstance() {
  return instances.find((item) => Number(item.id) === Number(currentInstanceId)) || null;
}

function formatTime(value) {
  const date = value ? new Date(value) : new Date();
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"
  }).format(date);
}

function scrollToBottom(force = false) {
  const distance = messagesElement.scrollHeight - messagesElement.scrollTop - messagesElement.clientHeight;
  if (force || distance < 180) {
    messagesElement.scrollTop = messagesElement.scrollHeight;
  }
}

function autoResizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  setText("#chat-count", `${input.value.length} / 20000`);
}

function messageNode(message, {pending = false, typing = false} = {}) {
  const role = message.role === "user" ? "user" : "assistant";
  const article = document.createElement("article");
  article.className = `chat-message ${role}${pending ? " pending" : ""}`;
  if (message.id) article.dataset.messageId = String(message.id);

  const avatar = document.createElement("span");
  avatar.className = "chat-avatar";
  avatar.textContent = role === "user" ? "我" : "A";

  const wrap = document.createElement("div");
  wrap.className = "chat-bubble-wrap";
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";

  if (typing) {
    const dots = document.createElement("span");
    dots.className = "chat-typing";
    dots.innerHTML = "<i></i><i></i><i></i>";
    bubble.appendChild(dots);
    article.id = "chat-typing";
  } else {
    bubble.textContent = message.content || "";
  }

  const meta = document.createElement("div");
  meta.className = "chat-message-meta";
  if (!typing) {
    const owner = role === "user" ? "你" : "给她爱";
    const model = role === "assistant" && message.model ? ` · ${message.model}` : "";
    const pendingText = pending ? " · 正在发送" : "";
    meta.textContent = `${owner}${model} · ${formatTime(message.created_at)}${pendingText}`;
  }

  if (role === "user") {
    wrap.append(bubble, meta);
    article.append(wrap, avatar);
  } else {
    wrap.append(bubble, meta);
    article.append(avatar, wrap);
  }
  return article;
}

function appendMessage(message, options = {}) {
  messagesElement.appendChild(messageNode(message, options));
  scrollToBottom();
}

function showEmptyConversation() {
  messagesElement.innerHTML = `
    <div class="chat-empty">
      <div class="chat-empty-card">
        <div class="logo">G</div>
        <h2>从一句话开始</h2>
        <p>这是直接和机器人聊天的独立窗口。可以直接问问题、让它联网查资料，或者接着之前的话题继续聊。</p>
        <div class="chat-suggestions">
          <button type="button" data-suggestion="用你平时的语气跟我聊聊今天怎么样">随便聊聊</button>
          <button type="button" data-suggestion="帮我梳理一下最近要做的事情">整理事情</button>
          <button type="button" data-suggestion="联网查一下今天值得关注的消息，并说明来源">联网搜索</button>
        </div>
      </div>
    </div>`;
  messagesElement.querySelectorAll("[data-suggestion]").forEach((button) => {
    button.onclick = () => {
      input.value = button.dataset.suggestion;
      autoResizeInput();
      input.focus();
    };
  });
}

function renderConversation(messages) {
  messagesElement.innerHTML = "";
  if (!messages?.length) {
    showEmptyConversation();
    return;
  }
  messages.forEach((message) => appendMessage(message));
  requestAnimationFrame(() => scrollToBottom(true));
}

function updateInstanceUi(instance = selectedInstance()) {
  const online = instance?.status === "online";
  const status = document.querySelector("#chat-instance-state");
  const dot = document.querySelector("#chat-online-dot");
  const adminLink = document.querySelector("#chat-admin-link");
  if (status) {
    status.className = `chat-instance-state ${instance ? (online ? "online" : "offline") : ""}`;
    status.textContent = instance
      ? online
        ? `运行中 · 127.0.0.1:${instance.port} · ${instance.provider_name || "当前模型"}`
        : `实例当前${instance.status || "离线"}，可以查看历史，但不能发送新消息`
      : "还没有可用的机器人实例";
  }
  if (dot) dot.className = `chat-online-dot ${online ? "online" : "offline"}`;
  setText("#chat-room-title", instance ? instance.name : "未选择机器人");
  setText("#chat-room-meta", online ? "在线 · 网页直连" : "离线");
  setText("#chat-hint", online
    ? "直接回复，不发送到 QQ；记录会在本实例下保存。"
    : "机器人离线，可查看已有记录；启动实例后再发送。");
  if (adminLink) {
    adminLink.hidden = !instance;
    if (instance) adminLink.href = `/api/instances/${instance.id}/open-admin`;
  }
  const canChat = Boolean(instance && online && !sending);
  input.readOnly = !canChat;
  sendButton.disabled = !canChat;
}

function startBusyTimer() {
  busyStartedAt = Date.now();
  clearInterval(busyTimer);
  busyTimer = setInterval(() => {
    const seconds = Math.max(1, Math.round((Date.now() - busyStartedAt) / 1000));
    sendButton.textContent = `等待 ${seconds}s`;
  }, 1000);
}

function stopBusyTimer() {
  clearInterval(busyTimer);
  busyTimer = null;
  sendButton.textContent = "发送";
}

function setSending(value) {
  sending = value;
  errorElement.textContent = "";
  if (value) {
    const typing = messageNode({role: "assistant"}, {typing: true});
    messagesElement.appendChild(typing);
    scrollToBottom(true);
    startBusyTimer();
  } else {
    document.querySelector("#chat-typing")?.remove();
    stopBusyTimer();
  }
  updateInstanceUi();
}

async function loadMe() {
  const me = await api("/api/auth/me");
  csrf = me.csrf_token;
  setText("#chat-user-label", me.username);
}

function rememberInstance(instanceId) {
  try { localStorage.setItem("qqbot-web-chat-instance", String(instanceId)); } catch {}
}

async function loadInstances() {
  const data = await api("/api/instances");
  instances = data.instances || [];
  const params = new URLSearchParams(location.search);
  const queryId = Number(params.get("instance") || 0);
  let storedId = 0;
  try { storedId = Number(localStorage.getItem("qqbot-web-chat-instance") || 0); } catch {}
  const preferred = instances.find((item) => Number(item.id) === queryId)
    || instances.find((item) => Number(item.id) === storedId)
    || instances.find((item) => item.status === "online")
    || instances[0];
  instanceSelect.innerHTML = instances.length
    ? instances.map((item) => `<option value="${item.id}">${esc(item.name)} · ${item.status === "online" ? "在线" : "离线"}</option>`).join("")
    : `<option value="">没有机器人实例</option>`;
  currentInstanceId = preferred ? Number(preferred.id) : null;
  if (currentInstanceId) {
    instanceSelect.value = String(currentInstanceId);
    rememberInstance(currentInstanceId);
  }
  updateInstanceUi(preferred);
  await loadConversation();
}

async function loadConversation(showLoading = true) {
  if (!currentInstanceId) {
    messagesElement.innerHTML = `<div class="chat-empty"><div class="chat-empty-card"><h2>还没有机器人</h2><p>请先回到控制台创建一个实例并启动。</p><a class="primary" href="/portal">返回控制台</a></div></div>`;
    updateInstanceUi(null);
    return;
  }
  if (showLoading) messagesElement.innerHTML = `<div class="chat-loading">正在读取聊天记录…</div>`;
  const data = await api(`/api/web-chat/${currentInstanceId}`);
  const index = instances.findIndex((item) => Number(item.id) === Number(currentInstanceId));
  if (index >= 0) instances[index] = data.instance;
  updateInstanceUi(data.instance);
  renderConversation(data.messages || []);
}

async function sendMessage(event) {
  event?.preventDefault();
  if (sending) return;
  const text = input.value.trim();
  if (!text) return;
  const instance = selectedInstance();
  if (!instance || instance.status !== "online") {
    errorElement.textContent = "机器人实例当前离线，无法发送消息。";
    return;
  }

  appendMessage({
    role: "user",
    content: text,
    created_at: new Date().toISOString(),
  }, {pending: true});
  input.value = "";
  autoResizeInput();
  setSending(true);

  try {
    const data = await api(`/api/web-chat/${currentInstanceId}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({message: text}),
    });
    document.querySelector("#chat-typing")?.remove();
    if (data.message) {
      appendMessage(data.message);
    } else {
      appendMessage({
        role: "assistant",
        content: data.reason || "这次没有生成回复，请再试一次。",
        created_at: new Date().toISOString(),
        model: data.model || "",
      });
    }
    document.querySelectorAll(".chat-message.pending").forEach((item) => {
      item.classList.remove("pending");
      const meta = item.querySelector(".chat-message-meta");
      if (meta) meta.textContent = meta.textContent.replace(" · 正在发送", "");
    });
  } catch (error) {
    errorElement.textContent = error.message || "发送失败";
    await loadConversation(false).catch(() => {});
  } finally {
    setSending(false);
    input.focus();
  }
}

async function clearConversation() {
  if (!currentInstanceId || sending) return;
  if (!confirm("清空这个实例的全部网页聊天记录？QQ 对话和长期记忆不会被删除。")) return;
  try {
    await api(`/api/web-chat/${currentInstanceId}`, {method: "DELETE"});
    renderConversation([]);
    errorElement.textContent = "";
  } catch (error) {
    errorElement.textContent = error.message;
  }
}

instanceSelect.addEventListener("change", async () => {
  if (sending) {
    instanceSelect.value = String(currentInstanceId);
    return;
  }
  currentInstanceId = Number(instanceSelect.value) || null;
  if (currentInstanceId) rememberInstance(currentInstanceId);
  updateInstanceUi();
  errorElement.textContent = "";
  try { await loadConversation(); }
  catch (error) { errorElement.textContent = error.message; }
});

document.querySelector("#chat-form").addEventListener("submit", sendMessage);
document.querySelector("#chat-reload").addEventListener("click", () => {
  loadConversation().catch((error) => { errorElement.textContent = error.message; });
});
document.querySelector("#chat-clear").addEventListener("click", clearConversation);
input.addEventListener("input", autoResizeInput);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
});
document.querySelector("#chat-logout").addEventListener("click", async () => {
  try { await api("/api/auth/logout", {method: "POST"}); } catch {}
  location.href = "/login";
});

autoResizeInput();
Promise.all([loadMe(), loadInstances()])
  .catch((error) => {
    messagesElement.innerHTML = "";
    errorElement.textContent = error.message || "聊天页面加载失败";
  });
