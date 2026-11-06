let csrf = "";
let providers = [];
let instances = [];
let groupDetails = {knowledge: [], members: [], relationships: []};
let groupFiles = [];

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (csrf && options.method && options.method !== "GET") headers["X-CSRF-Token"] = csrf;
  const response = await fetch(path, {...options, headers});
  if (response.status === 401) {
    location.href = "/login";
    throw new Error("登录已失效");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[char]));
}

function openPanel(id) { document.querySelector(`#${id}`).hidden = false; }
function closePanel(id) { document.querySelector(`#${id}`).hidden = true; }
document.querySelectorAll("[data-open]").forEach((button) => button.addEventListener("click", () => openPanel(button.dataset.open)));
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => closePanel(button.dataset.close)));

async function loadMe() {
  const me = await api("/api/auth/me");
  csrf = me.csrf_token;
  document.querySelector("#user-label").textContent = me.username;
}

async function loadProviders() {
  const data = await api("/api/providers");
  providers = data.providers;
  document.querySelector("#provider-count").textContent = providers.length;
  document.querySelector("#providers").innerHTML = providers.length ? providers.map((item) => `
    <article class="provider-item">
      <div class="item-head"><b>${esc(item.name)}</b><span class="status online"><i></i>${esc(item.provider_type)}</span></div>
      <div class="item-meta">${esc(item.base_url)}<br>${esc(item.model)} · ${item.api_key_configured ? "KEY 已加密保存" : "无 KEY"}</div>
      <div class="item-actions"><button data-edit-provider="${item.id}">编辑</button><button data-delete-provider="${item.id}">删除</button></div>
    </article>`).join("") : `<div class="empty">还没有 API 供应商</div>`;
  document.querySelectorAll("[data-edit-provider]").forEach((button) => button.addEventListener("click", () => editProvider(Number(button.dataset.editProvider))));
  document.querySelectorAll("[data-delete-provider]").forEach((button) => button.addEventListener("click", () => deleteProvider(Number(button.dataset.deleteProvider))));
  const select = document.querySelector("#instance-provider");
  select.innerHTML = providers.map((item) => `<option value="${item.id}">${esc(item.name)} · ${esc(item.model)}</option>`).join("");
}

async function loadInstances() {
  const data = await api("/api/instances");
  instances = data.instances;
  const adminTarget = instances.find((item) => item.status === "online")
    || instances[0];
  const topAdminLink = document.querySelector("#top-admin-link");
  if (adminTarget) {
    topAdminLink.href = `/api/instances/${adminTarget.id}/open-admin`;
    topAdminLink.textContent = `打开完整后台 · ${adminTarget.name}`;
    topAdminLink.hidden = false;
  } else {
    topAdminLink.hidden = true;
    topAdminLink.removeAttribute("href");
  }
  document.querySelector("#instance-count").textContent = instances.length;
  document.querySelector("#online-count").textContent = instances.filter((item) => item.status === "online").length;
  document.querySelector("#instances").innerHTML = instances.length ? instances.map((item) => `
    <article class="instance-item">
      <div class="item-head"><b>${esc(item.name)}</b><span class="status ${item.status === "online" ? "online" : ""}"><i></i>${esc(item.status)}</span></div>
      <div class="item-meta">127.0.0.1:${item.port} · ${esc(item.provider_name || "未知供应商")}<br>${esc(item.onebot_api_base)}</div>
      <div class="item-actions">
        ${item.status === "online" ? `<button data-stop="${item.id}">停止</button><button data-restart="${item.id}">重启</button><button data-memory="${item.id}">记忆维护</button><button data-unified="${item.id}">内容管理</button><button data-group-settings="${item.id}">群聊设置</button><button data-evolution="${item.id}">自适应与状态</button><button data-chat-test="${item.id}">对话测试</button><button data-traces="${item.id}">消息追踪</button><button data-model-routes="${item.id}">模型路由</button>` : `<button data-start="${item.id}">启动</button>`}
        <a href="/api/instances/${item.id}/open-admin" target="_blank">打开完整后台</a>
        <button data-edit-instance="${item.id}">编辑</button><button data-delete-instance="${item.id}">删除</button>
      </div>
    </article>`).join("") : `<div class="empty">还没有机器人实例</div>`;
  bindInstanceActions();
}

function bindInstanceActions() {
  document.querySelectorAll("[data-start]").forEach((b) => b.onclick = () => instanceAction(b.dataset.start, "start"));
  document.querySelectorAll("[data-stop]").forEach((b) => b.onclick = () => instanceAction(b.dataset.stop, "stop"));
  document.querySelectorAll("[data-restart]").forEach((b) => b.onclick = () => instanceAction(b.dataset.restart, "restart"));
  document.querySelectorAll("[data-memory]").forEach((b) => b.onclick = () => openMemory(Number(b.dataset.memory)));
  document.querySelectorAll("[data-unified]").forEach((b) => b.onclick = () => openUnified(Number(b.dataset.unified)));
  document.querySelectorAll("[data-group-settings]").forEach((b) => b.onclick = () => openGroupSettings(Number(b.dataset.groupSettings)));
  document.querySelectorAll("[data-evolution]").forEach((b) => b.onclick = () => openEvolution(Number(b.dataset.evolution)));
  document.querySelectorAll("[data-chat-test]").forEach((b) => b.onclick = () => openChatTest(Number(b.dataset.chatTest)));
  document.querySelectorAll("[data-traces]").forEach((b) => b.onclick = () => openTraces(Number(b.dataset.traces)));
  document.querySelectorAll("[data-model-routes]").forEach((b) => b.onclick = () => openModelRoutes(Number(b.dataset.modelRoutes)));
  document.querySelectorAll("[data-edit-instance]").forEach((b) => b.onclick = () => editInstance(Number(b.dataset.editInstance)));
  document.querySelectorAll("[data-delete-instance]").forEach((b) => b.onclick = () => deleteInstance(Number(b.dataset.deleteInstance)));
}

async function instanceAction(id, action) {
  try {
    await api(`/api/instances/${id}/${action}`, {method:"POST"});
    await loadInstances();
  } catch (error) { alert(error.message); }
}

function editProvider(id) {
  const item = providers.find((value) => value.id === id);
  if (!item) return;
  document.querySelector("#provider-id").value = item.id;
  document.querySelector("#provider-name").value = item.name;
  document.querySelector("#provider-type").value = item.provider_type;
  document.querySelector("#provider-base").value = item.base_url;
  document.querySelector("#provider-model").value = item.model;
  document.querySelector("#provider-key").value = "";
  openPanel("provider-form");
}

