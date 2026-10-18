import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = new URL('../../src/farmcore/core/static/navigation.js', import.meta.url);

function fixture({ href = 'http://localhost/today/?farm=1#home-map', native = false,
  busy = true, additionalSlots = [], navigationType = 'navigate', headerBottom = 0, bottomTop = 0 } = {}) {
  const handlers = new Map();
  const frames = [];
  const targets = new Map();
  const moved = [];
  const replaced = [];
  const body = {};
  const heading = target('home-heading', 10);
  const location = new URL(href);
  const state = { fcPanelPushed: true, fromFarm: true, preserved: 3 };
  let loading = busy;
  function target(id, top = 900) {
    return { id, isConnected: true,
      focus: (options) => { moved.push(['focus', id, options]); document.activeElement = targets.get(id) || heading; },
      getBoundingClientRect: () => ({ top, bottom: top + 40 }),
      scrollIntoView: (options) => moved.push(['scroll', id, options]),
    };
  }
  function on(name, fn) { handlers.set(name, [...(handlers.get(name) || []), fn]); }
  function off(name, fn) { handlers.set(name, (handlers.get(name) || []).filter((f) => f !== fn)); }
  const document = { readyState: 'complete', body, documentElement: {}, activeElement: body,
    getElementById: (id) => id === 'bottom-nav' && bottomTop ?
      { getBoundingClientRect: () => ({ top: bottomTop, height: 64 }) } : targets.get(id) || null,
    querySelector: (selector) => selector === '[data-return-heading]' ? heading :
      selector === 'header.sticky' && headerBottom ? { getBoundingClientRect: () => ({ bottom: headerBottom }) } :
      selector.includes('aria-busy') && (loading || additionalSlots.some(slot => slot.busy)) ? {} : null,
    addEventListener: on, removeEventListener: off,
  };
  if (native) targets.set(location.hash.slice(1), target(location.hash.slice(1)));
  const window = { location, innerHeight: 800, document,
    performance: { getEntriesByType: () => [{ type: navigationType }] },
    history: { state, replaceState: (...args) => replaced.push(args),
      pushState: () => assert.fail('Return restoration must not push history') },
    addEventListener: on, removeEventListener: off,
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
  };
  vm.runInNewContext(readFileSync(source, 'utf8'), { window, document, URL,
    history: window.history, location, performance: window.performance,
    requestAnimationFrame: window.requestAnimationFrame });
  return { targets, target, moved, replaced, state, document,
    settle: () => { loading = false; },
    emit: (name, event = {}) => { for (const fn of [...(handlers.get(name) || [])]) fn(event); },
    flush: () => { while (frames.length) frames.shift()(); },
  };
}

test('late Home source anchor receives one instant restoration after its initial fragment', () => {
  const f = fixture();
  f.flush();
  assert.deepEqual(f.moved, []);
  f.targets.set('home-map', f.target('home-map'));
  f.settle();
  f.emit('htmx:afterSettle');
  f.flush();
  assert.deepEqual(f.moved.map((x) => x.slice(0, 2)), [['focus', 'home-map'], ['scroll', 'home-map']]);
  assert.equal(f.moved[0][2].preventScroll, true);
  assert.equal(f.moved[1][2].behavior, 'instant');
  f.emit('htmx:afterSettle');
  f.flush();
  assert.equal(f.moved.length, 2);
  assert.deepEqual(f.replaced, []);
});

test('missing Plan day focuses its labelled heading, removes only hash, preserves history state/query', () => {
  const f = fixture({ href: 'http://localhost/forecast/plan?day=2023-04-10&farm=2#plan-day-2023-04-10', busy: false });
  f.flush();
  assert.equal(f.moved[0]?.[0], 'focus');
  assert.equal(f.replaced.length, 1);
  assert.equal(f.replaced[0][0], f.state);
  assert.equal(f.replaced[0][2], '/forecast/plan?day=2023-04-10&farm=2');
});

test('missing Activity row waits for initial slot terminal state before heading fallback', () => {
  const f = fixture({ href: 'http://localhost/fields/parcels/2?view=activity&type=spray&farm=2#activity-row-9' });
  f.flush();
  assert.deepEqual(f.moved, []);
  f.settle();
  f.emit('htmx:afterRequest');
  f.flush();
  assert.equal(f.moved[0]?.[0], 'focus');
  assert.equal(f.replaced[0][2], '/fields/parcels/2?view=activity&type=spray&farm=2');
});

for (const event of ['keydown', 'pointerdown', 'pagehide', 'popstate', 'pageshow']) {
  test(`pending restoration cancels on ${event} before late content arrives`, () => {
    const f = fixture();
    f.flush();
    f.emit(event, { persisted: true });
    f.targets.set('home-map', f.target('home-map'));
    f.emit('htmx:afterSettle');
    f.flush();
    assert.deepEqual(f.moved, []);
    assert.deepEqual(f.replaced, []);
  });
}

