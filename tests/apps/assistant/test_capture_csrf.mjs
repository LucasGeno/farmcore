import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { webcrypto } from 'node:crypto';
import vm from 'node:vm';

const source = new URL('../../../src/farmcore/apps/assistant/static/assistant.js', import.meta.url);

// DOM and fetch are platform boundaries. Run the actual shipped script through UI events.
function fixture({ messageUrl = '/assistant/message?farm=7', nonce = 'session-bound-nonce' } = {}) {
  class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.listeners = {}; this.dataset = {}; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
    get firstElementChild() { return this.children[0]; }
    querySelectorAll(selector) {
      const nodes = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
      return nodes.filter(node => selector === 'button' ? node.tag === 'button'
        : selector === 'button[type="submit"]' ? node.tag === 'button' && node.type === 'submit'
        : selector === '.fc-assistant-actions' && node.className === 'fc-assistant-actions');
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    setAttribute() {}
    focus() {}
    scrollIntoView() {}
  }
  const ids = Object.fromEntries(['assistant-launcher', 'assistant-dialog', 'assistant-form',
    'assistant-question', 'assistant-answer', 'assistant-status', 'assistant-new'].map(id => [id, new Element()]));
  const submit = new Element('button'); submit.type = 'submit';
  ids['assistant-form'].append(submit);
  ids['assistant-question'].value = 'I sprayed P1 copper 2 kg this morning';
  ids['assistant-status'].textContent = 'Ask your farm';
  ids['assistant-dialog'].dataset = { messageUrl, confirmUrl: '/assistant/confirm?farm=7', askUrl: '/assistant/ask?farm=7' };
  const calls = [];
  const document = { getElementById: id => ids[id], createElement: tag => new Element(tag),
    querySelector: selector => selector === 'meta[name="csrf-token"]' && nonce ? { content: nonce } : null };
  vm.runInNewContext(readFileSync(source, 'utf8'), { document, window: {}, URL, FormData,
    AbortController, crypto: webcrypto, location: new URL('https://farm.example/operations/?farm=7'),
    fetch: async (url, options) => {
      calls.push({ url: new URL(url), options });
      return { ok: true, json: async () => calls.length === 1
        ? { kind: 'draft', text: 'Preview', actions: [{ label: 'Confirm', action: 'confirm:42' }] }
        : { kind: 'done', text: '✅ Logged', links: [] } };
    } });
  return { ids, calls, submit: () => ids['assistant-form'].listeners.submit({ preventDefault() {} }),
    settle: () => new Promise(resolve => setImmediate(resolve)) };
}

test('member message and confirmation send the existing nonce with same-origin credentials', async () => {
  const f = fixture();
  f.submit(); await f.settle();
  assert.equal(f.calls.length, 1);
  const confirm = f.ids['assistant-answer'].querySelector('.fc-assistant-actions').querySelector('button');
  confirm.listeners.click(); await f.settle();
  assert.equal(f.calls.length, 2);
  for (const call of f.calls) {
    assert.equal(call.options.method, 'POST');
    assert.equal(call.options.credentials, 'same-origin');
    assert.equal(call.options.headers['X-CSRF-Token'], 'session-bound-nonce');
    assert.equal(call.url.origin, 'https://farm.example');
    assert.equal(call.url.searchParams.get('farm'), '7');
    assert.equal(call.url.href.includes('session-bound-nonce'), false);
  }
  assert.equal(f.calls[0].options.body.get('q'), 'I sprayed P1 copper 2 kg this morning');
  assert.equal(f.calls[1].options.body.get('action'), 'confirm:42');
});

test('a foreign mutation URL receives neither the nonce nor a request', async () => {
  const f = fixture({ messageUrl: 'https://foreign.example/assistant/message' });
  f.submit(); await f.settle();
  assert.equal(f.calls.length, 0);
  assert.match(f.ids['assistant-status'].textContent, /Could not read/);
});
