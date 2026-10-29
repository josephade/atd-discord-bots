const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { Standings, parseStandings, standingsHTML, EAST, WEST } = require('../standings');

const entry = (name, seed, wins, losses, diff) => ({ team: { displayName: name, abbreviation: name.slice(0, 3).toUpperCase() }, stats: [
  { name: 'playoffSeed', value: seed }, { name: 'wins', displayValue: String(wins) }, { name: 'losses', displayValue: String(losses) },
  { name: 'winPercent', displayValue: '.500' }, { name: 'gamesBehind', displayValue: '-' }, { name: 'streak', displayValue: 'W2' },
  { name: 'differential', value: diff }, { name: 'Last Ten Games', displayValue: `${wins}-${losses}` },
] });
const feed = { children: [
  { name: 'Eastern Conference', standings: { seasonDisplayName: '2026-27', entries: [entry('Boston Celtics', 2, 3, 1, 4), entry('New York Knicks', 1, 4, 0, 9)] } },
  { name: 'Western Conference', standings: { seasonDisplayName: '2026-27', entries: Array.from({ length: 12 }, (_, i) => entry(`West Team ${String(12 - i).padStart(2, '0')}`, 12 - i, 12 - i, i, 12 - 2 * i)) } },
] };

test('nba standings is a reserved command with no arguments', () => {
  assert.deepEqual(parse('nba standings'), { command: 'standings' });
  assert.throws(() => parse('nba standings east'), /West \/ East buttons/);
});

test('standings split by conference and sort by playoff seed', () => {
  const report = parseStandings(feed);
  assert.equal(report.season, '2026-27');
  assert.deepEqual(report.conferences.east.teams.map(t => t.name), ['New York Knicks', 'Boston Celtics']);
  assert.equal(report.conferences.west.teams[0].name, 'West Team 01');
  assert.throws(() => parseStandings({ children: [] }), /standings are unavailable/);
  const html = standingsHTML({ conference: report.conferences.west, season: report.season, asOf: '2026-10-29T12:00:00Z' });
  assert.equal((html.match(/<tr class="playoff/g) || []).length, 6);
  assert.equal((html.match(/<tr class="playin/g) || []).length, 4);
  assert.match(html, /<tr class="out cut">/);
  assert.match(html, /\+10\.0/); assert.match(html, /-10\.0/);
});

test('opens on the West and the East button flips conference without refetching', async () => {
  const edits = [], calls = [];
  const data = { standings: async options => { calls.push(options); return parseStandings(feed); } };
  const standings = new Standings({ data, renderer: { render: async model => Buffer.from(model.conference.name) } });
  const message = { id: 'm1', edit: async body => edits.push(body) };
  await standings.open(message);
  assert.equal(edits.at(-1).files[0].description, 'Western Conference standings');
  const press = id => standings.handle({ customId: id, message, deferUpdate: async () => {}, followUp: async () => {} });
  await press(EAST);
  assert.equal(edits.at(-1).files[0].description, 'Eastern Conference standings');
  assert.equal(calls.length, 1);
  await press(WEST);
  assert.equal(edits.at(-1).files[0].description, 'Western Conference standings');
});
