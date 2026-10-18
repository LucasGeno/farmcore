/* Progressive enhancement only: native links/forms and all fourteen dates work without JS. */
(function () {
  'use strict';
  let currentRoot = null;
  let first = 0;
  let previousSelected = null;
  let size = 0;
  let focusAfterSwap = null;
  const userRequests = new WeakSet();
  const small = window.matchMedia('(max-width: 800px)');
  function page(root, requested) {
    const cells = [...root.querySelectorAll('[data-plan-cell]')];
    size = small.matches ? 3 : 7;
    first = Math.max(0, Math.min(Math.floor((cells.length - 1) / size) * size, requested));
    cells.forEach((cell, i) => { cell.hidden = i < first || i >= first + size; });
    const visible = cells.slice(first, first + size);
    const format = new Intl.DateTimeFormat('en-GB', {day: 'numeric', month: 'short', timeZone: 'UTC'});
    root.querySelector('[data-plan-range]').textContent = visible.map((cell) =>
      format.format(new Date(`${cell.dataset.day}T12:00:00Z`))).filter((_, i, a) => i === 0 || i === a.length - 1).join(' – ');
    root.querySelector('[data-plan-range-caption]').textContent = small.matches ? 'Three days, with room to read' : 'Your next seven days';
    root.querySelector('[data-plan-nav="previous"]').disabled = first === 0;
    root.querySelector('[data-plan-nav="next"]').disabled = first + size >= cells.length;
    root.querySelector('[data-plan-navigation]').hidden = false;
    root.querySelector('[data-plan-grid]').classList.add('is-paged');
  }
  function init() {
    const root = document.querySelector('[data-field-week]');
    if (!root || root === currentRoot) return;
    currentRoot = root;
    const cells = [...root.querySelectorAll('[data-plan-cell]')];
    const selected = root.dataset.selectedDay;
    const count = small.matches ? 3 : 7;
    if (selected !== previousSelected || focusAfterSwap?.day) first = Math.floor(cells.findIndex(c => c.dataset.day === selected) / count) * count;
    previousSelected = selected;
    page(root, first);
    if (focusAfterSwap) {
      const {id, day} = focusAfterSwap;
      const control = id ? document.getElementById(id) : cells.find(c => c.dataset.day === day)?.querySelector('a');
      control?.focus({preventScroll: true});
      if (day && small.matches) root.querySelector('#plan-detail').scrollIntoView({
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start'
      });
      focusAfterSwap = null;
    }
  }
  document.addEventListener('click', function (event) {
    const control = event.target.closest('[data-plan-nav]');
    if (!control) return;
    const root = control.closest('[data-field-week]');
    if (control.dataset.planNav === 'today') {
      page(root, 0);
      const today = root.querySelector('[data-plan-cell]');
      if (root.dataset.selectedDay !== today.dataset.day) today.querySelector('a').click();
    } else page(root, first + (control.dataset.planNav === 'next' ? size : -size));
  });
  document.addEventListener('htmx:beforeRequest', function (event) {
    const el = event.detail.elt;
    const root = el.closest('[data-field-week]');
    if (!root || el === root) return;
    userRequests.add(event.detail.xhr);
    root.setAttribute('aria-busy', 'true');
    root.querySelector('[data-plan-error]').hidden = true;
    const button = el.matches('form') ? el.querySelector('button') : null;
    const cell = el.closest('[data-plan-cell]');
    focusAfterSwap = button ? {id: button.id} : {day: cell?.dataset.day || new URL(el.href).searchParams.get('day')};
  });
  document.addEventListener('htmx:afterRequest', function (event) {
    if (!userRequests.has(event.detail.xhr)) return;
    const root = document.querySelector('[data-field-week]');
    root?.setAttribute('aria-busy', 'false');
    if (event.detail.successful) userRequests.delete(event.detail.xhr);
  });
  function requestError(event) {
    if (!userRequests.has(event.detail.xhr)) return;
    userRequests.delete(event.detail.xhr);
    const root = document.querySelector('[data-field-week]');
    if (root) root.querySelector('[data-plan-error]').hidden = false;
    focusAfterSwap = null;
  }
  ['htmx:responseError', 'htmx:sendError', 'htmx:timeout'].forEach(name =>
    document.addEventListener(name, requestError));
  document.addEventListener('htmx:afterSwap', init);
  small.addEventListener('change', function () {
    if (!currentRoot) return;
    const cells = [...currentRoot.querySelectorAll('[data-plan-cell]')];
    const count = small.matches ? 3 : 7;
    page(currentRoot, Math.floor(cells.findIndex(c => c.dataset.day === previousSelected) / count) * count);
  });
  init();
})();
