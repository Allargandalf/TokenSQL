(() => {
  "use strict";
  const { state, els, $, node, api, toast, priceDisplay, loadQuotes, loadCatalog, appendMessage, appendProgress, renderToolActivity, checkAgent, renderFileChips, pricingDisplay } = window.TokenSQL;
  const terminal = new Set(["cancelled", "confirmed", "failed"]);
  let currentJob = null;
  let reviewBundle = null;
  let reviewDirty = false;
  let reviewBusy = false;
  let selectedUploads = new Set();
  let pollGeneration = 0;
  let memory = { revision: 0, rules: [] };
  let sessionGeneration = 0;
  const historyAnchors = new Map();
  const renderedReplies = new Set();
  const readyStatuses = new Set(["ready", "review"]);
  const waitingStatus = "waiting_for_input";

  function latestJob(jobs, sessionId = undefined) {
    return (jobs || []).filter((job) => !["cancelled", "confirmed"].includes(job.status) &&
      (sessionId === undefined || job.conversation_id === sessionId))
      .sort((left, right) => String(right.updated_at || right.created_at || "").localeCompare(String(left.updated_at || left.created_at || "")))[0];
  }

  function renderJobReply(job) {
    const key = job.id + ":" + (job.attempt || 1) + ":" + job.status;
    if (renderedReplies.has(key) || !job.assistant_message) return;
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
    const select = $("sessionSelect");
    select.replaceChildren(node("option", { text: "新会话" }));
    select.firstChild.value = "";
    (result.items || []).forEach((session) => {
      const option = node("option", { text: session.title || session.id });
      option.value = session.id;
      select.append(option);
    });
    if (state.conversationId && ![...select.options].some((option) => option.value === state.conversationId)) {
      const option = node("option", { text: state.conversationId }); option.value = state.conversationId; select.append(option);
    }
    select.value = state.conversationId || "";
  }

  async function restoreSession(id) {
    if (state.agentBusy) return;
    const generation = ++sessionGeneration;
    ++pollGeneration; currentJob = null; renderedReplies.clear();
    $("reviewDialog").close(); $("jobStrip").hidden = true; $("waitingPanel").hidden = true;
    rememberSession(id); state.sessionRestoring = Boolean(id); updateComposer();
    if (!id) { els.conversation.replaceChildren(); appendMessage("assistant", "添加文件和说明，解析完成后可审核全部候选数据。"); return; }
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
    els.conversation.replaceChildren();
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
    ["attachButton", "importButton"].forEach((id) => { $(id).disabled = blocked; });
    $("inputFiles").querySelectorAll("input[type=checkbox]").forEach((input) => { input.disabled = blocked && !input.checked; });
    els.prompt.placeholder = waiting ? "回答上方问题，继续处理原文件（无需重新上传）…" : "输入指令，或拖入 / 粘贴文件…";
    els.send.setAttribute("aria-label", waiting ? "发送补充回答" : "发送");
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
    currentJob = job;
    const ready = readyStatuses.has(job.status), waiting = job.status === waitingStatus;
    $("jobStrip").hidden = job.status === "cancelled" || job.status === "confirmed";
    $("jobStatus").textContent = waiting ? "等待补充 · 第 " + (job.attempt || 1) + " 轮 · 正式数据未变更" : ready ? "候选方案已就绪，等待人工审核" : job.status === "failed" ? "任务失败，可补充说明或取消并退回文件" : job.status === "queued" ? "任务排队中…" : "OpenCode 正在分析文件…";
    $("reviewOpenButton").hidden = !ready;
    $("abortButton").textContent = waiting || job.status === "failed" ? "取消并退回文件" : ready ? "取消草案" : "中止";
    $("waitingPanel").hidden = !waiting;
    if (waiting) {
      $("reviewDialog").close();
      $("waitingSummary").textContent = job.question_summary || "请补充以下关键问题，再继续生成入库草案。";
      $("waitingQuestions").replaceChildren(...(job.questions || []).map((question) => node("li", { text: question })));
      $("waitingFiles").textContent = (job.files || []).length ? "沿用处理中的原文件：" + job.files.map((file) => file.filename || file.name || file.id).join("、") : "当前任务无附件；回答会沿用同一会话。";
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
    if (state.agentBusy || state.sessionRestoring || (!prompt && !state.files.length && !selectedUploads.size)) return;
    if (currentJob?.status === waitingStatus) { await respondPrompt(prompt); return; }
    if (currentJob && !terminal.has(currentJob.status)) { toast("请先确认或取消当前草案。", true); return; }
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
        conversation_id: state.conversationId,
      }) });
      startedJob = job;
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
    } finally { if (generation === pollGeneration) lockComposer(false); historyAnchors.delete(generation); await checkAgent(); await loadFiles().catch(() => {}); }
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
        method: "POST", body: JSON.stringify({ expected_revision: waiting.revision, prompt }),
      });
      if (generation !== pollGeneration) return;
      if (els.prompt.value.trim() === prompt) els.prompt.value = "";
      showJob(job);
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
    els.conversation.replaceChildren();
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
    create: ["model_id", "billing_item", "supplier", "tags", "supplier_model_id", "amount", "discount_rate", "currency", "pricing_unit_id", "source", "source_ref"],
    update: ["model_id", "billing_item", "supplier", "tags", "supplier_model_id", "amount", "discount_rate", "currency", "pricing_unit_id", "source", "source_ref"],
    delete: ["id", "expected_revision"],
  };
  const fieldLabels = { model_id: "唯一模型 ID", vendor: "模型商", model_name: "模型名称", aliases: "别名（逗号分隔）",
    billing_item: "计费项", amount: "原币金额", currency: "币种（新增默认人民币）", pricing_unit_id: "计价单位",
    explicit_official: "文件 / 来源明确标注官网价", source: "来源", source_ref: "单元格 / 来源依据", supplier: "供应商",
    tags: "标签（逗号分隔）", supplier_model_id: "原渠道 ID", discount_rate: "折扣（折，8.0 = 八折）", id: "报价 ID", expected_revision: "原版本" };

  function markDirty() {
    reviewDirty = true;
    $("reviewBundle").value = JSON.stringify(reviewBundle, null, 2);
    $("reviewPreview").textContent = "方案已修改，请保存修正并校验；原始字段独立保留，计算仅供前端显示。";
  }

  function renderReviewSections() {
    const target = $("reviewSections"); target.replaceChildren();
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
          if (section === "update" && !(key in data)) return;
          const input = ["pricing_unit_id", "currency"].includes(key) ? node("select") : node("input", { type: key === "explicit_official" ? "checkbox" : "text" });
          if (key === "pricing_unit_id") {
            const empty = node("option", { text: "未提供 / 不明确" }); empty.value = ""; input.append(empty);
            state.pricingUnits.items.forEach((unit) => {
              const option = node("option", { text: unit.label || unit.currency + " / " + unit.scale });
              option.value = unit.id; input.append(option);
            });
          }
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
            input.title = data[key] == null ? "折数四舍五入保留一位小数。" : "付款倍率：" + window.TokenSQLConversion.discountRatio(data[key]) + " ×；按 HALF_UP 保留两位小数入库。";
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
        card.append(grid); block.append(card);
      });
      target.append(block);
    }
    $("reviewBundle").value = JSON.stringify(reviewBundle, null, 2);
  }

  function renderPreview(job) {
    const preview = job.preview || {};
    const target = $("reviewPreview"); target.replaceChildren(node("strong", { text: "原始数据校验预览 · 正式数据尚未变更" }));
    const changes = preview.changes || job.changes || {};
    target.append(node("p", { text: `新增 ${changes.added ?? (reviewBundle.create || []).length} · 更新 ${changes.updated ?? (reviewBundle.update || []).length} · 删除 ${changes.deleted ?? (reviewBundle.delete || []).length}` }));
    const items = preview.items || preview.create || [];
    items.forEach((item) => {
      const official = preview.catalog?.official_prices?.find((price) =>
        price.model_id === item.model_id && (price.billing_item ?? null) === (item.billing_item ?? null));
      const displayed = official ? { ...item, official_price: official } : item;
      target.append(node("div", { className: "review-computed-item" }, [node("strong", { text: `${item.model_id} / ${item.billing_item ?? "未提供计费项"} / ${item.supplier ?? "未提供供应商"}` }), priceDisplay(displayed), pricingDisplay(displayed)]));
    });
    if (!items.length && Object.keys(preview).length) {
      const details = node("details", {}, [node("summary", { text: "查看完整校验结果" }), node("pre", { text: JSON.stringify(preview, null, 2) })]);
      target.append(details);
    }
    $("reviewIssues").replaceChildren();
    const issues = job.issues || preview.issues || [];
    issues.forEach((issue) => $("reviewIssues").append(node("p", { className: "validation-issue", text: typeof issue === "string" ? issue : JSON.stringify(issue) })));
  }

  async function openReview(job) {
    if (!readyStatuses.has(job.status)) {
      showJob(job); $("reviewDialog").close();
      if (job.status === waitingStatus) renderJobReply(job);
      toast(job.status === waitingStatus ? "当前任务正在等待补充，尚无可确认的入库草案。" : "任务当前不可审核，请检查最新状态。", true);
      return;
    }
    currentJob = job; reviewBundle = JSON.parse(JSON.stringify(job.bundle || {})); reviewDirty = false;
    $("reviewMeta").textContent = `草案 ${job.id} · 版本 ${job.revision} · ${(job.files || []).map((file) => file.filename || file.name || "").join("、") || "人工编辑"}`;
    renderReviewSections(); renderPreview(job);
    $("reviewIssues").textContent = job.error || "";
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
    for (const input of $("reviewSections").querySelectorAll("input,select")) { if (!input.reportValidity()) return false; }
    reviewBusy = true; $("previewReviewButton").disabled = true; $("confirmReviewButton").disabled = true;
    try {
      const bundle = parseBundle($("reviewBundle").value);
      if (reviewDirty) {
        const job = await api(`/imports/${encodeURIComponent(currentJob.id)}/proposal`, {
          method: "PATCH", body: JSON.stringify({ expected_revision: currentJob.revision, bundle }),
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
    $("confirmReviewButton").disabled = true;
    try {
      const job = await api(`/imports/${encodeURIComponent(currentJob.id)}/confirm`, {
        method: "POST", body: JSON.stringify({ expected_revision: currentJob.revision, idempotency_key: `workspace-${currentJob.id}` }),
      });
      ++pollGeneration; showJob(job); $("reviewDialog").close();
      appendMessage("assistant", "已确认入库，文件已归档。已确认修正按作用域保存。", [], job.changes || {});
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
      appendMessage("assistant", "任务已取消，文件已退回输入区。正式报价与纠错记忆未变更。"); toast("已取消");
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
        "model_id", "billing_item", "amount", "currency", "unit", "unit_quantity", "pricing_unit_id", "explicit_official", "source", "source_ref", "conversion", "effective_date", "note",
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
          node("div", { className: "review-section-heading" }, [node("h3", { text: `${model.vendor ?? "未提供模型商"} · ${model.model_name || model.model_id}` }), edit]),
          node("p", { className: "muted-cell", text: `唯一 ID：${model.model_id}` }),
          node("p", { className: "muted-cell", text: `别名：${(model.aliases || []).join("、") || "无"}` }),
        ]);
        for (const price of state.catalog.official_prices.filter((item) => item.model_id === model.model_id)) {
          const button = node("button", { className: "text-button", type: "button", text: "编辑官网计费项" });
          button.addEventListener("click", () => manualDraft("official_prices", price));
          block.append(node("div", { className: "catalog-price" }, [
            node("span", { text: `${price.billing_item ?? "未提供计费项"} · ${price.currency ?? ""} ${price.amount ?? "未提供金额"}` }), priceDisplay(price),
            node("small", { text: `${price.source ?? ""} · ${price.source_ref ?? ""}` }), button,
          ]));
        }
        target.append(block);
      }
      if (!state.catalog.models.length) target.append(node("p", { text: "模型目录为空。新增报价时输入模型 ID 即可自动创建；也可在这里维护模型资料和官网计费项。" }));
      $("catalogDialog").showModal();
    } catch (error) { toast(error.message, true); }
  }

  function renderMemory() {
    const target = $("memoryRules"); target.replaceChildren();
    memory.rules.forEach((rule, index) => {
      const enabled = node("input", { type: "checkbox" }); enabled.checked = rule.enabled !== false;
      enabled.addEventListener("change", () => { rule.enabled = enabled.checked; });
      const scope = node("input", { type: "text" }); scope.value = typeof rule.scope === "object" ? JSON.stringify(rule.scope) : rule.scope || "";
      scope.setAttribute("aria-label", `规则 ${index + 1} 作用域`);
      scope.addEventListener("input", () => { rule.scope = scope.value; });
      const text = node("textarea"); text.rows = 3; text.value = rule.text || ""; text.setAttribute("aria-label", `规则 ${index + 1} 内容`);
      text.addEventListener("input", () => { rule.text = text.value; });
      const remove = node("button", { className: "text-button danger", type: "button", text: "删除" });
      remove.addEventListener("click", () => { memory.rules.splice(index, 1); renderMemory(); });
      target.append(node("section", { className: "memory-rule" }, [
        node("div", { className: "review-section-heading" }, [node("label", {}, [enabled, " 启用"]), remove]),
        node("label", {}, ["作用域（如 supplier:供应商 / model:唯一 ID）", scope]), text,
        node("small", { text: `来源：${rule.source || "人工编辑"}${rule.import_id ? " · 草案 " + rule.import_id : ""}` }),
      ]));
    });
    if (!memory.rules.length) target.append(node("p", { text: "尚无已确认纠错规则。" }));
  }

  async function openMemory() {
    try {
      memory = await api("/agent/memory"); renderMemory(); $("memoryError").textContent = ""; $("memoryDialog").showModal();
    } catch (error) { toast(error.message, true); }
  }

  async function saveMemory() {
    $("saveMemoryButton").disabled = true;
    try {
      memory = await api("/agent/memory", { method: "PATCH", body: JSON.stringify({ expected_revision: memory.revision, rules: memory.rules }) });
      renderMemory(); $("memoryDialog").close(); toast("纠错记忆已保存");
    } catch (error) { $("memoryError").textContent = error.message; }
    finally { $("saveMemoryButton").disabled = false; }
  }

  $("reviewOpenButton").addEventListener("click", async () => {
    try { await openReview(await api(`/imports/${encodeURIComponent(currentJob.id)}`)); } catch (error) { toast(error.message, true); }
  });
  $("hideReviewButton").addEventListener("click", () => $("reviewDialog").close());
  $("cancelReviewButton").addEventListener("click", cancelCurrent);
  $("abortButton").addEventListener("click", cancelCurrent);
  $("previewReviewButton").addEventListener("click", saveReview);
  $("confirmReviewButton").addEventListener("click", confirmReview);
  $("reviewBundle").addEventListener("input", () => { reviewDirty = true; });
  $("applyReviewJsonButton").addEventListener("click", () => {
    try { reviewBundle = parseBundle($("reviewBundle").value); reviewDirty = true; renderReviewSections(); $("reviewIssues").textContent = ""; }
    catch (error) { $("reviewIssues").textContent = error.message; }
  });
  $("sessionSelect").addEventListener("change", () => restoreSession($("sessionSelect").value).catch((error) => toast(error.message, true)));
  $("newSessionButton").addEventListener("click", () => restoreSession(null));
  $("catalogButton").addEventListener("click", openCatalog);
  $("closeCatalogButton").addEventListener("click", () => $("catalogDialog").close());
  $("newModelButton").addEventListener("click", () => manualDraft("models"));
  $("newOfficialButton").addEventListener("click", () => manualDraft("official_prices"));
  $("memoryButton").addEventListener("click", openMemory);
  $("closeMemoryButton").addEventListener("click", () => $("memoryDialog").close());
  $("saveMemoryButton").addEventListener("click", saveMemory);
  $("addMemoryRuleButton").addEventListener("click", () => {
    memory.rules.push({ id: `manual-${Date.now()}`, scope: "global", text: "", enabled: true, source: "manual" }); renderMemory();
  });
  $("reviewDialog").addEventListener("keydown", (event) => { if (event.key === "Enter" && event.target.tagName === "INPUT") event.preventDefault(); });

  $("continueUnknownButton").addEventListener("click", () => {
    if (currentJob?.status !== waitingStatus || state.agentBusy) return;
    const instruction = "尚未提供的非关键项请留空继续，不猜测；若无法唯一识别模型 ID 等关键歧义仍需向我确认。请生成待人工审核的草案，不直接入库。";
    els.prompt.value = [els.prompt.value.trim(), instruction].filter(Boolean).join("\n");
    sendPrompt();
  });
  window.TokenSQLReview = { sendPrompt, lookupOfficial, canAttachFiles };
  async function initialize() {
    const generation = sessionGeneration;
    state.sessionRestoring = true; updateComposer();
    try {
      const sessionId = new URL(window.location.href).searchParams.get("session");
      rememberSession(sessionId);
      await loadSessions(); await loadFiles();
      if (generation !== sessionGeneration) return;
      if (sessionId) { await restoreSession(sessionId); return; }
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
