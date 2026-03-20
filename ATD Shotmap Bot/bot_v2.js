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

function getCacheKey(playerName, yearStart, yearEnd, modern, playoff, dark) {
  const clean = playerName
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_|_$/g, '');
  const yKey = yearEnd && yearEnd !== yearStart ? `${yearStart}-${yearEnd}` : yearStart;
  return `${clean}${playoff ? '_ps' : ''}${modern ? '_modern' : ''}${dark ? '_dark' : ''}_${yKey}_v3.png`;
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

// ─── Shotmap ──────────────────────────────────────────────────────────────────

async function generateShotmap(playerName, yearStart, yearEnd, modern = false, playoff = false, dark = false) {
  const cacheKey = getCacheKey(playerName, yearStart, yearEnd, modern, playoff, dark);
  const cached = fromCache(cacheKey);
  if (cached) {
    console.log(`Cache hit: ${cacheKey}`);
    return cached;
  }

  const start = parseInt(yearStart);
  const end = yearEnd ? parseInt(yearEnd) : start;

  // Build seasons array newest-first (matches page JS logic)
  const allSeasons = [];
  for (let y = 2026; y >= 1997; y--) allSeasons.push(yearToSeason(y));
  const startSeason = yearToSeason(start);
  const endSeason   = yearToSeason(end);
  const si = allSeasons.indexOf(startSeason);
  const ei = allSeasons.indexOf(endSeason);
  if (si === -1) throw new Error(`Season ${startSeason} not available. Use 1997–2026.`);
  if (ei === -1) throw new Error(`Season ${endSeason} not available. Use 1997–2026.`);
  const seasons = allSeasons.slice(Math.min(si, ei), Math.max(si, ei) + 1);

  const stype = playoff ? 'playoffs' : 'regular';
  const seasonLabel = seasons.length === 1 ? seasons[0] : `${seasons[seasons.length - 1]} – ${seasons[0]}`;
  console.log(`Generating: ${playerName} | ${seasonLabel}${playoff ? ' | Playoffs' : ''}`);

  const b = await puppeteer.launch({
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    defaultViewport: { width: 1600, height: 1000 },
    protocolTimeout: 120000,
  });
  const page = await b.newPage();

  try {
    await page.setUserAgent(
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    );
    page.setDefaultTimeout(30000);

    // ── 1. Load page — establishes session cookie + gets CSRF token ──
    console.log('Loading page...');
    await page.goto('https://nbavisuals.com/shotmap', { waitUntil: 'networkidle2', timeout: 30000 });

    const csrfToken = await page.evaluate(() =>
      document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || ''
    );
    const cookies = await page.cookies();
    const cookieStr = cookies.map(c => `${c.name}=${c.value}`).join('; ');

    // ── 2. Fetch player list via API ──
    const seasonString = seasons.join(',');
    console.log(`Fetching players: ${seasonString} / ${stype}`);
    const playerMap = await page.evaluate(async (url) => {
      try {
        const r = await fetch(url);
        if (!r.ok) return {};
        return r.json();
      } catch { return {}; }
    }, `/get_players/${seasonString}/${stype}`);

    if (!playerMap || Object.keys(playerMap).length === 0)
      throw new Error(`No players found for ${seasonLabel}${playoff ? ' (playoffs)' : ''}. Season may not have data.`);

    // ── 3. Find player ID ──
    function norm(s) {
      return (s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
        .toLowerCase().replace(/[^a-z0-9 ]/g, ' ').replace(/\s+/g, ' ').trim();
    }
    const target = norm(playerName);
    let playerId = null, foundName = null;
    for (const [id, name] of Object.entries(playerMap)) {
      if (norm(name) === target) { playerId = id; foundName = name; break; }
    }
    if (!playerId) {
      for (const [id, name] of Object.entries(playerMap)) {
        if (norm(name).includes(target) || target.includes(norm(name))) {
          playerId = id; foundName = name; break;
        }
      }
    }
    if (!playerId) {
      const suggestions = Object.values(playerMap)
        .filter(n => target.split(' ').some(p => p.length > 2 && norm(n).includes(p)))
        .slice(0, 3).join(', ');
      throw new Error(`Player "${playerName}" not found.${suggestions ? ` Did you mean: ${suggestions}?` : ''}`);
    }
    console.log(`Found: "${foundName}" (ID: ${playerId})`);

    // ── 4. POST directly to /shotmap — no button click needed ──
    // The generate button submits a form via AJAX and returns { image: "base64..." }.
    // We replicate that request from Node.js to avoid Puppeteer protocol timeouts.
    console.log('Submitting API request...');
    const body = new URLSearchParams();
    body.append('csrf_token', csrfToken);
    body.append('graphtype', 'shotmap');
    seasons.forEach(s => body.append('seasons[]', s));
    body.append('player', playerId);
    if (playoff) body.append('season_type', 'playoffs');
    if (dark) body.append('darkmode', 'on');
    body.append('assistmode', 'on');
    if (modern) body.append('modernmode', 'on');

    const apiRes = await fetch('https://nbavisuals.com/shotmap', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
        'Cookie': cookieStr,
        'Referer': 'https://nbavisuals.com/shotmap',
      },
      body: body.toString(),
      signal: AbortSignal.timeout(180000),
    });

    if (!apiRes.ok) {
      const text = await apiRes.text().catch(() => '');
      throw new Error(`Server error ${apiRes.status}: ${text.slice(0, 200)}`);
    }

    const result = await apiRes.json();
    if (result.error) throw new Error(result.error);
    if (!result.image) throw new Error('No image returned from server');

    const screenshot = Buffer.from(result.image, 'base64');
    toCache(cacheKey, screenshot);
    console.log(`Done: ${foundName} | ${seasonLabel}`);
    return screenshot;

  } finally {
    await b.close();
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
    const embed = new Discord.EmbedBuilder()
      .setTitle('ShotMap Bot — Commands')
      .setColor(0x5865F2)
      .setDescription(
        'Generates NBA shot charts from [nbavisuals.com](https://nbavisuals.com/shotmap).\n\n' +
        '**Usage:** `!shotmap <player> <year> [endYear] [ps] [--modern] [--dark]`'
      )
      .addFields(
        {
          name: 'Flags',
          value: [
            '`ps` — playoffs instead of regular season',
            '`--dark` — dark background',
            '`--modern` — modern mode styling',
          ].join('\n'),
        },
        {
          name: 'Examples',
          value: [
            '`!shotmap LeBron James 2024` — 2023-24 regular season',
            '`!shotmap LeBron James 2024 ps` — 2023-24 playoffs',
            '`!shotmap Theo Ratliff 1998 2001` — multi-year range',
            '`!shotmap Steph Curry 2023 --modern --dark`',
            '`!shotmap Steve Nash 2005 2008 ps --dark`',
          ].join('\n'),
        }
      )
      .setFooter({ text: 'Shot data available from 1996-97 onwards' });
    return message.reply({ embeds: [embed] });
  }

  if (!content.startsWith('!shotmap ') && !content.startsWith('!shotchart ')) return;

  const parts = content.split(/\s+/);
  let playerParts = [], years = [], modern = false, playoff = false, dark = false;

  for (let i = 1; i < parts.length; i++) {
    if (parts[i] === '--modern') modern = true;
    else if (parts[i] === '--dark') dark = true;
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
    const img = await generateShotmap(playerName, yearStart, yearEnd, modern, playoff, dark);
    await message.reply({
      content: `Shotmap: **${playerName}** (${yearLabel}${playoff ? ' Playoffs' : ''})${modern ? ' - Modern' : ''}${dark ? ' - Dark' : ''}`,
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
