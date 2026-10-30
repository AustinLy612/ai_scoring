/* 日记自我反思 AI 编码评分工具 —— 前端逻辑 */

const STEP_NAMES = { 1: "自我觉察", 2: "触发识别", 3: "重新评估", 4: "评价", 5: "着眼未来" };
const DEPTH_NAMES = { 0: "没有描述", 1: "有一些描述", 2: "明确且深度描述" };

const state = {
  config: null,
  diaries: [],
  currentId: null,
  diary: null,
  segments: [], // 本地可编辑副本
  scores: [], // 五个步骤深度分的本地可编辑副本
  selection: null,
  dirty: false,
  focusId: null,
  pollTimer: null,
  newCounter: 0,
};

const el = (id) => document.getElementById(id);

/* ---------------------------------------------------------------- 工具 */

function toast(message, isError = false) {
  const box = el("toast");
  box.textContent = message;
  box.classList.toggle("error", isError);
  box.classList.remove("hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => box.classList.add("hidden"), isError ? 6000 : 3000);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch (_) { /* 非 JSON 响应 */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

/* ------------------------------------------------------------ 初始化 */

async function init() {
  buildStepOptions(el("newStep"));
  buildLegend();
  bindEvents();
  try {
    state.config = await api("/api/config");
    const c = state.config;
    el("modelInfo").textContent =
      `模型 ${c.model}｜SSH ${c.ssh_host || "未设置"}｜并发 ${c.max_concurrency}` +
      (c.ssh_configured ? "" : "｜未配置 SSH");
    if (!c.ssh_configured) {
      toast("未配置 SSH 登录信息，请复制 .env.example 为 .env 并填写 LLM_SSH_USER / LLM_SSH_PASSWORD 后重启服务。", true);
    }
  } catch (err) {
    toast(`读取配置失败：${err.message}`, true);
  }
  await refreshStatus();
}

function buildStepOptions(select, includeEmpty = false) {
  select.innerHTML = "";
  if (includeEmpty) select.appendChild(new Option("未选", ""));
  for (let i = 1; i <= 5; i += 1) select.appendChild(new Option(`${i} ${STEP_NAMES[i]}`, String(i)));
}

function buildLegend() {
  el("legend").innerHTML = Object.entries(STEP_NAMES)
    .map(([step, name]) => `<span class="legend-item" style="background: var(--step${step})">${step} ${name}</span>`)
    .join("") + '<span class="legend-item legend-multi">斜纹 = 多步骤重叠</span>';
}

function bindEvents() {
  el("fileInput").addEventListener("change", uploadFiles);
  el("pasteBtn").addEventListener("click", () => el("pasteModal").classList.remove("hidden"));
  el("pasteCancel").addEventListener("click", () => el("pasteModal").classList.add("hidden"));
  el("pasteConfirm").addEventListener("click", submitPaste);
  el("processBtn").addEventListener("click", startProcessing);
  el("exportBtn").addEventListener("click", exportExcel);
  el("clearBtn").addEventListener("click", clearAll);
  el("saveBtn").addEventListener("click", saveSegments);
  el("revertBtn").addEventListener("click", () => openDiary(state.currentId, true));
  el("addSegmentBtn").addEventListener("click", addSegmentFromSelection);
  el("diaryBody").addEventListener("mouseup", handleSelection);
  window.addEventListener("beforeunload", (e) => {
    if (state.dirty) { e.preventDefault(); e.returnValue = ""; }
  });
}

/* -------------------------------------------------------------- 导入 */

async function uploadFiles(event) {
  const files = [...event.target.files];
  event.target.value = "";
  if (!files.length) return;

  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  try {
    const data = await api("/api/import", { method: "POST", body: form });
    renderDiaryList(data.diaries);
    if (data.errors.length) toast(`部分文件导入失败：\n${data.errors.join("\n")}`, true);
    else toast(`已导入 ${data.added.length} 篇日记`);
  } catch (err) {
    toast(`导入失败：${err.message}`, true);
  }
}

async function submitPaste() {
  const text = el("pasteText").value;
  if (!text.trim()) { toast("请输入日记内容", true); return; }
  try {
    const data = await api("/api/import/text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: el("pasteId").value, text }),
    });
    renderDiaryList(data.diaries);
    el("pasteId").value = "";
    el("pasteText").value = "";
    el("pasteModal").classList.add("hidden");
    toast(`已添加日记 ${data.added[0]}`);
  } catch (err) {
    toast(`添加失败：${err.message}`, true);
  }
}

async function clearAll() {
  if (!state.diaries.length) return;
  if (!confirm("确定清空所有日记与标注结果？未导出的结果将丢失。")) return;
  try {
    const data = await api("/api/diaries", { method: "DELETE" });
    resetCurrent();
    renderDiaryList(data.diaries);
  } catch (err) {
    toast(err.message, true);
  }
}

async function deleteDiary(diaryId) {
  if (!confirm(`删除日记 ${diaryId}？`)) return;
  try {
    const data = await api(`/api/diary/${encodeURIComponent(diaryId)}`, { method: "DELETE" });
    if (state.currentId === diaryId) resetCurrent();
    renderDiaryList(data.diaries);
  } catch (err) {
    toast(err.message, true);
  }
}

function resetCurrent() {
  state.currentId = null;
  state.diary = null;
  state.segments = [];
  state.scores = [];
  state.dirty = false;
  el("diaryTitle").textContent = "日记全文";
  el("diaryBody").textContent = "从左侧选择一篇日记查看标注结果。";
  el("scoreTable").textContent = "未选择日记。";
  el("segmentTable").textContent = "";
  el("totalDepth").textContent = "";
  el("warnings").classList.add("hidden");
}

/* ------------------------------------------------------------ 批量处理 */

async function startProcessing() {
  try {
    const data = await api("/api/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ diary_ids: [], force: el("forceBox").checked }),
    });
    renderDiaryList(data.diaries);
    if (!data.started) toast("没有需要处理的日记（勾选“重跑已完成”可重新处理）。");
    else { toast(`已开始处理 ${data.started} 篇日记`); startPolling(); }
  } catch (err) {
    toast(`启动处理失败：${err.message}`, true);
  }
}

