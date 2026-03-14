/**
 * Test 2 — confirm correct graph_type values and POST response structure
 * Run: node test2.js
 */

const puppeteer = require('puppeteer');

async function explore() {
  const browser = await puppeteer.launch({
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    headless: 'new',
  });

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1600, height: 900 });

    console.log('Navigating...');
    await page.goto('https://nbavisuals.com/team-dashboard', { waitUntil: 'networkidle0', timeout: 60000 });
    console.log('Loaded.');

    // 1) Get full team list from the dropdown
    const teamList = await page.evaluate(() => {
      const sel = document.querySelector('#team-dropdown');
      if (!sel) return 'Not found';
      return Array.from(sel.options).map(o => ({ value: o.value, text: o.text }));
    });
    console.log('\n=== FULL TEAM LIST ===');
    console.log(JSON.stringify(teamList, null, 2));

    // 2) Intercept network requests to see what the form actually POSTs
    const requests = [];
    page.on('request', req => {
      if (req.url().includes('team-dashboard') && req.method() === 'POST') {
        requests.push({ url: req.url(), postData: req.postData(), headers: req.headers() });
      }
    });

    // Try clicking the form submit to capture the real request
    await page.evaluate(async () => {
      // Set season and team
      const seasonDrop = document.querySelector('#season-dropdown');
      const teamDrop = document.querySelector('#team-dropdown');
      if (seasonDrop) {
        Array.from(seasonDrop.options).forEach(o => o.selected = o.value === '2025-26');
      }
      if (teamDrop) {
        Array.from(teamDrop.options).forEach(o => o.selected = o.value === 'LAL');
      }
    });

    // 3) Directly try POST with LAL and different graph_type candidates
    const postTests = await page.evaluate(async () => {
      const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content')
        || document.querySelector('input[name="csrf_token"]')?.value;
      console.log('CSRF:', csrf);

      const headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
      };
      if (csrf) headers['X-CSRFToken'] = csrf;

      const results = [];
      const graphTypes = [
        'offense', 'defense', 'shooting', 'lineups',
        'Offense', 'Defense', 'Shooting', 'Lineups',
        'team_offense', 'team_defense', 'team_shooting',
        'off', 'def', 'shoot',
      ];

      for (const graphType of graphTypes) {
        const body = new URLSearchParams();
        body.append('seasons[]', '2025-26');
        body.append('team', 'LAL');
        body.append('graph_type', graphType);
        body.append('season_type', 'regular');
        try {
          const res = await fetch('/team-dashboard', { method: 'POST', headers, body: body.toString() });
          const text = await res.text();
          const isJson = text.trim().startsWith('{') || text.trim().startsWith('[');
          const preview = text.slice(0, 200);
          // If JSON and no error, show top-level keys
          let keys = null;
          if (isJson) {
            try {
              const obj = JSON.parse(text);
              keys = Array.isArray(obj) ? `Array[${obj.length}]` : Object.keys(obj).join(', ');
            } catch(e) {}
          }
          results.push({ graphType, status: res.status, keys, preview });
        } catch (e) {
          results.push({ graphType, error: e.message });
        }
      }
      return results;
    });

    console.log('\n=== POST /team-dashboard with LAL ===');
    postTests.forEach(r => {
      const status = r.status === 200 ? '✓ 200' : `✗ ${r.status}`;
      console.log(`${status}  graph_type="${r.graphType}"  keys=${r.keys || '-'}  preview=${r.preview?.slice(0,100)}`);
    });

    // 4) For any successful responses, show full structure
    const successTypes = postTests.filter(r => r.status === 200 && r.keys && !r.keys.includes('error'));
    if (successTypes.length > 0) {
      console.log('\n=== SUCCESSFUL GRAPH TYPE FULL RESPONSE KEYS ===');
      for (const s of successTypes) {
        const fullData = await page.evaluate(async (graphType) => {
          const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content')
            || document.querySelector('input[name="csrf_token"]')?.value;
          const headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Requested-With': 'XMLHttpRequest',
          };
          if (csrf) headers['X-CSRFToken'] = csrf;
          const body = new URLSearchParams();
          body.append('seasons[]', '2025-26');
          body.append('team', 'LAL');
          body.append('graph_type', graphType);
          body.append('season_type', 'regular');
          const res = await fetch('/team-dashboard', { method: 'POST', headers, body: body.toString() });
          const obj = await res.json();
          // Return structure info
          if (Array.isArray(obj)) return { type: 'array', length: obj.length, item0Keys: obj[0] ? Object.keys(obj[0]) : [] };
          const topKeys = Object.keys(obj);
          const nested = {};
          topKeys.forEach(k => {
            if (obj[k] && typeof obj[k] === 'object') {
              nested[k] = Array.isArray(obj[k]) ? `Array[${obj[k].length}]` : Object.keys(obj[k]).join(', ');
            } else {
              nested[k] = typeof obj[k];
            }
          });
          return { type: 'object', topKeys, nested };
        }, s.graphType);
        console.log(`\ngraph_type="${s.graphType}":`);
        console.log(JSON.stringify(fullData, null, 2));
      }
    }

  } finally {
    await browser.close();
  }
}

explore().catch(console.error);
