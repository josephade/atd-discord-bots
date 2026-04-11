require('dotenv').config();
const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const Discord = require('discord.js');

// ─── Cache ────────────────────────────────────────────────────────────────────

const CACHE_DIR = path.join(__dirname, 'cache');

/**
 * Returns the start year of the current NBA season.
 * The season that begins in October is keyed by its start year:
 *   Oct–Dec 2025 → season start year 2025 (2025-26)
 *   Jan–Sep 2026 → season start year 2025 (still 2025-26)
 */
function currentSeasonStartYear() {
  const now = new Date();
  const m = now.getMonth() + 1; // 1-based
  const y = now.getFullYear();
  return m >= 10 ? y : y - 1;
}

/**
 * Shotmap uses the season START year (2025 = "2025-26").
 * Do not cache if the requested year is the ongoing season.
 */
function isCurrentSeason(year) {
  return parseInt(year) === currentSeasonStartYear();
}

function getCacheKey(playerName, year, modern) {
  const clean = playerName
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_|_$/g, '');
  return `${clean}${modern ? '_modern' : ''}_${year}.png`;
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

async function generateShotmap(playerName, year, modern = false) {
  const cacheKey = getCacheKey(playerName, year, modern);
  const live = isCurrentSeason(year);
  if (!live) {
    const cached = fromCache(cacheKey);
    if (cached) {
      console.log(`Cache hit: ${cacheKey}`);
      return cached;
    }
  } else {
    console.log(`Live season (${year}) — skipping cache read`);
  }

  const seasonFormat = `${year}-${(parseInt(year) + 1).toString().slice(-2)}`;
  console.log(`Generating: ${playerName} | ${seasonFormat}${modern ? ' | Modern' : ''}`);

  const b = await getBrowser();
  const page = await b.newPage();

  try {
    await page.setUserAgent(
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    );
    page.setDefaultTimeout(30000);

    // ── 1. Load page ──
    console.log('Loading page...');
    await page.goto('https://nbavisuals.com/shotmap', {
      waitUntil: 'networkidle2',
      timeout: 30000,
    });

    // ── 2. Select season ──
    console.log(`Selecting season: ${seasonFormat}`);
    await page.waitForSelector('#season-dropdown');
    await page.select('#season-dropdown', seasonFormat);
    // Wait for season data to load into the player dropdown
    await page.waitForTimeout(2000);

    // ── 3. Find player via autocomplete source data ──
    console.log(`Searching for player: "${playerName}"`);

    const result = await page.evaluate((rawName, targetNorm) => {
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
          if (norm(label) === targetNorm) return { label, value: p.value || p };
        }
        for (const p of players) {
          const label = p.label || p.value || p;
          if (norm(label).includes(targetNorm) || targetNorm.includes(norm(label))) {
            return { label, value: p.value || p };
          }
        }
        return null;
      }

      // ── Strategy A: Call autocomplete source with the actual search term ──
      for (const inputId of ['#playerSearch', '#playerSearch1']) {
        const $el = window.$ && $(inputId);
        if (!$el || !$el.length) continue;

        const instance = $el.data('ui-autocomplete');
        if (!instance) continue;

        const source = instance.options.source;

        if (Array.isArray(source)) {
          const match = findInList(source);
          if (match) return { via: 'autocomplete-array', name: match.label, value: match.value, inputId };
          return { via: null, source: 'array', count: source.length, sample: (source[0].label || source[0]).toString() };
        }

        if (typeof source === 'function') {
          // Call with the actual player name — local sources return synchronously
          let filtered = null;
          source({ term: rawName }, (data) => { filtered = data; });
          if (filtered && filtered.length > 0) {
            const match = findInList(filtered);
            if (match) return { via: 'autocomplete-fn', name: match.label, value: match.value, inputId };
            return {
              via: null,
              source: 'fn-filtered',
              count: filtered.length,
              sample: (filtered[0].label || filtered[0].value || filtered[0]).toString(),
              all: filtered.slice(0, 20).map(p => p.label || p.value || p),
            };
          }
          // Try progressively simpler search terms to handle apostrophes/accents
          const fallbackTerms = [
            rawName.split(' ')[0].replace(/[^a-zA-Z0-9]/g, ''), // "Amare" from "Amar'e"
            rawName.split(' ')[0],                                // "Amar'e"
            rawName.split(' ').slice(-1)[0],                      // last name
            rawName.replace(/[^a-zA-Z0-9 ]/g, '').split(' ')[0], // stripped first name
          ].filter((t, i, arr) => t && arr.indexOf(t) === i);    // unique, non-empty

          for (const term of fallbackTerms) {
            let byTerm = null;
            source({ term }, (data) => { byTerm = data; });
            if (byTerm && byTerm.length > 0) {
              const match = findInList(byTerm);
              if (match) return { via: 'autocomplete-fn-short', name: match.label, value: match.value, inputId };
              return {
                via: null,
                source: 'fn-shortterm',
                term,
                count: byTerm.length,
                sample: (byTerm[0].label || byTerm[0]).toString(),
                all: byTerm.slice(0, 20).map(p => p.label || p.value || p),
              };
            }
          }
          return { via: null, source: 'fn-empty', filteredCount: filtered ? filtered.length : 0 };
        }
      }

      // ── Strategy B: Check all <select> elements for player data ──
      const selects = document.querySelectorAll('select');
      for (const sel of selects) {
        if (sel.options.length < 5) continue;
        for (const opt of sel.options) {
          if (!opt.value || !opt.text) continue;
          if (norm(opt.text) === targetNorm || norm(opt.text).includes(targetNorm)) {
            return { via: 'select', name: opt.text, selectId: sel.id, value: opt.value };
          }
        }
      }

      // Debug report
      return {
        via: null,
        selects: Array.from(selects).map(s => ({ id: s.id, count: s.options.length })),
        jquery: !!window.$,
        inputs: ['#playerSearch', '#playerSearch1'].map(id => {
          const $el = window.$ && $(id);
          if (!$el || !$el.length) return { id, found: false };
          const inst = $el.data('ui-autocomplete');
          return { id, found: true, hasAutocomplete: !!inst };
        }),
      };
    }, playerName, normalize(playerName));

    console.log('Player search result:', JSON.stringify(result));

    if (!result.via) {
      // ── Strategy C: Type into the input and use jQuery to trigger search ──
      console.log('Trying UI interaction fallback...');

      await page.waitForSelector('#playerSearch');
      await page.click('#playerSearch');
      await page.waitForTimeout(300);
      // Clear any existing text
      await page.keyboard.down('Control');
      await page.keyboard.press('a');
      await page.keyboard.up('Control');
      await page.type('#playerSearch', playerName, { delay: 60 });
      await page.waitForTimeout(500);

      // Trigger jQuery autocomplete search explicitly
      await page.evaluate((name) => {
        for (const id of ['#playerSearch', '#playerSearch1']) {
          const $el = window.$ && $(id);
          if ($el && $el.length && $el.data('ui-autocomplete')) {
            $el.autocomplete('search', name);
            return;
          }
        }
      }, playerName);
      await page.waitForTimeout(1500);

      // Now try to grab items from the open dropdown
      const fallback = await page.evaluate((target) => {
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
        console.log('ui-menu-item count:', items.length);
        for (const item of items) {
          const t = item.textContent.trim();
          if (norm(t) === targetNorm || norm(t).includes(targetNorm) || targetNorm.includes(norm(t))) {
            item.click();
            return { via: 'ui-click', name: t };
          }
        }
        return {
          via: null,
          itemCount: items.length,
          items: Array.from(items).map(i => i.textContent.trim()),
        };
      }, playerName);

      console.log('UI fallback result:', JSON.stringify(fallback));

      if (!fallback.via) {
        throw new Error(
          `Player "${playerName}" not found for the ${year} season. ` +
          `Debug: ${JSON.stringify({ ...result, ...fallback })}`
        );
      }

      // Successfully selected via UI click - proceed
      await page.waitForTimeout(500);
    } else {
      // ── Set player via discovered data ──
      await page.evaluate((res) => {
        function norm(s) {
          return (s || '')
            .normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '')
            .toLowerCase()
            .replace(/[^a-z0-9 ]/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
        }

        if (res.via === 'select') {
          // Direct select manipulation
          const sel = document.getElementById(res.selectId);
          sel.value = res.value;
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          const input = document.getElementById('playerSearch') || document.getElementById('playerSearch1');
          if (input) input.value = res.name;
        } else {
          // Autocomplete source - need to set it via jQuery UI
          for (const inputId of ['#playerSearch', '#playerSearch1']) {
            const $el = window.$ && $(inputId);
            if (!$el || !$el.length) continue;
            const inst = $el.data('ui-autocomplete');
            if (!inst) continue;

            // Simulate selecting via the autocomplete
            $el.val(res.name);
            // Trigger the select event that jQuery UI autocomplete fires
            $el.autocomplete('option', 'select')({ type: 'autocompleteselect' }, { item: { label: res.name, value: res.value } });
            $el.trigger('change');
            break;
          }

          // Also try setting the hidden select directly
          for (const selId of ['players-dropdown', 'players-dropdown1']) {
            const sel = document.getElementById(selId);
            if (!sel) continue;
            for (const opt of sel.options) {
              if (norm(opt.text) === norm(res.name) || opt.value === res.value) {
                sel.value = opt.value;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                break;
              }
            }
          }
        }
      }, result);

      console.log(`Selected [${result.via}]: ${result.name}`);
      await page.waitForTimeout(600);
    }

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
    const clicked = await page.evaluate(() => {
      const btn = document.getElementById('generateGraphButton');
      if (btn && !btn.disabled) { btn.click(); return true; }
      return false;
    });
    if (!clicked) throw new Error('Generate button not found or disabled');

    // ── 6. Wait for graph ──
    console.log('Waiting for graph...');
    await page.waitForFunction(
      () => {
        const gc = document.getElementById('graph-container');
        return gc && gc.querySelector('svg, canvas, img');
      },
      { timeout: 20000 }
    );
    await page.waitForTimeout(700);

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
    if (!live) {
      toCache(cacheKey, screenshot);
    } else {
      console.log(`Live season — screenshot not cached`);
    }
    console.log('Done!');
    return screenshot;

  } catch (err) {
    // Dump the page HTML to the console so we can see if the site returned
    // a bot-detection page, Cloudflare challenge, or a changed layout.
    try {
      const html = await page.content();
      console.error('Page HTML on failure (first 2000 chars):\n', html.slice(0, 2000));
    } catch (_) {}
    throw err;
  } finally {
    await page.close();
  }
}