async function deleteProvider(id) {
  if (!confirm("删除这个 API 供应商？")) return;
  try { await api(`/api/providers/${id}`, {method:"DELETE"}); await loadProviders(); }
  catch (error) { alert(error.message); }
}

function editInstance(id) {
  const item = instances.find((value) => value.id === id);
  if (!item) return;
  document.querySelector("#instance-id").value = item.id;
  document.querySelector("#instance-name").value = item.name;
  document.querySelector("#instance-port").value = item.port;
  document.querySelector("#instance-onebot").value = item.onebot_api_base;
  document.querySelector("#instance-token").value = "";
  document.querySelector("#instance-provider").value = item.provider_id;
  document.querySelector("#instance-db").value = item.database_path;
  openPanel("instance-form");
}

async function deleteInstance(id) {
  if (!confirm("删除实例并停止对应进程？")) return;
  try { await api(`/api/instances/${id}`, {method:"DELETE"}); await loadInstances(); }
  catch (error) { alert(error.message); }
}

document.querySelector("#provider-editor").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.querySelector("#provider-error");
  error.textContent = "";
  const id = Number(document.querySelector("#provider-id").value) || null;
  try {
    await api("/api/providers", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        id, name:document.querySelector("#provider-name").value.trim(),
        provider_type:document.querySelector("#provider-type").value,
        base_url:document.querySelector("#provider-base").value.trim(),
        model:document.querySelector("#provider-model").value.trim(),
        api_key:document.querySelector("#provider-key").value
      })
    });
    closePanel("provider-form");
    document.querySelector("#provider-editor").reset();
    document.querySelector("#provider-id").value = "";
    await loadProviders();
  } catch (err) { error.textContent = err.message; }
});

document.querySelector("#instance-editor").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.querySelector("#instance-error");
  error.textContent = "";
  const id = Number(document.querySelector("#instance-id").value) || null;
  try {
    await api("/api/instances", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        id, name:document.querySelector("#instance-name").value.trim(),
        port:Number(document.querySelector("#instance-port").value),
        onebot_api_base:document.querySelector("#instance-onebot").value.trim(),
        onebot_token:document.querySelector("#instance-token").value,
        provider_id:Number(document.querySelector("#instance-provider").value),
        database_path:document.querySelector("#instance-db").value.trim()
      })
    });
    closePanel("instance-form");
    document.querySelector("#instance-editor").reset();
    document.querySelector("#instance-id").value = "";
    await loadInstances();
  } catch (err) { error.textContent = err.message; }
});

document.querySelector("#logout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", {method:"POST"});
  location.href = "/login";
});

document.querySelector("#import-current").addEventListener("click", async () => {
  try {
    await api("/api/import-current", {method:"POST"});
    await Promise.all([loadProviders(), loadInstances()]);
  } catch (error) { alert(error.message); }
});

async function openMemory(instanceId) {
  const select = document.querySelector("#memory-instance");
  select.innerHTML = instances.map((item) => `<option value="${item.id}">${esc(item.name)} · :${item.port}</option>`).join("");
  select.value = String(instanceId);
  document.querySelector("#memory-person").innerHTML = `<option>加载中…</option>`;
  document.querySelector("#memory-list").innerHTML = "";
  document.querySelector("#memory-error").textContent = "";
  openPanel("memory-panel");
  await loadMemoryPeople(instanceId);
}

async function loadMemoryPeople(instanceId) {
  const personSelect = document.querySelector("#memory-person");
  const error = document.querySelector("#memory-error");
  error.textContent = "";
  try {
    const data = await api(`/api/instances/${instanceId}/people`);
    if (!data.people.length) {
      personSelect.innerHTML = `<option value="">暂无联系人</option>`;
      document.querySelector("#memory-list").innerHTML = `<div class="empty">这个实例还没有联系人记忆。</div>`;
      return;
    }
    personSelect.innerHTML = data.people.map((item) => `<option value="${item.user_id}">${esc(item.display_name)} · QQ ${item.user_id}</option>`).join("");
    await loadMemoryPerson(instanceId, Number(data.people[0].user_id));
  } catch (err) {
    error.textContent = err.message;
  }
}

async function loadMemoryPerson(instanceId, personId) {
  const list = document.querySelector("#memory-list");
  const error = document.querySelector("#memory-error");
  error.textContent = "";
  try {
    const data = await api(`/api/instances/${instanceId}/people/${personId}`);
    if (!data.memories.length) {
      list.innerHTML = `<div class="empty">这个联系人没有长期记忆。</div>`;
      return;
    }
    list.innerHTML = data.memories.map((item) => `
      <article class="memory-row">
        <div><span class="tag">${esc(item.visibility)}</span><p>${esc(item.content)}</p></div>
        <button data-delete-memory="${item.id}" data-person="${personId}" data-instance="${instanceId}">删除</button>
      </article>`).join("");
    document.querySelectorAll("[data-delete-memory]").forEach((button) => {
      button.onclick = () => deleteMemory(
        Number(button.dataset.instance),
        Number(button.dataset.person),
        Number(button.dataset.deleteMemory)
      );
    });
  } catch (err) {
    error.textContent = err.message;
  }
}

async function deleteMemory(instanceId, personId, memoryId) {
  if (!confirm("删除这条长期记忆？删除后模型不会再使用它。")) return;
  try {
    await api(`/api/instances/${instanceId}/people/${personId}/memories/${memoryId}`, {method:"DELETE"});
    await loadMemoryPerson(instanceId, personId);
  } catch (error) {
    document.querySelector("#memory-error").textContent = error.message;
  }
}

document.querySelector("#memory-instance").addEventListener("change", async (event) => {
  if (event.target.value) await loadMemoryPeople(Number(event.target.value));
});
document.querySelector("#memory-person").addEventListener("change", async (event) => {
  const instanceId = Number(document.querySelector("#memory-instance").value);
  if (instanceId && event.target.value) await loadMemoryPerson(instanceId, Number(event.target.value));
});

