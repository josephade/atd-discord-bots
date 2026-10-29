const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function gameStatus(game, now, zone) {
  if (/cancel|postpon|suspend/i.test(game.status || '')) return { badge: game.status.toUpperCase(), tone: 'waiting', text: 'Not in progress' };
  if (game.state === 'post') return { badge: 'FINAL', tone: 'final', text: game.status || 'Final' };
  if (game.state === 'in') {
    const clock = game.status?.match(/^(.+?)\s*-\s*(\d+)(?:st|nd|rd|th)$/i);
    return { badge: 'LIVE', tone: 'live', text: clock ? `Q${clock[2]} · ${clock[1]}` : game.status || 'In progress' };
  }
  const date = new Date(game.date);
  if (!Number.isFinite(date.getTime())) return { badge: 'UPCOMING', tone: 'next', text: 'Time TBC' };
  const time = new Intl.DateTimeFormat('en-GB', { timeZone: zone, hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }).format(date);
  if (/delay/i.test(game.status)) return { badge: 'DELAYED', tone: 'waiting', text: time };
  if (date.getTime() <= new Date(now).getTime()) return { badge: 'AWAITING TIP-OFF', tone: 'waiting', text: time };
  return { badge: 'UPCOMING', tone: 'next', text: time };
}
function gamesHTML({ date, games, allGames = false, timeZone = 'Europe/Dublin', updatedAt = new Date().toISOString() }) {
  const stopped = game => /cancel|postpon|suspend/i.test(game.status || '');
  const live = games.filter(game => game.state === 'in' && !stopped(game));
  const upcoming = games.filter(game => game.state === 'pre' && !stopped(game));
  const finished = games.filter(game => !live.includes(game) && !upcoming.includes(game));
  const night = /^\d{4}-\d{2}-\d{2}$/.test(date || '') ? new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', day: 'numeric', month: 'long', year: 'numeric' }).format(new Date(date + 'T12:00:00Z')) : 'Current NBA slate';
  const row = (team, active) => `<div class="team">${/^https:\/\/a\.espncdn\.com\//.test(team.logo || '') ? `<img src="${escape(team.logo)}">` : `<div class="logo-fallback">${escape(team.abbreviation || 'NBA')}</div>`}<div class="name"><b>${escape(team.name)}</b><span>${escape(team.abbreviation)}</span></div>${active ? `<strong>${escape(team.score)}</strong>` : ''}</div>`;
  const tile = game => {
    const status = gameStatus(game, updatedAt, timeZone);
    const scored = ['in', 'post'].includes(game.state) && !stopped(game);
    return `<article class="${status.tone}"><div class="tile-top"><span class="pill">${status.tone === 'live' ? '<i></i>' : ''}${status.badge}</span><b>${escape(status.text)}</b></div>${row(game.away, scored)}${row(game.home, scored)}<span class="home-label">${escape(game.away.abbreviation)} away · ${escape(game.home.abbreviation)} home</span></article>`;
  };
  const section = (title, items) => items.length ? `<section><div class="section-title"><h2>${title}</h2><span>${items.length} GAME${items.length === 1 ? '' : 'S'}</span></div><div class="games">${items.map(tile).join('')}</div></section>` : '';
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090d13;font-family:Arial,sans-serif;color:#f3f6fa}main{width:1120px;padding:38px 44px 25px;border-top:5px solid #258bea;background:radial-gradient(ellipse at 100% 0%,#16344c88,transparent 50%),#090d13}
    header{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}.eyebrow{font-size:13px;letter-spacing:3px;color:#8193a9;font-weight:700}h1{font-size:43px;margin:10px 0 7px;letter-spacing:-1px}.date{font-size:17px;color:#a5b3c5}.counts{display:flex;gap:10px}.count{padding:14px 17px;border:1px solid #273749;background:#111b28;border-radius:13px;text-align:center}.count strong{display:block;font-size:28px}.count span{display:block;font-size:11px;letter-spacing:1.3px;color:#91a4bc;margin-top:4px}.count:first-child strong{color:#35d6aa}
    section{margin-top:25px}.section-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}h2{font-size:15px;letter-spacing:2px;margin:0}.section-title>span{font-size:11px;letter-spacing:1px;color:#788da7}.games{display:grid;grid-template-columns:1fr 1fr;gap:13px}article{background:#111a26;border:1px solid #243448;border-radius:15px;padding:15px 18px 12px;overflow:hidden}article.live{border-color:#225446;background:linear-gradient(110deg,#11272399,#111a26 65%)}.tile-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}.tile-top>b{font-size:16px;letter-spacing:.2px;color:#bbcbe0}.pill{font-size:10px;font-weight:700;letter-spacing:1px;color:#73b8ff;background:#173553;padding:6px 8px;border-radius:5px}.live .pill{color:#42deb2;background:#154336}.waiting .pill{color:#efc778;background:#40321f}.pill i{display:inline-block;width:6px;height:6px;background:#42deb2;border-radius:50%;margin-right:6px}.team{display:flex;align-items:center;gap:12px;margin:9px 0;min-height:45px}.team img,.logo-fallback{width:44px;height:44px;object-fit:contain;flex-shrink:0}.logo-fallback{background:#1c3045;border-radius:50%;font-size:12px;display:grid;place-items:center;color:#9fc4ec}.name{flex:1;min-width:0}.name b{display:block;font-size:19px;line-height:1.2;overflow-wrap:anywhere}.name span{display:block;color:#7188a3;font-size:11px;letter-spacing:1px;margin-top:4px}.team strong{font-size:33px;font-variant-numeric:tabular-nums;letter-spacing:-1px;margin-left:10px}.home-label{display:block;font-size:10px;color:#60758f;margin-top:10px;letter-spacing:.4px}.empty{margin:30px 0;padding:32px;border:1px solid #26364a;border-radius:14px;background:#111a26;font-size:20px;color:#b7c7dc;line-height:1.5}footer{margin-top:25px;padding-top:16px;border-top:1px solid #243448;display:flex;justify-content:space-between;gap:20px;color:#71859f;font-size:11px;line-height:1.6}
  </style></head><body><main><header><div><div class="eyebrow">NBA LIVE · TONIGHT’S SCOREBOARD</div><h1>${allGames ? 'NBA Box Scores' : 'Tonight’s games'}</h1><div class="date">NBA night · ${escape(night)}</div></div><div class="counts"><div class="count"><strong>${live.length}</strong><span>LIVE NOW</span></div><div class="count"><strong>${upcoming.length}</strong><span>UPCOMING</span></div>${finished.length ? `<div class="count"><strong>${finished.length}</strong><span>FINISHED / OTHER</span></div>` : ''}</div></header>
  ${section('LIVE NOW', live)}${section('COMING UP', upcoming)}${section('FINAL & OTHER GAMES', finished)}${!games.length ? `<div class="empty">${allGames ? 'No NBA games are scheduled on this slate.' : 'No live or upcoming games remain on this NBA slate.<br>Games may have finished, or no games are scheduled.'}</div>` : ''}
  <footer><span>Source: ESPN · Scores may lag play<br>${allGames ? 'Use Refresh for scores and clock · Previous / Next Day for other dates' : 'Run !nba games again to refresh'}</span><span>Tip-off times: ${escape(timeZone)}<br>Updated ${escape(new Intl.DateTimeFormat('en-GB', { timeZone, hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short' }).format(new Date(updatedAt)))}</span></footer></main></body></html>`;
}
module.exports = { gamesHTML, gameStatus };
