const test = require('node:test');
const assert = require('node:assert/strict');
const { countdownEmbeds, gamesPayload, schedulePayload, teamEmoji } = require('../games');
const game = { state: 'pre', date: '2026-11-03T00:30:00Z', away: { abbreviation: 'CHI' }, home: { abbreviation: 'PHI' } };
const now = '2026-11-02T20:00:00Z';
test('countdowns use absolute Discord timestamps independent of source timezone', () => {
  const stamp = Math.floor(Date.parse(game.date) / 1000);
  for (const date of [game.date, '2026-11-02T19:30:00-05:00', '2026-11-03T06:00:00+05:30']) {
    const body = countdownEmbeds([{ ...game, date }], now)[0].toJSON();
    assert.match(body.description, new RegExp(`<t:${stamp}:R>`));
    assert.match(body.description, /CHI @ PHI/);
    assert.match(body.footer.text, /local time/);
  }
});
test('live and finished games have no countdown; overdue and stopped games never show misleading starts', () => {
  assert.deepEqual(countdownEmbeds([{ ...game, state: 'in' }, { ...game, state: 'post' }, { ...game, stopped: true }], now), []);
  assert.match(countdownEmbeds([{ ...game, date: '2026-11-02T19:00Z' }], now)[0].data.description, /Awaiting tip-off/);
  assert.match(countdownEmbeds([{ ...game, status: 'Delayed' }], now)[0].data.description, /Delayed/);
  assert.match(countdownEmbeds([{ ...game, date: undefined }], now)[0].data.description, /Time TBC/);
});
test('scoreboards and schedules retain PNGs alongside interactive local-time countdowns', () => {
  const slate = gamesPayload({ games: [game], updatedAt: now }, Buffer.from('png'));
  assert.match(slate.embeds[0].data.description, /<t:\d+:R>/);
  assert.equal(slate.files[0].name, 'nba-games.png');
  const schedule = schedulePayload({ team: game.home, updatedAt: now, upcoming: [{ ...game, home: true, opponent: game.away }] }, Buffer.from('png'));
  assert.match(schedule.embeds[0].data.description, /CHI @ PHI/);
});

test('all NBA teams have portable application emojis and provider aliases resolve consistently', () => {
  const registry = require('../team_emojis.json');
  assert.equal(Object.keys(registry).length, 30);
  for (const [abbreviation, emoji] of Object.entries(registry)) {
    assert.match(emoji, /^<:nba_team_[a-z]+:\d+>$/);
    assert.equal(teamEmoji({ abbreviation }), emoji);
  }
  for (const [alternate, primary] of [['GSW', 'GS'], ['NYK', 'NY'], ['NOP', 'NO'], ['UTA', 'UTAH'], ['SAS', 'SA'], ['WAS', 'WSH']]) {
    assert.ok(teamEmoji({ abbreviation: alternate }));
    assert.equal(teamEmoji({ abbreviation: alternate }), teamEmoji({ abbreviation: primary }));
  }
  const description = countdownEmbeds([game], now)[0].data.description;
  assert.ok(description.startsWith(teamEmoji(game.away)));
  assert.ok(description.includes(`**CHI @ PHI** ${teamEmoji(game.home)}`));
  assert.match(description, /<t:\d+:R>/);
  assert.equal(teamEmoji({ abbreviation: 'unknown' }), '');
});