function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(refreshStatus, 2000);
}

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    const before = new Map(state.diaries.map((d) => [d.id, d.status]));
    renderDiaryList(data.diaries);

    if (!data.processing && state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    el("processBtn").disabled = data.processing;

    // 当前打开的日记刚处理完，且没有未保存修改时自动刷新
    const current = data.diaries.find((d) => d.id === state.currentId);
    if (current && before.get(current.id) !== current.status && current.status === "done" && !state.dirty) {
      await openDiary(current.id, true);
    }
  } catch (err) {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    toast(`状态刷新失败：${err.message}`, true);
  }
}

/* ------------------------------------------------------------ 日记列表 */

const STATUS_LABEL = { pending: "待处理", processing: "处理中", done: "已完成", failed: "失败" };

function renderDiaryList(diaries) {
  state.diaries = diaries;
  el("listCount").textContent = `${diaries.length} 篇`;

  const list = el("diaryList");
  list.innerHTML = "";
  diaries.forEach((d) => {
    const li = document.createElement("li");
    li.className = d.id === state.currentId ? "active" : "";
    li.innerHTML = `
      <div class="row">
        <span class="id">${escapeHtml(d.id)}</span>
        <span class="status ${d.status}">${STATUS_LABEL[d.status] || d.status}</span>
      </div>
      <div class="row">
        <span class="muted">${d.char_count} 字｜${d.segment_count} 片段${d.total_depth !== null && d.total_depth !== undefined ? `｜深度 ${d.total_depth}/10` : ""}${d.reviewed ? "｜已修正" : ""}</span>
        <button class="del" title="删除">✕</button>
      </div>
      ${d.error ? `<div class="muted" style="color:#b3352f">${escapeHtml(d.error)}</div>` : ""}
    `;
    li.querySelector(".del").addEventListener("click", (e) => { e.stopPropagation(); deleteDiary(d.id); });
    li.addEventListener("click", () => openDiary(d.id));
    list.appendChild(li);
  });
}

/* ------------------------------------------------------------ 打开日记 */

