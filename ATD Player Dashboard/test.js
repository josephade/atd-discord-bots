const puppeteer = require('puppeteer');
const fs = require('fs');

function normalize(str) {
  return str.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9 ]/g, '').trim();
}
function findPlayer(query, playerMap) {
  const q = normalize(query);
  for (const [id, name] of Object.entries(playerMap)) {
    if (normalize(name) === q) return { id, name };
  }
  let bestId = null, bestName = null, bestScore = 0;
  for (const [id, name] of Object.entries(playerMap)) {
    const n = normalize(name);
    if (n.includes(q) || q.includes(n)) {
      const score = Math.min(n.length, q.length) / Math.max(n.length, q.length);
      if (score > bestScore) { bestScore = score; bestId = id; bestName = name; }
    }
  }
  return bestScore > 0.5 ? { id: bestId, name: bestName } : null;
}

const W = 1200, H = 750;

(async () => {
  const browser = await puppeteer.launch({ args: ['--no-sandbox', '--disable-setuid-sandbox'], headless: 'new' });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: W, height: H });

    console.log('Navigating...');
    await page.goto('https://nbavisuals.com/player-dashboard', { waitUntil: 'networkidle0', timeout: 30000 });

    const playerMap = await page.evaluate(async (season, seasonType) => {
      const res = await fetch(`/get_players/${encodeURIComponent(season)}/${encodeURIComponent(seasonType)}`);
      return res.json();
    }, '2024-25', 'regular');
    console.log('Players:', Object.keys(playerMap).length);

    const player = findPlayer('Aaron Gordon', playerMap);
    console.log('Player:', player);

    const chartData = await page.evaluate(async (playerId, season, graphType, seasonType) => {
      const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
      const body = new URLSearchParams();
      body.append('seasons[]', season);
      body.append('player', playerId);
      body.append('graph_type', graphType);
      body.append('season_type', seasonType);
      const headers = { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest' };
      if (csrfToken) headers['X-CSRFToken'] = csrfToken;
      const res = await fetch('/player-dashboard', { method: 'POST', headers, body: body.toString() });
      const text = await res.text();
      return { status: res.status, text };
    }, player.id, '2024-25', 'Playtype', 'regular');

    console.log('API status:', chartData.status);
    if (chartData.status !== 200) { console.error('Failed:', chartData.text.slice(0, 200)); return; }

    const parsed = JSON.parse(chartData.text);
    console.log('Traces:', parsed.data?.length, '| Layout title:', parsed.layout?.title?.text || parsed.layout?.title);

    const html = `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script src="https://cdn.plot.ly/plotly-2.8.3.min.js"></script>
<style>*{margin:0;padding:0}body{background:#000;overflow:hidden}#chart{width:${W}px;height:${H}px}</style>
</head><body><div id="chart"></div><script>
Plotly.newPlot('chart',${JSON.stringify(parsed.data)},${JSON.stringify({ ...parsed.layout, width: W, height: H, autosize: false })},{responsive:false,displayModeBar:false})
  .then(()=>document.body.setAttribute('data-ready','1'));
</script></body></html>`;

    await page.setContent(html, { waitUntil: 'networkidle0', timeout: 30000 });
    await page.waitForSelector('body[data-ready="1"]', { timeout: 15000 });
    const buf = await page.screenshot({ type: 'png' });
    fs.writeFileSync('test-output.png', buf);
    console.log('Screenshot saved: test-output.png (' + buf.length + ' bytes)');
  } finally {
    await browser.close();
  }
})().catch(e => console.error('ERROR:', e.message));
