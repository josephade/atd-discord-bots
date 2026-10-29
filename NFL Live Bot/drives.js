// Team-level drive-by-drive summary and quarter score progression. ESPN's NFL play
// feed is attributed by team only (no per-athlete participant list like NBA's play-by-play),
// so this stays honest about what the provider actually supports: no fabricated
// per-player play attribution here. See README for the rationale.
function gameDrives(summary, player) {
  const game = summary.header?.competitions?.[0];
  const state = game?.status?.type?.state;
  const competitors = [...(game?.competitors || [])].sort((a, b) => (a.homeAway === 'away' ? -1 : 1));
  const teams = competitors.map(c => ({
    id: String(c.team?.id || ''), abbreviation: c.team?.abbreviation || '', name: c.team?.displayName || 'Team',
    homeAway: c.homeAway, score: c.score ?? '—', linescores: c.linescores || [],
  }));
  const periodCount = Math.max(4, ...teams.map(t => t.linescores.length));
  const periods = Array.from({ length: periodCount }, (_, i) => ({
    period: i + 1, label: i < 4 ? `Q${i + 1}` : `OT${i - 3}`,
    values: teams.map(t => i < t.linescores.length ? (t.linescores[i]?.displayValue ?? '0') : '—'),
  }));
  const totals = teams.map(t => t.score);
  const rawDrives = summary.drives?.previous || [];
  const available = state !== 'pre' && Array.isArray(rawDrives) && rawDrives.length > 0;
  const playerTeamId = String(player?.team?.id || '');
  const drives = available ? rawDrives.map(drive => ({
    period: drive.start?.period?.number ?? null,
    teamId: String(drive.team?.id || ''),
    teamAbbreviation: drive.team?.abbreviation || '',
    isPlayerTeam: Boolean(playerTeamId) && String(drive.team?.id || '') === playerTeamId,
    start: drive.start?.text || '—',
    plays: drive.offensivePlays ?? drive.plays?.length ?? 0,
    timeOfPossession: drive.timeElapsed?.displayValue || '—',
    yards: Number.isFinite(drive.yards) ? drive.yards : null,
    isScore: Boolean(drive.isScore),
    result: drive.displayResult || drive.shortDisplayResult || drive.result || '—',
  })) : [];
  return { available, teams, periods, totals, drives };
}
module.exports = { gameDrives };
