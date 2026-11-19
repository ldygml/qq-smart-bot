const state = {
  selectedUser: null,
  people: [],
  selectedMemories: new Set(),
  groupProfiles: [],
  groupKnowledge: [],
  groupMembers: [],
  groupRelationships: [],
  groupFiles: [],
  groupFolderId: "",
  groupFolderTrail: [{id: "", name: "根目录"}],
  generatedArtifacts: [],
  selectedGenerated: null,
  generatedStorage: null,
  generationTargets: {people: [], groups: []},
  generationTargetsLoaded: false,
};

const $ = (selector) => document.querySelector(selector);
const adminApiBase = window.QQ_ADMIN_API_BASE || "/api/admin";
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[char]));

function scopedAdminPath(path) {
  if (!path || !path.startsWith("/api/admin")) return path;
  return `${adminApiBase}${path.slice("/api/admin".length)}`;
}

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (window.QQ_ADMIN_CSRF && options.method && options.method !== "GET") {
    headers["X-CSRF-Token"] = window.QQ_ADMIN_CSRF;
  }
  const response = await fetch(scopedAdminPath(path), {...options, headers});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

async function jsonRequest(path, method, body) {
  return api(path, {
    method,
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
}

function number(value, digits = 0) {
  const parsed = Number(value || 0);
  return parsed.toLocaleString("zh-CN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function dateText(value) {
  if (!value) return "未知时间";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

function sourceIds(value) {
  try {
    const ids = JSON.parse(value || "[]");
    return Array.isArray(ids) && ids.length ? ids.map((id) => `#${id}`).join("、") : "未记录";
  } catch {
    return "未记录";
  }
}

document.querySelectorAll("nav button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    $(`#${button.dataset.tab}`).classList.add("active");
    const loaders = {
      overview: loadOverview,
      people: loadPeople,
      timeline: loadTimeline,
      growth: loadGrowth,
      language: loadLanguage,
      stickers: loadStickers,
      knowledge: loadKnowledge,
      operations: loadOperations,
      evolution: loadEvolution,
      generate: loadGenerationWorkspace,
    };
    loaders[button.dataset.tab]?.().catch((error) => alert(error.message));
  });
});

async function loadOverview() {
  const data = await api("/api/admin/overview");
  const cards = [
    [data.memory.people || 0, "联系人"],
    [data.memory.memories || 0, "有效长期记忆"],
    [data.memory.episodes || 0, "对话片段"],
    [data.memory.reflections || 0, "长期反思"],
    [data.memory.growth || 0, "成长笔记"],
    [data.memory.conflicts || 0, "记忆冲突"],
    [data.vector_chunks || 0, "知识向量片段"],
    [data.stickers || 0, "启用表情包"],
    [data.usage.calls || 0, "模型调用"],
  ];
  $("#overview-cards").innerHTML = cards.map(([value, label]) =>
    `<div class="card"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`
  ).join("");
  const rows = [
    ["任务模型", data.models.chat],
    ["闲聊模型", data.models.casual],
    ["代码模型", data.models.code],
    ["图片模型", data.models.vision],
    ["语音模型", data.models.audio],
    ["向量模型", data.models.embedding],
    ["联网搜索", data.web_search_enabled ? "开启" : "关闭"],
    ["记忆隐私保护", data.memory_privacy_protection ? "开启" : "完整共享"],
    ["群聊必须 @", data.group_require_mention ? "是" : "否"],
    ["群聊行为学习", data.group_behavior_learning ? "开启" : "关闭"],
  ];
  $("#overview-config").innerHTML = rows.map(([key, value]) =>
    `<div class="row"><span class="row-meta">${esc(key)}</span>${esc(value)}</div>`
  ).join("");
  const alerts = [
    ...(data.inactive_relationships || []).map((item) =>
      `<div class="row"><span class="tag">长期未联系</span> ${esc(item.display_name)} · ${esc(item.days)} 天</div>`
    ),
    ...(data.relationship_events || []).slice(0, 10).map((item) =>
      `<div class="row"><span class="tag">关系变化</span> ${esc(item.display_name || item.user_id)}：${esc(item.old_value)} → ${esc(item.new_value)}</div>`
    ),
  ];
  $("#relationship-alerts").innerHTML = alerts.length
    ? alerts.join("")
    : `<div class="empty">暂无关系变化提醒。</div>`;
}

async function loadPeople() {
  const data = await api("/api/admin/people");
  state.people = data.people;
  updatePeopleSelects();
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

function updatePeopleSelects() {
  const options = [
    `<option value="">全部联系人</option>`,
    ...state.people.map((person) =>
      `<option value="${person.user_id}">${esc(person.display_name)} · QQ ${person.user_id}</option>`
    ),
  ].join("");
  const current = $("#timeline-person").value;
  $("#timeline-person").innerHTML = options;
  $("#timeline-person").value = current;
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
  const memories = renderList("有效长期记忆", data.memories, (item) =>
    `<div class="row"><span class="tag">${esc(item.category || "general")}</span> ${esc(item.content)}<div class="row-meta">${esc(item.fact_key || "无主题键")} · ${dateText(item.updated_at)}</div></div>`
  );
  const episodes = renderList("最近对话", data.episodes, (item) =>
    `<div class="row"><div class="row-meta">${esc(dateText(item.created_at))}</div>${esc(item.user_content)}<br><span class="muted">给她爱：${esc(item.assistant_content)}</span></div>`
  );
  const todos = renderList("待办", data.todos, (item) =>
    `<div class="row">${item.done ? "✓" : "○"} ${esc(item.content)}</div>`
  );
  const reminders = renderList("提醒", data.reminders, (item) =>
    `<div class="row">${esc(dateText(item.due_at))} · ${esc(item.content)}</div>`
  );
  $("#person-sections").innerHTML = memories + episodes + todos + reminders;
}

$("#save-profile").addEventListener("click", async () => {
  if (!state.selectedUser) return;
  try {
    await jsonRequest(`/api/admin/people/${state.selectedUser}/profile`, "POST", {
      display_name: $("#person-name").textContent,
      style_profile: $("#style-profile").value,
      relationship_note: $("#relationship-note").value,
    });
    $("#save-profile").textContent = "已保存";
    setTimeout(() => { $("#save-profile").textContent = "保存画像"; }, 1200);
  } catch (error) {
    alert(`保存失败：${error.message}`);
  }
});

$("#person-summary").addEventListener("click", async () => {
  if (!state.selectedUser) return;
  const button = $("#person-summary");
  const output = $("#person-summary-result");
  button.disabled = true;
  button.textContent = "整理中…";
  try {
    const data = await api(`/api/admin/people/${state.selectedUser}/summary`, {method:"POST"});
    output.hidden = false;
    output.textContent = data.summary;
  } catch (error) {
    output.hidden = false;
    output.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "生成近况摘要";
  }
});

$("#purge-person-memory").addEventListener("click", async () => {
  if (!state.selectedUser) return;
  const name = $("#person-name").textContent;
  if (!confirm(
    `确定删除“${name}”的全部联系人信息、长期记忆、对话片段、观察记录和相关反思吗？\n\n笔记、待办和提醒不会被删除。此操作不可撤销。`
  )) return;
  try {
    const result = await api(
      `/api/admin/people/${state.selectedUser}/all-memory`,
      {method:"DELETE"}
    );
    const deleted = result.deleted;
    alert(
      `已清除 ${name}：${deleted.memories} 条长期记忆、` +
      `${deleted.episodes} 条对话片段、${deleted.observations} 条观察、` +
      `${deleted.messages} 条短期消息、${deleted.vectors} 个向量片段。`
    );
    state.selectedUser = null;
    $("#person-content").hidden = true;
    $("#person-empty").hidden = false;
    await loadPeople();
    await loadOverview();
  } catch (error) {
    alert(`删除失败：${error.message}`);
  }
});

$("#purge-all-memory").addEventListener("click", async () => {
  if (!confirm(
    "确定删除所有联系人和全部记忆吗？\n\n将清除长期记忆、对话片段、未处理观察、联系人画像、反思、成长笔记、冲突、短期聊天和记忆向量。\n\n笔记、待办、提醒、群聊设置和知识库文件不会删除。此操作不可撤销。"
  )) return;
  try {
    const result = await api("/api/admin/memory/all", {method:"DELETE"});
    const deleted = result.deleted;
    alert(
      `已清空全部记忆：${deleted.people} 个联系人、` +
      `${deleted.memories} 条长期记忆、${deleted.episodes} 条对话片段、` +
      `${deleted.messages} 条短期消息、${deleted.vectors} 个向量片段。`
    );
    state.selectedUser = null;
    $("#person-content").hidden = true;
    $("#person-empty").hidden = false;
    await Promise.all([loadOverview(), loadPeople()]);
    if ($("#timeline").classList.contains("active")) await loadTimeline();
  } catch (error) {
    alert(`清空失败：${error.message}`);
  }
});

$("#purge-memory-range").addEventListener("click", async () => {
  const startAt = $("#purge-range-start").value;
  const endAt = $("#purge-range-end").value;
  if (!startAt || !endAt) {
    alert("请选择开始和结束时间。");
    return;
  }
  if (!confirm("删除这个时间段内的记忆、对话片段和观察记录？此操作不可撤销。")) return;
  try {
    const data = await jsonRequest("/api/admin/memory/purge-range", "POST", {
      start_at: startAt,
      end_at: endAt,
    });
    alert(`已删除 ${data.deleted.memories.length} 条记忆、${data.deleted.episodes.length} 条对话片段。`);
    await Promise.all([loadOverview(), loadPeople(), loadTimeline()]);
  } catch (error) {
    alert(error.message);
  }
});

$("#import-memory-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    const result = await jsonRequest("/api/admin/memory/import", "POST", data);
    alert(`记忆导入完成：${Object.entries(result.imported).map(([name, count]) => `${name} ${count}`).join("，")}`);
    await Promise.all([loadOverview(), loadPeople(), loadTimeline()]);
  } catch (error) {
    alert(`导入失败：${error.message}`);
  } finally {
    event.target.value = "";
  }
});

