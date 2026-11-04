const state = { selectedUser: null };

const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[char]));

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

document.querySelectorAll("nav button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    $(`#${button.dataset.tab}`).classList.add("active");
    if (button.dataset.tab === "growth") loadGrowth();
    if (button.dataset.tab === "stickers") loadStickers();
  });
});

async function loadOverview() {
  const data = await api("/api/admin/overview");
  const cards = [
    [data.memory.people || 0, "联系人"],
    [data.memory.memories || 0, "长期记忆"],
    [data.memory.episodes || 0, "对话片段"],
    [data.memory.growth || 0, "成长笔记"],
    [data.vector_chunks || 0, "向量片段"],
    [data.stickers || 0, "表情包"],
  ];
  $("#overview-cards").innerHTML = cards.map(([value, label]) =>
    `<div class="card"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`
  ).join("");
  const rows = [
    ["任务模型", data.models.chat],
    ["闲聊模型", data.models.casual],
    ["图片模型", data.models.vision],
    ["语音模型", data.models.audio],
    ["向量模型", data.models.embedding],
    ["联网搜索", data.web_search_enabled ? "开启" : "关闭"],
    ["记忆隐私保护", data.memory_privacy_protection ? "开启" : "完整共享"],
    ["群聊必须 @", data.group_require_mention ? "是" : "否"],
  ];
  $("#overview-config").innerHTML = rows.map(([key, value]) =>
    `<div class="row"><span class="row-meta">${esc(key)}</span>${esc(value)}</div>`
  ).join("");
}

async function loadPeople() {
  const data = await api("/api/admin/people");
  if (!data.people.length) {
    $("#people-list").innerHTML = `<div class="empty">还没有联系人记录。</div>`;
    return;
  }
  $("#people-list").innerHTML = data.people.map((person, index) => `
    <button class="person-item ${index === 0 ? "active" : ""}" data-user="${person.user_id}">
      ${esc(person.display_name)}
      <small>QQ ${esc(person.user_id)} · ${esc(person.episode_count)} 条片段</small>
    </button>
  `).join("");
  document.querySelectorAll(".person-item").forEach((button) => {
    button.addEventListener("click", () => selectPerson(Number(button.dataset.user), button));
  });
  await selectPerson(Number(data.people[0].user_id), $(".person-item"));
}

async function selectPerson(userId, button) {
  state.selectedUser = userId;
  document.querySelectorAll(".person-item").forEach((item) => item.classList.remove("active"));
  button?.classList.add("active");
  const data = await api(`/api/admin/people/${userId}`);
  $("#person-empty").hidden = true;
  $("#person-content").hidden = false;
  $("#person-id").textContent = `QQ ${data.person.user_id}`;
  $("#person-name").textContent = data.person.display_name;
  $("#style-profile").value = data.person.style_profile || "";
  $("#relationship-note").value = data.person.relationship_note || "";
  renderPersonSections(data);
}

function renderList(title, items, render) {
  return `<div class="section-box"><h3>${esc(title)}</h3>${items.length
    ? items.map(render).join("")
    : `<div class="muted">暂无记录</div>`}</div>`;
}

function renderPersonSections(data) {
  const memories = renderList("长期记忆", data.memories, (item) =>
    `<div class="row"><span class="tag">${esc(item.visibility)}</span> ${esc(item.content)}</div>`
  );
  const episodes = renderList("最近对话", data.episodes, (item) =>
    `<div class="row"><div class="row-meta">${esc(item.created_at)}</div>${esc(item.user_content)}<br><span class="muted">给她爱：${esc(item.assistant_content)}</span></div>`
  );
  const todos = renderList("待办", data.todos, (item) =>
    `<div class="row">${item.done ? "✓" : "○"} ${esc(item.content)}</div>`
  );
  const reminders = renderList("提醒", data.reminders, (item) =>
    `<div class="row">${esc(item.due_at)} · ${esc(item.content)}</div>`
  );
  $("#person-sections").innerHTML = memories + episodes + todos + reminders;
}

$("#save-profile").addEventListener("click", async () => {
  if (!state.selectedUser) return;
  try {
    await api(`/api/admin/people/${state.selectedUser}/profile`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        display_name: $("#person-name").textContent,
        style_profile: $("#style-profile").value,
        relationship_note: $("#relationship-note").value,
      }),
    });
    $("#save-profile").textContent = "已保存";
    setTimeout(() => { $("#save-profile").textContent = "保存画像"; }, 1200);
  } catch (error) {
    alert(`保存失败：${error.message}`);
  }
});

async function loadGrowth() {
  const data = await api("/api/admin/growth");
  $("#reflections").innerHTML = data.reflections.length
    ? data.reflections.map((item) => `<div class="row"><span class="tag">${Number(item.confidence).toFixed(2)}</span> ${esc(item.content)}</div>`).join("")
    : `<div class="empty">还没有反思记录。</div>`;
  $("#growth-list").innerHTML = data.growth.length
    ? data.growth.map((item) => `<div class="row"><div class="row-meta">${esc(item.aspect)}</div>${esc(item.content)}</div>`).join("")
    : `<div class="empty">还没有成长笔记。</div>`;
}

$("#reflect-now").addEventListener("click", async () => {
  const button = $("#reflect-now");
  button.disabled = true;
  button.textContent = "整理中…";
  try {
    const result = await api("/api/admin/reflect", {method:"POST"});
    alert(
      `已整理 ${result.memories} 条记忆，形成 ` +
      `${result.reflections} 条反思、${result.growth} 条成长笔记。`
    );
    await loadGrowth();
  } catch (error) {
    alert(`反思失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "立即反思";
  }
});

async function loadStickers() {
  const data = await api("/api/admin/stickers");
  $("#sticker-list").innerHTML = data.stickers.length
    ? data.stickers.map((item) => `<div class="sticker"><strong>${esc(item.summary)}</strong><div class="row-meta">emoji ${esc(item.emoji_id || "-")} · 使用 ${esc(item.use_count)} 次</div></div>`).join("")
    : `<div class="empty">还没有收集到表情包。</div>`;
}

loadOverview().catch((error) => {
  $("#overview-cards").innerHTML = `<div class="error">${esc(error.message)}</div>`;
});
loadPeople().catch((error) => {
  $("#people-list").innerHTML = `<div class="error">${esc(error.message)}</div>`;
});
