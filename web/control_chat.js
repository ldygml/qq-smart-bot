let csrf = "";
let instances = [];
let currentInstanceId = null;
let contacts = null;
let currentTarget = null;
let currentMembers = [];
let loadingHistory = false;
let sending = false;
let pollTimer = null;

const elements = {
  instance: document.querySelector("#chat-instance"),
  instanceState: document.querySelector("#chat-instance-state"),
  accountName: document.querySelector("#qq-account-name"),
  accountId: document.querySelector("#qq-account-id"),
  warning: document.querySelector("#qq-contact-warning"),
  search: document.querySelector("#qq-contact-search"),
  friends: document.querySelector("#qq-friends"),
  groups: document.querySelector("#qq-groups"),
  members: document.querySelector("#qq-members"),
  memberSection: document.querySelector("#qq-member-section"),
  friendCount: document.querySelector("#qq-friend-count"),
  groupCount: document.querySelector("#qq-group-count"),
  memberCount: document.querySelector("#qq-member-count"),
  roomAvatar: document.querySelector("#qq-room-avatar"),
  roomTitle: document.querySelector("#qq-room-title"),
  roomBadge: document.querySelector("#qq-room-badge"),
  roomMeta: document.querySelector("#qq-room-meta"),
  reload: document.querySelector("#qq-reload-history"),
  messages: document.querySelector("#qq-messages"),
  form: document.querySelector("#qq-send-form"),
  targetLabel: document.querySelector("#qq-send-target"),
  targetRelation: document.querySelector("#qq-send-relation"),
  input: document.querySelector("#qq-message-input"),
  send: document.querySelector("#qq-send-button"),
  hint: document.querySelector("#qq-compose-hint"),
  count: document.querySelector("#qq-message-count"),
  error: document.querySelector("#qq-error"),
  adminLink: document.querySelector("#chat-admin-link"),
  userLabel: document.querySelector("#chat-user-label"),
};

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

function initials(value) {
  const text = String(value || "Q").trim();
  return esc(Array.from(text)[0] || "Q").toUpperCase();
}

function selectedInstance() {
  return instances.find((item) => Number(item.id) === Number(currentInstanceId)) || null;
}

function instanceOnline() {
  return selectedInstance()?.status === "online";
}

function rememberInstance(instanceId) {
  try { localStorage.setItem("qqbot-qq-console-instance", String(instanceId)); } catch {}
}

function formatTime(timestamp, withDate = false) {
  const numeric = Number(timestamp || 0);
  const date = numeric > 0 ? new Date(numeric * 1000) : new Date();
  if (Number.isNaN(date.getTime())) return "";
  const options = withDate
    ? {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}
    : {hour: "2-digit", minute: "2-digit"};
  return new Intl.DateTimeFormat("zh-CN", options).format(date);
}

function formatDay(timestamp) {
  const numeric = Number(timestamp || 0);
  if (!numeric) return "最近消息";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit"
  }).format(new Date(numeric * 1000));
}

function setError(value = "") {
  elements.error.textContent = value;
}

function setLoading(selector, text) {
  const element = document.querySelector(selector);
  if (element) element.innerHTML = `<div class="qq-list-loading">${esc(text)}</div>`;
}

async function loadMe() {
  const me = await api("/api/auth/me");
  csrf = me.csrf_token || "";
  elements.userLabel.textContent = me.username || "";
}

function updateInstanceUi() {
  const instance = selectedInstance();
  const online = instance?.status === "online";
  elements.instanceState.textContent = instance
    ? `${instance.name} · ${online ? "机器人服务在线" : `服务${instance.status || "离线"}`}`
    : "没有可用机器人实例";
  elements.instanceState.className = `chat-instance-state ${online ? "online" : "offline"}`;
  elements.adminLink.hidden = !instance;
  if (instance) {
    elements.adminLink.href = `/api/instances/${instance.id}/open-admin`;
  }
  updateComposer();
}

async function loadInstances() {
  const data = await api("/api/instances");
  instances = data.instances || [];
  const queryId = Number(new URLSearchParams(location.search).get("instance") || 0);
  let storedId = 0;
  try { storedId = Number(localStorage.getItem("qqbot-qq-console-instance") || 0); } catch {}
  const preferred = instances.find((item) => Number(item.id) === queryId)
    || instances.find((item) => Number(item.id) === storedId)
    || instances.find((item) => item.status === "online")
    || instances[0];
  elements.instance.innerHTML = instances.length
    ? instances.map((item) => `<option value="${item.id}">${esc(item.name)} · ${item.status === "online" ? "在线" : "离线"}</option>`).join("")
    : `<option value="">没有机器人实例</option>`;
  currentInstanceId = preferred ? Number(preferred.id) : null;
  if (currentInstanceId) {
    elements.instance.value = String(currentInstanceId);
    rememberInstance(currentInstanceId);
  }
  updateInstanceUi();
  await loadContacts();
}

