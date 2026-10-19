/* HTMX mutations use the same server-bound nonce as ordinary POST forms. */
(function () {
  "use strict";
  document.addEventListener("htmx:configRequest", function (event) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    var path = new URL(event.detail.path, window.location.href);
    if (meta && path.origin === window.location.origin) {
      event.detail.headers["X-CSRF-Token"] = meta.content;
    }
  });
})();
