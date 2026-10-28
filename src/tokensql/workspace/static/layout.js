(() => {
  "use strict";
  const { $ } = window.TokenSQL;
  const grid = $("contentGrid");
  const agent = document.querySelector(".agent-panel");
  const horizontal = $("horizontalSplitter"), vertical = $("verticalSplitter");
  const defaults = { fraction: 66, composer: 160, collapsed: false, tab: "quotes" };
  let preference = { ...defaults };
  try { preference = { ...defaults, ...JSON.parse(localStorage.getItem("tokensql.layout.v23") || "{}") }; } catch (_) {}
  const clamp = (value, low, high) => Math.min(high, Math.max(low, Number.isFinite(Number(value)) ? Number(value) : low));
  function apply(save = true) {
    const width = grid.getBoundingClientRect().width;
    const desktop = window.matchMedia("(min-width: 901px)").matches;
    const minFraction = desktop ? Math.max(30, 36000 / width) : 30;
    const maxFraction = desktop ? Math.min(80, 100 * (width - 290) / width) : 80;
    preference.fraction = clamp(preference.fraction, minFraction, Math.max(minFraction, maxFraction));
    horizontal.setAttribute("aria-valuemin", String(Math.round(minFraction))); horizontal.setAttribute("aria-valuemax", String(Math.round(maxFraction)));
    const height = agent.getBoundingClientRect().height || grid.getBoundingClientRect().height;
    preference.composer = clamp(preference.composer, 140, Math.max(140, Math.min(360, height * .45)));
    grid.style.setProperty("--data-fraction", preference.fraction + "%");
    agent.style.setProperty("--composer-height", preference.composer + "px");
    grid.classList.toggle("chat-collapsed", Boolean(preference.collapsed));
    grid.dataset.activePanel = preference.tab === "chat" ? "chat" : "quotes";
    horizontal.setAttribute("aria-valuenow", String(Math.round(preference.fraction)));
    vertical.setAttribute("aria-valuenow", String(Math.round(preference.composer)));
    vertical.setAttribute("aria-valuemax", String(Math.round(Math.max(140, Math.min(360, height * .45)))));
    $("toggleChatButton").textContent = preference.collapsed ? "恢复聊天" : "收起聊天";
    $("showQuotesButton").setAttribute("aria-selected", String(preference.tab !== "chat"));
    $("showChatButton").setAttribute("aria-selected", String(preference.tab === "chat"));
    if (save) try { localStorage.setItem("tokensql.layout.v23", JSON.stringify(preference)); } catch (_) {}
  }
  function drag(handle, update) {
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault(); handle.setPointerCapture(event.pointerId); document.body.classList.add("resizing-layout");
      const move = (next) => { update(next); apply(false); };
      const stop = () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", stop); handle.removeEventListener("pointercancel", stop); document.body.classList.remove("resizing-layout"); apply(); };
      handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", stop); handle.addEventListener("pointercancel", stop);
    });
    handle.addEventListener("dblclick", () => { preference = { ...defaults }; apply(); });
  }
  drag(horizontal, (event) => { const bounds = grid.getBoundingClientRect(); preference.fraction = 100 * (event.clientX - bounds.left) / bounds.width; });
  drag(vertical, (event) => { const bounds = agent.getBoundingClientRect(); preference.composer = bounds.bottom - event.clientY - 16; });
  horizontal.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault(); preference.fraction = event.key === "Home" ? 30 : event.key === "End" ? 80 : preference.fraction + (event.key === "ArrowRight" ? 2 : -2); apply();
  });
  vertical.addEventListener("keydown", (event) => {
    if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    event.preventDefault(); preference.composer = event.key === "Home" ? 140 : event.key === "End" ? 360 : preference.composer + (event.key === "ArrowUp" ? 12 : -12); apply();
  });
  $("toggleChatButton").addEventListener("click", () => { preference.collapsed = !preference.collapsed; apply(); });
  $("resetLayoutButton").addEventListener("click", () => { preference = { ...defaults }; apply(); });
  $("showQuotesButton").addEventListener("click", () => { preference.tab = "quotes"; apply(); });
  $("showChatButton").addEventListener("click", () => { preference.tab = "chat"; preference.collapsed = false; apply(); });
  window.addEventListener("resize", () => apply(false));
  apply(false);
  window.TokenSQLLayout = { apply, get preference() { return { ...preference }; } };
})();