function contactSearchMatches(text) {
  const query = elements.search.value.trim().toLowerCase();
  return !query || String(text || "").toLowerCase().includes(query);
}

function emptyListMarkup(text) {
  return `<div class="qq-list-empty">${esc(text)}</div>`;
}

function friendRow(item) {
  if (!contactSearchMatches(`${item.display_name} ${item.nickname} ${item.remark} ${item.user_id}`)) return "";
  const active = currentTarget?.kind === "friend" && currentTarget.id === Number(item.user_id);
  return `
    <button class="qq-contact-row friend${active ? " active" : ""}" type="button" data-kind="friend" data-id="${item.user_id}">
      <span class="qq-contact-avatar">${initials(item.display_name)}</span>
      <span class="qq-contact-copy">
        <strong>${esc(item.display_name)}</strong>
        <small>QQ ${esc(item.user_id)}</small>
      </span>
      <span class="qq-contact-mark friend">可私聊</span>
    </button>`;
}

function groupRow(item) {
  if (!contactSearchMatches(`${item.display_name} ${item.group_name} ${item.group_id}`)) return "";
  const active = currentTarget?.kind === "group" && currentTarget.id === Number(item.group_id);
  return `
    <button class="qq-contact-row${active ? " active" : ""}" type="button" data-kind="group" data-id="${item.group_id}">
      <span class="qq-contact-avatar">${initials(item.display_name)}</span>
      <span class="qq-contact-copy">
        <strong>${esc(item.display_name)}</strong>
        <small>群号 ${esc(item.group_id)}</small>
      </span>
      <span class="qq-contact-mark group">群聊</span>
    </button>`;
}

function memberRow(item) {
  if (!contactSearchMatches(`${item.display_name} ${item.nickname} ${item.card} ${item.user_id}`)) return "";
  const active = currentTarget?.kind === "member" && currentTarget.id === Number(item.user_id);
  return `
    <button class="qq-contact-row ${item.can_private_chat ? "self-friend" : ""}${active ? " active" : ""}" type="button" data-kind="member" data-id="${item.user_id}">
      <span class="qq-contact-avatar">${initials(item.display_name)}</span>
      <span class="qq-contact-copy">
        <strong>${esc(item.display_name)}</strong>
        <small>${item.can_private_chat ? `好友 · QQ ${esc(item.user_id)}` : `群成员 · QQ ${esc(item.user_id)}`}</small>
      </span>
      <span class="qq-contact-mark ${item.can_private_chat ? "friend" : "member"}">${item.can_private_chat ? "可私聊" : "仅同群"}</span>
    </button>`;
}

function renderContacts() {
  const friends = contacts?.friends || [];
  const groups = contacts?.groups || [];
  const friendMarkup = friends.map(friendRow).filter(Boolean).join("");
  const groupMarkup = groups.map(groupRow).filter(Boolean).join("");
  elements.friends.innerHTML = friends.length
    ? (friendMarkup || emptyListMarkup("没有匹配的好友"))
    : emptyListMarkup(contacts?.friends_available === false ? "好友列表不可用" : "没有好友");
  elements.groups.innerHTML = groups.length
    ? (groupMarkup || emptyListMarkup("没有匹配的群聊"))
    : emptyListMarkup("没有群聊");
  elements.friendCount.textContent = String(friends.length);
  elements.groupCount.textContent = String(groups.length);
  bindContactRows(elements.friends, "friend");
  bindContactRows(elements.groups, "group");
  renderMembers();
}

function bindContactRows(container, kind) {
  container.querySelectorAll(".qq-contact-row").forEach((row) => {
    row.addEventListener("click", () => selectContact(kind, Number(row.dataset.id)));
  });
}

