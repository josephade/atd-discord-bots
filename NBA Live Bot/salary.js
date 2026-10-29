const { ActionRowBuilder, ButtonBuilder, ButtonStyle, AttachmentBuilder } = require('discord.js');
const SEASON_PREFIX = 'nba-salary-season-';
const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const decode = s => String(s ?? '').replace(/<[^>]+>/g, '').replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
  .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
  .replace(/&(amp|lt|gt|quot|nbsp);/g, (_, e) => ({ amp: '&', lt: '<', gt: '>', quot: '"', nbsp: ' ' }[e])).trim();
const money = s => { const n = Number(String(s || '').replace(/[$,]/g, '')); return String(s || '').includes('$') && Number.isFinite(n) ? n : null; };
// League thresholds, set by the NBA for 2026-27 (pr.nba.com/2026-27-salary-cap).
// Later seasons' lines are set each summer, so future payrolls are shown without them.
const THRESHOLDS = {
  '2026-27': { cap: 164961000, minimum: 148465000, tax: 200428000, apron1: 209015000, apron2: 221686000 },
};
// ESPN abbreviation -> Basketball-Reference team code, where they differ.
const BBREF_CODES = { GS: 'GSW', NY: 'NYK', NO: 'NOP', SA: 'SAS', UTAH: 'UTA', WSH: 'WAS', BKN: 'BRK', CHA: 'CHO', PHX: 'PHO' };
const bbrefCode = abbreviation => BBREF_CODES[abbreviation] || abbreviation;
function parseContracts(html) {
  const start = html.indexOf('id="contracts"');
  if (start < 0) throw new Error('The payroll table is unavailable. Please try again shortly.');
  const table = html.slice(start, html.indexOf('</table>', start));
  const seasons = [...table.matchAll(/<th[^>]*data-stat="y(\d)"[^>]*>(.*?)<\/th>/gs)].map(m => decode(m[2])).filter(s => /^\d{4}-\d{2}$/.test(s)).slice(0, 3);
  if (!seasons.length) throw new Error('The payroll table has no seasons listed.');
  const players = [];
  let totals;
  for (const [, row] of table.matchAll(/<tr[^>]*>(.*?)<\/tr>/gs)) {
    const name = decode(row.match(/data-stat="player"[^>]*>(.*?)<\/t[hd]>/s)?.[1]);
    if (!name || name === 'Player') continue;
    const salaries = seasons.map((_, i) => {
      const cell = row.match(new RegExp(`<td([^>]*)data-stat="y${i + 1}"[^>]*>(.*?)</td>`, 's'));
      const amount = money(decode(cell?.[2]));
      const css = cell?.[1] || '';
      return { amount, option: /salary-pl/.test(css) ? 'PO' : /salary-tm/.test(css) ? 'TO' : '' };
    });
    if (name === 'Team Totals') { totals = salaries.map(s => s.amount); continue; }
    if (salaries.some(s => s.amount !== null)) players.push({ name, salaries });
  }
  if (!totals) totals = seasons.map((_, i) => players.reduce((sum, p) => sum + (p.salaries[i].amount || 0), 0));
  return { seasons, players, totals };
}
function salaryModel(report, index = 0) {
  const season = report.seasons[index];
  const lines = THRESHOLDS[season];
  const payroll = report.totals[index] || 0;
  const players = report.players.filter(p => p.salaries[index].amount !== null)
    .map(p => ({ name: p.name, amount: p.salaries[index].amount, option: p.salaries[index].option }))
    .sort((a, b) => b.amount - a.amount);
  return { type: 'salary', team: report.team, season, seasons: report.seasons, index, payroll, lines, players, asOf: report.asOf };
}
const fmt = n => `$${(n / 1e6).toFixed(1)}M`;
function salaryHTML(model) {
  const { team, season, payroll, lines, players } = model;
  const accent = /^[a-f0-9]{6}$/i.test(team.color || '') ? `#${team.color}` : '#258bea';
  const logo = /^https:\/\/a\.espncdn\.com\//.test(team.logo || '') ? `<img class="logo" src="${escape(team.logo)}">` : '';
  const gap = (line, label) => {
    const diff = payroll - line;
    return `<div class="line ${diff > 0 ? 'over' : 'under'}"><span>${label}</span><b>${fmt(line)}</b><small>${diff > 0 ? `${fmt(diff)} over` : `${fmt(-diff)} under`}</small></div>`;
  };
  const marks = lines ? [['CAP', lines.cap], ['TAX', lines.tax], ['1ST', lines.apron1], ['2ND', lines.apron2]] : [];
  const scale = Math.max(payroll, lines?.apron2 || 0) * 1.08 || 1;
  const bar = lines ? `<div class="bar"><div class="fill" style="width:${(100 * payroll / scale).toFixed(1)}%"></div>${marks.map(([label, value]) => `<div class="mark" style="left:${(100 * value / scale).toFixed(1)}%"><span>${label}</span></div>`).join('')}</div>` : '';
  const shown = players.slice(0, 16);
  const rows = shown.map(p => `<div class="row"><span class="name">${escape(p.name)}${p.option ? `<i class="${p.option === 'PO' ? 'po' : 'to'}">${p.option}</i>` : ''}</span><b>${escape(`$${p.amount.toLocaleString('en-US')}`)}</b></div>`).join('');
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090d13;font-family:Arial,sans-serif;color:#f3f6fa}main{width:1120px;padding:38px 44px 25px;border-top:5px solid ${accent};background:radial-gradient(ellipse at 100% 0%,#16344c88,transparent 50%),#090d13}
    header{display:flex;justify-content:space-between;align-items:center}.title{display:flex;align-items:center;gap:20px}.logo{width:84px;height:84px;object-fit:contain}.eyebrow{font-size:13px;letter-spacing:3px;color:#8193a9;font-weight:700}h1{font-size:43px;margin:10px 0 7px;letter-spacing:-1px}.date{font-size:17px;color:#a5b3c5}.total{padding:14px 22px;border:1px solid #273749;background:#111b28;border-radius:13px;text-align:right}.total strong{display:block;font-size:34px;font-variant-numeric:tabular-nums}.total span{display:block;font-size:11px;letter-spacing:1.3px;color:#91a4bc;margin-top:4px}
    .lines{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:28px}.line{background:#111a26;border:1px solid #243448;border-radius:13px;padding:14px 16px}.line span{display:block;font-size:11px;letter-spacing:1.3px;color:#8aa0bb;font-weight:700}.line b{display:block;font-size:22px;margin-top:8px;font-variant-numeric:tabular-nums}.line small{display:block;font-size:14px;margin-top:6px;font-weight:700}.under small{color:#42deb2}.over small{color:#ff8a8a}.over{border-color:#5a2a33}
    .bar{position:relative;height:16px;margin:40px 4px 8px;background:#152233;border-radius:8px}.fill{height:100%;border-radius:8px;background:${accent}}.mark{position:absolute;top:-8px;bottom:-8px;width:2px;background:#d5e0ee}.mark span{position:absolute;top:-20px;left:50%;transform:translateX(-50%);font-size:10px;letter-spacing:1px;color:#9fb2c9;font-weight:700}
    .nolines{margin-top:26px;padding:16px 18px;border:1px solid #26364a;border-radius:13px;background:#111a26;color:#a9b9cd;font-size:15px}
    section{margin-top:26px}.section-title{display:flex;justify-content:space-between;margin-bottom:12px}h2{font-size:15px;letter-spacing:2px;margin:0}.section-title span{font-size:11px;letter-spacing:1px;color:#788da7}.list{display:grid;grid-template-columns:1fr 1fr;gap:0 26px;background:#111a26;border:1px solid #243448;border-radius:15px;padding:6px 20px}
    .row{display:flex;justify-content:space-between;align-items:center;padding:11px 0;border-bottom:1px solid #1d2a3a;font-size:17px}.name{display:flex;align-items:center;gap:8px;min-width:0}.row b{font-variant-numeric:tabular-nums;color:#d5e0ee}i{font-style:normal;font-size:10px;font-weight:700;letter-spacing:.8px;padding:3px 6px;border-radius:4px}.po{background:#173553;color:#73b8ff}.to{background:#40321f;color:#efc778}
    footer{margin-top:24px;padding-top:16px;border-top:1px solid #243448;display:flex;justify-content:space-between;gap:20px;color:#71859f;font-size:11px;line-height:1.6}
  </style></head><body><main><header><div class="title">${logo}<div><div class="eyebrow">NBA LIVE · TEAM SALARY</div><h1>${escape(team.name)}</h1><div class="date">${escape(season)} payroll</div></div></div><div class="total"><strong>${fmt(payroll)}</strong><span>${escape(season)} TEAM SALARY</span></div></header>
  ${lines ? `<div class="lines">${gap(lines.cap, 'SALARY CAP')}${gap(lines.tax, 'LUXURY TAX')}${gap(lines.apron1, '1ST APRON')}${gap(lines.apron2, '2ND APRON')}</div>${bar}` : `<div class="nolines">The NBA sets ${escape(season)} cap, tax and apron lines the summer before, so this is committed salary only. Unsigned roster spots and cap holds are not included.</div>`}
  <section><div class="section-title"><h2>CONTRACTS</h2><span>${players.length} PLAYER${players.length === 1 ? '' : 'S'}${players.length > shown.length ? ` · TOP ${shown.length} SHOWN` : ''}</span></div><div class="list">${rows}</div></section>
  <footer><span>Source: Basketball-Reference payroll · thresholds: NBA<br><i class="po">PO</i> player option &nbsp; <i class="to">TO</i> team option &nbsp; · Totals include options and dead money</span><span>Choose a season below<br>Updated ${escape(new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }).format(new Date(model.asOf || Date.now())))}</span></footer></main></body></html>`;
}
class Salaries {
  constructor({ data, renderer, now = Date.now, ttl = 3600000, limit = 100 }) {
    Object.assign(this, { data, renderer, now, ttl, limit }); this.entries = new Map();
  }
  owns(id) { return id.startsWith(SEASON_PREFIX); }
  prune() {
    for (const [id, entry] of this.entries) if (entry.expires <= this.now()) this.entries.delete(id);
    while (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value);
  }
  controls(entry) {
    return [new ActionRowBuilder().addComponents(entry.report.seasons.map((season, i) => new ButtonBuilder()
      .setCustomId(SEASON_PREFIX + i).setLabel(season).setStyle(i === entry.index ? ButtonStyle.Primary : ButtonStyle.Secondary).setDisabled(i === entry.index)))];
  }
  async show(entry) {
    const model = salaryModel(entry.report, entry.index);
    await entry.message.edit({ content: '', attachments: [], embeds: [],
      files: [new AttachmentBuilder(await this.renderer.render(model), { name: 'nba-salary.png', description: `${model.team.name} ${model.season} salary` })],
      components: this.controls(entry), allowedMentions: { parse: [] } });
  }
  async open(message, query) {
    const entry = { message, report: await this.data.salary(query), index: 0, expires: this.now() + this.ttl };
    await this.show(entry); this.entries.set(message.id, entry); this.prune();
  }
  async handle(interaction) {
    this.prune(); const entry = this.entries.get(interaction.message.id);
    if (!entry) { await interaction.reply({ content: 'This salary card expired or the bot restarted. Run !nba salary again.', flags: 64 }); return; }
    const index = Number(interaction.customId.slice(SEASON_PREFIX.length));
    if (!Number.isInteger(index) || !entry.report.seasons[index]) { await interaction.reply({ content: 'Choose one of the seasons below.', flags: 64 }); return; }
    await interaction.deferUpdate();
    entry.index = index;
    try { await this.show(entry); }
    catch (error) { console.error('Salary card:', error.message); await interaction.followUp({ content: 'Could not update this card just now. Please try again.', flags: 64 }); }
  }
}
module.exports = { Salaries, parseContracts, salaryModel, salaryHTML, bbrefCode, THRESHOLDS, SEASON_PREFIX };