// ─── Discord ──────────────────────────────────────────────────────────────────

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
      '**Usage:** `!shotmap <player> <year> [--modern]`\n' +
      '**Examples:**\n' +
      '`!shotmap LeBron James 2024`\n' +
      '`!shotmap Goran Dragic 2019`\n' +
      '`!shotmap Steph Curry 2023 --modern`'
    );
  }

  if (!content.startsWith('!shotmap ') && !content.startsWith('!shotchart ')) return;

  const parts = content.split(/\s+/);
  let playerParts = [], year = null, modern = false;

  for (let i = 1; i < parts.length; i++) {
    if (parts[i] === '--modern') modern = true;
    else if (/^\d{4}$/.test(parts[i])) year = parts[i];
    else playerParts.push(parts[i]);
  }

  if (!year)
    return message.reply('Please include a year. Example: `!shotmap LeBron James 2024`');
  if (!playerParts.length)
    return message.reply('Please include a player name. Example: `!shotmap LeBron James 2024`');

  const playerName = playerParts.join(' ');
  await message.react('⏳').catch(() => {});

  try {
    const img = await generateShotmap(playerName, year, modern);
    await message.reply({
      content: `Shotmap: **${playerName}** (${year})${modern ? ' - Modern' : ''}`,
      files: [{ attachment: img, name: `shotmap_${playerName.replace(/\s+/g, '_')}_${year}.png` }],
    });
  } catch (err) {
    console.error('Error:', err.message);
    await message.reply(`Error: ${err.message}`);
  }
});

client.login(process.env.DISCORD_TOKEN).catch(err => {
  console.error('Login failed:', err.message);
  process.exit(1);
});
