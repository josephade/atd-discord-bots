const { Client, GatewayIntentBits, AttachmentBuilder, EmbedBuilder } = require('discord.js');
const puppeteer = require('puppeteer');
require('dotenv').config();

// ── Chart type definitions ────────────────────────────────────────────────────
// command → { graphType: data-graph value from site, label, desc }
const CHART_TYPES = {
  'playtype':   { graphType: 'Playtype',      label: 'Play Type Chart',         desc: 'Bubble chart of efficiency (PPP) vs frequency for each play type — Post-Up, Roll-man, Spot-up, Isolation, etc.' },
  'scoring':    { graphType: 'Scoring',       label: 'Scoring Chart',           desc: 'Scoring tendencies and efficiency breakdown across scoring situations.' },
  'shotzone':   { graphType: 'ShotZone',      label: 'Shot Zone Chart',         desc: 'Field goal % and attempt frequency broken down by court zone.' },
  '3pt':        { graphType: 'Threepoint',    label: '3PT Splits',              desc: 'Three-point shooting split by catch-and-shoot, off-the-dribble, corner, etc.' },
  'dribble':    { graphType: 'DribbleShot',   label: 'Dribble Shot Chart',      desc: 'Shooting efficiency by number of dribbles taken before the shot.' },
  'shotdist':   { graphType: 'ShotDistance',  label: 'Shot Distance Chart',     desc: 'Shooting efficiency and shot volume by distance from the basket.' },
  'shotfreq':   { graphType: 'ShotFrequency', label: 'Shot Frequency Chart',    desc: 'Shot selection frequency distribution across different court areas.' },
  'playmaking': { graphType: 'Playmake',      label: 'Playmaking Chart',        desc: 'Assist tendencies, pass distributions, and playmaking patterns.' },
  'possession': { graphType: 'Ballhog',       label: 'Possession Chart',        desc: 'Ball-handling usage, touches per game, and possession breakdown.' },
  'hustle':     { graphType: 'Hustle',        label: 'Hustle Chart',            desc: 'Hustle metrics: deflections, contested shots, loose ball recoveries, charges drawn.' },
  'tracking':   { graphType: 'Tracking',      label: 'Tracking Stats',          desc: 'Detailed tracking data: speed, distance traveled, touches, time of possession.' },
  'spread':     { graphType: 'PlayerSpread',  label: 'Player Impact Spread',    desc: "Player's impact visualized across a spread of statistical categories." },
  'playstyle':  { graphType: 'Playstyle',     label: 'Playstyle Chart',         desc: 'Overall playstyle profile showing tendencies across key dimensions.' },
};

const PREFIX = '!';
const DEFAULT_SEASON = '2024-25';
const W = 1200, H = 750;

// ── Logging ───────────────────────────────────────────────────────────────────

function log(level, msg) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  console.log(`[${ts}] [${level.padEnd(5)}] ${msg}`);
}

// ── In-memory cache ───────────────────────────────────────────────────────────

// Player map cache:  "season_seasonType"                    → {id: name, ...}
// Chart image cache: "playerId_season_graphType_seasonType" → PNG Buffer
const playerMapCache = new Map();
const chartCache     = new Map();
const PLAYER_MAP_TTL =  1 * 60 * 60 * 1000;  // 1 hour  — player lists change rarely
const CHART_TTL      = 24 * 60 * 60 * 1000;  // 24 hours — historical charts never change

function getCached(cache, key) {
  const entry = cache.get(key);
  if (!entry) return null;
  if (Date.now() > entry.expires) { cache.delete(key); return null; }
  return entry.data;
}

function setCached(cache, key, data, ttl) {
  cache.set(key, { data, expires: Date.now() + ttl });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

// "2025" → "2024-25"
function parseSeason(yearStr) {
  const year = parseInt(yearStr);
  if (isNaN(year) || year < 2014 || year > 2026) return null;
  return `${year - 1}-${String(year).slice(-2)}`;
}

function normalize(str) {
  return str
    .toLowerCase()
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9 ]/g, '')
    .trim();
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

// "Aaron Gordon 2025 playoffs" → { playerName, season, seasonType }
function parseArgs(raw) {
  const tokens = raw.trim().split(/\s+/);

  let seasonType = 'regular';
  const pfIdx = tokens.findIndex(t => t.toLowerCase() === 'playoffs');
  if (pfIdx !== -1) { seasonType = 'playoffs'; tokens.splice(pfIdx, 1); }

  let season = null;
  const last = tokens[tokens.length - 1];
  if (/^\d{4}$/.test(last)) {
    season = parseSeason(last);
    if (!season) return { error: `Invalid year "${last}". Use 2014–2026.` };
    tokens.pop();
  } else if (/^\d{4}-\d{2}$/.test(last)) {
    season = last;
    tokens.pop();
  }

  const playerName = tokens.join(' ');
  if (!playerName) return { error: 'Please provide a player name.' };
  return { playerName, season: season || DEFAULT_SEASON, seasonType };
}

