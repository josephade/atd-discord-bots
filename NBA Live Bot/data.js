const { gameBreakdown } = require('./breakdown');
const { parseInjuries } = require('./injuries');
const SITE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba';
const WEB = 'https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba';
const normalize = s => s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
function editDistance(a, b) {
  let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    for (let j = 1; j <= b.length; j++) row[j] = Math.min(row[j - 1] + 1, previous[j] + 1, previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    previous = row;
  }
  return previous[b.length];
}
function playerSuggestions(query, players) {
  const q = normalize(query);
  if (q.length < 4 || /^\d+$/.test(q)) return [];
  const limit = Math.min(3, Math.max(1, Math.floor(q.length / 4)));
  const close = players.map(player => {
    const names = [normalize(player.displayName)];
    if (!query.trim().includes(' ')) names.push(...player.displayName.split(/\s+/).map(normalize));
    return { player, distance: Math.min(...names.map(name => editDistance(q, name))) };
  }).filter(match => match.distance <= limit)
    .sort((a, b) => a.distance - b.distance || a.player.displayName.localeCompare(b.player.displayName))
    .map(match => match.player);
  const extra = wordSuggestions(query, players).filter(player => !close.includes(player));
  return [...close, ...extra].slice(0, 3);
}
// Word-by-word matching for nicknames and partial surnames ("Victor Wemby" -> Victor
// Wembanyama): every typed word must match a word of the name exactly, as its start, by a
// shared start of 4+ letters, or within one typo. Costs rank the closest names first.
function wordSuggestions(query, players) {
  const words = query.trim().split(/\s+/).map(normalize).filter(Boolean);
  if (!words.length || normalize(query).length < 4) return [];
  const shared = (a, b) => { let i = 0; while (i < a.length && a[i] === b[i]) i++; return i; };
  const cost = (word, name) => {
    if (word === name) return 0;
    if (name.startsWith(word) && word.length >= 2) return 1;
    const prefix = shared(word, name);
    if (prefix >= 4 && prefix * 2 >= word.length) return 2 + (word.length - prefix);
    if (word.length >= 4 && editDistance(word, name) <= 1) return 2;
    return Infinity;
  };
  return players.map(player => {
    const names = player.displayName.split(/\s+/).map(normalize).filter(Boolean);
    const total = words.reduce((sum, word) => sum + Math.min(...names.map(name => cost(word, name))), 0);
    return { player, total };
  }).filter(match => Number.isFinite(match.total) && match.total > 0)
    .sort((a, b) => a.total - b.total || a.player.displayName.localeCompare(b.player.displayName))
    .slice(0, 3).map(match => match.player);
}
// Common nicknames -> full ESPN display name. Keys are matched after normalize(),
// so case, spaces and punctuation don't matter ("PG-13", "pg 13" and "PG13" all work).
const NICKNAMES = Object.fromEntries(Object.entries({
  'KD': 'Kevin Durant', 'Durantula': 'Kevin Durant', 'Slim Reaper': 'Kevin Durant',
  'SGA': 'Shai Gilgeous-Alexander', 'Shai': 'Shai Gilgeous-Alexander',
  'PG13': 'Paul George', 'PG': 'Paul George',
  'Tatum': 'Jayson Tatum', 'JT': 'Jayson Tatum',
  'Bron': 'LeBron James', 'King James': 'LeBron James', 'LBJ': 'LeBron James',
  'Steph': 'Stephen Curry', 'Chef Curry': 'Stephen Curry',
  'AD': 'Anthony Davis', 'The Brow': 'Anthony Davis',
  'Ant': 'Anthony Edwards', 'Ant Man': 'Anthony Edwards',
  'Greek Freak': 'Giannis Antetokounmpo', 'Giannis': 'Giannis Antetokounmpo',
  'Joker': 'Nikola Jokic', 'Wemby': 'Victor Wembanyama',
  'Dame': 'Damian Lillard', 'Dame Time': 'Damian Lillard',
  'Book': 'Devin Booker', 'Spida': 'Donovan Mitchell',
  'Jimmy Buckets': 'Jimmy Butler III', 'Embiid': 'Joel Embiid', 'JoJo': 'Joel Embiid',
  'Zion': 'Zion Williamson', 'Ja': 'Ja Morant', 'Trae': 'Trae Young', 'Ice Trae': 'Trae Young',
  'Kawhi': 'Kawhi Leonard', 'The Claw': 'Kawhi Leonard', 'Brunson': 'Jalen Brunson',
  'Cade': 'Cade Cunningham', 'Chet': 'Chet Holmgren', 'Banchero': 'Paolo Banchero',
  'Kyrie': 'Kyrie Irving', 'Uncle Drew': 'Kyrie Irving', 'Harden': 'James Harden', 'The Beard': 'James Harden',
  'Luka': 'Luka Doncic', 'Bam': 'Bam Adebayo', 'Hali': 'Tyrese Haliburton', 'Maxey': 'Tyrese Maxey',
}).map(([nickname, name]) => [normalize(nickname), name]));
const seasonLabel = year => `${year - 1}-${String(year).slice(-2)}`;
const mapStats = (labels, stats) => Object.fromEntries(labels.map((key, i) => [key, stats[i] ?? '—']));
const nbaDate = value => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(value));
function appearances(log) {
  const seen = new Set();
  return (log.seasonTypes || []).flatMap(s => (s.categories || []).filter(c => c.type === 'event').flatMap(c => c.events || []))
    .filter(e => {
      if (seen.has(e.eventId)) return false;
      seen.add(e.eventId);
      const meta = log.events?.[e.eventId];
      return meta?.gameDate && !/all[ -]?star/i.test(meta.eventNote || '') && /^\d+(\.\d+)?$/.test(String(mapStats(log.labels, e.stats).PTS));
    }).map(e => ({ eventId: e.eventId, date: nbaDate(log.events[e.eventId].gameDate) }));
}
function seasonDistance(year, target) {
  const start = Date.parse(`${year - 1}-07-01`), end = Date.parse(`${year}-06-30`);
  return target < start ? start - target : target > end ? target - end : 0;
}
function shots(value) {
  const match = String(value).match(/^(\d+(?:\.\d+)?)[-/](\d+(?:\.\d+)?)$/);
  return match ? match.slice(1).map(Number) : null;
}
function derived(s) {
  const fg = shots(s.FG), ft = shots(s.FT), threes = shots(s['3PT']);
  const num = key => /^[-+]?\d+(\.\d+)?$/.test(String(s[key])) ? Number(s[key]) : null;
  const fantasyKeys = ['PTS', 'REB', 'AST', 'STL', 'BLK', 'TO'];
  return {
    ...s,
    'FG%': fg?.[1] ? (100 * fg[0] / fg[1]).toFixed(1) : '—',
    'TS%': fg && ft && num('PTS') !== null && fg[1] + .44 * ft[1] > 0
      ? (100 * num('PTS') / (2 * (fg[1] + .44 * ft[1]))).toFixed(1) : '—',
    // ESPN default head-to-head points scoring, using ESPN box-score counts.
    FP: fg && ft && threes && fantasyKeys.every(k => num(k) !== null)
      ? (num('PTS') + 2 * fg[0] - fg[1] + ft[0] - ft[1] + threes[0]
        + num('REB') + 2 * num('AST') + 4 * num('STL') + 4 * num('BLK') - 2 * num('TO')).toFixed(1) : '—',
  };
}
function extractGame(summary, player) {
  const game = summary.header?.competitions?.[0];
  if (!game) throw new Error('The game feed is missing its status. Try again shortly.');
  let found, team;
  for (const group of summary.boxscore?.players || []) {
    for (const block of group.statistics || []) {
      const row = block.athletes?.find(a => String(a.athlete.id) === String(player.id));
      if (row) { found = { row, stats: mapStats(block.labels || block.names, row.stats || []) }; team = group.team; }
    }
  }
  const competitors = [...(game.competitors || [])].sort((a, b) => (a.homeAway === 'away' ? -1 : 1));
  const state = game.status?.type?.state;
  const status = game.status?.type?.shortDetail || game.status?.type?.description || 'Status unavailable';
  const dnp = found?.row.didNotPlay;
  return {
    player: { ...player, ...(found?.row.athlete || {}), team: team || player.team },
    eventId: summary.header.id, state, status, date: game.date,
    matchup: competitors.map(c => `${c.team.abbreviation} ${c.score ?? '—'}`).join('  ·  '),
    scoreboard: competitors.map(c => ({ abbreviation: c.team.abbreviation, score: c.score ?? '—', logo: c.team.logo || c.team.logos?.[0]?.href, homeAway: c.homeAway })),
    note: state === 'pre' ? 'Tip-off pending' : dnp ? `DNP · ${found.row.reason || 'Did not play'}`
      : !found ? 'No player box score available yet' : '',
    stats: derived(dnp || !found ? {} : found.stats),
    breakdown: gameBreakdown(summary, { ...player, ...found?.row.athlete }, dnp || !found ? {} : found.stats),
    played: Boolean(found && !dnp && found.row.stats?.length && /^\d+(\.\d+)?$/.test(String(found.stats.PTS))),
    season: summary.header.season,
  };
}
function extractBoxscore(summary) {
  const game = summary.header?.competitions?.[0];
  if (!game) throw new Error('The game feed is missing its status. Please retry shortly.');
  const competitors = [...(game.competitors || [])].sort((a, b) => (a.homeAway === 'away' ? -1 : 1));
  const teams = competitors.map(competitor => {
    const group = (summary.boxscore?.players || []).find(g => String(g.team?.id) === String(competitor.team?.id));
    const players = new Map(); let totals;
    for (const block of group?.statistics || []) {
      const labels = block.labels || block.names || [];
      if (Array.isArray(block.totals) && block.totals.length) totals = mapStats(labels, block.totals);
      for (const row of block.athletes || []) {
        const id = row.athlete?.id || row.athlete?.displayName;
        if (!id) continue;
        players.set(String(id), { id: String(id), name: row.athlete.displayName,
          starter: Boolean(row.starter), dnp: Boolean(row.didNotPlay), reason: row.reason || 'Did not play',
          stats: { ...(players.get(String(id))?.stats || {}), ...mapStats(labels, row.stats || []) } });
      }
    }
    return { name: competitor.team?.displayName || group?.team?.displayName || 'Team',
      abbreviation: competitor.team?.abbreviation, logo: competitor.team?.logo || competitor.team?.logos?.[0]?.href,
      score: competitor.score ?? '—', side: competitor.homeAway, totals,
      players: [...players.values()].sort((a, b) => Number(b.starter) - Number(a.starter)) };
  });
  return { type: 'boxscore', eventId: summary.header.id, date: game.date,
    state: game.status?.type?.state, status: game.status?.type?.shortDetail || game.status?.type?.description || 'Status unavailable', teams };
}
function seasonStats(log, year, playoffs = false) {
  const selected = log.filters?.find(f => f.name === 'season')?.value;
  if (String(selected) !== String(year)) throw new Error('The provider did not return the requested season.');
  const kind = playoffs ? 'Postseason' : 'Regular Season';
  const group = log.seasonTypes?.find(s => s.displayName === `${seasonLabel(year)} ${kind}`);
  const seen = new Set();
  const rows = (group?.categories || []).filter(c => c.type === 'event').flatMap(c => c.events || [])
    .filter(e => { if (seen.has(e.eventId)) return false; seen.add(e.eventId); return true; })
    // ESPN also lists All-Star appearances inside its regular-season game log.
    // Cup group/quarter/semi games count; the championship and play-in do not.
    .filter(e => !/all[ -]?star|nba cup.*(?:championship|\bfinal\b)|play[ -]?in/i.test(log.events?.[e.eventId]?.eventNote || ''))
    .map(e => mapStats(log.labels, e.stats)).filter(s => /^\d+(\.\d+)?$/.test(String(s.PTS)));
  if (!rows.length) return { stats: {}, games: 0, label: `${seasonLabel(year)} ${kind}` };
  const totals = {};
  for (const key of ['MIN', 'PTS', 'REB', 'AST', 'STL', 'BLK', 'TO', 'PF']) {
    totals[key] = rows.reduce((sum, s) => {
      const parts = String(s[key]).split(':').map(Number);
      return sum + (Number.isFinite(parts[0]) ? parts[0] + (parts[1] || 0) / 60 : 0);
    }, 0);
  }
  for (const key of ['FG', '3PT', 'FT']) {
    const pairs = rows.map(s => shots(s[key]));
    totals[key] = pairs.every(Boolean) ? pairs.reduce((a, p) => [a[0] + p[0], a[1] + p[1]], [0, 0]).join('-') : '—';
  }
  const calculated = derived(totals), stats = {};
  for (const key of ['MIN', 'PTS', 'REB', 'AST', 'STL', 'BLK', 'TO', 'PF']) stats[key] = (totals[key] / rows.length).toFixed(1);
  for (const key of ['FG', '3PT', 'FT']) stats[key] = shots(totals[key])?.map(n => (n / rows.length).toFixed(1)).join('/') || '—';
  for (const key of ['FG%', 'TS%']) stats[key] = calculated[key];
  for (const [key, shot] of [['3P%', '3PT'], ['FT%', 'FT']]) { const p = shots(totals[shot]); stats[key] = p?.[1] ? (100 * p[0] / p[1]).toFixed(1) : '—'; }
  stats.FP = calculated.FP === '—' ? '—' : (Number(calculated.FP) / rows.length).toFixed(1);
  return { stats, games: rows.length, label: `${seasonLabel(year)} ${kind}` };
}
class NBAData {
  constructor() { this.cache = new Map(); this.pending = new Map(); }
  async json(url, ttl = 25000) {
    const cached = this.cache.get(url);
    if (ttl !== 0 && cached && cached.expires > Date.now()) return cached.data;
    if (this.pending.has(url)) return this.pending.get(url);
    const task = (async () => {
      const res = await fetch(url, { signal: AbortSignal.timeout(15000), headers: { 'User-Agent': 'ATD-NBA-Live/1.0', Accept: 'application/json' } });
      if (!res.ok) throw new Error(`NBA data provider returned HTTP ${res.status}. Try again shortly.`);
      const data = await res.json();
      // Bound memory when many historical dates are requested.
      if (this.cache.size > 300) this.cache.delete(this.cache.keys().next().value);
      this.cache.set(url, { data, expires: Date.now() + ttl });
      return data;
    })();
    this.pending.set(url, task);
    try { return await task; } finally { this.pending.delete(url); }
  }
  async league() {
    const data = await this.json(`${SITE}/teams?limit=100`, 3600000);
    const league = data.sports?.[0]?.leagues?.[0];
    if (!league?.teams?.length) throw new Error('NBA team directory is unavailable.');
    return league;
  }
  async injuries() {
    const [feed, league] = await Promise.all([this.json(`${SITE}/injuries?limit=1000`, 60000), this.league()]);
    return parseInjuries(feed, league.teams.map(entry => entry.team));
  }
  async findTeam(query) {
    // Discord sends selected custom emoji as <:name:id>; match on the emoji name.
    const emoji = query.trim().match(/^<a?:([^:>]+):\d+>$/);
    const q = normalize(emoji ? emoji[1] : query);
    if (!q) throw new Error('Include a team. Example: !nba schedule Lakers');
    const teams = (await this.league()).teams.map(entry => entry.team);
    const names = team => [team.displayName, team.abbreviation, team.shortDisplayName, team.name, team.location].filter(Boolean).map(normalize);
    const exact = teams.filter(team => names(team).includes(q) || String(team.id) === query.trim());
    const matches = exact.length ? exact : teams.filter(team => normalize(team.displayName).includes(q));
    if (matches.length !== 1) throw new Error(matches.length ? `More than one team matches: ${matches.map(t => t.displayName).join(', ')}. Use the full team name.` : `No NBA team matches “${query}”. Try a name such as Lakers or LAL.`);
    return matches[0];
  }
  async salary(query) {
    const { parseContracts, bbrefCode } = require('./salary');
    const team = await this.findTeam(query);
    const url = `https://www.basketball-reference.com/contracts/${bbrefCode(team.abbreviation)}.html`;
    let cached = this.cache.get(url);
    if (!cached || cached.expires <= Date.now()) {
      // Payrolls change slowly; a 6-hour cache keeps requests to that site rare.
      const res = await fetch(url, { signal: AbortSignal.timeout(15000), headers: { 'User-Agent': 'ATD-NBA-Live/1.0', Accept: 'text/html' } });
      if (!res.ok) throw new Error(`The payroll source returned HTTP ${res.status}. Try again shortly.`);
      cached = { data: { ...parseContracts(await res.text()), asOf: new Date().toISOString() }, expires: Date.now() + 6 * 3600000 };
      this.cache.set(url, cached);
    }
    return { ...cached.data, team: { name: team.displayName, abbreviation: team.abbreviation, logo: team.logos?.[0]?.href, color: team.color } };
  }
  async schedule(query, { upcoming = 8, recent = 3 } = {}) {
    const team = await this.findTeam(query);
    const feed = await this.json(`${SITE}/teams/${team.id}/schedule`, 60000);
    if (!Array.isArray(feed.events)) throw new Error('That team schedule is unavailable. Please try again shortly.');
    const score = competitor => competitor?.score?.displayValue ?? competitor?.score ?? '—';
    const games = feed.events.flatMap(event => {
      const competition = event.competitions?.[0];
      const type = competition?.status?.type || event.status?.type;
      const us = competition?.competitors?.find(c => String(c.team?.id) === String(team.id));
      const them = competition?.competitors?.find(c => c !== us);
      if (!type?.state || !us || !them) return [];
      return [{ id: event.id, date: competition.date || event.date, state: type.state,
        status: type.shortDetail || type.description || 'Scheduled', stopped: /POSTPONED|CANCELED|CANCELLED|SUSPENDED/.test(type.name || ''),
        home: us.homeAway === 'home',
        opponent: { name: them.team?.displayName || them.team?.abbreviation || 'Opponent', abbreviation: them.team?.abbreviation || '?', logo: them.team?.logos?.[0]?.href || them.team?.logo },
        result: type.state === 'post' && us.winner !== undefined ? (us.winner ? 'W' : 'L') : '',
        score: `${score(us)}–${score(them)}`,
        broadcast: (competition.broadcasts || []).map(b => b.media?.shortName).filter(Boolean)[0] || '' }];
    }).sort((a, b) => (Date.parse(a.date) || 0) - (Date.parse(b.date) || 0));
    const played = games.filter(g => g.state === 'post' && g.result);
    const wins = played.filter(g => g.result === 'W').length;
    return { type: 'schedule', team: { name: team.displayName, abbreviation: team.abbreviation, logo: team.logos?.[0]?.href, color: team.color },
      season: feed.requestedSeason?.displayName ? `${feed.requestedSeason.displayName} ${feed.requestedSeason.name || ''}`.trim() : '',
      record: played.length ? `${wins}-${played.length - wins}` : '0-0',
      recent: played.slice(-recent).reverse(),
      upcoming: games.filter(g => g.state !== 'post').slice(0, upcoming) };
  }
  async games({ all = false, date, refresh = false } = {}) {
    // The active slate keeps late West Coast games with their NBA night,
    // even after midnight in the viewer's local time zone.
    const board = await this.json(`${SITE}/scoreboard${date ? `?dates=${date.replaceAll('-', '')}` : ''}`, refresh ? 0 : 25000);
    if (!Array.isArray(board.events)) throw new Error('The NBA scoreboard is unavailable. Please try again shortly.');
    const games = board.events.flatMap(event => {
      const competition = event.competitions?.[0];
      const status = competition?.status || event.status;
      const type = status?.type;
      if (!type?.state || !all && (!['in', 'pre'].includes(type.state) || /POSTPONED|CANCELLED|CANCELED|SUSPENDED/.test(type.name || ''))) return [];
      const teams = competition?.competitors || [];
      const team = side => {
        const competitor = teams.find(t => t.homeAway === side);
        return { name: competitor?.team?.displayName || competitor?.team?.abbreviation || 'Team unavailable',
          abbreviation: competitor?.team?.abbreviation || '?', score: competitor?.score ?? '—',
          logo: competitor?.team?.logo || competitor?.team?.logos?.[0]?.href };
      };
      return [{ id: event.id, state: type.state, date: competition?.date || event.date,
        status: type.shortDetail || type.description || 'Status unavailable',
        away: team('away'), home: team('home') }];
    }).sort((a, b) => (a.state === 'in' ? 0 : 1) - (b.state === 'in' ? 0 : 1)
      || (Date.parse(a.date) || 0) - (Date.parse(b.date) || 0));
    // ESPN keeps serving the finished night until well into the next US morning;
    // once nothing is live or upcoming there, roll forward to the next night's slate.
    if (!all && !date && !games.length && /^\d{4}-\d{2}-\d{2}$/.test(board.day?.date || '')) {
      const next = new Date(`${board.day.date}T12:00:00Z`);
      next.setUTCDate(next.getUTCDate() + 1);
      const nextSlate = await this.games({ date: next.toISOString().slice(0, 10), refresh });
      if (nextSlate.games.length) return nextSlate;
    }
    return { date: board.day?.date || date || '', games };
  }
  async boxscore(eventId, { refresh = false } = {}) {
    return extractBoxscore(await this.json(`${SITE}/summary?event=${encodeURIComponent(eventId)}`, refresh ? 0 : 25000));
  }
  async players() {
    if (this.directory?.expires > Date.now()) return this.directory.players;
    if (this.directoryPending) return this.directoryPending;
    this.directoryPending = this.loadPlayers().then(players => {
      this.directory = { players, expires: Date.now() + 3600000 };
      return players;
    }).finally(() => { this.directoryPending = null; });
    return this.directoryPending;
  }
  async loadPlayers() {
    const league = await this.league();
    const players = [];
    // Small batches keep the first directory load from hammering the provider.
    for (let i = 0; i < league.teams.length; i += 5) {
      const results = await Promise.all(league.teams.slice(i, i + 5).map(async ({ team }) => {
        const roster = await this.json(`${SITE}/teams/${team.id}/roster`, 3600000);
        return (roster.athletes || []).map(a => ({ ...a, team }));
      }));
      players.push(...results.flat());
    }
    return [...new Map(players.map(p => [p.id, p])).values()];
  }
  async findPlayer(query) {
    // A known nickname ("KD", "SGA", "PG13") stands in for the full name before any matching.
    query = NICKNAMES[normalize(query)] || query;
    const players = await this.players(), q = normalize(query);
    if (!q) throw new Error('Enter a player name, for example: !nba Paul George');
    const exact = players.filter(p => normalize(p.displayName) === q || p.id === query);
    const matches = exact.length ? exact : players.filter(p => normalize(p.displayName).includes(q) || normalize(p.shortName || '') === q);
    if (!matches.length) {
      const error = new Error(`No current NBA player matches “${query}”. Try their full name.`);
      error.suggestions = playerSuggestions(query, players);
      throw error;
    }
    if (matches.length > 1) throw new Error(`More than one match: ${matches.slice(0, 8).map(p => p.displayName).join(', ')}. Use the full name.`);
    return matches[0];
  }
  async game(player, date, eventId) {
    if (!eventId) {
      // ESPN's no-date scoreboard chooses its active NBA slate, including games after midnight.
      const board = await this.json(`${SITE}/scoreboard${date ? `?dates=${date.replaceAll('-', '')}` : ''}`);
      const events = (board.events || []).filter(e => e.competitions?.[0]?.competitors?.some(c => String(c.team.id) === String(player.team.id)));
      events.sort((a, b) => ({ in: 0, pre: 1, post: 2 }[a.status?.type?.state] ?? 3) - ({ in: 0, pre: 1, post: 2 }[b.status?.type?.state] ?? 3));
      if (!events.length) {
        if (!date) return this.latestGame(player);
        throw new Error(`${player.displayName}'s team has no game on ${date}. Try another date or use !season for season averages.`);
      }
      eventId = events[0].id;
    }
    return extractGame(await this.json(`${SITE}/summary?event=${eventId}`), player);
  }
  async datedGame(player, date) {
    // Scan the whole slate: a historical appearance may be for a former team.
    const board = await this.json(`${SITE}/scoreboard?dates=${date.replaceAll('-', '')}`);
    const events = (board.events || []).filter(e => e.status?.type?.state !== 'pre');
    for (let i = 0; i < events.length; i += 4) {
      const models = await Promise.all(events.slice(i, i + 4).map(e => this.game(player, null, e.id)));
      const match = models.find(m => m.played);
      if (match) return { model: match };
    }
    const nearest = await this.closestAppearance(player, date);
    if (nearest?.date === date) return { model: await this.game(player, null, nearest.eventId) };
    return { suggestion: nearest };
  }
  async latestGame(player, now = new Date()) {
    const latest = await this.closestAppearance(player, nbaDate(now), true);
    if (!latest) throw new Error(`No recorded played game was found for ${player.displayName}. Try a specific date or !season.`);
    const model = await this.game(player, null, latest.eventId);
    if (!model.played) throw new Error('The latest game log and box score disagree. Try a specific date or retry shortly.');
    return { ...model, note: [model.note, 'Latest played game'].filter(Boolean).join(' · ') };
  }
  async closestAppearance(player, date, pastOnly = false) {
    const currentYear = (await this.league()).season.year;
    const targetYear = Number(date.slice(0, 4)) + (Number(date.slice(5, 7)) >= 7 ? 1 : 0);
    const firstYear = Math.min(targetYear, currentYear);
    const load = year => this.json(`${WEB}/athletes/${player.id}/gamelog?season=${year}`, 60000);
    const first = await load(firstYear);
    const options = first.filters?.find(f => f.name === 'season')?.options || [];
    const years = [...new Set([firstYear, ...options.map(o => Number(o.value))])].filter(y => Number.isInteger(y) && y >= 2003 && y <= currentYear);
    const target = Date.parse(date);
    years.sort((a, b) => seasonDistance(a, target) - seasonDistance(b, target));
    let best, distance = Infinity;
    for (const year of years) {
      if (seasonDistance(year, target) > distance) break;
      const log = year === firstYear ? first : await load(year);
      if (String(log.filters?.find(f => f.name === 'season')?.value) !== String(year)) throw new Error('The provider did not return the requested game-log season.');
      for (const entry of appearances(log)) {
        if (pastOnly && entry.date > date) continue;
        const delta = Math.abs(Date.parse(entry.date) - target);
        if (delta < distance || (delta === distance && entry.date < best.date)) { best = entry; distance = delta; }
      }
    }
    return best;
  }
  // Splits "A vs B" / "A, B" / "A | B", or with no separator tries every split point.
  // Returns sides as { player } (resolved) or { query, suggestions } (needs a Yes/No).
  async comparePlayers(input) {
    const attempt = async query => {
      try { return { player: await this.findPlayer(query) }; }
      catch (error) { return error.suggestions?.length ? { query, suggestions: error.suggestions } : null; }
    };
    const explicit = input.split(/\s+(?:vs\.?|v)\s+|\s*[,|]\s*/i).map(s => s.trim()).filter(Boolean);
    if (explicit.length === 2) {
      const sides = await Promise.all(explicit.map(attempt));
      const bad = explicit.find((_, i) => !sides[i]);
      if (bad) throw new Error(`No current NBA player matches “${bad}”. Try their full name.`);
      return sides;
    }
    if (explicit.length > 2) throw new Error('Compare two players at a time, e.g. !nba compare LeBron James vs Stephen Curry');
    const words = input.trim().split(/\s+/);
    if (words.length < 2) throw new Error('Name two players, e.g. !nba compare LeBron James vs Stephen Curry');
    let partial;
    for (let i = 1; i < words.length; i++) {
      const sides = await Promise.all([words.slice(0, i).join(' '), words.slice(i).join(' ')].map(attempt));
      if (sides[0]?.player && sides[1]?.player && sides[0].player.id !== sides[1].player.id) return sides;
      if (!partial && sides[0] && sides[1] && (sides[0].player || sides[1].player)) partial = sides;
    }
    if (partial) return partial;
    throw new Error('Could not tell the two players apart. Put "vs" between them, e.g. !nba compare LeBron James vs Stephen Curry');
  }
  async standings({ refresh = false } = {}) {
    const { parseStandings } = require('./standings');
    return parseStandings(await this.json('https://site.api.espn.com/apis/v2/sports/basketball/nba/standings', refresh ? 0 : 300000));
  }
  async teamRecord(team) {
    if (!team?.id) return '';
    try { return (await this.json(`${SITE}/teams/${team.id}`, 600000)).team?.record?.items?.[0]?.summary || ''; }
    catch { return ''; }
  }
  async compare(players) {
    const sides = await Promise.all(players.map(async player => {
      const [season, record] = await Promise.all([this.season(player), this.teamRecord(player.team)]);
      return { player, stats: season.stats, games: season.games, record, label: season.label };
    }));
    return { type: 'compare', sides, label: sides[0].label };
  }
  async lastGames(player, count = 10) {
    // Current season only (regular season + playoffs, no preseason or All-Star): early in
    // the season this shows fewer than `count` games rather than reaching into last season.
    const current = (await this.league()).season.year;
    const games = [];
    for (const year of [current]) {
      const log = await this.json(`${WEB}/athletes/${player.id}/gamelog?season=${year}`, 60000);
      if (String(log.filters?.find(f => f.name === 'season')?.value) !== String(year)) continue;
      const seen = new Set();
      for (const type of log.seasonTypes || []) {
        if (!/regular season|postseason/i.test(type.displayName || '')) continue;
        for (const e of (type.categories || []).filter(c => c.type === 'event').flatMap(c => c.events || [])) {
          const meta = log.events?.[e.eventId];
          if (seen.has(e.eventId) || !meta?.gameDate || /all[ -]?star/i.test(meta.eventNote || '')) continue;
          const stats = mapStats(log.labels, e.stats);
          if (!/^\d+(\.\d+)?$/.test(String(stats.PTS))) continue;
          seen.add(e.eventId);
          const home = meta.atVs !== '@';
          const ours = home ? meta.homeTeamScore : meta.awayTeamScore, theirs = home ? meta.awayTeamScore : meta.homeTeamScore;
          games.push({ id: e.eventId, date: meta.gameDate, home, playoffs: /postseason/i.test(type.displayName),
            opponent: { abbreviation: meta.opponent?.abbreviation || '?', logo: meta.opponent?.logo },
            result: meta.gameResult || '', score: ours && theirs ? `${ours}-${theirs}` : meta.score || '',
            stats: derived(stats) });
        }
      }
    }
    games.sort((a, b) => Date.parse(b.date) - Date.parse(a.date));
    const recent = games.slice(0, count);
    if (!recent.length) throw new Error(`${player.displayName} has not played a game this season yet. Try !season for last season's averages.`);
    const sum = key => recent.reduce((t, g) => t + (Number(String(g.stats[key]).split(':')[0]) || 0), 0);
    const avg = key => (sum(key) / recent.length).toFixed(1);
    const made = (key, i) => recent.reduce((t, g) => t + (shots(g.stats[key])?.[i] || 0), 0);
    const pct = key => made(key, 1) ? (100 * made(key, 0) / made(key, 1)).toFixed(1) : '—';
    const fp = recent.map(g => Number(g.stats.FP)).filter(Number.isFinite);
    const averages = { MIN: avg('MIN'), PTS: avg('PTS'), REB: avg('REB'), AST: avg('AST'), STL: avg('STL'), BLK: avg('BLK'), TO: avg('TO'),
      'FG%': pct('FG'), '3P%': pct('3PT'), 'FT%': pct('FT'), FP: fp.length ? (fp.reduce((a, b) => a + b, 0) / fp.length).toFixed(1) : '—' };
    const wins = recent.filter(g => g.result === 'W').length;
    return { type: 'last10', player, games: recent, averages, record: `${wins}-${recent.filter(g => g.result === 'L').length}` };
  }
  async season(player, year, playoffs) {
    year ||= (await this.league()).season.year;
    const log = await this.json(`${WEB}/athletes/${player.id}/gamelog?season=${year}`, 60000);
    const result = seasonStats(log, year, playoffs);
    return { player, ...result, status: result.label, matchup: `${result.games} games · per-game averages`, note: result.games ? 'Completed recorded appearances · shooting shown as makes/attempts per game' : 'No recorded appearances for this season yet', state: 'season' };
  }
}
module.exports = { NBAData, normalize, derived, shots, extractGame, extractBoxscore, seasonStats, seasonLabel, appearances, playerSuggestions };