async function loadTimeline() {
  state.selectedMemories.clear();
  const userId = $("#timeline-person").value;
  const suffix = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
  const data = await api(`/api/admin/memory/timeline${suffix}`);
  $("#conflict-list").innerHTML = data.conflicts.length
    ? `<h3>自动检测到的冲突</h3>${data.conflicts.map((item) => `
      <div class="conflict">
        <div class="row-meta">${esc(item.display_name || item.subject_user_id)} · ${esc(item.fact_key || "未命名主题")} · ${dateText(item.created_at)}</div>
        <div><span class="old-value">${esc(item.old_content || "记录已删除")}</span></div>
        <div><span class="new-value">${esc(item.new_content || "记录已删除")}</span></div>
        <small>${esc(item.reason)}；审核状态：${esc(item.review_status || "pending")}</small>
        ${item.review_status === "pending" ? `<div class="item-actions">
          <button data-resolve-conflict="${item.id}" data-decision="keep_new">保留新说法</button>
          <button data-resolve-conflict="${item.id}" data-decision="revert_old">恢复旧说法</button>
          <button data-resolve-conflict="${item.id}" data-decision="dismiss">忽略提醒</button>
        </div>` : ""}
      </div>`).join("")}`
    : `<div class="muted">没有检测到记忆冲突。</div>`;
  $("#memory-timeline").innerHTML = data.memories.length
    ? data.memories.map((item) => memoryCard(item)).join("")
    : `<div class="empty">还没有长期记忆。机器人会在后续对话和反思中自动提取。</div>`;
  bindMemoryCards();
  bindConflictActions();
}

function bindConflictActions() {
  document.querySelectorAll("[data-resolve-conflict]").forEach((button) => {
    button.onclick = async () => {
      try {
        await jsonRequest(`/api/admin/conflicts/${button.dataset.resolveConflict}`, "PATCH", {
          decision: button.dataset.decision,
        });
        await loadTimeline();
      } catch (error) { alert(error.message); }
    };
  });
}

