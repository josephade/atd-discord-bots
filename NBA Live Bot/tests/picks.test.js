const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { NBAData } = require('../data');
const { html } = require('../render');
const { Picks, PickSource, picksHTML, splitConditions, YEARS, TEAMS, SOURCE_URL } = require('../picks');
const snapshot = require('../future_picks.json');
const lakers = { id: '13', name: 'Lakers', displayName: 'Los Angeles Lakers', abbreviation: 'LAL', color: '552583', logos: [{ href: 'https://a.espncdn.com/lal.png' }] };

test('pick and picks aliases accept team names and emojis, requiring a team', () => {
  for (const command of ['pick', 'picks']) {
    assert.deepEqual(parse(`NBA ${command} :lakers:`), { command: 'picks', query: ':lakers:' });
    assert.deepEqual(parse(`nba ${command} Los Angeles Lakers`), { command: 'picks', query: 'Los Angeles Lakers' });
    assert.throws(() => parse(`nba ${command}`), /Include a team/);
  }
});

test('team lookup retains typed emojis, selected custom emojis and abbreviations', async () => {
  const data = new NBAData(); data.league = async () => ({ teams: [{ team: lakers }] });
  for (const query of [':lakers:', '<:Lakers:123456789>', '<a:Lakers:123456789>', 'LAL', 'Los Angeles Lakers']) {
    assert.equal((await data.findTeam(query)).id, '13');
  }
});

test('all 30 teams have exactly four verified years, both rounds, and no guessed positions', async () => {
  const source = new PickSource();
  assert.equal(Object.keys(TEAMS).length, 30);
  assert.deepEqual(YEARS, [2027, 2028, 2029, 2030]);
  assert.equal(Object.keys(snapshot.teams).length, 30);
  for (const abbreviation of Object.keys(TEAMS)) {
    const model = await source.report({ abbreviation, displayName: TEAMS[abbreviation] });
    assert.deepEqual(model.years.map(row => row.year), YEARS);
    assert.equal(model.snapshot, true);
    assert.equal(model.verifiedOn, snapshot.verifiedOn);
    assert.equal(model.source, SOURCE_URL);
    assert.ok(model.years.every(row => row.first.trim() && row.second.trim()));
    assert.equal(model.currentPicks, undefined);
  }
});

test('Lakers ownership preserves protected retention, conditional transfers, swaps and outgoing rights', async () => {
  const model = await new PickSource().report(lakers);
  assert.equal(model.years[0].first, '1-4 Own; 5-30 to MEM (via UTA)');
  assert.match(model.years[0].second, /BKN if LAL conveys 1st round pick to MEM/);
  assert.match(model.years[0].second, /or to MEM if LAL does not convey/);
  assert.match(model.years[1].first, /Less favorable.*most favorable.*UTA/);
  assert.equal(model.years[2].first, 'To DAL');
  assert.match(model.years[3].first, /Own or UTA/);
  assert.equal(model.years[3].second, 'To BKN');
});

test('ESPN aliases and source naming differences resolve the correct snapshot team', async () => {
  const source = new PickSource();
  for (const [alias, code] of Object.entries({ GSW: 'GS', NOP: 'NO', NYK: 'NY', SAS: 'SA', UTA: 'UTAH', WAS: 'WSH' })) {
    assert.deepEqual((await source.report({ abbreviation: alias })).years, (await source.report({ abbreviation: code })).years);
  }
  assert.equal((await source.report({ abbreviation: 'PHI', displayName: 'Philadelphia 76ers' })).team.name, 'Philadelphia 76ers');
});

test('unavailable or duplicated year data fails instead of inventing own picks', async () => {
  for (const change of [rows => rows.pop(), rows => rows.push(rows[0]), rows => { rows[0].second = ''; }]) {
    const dataset = structuredClone(snapshot); change(dataset.teams['Los Angeles Lakers']);
    await assert.rejects(new PickSource({ dataset }).report(lakers), /unavailable/);
  }
  await assert.rejects(new PickSource().report({ abbreviation: '../../other' }), /No draft-pick ownership/);
});

test('source provenance and verification date are required; additional years are excluded', async () => {
  for (const overrides of [{ verifiedOn: '2026-02-30' }, { verifiedOn: '' }, { source: 'https://unverified.example' }]) {
    await assert.rejects(new PickSource({ dataset: { ...snapshot, ...overrides } }).report(lakers), /verification date/);
  }
  const dataset = structuredClone(snapshot);
  dataset.teams['Los Angeles Lakers'].push({ year: 2031, first: 'Own', second: 'Own' });
  assert.deepEqual((await new PickSource({ dataset }).report(lakers)).years.map(row => row.year), YEARS);
});

test('conditions keep semicolons within trade paths and parenthetical clauses together', () => {
  assert.deepEqual(splitConditions('Own; More favorable of A and B (via C; via D); To E [then F; then G]'),
    ['Own', 'More favorable of A and B (via C; via D)', 'To E [then F; then G]']);
});

test('one image includes every year and both rounds with dated-snapshot labelling and escaped source text', async () => {
  const model = await new PickSource().report(lakers);
  model.years[0].first += '; <script>bad</script> & condition';
  model.team.logo = 'javascript:unsafe';
  const markup = picksHTML(model);
  assert.equal((markup.match(/<section>/g) || []).length, 4);
  assert.equal((markup.match(/<h2>FIRST ROUND/g) || []).length, 4);
  assert.equal((markup.match(/<h2>SECOND ROUND/g) || []).length, 4);
  assert.match(markup, /2027–2030/);
  assert.match(markup, /verified 29 October 2026/);
  assert.match(markup, /not a live trade feed/);
  assert.match(markup, /&lt;script&gt;bad&lt;\/script&gt; &amp; condition/);
  assert.doesNotMatch(markup, /javascript:unsafe|<script>|PROJECTED POSITION|Upcoming 2027 draft only/);
  assert.equal(html(model), markup);
});

test('opening a report sends one PNG containing four years and ownership/conditions source links', async () => {
  const edits = [], models = [];
  const data = { findTeam: async () => lakers };
  const controller = new Picks({ data, renderer: { render: async model => { models.push(model); return Buffer.from('png'); } } });
  await controller.open({ edit: async body => edits.push(body) }, ':lakers:');
  assert.equal(edits.length, 1);
  assert.equal(models[0].years.length, 4);
  assert.equal(edits[0].files[0].name, 'nba-picks.png');
  assert.match(edits[0].files[0].description, /2027–2030/);
  const buttons = edits[0].components[0].toJSON().components;
  assert.equal(buttons[0].url, SOURCE_URL);
  assert.match(buttons[1].url, /future_drafts\/detailed/);
  data.findTeam = async () => { throw Error('No NBA team matches'); };
  await assert.rejects(controller.open({}, 'unknown'), /No NBA team/);
  assert.equal(models.length, 1);
});
