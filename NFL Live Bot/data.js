const { gameDrives } = require('./drives');
const { parseInjuries } = require('./injuries');
const SITE = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl';
const WEB = 'https://site.web.api.espn.com/apis/common/v3/sports/football/nfl';
const normalize = s => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
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
  return players.map(player => {
    const names = [normalize(player.displayName)];
    if (!query.trim().includes(' ')) names.push(...player.displayName.split(/\s+/).map(normalize));
    return { player, distance: Math.min(...names.map(name => editDistance(q, name))) };
  }).filter(match => match.distance <= limit)
    .sort((a, b) => a.distance - b.distance || a.player.displayName.localeCompare(b.player.displayName))
    .slice(0, 3).map(match => match.player);
}
// ESPN's NFL season value is the STARTING calendar year (e.g. 2025 covers Sep 2025 - Feb 2026),
// the opposite of NBA's ending-year convention.
const seasonLabel = year => String(year);
const mapStats = (keys, stats) => Object.fromEntries(keys.map((key, i) => [key, stats[i] ?? '—']));
const nflDate = value => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(value));
function appearances(log) {
  const seen = new Set();
  return (log.seasonTypes || []).flatMap(s => (s.categories || []).filter(c => c.type === 'event').flatMap(c => c.events || []))
    .filter(e => {
      if (seen.has(e.eventId)) return false;
      seen.add(e.eventId);
      const meta = log.events?.[e.eventId];
      if (!meta?.gameDate || /pro[ -]?bowl/i.test(meta.eventNote || '') || meta.team?.isAllStar || meta.opponent?.isAllStar) return false;
      return (e.stats || []).some(v => /^-?\d+(\.\d+)?$/.test(String(v)));
    }).map(e => ({ eventId: e.eventId, date: nflDate(log.events[e.eventId].gameDate) }));
}
function seasonDistance(year, target) {
  // NFL's season runs roughly August through the Super Bowl in early-to-mid February.
  const start = Date.parse(`${year}-08-01`), end = Date.parse(`${year + 1}-03-01`) - 86400000;
  return target < start ? start - target : target > end ? target - end : 0;
}
// ESPN's scoreboard "calendar" lists Preseason/Regular Season/Postseason groups (an
// unlabelled fourth "Off Season" group with no entries is skipped), each with its own
// week entries in order — this is the ordered list the Previous/Next Week browser walks.
function flattenCalendar(raw) {
  const out = [];
  for (const group of raw || []) {
    const seasonType = Number(group.value);
    if (!Number.isInteger(seasonType) || !Array.isArray(group.entries)) continue;
    for (const entry of group.entries) {
      out.push({ seasonType, week: Number(entry.value), label: entry.label, detail: entry.detail,
        startDate: entry.startDate, endDate: entry.endDate });
    }
  }
  return out;
}
function shots(value) {
  const match = String(value).match(/^(\d+(?:\.\d+)?)[-/](\d+(?:\.\d+)?)$/);
  return match ? match.slice(1).map(Number) : null;
}
// ESPN's own fixed boxscore category order; also used to pick a player's primary category.
const CATEGORY_ORDER = ['passing', 'rushing', 'receiving', 'fumbles', 'defensive', 'interceptions', 'kickReturns', 'puntReturns', 'kicking', 'punting'];
const CATEGORY_TITLES = {
  passing: 'Passing', rushing: 'Rushing', receiving: 'Receiving', fumbles: 'Fumbles',
  defensive: 'Defensive', interceptions: 'Interceptions', kickReturns: 'Kick Returns',
  puntReturns: 'Punt Returns', kicking: 'Kicking', punting: 'Punting',
};
// Hero stat keys shown on the card front, chosen from the player's primary category.
const HERO_FIELDS = {
  passing: ['completions/passingAttempts', 'passingYards', 'passingTouchdowns', 'interceptions'],
  rushing: ['rushingAttempts', 'rushingYards', 'rushingTouchdowns', 'longRushing'],
  receiving: ['receptions', 'receivingYards', 'receivingTouchdowns', 'receivingTargets'],
  defensive: ['totalTackles', 'sacks', 'passesDefended', 'tacklesForLoss'],
  interceptions: ['interceptions', 'interceptionYards', 'interceptionTouchdowns'],
  fumbles: ['fumbles', 'fumblesLost', 'fumblesRecovered'],
  kickReturns: ['kickReturns', 'kickReturnYards', 'kickReturnTouchdowns', 'longKickReturn'],
  puntReturns: ['puntReturns', 'puntReturnYards', 'puntReturnTouchdowns', 'longPuntReturn'],
  kicking: ['fieldGoalsMade/fieldGoalAttempts', 'extraPointsMade/extraPointAttempts', 'totalKickingPoints', 'longFieldGoalMade'],
  punting: ['punts', 'puntYards', 'grossAvgPuntYards', 'longPunt'],
};
// Extra grid stats shown underneath the hero row for the primary category.
const GRID_FIELDS = {
  passing: ['yardsPerPassAttempt', 'sacks-sackYardsLost', 'QBRating', 'adjQBR'],
  rushing: ['yardsPerRushAttempt'],
  receiving: ['yardsPerReception', 'longReception'],
  defensive: ['soloTackles', 'QBHits', 'defensiveTouchdowns'],
  interceptions: [],
  fumbles: [],
  kickReturns: ['yardsPerKickReturn'],
  puntReturns: ['yardsPerPuntReturn'],
  kicking: ['fieldGoalPct'],
  punting: ['touchbacks', 'puntsInside20'],
};
// Secondary categories worth a compact extra line on the card (e.g. a QB who also rushed).
const SECONDARY_CATEGORIES = { passing: ['rushing'], rushing: ['receiving'], receiving: ['rushing'] };
// Season-average models have no boxscore "categories" list to read a primary
// category from (see extractGame), so detect it generically from which stat keys exist.
const PRIMARY_PROBE = {
  passing: 'passingYards', rushing: 'rushingYards', receiving: 'receivingYards',
  defensive: 'totalTackles', interceptions: 'interceptionYards', fumbles: 'fumblesLost',
  kickReturns: 'kickReturnYards', puntReturns: 'puntReturnYards',
  kicking: 'totalKickingPoints', punting: 'punts',
};
function detectPrimary(stats = {}) {
  return CATEGORY_ORDER.find(key => stats[PRIMARY_PROBE[key]] !== undefined) || null;
}
// Display labels for the HERO_FIELDS/GRID_FIELDS keys above, matching ESPN's own
// box-score column headers so the season-average card (no boxscore blocks of its own)
// can show the same labels as the per-game card.
const FIELD_LABELS = {
  'completions/passingAttempts': 'C/ATT', passingYards: 'YDS', passingTouchdowns: 'TD', interceptions: 'INT',
  yardsPerPassAttempt: 'AVG', 'sacks-sackYardsLost': 'SACKS', QBRating: 'RTG', adjQBR: 'QBR',
  rushingAttempts: 'CAR', rushingYards: 'YDS', rushingTouchdowns: 'TD', longRushing: 'LONG', yardsPerRushAttempt: 'AVG',
  receptions: 'REC', receivingYards: 'YDS', receivingTouchdowns: 'TD', receivingTargets: 'TGTS', yardsPerReception: 'AVG', longReception: 'LONG',
  totalTackles: 'TOT', sacks: 'SACKS', passesDefended: 'PD', tacklesForLoss: 'TFL', soloTackles: 'SOLO', QBHits: 'QB HTS', defensiveTouchdowns: 'TD',
  interceptionYards: 'YDS', interceptionTouchdowns: 'TD',
  fumbles: 'FUM', fumblesLost: 'LOST', fumblesRecovered: 'REC',
  kickReturns: 'NO', kickReturnYards: 'YDS', kickReturnTouchdowns: 'TD', longKickReturn: 'LONG', yardsPerKickReturn: 'AVG',
  puntReturns: 'NO', puntReturnYards: 'YDS', puntReturnTouchdowns: 'TD', longPuntReturn: 'LONG', yardsPerPuntReturn: 'AVG',
  'fieldGoalsMade/fieldGoalAttempts': 'FG', 'extraPointsMade/extraPointAttempts': 'XP', totalKickingPoints: 'PTS', longFieldGoalMade: 'LONG', fieldGoalPct: 'PCT',
  punts: 'NO', puntYards: 'YDS', grossAvgPuntYards: 'AVG', longPunt: 'LONG', touchbacks: 'TB', puntsInside20: 'In 20',
};
function num(stats, key) {
  return /^[-+]?\d+(\.\d+)?$/.test(String(stats[key])) ? Number(stats[key]) : null;
}
// ESPN standard PPR scoring (matches ESPN's own FPTS). Kickers: 3/FG under 40 yds,
// 4/FG 40-49, 5/FG 50+, -1/missed FG, 1/XP. The single-game card takes made-FG
// distances from the game's scoring plays (fieldGoalDistances); !season rows use the
// gamelog's distance buckets. Defensive-only players have no single universally
// "default" IDP formula, so no FP tile is shown for them (see README).
const fgValue = yards => (yards >= 50 ? 5 : yards >= 40 ? 4 : 3);
const FG_BUCKETS = [['1_19', 3], ['20_29', 3], ['30_39', 3], ['40_49', 4], ['50', 5]];
function fieldGoalPoints(stats, fg) {
  const missed = fg[1] - fg[0];
  if (Array.isArray(stats.fieldGoalDistances)) return stats.fieldGoalDistances.reduce((t, y) => t + fgValue(y), 0) - missed;
  const buckets = FG_BUCKETS.map(([b, pts]) => [shots(stats[`fieldGoalsMade${b}-fieldGoalAttempts${b}`]), pts]);
  if (buckets.some(([pair]) => pair)) return buckets.reduce((t, [pair, pts]) => t + pts * (pair?.[0] ?? 0), 0) - missed;
  return 3 * fg[0] - missed;
}
function derived(stats = {}) {
  // Box-score keys use a slash ("fieldGoalsMade/fieldGoalAttempts"); the season
  // gamelog's equivalent combined field uses a hyphen. Accept either spelling.
  const fg = shots(stats['fieldGoalsMade/fieldGoalAttempts'] ?? stats['fieldGoalsMade-fieldGoalAttempts']);
  const xp = shots(stats['extraPointsMade/extraPointAttempts'] ?? stats['extraPointsMade-extraPointAttempts']);
  const passing = stats.passingYards !== undefined, rushing = stats.rushingYards !== undefined;
  const receiving = stats.receivingYards !== undefined, kicking = Boolean(fg || xp);
  let FP = '—';
  if (passing || rushing || receiving || kicking) {
    let total = 0;
    if (passing) total += 0.04 * (num(stats, 'passingYards') ?? 0) + 4 * (num(stats, 'passingTouchdowns') ?? 0) - 2 * (num(stats, 'interceptions') ?? 0);
    if (rushing) total += 0.1 * (num(stats, 'rushingYards') ?? 0) + 6 * (num(stats, 'rushingTouchdowns') ?? 0);
    if (receiving) total += (num(stats, 'receptions') ?? 0) + 0.1 * (num(stats, 'receivingYards') ?? 0) + 6 * (num(stats, 'receivingTouchdowns') ?? 0);
    if (num(stats, 'fumblesLost') !== null) total -= 2 * num(stats, 'fumblesLost');
    if (kicking) total += (fg ? fieldGoalPoints(stats, fg) : 0) + (xp?.[0] ?? 0);
    FP = total.toFixed(1);
  }
  return { ...stats, FP };
}
function extractGame(summary, player) {
  const game = summary.header?.competitions?.[0];
  if (!game) throw new Error('The game feed is missing its status. Try again shortly.');
  let team, dnp, reason, flatStats = {};
  const categories = [];
  for (const group of summary.boxscore?.players || []) {
    for (const block of group.statistics || []) {
      const row = block.athletes?.find(a => String(a.athlete.id) === String(player.id));
      if (!row) continue;
      team = group.team;
      if (row.didNotPlay) { dnp = true; reason = row.reason; }
      const keys = block.keys || block.labels || [];
      const stats = mapStats(keys, row.stats || []);
      categories.push({ key: block.name, label: CATEGORY_TITLES[block.name] || block.name, keys, labels: block.labels || keys, values: row.stats || [], athlete: row.athlete });
      flatStats = { ...flatStats, ...stats };
    }
  }
  const found = categories.length > 0;
  const competitors = [...(game.competitors || [])].sort((a, b) => (a.homeAway === 'away' ? -1 : 1));
  const state = game.status?.type?.state;
  const status = game.status?.type?.shortDetail || game.status?.type?.description || 'Status unavailable';
  categories.sort((a, b) => CATEGORY_ORDER.indexOf(a.key) - CATEGORY_ORDER.indexOf(b.key));
  const primary = categories[0]?.key;
  // Box scores carry no per-kick distance; the scoring plays do ("Chase McLaughlin 44 Yd Field Goal").
  const made = shots(flatStats['fieldGoalsMade/fieldGoalAttempts'])?.[0];
  if (made) {
    const name = normalize(categories[0]?.athlete?.displayName || player.displayName || '');
    const distances = (summary.scoringPlays || []).filter(p => p.type?.abbreviation === 'FG')
      .map(p => (p.text || '').match(/^(.*?)\s+(\d+)\s+Yd\s+Field Goal/i)).filter(m => m && normalize(m[1]) === name).map(m => Number(m[2]));
    if (distances.length === made) flatStats.fieldGoalDistances = distances;
  }
  return {
    player: { ...player, ...(categories[0]?.athlete || {}), team: team || player.team },
    eventId: summary.header.id, state, status, date: game.date,
    matchup: competitors.map(c => `${c.team.abbreviation} ${c.score ?? '—'}`).join('  ·  '),
    scoreboard: competitors.map(c => ({ abbreviation: c.team.abbreviation, score: c.score ?? '—', logo: c.team.logo || c.team.logos?.[0]?.href, homeAway: c.homeAway })),
    note: state === 'pre' ? 'Kickoff pending' : dnp ? `Inactive · ${reason || 'Did not play'}`
      : !found ? 'No player box score available yet' : '',
    period: game.status?.period, primary, categories,
    stats: derived(dnp || !found ? {} : flatStats),
    drives: gameDrives(summary, { ...player, ...categories[0]?.athlete }),
    played: Boolean(found && !dnp),
    season: summary.header.season,
  };
}
function extractBoxscore(summary) {
  const game = summary.header?.competitions?.[0];
  if (!game) throw new Error('The game feed is missing its status. Please retry shortly.');
  const competitors = [...(game.competitors || [])].sort((a, b) => (a.homeAway === 'away' ? -1 : 1));
  const teams = competitors.map(competitor => {
    const group = (summary.boxscore?.players || []).find(g => String(g.team?.id) === String(competitor.team?.id));
    const categories = (group?.statistics || []).filter(block => block.athletes?.length).map(block => ({
      key: block.name, label: CATEGORY_TITLES[block.name] || block.name,
      labels: block.labels || block.keys || [], totals: block.totals,
      players: (block.athletes || []).map(row => ({
        id: String(row.athlete?.id || row.athlete?.displayName), name: row.athlete?.displayName,
        starter: Boolean(row.starter), dnp: Boolean(row.didNotPlay), reason: row.reason || 'Did not play',
        values: row.stats || [],
      })),
    }));
    return { name: competitor.team?.displayName || group?.team?.displayName || 'Team',
      abbreviation: competitor.team?.abbreviation, logo: competitor.team?.logo || competitor.team?.logos?.[0]?.href,
      score: competitor.score ?? '—', side: competitor.homeAway, categories };
  });
  return { type: 'boxscore', eventId: summary.header.id, date: game.date,
    state: game.status?.type?.state, status: game.status?.type?.shortDetail || game.status?.type?.description || 'Status unavailable', teams };
}
// "Long" fields are a season best, not a sum. Percent/ratio fields are recomputed
// from summed totals (a weighted season rate), not averaged game-by-game.
const MAX_KEYS = new Set(['longPassing', 'longRushing', 'longReception', 'longKickReturn', 'longPuntReturn', 'longFieldGoalMade', 'longPunt', 'longInterception']);
const PERCENT_KEYS = { completionPct: ['completions', 'passingAttempts'], fieldGoalPct: ['fieldGoalsMade', 'fieldGoalAttempts'] };
const RATIO_KEYS = {
  yardsPerPassAttempt: ['passingYards', 'passingAttempts'], yardsPerRushAttempt: ['rushingYards', 'rushingAttempts'],
  yardsPerReception: ['receivingYards', 'receptions'], yardsPerKickReturn: ['kickReturnYards', 'kickReturns'],
  yardsPerPuntReturn: ['puntReturnYards', 'puntReturns'], grossAvgPuntYards: ['puntYards', 'punts'],
};
// Reconstructed as box-score-style "made/attempts" display strings once totals are known.
const DISPLAY_PAIRS = {
  'completions/passingAttempts': ['completions', 'passingAttempts', '/'],
  'fieldGoalsMade/fieldGoalAttempts': ['fieldGoalsMade', 'fieldGoalAttempts', '/'],
  'extraPointsMade/extraPointAttempts': ['extraPointsMade', 'extraPointAttempts', '/'],
  'sacks-sackYardsLost': ['sacks', 'sackYardsLost', '-'],
};
function seasonStats(log, year, playoffs = false) {
  const selected = log.filters?.find(f => f.name === 'season')?.value;
  if (String(selected) !== String(year)) throw new Error('The provider did not return the requested season.');
  const kind = playoffs ? 'Postseason' : 'Regular Season';
  const group = log.seasonTypes?.find(s => s.displayName === `${seasonLabel(year)} ${kind}`);
  const seen = new Set();
  const rows = (group?.categories || []).filter(c => c.type === 'event').flatMap(c => c.events || [])
    .filter(e => { if (seen.has(e.eventId)) return false; seen.add(e.eventId); return true; })
    .filter(e => !/pro[ -]?bowl/i.test(log.events?.[e.eventId]?.eventNote || ''))
    .map(e => mapStats(log.names, e.stats)).filter(s => Object.values(s).some(v => /^-?\d+(\.\d+)?$/.test(String(v))));
  const label = `${seasonLabel(year)} ${kind}`;
  if (!rows.length) return { stats: {}, games: 0, label };
  // Split any "made-attempts" composite gamelog fields (e.g. distance-bucketed kicking)
  // into their two plain counting stats, keyed by the two halves of the field's own name.
  const expand = row => {
    const out = {};
    for (const [key, value] of Object.entries(row)) {
      const pair = shots(value);
      if (pair && key.includes('-')) { const [madeKey, attKey] = key.split('-'); out[madeKey] = pair[0]; out[attKey] = pair[1]; }
      else if (pair) { out[`${key}Made`] = pair[0]; out[`${key}Attempts`] = pair[1]; }
      else out[key] = /^-?\d+(\.\d+)?$/.test(String(value)) ? Number(value) : value;
    }
    return out;
  };
  const expanded = rows.map(expand);
  const keys = [...new Set(expanded.flatMap(Object.keys))];
  const totals = {};
  for (const key of keys) {
    const values = expanded.map(r => r[key]).filter(v => typeof v === 'number');
    if (!values.length) continue;
    totals[key] = MAX_KEYS.has(key) ? Math.max(...values) : values.reduce((a, b) => a + b, 0);
  }
  const games = rows.length;
  const rateKeyNames = new Set([...Object.keys(PERCENT_KEYS), ...Object.keys(RATIO_KEYS)]);
  const stats = {};
  for (const key of keys) {
    if (totals[key] === undefined || rateKeyNames.has(key)) continue;
    stats[key] = MAX_KEYS.has(key) ? String(totals[key]) : (totals[key] / games).toFixed(1);
  }
  for (const [key, [made, attempted]] of Object.entries(PERCENT_KEYS)) {
    if (totals[made] !== undefined && totals[attempted]) stats[key] = (100 * totals[made] / totals[attempted]).toFixed(1);
  }
  for (const [key, [num, den]] of Object.entries(RATIO_KEYS)) {
    if (totals[num] !== undefined && totals[den]) stats[key] = (totals[num] / totals[den]).toFixed(1);
  }
  for (const [displayKey, [madeKey, attKey, sep]] of Object.entries(DISPLAY_PAIRS)) {
    if (totals[madeKey] !== undefined && totals[attKey] !== undefined) stats[displayKey] = `${totals[madeKey]}${sep}${totals[attKey]}`;
  }
  const perGameFP = rows.map(row => Number(derived(row).FP)).filter(Number.isFinite);
  stats.FP = perGameFP.length ? (perGameFP.reduce((a, b) => a + b, 0) / games).toFixed(1) : '—';
  return { stats, games, label };
}
class NFLData {
  constructor() { this.cache = new Map(); this.pending = new Map(); }
  async json(url, ttl = 25000) {
    const cached = this.cache.get(url);
    if (ttl !== 0 && cached && cached.expires > Date.now()) return cached.data;
    if (this.pending.has(url)) return this.pending.get(url);
    const task = (async () => {
      const res = await fetch(url, { signal: AbortSignal.timeout(15000), headers: { 'User-Agent': 'ATD-NFL-Live/1.0', Accept: 'application/json' } });
      if (!res.ok) throw new Error(`NFL data provider returned HTTP ${res.status}. Try again shortly.`);
      const data = await res.json();
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
    if (!league?.teams?.length) throw new Error('NFL team directory is unavailable.');
    return league;
  }
  async injuries() {
    const [feed, league] = await Promise.all([this.json(`${SITE}/injuries?limit=1000`, 60000), this.league()]);
    return parseInjuries(feed, league.teams.map(entry => entry.team));
  }
  // Resolves a specific calendar date to the NFL week that contains it (the league
  // plays Thu/Sun/Mon, not nightly, so a single literal date is otherwise almost
  // always empty — see flattenCalendar/games below for the week-based browsing this feeds).
  async weekInfo(date) {
    const board = await this.json(`${SITE}/scoreboard?dates=${date.replaceAll('-', '')}`);
    const season = board.leagues?.[0]?.season;
    if (!season?.type?.type || !season.year) throw new Error(`No NFL season information was found for ${date}.`);
    if (board.week?.number) return { week: board.week.number, seasonType: season.type.type, year: season.year };
    // ESPN omits "week" entirely when no games landed on this exact date (most days,
    // since the league plays Thu/Sun/Mon) — fall back to the calendar's date ranges.
    const target = Date.parse(`${date}T12:00:00Z`);
    const match = flattenCalendar(board.leagues?.[0]?.calendar)
      .find(entry => Date.parse(entry.startDate) <= target && target <= Date.parse(entry.endDate));
    if (!match) throw new Error(`No NFL week was found for ${date}.`);
    return { week: match.week, seasonType: match.seasonType, year: season.year };
  }
  async games({ all = false, date, week, seasonType, year, refresh = false } = {}) {
    if (date) ({ week, seasonType, year } = await this.weekInfo(date));
    const params = week && seasonType && year ? `?week=${week}&seasontype=${seasonType}&dates=${year}` : '';
    const board = await this.json(`${SITE}/scoreboard${params}`, refresh ? 0 : 25000);
    if (!Array.isArray(board.events)) throw new Error('The NFL scoreboard is unavailable. Please try again shortly.');
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
    const calendar = flattenCalendar(board.leagues?.[0]?.calendar);
    const resolvedSeason = board.leagues?.[0]?.season;
    const resolvedWeek = board.week?.number;
    const resolvedType = resolvedSeason?.type?.type;
    const current = calendar.find(entry => entry.seasonType === resolvedType && entry.week === resolvedWeek);
    return { week: resolvedWeek, seasonType: resolvedType, year: resolvedSeason?.year,
      label: current?.label || (resolvedWeek ? `Week ${resolvedWeek}` : 'Current week'), detail: current?.detail || '',
      calendar, games };
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
    for (let i = 0; i < league.teams.length; i += 5) {
      const results = await Promise.all(league.teams.slice(i, i + 5).map(async ({ team }) => {
        const roster = await this.json(`${SITE}/teams/${team.id}/roster`, 3600000);
        // NFL rosters come grouped by position ({ position, items: [...] }); flatten to athletes.
        return (roster.athletes || []).flatMap(a => a.items || [a]).map(a => ({ ...a, team }));
      }));
      players.push(...results.flat());
    }
    return [...new Map(players.map(p => [p.id, p])).values()];
  }
  async findPlayer(query) {
    const players = await this.players(), q = normalize(query);
    if (!q) throw new Error('Enter a player name, for example: !nfl Patrick Mahomes');
    const exact = players.filter(p => normalize(p.displayName) === q || p.id === query);
    const matches = exact.length ? exact : players.filter(p => normalize(p.displayName).includes(q) || normalize(p.shortName || '') === q);
    if (!matches.length) {
      const error = new Error(`No current NFL player matches “${query}”. Try their full name.`);
      error.suggestions = playerSuggestions(query, players);
      throw error;
    }
    if (matches.length > 1) throw new Error(`More than one match: ${matches.slice(0, 8).map(p => p.displayName).join(', ')}. Use the full name.`);
    return matches[0];
  }
  async game(player, date, eventId, { preferPlayed = false } = {}) {
    if (!eventId) {
      const board = await this.json(`${SITE}/scoreboard${date ? `?dates=${date.replaceAll('-', '')}` : ''}`);
      const events = (board.events || []).filter(e => e.competitions?.[0]?.competitors?.some(c => String(c.team.id) === String(player.team.id)));
      events.sort((a, b) => ({ in: 0, pre: 1, post: 2 }[a.status?.type?.state] ?? 3) - ({ in: 0, pre: 1, post: 2 }[b.status?.type?.state] ?? 3));
      if (!events.length) {
        if (!date) return this.latestGame(player);
        throw new Error(`${player.displayName}'s team has no game on ${date}. Try another date or use !season for season averages.`);
      }
      eventId = events[0].id;
      // Not live: show the player's last statline instead of an upcoming/missed game.
      if (preferPlayed && !date && events[0].status?.type?.state !== 'in') {
        const model = extractGame(await this.json(`${SITE}/summary?event=${eventId}`), player);
        if (model.played) return model;
        return this.latestGame(player).catch(() => model);
      }
    }
    return extractGame(await this.json(`${SITE}/summary?event=${eventId}`), player);
  }
  async datedGame(player, date) {
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
    const latest = await this.closestAppearance(player, nflDate(now), true);
    if (!latest) throw new Error(`No recorded played game was found for ${player.displayName}. Try a specific date or !season.`);
    const model = await this.game(player, null, latest.eventId);
    if (!model.played) throw new Error('The latest game log and box score disagree. Try a specific date or retry shortly.');
    return { ...model, note: [model.note, 'Latest played game'].filter(Boolean).join(' · ') };
  }
  async closestAppearance(player, date, pastOnly = false) {
    const currentYear = (await this.league()).season.year;
    const targetYear = Number(date.slice(0, 4)) - (Number(date.slice(5, 7)) <= 2 ? 1 : 0);
    const firstYear = Math.min(targetYear, currentYear);
    const load = year => this.json(`${WEB}/athletes/${player.id}/gamelog?season=${year}`, 60000);
    const first = await load(firstYear);
    const options = first.filters?.find(f => f.name === 'season')?.options || [];
    const years = [...new Set([firstYear, ...options.map(o => Number(o.value))])].filter(y => Number.isInteger(y) && y >= 1999 && y <= currentYear);
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
  async season(player, year, playoffs) {
    year ||= (await this.league()).season.year;
    const log = await this.json(`${WEB}/athletes/${player.id}/gamelog?season=${year}`, 60000);
    const result = seasonStats(log, year, playoffs);
    return { player, ...result, status: result.label, matchup: `${result.games} games · per-game averages`, note: result.games ? 'Completed recorded appearances · rates recomputed from season totals' : 'No recorded appearances for this season yet', state: 'season' };
  }
}
module.exports = { NFLData, normalize, derived, shots, extractGame, extractBoxscore, seasonStats, seasonLabel, appearances, playerSuggestions, detectPrimary, flattenCalendar, CATEGORY_ORDER, CATEGORY_TITLES, HERO_FIELDS, GRID_FIELDS, SECONDARY_CATEGORIES, FIELD_LABELS };
