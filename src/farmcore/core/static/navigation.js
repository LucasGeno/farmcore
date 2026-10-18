/* Cold reading returns only. Native initial anchors and browser history own their position.
   Missing/late source anchors were observed in actual Home/List/Plan browser returns (ticket05).
   No router, request lifecycle, scroll snapshot, storage or history entry is introduced. */
(function () {
  "use strict";
  if (window.__farmcoreReturnPosition) return;
  window.__farmcoreReturnPosition = true;

  // A named return can live in a Field disclosure. Reveal that target alone before focus;
  // ordinary visits and browser-owned history keep their disclosure state.
  function reveal(target) {
    var opened = false;
    for (var parent = target && target.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS" && !parent.open) {
        parent.open = true;
        opened = true;
      }
    }
    return opened;
  }

  function start() {
    var navigation = window.performance && window.performance.getEntriesByType("navigation")[0];
    if (navigation && navigation.type === "back_forward") return;
    var url = new URL(window.location.href);
    var id = url.hash.slice(1);
    var family = null;
    if (url.pathname === "/" || url.pathname === "/today/") family = /^home-(map|parcel-[1-9][0-9]*)$/;
    else if (url.pathname === "/forecast/plan") family = /^plan-day-[0-9]{4}-[0-9]{2}-[0-9]{2}$/;
    else if (url.pathname === "/advisor/") family = /^advisor-parcel-[1-9][0-9]*$/;
    else if (url.pathname === "/fields/" && url.searchParams.get("view") === "list") family = /^parcel-[1-9][0-9]*$/;
    else if (url.pathname === "/fields/") family = /^map-panel-heading$/;
    else if (url.pathname === "/activity/" ||
        (/^\/fields\/parcels\/[1-9][0-9]*$/.test(url.pathname) && url.searchParams.get("view") === "activity")) family = /^activity-row-[1-9][0-9]*$/;
    if (!family || !family.test(id)) return;
    var initialTarget = document.getElementById(id);
    if (initialTarget && !reveal(initialTarget)) return; // Visible native anchor stays native.
    var pending = true;
    var scheduled = false;
    var terminalEvents = ["htmx:afterSettle", "htmx:afterRequest", "htmx:responseError",
      "htmx:sendError", "htmx:timeout", "htmx:sendAbort"];
    function cancel() {
      pending = false;
      terminalEvents.forEach(function (name) { document.removeEventListener(name, schedule); });
      document.removeEventListener("keydown", cancel, true);
      document.removeEventListener("pointerdown", cancel, true);
      window.removeEventListener("pagehide", cancel);
      window.removeEventListener("popstate", cancel);
      window.removeEventListener("pageshow", pageshow);
    }
    function pageshow(event) { if (event.persisted) cancel(); }
    function restore() {
      scheduled = false;
      if (!pending) return;
      if (window.location.href !== url.href ||
          (document.activeElement !== document.body && document.activeElement !== document.documentElement)) {
        cancel();
        return;
      }
      // Late anchors can precede nested initial slots and move while those slots grow.
      if (document.querySelector('[data-fragment-body][aria-busy="true"]')) return;
      var target = document.getElementById(id);
      if (!target) {
        target = document.querySelector("[data-return-heading]");
        if (!target) { cancel(); return; }
        window.history.replaceState(window.history.state, "", url.pathname + url.search);
      }
      cancel();
      reveal(target);
      target.focus({ preventScroll: true });
      var rect = target.getBoundingClientRect();
      var header = document.querySelector("header.sticky");
      var top = header ? Math.max(0, header.getBoundingClientRect().bottom) : 0;
      var nav = document.getElementById("bottom-nav");
      var navRect = nav && nav.getBoundingClientRect();
      var bottom = navRect && navRect.height > 0 ? Math.min(window.innerHeight, navRect.top) : window.innerHeight;
      if (rect.top < top || rect.bottom > bottom) {
        target.scrollIntoView({ block: "start", behavior: "instant" });
      }
    }
    function schedule() {
      if (!pending || scheduled) return;
      scheduled = true;
      window.requestAnimationFrame(restore);
    }
    terminalEvents.forEach(function (name) { document.addEventListener(name, schedule); });
    document.addEventListener("keydown", cancel, true);
    document.addEventListener("pointerdown", cancel, true);
    window.addEventListener("pagehide", cancel);
    window.addEventListener("popstate", cancel);
    window.addEventListener("pageshow", pageshow);
    schedule();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
