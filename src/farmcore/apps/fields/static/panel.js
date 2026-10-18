/* The Map panel controller: Farm today, or one parcel's facts, beside the map (lg+) or in a
   bottom sheet above the phone navigation. fields/map.html renders the panel open on the server
   (`/fields/?parcel=<id>` works with no script); this file only makes it live:

   - `farmcore:parcel-tap` (map.js, unchanged): take it (preventDefault, so no MapLibre popup),
     push `?parcel=<id>` and load the parcel shell.
   - A list row's "Show on map" link: the same, and focus returns to that link on close.
   - Close (the X link), Escape, or Back: Farm today again, the map outline cleared through
     `farmcoreParcelMap.select(map, null)`, focus back on the invoker (else the map).
   - On open the panel heading takes focus; widening a phone to desktop with it still focused
     brings it back into view.
   - Every mount is fresh: the previous shell fetch is aborted and a late answer for an older
     selection is dropped (a counter), and the old slots' htmx requests are aborted through
     their wrappers before the old content leaves the document (`htmx:abort`, which
     core/static/fragments.js treats as teardown, not failure). Slot ids
     carry a per-mount nonce from the server, so A's response can never fill B's body.
   - The shell is fetched with the farm-qualified URLs the server put on the panel
     (`data-panel-farm-url`, `data-panel-parcel-url`), so `?farm=` always wins over the cookie.
   A failed shell fetch falls back to the plain page for that state. */
