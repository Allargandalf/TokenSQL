(() => {
  "use strict";
  const { node } = window.TokenSQL;
  const labels = { added: "新增", modified: "修改", deleted: "删除", moved: "位置调整", presentation: "展示格式" };
  const kinds = Object.keys(labels);
  const cellKey = (cell) => JSON.stringify(cell ? [cell.type, cell.value, cell.formula, cell.cached_value, cell.format_kind, cell.comment] : null);
  const cellStyle = (cell) => JSON.stringify(cell ? cell.style : null);
  function columnName(number) { let text = ""; while (number > 0) { number--; text = String.fromCharCode(65 + number % 26) + text; number = Math.floor(number / 26); } return text; }
  function value(cell) {
    if (!cell) return "";
    const displayed = cell.type === "formula" ? (cell.cached_value ?? cell.formula ?? cell.value) : cell.value;
    if (displayed === null || displayed === undefined) return "";
    const exact = window.TokenSQLConversion?._exact, raw = typeof displayed === "object" ? JSON.stringify(displayed) : String(displayed);
    if (exact && (cell.type === "number" || cell.type === "formula") && /^[-+]?\d+(?:\.\d+)?$/.test(raw)) {
      const pattern = String(cell.number_format || "General"), percent = /^(?:0)(?:\.(0{1,10}))?%$/.exec(pattern);
      if (percent) return exact.fixedDecimal(exact.multiply(exact.parseDecimal(raw), exact.parseDecimal("100")), percent[1]?.length || 0) + "%";
      const fixed = /^0\.(0{1,10})$/.exec(pattern);
      if (fixed) return exact.fixedDecimal(exact.parseDecimal(raw), fixed[1].length);
    }
    return raw;
  }
  function sheetIndex(snapshot) {
    const result = new Map();
    (snapshot?.files || []).forEach((file) => (file.sheets || []).forEach((sheet) => result.set(sheet.sheet_key, { ...sheet, filename: file.filename, file_key: file.file_key })));
    return result;
  }
  function sheetPairs(comparison, oldSheets, newSheets) {
    const result = [], seen = new Set(), oldUsed = new Set(), newUsed = new Set();
    function add(oldKey, newKey) {
      const key = (oldKey || "") + "|" + (newKey || "");
      if (seen.has(key) || !oldKey && !newKey) return;
      seen.add(key); if (oldKey) oldUsed.add(oldKey); if (newKey) newUsed.add(newKey);
      const before = oldSheets.get(oldKey), after = newSheets.get(newKey);
      result.push({ key, oldKey, newKey, before, after, title: before && after && before.name !== after.name ? before.name + " → " + after.name : (after?.name || before?.name || "工作表") });
    }
    (comparison.regions || []).forEach((region) => add(region.old_sheet_key, region.new_sheet_key));
    kinds.forEach((kind) => (comparison.groups?.[kind] || []).forEach((item) => { if (item.old && !item.new && oldUsed.has(item.old.sheet_key) || item.new && !item.old && newUsed.has(item.new.sheet_key)) return; add(item.old?.sheet_key, item.new?.sheet_key); }));
    oldSheets.forEach((sheet, key) => {
      if (oldUsed.has(key)) return;
      const matching = [...newSheets.entries()].find(([newKey, current]) => !newUsed.has(newKey) && current.name === sheet.name);
      add(key, matching?.[0]);
    });
    newSheets.forEach((sheet, key) => { if (!newUsed.has(key)) add(null, key); });
    const score = (pair) => kinds.reduce((sum, kind) => sum + (comparison.groups?.[kind] || []).filter((item) => item.old?.sheet_key === pair.oldKey || item.new?.sheet_key === pair.newKey).length, 0);
    return result.sort((left, right) => score(right) - score(left));
  }
  function renderMetrics(target, metrics = {}, contextUsage = {}, readingPlan = {}) {
    target.replaceChildren();
    const stats = readingPlan.stats || {};
    const available = (key) => typeof metrics[key] === "number" ? metrics[key] : typeof stats[key] === "number" ? stats[key] : null;
    const fields = [["reused_entries", "沿用条目"], ["reinterpreted_entries", "重新理解条目"], ["read_compare_seconds", "读取与比对", "秒"], ["model_seconds", "模型处理", "秒"], ["total_seconds", "处理耗时", "秒"], ["semantic_requests", "SDK 请求", "次"], ["submitted_message_chars", "提交字符"], ["changed_context_chars", "变化上下文字数"], ["clarification_count", "用户补答", "次"]];
    const cards = node("div", { className: "reading-metrics" });
    fields.forEach(([key, title, unit = ""]) => {
      const number = available(key); if (number === null) return;
      const displayed = unit === "秒" ? number.toFixed(3).replace(/0+$/, "").replace(/\.$/, "") : number.toLocaleString();
      cards.append(node("div", { className: "reading-metric" }, [node("span", { text: title }), node("strong", { text: displayed + (unit ? " " + unit : "") })]));
    });
    const steps = contextUsage.task_llm_steps ?? contextUsage.llm_steps;
    if (typeof steps === "number") cards.append(node("div", { className: "reading-metric" }, [node("span", { text: "模型步骤" }), node("strong", { text: steps })]));
    const tokens = contextUsage.task_tokens || contextUsage.last_turn_tokens || contextUsage.tokens;
    const tokenScope = contextUsage.task_tokens ? (contextUsage.task_token_measurement === "partial_history" ? "已测" : "本期") : "末次";
    if (tokens && typeof tokens === "object") {
      [["input", "SDK 输入 token"], ["output", "SDK 输出 token"], ["reasoning", "推理 token"]].forEach(([key, title]) => {
        if (typeof tokens[key] === "number") cards.append(node("div", { className: "reading-metric" }, [node("span", { text: tokenScope + " " + title }), node("strong", { text: tokens[key].toLocaleString() })]));
      });
      [["read", "缓存读取 token"], ["write", "缓存写入 token"]].forEach(([key, title]) => {
        if (typeof tokens.cache?.[key] === "number") cards.append(node("div", { className: "reading-metric" }, [node("span", { text: tokenScope + " " + title }), node("strong", { text: tokens.cache[key].toLocaleString() })]));
      });
    }
    const total = cards.childElementCount;
    const primary = node("div", { className: "reading-metrics reading-primary-metrics" });
    [...cards.children].forEach((card) => { if (["沿用条目", "重新理解条目", "处理耗时", "SDK 请求"].includes(card.firstElementChild.textContent)) primary.append(card); });
    if (primary.childElementCount) target.append(primary);
    if (cards.childElementCount) target.append(node("details", { className: "processing-details" }, [node("summary", { text: "处理详情" }), cards]));
    target.hidden = !total;
  }
  function render(target, { comparison, metrics = {}, contextUsage = {}, readingPlan = {} } = {}) {
    target.replaceChildren();
    if (!comparison) { renderMetrics(target, metrics, contextUsage, readingPlan); return; }
    target.hidden = false; target.classList.add("source-comparison");
    const oldSheets = sheetIndex(comparison.old_snapshot), newSheets = sheetIndex(comparison.new_snapshot);
    const pairs = sheetPairs(comparison, oldSheets, newSheets);
    let pairIndex = 0, activeKind = "", fullSheet = false, rowLimit = 100, columnPage = 0;
    const metricsTarget = node("div"); renderMetrics(metricsTarget, metrics, contextUsage, readingPlan);
    const heading = node("div", { className: "review-section-heading" }, [node("h3", { text: "原表变化" })]);
    const summary = node("div", { className: "source-change-filters", ariaLabel: "原表变化类型" });
    const sheetSelect = node("select", { ariaLabel: "对比工作表" });
    pairs.forEach((pair, index) => { const item = node("option", { text: pair.title }); item.value = String(index); sheetSelect.append(item); });
    const full = node("input", { type: "checkbox" });
    const controls = node("div", { className: "source-comparison-controls" }, [node("label", {}, ["工作表", sheetSelect]), node("label", { className: "checkbox-label" }, [full, " 查看整张工作表"])]);
    const panes = node("div", { className: "source-table-panes" });
    const pager = node("div", { className: "source-table-pager" });
    const detail = node("details", { className: "source-cell-detail" }, [node("summary", { text: "单元格与计算依据" }), node("div", { className: "source-cell-detail-body" })]);
    const note = node("p", { className: "source-comparison-note" });
    target.append(heading, metricsTarget, summary, controls, note, panes, pager, detail);
    function itemsForPair(kind, pair) {
      return (comparison.groups?.[kind] || []).filter((item) => item.old?.sheet_key === pair.oldKey || item.new?.sheet_key === pair.newKey);
    }
    function sourceDetail(cell, other, side, sheet, row) {
      const body = detail.lastElementChild; body.replaceChildren();
      const title = side + " · " + (sheet?.filename || "") + " / " + (sheet?.name || "") + " / " + (cell?.address || columnName(other?.column || 1) + row);
      body.append(node("strong", { text: title }));
      const data = { "表格显示": value(cell), "原始值": cell?.value ?? null, "数据类型": cell?.type ?? null, "数字格式": cell?.number_format ?? null,
        "公式": cell?.formula ?? null, "缓存值": cell?.cached_value ?? null, "备注": cell?.comment ?? null, "合并区域": sheet?.merged_ranges?.length ? sheet.merged_ranges.join("、") : null };
      const list = node("dl");
      Object.entries(data).filter(([, value]) => value !== null && value !== "").forEach(([name, item]) => list.append(node("dt", { text: name }), node("dd", { text: typeof item === "object" ? JSON.stringify(item) : String(item) })));
      body.append(list); detail.open = true;
    }
    function draw() {
      const pair = pairs[pairIndex];
      summary.replaceChildren(); panes.replaceChildren(); pager.replaceChildren();
      const counts = Object.fromEntries(kinds.map((kind) => [kind, (comparison.groups?.[kind] || []).length]));
      [["", "全部变化", kinds.reduce((sum, kind) => sum + counts[kind], 0)], ...kinds.map((kind) => [kind, labels[kind], counts[kind]])].forEach(([kind, label, count]) => {
        const button = node("button", { className: "source-change-filter" + (activeKind === kind ? " is-active" : ""), type: "button", text: label + " " + count });
        button.setAttribute("aria-pressed", String(activeKind === kind));
        button.addEventListener("click", () => { activeKind = kind; fullSheet = false; full.checked = false; rowLimit = 100; draw(); });
        summary.append(button);
      });
      if (!pair) { note.textContent = "本次文件暂未生成表格视图。"; return; }
      const oldRows = pair.before?.rows || [], newRows = pair.after?.rows || [];
      const beforeByRow = new Map(oldRows.map((row) => [row.row, row])), afterByRow = new Map(newRows.map((row) => [row.row, row]));
      const selectedKinds = activeKind ? [activeKind] : kinds;
      const selected = selectedKinds.flatMap((kind) => itemsForPair(kind, pair).map((item) => ({ ...item, kind })));
      const oldWanted = new Set(), newWanted = new Set();
      selected.forEach((item) => { if (item.old?.sheet_key === pair.oldKey) oldWanted.add(item.old.row); if (item.new?.sheet_key === pair.newKey) newWanted.add(item.new.row); });
      if (!activeKind) (comparison.regions || []).filter((region) => region.old_sheet_key === pair.oldKey || region.new_sheet_key === pair.newKey).forEach((region) => {
        (region.old_rows || []).forEach((row) => oldWanted.add(row.row)); (region.new_rows || []).forEach((row) => newWanted.add(row.row));
      });
      [oldWanted, newWanted].forEach((wanted, index) => {
        const rows = index ? newRows : oldRows, changed = [...wanted];
        changed.forEach((number) => { const position = rows.findIndex((row) => row.row === number); if (position >= 0) rows.slice(Math.max(0, position - 1), position + 2).forEach((row) => wanted.add(row.row)); });
        if (changed.length && rows[0]) wanted.add(rows[0].row);
      });
      const mapping = new Map();
      (comparison.row_matches || []).filter((item) => item.old?.sheet_key === pair.oldKey && item.new?.sheet_key === pair.newKey).forEach((item) => mapping.set(item.new.row, item.old.row));
      kinds.forEach((kind) => itemsForPair(kind, pair).forEach((item) => { if (item.old?.sheet_key === pair.oldKey && item.new?.sheet_key === pair.newKey) mapping.set(item.new.row, item.old.row); }));
      const noChanges = !kinds.some((kind) => counts[kind]);
      if (fullSheet || noChanges) { oldRows.forEach((row) => oldWanted.add(row.row)); newRows.forEach((row) => newWanted.add(row.row)); }
      const used = new Set(), rows = [];
      newRows.forEach((after) => {
        const before = beforeByRow.get(mapping.get(after.row));
        if (before) used.add(before.row);
        if (newWanted.has(after.row) || before && oldWanted.has(before.row)) rows.push({ before, after });
      });
      oldRows.forEach((before) => { if (oldWanted.has(before.row) && !used.has(before.row)) rows.push({ before, after: null }); });
      let columns = [...new Set(rows.flatMap((row) => [...(row.before?.cells || []), ...(row.after?.cells || [])].map((cell) => cell.column)))].sort((a, b) => a - b);
      if (!columns.length) columns = [1];
      const pages = Math.ceil(columns.length / 20); columnPage = Math.min(columnPage, pages - 1);
      const displayedColumns = columns.slice(columnPage * 20, (columnPage + 1) * 20), visible = rows.slice(0, rowLimit);
      note.textContent = noChanges ? "原表内容一致，沿用已确认解释。" : comparison.requires_full ? "本次按完整表格重新理解。" : "变化行与相关表头、相邻行并排显示。";
      ["上期原表", "本期原表"].forEach((side, sideIndex) => {
        const sheet = sideIndex ? pair.after : pair.before;
        const heading = node("header", {}, [node("strong", { text: side }), node("span", { text: sheet ? sheet.filename + " / " + sheet.name : "无对应工作表" })]);
        const table = node("table", { className: "source-data-table" + (displayedColumns.length <= 3 ? " is-compact" : "") });
        table.append(node("thead", {}, [node("tr", {}, [node("th", { text: "行" }), ...displayedColumns.map((column) => node("th", { text: columnName(column) }))])]));
        const body = node("tbody");
        visible.forEach((entry) => {
          const row = sideIndex ? entry.after : entry.before, otherRow = sideIndex ? entry.before : entry.after;
          const matching = selected.filter((item) => item.old?.row === entry.before?.row && item.old?.sheet_key === pair.oldKey || item.new?.row === entry.after?.row && item.new?.sheet_key === pair.newKey);
          const rowKinds = matching.map((item) => item.kind);
          const presentationColumns = new Set(matching.filter((item) => item.kind === "presentation").flatMap((item) => item.presentation_columns || []));
          const tr = node("tr", { className: rowKinds.map((kind) => "source-row-" + kind).join(" ") + (!rowKinds.length ? " source-row-context" : "") });
          const rowHead = node("th", { text: row?.row ?? "—", title: rowKinds.map((kind) => labels[kind]).join(" · ") }); tr.append(rowHead);
          displayedColumns.forEach((column) => {
            const cell = row?.cells?.find((item) => item.column === column), other = otherRow?.cells?.find((item) => item.column === column);
            const status = !row ? "empty" : !otherRow ? (sideIndex ? "added" : "deleted") : cellKey(cell) !== cellKey(other) ? "modified" : cellStyle(cell) !== cellStyle(other) || presentationColumns.has(column) ? "presentation" : "";
            const td = node("td", { className: status ? "source-cell-" + status : "" });
            if (cell || other) {
              const button = node("button", { type: "button", className: "source-cell-button", text: value(cell), title: (cell?.address || columnName(column) + (row?.row || "")) + " · " + (labels[status] || "查看单元格") });
              button.setAttribute("aria-label", side + " " + (cell?.address || columnName(column) + (row?.row || "")) + " " + value(cell));
              button.addEventListener("click", () => sourceDetail(cell, other, side, sheet, row?.row || otherRow?.row)); td.append(button);
            }
            tr.append(td);
          });
          body.append(tr);
        });
        table.append(body);
        const wrap = node("div", { className: "source-table-scroll" }, [table]);
        panes.append(node("section", { className: "source-table-pane" }, [heading, wrap]));
      });
      const scrolls = panes.querySelectorAll(".source-table-scroll");
      scrolls.forEach((scroll, index) => scroll.addEventListener("scroll", () => { if (scrolls[1 - index].scrollLeft !== scroll.scrollLeft) scrolls[1 - index].scrollLeft = scroll.scrollLeft; }));
      if (!rows.length) pager.append(node("span", { className: "muted-cell", text: "此工作表没有所选类型的变化。" }));
      if (rows.length > rowLimit) { const more = node("button", { className: "text-button", type: "button", text: "显示更多行（" + Math.min(rowLimit, rows.length) + " / " + rows.length + "）" }); more.addEventListener("click", () => { rowLimit += 100; draw(); }); pager.append(more); }
      if (pages > 1) {
        const previous = node("button", { className: "text-button", type: "button", text: "← 上一组列" }); previous.disabled = columnPage === 0; previous.addEventListener("click", () => { columnPage--; draw(); });
        const next = node("button", { className: "text-button", type: "button", text: "下一组列 →" }); next.disabled = columnPage === pages - 1; next.addEventListener("click", () => { columnPage++; draw(); });
        pager.append(previous, node("span", { text: "列 " + (columnPage + 1) + " / " + pages }), next);
      }
    }
    sheetSelect.addEventListener("change", () => { pairIndex = Number(sheetSelect.value); rowLimit = 100; columnPage = 0; draw(); });
    full.addEventListener("change", () => { fullSheet = full.checked; rowLimit = 100; draw(); });
    draw();
  }
  window.TokenSQLSourceComparison = { render, renderMetrics };
})();
