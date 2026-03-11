require('dotenv').config();
const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const Discord = require('discord.js');

// ─── Cache ────────────────────────────────────────────────────────────────────

const CACHE_DIR = path.join(__dirname, 'cache');

// year N → season (N-1)-N, e.g. 1998 → "1997-98"
function yearToSeason(y) {
  const yr = parseInt(y);
  return `${yr - 1}-${yr.toString().slice(-2)}`;
}

function getCacheKey(playerName, yearStart, yearEnd, modern, playoff) {
  const clean = playerName
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_|_$/g, '');
  const yKey = yearEnd && yearEnd !== yearStart ? `${yearStart}-${yearEnd}` : yearStart;
  return `${clean}${playoff ? '_ps' : ''}${modern ? '_modern' : ''}_${yKey}.png`;
}

function fromCache(key) {
  const p = path.join(CACHE_DIR, key);
  return fs.existsSync(p) ? fs.readFileSync(p) : null;
}

function toCache(key, buf) {
  if (!fs.existsSync(CACHE_DIR)) fs.mkdirSync(CACHE_DIR, { recursive: true });
  fs.writeFileSync(path.join(CACHE_DIR, key), buf);
}

// ─── Browser ──────────────────────────────────────────────────────────────────

let browser = null;

async function getBrowser() {
  if (!browser) {
    browser = await puppeteer.launch({
      headless: 'new',
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
      defaultViewport: { width: 1600, height: 1000 },
    });
  }
  return browser;
}

// ─── Shotmap ──────────────────────────────────────────────────────────────────

