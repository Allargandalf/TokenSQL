(() => {
  "use strict";
  const { state, node } = window.TokenSQL;
  const metrics = [["input", "输入"], ["output", "输出"], ["cache_read", "缓存读取"],
    ["cache_write", "缓存写入"], ["cache_storage", "缓存存储"]];
  const labels = { market: "市场 / 区域", platform: "平台", input_modality: "输入模态", output_modality: "输出模态",
    input_tokens: "输入上下文 token", output_tokens: "输出 token", mode: "请求模式", time_band: "忙闲时段",
    cache_ttl_seconds: "缓存有效期（秒）", resolution: "分辨率", with_audio: "包含音频", reference_video: "参考视频",
    customer_tier: "客户档位", legacy_text: "原文条件", raw: "其他原始条件" };
  const selected = new Map();
  const expanded = new Map();
  const scopeLabel = (scope) => ({ model: "整模型全部计费项", items: "指定计费项", unknown: "适用范围待确认" }[scope] || "适用范围待确认");
  const metricLabel = (value) => metrics.find(([key]) => key === value)?.[1] || value || "计费项未明确";
  function conditionText(conditions = {}) {
    return Object.entries(conditions || {}).filter(([, value]) => value !== null && value !== undefined && value !== "" && (typeof value !== "object" || Object.keys(value).length))
      .map(([key, value]) => {
        if (["input_tokens", "output_tokens"].includes(key) && typeof value === "object") {
          const left = value.min == null ? "不限下界" : (value.min_inclusive === false ? "> " : "≥ ") + value.min;
          const right = value.max == null ? "不限上界" : (value.max_inclusive === false ? "< " : "≤ ") + value.max;
          return (labels[key] || key) + "：" + left + "，" + right;
        }
        return (labels[key] || key) + "：" + (typeof value === "boolean" ? (value ? "是" : "否") : typeof value === "object" ? JSON.stringify(value) : value);
      }).join(" · ") || "未注明额外条件";
  }
  function priceLabel(price) {
    const metric = metricLabel(price.billing_metric || price.billing_item);
    const original = price.billing_item && price.billing_item !== price.billing_metric ? "（原计费项：" + price.billing_item + "）" : "";
    return metric + original + " · " + conditionText(price.pricing_conditions);
  }
  function same(left, right) { return JSON.stringify(left ?? null) === JSON.stringify(right ?? null); }
  function matchesSelector(price, selector) {
    if (selector.official_price_id && price.official_price_id !== selector.official_price_id) return false;
    if (selector.billing_metric && price.billing_metric !== selector.billing_metric) return false;
    if (selector.billing_item && price.billing_item !== selector.billing_item) return false;
    return Object.entries(selector.pricing_conditions || {}).every(([key, value]) => same(price.pricing_conditions?.[key], value));
  }
  function context(quote) {
    const catalog = quote._officialCatalog || state.catalog.official_prices || [];
    const candidates = catalog.filter((price) => price.model_id === quote.model_id);
    if (quote.official_match) {
      const match = quote.official_match;
      const byId = new Map([...candidates, ...(quote.official_prices || []), ...(quote.official_price ? [quote.official_price] : [])]
        .map((price) => [price.official_price_id, price]));
      const reasons = { discount_scope_unknown: "已有官网价，请明确源折扣适用于整模型还是指定计费项。",
        discount_items_required: "请指定源折扣适用的计费项。", conflicting_official_scenarios: "同一条件存在多条官网价，请核对来源和条件。",
        conditions_need_evidence: "适用条件尚不明确，请补充条件和依据后计算。", multiple_official_scenarios: "已有多条条件价，请明确适用条件。",
        legacy_official_metric_unclassified: "已有官网价，但原计费项尚未分类，请先审核计费项。", no_official_prices: "缺失官网价",
        no_matching_official_scenario: "当前计费项 / 条件尚无匹配官网价", explicit_model_scope: "整模型规则，官网各计费项分别适用",
        explicit_items_scope: "仅对明确指定的计费项适用", explicit_item_match: "已匹配明确计费项" };
      return { ...match, reason: reasons[match.reason] || "请核对适用计费项和条件。", matched: quote.official_prices || (quote.official_price ? [quote.official_price] : []),
        candidates: (match.candidate_ids?.length ? match.candidate_ids.map((id) => byId.get(id)).filter(Boolean) : match.status === "needs_scope" ? candidates : []) };
    }
    if (quote.official_price) return { status: "matched", reason: "", matched: [quote.official_price], candidates: [quote.official_price] };
    if (!candidates.length) return { status: "missing", reason: "缺失官网价", matched: [], candidates: [] };
    let applicable = candidates;
    if (quote.discount_scope === "items") applicable = candidates.filter((price) => (quote.discount_items || []).some((item) => matchesSelector(price, item)));
    else if (quote.discount_scope !== "model") {
      if (!quote.billing_metric && !quote.billing_item) return { status: "needs_scope", reason: "已有官网价，请先明确折扣适用于整模型还是指定计费项。", matched: [], candidates };
      applicable = candidates.filter((price) => quote.billing_metric ? price.billing_metric === quote.billing_metric : (price.billing_item ?? null) === quote.billing_item);
    }
    applicable = applicable.filter((price) => matchesSelector(price, { pricing_conditions: quote.pricing_conditions || {} }));
    if (!applicable.length) return { status: "ambiguous", reason: "已有官网价，适用范围或条件需进一步校验。", matched: [], candidates };
    const keys = applicable.map((price) => (price.billing_metric || price.billing_item || "") + "\0" + JSON.stringify(price.pricing_conditions || {}));
    const dimensions = new Set(applicable.map((price) => price.billing_metric || price.billing_item || ""));
    const ambiguous = new Set(keys).size !== keys.length || (quote.discount_scope !== "model" && quote.discount_scope !== "items" && applicable.length > dimensions.size);
    if (ambiguous) return { status: "ambiguous", reason: "已有多条条件价，请补充适用条件后再计算。", matched: [], candidates: applicable };
    return { status: applicable.length > 1 ? "multiple" : "matched", reason: "", matched: applicable, candidates: applicable };
  }
  function displayRows(quote) {
    const result = context(quote);
    const rows = result.matched.length ? result.matched : result.candidates;
    const id = selected.get(String(quote.id));
    return id ? rows.filter((row) => row.official_price_id === id) : rows;
  }
  function createEditor(data, { quote = false, label = "定价", onChange = () => {} } = {}) {
    const root = node("details", { className: "conditions-editor" }, [node("summary", { text: quote ? "计费项、折扣范围与适用条件" : "官网计费项与适用条件" })]);
    const grid = node("div", { className: "condition-grid" });
    root.append(grid);
    const controls = new Map();
    const typed = structuredClone(data.pricing_conditions || {});
    let advanced;
    function notify() { if (advanced) advanced.value = JSON.stringify(typed, null, 2); onChange(); }
    function field(key, title, value, options = null, parent = grid) {
      const input = options ? node("select") : node("input", { type: "text" });
      input.dataset.conditionField = key;
      input.setAttribute("aria-label", label + " " + title);
      if (options) {
        options.forEach(([val, text]) => { const option = node("option", { text }); option.value = val; input.append(option); });
        if (value != null && !options.some(([val]) => val === String(value))) {
          const option = node("option", { text: String(value) }); option.value = String(value); input.append(option);
        }
      }
      input.value = value == null ? "" : String(value);
      parent.append(node("label", {}, [node("span", { text: title }), input]));
      controls.set(key, input);
      return input;
    }
    const knownMetric = !data.billing_metric || metrics.some(([key]) => key === data.billing_metric);
    const metric = field("billing_metric", "标准计费项", knownMetric ? data.billing_metric : "__custom__", [["", "未知 / 未明确"], ...metrics, ["__custom__", "其他计费项…"]]);
    const customMetric = field("billing_metric_custom", "其他计费项名称", knownMetric ? "" : data.billing_metric);
    customMetric.parentElement.hidden = knownMetric;
    metric.addEventListener("input", () => { customMetric.parentElement.hidden = metric.value !== "__custom__"; data.billing_metric = metric.value === "__custom__" ? customMetric.value.trim() || null : metric.value || null; onChange(); });
    customMetric.addEventListener("input", () => { data.billing_metric = customMetric.value.trim() || null; onChange(); });
    if (quote) {
      const scope = field("discount_scope", "源折扣适用范围", data.discount_scope || "unknown",
        [["unknown", "未知 / 待确认"], ["model", "整模型全部计费项"], ["items", "仅指定计费项"]]);
      const items = node("div", { className: "discount-items" });
      const itemJson = node("textarea"); itemJson.rows = 3;
      itemJson.setAttribute("aria-label", label + " 指定项 JSON");
      itemJson.value = JSON.stringify(data.discount_items || [], null, 2);
      const choices = node("div", { className: "discount-item-choices" });
      function renderChoices() {
        choices.replaceChildren();
        (state.catalog.official_prices || []).filter((price) => price.model_id === data.model_id).forEach((price) => {
          const check = node("input", { type: "checkbox" });
          check.checked = (data.discount_items || []).some((item) => item.official_price_id === price.official_price_id);
          check.addEventListener("change", () => {
            data.discount_items = (data.discount_items || []).filter((item) => item.official_price_id !== price.official_price_id);
            if (check.checked) data.discount_items.push({ official_price_id: price.official_price_id, pricing_conditions: {} });
            itemJson.value = JSON.stringify(data.discount_items, null, 2); onChange();
          });
          choices.append(node("label", {}, [check, node("span", { text: priceLabel(price) })]));
        });
      }
      root.setModelId = (modelId) => { if (data.model_id !== modelId) { data.model_id = modelId; renderChoices(); } };
      const advancedItems = node("details", {}, [node("summary", { text: "指定项补充（JSON）" }),
        node("p", { text: '可用标准计费项和条件选定范围。例如 [{"billing_metric":"input","pricing_conditions":{"market":"cn"}}]；未知范围请在上方保留“未知”。' }), itemJson]);
      items.append(node("p", { text: "勾选明确适用的官网计费项；每个条件价独立保留。" }), choices, advancedItems);
      grid.append(items);
      function updateScope() { data.discount_scope = scope.value; items.hidden = scope.value !== "items"; renderChoices(); onChange(); }
      scope.addEventListener("input", updateScope);
      items.hidden = scope.value !== "items"; renderChoices();
      itemJson.addEventListener("input", () => {
        try { const value = JSON.parse(itemJson.value || "[]"); if (!Array.isArray(value)) throw Error("指定项必须是数组"); data.discount_items = value; itemJson.setCustomValidity(""); renderChoices(); onChange(); }
        catch (error) { itemJson.setCustomValidity(error.message); }
      });
    }
    const extra = node("details", { className: "condition-extra" }, [node("summary", { text: "地区、忙闲、上下文、缓存等条件" })]);
    const extraGrid = node("div", { className: "condition-grid" }); extra.append(extraGrid); root.append(extra);
    for (const key of ["market", "platform", "input_modality", "output_modality", "mode", "time_band", "cache_ttl_seconds", "resolution", "customer_tier", "legacy_text"]) {
      const options = key === "time_band" ? [["", "未知 / 未注明"], ["busy", "忙时"], ["idle", "闲时"]] : null;
      const input = field(key, labels[key], typed[key], options, extraGrid);
      input.placeholder = "未注明则留空";
      if (key === "cache_ttl_seconds") input.inputMode = "numeric";
      input.addEventListener("input", () => {
        input.setCustomValidity("");
        if (!input.value.trim()) delete typed[key];
        else if (key === "cache_ttl_seconds") {
          if (!/^\d+$/.test(input.value) || !Number.isSafeInteger(Number(input.value))) { input.setCustomValidity("请输入非负安全整数秒数"); return; }
          typed[key] = Number(input.value);
        } else typed[key] = input.value.trim();
        data.pricing_conditions = structuredClone(typed); notify();
      });
    }
    for (const key of ["with_audio", "reference_video"]) {
      const input = field(key, labels[key], typed[key], [["", "未知 / 未注明"], ["true", "是"], ["false", "否"]], extraGrid);
      input.addEventListener("input", () => { if (!input.value) delete typed[key]; else typed[key] = input.value === "true"; data.pricing_conditions = structuredClone(typed); notify(); });
    }
    for (const key of ["input_tokens", "output_tokens"]) {
      const range = node("fieldset", { className: "condition-range" }, [node("legend", { text: labels[key] })]);
      extraGrid.append(range);
      for (const bound of ["min", "max"]) {
        const input = field(key + "." + bound, bound === "min" ? "下界 token 数" : "上界 token 数", typed[key]?.[bound], null, range);
        input.inputMode = "numeric";
        const inclusive = field(key + "." + bound + "_inclusive", bound === "min" ? "下界是否包含" : "上界是否包含",
          typed[key]?.[bound + "_inclusive"] !== false, [["true", "包含"], ["false", "不包含"]], range);
        function updateRange() {
          input.setCustomValidity("");
          if (input.value && (!/^\d+$/.test(input.value) || !Number.isSafeInteger(Number(input.value)))) { input.setCustomValidity("请输入非负安全整数 token 数"); return; }
          const value = { ...(typed[key] || {}) };
          if (input.value) value[bound] = Number(input.value); else delete value[bound];
          value[bound + "_inclusive"] = inclusive.value === "true";
          if (value.min == null && value.max == null) delete typed[key]; else typed[key] = value;
          data.pricing_conditions = structuredClone(typed); notify();
        }
        input.addEventListener("input", updateRange); inclusive.addEventListener("input", updateRange);
      }
    }
    advanced = node("textarea"); advanced.rows = 5; advanced.value = JSON.stringify(typed, null, 2);
    advanced.setAttribute("aria-label", label + " 结构化条件 JSON");
    advanced.addEventListener("input", () => {
      try {
        const value = JSON.parse(advanced.value || "{}");
        if (!value || Array.isArray(value) || typeof value !== "object") throw Error("结构化条件必须是 JSON 对象");
        Object.keys(typed).forEach((key) => delete typed[key]); Object.assign(typed, value); data.pricing_conditions = structuredClone(value);
        controls.forEach((control, key) => {
          if (key.startsWith("billing_metric") || key === "discount_scope") return;
          const parts = key.split("."); const current = parts.length === 2 ? typed[parts[0]]?.[parts[1]] : typed[key];
          control.value = current == null ? (key.endsWith("_inclusive") ? "true" : "") : String(current);
        });
        advanced.setCustomValidity(""); onChange();
      } catch (error) { advanced.setCustomValidity(error.message); }
    });
    extra.append(node("details", {}, [node("summary", { text: "完整结构化条件（JSON）" }),
      node("p", { text: "常用字段可在上方填写；扩展条件保留在 raw 对象中，原文可放 legacy_text。留空表示未知，不代表所有条件。" }), advanced]));
    root.read = () => {
      for (const input of root.querySelectorAll("input,select,textarea")) if (!input.reportValidity()) throw Error(input.validationMessage);
      return { billing_metric: data.billing_metric ?? null, pricing_conditions: structuredClone(typed),
        ...(quote ? { discount_scope: data.discount_scope || "unknown", discount_items: structuredClone(data.discount_items || []) } : {}) };
    };
    return root;
  }
  window.TokenSQLConditions = { metrics, metricLabel, conditionText, priceLabel, scopeLabel, context, displayRows, selected, expanded, createEditor };
})();