function renderMembers() {
  if (!currentTarget?.groupContext && currentTarget?.kind !== "group") {
    elements.memberSection.hidden = true;
    return;
  }
  elements.memberSection.hidden = false;
  elements.memberCount.textContent = String(currentMembers.length);
  const markup = currentMembers.map(memberRow).filter(Boolean).join("");
  elements.members.innerHTML = currentMembers.length
    ? (markup || emptyListMarkup("没有匹配的群成员"))
    : emptyListMarkup("正在读取或没有可显示的群成员");
  bindContactRows(elements.members, "member");
}

async function loadContacts() {
  if (!currentInstanceId) {
    renderContacts();
    return;
  }
  setLoading("#qq-friends", "正在读取好友…");
  setLoading("#qq-groups", "正在读取群聊…");
  elements.accountName.textContent = "正在读取 QQ…";
  elements.accountId.textContent = "—";
  elements.warning.textContent = "";
  try {
    contacts = await api(`/api/instances/${currentInstanceId}/qq-web/contacts`);
    const login = contacts.login || {};
    elements.accountName.textContent = login.nickname || "机器人 QQ";
    elements.accountId.textContent = login.user_id ? `QQ ${login.user_id}` : "QQ 号未返回";
    elements.warning.textContent = contacts.warning || "";
    renderContacts();
    if (currentTarget) {
      const stillExists = resolveTarget(currentTarget.kind, currentTarget.id);
      if (!stillExists) clearTarget();
    }
  } catch (error) {
    contacts = {friends: [], groups: [], friends_available: false};
    elements.warning.textContent = error.message;
    renderContacts();
  }
}

function resolveTarget(kind, id) {
  if (!contacts) return null;
  if (kind === "friend") {
    const item = contacts.friends.find((entry) => Number(entry.user_id) === id);
    return item ? {
      kind: "friend", id, display_name: item.display_name,
      relationship: "friend", relationship_label: "好友 · 可直接私聊",
      can_send: true, target_type: "private", group_context: null,
    } : null;
  }
  if (kind === "group") {
    const item = contacts.groups.find((entry) => Number(entry.group_id) === id);
    return item ? {
      kind: "group", id, display_name: item.display_name,
      relationship: "group", relationship_label: "QQ 群聊",
      can_send: true, target_type: "group", group_context: id,
    } : null;
  }
  if (kind === "member") {
    const item = currentMembers.find((entry) => Number(entry.user_id) === id);
    if (!item) return null;
    return {
      kind: "member", id, display_name: item.display_name,
      relationship: item.relationship,
      relationship_label: item.relationship_label,
      can_send: Boolean(item.can_private_chat),
      target_type: "private",
      group_context: currentTarget?.group_context || currentTarget?.id || null,
    };
  }
  return null;
}

async function selectContact(kind, id) {
  if (kind === "group") {
    const target = resolveTarget("group", id);
    if (!target) return;
    currentTarget = target;
    currentMembers = [];
    updateTargetUi();
    renderContacts();
    await Promise.all([loadMembers(id), loadHistory()]);
    return;
  }
  if (kind === "member") {
    const groupContext = currentTarget?.group_context || currentTarget?.id || null;
    const item = currentMembers.find((entry) => Number(entry.user_id) === id);
    if (!item) return;
    currentTarget = {
      kind: "member", id, display_name: item.display_name,
      relationship: item.relationship,
      relationship_label: item.relationship_label,
      can_send: Boolean(item.can_private_chat),
      target_type: "private", group_context: groupContext,
    };
    updateTargetUi();
    renderContacts();
    if (item.can_private_chat) await loadHistory();
    else showBlockedMember(item);
    return;
  }
  const target = resolveTarget(kind, id);
  if (!target) return;
  currentTarget = target;
  elements.memberSection.hidden = true;
  updateTargetUi();
  renderContacts();
  await loadHistory();
}

function clearTarget() {
  currentTarget = null;
  currentMembers = [];
  elements.memberSection.hidden = true;
  elements.roomTitle.textContent = "选择一个好友或群聊";
  elements.roomAvatar.textContent = "Q";
  elements.roomBadge.hidden = true;
  elements.roomMeta.textContent = "消息会通过机器人账号直接发送到 QQ";
  elements.messages.innerHTML = `
    <div class="qq-empty-state">
      <span class="qq-empty-mark">QQ</span>
      <h2>这里是真实消息控制台</h2>
      <p>从左侧选择好友或群聊。只有好友和明确标为“可私聊”的群成员才能打开私聊。</p>
    </div>`;
  updateComposer();
  renderContacts();
}