function memoryCard(item) {
  const active = item.status === "active";
  return `<article class="memory-card ${active ? "" : "inactive"}">
    <div class="memory-head">
      <label class="check"><input type="checkbox" data-select-memory="${item.id}" ${active ? "" : "disabled"}> #${item.id}</label>
      <span class="tag">${esc(item.status)}</span>
      <span class="tag">${esc(item.category || "general")}</span>
      <span class="muted">${esc(item.subject_name)} · QQ ${esc(item.subject_user_id)}</span>
    </div>
    <textarea data-memory-content="${item.id}" rows="2" ${active ? "" : "disabled"}>${esc(item.content)}</textarea>
    <div class="field-row">
      <input data-memory-category="${item.id}" value="${esc(item.category || "general")}" aria-label="分类" ${active ? "" : "disabled"}>
      <input data-memory-key="${item.id}" value="${esc(item.fact_key || "")}" placeholder="主题键，例如 居住城市" ${active ? "" : "disabled"}>
      <select data-memory-visibility="${item.id}" ${active ? "" : "disabled"}>
        <option value="shareable" ${item.visibility === "shareable" ? "selected" : ""}>可共享</option>
        <option value="private" ${item.visibility === "private" ? "selected" : ""}>仅当事人</option>
      </select>
    </div>
    ${item.source_text ? `<details class="source"><summary>查看来源</summary><p>${esc(item.source_text)}</p>${item.source_reply ? `<p class="muted">当时回复：${esc(item.source_reply)}</p>` : ""}</details>` : `<p class="muted">来源：${esc(item.source_conversation)}</p>`}
    ${item.follow_up_at ? `<div class="row-meta">跟进时间 ${dateText(item.follow_up_at)} · 状态 ${esc(item.follow_up_status || "pending")}
      ${item.follow_up_status === "pending" ? `<button data-complete-followup="${item.id}">标记已跟进</button>` : ""}</div>` : ""}
    <div class="row-meta">创建 ${dateText(item.created_at)} · 更新 ${dateText(item.updated_at)}${item.superseded_by ? ` · 被 #${esc(item.superseded_by)} 取代` : ""}</div>
    ${active ? `<div class="item-actions"><button data-save-memory="${item.id}" data-user="${item.subject_user_id}">保存修改</button><button class="danger" data-delete-memory="${item.id}" data-user="${item.subject_user_id}">删除</button></div>` : ""}
  </article>`;
}

function bindMemoryCards() {
  document.querySelectorAll("[data-select-memory]").forEach((input) => {
    input.onchange = () => {
      const id = Number(input.dataset.selectMemory);
      if (input.checked) state.selectedMemories.add(id);
      else state.selectedMemories.delete(id);
    };
  });
  document.querySelectorAll("[data-save-memory]").forEach((button) => {
    button.onclick = async () => {
      const id = Number(button.dataset.saveMemory);
      try {
        await jsonRequest(`/api/admin/memories/${id}`, "PATCH", {
          user_id: Number(button.dataset.user),
          content: document.querySelector(`[data-memory-content="${id}"]`).value,
          category: document.querySelector(`[data-memory-category="${id}"]`).value,
          fact_key: document.querySelector(`[data-memory-key="${id}"]`).value,
          visibility: document.querySelector(`[data-memory-visibility="${id}"]`).value,
        });
        await loadTimeline();
      } catch (error) { alert(error.message); }
    };
  });
  document.querySelectorAll("[data-delete-memory]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm("删除这条记忆？")) return;
      try {
        await api(`/api/admin/people/${button.dataset.user}/memories/${button.dataset.deleteMemory}`, {method:"DELETE"});
        await loadTimeline();
      } catch (error) { alert(error.message); }
    };
  });
  document.querySelectorAll("[data-complete-followup]").forEach((button) => {
    button.onclick = async () => {
      try {
        await jsonRequest(`/api/admin/memories/${button.dataset.completeFollowup}/followup`, "PATCH", {
          follow_up_at: null,
          status: "done",
        });
        await loadTimeline();
      } catch (error) { alert(error.message); }
    };
  });
}

$("#refresh-timeline").addEventListener("click", () => loadTimeline().catch((error) => alert(error.message)));
$("#timeline-person").addEventListener("change", () => loadTimeline().catch((error) => alert(error.message)));
$("#merge-memories").addEventListener("click", async () => {
  if (state.selectedMemories.size !== 2) {
    alert("请准确选择两条同属一个联系人的有效记忆。");
    return;
  }
  const ids = [...state.selectedMemories];
  const first = document.querySelector(`[data-user][data-delete-memory="${ids[0]}"]`);
  const second = document.querySelector(`[data-user][data-delete-memory="${ids[1]}"]`);
  if (!first || !second || first.dataset.user !== second.dataset.user) {
    alert("只能合并同一联系人的记忆。");
    return;
  }
  try {
    await jsonRequest("/api/admin/memories/merge", "POST", {
      user_id: Number(first.dataset.user),
      primary_memory_id: ids[0],
      secondary_memory_id: ids[1],
    });
    await loadTimeline();
  } catch (error) { alert(error.message); }
});

async function loadGrowth() {
  const data = await api("/api/admin/growth");
  $("#reflections").innerHTML = data.reflections.length
    ? data.reflections.map((item) => `
      <article class="edit-card">
        <div class="row-meta">#${item.id} · ${dateText(item.updated_at)} · 证据 ${item.evidence_count || 1} · 来源记忆 ${sourceIds(item.source_memory_ids)}</div>
        <textarea data-reflection-content="${item.id}" rows="3">${esc(item.content)}</textarea>
        <div class="field-row">
          <select data-reflection-visibility="${item.id}">
            <option value="shareable" ${item.visibility === "shareable" ? "selected" : ""}>可共享</option>
            <option value="private" ${item.visibility === "private" ? "selected" : ""}>仅当事人</option>
          </select>
          <input data-reflection-confidence="${item.id}" type="number" min="0" max="1" step="0.05" value="${esc(item.confidence)}">
          <button data-save-reflection="${item.id}">保存</button>
          <button class="danger" data-delete-reflection="${item.id}">删除</button>
        </div>
      </article>`).join("")
    : `<div class="empty">还没有反思记录。点击“立即反思”或等待后台自动整理。</div>`;
  $("#growth-list").innerHTML = data.growth.length
    ? data.growth.map((item) => `
      <article class="edit-card">
        <div class="row-meta">#${item.id} · ${dateText(item.updated_at)} · 证据 ${item.evidence_count} · 来源记忆 ${sourceIds(item.source_memory_ids)}</div>
        <textarea data-growth-content="${item.id}" rows="3">${esc(item.content)}</textarea>
        <div class="field-row">
          <input data-growth-aspect="${item.id}" value="${esc(item.aspect)}" placeholder="方面">
          <select data-growth-visibility="${item.id}">
            <option value="shareable" ${item.visibility === "shareable" ? "selected" : ""}>可共享</option>
            <option value="private" ${item.visibility === "private" ? "selected" : ""}>仅当事人</option>
          </select>
          <button data-save-growth="${item.id}">保存</button>
          <button class="danger" data-delete-growth="${item.id}">删除</button>
        </div>
      </article>`).join("")
    : `<div class="empty">还没有成长笔记。</div>`;
  bindGrowthEditors();
}

function bindGrowthEditors() {
  document.querySelectorAll("[data-save-reflection]").forEach((button) => {
    button.onclick = async () => {
      const id = Number(button.dataset.saveReflection);
      try {
        await jsonRequest(`/api/admin/reflections/${id}`, "PATCH", {
          content: document.querySelector(`[data-reflection-content="${id}"]`).value,
          visibility: document.querySelector(`[data-reflection-visibility="${id}"]`).value,
          confidence: Number(document.querySelector(`[data-reflection-confidence="${id}"]`).value),
        });
        await loadGrowth();
      } catch (error) { alert(error.message); }
    };
  });
  document.querySelectorAll("[data-delete-reflection]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm("删除这条反思？")) return;
      try {
        await api(`/api/admin/reflections/${button.dataset.deleteReflection}`, {method:"DELETE"});
        await loadGrowth();
      } catch (error) { alert(error.message); }
    };
  });
  document.querySelectorAll("[data-save-growth]").forEach((button) => {
    button.onclick = async () => {
      const id = Number(button.dataset.saveGrowth);
      try {
        await jsonRequest(`/api/admin/growth/${id}`, "PATCH", {
          aspect: document.querySelector(`[data-growth-aspect="${id}"]`).value,
          content: document.querySelector(`[data-growth-content="${id}"]`).value,
          visibility: document.querySelector(`[data-growth-visibility="${id}"]`).value,
        });
        await loadGrowth();
      } catch (error) { alert(error.message); }
    };
  });
  document.querySelectorAll("[data-delete-growth]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm("删除这条成长笔记？")) return;
      try {
        await api(`/api/admin/growth/${button.dataset.deleteGrowth}`, {method:"DELETE"});
        await loadGrowth();
      } catch (error) { alert(error.message); }
    };
  });
}

function languageScopeLabel(item) {
  if (item.scope_type === "global") return "全网";
  if (item.scope_type === "group") {
    return "群聊 · " + (item.scope_name || item.scope_id);
  }
  return "联系人 · " + (item.scope_name || item.scope_id);
}

async function loadLanguage() {
  const data = await api("/api/admin/language");
  const cards = [
    [data.stats.pending || 0, "待学习消息"],
    [data.stats.profiles || 0, "场景说话画像"],
    [data.stats.expressions || 0, "有效口语表达"],
    [data.stats.learned || 0, "已处理样本"],
  ];
  $("#language-cards").innerHTML = cards.map(([value, label]) =>
    "<div class=\"card\"><strong>" + esc(value) + "</strong><span>" + esc(label) + "</span></div>"
  ).join("");
  $("#language-enabled").checked = Boolean(data.settings.enabled);
  $("#language-web-enabled").checked = Boolean(data.settings.web_learning_enabled);
  $("#language-auto-profile").checked = Boolean(data.settings.auto_profile);
  $("#language-meme-rate").value = Number(data.settings.meme_usage_rate || 0);
  $("#language-status").textContent = "聊天学习：" + (data.settings.last_chat_learned_at ? dateText(data.settings.last_chat_learned_at) : "尚未运行") + " · 热梗更新：" + (data.settings.last_web_learned_at ? dateText(data.settings.last_web_learned_at) : "尚未运行");

  $("#language-profiles").innerHTML = data.profiles.length
    ? data.profiles.map((item) => [
      "<article class=\"edit-card\">",
      "<div class=\"row-meta\">", esc(languageScopeLabel(item)), " · 证据 ", esc(item.evidence_count), " 条 · ", dateText(item.updated_at), "</div>",
      "<textarea rows=\"2\" readonly>", esc(item.summary || ""), "</textarea>",
      "<div class=\"row-meta\">节奏：", esc(item.rhythm || "暂无"), " · 口语倾向：", esc(item.vocabulary || "暂无"), " · 避开：", esc(item.avoid || "暂无"), "</div>",
      "<div class=\"item-actions\"><button class=\"danger\" data-delete-language-scope=\"", esc(item.scope_type), "\" data-scope-id=\"", esc(item.scope_id), "\">删除该场景学习结果</button></div>",
      "</article>",
    ].join("")).join("")
    : "<div class=\"empty\">还没有足够聊天样本。群聊里正常说话，机器人会定期自己整理。</div>";

  const expressionMarkup = data.expressions.map((item) => [
    "<article class=\"edit-card\">",
    "<div class=\"row-meta\">", esc(languageScopeLabel(item)), " · ", esc(item.source_kind === "web" ? "联网学习" : item.source_kind === "manual" ? "手动添加" : "聊天观察"), " · 看过 ", esc(item.seen_count), " 次 · 置信度 ", esc(item.confidence), "</div>",
    "<strong>", esc(item.phrase), "</strong>",
    "<input data-language-meaning=\"", item.id, "\" value=\"", esc(item.meaning || ""), "\" placeholder=\"含义\">",
    "<input data-language-usage=\"", item.id, "\" value=\"", esc(item.usage || ""), "\" placeholder=\"自然使用场景\">",
    "<div class=\"field-row\">",
    "<input value=\"", esc(item.tone || ""), "\" readonly placeholder=\"语气\">",
    "<select data-language-status=\"", item.id, "\">",
    "<option value=\"active\"", item.status === "active" ? " selected" : "", ">启用</option>",
    "<option value=\"blocked\"", item.status === "blocked" ? " selected" : "", ">禁用</option>",
    "</select>",
    item.source_url ? "<a class=\"button-link\" href=\"" + esc(item.source_url) + "\" target=\"_blank\" rel=\"noreferrer\">来源</a>" : "",
    "<button data-save-language-expression=\"", item.id, "\">保存</button>",
    "<button class=\"danger\" data-delete-language-expression=\"", item.id, "\">删除</button>",
    "</div></article>",
  ].join("")).join("");
  $("#language-expressions").innerHTML = data.expressions.length
    ? expressionMarkup
    : "<div class=\"empty\">还没有可用的热梗或口语表达。</div>";

  document.querySelectorAll("[data-save-language-expression]").forEach((button) => {
    button.onclick = async () => {
      const id = Number(button.dataset.saveLanguageExpression);
      try {
        await jsonRequest("/api/admin/language/expressions/" + id, "PATCH", {
          meaning: document.querySelector("[data-language-meaning=\"" + id + "\"]").value,
          usage: document.querySelector("[data-language-usage=\"" + id + "\"]").value,
          status: document.querySelector("[data-language-status=\"" + id + "\"]").value,
        });
        await loadLanguage();
      } catch (error) { alert(error.message); }
    };
  });
  document.querySelectorAll("[data-delete-language-expression]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm("删除这条语言学习记录？")) return;
      try {
        await api("/api/admin/language/expressions/" + button.dataset.deleteLanguageExpression, {method:"DELETE"});
        await loadLanguage();
      } catch (error) { alert(error.message); }
    };
  });
  document.querySelectorAll("[data-delete-language-scope]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm("删除这个场景的说话画像、观察样本和专属热梗？")) return;
      try {
        await api("/api/admin/language/scopes/" + button.dataset.deleteLanguageScope + "/" + button.dataset.scopeId, {method:"DELETE"});
        await loadLanguage();
      } catch (error) { alert(error.message); }
    };
  });
}

$("#refresh-language").addEventListener("click", () => loadLanguage().catch((error) => alert(error.message)));
$("#save-language-settings").addEventListener("click", async () => {
  try {
    await jsonRequest("/api/admin/language/settings", "PATCH", {
      enabled: $("#language-enabled").checked,
      web_learning_enabled: $("#language-web-enabled").checked,
      auto_profile: $("#language-auto-profile").checked,
      meme_usage_rate: Number($("#language-meme-rate").value || 0),
    });
    $("#language-status").textContent = "学习设置已保存。";
  } catch (error) { alert(error.message); }
});
$("#learn-chat-language").addEventListener("click", async () => {
  const button = $("#learn-chat-language");
  button.disabled = true;
  $("#language-status").textContent = "正在整理近期聊天表达…";
  try {
    const result = await jsonRequest("/api/admin/language/learn", "POST", {chat:true, web:false});
    $("#language-status").textContent = "已处理 " + result.observations + " 条消息，更新 " + result.profiles + " 个画像，学到 " + result.expressions + " 条表达。";
    await loadLanguage();
  } catch (error) { $("#language-status").textContent = "学习失败：" + error.message; }
  finally { button.disabled = false; }
});
$("#learn-web-language").addEventListener("click", async () => {
  const button = $("#learn-web-language");
  button.disabled = true;
  $("#language-status").textContent = "正在联网核对近期热梗…";
  try {
    const result = await jsonRequest("/api/admin/language/learn", "POST", {chat:false, web:true});
    $("#language-status").textContent = "联网更新完成，新增或刷新 " + result.web_expressions + " 条表达。";
    await loadLanguage();
  } catch (error) { $("#language-status").textContent = "联网学习失败：" + error.message; }
  finally { button.disabled = false; }
});
$("#add-language-expression").addEventListener("click", async () => {
  try {
    const scopeType = $("#manual-language-scope").value;
    await jsonRequest("/api/admin/language/expressions", "POST", {
      scope_type: scopeType,
      scope_id: scopeType === "global" ? 0 : Number($("#manual-language-scope-id").value || 0),
      phrase: $("#manual-language-phrase").value,
      meaning: $("#manual-language-meaning").value,
      usage: $("#manual-language-usage").value,
    });
    $("#manual-language-phrase").value = "";
    $("#manual-language-meaning").value = "";
    $("#manual-language-usage").value = "";
    await loadLanguage();
  } catch (error) { alert(error.message); }
});

$("#reflect-now").addEventListener("click", async () => {
  const button = $("#reflect-now");
  button.disabled = true;
  button.textContent = "整理中…";
  try {
    const result = await api("/api/admin/reflect", {method:"POST"});
    alert(`补扫 ${result.extracted || 0} 条记忆，形成 ${result.reflections} 条反思、${result.growth} 条成长笔记，检测到 ${result.conflicts || 0} 个冲突。`);
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
    ? data.stickers.map((item) => {
      const stickerUrl = scopedAdminPath(item.asset_url || item.image_source || "");
      return `
      <article class="sticker">
        <div class="sticker-preview">${stickerUrl ? `<img src="${esc(stickerUrl)}" alt="${esc(item.summary)}" loading="lazy">` : `<span>${esc(item.summary)}</span>`}</div>
        <strong>${esc(item.summary)}</strong>
        <div class="row-meta">${item.has_local_asset ? "本地文件已保存" : (item.asset_error ? "保存失败，可稍后补齐" : "等待补存图片")}</div>
        <div class="row-meta">emoji ${esc(item.emoji_id || "-")} · 使用 ${esc(item.use_count)} 次</div>
        <div class="row-meta">${(item.tags || []).map((tag) => `#${esc(tag)}`).join(" ") || "暂无识别标签"}</div>
        <label>分类<input data-sticker-category="${item.id}" value="${esc(item.category || "未分类")}"></label>
        <label>适用场景<input data-sticker-hint="${item.id}" value="${esc(item.usage_hint || "")}" placeholder="例如：无语、震惊、庆祝"></label>
        <label class="check"><input type="checkbox" data-sticker-enabled="${item.id}" ${item.enabled ? "checked" : ""}> 启用自动选择</label>
        <div class="item-actions"><button data-save-sticker="${item.id}">保存</button><button class="danger" data-delete-sticker="${item.id}">删除</button></div>
      </article>`;
    }).join("")
    : `<div class="empty">表情库为空。先让别人给机器人发几个表情包。</div>`;
  bindStickerActions();
}

$("#repair-sticker-assets").addEventListener("click", async () => {
  const button = $("#repair-sticker-assets");
  button.disabled = true;
  button.textContent = "正在补存…";
  try {
    const data = await api("/api/admin/stickers/repair-assets", {method:"POST"});
    alert(`检查 ${data.candidates} 个表情，成功补存 ${data.repaired} 个，失败 ${data.failed} 个。`);
    await loadStickers();
  } catch (error) {
    alert(`补存失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "补齐本地表情图片";
  }
});

