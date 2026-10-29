const months = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
function parseDate(input, now = new Date()) {
  let date = input;
  const short = input.match(/^(\d{1,2})\/([a-z]{3})(?:\/(\d{4}))?$/i);
  if (short) {
    const month = months.indexOf(short[2].toLowerCase()) + 1;
    if (!month) throw new Error('Use a month such as Jan, Feb, or Dec.');
    const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(now);
    let year = short[3] ? Number(short[3]) : Number(today.slice(0, 4));
    const suffix = `${String(month).padStart(2, '0')}-${short[1].padStart(2, '0')}`;
    if (!short[3] && `${year}-${suffix}` > today) year--;
    date = `${year}-${suffix}`;
  }
  const parsed = new Date(`${date}T12:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== date) throw new Error('Enter a valid game date: 26/Dec, 26/Dec/2025, or YYYY-MM-DD.');
  return date;
}
function parse(raw, now = new Date()) {
  const tokens = raw.trim().split(/\s+/);
  const command = tokens.shift().toLowerCase();
  const args = tokens.join(' ');
  if (['nbahelp', 'unwatch'].includes(command)) return { command, args };
  if (command === 'recent') {
    if (!args) throw new Error('Include a player name. Example: !recent LeBron James');
    return { command, query: args };
  }
  if (!['nba', 'watch', 'season'].includes(command)) return null;
  if (command === 'nba' && ['injury', 'injuries'].includes(tokens[0]?.toLowerCase())) {
    return { command: 'injury', query: tokens.slice(1).join(' ') };
  }
  if (command === 'nba' && ['pick', 'picks'].includes(tokens[0]?.toLowerCase())) {
    const query = tokens.slice(1).join(' ');
    if (!query) throw new Error('Include a team. Examples: !nba picks Lakers or !nba pick :lakers:');
    return { command: 'picks', query };
  }
  if (command === 'nba' && ['boxscore', 'boxscores'].includes(tokens[0]?.toLowerCase())) {
    if (tokens.length > 2) throw new Error('Use !nba boxscores or !nba boxscores YYYY-MM-DD.');
    return { command: 'boxscores', date: tokens[1] ? parseDate(tokens[1], now) : undefined };
  }
  if (command === 'nba' && tokens[0]?.toLowerCase() === 'standings') {
    if (tokens.length !== 1) throw new Error('Use !nba standings, then the West / East buttons to switch conference.');
    return { command: 'standings' };
  }
  if (command === 'nba' && tokens[0]?.toLowerCase() === 'compare') {
    const query = tokens.slice(1).join(' ');
    if (!query) throw new Error('Name two players, e.g. !nba compare LeBron James vs Stephen Curry');
    return { command: 'compare', query };
  }
  if (command === 'nba' && tokens[0]?.toLowerCase() === 'salary') {
    const query = tokens.slice(1).join(' ');
    if (!query) throw new Error('Include a team. Example: !nba salary Lakers');
    return { command: 'salary', query };
  }
  if (command === 'nba' && tokens[0]?.toLowerCase() === 'schedule') {
    const query = tokens.slice(1).join(' ');
    if (!query) throw new Error('Include a team. Example: !nba schedule Lakers');
    return { command: 'schedule', query };
  }
  if (command === 'nba' && tokens[0]?.toLowerCase() === 'games') {
    if (tokens.length !== 1) throw new Error('Use !nba games to see the current night’s live and upcoming games.');
    return { command: 'games' };
  }
  let date, year, playoffs = false;
  if (command === 'season') {
    if (/\s+playoffs$/i.test(` ${tokens.join(' ')}`)) { playoffs = true; tokens.pop(); }
    if (/^\d{4}(?:-\d{2})?$/.test(tokens.at(-1) || '')) {
      const input = tokens.pop();
      year = input.includes('-') ? Number(input.slice(0, 4)) + 1 : Number(input);
      if (year < 2003 || year > 2100 || (input.includes('-') && input.slice(-2) !== String(year).slice(-2))) throw new Error('Use a season such as 2025-26, or its ending year 2026.');
    }
  } else if (/^\d{4}-\d{2}-\d{2}$/.test(tokens.at(-1) || '') || /^\d{1,2}\//.test(tokens.at(-1) || '')) {
    date = parseDate(tokens.pop(), now);
  }
  const query = tokens.join(' ');
  if (!query) throw new Error('Include a player name. Example: !nba Paul George');
  return { command, query, date, year, playoffs };
}
module.exports = { parse, parseDate };
