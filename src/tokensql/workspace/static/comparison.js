(() => {
  "use strict";
  const app = window.TokenSQL;
  const { $, node, state, els, api, toast } = app;
  const selected = { modelId: null, scenario: null, sort: "price_asc" };
  let currentCandidates = [];
  let currentRows = [];
  const quoteDate = (quote) => quote.effective_date || quote.quote_date || null;
  const metricOrder = ["input", "output", "cache_read", "cache_write", "cache_storage"];
  const stable = (value) => JSON.stringify(normalize(value));
  function normalize(value) {
    if (Array.isArray(value)) return value.map(normalize);
    if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort()
      .filter((key) => value[key] != null && value[key] !== "" && !(typeof value[key] === "object" && !Object.keys(value[key]).length))
      .map((key) => [key, normalize(value[key])]));
    return value;
  }
  function conditionsKey(value) { return stable({ ...value, tax_status: value?.tax_status || "unknown" }); }
  function exactCompare(left, right) { const result = left.n * right.d - right.n * left.d; return result < 0n ? -1 : result > 0n ? 1 : 0; }
  function supplierName(quote) { return state.catalog.suppliers.find((supplier) => supplier.supplier_id === quote.supplier_id)?.name || quote.supplier || "待补供应商"; }
  function schemeKey(quote) { return stable([quote.model_id, quote.supplier_id || quote.supplier || "quote:" + quote.id, quote.supplier_model_id || "", [...(quote.tags || [])].sort()]); }
  function conditionUnknown(conditions) { return Boolean(conditions?.legacy_text || conditions?.raw && Object.keys(conditions.raw).length); }
  function quoteMetric(quote) { return quote.billing_metric || quote.billing_item || null; }
  function metricLabel(metric) { return window.TokenSQLConditions.metricLabel(metric); }
  function conditionLabel(conditions) {
    const value = { ...conditions };
    if (value.tax_status) value.tax_status = { included: "含税", excluded: "不含税", unknown: "税费状态未知" }[value.tax_status] || value.tax_status;
    return window.TokenSQLConditions.conditionText(value);
  }
  function selectorMetric(quote) {
    const metrics = [...new Set((quote.discount_items || []).map((item) => item.billing_metric || item.billing_item).filter(Boolean))];
    return metrics.length === 1 ? metrics[0] : null;
  }
  function pricingTarget(source) {
    const conversion = window.TokenSQLConversion;
    const scale = conversion.state.scale || (source.dimension === "token" ? "mtoken" : source.scale);
    const currency = conversion.state.currency || "CNY";
    return state.pricingUnits.items.find((unit) => unit.currency === currency && unit.scale === scale) || null;
  }
  function oneCandidate(quote, official, match) {
    const conversion = window.TokenSQLConversion;
    const exact = conversion._exact;
    const metric = official ? quoteMetric(official) : quoteMetric(quote) || selectorMetric(quote);
    const conditions = { ...(official?.pricing_conditions || {}), ...(quote.pricing_conditions || {}) };
    const candidate = { quote, official, metric, conditions, supplier: supplierName(quote), reason: null,
      amount: null, amountValue: null, discount: null, estimated: false, minimum: false, discountMinimum: false, derived: false,
      conditionKey: conditionsKey(conditions), scheme: schemeKey(quote), scope: quote.discount_scope || "unknown", evidence: {} };
    const rule = official && quote.effective_discount_rules?.find((entry) => entry.official_price_id === official.official_price_id);
    if (rule && rule.quote_id != null && String(rule.quote_id) !== String(quote.id)) return null;
    if (rule?.reason === "conflicting_discount_rules" || rule?.reason === "conflicting_pricing_rules") candidate.reason = "同优先级定价规则冲突，等待确认";
    if (!metric && candidate.scope !== "model") candidate.reason ||= "待确认计费项 / 折扣范围";
    if (!quote.pricing_basis && (quote.issues || []).some((issue) => issue.code === "pricing_basis_required")) candidate.reason ||= "原金额与折扣矛盾，请先确认有效定价依据";
    if (conditionUnknown(conditions)) candidate.reason ||= "影响价格的原文条件尚未规范，等待确认";
    if (["ambiguous", "needs_scope"].includes(match.status)) candidate.reason ||= match.reason;
    if (quote.amount != null && quote.pricing_basis !== "discount_rate" && !quoteMetric(quote) && match.matched.length !== 1) candidate.reason ||= "原金额未明确计费项，不能当作各计费项统一价格";
    let price = quote;
    let pricing = null;
    if (official) pricing = conversion.computePricing(quote, official);
    candidate.pricing = pricing;
    if ((quote.amount == null || quote.pricing_basis === "discount_rate") && pricing?.derivedPrice) { price = pricing.derivedPrice; candidate.derived = true; }
    if (quote.pricing_basis !== "discount_rate" && quote.amount != null && pricing?.calculatedDiscount != null) candidate.discount = pricing.calculatedDiscount;
    else candidate.discount = pricing?.rawDiscount ?? conversion.rawDiscount(quote);
    if (price.amount != null) {
      const source = conversion.sourceUnit(price);
      if (!source) candidate.reason ||= "计价单位或币种未明确";
      else {
        const target = pricingTarget(source);
        let converted = target ? conversion.convertTo(price, target) : null;
        // With no chosen display currency, a missing FX must not prevent direct same-currency comparison.
        if (converted?.status === "unavailable" && !conversion.state.currency && !conversion.state.scale) {
          const localTarget = state.pricingUnits.items.find((unit) => unit.currency === source.currency && unit.scale === (source.dimension === "token" ? "mtoken" : source.scale));
          if (localTarget) {
            const local = conversion.convertTo(price, localTarget);
            if (local?.status === "converted") { candidate.conversionNote = converted.reason + "；保留同币种比较"; converted = local; }
          }
        }
        if (converted?.status === "converted") {
          candidate.amount = converted.amount; candidate.amountValue = { n: BigInt(converted.amountNumerator), d: BigInt(converted.amountDenominator) };
          candidate.unit = state.pricingUnits.items.find((unit) => unit.id === converted.pricingUnitId) || target || source;
          candidate.currency = converted.currency; candidate.unitLabel = converted.unitLabel; candidate.estimated = converted.estimated;
          candidate.evidence = converted.evidence; candidate.conversion = converted;
          if (converted.estimated) candidate.reason ||= "工程估算，仅供估算比较";
        } else {
          candidate.amount = String(price.amount); candidate.currency = source.currency; candidate.unitLabel = source.label.replace(/^[A-Z]+\s*\/\s*/, ""); candidate.unit = source;
          candidate.reason ||= converted?.reason || "显示单位未能换算";
        }
      }
    } else candidate.reason ||= pricing?.discountIssue || pricing?.reason || (candidate.discount != null ? "缺少适用官网金额，供应商金额未知" : "金额和折扣均未提供");
    if (!quote.supplier_id && !quote.supplier) candidate.reason ||= "供应商待补充";
    candidate.strict = Boolean(candidate.amountValue && !candidate.reason && !candidate.estimated);
    candidate.discountOnly = !candidate.amountValue && candidate.discount != null && !conditionUnknown(conditions) && !["ambiguous", "needs_scope"].includes(match.status) && (candidate.scope === "model" || candidate.scope === "items" || metric);
    candidate.groupKey = stable([metric || (candidate.scope === "model" ? "__model__" : "__unknown__"), candidate.conditionKey,
      candidate.amountValue ? [candidate.currency, candidate.unit?.dimension, candidate.unit?.scale] : candidate.discountOnly ? ["discount", candidate.scope, quote.discount_items || []] : ["unavailable"]]);
    candidate.key = quote.id + ":" + (official?.official_price_id || candidate.groupKey);
    return candidate;
  }
  function buildCandidates(items = state.allItems) {
    if (!window.TokenSQLConversion || !window.TokenSQLConditions) return [];
    let candidates = [];
    for (const quote of items) {
      const match = window.TokenSQLConditions.context(quote);
      const prices = ["matched", "multiple"].includes(match.status) ? match.matched : [];
      for (const official of prices.length ? prices : [null]) {
        const candidate = oneCandidate(quote, official, match);
        if (candidate) candidates.push(candidate);
      }
    }
    // A specific item is an exception to its scheme's model default, including an amount-only exception.
    candidates = candidates.filter((candidate) => candidate.scope !== "model" || !candidates.some((other) =>
      other !== candidate && other.scheme === candidate.scheme && other.scope !== "model" && other.metric === candidate.metric &&
      other.conditionKey === candidate.conditionKey && (other.quote.amount != null || other.discount != null)));
    const groups = new Map();
    for (const candidate of candidates) {
      const key = candidate.scheme + "\0" + candidate.groupKey;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(candidate);
    }
    for (const group of groups.values()) {
      if (group.length < 2) continue;
      const signatures = new Set(group.map((candidate) => stable([candidate.amountValue ? [String(candidate.amountValue.n), String(candidate.amountValue.d)] : null, candidate.discount])));
      if (signatures.size > 1) group.forEach((candidate) => { candidate.reason = "同供应商同方案存在同优先级冲突，等待确认"; candidate.strict = false; candidate.discountOnly = false; });
    }
    return candidates;
  }
  function markMinimum(rows) {
    rows.forEach((row) => { row.minimum = false; row.discountMinimum = false; });
    const exactRows = rows.filter((row) => row.strict);
    if (exactRows.length) {
      const minimum = exactRows.reduce((left, right) => exactCompare(left.amountValue, right.amountValue) <= 0 ? left : right);
      exactRows.forEach((row) => { row.minimum = exactCompare(row.amountValue, minimum.amountValue) === 0; });
    } else {
      const discounts = rows.filter((row) => row.discountOnly);
      if (discounts.length) {
        const parse = window.TokenSQLConversion._exact.parseDecimal;
        const minimum = discounts.reduce((left, right) => exactCompare(parse(left.discount), parse(right.discount)) <= 0 ? left : right);
        discounts.forEach((row) => { row.discountMinimum = exactCompare(parse(row.discount), parse(minimum.discount)) === 0; });
      }
    }
    return rows;
  }
  function sortCandidates(rows, order = selected.sort) {
    const descending = order.endsWith("_desc");
    const parse = window.TokenSQLConversion._exact.parseDecimal;
    return [...rows].sort((left, right) => {
      let result = 0;
      if (order.startsWith("date")) {
        const a = quoteDate(left.quote), b = quoteDate(right.quote);
        if (!a || !b) return a ? -1 : b ? 1 : left.supplier.localeCompare(right.supplier, "zh-CN");
        result = String(a).localeCompare(String(b));
      }
      else {
        const a = order.startsWith("discount") ? left.discount == null ? null : parse(left.discount) : left.amountValue;
        const b = order.startsWith("discount") ? right.discount == null ? null : parse(right.discount) : right.amountValue;
        // Estimated / unavailable values stay after strict rows in either price direction.
        const aRank = left.strict ? 0 : left.discountOnly && order.startsWith("discount") ? 1 : left.estimated ? 2 : 3;
        const bRank = right.strict ? 0 : right.discountOnly && order.startsWith("discount") ? 1 : right.estimated ? 2 : 3;
        if (aRank !== bRank) return aRank - bRank;
        if (!a || !b) return a ? -1 : b ? 1 : left.supplier.localeCompare(right.supplier, "zh-CN");
        result = exactCompare(a, b);
      }
      return (descending ? -result : result) || left.supplier.localeCompare(right.supplier, "zh-CN") || Number(left.quote.id) - Number(right.quote.id);
    });
  }
  function button(text, handler, className = "text-button") { const result = node("button", { type: "button", text, className }); result.addEventListener("click", handler); return result; }
  function compactPrice(candidate) {
    const cell = node("div", { className: "compact-price" });
    cell.append(node("strong", { text: candidate.amount == null ? "金额未知" : [candidate.currency, candidate.amount].filter(Boolean).join(" ") }));
    if (candidate.amount != null) cell.append(node("small", { text: "/ " + (candidate.unitLabel || "单位未明确") }));
    if (candidate.minimum) cell.append(node("span", { className: "minimum-badge", text: "最低" }));
    if (candidate.discountMinimum) cell.append(node("span", { className: "minimum-badge", text: "最低折扣 · 金额未知" }));
    if (candidate.estimated) cell.append(node("span", { className: "estimated", text: "估算比较" }));
    return cell;
  }
  function overviewMetric(rows, metric) {
    const matches = rows.filter((candidate) => candidate.metric === metric);
    if (!matches.length) return node("td", { className: "muted-cell", text: "暂无报价" });
    const groups = new Map();
    matches.forEach((candidate) => { if (!groups.has(candidate.groupKey)) groups.set(candidate.groupKey, []); groups.get(candidate.groupKey).push(candidate); });
    const cell = node("td", { className: "overview-price" });
    let index = 0;
    for (const group of groups.values()) {
      markMinimum(group);
      const minimum = group.filter((candidate) => candidate.minimum || candidate.discountMinimum);
      if (index < 2) {
        const summary = node("div", { className: "overview-scenario", title: conditionLabel(group[0].conditions) });
        if (minimum.length) {
          summary.append(compactPrice(minimum[0]));
          if (minimum[0].discountMinimum) summary.append(node("strong", { text: window.TokenSQLConversion.discountFold(minimum[0].discount) + "折" }));
          summary.append(node("span", { className: "overview-suppliers", text: [...new Set(minimum.map((candidate) => candidate.supplier))].join("、") }));
        } else summary.append(node("span", { className: "muted-cell", text: "待补条件 / 金额" }));
        if (groups.size > 1 || Object.keys(normalize(group[0].conditions)).length) summary.append(node("small", { className: "condition-summary", text: conditionLabel(group[0].conditions) }));
        cell.append(summary);
      }
      index += 1;
    }
    if (groups.size > 2) cell.append(node("small", { text: "共 " + groups.size + " 个独立条件场景，进入模型查看" }));
    return cell;
  }
  function header(labels, selectable = false) {
    const row = node("tr");
    if (selectable) { els.selectAll.hidden = false; row.append(node("th", { className: "check-cell" }, [els.selectAll])); }
    else els.selectAll.hidden = true;
    labels.forEach((label) => row.append(node("th", { text: label })));
    $("comparisonHead").replaceChildren(row);
  }
  function chooseModel(modelId, scenario = null) { selected.modelId = modelId; selected.scenario = scenario; state.offset = 0; render(); }
  function renderOverview(candidates) {
    header(["model_id", "输入最低供应商价", "输出最低供应商价", "其他计费项", "操作"]);
    const ids = [...new Set(candidates.map((candidate) => candidate.quote.model_id))].sort();
    state.total = ids.length;
    const page = ids.slice(state.offset, state.offset + state.limit);
    state.items = []; els.quoteRows.replaceChildren();
    for (const modelId of page) {
      const rows = candidates.filter((candidate) => candidate.quote.model_id === modelId);
      const other = [...new Set(rows.map((candidate) => candidate.metric).filter((metric) => !["input", "output"].includes(metric)))];
      const row = node("tr", { className: "model-overview-row" }); row.dataset.modelId = modelId;
      row.append(node("td", { className: "model-cell" }, [button(modelId, () => chooseModel(modelId), "model-link")]), overviewMetric(rows, "input"), overviewMetric(rows, "output"),
        node("td", { className: "other-metrics", text: other.map((metric) => metric ? metricLabel(metric) : rows.some((candidate) => candidate.scope === "model") ? "全部计费项" : "待确认范围").join("、") || "—" }), node("td", {}, [button("查看排行", () => chooseModel(modelId))]));
      els.quoteRows.append(row);
    }
    currentRows = candidates;
    app.updatePagination(); app.updateSelection(); els.resultCount.textContent = `${ids.length} 个模型 · ${state.allItems.length} 条原始报价`;
  }
  function scenarioLabel(candidate) {
    const metric = candidate.metric ? metricLabel(candidate.metric) : candidate.scope === "model" ? "全部计费项" : "待确认范围";
    const unit = candidate.amountValue ? `${candidate.currency} / ${candidate.unitLabel}` : candidate.discountOnly ? "折扣比较 · 金额未知" : "不可计算";
    return metric + " · " + conditionLabel(candidate.conditions) + " · " + unit;
  }
  function showDetails(candidate = null) {
    const target = $("officialDialogContent"); target.replaceChildren();
    $("officialDialogTitle").textContent = candidate ? candidate.quote.model_id + " · " + candidate.supplier : selected.modelId + " · 官网计费明细";
    if (candidate) {
      const quote = candidate.quote;
      target.append(node("div", { className: "detail-payable" }, [compactPrice(candidate), node("span", { text: "折扣 " + (candidate.discount == null ? "无法计算" : window.TokenSQLConversion.discountFold(candidate.discount) + "折") })]),
        node("p", { text: "计费项：" + (candidate.metric ? metricLabel(candidate.metric) : window.TokenSQLConditions.scopeLabel(quote.discount_scope)) }),
        node("p", { text: conditionLabel(candidate.conditions) }), node("p", { text: "报价范围：" + window.TokenSQLConditions.scopeLabel(quote.discount_scope) }),
        node("p", { className: "muted-cell", text: candidate.derived ? "供应商价按明确官网项及原折扣计算；原始报价未被改写。" : "供应商价采用原始金额；折扣按可匹配官网价反算。" }));
      if (candidate.reason) target.append(node("p", { className: "validation-issue", text: candidate.reason }));
      const detail = { quote_id: quote.id, revision: quote.revision, supplier_id: quote.supplier_id, supplier_model_id: quote.supplier_model_id,
        source_scope: quote.source_scope, source_entry_id: quote.source_entry_id, tags: quote.tags, original_amount: quote.amount,
        original_discount_rate: quote.discount_rate, pricing_unit_id: quote.pricing_unit_id, currency: quote.currency,
        source: quote.source, source_ref: quote.source_ref, conditions: quote.conditions, note: quote.note,
        official_match: quote.official_match, effective_discount_rule: candidate.pricing?.effectiveRule, calculation: candidate.evidence };
      target.append(node("details", {}, [node("summary", { text: "原始输入、方案及计算依据" }), node("pre", { text: JSON.stringify(detail, null, 2) })]));
    }
    const prices = candidate?.official ? [candidate.official] : (state.catalog.official_prices || []).filter((price) => price.model_id === selected.modelId);
    for (const price of prices) target.append(node("section", { className: "official-detail-item" }, [node("strong", { text: window.TokenSQLConditions.priceLabel(price) }),
      app.priceDisplay(price), node("small", { text: [price.source, price.source_ref, "版本 " + (price.revision ?? "未知")].filter(Boolean).join(" · ") }),
      node("details", {}, [node("summary", { text: "完整官网记录" }), node("pre", { text: JSON.stringify(price, null, 2) })])]));
    if (!prices.length) target.append(node("p", { text: "当前模型尚无官网参考价。已明确的供应商金额仍可在同条件同单位下比较。" }), button("联网补官网价（独立会话）", () => { $("officialDialog").close(); window.TokenSQLReview?.lookupOfficial(candidate?.quote || { model_id: selected.modelId }); }));
    $("officialDialog").showModal();
  }
  function renderRanking(candidates) {
    const all = candidates.filter((candidate) => candidate.quote.model_id === selected.modelId);
    const metric = els.metric.value;
    const relevant = all.filter((candidate) => !metric || candidate.metric === metric || !candidate.metric);
    const groups = new Map(); relevant.forEach((candidate) => { if (!groups.has(candidate.groupKey)) groups.set(candidate.groupKey, candidate); });
    const ordered = [...groups.values()].sort((left, right) => (metricOrder.indexOf(left.metric) < 0 ? 99 : metricOrder.indexOf(left.metric)) - (metricOrder.indexOf(right.metric) < 0 ? 99 : metricOrder.indexOf(right.metric)) || scenarioLabel(left).localeCompare(scenarioLabel(right), "zh-CN"));
    if (!groups.has(selected.scenario)) selected.scenario = ordered.find((candidate) => candidate.strict)?.groupKey || ordered[0]?.groupKey || null;
    const select = $("scenarioFilter"); select.replaceChildren(...ordered.map((candidate) => { const option = node("option", { text: scenarioLabel(candidate) }); option.value = candidate.groupKey; return option; })); select.value = selected.scenario || "";
    const scene = groups.get(selected.scenario);
    const rows = relevant.filter((candidate) => candidate.groupKey === selected.scenario || !candidate.strict && !candidate.discountOnly && (!scene || candidate.metric === scene.metric || !candidate.metric));
    const unique = [...new Map(rows.map((candidate) => [candidate.key, candidate])).values()];
    markMinimum(unique.filter((candidate) => candidate.groupKey === selected.scenario));
    currentRows = sortCandidates(unique); state.total = currentRows.length;
    if (state.offset >= state.total) state.offset = 0;
    const page = currentRows.slice(state.offset, state.offset + state.limit);
    state.items = page.map((candidate) => candidate.quote);
    header(["供应商", "供应商价", "折扣", "适用条件摘要", "报价日期", "操作"], true);
    els.quoteRows.replaceChildren();
    for (const candidate of page) {
      const quote = candidate.quote;
      const row = node("tr", { className: candidate.strict ? "comparable-row" : candidate.estimated ? "estimated-row" : "unavailable-row" }); row.dataset.id = String(quote.id); row.dataset.comparable = String(candidate.strict);
      const check = node("input", { type: "checkbox", ariaLabel: "选择 " + candidate.supplier + " 的报价" }); check.checked = state.selected.has(quote.id);
      check.addEventListener("change", () => { check.checked ? state.selected.set(quote.id, quote.revision) : state.selected.delete(quote.id); app.updateSelection(); render(); });
      const conditions = node("td", { className: "rank-conditions" }, [node("span", { className: "condition-summary", text: candidate.scope === "model" ? "全部计费项 · " + conditionLabel(candidate.conditions) : conditionLabel(candidate.conditions) })]);
      if (candidate.reason) conditions.append(node("small", { className: "unavailable-reason", text: candidate.reason }));
      if (candidate.conversionNote) conditions.title = candidate.conversionNote;
      row.append(node("td", { className: "check-cell" }, [check]), node("td", { className: "supplier-name", text: candidate.supplier }), node("td", { className: "rank-price" }, [compactPrice(candidate)]),
        node("td", { className: "rank-discount", text: candidate.discount == null ? "—" : window.TokenSQLConversion.discountFold(candidate.discount) + "折" }), conditions,
        node("td", { className: "quote-date", text: quoteDate(quote) ? app.formatDate(quoteDate(quote)) : quote.updated_at ? "更新 " + app.formatDate(quote.updated_at) : "日期未知" }),
        node("td", { className: "rank-actions" }, [button("详情", () => showDetails(candidate)), button("编辑", () => app.openEditor(quote)), button("删除", () => app.deleteQuote(quote))]));
      els.quoteRows.append(row);
    }
    const summary = $("officialSummary"); summary.replaceChildren();
    if (scene?.official) summary.append(node("span", { text: "官网参考：" + [scene.official.currency, scene.official.amount ?? "金额未知"].filter(Boolean).join(" ") + " / " + (window.TokenSQLConversion.sourceUnit(scene.official)?.label.replace(/^[A-Z]+\s*\/\s*/, "") || "单位待确认") }));
    else summary.append(node("span", { text: "官网参考：当前场景无匹配金额" }));
    summary.append(button("官网完整明细", () => showDetails()));
    if (scene) summary.append(node("small", { text: scenarioLabel(scene) }));
    const count = currentRows.filter((candidate) => candidate.strict).length;
    const minima = currentRows.filter((candidate) => candidate.minimum).length;
    $("comparisonSubtitle").textContent = `${count} 条可比报价${minima > 1 ? " · " + minima + " 条并列最低" : ""} · 其他方案按条件分别查看`;
    app.updateSelection(); app.updatePagination();
    els.resultCount.textContent = `${state.total} 个报价候选 · ${new Set(currentRows.map((candidate) => candidate.quote.id)).size} 条原始报价 · 已先排序再分页`;
  }
  function render() {
    if (!window.TokenSQLConversion || !window.TokenSQLConditions) return;
    currentCandidates = buildCandidates();
    const metricValue = els.metric.value;
    const metrics = [...new Set(currentCandidates.map((candidate) => candidate.metric).filter(Boolean))].sort((left, right) => metricOrder.indexOf(left) - metricOrder.indexOf(right));
    const allMetrics = node("option", { text: "全部计费项" }); allMetrics.value = "";
    els.metric.replaceChildren(allMetrics, ...metrics.map((metric) => { const option = node("option", { text: metricLabel(metric) }); option.value = metric; return option; }));
    if (metrics.includes(metricValue)) els.metric.value = metricValue;
    $("backToOverviewButton").hidden = !selected.modelId; $("scenarioToolbar").hidden = !selected.modelId; $("officialSummary").hidden = !selected.modelId;
    $("comparisonTitle").textContent = selected.modelId || "模型概览";
    if (selected.modelId) renderRanking(currentCandidates);
    else { $("comparisonSubtitle").textContent = "各计费项与条件分别标明最低价及供应商"; renderOverview(currentCandidates.filter((candidate) => !els.metric.value || candidate.metric === els.metric.value)); }
    els.tableState.hidden = currentRows.length > 0;
  }
  function preview(quote) {
    const target = node("div", { className: "single-price-preview" });
    const candidates = buildCandidates([quote]);
    candidates.forEach((candidate) => { const row = node("section", { className: "review-computed-item" }, [node("strong", { text: (candidate.metric ? metricLabel(candidate.metric) : candidate.scope === "model" ? "全部计费项" : "待确认范围") + " · " + conditionLabel(candidate.conditions) }), compactPrice(candidate), node("span", { text: "折扣 " + (candidate.discount == null ? "无法计算" : window.TokenSQLConversion.discountFold(candidate.discount) + "折") })]); if (candidate.reason) row.append(node("small", { className: "unavailable-reason", text: candidate.reason })); target.append(row); });
    return target;
  }
  function exportRows(params = new URLSearchParams()) {
    const ids = new Set((params.get("ids") || "").split(",").filter(Boolean));
    const model = params.get("model_id");
    let rows = selected.modelId ? currentRows : sortCandidates(currentCandidates.filter((candidate) => !els.metric.value || candidate.metric === els.metric.value));
    if (model) rows = rows.filter((candidate) => candidate.quote.model_id === model);
    if (ids.size) rows = sortCandidates(currentCandidates.filter((candidate) => ids.has(String(candidate.quote.id))));
    return rows.map((candidate, index) => ({ rank: index + 1, quote_id: candidate.quote.id, revision: candidate.quote.revision,
      model_id: candidate.quote.model_id, supplier: candidate.supplier, billing_metric: candidate.metric,
      pricing_conditions: candidate.conditions, supplier_price: candidate.amount, display_currency: candidate.currency,
      display_unit: candidate.unitLabel, discount_fold: candidate.discount == null ? null : window.TokenSQLConversion.discountFold(candidate.discount),
      strict_comparable: candidate.strict, minimum: candidate.minimum, lowest_discount_only: candidate.discountMinimum,
      estimated: candidate.estimated, unavailable_reason: candidate.reason, price_basis: candidate.derived ? "official_discount" : "original_amount",
      original_amount: candidate.quote.amount, original_discount_rate: candidate.quote.discount_rate,
      original_currency: candidate.quote.currency, original_pricing_unit_id: candidate.quote.pricing_unit_id,
      official_price_id: candidate.official?.official_price_id, source: candidate.quote.source, source_ref: candidate.quote.source_ref,
      source_scope: candidate.quote.source_scope, supplier_model_id: candidate.quote.supplier_model_id, tags: candidate.quote.tags,
      effective_date: candidate.quote.effective_date || null, updated_at: candidate.quote.updated_at || null,
      amount_numerator: candidate.amountValue ? String(candidate.amountValue.n) : null, amount_denominator: candidate.amountValue ? String(candidate.amountValue.d) : null,
      calculation_evidence: candidate.evidence }));
  }
  async function exportCurrent(params) {
    try {
      const rows = exportRows(params); const format = params.get("format") || "xlsx";
      const labels = { rank: "序号", quote_id: "原报价ID", model_id: "model_id", supplier: "供应商", supplier_price: "供应商价", discount_fold: "折扣（折）", billing_metric: "计费项", display_currency: "显示币种", display_unit: "显示单位", minimum: "当前场景最低", strict_comparable: "严格可比", estimated: "估算", unavailable_reason: "不可比原因" };
      const columns = Object.keys(rows[0] || { model_id: null, supplier: null, supplier_price: null }).map((key) => ({ key, label: labels[key] || key }));
      const response = await fetch("/api/workspace/exports/view", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ format, filename: "tokensql-comparison." + format, columns, rows }) });
      if (!response.ok) throw new Error("比价导出失败（" + response.status + "）");
      const href = URL.createObjectURL(await response.blob()); const anchor = node("a"); anchor.href = href; anchor.download = "tokensql-comparison." + format; anchor.click(); URL.revokeObjectURL(href);
      toast("已按当前条件、单位和全量排序导出 " + rows.length + " 个候选");
    } catch (error) { toast(error.message, true); }
  }
  $("backToOverviewButton").addEventListener("click", () => { selected.modelId = null; selected.scenario = null; state.offset = 0; render(); });
  $("scenarioFilter").addEventListener("change", (event) => { selected.scenario = event.target.value; state.offset = 0; render(); });
  $("rankingSort").addEventListener("change", (event) => { selected.sort = event.target.value; state.offset = 0; render(); });
  $("closeOfficialButton").addEventListener("click", () => $("officialDialog").close());
  window.TokenSQLComparison = { render, preview, chooseModel, buildCandidates, sortCandidates, exportRows, exportCurrent, selected, get rows() { return currentRows; }, _exactCompare: exactCompare, _conditionsKey: conditionsKey };
  render();
})();
