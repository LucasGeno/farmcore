import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
// Import executes only pure discovery; no Playwright import/browser/network operation.
const { discoverParcelPages } = require('../../scripts/check_overflow.js');

test('parcel discovery covers workspace and same-parcel canonical page with explicit source farm', () => {
  const workspace = '/fields/parcels/42?view=protection&farm=7&return_to=%2Fadvisor%2F%3Ffarm%3D7';
  assert.deepEqual(discoverParcelPages(['/advisor/about', workspace]), [workspace, '/advisor/parcels/42?farm=7']);
});

test('older canonical-only UI remains discoverable without dropping its farm', () => {
  assert.deepEqual(discoverParcelPages(['/advisor/parcels/42?farm=7']), ['/advisor/parcels/42?farm=7']);
});

test('missing expected parcel or non-reading/foreign URL cannot silently remove coverage', () => {
  for (const hrefs of [[], ['/advisor/about'], ['https://foreign.invalid/fields/parcels/42'],
    ['/fields/parcels/42/aerial?farm=7'], ['/fields/parcels/42/outline.svg?farm=7']]) {
    assert.throws(() => discoverParcelPages(hrefs), /No parcel reading page/);
  }
});
