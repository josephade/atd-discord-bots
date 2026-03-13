'use strict';

require('dotenv').config();
const { Client, GatewayIntentBits, ActivityType } = require('discord.js');
const puppeteer = require('puppeteer');
const { findPlayer, parseSeason, parsePlayerArgs, parseWowyArgs } = require('./lookup');

// ─── Team abbreviation map ────────────────────────────────────────────────────

const TEAM_MAP = {
  // Full names / abbrevs
  'LAL': 'LAL', 'LAKERS': 'LAL', 'LA LAKERS': 'LAL', 'LOS ANGELES LAKERS': 'LAL',
  'GSW': 'GSW', 'WARRIORS': 'GSW', 'GOLDEN STATE': 'GSW', 'GOLDEN STATE WARRIORS': 'GSW',
  'BOS': 'BOS', 'CELTICS': 'BOS', 'BOSTON': 'BOS', 'BOSTON CELTICS': 'BOS',
  'MIA': 'MIA', 'HEAT': 'MIA', 'MIAMI': 'MIA', 'MIAMI HEAT': 'MIA',
  'CHI': 'CHI', 'BULLS': 'CHI', 'CHICAGO': 'CHI', 'CHICAGO BULLS': 'CHI',
  'NYK': 'NYK', 'KNICKS': 'NYK', 'NEW YORK': 'NYK', 'NEW YORK KNICKS': 'NYK',
  'SAS': 'SAS', 'SPURS': 'SAS', 'SAN ANTONIO': 'SAS', 'SAN ANTONIO SPURS': 'SAS',
  'BKN': 'BKN', 'NETS': 'BKN', 'BROOKLYN': 'BKN', 'BROOKLYN NETS': 'BKN',
  'PHI': 'PHI', '76ERS': 'PHI', 'SIXERS': 'PHI', 'PHILADELPHIA': 'PHI', 'PHILADELPHIA 76ERS': 'PHI',
  'LAC': 'LAC', 'CLIPPERS': 'LAC', 'LA CLIPPERS': 'LAC', 'LOS ANGELES CLIPPERS': 'LAC',
  'OKC': 'OKC', 'THUNDER': 'OKC', 'OKLAHOMA CITY': 'OKC', 'OKLAHOMA CITY THUNDER': 'OKC',
  'DEN': 'DEN', 'NUGGETS': 'DEN', 'DENVER': 'DEN', 'DENVER NUGGETS': 'DEN',
  'MIN': 'MIN', 'TIMBERWOLVES': 'MIN', 'MINNESOTA': 'MIN', 'WOLVES': 'MIN', 'MINNESOTA TIMBERWOLVES': 'MIN',
  'PHX': 'PHX', 'SUNS': 'PHX', 'PHOENIX': 'PHX', 'PHOENIX SUNS': 'PHX',
  'DAL': 'DAL', 'MAVERICKS': 'DAL', 'DALLAS': 'DAL', 'MAVS': 'DAL', 'DALLAS MAVERICKS': 'DAL',
  'HOU': 'HOU', 'ROCKETS': 'HOU', 'HOUSTON': 'HOU', 'HOUSTON ROCKETS': 'HOU',
  'MEM': 'MEM', 'GRIZZLIES': 'MEM', 'MEMPHIS': 'MEM', 'MEMPHIS GRIZZLIES': 'MEM',
  'NOP': 'NOP', 'PELICANS': 'NOP', 'NEW ORLEANS': 'NOP', 'NEW ORLEANS PELICANS': 'NOP',
  'ORL': 'ORL', 'MAGIC': 'ORL', 'ORLANDO': 'ORL', 'ORLANDO MAGIC': 'ORL',
  'TOR': 'TOR', 'RAPTORS': 'TOR', 'TORONTO': 'TOR', 'TORONTO RAPTORS': 'TOR',
  'CLE': 'CLE', 'CAVALIERS': 'CLE', 'CLEVELAND': 'CLE', 'CAVS': 'CLE', 'CLEVELAND CAVALIERS': 'CLE',
  'DET': 'DET', 'PISTONS': 'DET', 'DETROIT': 'DET', 'DETROIT PISTONS': 'DET',
  'IND': 'IND', 'PACERS': 'IND', 'INDIANA': 'IND', 'INDIANA PACERS': 'IND',
  'MIL': 'MIL', 'BUCKS': 'MIL', 'MILWAUKEE': 'MIL', 'MILWAUKEE BUCKS': 'MIL',
  'ATL': 'ATL', 'HAWKS': 'ATL', 'ATLANTA': 'ATL', 'ATLANTA HAWKS': 'ATL',
  'CHA': 'CHA', 'HORNETS': 'CHA', 'CHARLOTTE': 'CHA', 'CHARLOTTE HORNETS': 'CHA',
  'WAS': 'WAS', 'WIZARDS': 'WAS', 'WASHINGTON': 'WAS', 'WASHINGTON WIZARDS': 'WAS',
  'SAC': 'SAC', 'KINGS': 'SAC', 'SACRAMENTO': 'SAC', 'SACRAMENTO KINGS': 'SAC',
  'UTA': 'UTA', 'JAZZ': 'UTA', 'UTAH': 'UTA', 'UTAH JAZZ': 'UTA',
  'POR': 'POR', 'TRAIL BLAZERS': 'POR', 'PORTLAND': 'POR', 'BLAZERS': 'POR', 'PORTLAND TRAIL BLAZERS': 'POR',
};

