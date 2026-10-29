const { ActionRowBuilder, ButtonBuilder, ButtonStyle, AttachmentBuilder } = require('discord.js');
const WEST = 'nba-standings-west', EAST = 'nba-standings-east', REFRESH = 'nba-standings-refresh';
const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const espnImage = url => /^https:\/\/a\.espncdn\.com\//.test(url || '') ? url : '';
function parseStandings(feed) {
  const conferences = {};
  for (const child of feed.children || []) {
    const key = /west/i.test(child.name || child.abbreviation) ? 'west' : /east/i.test(child.name || child.abbreviation) ? 'east' : null;
    if (!key) continue;
    const teams = (child.standings?.entries || []).map(entry => {
      const stat = name => entry.stats?.find(s => s.name === name || s.type === name);
      const display = name => stat(name)?.displayValue ?? '—';
      return {
        name: entry.team?.displayName || entry.team?.abbreviation || 'Team', abbreviation: entry.team?.abbreviation || '',
        logo: entry.team?.logos?.[0]?.href, seed: Number(stat('playoffSeed')?.value) || 99,
        wins: display('wins'), losses: display('losses'), pct: display('winPercent'), gb: display('gamesBehind'),
        home: display('Home'), road: display('Road'), conf: display('vs. Conf.'), last10: display('Last Ten Games'),
        streak: display('streak'), diff: stat('differential')?.value,
      };
    }).sort((a, b) => a.seed - b.seed || a.name.localeCompare(b.name));
    conferences[key] = { name: child.name || (key === 'west' ? 'Western Conference' : 'Eastern Conference'), teams };
  }
  if (!conferences.west || !conferences.east) throw new Error('NBA standings are unavailable. Please try again shortly.');
  return { season: feed.children?.[0]?.standings?.seasonDisplayName || '', conferences, asOf: new Date().toISOString() };
}
function standingsHTML({ conference, season, asOf, timeZone = 'Europe/Dublin' }) {
  const rows = conference.teams.map((t, i) => {
    const zone = i < 6 ? 'playoff' : i < 10 ? 'playin' : 'out';
    const diff = Number.isFinite(t.diff) ? `${t.diff > 0 ? '+' : ''}${t.diff.toFixed(1)}` : '—';
    const streak = String(t.streak);
    return `<tr class="${zone}${i === 6 || i === 10 ? ' cut' : ''}"><td class="seed">${i + 1}</td><td class="team">${espnImage(t.logo) ? `<img src="${escape(t.logo)}">` : ''}<b>${escape(t.name)}</b></td><td class="strong">${escape(t.wins)}</td><td class="strong">${escape(t.losses)}</td><td>${escape(t.pct)}</td><td>${escape(t.gb)}</td><td>${escape(t.home)}</td><td>${escape(t.road)}</td><td>${escape(t.conf)}</td><td>${escape(t.last10)}</td><td class="${/^W/.test(streak) ? 'up' : /^L/.test(streak) ? 'down' : ''}">${escape(streak)}</td><td class="${Number(t.diff) > 0 ? 'up' : Number(t.diff) < 0 ? 'down' : ''}">${escape(diff)}</td></tr>`;
  }).join('');
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090d13;font-family:Arial,sans-serif;color:#f3f6fa}main{width:1120px;padding:36px 42px 24px;border-top:5px solid #258bea;background:radial-gradient(ellipse at 100% 0%,#16344c88,transparent 50%),#090d13}
    header{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:22px}.eyebrow{font-size:13px;letter-spacing:3px;color:#8193a9;font-weight:700}h1{font-size:42px;margin:10px 0 6px;letter-spacing:-1px}.date{font-size:17px;color:#a5b3c5}.key{display:flex;gap:16px;font-size:12px;color:#9fb2c9;letter-spacing:.5px}.key span{display:flex;align-items:center;gap:7px}.key i{width:12px;height:12px;border-radius:3px;display:inline-block}
    table{width:100%;border-collapse:collapse;background:#111a26;border:1px solid #243448;border-radius:14px;overflow:hidden;font-variant-numeric:tabular-nums}th{font-size:10.5px;letter-spacing:1.2px;color:#8193a9;font-weight:700;padding:11px 8px;text-align:center;background:#0e1620;border-bottom:1px solid #243448}td{font-size:16px;padding:9px 8px;text-align:center;border-top:1px solid #1b2836;color:#c9d5e4}
    th:nth-child(2),td.team{text-align:left}td.seed{width:44px;font-weight:700;color:#8fa3bb;box-shadow:inset 4px 0 0 transparent}td.team{display:flex;align-items:center;gap:11px;white-space:nowrap}td.team img{width:30px;height:30px;object-fit:contain}td.team b{font-size:17px;color:#f3f6fa}td.strong{font-weight:700;color:#fff}
    tr.playoff td.seed{box-shadow:inset 4px 0 0 #42deb2;color:#42deb2}tr.playin td.seed{box-shadow:inset 4px 0 0 #efc778;color:#efc778}tr.out td{color:#8193a9}tr.out td.team b{color:#a9b7c8}tr.cut td{border-top:2px dashed #3a4f68}.up{color:#42deb2!important}.down{color:#ff8a8a!important}
    footer{margin-top:18px;padding-top:14px;border-top:1px solid #243448;display:flex;justify-content:space-between;gap:20px;color:#71859f;font-size:11px;line-height:1.6}
  </style></head><body><main><header><div><div class="eyebrow">NBA LIVE · STANDINGS</div><h1>${escape(conference.name)}</h1><div class="date">${escape(season)} season</div></div><div class="key"><span><i style="background:#42deb2"></i>Playoffs (1–6)</span><span><i style="background:#efc778"></i>Play-in (7–10)</span></div></header>
  <table><thead><tr><th>#</th><th>TEAM</th><th>W</th><th>L</th><th>PCT</th><th>GB</th><th>HOME</th><th>ROAD</th><th>CONF</th><th>L10</th><th>STRK</th><th>DIFF</th></tr></thead><tbody>${rows}</tbody></table>
  <footer><span>Source: ESPN · seeds use ESPN's tiebreakers · DIFF = average point differential<br>Use the buttons to switch conference or refresh</span><span>Updated ${escape(new Intl.DateTimeFormat('en-GB', { timeZone, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }).format(new Date(asOf)))}</span></footer></main></body></html>`;
}
class Standings {
  constructor({ data, renderer, timeZone = 'Europe/Dublin', now = Date.now, ttl = 3600000, limit = 100 }) {
    Object.assign(this, { data, renderer, timeZone, now, ttl, limit }); this.entries = new Map();
  }
  owns(id) { return [WEST, EAST, REFRESH].includes(id); }
  prune() {
    for (const [id, entry] of this.entries) if (entry.expires <= this.now()) this.entries.delete(id);
    while (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value);
  }
  controls(entry) {
    return [new ActionRowBuilder().addComponents(
      new ButtonBuilder().setCustomId(WEST).setLabel('West').setStyle(entry.side === 'west' ? ButtonStyle.Primary : ButtonStyle.Secondary).setDisabled(entry.side === 'west'),
      new ButtonBuilder().setCustomId(EAST).setLabel('East').setStyle(entry.side === 'east' ? ButtonStyle.Primary : ButtonStyle.Secondary).setDisabled(entry.side === 'east'),
      new ButtonBuilder().setCustomId(REFRESH).setLabel('Refresh').setStyle(ButtonStyle.Secondary))];
  }
  model(entry) {
    return { type: 'standings', conference: entry.report.conferences[entry.side], season: entry.report.season, asOf: entry.report.asOf, timeZone: this.timeZone };
  }
  async show(entry) {
    const model = this.model(entry);
    await entry.message.edit({ content: '', attachments: [], embeds: [],
      files: [new AttachmentBuilder(await this.renderer.render(model), { name: 'nba-standings.png', description: `${model.conference.name} standings` })],
      components: this.controls(entry), allowedMentions: { parse: [] } });
  }
  async open(message) {
    const entry = { message, report: await this.data.standings(), side: 'west', expires: this.now() + this.ttl, queue: Promise.resolve() };
    await this.show(entry); this.entries.set(message.id, entry); this.prune();
  }
  async handle(interaction) {
    this.prune(); const entry = this.entries.get(interaction.message.id);
    if (!entry) { await interaction.reply({ content: 'These standings expired or the bot restarted. Run !nba standings again.', flags: 64 }); return; }
    await interaction.deferUpdate();
    const task = entry.queue.then(async () => {
      if (interaction.customId === REFRESH) entry.report = await this.data.standings({ refresh: true });
      else entry.side = interaction.customId === EAST ? 'east' : 'west';
      await this.show(entry);
    });
    entry.queue = task.catch(() => {});
    try { await task; }
    catch (error) { console.error('Standings:', error.message); await interaction.followUp({ content: 'Could not update the standings just now. Please try again.', flags: 64 }); }
  }
}
module.exports = { Standings, parseStandings, standingsHTML, WEST, EAST, REFRESH };
