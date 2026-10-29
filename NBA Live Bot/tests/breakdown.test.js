const { test } = require('node:test');
const assert = require('node:assert/strict');
const { shotType, gameBreakdown } = require('../breakdown');
const { extractGame } = require('../data');
const { html } = require('../render');
const player = { id: '1', displayName: 'Test Player', team: { displayName: 'Test Team' } };
const play = (id, text, period = 1, extra = {}) => ({ id, text, period: { number: period },
  participants: [{ athlete: { id: '1' } }], type: { text: 'Jump Shot' },
  shootingPlay: true, scoringPlay: /makes/.test(text), pointsAttempted: /three point/.test(text) ? 3 : 2, ...extra });
const summary = (plays, period = 1, state = 'in') => ({ plays, header: { id: 'game', competitions: [{ status: { period, type: { state } } }] } });

test('shot types are exclusive and distance never turns a three into mid-range', () => {
  for (const [text, expected] of [
    ['Test Player makes driving dunk', 'dunk'], ['Test Player misses running layup', 'layup'],
    ['Test Player makes 8-foot floating jump shot', 'floater'], ['Test Player misses 24-foot three point jumper', 'three'],
    ['Test Player makes 10-foot jumper', 'shortMid'], ['Test Player misses 15-foot jumper', 'shortMid'],
    ['Test Player makes 16-foot jumper', 'longMid'], ['Test Player misses 9-foot jumper', 'paint'],
    ['Test Player makes jump hook shot', 'hook'], ['Test Player makes tip-in', 'tip'],
    ['Test Player misses two point shot', 'other'], ['Test Player makes free throw 1 of 2', 'ft'],
  ]) assert.equal(shotType(play('1', text)), expected, text);
  assert.equal(shotType(play('1', 'Test Player makes two point shot', 1, { type: { text: 'Running Finger Roll Layup' } })), 'layup');
});

test('quarter and shot totals count makes, misses, free throws and overtime', () => {
  const b = gameBreakdown(summary([
    play('a', 'Test Player makes dunk'), play('b', 'Test Player misses 15-foot jumper'),
    play('c', 'Test Player makes 24-foot three point jumper', 2),
    play('d', 'Test Player makes free throw 1 of 2', 5, { pointsAttempted: 1 }),
    play('e', 'Test Player misses free throw 2 of 2', 5, { pointsAttempted: 1 }),
  ], 5), player, { PTS: 6, FG: '2-3', '3PT': '1-1', FT: '1-2' });
  assert.equal(b.partial, false);
  assert.equal(b.periods[0].PTS, 2); assert.equal(b.periods[1].PTS, 3);
  assert.equal(b.periods[4].label, 'OT1'); assert.equal(b.periods[4].PTS, 1);
  assert.equal(b.totals.FGA, 3); assert.equal(b.totals.FTA, 2);
  assert.equal(b.shots.reduce((n, s) => n + s.points, 0), b.totals.PTS);
});

test('assists, steals, blocks and turnovers credit the actor, not the other participant', () => {
  const secondary = [{ athlete: { id: '2' } }, { athlete: { id: '1' } }];
  const plays = [
    play('a', 'Someone Else makes layup (Test Player assists)', 1, { participants: secondary }),
    play('b', 'Someone Else bad pass turnover (Test Player steals)', 1, { participants: secondary, shootingPlay: false, pointsAttempted: 0, type: { text: 'Bad Pass\nTurnover' } }),
    play('c', 'Someone Else misses layup (Test Player blocks)', 1, { participants: secondary }),
    play('d', 'Test Player defensive rebound', 1, { shootingPlay: false, pointsAttempted: 0, type: { text: 'Defensive Rebound' } }),
    play('e', 'Test Player turnover (Someone Else steals)', 1, { shootingPlay: false, pointsAttempted: 0, type: { text: 'Turnover' } }),
  ];
  const b = gameBreakdown(summary(plays), player, { PTS: 0, AST: 1, STL: 1, BLK: 1, REB: 1, TO: 1 });
  assert.equal(b.partial, false);
  assert.deepEqual([b.totals.AST, b.totals.STL, b.totals.BLK, b.totals.REB, b.totals.TO, b.totals.FGA], [1, 1, 1, 1, 1, 0]);
});