function resolveTeam(input) {
  if (!input) return null;
  return TEAM_MAP[input.trim().toUpperCase()] || null;
}

// ─── Browser management ───────────────────────────────────────────────────────

let browser = null;

async function getBrowser() {
  if (!browser || !browser.isConnected()) {
    console.log('Launching Puppeteer browser...');
    browser = await puppeteer.launch({
      headless: 'new',
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
      defaultViewport: { width: 1440, height: 900 },
    });
    browser.on('disconnected', () => {
      console.warn('Browser disconnected — will relaunch on next request.');
      browser = null;
    });
  }
  return browser;
}

// ─── Screenshot helper ────────────────────────────────────────────────────────

/**
 * Navigate to url, wait for optional selector, take full-page screenshot.
 * Returns a Buffer (PNG).
 */
async function screenshotPage(url, waitForSelector) {
  const b = await getBrowser();
  const page = await b.newPage();
  try {
    await page.setViewport({ width: 1440, height: 900 });
    await page.setUserAgent(
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    );

    console.log(`Navigating to: ${url}`);
    await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });

    // Extra wait for SPA rendering
    await page.waitForTimeout(3500);

    if (waitForSelector) {
      await page.waitForSelector(waitForSelector, { timeout: 15000 }).catch(() => {
        console.warn(`Selector "${waitForSelector}" not found — proceeding anyway.`);
      });
    }

    const buffer = await page.screenshot({ type: 'png', fullPage: false });
    return buffer;
  } finally {
    await page.close();
  }
}

// ─── Arg parsing helpers ──────────────────────────────────────────────────────

/**
 * Check if a token looks like a team abbreviation (2-3 uppercase letters).
 * Validates against our TEAM_MAP.
 */
function extractTeamFromTokens(tokens) {
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i].toUpperCase();
    if (/^[A-Z]{2,3}$/.test(token) && TEAM_MAP[token]) {
      return { team: TEAM_MAP[token], index: i };
    }
  }
  return { team: null, index: -1 };
}

/**
 * Parse !onoff args: <player> [TEAM] [year]
 * Team abbrev can appear anywhere after the player name.
 */
function parseOnOffArgs(args) {
  const tokens = args.trim().split(/\s+/);

  // Extract year (last token if it looks like a year)
  let seasonInput = null;
  let remaining = [...tokens];
  const last = remaining[remaining.length - 1];
  if (/^\d{4}(-\d{2,4})?$/.test(last)) {
    seasonInput = last;
    remaining = remaining.slice(0, -1);
  }

  // Extract team abbreviation from remaining tokens
  const { team, index } = extractTeamFromTokens(remaining);
  if (index !== -1) {
    remaining.splice(index, 1);
  }

  const playerName = remaining.join(' ');
  return { playerName, team, ...parseSeason(seasonInput) };
}

// ─── Discord client ───────────────────────────────────────────────────────────

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
  ],
});

client.once('clientReady', async () => {
  console.log(`Bot online: ${client.user.tag}`);
  client.user.setActivity('databallr.com | !stats help', { type: ActivityType.Watching });

  // Pre-warm the browser
  try {
    await getBrowser();
    console.log('Browser pre-warmed.');
  } catch (e) {
    console.warn('Browser pre-warm failed:', e.message);
  }
});

// ─── Command handler ──────────────────────────────────────────────────────────

