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
        ${item.status === "online" ? `<button data-stop="${item.id}">停止</button><button data-restart="${item.id}">重启</button><a href="${esc(item.dashboard_url)}" target="_blank">打开实例后台</a>` : `<button data-start="${item.id}">启动</button>`}
        <button data-edit-instance="${item.id}">编辑</button><button data-delete-instance="${item.id}">删除</button>
      </div>
    </article>`).join("") : `<div class="empty">还没有机器人实例</div>`;
  bindInstanceActions();
}

function bindInstanceActions() {
  document.querySelectorAll("[data-start]").forEach((b) => b.onclick = () => instanceAction(b.dataset.start, "start"));
  document.querySelectorAll("[data-stop]").forEach((b) => b.onclick = () => instanceAction(b.dataset.stop, "stop"));
  document.querySelectorAll("[data-restart]").forEach((b) => b.onclick = () => instanceAction(b.dataset.restart, "restart"));
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

async function init() {
  try {
    await loadMe();
    await Promise.all([loadProviders(), loadInstances()]);
  } catch (error) {
    location.href = "/login";
  }
}
init();
