'use strict';

const players = require('./players.json');

// ─── Normalization ─────────────────────────────────────────────────────────────

/**
 * Normalize a string for fuzzy comparison:
 * - NFD decompose to strip accents
 * - lowercase
 * - remove apostrophes, dots
 * - collapse whitespace
 */
function normalize(str) {
  if (!str) return '';
  return str
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')  // strip combining diacriticals
    .toLowerCase()
    .replace(/['.]/g, '')             // remove apostrophes and dots
    .replace(/\s+/g, ' ')
    .trim();
}

// Pre-compute normalized names once
const normalizedPlayers = players.map(p => ({
  ...p,
  _norm: normalize(p.full_name),
}));

// ─── findPlayer ────────────────────────────────────────────────────────────────

/**
 * Fuzzy player search by name.
 * 1. Exact match (case-insensitive, normalized)
 * 2. Partial match (query is substring of name, or name is substring of query)
 * Returns {id, full_name, slug, team, is_active} or null.
 */
function findPlayer(name) {
  if (!name || !name.trim()) return null;
  const query = normalize(name);
  if (!query) return null;

  // 1. Exact match
  const exact = normalizedPlayers.find(p => p._norm === query);
  if (exact) return { id: exact.id, full_name: exact.full_name, slug: exact.slug, team: exact.team, is_active: exact.is_active };

  // 2. Partial match — prefer active players
  const partials = normalizedPlayers.filter(p =>
    p._norm.includes(query) || query.includes(p._norm)
  );

  if (partials.length === 0) return null;

  // Prefer active players, then sort by name length (shorter = closer match)
  partials.sort((a, b) => {
    if (a.is_active !== b.is_active) return a.is_active ? -1 : 1;
    return a._norm.length - b._norm.length;
  });

  const best = partials[0];
  return { id: best.id, full_name: best.full_name, slug: best.slug, team: best.team, is_active: best.is_active };
}

// ─── parseSeason ───────────────────────────────────────────────────────────────

/**
 * Parse a season from user input.
 * "2025"     → {season: "2024-25", year: 2025}
 * "2016-17"  → {season: "2016-17", year: 2017}
 * "2017"     → {season: "2016-17", year: 2017}
 * null/empty → {season: "2024-25", year: 2025}
 */
function parseSeason(input) {
  const DEFAULT = { season: '2024-25', year: 2025 };

  if (!input || !input.toString().trim()) return DEFAULT;

  const s = input.toString().trim();

  // Format: "2016-17" or "2016-2017"
  const dashMatch = s.match(/^(\d{4})-(\d{2,4})$/);
  if (dashMatch) {
    const startYear = parseInt(dashMatch[1]);
    let endSuffix = dashMatch[2];
    let endYear;
    if (endSuffix.length === 2) {
      // e.g. "2016-17" → end year is 2017
      endYear = Math.floor(startYear / 100) * 100 + parseInt(endSuffix);
      // Handle century boundary (e.g. 1999-00 → 2000)
      if (endYear <= startYear) endYear += 100;
    } else {
      endYear = parseInt(endSuffix);
    }
    const season = `${startYear}-${String(endYear).slice(-2).padStart(2, '0')}`;
    return { season, year: endYear };
  }

  // Format: bare 4-digit year, treated as the END year of the season
  const yearMatch = s.match(/^(\d{4})$/);
  if (yearMatch) {
    const endYear = parseInt(yearMatch[1]);
    const startYear = endYear - 1;
    const season = `${startYear}-${String(endYear).slice(-2).padStart(2, '0')}`;
    return { season, year: endYear };
  }

  return DEFAULT;
}

// ─── parsePlayerArgs ──────────────────────────────────────────────────────────

/**
 * Parse args for commands like: !stats <player> [year]
 * The last token is treated as a year if it matches 4 digits or "XXXX-XX".
 * Returns {playerName, season, year}
 */
function parsePlayerArgs(args) {
  if (!args || !args.trim()) {
    return { playerName: '', ...parseSeason(null) };
  }

  const tokens = args.trim().split(/\s+/);
  const last = tokens[tokens.length - 1];
  const isYear = /^\d{4}(-\d{2,4})?$/.test(last);

  let playerName, seasonInput;
  if (isYear && tokens.length > 1) {
    seasonInput = last;
    playerName = tokens.slice(0, -1).join(' ');
  } else {
    seasonInput = null;
    playerName = tokens.join(' ');
  }

  return { playerName, ...parseSeason(seasonInput) };
}

// ─── parseWowyArgs ────────────────────────────────────────────────────────────

/**
 * Parse args for WOWY: "LeBron James | Anthony Davis 2020"
 * Split on "|", strip each side, extract year from either side.
 * Returns {player1Name, player2Name, season, year}
 */
function parseWowyArgs(args) {
  if (!args || !args.trim()) {
    return { player1Name: '', player2Name: '', ...parseSeason(null) };
  }

  const parts = args.split('|');
  if (parts.length < 2) {
    // No pipe — treat entire thing as player1, no player2
    const { playerName, season, year } = parsePlayerArgs(args);
    return { player1Name: playerName, player2Name: '', season, year };
  }

  let left = parts[0].trim();
  let right = parts.slice(1).join('|').trim();

  // Try to extract year from the right side first, then left
  let seasonInput = null;

  // Check right side last token for year
  const rightTokens = right.split(/\s+/);
  const rightLast = rightTokens[rightTokens.length - 1];
  if (/^\d{4}(-\d{2,4})?$/.test(rightLast) && rightTokens.length > 1) {
    seasonInput = rightLast;
    right = rightTokens.slice(0, -1).join(' ');
  } else if (/^\d{4}(-\d{2,4})?$/.test(rightLast) && rightTokens.length === 1) {
    // Entire right side is a year — no player2
    seasonInput = rightLast;
    right = '';
  }

  // If no year found on right, check left side
  if (!seasonInput) {
    const leftTokens = left.split(/\s+/);
    const leftLast = leftTokens[leftTokens.length - 1];
    if (/^\d{4}(-\d{2,4})?$/.test(leftLast) && leftTokens.length > 1) {
      seasonInput = leftLast;
      left = leftTokens.slice(0, -1).join(' ');
    }
  }

  return {
    player1Name: left.trim(),
    player2Name: right.trim(),
    ...parseSeason(seasonInput),
  };
}

module.exports = { findPlayer, parseSeason, parsePlayerArgs, parseWowyArgs };
