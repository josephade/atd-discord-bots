const { ActionRowBuilder, StringSelectMenuBuilder, ButtonBuilder, ButtonStyle, AttachmentBuilder } = require('discord.js');
const TEAM_PREFIX = 'nba-injury-team-', PREV = 'nba-injury-prev', NEXT = 'nba-injury-next', REFRESH = 'nba-injury-refresh';
const PAGE_SIZE = 8;
const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const normalized = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
function returnInfo(injury, asOf) {
  const details = injury.details || {};
  if (details.fantasyStatus?.abbreviation === 'OFS') return { label: 'Out for season', note: 'Reported season absence' };
  const comments = `${injury.shortComment || ''} ${injury.longComment || ''}`;
  if (/\b(?:no|without(?: (?:a|an|any))?)\s+(?:(?:clear|definitive|set|official|specific|return)\s+){0,2}(?:timetable|timeline)|\bindefinitely\b/i.test(comments)) {
    return { label: 'TBD', note: 'No return timetable reported' };
  }
  const date = details.returnDate;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date || '') || !Number.isFinite(Date.parse(date + 'T12:00:00Z')) || new Date(date + 'T12:00:00Z').toISOString().slice(0, 10) !== date) return { label: 'TBD', note: 'No return date reported' };
  const formatted = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(date + 'T12:00:00Z'));
  return date < asOf.slice(0, 10) ? { label: 'Update pending', note: `Prior estimate: ${formatted}` }
    : { label: formatted, note: 'ESPN estimate · not confirmed' };
}
function parseInjuries(feed, leagueTeams) {
  if (!Array.isArray(feed.injuries) || feed.status && feed.status !== 'success') throw new Error('The NBA injury feed is unavailable. Please try again shortly.');
  const asOf = Number.isFinite(Date.parse(feed.timestamp)) ? feed.timestamp : new Date().toISOString();
  const teams = new Map(leagueTeams.map(team => [String(team.id), {
    id: String(team.id), name: team.displayName, abbreviation: team.abbreviation || '',
    logo: team.logo || team.logos?.[0]?.href, players: [],
  }]));
  for (const group of feed.injuries) {
    const id = String(group.id);
    if (!teams.has(id)) {
      const team = group.injuries?.[0]?.athlete?.team || {};
      teams.set(id, { id, name: group.displayName || team.displayName || 'Team unavailable', abbreviation: team.abbreviation || '', logo: team.logo || team.logos?.[0]?.href, players: [] });
    }
    const team = teams.get(id), seen = new Map(team.players.map(player => [player.id, player]));
    for (const injury of group.injuries || []) {
      const athlete = injury.athlete;
      if (!athlete?.displayName) continue;
      const link = athlete.links?.find(link => link.rel?.includes('playercard'))?.href || '';
      const playerId = String(athlete.id || link.match(/\/id\/(\d+)/)?.[1] || normalized(athlete.displayName));
      const details = injury.details || {};
      const kind = [details.side, details.type].filter(value => value && value !== 'Not Specified').join(' ') || 'Details not reported';
      const photo = athlete.headshot?.href;
      const photoPath = photo?.match(/^https:\/\/a\.espncdn\.com(\/i\/headshots\/[^?]+\.png)$/)?.[1];
      const headshot = photoPath ? `https://a.espncdn.com/combiner/i?img=${encodeURIComponent(photoPath)}&w=128` : photo;
      const player = { id: playerId, name: athlete.displayName, position: athlete.position?.abbreviation || '', headshot,
        status: injury.status || 'Status unavailable', injury: kind + (details.detail ? ` · ${details.detail}` : ''),
        note: injury.shortComment || injury.longComment || 'No additional report available.',
        return: returnInfo(injury, asOf), updatedAt: injury.date || '', teamId: id, teamName: team.name, teamAbbreviation: team.abbreviation, teamLogo: team.logo };
      const previous = seen.get(playerId);
      if (!previous || (Date.parse(player.updatedAt) || 0) >= (Date.parse(previous.updatedAt) || 0)) seen.set(playerId, player);
    }
    team.players = [...seen.values()].sort((a, b) => (a.status === 'Out' ? 0 : 1) - (b.status === 'Out' ? 0 : 1) || a.name.localeCompare(b.name));
  }
  return { asOf, teams: [...teams.values()].sort((a, b) => a.name.localeCompare(b.name)) };
}
function injuryPage(report, teamId = 'all', page = 0) {
  const team = report.teams.find(team => team.id === teamId);
  if (teamId !== 'all' && !team) throw new Error('That team is no longer available. Run !nba injury again.');
  const all = team ? team.players : report.teams.flatMap(team => team.players);
  const pages = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
  page = Math.max(0, Math.min(page, pages - 1));
  return { type: 'injuries', asOf: report.asOf, title: team?.name || 'All NBA teams', teamId, teamLogo: team?.logo,
    total: all.length, out: all.filter(player => player.status === 'Out').length, pages, page,
    players: all.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE) };
}
function injuriesHTML(model) {
  const image = (url, css) => /^https:\/\/a\.espncdn\.com\//.test(url || '') ? `<img class="${css}" src="${escape(url)}">` : '';
  const date = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Dublin', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }).format(new Date(model.asOf));
  const cards = model.players.map(player => `<article><div class="player-top">${image(player.headshot, 'portrait')}<div class="identity"><h2>${escape(player.name)}</h2><div class="team">${image(player.teamLogo, 'team-logo')}${escape(player.teamName)}${player.position ? ` · ${escape(player.position)}` : ''}</div></div><span class="pill ${player.status === 'Out' ? 'out' : 'day'}">${escape(player.status)}</span></div>
    <div class="details"><div><span class="label">INJURY / REASON</span><strong>${escape(player.injury)}</strong></div><div><span class="label">EXPECTED RETURN</span><strong class="return">${escape(player.return.label)}</strong><small>${escape(player.return.note)}</small></div></div><p>${escape(player.note)}</p></article>`).join('');
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090d13;color:#f1f5fc;font-family:Arial,sans-serif}main{width:1120px;padding:34px 40px 26px;border-top:5px solid #569eff;background:radial-gradient(ellipse at 100% 0%,#14365088,transparent 50%),#090d13}header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}.eyebrow{font-size:12px;letter-spacing:3px;color:#839ab6;font-weight:700}h1{margin:10px 0 8px;font-size:39px;letter-spacing:-.8px}.subtitle{font-size:17px;color:#a4b8d0;display:flex;align-items:center;gap:8px}.subtitle img{width:26px;height:26px;object-fit:contain}.summary{display:flex;gap:10px}.summary div{padding:13px 17px;text-align:center;border:1px solid #293b52;border-radius:12px;background:#111d2b}.summary b{font-size:29px;color:#fca0a7}.summary span{display:block;font-size:10px;color:#8da4c1;letter-spacing:1px;margin-top:5px}.pagebar{display:flex;justify-content:space-between;margin-bottom:12px;color:#728ba9;font-size:12px;letter-spacing:.5px}.cards{display:grid;grid-template-columns:1fr 1fr;gap:14px}article{background:#111c29;border:1px solid #273b52;border-radius:14px;padding:17px 17px 13px}.player-top{display:flex;align-items:center;gap:10px}.portrait{width:48px;height:48px;object-fit:cover;object-position:top;border-radius:50%;background:#203750;flex-shrink:0}.identity{flex:1;min-width:0}h2{font-size:20px;margin:0 0 5px;line-height:1.2;overflow-wrap:anywhere}.team{display:flex;gap:5px;align-items:center;font-size:11px;color:#809bb9}.team-logo{width:19px;height:19px;object-fit:contain}.pill{max-width:95px;font-size:10px;font-weight:700;letter-spacing:.3px;border-radius:5px;padding:7px;text-align:center;flex-shrink:0}.out{background:#42242d;color:#ffacb5}.day{background:#40371f;color:#f0d18b}.details{display:grid;grid-template-columns:1fr 1fr;gap:16px;border-top:1px solid #26384c;margin-top:14px;padding-top:13px}.label{display:block;font-size:9px;letter-spacing:1.2px;color:#708ba9;margin-bottom:6px}.details strong{display:block;font-size:19px;line-height:1.25;overflow-wrap:anywhere}.return{color:#84bdff}.details small{display:block;font-size:10px;color:#7e96b3;margin-top:5px;line-height:1.4}p{font-size:12px;line-height:1.5;color:#a0b0c6;margin:12px 0 0;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.empty{padding:35px;border:1px solid #293b52;border-radius:12px;font-size:20px;color:#a6bad3}footer{border-top:1px solid #293b52;padding-top:14px;margin-top:22px;display:flex;justify-content:space-between;gap:25px;font-size:11px;line-height:1.6;color:#738ca9}
    .portrait{object-fit:contain;object-position:center}
  </style></head><body><main><header><div><div class="eyebrow">NBA LIVE · AVAILABILITY</div><h1>Injury report</h1><div class="subtitle">${image(model.teamLogo, 'team-logo')}${escape(model.title)}</div></div><div class="summary"><div><b>${model.total}</b><span>LISTED PLAYERS</span></div><div><b>${model.out}</b><span>OUT</span></div></div></header><div class="pagebar"><span>${model.total ? `${model.page * PAGE_SIZE + 1}–${model.page * PAGE_SIZE + model.players.length} of ${model.total} players` : 'No entries'}</span><span>PAGE ${model.page + 1} / ${model.pages}</span></div>${cards ? `<div class="cards">${cards}</div>` : `<div class="empty">No injuries are currently listed by ESPN for ${escape(model.title)}.</div>`}<footer><span>Source: ESPN · Report updated ${escape(date)}<br>Return dates are estimates, not confirmed clearances. TBD = no reported timetable.</span><span>Choose a team below · Use arrows for every player<br>Refresh to check for newer reports</span></footer></main></body></html>`;
}
class Injuries {
  constructor({ data, renderer, now = Date.now, ttl = 3600000, limit = 100 }) {
    Object.assign(this, { data, renderer, now, ttl, limit }); this.entries = new Map();
  }
  owns(id) { return id.startsWith(TEAM_PREFIX) || [PREV, NEXT, REFRESH].includes(id); }
  prune() {
    for (const [id, entry] of this.entries) if (entry.expires <= this.now()) this.entries.delete(id);
    while (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value);
  }
  controls(entry, model) {
    const rows = [], teams = entry.report.teams;
    for (let start = 0; start < teams.length; start += 15) {
      const options = teams.slice(start, start + 15).map(team => ({ label: team.name.slice(0, 100), value: team.id,
        description: `${team.players.length} listed players`, default: entry.teamId === team.id }));
      if (!start) options.unshift({ label: 'All NBA teams', value: 'all', description: 'Every listed player, across all teams', default: entry.teamId === 'all' });
      rows.push(new ActionRowBuilder().addComponents(new StringSelectMenuBuilder().setCustomId(TEAM_PREFIX + start)
        .setPlaceholder(start ? 'Choose a team · second half' : 'Choose a team or view all NBA players').addOptions(options)));
    }
    rows.push(new ActionRowBuilder().addComponents(
      new ButtonBuilder().setCustomId(PREV).setLabel('Previous').setStyle(ButtonStyle.Secondary).setDisabled(model.page === 0),
      new ButtonBuilder().setCustomId('nba-injury-page').setLabel(`${model.page + 1} / ${model.pages}`).setStyle(ButtonStyle.Secondary).setDisabled(true),
      new ButtonBuilder().setCustomId(NEXT).setLabel('Next').setStyle(ButtonStyle.Secondary).setDisabled(model.page >= model.pages - 1),
      new ButtonBuilder().setCustomId(REFRESH).setLabel('Refresh').setStyle(ButtonStyle.Primary)));
    return rows;
  }
  async show(entry) {
    const model = injuryPage(entry.report, entry.teamId, entry.page);
    entry.page = model.page;
    await entry.message.edit({ content: '', attachments: [], embeds: [],
      files: [new AttachmentBuilder(await this.renderer.render(model), { name: 'nba-injuries.png', description: `${model.title} injury report, page ${model.page + 1} of ${model.pages}` })],
      components: this.controls(entry, model), allowedMentions: { parse: [] } });
  }
  async open(message, query = '') {
    const report = await this.data.injuries(); let teamId = 'all';
    if (query.trim()) {
      // Discord sends selected custom emoji as <:name:id> or <a:name:id>.
      // Match the team using its emoji name, never its numeric emoji ID.
      const emoji = query.trim().match(/^<a?:([^:>]+):\d+>$/);
      const q = normalized(emoji ? emoji[1] : query), exact = report.teams.filter(team => normalized(team.name) === q || normalized(team.abbreviation) === q || team.id === query.trim());
      const matches = exact.length ? exact : report.teams.filter(team => normalized(team.name).includes(q));
      if (matches.length !== 1) throw new Error(matches.length ? 'More than one team matches. Use the full team name or run !nba injury and choose a team.' : 'Team not found. Run !nba injury to choose from all teams.');
      teamId = matches[0].id;
    }
    const entry = { message, report, teamId, page: 0, expires: this.now() + this.ttl, queue: Promise.resolve() };
    await this.show(entry); this.entries.set(message.id, entry); this.prune();
  }
  async handle(interaction) {
    this.prune(); const entry = this.entries.get(interaction.message.id);
    if (!entry) { await interaction.reply({ content: 'This report expired or the bot restarted. Run !nba injury again.', flags: 64 }); return; }
    const teamId = interaction.customId.startsWith(TEAM_PREFIX) ? interaction.values?.[0] : undefined;
    if (teamId !== undefined && teamId !== 'all' && !entry.report.teams.some(team => team.id === teamId)) {
      await interaction.reply({ content: 'Choose a team from this report’s menu.', flags: 64 }); return;
    }
    await interaction.deferUpdate();
    const task = entry.queue.then(async () => {
      const previous = { report: entry.report, teamId: entry.teamId, page: entry.page };
      try {
        if (teamId !== undefined || interaction.customId === REFRESH) entry.report = await this.data.injuries();
        if (teamId !== undefined) { entry.teamId = teamId; entry.page = 0; }
        else if (interaction.customId === NEXT) entry.page++;
        else if (interaction.customId === PREV) entry.page--;
        await this.show(entry);
      } catch (error) { Object.assign(entry, previous); throw error; }
    });
    entry.queue = task.catch(() => {});
    try { await task; }
    catch (error) { console.error('Injury report:', error.message); await interaction.followUp({ content: 'Could not update this report just now. Please try again.', flags: 64 }); }
  }
}
module.exports = { Injuries, parseInjuries, returnInfo, injuryPage, injuriesHTML, PAGE_SIZE, TEAM_PREFIX, PREV, NEXT, REFRESH };
