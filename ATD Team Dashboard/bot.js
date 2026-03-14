const { Client, GatewayIntentBits, AttachmentBuilder, EmbedBuilder } = require('discord.js');
const puppeteer = require('puppeteer');
require('dotenv').config();

// ── Tab / chart definitions ───────────────────────────────────────────────────
const TABS = {
  offense: {
    label: 'Offense',
    tabId: 'offense-tab',
    paneId: 'offense',
    charts: [
      { id: 'playspread-chart',  label: 'Play Spread' },
      { id: 'stylespread-chart', label: 'Style Spread' },
      { id: 'scoring-chart',     label: 'Scoring' },
      { id: 'ballhog-chart',     label: 'Possession' },
    ],
  },
  defense: {
    label: 'Defense',
    tabId: 'defense-tab',
    paneId: 'defense',
    charts: [
      { id: 'dplayspread-chart',    label: 'Defensive Play Spread' },
      { id: 'dstylespread-chart',   label: 'Defensive Style Spread' },
      { id: 'dscatterspread-chart', label: 'Defensive Scatter' },
      { id: 'dteambron-chart',      label: 'Defensive Rating' },
    ],
  },
  shooting: {
    label: 'Shooting',
    tabId: 'shooting-tab',
    paneId: 'shooting',
    charts: [
      { id: 'shotzone-chart',      label: 'Shot Zone' },
      { id: 'teamthrees-chart',    label: '3PT Shooting' },
      { id: 'shotdistance-chart',  label: 'Shot Distance' },
      { id: 'shotfrequency-chart', label: 'Shot Frequency' },
    ],
  },
  lineups: {
    label: 'Lineups',
    tabId: 'lineups-tab',
    paneId: 'lineups',
    charts: [
      { id: 'lineups-chart',     label: 'Lineups' },
      { id: 'matchups-chart',    label: 'Matchups' },
      { id: 'passnetwork-chart', label: 'Pass Network' },
      { id: 'teambron-chart',    label: 'Team Rating' },
    ],
  },
};

const VALID_TEAMS = new Set([
  'ATL', 'BOS', 'CLE', 'NOP', 'CHI', 'DAL', 'DEN', 'GSW', 'HOU', 'LAC',
  'LAL', 'MIA', 'MIL', 'MIN', 'BKN', 'NYK', 'ORL', 'IND', 'PHI', 'PHX',
  'POR', 'SAC', 'SAS', 'OKC', 'TOR', 'UTA', 'MEM', 'WAS', 'DET', 'CHA',
]);

const PREFIX = '!';
const DEFAULT_SEASON = '2025-26';

// ── Logging ───────────────────────────────────────────────────────────────────
function log(level, msg) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  console.log(`[${ts}] [${level.padEnd(5)}] ${msg}`);
}

// ── Cache ─────────────────────────────────────────────────────────────────────
// key: "TEAM_season_tab_seasonType"  →  [{ label, png }]
const chartCache = new Map();
const CHART_TTL = 6 * 60 * 60 * 1000; // 6 hours

function getCached(key) {
  const entry = chartCache.get(key);
  if (!entry) return null;
  if (Date.now() > entry.expires) { chartCache.delete(key); return null; }
  return entry.data;
}

