// Source-advertised seasons and cache freshness for an ongoing NBA season.
function normalizeSeasons(values) {
  return [...new Set(values.filter(value => {
    const m = /^(\d{4})-(\d{2})$/.exec(value);
    return m && Number(m[1]) >= 1996 && String(Number(m[1]) + 1).slice(-2) === m[2];
  }))].sort((a, b) => Number(b.slice(0, 4)) - Number(a.slice(0, 4)));
}

function selectSeasons(values, startYear, endYear = startYear) {
  const seasons = normalizeSeasons(values);
  if (!seasons.length) throw new Error('Could not read available seasons from the shotmap source. Please try again later.');
  function season(year) { return `${year - 1}-${String(year).slice(-2)}`; }
  const start = seasons.indexOf(season(Number(startYear)));
  const end = seasons.indexOf(season(Number(endYear)));
  const first = Number(seasons[seasons.length - 1].slice(0, 4)) + 1;
  const last = Number(seasons[0].slice(0, 4)) + 1;
  if (start < 0 || end < 0) {
    const unavailable = start < 0 ? season(Number(startYear)) : season(Number(endYear));
    throw new Error(`Season ${unavailable} is not available from the source. Available ending years: ${first}-${last}.`);
  }
  return seasons.slice(Math.min(start, end), Math.max(start, end) + 1);
}

function canCache(startYear, endYear, now = new Date()) {
  const currentEnd = now.getUTCFullYear() + (now.getUTCMonth() >= 9 ? 1 : 0);
  return Math.max(Number(startYear), Number(endYear || startYear)) < currentEnd;
}

module.exports = { normalizeSeasons, selectSeasons, canCache };
