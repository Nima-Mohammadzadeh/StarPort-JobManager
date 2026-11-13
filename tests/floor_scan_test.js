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

// Review fix: with DataWedge's 50 ms inter-character delay a 24-character tag takes ~1.25 s to type, so
// keystrokes (not only Enter) must keep a burst open.
test('typing speed: two different tags typed back to back are refused', () => {
  const c = create();
  c.push(A, 0);                                        // tag 1 finished typing (Enter)
  for (let t = 50; t <= 1250; t += 50) { c.activity(t); assert.strictEqual(c.due(t), null); }
  c.push(B, 1250);                                     // tag 2 finished typing (Enter)
  assert.strictEqual(c.due(1649), null);
  assert.deepStrictEqual(c.due(1650), {kind: 'many', epcs: [A, B]});
});

test('typing speed: one tag still closes 400 ms after its Enter', () => {
  const c = create();
  c.push(A, 1250);
  assert.deepStrictEqual(c.due(1650), {kind: 'one', epc: A});
});

test('keystrokes with no open burst start nothing', () => {
  const c = create();
  c.activity(0); c.activity(50);
  assert.strictEqual(c.due(1000), null);
});
