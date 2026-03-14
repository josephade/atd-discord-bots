/**
 * Test 3 — intercept real network requests on form submission
 * + test GET approach with URL params
 * Run: node test3.js
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

    // Intercept ALL requests
    const captured = [];
    await page.setRequestInterception(true);
    page.on('request', req => {
      const url = req.url();
      const method = req.method();
      if (!url.includes('cdn.plot') && !url.includes('static') && !url.includes('.css') && !url.includes('.js')) {
        captured.push({ method, url, postData: req.postData() });
      }
      req.continue();
    });
    page.on('response', async res => {
      const url = res.url();
      if (url.includes('team-dashboard') && !url.includes('static')) {
        const ct = res.headers()['content-type'] || '';
        const body = ct.includes('json') ? await res.text().catch(() => '(error)') : `[${ct}]`;
        console.log(`\n=== RESPONSE: ${res.status()} ${url} ===`);
        console.log('Content-Type:', ct);
        console.log('Body preview:', body.slice(0, 400));
      }
    });

    console.log('Loading team dashboard...');
    await page.goto('https://nbavisuals.com/team-dashboard', { waitUntil: 'networkidle0', timeout: 60000 });

    // Set form values and click generate
    await page.evaluate(() => {
      const seasonDrop = document.querySelector('#season-dropdown');
      const teamDrop = document.querySelector('#team-dropdown');
      if (seasonDrop) Array.from(seasonDrop.options).forEach(o => o.selected = o.value === '2025-26');
      if (teamDrop) Array.from(teamDrop.options).forEach(o => o.selected = o.value === 'LAL');
      // Trigger change events
      seasonDrop?.dispatchEvent(new Event('change', { bubbles: true }));
      teamDrop?.dispatchEvent(new Event('change', { bubbles: true }));
    });

    // Find and click the generate button
    const btnText = await page.evaluate(() => {
      const btns = Array.from(document.querySelectorAll('button'));
      return btns.map(b => ({ id: b.id, text: b.textContent.trim(), type: b.type }));
    });
    console.log('\n=== BUTTONS ===');
    console.log(JSON.stringify(btnText, null, 2));

    // Click generate button
    await page.evaluate(() => {
      const btns = Array.from(document.querySelectorAll('button'));
      const gen = btns.find(b =>
        b.textContent.toLowerCase().includes('generat') ||
        b.textContent.toLowerCase().includes('submit') ||
        b.id === 'submit-btn' ||
        b.type === 'submit'
      );
      if (gen) { console.log('Clicking:', gen.textContent); gen.click(); }
      else {
        // Try form submit
        const form = document.querySelector('#dashboard-form');
        if (form) form.submit();
      }
    });

    await new Promise(r => setTimeout(r, 5000));

    console.log('\n=== CAPTURED REQUESTS (after form submit) ===');
    captured.forEach(r => {
      console.log(`${r.method} ${r.url}`);
      if (r.postData) console.log('  Body:', r.postData.slice(0, 200));
    });

    // Also try direct GET URL approach
    console.log('\n\n=== TRYING GET with URL PARAMS ===');
    const captured2 = [];
    page.on('request', req => {
      const url = req.url();
      if (!url.includes('cdn.plot') && !url.includes('static')) {
        captured2.push({ method: req.method(), url });
      }
    });

    await page.goto('https://nbavisuals.com/team-dashboard?seasons[]=2025-26&team=LAL', {
      waitUntil: 'networkidle0', timeout: 60000
    });
    await new Promise(r => setTimeout(r, 3000));

    console.log('GET URL params requests:');
    captured2.filter(r => !r.url.includes('cdn.plot') && !r.url.includes('.js') && !r.url.includes('.css'))
      .forEach(r => console.log(`  ${r.method} ${r.url}`));

    // Check what's on the page after GET
    const pageContent = await page.evaluate(() => {
      // Look for any chart divs, plotly elements, or chart data
      const charts = document.querySelectorAll('[id*="chart"], [id*="plot"], .plotly, [class*="chart"]');
      const chartInfo = Array.from(charts).map(c => ({ id: c.id, className: c.className.slice(0, 50), hasChildren: c.children.length }));

      // Look for any script tags with Plotly data
      const scripts = Array.from(document.querySelectorAll('script:not([src])'));
      const plotlyScripts = scripts.filter(s => s.textContent.includes('Plotly') || s.textContent.includes('newPlot'));

      return {
        chartElements: chartInfo,
        hasPlotlyScripts: plotlyScripts.length,
        plotlyScriptPreviews: plotlyScripts.map(s => s.textContent.slice(0, 200)),
        bodyText: document.body.textContent.slice(0, 300),
      };
    });
    console.log('\n=== PAGE CONTENT AFTER GET ===');
    console.log(JSON.stringify(pageContent, null, 2));

    // Try the actual AJAX fetch that the page's JS might do
    const ajaxResult = await page.evaluate(async () => {
      const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content')
        || document.querySelector('input[name="csrf_token"]')?.value;
      const headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json',
      };
      if (csrf) headers['X-CSRFToken'] = csrf;

      // Try with generate_charts or similar endpoints
      const endpoints = ['/generate-team-dashboard', '/team-charts', '/team_dashboard', '/api/team-dashboard'];
      const results = [];
      for (const endpoint of endpoints) {
        const body = new URLSearchParams();
        body.append('seasons[]', '2025-26');
        body.append('team', 'LAL');
        body.append('season_type', 'regular');
        try {
          const res = await fetch(endpoint, { method: 'POST', headers, body: body.toString() });
          results.push({ endpoint, status: res.status, preview: (await res.text()).slice(0, 100) });
        } catch(e) {
          results.push({ endpoint, error: e.message });
        }
      }
      return results;
    });
    console.log('\n=== ALT ENDPOINT ATTEMPTS ===');
    console.log(JSON.stringify(ajaxResult, null, 2));

  } finally {
    await browser.close();
  }
}

explore().catch(console.error);