document.querySelector("#purge-person-memory").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#memory-instance").value);
  const personSelect = document.querySelector("#memory-person");
  const personId = Number(personSelect.value);
  const personName = personSelect.selectedOptions[0]?.textContent || String(personId);
  const error = document.querySelector("#memory-error");
  const result = document.querySelector("#memory-result");
  error.textContent = "";
  result.textContent = "";
  if (!instanceId || !personId) {
    error.textContent = "请选择实例和联系人。";
    return;
  }
  if (!confirm(
    `确定删除“${personName}”的全部记忆、画像、对话片段和相关反思吗？\n\n笔记、待办和提醒不会删除。此操作不可撤销。`
  )) return;
  try {
    const data = await api(`/api/instances/${instanceId}/people/${personId}/all-memory`, {method:"DELETE"});
    const deleted = data.deleted;
    result.textContent = `已删除 ${deleted.memories} 条长期记忆、${deleted.episodes} 条对话片段、${deleted.messages} 条短期消息。`;
    await loadMemoryPeople(instanceId);
  } catch (err) { error.textContent = err.message; }
});

document.querySelector("#purge-all-memory").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#memory-instance").value);
  const error = document.querySelector("#memory-error");
  const result = document.querySelector("#memory-result");
  error.textContent = "";
  result.textContent = "";
  if (!instanceId) {
    error.textContent = "请选择机器人实例。";
    return;
  }
  if (!confirm(
    "确定清空这个实例所有联系人和全部记忆吗？\n\n将删除联系人、长期记忆、对话片段、观察、画像、反思、成长笔记、冲突、短期聊天和记忆向量。\n\n笔记、待办、提醒、群聊设置和知识库文件不会删除。此操作不可撤销。"
  )) return;
  try {
    const data = await api(`/api/instances/${instanceId}/memory/all`, {method:"DELETE"});
    const deleted = data.deleted;
    result.textContent = `已清空 ${deleted.people} 个联系人、${deleted.memories} 条长期记忆、${deleted.episodes} 条对话片段和 ${deleted.messages} 条短期消息。`;
    document.querySelector("#memory-person").innerHTML = `<option value="">暂无联系人</option>`;
    document.querySelector("#memory-list").innerHTML = `<div class="empty">这个实例的记忆已经清空。</div>`;
  } catch (err) { error.textContent = err.message; }
});

async function openGroupSettings(instanceId) {
  const select = document.querySelector("#settings-instance");
  select.innerHTML = instances
    .filter((item) => item.status === "online")
    .map((item) => `<option value="${item.id}">${esc(item.name)} · :${item.port}</option>`)
    .join("");
  select.value = String(instanceId);
  document.querySelector("#settings-error").textContent = "";
  document.querySelector("#settings-result").textContent = "";
  openPanel("group-settings-panel");
  await loadGroupSettings(instanceId);
}

async function openEvolution(instanceId) {
  const select = document.querySelector("#evolution-instance");
  select.innerHTML = instances
    .filter((item) => item.status === "online")
    .map((item) => `<option value="${item.id}">${esc(item.name)} · :${item.port}</option>`)
    .join("");
  select.value = String(instanceId);
  document.querySelector("#evolution-error").textContent = "";
  openPanel("evolution-panel");
  await loadEvolution(instanceId);
}

async function loadEvolution(instanceId) {
  const [adaptive, presenceData] = await Promise.all([
    api(`/api/instances/${instanceId}/adaptive`),
    api(`/api/instances/${instanceId}/presence`),
  ]);
  const global = presenceData.global || {};
  document.querySelector("#evolution-busy").checked = Boolean(global.busy);
  document.querySelector("#evolution-reason").value = global.reason || "";
  document.querySelector("#evolution-quiet-start").value = global.quiet_start || "";
  document.querySelector("#evolution-quiet-end").value = global.quiet_end || "";
  document.querySelector("#evolution-presence").textContent =
    `当前${global.busy ? "忙碌" : global.in_quiet_hours ? "处于勿扰时段" : "可聊天"}，`
    + `未接消息 ${presenceData.backlog?.length || 0} 条。`;
  document.querySelector("#evolution-rollouts").innerHTML = adaptive.rollouts?.length
    ? adaptive.rollouts.map((item) => `
      <div class="row"><strong>${esc(item.scope_key)}</strong>
      <span class="tag">${esc(item.challenger_name)} ${Math.round(Number(item.weight) * 100)}%</span>
      <div class="row-meta">胜 ${esc(item.wins)} / 负 ${esc(item.losses)} · 样本 ${esc(item.samples)}</div></div>`).join("")
    : `<div class="empty">还没有达到灰度阈值的策略</div>`;
}

async function openUnified(instanceId) {
  const select = document.querySelector("#unified-instance");
  select.innerHTML = instances
    .filter((item) => item.status === "online")
    .map((item) => `<option value="${item.id}">${esc(item.name)} · :${item.port}</option>`)
    .join("");
  select.value = String(instanceId);
  document.querySelector("#unified-error").textContent = "";
  openPanel("unified-panel");
  await loadUnified(instanceId);
}