(function () {
  "use strict";
  if (window.__farmcorePanel) return;
  window.__farmcorePanel = true;

  var mount = 0; // the latest mount; an older answer is dropped
  var inflight = null; // AbortController of the shell fetch in flight
  var invoker = null; // where focus returns on close

  function panel() {
    return document.querySelector("[data-map-panel]");
  }
  function mapEl() {
    return document.getElementById("parcel-map") || document.querySelector("[data-parcel-map]");
  }
  function reducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }
  function current(p) {
    return p.dataset.parcelId || null;
  }

  function selectOnMap(id) {
    if (window.farmcoreParcelMap && window.farmcoreParcelMap.select) {
      window.farmcoreParcelMap.select(mapEl(), id);
    }
  }

  function pageUrl(id) {
    var url = new URL(window.location.href);
    var p = panel();
    var farm = p && new URL(p.dataset.panelFarmUrl, window.location.href).searchParams.get("farm");
    if (farm) url.searchParams.set("farm", farm);
    if (id == null) url.searchParams.delete("parcel");
    else url.searchParams.set("parcel", String(id));
    return url.pathname + url.search + url.hash;
  }

  // Abort the old mount's slot requests while they are still in the document. `hx-sync` keeps
  // the in-flight xhr on the slot wrapper (`closest [data-fragment]`), so the wrapper is the
  // abort owner; the issuers are aborted too in case a slot ever syncs on itself.
  function teardown(p) {
    if (!window.htmx) return;
    p.querySelectorAll(
      "[data-fragment], [data-fragment-body], [data-fragment-retry]"
    ).forEach(function (el) {
      window.htmx.trigger(el, "htmx:abort");
    });
  }

  // The phone fold: a `data-collapsed` state on the panel that CSS applies below lg only
  // (`max-lg:group-data-[collapsed]:hidden` on the body), never the `hidden` attribute, so a fold
  // made on a phone cannot hide the desktop panel's facts. The toggle is itself phone-only, so
  // its aria-expanded always describes what the phone shows, also after a resize round trip.
  function setCollapsed(p, collapsed) {
    var toggle = p.querySelector("[data-panel-toggle]");
    if (collapsed) p.setAttribute("data-collapsed", "");
    else p.removeAttribute("data-collapsed");
    if (toggle) {
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      var chevron = toggle.querySelector("svg");
      if (chevron) chevron.style.transform = collapsed ? "rotate(180deg)" : "";
    }
    // Recheck the selected parcel after the sheet has expanded/folded and laid out.
    requestAnimationFrame(function () { if (p.isConnected) selectOnMap(current(p)); });
  }

  function isSheet(p) {
    return window.getComputedStyle(p).position === "fixed"; // the phone bottom sheet
  }
  function headerBottom() {
    var header = document.querySelector("header.sticky");
    return header ? Math.max(0, header.getBoundingClientRect().bottom) : 0;
  }

  // Beside the map (lg), the heading can sit under the sticky header or below the fold: one
  // scroll of the map region (its scroll margin clears the header). The fixed sheet is always on
  // screen, so it never scrolls the page.
  function revealPanel(p, heading) {
    if (isSheet(p)) return;
    var r = heading.getBoundingClientRect();
    if (r.top >= headerBottom() && r.bottom <= window.innerHeight) return;
    var region = p.closest("[data-map-region]") || p;
    region.scrollIntoView({ block: "start", behavior: reducedMotion() ? "auto" : "smooth" });
  }

  function focusHeading(p) {
    var heading = p.querySelector("[data-panel-heading]");
    p.scrollTop = 0;
    if (!heading) return;
    heading.focus({ preventScroll: true });
    revealPanel(p, heading);
  }

  function load(id, focus) {
    var p = panel();
    if (!p) return;
    var my = ++mount;
    if (inflight) inflight.abort();
    var ctrl = new AbortController();
    inflight = ctrl;
    var url =
      id == null
        ? p.dataset.panelFarmUrl
        : p.dataset.panelParcelUrl.replace("{id}", encodeURIComponent(String(id)));
    p.setAttribute("aria-busy", "true");
    fetch(url, { headers: { Accept: "text/html" }, signal: ctrl.signal, credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (text) {
        if (my !== mount || !p.isConnected) return;
        inflight = null;
        teardown(p);
        var doc = new DOMParser().parseFromString(text, "text/html");
        p.replaceChildren.apply(p, Array.prototype.slice.call(doc.body.childNodes));
        p.dataset.parcelId = id == null ? "" : String(id);
        p.removeAttribute("aria-busy");
        setCollapsed(p, false);
        if (window.htmx) window.htmx.process(p);
        if (focus) focusHeading(p);
      })
      .catch(function (err) {
        if (my !== mount || (err && err.name === "AbortError")) return;
        p.removeAttribute("aria-busy");
        window.location.assign(pageUrl(id)); // the server renders this state with no script
      });
  }

  function open(id, opts) {
    var p = panel();
    if (!p || id == null || id === "") return;
    invoker = opts.invoker || null;
    selectOnMap(id);
    if (opts.push) {
      if (current(p) && history.state && history.state.fcPanelPushed) {
        // A -> B: one Back closes; keep whether the entry below is Farm today.
        history.replaceState(history.state, "", pageUrl(id));
      } else {
        // fromFarm: the entry below shows Farm today, so closing may step Back to it.
        history.pushState({ fcPanelPushed: true, fromFarm: !current(p) }, "", pageUrl(id));
      }
    }
    p.dataset.parcelId = String(id);
    load(id, true);
  }

  function returnFocus() {
    var target = invoker && invoker.isConnected ? invoker : mapEl();
    invoker = null;
    if (!target) return;
    if (target === mapEl() && !target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: false });
  }

  function close(fromHistory) {
    var p = panel();
    if (!p || !current(p)) return;
    if (!fromHistory && history.state && history.state.fcPanelPushed && history.state.fromFarm) {
      history.back(); // popstate closes, so Back and X leave the same history
      return;
    }
    if (!fromHistory) history.replaceState(null, "", pageUrl(null));
    selectOnMap(null);
    p.dataset.parcelId = "";
    load(null, false);
    returnFocus();
  }

  document.addEventListener("farmcore:parcel-tap", function (e) {
    if (!panel() || !e.detail) return;
    e.preventDefault(); // the panel shows the parcel: no MapLibre popup
    open(e.detail.id, { push: true, invoker: null });
  });

  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
      return;
    }
    var target = e.target instanceof Element ? e.target : null;
    if (!target || !panel()) return;
    var show = target.closest("a[data-show-on-map]");
    if (show) {
      e.preventDefault();
      open(show.dataset.parcelId, { push: true, invoker: show });
      // Phone: bring the map above the sheet into view now. Desktop scrolls once, when the
      // heading takes focus (revealPanel), so two smooth scrolls never compete.
      var map = mapEl();
      if (map && isSheet(panel())) {
        map.scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
      }
      return;
    }
    if (target.closest("a[data-panel-close]")) {
      e.preventDefault();
      close(false);
      return;
    }
    var toggle = target.closest("[data-panel-toggle]");
    if (toggle) setCollapsed(panel(), !panel().hasAttribute("data-collapsed"));
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    if (document.querySelector("dialog[open]")) return; // the More sheet closes itself first
    var p = panel();
    if (p && current(p)) close(false);
  });

  window.addEventListener("popstate", function () {
    var p = panel();
    if (!p) return;
    var id = new URL(window.location.href).searchParams.get("parcel");
    if (id) {
      if (id !== current(p)) open(id, { push: false, invoker: null });
    } else if (current(p)) {
      close(true);
    }
  });

  // Phone -> desktop with the heading still focused: the sheet becomes the side panel and the
  // page keeps its phone scroll, which can leave the focused heading off screen. Reveal it once
  // per crossing (only when it holds focus; the phone sheet never scrolls the page).
  var wide = window.matchMedia && window.matchMedia("(min-width: 1024px)");
  if (wide && wide.addEventListener) {
    wide.addEventListener("change", function (e) {
      var p = panel();
      var heading = p && p.querySelector("[data-panel-heading]");
      if (e.matches && heading && document.activeElement === heading) revealPanel(p, heading);
    });
  }

  // A server-rendered parcel (`?parcel=`): outline it once the map has its parcels.
  var p = panel();
  if (p && current(p)) selectOnMap(current(p));
})();
