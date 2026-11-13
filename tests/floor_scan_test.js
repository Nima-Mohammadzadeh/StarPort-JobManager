// Tests for the scan collector inside templates/floor.html (one tag at a time).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'templates', 'floor.html'), 'utf8');
const m = html.match(/\/\* scan-collector:start \*\/([\s\S]*?)\/\* scan-collector:end \*\//);
assert.ok(m, 'scan-collector markers not found in templates/floor.html');
const {createScanCollector: create, createAutoSubmit} = new Function(m[1] + '; return {createScanCollector, createAutoSubmit};')();

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

// Scan-first: a different tag is accepted straight after a used scan; only the same tag waits out the cooldown.
test('different tag is accepted straight after a used scan', () => {
  const c = create();
  c.push(A, 0);
  assert.strictEqual(c.due(400).kind, 'one');
  assert.strictEqual(c.push(B, 500), true);
  assert.deepStrictEqual(c.due(900), {kind: 'one', epc: B});
});

// Auto-submit: 24 hex characters then 150 ms of quiet counts as a scan, no Enter needed.
test('auto: 24 hex then 150 ms of quiet submits once', () => {
  const a = createAutoSubmit();
  a.input(A, 0);
  assert.strictEqual(a.due(149), null);
  assert.strictEqual(a.due(150), A);
  assert.strictEqual(a.due(151), null);
});

test('auto: a partial value never submits', () => {
  const a = createAutoSubmit();
  a.input(A.slice(0, 12), 0);
  assert.strictEqual(a.due(5000), null);
});

test('auto: typing resets the timer', () => {
  const a = createAutoSubmit();
  a.input(A, 0); a.input(A, 100);
  assert.strictEqual(a.due(249), null);
  assert.strictEqual(a.due(250), A);
});

test('auto: lowercase hex counts, 25 characters and non-hex do not', () => {
  let a = createAutoSubmit();
  a.input(A.replace(/0/g, 'a'), 0);
  assert.strictEqual(a.due(150), A.replace(/0/g, 'a'));
  a = createAutoSubmit(); a.input(A + '0', 0);
  assert.strictEqual(a.due(1000), null);
  a = createAutoSubmit(); a.input('Z'.repeat(24), 0);
  assert.strictEqual(a.due(1000), null);
});

test('auto: a value that stops matching is forgotten', () => {
  const a = createAutoSubmit();
  a.input(A, 0); a.input('', 50);
  assert.strictEqual(a.due(1000), null);
});

// Review fix: with no Enter, anything that is not a clean 24-hex read must not sit in the field forever.
test('stale: the same tag typed twice back to back is handed over as two tags', () => {
  const a = createAutoSubmit();
  a.input(A + A, 0);
  assert.strictEqual(a.due(5000), null);                     // not exactly 24 hex: never auto-submitted
  assert.deepStrictEqual(a.stale(999), null);
  assert.deepStrictEqual(a.stale(1000), [A, A]);
  assert.strictEqual(a.stale(2000), null);
});

test('stale: two different tags typed back to back are handed over as two tags', () => {
  const a = createAutoSubmit();
  a.input(A + B, 0);
  assert.deepStrictEqual(a.stale(1000), [A, B]);
});

test('stale: a partial value is handed over whole after a second so the server can explain it', () => {
  const a = createAutoSubmit();
  a.input(A.slice(0, 12), 0);
  assert.strictEqual(a.stale(999), null);
  assert.deepStrictEqual(a.stale(1000), [A.slice(0, 12)]);
});

test('stale: typing keeps it waiting, an exact 24-hex value is left to due(), a cleared value is forgotten', () => {
  let a = createAutoSubmit();
  a.input('30', 0); a.input('3000', 800);
  assert.strictEqual(a.stale(1700), null);
  assert.deepStrictEqual(a.stale(1800), ['3000']);
  a = createAutoSubmit(); a.input(A, 0);
  assert.strictEqual(a.stale(5000), null);
  assert.strictEqual(a.due(5000), A);
  a = createAutoSubmit(); a.input('3000', 0); a.input('', 100);
  assert.strictEqual(a.stale(5000), null);
});
