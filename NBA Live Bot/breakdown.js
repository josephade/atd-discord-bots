// ESPN play-by-play counts; no inferred shot distance or unreported minutes.
const SHOT_TYPES = [
  ['dunk', 'Dunks'], ['layup', 'Layups'], ['floater', 'Floaters'], ['three', '3-pointers'],
  ['shortMid', 'Short mid-range', '10–15 ft'], ['longMid', 'Long mid-range', '16+ ft · 2PT'],
  ['paint', 'Other paint shots', '<10 ft'], ['hook', 'Hook shots'],
  ['tip', 'Tip-ins'], ['other', 'Other 2-pointers', 'Distance unavailable'], ['ft', 'Free throws'],
];
function shotType(play) {
  const text = `${play.type?.text || ''} ${play.text || ''}`.toLowerCase();
  if (/free throw/.test(text) || Number(play.pointsAttempted) === 1) return 'ft';
  if (Number(play.pointsAttempted) === 3 || /three[- ]point|3[- ]point|3pt/.test(text)) return 'three';
  if (/dunk/.test(text)) return 'dunk';
  if (/float|floating/.test(text)) return 'floater';
  if (/layup|lay up|finger roll/.test(text)) return 'layup';
  if (/hook/.test(text)) return 'hook';
  if (/tip[- ]?in|tip shot/.test(text)) return 'tip';
  const distance = text.match(/(\d+(?:\.\d+)?)[- ]foot/);
  if (!distance) return 'other';
  return Number(distance[1]) < 10 ? 'paint' : Number(distance[1]) < 16 ? 'shortMid' : 'longMid';
}
const empty = () => ({ PTS: 0, REB: 0, AST: 0, STL: 0, BLK: 0, TO: 0, FGM: 0, FGA: 0, TPM: 0, TPA: 0, FTM: 0, FTA: 0 });
const folded = text => String(text || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
function gameBreakdown(summary, player, boxStats = {}) {
  const game = summary.header?.competitions?.[0];
  const state = game?.status?.type?.state;
  // Final-game headers may omit period entirely. The plays still identify
  // every quarter/overtime and must not be truncated to Q1 in that case.
  const reportedPeriods = [game?.status?.period, ...(summary.plays || []).map(p => p.period?.number)]
    .map(Number).filter(n => Number.isInteger(n) && n > 0 && n <= 20);
  const current = Math.max(state === 'post' ? 4 : 1, ...reportedPeriods);
  const available = state !== 'pre' && Array.isArray(summary.plays) && summary.plays.length > 0;
  const periods = Array.from({ length: Math.max(4, current) }, (_, i) => ({
    period: i + 1, label: i < 4 ? `Q${i + 1}` : `OT${i - 3}`,
    started: available && i < current, ...empty(),
  }));
  const shots = SHOT_TYPES.map(([key, label, detail]) => ({ key, label, detail, made: 0, attempted: 0, points: 0 }));
  const heaves = [];
  const unique = new Map((available ? summary.plays : []).map((play, i) => [play.id ? String(play.id) : `index:${i}`, play]));
  for (const play of unique.values()) {
    const period = periods[Number(play.period?.number) - 1];
    if (!period?.started) continue;
    const ids = (play.participants || []).map(p => String(p.athlete?.id || ''));
    const primary = ids.length ? ids[0] === String(player.id)
      : folded(play.text).startsWith(folded(player.displayName)) && Boolean(player.displayName);
    const involved = ids.includes(String(player.id));
    const type = String(play.type?.text || '').toLowerCase();
    const text = String(play.text || '');
    // Secondary actors are credited only for the action naming them.
    const secondaryAction = action => {
      if (primary || !involved) return false;
      const clauses = [text, ...Array.from(text.matchAll(/\(([^)]*)\)/g), m => m[1])];
      return clauses.some(clause => {
        const match = clause.match(new RegExp(`^(.+?)\\s+${action}\\b`, 'i'));
        return match && [player.displayName, player.shortName].filter(Boolean).some(name => folded(match[1]) === folded(name));
      });
    };
    if (secondaryAction('assists?') || primary && /^assist$/.test(type)) period.AST++;
    if (secondaryAction('steals?') || primary && /^steal$/.test(type)) period.STL++;
    if (secondaryAction('blocks?') || primary && /^block$/.test(type)) period.BLK++;
    if (!primary) continue;
    if (/rebound/.test(type) && !/team rebound/.test(type)) period.REB++;
    if (/turnover/i.test(type + ' ' + text)) period.TO++;
    const isFT = /free throw/i.test(type + ' ' + text) || Number(play.pointsAttempted) === 1;
    if (!play.shootingPlay && !isFT && !(Number(play.pointsAttempted) >= 2)) continue;
    const category = shotType(play);
    const bucket = shots.find(s => s.key === category);
    const made = /\bmakes\b/i.test(text) || play.scoringPlay === true && Number(play.scoreValue) > 0;
    const missed = /\bmisses\b|\bblocked\b/i.test(text) || play.scoringPlay === false;
    if (!made && !missed) continue;
    const value = category === 'ft' ? 1 : category === 'three' ? 3 : 2;
    bucket.attempted++;
    bucket.made += Number(made);
    bucket.points += made ? value : 0;
    period.PTS += made ? value : 0;
    if (category === 'ft') { period.FTA++; period.FTM += Number(made); }
    else { period.FGA++; period.FGM += Number(made); }
    if (category === 'three') { period.TPA++; period.TPM += Number(made); }
    if (category === 'three' && !made && /heave/i.test(type)) heaves.push({ period, bucket });
  }
  // Newer NBA feeds include missed team-only heaves in the play list. Exclude
  // these only when both official FG and 3PT counts confirm that treatment;
  // historical games which charge the shooter retain their attempts.
  const fg = String(boxStats.FG || '').match(/^(\d+)[-/](\d+)$/);
  const tp = String(boxStats['3PT'] || '').match(/^(\d+)[-/](\d+)$/);
  const sum = key => periods.reduce((total, row) => total + row[key], 0);
  let excludedHeaves = 0;
  if (heaves.length && fg && tp && Number(fg[1]) === sum('FGM') && Number(tp[1]) === sum('TPM')
      && Number(fg[2]) === sum('FGA') - heaves.length && Number(tp[2]) === sum('TPA') - heaves.length) {
    for (const { period, bucket } of heaves) { period.FGA--; period.TPA--; bucket.attempted--; }
    excludedHeaves = heaves.length;
  }
  const totals = periods.reduce((all, period) => {
    for (const key of Object.keys(all)) all[key] += period[key];
    return all;
  }, empty());
  const checks = [];
  for (const key of ['PTS', 'REB', 'AST', 'STL', 'BLK', 'TO']) {
    if (/^\d+$/.test(String(boxStats[key]))) checks.push([Number(boxStats[key]), totals[key]]);
  }
  for (const [key, made, attempted] of [['FG', 'FGM', 'FGA'], ['3PT', 'TPM', 'TPA'], ['FT', 'FTM', 'FTA']]) {
    const pair = String(boxStats[key] || '').match(/^(\d+)[-/](\d+)$/);
    if (pair) checks.push([Number(pair[1]), totals[made]], [Number(pair[2]), totals[attempted]]);
  }
  const partial = available && (!checks.length || checks.some(([box, counted]) => box !== counted));
  return { available, partial, current, periods, shots, totals, excludedHeaves };
}
module.exports = { SHOT_TYPES, shotType, gameBreakdown };