async function loadUnified(instanceId) {
  const [qualityData, stickerData, knowledgeData, auditData] = await Promise.all([
    api(`/api/instances/${instanceId}/quality`),
    api(`/api/instances/${instanceId}/stickers`),
    api(`/api/instances/${instanceId}/knowledge`),
    api("/api/audit"),
  ]);
  document.querySelector("#unified-quality-list").innerHTML =
    qualityData.recent?.length
      ? qualityData.recent.slice(0, 30).map((item) => `
        <article class="source-row">
          <div><span class="tag">${item.score == null ? "待评测" : esc(item.score)}</span>
          ${esc(item.user_text)}</div>
          <div class="row-meta">${esc(item.assistant_text)}</div>
          ${item.review_notes ? `<div class="row-meta">建议：${esc(item.review_notes)}</div>` : ""}
          ${item.fact_status && item.fact_status !== "not_checked"
            ? `<div class="row-meta">事实状态：${esc(item.fact_status)}</div>` : ""}
          ${(item.sources || []).map((source) =>
            `<div class="row-meta">来源：<a href="${esc(source.url)}" target="_blank" rel="noreferrer">${esc(source.title)}</a></div>`
          ).join("")}
        </article>`).join("")
      : `<div class="empty">暂时没有质量记录</div>`;
  document.querySelector("#unified-sticker-list").innerHTML =
    stickerData.stickers?.length
      ? stickerData.stickers.map((item) => `
        <article class="source-row">
          <div><strong>${esc(item.summary)}</strong>
          <span class="tag">${esc(item.category)}</span></div>
          <div class="row-meta">${esc(item.usage_hint || "未填写使用场景")}</div>
          <div class="item-actions">
            <button data-sticker-toggle="${item.id}" data-enabled="${item.enabled ? "1" : "0"}">
              ${item.enabled ? "停用" : "启用"}
            </button>
            <button class="danger" data-sticker-delete="${item.id}">删除</button>
          </div>
        </article>`).join("")
      : `<div class="empty">表情库为空</div>`;
  document.querySelector("#unified-knowledge-list").innerHTML =
    knowledgeData.sources?.length
      ? knowledgeData.sources.map((item) => `
        <article class="source-row">
          <div><strong>${esc(item.title || item.source_id)}</strong>
          <span class="tag">${esc(item.namespace || "知识")}</span></div>
          <div class="row-meta">${esc(item.location || "")}</div>
          <button class="danger" data-knowledge-delete="${esc(item.source_id)}"
            data-knowledge-namespace="${esc(item.namespace || "document")}">删除来源</button>
        </article>`).join("")
      : `<div class="empty">知识库为空</div>`;
  document.querySelector("#audit-list").innerHTML =
    auditData.events?.length
      ? auditData.events.slice(0, 80).map((item) => `
        <div class="row"><strong>${esc(item.method)} ${esc(item.path)}</strong>
        <span class="tag">${esc(item.status_code)}</span>
        <div class="row-meta">${esc(item.username || "未登录")} ·
        ${esc(item.client_ip || "本地")} · ${esc(item.created_at)}</div></div>`).join("")
      : `<div class="empty">暂无审计记录</div>`;
  document.querySelectorAll("[data-sticker-toggle]").forEach((button) => {
    button.onclick = async () => {
      await api(`/api/instances/${instanceId}/stickers/${button.dataset.stickerToggle}`, {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({enabled: button.dataset.enabled !== "1"}),
      });
      await loadUnified(instanceId);
    };
  });
  document.querySelectorAll("[data-sticker-delete]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm("删除这个表情包记录？")) return;
      await api(`/api/instances/${instanceId}/stickers/${button.dataset.stickerDelete}`, {method:"DELETE"});
      await loadUnified(instanceId);
    };
  });
  document.querySelectorAll("[data-knowledge-delete]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm("删除这个知识来源及其向量片段？")) return;
      await api(`/api/instances/${instanceId}/knowledge/delete`, {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          namespace:button.dataset.knowledgeNamespace,
          source_id:button.dataset.knowledgeDelete,
        }),
      });
      await loadUnified(instanceId);
    };
  });
}

async function openChatTest(instanceId) {
  const select = document.querySelector("#test-instance");
  select.innerHTML = instances
    .filter((item) => item.status === "online")
    .map((item) => `<option value="${item.id}">${esc(item.name)} · :${item.port}</option>`)
    .join("");
  select.value = String(instanceId);
  document.querySelector("#test-error").textContent = "";
  document.querySelector("#test-result").hidden = true;
  openPanel("chat-test-panel");
}

async function openTraces(instanceId) {
  const select = document.querySelector("#trace-instance");
  select.innerHTML = instances
    .filter((item) => item.status === "online")
    .map((item) => `<option value="${item.id}">${esc(item.name)} · :${item.port}</option>`)
    .join("");
  select.value = String(instanceId);
  document.querySelector("#trace-error").textContent = "";
  openPanel("trace-panel");
  await loadTraces(instanceId);
}

async function loadTraces(instanceId) {
  const skipped = document.querySelector("#trace-skipped-only").checked;
  const data = await api(
    `/api/instances/${instanceId}/traces?limit=150&skipped_only=${skipped ? "true" : "false"}`
  );
  document.querySelector("#trace-list").innerHTML = data.traces?.length
    ? data.traces.map((item) => `
      <article class="source-row">
        <div><span class="tag">${esc(item.status)}</span>
        <strong>${esc(item.user_text || "[无文本]")}</strong></div>
        <div class="row-meta">${esc(item.reason || item.decision)}
        · ${item.latency_ms == null ? "-" : esc(item.latency_ms) + " ms"}</div>
        ${item.reply ? `<div class="row-meta">回复：${esc(item.reply)}</div>` : ""}
        <div class="row-meta">${esc(item.created_at)} · ${esc(item.trace_id)}</div>
      </article>`).join("")
    : `<div class="empty">还没有消息追踪记录</div>`;
}

async function loadGroupSettings(instanceId) {
  const error = document.querySelector("#settings-error");
  error.textContent = "";
  try {
    const data = await api(`/api/instances/${instanceId}/group-settings`);
    const settings = data.settings;
    document.querySelector("#settings-group-enabled").checked = Boolean(settings.enable_group);
    document.querySelector("#settings-require-mention").checked = Boolean(settings.group_require_mention);
    document.querySelector("#settings-smart").checked = Boolean(settings.group_smart_participation);
    document.querySelector("#settings-learning").checked = Boolean(settings.group_behavior_learning);
    document.querySelector("#settings-quote").checked = Boolean(settings.quote_replies);
    document.querySelector("#settings-quote-mention").checked = Boolean(settings.group_quote_counts_as_mention);
    document.querySelector("#settings-natural-pacing").checked = Boolean(settings.natural_pacing_enabled);
    document.querySelector("#settings-natural-split").checked = Boolean(settings.natural_split_replies);
    document.querySelector("#settings-confidence").value = settings.group_min_reply_confidence;
    document.querySelector("#settings-cooldown").value = settings.group_reply_cooldown_seconds;
    document.querySelector("#settings-model").value = settings.group_participation_model || "";
    document.querySelector("#settings-allowlist").value = settings.group_allowlist || "";
    document.querySelector("#settings-prefixes").value = settings.group_trigger_prefixes || "";
    document.querySelector("#settings-delay-min").value = settings.natural_reply_delay_min_ms ?? 200;
    document.querySelector("#settings-delay-max").value = settings.natural_reply_delay_max_ms ?? 900;
    document.querySelector("#settings-debounce").value = settings.natural_message_debounce_ms ?? 0;
    document.querySelector("#settings-sticker-cooldown").value = settings.auto_sticker_cooldown_seconds ?? 180;
    await loadGroupProfiles(instanceId);
  } catch (err) {
    error.textContent = err.message;
  }
}

async function loadGroupProfiles(instanceId) {
  const data = await api(`/api/instances/${instanceId}/group-profiles`);
  const select = document.querySelector("#settings-profile-select");
  const previous = select.value;
  select.dataset.instance = String(instanceId);
  select.innerHTML = data.groups.length
    ? data.groups.map((item) => `<option value="${item.group_id}">${esc(item.name || `群 ${item.group_id}`)} · ${item.group_id}</option>`).join("")
    : `<option value="">暂无群聊记录</option>`;
  if (previous && data.groups.some((item) => String(item.group_id) === previous)) {
    select.value = previous;
  }
  if (select.value) renderGroupProfile(data.groups, Number(select.value));
  if (select.value) await loadGroupDetails(instanceId, Number(select.value));
}