async function openDiary(diaryId, force = false) {
  if (!diaryId) return;
  if (!force && state.dirty && diaryId !== state.currentId) {
    if (!confirm("当前日记有未保存的修正，切换将丢失。继续？")) return;
  }
  try {
    const diary = await api(`/api/diary/${encodeURIComponent(diaryId)}`);
    state.currentId = diary.id;
    state.diary = diary;
    state.segments = diary.segments.map((s) => ({
      id: s.id,
      start: s.start,
      end: s.end,
      text: s.text,
      step: s.step,
      step_reason: s.step_reason || "",
      deleted: s.status === "deleted",
      source: s.source,
      ai_step: s.ai_step,
      fuzzy_located: s.fuzzy_located,
    }));
    state.scores = [1, 2, 3, 4, 5].map((step) => {
      const found = (diary.step_scores || []).find((s) => s.step === step) || {};
      return {
        step,
        score: found.score ?? null,
        reason: found.reason || "",
        ai_score: found.ai_score ?? null,
        ai_reason: found.ai_reason || "",
      };
    });
    state.dirty = false;
    state.selection = null;
    state.focusId = null;
    state.newCounter = 0;
    el("selectionBar").classList.add("hidden");
    renderDiaryList(state.diaries);
    renderDiary();
  } catch (err) {
    toast(`打开日记失败：${err.message}`, true);
  }
}

function renderDiary() {
  const diary = state.diary;
  el("diaryTitle").textContent = `${diary.id}（${diary.text.length} 字${diary.source_name ? "，来源 " + diary.source_name : ""}）`;

  const messages = [...(diary.warnings || [])];
  if (diary.error) messages.push(`处理失败：${diary.error}`);
  const warnBox = el("warnings");
  if (messages.length) {
    warnBox.innerHTML = `<strong>处理提示</strong><ul>${messages.map((m) => `<li>${escapeHtml(m)}</li>`).join("")}</ul>`;
    warnBox.classList.remove("hidden");
  } else {
    warnBox.classList.add("hidden");
  }

  renderBody();
  renderScores();
  renderSegments();
}

/* ---------------------------------------------------------- 步骤评分 */

function renderScores() {
  const host = el("scoreTable");
  host.innerHTML = "";

  if (state.diary.status !== "done" && state.scores.every((s) => s.score === null)) {
    host.innerHTML = '<p class="muted">尚未处理。点击顶部“开始处理”后查看结果。</p>';
    el("totalDepth").textContent = "";
    return;
  }

  const activeSegs = state.segments.filter((s) => !s.deleted);
  state.scores.forEach((item) => {
    const count = activeSegs.filter((s) => s.step === item.step).length;
    const row = document.createElement("div");
    row.className = `score-row s${item.step}`;

    // 片段数与分数不一致时提示（有片段却 0 分，或无片段却有分）
    const mismatch = item.score !== null && ((count === 0 && item.score > 0) || (count > 0 && item.score === 0));
    const tags = [];
    if (item.ai_score !== null && item.score !== item.ai_score) {
      tags.push(`<span class="tag ai-diff">AI原判 ${item.ai_score} ${DEPTH_NAMES[item.ai_score]}</span>`);
    }
    if (item.score === null) tags.push('<span class="tag fuzzy">未评分</span>');

    row.innerHTML = `
      <div class="step-label">${item.step} ${STEP_NAMES[item.step]}</div>
      <select class="score-select"></select>
      <div class="seg-count ${mismatch ? "mismatch" : ""}">${count} 个片段${mismatch ? "（与分数不一致，请核对）" : ""}</div>
      ${tags.length ? `<div class="score-tags">${tags.join("")}</div>` : ""}
      <textarea class="score-reason" rows="2" placeholder="评分理由">${escapeHtml(item.reason)}</textarea>
    `;

    const select = row.querySelector(".score-select");
    select.appendChild(new Option("未评分", ""));
    for (let v = 0; v <= 2; v += 1) select.appendChild(new Option(`${v} ${DEPTH_NAMES[v]}`, String(v)));
    select.value = item.score === null ? "" : String(item.score);
    select.addEventListener("change", () => {
      item.score = select.value === "" ? null : Number(select.value);
      state.dirty = true;
      renderScores();
    });

    row.querySelector(".score-reason").addEventListener("input", (e) => {
      item.reason = e.target.value;
      state.dirty = true;
    });

    host.appendChild(row);
  });

  const values = state.scores.map((s) => s.score);
  el("totalDepth").textContent = values.every((v) => v !== null)
    ? `总深度分 ${values.reduce((a, b) => a + b, 0)} / 10`
    : "总深度分 —（有步骤未评分）";
}

/* -------------------------------------------------------------- 高亮 */

