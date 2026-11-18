// The first-run map: tap to place the farm, or use the browser's location. Coordinates are the
// only thing the form submits; the offline time-zone and country lookup happens on the server.
// When the map library fails, the coordinate inputs still work.
(function () {
  "use strict";

  var el = document.getElementById("location-map");
  var latitudeInput = document.getElementById("latitude");
  var longitudeInput = document.getElementById("longitude");
  var status = document.getElementById("location-status");
  var useButton = document.getElementById("use-my-location");
  if (!el || !latitudeInput || !longitudeInput) return;

  function say(text) {
    if (status) status.textContent = text;
  }

  function number(value) {
    var parsed = parseFloat(value);
    return isFinite(parsed) ? parsed : null;
  }

  var startLat = number(el.dataset.latitude);
  var startLon = number(el.dataset.longitude);
  var hasStart = startLat !== null && startLon !== null;

  function fill(lat, lon) {
    latitudeInput.value = lat.toFixed(6);
    longitudeInput.value = lon.toFixed(6);
    say("Point: " + latitudeInput.value + ", " + longitudeInput.value + ". Save to confirm.");
  }

  if (!window.maplibregl) {
    say("The map did not load. Type the coordinates or use this device's location.");
    if (useButton) useButton.disabled = false;
    return;
  }

  var tileUrl = el.dataset.tileUrl || "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  var attribution = el.dataset.tileAttribution || "&copy; OpenStreetMap contributors";
  var map = new maplibregl.Map({
    container: el,
    style: {
      version: 8,
      sources: {
        basemap: {
          type: "raster",
          tiles: [tileUrl],
          tileSize: 256,
          maxzoom: 19,
          attribution: attribution,
        },
      },
      layers: [{ id: "basemap", type: "raster", source: "basemap" }],
    },
    center: hasStart ? [startLon, startLat] : [2.4, 46.5],
    zoom: hasStart ? 13 : 5,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

  var marker = null;
  function mark(lat, lon, zoom) {
    if (marker) marker.setLngLat([lon, lat]);
    else marker = new maplibregl.Marker({ color: "#1f3a2b" }).setLngLat([lon, lat]).addTo(map);
    fill(lat, lon);
    if (zoom) map.flyTo({ center: [lon, lat], zoom: zoom });
  }

  if (hasStart) mark(startLat, startLon, 0);

  map.on("click", function (event) {
    mark(event.lngLat.lat, event.lngLat.lng, 0);
  });

  if (useButton) {
    useButton.addEventListener("click", function () {
      if (!navigator.geolocation) {
        say("This browser cannot share its location.");
        return;
      }
      say("Asking this device for its location…");
      navigator.geolocation.getCurrentPosition(
        function (position) {
          mark(position.coords.latitude, position.coords.longitude, 15);
        },
        function () {
          say("This device did not share its location. Tap the map or type coordinates.");
        },
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
      );
    });
  }
})();
