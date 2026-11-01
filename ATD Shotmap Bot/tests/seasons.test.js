const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeSeasons, selectSeasons, canCache } = require('../seasons');

test('2027 selects 2026-27 from the actual source catalog', () => {
  assert.deepEqual(selectSeasons(['2025-26', '2026-27', '1996-97'], 2027), ['2026-27']);
});
test('future newly published seasons need no code update', () => {
  assert.deepEqual(selectSeasons(['2026-27', '2027-28'], 2028), ['2027-28']);
});
test('ranges preserve newest-first order and accept either direction', () => {
  const catalog = ['2025-26', '2023-24', '2024-25', '2026-27'];
  const expected = ['2026-27', '2025-26', '2024-25'];
  assert.deepEqual(selectSeasons(catalog, 2025, 2027), expected);
  assert.deepEqual(selectSeasons(catalog, 2027, 2025), expected);
});
test('season errors report the source range instead of a fixed year limit', () => {
  assert.throws(() => selectSeasons(['1996-97', '2026-27'], 2028), /1997-2027/);
  assert.throws(() => selectSeasons([], 2027), /Could not read available seasons/);
});
test('duplicate dropdown values and non-season options are excluded', () => {
  assert.deepEqual(normalizeSeasons(['2026-27', '2026-27', '', 'all', '2026-09', '2025-26']), ['2026-27', '2025-26']);
});
test('ongoing season and ranges containing it bypass persistent image cache', () => {
  const november = new Date('2026-11-01T12:00:00Z');
  assert.equal(canCache(2027, null, november), false);
  assert.equal(canCache(2020, 2027, november), false);
  assert.equal(canCache(2026, null, november), true);
  assert.equal(canCache(2027, null, new Date('2027-03-01T12:00:00Z')), false);
  assert.equal(canCache(2027, null, new Date('2027-10-01T12:00:00Z')), true);
});
