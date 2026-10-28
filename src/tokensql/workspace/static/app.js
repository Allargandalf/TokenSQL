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
    selected: new Map(),
    deleting: false,
    files: [],
    conversationId: null,
    agentBusy: false,
    sessionRestoring: false,
    editing: null,
    querySerial: 0,
    catalog: { models: [], official_prices: [], suppliers: [] },
    pricingUnits: { version: null, items: [] },
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
    tag: $("tagFilter"),
    currency: $("currencyFilter"),
    metric: $("metricFilter"),

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
        detail = typeof payload.detail === "string" ? payload.detail : Array.isArray(payload.detail) ? payload.detail.map((item) => `${(item.loc || []).join(".")}: ${item.msg}`).join("\n") : detail;
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
      ["vendor", els.manufacturer.value],
      ["supplier", els.provider.value],
      ["tag", els.tag.value],
      ["currency", els.currency.value],
      ["billing_item", els.metric.value],
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
    fillOptions(els.manufacturer, options.vendors || options.manufacturers, "全部模型商");
    fillOptions(els.provider, options.suppliers || options.providers, "全部供应商");
    fillOptions(els.tag, options.tags, "全部标签");
    fillOptions(els.currency, options.currencies || ["CNY", "USD", "HKD"], "全部原币");
    fillOptions(els.metric, options.billing_items || options.metrics, "全部计费项");
  }

  function tableCell(text, className = "") {
    return node("td", { className, text: valueOrDash(text) });
  }

  function priceDisplay(price, className = "") {
    const container = node("div", { className: `price-display ${className}` });
    if (!price) { container.append(node("span", { text: "未提供价格依据" })); return container; }
    const sourceUnit = window.TokenSQLConversion?.sourceUnit(price);
    const rawUnit = sourceUnit?.label || [price.currency, price.unit_quantity, price.unit].filter((value) => value != null && value !== "").join(" ") || "单位未提供";
    container.append(node("span", { text: price.amount == null ? "原金额未提供" : ((sourceUnit?.currency || price.currency) ? (sourceUnit?.currency || price.currency) + " " : "") + price.amount }),
      node("span", { className: "muted-cell", text: rawUnit + " · 原始单位" }));
    const display = window.TokenSQLConversion?.displayPrice(price) || price;
    if (display !== price) container.append(node("span", { className: "display-conversion", text: "前端折后价 " + display.currency + " " + display.amount + " / " + (window.TokenSQLConversion.sourceUnit(display)?.label || "官网单位").replace(/^[A-Z]+\s*\/\s*/, "") + " · 非原始报价" }));
    const converted = window.TokenSQLConversion?.convert(display);
    if (converted?.status === "converted") {
      container.append(node("span", { className: "display-conversion " + (converted.estimated ? "estimated" : "exact"), text: (converted.estimated ? "工程估算" : "显示换算") + " " + converted.currency + " " + converted.amount + " / " + converted.unitLabel }));
      container.append(node("span", { className: "muted-cell", text: "原价 " + converted.originalCurrency + " " + converted.originalAmount + " / " + converted.originalUnitLabel + " · " + converted.formula }));
      const evidence = node("details", { className: "price-evidence" }, [node("summary", { text: "FX 来源 / 版本 / 倍率" }), node("pre", { text: JSON.stringify(converted.evidence, null, 2) })]);
      container.append(evidence);
    } else if (converted?.status === "unavailable") {
      container.append(node("span", { className: "display-conversion unavailable", text: "未换算：" + converted.reason }));
      container.append(node("span", { className: "muted-cell", text: "已保留原价，未伪填结果。" }));
    }
    return container;
  }

  function priceCell(price) {
    return node("td", { className: "number-cell" }, [priceDisplay(price)]);
  }

  function pricingResult(computed) {
    const result = node("div", { className: "price-display pricing-result" });
    if (computed?.derivedAmount != null) result.append(node("span", { className: "display-conversion", text:
      "折后价 " + computed.officialUnit.currency + " " + computed.derivedAmount + " / " + computed.officialUnit.label.replace(/^[A-Z]+\s*\/\s*/, "") }));
    if (computed?.effectiveRule?.reason === "item_override") result.append(node("span", { className: "muted-cell", text: "采用指定计费项源折扣 " + window.TokenSQLConversion.discountFold(computed.rawDiscount) + "折（报价 #" + computed.effectiveRule.quote_id + "）" }));
    if (computed?.calculatedDiscountFold != null) result.append(node("span", { className: computed.estimated ? "estimated" : "display-conversion",
      text: (computed.estimated ? "按官网估算折扣 " : "按官网反算折扣 ") + computed.calculatedDiscountFold + "折" }));
    if (computed?.reason || computed?.discountIssue) result.append(node("span", { className: "display-conversion unavailable", text: computed.reason || computed.discountIssue }));
    return result;
  }

  function officialDetails(quote, calculation = false) {
    const conditions = window.TokenSQLConditions;
    const context = conditions.context(quote);
    const rows = conditions.displayRows(quote);
    const all = context.matched.length ? context.matched : context.candidates;
    const detailsKey = String(quote.id ?? quote.model_id) + ":" + calculation;
    const details = node("details", { className: "official-details" }, [node("summary", { text: calculation ? "展开逐项折后价 / 折扣" : "展开官网计费项与完整条件" })]);
    details.open = Boolean(conditions.expanded.get(detailsKey));
    details.addEventListener("toggle", () => conditions.expanded.set(detailsKey, details.open));
    if (all.length > 1) {
      const select = node("select"); select.setAttribute("aria-label", "查看 " + quote.model_id + " 的官网计费项" + (calculation ? "计算" : ""));
      const empty = node("option", { text: "全部 " + all.length + " 项（逐项展示）" }); empty.value = ""; select.append(empty);
      all.forEach((price) => { const option = node("option", { text: conditions.priceLabel(price) }); option.value = price.official_price_id || ""; select.append(option); });
      select.value = conditions.selected.get(String(quote.id)) || "";
      select.addEventListener("change", () => { conditions.selected.set(String(quote.id), select.value); conditions.expanded.set(detailsKey, true); renderRows(); });
      details.append(select);
    }
    for (const price of rows) {
      const item = node("section", { className: "official-detail-item" }, [node("strong", { text: conditions.metricLabel(price.billing_metric || price.billing_item) }),
        node("span", { className: "muted-cell", text: price.billing_item ? "原计费项：" + price.billing_item : "原计费项未提供" }),
        node("span", { className: "condition-description", text: conditions.conditionText(price.pricing_conditions) })]);
      if (price.conditions && Object.keys(price.conditions).length) item.append(node("span", { className: "muted-cell", text: "来源原文条件：" + JSON.stringify(price.conditions) }));
      if (calculation && ["matched", "multiple"].includes(context.status)) item.append(pricingResult(window.TokenSQLConversion.computePricing(quote, price)));
      else item.append(priceDisplay(price));
      item.append(node("span", { className: "muted-cell", text: [price.source, price.source_ref, price.revision != null ? "版本 " + price.revision : null].filter(Boolean).join(" · ") }));
      details.append(item);
    }
    return details;
  }

  function pricingDisplay(quote, force = false) {
    const conversion = window.TokenSQLConversion;
    const raw = conversion?.rawDiscount(quote) ?? quote.raw_discount_rate ?? quote.discount_rate;
    const result = node("div", { className: "price-display pricing-result" }, [
      node("span", { text: raw == null ? "源折扣未提供" : conversion ? "源折扣 " + conversion.discountFold(raw) + "折" : "源折扣暂不可显示" }),
      node("span", { className: "muted-cell", text: window.TokenSQLConditions?.scopeLabel(quote.discount_scope) || "适用范围待确认" }),
    ]);
    if (!conversion?.state.pricing && !force) return result;
    const context = window.TokenSQLConditions.context(quote);
    if (context.status === "multiple") {
      result.append(node("span", { className: "display-conversion", text: "按 " + context.matched.length + " 项官网价分别计算" }), officialDetails(quote, true));
    } else {
      result.append(pricingResult(conversion.computePricing(quote)));
    }
    if (context.status === "missing") {
      const lookup = node("button", { className: "text-button official-lookup", type: "button", text: "联网补官网价（新会话）" });
      lookup.addEventListener("click", () => window.TokenSQLReview?.lookupOfficial(quote)); result.append(lookup);
    }
    return result;
  }

  function officialCell(quote) {
    const conditions = window.TokenSQLConditions;
    if (!conditions) return node("td", { className: "number-cell", text: "正在读取官网价…" });
    const context = conditions.context(quote);
    const rows = context.matched.length ? context.matched : context.candidates;
    const cell = node("td", { className: "number-cell official-cell" });
    if (context.status === "matched" && rows.length === 1) {
      cell.append(priceDisplay(rows[0]), node("span", { className: "muted-cell", text: conditions.priceLabel(rows[0]) }));
    } else {
      cell.append(node("span", { className: "official-count", text: rows.length ? "已有 " + rows.length + " 项官网价" : context.status === "missing" ? "缺失官网价" : context.status === "needs_scope" ? "适用范围待确认" : "官网适用条件待确认" }));
      if (context.reason && context.status !== "multiple") cell.append(node("span", { className: "condition-description", text: context.reason }));
      if (rows.length) cell.append(officialDetails(quote));
    }
    if (context.status === "missing") {
      const button = node("button", { className: "text-button official-lookup", type: "button", text: "联网补官网价" });
      button.addEventListener("click", () => window.TokenSQLReview?.lookupOfficial(quote)); cell.append(button);
    }
    return cell;
  }

  function renderRows() {
    els.quoteRows.replaceChildren();
    for (const quote of state.items) {
      const row = node("tr");
      row.dataset.id = String(quote.id);
      row.classList.toggle("is-selected", state.selected.has(quote.id));
      const check = node("input", { type: "checkbox", ariaLabel: `选择 ${quote.model_name || quote.model_id}` });
      check.checked = state.selected.has(quote.id);
      check.addEventListener("change", () => {
        check.checked ? state.selected.set(quote.id, quote.revision) : state.selected.delete(quote.id);
        updateSelection(); renderRows();
      });
      const modelCell = node("td", { className: "model-cell" }, [
        node("span", { className: "model-name", text: quote.model_name || quote.model_id }),
        node("span", { className: "model-version", text: quote.model_id }),
      ]);
      const supplierCell = node("td", {}, [
        node("span", { text: valueOrDash(quote.supplier || quote.provider) }),
        node("span", { className: "muted-cell", text: quote.supplier_model_id || "未指定渠道 ID" }),
        node("div", { className: "tag-list" }, (quote.tags || []).map((tag) => node("span", { className: "metric-badge", text: tag }))),
      ]);
      const actions = node("td", { className: "action-cell" });
      for (const [text, label, handler] of [
        ["编辑", "编辑报价", () => openEditor(quote)],
        ["导出", "导出此模型", () => exportQuotes(new URLSearchParams({ format: $("exportFormat").value, model_id: quote.model_id }))],
        ["删除", "删除报价", () => deleteQuote(quote)],
      ]) {
        const button = node("button", { className: "text-button table-action", text, type: "button", ariaLabel: `${label} ${quote.model_id}` });
        button.addEventListener("click", (event) => { event.stopPropagation(); handler(); });
        actions.append(button);
      }
      row.append(node("td", { className: "check-cell" }, [check]), modelCell,
        tableCell(quote.vendor || quote.manufacturer), supplierCell,
        node("td", {}, [node("span", { className: "metric-badge", text: window.TokenSQLConditions?.metricLabel(quote.billing_metric || quote.billing_item || quote.metric) || valueOrDash(quote.billing_item) }),
          node("span", { className: "condition-description", text: window.TokenSQLConditions?.conditionText(quote.pricing_conditions) || "" })]),
        officialCell(quote), priceCell(quote),
        node("td", { className: "number-cell" }, [pricingDisplay(quote)]),
        node("td", {}, [node("span", { text: quote.source || "—" }), node("span", { className: "muted-cell", text: quote.source_ref || "" })]), actions);
      row.addEventListener("dblclick", (event) => {
        if (!event.target.closest("button,input")) openEditor(quote);
      });
      els.quoteRows.append(row);
    }
  }

  async function deleteQuote(quote) {
    if (state.deleting || !window.confirm(`永久删除 ${quote.model_id} 的这条报价？确认后直接清空，不能撤销；不会删除模型、官网价或归档文件。`)) return;
    state.deleting = true; updateSelection();
    try {
      await api(`/quotes/${quote.id}?expected_revision=${quote.revision}`, { method: "DELETE" });
      state.selected.delete(quote.id);
      toast("报价已永久删除");
      await loadQuotes({ quiet: true });
    } catch (error) {
      toast(error.status === 409 ? "报价已被修改，本次未删除。请刷新后重新选择。" : error.message, true);
      if ([404, 409].includes(error.status)) { state.selected.clear(); await loadQuotes({ quiet: true }); }
    } finally { state.deleting = false; updateSelection(); }
  }

  async function deleteSelected() {
    if (state.deleting || !state.selected.size) return;
    const items = [...state.selected].map(([id, expected_revision]) => ({ id, expected_revision }));
    if (!window.confirm(`永久删除所选 ${items.length} 条报价（包含其他页的选择）？确认后直接清空，不能撤销；不会删除模型、官网价或归档文件。`)) return;
    state.deleting = true; updateSelection();
    try {
      const result = await api("/quotes/bulk-delete", { method: "POST", body: JSON.stringify({ items, actor: "workspace-ui" }) });
      (result.deleted_ids || items.map((item) => item.id)).forEach((id) => state.selected.delete(id));
      state.offset = 0;
      toast(`已永久删除 ${result.deleted} 条报价`);
      await loadQuotes({ quiet: true });
    } catch (error) {
      toast(error.status === 409 ? "所选报价有版本变化，本批次全部未删除。已刷新，请重新选择。" : error.status === 404 ? "所选报价有记录已不存在，本批次全部未删除。已刷新，请重新选择。" : error.message, true);
      if ([404, 409].includes(error.status)) { state.selected.clear(); await loadQuotes({ quiet: true }); }
    } finally { state.deleting = false; updateSelection(); }
  }

  async function loadCatalog() {
    state.catalog = await api("/catalog");
    renderRows();
    return state.catalog;
  }

  async function loadPricingUnits() {
    state.pricingUnits = await api("/pricing-units");
    const select = formField("pricing_unit_id");
    if (select) {
      const active = select.value;
      const empty = node("option", { text: "未提供 / 不明确（可留空）" }); empty.value = "";
      select.replaceChildren(empty, ...state.pricingUnits.items.map((unit) => {
        const option = node("option", { text: unit.label || unit.currency + " / " + unit.scale });
        option.value = unit.id;
        return option;
      }));
      if ([...select.options].some((option) => option.value === active)) select.value = active;
    }
    return state.pricingUnits;
  }

  function updateSelection() {
    const count = state.selected.size;
    els.selectionBar.hidden = count === 0;
    els.selectedCount.textContent = String(count);
    $("deleteSelectedButton").disabled = state.deleting || !count;
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
    [els.manufacturer, els.provider, els.tag, els.metric, els.currency].forEach((select) => { select.value = ""; });
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

  async function exportQuotes(params, displayItems = null) {
    if (window.TokenSQLConversion) {
      await window.TokenSQLConversion.exportConverted(params, displayItems);
      return;
    }
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

  let pricingEditor = null;
  function openEditor(quote = null) {
    state.editing = quote;
    els.editorTitle.textContent = quote ? "编辑报价" : "新增报价";
    els.formError.textContent = "";
    const modelSelect = formField("model_id");
    $("modelIdentifiers").replaceChildren(...state.catalog.models.map((model) => {
      const option = node("option", { text: `${valueOrDash(model.vendor)} · ${model.model_name || model.model_id}` });
      option.value = model.model_id;
      return option;
    }));
    $("supplierNames").replaceChildren(...state.catalog.suppliers.map((supplier) => {
      const option = node("option"); option.value = supplier.name; return option;
    }));
    const legacyUnit = quote && !quote.pricing_unit_id ? state.pricingUnits.items.find((unit) =>
      unit.currency === quote.currency && unit.unit === quote.unit && String(unit.unit_quantity) === String(quote.unit_quantity)) : null;
    const values = { model_id: "", billing_item: "", supplier: "", tags: [], supplier_model_id: "",
      amount: "", discount_rate: "", pricing_unit_id: "", currency: "CNY",
      source: "", source_ref: "", conditions: {}, note: "", ...(quote || {}) };
    values.pricing_unit_id = quote?.pricing_unit_id || legacyUnit?.id || values.pricing_unit_id;
    const originalDiscount = quote?.raw_discount_rate ?? quote?.discount_rate ?? null;
    values.discount_rate = window.TokenSQLConversion?.discountFold(originalDiscount) ?? "";
    formField("discount_rate").dataset.originalRatio = window.TokenSQLConversion.discountRatio(originalDiscount) ?? "";
    formField("discount_rate").dataset.initialFold = values.discount_rate;
    formField("discount_rate").title = "以折为单位；8.0 代表八折，折数保留一位小数。";
    Object.keys(values).forEach((name) => {
      const field = formField(name);
      if (!field) return;
      field.value = name === "tags" ? (values.tags || []).join(", ") : name === "conditions" ? (values.conditions == null ? "" : JSON.stringify(values.conditions, null, 2)) : values[name] ?? "";
    });
    pricingEditor = window.TokenSQLConditions.createEditor(values, { quote: true, label: "供应商报价", onChange: () => calculateQuotePreview() });
    $("quoteConditionsEditor").replaceChildren(pricingEditor);
    updateComponentFields();
    els.editor.showModal();
    modelSelect.focus();
    calculateQuotePreview();
  }

  function updateComponentFields() {
    formField("amount").required = false;
    formField("discount_rate").required = false;
    const model = state.catalog.models.find((item) => item.model_id === formField("model_id").value);
    if (pricingEditor?.setModelId) pricingEditor.setModelId(formField("model_id").value);
    $("editorModelInfo").textContent = model ? `模型商：${valueOrDash(model.vendor)} · 别名：${(model.aliases || []).join(", ") || "无"}` : "输入新模型 ID 即可；确认报价时自动创建缺失模型，无需先审核实体。";
  }

  function quoteFormItem() {
    const item = { pricing_unit_id: formField("pricing_unit_id").value || null, currency: formField("currency").value || null };
    ["model_id", "billing_item", "supplier", "supplier_model_id", "source", "source_ref", "note"].forEach((name) => { item[name] = formField(name).value.trim(); });
    item.amount = formField("amount").value.trim() || null;
    const discountInput = formField("discount_rate");
    const fold = discountInput.value.trim();
    item.discount_rate = fold === discountInput.dataset.initialFold && discountInput.dataset.originalRatio !== ""
      ? window.TokenSQLConversion.discountRatio(discountInput.dataset.originalRatio) : window.TokenSQLConversion.ratioFromFold(fold);
    ["billing_item", "supplier", "supplier_model_id", "source", "source_ref", "note"].forEach((key) => { if (!item[key]) item[key] = null; });
    if (!item.supplier_model_id) item.supplier_model_id = null;
    item.tags = formField("tags").value.split(/[,，]/).map((value) => value.trim()).filter(Boolean);
    Object.assign(item, pricingEditor?.read() || {});
    item.conditions = JSON.parse(formField("conditions").value.trim() || "null");
    if (item.conditions !== null && (Array.isArray(item.conditions) || typeof item.conditions !== "object")) throw new Error("条件必须是 JSON 对象或留空");
    if (state.editing && !state.editing.pricing_unit_id && !item.pricing_unit_id) {
      ["unit", "unit_quantity"].forEach((key) => { if (state.editing[key] != null) item[key] = state.editing[key]; });
    }
    return item;
  }

  let previewTimer = null;
  let previewSerial = 0;
  async function calculateQuotePreview() {
    const serial = ++previewSerial;
    try {
      const item = quoteFormItem();
      if (!item.model_id) {
        $("quotePreview").textContent = "只需输入模型 ID；其余字段均可留空。币种默认人民币，计价尺度不猜测。";
        return;
      }
      const preview = state.editing
        ? await api("/catalog/preview", { method: "POST", body: JSON.stringify({ update: [{ id: state.editing.id, expected_revision: state.editing.revision, changes: item }] }) })
        : await api("/quotes/preview", { method: "POST", body: JSON.stringify({ items: [item] }) });
      if (serial !== previewSerial) return;
      const projected = (preview.items || preview.create || []).find((row) => state.editing ? row.id === state.editing.id : row.model_id === item.model_id) || item;
      const displayed = { ...projected, _officialCatalog: preview.catalog?.official_prices || state.catalog.official_prices };
      $("quotePreview").replaceChildren(node("strong", { text: "计算预览 · 原始输入独立保存" }), priceDisplay(displayed), pricingDisplay(displayed, true),
        node("small", { text: "折后价与反算折扣仅供显示及导出。" }));
    } catch (error) { if (serial === previewSerial) $("quotePreview").textContent = error.message; }
  }

  async function saveQuote() {
    if (!els.quoteForm.reportValidity()) return;
    let item;
    try { item = quoteFormItem(); } catch (error) { els.formError.textContent = error.message; return; }
    els.saveQuote.disabled = true;
    els.formError.textContent = "";
    try {
      if (state.editing) {
        await api(`/quotes/${state.editing.id}`, { method: "PATCH", body: JSON.stringify({ ...item, expected_revision: state.editing.revision }) });
      } else {
        await api("/quotes", { method: "POST", body: JSON.stringify({ items: [item] }) });
      }
      toast(state.editing ? "报价已更新" : "报价已新增");
      els.editor.close();
      await Promise.all([loadQuotes({ quiet: true }), loadCatalog()]);
    } catch (error) {
      els.formError.textContent = error.status === 409 ? "报价版本已变化，请刷新后重新编辑。" : error.message;
      if (error.status === 409) loadQuotes({ quiet: true });
    } finally { els.saveQuote.disabled = false; }
  }

  function fileKey(file) {
    return `${file.name}:${file.size}:${file.lastModified}`;
  }

  function addFiles(fileList) {
    if (Array.from(fileList || []).length && window.TokenSQLReview && !window.TokenSQLReview.canAttachFiles()) return;
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
      const labels = [["added", "新增"], ["updated", "更新"], ["deleted", "删除"], ["models", "模型"], ["official_prices", "官网计费项"], ["total", "合计"]];
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
    const label = node("span", { text: "正在分析文件并生成待审核方案…" });
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
      els.send.disabled = !status.available || state.agentBusy || state.sessionRestoring;
    } catch (_) {
      setAgentStatus(false, "未连接");
      els.send.disabled = true;
    }
  }

  async function sendPrompt() {
    if (window.TokenSQLReview) await window.TokenSQLReview.sendPrompt();
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
  [els.manufacturer, els.provider, els.tag, els.metric, els.currency].forEach((select) => {
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
    state.items.forEach((item) => els.selectAll.checked ? state.selected.set(item.id, item.revision) : state.selected.delete(item.id));
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
    exportQuotes(new URLSearchParams({ format: $("exportFormat").value, ids: [...state.selected.keys()].join(",") }));
  });
  $("deleteSelectedButton").addEventListener("click", deleteSelected);
  $("exportFilteredButton").addEventListener("click", () => {
    const params = currentFilters(false);
    params.set("format", $("exportFormat").value);
    exportQuotes(params);
  });

  els.quoteForm.addEventListener("submit", (event) => {
    event.preventDefault();
  });
  els.quoteForm.addEventListener("keydown", (event) => { if (event.key === "Enter" && event.target.tagName !== "TEXTAREA") event.preventDefault(); });
  els.saveQuote.addEventListener("click", saveQuote);
  els.quoteForm.addEventListener("input", (event) => {
    const unitField = formField("pricing_unit_id");
    const currencyField = formField("currency");
    const selectedUnit = state.pricingUnits.items.find((unit) => unit.id === unitField.value);
    if (event.target === unitField && selectedUnit) currencyField.value = selectedUnit.currency;
    if (event.target === currencyField && selectedUnit) {
      const matching = state.pricingUnits.items.find((unit) => unit.currency === currencyField.value && unit.scale === selectedUnit.scale);
      unitField.value = matching?.id || "";
    }
    updateComponentFields(); window.clearTimeout(previewTimer); previewTimer = window.setTimeout(calculateQuotePreview, 350);
  });
  document.querySelectorAll("[data-close-editor]").forEach((button) => {
    button.addEventListener("click", () => els.editor.close());
  });
  els.editor.addEventListener("click", (event) => {
    if (event.target === els.editor) els.editor.close();
  });

  function pickFiles() {
    if (window.TokenSQLReview && !window.TokenSQLReview.canAttachFiles()) return;
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

  window.TokenSQL = { state, els, $, node, api, toast, priceDisplay, pricingDisplay, officialDetails, renderRows, loadQuotes, loadCatalog, loadPricingUnits, appendMessage, appendProgress, renderToolActivity, checkAgent, addFiles, renderFileChips };
  Promise.allSettled([loadPricingUnits().catch((error) => toast(error.message, true)), loadQuotes(), loadCatalog().catch((error) => toast(error.message, true)), checkAgent()]);
  window.setInterval(checkAgent, 30000);
})();