function renderBody() {
  const text = state.diary.text;
  const active = state.segments.filter((s) => !s.deleted);
  const body = el("diaryBody");
  body.innerHTML = "";

  const points = new Set([0, text.length]);
  active.forEach((s) => { points.add(s.start); points.add(s.end); });
  const bounds = [...points].filter((p) => p >= 0 && p <= text.length).sort((a, b) => a - b);

  const frag = document.createDocumentFragment();
  for (let i = 0; i < bounds.length - 1; i += 1) {
    const from = bounds[i];
    const to = bounds[i + 1];
    if (from >= to) continue;
    const covering = active.filter((s) => s.start <= from && s.end >= to);
    const chunk = text.slice(from, to);

    if (!covering.length) {
      const span = document.createElement("span");
      span.dataset.start = String(from);
      span.textContent = chunk;
      frag.appendChild(span);
      continue;
    }

    const mark = document.createElement("mark");
    mark.dataset.start = String(from);
    mark.dataset.segIds = covering.map((s) => s.id).join(",");
    mark.className = `s${covering[0].step}${covering.length > 1 ? " multi" : ""}` +
      (state.focusId && covering.some((s) => s.id === state.focusId) ? " focus" : "");
    if (covering.length > 1) {
      // 多步骤重叠：用各步骤颜色的斜条纹表示
      const width = 9;
      const stops = covering
        .map((s, k) => `var(--step${s.step}) ${k * width}px, var(--step${s.step}) ${(k + 1) * width}px`)
        .join(", ");
      mark.style.background = `repeating-linear-gradient(45deg, ${stops})`;
    }
    mark.title = covering.map((s) => `步骤${s.step} ${STEP_NAMES[s.step]}`).join("\n");
    mark.textContent = chunk;
    mark.addEventListener("click", () => focusSegment(covering[0].id));
    frag.appendChild(mark);
  }
  body.appendChild(frag);
}

function focusSegment(segmentId) {
  state.focusId = segmentId;
  renderBody();
  renderSegments();
  const card = document.querySelector(`[data-card-id="${cssEscape(segmentId)}"]`);
  if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

/* ---------------------------------------------------------- 选中新增 */

function handleSelection() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) {
    state.selection = null;
    el("selectionBar").classList.add("hidden");
    return;
  }
  const range = sel.getRangeAt(0);
  const body = el("diaryBody");
  if (!body.contains(range.commonAncestorContainer)) return;

  const start = offsetOf(range.startContainer, range.startOffset);
  const end = offsetOf(range.endContainer, range.endOffset);
  if (start === null || end === null || start >= end) {
    state.selection = null;
    el("selectionBar").classList.add("hidden");
    return;
  }

  state.selection = { start, end };
  const preview = state.diary.text.slice(start, end);
  el("selectionInfo").textContent =
    `已选中 [${start}, ${end}) 共 ${end - start} 字：${preview.length > 30 ? preview.slice(0, 30) + "…" : preview}`;
  el("selectionBar").classList.remove("hidden");
}

function offsetOf(node, offset) {
  if (node.nodeType === Node.TEXT_NODE) {
    const holder = node.parentElement;
    if (holder && holder.dataset.start !== undefined) return Number(holder.dataset.start) + offset;
    return null;
  }
  // 选区端点落在容器元素上：offset 是子节点索引
  const children = [...node.childNodes];
  if (offset < children.length) {
    const child = children[offset];
    if (child.dataset && child.dataset.start !== undefined) return Number(child.dataset.start);
  }
  const last = children[children.length - 1];
  if (last && last.dataset && last.dataset.start !== undefined) {
    return Number(last.dataset.start) + (last.textContent || "").length;
  }
  return null;
}

function addSegmentFromSelection() {
  if (!state.selection) return;
  const { start, end } = state.selection;
  state.newCounter += 1;
  state.segments.push({
    id: `new-${state.newCounter}`,
    start,
    end,
    text: state.diary.text.slice(start, end),
    step: Number(el("newStep").value),
    step_reason: "",
    deleted: false,
    source: "human_added",
    ai_step: null,
    fuzzy_located: false,
  });
  state.segments.sort((a, b) => a.start - b.start || a.end - b.end || a.step - b.step);
  state.dirty = true;
  state.selection = null;
  el("selectionBar").classList.add("hidden");
  window.getSelection().removeAllRanges();
  renderBody();
  renderScores();
  renderSegments();
  toast("已新增片段，记得核对该步骤的深度分并点击“保存修正”。");
}

/* ------------------------------------------------------------ 片段编辑 */