$("#organize-stickers").addEventListener("click", async () => {
  const button = $("#organize-stickers");
  button.disabled = true;
  button.textContent = "整理中…";
  try {
    const data = await api("/api/admin/stickers/organize", {method:"POST"});
    alert(`已自动整理 ${data.organized} 个表情，失败 ${data.failed} 个。`);
    await loadStickers();
  } catch (error) {
    alert(`整理失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "自动整理未分类表情";
  }
});

$("#reclassify-stickers").addEventListener("click", async () => {
  const button = $("#reclassify-stickers");
  if (!confirm("会重新识别全部表情的画面、文字和适用场景，可能覆盖自动分类。继续吗？")) return;
  button.disabled = true;
  button.textContent = "重新判断中…";
  try {
    const data = await jsonRequest("/api/admin/stickers/organize", "POST", {
      refresh_all: true,
    });
    alert(`已重新判断 ${data.organized} 个表情，失败 ${data.failed} 个。`);
    await loadStickers();
  } catch (error) {
    alert(`重新判断失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "按画面重新判断全部表情";
  }
});

function bindStickerActions() {
  document.querySelectorAll("[data-save-sticker]").forEach((button) => {
    button.onclick = async () => {
      const id = Number(button.dataset.saveSticker);
      try {
        await jsonRequest(`/api/admin/stickers/${id}`, "PATCH", {
          category: document.querySelector(`[data-sticker-category="${id}"]`).value,
          usage_hint: document.querySelector(`[data-sticker-hint="${id}"]`).value,
          enabled: document.querySelector(`[data-sticker-enabled="${id}"]`).checked,
        });
        await loadStickers();
      } catch (error) { alert(error.message); }
    };
  });
  document.querySelectorAll("[data-delete-sticker]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm("删除这个表情包记录？")) return;
      try {
        await api(`/api/admin/stickers/${button.dataset.deleteSticker}`, {method:"DELETE"});
        await loadStickers();
      } catch (error) { alert(error.message); }
    };
  });
}

async function loadKnowledge() {
  const data = await api("/api/admin/knowledge");
  $("#knowledge-list").innerHTML = data.sources.length
    ? data.sources.map((item) => `
      <article class="source-row">
        <div><span class="tag">${esc(item.namespace)}</span> <strong>${esc(item.title)}</strong></div>
        <div class="row-meta">${esc(item.location)} · ${esc(item.chunk_count)} 个片段 · ${dateText(item.created_at)}</div>
        <button class="danger" data-delete-source="${encodeURIComponent(item.source_id)}" data-namespace="${esc(item.namespace)}">删除来源</button>
      </article>`).join("")
    : `<div class="empty">还没有上传文件或收藏网页。</div>`;
  document.querySelectorAll("[data-delete-source]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm("删除这个知识来源及其全部向量片段？")) return;
      try {
        await jsonRequest("/api/admin/knowledge/delete", "POST", {
          namespace: button.dataset.namespace,
          source_id: decodeURIComponent(button.dataset.deleteSource),
        });
        await loadKnowledge();
      } catch (error) { alert(error.message); }
    };
  });
}

async function loadOperations() {
  const [usage, groups, settingsData, profileData, qualityData] = await Promise.all([
    api("/api/admin/usage"),
    api("/api/admin/group-behavior"),
    api("/api/admin/group-settings"),
    api("/api/admin/group-profiles"),
    api("/api/admin/quality"),
  ]);
  renderGroupSettings(settingsData.settings);
  renderGroupProfiles(profileData.groups);
  renderQuality(qualityData);
  const totals = usage.totals;
  $("#input-price").value = usage.pricing?.input || 0;
  $("#output-price").value = usage.pricing?.output || 0;
  $("#usage-cards").innerHTML = [
    [number(totals.calls), "调用次数"],
    [number(totals.errors), "失败次数"],
    [`${number(totals.avg_ms, 0)} ms`, "平均延迟"],
    [number(totals.input_tokens), "估算输入 Token"],
    [number(totals.output_tokens), "估算输出 Token"],
    [totals.pricing_configured ? `¥${number(totals.estimated_cost, 4)}` : "未配置单价", "估算费用"],
  ].map(([value, label]) => `<div class="card"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`).join("");
  $("#usage-models").innerHTML = `<table><thead><tr><th>模型</th><th>调用</th><th>失败</th><th>成功率</th><th>平均延迟</th><th>输入 Token</th><th>输出 Token</th><th>估算费用</th></tr></thead><tbody>${usage.by_model.length ? usage.by_model.map((item) => `<tr><td>${esc(item.model)}</td><td>${esc(item.calls)}</td><td>${esc(item.errors)}</td><td>${number(Number(item.success_rate) * 100, 1)}%</td><td>${number(item.avg_ms)} ms</td><td>${number(item.input_tokens)}</td><td>${number(item.output_tokens)}</td><td>${usage.totals.pricing_configured ? `¥${number(item.estimated_cost, 4)}` : "未配置"}</td></tr>`).join("") : `<tr><td colspan="8">暂无调用记录</td></tr>`}</tbody></table>`;
  $("#usage-recent").innerHTML = usage.recent.length
    ? `<h3>最近调用</h3><div class="recent-grid">${usage.recent.slice(0, 20).map((item) => `<div class="usage-line ${item.success ? "" : "failed"}"><span>${esc(item.operation)}</span><b>${esc(item.model)}</b><span>${number(item.duration_ms)} ms</span><small>${esc(item.error || "成功")}</small></div>`).join("")}</div>`
    : "";
  $("#group-list").innerHTML = groups.groups.length
    ? groups.groups.map((item) => `
      <article class="source-row">
        <div><strong>群 ${esc(item.group_id)}</strong> <span class="tag">沉默倾向 ${number(Number(item.silence_bias) * 100)}%</span></div>
        <div class="row-meta">观察 ${esc(item.messages_seen)} 条 · 普通候选 ${esc(item.ordinary_candidates)} · 跳过 ${esc(item.ordinary_ignored)} · 回复率 ${number(Number(item.reply_rate) * 100)}% · 最近回复 ${dateText(item.last_reply_at)}</div>
        <button data-reset-group="${item.group_id}">重置该群学习</button>
      </article>`).join("")
    : `<div class="empty">还没有群聊行为记录。</div>`;
  document.querySelectorAll("[data-reset-group]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm("重置这个群的沉默倾向学习？")) return;
      try {
        await api(`/api/admin/group-behavior/${button.dataset.resetGroup}`, {method:"DELETE"});
        await loadOperations();
      } catch (error) { alert(error.message); }
    };
  });
}

