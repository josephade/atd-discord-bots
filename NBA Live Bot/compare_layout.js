const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const espnImage = url => /^https:\/\/a\.espncdn\.com\//.test(url || '') ? url : '';
// [label, key, which way is better]. Games played is shown but never "won".
const ROWS = [
  ['GAMES', 'GP', 0], ['MINUTES', 'MIN', 1], ['POINTS', 'PTS', 1], ['REBOUNDS', 'REB', 1], ['ASSISTS', 'AST', 1],
  ['STEALS', 'STL', 1], ['BLOCKS', 'BLK', 1], ['TURNOVERS', 'TO', -1], ['FG%', 'FG%', 1], ['3P%', '3P%', 1],
  ['FT%', 'FT%', 1], ['TS%', 'TS%', 1], ['FANTASY PTS', 'FP', 1],
];
const value = v => (/^-?\d+(\.\d+)?$/.test(String(v)) ? Number(v) : null);
const winPct = record => { const m = String(record || '').match(/^(\d+)-(\d+)$/); return m && Number(m[1]) + Number(m[2]) ? Number(m[1]) / (Number(m[1]) + Number(m[2])) : null; };
// Returns 0 or 1 for the better side, or -1 for a tie / nothing to compare.
function leader(a, b, direction) {
  if (!direction || a === null || b === null || a === b) return -1;
  return (a > b) === (direction > 0) ? 0 : 1;
}
function compareHTML({ sides, label }) {
  const colors = sides.map(s => (/^[a-f0-9]{6}$/i.test(s.player.team?.color || '') ? `#${s.player.team.color}` : '#258bea'));
  const stats = sides.map(s => ({ ...s.stats, GP: String(s.games) }));
  const wins = [0, 0];
  const rows = ROWS.map(([title, key, direction]) => {
    const values = stats.map(st => st[key] ?? '—');
    const best = leader(value(values[0]), value(values[1]), direction);
    if (best >= 0) wins[best]++;
    const cell = i => `<div class="val ${best === i ? 'best' : best >= 0 ? 'dim' : ''} ${i ? 'right' : 'left'}" style="--c:${colors[i]}">${escape(values[i])}</div>`;
    return `<div class="row">${cell(0)}<div class="label">${title}${direction < 0 ? '<small>LOWER IS BETTER</small>' : ''}</div>${cell(1)}</div>`;
  }).join('');
  const recordBest = leader(winPct(sides[0].record), winPct(sides[1].record), 1);
  const head = (s, i) => {
    const team = s.player.team || {};
    const shot = espnImage(s.player.headshot?.href), logo = espnImage(team.logo || team.logos?.[0]?.href);
    return `<div class="player ${i ? 'right' : 'left'}" style="--c:${colors[i]}">${shot ? `<img class="portrait" src="${escape(shot)}">` : '<div class="portrait"></div>'}<h1>${escape(s.player.displayName)}</h1><div class="team">${logo ? `<img src="${escape(logo)}">` : ''}${escape(team.displayName || '')}${s.player.position?.abbreviation ? ` · ${escape(s.player.position.abbreviation)}` : ''}</div>
      <div class="record ${recordBest === i ? 'best' : recordBest >= 0 ? 'dim' : ''}"><b>${escape(s.record || '—')}</b><span>TEAM RECORD</span></div><div class="tally"><b>${wins[i]}</b> categories led</div></div>`;
  };
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090d13;font-family:Arial,sans-serif;color:#f3f6fa}main{width:1120px;padding:34px 44px 24px;border-top:5px solid;border-image:linear-gradient(90deg,${colors[0]},${colors[1]}) 1;background:radial-gradient(ellipse at 0% 0%,${colors[0]}33,transparent 45%),radial-gradient(ellipse at 100% 0%,${colors[1]}33,transparent 45%),#090d13}
    .eyebrow{text-align:center;font-size:13px;letter-spacing:3px;color:#8193a9;font-weight:700;margin-bottom:22px}.top{display:grid;grid-template-columns:1fr 70px 1fr;align-items:start}.vs{align-self:center;text-align:center;font-size:26px;font-weight:800;color:#5d7088}
    .player{display:flex;flex-direction:column;align-items:center;text-align:center}.portrait{width:150px;height:150px;border-radius:50%;object-fit:cover;object-position:top;background:#16222f;border:3px solid var(--c)}h1{font-size:33px;margin:16px 0 8px;letter-spacing:-.6px}.team{display:flex;align-items:center;gap:8px;font-size:16px;color:#a5b3c5}.team img{width:24px;height:24px;object-fit:contain}
    .record{margin-top:16px;padding:10px 26px;border-radius:12px;border:1px solid #273749;background:#111b28}.record b{display:block;font-size:28px;font-variant-numeric:tabular-nums}.record span{display:block;font-size:10px;letter-spacing:1.3px;color:#91a4bc;margin-top:4px}.tally{margin-top:12px;font-size:14px;color:#9fb2c9}.tally b{color:#fff;font-size:18px}
    .stats{margin-top:26px;background:#111a26;border:1px solid #243448;border-radius:16px;overflow:hidden}.row{display:grid;grid-template-columns:1fr 220px 1fr;align-items:center;border-top:1px solid #1b2836;min-height:52px}.row:first-child{border-top:none}
    .label{text-align:center;font-size:12px;letter-spacing:1.6px;color:#8193a9;font-weight:700}.label small{display:block;font-size:8.5px;letter-spacing:1px;color:#5f738b;margin-top:3px}
    .val{font-size:25px;font-variant-numeric:tabular-nums;padding:11px 34px;color:#c7d3e2;height:100%;display:flex;align-items:center}.val.left{justify-content:flex-end}.val.right{justify-content:flex-start}
    .best{color:#fff;font-weight:800;text-shadow:0 0 14px var(--c)}.val.best.left{background:linear-gradient(270deg,color-mix(in srgb,var(--c) 34%,transparent),transparent 85%);box-shadow:inset -4px 0 0 var(--c)}.val.best.right{background:linear-gradient(90deg,color-mix(in srgb,var(--c) 34%,transparent),transparent 85%);box-shadow:inset 4px 0 0 var(--c)}.dim{color:#5a6b80}
    .record.best{border-color:var(--c);box-shadow:0 0 18px color-mix(in srgb,var(--c) 45%,transparent)}.record.dim b{color:#6b7c91}
    footer{margin-top:20px;padding-top:14px;border-top:1px solid #243448;display:flex;justify-content:space-between;gap:20px;color:#71859f;font-size:11px;line-height:1.6}
  </style></head><body><main><div class="eyebrow">NBA LIVE · PLAYER COMPARISON · ${escape(label || 'CURRENT SEASON')}</div>
  <div class="top">${head(sides[0], 0)}<div class="vs">VS</div>${head(sides[1], 1)}</div>
  <div class="stats">${rows}</div>
  <footer><span>Source: ESPN · per-game regular-season averages · team record = current team<br>Brighter value = better in that category · shooting % from season totals</span><span>FP: ESPN default points scoring<br>Ties and games played are not highlighted</span></footer></main></body></html>`;
}
module.exports = { compareHTML, leader };
