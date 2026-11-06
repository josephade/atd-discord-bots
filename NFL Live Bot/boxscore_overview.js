const escape = value => String(value ?? '—').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const numeric = value => /^-?\d+(\.\d+)?$/.test(String(value)) ? Number(value) : null;

function leaders(team) {
  const specs = [
    ['passing', 'passingYards', 'YDS', 'Passing leader', [['passingYards', 'YDS', 'pass yds'], ['passingTouchdowns', 'TD', 'TD'], ['interceptions', 'INT', 'INT']]],
    ['rushing', 'rushingYards', 'YDS', 'Rushing leader', [['rushingYards', 'YDS', 'rush yds'], ['rushingAttempts', 'CAR', 'carries'], ['rushingTouchdowns', 'TD', 'TD']]],
    ['receiving', 'receivingYards', 'YDS', 'Receiving leader', [['receivingYards', 'YDS', 'rec yds'], ['receptions', 'REC', 'catches'], ['receivingTouchdowns', 'TD', 'TD']]],
    ['defensive', 'totalTackles', 'TOT', 'Tackle leader', [['totalTackles', 'TOT', 'tackles'], ['sacks', 'SACKS', 'sacks'], ['tacklesForLoss', 'TFL', 'TFL']]],
  ];
  return specs.flatMap(([key, rankKey, rankLabel, label, stats]) => {
    const category = team.categories?.find(c => c.key === key);
    if (!category) return [];
    const value = (player, statKey, statLabel) => {
      const keyed = category.keys?.indexOf(statKey) ?? -1;
      const index = keyed >= 0 ? keyed : category.labels.indexOf(statLabel);
      return index < 0 ? null : player.values[index];
    };
    const candidates = category.players.filter(p => !p.dnp && numeric(value(p, rankKey, rankLabel)) !== null);
    candidates.sort((a, b) => numeric(value(b, rankKey, rankLabel)) - numeric(value(a, rankKey, rankLabel)));
    const player = candidates[0];
    if (!player) return [];
    return [{ label, name: player.name, stats: stats.map(([statKey, statLabel, unit]) =>
      `${value(player, statKey, statLabel) ?? '—'} ${unit}`).join(' · ') }];
  });
}

function overviewHTML(model) {
  const teams = model.teams || [];
  const periods = Math.max(4, ...teams.map(t => t.linescores?.length || 0));
  const scoreHeader = Array.from({ length: periods }, (_, i) => `<th>${i < 4 ? `Q${i + 1}` : i === 4 ? 'OT' : `${i - 3}OT`}</th>`).join('');
  const quarters = `<div class="overview-title">SCORE BY QUARTER</div><table class="quarter-table"><thead><tr><th>TEAM</th>${scoreHeader}<th>TOTAL</th></tr></thead><tbody>${teams.map(t => `<tr><th>${escape(t.abbreviation || t.name)}</th>${Array.from({ length: periods }, (_, i) => `<td>${escape(t.linescores?.[i])}</td>`).join('')}<td class="score-total">${model.state === 'pre' ? '—' : escape(t.score)}</td></tr>`).join('')}</tbody></table>`;
  const hasScores = teams.length === 2 && teams.every(t => numeric(t.score) !== null);
  const winner = hasScores ? [...teams].sort((a, b) => Number(b.score) - Number(a.score))[0] : null;
  const margin = hasScores ? Math.abs(Number(teams[0].score) - Number(teams[1].score)) : 0;
  const headline = model.state === 'pre' ? 'Kickoff pending — game leaders appear once play begins.'
    : hasScores ? margin ? `${winner.name} ${model.state === 'post' ? 'win' : 'lead'} by ${margin}${/ot|overtime/i.test(model.status || '') ? ' in overtime' : ''}.`
      : model.state === 'post' ? 'The game finishes tied.' : 'All tied up.' : 'Live game overview';
  const standouts = teams.map(team => {
    const rows = model.state === 'pre' ? [] : leaders(team);
    const logo = /^https:\/\/a\.espncdn\.com\//.test(team.logo || '') ? `<img src="${escape(team.logo)}">` : '';
    return `<div class="leader-team"><div class="leader-heading">${logo}<div><b>${escape(team.name)}${team.record ? `<em class="team-record">${escape(team.record)}</em>` : ''}</b><span>${team.side === 'away' ? 'AWAY' : 'HOME'} · GAME LEADERS</span></div></div>${rows.length ? rows.map(p => `<div class="leader"><span>${escape(p.label)}</span><b>${escape(p.name)}</b><p>${escape(p.stats)}</p></div>`).join('') : '<div class="leader-empty">Player stats unavailable yet.</div>'}</div>`;
  }).join('');
  const comparisons = [['totalYards', 'TOTAL YARDS'], ['firstDowns', 'FIRST DOWNS'], ['thirdDownEff', '3RD DOWN'], ['turnovers', 'TURNOVERS'], ['possessionTime', 'POSSESSION']];
  const comparison = teams.length === 2 ? `<div class="overview-title">TEAM COMPARISON</div><div class="comparison">${comparisons.map(([key, label]) => `<div><b>${escape(teams[0].teamStats?.[key])}</b><span>${label}</span><b>${escape(teams[1].teamStats?.[key])}</b></div>`).join('')}</div>` : '';
  return `<div class="game-headline">${escape(headline)}</div>${quarters}<div class="overview-title">STANDOUT PLAYERS</div><div class="leader-grid">${standouts}</div>${comparison}`;
}

const overviewCSS = `
.game-headline{font-size:21px;color:#c8d9eb;margin:22px 0 24px}.overview-title{font-size:13px;letter-spacing:2px;font-weight:700;color:#81bfff;margin:22px 0 12px}.quarter-table th,.quarter-table td{font-size:18px}.quarter-table th:first-child{width:260px}.score-total{color:#80c2ff;font-weight:700}.leader-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.leader-team{border:1px solid #263b50;background:#101b27;border-radius:14px;overflow:hidden}.leader-heading{padding:18px;display:flex;align-items:center;gap:12px;background:#182a3b}.leader-heading img{height:42px;width:42px;object-fit:contain}.leader-heading b{font-size:20px;display:block}.leader-heading span{display:block;color:#8ca7c2;font-size:10px;letter-spacing:1.5px;margin-top:5px}.leader{padding:14px 18px;border-top:1px solid #25384b}.leader>span{display:block;text-transform:uppercase;font-size:11px;letter-spacing:1.5px;color:#839cb7}.leader>b{display:block;font-size:23px;margin:6px 0}.leader p{font-size:17px;color:#91c9ff;margin:0}.leader-empty{padding:24px;color:#91a8bd}.comparison{border:1px solid #263b50;border-radius:12px;overflow:hidden}.comparison>div{display:grid;grid-template-columns:1fr 1fr 1fr;text-align:center;align-items:center;padding:12px 20px;font-size:20px}.comparison>div:nth-child(odd){background:#132131}.comparison span{font-size:12px;letter-spacing:1px;color:#90a8c1}
`;
module.exports = { overviewHTML, overviewCSS, leaders };
