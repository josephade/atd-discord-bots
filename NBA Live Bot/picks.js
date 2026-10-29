const { ActionRowBuilder, ButtonBuilder, ButtonStyle, AttachmentBuilder } = require('discord.js');
const snapshot = require('./future_picks.json');
const FUTURE_URL = 'https://basketball.realgm.com/nba/draft/future_drafts/detailed';
const SOURCE_URL = 'https://basketball.realgm.com/nba/draft/future_drafts/team';
const YEARS = [2027, 2028, 2029, 2030];
const TEAMS = {
  ATL: 'Atlanta Hawks', BOS: 'Boston Celtics', BKN: 'Brooklyn Nets', CHA: 'Charlotte Hornets',
  CHI: 'Chicago Bulls', CLE: 'Cleveland Cavaliers', DAL: 'Dallas Mavericks', DEN: 'Denver Nuggets',
  DET: 'Detroit Pistons', GS: 'Golden State Warriors', HOU: 'Houston Rockets', IND: 'Indiana Pacers',
  LAC: 'Los Angeles Clippers', LAL: 'Los Angeles Lakers', MEM: 'Memphis Grizzlies', MIA: 'Miami Heat',
  MIL: 'Milwaukee Bucks', MIN: 'Minnesota Timberwolves', NO: 'New Orleans Pelicans', NY: 'New York Knicks',
  OKC: 'Oklahoma City Thunder', ORL: 'Orlando Magic', PHI: 'Philadelphia Sixers', PHX: 'Phoenix Suns',
  POR: 'Portland Trail Blazers', SAC: 'Sacramento Kings', SA: 'San Antonio Spurs', TOR: 'Toronto Raptors',
  UTAH: 'Utah Jazz', WSH: 'Washington Wizards',
};
const CODE_ALIASES = { GSW: 'GS', NOP: 'NO', NYK: 'NY', SAS: 'SA', UTA: 'UTAH', WAS: 'WSH' };
// RealGM's historical abbreviations -> current abbreviations used in this report.
const DISPLAY_CODES = { BRK: 'BKN', PHL: 'PHI', UTH: 'UTA', SAN: 'SAS', GOS: 'GSW' };
const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const display = text => text.replace(/\b(BRK|PHL|UTH|SAN|GOS)\b/g, code => DISPLAY_CODES[code]);
function splitConditions(text) {
  const result = []; let start = 0, depth = 0;
  for (let i = 0; i < text.length; i++) {
    if ('(['.includes(text[i])) depth++;
    if (')]'.includes(text[i])) depth--;
    if (text[i] === ';' && depth === 0) { result.push(text.slice(start, i).trim()); start = i + 1; }
  }
  result.push(text.slice(start).trim());
  return result.filter(Boolean);
}

class PickSource {
  constructor({ dataset = snapshot } = {}) { this.dataset = dataset; }
  async report(team) {
    const code = CODE_ALIASES[team.abbreviation] || team.abbreviation;
    const name = TEAMS[code], dataset = this.dataset;
    if (!name) throw new Error('No draft-pick ownership data is configured for this NBA team.');
    const date = new Date(`${dataset.verifiedOn}T12:00:00Z`);
    if (dataset.source !== SOURCE_URL || !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== dataset.verifiedOn) {
      throw new Error('The draft-pick snapshot has no valid source verification date.');
    }
    const sourceRows = dataset.teams?.[name] || [];
    const years = YEARS.map(year => {
      const rows = sourceRows.filter(row => row.year === year);
      if (rows.length !== 1 || !['first', 'second'].every(key => typeof rows[0][key] === 'string' && rows[0][key].trim())) {
        throw new Error(`Draft-pick ownership for ${name} in ${year} is unavailable. No holdings were guessed.`);
      }
      return { year, first: display(rows[0].first), second: display(rows[0].second) };
    });
    return { type: 'picks', startYear: YEARS[0], endYear: YEARS.at(-1), years,
      source: SOURCE_URL, verifiedOn: dataset.verifiedOn, snapshot: true,
      team: { name: team.displayName, abbreviation: code, logo: team.logos?.[0]?.href, color: team.color } };
  }
}

