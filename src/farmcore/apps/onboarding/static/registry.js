/* Public boundaries are suggestions; only the normal draft confirmation writes parcels. */
(function () {
  "use strict";
  var el = document.getElementById("registry-map");
  if (!el) return;
  var status = document.getElementById("registry-status");
  var load = document.getElementById("registry-load");
  var review = document.getElementById("registry-review");
  var options = document.getElementById("registry-options");
  var candidates = document.getElementById("registry-candidates");
  var selected = new Set();
  var inputs = new Map();
  if (!window.maplibregl) {
    status.textContent = "Map unavailable. Import a file instead.";
    load.disabled = true;
    return;
  }
  var styles = getComputedStyle(document.documentElement);
  var colour = "rgb(" + styles.getPropertyValue("--brand-900-rgb").trim().split(/\s+/).join(",") + ")";
  var map = new maplibregl.Map({
    container: el, center: [Number(el.dataset.lon), Number(el.dataset.lat)], zoom: 16,
    style: {version: 8, sources: {
      base: {type: "raster", tiles: [el.dataset.tileUrl], tileSize: 256, attribution: el.dataset.tileAttribution}
    }, layers: [{id: "base", type: "raster", source: "base"}]}
  });
  map.addControl(new maplibregl.NavigationControl());
  function change(id, checked) {
    if (checked) selected.add(id); else selected.delete(id);
    map.setFeatureState({source: "cap", id: id}, {selected: checked});
    inputs.get(id).checked = checked;
    review.disabled = selected.size === 0;
    status.textContent = selected.size + " parcels selected. Review before saving.";
  }
  map.on("load", function () {
    map.addSource("cap", {type: "geojson", data: {type: "FeatureCollection", features: []}});
    map.addLayer({id: "cap-fill", type: "fill", source: "cap", paint: {
      "fill-color": colour, "fill-opacity": ["case", ["boolean", ["feature-state", "selected"], false], 0.7, 0.15]
    }});
    map.addLayer({id: "cap-line", type: "line", source: "cap", paint: {"line-color": colour, "line-width": 2}});
    map.on("click", "cap-fill", function (event) {
      var id = event.features[0].id;
      if (inputs.has(id)) change(id, !selected.has(id));
    });
    load.disabled = false;
  });
  load.disabled = true;
  load.addEventListener("click", async function () {
    load.disabled = true; review.disabled = true;
    options.replaceChildren(); selected.clear(); inputs.clear();
    map.removeFeatureState({source: "cap"});
    map.getSource("cap").setData({type: "FeatureCollection", features: []});
    status.textContent = "Loading public parcels…";
    var bounds = map.getBounds();
    var bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].join(",");
    try {
      var url = new URL(el.dataset.url, window.location.origin);
      url.searchParams.set("bbox", bbox);
      var response = await fetch(url);
      if (!response.ok) throw new Error("Unavailable");
      var data = await response.json();
      candidates.value = data.candidates || "";
      map.getSource("cap").setData(data);
      data.features.forEach(function (feature) {
        var label = document.createElement("label");
        label.className = "block min-h-[48px] py-2";
        var input = document.createElement("input");
        input.type = "checkbox"; input.name = "selected"; input.value = feature.properties.source_ref;
        inputs.set(feature.id, input);
        input.addEventListener("change", function () { change(feature.id, input.checked); });
        label.append(input, document.createTextNode(" " + feature.properties.name + " · " +
          feature.properties.area_ha.toFixed(2) + " ha · " + (feature.properties.crop_code || "Unknown crop")));
        options.append(label);
      });
      status.textContent = data.message || "Tap your parcels or select their checkboxes.";
    } catch (_) {
      status.textContent = "Public registry unavailable. Import a file instead.";
    } finally { load.disabled = false; }
  });
})();
