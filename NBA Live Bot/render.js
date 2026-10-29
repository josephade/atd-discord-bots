const puppeteer = require('puppeteer');
const { existsSync } = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { gamesHTML } = require('./games_layout');
const { scheduleHTML } = require('./schedule_layout');
const { salaryHTML } = require('./salary');
const { last10HTML } = require('./last10_layout');
const { compareHTML } = require('./compare_layout');
const { standingsHTML } = require('./standings');
const { picksHTML } = require('./picks');
const { boxscoreHTML } = require('./boxscores');
const { injuriesHTML } = require('./injuries');
async function browserExecutable() {
  if (process.env.PUPPETEER_EXECUTABLE_PATH) return process.env.PUPPETEER_EXECUTABLE_PATH;
  const bundled = await puppeteer.executablePath();
  if (bundled && existsSync(bundled)) return bundled;
  if (process.platform === 'win32') {
    const candidates = [
      [process.env.PROGRAMFILES, 'Google/Chrome/Application/chrome.exe'],
      [process.env['PROGRAMFILES(X86)'], 'Google/Chrome/Application/chrome.exe'],
      [process.env.LOCALAPPDATA, 'Google/Chrome/Application/chrome.exe'],
      [process.env.PROGRAMFILES, 'Microsoft/Edge/Application/msedge.exe'],
      [process.env['PROGRAMFILES(X86)'], 'Microsoft/Edge/Application/msedge.exe'],
    ];
    for (const [root, relative] of candidates) {
      if (root && existsSync(path.join(root, relative))) return path.join(root, relative);
    }
  }
  throw new Error('No Chrome browser found. Install it with npx.cmd puppeteer browsers install chrome, or set PUPPETEER_EXECUTABLE_PATH in .env.');
}
const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const imageURL = value => /^https:\/\/a\.espncdn\.com\//.test(value || '') ? escape(value) : '';
const fields = ['MIN', 'PTS', 'REB', 'AST', 'STL', 'BLK', '+/-', 'FG%', 'FG', '3PT', 'FT', 'TO', 'PF', 'OREB', 'DREB', 'TS%'];
function backHTML(model) {
  const { player, breakdown: b } = model;
  const quarterPage = model.face !== 'shots';
  const title = quarterPage ? 'QUARTER BY QUARTER' : 'SHOT BREAKDOWN';
  const color = /^[a-f0-9]{6}$/i.test(player.team?.color) ? `#${player.team.color}` : '#128de2';
  const ready = b?.available;
  const columns = ['PTS', 'REB', 'AST', 'STL', 'BLK', 'TO', 'FG', '3FG', 'FT'];
  const values = row => [row.PTS, row.REB, row.AST, row.STL, row.BLK, row.TO,
    `${row.FGM}-${row.FGA}`, `${row.TPM}-${row.TPA}`, `${row.FTM}-${row.FTA}`];
  const quarters = (b?.periods || []).map(row => `<tr class="${model.state === 'in' && row.period === b.current ? 'current' : ''}"><th>${escape(row.label)}${model.state === 'in' && row.period === b.current ? '<small>LIVE</small>' : ''}</th>${values(row).map(value => `<td>${row.started ? escape(value) : '—'}</td>`).join('')}</tr>`).join('');
  const shots = (b?.shots || []).map(shot => {
    const pct = shot.attempted ? (100 * shot.made / shot.attempted).toFixed(1) : '—';
    return `<div class="shot"><div class="shot-title"><b>${escape(shot.label)}</b>${shot.detail ? `<small>${escape(shot.detail)}</small>` : ''}</div><div class="shot-values"><strong>${ready ? `${shot.made}<em>/${shot.attempted}</em>` : '—'}</strong><span>${ready ? pct : '—'}${ready && shot.attempted ? '%' : ''}</span><span>${ready ? shot.points : '—'} pts</span></div><div class="track"><div style="width:${ready && shot.attempted ? 100 * shot.made / shot.attempted : 0}%"></div></div></div>`;
  }).join('');
  const availability = !ready ? (model.state === 'pre' ? 'Available after tip-off' : 'Play-by-play unavailable')
    : b.partial ? 'Partial feed · totals may differ' : model.state === 'in' ? 'Live play-by-play' : 'Game play-by-play';
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090b0e;color:#f3f4f6;font-family:Arial,sans-serif}
    main{width:1120px;padding:32px 48px 26px;border-top:5px solid ${color};background:radial-gradient(ellipse at 95% 0%,${color}26,transparent 50%),#090b0e}
    .eyebrow{display:flex;justify-content:space-between;font-size:14px;font-weight:700;letter-spacing:2.5px;color:#9ba4b3}.badge{color:#6bb8ff}
    header{display:flex;align-items:center;gap:18px;margin:24px 0 22px}.portrait{width:76px;height:76px;border-radius:50%;object-fit:cover;object-position:top;background:${color}55}.identity{flex:1;min-width:0}h1{font-size:31px;margin:0 0 6px;overflow-wrap:anywhere}.game{font-size:16px;color:#9ba4b3;line-height:1.5}.status{text-align:right;font-size:15px;color:#6bb8ff;max-width:310px}
    h2{font-size:17px;letter-spacing:1px;margin:0;color:#e0e6ef}.section-title{display:flex;justify-content:space-between;align-items:center;margin:24px 0 13px}.section-title span{font-size:13px;color:#9ba4b3}.warning{color:#fbbf24!important}
    table{width:100%;border-collapse:collapse;table-layout:fixed;font-variant-numeric:tabular-nums}thead th{font-size:12px;font-weight:400;color:#9ba4b3;padding:10px 3px;background:#111820}th:first-child{width:91px;text-align:left;padding-left:13px}tbody th,tbody td{padding:13px 3px;border-bottom:1px solid #232d38;text-align:center;font-size:18px}tbody th{font-size:15px}tbody small{display:inline-block;color:#34d399;font-size:9px;margin-left:7px}.current{background:#14212a}.total{color:#6bb8ff;font-weight:700;background:#0d141b}
    .shots{display:grid;grid-template-columns:1fr 1fr;gap:12px 20px}.shot{padding:13px 15px 12px;background:#10171e;border:1px solid #25303b;border-radius:10px;display:grid;grid-template-columns:1fr auto;gap:10px}.shot-title b{font-size:16px}.shot-title small{display:block;font-size:11px;color:#8291a3;margin-top:3px}.shot-values{display:grid;grid-template-columns:60px 66px 46px;gap:7px;align-items:center;text-align:right;font-variant-numeric:tabular-nums}.shot-values strong{font-size:21px}.shot-values em{font-style:normal;font-size:16px;color:#8391a3}.shot-values span{font-size:12px;color:#aab5c4}.track{grid-column:1/-1;height:3px;background:#25303b;border-radius:3px;overflow:hidden}.track div{height:100%;background:#6bb8ff}
    .unavailable{font-size:15px;line-height:1.5;color:#bbc2cd;padding:12px 0}.note{font-size:12px;color:#8f9caf;line-height:1.6;margin-top:16px}footer{margin-top:18px;padding-top:14px;border-top:1px solid #25303b;font-size:12px;color:#717e91;display:flex;justify-content:space-between;gap:15px}
    .quarter-page tbody th,.quarter-page tbody td{padding-top:23px;padding-bottom:23px;font-size:23px}.quarter-page thead th{font-size:14px}.shot-page .shot{padding:19px 17px 15px}.shot-page .shot-title b{font-size:19px}.shot-page .shot-values strong{font-size:26px}.shot-page .shot-values span{font-size:14px}.shot-page .shot-title small{font-size:13px}
  </style></head><body><main class="${quarterPage ? 'quarter-page' : 'shot-page'}"><div class="eyebrow"><span>NBA PLAYER STATS</span><span class="badge">${title}</span></div>
    <header>${imageURL(player.headshot?.href) ? `<img class="portrait" src="${imageURL(player.headshot.href)}">` : ''}<div class="identity"><h1>${escape(player.displayName)}</h1><div class="game">${escape(player.team?.displayName)} · ${escape(model.matchup)}</div></div><div class="status">${escape(model.status)}<br>${model.date ? escape(new Date(model.date).toLocaleDateString('en-GB', { timeZone: 'America/New_York', day: 'numeric', month: 'short', year: 'numeric' })) : ''}</div></header>
    ${quarterPage ? `<div class="section-title"><h2>QUARTER BY QUARTER</h2><span class="${b?.partial ? 'warning' : ''}">${escape(availability)}</span></div>
    <table><thead><tr><th>PERIOD</th>${columns.map(c => `<th>${c}</th>`).join('')}</tr></thead><tbody>${quarters}${ready ? `<tr class="total"><th>TOTAL</th>${values(b.totals).map(v => `<td>${escape(v)}</td>`).join('')}</tr>` : ''}</tbody></table>
    ${!ready ? '<div class="unavailable">Quarter and shot counts need a play-by-play feed. Unavailable data is shown as —.</div>' : ''}
    ` : `<div class="section-title"><h2>HOW THEY SHOT</h2><span>MADE / ATTEMPTED &nbsp; · &nbsp; FG% / FT% &nbsp; · &nbsp; POINTS</span></div>
    <div class="section-title"><span class="${b?.partial ? 'warning' : ''}">${escape(availability)}</span></div><div class="shots">${shots}</div>`}
    <div class="note">${quarterPage ? 'Quarter counts come from play-by-play; minutes and +/− by quarter are not supplied. — = unavailable or period not started.' : 'Each attempt appears in one category. Mid-range excludes layups, dunks, floaters, hooks and tip-ins. — = unavailable or no attempts.'}${b?.partial ? '<br>Partial feed: play-by-play does not yet match the box score. Counts may update with provider corrections.' : ''}</div>
    <footer><span>Source: ESPN play-by-play · Updated ${escape(new Date().toISOString().replace('T', ' ').slice(0, 19))} UTC</span><span>Choose a page using the buttons below</span></footer></main></body></html>`;
}
function html(model) {
  if (model.type === 'picks') return picksHTML(model);
  if (model.type === 'injuries') return injuriesHTML(model);
  if (model.type === 'boxscore') return boxscoreHTML(model);
  if (model.type === 'games') return gamesHTML(model);
  if (model.type === 'schedule') return scheduleHTML(model);
  if (model.type === 'salary') return salaryHTML(model);
  if (model.type === 'last10') return last10HTML(model);
  if (model.type === 'compare') return compareHTML(model);
  if (model.type === 'standings') return standingsHTML(model);
  if (['back', 'quarters', 'shots'].includes(model.face)) return backHTML(model);
  const { player, stats = {} } = model;
  const color = /^[a-f0-9]{6}$/i.test(player.team?.color) ? `#${player.team.color}` : '#128de2';
  const cell = key => `<div class="cell"><strong style="${String(stats[key] ?? '').length > 6 ? 'font-size:27px' : ''}" class="${key === '+/-' && Number(stats[key]) > 0 ? 'positive' : ''}">${escape(stats[key] ?? '—')}</strong><span>${key === '3PT' ? '3FG' : key}</span></div>`;
  const threes = String(stats['3PT'] || '').split(/[-/]/)[0];
  const scoreboard = model.scoreboard?.length === 2 ? `<div class="scoreboard"><div class="score-teams">${model.scoreboard.map((team, i) => `${i ? '<div class="score-divider">–</div>' : ''}<div class="score-team"><div class="score-team-name">${imageURL(team.logo) ? `<img src="${imageURL(team.logo)}">` : ''}<span>${escape(team.abbreviation)}</span></div><b>${escape(team.score)}</b></div>`).join('')}</div><div class="score-status">${model.state === 'in' ? '<i></i>' : ''}${escape(model.status)}</div></div>` : '';
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090b0e;color:#f3f4f6;font-family:Arial,sans-serif}
    main{width:1120px;padding:44px 48px 30px;background:radial-gradient(ellipse at 95% 0%,${color}26,transparent 50%),#090b0e;border-top:5px solid ${color}}
    .eyebrow{font-size:17px;letter-spacing:3px;color:#9ba4b3;font-weight:700;display:flex;justify-content:space-between}.badge{color:${model.state === 'in' ? '#34d399' : '#6bb8ff'}}
    header{display:flex;align-items:center;gap:25px;margin-top:34px}.portrait{width:148px;height:148px;object-fit:cover;object-position:top;border-radius:50%;background:${color}55}.identity{flex:1;min-width:0}
    h1{font-size:42px;line-height:1.12;margin:0 0 12px;overflow-wrap:anywhere}.team{color:#9ba4b3;font-size:21px;display:flex;gap:10px;align-items:center}.logo{width:35px;height:35px;object-fit:contain}.fp{text-align:right}.fp b{font-size:44px}.fp span{display:block;font-size:14px;letter-spacing:2px;color:#9ba4b3;margin-top:5px}
    .heroes{display:flex;gap:66px;margin:30px 0 32px}.hero b{font-size:76px;letter-spacing:-3px}.hero span{font-size:24px;color:#9ba4b3;margin-left:10px}
    .highlights{display:flex;align-items:center;justify-content:space-between;gap:28px;margin:30px 0 32px}.highlights .heroes{margin:0}.highlights.with-score .heroes{gap:26px}.with-score .hero b{font-size:64px}.with-score .hero span{font-size:20px;margin-left:7px}
    .scoreboard{flex:0 0 330px;padding:14px 20px;border:1px solid #293440;border-radius:16px;background:#101820b3}.score-teams{display:grid;grid-template-columns:1fr 28px 1fr;align-items:end;text-align:center}.score-team-name{display:flex;align-items:center;justify-content:center;gap:8px;font-size:17px;font-weight:700;color:#bac5d3}.score-team-name img{width:36px;height:36px;object-fit:contain}.score-team b{display:block;font-size:47px;line-height:1.12;margin-top:4px;letter-spacing:-1px}.score-divider{font-size:30px;color:#758296;padding-bottom:7px}.score-status{margin-top:10px;text-align:center;color:#6bb8ff;font-size:17px;font-weight:700}.score-status i{display:inline-block;width:8px;height:8px;background:#34d399;border-radius:50%;margin-right:8px}
    .game{border-top:1px solid #252a33;border-bottom:1px solid #252a33;padding:20px 0;display:flex;justify-content:space-between;gap:20px;font-size:22px}.game span{color:#6bb8ff;text-align:right}
    .game-divider{border-top:1px solid #252a33}
    .grid{display:grid;grid-template-columns:repeat(8,1fr);row-gap:28px;margin:30px 0}.cell strong{display:block;font-size:33px;letter-spacing:-1px;white-space:nowrap}.cell span{display:block;color:#9ba4b3;font-size:17px;margin-top:6px}.positive{color:#34d399}
    .note{font-size:18px;color:#bbc2cd;line-height:1.5;min-height:27px}footer{margin-top:26px;padding-top:18px;border-top:1px solid #252a33;color:#717e91;font-size:14px;line-height:1.6;display:flex;justify-content:space-between;gap:20px}
  </style></head><body><main><div class="eyebrow"><span>NBA PLAYER STATS</span><span class="badge">${model.state === 'in' ? '● LIVE' : model.state === 'post' ? 'FINAL' : model.state === 'season' ? 'SEASON AVERAGES' : 'UPCOMING'}</span></div>
  <header>${imageURL(player.headshot?.href) ? `<img class="portrait" src="${imageURL(player.headshot.href)}">` : ''}<div class="identity"><h1>${escape(player.displayName)}</h1><div class="team">${imageURL(player.team?.logo || player.team?.logos?.[0]?.href) ? `<img class="logo" src="${imageURL(player.team.logo || player.team.logos[0].href)}">` : ''}${escape(player.team?.displayName)} · ${escape(player.position?.abbreviation || '')}</div></div><div class="fp"><b>${escape(stats.FP ?? '—')}</b><span>FANTASY PTS</span></div></header>
  <div class="highlights ${scoreboard ? 'with-score' : ''}"><div class="heroes"><div class="hero"><b>${escape(stats.PTS ?? '—')}</b><span>pts</span></div><div class="hero"><b>${escape(threes && threes !== '—' ? threes : '—')}</b><span>3pm</span></div><div class="hero"><b>${escape(stats['FG%'] ?? '—')}</b><span>FG%</span></div></div>${scoreboard}</div>
  ${scoreboard ? '<div class="game-divider"></div>' : `<div class="game"><b>${escape(model.matchup)}</b><span>${escape(model.status)}</span></div>`}<div class="grid">${fields.map(cell).join('')}</div>
  <div class="note">${escape(model.note)}${model.date ? ` · ${escape(new Date(model.date).toLocaleDateString('en-GB', { timeZone: 'America/New_York', day: 'numeric', month: 'short', year: 'numeric' }))}` : ''}</div>
  <footer><span>Source: ESPN · Updated ${escape(new Date().toISOString().replace('T', ' ').slice(0, 19))} UTC<br>FP: ESPN default points scoring<br>PTS + 2 FGM − FGA + FTM − FTA + 3PM<br>+ REB + 2 AST + 4 STL + 4 BLK − 2 TO</span><span>TS% = PTS ÷ [2 × (FGA + 0.44 FTA)]<br>Stats may lag play; — = unavailable</span></footer></main></body></html>`;
}
class Renderer {
  constructor() { this.queue = Promise.resolve(); this.browser = null; this.page = null; this.cache = new Map(); this.pending = new Map(); }
  render(model) {
    const key = createHash('sha256').update(JSON.stringify(model)).digest('hex');
    const cached = this.cache.get(key);
    if (cached && cached.expires > Date.now()) return Promise.resolve(cached.png);
    if (this.pending.has(key)) return this.pending.get(key);
    const task = this.queue.then(() => this.draw(model)).then(png => {
      this.cache.set(key, { png, expires: Date.now() + 30000 });
      while (this.cache.size > 32) this.cache.delete(this.cache.keys().next().value);
      return png;
    }).finally(() => this.pending.delete(key));
    this.pending.set(key, task);
    this.queue = task.catch(() => {});
    return task;
  }
  async warm() {
    if (this.warming) return this.warming;
    this.warming = this.open().finally(() => { this.warming = null; });
    return this.warming;
  }
  async open() {
    if (!this.browser?.connected) this.browser = await puppeteer.launch({
      executablePath: await browserExecutable(), headless: true, protocolTimeout: 45000,
      args: ['--disable-dev-shm-usage', '--disable-gpu', ...(process.env.PUPPETEER_NO_SANDBOX === 'true' ? ['--no-sandbox'] : [])],
    });
    if (!this.page || this.page.isClosed()) {
      this.page = await this.browser.newPage();
      await this.page.setViewport({ width: 1120, height: 900, deviceScaleFactor: 1.5 });
    }
    return this.page;
  }
  async draw(model) {
    const page = await this.warm();
    try {
      await page.setContent(html(model), { waitUntil: 'domcontentloaded', timeout: 10000 });
      await page.evaluate(async () => {
        await Promise.race([
          Promise.all(Array.from(document.images, image => image.decode().catch(() => {}))),
          new Promise(resolve => setTimeout(resolve, 1500)),
        ]);
      });
      const card = await page.$('main');
      try { return Buffer.from(await card.screenshot({ type: 'png' })); }
      finally { await card.dispose(); }
    } catch (error) {
      this.page = null;
      await page.close().catch(() => {});
      throw error;
    }
  }
  async close() { await this.queue; await this.warming?.catch(() => {}); await this.browser?.close(); this.cache.clear(); }
}
module.exports = { Renderer, html, backHTML };