function renderSegments() {
  const host = el("segmentTable");
  host.innerHTML = "";

  if (!state.segments.length) {
    host.innerHTML = state.diary.status === "done"
      ? '<p class="muted">该日记未标注出任何 self-reflection 片段。可在左侧全文中选中文字手动新增。</p>'
      : '<p class="muted">尚未处理。点击顶部“开始处理”后查看结果。</p>';
    return;
  }

  state.segments.forEach((seg, index) => {
    const card = document.createElement("div");
    card.className = `seg-card s${seg.step}${seg.deleted ? " deleted" : ""}${seg.id === state.focusId ? " focus" : ""}`;
    card.dataset.cardId = seg.id;

    const overlaps = state.segments
      .filter((o) => !o.deleted && o.id !== seg.id && o.start < seg.end && seg.start < o.end)
      .map((o) => `步骤${o.step}`);

    const tags = [];
    if (seg.source === "human_added") tags.push('<span class="tag human">人工新增</span>');
    if (seg.fuzzy_located) tags.push('<span class="tag fuzzy">模糊定位</span>');
    if (seg.ai_step && seg.step !== seg.ai_step) {
      tags.push(`<span class="tag ai-diff">AI原判 步骤${seg.ai_step}</span>`);
    }
    if (overlaps.length) tags.push(`<span class="tag">重叠：${overlaps.join("、")}</span>`);

    card.innerHTML = `
      <div class="seg-head">
        <span class="sid">#${index + 1}　${escapeHtml(seg.id)}　[${seg.start}, ${seg.end})</span>
        ${tags.join("")}
      </div>
      <p class="seg-text">${escapeHtml(seg.text)}</p>
      <div class="seg-controls">
        <label>步骤 <select class="step-select"></select></label>
        <button class="btn locate-btn">定位</button>
        <button class="btn ${seg.deleted ? "" : "danger"} del-btn">${seg.deleted ? "恢复" : "删除"}</button>
      </div>
      <textarea class="seg-reason step-reason" rows="2" placeholder="步骤判断理由">${escapeHtml(seg.step_reason)}</textarea>
    `;

    const stepSelect = card.querySelector(".step-select");
    buildStepOptions(stepSelect);
    stepSelect.value = String(seg.step);
    stepSelect.addEventListener("change", () => {
      seg.step = Number(stepSelect.value);
      state.dirty = true;
      renderBody();
      renderScores();
      renderSegments();
    });

    card.querySelector(".locate-btn").addEventListener("click", () => {
      state.focusId = seg.id;
      renderBody();
      renderSegments();
      const mark = [...document.querySelectorAll("#diaryBody mark")].find((m) =>
        (m.dataset.segIds || "").split(",").includes(seg.id));
      if (mark) mark.scrollIntoView({ block: "center", behavior: "smooth" });
    });

    card.querySelector(".del-btn").addEventListener("click", () => {
      if (seg.deleted) {
        seg.deleted = false;
      } else if (seg.id.startsWith("new-")) {
        state.segments = state.segments.filter((s) => s !== seg);
      } else {
        seg.deleted = true;
      }
      state.dirty = true;
      renderBody();
      renderScores();
      renderSegments();
    });

    card.querySelector(".step-reason").addEventListener("input", (e) => {
      seg.step_reason = e.target.value;
      state.dirty = true;
    });

    host.appendChild(card);
  });
}

async function saveSegments() {
  if (!state.currentId) { toast("请先选择日记", true); return; }
  const payload = {
    segments: state.segments.map((s) => ({
      id: s.id,
      start: s.start,
      end: s.end,
      text: s.text,
      step: s.step,
      step_reason: s.step_reason,
      deleted: s.deleted,
    })),
    step_scores: state.scores.map((s) => ({
      step: s.step,
      score: s.score,
      reason: s.reason,
    })),
  };
  try {
    await api(`/api/diary/${encodeURIComponent(state.currentId)}/review`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.dirty = false;
    await openDiary(state.currentId, true);
    await refreshStatus();
    toast("修正已保存");
  } catch (err) {
    toast(`保存失败：${err.message}`, true);
  }
}

/* -------------------------------------------------------------- 导出 */

async function exportExcel() {
  if (state.dirty && !confirm("当前日记有未保存的修正，导出将不包含它们。继续？")) return;
  try {
    const response = await fetch("/api/export.xlsx");
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `${response.status} ${response.statusText}`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="?([^"]+)"?/);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = match ? match[1] : "coding_result.xlsx";
    link.click();
    URL.revokeObjectURL(link.href);
    toast("已导出 Excel");
  } catch (err) {
    toast(`导出失败：${err.message}`, true);
  }
}

/* -------------------------------------------------------------- 杂项 */

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/["\\]/g, "\\$&");
}

init();