function picksHTML(model) {
  const { team, startYear, endYear, years, verifiedOn } = model;
  const accent = /^[a-f0-9]{6}$/i.test(team.color) ? `#${team.color}` : '#258bea';
  const logo = /^https:\/\/a\.espncdn\.com\//.test(team.logo || '') ? `<img src="${escape(team.logo)}">` : '';
  const checked = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', day: 'numeric', month: 'long', year: 'numeric' }).format(new Date(`${verifiedOn}T12:00:00Z`));
  const conditions = text => splitConditions(text).map(item => `<li>${escape(item)}</li>`).join('');
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090d13;color:#f3f6fa;font-family:Arial,sans-serif}main{width:1120px;padding:35px 42px 25px;border-top:6px solid ${accent};background:radial-gradient(ellipse at 100% 0%,${accent}35,transparent 44%),#090d13}
    header{display:flex;align-items:center;gap:20px}header img{width:84px;height:84px;object-fit:contain}.eyebrow{font-size:12px;letter-spacing:2.8px;color:#92a8c1;font-weight:700}h1{font-size:39px;margin:10px 0 7px;overflow-wrap:anywhere}.subtitle{color:#9aacc2;font-size:18px}.range{display:flex;gap:9px;margin:23px 0 18px}.range span{padding:10px 19px;background:#1a2c42;color:#c2ddfa;border:1px solid #304867;border-radius:8px;font-size:18px;font-weight:bold}
    .status{padding:14px 18px;border:1px solid #3b4860;border-radius:11px;background:#162133;font-size:14px;line-height:1.6;color:#b2c4dc}.status b{color:#e1eaf5}.legend{display:flex;gap:25px;font-size:13px;line-height:1.5;color:#8fa5bf;margin:17px 0 20px}.legend b{color:#e0eafa}
    section{background:#111b28;border:1px solid #28394e;border-radius:14px;overflow:hidden;margin:17px 0}.year{display:flex;justify-content:space-between;align-items:center;padding:15px 20px;background:linear-gradient(105deg,${accent}55,#17283a);border-bottom:1px solid #2b3c51}.year strong{font-size:26px}.year span{color:#9fb4cf;font-size:11px;letter-spacing:1.4px}
    .rounds{display:grid;grid-template-columns:1fr 1fr}.round{padding:18px 20px 20px;min-width:0}.round+.round{border-left:1px solid #28394e}h2{margin:0 0 13px;color:#85c4ff;font-size:13px;letter-spacing:1.8px}ul{margin:0;padding-left:18px}li{font-size:16px;line-height:1.65;margin:7px 0;color:#d1dce9;overflow-wrap:anywhere}li::marker{color:#638bb8}
    footer{margin-top:22px;padding-top:15px;border-top:1px solid #28384d;color:#7f94af;font-size:12px;line-height:1.8}footer b{color:#b1c3d9}
  </style></head><body><main><header>${logo}<div><div class="eyebrow">NBA LIVE · FUTURE DRAFT PICKS</div><h1>${escape(team.name)}</h1><div class="subtitle">${startYear}–${endYear} · First and second rounds · Protections and swaps</div></div></header>
  <div class="range">${years.map(row => `<span>${row.year}</span>`).join('')}</div>
  <div class="status"><b>Ownership snapshot verified ${escape(checked)}.</b> This is dated data, not a live trade feed. Trades after that date may change these rights.</div>
  <div class="legend"><span><b>Own</b> = this team's original pick</span><span>Ranges such as <b>1–4</b> are draft positions</span><span>Conditional rights are not guaranteed extra picks</span></div>
  ${years.map(row => `<section><div class="year"><strong>${row.year}</strong><span>DRAFT OWNERSHIP & CONDITIONS</span></div><div class="rounds"><div class="round"><h2>FIRST ROUND</h2><ul>${conditions(row.first)}</ul></div><div class="round"><h2>SECOND ROUND</h2><ul>${conditions(row.second)}</ul></div></div></section>`).join('')}
  <footer><b>Source: RealGM Future Drafts By Team · Verified ${escape(checked)}</b><br>Includes picks retained, acquired, traded away and conditional swap rights. Final pick numbers are not determined.<br>Use the source links below for the full ownership table and detailed trade conditions.</footer></main></body></html>`;
}

class Picks {
  constructor({ data, renderer, source = new PickSource() }) { Object.assign(this, { data, renderer, source }); }
  async open(message, query) {
    const team = await this.data.findTeam(query);
    const model = await this.source.report(team);
    const png = await this.renderer.render(model);
    const links = new ActionRowBuilder().addComponents(
      new ButtonBuilder().setLabel('Ownership source · RealGM').setStyle(ButtonStyle.Link).setURL(model.source),
      new ButtonBuilder().setLabel('Detailed trade conditions').setStyle(ButtonStyle.Link).setURL(FUTURE_URL));
    await message.edit({ content: '', embeds: [], attachments: [], files: [new AttachmentBuilder(png, { name: 'nba-picks.png', description: `${team.displayName} ${model.startYear}–${model.endYear} draft-pick ownership, protections and swaps` })],
      components: [links], allowedMentions: { parse: [] } });
  }
}
module.exports = { Picks, PickSource, picksHTML, splitConditions, YEARS, TEAMS, SOURCE_URL, FUTURE_URL };