function renderGroupProfile(profiles, groupId) {
  const item = profiles.find((profile) => Number(profile.group_id) === groupId);
  if (!item) return;
  document.querySelector("#profile-name").value = item.name || "";
  document.querySelector("#profile-enabled").checked = Boolean(item.enabled);
  document.querySelector("#profile-require-mention").checked = Boolean(item.require_mention);
  document.querySelector("#profile-smart").checked = Boolean(item.smart_participation);
  document.querySelector("#profile-learning").checked = Boolean(item.behavior_learning);
  document.querySelector("#profile-quote").checked = Boolean(item.quote_replies);
  document.querySelector("#profile-quote-mention").checked = Boolean(item.quote_counts_as_mention);
  document.querySelector("#profile-sticker").checked = Boolean(item.auto_sticker_enabled);
  document.querySelector("#profile-avoid-last").checked = Boolean(item.avoid_last_word);
  document.querySelector("#profile-confidence").value = item.min_reply_confidence;
  document.querySelector("#profile-cooldown").value = item.reply_cooldown_seconds;
  document.querySelector("#profile-hourly-limit").value = item.hourly_reply_limit;
  document.querySelector("#profile-model").value = item.participation_model || "";
  document.querySelector("#profile-prefixes").value = item.trigger_prefixes || "";
  document.querySelector("#profile-poke-policy").value = item.poke_policy || "allow";
  document.querySelector("#profile-quote-mode").value = item.quote_mode || "smart";
  document.querySelector("#profile-active-hours").value = item.active_hours || "";
  document.querySelector("#profile-blocked-keywords").value = item.blocked_keywords || "";
  document.querySelector("#profile-delay-min").value = item.reply_delay_min_ms ?? 200;
  document.querySelector("#profile-delay-max").value = item.reply_delay_max_ms ?? 900;
  document.querySelector("#profile-digest-hours").value = item.announcement_digest_hours ?? 24;
  document.querySelector("#profile-welcome-message").value = item.welcome_message || "";
  document.querySelector("#profile-custom-prompt").value = item.custom_prompt || "";
}

document.querySelector("#settings-profile-select").addEventListener("change", async (event) => {
  const instanceId = Number(document.querySelector("#settings-instance").value);
  if (!instanceId || !event.target.value) return;
  const data = await api(`/api/instances/${instanceId}/group-profiles`);
  renderGroupProfile(data.groups, Number(event.target.value));
  await loadGroupDetails(instanceId, Number(event.target.value));
});

async function loadGroupDetails(instanceId, groupId) {
  const data = await api(`/api/instances/${instanceId}/group-profiles/${groupId}/details`);
  groupDetails = data;
  groupFiles = [];
  renderGroupKnowledge();
  renderGroupMembers();
  renderGroupRelationships();
  renderGroupFiles();
}

function renderGroupKnowledge() {
  const list = document.querySelector("#group-knowledge-list");
  list.innerHTML = groupDetails.knowledge.length
    ? groupDetails.knowledge.map((item) => `
      <article class="source-row">
        <div><span class="tag">${esc(item.kind)}</span> <strong>${esc(item.title)}</strong></div>
        <div class="row-meta">${esc(item.content)}</div>
        <button data-delete-group-knowledge="${item.id}">删除</button>
      </article>`).join("")
    : `<div class="empty">暂无群资料</div>`;
  document.querySelectorAll("[data-delete-group-knowledge]").forEach((button) => {
    button.onclick = async () => {
      const instanceId = Number(document.querySelector("#settings-instance").value);
      const groupId = Number(document.querySelector("#settings-profile-select").value);
      await api(`/api/instances/${instanceId}/group-profiles/${groupId}/knowledge/${button.dataset.deleteGroupKnowledge}`, {method:"DELETE"});
      await loadGroupDetails(instanceId, groupId);
    };
  });
}

function renderGroupMembers() {
  const list = document.querySelector("#group-member-list");
  list.innerHTML = groupDetails.members.length
    ? groupDetails.members.map((item) => `
      <div class="row"><strong>${esc(item.nickname || item.user_id)}</strong> · QQ ${esc(item.user_id)}
      ${item.role ? `<span class="tag">${esc(item.role)}</span>` : ""}
      ${item.no_jokes ? `<span class="tag">不要开玩笑</span>` : ""}
      <div class="row-meta">${esc(item.notes || "无额外偏好")}</div></div>`).join("")
    : `<div class="empty">暂无成员偏好</div>`;
}

function renderGroupRelationships() {
  const relationships = groupDetails.relationships || [];
  document.querySelector("#group-relationship-list").innerHTML = relationships.length
    ? relationships.map((item) => `
      <div class="row"><strong>${esc(item.name_a || item.user_a)}</strong> ↔
      <strong>${esc(item.name_b || item.user_b)}</strong>
      <div class="row-meta">提及 ${esc(item.mention_count)} ·
      回复 ${esc(item.reply_count)} · 戳 ${esc(item.poke_count)}</div></div>`).join("")
    : `<div class="empty">还没有足够的成员互动记录</div>`;
}

function renderGroupFiles() {
  const list = document.querySelector("#group-file-list");
  list.innerHTML = groupFiles.length
    ? groupFiles.map((item) => `
      <article class="source-row">
        <div><span class="tag">${item.kind === "folder" ? "文件夹" : "文件"}</span>
        <strong>${esc(item.name)}</strong></div>
        <div class="row-meta">${item.kind === "folder"
          ? `ID: ${esc(item.folder_id)} · ${esc(item.file_count || 0)} 项`
          : `${esc(item.size || 0)} 字节 · 上传者 ${esc(item.uploader || "未知")}`}</div>
      </article>`).join("")
    : `<div class="empty">尚未读取群文件</div>`;
}

document.querySelector("#profile-add-group").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#settings-instance").value);
  const groupId = Number(document.querySelector("#profile-new-group-id").value);
  const error = document.querySelector("#profile-error");
  error.textContent = "";
  if (!instanceId || !Number.isInteger(groupId) || groupId <= 0) {
    error.textContent = "请选择实例并填写有效群号。";
    return;
  }
  try {
    await api(`/api/instances/${instanceId}/group-profiles`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({group_id:groupId, name:document.querySelector("#profile-new-group-name").value.trim()}),
    });
    document.querySelector("#profile-new-group-id").value = "";
    document.querySelector("#profile-new-group-name").value = "";
    await loadGroupProfiles(instanceId);
    document.querySelector("#settings-profile-select").value = String(groupId);
  } catch (err) { error.textContent = err.message; }
});

