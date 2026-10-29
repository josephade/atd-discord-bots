const { ActionRowBuilder, StringSelectMenuBuilder, ButtonBuilder, ButtonStyle, AttachmentBuilder } = require('discord.js');
const { gamesPayload } = require('./games');
const { gameStatus } = require('./games_layout');
const SELECT_PREFIX = 'nfl-boxscore-game-', HOME_ID = 'nfl-boxscore-home';
const PREV_ID = 'nfl-boxscore-prev', NEXT_ID = 'nfl-boxscore-next', REFRESH_ID = 'nfl-boxscore-refresh';
// NFL games cluster on 3-4 days a week (Thu/Sun/Mon), not nightly like NBA, so browsing
// steps by week (using the ordered calendar data.games() resolves), not by calendar day.
function weekIndex(calendar, current) {
  return (calendar || []).findIndex(entry => entry.seasonType === current?.seasonType && entry.week === current?.week);
}
function shiftWeek(calendar, current, delta) {
  const index = weekIndex(calendar, current);
  if (index === -1) return current;
  return calendar[index + delta] || current;
}
const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// One table per non-empty stat category (passing/rushing/receiving/...), not one flat
// column set — NFL box scores don't share a single per-player stat line like NBA's.
function boxscoreHTML(model) {
  const teams = model.teams || [];
  const updated = model.updatedAt ? new Intl.DateTimeFormat('en-GB', { timeZone: model.timeZone || 'Europe/Dublin', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short' }).format(new Date(model.updatedAt)) : '';
  const logo = team => /^https:\/\/a\.espncdn\.com\//.test(team.logo || '') ? `<img src="${escape(team.logo)}">` : '';
  const categoryTable = category => `<div class="category"><h3>${escape(category.label)}</h3><table><colgroup><col class="player-col">${category.labels.map(() => '<col>').join('')}</colgroup><thead><tr><th>PLAYER</th>${category.labels.map(label => `<th>${escape(label)}</th>`).join('')}</tr></thead><tbody>${category.players.map(player => `<tr class="${player.dnp ? 'dnp' : ''}"><th>${escape(player.name)}${player.starter ? '<small>STARTER</small>' : ''}</th>${player.dnp ? `<td colspan="${category.labels.length}">DNP · ${escape(player.reason)}</td>` : category.labels.map((_, i) => `<td>${escape(player.values[i] == null || player.values[i] === '' ? '—' : player.values[i])}</td>`).join('')}</tr>`).join('')}${category.totals ? `<tr class="total"><th>TEAM TOTAL</th>${category.totals.map(v => `<td>${escape(v == null || v === '' ? '—' : v)}</td>`).join('')}</tr>` : ''}</tbody></table></div>`;
  const teamSection = team => `<section><div class="team-title">${logo(team)}<div><h2>${escape(team.name)}</h2><span>${team.side === 'away' ? 'AWAY' : 'HOME'}</span></div></div>
    ${team.categories?.length && model.state !== 'pre' ? team.categories.map(categoryTable).join('') : `<div class="empty">${/cancel|postpon|suspend/i.test(model.status || '') ? `Game status: ${escape(model.status)}. Player stats are unavailable.` : model.state === 'pre' ? 'Kickoff pending. Player stats will appear once the game starts.' : 'Player box score unavailable from the feed. Please select this game again shortly.'}</div>`}</section>`;
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090d13;color:#eef4fc;font-family:Arial,sans-serif}main{width:1120px;padding:30px 32px 24px;border-top:5px solid #258bea;background:radial-gradient(ellipse at 100% 0%,#17374c77,transparent 40%),#090d13}.eyebrow{font-size:13px;letter-spacing:3px;color:#879db7;font-weight:700}.header{display:flex;justify-content:space-between;align-items:center;margin:24px 0 10px}.match{display:flex;gap:22px;align-items:center}.score-team{display:flex;align-items:center;gap:12px;font-size:24px;font-weight:bold}.score-team img{width:52px;height:52px;object-fit:contain}.score-team b{font-size:43px}.divider{color:#6d8099;font-size:23px}.status{text-align:right;color:#67b5ff;font-size:17px;line-height:1.7}.live{color:#3edbb0}section{margin-top:26px}.team-title{display:flex;align-items:center;gap:12px;margin-bottom:11px}.team-title img{width:33px;height:33px;object-fit:contain}h2{font-size:22px;margin:0}.team-title span{font-size:10px;letter-spacing:2px;color:#758da9}.category{margin-top:14px}.category h3{font-size:12px;letter-spacing:1.5px;color:#7ebfff;margin:0 0 6px;text-transform:uppercase}table{width:100%;border-collapse:collapse;table-layout:fixed;font-variant-numeric:tabular-nums}.player-col{width:206px}thead{background:#172330}thead th{padding:11px 1px;font-size:11px;color:#a6b7cc;font-weight:normal}thead th:first-child{text-align:left;padding-left:10px}tbody th,tbody td{border-bottom:1px solid #223043;padding:10px 1px;text-align:center;font-size:14px}tbody th{text-align:left;font-size:15px;padding-left:10px;font-weight:normal;overflow-wrap:anywhere}tbody tr:nth-child(even){background:#101a25}small{display:block;font-size:8px;letter-spacing:1px;color:#708ba8;margin-top:3px}.dnp{color:#697f99}.dnp td{text-align:left;padding-left:17px;font-size:12px}.total th,.total td{font-weight:700;background:#162639;color:#7ebfff}.empty{padding:20px;border:1px solid #273a50;border-radius:10px;color:#a7bdd5;font-size:16px}footer{display:flex;justify-content:space-between;border-top:1px solid #27374b;margin-top:25px;padding-top:14px;color:#768da8;font-size:11px;line-height:1.7}
  </style></head><body><main><div class="eyebrow">NFL LIVE · GAME BOX SCORE</div><div class="header"><div class="match">${teams.map((team, i) => `${i ? '<span class="divider">—</span>' : ''}<div class="score-team">${logo(team)}<span>${escape(team.abbreviation || team.name)}</span><b>${model.state === 'pre' ? '—' : escape(team.score)}</b></div>`).join('')}</div><div class="status ${model.state === 'in' ? 'live' : ''}">${escape(model.status)}<br>${model.date ? escape(new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Dublin', day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(model.date))) : ''}</div></div>${teams.map(teamSection).join('')}<footer><span>Source: ESPN · — = unavailable<br>Scores and stats may lag play</span><span>Updated ${escape(updated)}<br>Refresh for current score and clock · All Games for this date</span></footer></main></body></html>`;
}
class Boxscores {
  constructor({ data, renderer, timeZone = 'Europe/Dublin', now = Date.now, ttl = 3600000, limit = 100 }) {
    Object.assign(this, { data, renderer, timeZone, now, ttl, limit });
    this.entries = new Map();
  }
  owns(id) { return [HOME_ID, PREV_ID, NEXT_ID, REFRESH_ID].includes(id) || id.startsWith(SELECT_PREFIX); }
  prune() {
    for (const [id, entry] of this.entries) if (entry.expires <= this.now()) this.entries.delete(id);
    while (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value);
  }
  components(entry, selected) {
    const rows = [], games = entry.slate.games;
    if (games.length > 100) throw new Error('Too many games this week. Please choose a more specific game.');
    for (let start = 0; start < games.length; start += 25) {
      rows.push(new ActionRowBuilder().addComponents(new StringSelectMenuBuilder()
        .setCustomId(SELECT_PREFIX + start).setPlaceholder('Choose a game to see its box score')
        .addOptions(games.slice(start, start + 25).map(game => {
          const status = gameStatus(game, this.now(), this.timeZone);
          return { label: `${game.away.name} @ ${game.home.name}`.slice(0, 100), value: String(game.id),
            description: `${status.badge} · ${status.text}`.slice(0, 100), default: String(game.id) === selected };
        }))));
    }
    const index = weekIndex(entry.slate.calendar, entry.slate);
    const buttons = [
      new ButtonBuilder().setCustomId(PREV_ID).setLabel('← Previous Week').setStyle(ButtonStyle.Primary).setDisabled(index <= 0),
      new ButtonBuilder().setCustomId(REFRESH_ID).setLabel('Refresh').setStyle(ButtonStyle.Secondary),
      new ButtonBuilder().setCustomId(NEXT_ID).setLabel('Next Week →').setStyle(ButtonStyle.Primary).setDisabled(index === -1 || index >= (entry.slate.calendar?.length || 0) - 1),
    ];
    if (selected) buttons.push(new ButtonBuilder().setCustomId(HOME_ID).setLabel('All Games').setStyle(ButtonStyle.Secondary));
    rows.push(new ActionRowBuilder().addComponents(...buttons));
    return rows;
  }
  async overview(entry) {
    const model = { ...entry.slate, type: 'games', allGames: true, timeZone: this.timeZone, updatedAt: entry.updatedAt };
    return { ...gamesPayload(model, await this.renderer.render(model)), attachments: [], components: this.components(entry, null) };
  }
  async open(message, slate) {
    const entry = { message, slate, selected: null,
      updatedAt: new Date(this.now()).toISOString(), expires: this.now() + this.ttl, queue: Promise.resolve() };
    await message.edit(await this.overview(entry));
    this.entries.set(message.id, entry); this.prune();
  }
  async handle(interaction) {
    this.prune();
    const entry = this.entries.get(interaction.message.id);
    if (!entry) { await interaction.reply({ content: 'This game selector expired or the bot restarted. Run !nfl boxscores again.', flags: 64 }); return; }
    const selecting = interaction.customId.startsWith(SELECT_PREFIX);
    const selected = interaction.values?.[0];
    if (selecting && !entry.slate.games.some(game => String(game.id) === selected)) {
      await interaction.reply({ content: 'Please choose a game from this menu.', flags: 64 }); return;
    }
    await interaction.deferUpdate();
    const task = entry.queue.then(async () => {
      const next = { ...entry };
      const navigating = [PREV_ID, NEXT_ID].includes(interaction.customId);
      if (navigating || interaction.customId === HOME_ID || interaction.customId === REFRESH_ID) {
        const target = navigating ? shiftWeek(entry.slate.calendar, entry.slate, interaction.customId === PREV_ID ? -1 : 1) : entry.slate;
        next.slate = await this.data.games({ all: true, week: target.week, seasonType: target.seasonType, year: entry.slate.year, refresh: true });
      }
      next.selected = navigating || interaction.customId === HOME_ID ? null : selecting ? selected : entry.selected;
      if (next.selected && !next.slate.games.some(game => String(game.id) === next.selected)) next.selected = null;
      next.updatedAt = new Date(this.now()).toISOString();
      if (next.selected === null) await entry.message.edit(await this.overview(next));
      else {
        const model = { ...await this.data.boxscore(next.selected, { refresh: true }), updatedAt: next.updatedAt, timeZone: this.timeZone };
        const png = await this.renderer.render(model);
        await entry.message.edit({ content: '', embeds: [], attachments: [], files: [new AttachmentBuilder(png, { name: 'nfl-boxscore.png', description: 'Full NFL game box score for both teams' })],
          components: this.components(next, next.selected), allowedMentions: { parse: [] } });
      }
      Object.assign(entry, { slate: next.slate, selected: next.selected, updatedAt: next.updatedAt, expires: this.now() + this.ttl });
    });
    entry.queue = task.catch(() => {});
    try { await task; }
    catch (error) {
      console.error('Game boxscore:', error.message);
      await interaction.followUp({ content: 'Could not update this box score or game list just now. Your previous view is unchanged. Please try Refresh again.', flags: 64 });
    }
  }
}
module.exports = { Boxscores, boxscoreHTML, SELECT_PREFIX, HOME_ID, PREV_ID, NEXT_ID, REFRESH_ID, shiftWeek, weekIndex };
