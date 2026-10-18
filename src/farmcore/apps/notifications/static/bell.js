/* Browser-local read preference: no requests and no persistent farm changes. */
(function () {
  "use strict";
  if (window.__farmcoreNotifications) return;
  window.__farmcoreNotifications = true;
  function shut(feed, restoreFocus) {
    feed.open = false;
    if (restoreFocus) feed.querySelector("summary").focus();
  }
  document.addEventListener("toggle", function (event) {
    var feed = event.target;
    if (!feed.matches || !feed.matches("[data-notifications]") || !feed.open) return;
    var rows = Array.from(feed.querySelectorAll("[data-notification-key]"));
    var keys = rows.map(function (row) { return row.dataset.notificationKey; })
      .filter(function (key) { return /^[0-9a-f]{24}$/.test(key); }).slice(0, 100);
    document.cookie = feed.dataset.cookie + "=" + encodeURIComponent(keys.join(",")) +
      "; Path=/; SameSite=Lax" + (location.protocol === "https:" ? "; Secure" : "");
    var unread = 0;
    rows.forEach(function (row) {
      var read = keys.indexOf(row.dataset.notificationKey) !== -1;
      row.dataset.unread = read ? "false" : "true";
      row.querySelector("[data-bell-new]").hidden = read;
      if (!read && row.dataset.severity !== "info") unread += 1;
    });
    feed.dataset.unreadCount = String(unread);
    feed.querySelector("summary").setAttribute("aria-label", "Notifications, " + unread + " unread");
    var badge = feed.querySelector("[data-bell-badge]");
    badge.textContent = unread > 99 ? "99+" : String(unread);
    badge.hidden = unread === 0;
  }, true);
  document.addEventListener("keydown", function (event) {
    var feed = document.querySelector("[data-notifications][open]");
    if (feed && event.key === "Escape") { event.preventDefault(); shut(feed, true); }
  });
  document.addEventListener("click", function (event) {
    var feed = document.querySelector("[data-notifications][open]");
    if (!feed) return;
    if (event.target.closest("[data-bell-close]")) shut(feed, true);
    else if (!feed.contains(event.target)) shut(feed, feed.contains(document.activeElement));
  });
  function ownFeed(event) {
    var detail = event.detail || {};
    var feed = detail.elt || detail.target;
    return feed && feed.matches && feed.matches("[data-notifications]") ? feed : null;
  }
  function protectedState(feed) {
    return document.hidden || feed.open || feed.contains(document.activeElement);
  }
  document.addEventListener("htmx:beforeRequest", function (event) {
    var feed = ownFeed(event);
    if (feed && protectedState(feed)) event.preventDefault();
  });
  document.addEventListener("htmx:beforeSwap", function (event) {
    var feed = event.detail && event.detail.target;
    if (feed && feed.matches("[data-notifications]") && protectedState(feed)) event.preventDefault();
  });
  ["htmx:responseError", "htmx:sendError", "htmx:timeout"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      var feed = ownFeed(event);
      if (feed) feed.querySelector("[data-bell-refresh-error]").hidden = false;
    });
  });
})();