document.querySelector("#save-group-profile").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#settings-instance").value);
  const groupId = Number(document.querySelector("#settings-profile-select").value);
  const error = document.querySelector("#profile-error");
  const result = document.querySelector("#profile-result");
  error.textContent = "";
  result.textContent = "";
  if (!instanceId || !groupId) {
    error.textContent = "请选择实例和群聊。";
    return;
  }
  try {
    await api(`/api/instances/${instanceId}/group-profiles/${groupId}`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        name:document.querySelector("#profile-name").value.trim(),
        enabled:document.querySelector("#profile-enabled").checked,
        require_mention:document.querySelector("#profile-require-mention").checked,
        smart_participation:document.querySelector("#profile-smart").checked,
        behavior_learning:document.querySelector("#profile-learning").checked,
        quote_replies:document.querySelector("#profile-quote").checked,
        quote_counts_as_mention:document.querySelector("#profile-quote-mention").checked,
        auto_sticker_enabled:document.querySelector("#profile-sticker").checked,
        avoid_last_word:document.querySelector("#profile-avoid-last").checked,
        min_reply_confidence:Number(document.querySelector("#profile-confidence").value),
        reply_cooldown_seconds:Number(document.querySelector("#profile-cooldown").value),
        hourly_reply_limit:Number(document.querySelector("#profile-hourly-limit").value),
        participation_model:document.querySelector("#profile-model").value.trim(),
        trigger_prefixes:document.querySelector("#profile-prefixes").value.trim(),
        poke_policy:document.querySelector("#profile-poke-policy").value,
        quote_mode:document.querySelector("#profile-quote-mode").value,
        active_hours:document.querySelector("#profile-active-hours").value.trim(),
        blocked_keywords:document.querySelector("#profile-blocked-keywords").value.trim(),
        reply_delay_min_ms:Number(document.querySelector("#profile-delay-min").value),
        reply_delay_max_ms:Number(document.querySelector("#profile-delay-max").value),
        announcement_digest_hours:Number(document.querySelector("#profile-digest-hours").value),
        welcome_message:document.querySelector("#profile-welcome-message").value.trim(),
        custom_prompt:document.querySelector("#profile-custom-prompt").value.trim(),
      }),
    });
    result.textContent = "本群设置已保存并立即生效。";
    await loadGroupDetails(instanceId, groupId);
  } catch (err) { error.textContent = err.message; }
});

document.querySelector("#add-group-knowledge").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#settings-instance").value);
  const groupId = Number(document.querySelector("#settings-profile-select").value);
  if (!instanceId || !groupId) return;
  try {
    await api(`/api/instances/${instanceId}/group-profiles/${groupId}/knowledge`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        kind:document.querySelector("#group-knowledge-kind").value,
        title:document.querySelector("#group-knowledge-title").value.trim(),
        content:document.querySelector("#group-knowledge-content").value.trim(),
      }),
    });
    document.querySelector("#group-knowledge-title").value = "";
    document.querySelector("#group-knowledge-content").value = "";
    await loadGroupDetails(instanceId, groupId);
  } catch (err) {
    document.querySelector("#profile-error").textContent = err.message;
  }
});

document.querySelector("#save-group-member").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#settings-instance").value);
  const groupId = Number(document.querySelector("#settings-profile-select").value);
  const memberId = Number(document.querySelector("#group-member-id").value);
  if (!instanceId || !groupId || !memberId) return;
  try {
    await api(`/api/instances/${instanceId}/group-profiles/${groupId}/members`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        user_id:memberId,
        nickname:document.querySelector("#group-member-name").value.trim(),
        role:document.querySelector("#group-member-role").value.trim(),
        notes:document.querySelector("#group-member-notes").value.trim(),
        no_jokes:document.querySelector("#group-member-no-jokes").checked,
      }),
    });
    document.querySelector("#group-member-id").value = "";
    document.querySelector("#group-member-name").value = "";
    document.querySelector("#group-member-role").value = "";
    document.querySelector("#group-member-notes").value = "";
    document.querySelector("#group-member-no-jokes").checked = false;
    await loadGroupDetails(instanceId, groupId);
  } catch (err) {
    document.querySelector("#profile-error").textContent = err.message;
  }
});

document.querySelector("#load-group-files").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#settings-instance").value);
  const groupId = Number(document.querySelector("#settings-profile-select").value);
  if (!instanceId || !groupId) return;
  const button = document.querySelector("#load-group-files");
  button.disabled = true;
  button.textContent = "读取中…";
  try {
    const folderId = document.querySelector("#group-folder-id").value.trim();
    const suffix = folderId ? `?folder_id=${encodeURIComponent(folderId)}` : "";
    const data = await api(`/api/instances/${instanceId}/group-profiles/${groupId}/files${suffix}`);
    groupFiles = [
      ...(data.folders || []).map((item) => ({...item, kind:"folder"})),
      ...(data.files || []).map((item) => ({...item, kind:"file"})),
    ];
    renderGroupFiles();
  } catch (err) {
    document.querySelector("#profile-error").textContent = err.message;
  } finally {
    button.disabled = false;
    button.textContent = "读取群文件";
  }
});

document.querySelector("#reset-group-profile").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#settings-instance").value);
  const groupId = Number(document.querySelector("#settings-profile-select").value);
  if (!instanceId || !groupId || !confirm("恢复此群的默认设置并清空行为学习？")) return;
  try {
    await api(`/api/instances/${instanceId}/group-profiles/${groupId}/reset`, {method:"POST"});
    await loadGroupProfiles(instanceId);
    document.querySelector("#profile-result").textContent = "已恢复默认设置。";
  } catch (err) {
    document.querySelector("#profile-error").textContent = err.message;
  }
});

document.querySelector("#settings-instance").addEventListener("change", async (event) => {
  if (event.target.value) await loadGroupSettings(Number(event.target.value));
});

