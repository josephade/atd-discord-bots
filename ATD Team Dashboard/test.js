/**
 * Exploration script — discovers nbavisuals.com/team-dashboard API structure
 * Run: node test.js
 * This will log: nav graph_type values, team list endpoint, and sample POST response
 */

const puppeteer = require('puppeteer');
const fs = require('fs');

async function explore() {
  const browser = await puppeteer.launch({
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    headless: 'new',
  });

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1600, height: 900 });

    console.log('Navigating to team dashboard...');
    await page.goto('https://nbavisuals.com/team-dashboard', { waitUntil: 'networkidle0', timeout: 60000 });
    console.log('Page loaded.');

    // 1) Inspect nav/tab elements for data-graph attributes
    const navInfo = await page.evaluate(() => {
      const results = [];
      // Try various selectors that might have graph type info
      const selectors = ['[data-graph]', '[data-tab]', '[data-chart]', '.nav-link', '.tab-link', 'button[data-*]', 'a[data-*]'];
      document.querySelectorAll('[data-graph], [data-tab], [data-chart]').forEach(el => {
        results.push({
          tag: el.tagName,
          text: el.textContent.trim().slice(0, 80),
          dataGraph: el.getAttribute('data-graph'),
          dataTab: el.getAttribute('data-tab'),
          dataChart: el.getAttribute('data-chart'),
          id: el.id,
          className: el.className.slice(0, 60),
        });
      });
      return results;
    });
    console.log('\n=== NAV / DATA-GRAPH elements ===');
    console.log(JSON.stringify(navInfo, null, 2));

    // 2) Find the season select options
    const seasonOptions = await page.evaluate(() => {
      const sel = document.querySelector('#season-select, select[name="season"], select');
      if (!sel) return 'No select found';
      return Array.from(sel.options).map(o => ({ value: o.value, text: o.text }));
    });
    console.log('\n=== SEASON OPTIONS ===');
    console.log(JSON.stringify(seasonOptions, null, 2));

    // 3) Find team select options
    const teamOptions = await page.evaluate(() => {
      const selects = document.querySelectorAll('select');
      const out = {};
      selects.forEach((sel, i) => {
        out[`select_${i}_id_${sel.id}`] = Array.from(sel.options).slice(0, 5).map(o => ({ value: o.value, text: o.text }));
      });
      return out;
    });
    console.log('\n=== TEAM SELECT (first 5 options each) ===');
    console.log(JSON.stringify(teamOptions, null, 2));

    // 4) Find form action and inputs
    const formInfo = await page.evaluate(() => {
      const forms = document.querySelectorAll('form');
      return Array.from(forms).map(f => ({
        id: f.id,
        action: f.action,
        method: f.method,
        inputs: Array.from(f.querySelectorAll('input, select, textarea')).map(i => ({
          tag: i.tagName, type: i.type, name: i.name, id: i.id, value: i.value
        }))
      }));
    });
    console.log('\n=== FORMS ===');
    console.log(JSON.stringify(formInfo, null, 2));

    // 5) Try to get team list — similar to /get_players/{season}/{seasonType}
    const teamListResult = await page.evaluate(async () => {
      const attempts = [];
      const endpoints = [
        '/get_teams/2025-26/regular',
        '/get_teams/2025-26',
        '/get_teams',
        '/teams/2025-26',
      ];
      for (const url of endpoints) {
        try {
          const res = await fetch(url);
          const text = await res.text();
          attempts.push({ url, status: res.status, preview: text.slice(0, 300) });
        } catch (e) {
          attempts.push({ url, error: e.message });
        }
      }
      return attempts;
    });
    console.log('\n=== TEAM LIST ENDPOINT ATTEMPTS ===');
    console.log(JSON.stringify(teamListResult, null, 2));

    // 6) Try sample POST to /team-dashboard with various graph_type guesses
    const postResult = await page.evaluate(async () => {
      const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
      const headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
      };
      if (csrf) headers['X-CSRFToken'] = csrf;

      // First figure out what team IDs look like by hitting the team list
      // Try 'LAL' or numeric ID
      const attempts = [];
      const graphTypes = ['Offense', 'Defense', 'Shooting', 'TeamOffense', 'TeamDefense', 'TeamShooting', 'Playtype'];
      const teams = ['1610612747', 'LAL', '14']; // Lakers IDs to try

      // Just try one combination first
      for (const graphType of graphTypes.slice(0, 3)) {
        const body = new URLSearchParams();
        body.append('seasons[]', '2025-26');
        body.append('team', '1610612747'); // Lakers NBA ID
        body.append('graph_type', graphType);
        body.append('season_type', 'regular');
        try {
          const res = await fetch('/team-dashboard', { method: 'POST', headers, body: body.toString() });
          const text = await res.text();
          attempts.push({ graphType, team: '1610612747', status: res.status, preview: text.slice(0, 300) });
        } catch (e) {
          attempts.push({ graphType, error: e.message });
        }
      }
      return attempts;
    });
    console.log('\n=== POST /team-dashboard ATTEMPTS ===');
    console.log(JSON.stringify(postResult, null, 2));

    // 7) Check page HTML for any inline JS that reveals API patterns
    const jsPatterns = await page.evaluate(() => {
      const scripts = Array.from(document.querySelectorAll('script:not([src])'));
      return scripts.map(s => s.textContent.slice(0, 500)).filter(t => t.includes('team') || t.includes('graph') || t.includes('fetch') || t.includes('dashboard'));
    });
    console.log('\n=== INLINE SCRIPTS (relevant) ===');
    jsPatterns.forEach((s, i) => console.log(`Script ${i}:`, s.slice(0, 600)));

  } finally {
    await browser.close();
  }
}

explore().catch(console.error);
