const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { parseContracts, salaryModel, salaryHTML, bbrefCode } = require('../salary');

const page = `<table id="contracts"><thead><tr><th>x</th></tr><tr><th data-stat="player" scope="col">Player</th><th data-stat="y1" scope="col">2026-27</th><th data-stat="y2" scope="col">2027-28</th><th data-stat="y3" scope="col">2028-29</th></tr></thead><tbody>
<tr><th data-stat="player"><a href="#">Luka Don&#269;i&#263;</a></th><td class="right " data-stat="y1">$49,800,000</td><td class="right " data-stat="y2">$53,784,000</td><td class="right salary-pl" data-stat="y3">$57,768,000</td></tr>
<tr><th data-stat="player">Bench Guy</th><td class="right " data-stat="y1">$2,000,000</td><td class="right salary-tm" data-stat="y2">$2,100,000</td><td class="right iz" data-stat="y3"></td></tr>
<tr><th data-stat="player">Unsigned</th><td class="right iz" data-stat="y1"></td><td class="right iz" data-stat="y2"></td><td class="right iz" data-stat="y3"></td></tr>
<tr><th data-stat="player">Team Totals</th><td data-stat="y1">$215,000,000</td><td data-stat="y2">$55,884,000</td><td data-stat="y3">$57,768,000</td></tr>
</tbody></table>`;

test('nba salary parses a team and requires one', () => {
  assert.deepEqual(parse('nba salary Lakers'), { command: 'salary', query: 'Lakers' });
  assert.throws(() => parse('nba salary'), /Include a team/);
});

test('payroll parser reads seasons, options, names and the team total row', () => {
  const report = parseContracts(page);
  assert.deepEqual(report.seasons, ['2026-27', '2027-28', '2028-29']);
  assert.deepEqual(report.totals, [215000000, 55884000, 57768000]);
  assert.deepEqual(report.players.map(p => p.name), ['Luka Dončić', 'Bench Guy']);
  assert.equal(report.players[0].salaries[2].option, 'PO');
  assert.equal(report.players[1].salaries[1].option, 'TO');
  assert.equal(report.players[1].salaries[2].amount, null);
  assert.throws(() => parseContracts('<html></html>'), /payroll table is unavailable/);
});

test('current season compares payroll to the aprons; later seasons show committed salary only', () => {
  const report = { ...parseContracts(page), team: { name: 'Los Angeles Lakers' }, asOf: '2026-10-29T12:00:00Z' };
  const now = salaryModel(report, 0);
  assert.equal(now.lines.apron1, 209015000);
  const html = salaryHTML(now);
  assert.match(html, /1ST APRON/); assert.match(html, /\$6\.0M over/); assert.match(html, /\$6\.7M under/);
  const later = salaryModel(report, 2);
  assert.equal(later.lines, undefined);
  assert.deepEqual(later.players.map(p => p.name), ['Luka Dončić']);
  assert.match(salaryHTML(later), /committed salary only/);
});

test('ESPN abbreviations map to Basketball-Reference team codes', () => {
  assert.equal(bbrefCode('BKN'), 'BRK'); assert.equal(bbrefCode('PHX'), 'PHO'); assert.equal(bbrefCode('CHA'), 'CHO');
  assert.equal(bbrefCode('GS'), 'GSW'); assert.equal(bbrefCode('UTAH'), 'UTA'); assert.equal(bbrefCode('LAL'), 'LAL');
});
