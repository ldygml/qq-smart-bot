let csrf = "";
let providers = [];
let instances = [];

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
  document.querySelector("#instance-count").textContent = instances.length;
  document.querySelector("#online-count").textContent = instances.filter((item) => item.status === "online").length;
  document.querySelector("#instances").innerHTML = instances.length ? instances.map((item) => `
    <article class="instance-item">
      <div class="item-head"><b>${esc(item.name)}</b><span class="status ${item.status === "online" ? "online" : ""}"><i></i>${esc(item.status)}</span></div>
      <div class="item-meta">127.0.0.1:${item.port} · ${esc(item.provider_name || "未知供应商")}<br>${esc(item.onebot_api_base)}</div>
      <div class="item-actions">
        ${item.status === "online" ? `<button data-stop="${item.id}">停止</button><button data-restart="${item.id}">重启</button><button data-memory="${item.id}">记忆维护</button><a href="${esc(item.dashboard_url)}" target="_blank">打开实例后台</a>` : `<button data-start="${item.id}">启动</button>`}
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