function renderQuality(data) {
  const stats = data.stats || {};
  $("#quality-stats").innerHTML = [
    [stats.total || 0, "已记录回复"],
    [stats.reviewed || 0, "已评测"],
    [stats.corrected || 0, "被纠正/追问"],
    [stats.average_score == null ? "-" : Number(stats.average_score).toFixed(2), "平均分"],
  ].map(([value, label]) =>
    `<div class="card"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`
  ).join("");
  const items = (data.recent || []).slice(0, 30);
  $("#quality-list").innerHTML = items.length
    ? items.map((item) => `
      <article class="source-row">
        <div><span class="tag">${item.score == null ? item.status : Number(item.score).toFixed(2)}</span>
          <strong>${esc(item.user_text)}</strong></div>
        <div class="row-meta">${esc(item.assistant_text)}<br>
          ${esc((item.issues || []).join("、") || item.review_notes || "尚无评测问题")}
        </div>
        ${item.fact_status && item.fact_status !== "not_checked"
          ? `<div class="row-meta">事实状态：${esc(item.fact_status)}</div>` : ""}
        ${(item.sources || []).map((source) =>
          `<div class="row-meta">来源：<a href="${esc(source.url)}" target="_blank" rel="noreferrer">${esc(source.title)}</a></div>`
        ).join("")}
      </article>`).join("")
    : `<div class="empty">还没有质量记录。</div>`;
}

$("#review-quality").addEventListener("click", async () => {
  const button = $("#review-quality");
  button.disabled = true;
  button.textContent = "评测中…";
  try {
    const data = await api("/api/admin/quality/review", {method:"POST"});
    renderQuality({stats:(await api("/api/admin/quality")).stats, recent:data.reviews});
    alert(`已评测 ${data.reviewed} 条回复。`);
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "评测最近回复";
  }
});

$("#run-replay").addEventListener("click", async () => {
  const button = $("#run-replay");
  const output = $("#replay-results");
  let variants;
  try {
    variants = JSON.parse($("#replay-variants").value || "[]");
  } catch {
    alert("候选方案不是有效 JSON。");
    return;
  }
  button.disabled = true;
  button.textContent = "生成中…";
  try {
    const data = await jsonRequest("/api/admin/quality/replay", "POST", {
      user_message: $("#replay-message").value.trim(),
      variants,
    });
    output.innerHTML = data.results.map((item) => `
      <article class="source-row">
        <div><strong>${esc(item.name)}</strong> <span class="tag">${esc(item.model)}</span></div>
        <div>${esc(item.reply || item.error)}</div>
      </article>`).join("");
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "运行对比";
  }
});

function renderGroupProfiles(profiles) {
  state.groupProfiles = profiles;
  const select = $("#group-profile-select");
  const previous = select.value;
  select.innerHTML = profiles.length
    ? profiles.map((item) => `<option value="${item.group_id}">${esc(item.name || `群 ${item.group_id}`)} · ${item.group_id}</option>`).join("")
    : `<option value="">暂无群聊记录</option>`;
  if (previous && profiles.some((item) => String(item.group_id) === previous)) {
    select.value = previous;
  }
  if (select.value) renderGroupProfile(Number(select.value));
  if (select.value) loadGroupDetails(Number(select.value)).catch(() => {});
}

function renderGroupProfile(groupId) {
  const profile = state.groupProfiles.find((item) => Number(item.group_id) === groupId);
  if (!profile) return;
  $("#profile-name").value = profile.name || "";
  $("#profile-enabled").checked = Boolean(profile.enabled);
  $("#profile-require-mention").checked = Boolean(profile.require_mention);
  $("#profile-smart").checked = Boolean(profile.smart_participation);
  $("#profile-learning").checked = Boolean(profile.behavior_learning);
  $("#profile-quote").checked = Boolean(profile.quote_replies);
  $("#profile-quote-mention").checked = Boolean(profile.quote_counts_as_mention);
  $("#profile-sticker").checked = Boolean(profile.auto_sticker_enabled);
  $("#profile-avoid-last").checked = Boolean(profile.avoid_last_word);
  $("#profile-confidence").value = profile.min_reply_confidence;
  $("#profile-cooldown").value = profile.reply_cooldown_seconds;
  $("#profile-hourly-limit").value = profile.hourly_reply_limit;
  $("#profile-model").value = profile.participation_model || "";
  $("#profile-prefixes").value = profile.trigger_prefixes || "";
  $("#profile-poke-policy").value = profile.poke_policy || "allow";
  $("#profile-quote-mode").value = profile.quote_mode || "smart";
  $("#profile-active-hours").value = profile.active_hours || "";
  $("#profile-blocked-keywords").value = profile.blocked_keywords || "";
  $("#profile-delay-min").value = profile.reply_delay_min_ms ?? 200;
  $("#profile-delay-max").value = profile.reply_delay_max_ms ?? 900;
  $("#profile-digest-hours").value = profile.announcement_digest_hours ?? 24;
  $("#profile-welcome-message").value = profile.welcome_message || "";
  $("#profile-custom-prompt").value = profile.custom_prompt || "";
  $("#group-profile-status").textContent = `已记录消息 ${profile.messages_seen || 0} 条，已回复 ${profile.replies_sent || 0} 次。`;
}

async function loadGroupDetails(groupId) {
  const data = await api(`/api/admin/group-profiles/${groupId}/details`);
  state.groupKnowledge = data.knowledge;
  state.groupMembers = data.members;
  state.groupRelationships = data.relationships || [];
  state.groupFiles = [];
  state.groupFolderId = "";
  state.groupFolderTrail = [{id: "", name: "根目录"}];
  renderGroupKnowledge();
  renderGroupMembers();
  renderGroupRelationships();
  renderGroupFiles();
}

function renderGroupKnowledge() {
  $("#group-knowledge-list").innerHTML = state.groupKnowledge.length
    ? state.groupKnowledge.map((item) => `
      <article class="source-row">
        <div><span class="tag">${esc(item.kind)}</span> <strong>${esc(item.title)}</strong></div>
        <div class="row-meta">${esc(item.content)}</div>
        <button class="danger" data-delete-group-knowledge="${item.id}">删除</button>
      </article>`).join("")
    : `<div class="muted">暂无群资料</div>`;
  document.querySelectorAll("[data-delete-group-knowledge]").forEach((button) => {
    button.onclick = async () => {
      const groupId = Number($("#group-profile-select").value);
      await api(`/api/admin/group-profiles/${groupId}/knowledge/${button.dataset.deleteGroupKnowledge}`, {method:"DELETE"});
      await loadGroupDetails(groupId);
    };
  });
}

function renderGroupMembers() {
  $("#group-member-list").innerHTML = state.groupMembers.length
    ? state.groupMembers.map((item) => `
      <div class="row"><strong>${esc(item.nickname || item.user_id)}</strong> · QQ ${esc(item.user_id)}
      ${item.role ? `<span class="tag">${esc(item.role)}</span>` : ""}
      ${item.no_jokes ? `<span class="tag">不要开玩笑</span>` : ""}
      <div class="row-meta">${esc(item.notes || "无额外偏好")}</div></div>`).join("")
    : `<div class="muted">暂无成员偏好</div>`;
}

function renderGroupRelationships() {
  $("#group-relationship-list").innerHTML = state.groupRelationships.length
    ? state.groupRelationships.map((item) => {
      const nameA = item.name_a || item.user_a;
      const nameB = item.name_b || item.user_b;
      return `<div class="row"><strong>${esc(nameA)}</strong> ↔
        <strong>${esc(nameB)}</strong>
        <div class="row-meta">提及 ${esc(item.mention_count)} ·
        回复 ${esc(item.reply_count)} · 戳一戳 ${esc(item.poke_count)}</div></div>`;
    }).join("")
    : `<div class="muted">还没有形成稳定的成员互动关系。</div>`;
}

