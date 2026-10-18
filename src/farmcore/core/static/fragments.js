/* Lifecycle of the shared fragment slot (core/_fragment.html `fragment_slot`).

   One delegated listener set on the document, installed once (base.html loads this file; a
   second copy returns at the guard). It reacts only to requests issued by a slot's own body or
   its own Retry button into that slot's body, bound by xhr at beforeSend (see `owned`), so htmx
   activity inside a loaded app fragment never marks its parent.

   - htmx:beforeSend (a request really leaves): busy, "Loading <label>".
   - htmx:afterRequest with detail.successful: loaded; hide the error; "<label> loaded". If the
     Retry button still holds focus (a keyboard/user retry and the user has not moved on), focus
     moves to the body (tabindex=-1), since the button is about to hide. Automatic loads never
     move focus.
   - htmx:responseError / htmx:sendError / htmx:timeout: one terminal failure per request (htmx
     2.0.4 fires afterRequest before the transport events and does not set `failed` on them, so
     afterRequest.failed alone is not enough): show "<label> could not load." and Retry,
     "<label> could not load" once.
   - htmx:sendAbort: an intentional teardown (a panel closing or a new parcel): no failure.
   - Events from a slot no longer in the document are ignored. To tear a slot down, abort via its
     wrapper: `hx-sync` keeps the in-flight xhr on `[data-fragment]`, so
     htmx.trigger(wrapper, "htmx:abort") cancels it (the body holds none).
   Text is set with textContent only; no server text is ever shown. */
(function () {
  "use strict";
  if (window.__farmcoreFragments) return;
  window.__farmcoreFragments = true;

  // Request ownership, bound once at htmx:beforeSend: xhr -> the slot parts. A request is
  // owned only when the element it is dispatched on (the true issuer: htmx sets detail.elt to
  // the receiving element on every dispatch, and rebroadcasts afterRequest from a surviving
  // ancestor when a nested issuer removed itself) is this slot's own body or Retry AND its
  // target is this slot's body. Terminal events are looked up by detail.xhr only, so a nested
  // request inside a loaded fragment, or its rebroadcast, never touches the slot. Deleting the
  // entry on the first terminal event makes the transition idempotent.
  var owned = new WeakMap();

  // Poll only the marked feed itself. Preserve keyboard focus and stop background work.
  document.addEventListener("htmx:beforeRequest", function (evt) {
    var el = evt.detail && evt.detail.elt;
    if (el && el.matches("[data-poll-feed]") &&
        (document.hidden || el.contains(document.activeElement))) evt.preventDefault();
  });

  // Focus may enter the feed while its poll is in flight. Skip this swap; the next poll retries.
  document.addEventListener("htmx:beforeSwap", function (evt) {
    var d = evt.detail || {};
    var target = d.target;
    if (target && d.requestConfig && d.requestConfig.elt === target &&
        target.matches("[data-poll-feed]") && target.contains(document.activeElement)) {
      evt.preventDefault();
    }
  });

  function child(slot, selector) {
    for (var i = 0; i < slot.children.length; i++) {
      if (slot.children[i].matches(selector)) return slot.children[i];
    }
    return null;
  }

  // The slot parts when `issuer` is that slot's own body or Retry, else null.
  function partsFor(issuer) {
    if (!issuer || !issuer.matches) return null;
    if (!issuer.matches("[data-fragment-body], [data-fragment-retry]")) return null;
    var slot = issuer.closest("[data-fragment]");
    if (!slot || !slot.isConnected) return null;
    var body = child(slot, "[data-fragment-body]");
    var error = child(slot, "[data-fragment-error]");
    var retry = error && error.querySelector("[data-fragment-retry]");
    if (issuer !== body && issuer !== retry) return null;
    return {
      slot: slot,
      body: body,
      error: error,
      retry: retry,
      issuer: issuer,
      text: error && error.querySelector("[data-fragment-error-text]"),
      status: child(slot, "[data-fragment-status]"),
      label: slot.getAttribute("data-fragment-label") || "",
    };
  }

  // The owned request's parts for a terminal event (once), or null: not ours, already
  // finished, or the slot has left the document.
  function finish(evt) {
    var xhr = evt.detail && evt.detail.xhr;
    var p = xhr && owned.get(xhr);
    if (!p) return null;
    owned.delete(xhr);
    return p.slot.isConnected ? p : null;
  }

  function say(p, text) {
    if (!p.status) return;
    // Clear first so the same words (a second failure) are announced again.
    p.status.textContent = "";
    setTimeout(function () {
      if (p.status.isConnected) p.status.textContent = text;
    }, 50);
  }

  function settle(p) {
    p.body.removeAttribute("aria-busy");
    if (p.retry) p.retry.removeAttribute("aria-disabled");
  }

  function sending(evt) {
    var d = evt.detail || {};
    if (!d.xhr || owned.has(d.xhr)) return;
    // evt.target, not detail.elt: the element this dispatch was made on, the true issuer.
    var p = partsFor(evt.target);
    if (!p || d.elt !== evt.target || d.target !== p.body) return;
    owned.set(d.xhr, p);
    p.slot.setAttribute("data-state", "loading");
    p.body.setAttribute("aria-busy", "true");
    p.body.hidden = false;
    if (p.retry && p.issuer === p.retry) {
      p.retry.setAttribute("aria-disabled", "true");
      if (p.text) p.text.hidden = true;
    }
    say(p, "Loading " + p.label);
  }

  function loaded(evt) {
    if (!evt.detail || evt.detail.successful !== true) return;
    var p = finish(evt);
    if (!p) return;
    var hadFocus = p.retry && document.activeElement === p.retry;
    settle(p);
    p.slot.setAttribute("data-state", "loaded");
    p.body.setAttribute("data-loaded", "1");
    if (p.error) p.error.hidden = true;
    if (p.text) p.text.hidden = false;
    if (hadFocus) p.body.focus();
    say(p, p.label + " loaded");
  }

  function failed(evt) {
    var p = finish(evt);
    if (!p) return;
    settle(p);
    p.slot.setAttribute("data-state", "error");
    // The skeleton would claim it is still loading; a loaded body keeps its content.
    if (!p.body.hasAttribute("data-loaded")) p.body.hidden = true;
    if (p.text) p.text.hidden = false;
    if (p.error) p.error.hidden = false;
    say(p, p.label + " could not load");
  }

  function aborted(evt) {
    var p = finish(evt);
    if (!p) return;
    settle(p);
    p.slot.removeAttribute("data-state");
  }

  document.addEventListener("htmx:beforeSend", sending);
  document.addEventListener("htmx:afterRequest", loaded);
  document.addEventListener("htmx:responseError", failed);
  document.addEventListener("htmx:sendError", failed);
  document.addEventListener("htmx:timeout", failed);
  document.addEventListener("htmx:sendAbort", aborted);
})();