function normalize(s) {
  return (s || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9 ]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

async function generateShotmap(playerName, yearStart, yearEnd, modern = false, playoff = false) {
  const cacheKey = getCacheKey(playerName, yearStart, yearEnd, modern, playoff);
  const cached = fromCache(cacheKey);
  if (cached) {
    console.log(`Cache hit: ${cacheKey}`);
    return cached;
  }

  // Build list of seasons to select
  const start = parseInt(yearStart);
  const end = yearEnd ? parseInt(yearEnd) : start;
  const seasons = [];
  for (let y = start; y <= end; y++) seasons.push(yearToSeason(y));
  const seasonLabel = seasons.length === 1 ? seasons[0] : `${seasons[0]} - ${seasons[seasons.length - 1]}`;
  console.log(`Generating: ${playerName} | ${seasonLabel}${modern ? ' | Modern' : ''}`);

  const b = await getBrowser();
  const page = await b.newPage();

  try {
    await page.setUserAgent(
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    );
    page.setDefaultTimeout(90000);

    // ── 1. Load page ──
    console.log('Loading page...');
    await page.goto('https://nbavisuals.com/shotmap', {
      waitUntil: 'networkidle2',
      timeout: 30000,
    });

    // ── 1.5. Switch to Playoffs if requested ──
    if (playoff) {
      console.log('Switching to Playoffs...');
      const toggled = await page.evaluate(() => {
        // Find the toggle by its associated label text "Regular season"
        const labels = document.querySelectorAll('label');
        for (const label of labels) {
          if (label.textContent.trim().toLowerCase().includes('regular season')) {
            const input = label.querySelector('input[type="checkbox"]') ||
                          label.previousElementSibling?.querySelector('input') ||
                          document.getElementById(label.htmlFor);
            if (input) { input.click(); return true; }
            label.click();
            return true;
          }
        }
        // Fallback: find any checkbox near text "Regular season"
        const inputs = document.querySelectorAll('input[type="checkbox"], input[type="radio"]');
        for (const inp of inputs) {
          const nearby = inp.closest('div, span, label');
          if (nearby && nearby.textContent.toLowerCase().includes('regular season')) {
            inp.click();
            return true;
          }
        }
        return false;
      });
      if (!toggled) console.warn('Could not find Regular season toggle - proceeding as regular season');
      await page.waitForTimeout(500);
    }

    // ── 2. Select season(s) ──
    console.log(`Selecting seasons: ${seasons.join(', ')}`);
    await page.waitForSelector('#season-dropdown');
    await page.select('#season-dropdown', ...seasons);
    // Wait for season data to load into the player dropdown
    await page.waitForTimeout(2000);

    // ── 3. Resolve player name + ID from autocomplete source (no side effects) ──
    console.log(`Searching for player: "${playerName}"`);

    const playerResolution = await page.evaluate((rawName, targetNorm) => {
      function norm(s) {
        return (s || '')
          .normalize('NFD')
          .replace(/[\u0300-\u036f]/g, '')
          .toLowerCase()
          .replace(/[^a-z0-9 ]/g, ' ')
          .replace(/\s+/g, ' ')
          .trim();
      }
      function findInList(players) {
        if (!players || !players.length) return null;
        for (const p of players) {
          const label = p.label || p.value || p;
          if (norm(label) === targetNorm) return { name: label, value: p.value || label };
        }
        for (const p of players) {
          const label = p.label || p.value || p;
          if (norm(label).includes(targetNorm) || targetNorm.includes(norm(label)))
            return { name: label, value: p.value || label };
        }
        return null;
      }

      for (const inputId of ['#playerSearch', '#playerSearch1']) {
        const $el = window.$ && $(inputId);
        if (!$el || !$el.length) continue;
        const instance = $el.data('ui-autocomplete');
        if (!instance) continue;
        const source = instance.options.source;

        const tryTerms = [
          rawName,
          rawName.split(' ')[0].replace(/[^a-zA-Z0-9]/g, ''),
          rawName.split(' ')[0],
          rawName.split(' ').slice(-1)[0],
        ].filter((t, i, arr) => t && arr.indexOf(t) === i);

        if (Array.isArray(source)) {
          const match = findInList(source);
          if (match) return match;
        } else if (typeof source === 'function') {
          for (const term of tryTerms) {
            let data = null;
            source({ term }, (d) => { data = d; });
            const match = findInList(data);
            if (match) return match;
          }
        }
      }
      return null;
    }, playerName, normalize(playerName));

    const typeTarget = playerResolution ? playerResolution.name : playerName;

    // Get the numeric player ID from #players-dropdown (already populated after season wait)
    const resolvedPlayerId = await page.evaluate((name) => {
      function norm(s) {
        return (s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').replace(/\s+/g, ' ').trim();
      }
      const target = norm(name);
      const sel = document.getElementById('players-dropdown');
      if (!sel) return null;
      for (const opt of sel.options) {
        if (norm(opt.text) === target) return opt.value;
      }
      for (const opt of sel.options) {
        if (norm(opt.text).includes(target) || target.includes(norm(opt.text))) return opt.value;
      }
      return null;
    }, typeTarget);

    console.log(`Resolved player: "${typeTarget}" (ID: ${resolvedPlayerId})`);

    // ── 3b. For playoffs with multiple seasons: filter seasons BEFORE selecting the player ──
    // Selecting the player then changing seasons resets the player dropdown.
    // So we validate seasons first, then select the player once with the final season set.
    let effectiveSeasons = seasons;
    if (playoff && resolvedPlayerId && seasons.length > 1) {
      console.log('Checking valid playoff seasons...');
      const validSeasons = [];
      for (const season of seasons) {
        await page.select('#season-dropdown', season);
        await page.waitForTimeout(400);
        const hasData = await page.evaluate((pid) => {
          const sel = document.getElementById('players-dropdown');
          return sel ? Array.from(sel.options).some(o => o.value === pid) : false;
        }, resolvedPlayerId);
        console.log(`  ${season}: ${hasData ? '✓' : '✗'}`);
        if (hasData) validSeasons.push(season);
      }

      if (validSeasons.length === 0) {
        throw new Error(`${playerName} has no playoff data for the selected season range.`);
      }
      effectiveSeasons = validSeasons;
      console.log(`Using playoff seasons: ${effectiveSeasons.join(', ')}`);

      // Set the final season selection before player selection
      await page.select('#season-dropdown', ...effectiveSeasons);
      await page.waitForTimeout(1500); // wait for player list to update
    }

    // ── 4. Select player via real UI interaction (type → click autocomplete item) ──
    // Done AFTER season validation so the season dropdown is in its final state.
    console.log(`Selecting player: "${typeTarget}"`);

    await page.waitForSelector('#playerSearch');
    await page.click('#playerSearch');
    await page.waitForTimeout(200);
    await page.keyboard.down('Control');
    await page.keyboard.press('a');
    await page.keyboard.up('Control');
    await page.keyboard.press('Backspace');

    await page.type('#playerSearch', typeTarget, { delay: 50 });
    await page.evaluate((name) => {
      for (const id of ['#playerSearch', '#playerSearch1']) {
        const $el = window.$ && $(id);
        if ($el && $el.length && $el.data('ui-autocomplete')) {
          $el.autocomplete('search', name);
          return;
        }
      }
    }, typeTarget);
    await page.waitForTimeout(1000);

    const clicked = await page.evaluate((target) => {
      function norm(s) {
        return (s || '')
          .normalize('NFD')
          .replace(/[\u0300-\u036f]/g, '')
          .toLowerCase()
          .replace(/[^a-z0-9 ]/g, ' ')
          .replace(/\s+/g, ' ')
          .trim();
      }
      const targetNorm = norm(target);
      const items = document.querySelectorAll('.ui-autocomplete .ui-menu-item');
      for (const item of items) {
        const t = item.textContent.trim();
        if (norm(t) === targetNorm || norm(t).includes(targetNorm) || targetNorm.includes(norm(t))) {
          item.click();
          return { ok: true, name: t };
        }
      }
      return { ok: false, count: items.length, items: Array.from(items).map(i => i.textContent.trim()) };
    }, typeTarget);

    console.log('Autocomplete click:', JSON.stringify(clicked));
    if (!clicked.ok) {
      throw new Error(`Player "${playerName}" not found in autocomplete. Items: ${JSON.stringify(clicked.items)}`);
    }
    await page.waitForTimeout(800);

    // ── 4. Modern mode ──
    if (modern) {
      await page.evaluate(() => {
        const toggle = document.getElementById('modernModeToggle');
        if (toggle && !toggle.checked) toggle.click();
      });
      await page.waitForTimeout(300);
    }

    // ── 5. Generate ──
    console.log('Clicking generate...');
    await page.waitForSelector('#generateGraphButton', { timeout: 10000 });
    const btnDisabled = await page.evaluate(() => {
      const btn = document.getElementById('generateGraphButton');
      return !btn || btn.disabled;
    });
    if (btnDisabled) throw new Error('Generate button not found or disabled');
    await page.click('#generateGraphButton');

    // ── 6. Wait for graph ──
    const graphTimeout = Math.max(45000, effectiveSeasons.length * 12000);
    console.log(`Waiting for graph (timeout: ${graphTimeout / 1000}s)...`);

    await page.waitForFunction(
      () => {
        const gc = document.getElementById('graph-container');
        return gc && gc.querySelector('img, canvas, svg');
      },
      { timeout: graphTimeout }
    );
    await page.waitForTimeout(500);

    // ── 7. Screenshot ──
    // Hide buttons before capturing
    await page.evaluate(() => {
      document.querySelectorAll('button, a').forEach(el => {
        const t = el.textContent.trim();
        if (t === 'Download Graph' || t === 'Copy to Clipboard') {
          el.style.visibility = 'hidden';
        }
      });
    });

    const rect = await page.evaluate(() => {
      // Prefer the inner SVG for the tightest crop; fall back to broader containers
      const candidates = [
        document.querySelector('.js-plotly-plot .main-svg'),
        document.querySelector('.js-plotly-plot'),
        document.querySelector('.plot-container'),
        document.getElementById('graph-container'),
      ];
      for (const el of candidates) {
        if (!el) continue;
        const r = el.getBoundingClientRect();
        if (r.width > 100 && r.height > 100) {
          return { x: r.x, y: r.y, width: r.width, height: r.height };
        }
      }
      return null;
    });

    const pad = 10;
    const clip = rect
      ? {
          x: Math.max(0, rect.x - pad),
          y: Math.max(0, rect.y - pad),
          width: Math.min(rect.width + pad * 2, 1000),
          height: Math.min(rect.height + pad * 2, 850),
        }
      : { x: 20, y: 100, width: 960, height: 750 };

    console.log('Taking screenshot...');
    const screenshot = await page.screenshot({ type: 'png', clip });
    toCache(cacheKey, screenshot);
    console.log('Done!');
    return screenshot;

  } finally {
    await page.close();
  }
}

// ─── Discord ───

const client = new Discord.Client({
  intents: [
    Discord.GatewayIntentBits.Guilds,
    Discord.GatewayIntentBits.GuildMessages,
    Discord.GatewayIntentBits.MessageContent,
  ],
});

client.once('clientReady', () => {
  console.log(`Bot online: ${client.user.tag}`);
  client.user.setActivity('!shotmap help', { type: Discord.ActivityType.Watching });
});

client.on('messageCreate', async (message) => {
  if (message.author.bot || !message.guild) return;
  const content = message.content.trim();

  if (content === '!shotmap help' || content === '!shotchart help') {
    return message.reply(
      '**Usage:** `!shotmap <player> <year> [endYear] [ps] [--modern]`\n' +
      '**Examples:**\n' +
      '`!shotmap LeBron James 2024` — 2023-24 regular season\n' +
      '`!shotmap LeBron James 2024 ps` — 2023-24 playoffs\n' +
      '`!shotmap Theo Ratliff 1998 2001` — 1997-98 through 2000-01\n' +
      '`!shotmap Steph Curry 2023 --modern`'
    );
  }

  if (!content.startsWith('!shotmap ') && !content.startsWith('!shotchart ')) return;

  const parts = content.split(/\s+/);
  let playerParts = [], years = [], modern = false, playoff = false;

  for (let i = 1; i < parts.length; i++) {
    if (parts[i] === '--modern') modern = true;
    else if (parts[i].toLowerCase() === 'ps') playoff = true;
    else if (/^\d{4}$/.test(parts[i])) years.push(parts[i]);
    else playerParts.push(parts[i]);
  }

  if (years.length === 0)
    return message.reply('Please include a year. Example: `!shotmap LeBron James 2024`');
  if (!playerParts.length)
    return message.reply('Please include a player name. Example: `!shotmap LeBron James 2024`');
  if (parseInt(years[0]) < 1997)
    return message.reply('Shot data is only available from the **1996-97** season onwards. Please use a year of **1997** or later.');

  const playerName = playerParts.join(' ');
  const yearStart = years[0];
  const yearEnd = years.length > 1 ? years[years.length - 1] : null;
  const yearLabel = yearEnd ? `${yearStart}-${yearEnd}` : yearStart;

  await message.react('⏳').catch(() => {});

  try {
    const img = await generateShotmap(playerName, yearStart, yearEnd, modern, playoff);
    await message.reply({
      content: `Shotmap: **${playerName}** (${yearLabel}${playoff ? ' Playoffs' : ''})${modern ? ' - Modern' : ''}`,
      files: [{ attachment: img, name: `shotmap_${playerName.replace(/\s+/g, '_')}_${yearLabel}${playoff ? '_ps' : ''}.png` }],
    });
  } catch (err) {
    console.error('Error:', err.message);
    const msg = err.message.toLowerCase().includes('waiting failed') || err.message.toLowerCase().includes('exceeded')
      ? 'The graph took too long to load. Try a shorter range of years.'
      : `Error: ${err.message}`;
    await message.reply(msg);
  }
});

client.login(process.env.DISCORD_TOKEN).catch(err => {
  console.error('Login failed:', err.message);
  process.exit(1);
});