function renderGroupFiles(data = null) {
  if (data) {
    state.groupFiles = [
      ...(data.folders || []).map((item) => ({...item, kind: "folder"})),
      ...(data.files || []).map((item) => ({...item, kind: "file"})),
    ];
    state.groupFolderId = data.folder_id || "";
  }
  const breadcrumb = $("#group-file-breadcrumb");
  breadcrumb.innerHTML = state.groupFolderTrail.map((item, index) => `
    <button type="button" data-group-folder-index="${index}">${esc(item.name || "文件夹")}</button>
    ${index < state.groupFolderTrail.length - 1 ? "<span>/</span>" : ""}
  `).join("");
  breadcrumb.querySelectorAll("[data-group-folder-index]").forEach((button) => {
    button.onclick = () => {
      const index = Number(button.dataset.groupFolderIndex);
      const target = state.groupFolderTrail[index];
      if (!target) return;
      state.groupFolderTrail = state.groupFolderTrail.slice(0, index + 1);
      loadGroupFiles(target.id, target.name, false).catch((error) => {
        $("#group-profile-status").textContent = error.message;
      });
    };
  });
  $("#group-file-list").innerHTML = state.groupFiles.length
    ? state.groupFiles.map((item, index) => `
      <article class="source-row">
        <div><span class="tag">${item.kind === "folder" ? "文件夹" : "文件"}</span>
        <strong>${esc(item.name)}</strong></div>
        <div class="row-meta">${item.kind === "folder"
          ? `ID: ${esc(item.folder_id)} · ${esc(item.file_count || 0)} 项`
          : `${number(item.size)} 字节 · 上传者 ${esc(item.uploader || "未知")}`}</div>
        ${item.kind === "folder"
          ? `<button type="button" data-open-group-folder="${esc(item.folder_id)}" data-folder-name="${esc(item.name)}">打开</button>`
          : `<div class="file-actions"><button type="button" data-preview-group-index="${index}">查看</button><a class="button-link" href="${esc(groupFileContentUrl(item, true))}">下载</a></div>`}
      </article>`).join("")
    : `<div class="muted">尚未读取群文件。</div>`;
  document.querySelectorAll("[data-open-group-folder]").forEach((button) => {
    button.onclick = () => {
      const folderId = button.dataset.openGroupFolder || "";
      const folderName = button.dataset.folderName || "文件夹";
      state.groupFolderTrail.push({id: folderId, name: folderName});
      loadGroupFiles(folderId, folderName, false).catch((error) => {
        $("#group-profile-status").textContent = error.message;
      });
    };
  });
  document.querySelectorAll("[data-preview-group-index]").forEach((button) => {
    button.onclick = () => {
      const item = state.groupFiles[Number(button.dataset.previewGroupIndex)];
      if (item) previewGroupFile(item);
    };
  });
}

function groupFileContentUrl(item, download) {
  const params = new URLSearchParams({
    file_id: String(item.file_id || ""),
    busid: String(item.busid ?? ""),
    name: String(item.name || ""),
  });
  if (download) params.set("download", "true");
  const groupId = Number($("#group-profile-select").value);
  return `${scopedAdminPath(`/api/admin/group-profiles/${groupId}/file-content`)}?${params}`;
}

async function previewGroupFile(item) {
  const panel = $("#group-file-preview");
  panel.hidden = false;
  panel.innerHTML = `<div class="muted">正在读取并解析文件…</div>`;
  const groupId = Number($("#group-profile-select").value);
  const params = new URLSearchParams({
    file_id: String(item.file_id || ""),
    busid: String(item.busid ?? ""),
    name: String(item.name || ""),
  });
  try {
    const data = await api(`/api/admin/group-profiles/${groupId}/file-preview?${params}`);
    const inlineUrl = groupFileContentUrl(item, false);
    let body = "";
    if (data.preview_kind === "image") {
      body = `<img src="${esc(inlineUrl)}" alt="${esc(data.file_name || item.name)}">`;
    } else if (data.preview_kind === "audio") {
      body = `<audio controls src="${esc(inlineUrl)}"></audio>`;
    } else if (data.preview_kind === "video") {
      body = `<video controls src="${esc(inlineUrl)}"></video>`;
    } else if (data.preview_kind === "pdf") {
      body = `<iframe class="document-preview" src="${esc(inlineUrl)}" title="${esc(data.file_name || item.name)}"></iframe>`;
    } else if (data.preview_kind === "archive") {
      body = `<div class="archive-list">${(data.entries || []).map((entry) => `<code>${esc(entry)}</code>`).join("")}</div>`;
    } else if (data.excerpt) {
      body = `<pre class="content-preview">${esc(data.excerpt)}</pre>`;
    } else {
      body = `<p class="muted">${esc(data.message || "这个格式暂不支持网页内预览，请下载后打开。")}</p>`;
    }
    panel.innerHTML = `
      <div class="detail-head"><div><p class="eyebrow">FILE PREVIEW</p><h3>${esc(data.file_name || item.name)}</h3></div>
      <a class="button-link" href="${esc(groupFileContentUrl(item, true))}">下载文件</a></div>
      ${body}
      <p class="row-meta">${data.temporary_cache === false ? "仅在预览请求期间临时读取，不会长期保存到机器人本地。" : ""}</p>`;
  } catch (error) {
    panel.innerHTML = `<div class="error">${esc(error.message)}</div>`;
  }
}

async function loadGroupFiles(folderId = "", folderName = "", pushTrail = true) {
  const groupId = Number($("#group-profile-select").value);
  if (!groupId) return;
  $("#group-file-preview").hidden = true;
  $("#group-file-preview").innerHTML = "";
  const button = $("#load-group-files");
  button.disabled = true;
  button.textContent = "读取中…";
  try {
    const query = folderId ? `?folder_id=${encodeURIComponent(folderId)}` : "";
    const data = await api(`/api/admin/group-profiles/${groupId}/files${query}`);
    if (pushTrail && folderId) {
      state.groupFolderTrail.push({id: folderId, name: folderName || "文件夹"});
    }
    renderGroupFiles(data);
  } finally {
    button.disabled = false;
    button.textContent = "读取群文件";
  }
}

$("#group-profile-select").addEventListener("change", async (event) => {
  if (!event.target.value) return;
  const groupId = Number(event.target.value);
  renderGroupProfile(groupId);
  await loadGroupDetails(groupId);
});

$("#add-group-profile").addEventListener("click", async () => {
  const groupId = Number($("#new-group-id").value);
  const status = $("#group-profile-status");
  if (!Number.isInteger(groupId) || groupId <= 0) {
    status.textContent = "请填写有效的 QQ 群号。";
    return;
  }
  try {
    await jsonRequest("/api/admin/group-profiles", "POST", {
      group_id: groupId,
      name: $("#new-group-name").value.trim(),
    });
    $("#new-group-id").value = "";
    $("#new-group-name").value = "";
    await loadOperations();
    $("#group-profile-select").value = String(groupId);
    renderGroupProfile(groupId);
    await loadGroupDetails(groupId);
  } catch (error) { status.textContent = error.message; }
});

$("#save-group-profile").addEventListener("click", async () => {
  const groupId = Number($("#group-profile-select").value);
  const status = $("#group-profile-status");
  if (!groupId) {
    status.textContent = "请先选择群聊。";
    return;
  }
  try {
    await jsonRequest(`/api/admin/group-profiles/${groupId}`, "PATCH", {
      name: $("#profile-name").value.trim(),
      enabled: $("#profile-enabled").checked,
      require_mention: $("#profile-require-mention").checked,
      smart_participation: $("#profile-smart").checked,
      behavior_learning: $("#profile-learning").checked,
      quote_replies: $("#profile-quote").checked,
      quote_counts_as_mention: $("#profile-quote-mention").checked,
      auto_sticker_enabled: $("#profile-sticker").checked,
      avoid_last_word: $("#profile-avoid-last").checked,
      min_reply_confidence: Number($("#profile-confidence").value),
      reply_cooldown_seconds: Number($("#profile-cooldown").value),
      hourly_reply_limit: Number($("#profile-hourly-limit").value),
      participation_model: $("#profile-model").value.trim(),
      trigger_prefixes: $("#profile-prefixes").value.trim(),
      poke_policy: $("#profile-poke-policy").value,
      quote_mode: $("#profile-quote-mode").value,
      active_hours: $("#profile-active-hours").value.trim(),
      blocked_keywords: $("#profile-blocked-keywords").value.trim(),
      reply_delay_min_ms: Number($("#profile-delay-min").value),
      reply_delay_max_ms: Number($("#profile-delay-max").value),
      announcement_digest_hours: Number($("#profile-digest-hours").value),
      welcome_message: $("#profile-welcome-message").value.trim(),
      custom_prompt: $("#profile-custom-prompt").value.trim(),
    });
    await loadOperations();
    $("#group-profile-select").value = String(groupId);
    renderGroupProfile(groupId);
    await loadGroupDetails(groupId);
    status.textContent = "本群设置已保存并立即生效。";
  } catch (error) { status.textContent = error.message; }
});

$("#reset-group-profile").addEventListener("click", async () => {
  const groupId = Number($("#group-profile-select").value);
  if (!groupId || !confirm("恢复此群的全局默认设置，并清空该群的行为学习记录？")) return;
  try {
    await jsonRequest(`/api/admin/group-profiles/${groupId}/reset`, "POST", {
      reset_behavior: true,
    });
    await loadOperations();
    $("#group-profile-select").value = String(groupId);
    renderGroupProfile(groupId);
    await loadGroupDetails(groupId);
  } catch (error) {
    $("#group-profile-status").textContent = error.message;
  }
});

$("#add-group-knowledge").addEventListener("click", async () => {
  const groupId = Number($("#group-profile-select").value);
  if (!groupId) return;
  try {
    await jsonRequest(`/api/admin/group-profiles/${groupId}/knowledge`, "POST", {
      kind: $("#group-knowledge-kind").value,
      title: $("#group-knowledge-title").value.trim(),
      content: $("#group-knowledge-content").value.trim(),
    });
    $("#group-knowledge-title").value = "";
    $("#group-knowledge-content").value = "";
    await loadGroupDetails(groupId);
  } catch (error) {
    $("#group-profile-status").textContent = error.message;
  }
});