function updateTargetUi() {
  if (!currentTarget) return;
  elements.roomAvatar.textContent = initials(currentTarget.display_name);
  elements.roomTitle.textContent = currentTarget.display_name;
  elements.roomBadge.hidden = false;
  elements.roomBadge.className = `qq-relation-badge ${currentTarget.relationship}`;
  elements.roomBadge.textContent = currentTarget.relationship_label;
  elements.roomMeta.textContent = currentTarget.kind === "group"
    ? `群号 ${currentTarget.id} · 直接发送到 QQ 群`
    : currentTarget.can_send
      ? `QQ ${currentTarget.id} · 直接发送私聊`
      : `QQ ${currentTarget.id} · 不是机器人好友，私聊不可用`;
  elements.targetLabel.textContent = currentTarget.display_name;
  elements.targetRelation.textContent = currentTarget.relationship_label;
  updateComposer();
}

function updateComposer() {
  const canSend = Boolean(
    currentTarget?.can_send
    && currentInstanceId
    && instanceOnline()
    && !sending
  );
  elements.input.disabled = !canSend;
  elements.send.disabled = !canSend;
  elements.reload.disabled = !currentTarget || currentTarget.kind === "member" && !currentTarget.can_send;
  if (!currentTarget) {
    elements.targetLabel.textContent = "尚未选择";
    elements.targetRelation.textContent = "";
    elements.input.placeholder = "选择联系人后输入消息。Enter 发送，Shift+Enter 换行";
    elements.hint.textContent = "消息不会经过模型改写";
  } else if (!currentTarget.can_send) {
    elements.input.placeholder = "对方不是 QQ 好友，无法直接私聊";
    elements.hint.textContent = "仅同群成员没有直接私聊权限";
  } else if (!instanceOnline()) {
    elements.input.placeholder = "机器人服务当前离线";
    elements.hint.textContent = "服务恢复后可发送";
  } else {
    elements.input.placeholder = "输入要发送到 QQ 的消息。Enter 发送，Shift+Enter 换行";
    elements.hint.textContent = currentTarget.kind === "group"
      ? "将直接发送到当前 QQ 群"
      : "将直接发送到 QQ 好友私聊";
  }
}

async function loadMembers(groupId) {
  if (!currentInstanceId || !groupId) return;
  setLoading("#qq-members", "正在读取群成员…");
  elements.memberSection.hidden = false;
  elements.memberCount.textContent = "—";
  try {
    const data = await api(`/api/instances/${currentInstanceId}/qq-web/group-members?group_id=${groupId}`);
    if (currentTarget?.kind !== "group" || Number(currentTarget.id) !== Number(groupId)) return;
    currentMembers = data.members || [];
    if (data.warning) elements.warning.textContent = data.warning;
    renderMembers();
  } catch (error) {
    currentMembers = [];
    elements.members.innerHTML = emptyListMarkup(`群成员读取失败：${error.message}`);
  }
}

function messageMarkup(message) {
  const assistant = message.role === "assistant";
  const sender = assistant ? (message.sender_name || "机器人") : (message.sender_name || "群成员");
  return `
    <article class="qq-message ${assistant ? "assistant" : "user"}">
      <span class="qq-message-avatar">${initials(sender)}</span>
      <div class="qq-message-body">
        <div class="qq-message-sender">${esc(sender)}</div>
        <div class="qq-message-bubble">${esc(message.content || "")}</div>
        <div class="qq-message-time">${esc(formatTime(message.timestamp))}</div>
      </div>
    </article>`;
}

function showBlockedMember(item) {
  elements.messages.innerHTML = `
    <div class="qq-empty-state">
      <span class="qq-empty-mark">!</span>
      <h2>${esc(item.display_name)} 不是机器人好友</h2>
      <p>对方只是当前群里的成员。QQ 不允许机器人凭群关系直接打开私聊，所以这里不会伪装成可以发送。你可以回到群聊向群内发消息。</p>
    </div>`;
  elements.roomMeta.textContent = `QQ ${item.user_id} · 仅同群，不能直接私聊`;
}

