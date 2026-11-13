// Tests for the scan collector inside templates/floor.html (one tag at a time).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'templates', 'floor.html'), 'utf8');
const m = html.match(/\/\* scan-collector:start \*\/([\s\S]*?)\/\* scan-collector:end \*\//);
assert.ok(m, 'scan-collector markers not found in templates/floor.html');
const create = new Function(m[1] + '; return createScanCollector;')();

const A = '300000000000000000000001';
const B = '300000000000000000000002';

test('one tag after the burst window', () => {
  const c = create();
  assert.strictEqual(c.push(A, 0), true);
  assert.strictEqual(c.due(399), null);
  assert.deepStrictEqual(c.due(400), {kind: 'one', epc: A});
});

test('same tag repeated is one tag', () => {
  const c = create();
  c.push(A, 0); c.push(A, 100); c.push(A, 200);
  assert.deepStrictEqual(c.due(700), {kind: 'one', epc: A});
});

test('surrounding whitespace is trimmed', () => {
  const c = create();
  c.push(' ' + A + '\n', 0);
  assert.deepStrictEqual(c.due(400), {kind: 'one', epc: A});
});

test('two different tags are rejected, none used', () => {
  const c = create();
  c.push(A, 0); c.push(B, 100);
  assert.deepStrictEqual(c.due(500), {kind: 'many', epcs: [A, B]});
});

test('cooldown after an accepted scan', () => {
  const c = create();
  c.push(A, 0);
  assert.strictEqual(c.due(400).kind, 'one');
  assert.strictEqual(c.push(A, 1000), false);
  assert.strictEqual(c.push(A, 1900), true);
});

test('no cooldown after a rejected burst', () => {
  const c = create();
  c.push(A, 0); c.push(B, 50);
  assert.strictEqual(c.due(450).kind, 'many');
  assert.strictEqual(c.push(A, 500), true);
});

test('empty input is ignored', () => {
  const c = create();
  assert.strictEqual(c.push('  ', 0), false);
  assert.strictEqual(c.due(1000), null);
});
