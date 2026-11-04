const state = {
  selectedUser: null,
  people: [],
  selectedMemories: new Set(),
};

const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[char]));

async function api(path, options = {}) {
  const response = await fetch(path, options);
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
      stickers: loadStickers,
      knowledge: loadKnowledge,
      operations: loadOperations,
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
        <small>${esc(item.reason)}；当前采用较新的说法。</small>
      </div>`).join("")}`
    : `<div class="muted">没有检测到记忆冲突。</div>`;
  $("#memory-timeline").innerHTML = data.memories.length
    ? data.memories.map((item) => memoryCard(item)).join("")
    : `<div class="empty">还没有长期记忆。机器人会在后续对话和反思中自动提取。</div>`;
  bindMemoryCards();
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
    ? data.stickers.map((item) => `
      <article class="sticker">
        <div class="sticker-preview">${item.image_source ? `<img src="${esc(item.image_source)}" alt="${esc(item.summary)}" loading="lazy">` : `<span>${esc(item.summary)}</span>`}</div>
        <strong>${esc(item.summary)}</strong>
        <div class="row-meta">emoji ${esc(item.emoji_id || "-")} · 使用 ${esc(item.use_count)} 次</div>
        <label>分类<input data-sticker-category="${item.id}" value="${esc(item.category || "未分类")}"></label>
        <label>适用场景<input data-sticker-hint="${item.id}" value="${esc(item.usage_hint || "")}" placeholder="例如：无语、震惊、庆祝"></label>
        <label class="check"><input type="checkbox" data-sticker-enabled="${item.id}" ${item.enabled ? "checked" : ""}> 启用自动选择</label>
        <div class="item-actions"><button data-save-sticker="${item.id}">保存</button><button class="danger" data-delete-sticker="${item.id}">删除</button></div>
      </article>`).join("")
    : `<div class="empty">表情库为空。先让别人给机器人发几个表情包。</div>`;
  bindStickerActions();
}

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
  const [usage, groups] = await Promise.all([
    api("/api/admin/usage"),
    api("/api/admin/group-behavior"),
  ]);
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
  $("#usage-models").innerHTML = `<table><thead><tr><th>模型</th><th>调用</th><th>失败</th><th>平均延迟</th><th>输入 Token</th><th>输出 Token</th></tr></thead><tbody>${usage.by_model.length ? usage.by_model.map((item) => `<tr><td>${esc(item.model)}</td><td>${esc(item.calls)}</td><td>${esc(item.errors)}</td><td>${number(item.avg_ms)} ms</td><td>${number(item.input_tokens)}</td><td>${number(item.output_tokens)}</td></tr>`).join("") : `<tr><td colspan="6">暂无调用记录</td></tr>`}</tbody></table>`;
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
    });
    resultBox.innerHTML = `<div class="generated-result"><strong>${esc(result.summary)}</strong><br><a href="${esc(result.download_url)}">下载 ${esc(result.name)}</a>${result.kind === "image" ? `<br><img src="${esc(result.download_url)}" alt="${esc(result.name)}">` : ""}</div>`;
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "开始生成";
  }
});

Promise.all([loadOverview(), loadPeople()]).catch((error) => {
  $("#overview-cards").innerHTML = `<div class="error">${esc(error.message)}</div>`;
});
