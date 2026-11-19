let csrf = "";
let instances = [];
let currentInstanceId = null;
let contacts = null;
let currentTarget = null;
let currentMembers = [];
let loadingHistory = false;
let sending = false;
let pollTimer = null;
let pendingAttachments = [];
let libraryItems = [];
let libraryFilter = "all";
let libraryLoading = false;
let uploadingCount = 0;
let uploadQueueTotal = 0;
let uploadQueueDone = 0;
let currentFileProgress = 0;
let quoteTarget = null;
let forwardMessages = [];
let targetSelection = new Set();
let liveEvents = null;
let uploadPaused = false;
let uploadCancelled = false;
let uploadFailures = 0;
let uploadRunToken = 0;
let activeUploadId = "";
let uploadAbortController = null;
let currentMessages = [];
let pendingMentions = [];
let renderedHistoryTarget = "";

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
  liveState: document.querySelector("#qq-live-state"),
  searchButton: document.querySelector("#qq-search-button"),
  multiButton: document.querySelector("#qq-multi-button"),
  messages: document.querySelector("#qq-messages"),
  form: document.querySelector("#qq-send-form"),
  targetLabel: document.querySelector("#qq-send-target"),
  targetRelation: document.querySelector("#qq-send-relation"),
  input: document.querySelector("#qq-message-input"),
  send: document.querySelector("#qq-send-button"),
  attach: document.querySelector("#qq-attach-button"),
  mention: document.querySelector("#qq-mention-button"),
  fileInput: document.querySelector("#qq-file-input"),
  pending: document.querySelector("#qq-pending-attachments"),
  uploadProgress: document.querySelector("#qq-upload-progress"),
  uploadProgressText: document.querySelector("#qq-upload-progress-text"),
  uploadProgressBar: document.querySelector("#qq-upload-progress-bar"),
  uploadPause: document.querySelector("#qq-upload-pause"),
  uploadCancel: document.querySelector("#qq-upload-cancel"),
  quoteBar: document.querySelector("#qq-quote-bar"),
  quoteText: document.querySelector("#qq-quote-text"),
  quoteCancel: document.querySelector("#qq-quote-cancel"),
  hint: document.querySelector("#qq-compose-hint"),
  count: document.querySelector("#qq-message-count"),
  error: document.querySelector("#qq-error"),
  libraryDialog: document.querySelector("#qq-library-dialog"),
  libraryClose: document.querySelector("#qq-library-close"),
  libraryDone: document.querySelector("#qq-library-done"),
  libraryUpload: document.querySelector("#qq-library-upload"),
  librarySearch: document.querySelector("#qq-library-search"),
  libraryTabs: document.querySelector("#qq-library-tabs"),
  libraryGrid: document.querySelector("#qq-library-grid"),
  libraryCount: document.querySelector("#qq-library-count"),
  libraryMessage: document.querySelector("#qq-library-message"),
  libraryStorage: document.querySelector("#qq-library-storage"),
  libraryCleanup: document.querySelector("#qq-library-cleanup"),
  targetDialog: document.querySelector("#qq-target-dialog"),
  targetList: document.querySelector("#qq-target-list"),
  targetSend: document.querySelector("#qq-target-send"),
  targetMessage: document.querySelector("#qq-target-message"),
  searchDialog: document.querySelector("#qq-search-dialog"),
  globalQuery: document.querySelector("#qq-global-query"),
  globalType: document.querySelector("#qq-global-type"),
  globalFiles: document.querySelector("#qq-global-files"),
  globalRun: document.querySelector("#qq-global-run"),
  globalResults: document.querySelector("#qq-global-results"),
  globalMessage: document.querySelector("#qq-global-message"),
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
  openLiveEvents();
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
  const nextKey = `${kind}:${id}`;
  const previousKey = currentTarget ? `${currentTarget.kind}:${currentTarget.id}` : "";
  if (previousKey && previousKey !== nextKey && pendingAttachments.length) {
    pendingAttachments = [];
    renderPendingAttachments();
    updateComposer();
  }
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
  currentMessages = [];
  renderedHistoryTarget = "";
  clearQuote();
  clearMentions();
  pendingAttachments = [];
  renderPendingAttachments();
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
    && !uploadingCount
  );
  elements.input.disabled = !canSend;
  elements.attach.disabled = !canSend;
  elements.mention.disabled = !canSend || currentTarget?.kind !== "group";
  elements.send.disabled = !canSend || (!elements.input.value.trim() && !pendingAttachments.length);
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

function clearQuote() {
  quoteTarget = null;
  elements.quoteBar.hidden = true;
  elements.quoteText.textContent = "";
}

function clearMentions() {
  pendingMentions = [];
}

function addMention() {
  if (currentTarget?.kind !== "group") return;
  const choices = (currentMembers || [])
    .filter((item) => Number(item.user_id) > 0)
    .map((item) => `${item.user_id} ${item.display_name || ""}`.trim());
  if (!choices.length) {
    setError("当前没有可用的群成员列表");
    return;
  }
  const raw = window.prompt(`输入要 @ 的群成员 QQ 号\n${choices.slice(0, 30).join("\n")}`);
  const user_id = Number(String(raw || "").replace(/\D/g, ""));
  const member = currentMembers.find((item) => Number(item.user_id) === user_id);
  if (!member) {
    setError("没有找到这个群成员");
    return;
  }
  if (!pendingMentions.includes(user_id)) pendingMentions.push(user_id);
  setError("");
  updateComposer();
}