// ── Chart generation ──────────────────────────────────────────────────────────

async function generateChartImage(playerName, season, graphType, seasonType) {
  const t0 = Date.now();

  // ── Fast path: check caches before launching Puppeteer ──────────────────────
  const mapKey = `${season}_${seasonType}`;
  const cachedPlayerMap = getCached(playerMapCache, mapKey);
  if (cachedPlayerMap) {
    const player = findPlayer(playerName, cachedPlayerMap);
    if (player) {
      const chartKey = `${player.id}_${season}_${graphType}_${seasonType}`;
      const cachedPng = getCached(chartCache, chartKey);
      if (cachedPng) {
        log('INFO', `Cache HIT  chart [${player.name} / ${season} / ${graphType}] — ${Date.now() - t0}ms`);
        return { screenshot: cachedPng, playerName: player.name };
      }
    }
  }

  // ── Full path: launch Puppeteer ──────────────────────────────────────────────
  log('INFO', `Starting  [${playerName} / ${season} / ${graphType} / ${seasonType}]`);
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
    await page.setViewport({ width: W, height: H });

    // Navigate — establishes session cookie + CSRF token
    const t1 = Date.now();
    await page.goto('https://nbavisuals.com/player-dashboard', { waitUntil: 'networkidle0', timeout: 60000 });
    log('INFO', `Page load  ${Date.now() - t1}ms`);

    // Player map — fetch if not cached
    let playerMap = getCached(playerMapCache, mapKey);
    if (!playerMap) {
      const t2 = Date.now();
      playerMap = await page.evaluate(async (season, seasonType) => {
        const res = await fetch(`/get_players/${encodeURIComponent(season)}/${encodeURIComponent(seasonType)}`);
        if (!res.ok) throw new Error(`Player list HTTP ${res.status}`);
        return res.json();
      }, season, seasonType);
      if (!playerMap || Object.keys(playerMap).length === 0) {
        throw new Error(`No players found for ${season} (${seasonType}). Season may not be available yet.`);
      }
      setCached(playerMapCache, mapKey, playerMap, PLAYER_MAP_TTL);
      log('INFO', `Player map ${Object.keys(playerMap).length} players, cached ${PLAYER_MAP_TTL / 3600000}h — ${Date.now() - t2}ms`);
    } else {
      log('INFO', `Cache HIT  player map [${mapKey}]`);
    }

    // Match player
    const player = findPlayer(playerName, playerMap);
    if (!player) {
      const q = normalize(playerName);
      const suggestions = Object.values(playerMap)
        .filter(n => normalize(n).split(' ').some(p => p.length > 2 && (q.includes(p) || p.includes(q.split(' ')[0]))))
        .slice(0, 3).join(', ');
      throw new Error(
        `Player "${playerName}" not found for ${season}.` +
        (suggestions ? ` Did you mean: ${suggestions}?` : '')
      );
    }
    log('INFO', `Matched    ${player.name} (id=${player.id})`);

    // Check chart cache again with canonical player ID (handles name variant lookups)
    const chartKey = `${player.id}_${season}_${graphType}_${seasonType}`;
    const cachedPng = getCached(chartCache, chartKey);
    if (cachedPng) {
      log('INFO', `Cache HIT  chart [${player.name} / ${season} / ${graphType}] — ${Date.now() - t0}ms`);
      return { screenshot: cachedPng, playerName: player.name };
    }

    // POST chart data
    const t3 = Date.now();
    const chartData = await page.evaluate(async (playerId, season, graphType, seasonType) => {
      const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
      const body = new URLSearchParams();
      body.append('seasons[]', season);
      body.append('player', playerId);
      body.append('graph_type', graphType);
      body.append('season_type', seasonType);
      const headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
      };
      if (csrfToken) headers['X-CSRFToken'] = csrfToken;
      const res = await fetch('/player-dashboard', { method: 'POST', headers, body: body.toString() });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
      }
      return res.json();
    }, player.id, season, graphType, seasonType);
    log('INFO', `API data   ${Date.now() - t3}ms`);

    if (chartData.error) throw new Error(chartData.error);
    if (!chartData.data || !chartData.layout) throw new Error('No chart data returned');

    // Render Plotly chart → PNG
    const t4 = Date.now();
    const { data: plotlyData, layout } = chartData;

    // Escape </script> so injected JSON can't break the HTML parser
    const safeJSON = (obj) => JSON.stringify(obj).replace(/<\/script>/gi, '<\\/script>');

    const html = `<!DOCTYPE html>
<html><head>
  <meta charset="utf-8">
  <script src="https://cdn.plot.ly/plotly-2.8.3.min.js"></script>
  <style>* { margin: 0; padding: 0; box-sizing: border-box; } body { background: #000; overflow: hidden; } #chart { width: ${W}px; height: ${H}px; }</style>
</head><body>
  <div id="chart"></div>
  <script>
    Plotly.newPlot('chart', ${safeJSON(plotlyData)}, ${safeJSON({ ...layout, width: W, height: H, autosize: false })}, { responsive: false, displayModeBar: false })
      .then(() => document.body.setAttribute('data-ready', '1'));
  </script>
</body></html>`;

    await page.setContent(html, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForSelector('body[data-ready="1"]', { timeout: 45000 });
    const screenshot = await page.screenshot({ type: 'png' });
    log('INFO', `Rendered   ${Date.now() - t4}ms`);

    // Cache the PNG
    setCached(chartCache, chartKey, screenshot, CHART_TTL);
    log('INFO', `Done       ${player.name} / ${season} / ${graphType} — total ${Date.now() - t0}ms`);

    return { screenshot, playerName: player.name };
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

  // ── !charthelp ─────────────────────────────────────────────────────────────
  if (cmd === 'charthelp') {
    const embed = new EmbedBuilder()
      .setTitle('NBA Player Dashboard — Commands')
      .setColor(0xE87722)
      .setDescription(
        `Charts are pulled live from [nbavisuals.com](https://nbavisuals.com/player-dashboard).\n\n` +
        `**Usage:** \`!<command> <player name> [year]\`\n` +
        `**Year** — 4-digit season end year, e.g. \`2025\` = 2024-25. Defaults to current season.\n` +
        `**Playoffs** — append \`playoffs\` for playoff data.\n\n` +
        `**Examples:**\n` +
        `\`!playtype Aaron Gordon 2025\`\n` +
        `\`!shotzone LeBron James 2020 playoffs\`\n` +
        `\`!scoring Steph Curry\``
      );

    for (const [key, def] of Object.entries(CHART_TYPES)) {
      embed.addFields({ name: `\`!${key}\`  —  ${def.label}`, value: def.desc, inline: false });
    }

    embed.setFooter({ text: `Data available from 2013-14 onwards` });
    return message.reply({ embeds: [embed] });
  }

  // ── Chart commands ──────────────────────────────────────────────────────────
  const chartDef = CHART_TYPES[cmd];
  if (!chartDef) return;

  const argsStr = spaceIdx === -1 ? '' : withoutPrefix.slice(spaceIdx + 1).trim();
  if (!argsStr) {
    return message.reply(
      `Usage: \`!${cmd} <player name> [year]\`\n` +
      `\`!${cmd} Aaron Gordon 2025\`\n` +
      `\`!${cmd} LeBron James 2020 playoffs\``
    );
  }

  const parsed = parseArgs(argsStr);
  if (parsed.error) return message.reply(parsed.error);

  const { playerName, season, seasonType } = parsed;
  const seasonLabel = seasonType === 'playoffs' ? `${season} Playoffs` : season;

  log('INFO', `Request    !${cmd} "${playerName}" ${season} ${seasonType} — ${message.author.tag}`);
  const thinking = await message.reply(`Generating **${chartDef.label}** for **${playerName}** (${seasonLabel})...`);

  try {
    const { screenshot, playerName: foundName } = await generateChartImage(playerName, season, chartDef.graphType, seasonType);

    const attachment = new AttachmentBuilder(screenshot, { name: 'chart.png' });
    await thinking.edit({
      content: `**${chartDef.label}** — ${foundName} (${seasonLabel})`,
      files: [attachment],
    });
    log('INFO', `Delivered  !${cmd} ${foundName} to ${message.author.tag}`);
  } catch (err) {
    log('ERROR', `!${cmd} "${playerName}" — ${err.message}`);
    await thinking.edit(`Error: ${err.message}`);
  }
});

client.once('ready', () => {
  log('INFO', `Online     ${client.user.tag}`);
  log('INFO', `Commands   ${Object.keys(CHART_TYPES).map(c => `!${c}`).join('  ')}  !charthelp`);
});

if (!process.env.DISCORD_BOT_TOKEN) {
  log('ERROR', 'DISCORD_BOT_TOKEN not found in .env');
  process.exit(1);
}

client.login(process.env.DISCORD_BOT_TOKEN);
