(() => {
  "use strict";
  const { $, api, toast, loadQuotes, loadCatalog } = window.TokenSQL;
  let context = null;
  let busy = false;
  function render(snapshot) {
    context = snapshot;
    window.dispatchEvent(new CustomEvent("tokensql:pricing-context", { detail: snapshot }));
    $("contextMeta").textContent = `参数版本 ${snapshot.revision} · 更新于 ${snapshot.updated_at || "未记录"}`;
    $("contextFx").value = JSON.stringify(snapshot.fx_rates || [], null, 2);
    $("contextFactors").value = JSON.stringify(snapshot.token_equivalences || [], null, 2);
  }
  async function open() {
    try {
      render(await api("/pricing-context")); $("contextError").textContent = ""; $("contextDialog").showModal();
    } catch (error) { toast(error.message, true); }
  }
  function setBusy(value) {
    busy = value; $("saveContextButton").disabled = value; $("refreshFxButton").disabled = value;
  }
  async function save() {
    if (busy || !context) return;
    setBusy(true); $("contextError").textContent = "";
    try {
      const fx = JSON.parse($("contextFx").value), factors = JSON.parse($("contextFactors").value);
      if (!Array.isArray(fx) || !Array.isArray(factors)) throw new Error("两组参数都必须是 JSON 数组");
      render(await api("/pricing-context", { method: "PUT", body: JSON.stringify({
        expected_revision: context.revision, fx_rates: fx, token_equivalences: factors, actor: "workspace-ui",
      }) }));
      await Promise.all([loadCatalog(), loadQuotes({ quiet: true })]);
      toast("参数已保存，前端显示已重新计算；原始报价与付款倍率未改变。");
    } catch (error) {
      $("contextError").textContent = error.status === 409 ? "参数版本已变化，请关闭后重新打开最新版本再编辑。" : error.message;
    } finally { setBusy(false); }
  }
  async function refreshFx() {
    if (busy || !context) return;
    setBusy(true); $("contextError").textContent = "";
    try {
      const snapshot = await api("/pricing-context/refresh-fx", { method: "POST", body: JSON.stringify({ expected_revision: context.revision }) });
      render(snapshot.context || snapshot);
      await Promise.all([loadCatalog(), loadQuotes({ quiet: true })]);
      toast("汇率来源与时效已更新。");
    } catch (error) { $("contextError").textContent = error.message; }
    finally { setBusy(false); }
  }
  $("contextButton").addEventListener("click", open);
  $("closeContextButton").addEventListener("click", () => $("contextDialog").close());
  $("saveContextButton").addEventListener("click", save);
  $("refreshFxButton").addEventListener("click", refreshFx);
})();