function useQuote(message) {
  if (!message?.message_id) return;
  quoteTarget = {
    message_id: String(message.message_id),
    content: visibleMessageContent(message.content) || "附件消息",
    sender_name: message.sender_name || "",
  };
  elements.quoteText.textContent = `${quoteTarget.sender_name ? `${quoteTarget.sender_name}：` : ""}${quoteTarget.content.slice(0, 90)}`;
  elements.quoteBar.hidden = false;
}

function messageActionButtons(message) {
  if (!message?.message_id) return "";
  const quote = `<button type="button" class="qq-message-action" data-quote-message="${esc(message.message_id)}">引用</button>`;
  const forward = `<button type="button" class="qq-message-action" data-forward-message="${esc(message.message_id)}">转发</button>`;
  const recall = message.role === "assistant"
    ? `<button type="button" class="qq-message-action danger" data-recall-message="${esc(message.message_id)}">撤回</button>`
    : "";
  return `<div class="qq-message-actions">${quote}${forward}${recall}</div>`;
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

function resolveQQMediaUrl(url) {
  const value = String(url || "");
  if (value.startsWith("/api/admin/") && currentInstanceId) {
    return `/instances/${currentInstanceId}${value}`;
  }
  return value;
}

function formatAttachmentSize(value) {
  let size = Number(value || 0);
  if (!Number.isFinite(size) || size <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 10 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

function attachmentsMarkup(message) {
  const attachments = Array.isArray(message.attachments) ? message.attachments : [];
  if (!attachments.length) return "";
  return `<div class="qq-message-attachments">${attachments.map((item) => {
    const type = String(item.type || "file");
    const name = String(item.name || attachmentTypeLabel(type));
    const url = resolveQQMediaUrl(item.url);
    if ((type === "image" || type === "sticker") && url) {
      return `
        <a class="qq-message-image-link ${type === "sticker" ? "sticker" : ""}" href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="打开原图">
          <img class="qq-message-image" src="${esc(url)}" alt="${esc(name)}" loading="lazy" referrerpolicy="no-referrer">
        </a>`;
    }
    if (type === "video" && url) {
      return `
        <video class="qq-message-video" src="${esc(url)}" controls preload="metadata" playsinline aria-label="${esc(name)}"></video>`;
    }
    if (type === "audio" && url) {
      return `<audio class="qq-message-audio" src="${esc(url)}" controls preload="metadata" aria-label="${esc(name)}"></audio>`;
    }
    const label = attachmentTypeIcon(type);
    const typeText = attachmentTypeLabel(type);
    const size = formatAttachmentSize(item.size);
    const body = `
      <span class="qq-attachment-icon">${label}</span>
      <span class="qq-attachment-copy">
        <strong>${esc(name)}</strong>
        <small>${esc(typeText)}${size ? ` · ${esc(size)}` : ""}</small>
      </span>`;
    return url
      ? `<a class="qq-attachment-card" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${body}</a>`
      : `<div class="qq-attachment-card unavailable">${body}</div>`;
  }).join("")}</div>`;
}

function attachmentTypeLabel(type) {
  if (type === "audio") return "语音";
  if (type === "video") return "视频";
  if (type === "sticker") return "表情包";
  if (type === "image") return "图片";
  return "文件";
}

function attachmentTypeIcon(type) {
  if (type === "audio") return "VOICE";
  if (type === "video") return "VIDEO";
  if (type === "sticker") return "STK";
  if (type === "image") return "IMG";
  return "FILE";
}

function renderPendingAttachments() {
  if (!elements.pending) return;
  elements.pending.hidden = !pendingAttachments.length;
  elements.pending.innerHTML = pendingAttachments.map((item) => {
    const type = String(item.type || "file");
    const url = resolveQQMediaUrl(item.url);
    const name = String(item.name || attachmentTypeLabel(type));
    const thumb = (type === "image" || type === "sticker") && url
      ? `<img class="qq-pending-thumb" src="${esc(url)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
      : `<span class="qq-pending-thumb">${attachmentTypeIcon(type)}</span>`;
    return `
      <div class="qq-pending-item" title="${esc(name)}">
        ${thumb}
        <span class="qq-pending-copy">
          <strong>${esc(name)}</strong>
          <small>${esc(item.source_label || attachmentTypeLabel(type))}${item.size ? ` · ${esc(formatAttachmentSize(item.size))}` : ""}</small>
        </span>
        <button class="qq-pending-remove" type="button" data-remove-attachment="${esc(item.library_id)}" aria-label="移除 ${esc(name)}">×</button>
      </div>`;
  }).join("");
  updateMessageCount();
}

function addPendingAttachment(item) {
  if (!item?.library_id || !item?.url) return false;
  if (pendingAttachments.some((entry) => entry.library_id === item.library_id)) return true;
  if (pendingAttachments.length >= 10) {
    setError("一条消息最多发送 10 个附件");
    return false;
  }
  pendingAttachments.push({...item});
  setError("");
  renderPendingAttachments();
  updateComposer();
  return true;
}

function removePendingAttachment(libraryId) {
  pendingAttachments = pendingAttachments.filter((item) => item.library_id !== libraryId);
  renderPendingAttachments();
  updateComposer();
}

function visibleMessageContent(content) {
  return String(content || "")
    .replace(/\[(?:图片|表情包|语音)\]/g, "")
    .replace(/\[文件:[^\]]*\]/g, "")
    .trim();
}

function messageMarkup(message) {
  const assistant = message.role === "assistant";
  const sender = assistant ? (message.sender_name || "机器人") : (message.sender_name || "群成员");
  const content = visibleMessageContent(message.content);
  return `
    <article class="qq-message ${assistant ? "assistant" : "user"}">
      <span class="qq-message-avatar">${initials(sender)}</span>
      <div class="qq-message-body">
        <div class="qq-message-sender">${esc(sender)}</div>
        ${content ? `<div class="qq-message-bubble">${esc(content)}</div>` : ""}
        ${attachmentsMarkup(message)}
        <div class="qq-message-time">${esc(formatTime(message.timestamp))}${messageActionButtons(message)}</div>
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
  const sameRenderedTarget = renderedHistoryTarget === targetSnapshot && currentMessages.length > 0;
  const previousScrollTop = elements.messages.scrollTop;
  const wasNearBottom = (
    elements.messages.scrollHeight - elements.messages.scrollTop - elements.messages.clientHeight
  ) < 80;
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
    currentMessages = messages;
    renderedHistoryTarget = targetSnapshot;
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
    if (!sameRenderedTarget || wasNearBottom) {
      elements.messages.scrollTop = elements.messages.scrollHeight;
    } else {
      const maxScrollTop = Math.max(0, elements.messages.scrollHeight - elements.messages.clientHeight);
      elements.messages.scrollTop = Math.min(previousScrollTop, maxScrollTop);
    }
  } catch (error) {
    renderedHistoryTarget = "";
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

function updateUploadProgress(text, value = 0) {
  const visible = uploadingCount > 0;
  elements.uploadProgress.hidden = !visible;
  if (!visible) return;
  elements.uploadProgressText.textContent = text;
  elements.uploadProgressBar.value = Math.max(0, Math.min(100, value));
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForUploadGate(token) {
  while (uploadPaused && !uploadCancelled && token === uploadRunToken) {
    await wait(150);
  }
  return !uploadCancelled && token === uploadRunToken;
}

async function uploadChunkWithRetry(instanceId, uploadId, index, blob, fileName, token) {
  let lastError = null;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    if (!(await waitForUploadGate(token))) throw new DOMException("上传已取消", "AbortError");
    uploadAbortController = new AbortController();
    try {
      return await api(
        `/instances/${instanceId}/api/admin/qq-web/uploads/${encodeURIComponent(uploadId)}/chunks/${index}`,
        {
          method: "POST",
          headers: {"Content-Type": "application/octet-stream"},
          body: blob,
          signal: uploadAbortController.signal,
        }
      );
    } catch (error) {
      if (uploadCancelled || error.name === "AbortError") {
        throw new DOMException("上传已取消", "AbortError");
      }
      lastError = error;
      if (attempt < 3) {
        elements.uploadProgressText.textContent = `${fileName} 上传中断，2 秒后重试（${attempt + 1}/3）`;
        await wait(2000 * (attempt + 1));
      }
    } finally {
      uploadAbortController = null;
    }
  }
  throw lastError || new Error("分片上传失败");
}

async function completeUploadWithRetry(instanceId, uploadId, fileName, token) {
  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (!(await waitForUploadGate(token))) throw new DOMException("上传已取消", "AbortError");
    try {
      return await api(
        `/instances/${instanceId}/api/admin/qq-web/uploads/${encodeURIComponent(uploadId)}/complete`,
        {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: "{}",
        }
      );
    } catch (error) {
      lastError = error;
      if (attempt < 2) {
        elements.uploadProgressText.textContent = `${fileName} 正在完成上传，稍后重试`;
        await wait(1500 * (attempt + 1));
      }
    }
  }
  throw lastError || new Error("完成上传失败");
}

async function uploadOneFile(file, token) {
  const instanceId = currentInstanceId;
  const init = await api(`/instances/${instanceId}/api/admin/qq-web/uploads/init`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      name: file.name,
      size: file.size,
      mime_type: file.type || "application/octet-stream",
    }),
  });
  if (uploadCancelled || token !== uploadRunToken) {
    throw new DOMException("上传已取消", "AbortError");
  }
  activeUploadId = init.upload_id;
  const chunkSize = Number(init.chunk_size) || (4 * 1024 * 1024);
  const totalChunks = Number(init.total_chunks) || 1;
  for (let index = 0; index < totalChunks; index += 1) {
    if (!(await waitForUploadGate(token))) throw new DOMException("上传已取消", "AbortError");
    const start = index * chunkSize;
    const blob = file.slice(start, Math.min(file.size, start + chunkSize));
    await uploadChunkWithRetry(instanceId, init.upload_id, index, blob, file.name, token);
    currentFileProgress = totalChunks ? (index + 1) / totalChunks : 1;
    const completedUnits = uploadQueueDone + currentFileProgress;
    updateUploadProgress(
      `正在上传 ${file.name} · ${uploadQueueDone + 1}/${uploadQueueTotal}`,
      uploadQueueTotal ? (completedUnits / uploadQueueTotal) * 100 : 0,
    );
  }
  const completed = await completeUploadWithRetry(instanceId, init.upload_id, file.name, token);
  activeUploadId = "";
  return completed.item;
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []).filter(Boolean);
  if (!files.length || !currentTarget?.can_send || !currentInstanceId || !instanceOnline()) {
    if (!currentTarget?.can_send) setError("请先选择一个可以发送消息的好友或群聊");
    return;
  }
  const targetKey = `${currentTarget.kind}:${currentTarget.id}`;
  const slots = Math.max(0, 10 - pendingAttachments.length);
  const accepted = files.slice(0, slots);
  if (files.length > accepted.length) {
    setError("一条消息最多发送 10 个附件，多出的文件已忽略");
  }
  if (!accepted.length) return;
  uploadingCount = accepted.length;
  uploadQueueTotal = accepted.length;
  uploadQueueDone = 0;
  currentFileProgress = 0;
  uploadFailures = 0;
  uploadPaused = false;
  uploadCancelled = false;
  uploadRunToken += 1;
  const runToken = uploadRunToken;
  elements.uploadPause.textContent = "暂停";
  elements.uploadPause.disabled = false;
  elements.uploadCancel.disabled = false;
  updateComposer();
  renderPendingAttachments();
  updateUploadProgress(`准备上传 · 0/${uploadQueueTotal}`, 0);
  try {
    for (const file of accepted) {
      if (uploadCancelled || runToken !== uploadRunToken) break;
      if (`${currentTarget?.kind}:${currentTarget?.id}` !== targetKey) {
        setError("发送对象已切换，上传完成的文件只保存在素材库，未加入当前消息");
        break;
      }
      try {
        const item = await uploadOneFile(file, runToken);
        if (`${currentTarget?.kind}:${currentTarget?.id}` === targetKey) {
          addPendingAttachment(item);
        }
        if (elements.libraryDialog.open) await loadLibrary(false);
      } catch (error) {
        if (error.name !== "AbortError" && !uploadCancelled) {
          uploadFailures += 1;
          setError(`${file.name} 上传失败：${error.message}`);
        }
      } finally {
        uploadQueueDone += 1;
        currentFileProgress = 0;
      }
    }
  } finally {
    const wasCancelled = uploadCancelled;
    if (activeUploadId && currentInstanceId && wasCancelled) {
      try {
        await api(
          `/instances/${currentInstanceId}/api/admin/qq-web/uploads/${encodeURIComponent(activeUploadId)}`,
          {method: "DELETE"}
        );
      } catch {}
    }
    activeUploadId = "";
    uploadingCount = 0;
    uploadQueueTotal = 0;
    uploadQueueDone = 0;
    currentFileProgress = 0;
    updateUploadProgress("", 0);
    elements.uploadProgress.hidden = true;
    uploadPaused = false;
    uploadCancelled = false;
    uploadAbortController = null;
    elements.uploadPause.textContent = "暂停";
    elements.uploadPause.disabled = false;
    elements.uploadCancel.disabled = false;
    updateComposer();
    renderPendingAttachments();
    if (wasCancelled) setError("已取消本次上传；已完成的分片记录已清理。");
    else if (uploadFailures) setError(`本次有 ${uploadFailures} 个文件未上传成功，可重新选择文件重试。`);
  }
}

function toggleUploadPause() {
  if (!uploadingCount) return;
  uploadPaused = !uploadPaused;
  elements.uploadPause.textContent = uploadPaused ? "继续" : "暂停";
  if (uploadPaused) {
    elements.uploadProgressText.textContent = "将在当前分片完成后暂停";
  } else {
    elements.uploadProgressText.textContent = "继续上传…";
  }
}

async function cancelUploadQueue() {
  if (!uploadingCount) return;
  if (!window.confirm("取消当前上传队列？已上传的临时分片会被清理。")) return;
  uploadCancelled = true;
  uploadPaused = false;
  uploadRunToken += 1;
  elements.uploadPause.disabled = true;
  elements.uploadCancel.disabled = true;
  elements.uploadProgressText.textContent = "正在取消上传…";
  uploadAbortController?.abort();
}

function chooseLocalFiles() {
  if (!currentTarget?.can_send || !instanceOnline() || sending || uploadingCount) return;
  elements.fileInput.click();
}

function libraryItemMatches(item) {
  const query = elements.librarySearch.value.trim().toLowerCase();
  const searchable = [
    item.name,
    item.category,
    item.summary,
    item.source_label,
    item.type,
    item.mime_type,
  ].join(" ").toLowerCase();
  if (query && !searchable.includes(query)) return false;
  if (libraryFilter === "all") return true;
  if (libraryFilter === "upload" || libraryFilter === "generated" || libraryFilter === "sticker") {
    return item.source === libraryFilter;
  }
  if (libraryFilter === "image") return item.type === "image";
  if (libraryFilter === "video") return item.type === "video";
  if (libraryFilter === "file") return item.type === "file";
  return true;
}

function libraryPreviewMarkup(item) {
  const type = String(item.type || "file");
  const url = resolveQQMediaUrl(item.url);
  if ((type === "image" || type === "sticker") && url) {
    return `<img src="${esc(url)}" alt="" loading="lazy" referrerpolicy="no-referrer">`;
  }
  if (type === "video" && url) {
    return `<video src="${esc(url)}" muted preload="metadata" playsinline></video>`;
  }
  return `<span class="qq-library-kind">${attachmentTypeIcon(type)}</span>`;
}

function libraryCardMarkup(item) {
  const selected = pendingAttachments.some((entry) => entry.library_id === item.library_id);
  const name = String(item.name || attachmentTypeLabel(item.type));
  const size = formatAttachmentSize(item.size);
  const meta = [item.source_label || attachmentTypeLabel(item.type), size].filter(Boolean).join(" · ");
  const deleteButton = item.source === "upload"
    ? `<button class="qq-library-delete" type="button" data-delete-library="${esc(item.library_id)}" title="从站内素材库删除">删除</button>`
    : "";
  const previewButton = `
    <button class="qq-library-preview-button" type="button" data-preview-library="${esc(item.library_id)}">
      查看内容
    </button>`;
  return `
    <article class="qq-library-card ${selected ? "selected" : ""}">
      <button class="qq-library-select" type="button" data-select-library="${esc(item.library_id)}" title="${esc(name)}">
        <span class="qq-library-preview">${libraryPreviewMarkup(item)}</span>
        <span class="qq-library-card-copy">
          <strong>${esc(name)}</strong>
          <small>${esc(meta)}</small>
        </span>
      </button>
      ${previewButton}
      ${deleteButton}
    </article>`;
}

function renderLibrary() {
  if (libraryLoading) {
    elements.libraryGrid.innerHTML = `<div class="qq-library-empty">正在读取素材库…</div>`;
    return;
  }
  const visible = libraryItems.filter(libraryItemMatches);
  elements.libraryGrid.innerHTML = visible.length
    ? visible.map(libraryCardMarkup).join("")
    : `<div class="qq-library-empty">这个分类里还没有可用素材。可以上传本机文件，或先在机器人里收藏表情包。</div>`;
  elements.libraryCount.textContent = `已选 ${pendingAttachments.length} 个 · 显示 ${visible.length}/${libraryItems.length}`;
}

async function loadLibrary(showLoading = true) {
  if (!currentInstanceId || libraryLoading) return;
  libraryLoading = true;
  elements.libraryMessage.textContent = "";
  if (showLoading) renderLibrary();
  try {
    const data = await api(`/instances/${currentInstanceId}/api/admin/qq-web/library?limit=500`);
    libraryItems = Array.isArray(data.items) ? data.items : [];
    if (data.storage && elements.libraryStorage) {
      const storage = data.storage;
      elements.libraryStorage.textContent = `本地 ${formatAttachmentSize(storage.total_bytes)} · 可用 ${formatAttachmentSize(storage.disk_free_bytes)}`;
    }
  } catch (error) {
    libraryItems = [];
    elements.libraryMessage.textContent = error.message || "素材库读取失败";
  } finally {
    libraryLoading = false;
    renderLibrary();
  }
}

async function openLibrary() {
  if (!currentTarget?.can_send) {
    setError("请先选择一个可以发送消息的好友或群聊");
    return;
  }
  if (typeof elements.libraryDialog.showModal === "function") {
    elements.libraryDialog.showModal();
  } else {
    elements.libraryDialog.setAttribute("open", "");
  }
  await loadLibrary(true);
}

function closeLibrary() {
  if (typeof elements.libraryDialog.close === "function") elements.libraryDialog.close();
  else elements.libraryDialog.removeAttribute("open");
}

async function previewLibraryItem(item) {
  if (!item || !currentInstanceId) return;
  elements.libraryMessage.textContent = "正在解析预览…";
  try {
    const data = await api(`/instances/${currentInstanceId}/api/admin/qq-web/library/${encodeURIComponent(item.library_id)}/preview`);
    const preview = data.preview || {};
    const text = preview.excerpt || preview.message || "该文件暂不支持网页内容预览，请下载查看。";
    elements.libraryMessage.textContent = `${item.name}：${String(text).slice(0, 1200)}`;
  } catch (error) {
    elements.libraryMessage.textContent = error.message;
  }
}

async function cleanupLibraryStorage() {
  if (!currentInstanceId || !window.confirm("清理未被索引的孤立附件文件？收藏素材不会被删除。")) return;
  try {
    const data = await api(`/instances/${currentInstanceId}/api/admin/qq-web/storage/cleanup`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({orphaned: true}),
    });
    elements.libraryMessage.textContent = `已清理 ${data.orphan_deleted || 0} 个孤立文件`;
    await loadLibrary(false);
  } catch (error) {
    elements.libraryMessage.textContent = error.message;
  }
}