client.on('messageCreate', async (message) => {
  if (message.author.bot || !message.guild) return;

  const content = message.content.trim();
  if (!content.startsWith('!')) return;

  const spaceIdx = content.indexOf(' ');
  const command = (spaceIdx === -1 ? content.slice(1) : content.slice(1, spaceIdx)).toLowerCase();
  const args = spaceIdx === -1 ? '' : content.slice(spaceIdx + 1).trim();

  // ── Help ──────────────────────────────────────────────────────────────────
  if (command === 'stats' && args.toLowerCase() === 'help') {
    return message.reply(
      '**Commands:**\n' +
      '`!stats <player> [year]` — Player season averages (game log page)\n' +
      '`!lastx <player> [n] [year]` — Last N games (default 5)\n' +
      '`!onoff <player> [TEAM] [year]` — On/Off splits\n' +
      '`!wowy <player1> | <player2> [year]` — WOWY two-player splits\n' +
      '`!team <team> [year]` — Full team WOWY page\n\n' +
      '**Year format:** `2025` or `2024-25` (end year of season). Defaults to 2024-25.\n' +
      '**Examples:**\n' +
      '`!stats LeBron James`\n' +
      '`!onoff Nikola Jokic 2024`\n' +
      '`!wowy LeBron James | Anthony Davis 2025`\n' +
      '`!team Lakers 2025`'
    );
  }

  // ── !stats <player> [year] ────────────────────────────────────────────────
  if (command === 'stats') {
    await message.channel.sendTyping();
    const { playerName, season } = parsePlayerArgs(args);
    if (!playerName) {
      return message.reply('Usage: `!stats <player> [year]`\nExample: `!stats LeBron James`');
    }

    const player = findPlayer(playerName);
    if (!player) {
      return message.reply(`Player **${playerName}** not found. Check spelling.`);
    }

    const url = `https://databallr.com/last-games/${player.id}/${player.slug}`;
    try {
      const buffer = await screenshotPage(url, null);
      return message.channel.send({
        content: `**${player.full_name}** | Season averages (2024-25 current)`,
        files: [{ attachment: buffer, name: 'stats.png' }],
      });
    } catch (err) {
      console.error('!stats error:', err.message);
      return message.reply('Failed to load databallr.com. Try again.');
    }
  }

  // ── !lastx <player> [n] [year] ────────────────────────────────────────────
  if (command === 'lastx') {
    await message.channel.sendTyping();

    // Parse: tokens may contain a number (n) anywhere
    const tokens = args.trim().split(/\s+/);
    let n = 5;
    let seasonInput = null;
    const remaining = [];

    for (const tok of tokens) {
      if (/^\d{1,2}$/.test(tok) && parseInt(tok) >= 1 && parseInt(tok) <= 82) {
        n = parseInt(tok);
      } else if (/^\d{4}(-\d{2,4})?$/.test(tok)) {
        seasonInput = tok;
      } else {
        remaining.push(tok);
      }
    }

    const playerName = remaining.join(' ');
    if (!playerName) {
      return message.reply('Usage: `!lastx <player> [n] [year]`\nExample: `!lastx LeBron James 10`');
    }

    const { season } = parseSeason(seasonInput);
    const player = findPlayer(playerName);
    if (!player) {
      return message.reply(`Player **${playerName}** not found. Check spelling.`);
    }

    const url = `https://databallr.com/last-games/${player.id}/${player.slug}`;
    try {
      const buffer = await screenshotPage(url, null);
      return message.channel.send({
        content: `**${player.full_name}** | Last ${n} games`,
        files: [{ attachment: buffer, name: 'lastx.png' }],
      });
    } catch (err) {
      console.error('!lastx error:', err.message);
      return message.reply('Failed to load databallr.com. Try again.');
    }
  }

  // ── !onoff <player> [TEAM] [year] ─────────────────────────────────────────
  if (command === 'onoff') {
    await message.channel.sendTyping();

    if (!args.trim()) {
      return message.reply('Usage: `!onoff <player> [TEAM] [year]`\nExample: `!onoff LeBron James LAL 2025`');
    }

    const { playerName, team: explicitTeam, season, year } = parseOnOffArgs(args);

    if (!playerName) {
      return message.reply('Usage: `!onoff <player> [TEAM] [year]`');
    }

    const player = findPlayer(playerName);
    if (!player) {
      return message.reply(`Player **${playerName}** not found. Check spelling.`);
    }

    // Resolve team: explicit arg > players.json lookup
    let teamAbbr = explicitTeam || (player.team ? resolveTeam(player.team) || player.team : null);

    if (!teamAbbr) {
      return message.reply(
        `Couldn't auto-detect team for **${player.full_name}**. ` +
        `Try: \`!onoff ${player.full_name} LAL ${year}\` (replace LAL with the correct team)`
      );
    }

    const url = `https://databallr.com/wowy/${teamAbbr}/${year}/${year}/regular/high/wowy/${player.id}`;
    try {
      const buffer = await screenshotPage(url, null);
      return message.channel.send({
        content: `**${player.full_name}** | On/Off | ${season} | ${teamAbbr}`,
        files: [{ attachment: buffer, name: 'onoff.png' }],
      });
    } catch (err) {
      console.error('!onoff error:', err.message);
      return message.reply('Failed to load databallr.com. Try again.');
    }
  }

  // ── !wowy <player1> | <player2> [year] ───────────────────────────────────
  if (command === 'wowy') {
    await message.channel.sendTyping();

    if (!args.trim()) {
      return message.reply('Usage: `!wowy <player1> | <player2> [year]`\nExample: `!wowy LeBron James | Anthony Davis 2025`');
    }

    // Check if a pipe is present
    if (!args.includes('|')) {
      return message.reply('Usage: `!wowy <player1> | <player2> [year]`\nSeparate players with `|`');
    }

    const { player1Name, player2Name, season, year } = parseWowyArgs(args);

    if (!player1Name) {
      return message.reply('Usage: `!wowy <player1> | <player2> [year]`');
    }

    const player1 = findPlayer(player1Name);
    if (!player1) {
      return message.reply(`Player **${player1Name}** not found. Check spelling.`);
    }

    // Team from player1's record
    let teamAbbr = player1.team || null;

    // Check if user snuck a team abbrev into the args
    const allTokens = args.split(/[\s|]+/);
    const { team: explicitTeam } = extractTeamFromTokens(allTokens);
    if (explicitTeam) teamAbbr = explicitTeam;

    if (!teamAbbr) {
      return message.reply(
        `Couldn't auto-detect team for **${player1.full_name}**. ` +
        `Try adding the team abbreviation, e.g.: \`!wowy ${player1.full_name} LAL | ${player2Name} ${year}\``
      );
    }

    let url;
    if (player2Name) {
      const player2 = findPlayer(player2Name);
      if (!player2) {
        return message.reply(`Player **${player2Name}** not found. Check spelling.`);
      }
      url = `https://databallr.com/wowy/${teamAbbr}/${year}/${year}/regular/high/wowy/${player1.id}/${player2.id}`;
      try {
        const buffer = await screenshotPage(url, null);
        return message.channel.send({
          content: `**${player1.full_name}** + **${player2.full_name}** | WOWY | ${season} | ${teamAbbr}`,
          files: [{ attachment: buffer, name: 'wowy.png' }],
        });
      } catch (err) {
        console.error('!wowy error:', err.message);
        return message.reply('Failed to load databallr.com. Try again.');
      }
    } else {
      // No second player — treat as on/off for player1
      url = `https://databallr.com/wowy/${teamAbbr}/${year}/${year}/regular/high/wowy/${player1.id}`;
      try {
        const buffer = await screenshotPage(url, null);
        return message.channel.send({
          content: `**${player1.full_name}** | On/Off | ${season} | ${teamAbbr}`,
          files: [{ attachment: buffer, name: 'wowy.png' }],
        });
      } catch (err) {
        console.error('!wowy error:', err.message);
        return message.reply('Failed to load databallr.com. Try again.');
      }
    }
  }

  // ── !team <team> [year] ───────────────────────────────────────────────────
  if (command === 'team') {
    await message.channel.sendTyping();

    if (!args.trim()) {
      return message.reply('Usage: `!team <team> [year]`\nExample: `!team Lakers 2025`\nExample: `!team GSW 2016`');
    }

    const tokens = args.trim().split(/\s+/);
    let seasonInput = null;
    const remaining = [];

    for (const tok of tokens) {
      if (/^\d{4}(-\d{2,4})?$/.test(tok)) {
        seasonInput = tok;
      } else {
        remaining.push(tok);
      }
    }

    const teamInput = remaining.join(' ');
    const { season, year } = parseSeason(seasonInput);
    const teamAbbr = resolveTeam(teamInput);

    if (!teamAbbr) {
      return message.reply(
        `Team **${teamInput}** not recognized. Use an abbreviation (e.g. \`LAL\`, \`GSW\`) ` +
        `or a common name (e.g. \`Lakers\`, \`Warriors\`).`
      );
    }

    const url = `https://databallr.com/wowy/${teamAbbr}/${year}/${year}/regular/high/wowy`;
    try {
      const buffer = await screenshotPage(url, null);
      return message.channel.send({
        content: `**${teamAbbr}** | Team WOWY | ${season}`,
        files: [{ attachment: buffer, name: 'team.png' }],
      });
    } catch (err) {
      console.error('!team error:', err.message);
      return message.reply('Failed to load databallr.com. Try again.');
    }
  }
});

// ─── Login ────────────────────────────────────────────────────────────────────

const token = process.env.DISCORD_TOKEN;
if (!token) {
  console.error('ERROR: DISCORD_TOKEN is not set in .env');
  process.exit(1);
}

client.login(token).catch(err => {
  console.error('Login failed:', err.message);
  process.exit(1);
});
