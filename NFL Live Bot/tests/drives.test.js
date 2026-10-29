const { test } = require('node:test');
const assert = require('node:assert/strict');
const { gameDrives } = require('../drives');
const player = { id: '1', displayName: 'Test Player', team: { id: '10' } };
function summary({ state = 'in', drives = [], linescoresAway = [], linescoresHome = [], scoreAway = '0', scoreHome = '0' } = {}) {
  return {
    header: { competitions: [{ status: { type: { state } }, competitors: [
      { team: { id: '20' }, homeAway: 'away', score: scoreAway, linescores: linescoresAway.map(v => ({ displayValue: String(v) })) },
      { team: { id: '10' }, homeAway: 'home', score: scoreHome, linescores: linescoresHome.map(v => ({ displayValue: String(v) })) },
    ] }] },
    drives: drives.length ? { previous: drives } : undefined,
  };
}
function drive(overrides = {}) {
  return { start: { period: { number: 1 }, text: 'DAL 25' }, timeElapsed: { displayValue: '2:15' }, yards: 55, isScore: true,
    offensivePlays: 7, team: { id: '10', abbreviation: 'DAL' }, result: 'TD', shortDisplayResult: 'TD', displayResult: 'Touchdown', ...overrides };
}
test('quarter progression comes from the official linescores, not an inferred play count', () => {
  const d = gameDrives(summary({ linescoresAway: [0, 7, 0, 3], linescoresHome: [7, 0, 7, 0], scoreAway: '10', scoreHome: '14' }), player);
  assert.equal(d.periods.length, 4);
  assert.deepEqual(d.periods[0].values, ['0', '7']);
  assert.deepEqual(d.totals, ['10', '14']);
});
test('a team with a game underway shows "—" for periods not yet reached', () => {
  const d = gameDrives(summary({ linescoresAway: [0, 7], linescoresHome: [7, 0] }), player);
  assert.deepEqual(d.periods[2].values, ['—', '—']);
});
test('drives are tagged by team and highlighted for the viewed player’s team', () => {
  const d = gameDrives(summary({ drives: [drive({ team: { id: '10', abbreviation: 'DAL' } }), drive({ team: { id: '20', abbreviation: 'NE' } })] }), player);
  assert.equal(d.available, true);
  assert.equal(d.drives[0].isPlayerTeam, true);
  assert.equal(d.drives[1].isPlayerTeam, false);
  assert.equal(d.drives[0].result, 'Touchdown');
  assert.equal(d.drives[0].plays, 7);
  assert.equal(d.drives[0].timeOfPossession, '2:15');
});
test('drive data is unavailable before kickoff or when the feed has no drives yet', () => {
  assert.equal(gameDrives(summary({ state: 'pre', drives: [drive()] }), player).available, false);
  assert.equal(gameDrives(summary({ state: 'in', drives: [] }), player).available, false);
});
test('overtime extends the period table beyond four quarters', () => {
  const d = gameDrives(summary({ state: 'post', linescoresAway: [7, 7, 0, 3, 3], linescoresHome: [3, 7, 7, 0, 0] }), player);
  assert.equal(d.periods.length, 5);
  assert.equal(d.periods[4].label, 'OT1');
});
