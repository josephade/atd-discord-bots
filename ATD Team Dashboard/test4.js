/**
 * Test 4 — confirm tab→chart mapping and rendering behavior
 * Run: node test4.js
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

    console.log('Navigating to team dashboard with LAL 2025-26...');
    await page.goto('https://nbavisuals.com/team-dashboard?seasons[]=2025-26&team=LAL', {
      waitUntil: 'networkidle0', timeout: 60000,
    });
    console.log('Page loaded (networkidle0).');

    // Wait a bit more for Plotly rendering
    await new Promise(r => setTimeout(r, 3000));

    // Check which charts are visible and which have Plotly rendered
    const chartStatus = await page.evaluate(() => {
      const charts = document.querySelectorAll('.chart-container');
      return Array.from(charts).map(c => {
        const target = c.querySelector('.chart-render-target');
        const hasPlotly = target && target.querySelector('.js-plotly-plot, svg');
        const style = window.getComputedStyle(c);
        const parentStyle = c.parentElement ? window.getComputedStyle(c.parentElement) : null;
        return {
          id: c.id,
          isHidden: style.display === 'none' || style.visibility === 'hidden',
          parentHidden: parentStyle ? (parentStyle.display === 'none') : null,
          hasPlotly: !!hasPlotly,
          svgCount: target ? target.querySelectorAll('svg').length : 0,
          parentId: c.parentElement?.id || '',
          parentClass: c.parentElement?.className?.slice(0, 80) || '',
        };
      });
    });
    console.log('\n=== CHART STATUS (default Offense tab visible) ===');
    chartStatus.forEach(c => {
      const rendered = c.hasPlotly ? '✓ rendered' : '✗ empty';
      const visible = c.isHidden ? 'hidden' : 'visible';
      console.log(`${rendered}  ${visible}  id=${c.id}  parentId=${c.parentId}`);
    });

    // Check tab panel structure
    const tabPanels = await page.evaluate(() => {
      const panels = document.querySelectorAll('[id$="-panel"], [id$="-tab-content"], [role="tabpanel"], .tab-content, .tab-panel');
      return Array.from(panels).map(p => ({
        id: p.id,
        className: p.className.slice(0, 60),
        isHidden: window.getComputedStyle(p).display === 'none',
        chartIds: Array.from(p.querySelectorAll('.chart-container')).map(c => c.id),
      }));
    });
    console.log('\n=== TAB PANELS ===');
    console.log(JSON.stringify(tabPanels, null, 2));

    // Check the direct parent divs of each chart to understand tab grouping
    const chartParents = await page.evaluate(() => {
      const results = [];
      // Walk up 3 levels from each chart to find tab-related containers
      document.querySelectorAll('.chart-container').forEach(c => {
        let el = c.parentElement;
        const parents = [];
        for (let i = 0; i < 5 && el; i++) {
          parents.push({ tag: el.tagName, id: el.id, cls: el.className.slice(0, 60) });
          el = el.parentElement;
        }
        results.push({ chartId: c.id, parents });
      });
      return results;
    });
    console.log('\n=== CHART PARENT HIERARCHY ===');
    chartParents.forEach(r => {
      console.log(`\n${r.chartId}:`);
      r.parents.forEach((p, i) => console.log(`  ${'  '.repeat(i)}↑ ${p.tag}  id="${p.id}"  cls="${p.cls}"`));
    });

    // Click Defense tab and check visibility
    console.log('\n\n=== CLICKING DEFENSE TAB ===');
    await page.click('#defense-tab');
    await new Promise(r => setTimeout(r, 2000));

    const afterDefense = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('.chart-container')).map(c => ({
        id: c.id,
        isHidden: window.getComputedStyle(c).display === 'none',
        parentHidden: c.parentElement ? window.getComputedStyle(c.parentElement).display === 'none' : null,
        grandHidden: c.parentElement?.parentElement ? window.getComputedStyle(c.parentElement.parentElement).display === 'none' : null,
      }));
    });
    console.log('Visible charts after clicking Defense tab:');
    afterDefense.filter(c => !c.isHidden && !c.parentHidden && !c.grandHidden).forEach(c => console.log(' ', c.id));
    console.log('Hidden charts:');
    afterDefense.filter(c => c.isHidden || c.parentHidden || c.grandHidden).forEach(c => console.log(' ', c.id));

    // Take a screenshot to visually confirm
    await page.screenshot({ path: 'test-defense-tab.png', fullPage: false });
    console.log('\nScreenshot saved: test-defense-tab.png');

    // Try screenshotting a single chart element
    const firstVisibleChart = afterDefense.find(c => !c.isHidden && !c.parentHidden && !c.grandHidden);
    if (firstVisibleChart) {
      const el = await page.$(`#${firstVisibleChart.id}`);
      if (el) {
        await el.screenshot({ path: 'test-single-chart.png' });
        console.log(`Single chart screenshot saved: test-single-chart.png (${firstVisibleChart.id})`);
      }
    }

  } finally {
    await browser.close();
  }
}

explore().catch(console.error);
