(() => {
  "use strict";
  const { state, els, $, node, api, toast, priceDisplay, loadQuotes, loadCatalog, appendMessage, appendProgress, renderToolActivity, checkAgent, renderFileChips, pricingDisplay } = window.TokenSQL;
  const terminal = new Set(["cancelled", "confirmed", "failed"]);
  let currentJob = null;
  const waitingCard = $("waitingPanel");
  let nextUpdateRequest = null;
  let reviewDecisions = [];
  let reconciliationDirty = false;
  let waitingAttempt = null;
  let waitingSupplierId = null;
  const csvValues = (value) => value.split(/[,，]/).map((item) => item.trim()).filter(Boolean);
  function supplierOptions(select, placeholder = "请选择已确认供应商") {
    const active = select.value;
    select.replaceChildren(node("option", { text: placeholder })); select.firstChild.value = "";
    state.catalog.suppliers.forEach((supplier) => { const option = node("option", { text: supplier.name }); option.value = supplier.supplier_id; select.append(option); });
    if ([...select.options].some((option) => option.value === active)) select.value = active;
  }
  function resetConversation() { els.conversation.replaceChildren(waitingCard); waitingCard.hidden = true; waitingAttempt = null; }

  let reviewBundle = null;
  let reviewDirty = false;
  let reviewBusy = false;
  let selectedUploads = new Set();
  let pollGeneration = 0;
  let sessionGeneration = 0;
  const historyAnchors = new Map();
  const renderedReplies = new Set();
  const readyStatuses = new Set(["ready", "review"]);
  const waitingStatus = "waiting_for_input";


  const supplierDrafts = new Map();
  let supplierRouteGeneration = 0, activeDefaultPrompt = "", supplierContext = null, sessionItems = [];
  function fillConversationSuppliers() {
    const select = $("conversationSupplier"), active = state.supplierId || select.value;
    select.replaceChildren(node("option", { text: "选择供应商" })); select.firstChild.value = "";
    state.catalog.suppliers.forEach((supplier) => { const item = node("option", { text: supplier.name }); item.value = supplier.supplier_id; select.append(item); });
    select.value = active || "";
  }
  function rememberSupplier(id) {
    state.supplierId = id || null;
    $("conversationSupplier").value = id || "";
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("supplier", id); else { url.searchParams.delete("supplier"); url.searchParams.delete("scope"); }
    window.history.replaceState(null, "", url);
  }
  function stashSupplierDraft() {
    supplierDrafts.set(state.supplierId || "", { files: [...state.files], uploads: [...selectedUploads], prompt: els.prompt.value,
      scope: $("importSourceScope").value, persist: $("persistProfile").checked, remember: $("rememberPrompt").checked,
      updateRequest: nextUpdateRequest, fullReparse: $("fullReparse").checked });
  }
  function restoreSupplierDraft(id) {
    const draft = supplierDrafts.get(id || "");
    state.files = [...(draft?.files || [])]; selectedUploads = new Set(draft?.uploads || []);
    els.prompt.value = draft?.prompt || ""; $("importSourceScope").value = draft?.scope || "";
    $("fullReparse").checked = draft?.fullReparse ?? false;
    $("persistProfile").checked = draft?.persist ?? true; $("rememberPrompt").checked = draft?.remember ?? false;
    nextUpdateRequest = draft?.updateRequest || null;
    $("preparedUpdateNotice").hidden = !nextUpdateRequest;
    $("supplierUpdateButton").textContent = nextUpdateRequest ? "更新模式已准备" : "更新供应商报价";
    if (nextUpdateRequest) $("preparedUpdateText").textContent = "待更新：" + (state.catalog.suppliers.find((s) => s.supplier_id === id)?.name || "") + " / " + nextUpdateRequest.source_scope;
    renderFileChips();
  }
  function applySupplierProfile(profile, sourceScope = $("importSourceScope").value.trim() || "default") {
    const previousDefault = activeDefaultPrompt;
    const source = profile?.sources?.[sourceScope];
    activeDefaultPrompt = source?.default_prompt ?? profile?.default_prompt ?? "";
    const list = $("supplierSourceScopes"); list.replaceChildren();
    Object.keys(profile?.sources || {}).forEach((name) => { const item = node("option"); item.value = name; list.append(item); });
    if (!els.prompt.value.trim() || els.prompt.value === previousDefault) els.prompt.value = activeDefaultPrompt;
    const name = state.catalog.suppliers.find((s) => s.supplier_id === state.supplierId)?.name;
    $("supplierContextSummary").textContent = name ? name + " · " + (sourceScope === "default" ? "默认系列" : sourceScope) + (profile ? " · 档案 v" + profile.revision : "") : "选择供应商，继续对应对话";
    $("continueSupplierSessionButton").hidden = !state.supplierId;
    $("supplierLatestButton").hidden = !(supplierContext?.recent_supplier_result || supplierContext?.recent_job);
  }
  async function refreshSupplierProfile() {
    if (!state.supplierId) { applySupplierProfile(null); return; }
    const id = state.supplierId, sourceScope = $("importSourceScope").value.trim() || "default";
    const profile = await api("/suppliers/" + encodeURIComponent(id) + "/profile?source_scope=" + encodeURIComponent(sourceScope));
    if (state.supplierId === id && ($("importSourceScope").value.trim() || "default") === sourceScope) { applySupplierProfile(profile, sourceScope); const url = new URL(window.location.href); url.searchParams.set("scope", sourceScope); window.history.replaceState(null, "", url); }
  }
  async function routeSupplier(id, { newRound = false, scope: requestedScope = null } = {}) {
    if (state.submitting) { $("conversationSupplier").value = state.supplierId || ""; return; }
    const oldJob = currentJob, oldSupplier = state.supplierId;
    stashSupplierDraft();
    const generation = ++supplierRouteGeneration;
    ++pollGeneration; state.agentBusy = false; state.sessionRestoring = true; updateComposer();
    try {
      if (!id) {
        rememberSupplier(null); supplierContext = null; activeDefaultPrompt = ""; restoreSupplierDraft(null);
        await restoreSession(null, { force: true }); applySupplierProfile(null); await loadFiles(); return;
      }
      const draft = supplierDrafts.get(id), sourceScope = requestedScope || draft?.scope || "default";
      const result = await api("/agent/suppliers/" + encodeURIComponent(id) + "/session", {
        method: "POST", body: JSON.stringify({ source_scope: sourceScope, new_round: newRound }),
      });
      if (generation !== supplierRouteGeneration) return;
      supplierContext = result; rememberSupplier(id); activeDefaultPrompt = "";
      restoreSupplierDraft(id); $("importSourceScope").value = sourceScope === "default" ? "" : sourceScope;
      applySupplierProfile(result.profile, sourceScope);
      const routeUrl = new URL(window.location.href); routeUrl.searchParams.set("scope", sourceScope); window.history.replaceState(null, "", routeUrl);
      await restoreSession(result.conversation_id, { force: true });
      if (generation !== supplierRouteGeneration) return;
      await loadFiles();
      if (currentJob?.status === waitingStatus && !draft?.prompt) els.prompt.value = "";
      if (result.waiting_job && (!currentJob || currentJob.id !== result.waiting_job.id)) await resumeJob(result.waiting_job);
      if (newRound) { $("fullReparse").checked = false; $("rememberPrompt").checked = false; toast("已开启供应商新一轮对话"); }
    } catch (error) {
      if (generation === supplierRouteGeneration) {
        rememberSupplier(oldSupplier); state.sessionRestoring = false;
        if (oldJob) await resumeJob(oldJob); else updateComposer();
        toast(error.message, true);
      }
    } finally { if (generation === supplierRouteGeneration) { state.sessionRestoring = false; updateComposer(); } }
  }
  window.addEventListener("tokensql:catalog", fillConversationSuppliers);
  window.addEventListener("tokensql:profile-saved", (event) => { if (event.detail.supplier_id === state.supplierId) refreshSupplierProfile().catch((error) => toast(error.message, true)); });
  $("conversationSupplier").addEventListener("change", () => routeSupplier($("conversationSupplier").value));
  $("importSourceScope").addEventListener("change", () => refreshSupplierProfile().catch((error) => toast(error.message, true)));
  $("continueSupplierSessionButton").addEventListener("click", () => routeSupplier(state.supplierId));
  $("supplierLatestButton").addEventListener("click", async () => {
    const job = supplierContext?.recent_supplier_result || supplierContext?.recent_job;
    if (!job) return;
    try {
      const latest = await api("/imports/" + encodeURIComponent(job.id));
      if (readyStatuses.has(latest.status)) await openReview(latest);
      else { const changes = latest.changes || {}; toast((latest.status === "confirmed" ? "最近已确认：" : "最近任务：") + "新增 " + (changes.added || 0) + " · 修改 " + (changes.updated || 0) + " · 删除 " + (changes.deleted || 0)); }
    } catch (error) { toast(error.message, true); }
  });
  $("providerFilter").addEventListener("change", () => {
    const supplier = state.catalog.suppliers.find((item) => item.name === $("providerFilter").value);
    if (supplier) routeSupplier(supplier.supplier_id);
  });
  fillConversationSuppliers();

  function latestJob(jobs, sessionId = undefined) {
    return (jobs || []).filter((job) => !["cancelled", "confirmed"].includes(job.status) &&
      (sessionId === undefined || job.conversation_id === sessionId))
      .sort((left, right) => String(right.updated_at || right.created_at || "").localeCompare(String(left.updated_at || left.created_at || "")))[0];
  }

  function renderJobReply(job) {
    const key = job.id + ":" + (job.attempt || 1) + ":" + job.status;
    if (renderedReplies.has(key) || !job.assistant_message) return;
    if (job.status === waitingStatus) {
      waitingCard.querySelector(".waiting-analysis")?.remove();
      if (job.assistant_message !== job.question_summary && !(job.questions || []).some((question) => question === job.assistant_message)) {
        const details = node("details", { className: "waiting-analysis" }, [node("summary", { text: "查看分析说明" }), node("pre", { text: job.assistant_message })]);
        waitingCard.append(details);
      }
      renderedReplies.add(key); return;
    }
    const last = els.conversation.lastElementChild;
    if (!last?.classList.contains("assistant") || last.querySelector(".message-body > p")?.textContent !== job.assistant_message) {
      appendMessage("assistant", job.assistant_message, [], null, job.messages);
    }
    renderedReplies.add(key);
  }

  function rememberSession(id) {
    state.conversationId = id || null;
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("session", id); else url.searchParams.delete("session");
    window.history.replaceState(null, "", url);
  }

  async function loadSessions() {
    const result = await api("/agent/sessions");
    sessionItems = result.items || [];
    const select = $("sessionSelect");
    select.replaceChildren(node("option", { text: "新会话" }));
    select.firstChild.value = "";
    (result.items || []).filter((session) => !state.supplierId || session.supplier_id === state.supplierId || session.id === state.conversationId).forEach((session) => {
      const option = node("option", { text: session.title || session.id });
      option.value = session.id;
      select.append(option);
    });
    if (state.conversationId && ![...select.options].some((option) => option.value === state.conversationId)) {
      const option = node("option", { text: state.conversationId }); option.value = state.conversationId; select.append(option);
    }
    select.value = state.conversationId || "";
  }

  async function restoreSession(id, { force = false } = {}) {
    if (state.agentBusy && !force) return;
    if (force) state.agentBusy = false;
    const generation = ++sessionGeneration;
    ++pollGeneration; currentJob = null; renderedReplies.clear();
    $("reviewDialog").close(); $("jobStrip").hidden = true; $("waitingPanel").hidden = true;
    rememberSession(id); state.sessionRestoring = Boolean(id); updateComposer();
    if (!id) { resetConversation(); appendMessage("assistant", "添加文件和说明，解析完成后可审核全部候选数据。"); return; }
    let history, imports;
    try {
      [history, imports] = await Promise.all([
        limitedHistory(id, 2500).catch((error) => ({ messages: [], historyError: error.message })),
        api("/imports"),
      ]);
    } catch (error) {
      if (generation === sessionGeneration) { state.sessionRestoring = false; updateComposer(); }
      throw error;
    }
    if (generation !== sessionGeneration || state.conversationId !== id) return;
    state.sessionRestoring = false;
    resetConversation();
    (history.messages || []).filter((message) => message.role === "user" || message.role === "assistant").forEach((message) => {
      const text = message.text || (typeof message.content === "string" ? message.content : (message.parts || []).filter((part) => part.type === "text").map((part) => part.text).join("\n"));
      if (text) appendMessage(message.role, text, message.files || []);
    });
    if (history.historyError) appendMessage("assistant", "会话历史暂不可读取；下方任务状态仍从服务端恢复。");
    if (!els.conversation.children.length) appendMessage("assistant", "此会话暂无可显示的消息。");
    $("sessionSelect").value = id;
    const job = latestJob(imports.items, id);
    if (job) await resumeJob(job);
    updateComposer();
  }

  async function resumeJob(job) {
    if (job.source_comparison_available && !job.source_comparison) {
      const session = state.conversationId, generation = sessionGeneration;
      job = await api("/imports/" + encodeURIComponent(job.id));
      if (session !== state.conversationId || generation !== sessionGeneration) return;
    }
    showJob(job);
    if (job.status === waitingStatus) { renderJobReply(job); lockComposer(false); return; }
    if (readyStatuses.has(job.status)) { await openReview(job); return; }
    if (terminal.has(job.status)) return;
    lockComposer(true);
    const generation = ++pollGeneration;
    pollJob(job.id, appendProgress(), generation).catch((error) => {
      if (generation === pollGeneration) toast(error.message, true);
    }).finally(() => { if (generation === pollGeneration) { lockComposer(false); checkAgent(); } });
  }

  async function loadFiles() {
    const result = await api("/imports/files");
    const input = (result.items || []).filter((file) => file.status === "input");
    $("inputFileCount").textContent = String(input.length);
    $("inputFiles").replaceChildren();
    const availableIds = new Set(input.map((file) => file.id));
    selectedUploads = new Set([...selectedUploads].filter((id) => availableIds.has(id)));
    input.forEach((file) => {
      const checkbox = node("input", { type: "checkbox" }); checkbox.checked = selectedUploads.has(file.id);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked && (state.agentBusy || state.sessionRestoring || currentJob && !terminal.has(currentJob.status))) {
          checkbox.checked = false; toast("当前任务不接收新文件；请选择新会话。", true);
        }
        if (checkbox.checked) selectedUploads.add(file.id); else selectedUploads.delete(file.id);
        updateComposer();
      });
      $("inputFiles").append(node("label", { className: "input-file" }, [checkbox, node("span", { text: file.filename || file.name || file.id })]));
    });
    if (!input.length) $("inputFiles").append(node("p", { className: "muted-cell", text: "暂无待处理文件。选择或拖入文件后发送。" }));
    updateComposer();
  }

  function updateComposer() {
    const waiting = currentJob?.status === waitingStatus;
    const active = Boolean(currentJob && !terminal.has(currentJob.status));
    const blocked = active || state.agentBusy || state.sessionRestoring;
    ["attachButton", "importButton", "supplierUpdateButton"].forEach((id) => { $(id).disabled = blocked; });
    $("inputFiles").querySelectorAll("input[type=checkbox]").forEach((input) => { input.disabled = blocked && !input.checked; });
    els.prompt.placeholder = waiting ? "回答上方问题，继续处理原文件（无需重新上传）…" : "输入指令，或拖入 / 粘贴文件…";
    els.send.setAttribute("aria-label", waiting ? "发送补充回答" : "发送");
    $("conversationSupplier").disabled = state.submitting || state.sessionRestoring;
    $("providerFilter").disabled = state.submitting || state.sessionRestoring;
    $("importSourceScope").disabled = blocked;
    $("continueSupplierSessionButton").disabled = state.submitting || state.sessionRestoring;
    $("continueUnknownButton").disabled = state.agentBusy || state.sessionRestoring;
    els.send.disabled = state.agentBusy || state.sessionRestoring || els.agentStatus.classList.contains("is-offline");
    $("composerHint").textContent = waiting ? "补充回答沿用当前任务、会话和原文件；若要添加文件，请先取消或新建会话。" : "";
    $("composerHint").hidden = !waiting;
  }

  function canAttachFiles() {
    if (state.sessionRestoring) { toast("正在恢复会话任务，请稍候再添加文件。", true); return false; }
    if (state.agentBusy || (currentJob && !terminal.has(currentJob.status))) {
      toast(currentJob?.status === waitingStatus ? "等待补充时不能添加新附件；原文件已保留。请先回答，或取消任务 / 新建会话后添加。" : "当前任务尚未结束，请先确认或取消，再添加新附件。", true);
      return false;
    }
    return true;
  }

  function showJob(job) {
    const changedJob = currentJob?.id !== job.id;
    currentJob = job;
    if (changedJob) {
      if (typeof job.full_reparse === "boolean") $("fullReparse").checked = job.full_reparse;
      if (typeof job.persist_profile === "boolean") $("persistProfile").checked = job.persist_profile;
      if (typeof job.remember_prompt === "boolean") $("rememberPrompt").checked = job.remember_prompt;
    }
    const ready = readyStatuses.has(job.status), waiting = job.status === waitingStatus;
    $("jobStrip").hidden = job.status === "cancelled" || job.status === "confirmed";
    $("jobStatus").textContent = waiting ? "待补充 " + (job.questions || []).length + " 项" : ready ? "候选方案已就绪，等待人工审核" : job.status === "failed" ? "任务失败，可补充说明或取消并退回文件" : job.status === "queued" ? "任务排队中…" : "OpenCode 正在分析文件…";
    $("reviewOpenButton").hidden = !ready;
    window.TokenSQLSourceComparison?.renderMetrics($("jobReadingSummary"), job.metrics || {}, job.context_usage || {}, job.reading_plan || {});
    $("abortButton").textContent = waiting || job.status === "failed" ? "取消并退回文件" : ready ? "取消草案" : "中止";
    $("waitingPanel").hidden = !waiting;
    if (waiting) {
      $("reviewDialog").close();
      const attempt = job.id + ":" + (job.attempt || 1);
      if (waitingAttempt && waitingAttempt !== attempt && waitingCard.parentElement === els.conversation) {
        const previous = waitingCard.cloneNode(true); previous.hidden = false;
        previous.querySelectorAll("[id]").forEach((element) => element.removeAttribute("id")); previous.removeAttribute("id");
        previous.querySelectorAll("button,.waiting-supplier").forEach((element) => element.remove());
        previous.querySelector("h3").textContent = "此前补充问题"; els.conversation.insertBefore(previous, waitingCard);
      }
      waitingAttempt = attempt;
      els.conversation.append(waitingCard); waitingCard.hidden = false;
      $("waitingSummary").textContent = job.question_summary || "请逐项补充关键问题，原文件和当前任务均已保留。";
      $("waitingQuestions").replaceChildren(...(job.questions || []).map((question) => {
        const item = node("li", { text: typeof question === "string" ? question : question.question || question.text || JSON.stringify(question) });
        if (typeof question === "object" && (question.evidence || question.source_ref)) item.append(node("details", { className: "waiting-question-evidence" }, [node("summary", { text: "文件与原文依据" }), node("pre", { text: JSON.stringify(question.evidence || question.source_ref, null, 2) })]));
        return item;
      }));
      $("waitingFiles").replaceChildren(node("details", {}, [node("summary", { text: "沿用原附件 " + (job.files || []).length + " 个" }), node("span", { text: (job.files || []).map((file) => file.filename || file.name || file.id).join("、") || "当前任务无附件" })]));
      waitingCard.querySelector(".waiting-supplier")?.remove();
      if (job.waiting_reason === "supplier" || (job.questions || []).some((question) => String(question).includes("供应商"))) {
        const select = node("select"); select.id = "waitingSupplierSelect"; select.setAttribute("aria-label", "为本批报价选择供应商"); supplierOptions(select);
        select.value = waitingSupplierId || ""; select.addEventListener("change", () => { waitingSupplierId = select.value || null; });
        const createInput = node("input", { type: "text" }); createInput.setAttribute("aria-label", "确认新供应商名称"); createInput.placeholder = "或输入新供应商统一名称";
        const create = node("button", { type: "button", className: "text-button", text: "确认新供应商" });
        create.addEventListener("click", async () => { try { if (!createInput.value.trim()) return; const result = await api("/suppliers", { method: "POST", body: JSON.stringify({ name: createInput.value.trim() }) }); await loadCatalog(); supplierOptions(select); waitingSupplierId = result.supplier_id || result.item?.supplier_id; select.value = waitingSupplierId || ""; toast("供应商已确认，请发送补充回答继续原任务"); } catch (error) { toast(error.message, true); } });
        waitingCard.append(node("label", { className: "waiting-supplier" }, [node("span", { text: "整批供应商（不重新上传）" }), select, createInput, create]));
      }
      els.conversation.scrollTop = els.conversation.scrollHeight;
    }
    updateComposer();
  }

  function assistantText(messages) {
    if (!Array.isArray(messages)) return "";
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message?.role === "user") break;
      if (message?.role !== "assistant") continue;
      const text = message.text || (typeof message.content === "string" ? message.content :
        (message.parts || []).filter((part) => part.type === "text").map((part) => part.text || "").join("\n"));
      if (text) return text;
    }
    return "";
  }

  function historyTexts(messages) {
    return (messages || []).filter((message) => ["user", "assistant"].includes(message?.role)).map((message) => ({
      role: message.role, text: message.text || (typeof message.content === "string" ? message.content :
        (message.parts || []).filter((part) => part.type === "text").map((part) => part.text || "").join("\n")),
    }));
  }
  async function limitedHistory(sessionId, milliseconds) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), milliseconds);
    try { return await api(`/agent/sessions/${encodeURIComponent(sessionId)}/messages`, { signal: controller.signal }); }
    finally { window.clearTimeout(timer); }
  }
  async function captureHistoryAnchor(generation) {
    const sessionId = state.conversationId;
    if (!sessionId) { historyAnchors.set(generation, { sessionId: null, messages: [] }); return; }
    try {
      const result = await limitedHistory(sessionId, 1500);
      if (generation === pollGeneration && state.conversationId === sessionId) {
        historyAnchors.set(generation, { sessionId, messages: historyTexts(result.messages) });
      }
    } catch (_) { /* Without a baseline, unanchored historical answers must not be called this turn's reply. */ }
  }
  function anchoredAssistant(messages, sessionId, generation) {
    const anchor = historyAnchors.get(generation);
    if (!anchor || (anchor.sessionId && anchor.sessionId !== sessionId)) return "";
    const current = historyTexts(messages);
    if (current.length < anchor.messages.length || anchor.messages.some((message, index) =>
      message.role !== current[index].role || message.text !== current[index].text)) return "";
    const delta = current.slice(anchor.messages.length);
    const firstUser = delta.findIndex((message) => message.role === "user");
    return firstUser < 0 ? "" : assistantText(delta.slice(firstUser));
  }
  async function renderFailure(job, error, generation) {
    const sessionId = job?.conversation_id || state.conversationId;
    const authoritative = job && Object.prototype.hasOwnProperty.call(job, "assistant_message");
    let text = authoritative ? (job.assistant_message || "") : anchoredAssistant(job?.messages, sessionId, generation);
    if (text && generation === pollGeneration) appendMessage("assistant", text, [], null, job?.messages);
    if (!text && sessionId && !(authoritative && job.status === "failed")) {
      try {
        const history = await limitedHistory(sessionId, 2500);
        if (generation !== pollGeneration || state.conversationId !== sessionId) return;
        text = anchoredAssistant(history.messages, sessionId, generation);
        if (text) appendMessage("assistant", text, [], null, job?.messages);
      } catch (_) { /* Keep any already-rendered model response if history is unavailable. */ }
    }
    if (generation !== pollGeneration) return;
    appendMessage("assistant", "任务未完成：" + (error || job?.error || "请检查文件和指令") +
      "。以上回复（如有）为 OpenCode 的说明；可在本会话补充信息后重试。", [], null, text ? [] : job?.messages);
  }

  function lockComposer(busy) {
    state.agentBusy = busy; els.send.disabled = busy;
    $("sessionSelect").disabled = busy; $("newSessionButton").disabled = busy;
    updateComposer();
  }

  async function pollJob(identifier, progress, generation) {
    while (generation === pollGeneration) {
      const job = await api(`/imports/${encodeURIComponent(identifier)}`);
      if (generation !== pollGeneration) return;
      showJob(job);
      if (job.conversation_id) { rememberSession(job.conversation_id); loadSessions().catch(() => {}); }
      if (progress) {
        renderToolActivity(progress.activity, job.messages);
        progress.label.textContent = job.status === "queued" ? "已排队，等待 OpenCode…" : "正在分析文件并生成候选方案…";
      }
      if (job.status === waitingStatus) {
        progress?.article.remove(); renderJobReply(job); return;
      }
      if (job.status === "ready" || job.status === "review") {
        progress?.article.remove();
        appendMessage("assistant", job.assistant_message || "解析完成。请审核候选模型、官网价及供应商报价，确认后入库。", [], null, job.messages);
        await openReview(job); return;
      }
      if (terminal.has(job.status)) {
        progress?.article.remove();
        if (job.status === "failed") await renderFailure(job, job.error, generation);
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1200));
    }
  }

  async function sendPrompt() {
    const prompt = els.prompt.value.trim();
    if (state.agentBusy || state.sessionRestoring || (!prompt && !state.files.length && !selectedUploads.size && !waitingSupplierId)) return;
    if (currentJob?.status === waitingStatus) { await respondPrompt(prompt || (waitingSupplierId ? "已确认本批供应商，请继续预审。" : "")); return; }
    if (currentJob && !terminal.has(currentJob.status)) { toast("请先确认或取消当前草案。", true); return; }
    const requestContext = { full_reparse: $("fullReparse").checked, conversation_id: state.conversationId, supplier_id: nextUpdateRequest?.supplier_id || state.supplierId, source_scope: nextUpdateRequest?.source_scope || $("importSourceScope").value.trim() || null, persist_profile: $("persistProfile").checked, remember_prompt: $("rememberPrompt").checked };
    state.submitting = true;
    const sentFiles = [...state.files];
    const existingUploads = [...selectedUploads];
    lockComposer(true);
    appendMessage("user", prompt || "分析这些文件并生成候选方案。", sentFiles.map((file) => file.name));
    const progress = appendProgress();
    const generation = ++pollGeneration;
    let uploaded = [];
    let startedJob = null;
    try {
      if (sentFiles.length) {
        const form = new FormData(); sentFiles.forEach((file) => form.append("files", file, file.name));
        const result = await api("/imports/upload", { method: "POST", body: form });
        uploaded = (result.items || []).map((file) => file.id);
        state.files = []; renderFileChips();
      }
      await captureHistoryAnchor(generation);
      const job = await api("/imports/start", { method: "POST", body: JSON.stringify({
        upload_ids: [...existingUploads, ...uploaded], prompt: prompt || "请根据附件及已确认规则生成候选方案，所有修改等待审核。",
        ...requestContext,
        ...(nextUpdateRequest ? { supplier_id: nextUpdateRequest.supplier_id, update_request: nextUpdateRequest } : {}),
      }) });
      startedJob = job; state.submitting = false; updateComposer(); nextUpdateRequest = null; $("preparedUpdateNotice").hidden = true; $("supplierUpdateButton").textContent = "更新供应商报价";
      els.prompt.value = ""; selectedUploads.clear();
      showJob(job); await loadFiles();
      await pollJob(job.id || job.import_id, progress, generation);
    } catch (error) {
      progress.article.remove();
      if (generation === pollGeneration) {
        let snapshot = startedJob;
        if (startedJob?.id) {
          try { snapshot = await api(`/imports/${encodeURIComponent(startedJob.id)}`); }
          catch (_) { /* The session-history fallback below can still show a partial model response. */ }
        }
        await renderFailure(snapshot, error.message, generation);
        if (generation === pollGeneration) toast(error.message, true);
        if (!uploaded.length) { state.files = sentFiles; renderFileChips(); }
      }
    } finally { state.submitting = false; if (generation === pollGeneration) lockComposer(false); historyAnchors.delete(generation); await checkAgent(); await loadFiles().catch(() => {}); }
  }

  async function respondPrompt(prompt) {
    const waiting = currentJob;
    if (state.agentBusy || waiting?.status !== waitingStatus || !prompt) return;
    if (state.files.length || selectedUploads.size) {
      toast("补充回答不能夹带新附件；请先移除选择，或取消任务 / 新建会话后上传。", true); return;
    }
    lockComposer(true);
    const generation = ++pollGeneration;
    appendMessage("user", prompt);
    const progress = appendProgress();
    try {
      await captureHistoryAnchor(generation);
      if (generation !== pollGeneration) return;
      const job = await api(`/imports/${encodeURIComponent(waiting.id)}/respond`, {
        method: "POST", body: JSON.stringify({ expected_revision: waiting.revision, prompt, persist_profile: $("persistProfile").checked, remember_prompt: $("rememberPrompt").checked, ...(waitingSupplierId ? { supplier_id: waitingSupplierId } : {}) }),
      });
      if (generation !== pollGeneration) return;
      if (els.prompt.value.trim() === prompt) els.prompt.value = "";
      waitingSupplierId = null; showJob(job);
      await pollJob(job.id, progress, generation);
    } catch (error) {
      progress.article.remove();
      if (generation !== pollGeneration) return;
      let snapshot = null;
      try { snapshot = await api(`/imports/${encodeURIComponent(waiting.id)}`); } catch (_) {}
      if (generation !== pollGeneration) return;
      if (snapshot) {
        showJob(snapshot);
        if (snapshot.status === waitingStatus) renderJobReply(snapshot);
        else if (readyStatuses.has(snapshot.status)) await openReview(snapshot);
        else if (!terminal.has(snapshot.status)) {
          // A lost response or concurrent tab may already have started this attempt.
          // Observe its existing job; never issue an automatic second inference.
          await pollJob(snapshot.id, appendProgress(), generation);
        }
      }
      if (generation !== pollGeneration) return;
      const detail = error.status === 409 ? "补充未重复发送：任务版本或状态已变化，已刷新最新状态。请核对问题后再发送。" :
        "补充请求未确认完成，已尝试恢复当前任务：" + error.message + "。不会自动重发。";
      appendMessage("assistant", detail); toast(detail, true);
    } finally {
      if (generation === pollGeneration) { lockComposer(false); await checkAgent(); await loadFiles().catch(() => {}); }
      historyAnchors.delete(generation);
    }
  }

  async function lookupOfficial(quote) {
    if (state.sessionRestoring) { toast("正在恢复会话任务，请稍候再启动官网补价。", true); return; }
    if (state.agentBusy || (currentJob && !terminal.has(currentJob.status))) {
      toast("请先确认或取消当前草案，再启动独立官网价会话。", true); return;
    }
    if (!quote?.model_id) { toast("缺少唯一模型 ID，无法查询官网价。", true); return; }
    if (!window.confirm("缺失官网价。是否开启全新 OpenCode 会话，读取官网链接索引并联网核实？结果必须经人工审核确认，不会自动入库。")) return;
    ++sessionGeneration; rememberSession(null);
    state.files = []; selectedUploads.clear(); renderFileChips(); els.prompt.value = "";
    $("inputFiles").querySelectorAll("input[type=checkbox]").forEach((input) => { input.checked = false; });
    resetConversation();
    appendMessage("user", "联网核实官网价：" + quote.model_id + " / " + (quote.billing_item ?? "所有可确认计费项") +
      "。读取 docs/official_price_sources.md；保留原币原价、正式单位和官网 URL 证据，提交独立审核。");
    const progress = appendProgress();
    const generation = ++pollGeneration;
    lockComposer(true);
    let job = null;
    try {
      await captureHistoryAnchor(generation);
      job = await api("/official-prices/lookup", { method: "POST", body: JSON.stringify({
        model_id: quote.model_id, billing_item: quote.billing_item ?? null, prompt: "",
      }) });
      if (generation !== pollGeneration) return;
      if (job.conversation_id) rememberSession(job.conversation_id);
      showJob(job); await loadSessions();
      await pollJob(job.id || job.import_id, progress, generation);
    } catch (error) {
      progress.article.remove();
      await renderFailure(job, error.message, generation);
      if (generation === pollGeneration) toast(error.message, true);
    } finally { if (generation === pollGeneration) lockComposer(false); historyAnchors.delete(generation); await checkAgent(); }
  }

  const sectionNames = { models: "模型实体", official_prices: "官网计费项", create: "新增供应商报价", update: "更新供应商报价", delete: "删除报价" };
  const fields = {
    models: ["model_id", "vendor", "model_name", "aliases"],
    official_prices: ["model_id", "billing_item", "amount", "currency", "pricing_unit_id", "explicit_official", "source", "source_ref"],
    create: ["model_id", "billing_item", "supplier", "source_scope", "source_entry_id", "pricing_basis", "tags", "supplier_model_id", "amount", "discount_rate", "currency", "pricing_unit_id", "source", "source_ref"],
    update: ["model_id", "billing_item", "supplier", "source_scope", "source_entry_id", "pricing_basis", "tags", "supplier_model_id", "amount", "discount_rate", "currency", "pricing_unit_id", "source", "source_ref"],
    delete: ["id", "expected_revision"],
  };
  const fieldLabels = { model_id: "唯一模型 ID", vendor: "模型商", model_name: "模型名称", aliases: "别名（逗号分隔）",
    billing_item: "计费项", amount: "原币金额", currency: "币种（新增默认人民币）", pricing_unit_id: "计价单位",
    explicit_official: "文件 / 来源明确标注官网价", source: "来源", source_ref: "单元格 / 来源依据", supplier: "供应商",
    source_scope: "报价单系列 / 范围", source_entry_id: "来源真实条目编号", pricing_basis: "有效定价依据", tags: "标签（逗号分隔）", supplier_model_id: "原渠道 ID", discount_rate: "折扣（折，8.0 = 八折）", id: "报价 ID", expected_revision: "原版本" };

  function markDirty() {
    reviewDirty = true;
    $("reviewBundle").value = JSON.stringify(reviewBundle, null, 2);
    $("reviewPreview").textContent = "方案已修改，请保存修正并校验；原始字段独立保留，计算仅供前端显示。";
  }

  function renderReviewSections() {
    const target = $("reviewSections"); target.replaceChildren();
    if ((reviewBundle.create || []).length || (reviewBundle.update || []).length) {
      const select = node("select"); select.setAttribute("aria-label", "整批报价供应商"); supplierOptions(select, "选择整批供应商（可逐组修改）");
      const apply = node("button", { type: "button", className: "text-button", text: "应用到本批报价" });
      apply.addEventListener("click", () => { const supplier = state.catalog.suppliers.find((item) => item.supplier_id === select.value); if (!supplier) return;
        for (const entry of [...(reviewBundle.create || []), ...(reviewBundle.update || []).map((item) => item.changes)]) { entry.supplier_id = supplier.supplier_id; entry.supplier = supplier.name; }
        markDirty(); renderReviewSections(); });
      target.append(node("div", { className: "review-batch-supplier" }, [node("span", { text: "一次确认本批供应商" }), select, apply]));
    }
    for (const [section, label] of Object.entries(sectionNames)) {
      const entries = reviewBundle[section] || [];
      const header = node("div", { className: "review-section-heading" }, [node("h3", { text: `${label} · ${entries.length}` })]);
      if (section !== "delete" && section !== "update") {
        const add = node("button", { className: "text-button", type: "button", text: "+ 添加" });
        add.addEventListener("click", () => {
          entries.push(section === "models" ? { model_id: "", vendor: "", model_name: "", aliases: [] } : section === "official_prices" ? {
            model_id: "", billing_item: null, amount: null, currency: "CNY", pricing_unit_id: null, explicit_official: null, source: null, source_ref: null,
          } : { model_id: "", billing_item: null, supplier: null, tags: [], amount: null, currency: "CNY", pricing_unit_id: null });
          reviewBundle[section] = entries; markDirty(); renderReviewSections();
        });
        header.append(add);
      }
      const block = node("section", { className: "review-section" }, [header]);
      entries.forEach((entry, index) => {
        const data = section === "update" ? entry.changes : entry;
        const card = node("div", { className: "review-entry" });
        const caption = section === "update" ? `报价 #${entry.id} · 原版本 ${entry.expected_revision}` : `第 ${index + 1} 项`;
        const remove = node("button", { className: "text-button danger", type: "button", text: "从草案移除" });
        remove.addEventListener("click", () => { entries.splice(index, 1); markDirty(); renderReviewSections(); });
        card.append(node("div", { className: "review-section-heading" }, [node("strong", { text: caption }), remove]));
        const grid = node("div", { className: "review-field-grid" });
        fields[section].forEach((key) => {
          if (section === "update" && !(key in data) && key !== "pricing_basis") return;
          const input = ["pricing_unit_id", "currency", "pricing_basis"].includes(key) ? node("select") : node("input", { type: key === "explicit_official" ? "checkbox" : "text" });
          if (key === "pricing_unit_id") {
            const empty = node("option", { text: "未提供 / 不明确" }); empty.value = ""; input.append(empty);
            state.pricingUnits.items.forEach((unit) => {
              const option = node("option", { text: unit.label || unit.currency + " / " + unit.scale });
              option.value = unit.id; input.append(option);
            });
          }
          if (key === "pricing_basis") {
            [["", "按原金额或折扣自动判定"], ["amount", "以原金额为有效依据"], ["discount_rate", "以原折扣为有效依据"]].forEach(([value, text]) => { const option = node("option", { text }); option.value = value; input.append(option); });
          }
          if (key === "supplier" && ["create", "update"].includes(section)) { input.required = true; input.setAttribute("list", "reviewSupplierNames"); }
          if (key === "currency") {
            [["CNY", "人民币 CNY"], ["USD", "美元 USD"], ["HKD", "港币 HKD"], ["", "未提供 / 不明确"]].forEach(([value, text]) => {
              const option = node("option", { text }); option.value = value; input.append(option);
            });
          }
          input.dataset.field = key;
          input.setAttribute("aria-label", `${label} ${index + 1} ${fieldLabels[key]}`);
          if (key === "explicit_official") input.checked = data[key] === true;
          else input.value = key === "discount_rate" ? window.TokenSQLConversion.discountFold(data[key]) ?? "" : Array.isArray(data[key]) ? data[key].join(", ") : data[key] ?? "";
          if (key === "discount_rate") {
            input.dataset.originalRatio = window.TokenSQLConversion.discountRatio(data[key]) ?? ""; input.dataset.initialFold = input.value;
            input.title = "以折为单位，保留一位小数，例如八折填写8.0。";
          }
          if (section === "delete" || (entry.expected_revision && key === "model_id")) input.readOnly = true;
          input.addEventListener("input", () => {
            input.setCustomValidity("");
            if (key === "discount_rate") {
              try { data[key] = input.value.trim() === input.dataset.initialFold && input.dataset.originalRatio !== ""
                ? window.TokenSQLConversion.discountRatio(input.dataset.originalRatio) : window.TokenSQLConversion.ratioFromFold(input.value); }
              catch (error) { input.setCustomValidity(error.message); input.reportValidity(); return; }
              markDirty(); return;
            }
            if (key === "supplier") { const supplier = state.catalog.suppliers.find((item) => item.name === input.value.trim() || (item.aliases || []).includes(input.value.trim())); data.supplier_id = supplier?.supplier_id || null; }
            data[key] = key === "explicit_official" ? (input.checked ? true : null) : ["aliases", "tags"].includes(key) ? input.value.split(/[,，]/).map((value) => value.trim()).filter(Boolean) : input.value.trim() === "" && key !== "model_id" ? null : input.value;
            if (key === "pricing_unit_id") {
              const unit = state.pricingUnits.items.find((item) => item.id === input.value);
              if (unit) data.currency = unit.currency;
              data.unit = null; data.unit_quantity = null;
              const currencyInput = grid.querySelector('[data-field="currency"]');
              if (currencyInput) currencyInput.value = data.currency ?? "";
            }
            if (key === "currency" && data.pricing_unit_id) {
              const previous = state.pricingUnits.items.find((item) => item.id === data.pricing_unit_id);
              const matching = state.pricingUnits.items.find((item) => item.currency === data.currency && item.scale === previous?.scale);
              data.pricing_unit_id = matching?.id || null; data.unit = null; data.unit_quantity = null;
              const unitInput = grid.querySelector('[data-field="pricing_unit_id"]');
              if (unitInput) unitInput.value = data.pricing_unit_id || "";
            }
            if (section === "create" || section === "update") {
              if (key === "supplier_model_id" && !input.value.trim()) data[key] = null;
            }
            markDirty();
          });
          grid.append(node("label", {}, [node("span", { text: fieldLabels[key] }), input]));
        });
        card.append(grid);
        if (["official_prices", "create", "update"].includes(section)) {
          card.append(window.TokenSQLConditions.createEditor(data, { quote: section !== "official_prices", label: label + " " + (index + 1), onChange: markDirty }));
          for (const key of ["conditions", "note"]) {
            const input = node("textarea"); input.rows = 2;
            input.setAttribute("aria-label", label + " " + (index + 1) + (key === "conditions" ? " 来源原文条件 JSON" : " 备注"));
            input.value = key === "conditions" ? JSON.stringify(data[key] || {}, null, 2) : data[key] || "";
            input.addEventListener("input", () => {
              try { const value = key === "conditions" ? JSON.parse(input.value || "{}") : input.value || null;
                if (key === "conditions" && (!value || Array.isArray(value) || typeof value !== "object")) throw Error("来源原文条件必须是对象");
                data[key] = value; input.setCustomValidity(""); markDirty(); }
              catch (error) { input.setCustomValidity(error.message); }
            });
            card.append(node("details", {}, [node("summary", { text: key === "conditions" ? "来源原文条件" : "备注" }), input]));
          }
        }
        block.append(card);
      });
      target.append(block);
    }
    const names = node("datalist"); names.id = "reviewSupplierNames"; state.catalog.suppliers.forEach((supplier) => { const option = node("option"); option.value = supplier.name; names.append(option); }); target.append(names);
    $("reviewBundle").value = JSON.stringify(reviewBundle, null, 2);
  }

  function renderReconciliation(preview, target) {
    if (!preview.groups) return;
    const names = { create: "新增", update: "修改", delete: "删除", unchanged: "不变", needs_confirmation: "待确认" };
    const labels = { model_id: "model_id", supplier: "供应商", amount: "原金额", discount_rate: "折扣倍率", billing_metric: "计费项", pricing_conditions: "适用条件", pricing_unit_id: "单位", discount_scope: "折扣范围", source_scope: "报价单范围", supplier_model_id: "渠道方案" };
    for (const [key, name] of Object.entries(names)) {
      const entries = preview.groups[key] || []; const group = node("details", { className: "reconciliation-group" }, [node("summary", { text: `${name} · ${entries.length}` })]); group.open = entries.length > 0 && key !== "unchanged";
      entries.forEach((entry) => {
        const block = node("div", { className: "reconciliation-change" }, [node("strong", { text: (entry.after?.model_id || entry.before?.model_id || "报价") + (entry.before?.id ? " · 原报价 #" + entry.before.id : "") }), node("p", { className: "muted-cell", text: entry.reason || "" })]);
        const diff = node("dl"); diff.append(node("dt", { text: "字段" }), node("dd", { text: "变更前" }), node("dd", { text: "变更后" }));
        for (const [field, label] of Object.entries(labels)) {
          const before = entry.before?.[field], after = entry.after?.[field]; if (key === "update" && JSON.stringify(before) === JSON.stringify(after)) continue;
          const text = (value) => value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
          diff.append(node("dt", { text: label }), node("dd", { text: text(before) }), node("dd", { text: text(after) }));
        }
        block.append(diff);
        if (key === "needs_confirmation") {
          const select = node("select"); select.setAttribute("aria-label", "确认报价映射 " + entry.entry_key);
          [["", "请选择：覆盖 / 新增 / 忽略"], ["create", "作为新增报价"], ["ignore", "忽略本条"]].forEach(([value, text]) => { const option = node("option", { text }); option.value = value; select.append(option); });
          (entry.candidate_ids || []).forEach((id) => { const option = node("option", { text: "覆盖原报价 #" + id }); option.value = "update:" + id; select.append(option); });
          const active = reviewDecisions.find((item) => item.entry_key === entry.entry_key); select.value = active ? active.action === "update" ? "update:" + active.quote_id : active.action : "";
          select.addEventListener("change", () => { reviewDecisions = reviewDecisions.filter((item) => item.entry_key !== entry.entry_key); if (select.value) { const [action, id] = select.value.split(":"); const old = state.allItems.find((item) => String(item.id) === id); reviewDecisions.push({ entry_key: entry.entry_key, action, ...(id ? { quote_id: Number(id), ...(old ? { expected_revision: old.revision } : {}) } : {}) }); } reconciliationDirty = true; $("reviewIssues").textContent = "映射已选择，请保存修正并重新预审。"; }); block.append(select);
        }
        group.append(block);
      }); target.append(group);
    }
    if (preview.ready === false) target.append(node("p", { className: "validation-issue", text: "仍有待确认条目，请补充映射 / 来源范围后重新预审；当前不能入库。" }));
  }

  function renderPreview(job) {
    const preview = job.preview || {};
    window.TokenSQLSourceComparison?.render($("reviewSourceComparison"), { comparison: job.source_comparison, metrics: job.metrics || {}, readingPlan: job.reading_plan || {}, contextUsage: job.context_usage || {} });
    const officialChanges = job.official_catalog_changes || [];
    if (officialChanges.length) {
      const container = $("reviewSourceComparison"); container.hidden = false;
      const list = node("section", { className: "official-source-changes" }, [node("h3", { text: "原表官网价更新" })]);
      const priceText = (price) => {
        const unit = state.pricingUnits.items.find((item) => item.id === price?.pricing_unit_id);
        return String(price?.amount ?? "—") + " " + (unit?.label || [price?.currency || "", price?.unit || price?.pricing_unit_id || ""].filter(Boolean).join(" / "));
      };
      officialChanges.forEach((change) => {
        const before = change.before || {}, after = change.after || {};
        const fields = (change.changed_fields || []).join("、");
        list.append(node("article", { className: "official-source-change" }, [
          node("strong", { text: (after.model_id || before.model_id || change.official_price_id) + " · " + (after.billing_item || before.billing_item || "") }),
          node("p", { text: priceText(before) + " → " + priceText(after) }),
          node("details", {}, [node("summary", { text: "变更字段：" + fields }), node("pre", { text: JSON.stringify({ before, after }, null, 2) })]),
        ]));
      });
      container.append(list);
    }
    const target = $("reviewPreview"); target.replaceChildren(node("strong", { text: "原始数据校验预览 · 正式数据尚未变更" }));
    const changes = preview.changes || job.changes || {};
    target.append(node("p", { text: `新增 ${changes.added ?? (reviewBundle.create || []).length} · 更新 ${changes.updated ?? (reviewBundle.update || []).length} · 删除 ${changes.deleted ?? (reviewBundle.delete || []).length}` }));
    renderReconciliation(preview, target);
    const items = preview.items || preview.create || [];
    items.forEach((item) => {
      const displayed = { ...item, _officialCatalog: preview.catalog?.official_prices || state.catalog.official_prices };
      target.append(node("div", { className: "review-computed-item" }, [node("strong", { text: `${item.model_id} / ${item.billing_item ?? "未提供计费项"} / ${item.supplier ?? "未提供供应商"}` }), window.TokenSQLComparison ? window.TokenSQLComparison.preview(displayed) : priceDisplay(displayed)]));
    });
    if (!items.length && Object.keys(preview).length) {
      const details = node("details", {}, [node("summary", { text: "查看完整校验结果" }), node("pre", { text: JSON.stringify(preview, null, 2) })]);
      target.append(details);
    }
    $("reviewIssues").replaceChildren();
    const issues = [...(job.issues || []), ...(preview.issues || [])].filter((issue, index, array) => array.findIndex((value) => JSON.stringify(value) === JSON.stringify(issue)) === index);
    issues.forEach((issue) => $("reviewIssues").append(node("p", { className: "validation-issue", text: typeof issue === "string" ? issue : JSON.stringify(issue) })));
  }

  async function openReview(job) {
    if (job.source_comparison_available && !job.source_comparison) {
      const generation = sessionGeneration;
      job = await api("/imports/" + encodeURIComponent(job.id));
      if (generation !== sessionGeneration) return;
    }
    if (!readyStatuses.has(job.status)) {
      showJob(job); $("reviewDialog").close();
      if (job.status === waitingStatus) renderJobReply(job);
      toast(job.status === waitingStatus ? "当前任务正在等待补充，尚无可确认的入库草案。" : "任务当前不可审核，请检查最新状态。", true);
      return;
    }
    // Supplier updates reconcile the uploaded proposal into a separate applied bundle.
    // Edit the source rows so re-preview retains matched updates and missing-row deletes.
    currentJob = job; reviewBundle = JSON.parse(JSON.stringify(job.proposal_bundle ?? job.bundle ?? {})); reviewDirty = false; reconciliationDirty = false;
    $("reviewProfileSettings").hidden = job.intent === "official_price_lookup" || !job.supplier_id && !(reviewBundle.create || []).length && !(reviewBundle.update || []).length;
    $("reviewPersistProfile").checked = job.persist_profile ?? true;
    $("reviewRememberPrompt").checked = job.remember_prompt ?? false;
    reviewDecisions = job.reconciliation_request?.decisions || [];
    $("reviewMeta").textContent = `草案 ${job.id} · 版本 ${job.revision} · ${(job.files || []).map((file) => file.filename || file.name || "").join("、") || "人工编辑"}`;
    renderReviewSections(); renderPreview(job);
    if (job.error) $("reviewIssues").prepend(node("p", { className: "validation-issue", text: job.error }));
    if (!$("reviewDialog").open) $("reviewDialog").showModal();
  }

  function parseBundle(text) {
    const bundle = JSON.parse(text);
    if (!bundle || Array.isArray(bundle) || typeof bundle !== "object") throw new Error("候选方案必须是 JSON 对象");
    for (const section of Object.keys(sectionNames)) {
      if (bundle[section] === undefined) continue;
      if (!Array.isArray(bundle[section]) || bundle[section].some((entry) => !entry || typeof entry !== "object" || Array.isArray(entry))) throw new Error(`${section} 必须是对象数组`);
      if (section === "update" && bundle[section].some((entry) => !entry.changes || typeof entry.changes !== "object" || Array.isArray(entry.changes))) throw new Error("update 中每项必须包含 changes 对象");
    }
    return bundle;
  }

  async function saveReview() {
    if (!currentJob || !readyStatuses.has(currentJob.status) || reviewBusy) return false;
    for (const input of $("reviewSections").querySelectorAll("input,select,textarea")) { if (!input.reportValidity()) return false; }
    reviewBusy = true; $("previewReviewButton").disabled = true; $("confirmReviewButton").disabled = true;
    try {
      const bundle = parseBundle($("reviewBundle").value);
      const preferencesDirty = $("reviewPersistProfile").checked !== (currentJob.persist_profile ?? true) || $("reviewRememberPrompt").checked !== (currentJob.remember_prompt ?? false);
      if (reviewDirty || reconciliationDirty || preferencesDirty) {
        const job = await api(`/imports/${encodeURIComponent(currentJob.id)}/proposal`, {
          method: "PATCH", body: JSON.stringify({ expected_revision: currentJob.revision, persist_profile: $("reviewPersistProfile").checked, remember_prompt: $("reviewRememberPrompt").checked, ...(reviewDirty ? { bundle } : {}), ...(reconciliationDirty ? { decisions: reviewDecisions } : {}) }),
        });
        await openReview(job);
      }
      return true;
    } catch (error) {
      $("reviewIssues").textContent = error.status === 409 ? "草案版本已变化，请关闭后重新打开最新草案。" : error.message;
      return false;
    } finally { reviewBusy = false; $("previewReviewButton").disabled = false; $("confirmReviewButton").disabled = false; }
  }

  async function confirmReview() {
    if (!(await saveReview())) return;
    if (currentJob?.preview?.ready === false) { $("reviewIssues").textContent = "请先完成待确认映射并保存预审。"; return; }
    $("confirmReviewButton").disabled = true;
    try {
      const job = await api(`/imports/${encodeURIComponent(currentJob.id)}/confirm`, {
        method: "POST", body: JSON.stringify({ expected_revision: currentJob.revision, idempotency_key: `workspace-${currentJob.id}` }),
      });
      ++pollGeneration; showJob(job); $("reviewDialog").close(); $("fullReparse").checked = false; $("rememberPrompt").checked = false;
      appendMessage("assistant", "已确认入库，文件已归档。已确认修正已按本次导入设置处理。", [], job.changes || {});
      await Promise.all([loadQuotes({ quiet: true }), loadCatalog(), loadFiles(), loadSessions()]);
      toast("审核方案已确认");
    } catch (error) {
      if (error.status === 409) {
        try {
          await openReview(await api(`/imports/${encodeURIComponent(currentJob.id)}`));
          $("reviewIssues").textContent = "数据或官网价已变化，已重新加载最新计算预览。请检查后再次确认。";
        } catch (refreshError) { $("reviewIssues").textContent = refreshError.message; }
      } else $("reviewIssues").textContent = error.message;
    }
    finally { $("confirmReviewButton").disabled = false; }
  }

  async function cancelCurrent() {
    if (!currentJob || ["cancelled", "confirmed"].includes(currentJob.status)) return;
    const identifier = currentJob.id;
    const generation = ++pollGeneration;
    lockComposer(true); $("abortButton").disabled = true;
    try {
      const job = await api(`/imports/${encodeURIComponent(identifier)}/cancel`, { method: "POST", body: "{}" });
      if (generation !== pollGeneration) return;
      showJob(job); $("reviewDialog").close();
      els.conversation.querySelectorAll(".run-progress").forEach((progress) => progress.closest(".message")?.remove());
      await loadFiles();
      appendMessage("assistant", "任务已取消，文件已退回输入区。正式报价与供应商档案未变更。"); toast("已取消");
    } catch (error) {
      if (generation === pollGeneration) {
        toast("取消请求未确认完成，正在恢复任务状态：" + error.message, true);
        try {
          const snapshot = await api(`/imports/${encodeURIComponent(identifier)}`);
          if (generation === pollGeneration) {
            els.conversation.querySelectorAll(".run-progress").forEach((progress) => progress.closest(".message")?.remove());
            await resumeJob(snapshot);
          }
        } catch (refreshError) { if (generation === pollGeneration) toast("请刷新检查任务状态：" + refreshError.message, true); }
      }
    }
    finally {
      $("abortButton").disabled = false;
      if (generation === pollGeneration) { lockComposer(false); await checkAgent(); }
    }
  }

  async function manualDraft(section, record = null) {
    if (state.agentBusy || state.sessionRestoring) { toast("正在处理或恢复会话任务，请稍候再创建手工草案。", true); return; }
    if (currentJob && !terminal.has(currentJob.status)) { toast("请先确认或取消当前草案。", true); return; }
    $("catalogDialog").close();
    try {
      const job = await api("/imports/drafts", { method: "POST", body: JSON.stringify({ bundle: {}, prompt: "人工修订模型目录与官网价格" }) });
      await openReview(job); showJob(job);
      const allowed = section === "models" ? ["model_id", "vendor", "model_name", "aliases", "source", "source_ref", "note"] : [
        "model_id", "official_price_id", "billing_item", "billing_metric", "pricing_conditions", "conditions", "amount", "currency", "unit", "unit_quantity", "pricing_unit_id", "explicit_official", "source", "source_ref", "conversion", "effective_date", "note",
      ];
      let entry = section === "models" ? { model_id: "", vendor: "", model_name: "", aliases: [] } :
        { model_id: "", billing_item: null, amount: null, currency: "CNY", pricing_unit_id: null, explicit_official: null, source: null, source_ref: null };
      if (record) {
        entry = Object.fromEntries(allowed.filter((key) => record[key] !== undefined).map((key) => [key, record[key]]));
        entry.expected_revision = record.revision;
      }
      reviewBundle[section] = [entry]; markDirty(); renderReviewSections();
    } catch (error) { toast(error.message, true); }
  }

  async function openCatalog() {
    try {
      await loadCatalog();
      const target = $("catalogContents"); target.replaceChildren();
      for (const model of state.catalog.models) {
        const edit = node("button", { className: "text-button", type: "button", text: "编辑模型" });
        edit.addEventListener("click", () => manualDraft("models", model));
        const block = node("section", { className: "catalog-model" }, [
          node("div", { className: "review-section-heading" }, [node("h3", { text: model.model_id }), edit]),

          node("p", { className: "muted-cell", text: `别名：${(model.aliases || []).join("、") || "无"}` }),
        ]);
        for (const price of state.catalog.official_prices.filter((item) => item.model_id === model.model_id)) {
          const button = node("button", { className: "text-button", type: "button", text: "编辑官网计费项" });
          button.addEventListener("click", () => manualDraft("official_prices", price));
          block.append(node("div", { className: "catalog-price" }, [
            node("span", { text: window.TokenSQLConditions.priceLabel(price) }), priceDisplay(price),
            node("small", { text: "版本 " + price.revision }),
            node("small", { text: `${price.source ?? ""} · ${price.source_ref ?? ""}` }), button,
          ]));
        }
        target.append(block);
      }
      if (!state.catalog.models.length) target.append(node("p", { text: "模型目录为空。新增报价时输入模型 ID 即可自动创建；也可在这里维护模型资料和官网计费项。" }));
      $("catalogDialog").showModal();
    } catch (error) { toast(error.message, true); }
  }

  $("reviewOpenButton").addEventListener("click", async () => {
    try { await openReview(await api(`/imports/${encodeURIComponent(currentJob.id)}`)); } catch (error) { toast(error.message, true); }
  });
  $("hideReviewButton").addEventListener("click", () => $("reviewDialog").close());
  $("cancelReviewButton").addEventListener("click", cancelCurrent);
  $("abortButton").addEventListener("click", cancelCurrent);
  $("previewReviewButton").addEventListener("click", saveReview);
  $("confirmReviewButton").addEventListener("click", confirmReview);
  $("reviewPersistProfile").addEventListener("change", () => { $("persistProfile").checked = $("reviewPersistProfile").checked; });
  $("reviewRememberPrompt").addEventListener("change", () => { $("rememberPrompt").checked = $("reviewRememberPrompt").checked; });
  $("reviewBundle").addEventListener("input", () => { reviewDirty = true; });
  $("applyReviewJsonButton").addEventListener("click", () => {
    try { reviewBundle = parseBundle($("reviewBundle").value); reviewDirty = true; renderReviewSections(); $("reviewIssues").textContent = ""; }
    catch (error) { $("reviewIssues").textContent = error.message; }
  });
  $("sessionSelect").addEventListener("change", () => restoreSession($("sessionSelect").value).catch((error) => toast(error.message, true)));
  $("newSessionButton").addEventListener("click", () => state.supplierId ? routeSupplier(state.supplierId, { newRound: true }) : restoreSession(null));
  $("catalogButton").addEventListener("click", openCatalog);
  $("closeCatalogButton").addEventListener("click", () => $("catalogDialog").close());
  $("newModelButton").addEventListener("click", () => manualDraft("models"));
  $("newOfficialButton").addEventListener("click", () => manualDraft("official_prices"));
  $("reviewDialog").addEventListener("keydown", (event) => { if (event.key === "Enter" && event.target.tagName === "INPUT") event.preventDefault(); });

  $("continueUnknownButton").addEventListener("click", () => {
    if (currentJob?.status !== waitingStatus || state.agentBusy) return;
    const instruction = "尚未提供的非关键项请留空继续，不猜测；若无法唯一识别模型 ID 等关键歧义仍需向我确认。请生成待人工审核的草案，不直接入库。";
    els.prompt.value = [els.prompt.value.trim(), instruction].filter(Boolean).join("\n");
    sendPrompt();
  });
  $("supplierUpdateButton").addEventListener("click", async () => {
    if (!canAttachFiles()) return;
    await loadCatalog(); supplierOptions($("updateSupplier")); if (state.supplierId) $("updateSupplier").value = state.supplierId; if ($("importSourceScope").value) $("updateSourceScope").value = $("importSourceScope").value; $("supplierUpdateError").textContent = ""; $("supplierUpdateDialog").showModal();
  });
  $("clearPreparedUpdateButton").addEventListener("click", () => { nextUpdateRequest = null; $("preparedUpdateNotice").hidden = true; $("supplierUpdateButton").textContent = "更新供应商报价"; });
  $("closeSupplierUpdateButton").addEventListener("click", () => $("supplierUpdateDialog").close());
  $("supplierUpdateForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!$("supplierUpdateForm").reportValidity()) return;
    const mode = $("updateMode").value;
    const sheets = csvValues($("updateSheets").value);
    if (mode === "full" && (!$("updateCompleteRead").checked || !sheets.length)) { $("supplierUpdateError").textContent = "完整替换必须确认完整范围，并明确覆盖工作表；部分解析不能推导删除。"; return; }
    const updateSupplierId = $("updateSupplier").value;
    if (updateSupplierId !== state.supplierId) { await routeSupplier(updateSupplierId, { scope: $("updateSourceScope").value.trim() }); if (state.supplierId !== updateSupplierId) return; }
    nextUpdateRequest = { supplier_id: updateSupplierId, source_scope: $("updateSourceScope").value.trim(), mode,
      scope_confirmed: $("updateScopeConfirmed").checked, complete_read: $("updateCompleteRead").checked,
      covered_sheets: sheets, expected_sheets: sheets, model_ids: csvValues($("updateModels").value), supplier_model_ids: csvValues($("updateChannels").value),
      source_version: $("updateVersion").value.trim() || null, source_effective_date: $("updateEffectiveDate").value || null,
      latest_confirmed: $("updateLatestConfirmed").checked, allow_rollback: $("updateAllowRollback").checked };
    $("importSourceScope").value = nextUpdateRequest.source_scope; await refreshSupplierProfile().catch((error) => toast(error.message, true));
    $("supplierUpdateDialog").close(); $("supplierUpdateButton").textContent = "更新模式已准备";
    const supplier = state.catalog.suppliers.find((item) => item.supplier_id === nextUpdateRequest.supplier_id);
    els.prompt.value = `更新 ${supplier?.name || "所选供应商"} 在「${nextUpdateRequest.source_scope}」内的报价。${mode === "full" ? "按确认完整范围预审新增、修改及缺失删除。" : "增量更新，未出现的旧报价保留。"} 保留原报价ID，所有修改等待审核确认。`;
    $("preparedUpdateText").textContent = `待更新：${supplier?.name || "供应商"} / ${nextUpdateRequest.source_scope} / ${mode === "full" ? "完整替换" : "增量"}`; $("preparedUpdateNotice").hidden = false;
    els.filePicker.click(); els.prompt.focus();
  });
  window.TokenSQLReview = { sendPrompt, lookupOfficial, canAttachFiles, showJob, openReview, routeSupplier, refreshSupplierProfile, get supplierContext() { return supplierContext; }, get currentJob() { return currentJob; } };
  async function initialize() {
    const generation = sessionGeneration;
    state.sessionRestoring = true; updateComposer();
    try {
      const url = new URL(window.location.href), sessionId = url.searchParams.get("session"), supplierId = url.searchParams.get("supplier");
      rememberSession(sessionId);
      await loadCatalog(); fillConversationSuppliers(); await loadSessions(); await loadFiles();
      if (generation !== sessionGeneration) return;
      if (supplierId && state.catalog.suppliers.some((supplier) => supplier.supplier_id === supplierId)) { await routeSupplier(supplierId, { scope: url.searchParams.get("scope") || "default" }); return; }
      if (sessionId) {
        const session = sessionItems.find((item) => item.id === sessionId);
        if (session?.supplier_id) { rememberSupplier(session.supplier_id); await refreshSupplierProfile(); }
        await restoreSession(sessionId); return;
      }
      const result = await api("/imports");
      if (generation !== sessionGeneration) return;
      const pending = latestJob((result.items || []).filter((job) => !terminal.has(job.status)));
      if (pending?.conversation_id) await restoreSession(pending.conversation_id);
      else {
        state.sessionRestoring = false;
        if (pending) await resumeJob(pending);
        updateComposer();
      }
    } catch (error) {
      if (generation === sessionGeneration) { state.sessionRestoring = false; updateComposer(); }
      toast(error.message, true);
    }
  }
  initialize();
})();