document.querySelector("#save-settings").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#settings-instance").value);
  const button = document.querySelector("#save-settings");
  const error = document.querySelector("#settings-error");
  const result = document.querySelector("#settings-result");
  error.textContent = "";
  result.textContent = "";
  if (!instanceId) {
    error.textContent = "请先选择一个在线实例。";
    return;
  }
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const data = await api(`/api/instances/${instanceId}/group-settings`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        enable_group:document.querySelector("#settings-group-enabled").checked,
        group_require_mention:document.querySelector("#settings-require-mention").checked,
        group_smart_participation:document.querySelector("#settings-smart").checked,
        group_behavior_learning:document.querySelector("#settings-learning").checked,
        quote_replies:document.querySelector("#settings-quote").checked,
        group_quote_counts_as_mention:document.querySelector("#settings-quote-mention").checked,
        natural_pacing_enabled:document.querySelector("#settings-natural-pacing").checked,
        natural_split_replies:document.querySelector("#settings-natural-split").checked,
        natural_reply_delay_min_ms:Number(document.querySelector("#settings-delay-min").value),
        natural_reply_delay_max_ms:Number(document.querySelector("#settings-delay-max").value),
        natural_message_debounce_ms:Number(document.querySelector("#settings-debounce").value),
        auto_sticker_cooldown_seconds:Number(document.querySelector("#settings-sticker-cooldown").value),
        group_min_reply_confidence:Number(document.querySelector("#settings-confidence").value),
        group_reply_cooldown_seconds:Number(document.querySelector("#settings-cooldown").value),
        group_participation_model:document.querySelector("#settings-model").value.trim(),
        group_allowlist:document.querySelector("#settings-allowlist").value.trim(),
        group_trigger_prefixes:document.querySelector("#settings-prefixes").value.trim(),
      }),
    });
    result.textContent = "设置已保存并立即生效，重启实例后仍保留。";
  } catch (err) {
    error.textContent = err.message;
  } finally {
    button.disabled = false;
    button.textContent = "保存群聊设置";
  }
});

const routeCapabilities = [
  ["chat", "聊天"],
  ["code", "代码"],
  ["vision", "视觉"],
  ["ocr", "OCR"],
  ["audio", "语音识别"],
  ["tts", "语音合成"],
  ["embedding", "向量"],
  ["reflection", "反思"],
];

async function openModelRoutes(instanceId) {
  const select = document.querySelector("#routes-instance");
  select.innerHTML = instances.map((item) => `<option value="${item.id}">${esc(item.name)} · :${item.port}</option>`).join("");
  select.value = String(instanceId);
  document.querySelector("#routes-error").textContent = "";
  document.querySelector("#routes-result").textContent = "";
  openPanel("model-routes-panel");
  await loadModelRoutes(instanceId);
}

async function loadModelRoutes(instanceId) {
  const data = await api(`/api/instances/${instanceId}/model-routes`);
  const byCapability = new Map(data.routes.map((item) => [item.capability, item]));
  const firstProvider = providers[0];
  document.querySelector("#model-route-editor").innerHTML = routeCapabilities.map(([capability, label]) => {
    const route = byCapability.get(capability);
    const providerId = route?.primary_provider_id || firstProvider?.id || "";
    const provider = providers.find((item) => item.id === Number(providerId));
    const model = route?.primary_model || provider?.model || "";
    const fallbackText = (route?.fallbacks || []).map((item) => `${item.provider_id}:${item.model}`).join(",");
    return `<section class="route-row" data-capability="${capability}">
      <h3>${label}</h3>
      <div class="route-fields">
        <label>主供应商<select data-route-provider>${providers.map((item) => `<option value="${item.id}" ${item.id === Number(providerId) ? "selected" : ""}>${esc(item.name)}</option>`).join("")}</select></label>
        <label>主模型<input data-route-model value="${esc(model)}"></label>
        <label>备用链<input data-route-fallbacks value="${esc(fallbackText)}" placeholder="供应商ID:模型,供应商ID:模型"></label>
      </div>
      <div class="item-meta">可用供应商：${providers.map((item) => `ID ${item.id} = ${esc(item.name)}`).join("；")}</div>
    </section>`;
  }).join("");
}

document.querySelector("#routes-instance").addEventListener("change", async (event) => {
  if (event.target.value) await loadModelRoutes(Number(event.target.value));
});

document.querySelector("#save-model-routes").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#routes-instance").value);
  const error = document.querySelector("#routes-error");
  const result = document.querySelector("#routes-result");
  error.textContent = "";
  result.textContent = "";
  try {
    const routes = [...document.querySelectorAll(".route-row")].map((row) => {
      const fallbackText = row.querySelector("[data-route-fallbacks]").value.trim();
      const fallbacks = fallbackText
        ? fallbackText.split(",").filter(Boolean).map((item) => {
          const [providerId, ...modelParts] = item.split(":");
          return {provider_id:Number(providerId), model:modelParts.join(":").trim()};
        })
        : [];
      return {
        capability: row.dataset.capability,
        primary_provider_id: Number(row.querySelector("[data-route-provider]").value),
        primary_model: row.querySelector("[data-route-model]").value.trim(),
        fallbacks,
      };
    });
    await api(`/api/instances/${instanceId}/model-routes`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({routes}),
    });
    result.textContent = "路由已保存；重启实例后生效。";
  } catch (err) {
    error.textContent = err.message;
  }
});

