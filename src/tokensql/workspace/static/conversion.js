(() => {
  "use strict";

  const { $, api, node, toast, state: appState, renderRows, loadPricingUnits } = window.TokenSQL;
  const state = {
    currency: null,
    scale: null,
    pricing: false,
    context: null,
    customFactors: new Map(),
  };

  const SCALE_LABELS = {
    ktoken: "千 token",
    mtoken: "百万 token",
    second: "秒",
    image: "张",
    mtoken_hour: "百万 token·小时",
    request: "次请求",
    account_month: "账号·月",
  };

  const SUGGESTIONS = [
    { model: "Seedance 2.5", resolution: "480p · 16:9 / 9:16", size: "854 × 480", fps: "24", value: "9607.5" },
    { model: "Seedance 2.0", resolution: "480p · 16:9 / 9:16", size: "864 × 496", fps: "24", value: "10044" },
    { model: "Seedance 2.0 / 2.5", resolution: "720p · 16:9 / 9:16", size: "1280 × 720", fps: "24", value: "21600" },
    { model: "Seedance 2.0 / 2.5", resolution: "1080p · 16:9 / 9:16", size: "1920 × 1080", fps: "24", value: "48600" },
    { model: "Seedance 2.0", resolution: "4K · 16:9 / 9:16", size: "3840 × 2160", fps: "24", value: "194400" },
  ];
  const SUGGESTION_SOURCE = "火山方舟视频 token 估算公式 T=(输入秒+输出秒)×宽×高×FPS/1024；准确值以 API usage.completion_tokens 为准";

  function abs(value) { return value < 0n ? -value : value; }
  function gcd(a, b) {
    a = abs(a); b = abs(b);
    while (b) [a, b] = [b, a % b];
    return a || 1n;
  }
  function rational(n, d = 1n) {
    if (d === 0n) throw new Error("除数不能为 0");
    if (d < 0n) { n = -n; d = -d; }
    const common = gcd(n, d);
    return { n: n / common, d: d / common };
  }
  function parseDecimal(value) {
    const text = String(value ?? "").trim();
    if (text.length > 10000) throw new Error("十进制字符串过长");
    const match = text.match(/^([+-]?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/);
    if (!match) throw new Error("必须是合法十进制数字");
    const fraction = match[3] || "";
    const exponent = Number(match[4] || "0");
    if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 10000) throw new Error("十进制指数超出支持范围");
    const sign = match[1] === "-" ? -1n : 1n;
    const digits = BigInt(match[2] + fraction);
    const scale = fraction.length - exponent;
    return scale >= 0
      ? rational(sign * digits, 10n ** BigInt(scale))
      : rational(sign * digits * (10n ** BigInt(-scale)), 1n);
  }
  function multiply(...values) {
    return values.reduce((result, value) => rational(result.n * value.n, result.d * value.d), rational(1n));
  }
  function divide(left, right) {
    if (right.n === 0n) throw new Error("倍率不能为 0");
    return rational(left.n * right.d, left.d * right.n);
  }
  function positiveDecimal(value) {
    const parsed = parseDecimal(value);
    if (parsed.n <= 0n) throw new Error("倍率必须大于 0");
    return parsed;
  }
  function decimalString(value, places = 120) {
    const negative = value.n < 0n;
    let numerator = abs(value.n);
    const whole = numerator / value.d;
    let remainder = numerator % value.d;
    let fraction = "";
    for (let index = 0; remainder && index < places; index += 1) {
      remainder *= 10n;
      fraction += String(remainder / value.d);
      remainder %= value.d;
    }
    fraction = fraction.replace(/0+$/, "");
    const rendered = (negative ? "-" : "") + String(whole) + (fraction ? "." + fraction : "");
    return remainder ? rendered + "…（近似）" : rendered;
  }

  function fixedDecimal(value, places = 1) {
    const factor = 10n ** BigInt(places);
    const scaled = abs(value.n) * factor;
    let rounded = scaled / value.d;
    if ((scaled % value.d) * 2n >= value.d) rounded += 1n;
    const digits = String(rounded).padStart(places + 1, "0");
    return (value.n < 0n && rounded !== 0n ? "-" : "") +
      (places ? digits.slice(0, -places) + "." + digits.slice(-places) : digits);
  }
  function discountRatio(value) {
    if (value == null || String(value).trim() === "") return null;
    const parsed = parseDecimal(value);
    if (parsed.n < 0n) throw new Error("付款倍率不能为负数");
    return fixedDecimal(parsed, 2);
  }
  function discountFold(value) {
    const ratio = discountRatio(value);
    if (ratio == null) return null;
    return fixedDecimal(multiply(parseDecimal(ratio), rational(10n)), 1);
  }
  function ratioFromFold(value) {
    if (value == null || String(value).trim() === "") return null;
    const parsed = parseDecimal(value);
    if (parsed.n < 0n) throw new Error("折扣不能为负数");
    const rounded = parseDecimal(fixedDecimal(parsed, 1));
    return fixedDecimal(divide(rounded, rational(10n)), 2);
  }

  function legacyScale(price) {
    if (price.unit === "token" && String(price.unit_quantity) === "1000") return "ktoken";
    if (price.unit === "token" && String(price.unit_quantity) === "1000000") return "mtoken";
    if (price.unit === "second" && String(price.unit_quantity) === "1") return "second";
    if (price.unit === "image" && String(price.unit_quantity) === "1") return "image";
    return null;
  }
  function sourceUnit(price) {
    if (!price) return null;
    const canonical = appState.pricingUnits.items.find((item) => item.id === price.pricing_unit_id);
    if (canonical) return canonical;
    const scale = legacyScale(price);
    if (!scale || !["CNY", "USD", "HKD"].includes(price.currency)) return null;
    return {
      id: null,
      currency: price.currency,
      unit: price.unit,
      unit_quantity: String(price.unit_quantity),
      dimension: price.unit === "second" ? "time" : price.unit,
      scale,
      label: price.currency + " / " + SCALE_LABELS[scale],
    };
  }
  function targetUnit(source) {
    return appState.pricingUnits.items.find((item) =>
      item.currency === (state.currency || source.currency) && item.scale === (state.scale || source.scale));
  }
  function rate(currency) {
    if (currency === "CNY") return {
      value: rational(1n), source: "CNY 恒等汇率", observed_at: null,
    };
    const row = (state.context?.fx_rates || []).find((item) => item.currency === currency);
    if (!row || !row.cny_per_unit || !String(row.source || "").trim()) return null;
    if (row.expires_at) {
      const expires = new Date(row.expires_at);
      if (!Number.isNaN(expires.getTime()) && expires.getTime() <= Date.now()) {
        return { error: currency + " 汇率已过期（" + row.expires_at + "）" };
      }
    }
    try {
      return { value: positiveDecimal(row.cny_per_unit), source: row.source, source_url: row.source_url || null,
        observed_at: row.observed_at || null, expires_at: row.expires_at || null };
    } catch (_) { return null; }
  }
  function unitFactor(price, unit) {
    if (unit.dimension === "token") return { value: rational(1n), source: "token 同量纲恒等", temporary: false };
    const custom = state.customFactors.get(price.model_id + "\u0000" + price.billing_item);
    const key = unit.dimension === "time" ? "tokens_per_second" : "tokens_per_image";
    if (custom?.[key]) return { value: custom[key], source: custom.source, temporary: true, model_id: price.model_id };
    const row = (state.context?.token_equivalences || []).find((item) =>
      item.model_id === price.model_id && item.billing_item === price.billing_item && item.unit === unit.unit);
    if (!row || !row.tokens_per_unit || !String(row.source || row.source_ref || "").trim()) return null;
    try {
      return { value: positiveDecimal(row.tokens_per_unit), source: row.source || row.source_ref,
        source_ref: row.source_ref || null, observed_at: row.observed_at || null,
        assumptions: row.assumptions || null, temporary: false, model_id: row.model_id };
    } catch (_) { return null; }
  }
  function unitLabel(unit) {
    return SCALE_LABELS[unit.scale] || (String(unit.unit_quantity) + " " + unit.unit);
  }

  function convertTo(price, explicitTarget = null) {
    if (!price || price.amount === null || price.amount === undefined) return null;
    const source = sourceUnit(price);
    if (!source) return { status: "unavailable", reason: !price.currency && !price.pricing_unit_id ? "原币种未提供，不能假定同币种" : "原计价单位缺失或不在固定字典中" };
    const target = explicitTarget || targetUnit(source);
    if (!target) return { status: "unavailable", reason: "目标计价单位不存在" };
    let amount;
    try { amount = price._displayRational || parseDecimal(price.amount); }
    catch (_) { return { status: "unavailable", reason: "原金额不是合法十进制字符串" }; }

    let currencyRatio = rational(1n);
    let sourceRate = null;
    let targetRate = null;
    if (source.currency !== target.currency) {
      sourceRate = rate(source.currency); targetRate = rate(target.currency);
      if (sourceRate?.error || targetRate?.error) return {
        status: "unavailable", reason: sourceRate?.error || targetRate?.error,
      };
      if (!sourceRate || !targetRate) return {
        status: "unavailable",
        reason: "缺少 " + (!sourceRate ? source.currency : target.currency) + " 的带来源汇率",
      };
      currencyRatio = divide(sourceRate.value, targetRate.value);
    }

    let sourceFactor = { value: rational(1n), source: "同量纲", temporary: false };
    let targetFactor = sourceFactor;
    const estimated = source.dimension !== target.dimension;
    if (estimated && (![source.dimension, target.dimension].every((dimension) => ["token", "time", "image"].includes(dimension)))) {
      return { status: "unavailable", reason: "这些计价单位属于不同量纲，不能换算" };
    }
    if (estimated) {
      sourceFactor = unitFactor(price, source);
      targetFactor = unitFactor(price, target);
      if (!sourceFactor || !targetFactor) {
        const missing = !sourceFactor ? source.dimension : target.dimension;
        return { status: "unavailable", reason: "缺少该模型的 " + (missing === "time" ? "tokens_per_second" : "tokens_per_image") + " 合法倍率" };
      }
    }

    const sourceQuantity = positiveDecimal(source.unit_quantity);
    const targetQuantity = positiveDecimal(target.unit_quantity);
    const unitRatio = divide(multiply(targetQuantity, targetFactor.value), multiply(sourceQuantity, sourceFactor.value));
    const multiplier = multiply(currencyRatio, unitRatio);
    const converted = multiply(amount, multiplier);
    const renderedAmount = decimalString(converted);
    const renderedMultiplier = decimalString(multiplier);
    const fxSource = source.currency === target.currency ? "同币种，无 FX" :
      sourceRate.source + " → CNY → " + targetRate.source;
    const factorSource = estimated ? sourceFactor.source + " → " + targetFactor.source : "同量纲精确尺度";
    return {
      status: "converted",
      amount: renderedAmount,
      amountNumerator: String(converted.n),
      amountDenominator: String(converted.d),
      amountApproximate: renderedAmount.includes("…"),
      decimalPlaces: "120",
      currency: target.currency,
      pricingUnitId: target.id,
      unit: target.unit,
      unitQuantity: target.unit_quantity,
      unitLabel: unitLabel(target),
      originalAmount: String(price.amount),
      originalCurrency: source.currency,
      originalPricingUnitId: source.id,
      originalUnit: source.unit,
      originalUnitQuantity: source.unit_quantity,
      originalUnitLabel: unitLabel(source),
      multiplier: renderedMultiplier,
      multiplierNumerator: String(multiplier.n),
      multiplierDenominator: String(multiplier.d),
      estimated,
      formula: "原金额 × FX(" + decimalString(currencyRatio) + ") × 单位倍率(" + decimalString(unitRatio) + ")",
      evidence: {
        pricing_unit_dictionary_version: appState.pricingUnits.version,
        pricing_context_revision: state.context?.revision ?? null,
        fx_source: fxSource,
        fx_observed_at: sourceRate?.observed_at || targetRate?.observed_at || null,
        factor_source: factorSource,
        temporary_factor: Boolean(sourceFactor.temporary || targetFactor.temporary),
        estimate: estimated,
      },
    };
  }

  function convert(price) { return (state.currency || state.scale) ? convertTo(price) : null; }
  function officialFor(price) {
    const context = window.TokenSQLConditions?.context(price);
    if (context) return context.matched.length === 1 ? context.matched[0] : null;
    return price?.official_price || null;
  }
  function pricingRows(price) {
    const context = window.TokenSQLConditions.context(price);
    return window.TokenSQLConditions.displayRows(price).map((official) => ({
      official, applicable: context.matched.some((row) => row.official_price_id ? row.official_price_id === official.official_price_id : row === official),
      pricing: ["matched", "multiple"].includes(context.status) ? computePricing(price, official) : null,
    }));
  }
  function rawDiscount(price) { return discountRatio(price?.raw_discount_rate ?? price?.discount_rate ?? null); }
  function computePricing(price, explicitOfficial = null) {
    const match = window.TokenSQLConditions?.context(price);
    const official = explicitOfficial || officialFor(price);
    const effectiveRule = price.effective_discount_rules?.find((row) => row.official_price_id === official?.official_price_id);
    const originalDiscount = effectiveRule ? discountRatio(effectiveRule.discount_rate) : rawDiscount(price);
    if (!official) return { status: "unavailable", reason: match?.status === "multiple" ? "已有多项适用官网价，请展开逐项查看折后价。" : match?.reason || "缺失官网价", missingOfficial: !match || match.status === "missing" };
    if (official.amount == null) return { status: "unavailable", reason: "该官网计费项未提供金额", missingOfficial: match?.status === "missing" };
    const officialUnit = sourceUnit(official);
    if (!officialUnit) return { status: "unavailable", reason: !official.currency && !official.pricing_unit_id ? "官网币种未提供，不能假定同币种" : "官网计价单位缺失或不完整" };
    let officialAmount;
    try { officialAmount = parseDecimal(official.amount); }
    catch (_) { return { status: "unavailable", reason: "官网价不是合法十进制数字" }; }
    const result = { status: "computed", officialAmount: String(official.amount), officialUnit,
      rawDiscount: originalDiscount, effectiveRule, evidence: { calculation: "frontend_only", official_model_id: official.model_id,
        official_price_id: official.official_price_id ?? null, official_billing_metric: official.billing_metric ?? null,
        official_pricing_conditions: official.pricing_conditions || {}, discount_scope: price.discount_scope || "unknown",
        effective_discount_rule: effectiveRule || null,
        official_billing_item: official.billing_item ?? null, official_revision: official.revision ?? null,
        official_source: official.source ?? null, official_source_ref: official.source_ref ?? null,
        official_amount: String(official.amount), official_pricing_unit_id: officialUnit.id } };
    try {
      if (effectiveRule?.reason === "conflicting_discount_rules") result.discountIssue = "同等优先级的源折扣冲突，请先确认适用报价。";
      if (originalDiscount != null) {
        const value = multiply(officialAmount, parseDecimal(originalDiscount));
        result.derivedAmount = decimalString(value);
        result.derivedNumerator = String(value.n); result.derivedDenominator = String(value.d);
        result.derivedPrice = { ...price, amount: result.derivedAmount, _displayRational: value,
          pricing_unit_id: officialUnit.id, currency: officialUnit.currency, unit: officialUnit.unit,
          unit_quantity: officialUnit.unit_quantity };
        result.derivedFormula = "官网价按源折扣计算";
      }
      if (price.amount != null) {
        const itemKnown = price.billing_metric || price.billing_item || match?.matched.length === 1;
        const aligned = itemKnown ? convertTo(price, officialUnit) : { status: "unavailable", reason: "源金额未明确计费项，不能逐项反算折扣" };
        if (!aligned || aligned.status !== "converted") {
          result.discountIssue = aligned?.reason || "供应商计价单位缺失或不完整";
        } else if (officialAmount.n === 0n) {
          result.discountIssue = "官网价为 0，不能反算折扣";
        } else {
          const value = divide(rational(BigInt(aligned.amountNumerator), BigInt(aligned.amountDenominator)), officialAmount);
          result.calculatedDiscount = fixedDecimal(value, 2);
          result.calculatedDiscountFold = discountFold(result.calculatedDiscount);
          const rounded = parseDecimal(result.calculatedDiscount);
          result.discountRounded = rounded.n * value.d !== value.n * rounded.d;
          result.evidence.discount_rounding = "付款倍率按 HALF_UP 保留两位小数；辅助折数由两位倍率计算";
          result.evidence.discount_rounded = result.discountRounded;
          result.discountNumerator = String(value.n); result.discountDenominator = String(value.d);
          result.estimated = aligned.estimated;
          result.evidence.alignment = aligned.evidence;
          result.evidence.alignment_formula = aligned.formula;
          result.evidence.alignment_multiplier_numerator = aligned.multiplierNumerator;
          result.evidence.alignment_multiplier_denominator = aligned.multiplierDenominator;
          result.evidence.aligned_supplier_amount = aligned.amount;
          result.discountFormula = "供应商原价转换为官网单位 ÷ 官网原价";
        }
      }
      if (price.amount == null && originalDiscount == null && !result.discountIssue) result.discountIssue = "源金额与源折扣均未提供";
    } catch (error) { return { status: "unavailable", reason: error.message }; }
    return result;
  }
  function displayPrice(price) {
    if (price?.amount != null || !state.pricing) return price;
    return computePricing(price).derivedPrice || price;
  }
  function isActive() { return Boolean(state.currency || state.scale || state.pricing); }

  function optionButton(text, active, handler) {
    const button = node("button", { className: "conversion-option" + (active ? " is-active" : ""), text, type: "button" });
    button.addEventListener("click", handler);
    return button;
  }
  function renderControls() {
    const currencyOptions = $("currencyConversionOptions");
    currencyOptions.replaceChildren(optionButton("恢复原币", state.currency === null, () => setCurrency(null)));
    ["USD", "HKD", "CNY"].forEach((currency) =>
      currencyOptions.append(optionButton(currency, state.currency === currency, () => setCurrency(currency))));
    const unitOptions = $("unitConversionOptions");
    unitOptions.replaceChildren(optionButton("恢复原单位", state.scale === null, () => setScale(null)));
    Object.entries(SCALE_LABELS).filter(([scale]) => appState.pricingUnits.items.some((unit) => unit.scale === scale)).forEach(([scale, label]) =>
      unitOptions.append(optionButton(label, state.scale === scale, () => setScale(scale))));
    $("currencyConversionButton").textContent = "币种：" + (state.currency || "原币");
    $("unitConversionButton").textContent = "单位：" + (state.scale ? SCALE_LABELS[state.scale] : "原单位");
    $("currencyConversionButton").classList.toggle("is-active", Boolean(state.currency));
    $("unitConversionButton").classList.toggle("is-active", Boolean(state.scale));
    $("pricingConversionButton").textContent = "折扣 / 折后价：" + (state.pricing ? "计算显示" : "原始值");
    $("pricingConversionButton").classList.toggle("is-active", state.pricing);
    $("pricingConversionButton").setAttribute("aria-pressed", String(state.pricing));
    $("resetConversionButton").hidden = !isActive();
    $("conversionSummary").textContent = isActive()
      ? "临时显示换算已启用 · CSV / XLSX 导出包含换算证据 · 不影响入库"
      : "两个显示换算可独立使用 · 缺合法倍率不换算";
    const targetLabel = state.scale ? SCALE_LABELS[state.scale] : "原单位";
    $("conversionFormula").textContent = "公式：原金额 ×（原币 CNY 汇率 ÷ 目标币 CNY 汇率）×（目标数量 × 目标 tokens/单位）÷（原数量 × 原 tokens/单位）。当前目标：" + targetLabel + "。";
    renderRows();
  }
  function setCurrency(currency) {
    state.currency = currency;
    renderControls();
    $("currencyConversionDialog").close();
  }
  function setScale(scale) {
    state.scale = scale;
    renderControls();
  }
  function reset() {
    state.currency = null; state.scale = null; state.pricing = false;
    renderControls();
  }

  function applyTemporaryFactors() {
    const modelId = $("temporaryFactorModel").value.trim();
    const billingItem = $("temporaryFactorBillingItem").value.trim();
    const source = $("temporaryFactorSource").value.trim();
    const second = $("temporaryTokensPerSecond").value.trim();
    const image = $("temporaryTokensPerImage").value.trim();
    $("temporaryFactorError").textContent = "";
    try {
      if (!modelId) throw new Error("请填写适用模型 ID；临时倍率不能跨模型默认套用。");
      if (!billingItem) throw new Error("请填写适用计费项；临时倍率不能污染同模型其他计费项。");
      if (!source) throw new Error("请填写临时倍率来源 / 说明。");
      if (!second && !image) throw new Error("请至少填写一个倍率。");
      state.customFactors.set(modelId + "\u0000" + billingItem, {
        tokens_per_second: second ? positiveDecimal(second) : null,
        tokens_per_image: image ? positiveDecimal(image) : null,
        source,
      });
      $("temporaryFactorScope").textContent = "当前临时范围：" + modelId + " / " + billingItem +
        "；仅该组合中需要跨量纲的行受影响。其他模型、计费项与同量纲价格不使用此倍率。";
      toast("临时倍率已应用于 " + modelId + " / " + billingItem + "；刷新页面即清除。");
      renderRows();
    } catch (error) { $("temporaryFactorError").textContent = error.message; }
  }

  function renderSuggestions() {
    const target = $("factorSuggestions");
    target.replaceChildren();
    SUGGESTIONS.forEach((suggestion) => {
      const use = node("button", { className: "text-button", type: "button", text: suggestion.value });
      use.setAttribute("aria-label", "使用 " + suggestion.model + " " + suggestion.resolution + " 建议倍率");
      use.addEventListener("click", () => {
        $("temporaryFactorModel").value = suggestion.model;
        $("temporaryFactorBillingItem").value = "";
        $("temporaryTokensPerSecond").value = suggestion.value;
        $("temporaryFactorSource").value = SUGGESTION_SOURCE + "；建议元数据：" + suggestion.model + " / " +
          suggestion.resolution + " / " + suggestion.size + " / " + suggestion.fps + " FPS；若改用于非 Seedance，仅可作为未验证参考";
        $("temporaryFactorError").textContent = "请把适用模型 ID 改成精确 model_id，并确认计费项后再应用；非 Seedance 仅可视作未验证参考，建议不会自动生效。";
      });
      const links = node("span", { className: "source-links" });
      [
        ["估算公式", "https://docs.volcengine.com/docs/ark/model-pricing?lang=zh&redirect=1"],
        ["像素档", "https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh"],
        ["24 FPS", "https://docs.volcengine.com/docs/ark/model-list?lang=zh"],
      ].forEach(([label, href]) => {
        const link = document.createElement("a");
        link.href = href; link.target = "_blank"; link.rel = "noreferrer"; link.textContent = label;
        links.append(link);
      });
      target.append(node("tr", {}, [
        node("td", { text: suggestion.model }), node("td", { text: suggestion.resolution }),
        node("td", { text: suggestion.size }), node("td", { text: suggestion.fps }),
        node("td", {}, [use]), node("td", {}, [links]),
      ]));
    });
  }

  function csvCell(value) {
    const text = value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
    return '"' + text.replace(/"/g, '""') + '"';
  }
  async function filteredItems(params) {
    const query = new URLSearchParams(params);
    const selectedIds = new Set((query.get("ids") || "").split(",").filter(Boolean));
    query.delete("format"); query.delete("ids");
    const items = [];
    let offset = 0;
    const limit = 500;
    while (true) {
      query.set("limit", String(limit)); query.set("offset", String(offset));
      const payload = await api("/quotes?" + query);
      items.push(...(payload.items || []));
      if (!payload.items?.length || items.length >= Number(payload.total || items.length)) break;
      offset += payload.items.length;
    }
    return selectedIds.size ? items.filter((item) => selectedIds.has(String(item.id))) : items;
  }
  async function exportConverted(params, providedItems) {
    try {
      const items = providedItems || await filteredItems(params);
      const columns = [
        ["quote_id", "源报价 ID"], ["model_id", "模型 ID"], ["model_name", "模型名称"], ["vendor", "模型商"],
        ["billing_metric", "标准计费项"], ["pricing_conditions", "供应商适用条件"],
        ["discount_scope", "源折扣适用范围"], ["discount_items", "源折扣指定项"],
        ["official_billing_metric", "官网标准计费项"], ["official_billing_item", "官网原计费项"],
        ["official_conditions", "官网适用条件"], ["official_amount", "官网原价"], ["official_unit", "官网计价单位"],
        ["official_source", "官网来源"], ["official_source_ref", "官网来源依据"], ["official_revision", "官网价版本"], ["official_explanation", "官网匹配说明"],
        ["effective_discount_fold", "本项采用源折扣（折）"], ["effective_quote_id", "本项折扣源报价 ID"], ["effective_discount_basis", "本项折扣依据"],
        ["billing_item", "计费项"], ["supplier", "供应商"], ["tags", "标签"], ["supplier_model_id", "渠道 ID"],
        ["source", "原来源"], ["source_ref", "原来源依据"], ["conditions", "原条件"], ["note", "原备注"],
        ["original_amount", "原价"], ["original_discount_rate", "原始存储折扣值（两位小数）"], ["original_discount_fold", "源折扣（折，一位小数）"],
        ["frontend_derived_amount", "前端折后价"], ["frontend_derived_pricing_unit_id", "前端折后价单位"],
        ["frontend_calculated_discount", "按官网反算折扣值（两位小数）"], ["frontend_calculated_discount_fold", "按官网反算折扣（折，一位小数）"],
        ["frontend_pricing_reason", "前端计算说明"], ["frontend_pricing_evidence", "官网价及计算依据"],
        ["frontend_discount_numerator", "反算倍率精确分子"], ["frontend_discount_denominator", "反算倍率精确分母"],
        ["frontend_price_numerator", "折后价精确分子"], ["frontend_price_denominator", "折后价精确分母"],
        ["frontend_price_approximate", "折后价显示为近似"], ["frontend_discount_approximate", "反算倍率显示为近似"], ["frontend_discount_rounding", "反算倍率舍入规则"],
        ["original_pricing_unit_id", "原计价单位 ID"],
        ["original_currency", "原币"], ["original_unit", "原单位"], ["original_unit_quantity", "原单位数量"],
        ["converted_amount", "换算价"], ["target_pricing_unit_id", "目标计价单位 ID"],
        ["target_currency", "目标币"], ["target_unit", "目标单位"], ["target_unit_quantity", "目标单位数量"],
        ["conversion_status", "换算状态"], ["estimated", "工程估算"], ["amount_approximate", "小数为近似"],
        ["converted_amount_numerator", "换算价有理数分子"], ["converted_amount_denominator", "换算价有理数分母"],
        ["fx_source", "FX 来源"], ["fx_version", "FX / pricing-context 版本"],
        ["multiplier", "总倍率"], ["multiplier_numerator", "倍率有理数分子"], ["multiplier_denominator", "倍率有理数分母"],
        ["decimal_places", "最大小数位"], ["formula", "换算公式 / 未换算原因"], ["factor_source", "工程倍率来源"],
      ].map(([key, label]) => ({ key, label }));
      const rows = items.flatMap((item) => {
        const match = window.TokenSQLConditions.context(item);
        const choices = state.pricing ? pricingRows(item) : [];
        return (choices.length ? choices : [{ official: null, pricing: state.pricing ? computePricing(item) : null }]).map(({ official, pricing }) => {
        const display = item.amount == null && state.pricing ? pricing?.derivedPrice || item : item;
        const result = convert(display);
        const source = sourceUnit(item);
        const displaySource = sourceUnit(display);
        const intendedTarget = displaySource ? targetUnit(displaySource) : null;
        const crossDimension = Boolean(source && intendedTarget && source.dimension !== intendedTarget.dimension);
        return {
          quote_id: item.id, model_id: item.model_id, model_name: item.model_name ?? null, vendor: item.vendor ?? null,
          billing_metric: item.billing_metric || "", pricing_conditions: item.pricing_conditions || {},
          discount_scope: window.TokenSQLConditions.scopeLabel(item.discount_scope), discount_items: item.discount_items || [],
          official_billing_metric: official?.billing_metric || official?.billing_item || "",
          official_billing_item: official?.billing_item || "", official_conditions: official?.pricing_conditions || {},
          official_amount: official?.amount ?? "", official_unit: sourceUnit(official)?.label || "",
          official_source: official?.source || "", official_source_ref: official?.source_ref || "", official_revision: official?.revision ?? "",
          official_explanation: match.reason || (match.matched.length > 1 ? "逐项适用" : "唯一适用官网价"),
          effective_discount_fold: discountFold(pricing?.rawDiscount), effective_quote_id: pricing?.effectiveRule?.quote_id ?? item.id,
          effective_discount_basis: pricing?.effectiveRule?.reason === "item_override" ? "指定计费项源折扣优先" : pricing?.effectiveRule?.reason === "conflicting_discount_rules" ? "同等优先级源折扣冲突，未计算" : pricing?.rawDiscount != null ? "采用适用源折扣" : "无适用源折扣",
          billing_item: item.billing_item, supplier: item.supplier ?? item.provider ?? null,
          tags: item.tags == null ? null : JSON.stringify(item.tags), supplier_model_id: item.supplier_model_id ?? null,
          source: item.source ?? null, source_ref: item.source_ref ?? null,
          conditions: item.conditions == null ? null : JSON.stringify(item.conditions), note: item.note ?? null,
          original_amount: item.amount, original_discount_rate: rawDiscount(item), original_discount_fold: discountFold(rawDiscount(item)),
          frontend_derived_amount: pricing?.derivedAmount ?? "",
          frontend_derived_pricing_unit_id: pricing?.derivedAmount != null ? pricing.officialUnit.id : "",
          frontend_calculated_discount: pricing?.calculatedDiscount ?? "", frontend_calculated_discount_fold: pricing?.calculatedDiscountFold ?? "",
          frontend_pricing_status: pricing?.status || "disabled",
          frontend_pricing_reason: pricing?.reason || pricing?.discountIssue || "",
          frontend_pricing_evidence: pricing?.evidence ? JSON.stringify(pricing.evidence) : "",
          frontend_discount_numerator: pricing?.discountNumerator ?? "", frontend_discount_denominator: pricing?.discountDenominator ?? "",
          frontend_price_numerator: pricing?.derivedNumerator ?? "", frontend_price_denominator: pricing?.derivedDenominator ?? "",
          frontend_price_approximate: pricing?.derivedAmount?.includes("…") ?? false,
          frontend_discount_approximate: pricing?.discountRounded ?? false,
          frontend_discount_rounding: pricing?.calculatedDiscount != null ? "HALF_UP，两位小数；仅前端显示，不入库" : "",
          original_pricing_unit_id: source?.id || item.pricing_unit_id || "",
          original_currency: source?.currency || item.currency, original_unit: source?.unit || item.unit,
          original_unit_quantity: source?.unit_quantity || item.unit_quantity,
          converted_amount: result?.status === "converted" ? result.amount : "",
          target_pricing_unit_id: result?.pricingUnitId || intendedTarget?.id || "",
          target_currency: result?.currency || intendedTarget?.currency || "",
          target_unit: result?.unit || intendedTarget?.unit || "",
          target_unit_quantity: result?.unitQuantity || intendedTarget?.unit_quantity || "",
          conversion_status: result?.status || "unavailable", estimated: result?.estimated ?? crossDimension,
          amount_approximate: result?.amountApproximate ?? false,
          converted_amount_numerator: result?.amountNumerator || "", converted_amount_denominator: result?.amountDenominator || "",
          fx_source: result?.evidence?.fx_source || "", fx_version: String(result?.evidence?.pricing_context_revision ?? ""),
          multiplier: result?.multiplier || "", multiplier_numerator: result?.multiplierNumerator || "",
          multiplier_denominator: result?.multiplierDenominator || "", decimal_places: result?.decimalPlaces || "",
          formula: result?.formula || result?.reason || "", factor_source: result?.evidence?.factor_source || "",
        };
      });
      });
      const format = params.get("format") === "xlsx" ? "xlsx" : "csv";
      const filename = "tokensql-display-converted." + format;
      const response = await fetch("/api/workspace/exports/view", {
        method: "POST", credentials: "same-origin",
        headers: { Accept: format === "xlsx" ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : "text/csv",
          "Content-Type": "application/json" },
        body: JSON.stringify({ format, filename, columns, rows }),
      });
      if (!response.ok) {
        let detail = "转换导出失败（" + response.status + "）";
        try { const body = await response.json(); detail = typeof body.detail === "string" ? body.detail : detail; } catch (_) {}
        throw new Error(detail);
      }
      const href = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = href; anchor.download = filename;
      document.body.append(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(href);
      toast("已导出转换显示 " + format.toUpperCase() + "（含原价、换算价、FX 版本、倍率与估算标识）");
    } catch (error) { toast(error.message, true); }
  }

  async function initialize() {
    try {
      await loadPricingUnits();
      appState.pricingUnits.items.forEach((unit) => { if (!SCALE_LABELS[unit.scale]) SCALE_LABELS[unit.scale] = (unit.label || unit.scale).replace(/^[A-Z]+\s*\/\s*/, ""); });
      state.context = await api("/pricing-context");
      $("currencyConversionHelp").textContent = "当前 pricing-context 版本 " + state.context.revision +
        "。只使用带 source 的 cny_per_unit；CNY 使用恒等汇率。";
      renderSuggestions();
      renderControls();
    } catch (error) {
      toast("显示换算初始化失败：" + error.message, true);
    }
  }

  $("currencyConversionButton").addEventListener("click", () => $("currencyConversionDialog").showModal());
  $("unitConversionButton").addEventListener("click", () => $("unitConversionDialog").showModal());
  $("closeCurrencyConversionButton").addEventListener("click", () => $("currencyConversionDialog").close());
  $("closeUnitConversionButton").addEventListener("click", () => $("unitConversionDialog").close());
  $("resetConversionButton").addEventListener("click", reset);
  $("pricingConversionButton").addEventListener("click", () => { state.pricing = !state.pricing; renderControls(); });
  $("applyTemporaryFactorsButton").addEventListener("click", applyTemporaryFactors);
  window.addEventListener("tokensql:pricing-context", (event) => {
    state.context = event.detail;
    renderControls();
  });

  window.TokenSQLConversion = { convert, convertTo, pricingRows, computePricing, displayPrice, rawDiscount, discountRatio, discountFold, ratioFromFold, sourceUnit, isActive, exportConverted, state, _exact: { parseDecimal, multiply, divide, decimalString, fixedDecimal } };
  initialize();
})();