test('Back/Forward navigation and existing focused controls retain browser ownership', () => {
  const back = fixture({ navigationType: 'back_forward', busy: false });
  back.flush();
  back.targets.set('home-map', back.target('home-map'));
  back.emit('htmx:afterSettle');
  back.flush();
  assert.deepEqual(back.moved, []);
  assert.deepEqual(back.replaced, []);
  const active = fixture();
  active.document.activeElement = { id: 'search' };
  active.targets.set('home-map', active.target('home-map'));
  active.emit('htmx:afterSettle');
  active.flush();
  assert.deepEqual(active.moved, []);
});

test('terminal initial provider failure permits missing-row heading fallback', () => {
  const f = fixture({ href: 'http://localhost/activity/?type=spray&farm=2#activity-row-9' });
  f.flush();
  f.settle();
  f.emit('htmx:timeout');
  f.flush();
  assert.equal(f.moved[0]?.[0], 'focus');
});

for (const href of [
  'http://localhost/forecast/plan?day=2024-04-10&farm=1#plan-day-2024-04-10',
  'http://localhost/activity/?type=spray&farm=1#activity-row-59',
  'http://localhost/fields/?view=list&q=Merlot&farm=1#parcel-2',
]) {
  test(`present SSR anchor stays native: ${new URL(href).pathname}`, () => {
    const f = fixture({ href, native: true, busy: false });
    f.flush();
    f.emit('htmx:afterSettle');
    f.flush();
    assert.deepEqual(f.moved, []);
    assert.deepEqual(f.replaced, []);
  });
}

test('late target hidden under sticky header is revealed once', () => {
  const f = fixture({ headerBottom: 64 });
  f.flush();
  f.targets.set('home-map', f.target('home-map', 30));
  f.settle();
  f.emit('htmx:afterSettle');
  f.flush();
  assert.deepEqual(f.moved.map((x) => x.slice(0, 2)), [['focus', 'home-map'], ['scroll', 'home-map']]);
});

test('late target obscured by phone bottom navigation is revealed once', () => {
  const f = fixture({ bottomTop: 736 });
  f.flush();
  f.targets.set('home-map', f.target('home-map', 730));
  f.settle();
  f.emit('htmx:afterSettle');
  f.flush();
  assert.equal(f.moved.filter((x) => x[0] === 'scroll').length, 1);
});


test('late Home anchor waits for nested initial fragments before measuring its final position', () => {
  const f = fixture();
  f.flush();
  let top = 400;
  const anchor = f.target('home-map');
  anchor.getBoundingClientRect = () => ({ top, bottom: top + 280 });
  f.targets.set('home-map', anchor);
  f.emit('htmx:afterSettle');
  f.flush();
  assert.deepEqual(f.moved, []);
  top = 850;
  f.settle();
  f.emit('htmx:afterRequest');
  f.flush();
  assert.deepEqual(f.moved.map((x) => x.slice(0, 2)), [['focus', 'home-map'], ['scroll', 'home-map']]);
  f.emit('htmx:afterSettle');
  f.flush();
  assert.equal(f.moved.length, 2);
});


test('closed deferred aerial cannot block a late Activity row after initial feed settles', () => {
  const aerial = { busy: false }; // HTTP test binds this to the actual unopened disclosure markup.
  const f = fixture({ href: 'http://localhost/fields/parcels/2?view=activity&farm=2#activity-row-9', additionalSlots: [aerial] });
  f.flush();
  assert.deepEqual(f.moved, []);
  f.targets.set('activity-row-9', f.target('activity-row-9'));
  f.settle();
  f.emit('htmx:afterSettle');
  f.flush();
  assert.deepEqual(f.moved.map(x => x.slice(0, 2)), [['focus', 'activity-row-9'], ['scroll', 'activity-row-9']]);
  assert.deepEqual(f.replaced, []);
});

test('an actively requested aerial still blocks measurement until its terminal state', () => {
  const aerial = { busy: true }; // fragments.js beforeSend makes deferred slots busy when requested.
  const f = fixture({ href: 'http://localhost/fields/parcels/2?view=activity&farm=2#activity-row-9', additionalSlots: [aerial] });
  f.targets.set('activity-row-9', f.target('activity-row-9'));
  f.settle();
  f.emit('htmx:afterRequest');
  f.flush();
  assert.deepEqual(f.moved, []);
  aerial.busy = false;
  f.emit('htmx:afterRequest');
  f.flush();
  assert.deepEqual(f.moved.map(x => x.slice(0, 2)), [['focus', 'activity-row-9'], ['scroll', 'activity-row-9']]);
});

test('explicit late Home map return opens only ancestor disclosures before focus', () => {
  const f = fixture();
  const outer = { tagName: 'DETAILS', open: false, parentElement: null };
  const inner = { tagName: 'DIV', parentElement: outer };
  const unrelated = { tagName: 'DETAILS', open: false };
  const anchor = f.target('home-map');
  anchor.parentElement = inner;
  anchor.focus = () => { assert.equal(outer.open, true); f.moved.push(['focus', 'home-map']); };
  f.targets.set('home-map', anchor);
  f.settle();
  f.emit('htmx:afterSettle');
  f.flush();
  assert.equal(outer.open, true);
  assert.equal(unrelated.open, false);
  assert.equal(f.moved[0][0], 'focus');
});