document.querySelector("#create-backup").addEventListener("click", async () => {
  const error = document.querySelector("#backup-error");
  const result = document.querySelector("#backup-result");
  error.textContent = "";
  result.textContent = "";
  const passphrase = document.querySelector("#backup-passphrase").value;
  if (passphrase.length < 8) {
    error.textContent = "备份口令至少需要 8 个字符。";
    return;
  }
  const button = document.querySelector("#create-backup");
  button.disabled = true;
  button.textContent = "正在打包…";
  try {
    const response = await fetch("/api/backup", {
      method:"POST",
      headers:{"Content-Type":"application/json", "X-CSRF-Token":csrf},
      body:JSON.stringify({passphrase}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/);
    const name = match ? match[1] : "qq-bot-backup.qqbackup";
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    URL.revokeObjectURL(url);
    result.textContent = `已生成 ${name}，请把备份文件和口令分开保管。`;
  } catch (err) {
    error.textContent = err.message;
  } finally {
    button.disabled = false;
    button.textContent = "下载加密备份";
  }
});

document.querySelector("#restore-backup").addEventListener("click", async () => {
  const error = document.querySelector("#backup-error");
  const result = document.querySelector("#backup-result");
  error.textContent = "";
  result.textContent = "";
  const file = document.querySelector("#restore-file").files[0];
  const passphrase = document.querySelector("#restore-passphrase").value;
  if (!file) {
    error.textContent = "请选择备份文件。";
    return;
  }
  if (passphrase.length < 8) {
    error.textContent = "恢复口令至少需要 8 个字符。";
    return;
  }
  if (!confirm("恢复会停止当前全部实例并覆盖数据库、.env 和控制台密钥。确定继续？")) return;
  const button = document.querySelector("#restore-backup");
  button.disabled = true;
  button.textContent = "正在恢复…";
  try {
    const contentBase64 = await fileToBase64(file);
    const data = await api("/api/restore", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({passphrase, content_base64:contentBase64}),
    });
    result.textContent = data.note;
    alert(`恢复完成。原数据安全副本位于：\n${data.safety_directory}\n\n请重新登录控制台。`);
    setTimeout(() => { location.href = "/login"; }, 800);
  } catch (err) {
    error.textContent = err.message;
  } finally {
    button.disabled = false;
    button.textContent = "停止实例并恢复";
  }
});

document.querySelector("#evolution-instance").addEventListener("change", (event) => {
  loadEvolution(Number(event.target.value)).catch((err) => {
    document.querySelector("#evolution-error").textContent = err.message;
  });
});

document.querySelector("#unified-instance").addEventListener("change", (event) => {
  loadUnified(Number(event.target.value)).catch((err) => {
    document.querySelector("#unified-error").textContent = err.message;
  });
});

document.querySelector("#review-instance-quality").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#unified-instance").value);
  const error = document.querySelector("#unified-error");
  error.textContent = "";
  if (!instanceId) return;
  try {
    await api(`/api/instances/${instanceId}/quality/review`, {method:"POST"});
    await loadUnified(instanceId);
  } catch (err) { error.textContent = err.message; }
});

document.querySelector("#organize-instance-stickers").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#unified-instance").value);
  const error = document.querySelector("#unified-error");
  error.textContent = "";
  if (!instanceId) return;
  try {
    await api(`/api/instances/${instanceId}/stickers/organize`, {method:"POST"});
    await loadUnified(instanceId);
  } catch (err) { error.textContent = err.message; }
});

document.querySelector("#refresh-instance-knowledge").addEventListener("click", () => {
  const instanceId = Number(document.querySelector("#unified-instance").value);
  if (instanceId) loadUnified(instanceId).catch((err) => {
    document.querySelector("#unified-error").textContent = err.message;
  });
});

document.querySelector("#refresh-audit").addEventListener("click", () => {
  const instanceId = Number(document.querySelector("#unified-instance").value);
  if (instanceId) loadUnified(instanceId).catch((err) => {
    document.querySelector("#unified-error").textContent = err.message;
  });
});

document.querySelector("#run-chat-test").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#test-instance").value);
  const error = document.querySelector("#test-error");
  const result = document.querySelector("#test-result");
  error.textContent = "";
  result.hidden = true;
  if (!instanceId) {
    error.textContent = "请选择在线实例。";
    return;
  }
  try {
    const data = await api(`/api/instances/${instanceId}/test-chat`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        message_type:document.querySelector("#test-message-type").value,
        user_id:Number(document.querySelector("#test-user-id").value || 10001),
        group_id:Number(document.querySelector("#test-group-id").value || 0) || null,
        display_name:document.querySelector("#test-display-name").value.trim(),
        mentioned:document.querySelector("#test-mentioned").checked,
        use_memory:document.querySelector("#test-memory").checked,
        message:document.querySelector("#test-message").value.trim(),
      }),
    });
    result.hidden = false;
    result.innerHTML = data.status === "skipped"
      ? `<strong>没有回复</strong><div class="row-meta">${esc(data.reason)}</div>`
      : `<strong>${esc(data.reply)}</strong>
        <div class="row-meta">模型 ${esc(data.model)} · 策略 ${esc(data.strategy)}
        · ${esc(data.latency_ms)} ms</div>`;
  } catch (err) {
    error.textContent = err.message;
  }
});

document.querySelector("#trace-instance").addEventListener("change", (event) => {
  const instanceId = Number(event.target.value);
  if (instanceId) loadTraces(instanceId).catch((err) => {
    document.querySelector("#trace-error").textContent = err.message;
  });
});

document.querySelector("#refresh-traces").addEventListener("click", () => {
  const instanceId = Number(document.querySelector("#trace-instance").value);
  if (instanceId) loadTraces(instanceId).catch((err) => {
    document.querySelector("#trace-error").textContent = err.message;
  });
});

document.querySelector("#trace-skipped-only").addEventListener("change", () => {
  const instanceId = Number(document.querySelector("#trace-instance").value);
  if (instanceId) loadTraces(instanceId).catch((err) => {
    document.querySelector("#trace-error").textContent = err.message;
  });
});

document.querySelector("#save-evolution-presence").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#evolution-instance").value);
  const error = document.querySelector("#evolution-error");
  error.textContent = "";
  if (!instanceId) return;
  try {
    await api(`/api/instances/${instanceId}/presence`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        scope_key:"global",
        busy:document.querySelector("#evolution-busy").checked,
        reason:document.querySelector("#evolution-reason").value.trim(),
        quiet_start:document.querySelector("#evolution-quiet-start").value.trim(),
        quiet_end:document.querySelector("#evolution-quiet-end").value.trim(),
      }),
    });
    await loadEvolution(instanceId);
  } catch (err) {
    error.textContent = err.message;
  }
});

document.querySelector("#load-evolution-topics").addEventListener("click", async () => {
  const instanceId = Number(document.querySelector("#evolution-instance").value);
  const scopeKey = document.querySelector("#evolution-topic-scope").value.trim();
  const error = document.querySelector("#evolution-error");
  error.textContent = "";
  if (!instanceId || !scopeKey) return;
  try {
    const data = await api(
      `/api/instances/${instanceId}/topics?scope_key=${encodeURIComponent(scopeKey)}`
    );
    document.querySelector("#evolution-topics").innerHTML = data.topics?.length
      ? data.topics.map((item) => `
        <article class="source-row">
          <div><span class="tag">${esc(item.status)}</span> <strong>${esc(item.title)}</strong></div>
          <div class="row-meta">${esc(item.summary)}</div>
        </article>`).join("")
      : `<div class="empty">没有进行中的话题</div>`;
  } catch (err) {
    error.textContent = err.message;
  }
});

async function fileToBase64(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return btoa(binary);
}

async function init() {
  try {
    await loadMe();
    await Promise.all([loadProviders(), loadInstances()]);
  } catch (error) {
    location.href = "/login";
  }
}
init();