function setCached(key, data) {
  chartCache.set(key, { data, expires: Date.now() + CHART_TTL });
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function parseSeason(yearStr) {
  const year = parseInt(yearStr);
  if (isNaN(year) || year < 2014 || year > 2027) return null;
  return `${year - 1}-${String(year).slice(-2)}`;
}

// "LAL 2026 Offense" or "LAL Offense" → { team, season, tab, seasonType }
function parseArgs(raw) {
  const tokens = raw.trim().split(/\s+/);

  let seasonType = 'regular';
  const pfIdx = tokens.findIndex(t => t.toLowerCase() === 'playoffs');
  if (pfIdx !== -1) { seasonType = 'playoffs'; tokens.splice(pfIdx, 1); }

  // Last token must be a valid tab name
  const tabRaw = tokens[tokens.length - 1]?.toLowerCase();
  if (!TABS[tabRaw]) {
    return { error: `Invalid tab \`${tokens[tokens.length - 1]}\`. Choose: \`offense\`, \`defense\`, \`shooting\`, \`lineups\`` };
  }
  const tab = tabRaw;
  tokens.pop();

  // Optional season year (4-digit)
  let season = DEFAULT_SEASON;
  const last = tokens[tokens.length - 1];
  if (/^\d{4}$/.test(last)) {
    season = parseSeason(last);
    if (!season) return { error: `Invalid year \`${last}\`. Use 2014–2027.` };
    tokens.pop();
  } else if (/^\d{4}-\d{2}$/.test(last)) {
    season = last;
    tokens.pop();
  }

  // Remaining token(s) are the team abbreviation
  const team = tokens.join(' ').toUpperCase();
  if (!team) return { error: 'Please provide a team. E.g. `!team LAL 2026 offense`' };
  if (!VALID_TEAMS.has(team)) {
    return { error: `Unknown team \`${team}\`.\nValid: ${[...VALID_TEAMS].sort().join(', ')}` };
  }

  return { team, season, tab, seasonType };
}

// ── Wait for charts in a given tab to render ──────────────────────────────────
async function waitForTabCharts(page, tab, timeout = 30000) {
  const firstChartId = TABS[tab].charts[0].id;
  const targetId = `${firstChartId}-target`;
  const start = Date.now();

  while (Date.now() - start < timeout) {
    const ready = await page.evaluate(id => {
      const el = document.getElementById(id);
      return !!(el && (el.querySelector('svg') || el.querySelector('.js-plotly-plot')));
    }, targetId);
    if (ready) return;
    await new Promise(r => setTimeout(r, 500));
  }
  throw new Error(`Charts for "${tab}" tab did not render within ${timeout / 1000}s`);
}

// ── Main chart generation ─────────────────────────────────────────────────────
async function generateTeamCharts(team, season, tab, seasonType) {
  const t0 = Date.now();
  const tabDef = TABS[tab];

  // Cache check
  const cacheKey = `${team}_${season}_${tab}_${seasonType}`;
  const cached = getCached(cacheKey);
  if (cached) {
    log('INFO', `Cache HIT  [${team} / ${season} / ${tab}] — ${Date.now() - t0}ms`);
    return cached;
  }

  log('INFO', `Starting   [${team} / ${season} / ${tab} / ${seasonType}]`);

  const browser = await puppeteer.launch({
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-gpu',
    ],
    headless: 'new',
  });

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1600, height: 900 });

    // ── 1. Load the page ─────────────────────────────────────────────────────
    const t1 = Date.now();
    await page.goto('https://nbavisuals.com/team-dashboard', { waitUntil: 'networkidle0', timeout: 60000 });
    log('INFO', `Page load  ${Date.now() - t1}ms`);

    // ── 2. Set dropdowns ─────────────────────────────────────────────────────
    await page.evaluate((season, team) => {
      const seasonDrop = document.querySelector('#season-dropdown');
      const teamDrop = document.querySelector('#team-dropdown');
      if (seasonDrop) Array.from(seasonDrop.options).forEach(o => o.selected = o.value === season);
      if (teamDrop) Array.from(teamDrop.options).forEach(o => o.selected = o.value === team);
      seasonDrop?.dispatchEvent(new Event('change', { bubbles: true }));
      teamDrop?.dispatchEvent(new Event('change', { bubbles: true }));
    }, season, team);

    // ── 3. Click Generate — wait for the AJAX data response ──────────────────
    const t2 = Date.now();
    const dataReady = page.waitForResponse(
      resp => resp.url().includes('/get_teamyears/') && resp.status() === 200,
      { timeout: 60000 },
    );
    await page.evaluate(() => document.querySelector('#generateGraphButton').click());
    await dataReady;
    log('INFO', `Data fetch ${Date.now() - t2}ms`);

    // ── 4. Wait for offense charts to render (always the first to load) ───────
    const t3 = Date.now();
    await waitForTabCharts(page, 'offense', 30000);
    log('INFO', `Offense rendered  ${Date.now() - t3}ms`);

    // ── 5. Switch to requested tab if not offense ─────────────────────────────
    if (tab !== 'offense') {
      const t4 = Date.now();
      // Click via JS to avoid CDP mouse timeout
      await page.evaluate(tabId => document.querySelector(`#${tabId}`)?.click(), tabDef.tabId);
      // Give the JS time to start rendering the new tab
      await new Promise(r => setTimeout(r, 500));
      // Wait for any additional AJAX this tab might trigger
      await page.waitForNetworkIdle({ idleTime: 800, timeout: 15000 }).catch(() => null);
      // Wait for charts in this tab
      await waitForTabCharts(page, tab, 25000);
      log('INFO', `${tab} rendered  ${Date.now() - t4}ms`);
    }

    // ── 6. Ensure the tab pane is visible (some are hidden by default) ────────
    await page.evaluate(paneId => {
      const pane = document.getElementById(paneId);
      if (pane) {
        pane.classList.remove('hidden');
        pane.style.display = 'block';
        pane.style.visibility = 'visible';
        pane.style.opacity = '1';
      }
    }, tabDef.paneId);
    await new Promise(r => setTimeout(r, 300));

    // ── 7. Screenshot each chart ──────────────────────────────────────────────
    const screenshots = [];
    for (const chart of tabDef.charts) {
      const el = await page.$(`#${chart.id}`);
      if (!el) {
        log('WARN', `Element not found: ${chart.id}`);
        continue;
      }
      const png = await el.screenshot({ type: 'png' });
      screenshots.push({ label: chart.label, png });
      log('INFO', `Screenshot ${chart.id}`);
    }

    if (screenshots.length === 0) throw new Error('No charts were captured — charts may not have rendered');

    log('INFO', `Done       [${team} / ${season} / ${tab}] — ${screenshots.length} charts, total ${Date.now() - t0}ms`);

    setCached(cacheKey, screenshots);
    return screenshots;

  } finally {
    await browser.close();
  }
}