async function deleteLibraryUpload(item) {
  if (item?.source !== "upload") return;
  if (!window.confirm(`从站内素材库永久删除“${item.name}”？已经发送到 QQ 的消息不受影响。`)) return;
  try {
    await api(`/instances/${currentInstanceId}/api/admin/qq-web/media-library/${item.id}`, {
      method: "DELETE",
    });
    removePendingAttachment(item.library_id);
    await loadLibrary(false);
  } catch (error) {
    elements.libraryMessage.textContent = error.message || "删除失败";
  }
}

async function sendMessage(event) {
  event?.preventDefault();
  if (sending || uploadingCount || !currentTarget?.can_send) return;
  const text = elements.input.value.trim();
  const attachments = pendingAttachments.map((item) => item.library_id);
  const replyTo = quoteTarget?.message_id || "";
  if (!text && !attachments.length) return;
  sending = true;
  setError("");
  updateComposer();
  elements.send.textContent = "发送中…";
  try {
    const data = attachments.length
      ? await api(`/instances/${currentInstanceId}/api/admin/qq-web/send-attachments`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            target_type: currentTarget.target_type,
            target_id: currentTarget.id,
            message: text,
            attachments,
            reply_to: replyTo,
            mentions: pendingMentions,
          }),
        })
      : await api(`/api/instances/${currentInstanceId}/qq-web/send`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            target_type: currentTarget.target_type,
            target_id: currentTarget.id,
            message: text,
            reply_to: replyTo,
            mentions: pendingMentions,
          }),
        });
    elements.input.value = "";
    pendingAttachments = [];
    clearQuote();
    clearMentions();
    renderPendingAttachments();
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
    if (elements.libraryDialog.open && libraryItems.length && !pendingAttachments.length) renderLibrary();
  }
}

