const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const logo = (url, fallback) => /^https:\/\/a\.espncdn\.com\//.test(url || '') ? `<img src="${escape(url)}">` : `<div class="logo-fallback">${escape(fallback || 'NBA')}</div>`;
function when(game, zone) {
  const date = new Date(game.date);
  if (!Number.isFinite(date.getTime())) return { day: 'Date TBC', time: '' };
  return {
    day: new Intl.DateTimeFormat('en-GB', { timeZone: zone, weekday: 'short', day: 'numeric', month: 'short' }).format(date),
    time: new Intl.DateTimeFormat('en-GB', { timeZone: zone, hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }).format(date),
  };
}
function scheduleHTML({ team, season, record, recent = [], upcoming = [], timeZone = 'Europe/Dublin', updatedAt = new Date().toISOString() }) {
  const accent = /^[a-f0-9]{6}$/i.test(team.color || '') ? `#${team.color}` : '#258bea';
  const row = (game, past) => {
    const { day, time } = when(game, timeZone);
    const right = past
      ? `<div class="result ${game.result === 'W' ? 'win' : 'loss'}"><b>${escape(game.result)}</b><span>${escape(game.score)}</span></div>`
      : game.stopped ? `<div class="side"><span class="pill waiting">${escape(game.status.toUpperCase())}</span></div>`
      : game.state === 'in' ? `<div class="side"><span class="pill live"><i></i>LIVE</span><span>${escape(game.status)}</span></div>`
      : `<div class="side"><b>${escape(time)}</b><span>${escape(game.broadcast)}</span></div>`;
    return `<div class="game"><div class="day">${escape(day)}</div><div class="vs">${game.home ? 'vs' : '@'}</div>${logo(game.opponent.logo, game.opponent.abbreviation)}<div class="opp"><b>${escape(game.opponent.name)}</b><span>${game.home ? 'HOME' : 'AWAY'} · ${escape(game.opponent.abbreviation)}</span></div>${right}</div>`;
  };
  const section = (title, items, past) => items.length ? `<section><div class="section-title"><h2>${title}</h2><span>${items.length} GAME${items.length === 1 ? '' : 'S'}</span></div><div class="list">${items.map(g => row(g, past)).join('')}</div></section>` : '';
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090d13;font-family:Arial,sans-serif;color:#f3f6fa}main{width:1120px;padding:38px 44px 25px;border-top:5px solid ${accent};background:radial-gradient(ellipse at 100% 0%,#16344c88,transparent 50%),#090d13}
    header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}.title{display:flex;align-items:center;gap:20px}.title img,.title .logo-fallback{width:84px;height:84px}.eyebrow{font-size:13px;letter-spacing:3px;color:#8193a9;font-weight:700}h1{font-size:43px;margin:10px 0 7px;letter-spacing:-1px}.date{font-size:17px;color:#a5b3c5}.count{padding:14px 20px;border:1px solid #273749;background:#111b28;border-radius:13px;text-align:center}.count strong{display:block;font-size:28px;font-variant-numeric:tabular-nums}.count span{display:block;font-size:11px;letter-spacing:1.3px;color:#91a4bc;margin-top:4px}
    section{margin-top:25px}.section-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}h2{font-size:15px;letter-spacing:2px;margin:0}.section-title>span{font-size:11px;letter-spacing:1px;color:#788da7}.list{background:#111a26;border:1px solid #243448;border-radius:15px;overflow:hidden}
    .game{display:flex;align-items:center;gap:16px;padding:13px 20px;border-top:1px solid #1d2a3a;min-height:68px}.game:first-child{border-top:none}.day{width:120px;font-size:17px;font-weight:700;color:#d5e0ee}.vs{width:22px;color:#7188a3;font-size:15px;text-align:center}.game img,.logo-fallback{width:42px;height:42px;object-fit:contain;flex-shrink:0}.logo-fallback{background:#1c3045;border-radius:50%;font-size:12px;display:grid;place-items:center;color:#9fc4ec}.opp{flex:1;min-width:0}.opp b{display:block;font-size:19px}.opp span{display:block;color:#7188a3;font-size:11px;letter-spacing:1px;margin-top:4px}
    .side,.result{text-align:right;min-width:170px}.side b{display:block;font-size:18px;color:#bbcbe0}.side span{display:block;font-size:12px;color:#7d92ab;margin-top:5px}.result{display:flex;justify-content:flex-end;align-items:baseline;gap:12px}.result b{font-size:22px}.result span{font-size:22px;font-variant-numeric:tabular-nums}.win b{color:#42deb2}.loss b{color:#ff7a7a}
    .pill{display:inline-block;font-size:10px;font-weight:700;letter-spacing:1px;padding:6px 8px;border-radius:5px}.pill.live{color:#42deb2;background:#154336}.pill.waiting{color:#efc778;background:#40321f}.pill i{display:inline-block;width:6px;height:6px;background:#42deb2;border-radius:50%;margin-right:6px}
    .empty{padding:26px;border:1px solid #26364a;border-radius:14px;background:#111a26;font-size:18px;color:#b7c7dc}footer{margin-top:25px;padding-top:16px;border-top:1px solid #243448;display:flex;justify-content:space-between;gap:20px;color:#71859f;font-size:11px;line-height:1.6}
  </style></head><body><main><header><div class="title">${logo(team.logo, team.abbreviation)}<div><div class="eyebrow">NBA LIVE · TEAM SCHEDULE</div><h1>${escape(team.name)}</h1><div class="date">${escape(season || 'Current season')}</div></div></div><div class="count"><strong>${escape(record)}</strong><span>RECORD</span></div></header>
  ${section('UPCOMING', upcoming, false)}${!upcoming.length ? '<section><div class="empty">No upcoming games on this schedule.</div></section>' : ''}${section('LAST RESULTS', recent, true)}
  <footer><span>Source: ESPN · Regular-season schedule<br>Run !nba schedule again to refresh</span><span>Tip-off times: ${escape(timeZone)}<br>Updated ${escape(new Intl.DateTimeFormat('en-GB', { timeZone, hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short' }).format(new Date(updatedAt)))}</span></footer></main></body></html>`;
}
module.exports = { scheduleHTML };
