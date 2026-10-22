/* Execute the shipped controller through its DOM/MapLibre interfaces, no browser/network. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

async function fixture({ info = null, home = true, intercepted = false, insideDisclosure = false, rect = { left: 450, top: 280, right: 710, bottom: 500 } } = {}) {
  const frame = { left: 284, top: 300, right: 818, bottom: 580 };
  const popups = [];
  const handlers = new Map();
  const filters = [];
  const points = [];
  let map;
  const parcel = { id: 2, alias: 'P2', name: 'Synthetic parcel', label_point: [0, 0] };
  class Element {
    constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.dataset = {}; this.style = {};
      this.children = []; this.classList = { toggle() {}, remove() {} }; }
    addEventListener(name, callback) { (this.listeners ||= new Map()).set(name, callback); }
    removeEventListener(name, callback) { if (this.listeners?.get(name) === callback) this.listeners.delete(name); }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(name, value) { this[name] = value; }
    getBoundingClientRect() { return { ...frame, width: 534, height: 280 }; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    closest(selector) { return selector === 'section' ? section : null; }
  }
  const section = new Element();
  section.querySelector = (selector) => selector.startsWith('template[data-chip=') ?
    { content: { firstElementChild: { cloneNode: () => ({textContent: 'No action'}) } } } : null;
  const element = new Element();
  const disclosure = new Element('details');
  disclosure.open = false;
  element.parentElement = insideDisclosure ? disclosure : null;
  element.clientWidth = 933;
  element.isConnected = true;
  element.clientHeight = 280;
  element.dataset = { geojsonUrl: '/fields/parcels.geojson?farm=1',
    ...(info ? {layers: JSON.stringify([{key: 'advice', kind: 'parcels', url: '/advisor/layer.geojson'}]), colouring: 'advice'} : {}),
    ...(home ? { workspaceOrigin: '/?farm=1#home-map' } : {}) };
  class MapAdapter {
    constructor() { this.sources = {}; this.canvasWidth = element.clientWidth; map = this; }
    resize() { this.canvasWidth = element.clientWidth; }
    loaded() { return true; }
    once(name, callback) { handlers.set(name + ":once", callback); }
    on(name, layer, callback) { handlers.set(layer === 'parcel-fill' ? name : name + ':other', callback || layer); }
    addControl() {}
    addImage() {}
    addSource(key) { this.sources[key] = { setData() {} }; }
    getSource(key) { return this.sources[key]; }
    addLayer() {}
    setFilter(...args) { filters.push(args); }
    fitBounds() {}
    project() { return { x: 250, y: 200 }; }
    getCanvas() { return { style: {} }; }
  }
  class Bounds { extend() {} getSouthWest() { return [0, 0]; } getNorthEast() { return [1, 1]; } }
  class Marker { setLngLat() { return this; } addTo() { return this; } }
  class Popup {
    constructor(options) { this.options = options; this.offset = [0, 0]; popups.push(this); }
    setLngLat() { return this; }
    setDOMContent(content) { this.content = content; return this; }
    on(name, callback) { this[name] = callback; return this; }
    addTo() { return this; }
    getElement() { return { getBoundingClientRect: () => {
      // MapLibre's default top anchoring produced the recorded lower-edge clipping.
      const box = this.options.anchor === 'bottom' ? rect : { ...rect, top: 500, bottom: 720 };
      return { left: box.left + this.offset[0], right: box.right + this.offset[0],
        top: box.top + this.offset[1], bottom: box.bottom + this.offset[1] };
    } }; }
    setOffset(offset) { this.offset = offset; points.push(offset); return this; }
  }
  const maplibregl = { Map: MapAdapter, Popup, Marker, LngLatBounds: Bounds, NavigationControl: class {} };
  const document = { querySelectorAll: (selector) => selector === '[data-parcel-map]' ? [element] : [],
    getElementById: () => null, addEventListener() {}, dispatchEvent: () => !intercepted,
    createElement: (tag) => {
      const e = new Element(tag);
      if (tag === 'canvas') e.getContext = () => ({ beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, getImageData: () => ({}) });
      return e;
    }, createDocumentFragment: () => new Element(), createTextNode: (text) => ({ textContent: text }),
  };
  const window = { maplibregl, location: { origin: 'http://localhost' },
    matchMedia: () => ({ matches: true }), localStorage: { getItem: () => null }, console };
  vm.runInNewContext(readFileSync(new URL('../../../src/farmcore/apps/fields/static/map.js', import.meta.url), 'utf8'),
    { window, document, maplibregl, URL, console, setTimeout, clearTimeout,
      requestAnimationFrame: (fn) => fn(), CustomEvent: class { constructor(name, options) { this.name = name; this.detail = options.detail; } },
      fetch: async (url) => ({ ok: true, json: async () => ({ type: 'FeatureCollection', features: [
        { properties: url.includes('/advisor/') ? {...parcel, ...info} : parcel, geometry: { coordinates: [[[0, 0], [1, 1]]] } },
      ] }) }),
    });
  await new Promise(setImmediate);
  assert.ok(handlers.get('click'), 'Synthetic map must reach the real parcel click interface');
  handlers.get('click')({ features: [{ properties: parcel }], lngLat: [0, 0] });
  return { frame, popups, points, filters, element, disclosure, map, handlers };
}

test('Home popup header and CTA fit inside short map without camera movement', async () => {
  const f = await fixture();
  const [popup] = f.popups;
  const placed = popup.getElement().getBoundingClientRect();
  assert.ok(placed.top >= 300 && placed.bottom <= 580, JSON.stringify(placed));
  assert.ok(placed.left >= 284 && placed.right <= 818, JSON.stringify(placed));
  const link = popup.content.children.find((child) => child.tagName === 'A');
  assert.equal(link.textContent, 'Open parcel');
  assert.equal(new URL(link.href, 'http://localhost').pathname, '/fields/parcels/2');
  assert.equal(new URL(link.href, 'http://localhost').searchParams.get('return_to'), '/?farm=1#home-map');
  popup.close();
  assert.equal(f.filters.at(-1)[1][2], -1);
});

for (const rect of [
  { left: 200, top: 280, right: 460, bottom: 500 },
  { left: 700, top: 280, right: 960, bottom: 500 },
  { left: 450, top: 420, right: 710, bottom: 640 },
]) {
  test('Home popup clamps the measured edge without a second controller', async () => {
    const f = await fixture({ rect });
    const box = f.popups[0].getElement().getBoundingClientRect();
    assert.ok(box.left >= 284 && box.right <= 818 && box.top >= 300 && box.bottom <= 580,
      JSON.stringify(box));
    assert.equal(f.points.length, 1);
  });
}

test('fitting popup has no extra offset; non-Home popup keeps existing anchor policy', async () => {
  const fitting = await fixture({ rect: { left: 450, top: 330, right: 710, bottom: 550 } });
  assert.deepEqual(fitting.points, []);
  const other = await fixture({ home: false });
  assert.equal(other.popups[0].options.anchor, undefined);
  assert.deepEqual(other.points, []);
});

test('An intercepted parcel tap creates no popup and retains its selection ownership', async () => {
  const f = await fixture({ home: false, intercepted: true });
  assert.deepEqual(f.popups, []);
  assert.equal(f.filters.at(-1)[1][2], 2);
});


test('Home map resized while closed measures its current width when disclosure opens and cleans listener on removal', async () => {
  const f = await fixture({ insideDisclosure: true });
  f.element.clientWidth = 294;
  f.disclosure.open = true;
  f.disclosure.listeners?.get('toggle')?.({ target: f.disclosure });
  assert.equal(f.map.canvasWidth, 294);
  f.handlers.get('remove:once')?.();
  assert.equal(f.disclosure.listeners?.has('toggle'), false);
});

test('re-swapped Home map drops its disclosure listener instead of resizing a detached map', async () => {
  const f = await fixture({ insideDisclosure: true });
  f.element.isConnected = false;
  f.element.clientWidth = 294;
  f.disclosure.open = true;
  f.disclosure.listeners?.get('toggle')?.({ target: f.disclosure });
  assert.equal(f.map.canvasWidth, 933);
  assert.equal(f.disclosure.listeners?.has('toggle'), false);
});

for (const status of ['limited', 'unavailable']) {
  test(`Popup uses explicit ${status} text even when decision template says no action`, async () => {
    const f = await fixture({info: {decision: 'none', advised: true, assessment_status: status,
      label: 'Assessment ' + status, card_url: '/advisor/parcels/2'}});
    function text(el) { return (el.textContent || '') + (el.children || []).map(text).join(' '); }
    const words = text(f.popups[0].content);
    assert.ok(words.includes('Assessment ' + status), words);
    assert.ok(!words.includes('No action'), words);
  });
}