async function loadHistory(showLoading = true) {
  if (!currentTarget || !currentInstanceId || !["private", "group"].includes(currentTarget.target_type)) {
    return;
  }
  if (!currentTarget.can_send) return;
  if (loadingHistory) return;
  loadingHistory = true;
  setError("");
  const targetSnapshot = `${currentTarget.kind}:${currentTarget.id}`;
  if (showLoading) {
    elements.messages.innerHTML = `<div class="qq-empty-state"><p>正在读取 QQ 消息记录…</p></div>`;
  }
  try {
    const data = await api(
      `/api/instances/${currentInstanceId}/qq-web/history`
      + `?target_type=${encodeURIComponent(currentTarget.target_type)}`
      + `&target_id=${currentTarget.id}&count=80`
    );
    if (`${currentTarget?.kind}:${currentTarget?.id}` !== targetSnapshot) return;
    const messages = data.messages || [];
    if (!messages.length) {
      elements.messages.innerHTML = `
        <div class="qq-empty-state">
          <span class="qq-empty-mark">QQ</span>
          <h2>还没有可显示的消息</h2>
          <p>可以直接在下方发送第一条消息。</p>
        </div>`;
      return;
    }
    const firstTimestamp = messages[0]?.timestamp;
    elements.messages.innerHTML = `
      <div class="qq-day-divider">${esc(formatDay(firstTimestamp))}</div>
      ${messages.map(messageMarkup).join("")}`;
    elements.messages.scrollTop = elements.messages.scrollHeight;
  } catch (error) {
    setError(error.message);
    elements.messages.innerHTML = `
      <div class="qq-empty-state">
        <span class="qq-empty-mark">!</span>
        <h2>消息记录暂时读不到</h2>
        <p>${esc(error.message)}</p>
      </div>`;
  } finally {
    loadingHistory = false;
  }
}

async function sendMessage(event) {
  event?.preventDefault();
  if (sending || !currentTarget?.can_send) return;
  const text = elements.input.value.trim();
  if (!text) return;
  sending = true;
  setError("");
  updateComposer();
  elements.send.textContent = "发送中…";
  try {
    const data = await api(`/api/instances/${currentInstanceId}/qq-web/send`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        target_type: currentTarget.target_type,
        target_id: currentTarget.id,
        message: text,
      }),
    });
    elements.input.value = "";
    updateMessageCount();
    const divider = elements.messages.querySelector(".qq-day-divider");
    const empty = elements.messages.querySelector(".qq-empty-state");
    if (empty) elements.messages.innerHTML = `<div class="qq-day-divider">今天</div>`;
    else if (!divider) elements.messages.insertAdjacentHTML("afterbegin", `<div class="qq-day-divider">最近消息</div>`);
    elements.messages.insertAdjacentHTML("beforeend", messageMarkup({
      ...data.message,
      role: "assistant",
    }));
    elements.messages.scrollTop = elements.messages.scrollHeight;
  } catch (error) {
    setError(error.message || "消息发送失败");
  } finally {
    sending = false;
    elements.send.textContent = "发送到 QQ";
    updateComposer();
    elements.input.focus();
  }
}

function autoResizeInput() {
  elements.input.style.height = "auto";
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 180)}px`;
  updateMessageCount();
}

function updateMessageCount() {
  elements.count.textContent = `${elements.input.value.length} / 20000`;
}

async function switchInstance(instanceId) {
  if (sending) {
    elements.instance.value = String(currentInstanceId);
    return;
  }
  currentInstanceId = Number(instanceId) || null;
  if (currentInstanceId) rememberInstance(currentInstanceId);
  clearTarget();
  updateInstanceUi();
  await loadContacts();
}

elements.instance.addEventListener("change", () => {
  switchInstance(elements.instance.value).catch((error) => setError(error.message));
});
document.querySelector("#qq-refresh-contacts").addEventListener("click", () => {
  loadContacts().catch((error) => setError(error.message));
});
elements.search.addEventListener("input", renderContacts);
elements.form.addEventListener("submit", sendMessage);
elements.reload.addEventListener("click", () => {
  loadHistory().catch((error) => setError(error.message));
});
elements.input.addEventListener("input", autoResizeInput);
elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
});
document.querySelector("#chat-logout").addEventListener("click", async () => {
  try { await api("/api/auth/logout", {method: "POST"}); } catch {}
  location.href = "/login";
});

pollTimer = setInterval(() => {
  if (document.hidden || sending || loadingHistory || !currentTarget?.can_send) return;
  loadHistory(false).catch(() => {});
}, 15000);
window.addEventListener("beforeunload", () => clearInterval(pollTimer));

autoResizeInput();
Promise.all([loadMe(), loadInstances()])
  .catch((error) => {
    elements.messages.innerHTML = "";
    setError(error.message || "QQ 消息控制台加载失败");
  });
