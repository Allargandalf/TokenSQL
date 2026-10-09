(() => {
  "use strict";

  const API_BASE = "/api/workspace";
  const PAGE_SIZE = 100;
  const POLL_INTERVAL = 1400;
  const MAX_FILE_COUNT = 12;

  const state = {
    items: [],
    total: 0,
    offset: 0,
    limit: PAGE_SIZE,
    selected: new Set(),
    files: [],
    conversationId: null,
    agentBusy: false,
    editing: null,
    querySerial: 0,
  };

  const $ = (id) => document.getElementById(id);
  const els = {
    quoteRows: $("quoteRows"),
    tableState: $("tableState"),
    resultCount: $("resultCount"),
    pageLabel: $("pageLabel"),
    previousPage: $("previousPageButton"),
    nextPage: $("nextPageButton"),
    selectAll: $("selectAll"),
    selectionBar: $("selectionBar"),
    selectedCount: $("selectedCount"),
    search: $("searchInput"),
    manufacturer: $("manufacturerFilter"),
    provider: $("providerFilter"),
    currency: $("currencyFilter"),
    metric: $("metricFilter"),
    dimension: $("dimensionFilter"),
    syncLabel: $("syncLabel"),
    refresh: $("refreshButton"),
    filePicker: $("filePicker"),
    prompt: $("promptInput"),
    composer: $("composer"),
    fileChips: $("fileChips"),
    send: $("sendButton"),
    conversation: $("conversation"),
    agentStatus: $("agentStatus"),
    globalHealth: $("globalHealth"),
    editor: $("editorDialog"),
    editorTitle: $("editorTitle"),
    quoteForm: $("quoteForm"),
    saveQuote: $("saveQuoteButton"),
    formError: $("formError"),
    toasts: $("toastRegion"),
  };

  function node(tag, options = {}, children = []) {
    const element = document.createElement(tag);
    if (options.className) element.className = options.className;
    if (options.text !== undefined) element.textContent = String(options.text);
    if (options.title) element.title = options.title;
    if (options.type) element.type = options.type;
    if (options.ariaLabel) element.setAttribute("aria-label", options.ariaLabel);
    for (const child of children) {
      if (child !== null && child !== undefined) {
        element.append(child instanceof Node ? child : document.createTextNode(String(child)));
      }
    }
    return element;
  }

  function valueOrDash(value) {
    return value === null || value === undefined || value === "" ? "—" : String(value);
  }

  function toast(message, isError = false) {
    const item = node("div", { className: isError ? "toast error" : "toast", text: message });
    els.toasts.append(item);
    window.setTimeout(() => item.remove(), 3600);
  }

  async function api(path, options = {}) {
    const response = await fetch(API_BASE + path, {
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...(options.body instanceof FormData ? {} : options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
      ...options,
    });
    if (!response.ok) {
      let detail = `请求失败（${response.status}）`;
      try {
        const payload = await response.json();
        detail = typeof payload.detail === "string" ? payload.detail : detail;
      } catch (_) {
        // Keep the status-based message for non-JSON responses.
      }
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function currentFilters(includePaging = true) {
    const params = new URLSearchParams();
    const fields = [
      ["q", els.search.value.trim()],
      ["manufacturer", els.manufacturer.value],
      ["provider", els.provider.value],
      ["currency", els.currency.value],
      ["dimension", els.dimension.value],
      ["metric", els.metric.value],
    ];
    fields.forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    if (includePaging) {
      params.set("limit", String(state.limit));
      params.set("offset", String(state.offset));
    }
    return params;
  }

  function setTableState(kind, message) {
    els.tableState.replaceChildren();
    if (!kind) {
      els.tableState.hidden = true;
      return;
    }
    els.tableState.hidden = false;
    if (kind === "loading") {
      els.tableState.append(node("div", { className: "spinner" }), node("span", { text: message || "正在读取报价…" }));
      return;
    }
    const title = kind === "error" ? "暂时无法读取数据" : "没有符合条件的报价";
    const retry = node("button", { className: "button secondary small", text: kind === "error" ? "重试" : "清除筛选", type: "button" });
    retry.addEventListener("click", () => kind === "error" ? loadQuotes() : clearFilters());
    els.tableState.append(node("strong", { text: title }), node("span", { text: message }), retry);
  }

  function fillOptions(select, values, label) {
    const active = select.value;
    const normalized = Array.isArray(values) ? values : [];
    select.replaceChildren(node("option", { text: label }));
    select.firstChild.value = "";
    for (const value of normalized) {
      if (value === null || value === undefined || value === "") continue;
      const option = node("option", { text: value });
      option.value = String(value);
      select.append(option);
    }
    if ([...select.options].some((option) => option.value === active)) select.value = active;
  }

  function applyFilterOptions(options = {}) {
    fillOptions(els.manufacturer, options.manufacturers || options.manufacturer, "全部厂商");
    fillOptions(els.provider, options.providers || options.provider, "全部供应商");
    fillOptions(els.dimension, options.dimensions || options.dimension, "全部维度");
    fillOptions(els.currency, options.currencies || options.currency, "全部币种");
    fillOptions(els.metric, options.metrics || options.metric, "全部计费项");
  }

  function tableCell(text, className = "") {
    return node("td", { className, text: valueOrDash(text) });
  }

  function renderRows() {
    els.quoteRows.replaceChildren();
    for (const quote of state.items) {
      const row = node("tr");
      row.dataset.id = String(quote.id);
      if (state.selected.has(quote.id)) row.classList.add("is-selected");

      const check = node("input", { type: "checkbox", ariaLabel: `选择 ${quote.model_name}` });
      check.checked = state.selected.has(quote.id);
      check.addEventListener("change", () => {
        check.checked ? state.selected.add(quote.id) : state.selected.delete(quote.id);
        updateSelection();
        renderRows();
      });
      const checkCell = node("td", { className: "check-cell" }, [check]);

      const name = node("span", { className: "model-name", text: valueOrDash(quote.model_name), title: valueOrDash(quote.model_name) });
      const version = node("span", { className: "model-version", text: quote.model_version || "" });
      const modelCell = node("td", { className: "model-cell" }, [name, version]);

      const metricBadge = node("span", { className: "metric-badge", text: valueOrDash(quote.metric) });
      const metricCell = node("td", {}, [metricBadge]);
      const discountRate = Number(quote.discount_rate);
      const displayAmount = quote.component_type === "discount" && Number.isFinite(discountRate)
        ? `${(discountRate * 10).toLocaleString("zh-CN", { maximumFractionDigits: 4 })} 折` : quote.amount;
      const amountCell = tableCell(displayAmount, "number-cell");

      const unitCell = quote.component_type === "discount"
        ? node("td", {}, [node("span", { text: "相对目录价" }), node("span", { className: "muted-cell", text: "折扣率" })])
        : node("td", {}, [
          node("span", { text: valueOrDash(quote.currency) }),
          node("span", { className: "muted-cell", text: `/ ${valueOrDash(quote.unit_quantity)} ${valueOrDash(quote.unit)}` }),
        ]);
      const sourceCell = node("td", {}, [
        node("span", { text: valueOrDash(quote.source), title: valueOrDash(quote.source) }),
        node("span", { className: "muted-cell", text: quote.updated_at ? `更新于 ${formatDateTime(quote.updated_at)}` : "" }),
      ]);

      const exportButton = node("button", {
        className: "icon-button table-action",
        text: "↗",
        type: "button",
        title: `导出 ${quote.model_name} 的全部报价`,
        ariaLabel: `导出 ${quote.model_name} 的全部报价`,
      });
      exportButton.addEventListener("click", (event) => {
        event.stopPropagation();
        exportQuotes(new URLSearchParams({ format: $("exportFormat").value, model_name: quote.model_name }));
      });

      row.append(
        checkCell,
        modelCell,
        tableCell(quote.manufacturer),
        tableCell(quote.provider),
        metricCell,
        amountCell,
        unitCell,
        tableCell(formatDate(quote.effective_date)),
        sourceCell,
        node("td", { className: "action-cell" }, [exportButton]),
      );
      row.addEventListener("dblclick", (event) => {
        if (event.target instanceof HTMLInputElement || event.target instanceof HTMLButtonElement) return;
        openEditor(quote);
      });
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          openEditor(quote);
        }
      });
      row.tabIndex = 0;
      row.title = "双击编辑";
      els.quoteRows.append(row);
    }
  }

  function updateSelection() {
    const count = state.selected.size;
    els.selectionBar.hidden = count === 0;
    els.selectedCount.textContent = String(count);
    const currentIds = state.items.map((item) => item.id);
    els.selectAll.checked = currentIds.length > 0 && currentIds.every((id) => state.selected.has(id));
    els.selectAll.indeterminate = currentIds.some((id) => state.selected.has(id)) && !els.selectAll.checked;
  }

  function updatePagination() {
    const pageCount = Math.max(1, Math.ceil(state.total / state.limit));
    const currentPage = Math.floor(state.offset / state.limit) + 1;
    els.resultCount.textContent = `${state.total.toLocaleString("zh-CN")} 条报价`;
    els.pageLabel.textContent = `${currentPage} / ${pageCount}`;
    els.previousPage.disabled = state.offset <= 0;
    els.nextPage.disabled = state.offset + state.limit >= state.total;
  }

  async function loadQuotes(options = {}) {
    const serial = ++state.querySerial;
    if (!options.quiet) setTableState("loading");
    try {
      const payload = await api(`/quotes?${currentFilters()}`);
      if (serial !== state.querySerial) return;
      state.items = Array.isArray(payload.items) ? payload.items : [];
      state.total = Number(payload.total || 0);
      applyFilterOptions(payload.filter_options || {});
      renderRows();
      updateSelection();
      updatePagination();
      setTableState(state.items.length ? null : "empty", "换个关键词或清除筛选后再试。");
      els.syncLabel.textContent = `已同步 · ${new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date())}`;
      els.globalHealth.className = "health-dot is-online";
      els.globalHealth.title = "数据库已连接";
    } catch (error) {
      if (serial !== state.querySerial) return;
      state.items = [];
      renderRows();
      updatePagination();
      setTableState("error", error.message);
      els.syncLabel.textContent = "同步失败";
      els.globalHealth.className = "health-dot is-offline";
      els.globalHealth.title = error.message;
    }
  }

  function clearFilters() {
    els.search.value = "";
    [els.manufacturer, els.provider, els.currency, els.metric, els.dimension].forEach((select) => { select.value = ""; });
    state.offset = 0;
    state.selected.clear();
    updateSelection();
    loadQuotes();
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value).slice(0, 10) : new Intl.DateTimeFormat("zh-CN").format(date);
  }

  function formatDateTime(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
  }

  function exportName(response, fallback) {
    const disposition = response.headers.get("Content-Disposition") || "";
    const utf = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const plain = disposition.match(/filename="?([^";]+)"?/i);
    const candidate = utf ? decodeURIComponent(utf[1]) : plain ? plain[1] : fallback;
    return candidate.replace(/[\\/:*?"<>|]/g, "_");
  }

  async function exportQuotes(params) {
    try {
      const response = await fetch(`${API_BASE}/exports?${params}`, { credentials: "same-origin" });
      if (!response.ok) {
        let message = `导出失败（${response.status}）`;
        try {
          const payload = await response.json();
          if (typeof payload.detail === "string") message = payload.detail;
        } catch (_) {}
        throw new Error(message);
      }
      const blob = await response.blob();
      const href = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = exportName(response, "tokensql-quotes.xlsx");
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(href);
      toast("导出文件已生成");
    } catch (error) {
      toast(error.message, true);
    }
  }

  function formField(name) {
    return els.quoteForm.elements.namedItem(name);
  }

  function openEditor(quote = null) {
    state.editing = quote;
    els.editorTitle.textContent = quote ? "编辑报价" : "新增报价";
    els.formError.textContent = "";
    const defaults = {
      model_name: "", model_version: "", manufacturer: "", provider: "", provider_type: "unknown", component_type: "monetary",
      amount: "", discount_rate: "", currency: "CNY", unit: "token", unit_quantity: "1000000",
      metric: "input_tokens", dimension: "token", direction: "input", effective_date: "",
      source: "", source_ref: "", conditions: {}, note: "",
    };
    const values = { ...defaults, ...(quote || {}) };
    for (const name of Object.keys(defaults)) {
      const field = formField(name);
      if (!field) continue;
      if (name === "conditions") field.value = JSON.stringify(values.conditions || {}, null, 2);
      else if (name === "effective_date") field.value = values[name] ? String(values[name]).slice(0, 10) : "";
      else field.value = values[name] ?? "";
    }
    updateComponentFields();
    els.editor.showModal();
    window.setTimeout(() => formField("model_name").focus(), 30);
  }

  function updateComponentFields() {
    const isDiscount = formField("component_type").value === "discount";
    els.quoteForm.querySelector("[data-money-field]").hidden = isDiscount;
    els.quoteForm.querySelector("[data-discount-field]").hidden = !isDiscount;
    formField("amount").required = !isDiscount;
    formField("discount_rate").required = isDiscount;
    ["currency", "unit", "unit_quantity"].forEach((name) => {
      formField(name).required = !isDiscount;
    });
  }

  async function saveQuote() {
    if (!els.quoteForm.reportValidity()) return;
    const isDiscount = formField("component_type").value === "discount";
    if (isDiscount) {
      const rate = Number(formField("discount_rate").value);
      if (!Number.isFinite(rate) || rate <= 0 || rate > 1) {
        els.formError.textContent = "折扣率必须大于 0 且不超过 1，例如 0.31 表示 3.1 折。";
        formField("discount_rate").focus();
        return;
      }
    }
    let conditions;
    try {
      conditions = JSON.parse(formField("conditions").value.trim() || "{}");
      if (!conditions || Array.isArray(conditions) || typeof conditions !== "object") throw new Error();
    } catch (_) {
      els.formError.textContent = "条件必须是 JSON 对象，例如 {\"region\": \"cn\"}。";
      formField("conditions").focus();
      return;
    }
    const fields = [
      "model_name", "model_version", "manufacturer", "provider", "provider_type", "component_type",
      "amount", "discount_rate", "currency", "unit", "unit_quantity", "metric",
      "dimension", "direction", "effective_date", "source", "source_ref", "note",
    ];
    const item = { conditions };
    fields.forEach((name) => { item[name] = formField(name).value.trim(); });
    ["model_version", "manufacturer", "amount", "discount_rate", "currency", "unit", "unit_quantity", "dimension", "effective_date", "source", "source_ref", "note"].forEach((name) => {
      if (!item[name]) item[name] = null;
    });
    item.amount = isDiscount ? null : item.amount;
    item.discount_rate = isDiscount ? item.discount_rate : null;
    if (!item.unit_quantity) item.unit_quantity = "1";
    els.saveQuote.disabled = true;
    els.formError.textContent = "";
    try {
      if (state.editing) {
        await api(`/quotes/${state.editing.id}`, {
          method: "PATCH",
          body: JSON.stringify({ ...item, expected_revision: state.editing.revision }),
        });
        toast("报价已更新");
      } else {
        await api("/quotes", { method: "POST", body: JSON.stringify({ items: [item] }) });
        toast("报价已新增");
      }
      els.editor.close();
      await loadQuotes({ quiet: true });
    } catch (error) {
      els.formError.textContent = error.status === 409
        ? "这条报价已被其他操作更新。表格已经刷新，请重新打开后编辑。"
        : error.message;
      if (error.status === 409) loadQuotes({ quiet: true });
    } finally {
      els.saveQuote.disabled = false;
    }
  }

  function fileKey(file) {
    return `${file.name}:${file.size}:${file.lastModified}`;
  }

  function addFiles(fileList) {
    const existing = new Set(state.files.map(fileKey));
    for (const file of Array.from(fileList || [])) {
      if (state.files.length >= MAX_FILE_COUNT) {
        toast(`一次最多添加 ${MAX_FILE_COUNT} 个文件`, true);
        break;
      }
      if (!existing.has(fileKey(file))) {
        state.files.push(file);
        existing.add(fileKey(file));
      }
    }
    renderFileChips();
    els.prompt.focus();
  }

  function renderFileChips() {
    els.fileChips.replaceChildren();
    state.files.forEach((file, index) => {
      const remove = node("button", { text: "×", type: "button", ariaLabel: `移除 ${file.name}` });
      remove.addEventListener("click", () => {
        state.files.splice(index, 1);
        renderFileChips();
      });
      els.fileChips.append(node("div", { className: "file-chip" }, [
        node("span", { text: file.name, title: file.name }),
        node("small", { text: formatFileSize(file.size) }),
        remove,
      ]));
    });
  }

  function formatFileSize(size) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
  }

  function toolMessages(messages) {
    return Array.isArray(messages)
      ? messages.filter((message) => message && message.kind === "tool")
      : [];
  }

  function renderToolActivity(container, messages) {
    const tools = toolMessages(messages);
    container.replaceChildren();
    tools.forEach((tool) => {
      const status = tool.status === "failed" ? "失败" : tool.status === "completed" ? "完成" : "执行中";
      container.append(node("div", { className: `tool-step ${tool.status || "running"}` }, [
        node("i"), node("span", { text: tool.text || tool.name || "工具调用" }), node("small", { text: status }),
      ]));
    });
  }
  function appendMessage(role, text, files = [], changes = null, activity = []) {
    const article = node("article", { className: `message ${role}` });
    article.append(node("div", { className: "message-label", text: role === "user" ? "你" : "OPENCODE" }));
    const body = node("div", { className: "message-body" });
    const tools = toolMessages(activity);
    if (tools.length) {
      const details = node("details", { className: "tool-summary" });
      details.append(node("summary", { text: `查看执行步骤（${tools.length}）` }));
      const steps = node("div", { className: "tool-steps" });
      renderToolActivity(steps, tools);
      details.append(steps);
      body.append(details);
    }
    body.append(node("p", { text }));
    if (files.length) {
      const attachments = node("div", { className: "message-files" });
      files.forEach((name) => attachments.append(node("span", { className: "message-file", text: name })));
      body.append(attachments);
    }
    if (changes) {
      const summary = node("div", { className: "change-summary" });
      const labels = [["added", "新增"], ["updated", "更新"], ["deleted", "删除"], ["total", "合计"]];
      labels.forEach(([key, label]) => {
        if (changes[key] !== undefined && changes[key] !== null) {
          summary.append(node("span", { text: `${label} ${changes[key]}` }));
        }
      });
      body.append(summary);
    }
    article.append(body);
    els.conversation.append(article);
    els.conversation.scrollTop = els.conversation.scrollHeight;
    return article;
  }

  function appendProgress() {
    const article = node("article", { className: "message assistant" });
    article.append(node("div", { className: "message-label", text: "OPENCODE" }));
    const label = node("span", { text: "正在读取文件并维护数据库…" });
    const activity = node("div", { className: "tool-steps live" });
    const body = node("div", { className: "message-body" }, [
      node("div", { className: "run-progress" }, [node("div", { className: "spinner" }), label]),
      activity,
    ]);
    article.append(body);
    els.conversation.append(article);
    els.conversation.scrollTop = els.conversation.scrollHeight;
    return { article, label, activity };
  }

  function setAgentStatus(available, text) {
    els.agentStatus.className = `status-pill ${available ? "is-online" : "is-offline"}`;
    els.agentStatus.replaceChildren(node("i"), document.createTextNode(text));
  }

  async function checkAgent() {
    try {
      const status = await api("/agent/status");
      setAgentStatus(Boolean(status.available), status.available ? "可用" : (status.status || "不可用"));
      els.agentStatus.title = status.model || status.status || "";
      els.send.disabled = !status.available || state.agentBusy;
    } catch (_) {
      setAgentStatus(false, "未连接");
      els.send.disabled = true;
    }
  }

  async function sendPrompt() {
    const prompt = els.prompt.value.trim();
    if ((!prompt && state.files.length === 0) || state.agentBusy) return;
    const sentFiles = [...state.files];
    appendMessage("user", prompt || "请处理这些文件。", sentFiles.map((file) => file.name));
    els.prompt.value = "";
    state.files = [];
    renderFileChips();
    state.agentBusy = true;
    els.send.disabled = true;
    const progress = appendProgress();

    try {
      const form = new FormData();
      form.append("prompt", prompt || "请分析附件并根据其中的信息维护报价数据库。");
      if (state.conversationId) form.append("conversation_id", state.conversationId);
      sentFiles.forEach((file) => form.append("files", file, file.name));
      const accepted = await api("/agent/messages", { method: "POST", body: form });
      state.conversationId = accepted.conversation_id || state.conversationId;
      await pollRun(accepted.run_id, progress);
    } catch (error) {
      progress.article.remove();
      appendMessage("assistant", `执行失败：${error.message}`);
      toast(error.message, true);
    } finally {
      state.agentBusy = false;
      await checkAgent();
    }
  }

  async function pollRun(runId, progress) {
    if (!runId) throw new Error("服务没有返回任务编号");
    for (;;) {
      await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL));
      const run = await api(`/agent/runs/${encodeURIComponent(runId)}`);
      state.conversationId = run.conversation_id || state.conversationId;
      renderToolActivity(progress.activity, run.messages);
      if (run.status === "queued") {
        progress.label.textContent = "任务已排队，正在等待 OpenCode…";
        continue;
      }
      if (run.status === "running") {
        progress.label.textContent = "OpenCode 正在分析文件并写入数据库…";
        continue;
      }
      progress.article.remove();
      if (run.status === "completed") {
        const message = run.assistant_message || finalAssistantMessage(run.messages) || "处理完成，报价数据已刷新。";
        appendMessage("assistant", message, [], run.changes || null, run.messages);
        await loadQuotes({ quiet: true });
        return;
      }
      if (run.status === "failed") {
        const message = `执行失败：${run.error || run.assistant_message || "OpenCode 未能完成任务"}`;
        appendMessage("assistant", message, [], run.changes || null, run.messages);
        if (Number(run.changes?.total || 0) > 0) await loadQuotes({ quiet: true });
        toast(run.error || "OpenCode 任务执行失败", true);
        return;
      }

      throw new Error(run.error || "OpenCode 任务执行失败");
    }
  }

  function finalAssistantMessage(messages) {
    if (!Array.isArray(messages)) return "";
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message && message.role === "assistant" && typeof message.text === "string") return message.text;
      if (message && message.role === "assistant" && typeof message.content === "string") return message.content;
    }
    return "";
  }

  let searchTimer = null;
  els.search.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.offset = 0;
      state.selected.clear();
      updateSelection();
      loadQuotes();
    }, 280);
  });
  [els.manufacturer, els.provider, els.currency, els.metric, els.dimension].forEach((select) => {
    select.addEventListener("change", () => {
      state.offset = 0;
      state.selected.clear();
      updateSelection();
      loadQuotes();
    });
  });

  $("clearFiltersButton").addEventListener("click", clearFilters);
  els.refresh.addEventListener("click", () => loadQuotes());
  $("newQuoteButton").addEventListener("click", () => openEditor());
  els.previousPage.addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadQuotes();
  });
  els.nextPage.addEventListener("click", () => {
    state.offset += state.limit;
    loadQuotes();
  });
  els.selectAll.addEventListener("change", () => {
    state.items.forEach((item) => els.selectAll.checked ? state.selected.add(item.id) : state.selected.delete(item.id));
    renderRows();
    updateSelection();
  });
  $("clearSelectionButton").addEventListener("click", () => {
    state.selected.clear();
    renderRows();
    updateSelection();
  });
  $("exportSelectedButton").addEventListener("click", () => {
    if (!state.selected.size) return;
    exportQuotes(new URLSearchParams({ format: $("exportFormat").value, ids: [...state.selected].join(",") }));
  });
  $("exportFilteredButton").addEventListener("click", () => {
    const params = currentFilters(false);
    params.set("format", $("exportFormat").value);
    exportQuotes(params);
  });

  els.quoteForm.addEventListener("submit", (event) => {
    event.preventDefault();
    saveQuote();
  });
  formField("component_type").addEventListener("change", updateComponentFields);
  document.querySelectorAll("[data-close-editor]").forEach((button) => {
    button.addEventListener("click", () => els.editor.close());
  });
  els.editor.addEventListener("click", (event) => {
    if (event.target === els.editor) els.editor.close();
  });

  function pickFiles() {
    els.filePicker.value = "";
    els.filePicker.click();
  }
  $("importButton").addEventListener("click", pickFiles);
  $("attachButton").addEventListener("click", pickFiles);
  els.filePicker.addEventListener("change", () => addFiles(els.filePicker.files));
  els.send.addEventListener("click", sendPrompt);
  els.prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendPrompt();
    }
  });
  els.prompt.addEventListener("paste", (event) => {
    const files = Array.from(event.clipboardData?.items || [])
      .filter((item) => item.kind === "file")
      .map((item) => item.getAsFile())
      .filter(Boolean);
    if (files.length) {
      event.preventDefault();
      addFiles(files);
    }
  });

  let dragDepth = 0;
  els.composer.addEventListener("dragenter", (event) => {
    event.preventDefault();
    dragDepth += 1;
    els.composer.classList.add("is-dragging");
  });
  els.composer.addEventListener("dragover", (event) => event.preventDefault());
  els.composer.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) els.composer.classList.remove("is-dragging");
  });
  els.composer.addEventListener("drop", (event) => {
    event.preventDefault();
    dragDepth = 0;
    els.composer.classList.remove("is-dragging");
    addFiles(event.dataTransfer?.files);
  });

  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      els.prompt.value = button.dataset.prompt || "";
      els.prompt.focus();
    });
  });

  Promise.allSettled([loadQuotes(), checkAgent()]);
  window.setInterval(checkAgent, 30000);
})();