// ── Discord bot ───────────────────────────────────────────────────────────────
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
  ],
});

client.on('messageCreate', async (message) => {
  if (message.author.bot || !message.content.startsWith(PREFIX)) return;

  const withoutPrefix = message.content.slice(PREFIX.length);
  const spaceIdx = withoutPrefix.indexOf(' ');
  const cmd = (spaceIdx === -1 ? withoutPrefix : withoutPrefix.slice(0, spaceIdx)).toLowerCase();

  if (cmd !== 'team') return;

  const argsStr = (spaceIdx === -1 ? '' : withoutPrefix.slice(spaceIdx + 1)).trim();

  // ── !team help ──────────────────────────────────────────────────────────────
  if (!argsStr || argsStr.toLowerCase() === 'help') {
    const embed = new EmbedBuilder()
      .setTitle('NBA Team Dashboard — Commands')
      .setColor(0xE87722)
      .setDescription(
        'Charts from [nbavisuals.com/team-dashboard](https://nbavisuals.com/team-dashboard)\n\n' +
        '**Usage:** `!team <TEAM> [year] <tab> [playoffs]`\n' +
        '**Year** — 4-digit end year, e.g. `2026` = 2025-26. Defaults to current season.\n\n' +
        '**Examples:**\n' +
        '`!team LAL 2026 offense`\n' +
        '`!team BOS 2024 defense`\n' +
        '`!team GSW 2022 shooting`\n' +
        '`!team MIA 2023 lineups playoffs`',
      )
      .addFields(
        { name: '`offense`',  value: 'Play Spread · Style Spread · Scoring · Possession',               inline: false },
        { name: '`defense`',  value: 'Def. Play Spread · Def. Style Spread · Scatter · Rating',         inline: false },
        { name: '`shooting`', value: 'Shot Zone · 3PT · Shot Distance · Shot Frequency',                inline: false },
        { name: '`lineups`',  value: 'Lineups · Matchups · Pass Network · Team Rating',                 inline: false },
      )
      .setFooter({ text: 'Teams: ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS' });
    return message.reply({ embeds: [embed] });
  }

  // ── !team <TEAM> [year] <tab> ───────────────────────────────────────────────
  const parsed = parseArgs(argsStr);
  if (parsed.error) return message.reply(parsed.error);

  const { team, season, tab, seasonType } = parsed;
  const tabDef = TABS[tab];
  const seasonLabel = seasonType === 'playoffs' ? `${season} Playoffs` : season;

  log('INFO', `Request    !team ${team} ${season} ${tab} ${seasonType} — ${message.author.tag}`);

  const thinking = await message.reply(
    `Generating **${tabDef.label}** charts for **${team}** (${seasonLabel})…`
  );

  try {
    const screenshots = await generateTeamCharts(team, season, tab, seasonType);

    const files = screenshots.map(s =>
      new AttachmentBuilder(s.png, { name: `${s.label.replace(/\s+/g, '_')}.png` })
    );

    await thinking.edit({
      content: `**${tabDef.label}** — **${team}** (${seasonLabel})`,
      files,
    });
    log('INFO', `Delivered  !team ${team} ${tab} → ${message.author.tag} (${files.length} charts)`);
  } catch (err) {
    log('ERROR', `!team ${team} ${tab} — ${err.message}`);
    await thinking.edit(`Error: ${err.message}`);
  }
});

client.once('ready', () => {
  log('INFO', `Online     ${client.user.tag}`);
  log('INFO', `Commands   !team <TEAM> [year] <tab>   |   !team help`);
  log('INFO', `Tabs       offense  defense  shooting  lineups`);
});

if (!process.env.DISCORD_BOT_TOKEN) {
  log('ERROR', 'DISCORD_BOT_TOKEN not found in .env');
  process.exit(1);
}

client.login(process.env.DISCORD_BOT_TOKEN);