$("#save-group-member").addEventListener("click", async () => {
  const groupId = Number($("#group-profile-select").value);
  const memberId = Number($("#group-member-id").value);
  if (!groupId || !memberId) return;
  try {
    await jsonRequest(`/api/admin/group-profiles/${groupId}/members`, "POST", {
      user_id: memberId,
      nickname: $("#group-member-name").value.trim(),
      role: $("#group-member-role").value.trim(),
      notes: $("#group-member-notes").value.trim(),
      no_jokes: $("#group-member-no-jokes").checked,
    });
    $("#group-member-id").value = "";
    $("#group-member-name").value = "";
    $("#group-member-role").value = "";
    $("#group-member-notes").value = "";
    $("#group-member-no-jokes").checked = false;
    await loadGroupDetails(groupId);
  } catch (error) {
    $("#group-profile-status").textContent = error.message;
  }
});

$("#load-group-files").addEventListener("click", async () => {
  const folderId = $("#group-folder-id").value.trim();
  if (!folderId) state.groupFolderTrail = [{id: "", name: "根目录"}];
  try {
    await loadGroupFiles(
      folderId,
      folderId ? `文件夹 ${folderId}` : "根目录",
      Boolean(folderId),
    );
  } catch (error) {
    $("#group-profile-status").textContent = error.message;
  }
});

$("#group-files-up").addEventListener("click", async () => {
  if (state.groupFolderTrail.length <= 1) return;
  state.groupFolderTrail.pop();
  const parent = state.groupFolderTrail[state.groupFolderTrail.length - 1];
  try {
    await loadGroupFiles(parent.id, parent.name, false);
  } catch (error) {
    $("#group-profile-status").textContent = error.message;
  }
});

function renderGroupSettings(settings) {
  $("#group-enabled").checked = Boolean(settings.enable_group);
  $("#group-require-mention").checked = Boolean(settings.group_require_mention);
  $("#group-smart").checked = Boolean(settings.group_smart_participation);
  $("#group-learning").checked = Boolean(settings.group_behavior_learning);
  $("#quote-replies").checked = Boolean(settings.quote_replies);
  $("#group-quote-mention").checked = Boolean(settings.group_quote_counts_as_mention);
  $("#natural-pacing").checked = Boolean(settings.natural_pacing_enabled);
  $("#natural-split").checked = Boolean(settings.natural_split_replies);
  $("#group-confidence").value = settings.group_min_reply_confidence;
  $("#group-cooldown").value = settings.group_reply_cooldown_seconds;
  $("#group-model").value = settings.group_participation_model || "";
  $("#group-allowlist").value = settings.group_allowlist || "";
  $("#group-prefixes").value = settings.group_trigger_prefixes || "";
  $("#natural-delay-min").value = settings.natural_reply_delay_min_ms ?? 200;
  $("#natural-delay-max").value = settings.natural_reply_delay_max_ms ?? 900;
  $("#natural-debounce").value = settings.natural_message_debounce_ms ?? 0;
  $("#sticker-cooldown").value = settings.auto_sticker_cooldown_seconds ?? 90;
  $("#sticker-confidence").value = settings.sticker_min_confidence ?? 0.45;
}

$("#save-group-settings").addEventListener("click", async () => {
  const button = $("#save-group-settings");
  const status = $("#group-settings-status");
  status.textContent = "";
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const data = await jsonRequest("/api/admin/group-settings", "PATCH", {
      enable_group: $("#group-enabled").checked,
      group_require_mention: $("#group-require-mention").checked,
      group_smart_participation: $("#group-smart").checked,
      group_behavior_learning: $("#group-learning").checked,
      quote_replies: $("#quote-replies").checked,
      group_quote_counts_as_mention: $("#group-quote-mention").checked,
      natural_pacing_enabled: $("#natural-pacing").checked,
      natural_split_replies: $("#natural-split").checked,
      natural_reply_delay_min_ms: Number($("#natural-delay-min").value),
      natural_reply_delay_max_ms: Number($("#natural-delay-max").value),
      natural_message_debounce_ms: Number($("#natural-debounce").value),
      auto_sticker_cooldown_seconds: Number($("#sticker-cooldown").value),
      sticker_min_confidence: Number($("#sticker-confidence").value),
      group_min_reply_confidence: Number($("#group-confidence").value),
      group_reply_cooldown_seconds: Number($("#group-cooldown").value),
      group_participation_model: $("#group-model").value.trim(),
      group_allowlist: $("#group-allowlist").value.trim(),
      group_trigger_prefixes: $("#group-prefixes").value.trim(),
    });
    if ($("#apply-group-settings-all").checked) {
      await api("/api/admin/group-settings/apply-to-all", {method:"POST"});
    }
    renderGroupSettings(data.settings);
    status.textContent = "设置已保存并立即生效。";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "保存设置";
  }
});

$("#refresh-groups").addEventListener("click", () => loadOperations().catch((error) => alert(error.message)));
$("#save-pricing").addEventListener("click", async () => {
  try {
    await jsonRequest("/api/admin/usage/pricing", "POST", {
      input: Number($("#input-price").value || 0),
      output: Number($("#output-price").value || 0),
    });
    await loadOperations();
  } catch (error) { alert(error.message); }
});

async function loadEvolution() {
  const [adaptive, presenceData] = await Promise.all([
    api("/api/admin/adaptive"),
    api("/api/admin/presence"),
  ]);
  const global = presenceData.global || {};
  $("#presence-busy").checked = Boolean(global.busy);
  $("#presence-reason").value = global.reason || "";
  $("#presence-quiet-start").value = global.quiet_start || "";
  $("#presence-quiet-end").value = global.quiet_end || "";
  $("#presence-summary").textContent =
    `当前${global.busy ? "忙碌" : global.in_quiet_hours ? "处于勿扰时段" : "可以聊天"}，`
    + `未接消息 ${presenceData.backlog?.length || 0} 条。`;
  $("#adaptive-rollouts").innerHTML = adaptive.rollouts?.length
    ? adaptive.rollouts.map((item) => `
      <div class="row"><strong>${esc(item.scope_key)}</strong>
      <span class="tag">${esc(item.challenger_name)} ${Math.round(Number(item.weight) * 100)}%</span>
      <div class="row-meta">胜 ${esc(item.wins)} / 负 ${esc(item.losses)} · 样本 ${esc(item.samples)}</div></div>`).join("")
    : `<div class="muted">还没有达到启用阈值的自适应策略。</div>`;
  $("#adaptive-experiments").innerHTML = adaptive.experiments?.length
    ? adaptive.experiments.map((item) => `
      <article class="source-row">
        <div><strong>${esc(item.scope_key)}</strong>
        <span class="tag">${item.winner === "challenger" ? "候选更好" : "保留原版"}</span>
        ${item.observed_outcome ? `<span class="tag">后续：${esc(item.observed_outcome)}</span>` : ""}</div>
        <div class="row-meta">候选：${esc(item.challenger_reply)}</div>
        <div class="row-meta">原版：${esc(item.baseline_reply)}</div>
      </article>`).join("")
    : `<div class="muted">还没有影子回复对比记录。</div>`;
}

$("#refresh-evolution").addEventListener("click", () => {
  loadEvolution().catch((error) => alert(error.message));
});

$("#save-presence").addEventListener("click", async () => {
  try {
    await jsonRequest("/api/admin/presence", "PATCH", {
      scope_key: "global",
      busy: $("#presence-busy").checked,
      reason: $("#presence-reason").value.trim(),
      quiet_start: $("#presence-quiet-start").value.trim(),
      quiet_end: $("#presence-quiet-end").value.trim(),
    });
    await loadEvolution();
  } catch (error) {
    alert(error.message);
  }
});

$("#load-topics").addEventListener("click", async () => {
  const scopeKey = $("#topic-scope-key").value.trim();
  if (!scopeKey) return;
  try {
    const data = await api(`/api/admin/topics?scope_key=${encodeURIComponent(scopeKey)}`);
    $("#topic-list").innerHTML = data.topics?.length
      ? data.topics.map((item) => `
        <article class="source-row">
          <div><span class="tag">${esc(item.status)}</span> <strong>${esc(item.title)}</strong></div>
          <div class="row-meta">${esc(item.summary)}</div>
          ${item.pending_question ? `<div class="row-meta">等待回应：${esc(item.pending_question)}</div>` : ""}
          <button data-resolve-topic="${item.id}">标记完成</button>
        </article>`).join("")
      : `<div class="muted">这个范围没有进行中的话题。</div>`;
    document.querySelectorAll("[data-resolve-topic]").forEach((button) => {
      button.onclick = async () => {
        await api(`/api/admin/topics/${button.dataset.resolveTopic}/resolve`, {method:"POST"});
        $("#load-topics").click();
      };
    });
  } catch (error) {
    alert(error.message);
  }
});

function generatedKindLabel(kind) {
  return ({
    image: "图表",
    file: "文档/文件",
    code: "代码",
    archive: "ZIP 项目",
    audio: "语音",
  })[kind] || kind || "文件";
}

function generatedStatusLabel(item) {
  if (item.status === "ready") return "可下载";
  if (item.status === "running") return "生成中";
  return "失败";
}

