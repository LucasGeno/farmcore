// Parcel map, mounted once per `[data-parcel-map]` container (the Fields page `#parcel-map` and
// the Today fragment share it). Basemaps: IGN orthophoto by default (Géoplateforme WMTS, "© IGN")
// and the OpenStreetMap plan, switched by the "Satellite / Plan" button, kept in localStorage.
// Parcels from /fields/parcels.geojson arrive neutral and are labelled with the alias only.
// Layer chips (the visible apps' `map_layers` in `data-layers`): the
// active "Colour by" layer (Spray advice: /advisor/layer.geojson) fills and outlines them in its
// `colour` per parcel id (spray adds a diagonal hatch); point layers (Weather) add markers. A tap
// outlines the parcel, rings its label and dispatches `farmcore:parcel-tap` ({id, alias,
// decision}); unless a listener calls preventDefault() (a Today bottom sheet), the popup with the
// name, the status chip (cloned from the fragment's <template data-chip>, the core
// `decision_chip`) and the advice-card link opens.
// Advisory fragment polls refresh the advice source in place after a Telegram confirmation.
(function () {
  "use strict";

  var STORE_KEY = "farmcore.basemap";
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // Used when a container has no data-basemaps (the plan alone, as before).
  var DEFAULT_BASEMAPS = {
    plan: {
      label: "Plan",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      maxzoom: 19,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
  };

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function readChoice() {
    try {
      return window.localStorage.getItem(STORE_KEY);
    } catch (e) {
      return null;
    }
  }

  function saveChoice(value) {
    try {
      window.localStorage.setItem(STORE_KEY, value);
    } catch (e) {
      /* private window or blocked storage: the choice lasts for this page only */
    }
  }

  function day(iso) {
    // "2024-04-10" -> "10 Apr 2024", the date format used across the app.
    var parts = (iso || "").split("-");
    if (parts.length !== 3) return iso;
    return parseInt(parts[2], 10) + " " + MONTHS[parseInt(parts[1], 10) - 1] + " " + parts[0];
  }

  function json(el, name, fallback) {
    try {
      return el.dataset[name] ? JSON.parse(el.dataset[name]) : fallback;
    } catch (e) {
      return fallback;
    }
  }

  // A 45° white hatch on a transparent tile; drawn over the spray fill so spray reads without colour.
  function hatchImage() {
    var size = 16; // 8 css px at pixelRatio 2
    var canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    var ctx = canvas.getContext("2d");
    ctx.strokeStyle = "rgba(255,255,255,0.7)";
    ctx.lineWidth = 3;
    [[0, size, size, 0], [-size / 2, size / 2, size / 2, -size / 2], [size / 2, size * 1.5, size * 1.5, size / 2]]
      .forEach(function (l) {
        ctx.beginPath();
        ctx.moveTo(l[0], l[1]);
        ctx.lineTo(l[2], l[3]);
        ctx.stroke();
      });
    return ctx.getImageData(0, 0, size, size);
  }

  function extend(bounds, coords) {
    if (typeof coords[0] === "number") bounds.extend(coords);
    else coords.forEach(function (c) { extend(bounds, c); });
  }

  function mount(el) {
    if (el.dataset.mapMounted) return;
    el.dataset.mapMounted = "1";
    var section = el.closest("section") || document;
    var status = section.querySelector("[data-map-status]") || document.getElementById("map-status");
    var toggle = section.querySelector("[data-basemap-toggle]");

    function say(text) {
      if (!status) return;
      status.textContent = text;
      status.hidden = false;
    }
    if (status) status.hidden = true; // a "did not load" line from a timed-out wait no longer holds

    // Start the parcel request now, not after every basemap tile has loaded.
    var parcels = fetch(el.dataset.geojsonUrl, { headers: { Accept: "application/geo+json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });

    var basemaps = json(el, "basemaps", DEFAULT_BASEMAPS);
    var names = Object.keys(basemaps); // JSON keys arrive sorted: name the default explicitly
    var choice = readChoice();
    if (names.indexOf(choice) < 0) choice = names.indexOf("satellite") >= 0 ? "satellite" : names[0];

    // Layer chips. The parcels arrive neutral (outline + alias); the active "Colour by"
    // layer's own GeoJSON sets `fc_colour` / `fc_hatch` per parcel id. Point layers add their
    // own markers. Each layer fetches its own URL and fails on its own chip.
    var layerDefs = json(el, "layers", []);
    var layerByKey = {};
    layerDefs.forEach(function (d) { layerByKey[d.key] = d; });
    var chipRow = section.querySelector("[data-map-chips]");
    var chipEls = {};
    if (chipRow) {
      chipRow.querySelectorAll("[data-layer-chip]").forEach(function (b) {
        if (layerByKey[b.dataset.layerChip]) chipEls[b.dataset.layerChip] = b;
      });
    }
    var activeKey = el.dataset.colouring || null;
    if (activeKey && !layerByKey[activeKey]) activeKey = null;
    var colouring = null; // {key, byId: parcel id -> the layer's properties}
    var baseData = null; // the neutral parcels from /fields/parcels.geojson
    var fetched = {}; // layer key -> Promise of its GeoJSON
    var colourVersion = 0; // ignore responses superseded by a chip change or refresh
    var refreshing = false;
    var markers = {}; // point layer key -> [maplibregl.Marker]
    var labelEls = {}; // parcel id -> its DOM label
    var relayoutLabels = null; // re-runs the label declutter once a label's text changes
    var refit = null; // frames the parcels and the shown points, once the parcels are in
    var NEUTRAL = "#fdfdf9"; // the surface token: a light outline on the photo, no decision fill
    var fill = ["coalesce", ["get", "fc_colour"], NEUTRAL];
    var fillOpacity = ["case", ["has", "fc_colour"], 0.78, 0.12];
    if (activeKey) fetchLayer(layerByKey[activeKey]); // start it with the parcels request
    var transition = { duration: reducedMotion() ? 0 : 300, delay: 0 };

    var sources = {};
    var layers = [];
    names.forEach(function (name) {
      var b = basemaps[name];
      sources[name] = {
        type: "raster",
        tiles: b.tiles,
        tileSize: 256,
        maxzoom: b.maxzoom || 19,
        attribution: b.attribution,
      };
      layers.push({
        id: name,
        type: "raster",
        source: name,
        layout: { visibility: name === choice ? "visible" : "none" },
      });
    });

    var map = new maplibregl.Map({
      container: el,
      style: { version: 8, sources: sources, layers: layers },
      // Demo farm area (Saint-Émilion appellation); fitBounds then frames the parcels exactly.
      center: [-0.2, 44.946],
      zoom: 13,
      fadeDuration: reducedMotion() ? 0 : 300,
    });
    // A hidden Field disclosure can retain the desktop canvas size through a phone resize.
    // Re-measure on reveal; MapLibre's existing resize handling refits the same parcel bounds.
    var disclosures = [];
    function forgetDisclosures() {
      disclosures.forEach(function (details) { details.removeEventListener("toggle", resizeOnOpen); });
    }
    function resizeOnOpen(event) {
      // A re-swapped map fragment never calls map.remove(); drop listeners of a detached map.
      if (!el.isConnected) return forgetDisclosures();
      if (event.target.open) map.resize();
    }
    for (var parent = el.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS") {
        parent.addEventListener("toggle", resizeOnOpen);
        disclosures.push(parent);
      }
    }
    if (disclosures.length) map.once("remove", forgetDisclosures);
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    // On a narrow map MapLibre's compact attribution opens expanded until the first drag and
    // covers the legend: fold it behind its (i) button, which shows "© IGN" on a tap.
    function foldAttribution() {
      var attrib = el.querySelector(".maplibregl-ctrl-attrib.maplibregl-compact-show");
      if (attrib) attrib.classList.remove("maplibregl-compact-show");
    }
    map.once("idle", foldAttribution);
    map.on("resize", function () { setTimeout(foldAttribution, 0); });

    function renderToggle() {
      if (!toggle) return;
      toggle.hidden = names.length < 2;
      // The name starts with the visible words ("Satellite / Plan", WCAG 2.5.3) and states the
      // layer once; no aria-pressed, which would be a second signal for the same state.
      toggle.setAttribute("aria-label", toggleWords + ", showing " + basemaps[choice].label);
      toggle.querySelectorAll("[data-basemap-option]").forEach(function (opt) {
        var on = opt.dataset.basemapOption === choice;
        opt.classList.toggle("underline", on);
        opt.classList.toggle("text-muted", !on);
      });
    }

    var toggleWords = toggle ? toggle.textContent.replace(/\s+/g, " ").trim() : "";

    function show(name) {
      choice = name;
      names.forEach(function (n) {
        if (map.getLayer(n)) map.setLayoutProperty(n, "visibility", n === name ? "visible" : "none");
      });
      renderToggle();
    }

    renderToggle();
    if (toggle) {
      toggle.addEventListener("click", function () {
        var next = names[(names.indexOf(choice) + 1) % names.length];
        show(next);
        saveChoice(next);
      });
    }

    // IGN tiles failing before any loaded: fall back to the plan, say so in one line.
    var satelliteOk = false;
    var fellBack = false;
    map.on("sourcedata", function (e) {
      if (e.sourceId === "satellite" && e.tile) satelliteOk = true;
    });
    map.on("error", function (e) {
      if (e.sourceId !== "satellite") {
        // A listener silences MapLibre's own logging: keep other errors visible.
        if (window.console) console.error(e.error || e);
        return;
      }
      if (satelliteOk || fellBack || !basemaps.plan) return;
      fellBack = true;
      show("plan");
      say("The satellite photo did not load; showing the plan map.");
    });

    // The status chip: the core decision_chip, rendered once per decision into <template>s
    // (only while Crop protection is on). Another colouring layer falls back to its own label.
    function chip(info) {
      if (info.assessment_status === "limited" || info.assessment_status === "unavailable") {
        var availability = document.createElement("span");
        availability.className = "font-semibold text-muted";
        availability.textContent = "Assessment " + info.assessment_status;
        return availability;
      }
      var key = info.advised === false ? "not-advised" : info.decision;
      var tpl = key ? section.querySelector('template[data-chip="' + key + '"]') : null;
      if (tpl) return tpl.content.firstElementChild.cloneNode(true);
      var span = document.createElement("span");
      span.className = "font-semibold";
      span.textContent = info.label || "";
      return span;
    }

    // Neutral priority bars: ranking only, independent of the Spray/Wait decision.
    // Map labels omit the visible word; popups show it just like lists and advice cards.
    function priorityBadge(info, compact) {
      var badge = document.createElement("span");
      badge.dataset.priority = String(info.priority);
      badge.setAttribute("role", "img");
      badge.setAttribute("aria-label", "priority " + info.priority + " of 3: " + info.priority_label);
      badge.className = "fc-priority";
      var bars = document.createElement("span");
      bars.className = "fc-priority-bars";
      bars.setAttribute("aria-hidden", "true");
      for (var n = 1; n <= 3; n++) {
        var bar = document.createElement("span");
        bar.dataset.priorityBar = String(n);
        bar.dataset.filled = String(n <= info.priority);
        bars.appendChild(bar);
      }
      badge.appendChild(bars);
      if (!compact) {
        var word = document.createElement("span");
        word.setAttribute("aria-hidden", "true");
        word.textContent = info.priority_label;
        badge.appendChild(word);
      }
      return badge;
    }

    function priorityFacts(info) {
      var box = document.createElement("div");
      box.className = "mt-1.5 flex flex-col items-start gap-1";
      box.dataset.popupPriority = "";
      box.appendChild(priorityBadge(info));
      if (info.priority_why) box.appendChild(line(info.priority_why, "text-[13px] leading-snug text-muted"));
      if (info.priority_assumption) {
        box.appendChild(line("Priority: assumption, agronomist to confirm.", "text-[13px] font-semibold text-wait"));
      }
      return box;
    }

    // The label on the map: the alias plus bars, with the word in the accessible name.
    function labelContent(alias, info) {
      var frag = document.createDocumentFragment();
      frag.appendChild(document.createTextNode(alias));
      if (info && info.priority) {
        frag.appendChild(priorityBadge(info, true));
      }
      return frag;
    }

    function updateLabels() {
      var byId = colouring ? colouring.byId : {};
      Object.keys(labelEls).forEach(function (id) {
        var feature = baseData && baseData.features.find(function (f) { return String(f.properties.id) === id; });
        if (feature) labelEls[id].replaceChildren(labelContent(feature.properties.alias, byId[id]));
      });
      if (relayoutLabels) relayoutLabels();
    }

    function line(text, cls) {
      var div = document.createElement("div");
      div.className = cls || "mt-1.5 text-muted";
      div.textContent = text;
      return div;
    }

    function popupContent(p) {
      // `p` is the neutral parcel; `info` the active colouring's facts for it (none when the
      // colouring app is off or its layer failed): no chip, no cover and no card link then.
      var info = colouring && colouring.byId[p.id];
      var root = document.createElement("div");
      root.className = "max-h-48 overflow-y-auto text-sm";
      root.appendChild(line(p.alias + " · " + p.name, "pr-6 font-semibold"));
      root.dataset.parcelPopup = p.id;
      root.appendChild(parcelPopupFacts(p, info));
      if (el.dataset.workspaceOrigin || (info && info.card_url)) {
        var link = document.createElement("a");
        if (el.dataset.workspaceOrigin) {
          var destination = new URL("/fields/parcels/" + encodeURIComponent(p.id), window.location.origin);
          var origin = new URL(el.dataset.workspaceOrigin, window.location.origin);
          destination.searchParams.set("farm", origin.searchParams.get("farm"));
          destination.searchParams.set("return_to", el.dataset.workspaceOrigin);
          link.href = destination.pathname + destination.search;
        } else link.href = info.card_url;
        link.className = "inline-flex min-h-[44px] items-center font-semibold text-brand-700 underline underline-offset-2";
        link.textContent = el.dataset.workspaceOrigin ? "Open parcel" : "Open advice card";
        link.setAttribute("aria-label", link.textContent + " for " + p.alias + " " + p.name);
        root.appendChild(link);
      }
      return root;
    }

    // Only non-interactive facts change: the popup, its link and keyboard focus stay put.
    function parcelPopupFacts(p, info) {
      var root = document.createElement("div");
      root.dataset.popupFacts = "";
      if (info) {
        var status = document.createElement("div");
        status.className = "mt-1.5";
        status.appendChild(chip(info));
        root.appendChild(status);
        if (info.priority) root.appendChild(priorityFacts(info));
        // The cover date only for a protected parcel: a spray/wait advice can still carry the
        // downy cover end (the sulphur-only pass), which must not read as an all-clear.
        if (info.decision === "protected" && info.protected_until) {
          root.appendChild(line("Protected until " + day(info.protected_until)));
        }
      }
      var last = info && "last_spray" in info ? info.last_spray : p.last_spray;
      root.appendChild(line("Last spray: " + (last ? day(last) : "none recorded")));
      return root;
    }

    function refreshParcelPopup() {
      el.querySelectorAll("[data-parcel-popup]").forEach(function (root) {
        var id = root.dataset.parcelPopup;
        var feature = baseData.features.find(function (f) { return String(f.properties.id) === id; });
        var old = root.querySelector("[data-popup-facts]");
        if (!feature || !old) return;
        var next = parcelPopupFacts(feature.properties, colouring && colouring.byId[id]);
        old.replaceChildren.apply(old, Array.from(next.childNodes));
      });
    }

    // --- layer chips ---------------------------------------------------------------------

    function fetchLayer(def) {
      if (!fetched[def.key]) {
        fetched[def.key] = fetch(def.url, { headers: { Accept: "application/geo+json" } })
          .then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
          })
          .catch(function (err) {
            delete fetched[def.key]; // a later tap on the chip tries again
            throw err;
          });
      }
      return fetched[def.key];
    }

    function chipNote(key, text) {
      var b = chipEls[key];
      if (!b) return;
      var note = b.querySelector("[data-chip-note]");
      if (note) {
        note.textContent = text ? "· " + text : "";
        note.hidden = !text;
      }
    }

    // A layer that did not load: said on its own chip (and its legend), never on the map-wide
    // status line; the base map, the parcels and the other layers keep working.
    function layerFailed(key, err) {
      var b = chipEls[key];
      if (b) {
        b.dataset.state = "error";
        b.setAttribute("title", (layerByKey[key] || {}).label + " did not load (" + err.message + ")");
        var note = b.querySelector("[data-chip-note]");
        // Points have no legend to say so: the chip itself says a tap retries.
        var kind = (layerByKey[key] || {}).kind;
        if (note) {
          note.textContent = kind === "points" ? "· did not load, tap to retry" : "· did not load";
          note.hidden = false;
        }
        if (b.scrollIntoView) b.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
      section.querySelectorAll('[data-legend-for="' + key + '"]').forEach(function (legend) {
        var ul = legend.querySelector("ul");
        var msg = legend.querySelector("[data-legend-note]");
        if (ul) ul.hidden = true;
        if (msg) { msg.textContent = "Could not load this layer. Tap the chip to try again."; msg.hidden = false; }
      });
    }

    function layerOk(key) {
      var b = chipEls[key];
      if (b) {
        delete b.dataset.state;
        b.removeAttribute("title");
      }
      chipNote(key, "");
      section.querySelectorAll('[data-legend-for="' + key + '"]').forEach(function (legend) {
        var ul = legend.querySelector("ul");
        var msg = legend.querySelector("[data-legend-note]");
        if (ul) ul.hidden = false;
        if (msg) msg.hidden = true;
      });
    }

    // The parcels with the active colouring merged in by id; without one they stay neutral.
    function repaint() {
      if (!baseData || !map.getSource("parcels")) return;
      var byId = colouring ? colouring.byId : {};
      map.getSource("parcels").setData({
        type: "FeatureCollection",
        features: baseData.features.map(function (f) {
          var props = {};
          Object.keys(f.properties).forEach(function (k) {
            if (k.indexOf("fc_") !== 0) props[k] = f.properties[k];
          });
          var info = byId[f.properties.id];
          if (info && info.colour) {
            props.fc_colour = info.colour;
            props.fc_hatch = info.hatch === true;
          }
          return { type: "Feature", id: f.id, geometry: f.geometry, properties: props };
        }),
      });
      updateLabels();
    }

    // "Colour by": exactly one parcels chip pressed. Switching clears the old colouring at once
    // (no stale colours while the new layer loads).
    function colourBy(key) {
      var version = ++colourVersion;
      activeKey = key;
      Object.keys(chipEls).forEach(function (k) {
        if (layerByKey[k].kind === "parcels") chipEls[k].setAttribute("aria-pressed", String(k === key));
      });
      section.querySelectorAll("[data-legend-for]").forEach(function (legend) {
        legend.hidden = legend.dataset.legendFor !== key;
      });
      colouring = null;
      repaint();
      if (!key) return;
      chipNote(key, "loading");
      fetchLayer(layerByKey[key])
        .then(function (data) {
          if (activeKey !== key || version !== colourVersion) return;
          var byId = {};
          (data.features || []).forEach(function (f) {
            var id = f.properties && f.properties.id != null ? f.properties.id : f.id;
            byId[id] = f.properties || {};
          });
          colouring = { key: key, byId: byId };
          layerOk(key);
          repaint();
        })
        .catch(function (err) {
          if (activeKey === key && version === colourVersion) layerFailed(key, err);
        });
    }

    // The visible, focus-preserving HTML polls are the clock for this refresh too.
    // The advice layer already contains the parcel facts; no map teardown or extra timer.
    el.__farmcoreRefreshAdvice = function () {
      if (!el.isConnected || document.hidden || !baseData || refreshing || !layerByKey.advice) return;
      delete fetched.advice;
      if (activeKey !== "advice") return; // keep a different colouring chosen by the user
      refreshing = true;
      var version = ++colourVersion;
      fetchLayer(layerByKey.advice).then(function (data) {
        if (!el.isConnected || activeKey !== "advice" || version !== colourVersion) return;
        var byId = {};
        (data.features || []).forEach(function (f) { byId[f.properties.id] = f.properties; });
        baseData = data;
        colouring = { key: "advice", byId: byId };
        layerOk("advice");
        repaint(); // setData preserves camera, selected outline, labels and open panel
        refreshParcelPopup();
      }).catch(function () {
        if (el.isConnected && activeKey === "advice" && version === colourVersion) {
          chipNote("advice", "refresh failed; retrying"); // retain the last good map
        }
      }).finally(function () { refreshing = false; });
    };

    function pointPopup(p) {
      var root = document.createElement("div");
      root.className = "text-sm";
      root.appendChild(line(p.name || "Weather station", "pr-6 font-semibold"));
      // A stale reading is never "Now"; the time carries its day ("Sun 06:00"), as on Today.
      var temp = p.t_c == null ? "—" : Math.round(p.t_c) + " °C";
      root.appendChild(line((p.stale ? "Last reading " : "Now ") + temp, "mt-1.5"));
      // Partial rain coverage: a lower bound, worded as the Today weather card words it.
      var rain = "rain readings unavailable";
      if (p.rain_48h_mm != null) {
        var partial = p.rain_48h_hours != null && p.rain_48h_hours < 48;
        rain = (partial ? "at least " : "") + p.rain_48h_mm.toFixed(1) + " mm";
        if (partial) rain += " · " + p.rain_48h_hours + "/48 h of rain readings";
      }
      root.appendChild(line("Rain last 48 h: " + rain, "mt-1"));
      var when = p.as_of_text || p.as_of;
      root.appendChild(line(p.stale ? "Stale: last reading " + when : "As of " + when));
      if (p.url) {
        var link = document.createElement("a");
        link.href = p.url;
        link.className = "inline-flex min-h-[44px] items-center font-semibold text-brand-700 underline underline-offset-2";
        link.textContent = "Open weather";
        root.appendChild(link);
      }
      return root;
    }

    function pointMarker(p, lngLat) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "fc-map-point";
      b.dataset.stale = String(p.stale === true);
      b.textContent = p.t_c == null ? "—" : Math.round(p.t_c) + "°";
      var label = (p.name || "Weather station") + ": "
        + (p.t_c == null ? "no temperature" : Math.round(p.t_c) + " °C") + ", open details";
      var popup = new maplibregl.Popup({ maxWidth: "260px", offset: 18 }).setDOMContent(pointPopup(p));
      var marker = new maplibregl.Marker({ element: b }).setLngLat(lngLat).setPopup(popup).addTo(map);
      b.setAttribute("aria-label", label); // after addTo: MapLibre writes "Map marker"
      // The button's own click is the one activation. MapLibre's Enter/Space keypress toggle runs
      // before the click the button fires anyway (two toggles: nothing opens), so it is stopped
      // here, the button default kept; the click stays off the map (its marker toggle and the
      // parcel under the station).
      b.addEventListener("keypress", function (e) {
        if (e.key === "Enter" || e.key === " " || e.code === "Space") e.stopImmediatePropagation();
      }, true);
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        marker.togglePopup();
      });
      return marker;
    }

    // A point layer: an independent toggle (off by default); its markers come and go alone.
    function togglePoints(key, on) {
      var b = chipEls[key];
      if (b) b.setAttribute("aria-pressed", String(on));
      (markers[key] || []).forEach(function (m) { m.remove(); });
      markers[key] = [];
      if (!on) {
        layerOk(key);
        return;
      }
      chipNote(key, "loading");
      fetchLayer(layerByKey[key])
        .then(function (data) {
          if (!b || b.getAttribute("aria-pressed") !== "true") return;
          layerOk(key);
          var features = (data.features || []).filter(function (f) {
            return f.geometry && f.geometry.type === "Point";
          });
          if (!features.length) chipNote(key, "no reading yet");
          (markers[key] || []).forEach(function (m) { m.remove(); });
          markers[key] = features.map(function (f) {
            return pointMarker(f.properties || {}, f.geometry.coordinates);
          });
          var view = map.getBounds();
          var outside = features.some(function (f) { return !view.contains(f.geometry.coordinates); });
          if (outside && refit) refit();
        })
        .catch(function (err) {
          // Switched off while loading: an unpressed chip is not marked as failed.
          if (b && b.getAttribute("aria-pressed") === "true") layerFailed(key, err);
        });
    }

    function wireChips() {
      if (!chipRow) return;
      chipRow.hidden = false;
      Object.keys(chipEls).forEach(function (key) {
        var def = layerByKey[key];
        chipEls[key].addEventListener("click", function () {
          var pressed = chipEls[key].getAttribute("aria-pressed") === "true";
          if (def.kind === "parcels") {
            // Exclusive: tapping the active one again only retries a failed load.
            if (!pressed || chipEls[key].dataset.state === "error") colourBy(key);
          } else if (def.kind === "points") {
            // A pressed chip whose load failed: one tap tries again rather than switching off.
            var retry = pressed && chipEls[key].dataset.state === "error";
            togglePoints(key, retry || !pressed);
          }
        });
      });
    }
    wireChips();

    function addParcels(data) {
      if (!data.features.length) {
        say("No parcels yet. Run farmcore seed to load the demo farm.");
        return;
      }
      baseData = data;
      map.addImage("spray-hatch", hatchImage(), { pixelRatio: 2 });
      // Neutral until a colouring layer answers: the base GeoJSON's own decision is never used,
      // so switching Crop protection off leaves no advice colours behind.
      map.addSource("parcels", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "parcel-fill",
        type: "fill",
        source: "parcels",
        paint: { "fill-color": fill, "fill-opacity": fillOpacity, "fill-color-transition": transition },
      });
      map.addLayer({
        id: "parcel-hatch",
        type: "fill",
        source: "parcels",
        filter: ["==", ["get", "fc_hatch"], true],
        paint: { "fill-pattern": "spray-hatch" },
      });
      map.addLayer({
        id: "parcel-casing",
        type: "line",
        source: "parcels",
        paint: { "line-color": "#ffffff", "line-width": 4, "line-opacity": 0.8 },
      });
      map.addLayer({
        id: "parcel-line",
        type: "line",
        source: "parcels",
        paint: { "line-color": fill, "line-width": 3, "line-color-transition": transition },
      });
      // The tapped parcel: a dark outline (the ink token), so the selection reads on any photo.
      map.addLayer({
        id: "parcel-selected",
        type: "line",
        source: "parcels",
        filter: ["==", ["get", "id"], -1],
        paint: { "line-color": "#1c2420", "line-width": 3.5 },
      });

      colourBy(activeKey); // paints neutral now, then the active layer once it answers
      var bounds = new maplibregl.LngLatBounds();
      var labels = {};
      data.features.forEach(function (f) {
        extend(bounds, f.geometry.coordinates);
        var label = document.createElement("div");
        label.className = "parcel-label";
        label.textContent = f.properties.alias; // the alias only; the name is in the popup
        labels[f.properties.id] = label;
        labelEls[f.properties.id] = label;
        new maplibregl.Marker({ element: label }).setLngLat(f.properties.label_point).addTo(map);
      });
      var selected = null;
      // DOM labels do not collide like a symbol layer. Place them in turn, the selected parcel
      // first, then P1..P10: a label that would overlap one already shown is nudged below,
      // above, right or left of its point (by its own size, so it stays by its parcel), and is
      // hidden only when every nudge collides too. The nudge uses the CSS `translate` property:
      // MapLibre owns the marker element's `transform`.
      var LABEL_NUDGES = [[0, 0], [0, 1], [0, -1], [1, 0], [-1, 0]];
      function declutter() {
        var shown = [];
        var order = data.features.map(function (f) { return f.properties.id; });
        order.sort(function (a, b) { return (b === selected) - (a === selected); });
        order.forEach(function (id) {
          var label = labels[id];
          label.style.visibility = "";
          label.style.translate = "";
          var base = label.getBoundingClientRect();
          var placed = LABEL_NUDGES.some(function (n) {
            var dx = n[0] * (base.width + 2), dy = n[1] * (base.height + 2);
            var r = {
              left: base.left + dx, right: base.right + dx,
              top: base.top + dy, bottom: base.bottom + dy,
            };
            var hit = shown.some(function (o) {
              return r.left < o.right && r.right > o.left && r.top < o.bottom && r.bottom > o.top;
            });
            if (hit) return false;
            label.style.translate = dx || dy ? dx + "px " + dy + "px" : "";
            shown.push(r);
            return true;
          });
          if (!placed) label.style.visibility = "hidden";
        });
      }
      // A phone sheet overlays the map without resizing it. Keep the selected parcel in
      // the unobscured area, at the current zoom; this runs on selection/framing, not polls.
      function revealSelected() {
        var region = el.closest("[data-map-region]");
        var panel = region && region.querySelector("[data-map-panel]");
        if (selected == null || !panel || getComputedStyle(panel).position !== "fixed") return;
        var feature = data.features.find(function (f) { return String(f.properties.id) === String(selected); });
        if (!feature) return;
        var rect = el.getBoundingClientRect();
        var header = document.querySelector("header.sticky");
        var margin = parseInt(el.dataset.fitPadding || "40", 10);
        var top = Math.max(rect.top, header ? header.getBoundingClientRect().bottom : 0) + margin;
        var bottom = Math.min(rect.bottom, panel.getBoundingClientRect().top) - margin;
        if (bottom <= top) return; // the map itself is off-screen
        var point = map.project(feature.properties.label_point);
        if (rect.top + point.y >= top && rect.top + point.y <= bottom) return;
        map.panBy([0, rect.top + point.y - (top + bottom) / 2], { duration: 0 });
      }
      function select(id) {
        selected = id == null ? null : id;
        map.setFilter("parcel-selected", ["==", ["get", "id"], id == null ? -1 : id]);
        Object.keys(labels).forEach(function (k) {
          labels[k].classList.toggle("is-selected", String(k) === String(id));
        });
        declutter();
        revealSelected();
      }
      // The outside select/clear seam (`farmcoreParcelMap.select`): the panel sets or clears
      // the outline; a selection asked for before the parcels arrived applies now.
      relayoutLabels = declutter;
      el.__farmcoreSelect = select;
      if (el.dataset.selectedParcel) select(Number(el.dataset.selectedParcel));
      var padding = parseInt(el.dataset.fitPadding || "40", 10);
      var legend = section.querySelector("[data-map-legend]");
      // Keep the parcels clear of the overlays: the basemap toggle (top left) and the legend
      // (bottom left) sit inside the map. Plain padding when the map is too short for both.
      // The frame also takes in the points of the layers switched on (the station may sit
      // outside the parcels).
      function fit() {
        var top = padding + (toggle && !toggle.hidden ? toggle.offsetHeight + 8 : 0);
        var bottom = padding + (legend ? legend.offsetHeight + 8 : 0);
        var room = el.clientHeight - top - bottom > 120;
        var pad = room ? { top: top, bottom: bottom, left: padding, right: padding } : padding;
        var target = new maplibregl.LngLatBounds(bounds.getSouthWest(), bounds.getNorthEast());
        Object.keys(markers).forEach(function (k) {
          markers[k].forEach(function (m) { target.extend(m.getLngLat()); });
        });
        map.fitBounds(target, { padding: pad, maxZoom: 16, duration: 0 });
        revealSelected();
      }
      refit = fit;
      fit();
      map.on("moveend", declutter);
      requestAnimationFrame(declutter);
      // A phone rotating or the window resizing: frame the parcels again.
      var resizeTimer = null;
      map.on("resize", function () {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(fit, 150);
      });

      map.on("click", "parcel-fill", function (e) {
        var p = e.features[0].properties;
        select(p.id);
        var info = colouring && colouring.byId[p.id];
        var tap = new CustomEvent("farmcore:parcel-tap", {
          detail: { id: p.id, alias: p.alias, decision: info ? info.decision : undefined },
          cancelable: true,
        });
        if (!document.dispatchEvent(tap)) return; // a listener (the Today sheet) took it
        var popup = new maplibregl.Popup(el.dataset.workspaceOrigin
          ? { maxWidth: "260px", anchor: "bottom" } : { maxWidth: "260px" })
          .setLngLat(e.lngLat)
          .setDOMContent(popupContent(p))
          .on("close", function () { select(null); })
          .addTo(map);
        if (el.dataset.workspaceOrigin) {
          // Auto anchoring can open below a tap and clip the CTA in Home's short preview.
          // Keep this one popup inside that map, without moving its camera or the map panel.
          var frame = el.getBoundingClientRect();
          var box = popup.getElement().getBoundingClientRect();
          var dx = Math.max(frame.left - box.left, Math.min(0, frame.right - box.right));
          var dy = Math.max(frame.top - box.top, Math.min(0, frame.bottom - box.bottom));
          if (dx || dy) popup.setOffset([dx, dy]);
        }
      });
      map.on("mouseenter", "parcel-fill", function () { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "parcel-fill", function () { map.getCanvas().style.cursor = ""; });
    }

    // Add the parcels as soon as the style is parsed: "load" waits for every basemap tile, so a
    // slow tile host would delay the parcels.
    var styleReady = new Promise(function (resolve) {
      var done = false;
      function once() {
        if (!done) { done = true; resolve(); }
      }
      if (map.loaded()) once();
      map.once("style.load", once);
      map.once("load", once);
    });
    Promise.all([parcels, styleReady])
      .then(function (results) { addParcels(results[0]); })
      .catch(function (err) {
        say("Could not load the parcels (" + err.message + ").");
      });
  }

  function mountAll(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var found = Array.prototype.slice.call(scope.querySelectorAll("[data-parcel-map]"));
    var legacy = document.getElementById("parcel-map");
    if (legacy && found.indexOf(legacy) < 0) found.push(legacy);
    found.forEach(function (el) {
      if (el.dataset.mapMounted) return;
      if (!window.maplibregl) {
        var status = (el.closest("section") || document).querySelector("[data-map-status]")
          || document.getElementById("map-status");
        if (status) {
          status.textContent = "The map library did not load; the parcel list below still shows every parcel.";
          status.hidden = false;
        }
        return;
      }
      mount(el);
    });
  }

  // Wait for the library only while it is genuinely still loading. Each maplibre <script> carries
  // data-maplibre and sets it to "loaded" / "failed" from its onload / onerror attributes: on
  // /fields/ (a blocking head script) and in an htmx-inserted fragment (async=false) the result is
  // already known when this runs, so a failed CDN shows the fallback line at once. A tag with no
  // result yet gets listeners plus a timeout, in case its events already fired unseen.
  var LIB_WAIT_MS = 10000;
  function start() {
    if (window.maplibregl) return mountAll(document);
    var pending = Array.prototype.filter.call(
      document.querySelectorAll('script[src*="maplibre-gl"]'),
      function (s) { return !s.dataset.maplibre; }
    );
    if (!pending.length) return mountAll(document);
    // mountAll is idempotent: a late load after the timeout still mounts (and clears the line).
    function go() { mountAll(document); }
    pending.forEach(function (lib) {
      lib.addEventListener("load", go, { once: true });
      lib.addEventListener("error", go, { once: true });
    });
    setTimeout(go, LIB_WAIT_MS);
  }

  // Select (an id) or clear (null) the outlined parcel of one mounted map from outside, e.g. the
  // Map panel on close, Escape or Back. Before the parcels have loaded it is remembered on
  // the element and applied once they arrive.
  function selectParcel(el, id) {
    if (!el || !el.dataset) return;
    var value = id == null || id === "" ? null : Number(id);
    if (value == null || isNaN(value)) delete el.dataset.selectedParcel;
    else el.dataset.selectedParcel = String(value);
    if (el.__farmcoreSelect) el.__farmcoreSelect(value != null && !isNaN(value) ? value : null);
  }

  window.farmcoreParcelMap = { mountAll: mountAll, select: selectParcel };
  if (!window.__farmcoreParcelMapListening) {
    window.__farmcoreParcelMapListening = true;
    document.addEventListener("htmx:afterSwap", function (e) {
      var issuer = e.detail && e.detail.requestConfig && e.detail.requestConfig.elt;
      if (!issuer || !issuer.matches ||
          !issuer.matches('[data-poll-feed]#today, [data-poll-feed][data-map-section^="advisor-"]')) return;
      document.querySelectorAll("[data-parcel-map]").forEach(function (el) {
        if (el.__farmcoreRefreshAdvice) el.__farmcoreRefreshAdvice();
      });
    });
    document.addEventListener("htmx:load", function (e) {
      if (window.maplibregl) mountAll(e.target);
    });
  }
  start();
})();