test('duplicate play IDs use the latest correction without counting twice', () => {
  const b = gameBreakdown(summary([play('same', 'Test Player makes dunk'), play('same', 'Test Player misses dunk')]), player, { PTS: 0, FG: '0-1' });
  assert.equal(b.totals.PTS, 0); assert.equal(b.totals.FGA, 1); assert.equal(b.partial, false);
});

test('final games without a header period retain all quarters and overtime', () => {
  const s = summary([play('q4', 'Test Player makes dunk', 4), play('ot', 'Test Player makes dunk', 5)], 1, 'post');
  delete s.header.competitions[0].status.period;
  const b = gameBreakdown(s, player, { PTS: 4, FG: '2-2' });
  assert.equal(b.current, 5); assert.equal(b.periods[3].PTS, 2); assert.equal(b.periods[4].PTS, 2);
  assert.equal(b.partial, false);
});

test('blocker-first descriptions and traveling turnovers are credited correctly', () => {
  const b = gameBreakdown(summary([
    play('b', 'Test Player blocks Someone Else\'s layup', 1, { participants: [{ athlete: { id: '2' } }, { athlete: { id: '1' } }] }),
    play('t', 'Test Player traveling turnover', 1, { type: { text: 'Traveling' }, shootingPlay: false, pointsAttempted: 0 }),
  ]), player, { PTS: 0, BLK: 1, TO: 1 });
  assert.equal(b.totals.BLK, 1); assert.equal(b.totals.TO, 1); assert.equal(b.totals.FGA, 0);
  assert.equal(b.partial, false);
});

test('team-only heaves are excluded only when official shooting counts confirm it', () => {
  const s = summary([play('h', 'Test Player misses heave jump shot', 1, { type: { text: 'Heave Jump Shot' }, pointsAttempted: 3 })]);
  const modern = gameBreakdown(s, player, { PTS: 0, FG: '0-0', '3PT': '0-0' });
  assert.equal(modern.excludedHeaves, 1); assert.equal(modern.totals.FGA, 0); assert.equal(modern.partial, false);
  const historical = gameBreakdown(s, player, { PTS: 0, FG: '0-1', '3PT': '0-1' });
  assert.equal(historical.excludedHeaves, 0); assert.equal(historical.totals.FGA, 1); assert.equal(historical.partial, false);
});

test('missing feeds, future quarters and incomplete totals are explicitly flagged', () => {
  assert.equal(gameBreakdown(summary([], 1, 'pre'), player).available, false);
  assert.equal(gameBreakdown(summary(undefined, 4, 'post'), player).available, false);
  const b = gameBreakdown(summary([play('a', 'Test Player makes dunk')]), player, { PTS: 20, FG: '8-12' });
  assert.equal(b.partial, true); assert.equal(b.periods[1].started, false);
  const markup = html({ player, state: 'in', face: 'back', breakdown: b });
  assert.match(markup, /Partial feed/); assert.match(markup, /<th>Q2<\/th><td>—/);
  const shotMarkup = html({ player, state: 'in', face: 'shots', breakdown: b });
  assert.match(shotMarkup, /Short mid-range/); assert.match(shotMarkup, /16\+ ft/);
  assert.doesNotMatch(markup, /<h2>HOW THEY SHOT/);
  assert.doesNotMatch(shotMarkup, /<table>/);
  const unavailable = html({ player, face: 'back', breakdown: gameBreakdown(summary([]), player) });
  assert.match(unavailable, /Play-by-play unavailable/);
});

test('game extraction attaches breakdown and both faces escape player text', () => {
  const model = extractGame(summary([play('a', 'Test Player makes dunk')]), { ...player, displayName: '<unsafe>' });
  assert.equal(model.breakdown.available, true);
  for (const face of ['front', 'quarters', 'shots']) {
    const markup = html({ ...model, face });
    assert.match(markup, /&lt;unsafe&gt;/); assert.doesNotMatch(markup, /<unsafe>/);
  }
});
