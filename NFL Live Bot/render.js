const puppeteer = require('puppeteer');
const { existsSync } = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { gamesHTML } = require('./games_layout');
const { boxscoreHTML } = require('./boxscores');
const { injuriesHTML } = require('./injuries');
const { HERO_FIELDS, GRID_FIELDS, SECONDARY_CATEGORIES, CATEGORY_TITLES, FIELD_LABELS, detectPrimary } = require('./data');
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
function quartersHTML(model) {
  const { player, drives: d } = model;
  const color = /^[a-f0-9]{6}$/i.test(player.team?.color) ? `#${player.team.color}` : '#128de2';
  const teams = d?.teams || [];
  const playerTeamIndex = teams.findIndex(t => String(t.id) === String(player.team?.id));
  const periods = d?.periods || [];
  const current = model.period;
  const rows = periods.map(row => `<tr class="${model.state === 'in' && row.period === current ? 'current' : ''}"><th>${escape(row.label)}${model.state === 'in' && row.period === current ? '<small>LIVE</small>' : ''}</th>${row.values.map((v, i) => `<td class="${i === playerTeamIndex ? 'yours' : ''}">${escape(v)}</td>`).join('')}</tr>`).join('');
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090b0e;color:#f3f4f6;font-family:Arial,sans-serif}
    main{width:1120px;padding:32px 48px 26px;border-top:5px solid ${color};background:radial-gradient(ellipse at 95% 0%,${color}26,transparent 50%),#090b0e}
    .eyebrow{display:flex;justify-content:space-between;font-size:14px;font-weight:700;letter-spacing:2.5px;color:#9ba4b3}.badge{color:#6bb8ff}
    header{display:flex;align-items:center;gap:18px;margin:24px 0 22px}.portrait{width:76px;height:76px;border-radius:50%;object-fit:cover;object-position:top;background:${color}55}.identity{flex:1;min-width:0}h1{font-size:31px;margin:0 0 6px;overflow-wrap:anywhere}.game{font-size:16px;color:#9ba4b3;line-height:1.5}.status{text-align:right;font-size:15px;color:#6bb8ff;max-width:310px}
    h2{font-size:17px;letter-spacing:1px;margin:0;color:#e0e6ef}.section-title{display:flex;justify-content:space-between;align-items:center;margin:24px 0 13px}.section-title span{font-size:13px;color:#9ba4b3}
    table{width:100%;border-collapse:collapse;table-layout:fixed;font-variant-numeric:tabular-nums}thead th{font-size:14px;font-weight:400;color:#9ba4b3;padding:12px 3px;background:#111820}th:first-child{width:91px;text-align:left;padding-left:13px}tbody th,tbody td{padding:19px 3px;border-bottom:1px solid #232d38;text-align:center;font-size:24px}tbody th{font-size:16px}tbody small{display:inline-block;color:#34d399;font-size:9px;margin-left:7px}.current{background:#14212a}.total{color:#6bb8ff;font-weight:700;background:#0d141b}.yours{color:#fff;font-weight:700}
    .note{font-size:12px;color:#8f9caf;line-height:1.6;margin-top:16px}footer{margin-top:18px;padding-top:14px;border-top:1px solid #25303b;font-size:12px;color:#717e91;display:flex;justify-content:space-between;gap:15px}
  </style></head><body><main class="quarter-page"><div class="eyebrow"><span>NFL PLAYER STATS</span><span class="badge">QUARTER BY QUARTER</span></div>
    <header>${imageURL(player.headshot?.href) ? `<img class="portrait" src="${imageURL(player.headshot.href)}">` : ''}<div class="identity"><h1>${escape(player.displayName)}</h1><div class="game">${escape(player.team?.displayName)} · ${escape(model.matchup)}</div></div><div class="status">${escape(model.status)}<br>${model.date ? escape(new Date(model.date).toLocaleDateString('en-GB', { timeZone: 'America/New_York', day: 'numeric', month: 'short', year: 'numeric' })) : ''}</div></header>
    <div class="section-title"><h2>TEAM SCORE BY QUARTER</h2><span>${teams.map(t => escape(t.abbreviation)).join(' @ ')}</span></div>
    <table><thead><tr><th>PERIOD</th>${teams.map((t, i) => `<th class="${i === playerTeamIndex ? 'yours' : ''}">${escape(t.abbreviation)}</th>`).join('')}</tr></thead><tbody>${rows}<tr class="total"><th>FINAL</th>${(d?.totals || []).map((v, i) => `<td class="${i === playerTeamIndex ? 'yours' : ''}">${escape(v)}</td>`).join('')}</tr></tbody></table>
    <div class="note">Quarter scores come from the official scoring summary, not an inferred play-by-play count. ${player.team?.displayName || 'The player’s team'}'s column is highlighted. — = period not yet played.</div>
    <footer><span>Source: ESPN · Updated ${escape(new Date().toISOString().replace('T', ' ').slice(0, 19))} UTC</span><span>Choose a page using the buttons below</span></footer></main></body></html>`;
}
function drivesHTML(model) {
  const { player, drives: d } = model;
  const color = /^[a-f0-9]{6}$/i.test(player.team?.color) ? `#${player.team.color}` : '#128de2';
  const ready = d?.available;
  const rows = (d?.drives || []).map(drive => `<tr class="${drive.isPlayerTeam ? 'yours' : ''}"><th>Q${drive.period ?? '—'}</th><td>${escape(drive.teamAbbreviation)}</td><td>${escape(drive.start)}</td><td>${escape(drive.plays)}</td><td>${escape(drive.timeOfPossession)}</td><td>${drive.yards ?? '—'}</td><td class="${drive.isScore ? 'score' : ''}">${escape(drive.result)}</td></tr>`).join('');
  const availability = !ready ? (model.state === 'pre' ? 'Available after kickoff' : 'Drive data unavailable') : model.state === 'in' ? 'Live drive-by-drive summary' : 'Full game drive-by-drive summary';
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090b0e;color:#f3f4f6;font-family:Arial,sans-serif}
    main{width:1120px;padding:32px 48px 26px;border-top:5px solid ${color};background:radial-gradient(ellipse at 95% 0%,${color}26,transparent 50%),#090b0e}
    .eyebrow{display:flex;justify-content:space-between;font-size:14px;font-weight:700;letter-spacing:2.5px;color:#9ba4b3}.badge{color:#6bb8ff}
    header{display:flex;align-items:center;gap:18px;margin:24px 0 22px}.portrait{width:76px;height:76px;border-radius:50%;object-fit:cover;object-position:top;background:${color}55}.identity{flex:1;min-width:0}h1{font-size:31px;margin:0 0 6px;overflow-wrap:anywhere}.game{font-size:16px;color:#9ba4b3;line-height:1.5}.status{text-align:right;font-size:15px;color:#6bb8ff;max-width:310px}
    h2{font-size:17px;letter-spacing:1px;margin:0;color:#e0e6ef}.section-title{display:flex;justify-content:space-between;align-items:center;margin:24px 0 13px}.section-title span{font-size:13px;color:#9ba4b3}
    table{width:100%;border-collapse:collapse;table-layout:fixed;font-variant-numeric:tabular-nums}thead th{font-size:11px;font-weight:400;color:#9ba4b3;padding:9px 3px;background:#111820;text-align:center}thead th:first-child,thead th:nth-child(2){text-align:left}th:first-child{width:52px;text-align:left;padding-left:13px}tbody th,tbody td{padding:9px 3px;border-bottom:1px solid #232d38;text-align:center;font-size:13px}tbody th{font-size:12px}tbody td:nth-child(2),tbody td:nth-child(3){text-align:left}.yours{background:#14212a}.yours th,.yours td{font-weight:700;color:#fff}.score{color:#34d399;font-weight:700}
    .unavailable{font-size:15px;line-height:1.5;color:#bbc2cd;padding:12px 0}.note{font-size:12px;color:#8f9caf;line-height:1.6;margin-top:16px}footer{margin-top:18px;padding-top:14px;border-top:1px solid #25303b;font-size:12px;color:#717e91;display:flex;justify-content:space-between;gap:15px}
  </style></head><body><main class="drives-page"><div class="eyebrow"><span>NFL PLAYER STATS</span><span class="badge">DRIVE SUMMARY</span></div>
    <header>${imageURL(player.headshot?.href) ? `<img class="portrait" src="${imageURL(player.headshot.href)}">` : ''}<div class="identity"><h1>${escape(player.displayName)}</h1><div class="game">${escape(player.team?.displayName)} · ${escape(model.matchup)}</div></div><div class="status">${escape(model.status)}<br>${model.date ? escape(new Date(model.date).toLocaleDateString('en-GB', { timeZone: 'America/New_York', day: 'numeric', month: 'short', year: 'numeric' })) : ''}</div></header>
    <div class="section-title"><h2>DRIVE BY DRIVE</h2><span>${escape(availability)}</span></div>
    ${ready ? `<table><thead><tr><th>QTR</th><th>TEAM</th><th>START</th><th>PLAYS</th><th>TOP</th><th>YDS</th><th>RESULT</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="note">Team-level drive data from the official scoring summary. ESPN does not attribute individual NFL plays to specific players, so this page shows drives, not this player's individual plays. ${escape(player.team?.displayName || 'This player’s team')}'s drives are highlighted.</div>`
    : `<div class="unavailable">${model.state === 'pre' ? 'Drive-by-drive data becomes available once the game kicks off.' : 'Drive-by-drive data is unavailable for this game.'}</div>`}
    <footer><span>Source: ESPN · Updated ${escape(new Date().toISOString().replace('T', ' ').slice(0, 19))} UTC</span><span>Choose a page using the buttons below</span></footer></main></body></html>`;
}
function statCell(stats, key, big) {
  const label = FIELD_LABELS[key] || key;
  const value = stats[key] ?? '—';
  return `<div class="cell"><strong style="${big && String(value).length > 6 ? 'font-size:27px' : ''}">${escape(value)}</strong><span>${escape(label)}</span></div>`;
}
function frontHTML(model) {
  const { player, stats = {} } = model;
  const color = /^[a-f0-9]{6}$/i.test(player.team?.color) ? `#${player.team.color}` : '#128de2';
  const primary = model.primary || detectPrimary(stats);
  const hero = (HERO_FIELDS[primary] || []).slice(0, 3);
  const gridFields = [...(HERO_FIELDS[primary] || []).slice(3), ...(GRID_FIELDS[primary] || [])];
  const secondary = model.categories ? (SECONDARY_CATEGORIES[primary] || []).find(key => model.categories.some(c => c.key === key)) : null;
  const secondaryLine = secondary ? (() => {
    const cat = model.categories.find(c => c.key === secondary);
    const parts = cat.labels.map((label, i) => `${escape(cat.values[i] ?? '—')} ${escape(label)}`).join(' · ');
    return `<div class="secondary"><span>${escape(CATEGORY_TITLES[secondary] || secondary)}</span> ${parts}</div>`;
  })() : '';
  const scoreboard = model.scoreboard?.length === 2 ? `<div class="scoreboard"><div class="score-teams">${model.scoreboard.map((team, i) => `${i ? '<div class="score-divider">–</div>' : ''}<div class="score-team"><div class="score-team-name">${imageURL(team.logo) ? `<img src="${imageURL(team.logo)}">` : ''}<span>${escape(team.abbreviation)}</span></div><b>${escape(team.score)}</b></div>`).join('')}</div><div class="score-status">${model.state === 'in' ? '<i></i>' : ''}${escape(model.status)}</div></div>` : '';
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#090b0e;color:#f3f4f6;font-family:Arial,sans-serif}
    main{width:1120px;padding:44px 48px 30px;background:radial-gradient(ellipse at 95% 0%,${color}26,transparent 50%),#090b0e;border-top:5px solid ${color}}
    .eyebrow{font-size:17px;letter-spacing:3px;color:#9ba4b3;font-weight:700;display:flex;justify-content:space-between}.badge{color:${model.state === 'in' ? '#34d399' : '#6bb8ff'}}
    header{display:flex;align-items:center;gap:25px;margin-top:34px}.portrait{width:148px;height:148px;object-fit:cover;object-position:top;border-radius:50%;background:${color}55}.identity{flex:1;min-width:0}
    h1{font-size:42px;line-height:1.12;margin:0 0 12px;overflow-wrap:anywhere}.team{color:#9ba4b3;font-size:21px;display:flex;gap:10px;align-items:center}.logo{width:35px;height:35px;object-fit:contain}.fp{text-align:right}.fp b{font-size:44px}.fp span{display:block;font-size:14px;letter-spacing:2px;color:#9ba4b3;margin-top:5px}
    .heroes{display:flex;gap:46px;margin:30px 0 32px}.hero b{font-size:60px;letter-spacing:-3px}.hero span{font-size:22px;color:#9ba4b3;margin-left:10px}
    .highlights{display:flex;align-items:center;justify-content:space-between;gap:28px;margin:30px 0 20px}.highlights .heroes{margin:0}.highlights.with-score .heroes{gap:26px}.with-score .hero b{font-size:52px}.with-score .hero span{font-size:17px;margin-left:7px}
    .scoreboard{flex:0 0 330px;padding:14px 20px;border:1px solid #293440;border-radius:16px;background:#101820b3}.score-teams{display:grid;grid-template-columns:1fr 28px 1fr;align-items:end;text-align:center}.score-team-name{display:flex;align-items:center;justify-content:center;gap:8px;font-size:17px;font-weight:700;color:#bac5d3}.score-team-name img{width:36px;height:36px;object-fit:contain}.score-team b{display:block;font-size:47px;line-height:1.12;margin-top:4px;letter-spacing:-1px}.score-divider{font-size:30px;color:#758296;padding-bottom:7px}.score-status{margin-top:10px;text-align:center;color:#6bb8ff;font-size:17px;font-weight:700}.score-status i{display:inline-block;width:8px;height:8px;background:#34d399;border-radius:50%;margin-right:8px}
    .game{border-top:1px solid #252a33;border-bottom:1px solid #252a33;padding:20px 0;display:flex;justify-content:space-between;gap:20px;font-size:22px}.game span{color:#6bb8ff;text-align:right}
    .game-divider{border-top:1px solid #252a33}
    .secondary{font-size:15px;color:#aab5c4;margin:4px 0 14px}.secondary span{color:#6bb8ff;font-weight:700;margin-right:8px}
    .grid{display:grid;grid-template-columns:repeat(4,1fr);row-gap:28px;margin:22px 0 30px}.cell strong{display:block;font-size:30px;letter-spacing:-1px;white-space:nowrap}.cell span{display:block;color:#9ba4b3;font-size:16px;margin-top:6px}
    .note{font-size:18px;color:#bbc2cd;line-height:1.5;min-height:27px}footer{margin-top:26px;padding-top:18px;border-top:1px solid #252a33;color:#717e91;font-size:14px;line-height:1.6;display:flex;justify-content:space-between;gap:20px}
  </style></head><body><main><div class="eyebrow"><span>NFL PLAYER STATS</span><span class="badge">${model.state === 'in' ? '● LIVE' : model.state === 'post' ? 'FINAL' : model.state === 'season' ? 'SEASON AVERAGES' : 'UPCOMING'}</span></div>
  <header>${imageURL(player.headshot?.href) ? `<img class="portrait" src="${imageURL(player.headshot.href)}">` : ''}<div class="identity"><h1>${escape(player.displayName)}</h1><div class="team">${imageURL(player.team?.logo || player.team?.logos?.[0]?.href) ? `<img class="logo" src="${imageURL(player.team.logo || player.team.logos[0].href)}">` : ''}${escape(player.team?.displayName)} · ${escape(player.position?.abbreviation || '')}</div></div>${stats.FP !== '—' ? `<div class="fp"><b>${escape(stats.FP ?? '—')}</b><span>FANTASY PTS</span></div>` : ''}</header>
  <div class="highlights ${scoreboard ? 'with-score' : ''}"><div class="heroes">${hero.map(key => `<div class="hero"><b>${escape(stats[key] ?? '—')}</b><span>${escape((FIELD_LABELS[key] || key).toLowerCase())}</span></div>`).join('')}</div>${scoreboard}</div>
  ${scoreboard ? '<div class="game-divider"></div>' : `<div class="game"><b>${escape(model.matchup)}</b><span>${escape(model.status)}</span></div>`}${secondaryLine}<div class="grid" style="grid-template-columns:repeat(${Math.min(Math.max(gridFields.length, 1), 5)},1fr)">${gridFields.map(key => statCell(stats, key)).join('')}</div>
  <div class="note">${escape(model.note)}${model.date ? ` · ${escape(new Date(model.date).toLocaleDateString('en-GB', { timeZone: 'America/New_York', day: 'numeric', month: 'short', year: 'numeric' }))}` : ''}</div>
  <footer><span>Source: ESPN · Updated ${escape(new Date().toISOString().replace('T', ' ').slice(0, 19))} UTC<br>FP: ESPN standard PPR scoring<br>0.04/pass yd, 4/pass TD, −2/INT, 1/rec, 0.1/rush or rec yd<br>6/rush or rec TD, −2/fumble lost</span><span>Kicker FP: 3/FG &lt;40 yds, 4/FG 40–49, 5/FG 50+, −1/missed FG, 1/XP<br>No FP shown for defense-only players · Stats may lag play · — = unavailable</span></footer></main></body></html>`;
}
function html(model) {
  if (model.type === 'injuries') return injuriesHTML(model);
  if (model.type === 'boxscore') return boxscoreHTML(model);
  if (model.type === 'games') return gamesHTML(model);
  if (model.face === 'quarters') return quartersHTML(model);
  if (model.face === 'drives') return drivesHTML(model);
  return frontHTML(model);
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
  // A crashed or detached tab is thrown away by drawOnce, so one retry gets a fresh one.
  async draw(model) {
    try { return await this.drawOnce(model); }
    catch (error) {
      console.error('Render retry:', error.message);
      return this.drawOnce(model);
    }
  }
  async drawOnce(model) {
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
module.exports = { Renderer, html, frontHTML, quartersHTML, drivesHTML };
