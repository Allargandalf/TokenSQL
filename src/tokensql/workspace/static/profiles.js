(() => {
  "use strict";
  const { state, $, node, api, toast, loadCatalog } = window.TokenSQL;
  const drafts = new Map();
  let profile = null, scope = "", loadGeneration = 0, globalPreferences = null;
  const controls = ["profileScope", "profileNewScope", "addProfileScopeButton", "addProfileMappingButton", "addProfileRuleButton", "saveProfileButton"];
  function section() {
    if (!profile) return null;
    return scope ? profile.sources[scope] : profile;
  }
  function dirty() {
    if (!profile) return;
    profile._dirty = true;
    $("profileSaveState").textContent = "有未保存的修改";
    $("profileError").textContent = "";
  }
  function option(select, value, text) {
    const item = node("option", { text }); item.value = value; select.append(item);
  }
  function field(text, value, onInput, options = {}) {
    const input = node(options.multiline ? "textarea" : "input", { type: options.multiline ? undefined : "text" });
    input.value = value || ""; input.setAttribute("aria-label", options.label || text);
    if (options.required) input.required = true;
    if (options.list) input.setAttribute("list", options.list);
    if (options.placeholder) input.placeholder = options.placeholder;
    if (options.multiline) input.rows = options.rows || 2;
    input.addEventListener("input", () => { onInput(input.value); if (options.track !== false) dirty(); });
    return node("label", {}, [text, input]);
  }
  function evidence(item) {
    const details = node("details", { className: "profile-evidence" }, [node("summary", { text: "确认来源" })]);
    const source = item.evidence && Object.keys(item.evidence).length ? item.evidence : { source: "人工编辑" };
    details.append(node("pre", { text: JSON.stringify(source, null, 2) }));
    return details;
  }
  function rowHeader(item, onDelete, track = true) {
    const enabled = node("input", { type: "checkbox" }); enabled.checked = item.enabled !== false;
    enabled.addEventListener("change", () => { item.enabled = enabled.checked; if (track) dirty(); });
    const remove = node("button", { className: "text-button danger", type: "button", text: "删除" });
    remove.addEventListener("click", () => { onDelete(); if (track) { dirty(); render(); } });
    return node("div", { className: "review-section-heading" }, [node("label", { className: "checkbox-label" }, [enabled, " 启用"]), remove]);
  }
  function render() {
    const current = section();
    $("profileMappings").replaceChildren(); $("profileRules").replaceChildren();
    $("profileFields").hidden = !current;
    controls.forEach((id) => { $(id).disabled = !profile; });
    $("deleteProfileScopeButton").hidden = !scope;
    $("profileSaveState").textContent = profile?._dirty ? "有未保存的修改" : "";
    $("profileEmpty").textContent = profile ? "" : "先在报价工作台添加或确认供应商，即可维护档案。";
    $("profileMeta").textContent = profile ? (profile.supplier_name || "") + " · 版本 " + profile.revision + (profile.updated_at ? " · " + profile.updated_at : "") : "";
    $("profilePrompt").value = current?.default_prompt || "";
    if (!current) return;
    current.model_mappings ||= []; current.rules ||= [];
    current.model_mappings.forEach((item, index) => {
      const label = "映射 " + (index + 1);
      const grid = node("div", { className: "profile-entry-grid" }, [
        field("原始模型名称", item.raw_name, (value) => { item.raw_name = value; }, { required: true, label: label + " 原始模型名称" }),
        field("系统 model_id", item.model_id, (value) => { item.model_id = value; }, { required: true, label: label + " 系统 model_id", list: "modelIdentifiers" }),
        field("供应商名称 / 渠道 ID", item.supplier_model_id, (value) => { item.supplier_model_id = value || null; }, { label: label + " 渠道 ID" }),
        field("常用别名（逗号分隔）", (item.aliases || []).join("、"), (value) => { item.aliases = value.split(/[,，、]/).map((v) => v.trim()).filter(Boolean); }, { label: label + " 别名" }),
      ]);
      $("profileMappings").append(node("article", { className: "profile-entry" }, [rowHeader(item, () => current.model_mappings.splice(index, 1)), grid, evidence(item)]));
    });
    if (!current.model_mappings.length) $("profileMappings").append(node("p", { className: "muted-cell", text: "添加供应商使用的名称与系统模型 ID 对应关系。" }));
    current.rules.forEach((item, index) => {
      const label = "规则 " + (index + 1);
      const kind = node("select"); kind.setAttribute("aria-label", label + " 类别");
      const kinds = [["interpretation", "表格含义"], ["unit", "币种与单位"], ["discount", "折扣口径"], ["preference", "长期偏好"]];
      if (item.kind && !kinds.some(([value]) => value === item.kind)) kinds.push([item.kind, item.kind]);
      kinds.forEach(([value, text]) => option(kind, value, text)); kind.value = item.kind || "interpretation";
      kind.addEventListener("change", () => { item.kind = kind.value; dirty(); });
      $("profileRules").append(node("article", { className: "profile-entry" }, [
        rowHeader(item, () => current.rules.splice(index, 1)),
        node("label", {}, ["类别", kind]),
        field("规则内容", item.text, (value) => { item.text = value; }, { multiline: true, required: true, label: label + " 内容", placeholder: "例如：价格列按人民币 / 百万 token；输入、输出分项计价" }),
        evidence(item),
      ]));
    });
    if (!current.rules.length) $("profileRules").append(node("p", { className: "muted-cell", text: "记录表头、工作表、计价单位与长期使用习惯。" }));
  }
  function renderScopes(preferred = scope) {
    $("profileScope").replaceChildren();
    option($("profileScope"), "", "该供应商通用");
    Object.keys(profile?.sources || {}).forEach((name) => option($("profileScope"), name, name === "default" ? "默认系列" : name));
    scope = preferred && profile?.sources?.[preferred] ? preferred : "";
    $("profileScope").value = scope; render();
  }
  async function selectSupplier(id, selectedScope = "") {
    const generation = ++loadGeneration;
    $("profileError").textContent = ""; $("reloadProfileButton").hidden = true;
    profile = null; render();
    if (!id) return;
    try {
      let loaded = drafts.get(id);
      if (!loaded?._dirty) loaded = await api("/suppliers/" + encodeURIComponent(id) + "/profile");
      if (generation !== loadGeneration) return;
      loaded.sources ||= {}; loaded.rules ||= []; loaded.model_mappings ||= [];
      loaded.supplier_id ||= id;
      loaded.supplier_name ||= state.catalog.suppliers.find((supplier) => supplier.supplier_id === id)?.name || "";
      drafts.set(id, loaded); profile = loaded; renderScopes(selectedScope);
    } catch (error) {
      if (generation === loadGeneration) { render(); $("profileError").textContent = error.message; }
    }
  }
  async function open(supplierId = state.supplierId || null, sourceScope = $("importSourceScope").value.trim()) {
    try {
      await loadCatalog();
      const select = $("profileSupplier"); select.replaceChildren();
      state.catalog.suppliers.forEach((supplier) => option(select, supplier.supplier_id, supplier.name));
      const filtered = state.catalog.suppliers.find((supplier) => supplier.name === $("providerFilter").value);
      select.value = supplierId || filtered?.supplier_id || state.catalog.suppliers[0]?.supplier_id || "";
      if (!select.value && select.options.length) select.selectedIndex = 0;
      $("profileDialog").showModal();
      await selectSupplier(select.value, sourceScope);
    } catch (error) { toast(error.message, true); }
  }
  async function save() {
    if (!profile) return;
    for (const input of $("profileFields").querySelectorAll("input,textarea,select")) if (!input.reportValidity()) return;
    $("saveProfileButton").disabled = true; $("profileError").textContent = "";
    const original = profile, id = original.supplier_id;
    const payload = { expected_revision: original.revision, default_prompt: original.default_prompt || "", model_mappings: original.model_mappings, rules: original.rules, sources: original.sources };
    try {
      const saved = await api("/suppliers/" + encodeURIComponent(id) + "/profile", { method: "PUT", body: JSON.stringify(payload) });
      drafts.set(id, saved);
      if (profile === original) { profile = saved; renderScopes(); $("profileSaveState").textContent = "已保存"; }
      window.dispatchEvent(new CustomEvent("tokensql:profile-saved", { detail: saved }));
      toast("供应商档案已保存");
    } catch (error) {
      $("reloadProfileButton").hidden = error.status !== 409;
      $("profileError").textContent = error.status === 409 ? "档案已有新版本，当前修订已保留。" : error.message;
    } finally { $("saveProfileButton").disabled = !profile; }
  }
  function renderGlobalPreferences() {
    const target = $("globalPreferencesRules"); target.replaceChildren();
    const rules = (globalPreferences?.rules || []).filter((rule) => ["workspace", "global"].includes(rule.scope));
    rules.forEach((rule, index) => {
      const input = field("偏好 " + (index + 1), rule.text, (value) => { rule.text = value; }, { multiline: true, required: true, label: "应用偏好 " + (index + 1), track: false });
      target.append(node("section", { className: "profile-entry" }, [rowHeader(rule, () => { globalPreferences.rules.splice(globalPreferences.rules.indexOf(rule), 1); renderGlobalPreferences(); }, false), input]));
    });
    if (!rules.length) target.append(node("p", { className: "muted-cell", text: "添加跨供应商使用的要求；各供应商的模型映射和表格含义在档案内维护。" }));
  }
  $("openGlobalPreferencesButton").addEventListener("click", async () => {
    try { globalPreferences = await api("/agent/memory"); renderGlobalPreferences(); $("globalPreferencesError").textContent = ""; $("globalPreferencesDialog").showModal(); }
    catch (error) { toast(error.message, true); }
  });
  $("closeGlobalPreferencesButton").addEventListener("click", () => $("globalPreferencesDialog").close());
  $("addGlobalPreferenceButton").addEventListener("click", () => {
    globalPreferences.rules.push({ id: "manual-" + crypto.randomUUID(), scope: "workspace", text: "", enabled: true, source: "human" }); renderGlobalPreferences();
  });
  $("saveGlobalPreferencesButton").addEventListener("click", async () => {
    for (const input of $("globalPreferencesRules").querySelectorAll("textarea")) if (!input.reportValidity()) return;
    $("saveGlobalPreferencesButton").disabled = true;
    try {
      globalPreferences = await api("/agent/memory", { method: "PATCH", body: JSON.stringify({ expected_revision: globalPreferences.revision, rules: globalPreferences.rules }) });
      $("globalPreferencesDialog").close(); toast("应用偏好已保存");
    } catch (error) { $("globalPreferencesError").textContent = error.message; }
    finally { $("saveGlobalPreferencesButton").disabled = false; }
  });
  $("reloadProfileButton").addEventListener("click", () => {
    if (!profile) return;
    const id = profile.supplier_id; drafts.delete(id); selectSupplier(id, scope);
  });
  $("memoryButton").addEventListener("click", () => open());
  $("closeProfileButton").addEventListener("click", () => $("profileDialog").close());
  $("profileSupplier").addEventListener("change", () => selectSupplier($("profileSupplier").value));
  $("profileScope").addEventListener("change", () => { scope = $("profileScope").value; render(); });
  $("profilePrompt").addEventListener("input", () => { if (section()) { section().default_prompt = $("profilePrompt").value; dirty(); } });
  $("addProfileScopeButton").addEventListener("click", () => {
    const name = $("profileNewScope").value.trim();
    if (!profile || !name) return;
    if (!profile.sources[name]) { profile.sources[name] = { default_prompt: "", model_mappings: [], rules: [] }; dirty(); }
    $("profileNewScope").value = ""; renderScopes(name);
  });
  $("deleteProfileScopeButton").addEventListener("click", () => { if (profile && scope) { delete profile.sources[scope]; dirty(); renderScopes(""); } });
  $("addProfileMappingButton").addEventListener("click", () => {
    if (!section()) return;
    section().model_mappings.push({ id: "manual-" + crypto.randomUUID(), raw_name: "", model_id: "", aliases: [], enabled: true, evidence: { source: "manual" } }); dirty(); render();
  });
  $("addProfileRuleButton").addEventListener("click", () => {
    if (!section()) return;
    section().rules.push({ id: "manual-" + crypto.randomUUID(), kind: "interpretation", text: "", enabled: true, evidence: { source: "manual" } }); dirty(); render();
  });
  $("saveProfileButton").addEventListener("click", save);
  window.TokenSQLProfiles = { open, get current() { return profile; } };
})();
