/* Optional parcel photo. The disclosure fetches the frame; only this mounted frame requests
   tiles. Show the complete photo or keep the neutral boundary, never a partial mosaic. */
(function () {
  "use strict";
  if (window.__farmcoreAerial) return;
  window.__farmcoreAerial = true;
  var mounted = new WeakSet();
  var TIMEOUT_MS = 15000;

  function mount(frame) {
    if (mounted.has(frame)) return;
    mounted.add(frame);
    var tiles = Array.from(frame.querySelectorAll("[data-aerial-tile]"));
    var photo = frame.querySelector("[data-aerial-photo]");
    var status = frame.querySelector("[data-aerial-status]");
    var retry = frame.querySelector("[data-aerial-retry]");
    var run = 0;

    function start() {
      var generation = ++run;
      var remaining = tiles.length;
      var terminal = false;
      var listeners = [];
      frame.dataset.aerialState = "loading";
      frame.setAttribute("aria-busy", "true");
      photo.setAttribute("hidden", "");
      var restoreFocus = document.activeElement === retry;
      if (restoreFocus) frame.focus();
      retry.hidden = true;
      status.textContent = "Loading aerial photo…";

      function finish(ok) {
        if (terminal || generation !== run) return;
        terminal = true;
        clearTimeout(timer);
        listeners.forEach(function (entry) {
          entry.tile.removeEventListener("load", entry.load);
          entry.tile.removeEventListener("error", entry.error);
        });
        if (!frame.isConnected) return;
        frame.dataset.aerialState = ok ? "success" : "error";
        frame.setAttribute("aria-busy", "false");
        if (ok) photo.removeAttribute("hidden");
        retry.hidden = ok;
        if (!ok && restoreFocus && document.activeElement === frame) retry.focus();
        status.textContent = ok ? "North ↑ · Aerial photo loaded." :
          "Aerial photo unavailable. Showing the parcel outline.";
      }
      var timer = setTimeout(function () { finish(false); }, TIMEOUT_MS);
      tiles.forEach(function (tile) {
        var load = function () { if (--remaining === 0) finish(true); };
        var error = function () { finish(false); };
        listeners.push({ tile: tile, load: load, error: error });
        tile.addEventListener("load", load);
        tile.addEventListener("error", error);
      });
      tiles.forEach(function (tile) { tile.setAttribute("href", tile.dataset.src); });
      if (!tiles.length) finish(false);
    }
    retry.addEventListener("click", start);
    start();
  }
  function boot() { document.querySelectorAll("[data-parcel-aerial]").forEach(mount); }
  document.addEventListener("htmx:load", boot);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