function autoResizeInput() {
  elements.input.style.height = "auto";
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 180)}px`;
  updateMessageCount();
  updateComposer();
}

function updateMessageCount() {
  const attachmentText = pendingAttachments.length ? ` · ${pendingAttachments.length} 个附件` : "";
  elements.count.textContent = `${elements.input.value.length} / 20000${attachmentText}`;
}

async function switchInstance(instanceId) {
  if (sending || uploadingCount) {
    elements.instance.value = String(currentInstanceId);
    return;
  }
  currentInstanceId = Number(instanceId) || null;
  if (currentInstanceId) rememberInstance(currentInstanceId);
  clearTarget();
  updateInstanceUi();
  await loadContacts();
  openLiveEvents();
}

function closeLiveEvents() {
  if (liveEvents) {
    liveEvents.close();
    liveEvents = null;
  }
  if (elements.liveState) elements.liveState.textContent = "实时同步未连接";
}

function openLiveEvents() {
  closeLiveEvents();
  if (!currentInstanceId || typeof EventSource === "undefined") return;
  const instanceId = currentInstanceId;
  liveEvents = new EventSource(`/instances/${instanceId}/api/admin/qq-web/events`);
  liveEvents.onopen = () => {
    if (elements.liveState) elements.liveState.textContent = "实时同步已连接";
  };
  liveEvents.onerror = () => {
    if (elements.liveState) elements.liveState.textContent = "实时同步重连中";
  };
  liveEvents.addEventListener("message", (event) => {
    let payload = null;
    try { payload = JSON.parse(event.data || "{}"); } catch { return; }
    if (!currentTarget || !payload.target_type || !payload.target_id) return;
    const sameTarget = String(payload.target_type) === currentTarget.target_type
      && Number(payload.target_id) === Number(currentTarget.id);
    if (sameTarget) loadHistory(false).catch(() => {});
  });
  liveEvents.addEventListener("recall", (event) => {
    let payload = null;
    try { payload = JSON.parse(event.data || "{}"); } catch { return; }
    if (currentTarget && String(payload.target_type) === currentTarget.target_type
      && Number(payload.target_id) === Number(currentTarget.id)) {
      loadHistory(false).catch(() => {});
    }
  });
}

function globalResultMarkup(item) {
  const type = item.message_type === "group" ? "群聊" : "私聊";
  const label = item.target_label || item.sender_name || item.target_id || "";
  const targetKind = item.message_type === "group" ? "group" : "friend";
  const targetId = item.message_type === "group" ? item.group_id : item.target_id;
  return `<article class="qq-global-result">
    <div><span class="qq-result-type">${type}</span><strong>${esc(label)}</strong>
    <small>${esc(formatTime(item.event_time, true))} · ${esc(item.sender_name || "")}</small></div>
    <p>${esc(item.snippet || item.content || "")}</p>
    <button type="button" data-open-result-kind="${esc(targetKind)}" data-open-result-id="${esc(targetId)}">打开会话</button>
  </article>`;
}

async function runGlobalSearch() {
  if (!currentInstanceId) return;
  elements.globalMessage.textContent = "";
  elements.globalResults.innerHTML = "<div class=qq-global-empty>正在搜索…</div>";
  const params = new URLSearchParams({
    query: elements.globalQuery.value.trim(),
    limit: "100",
  });
  if (elements.globalType.value) params.set("message_type", elements.globalType.value);
  if (elements.globalFiles.checked) params.set("file_only", "true");
  try {
    const data = await api(`/instances/${currentInstanceId}/api/admin/qq-web/search?${params}`);
    const messages = data.messages || [];
    elements.globalResults.innerHTML = messages.length
      ? messages.map(globalResultMarkup).join("")
      : "<div class=qq-global-empty>没有找到匹配消息</div>";
    if (!messages.length) elements.globalMessage.textContent = "可以换关键词，或取消附件过滤。";
  } catch (error) {
    elements.globalResults.innerHTML = "";
    elements.globalMessage.textContent = error.message;
  }
}

function closeTargetDialog() {
  if (elements.targetDialog.open) elements.targetDialog.close();
  elements.targetMessage.textContent = "";
}

function targetOptionMarkup() {
  const options = [];
  for (const item of contacts?.friends || []) {
    options.push(`<label><input type="checkbox" value="private:${item.user_id}" ${targetSelection.has(`private:${item.user_id}`) ? "checked" : ""}> <span>好友</span> ${esc(item.display_name)} <small>${esc(item.user_id)}</small></label>`);
  }
  for (const item of contacts?.groups || []) {
    options.push(`<label><input type="checkbox" value="group:${item.group_id}" ${targetSelection.has(`group:${item.group_id}`) ? "checked" : ""}> <span>群聊</span> ${esc(item.display_name)} <small>${esc(item.group_id)}</small></label>`);
  }
  return options.join("") || "<div class=qq-global-empty>没有可发送目标</div>";
}

function openTargetDialog(mode = "send") {
  if (!currentTarget?.can_send && mode === "send") {
    setError("请先选择可发送的好友或群聊");
    return;
  }
  targetSelection = new Set();
  if (currentTarget?.can_send) targetSelection.add(`${currentTarget.target_type === "group" ? "group" : "private"}:${currentTarget.id}`);
  elements.targetList.innerHTML = targetOptionMarkup();
  elements.targetMessage.textContent = mode === "forward" ? "转发模式：将把选中消息发送到多个目标。" : "";
  elements.targetList.querySelectorAll("input").forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) targetSelection.add(input.value); else targetSelection.delete(input.value);
    });
  });
  elements.targetSend.dataset.mode = mode;
  if (typeof elements.targetDialog.showModal === "function") elements.targetDialog.showModal();
  else elements.targetDialog.setAttribute("open", "");
}

async function sendToTargets() {
  const mode = elements.targetSend.dataset.mode || "send";
  const targets = Array.from(targetSelection).map((value) => {
    const [target_type, id] = value.split(":");
    return {target_type, target_id: Number(id)};
  });
  if (!targets.length) { elements.targetMessage.textContent = "至少选择一个目标"; return; }
  elements.targetSend.disabled = true;
  elements.targetMessage.textContent = "正在逐项发送…";
  try {
    let data;
    if (mode === "forward" && forwardMessages.length) {
      data = await api(`/instances/${currentInstanceId}/api/admin/qq-web/forward`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({targets, messages: forwardMessages}),
      });
    } else {
      data = await api(`/instances/${currentInstanceId}/api/admin/qq-web/send-batch`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          targets,
          message: elements.input.value.trim(),
          attachments: pendingAttachments.map((item) => item.library_id),
          reply_to: quoteTarget?.message_id || "",
          mentions: pendingMentions,
        }),
      });
    }
    const failed = (data.results || []).filter((item) => !item.ok);
    elements.targetMessage.textContent = failed.length
      ? `完成 ${data.success_count || 0} 个，失败 ${failed.length} 个：${failed.map((item) => item.error).filter(Boolean).join("；")}`
      : `已发送到 ${data.success_count || targets.length} 个目标`;
    if (!failed.length) {
      elements.input.value = "";
      pendingAttachments = [];
      forwardMessages = [];
      renderPendingAttachments();
      renderLibrary();
      clearQuote();
      clearMentions();
      setTimeout(closeTargetDialog, 900);
      loadHistory(false).catch(() => {});
    }
  } catch (error) {
    elements.targetMessage.textContent = error.message;
  } finally {
    elements.targetSend.disabled = false;
  }
}

async function recallMessage(messageId) {
  if (!messageId || !currentTarget) return;
  if (!window.confirm("确认撤回这条 QQ 消息？")) return;
  try {
    await api(`/instances/${currentInstanceId}/api/admin/qq-web/recall`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        message_id: messageId,
        target_type: currentTarget.target_type,
        target_id: currentTarget.id,
      }),
    });
    await loadHistory(false);
  } catch (error) {
    setError(error.message);
  }
}

elements.instance.addEventListener("change", () => {
  switchInstance(elements.instance.value).catch((error) => setError(error.message));
});
document.querySelector("#qq-refresh-contacts").addEventListener("click", () => {
  loadContacts().catch((error) => setError(error.message));
});
elements.search.addEventListener("input", renderContacts);
elements.form.addEventListener("submit", sendMessage);
elements.mention.addEventListener("click", addMention);
elements.quoteCancel.addEventListener("click", clearQuote);
elements.searchButton.addEventListener("click", () => {
  if (!currentInstanceId) return;
  if (typeof elements.searchDialog.showModal === "function") elements.searchDialog.showModal();
  else elements.searchDialog.setAttribute("open", "");
  elements.globalQuery.focus();
});
elements.multiButton.addEventListener("click", () => openTargetDialog("send"));
elements.targetSend.addEventListener("click", () => sendToTargets().catch((error) => {
  elements.targetMessage.textContent = error.message;
}));
document.querySelectorAll("[data-close-target]").forEach((button) => button.addEventListener("click", closeTargetDialog));
document.querySelectorAll("[data-close-search]").forEach((button) => button.addEventListener("click", () => elements.searchDialog.close()));
elements.globalRun.addEventListener("click", () => runGlobalSearch().catch((error) => setError(error.message)));
elements.globalQuery.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); runGlobalSearch().catch((error) => setError(error.message)); }
});
elements.globalResults.addEventListener("click", (event) => {
  const button = event.target.closest("[data-open-result-kind]");
  if (!button) return;
  const kind = button.dataset.openResultKind;
  const id = Number(button.dataset.openResultId);
  elements.searchDialog.close();
  selectContact(kind, id).catch((error) => setError(error.message));
});
elements.messages.addEventListener("click", (event) => {
  const quoteButton = event.target.closest("[data-quote-message]");
  const forwardButton = event.target.closest("[data-forward-message]");
  const recallButton = event.target.closest("[data-recall-message]");
  const id = quoteButton?.dataset.quoteMessage || forwardButton?.dataset.forwardMessage || recallButton?.dataset.recallMessage;
  if (!id) return;
  const message = currentMessages.find((item) => String(item.message_id) === String(id));
  if (!message) return;
  if (quoteButton) useQuote(message);
  if (forwardButton) { forwardMessages = [message]; openTargetDialog("forward"); }
  if (recallButton) recallMessage(id).catch((error) => setError(error.message));
});
elements.attach.addEventListener("click", () => {
  openLibrary().catch((error) => setError(error.message));
});
elements.pending.addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-attachment]");
  if (!button) return;
  removePendingAttachment(button.dataset.removeAttachment);
});
elements.fileInput.addEventListener("change", () => {
  const files = Array.from(elements.fileInput.files || []);
  elements.fileInput.value = "";
  uploadFiles(files).catch((error) => setError(error.message));
});
elements.uploadPause.addEventListener("click", toggleUploadPause);
elements.uploadCancel.addEventListener("click", () => {
  cancelUploadQueue().catch((error) => setError(error.message));
});
elements.form.addEventListener("dragover", (event) => {
  if (!event.dataTransfer?.types?.includes("Files")) return;
  event.preventDefault();
  elements.form.classList.add("dragging");
});
elements.form.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget || !elements.form.contains(event.relatedTarget)) {
    elements.form.classList.remove("dragging");
  }
});
elements.form.addEventListener("drop", (event) => {
  if (!event.dataTransfer?.files?.length) return;
  event.preventDefault();
  elements.form.classList.remove("dragging");
  uploadFiles(event.dataTransfer.files).catch((error) => setError(error.message));
});
elements.form.addEventListener("paste", (event) => {
  const files = Array.from(event.clipboardData?.files || []);
  if (!files.length) return;
  event.preventDefault();
  uploadFiles(files).catch((error) => setError(error.message));
});
elements.libraryUpload.addEventListener("click", chooseLocalFiles);
elements.libraryCleanup.addEventListener("click", () => {
  cleanupLibraryStorage().catch((error) => { elements.libraryMessage.textContent = error.message; });
});
elements.libraryClose.addEventListener("click", closeLibrary);
elements.libraryDone.addEventListener("click", closeLibrary);
elements.libraryDialog.addEventListener("click", (event) => {
  if (event.target === elements.libraryDialog) closeLibrary();
});
elements.librarySearch.addEventListener("input", renderLibrary);
elements.libraryTabs.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  libraryFilter = button.dataset.filter || "all";
  for (const tab of elements.libraryTabs.querySelectorAll("button[data-filter]")) {
    tab.classList.toggle("active", tab === button);
  }
  renderLibrary();
});
elements.libraryGrid.addEventListener("click", (event) => {
  const deleteButton = event.target.closest("[data-delete-library]");
  if (deleteButton) {
    const item = libraryItems.find((entry) => entry.library_id === deleteButton.dataset.deleteLibrary);
    deleteLibraryUpload(item).catch((error) => {
      elements.libraryMessage.textContent = error.message;
    });
    return;
  }
  const previewButton = event.target.closest("[data-preview-library]");
  if (previewButton) {
    const item = libraryItems.find((entry) => entry.library_id === previewButton.dataset.previewLibrary);
    previewLibraryItem(item).catch((error) => { elements.libraryMessage.textContent = error.message; });
    return;
  }
  const selectButton = event.target.closest("[data-select-library]");
  if (!selectButton) return;
  const item = libraryItems.find((entry) => entry.library_id === selectButton.dataset.selectLibrary);
  if (item) {
    addPendingAttachment(item);
    renderLibrary();
  }
});
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
  if (document.hidden || sending || uploadingCount || loadingHistory || !currentTarget?.can_send) return;
  loadHistory(false).catch(() => {});
}, 15000);
window.addEventListener("beforeunload", (event) => {
  clearInterval(pollTimer);
  if (uploadingCount) {
    event.preventDefault();
    event.returnValue = "";
  }
});

autoResizeInput();
renderPendingAttachments();
Promise.all([loadMe(), loadInstances()])
  .catch((error) => {
    elements.messages.innerHTML = "";
    setError(error.message || "QQ 消息控制台加载失败");
  });