async function loadGeneratedHistory() {
  const data = await api("/api/admin/generated?limit=40");
  state.generatedArtifacts = data.artifacts || [];
  state.generatedStorage = data.storage || null;
  renderGeneratedHistory();
  renderGeneratedStorage();
}

async function loadGenerationWorkspace() {
  await Promise.all([loadGeneratedHistory(), loadGenerationTargets()]);
}

async function loadGenerationTargets(force = false) {
  if (state.generationTargetsLoaded && !force) {
    renderGenerationTargets();
    return;
  }
  const data = await api("/api/admin/generated/targets");
  state.generationTargets = {
    people: data.people || [],
    groups: data.groups || [],
  };
  state.generationTargetsLoaded = true;
  renderGenerationTargets();
}

function renderGenerationTargets() {
  const select = $("#generate-send-target-select");
  if (!select) return;
  const type = $("#generate-send-type").value;
  const items = type === "group" ? state.generationTargets.groups : state.generationTargets.people;
  const placeholder = type === "group" ? "选择一个群聊" : "选择一个联系人";
  select.innerHTML = [
    `<option value="">${placeholder}</option>`,
    ...items.map((item) => {
      const id = type === "group" ? item.group_id : item.user_id;
      const name = type === "group"
        ? (item.name || `未命名群聊 ${id}`)
        : (item.display_name || `QQ ${id}`);
      return `<option value="${id}">${esc(name)} · ${id}</option>`;
    }),
  ].join("");
  if (!items.length) {
    select.innerHTML = `<option value="">暂无可选项，请在右侧手动填写</option>`;
  }
}

function renderGeneratedStorage() {
  const box = $("#generated-storage");
  if (!box) return;
  const storage = state.generatedStorage;
  if (!storage) {
    box.innerHTML = "";
    return;
  }
  const megabytes = Number(storage.total_bytes || 0) / 1024 / 1024;
  box.innerHTML = `
    <strong>本机保存：${esc(storage.path)}</strong><br>
    当前共有 ${number(storage.file_count)} 个生成文件，合计 ${number(megabytes, 2)} MB。重启电脑后仍会保留，点击单条记录的“删除”才会移除。
    群文件在线查看只会临时读取，不会加入这个目录。`;
}

function renderGeneratedHistory() {
  const list = $("#generated-history");
  list.innerHTML = state.generatedArtifacts.length
    ? state.generatedArtifacts.map((item) => `
      <article class="source-row generated-history-row">
        <div><span class="tag">${esc(generatedKindLabel(item.kind))}</span>
          <strong>${esc(item.display_name || "未命名生成任务")}</strong>
          <span class="tag">${esc(generatedStatusLabel(item))}</span></div>
        <div class="row-meta">${dateText(item.created_at)} · ${number(item.size)} 字节 · 来源 ${esc(item.source || "website")}</div>
        <div class="row-meta">${esc((item.prompt || "").slice(0, 180))}</div>
        ${item.status === "failed" ? `<div class="error">${esc(item.error || "生成失败")}</div>` : ""}
        <div class="file-actions">
          ${item.status === "ready" && item.available ? `<button type="button" data-view-generated="${item.id}">预览</button><a class="button-link" href="${esc(scopedAdminPath(item.download_url))}">下载</a>` : ""}
          <button type="button" data-reuse-generated="${item.id}">再来一次</button>
          <button type="button" class="danger" data-delete-generated="${item.id}">删除</button>
        </div>
      </article>`).join("")
    : `<div class="muted">还没有生成记录。生成过的文件在刷新或关机后仍会保留在这里。</div>`;
  list.querySelectorAll("[data-view-generated]").forEach((button) => {
    button.onclick = () => showGeneratedRecord(Number(button.dataset.viewGenerated));
  });
  list.querySelectorAll("[data-reuse-generated]").forEach((button) => {
    button.onclick = () => {
      const item = state.generatedArtifacts.find((entry) => entry.id === Number(button.dataset.reuseGenerated));
      if (!item) return;
      $("#generate-kind").value = item.kind === "image" ? "chart" : item.kind === "archive" || item.kind === "code" ? "code" : item.kind === "audio" ? "voice" : "file";
      $("#generate-prompt").value = item.prompt || "";
      $("#generate-file-name").value = item.display_name || "";
      $("#generate-error").textContent = "";
      $("#generate-result").innerHTML = "";
      $("#generate-send-box").hidden = true;
      $("#generate-prompt").focus();
    };
  });
  list.querySelectorAll("[data-delete-generated]").forEach((button) => {
    button.onclick = async () => {
      const id = Number(button.dataset.deleteGenerated);
      if (!confirm("删除这条生成记录和对应文件？")) return;
      await api(`/api/admin/generated/records/${id}`, {method: "DELETE"});
      if (state.selectedGenerated === id) {
        state.selectedGenerated = null;
        $("#generate-result").innerHTML = "";
        $("#generate-send-box").hidden = true;
      }
      await loadGeneratedHistory();
    };
  });
}

async function showGeneratedRecord(recordId) {
  const preview = await api(`/api/admin/generated/records/${recordId}/preview`);
  state.selectedGenerated = recordId;
  const item = preview.artifact;
  const downloadUrl = scopedAdminPath(preview.download_url);
  const inlineUrl = scopedAdminPath(preview.inline_url);
  let body = "";
  if (preview.preview_kind === "image") {
    body = `<img src="${esc(inlineUrl)}" alt="${esc(item.display_name)}">`;
  } else if (preview.preview_kind === "audio") {
    body = `<audio controls src="${esc(inlineUrl)}"></audio>`;
  } else if (preview.preview_kind === "video") {
    body = `<video controls src="${esc(inlineUrl)}"></video>`;
  } else if (preview.preview_kind === "pdf") {
    body = `<iframe class="document-preview" src="${esc(inlineUrl)}" title="${esc(item.display_name)}"></iframe>`;
  } else if (preview.preview_kind === "archive") {
    body = `<p><strong>文件清单</strong></p><div class="archive-list">${(preview.entries || []).map((entry) => `<code>${esc(entry)}</code>`).join("")}</div>`;
  } else if (preview.excerpt) {
    body = `<pre class="content-preview">${esc(preview.excerpt)}</pre>`;
  } else {
    body = `<p class="muted">${esc(preview.message || "这类文件没有网页内预览，请下载后打开。")}</p>`;
  }
  $("#generate-result").innerHTML = `
    <div class="generated-result">
      <span class="tag">${esc(generatedKindLabel(item.kind))}</span>
      <strong>${esc(item.display_name)}</strong>
      <p class="row-meta">${esc(item.summary || "")}</p>
      <div class="item-actions">
        <a class="button-link" href="${esc(downloadUrl)}">下载文件</a>
        <a class="button-link" target="_blank" rel="noopener" href="${esc(inlineUrl)}">浏览器打开</a>
      </div>
      ${body}
    </div>`;
  $("#generate-send-box").hidden = false;
  $("#generate-send-status").textContent = "";
  loadGenerationTargets().catch(() => {});
}

$("#refresh-generated").addEventListener("click", () => {
  loadGenerationWorkspace().catch((error) => {
    $("#generate-error").textContent = error.message;
  });
});

$("#generate-send-type").addEventListener("change", () => {
  $("#generate-send-target").value = "";
  renderGenerationTargets();
});

$("#generate-send-target-select").addEventListener("change", (event) => {
  if (event.target.value) {
    $("#generate-send-target").value = event.target.value;
  }
});

$("#generate-send").addEventListener("click", async () => {
  const errorBox = $("#generate-error");
  const status = $("#generate-send-status");
  errorBox.textContent = "";
  status.textContent = "";
  if (!state.selectedGenerated) {
    errorBox.textContent = "请先生成或选择一个文件。";
    return;
  }
  const targetId = Number($("#generate-send-target").value);
  if (!Number.isInteger(targetId) || targetId <= 0) {
    errorBox.textContent = "请从列表选择联系人或群聊，也可以手动填写有效 QQ 号/群号。";
    return;
  }
  const button = $("#generate-send");
  button.disabled = true;
  button.textContent = "发送中…";
  try {
    const data = await jsonRequest(`/api/admin/generated/records/${state.selectedGenerated}/send`, "POST", {
      target_type: $("#generate-send-type").value,
      target_id: targetId,
    });
    status.textContent = data.message;
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "发送当前文件";
  }
});

$("#generate-submit").addEventListener("click", async () => {
  const button = $("#generate-submit");
  const errorBox = $("#generate-error");
  const resultBox = $("#generate-result");
  errorBox.textContent = "";
  resultBox.innerHTML = "";
  button.disabled = true;
  button.textContent = "生成中…";
  try {
    const result = await jsonRequest("/api/admin/generate", "POST", {
      kind: $("#generate-kind").value,
      prompt: $("#generate-prompt").value,
      file_name: $("#generate-file-name").value.trim(),
      research: $("#generate-research").checked,
    });
    resultBox.innerHTML = `<div class="muted">文件已生成，正在加载预览…</div>`;
    await showGeneratedRecord(result.record_id);
    await loadGeneratedHistory();
  } catch (error) {
    errorBox.textContent = error.message;
    await loadGeneratedHistory().catch(() => {});
  } finally {
    button.disabled = false;
    button.textContent = "开始生成";
  }
});

Promise.all([loadOverview(), loadPeople()]).catch((error) => {
  $("#overview-cards").innerHTML = `<div class="error">${esc(error.message)}</div>`;
});
