'use strict';

const $ = (id) => document.getElementById(id);

const PLAYER_COLUMNS = [
  { key: 'name', label: 'Player', text: true },
  { key: 'current_price', label: 'Current', money: true },
  { key: 'votes', label: 'Price votes' },
  { key: 'dont_know', label: "Don't know" },
  { key: 'median', label: 'Median', money: true },
  { key: 'q1', label: 'Middle 50%', range: true },
  { key: 'average', label: 'Average', money: true },
  { key: 'low', label: 'Low', money: true },
  { key: 'high', label: 'High', money: true },
  { key: 'gap', label: 'Median minus current', gap: true },
];
const VOTER_COLUMNS = [
  { key: 'username', label: 'Username', text: true },
  { key: 'answered', label: 'Answered' },
  { key: 'votes_in_results', label: 'In results' },
  { key: 'dont_know', label: "Don't know" },
  { key: 'flag_count', label: 'Flags', left: true },
  { key: 'last_seen_at', label: 'Last seen', text: true },
];

const state = {
  data: null,
  voters: [],
  playerSort: { key: 'current_price', dir: -1 },
  voterSort: { key: 'flag_count', dir: -1 },
  charts: {},
  pendingDelete: null,
  currentVoter: null,
  currentPlayer: null,
  tab: 'overview',
  loading: false,
  exporting: false,
};

class ApiFailure extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(method, path, body) {
  const options = { method, credentials: 'same-origin', headers: {} };
  if (method !== 'GET') {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body || {});
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    throw new ApiFailure(0, 'Connection lost.');
  }
  let data = {};
  try {
    data = await response.json();
  } catch (err) {
    data = {};
  }
  if (!response.ok) {
    if (response.status === 401 && !path.endsWith('/login')) {
      showLogin();
    }
    throw new ApiFailure(response.status, data.error || 'Something went wrong.');
  }
  return data;
}

function setText(element, text) {
  element.textContent = text || '';
  element.hidden = !text;
}

function setStatus(text) {
  setText($('status'), text);
}

function make(tag, props, children) {
  const element = document.createElement(tag);
  Object.assign(element, props || {});
  for (const child of children || []) {
    element.append(child);
  }
  return element;
}

const number = (value) => Number(value || 0).toLocaleString();
const money = (value) => (value === null || value === undefined ? '—' : `$${Number(value).toLocaleString(undefined, {maximumFractionDigits: 2})}`);
const signedMoney = (value) => {
  if (value === null || value === undefined) {
    return '';
  }
  return value > 0 ? `+$${value}` : value < 0 ? `-$${Math.abs(value)}` : '$0';
};
const timeText = (iso) => (iso ? new Date(iso).toLocaleString() : '');
const relativeTime = (iso) => {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60000));
  return minutes < 1 ? 'Just now' : minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ago` : `${Math.floor(minutes / 1440)}d ago`;
};
const initials = (name) => name.split(/\s+/).filter(Boolean).map(x => x[0]).slice(0, 2).join('').toUpperCase();
function progressBar(percent) {
  const fill = make('div', {className: 'progress-fill'});
  fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
  return make('div', {className: 'progress-track'}, [fill]);
}
function metric(value, label, meta, action) {
  const item = make(action ? 'button' : 'div', {className: `stat${action ? ' stat-button' : ''}`}, [
    make('div', {className: 'stat-label', textContent: label}),
    make('div', {className: 'stat-value', textContent: value}),
    make('div', {className: 'stat-meta', textContent: meta}),
  ]);
  if (action) { item.type = 'button'; item.addEventListener('click', action); }
  return item;
}

function chartTheme(chart) {
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(`--chart-${name}`).trim();
  for (const axis of Object.values(chart.options.scales || {})) {
    if (axis.ticks) axis.ticks.color = color('text');
    if (axis.title) axis.title.color = color('text');
    if (axis.grid) axis.grid.color = color('grid');
  }
  const datasets = chart.data.datasets;
  if (chart.canvas.id === 'chart-activity') {
    datasets[0].backgroundColor = color('orange');
    datasets[1].backgroundColor = color('neutral');
  } else if (chart.canvas.id === 'chart-overpriced' || chart.canvas.id === 'chart-underpriced') {
    datasets[0].backgroundColor = color(chart.canvas.id === 'chart-overpriced' ? 'negative' : 'positive');
  } else if (chart.canvas.id === 'chart-player') {
    datasets[0].backgroundColor = chart.data.labels.map((_, index) =>
      index === datasets[0].currentPriceIndex ? color('orange') : color('neutral'));
  }
  chart.update('none');
}

function updateThemeControls() {
  const dark = document.documentElement.dataset.theme === 'dark';
  document.querySelectorAll('.theme-toggle').forEach(button => {
    button.textContent = dark ? '☀ Light mode' : '☾ Dark mode';
    button.setAttribute('aria-label', `Switch to ${dark ? 'light' : 'dark'} mode`);
  });
  Object.values(state.charts).forEach(chartTheme);
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('tfm-admin-theme', next); } catch (_) {}
  updateThemeControls();
}
function emptyTable(columns, message) {
  return make('tr', {}, [make('td', {className: 'empty-state', colSpan: columns, textContent: message})]);
}
async function filterPlayers(value) {
  $('player-filter').value = value;
  $('player-search').value = '';
  state.playerSort = value==='lower' ? {key:'gap',dir:1} : value==='higher' ? {key:'gap',dir:-1} : value==='low'||value==='none' ? {key:'votes',dir:1} : {key:'current_price',dir:-1};
  await openTab('players');
}
async function filterVoters(value) {
  $('voter-filter').value = value;
  $('voter-search').value = '';
  await openTab('voters');
}
const answerText = (vote) => {
  if (vote.answer_type === 'dont_know') {
    return "Don't know him";
  }
  return vote.answer_type === 'other' ? `${money(vote.price)} (Other)` : money(vote.price);
};

/* Tabs */

function showTab(name) {
  state.tab = name;
  document.querySelectorAll('.tab-page').forEach((page) => {
    page.hidden = page.id !== `tab-${name}`;
  });
  const active = name === 'player' ? 'players' : name === 'voter' || name === 'voter-report' ? 'voters' : name;
  document.querySelectorAll('.tab').forEach((tab) => {
    const on = tab.dataset.tab === active;
    tab.classList.toggle('active', on);
    tab.setAttribute('aria-current', on ? 'page' : 'false');
  });
  $('section-name').textContent = {overview: 'Overview', combined:'Combined ADP', players: 'Player insights', player: 'Player details', voters: 'Voters & review', voter: 'Voter details', 'voter-report':'Voter report', deleted: 'Deleted answers'}[name];
  setStatus('');
}

async function openTab(name) {
  showTab(name);
  try {
    if (name === 'overview' || name === 'players' || name === 'combined') {
      await loadResults();
    } else if (name === 'voters') {
      await loadVoters();
    } else if (name === 'deleted') {
      await loadBatches();
    }
  } catch (err) {
    if (err.status !== 401) {
      setStatus(err.message);
    }
  }
}

/* Login */

function showLogin() {
  $('app').hidden = true;
  $('login').hidden = false;
  $('password').focus();
}

async function submitLogin(event) {
  event.preventDefault();
  setText($('login-error'), '');
  try {
    await api('POST', '/api/admin/login', { password: $('password').value });
    $('password').value = '';
    await enterApp();
  } catch (err) {
    setText($('login-error'), err.message);
  }
}

async function enterApp() {
  await loadResults();
  $('login').hidden = true;
  $('app').hidden = false;
  showTab('overview');
  renderOverview();
}

/* Results */

async function loadResults() {
  state.data = await api('GET', '/api/admin/players');
  $('last-refreshed').textContent = `Updated ${new Date(state.data.generated_at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`;
  renderOverview();
  renderPlayersTable();
  renderCombinedAdp();
}

function renderCombinedAdp() {
  if(!state.data?.combined_adp)return;
  const data=state.data.combined_adp, field=$('combined-method').value;
  const filter=$('combined-filter').value, query=$('combined-search').value.trim().toLocaleLowerCase();
  const rankField=`${field}_rank`;
  const rows=[...data.players].sort((a,b)=>a[rankField]===null||b[rankField]===null ? a[rankField]===b[rankField]?a.name.localeCompare(b.name):a[rankField]===null?1:-1 : a[rankField]-b[rankField]);
  const visible=rows.filter(row=>row.name.toLocaleLowerCase().includes(query) && (
    filter==='all'||filter==='qualified'&&row.votes>=5||filter==='low'&&row.votes>0&&row.votes<5||filter==='none'&&row.votes===0||
    row[field]!==null&&(filter==='higher'&&row[field]>row.current_price||filter==='lower'&&row[field]<row.current_price||filter==='same'&&row[field]===row.current_price)));
  $('combined-metrics').replaceChildren(metric(number(data.contributors),'Contributing sessions','At least one included price vote'),
    metric(number(data.price_votes),'Price votes','Includes $0; excludes Don’t know'),
    metric(`${data.priced_players} / ${data.players.length}`,'Players priced','Unpriced players remain unranked'),
    metric(field==='median'?'Median':'Average','Ranking method',field==='median'?'The middle submitted price':'Mean of submitted prices'));
  $('combined-count').textContent=`${visible.length} of ${rows.length} players shown · ${field==='median'?'Median':'Average'} price, highest first. Equal prices sort by name. Rank uses the full list, not the current filter.`;
  const body=make('tbody',{},visible.map(row=>{
    const difference=row[field]===null?null:Math.round((row[field]-row.current_price)*100)/100;
    const name=make('button',{type:'button',className:'link',textContent:row.name});name.onclick=()=>openPlayer(row.id);
    return make('tr',{},[
      make('td',{className:'num',textContent:row[`${field}_rank`]||'—'}),make('td',{},[name]),
      make('td',{className:'num',textContent:money(row.median)}),make('td',{className:'num',textContent:money(row.average)}),
      make('td',{className:'num',textContent:money(row.current_price)}),
      make('td',{className:`num ${difference>0?'gap-up':difference<0?'gap-down':''}`,textContent:difference===null?'—':signedMoney(difference)}),
      make('td',{className:'num',textContent:String(row.votes)}),make('td',{textContent:row.votes===0?'No price votes':row.votes<5?'Early: fewer than 5':'5+ price votes'})]);
  }));
  if(!visible.length)body.append(emptyTable(8,'No players match these filters.'));
  $('combined-table').replaceChildren(make('thead',{},[make('tr',{},['Rank','Player','Median price','Average price','Current','Difference','Price votes','Coverage'].map((label,i)=>make('th',{className:i===1||i===7?'':'num',textContent:label,title:label==='Difference'?`${field} minus current ADP price`:''})))]),body);
}

function renderOverview() {
  const data = state.data;
  if (!data) {
    return;
  }
  const open = data.voting_open;
  $('voting-state').textContent = open ? 'Voting is open' : 'Voting is closed';
  $('voting-state').classList.toggle('closed', !open);
  $('toggle-voting').textContent = open ? 'Close voting' : 'Open voting';
  $('export-sheets').disabled = !data.sheets_ready || state.exporting;
  $('export-sheets').title = data.sheets_ready ? '' : 'Set EXPORT_SPREADSHEET_ID and the service account first.';

  const o = data.overview, d = data.dashboard;
  $('survey-scope').textContent = `${number(o.total_players)} players · $100 team budget · Starting prices before Flux`;
  $('stats').replaceChildren(
    metric(number(o.total_voters), 'Voter sessions', `${number(d.active_voters_24h)} active in the last 24h`, () => filterVoters('all')),
    metric(number(o.total_votes), 'Answers included', `${number(o.price_votes)} prices · ${number(o.dont_know_votes)} don't know`),
    metric(`${d.average_completion}%`, 'Average voter progress', `${number(o.finished_voters)} finished · ${number(d.in_progress_voters)} in progress`, () => filterVoters('progress')),
    metric(`${number(d.qualified_players)} / ${number(o.total_players)}`, 'Ready to compare', `Players with ${d.minimum_price_votes}+ price votes`, () => filterPlayers('qualified')),
    metric(number(d.flagged_voters), 'Voters needing review', 'Flags are suggestions, not verdicts', () => filterVoters('flagged')),
    metric(number(d.excluded_answers), 'Answers removed', 'Excluded from all pricing results', () => openTab('deleted')),
  );
  $('review-badge').textContent = d.flagged_voters;
  $('review-badge').hidden = !d.flagged_voters;
  $('completion-headline').textContent = `${o.finished_voters} finished`;
  $('completion-note').textContent = `Progress includes answers later removed from results. ${number(d.not_started_voters)} sessions have not started.`;
  $('completion-bars').replaceChildren(...d.completion.map(row => make('div', {className: 'completion-row'}, [
    make('span', {textContent: row.label}), progressBar(o.total_voters ? row.count / o.total_voters * 100 : 0), make('b', {textContent: number(row.count)}),
  ])));
  const attention = [
    ['◎', d.flagged_voters?`${number(d.flagged_voters)} ${d.flagged_voters===1?'voter needs':'voters need'} review`:'No voters flagged', 'Inspect unusual patterns and the answers behind them.', () => filterVoters('flagged')],
    ['◫', d.low_coverage_players?`${number(d.low_coverage_players)} ${d.low_coverage_players===1?'player needs':'players need'} more votes`:'Every player has 5+ price votes', `${number(d.unpriced_players)} ${d.unpriced_players===1?'has':'have'} no price votes. Every price needs enough voices.`, () => filterPlayers('low')],
    ['↗', `${number(d.in_progress_voters)} ${d.in_progress_voters===1?'survey':'surveys'} still in progress`, 'See who has started and how far they have reached.', () => filterVoters('progress')],
  ];
  $('attention').replaceChildren(...attention.map(([icon, title, detail, action]) => {
    const button = make('button', {className: 'attention-card', type: 'button'}, [
      make('span', {className: 'attention-icon', textContent: icon}),
      make('div', {}, [make('b', {textContent: title}), make('p', {textContent: detail})]),
      make('span', {className: 'arrow', textContent: '→'}),
    ]); button.addEventListener('click', action); return button;
  }));
  $('gap-explainer').textContent = `Median vote compared with the current price. Up to 8 players per chart; at least ${d.minimum_price_votes} price votes required. These are opinions, not new prices.`;
  drawGapChart('overpriced', data.gaps.overpriced.slice(0, 8), '#d59a8e');
  drawGapChart('underpriced', data.gaps.underpriced.slice(0, 8), '#7aac98');
  drawChart('chart-activity', {
    type: 'bar', data: {labels: d.activity.map(day => new Date(`${day.date}T12:00:00Z`).toLocaleDateString(undefined, {month:'short', day:'numeric', timeZone:'UTC'})), datasets: [
      {label:'Price votes', data: d.activity.map(day => day.price_votes), backgroundColor:'#e9ad82', borderRadius:4, maxBarThickness:27},
      {label:"Don't know", data:d.activity.map(day => day.dont_know), backgroundColor:'#dce2e9', borderRadius:4, maxBarThickness:27},
    ]}, options: {maintainAspectRatio:false, plugins:{legend:{display:false}}, scales:{x:{stacked:true,grid:{display:false},ticks:{color:'#87929c',font:{size:10}}},y:{stacked:true,beginAtZero:true,grid:{color:'#f0f2f5'},border:{display:false},ticks:{color:'#9ca4ae',precision:0,font:{size:10}}}}},
  });
  $('fewest').replaceChildren(...data.fewest_votes.slice(0, 8).map(row => {
    const link = make('button', {type:'button', className:'link', textContent:row.name});
    link.addEventListener('click', () => openPlayer(row.id));
    return make('div', {className:'coverage-row'}, [make('span', {className:'player-initials', textContent:initials(row.name)}), link,
      make('span', {className:'fine', textContent:`${row.votes} price votes`}), make('div', {className:'mini-progress'}, [progressBar(row.votes / d.minimum_price_votes * 100)])]);
  }));
  $('recent-answers').replaceChildren(...d.recent_answers.map(row => {
    const voter=make('button',{className:'link',type:'button',textContent:row.username,title:row.username});
    voter.addEventListener('click',()=>openVoter(row.voter_id));
    const player=make('button',{className:'text-action',type:'button',textContent:row.player,title:row.player});
    player.addEventListener('click',()=>openPlayer(row.player_id));
    return make('div',{className:'recent-row'},[make('span',{className:'player-initials',textContent:initials(row.username)}),make('div',{},[voter,make('p',{},[player])]),make('span',{className:'answer-pill',textContent:row.answer_type==='dont_know'?"Don't know":money(row.price)}),make('time',{dateTime:row.saved_at,title:timeText(row.saved_at),textContent:relativeTime(row.saved_at)})]);
  }));
  if (!d.recent_answers.length) $('recent-answers').append(make('p',{className:'empty-state',textContent:open?'Waiting for the first answer. New saves will appear here.':'No answers yet. Open voting when you are ready.'}));
  $('connection-status').textContent = `Sheets export: ${data.sheets_ready?'connected':'not connected'} · Resume emails: ${data.email_ready?'available':'copy-link only'}`;
  $('last-export').textContent = data.last_export ? `Last export: ${timeText(data.last_export.created_at)}` : 'No export recorded yet';
}

function drawChart(id, config) {
  if (state.charts[id]) {
    state.charts[id].destroy();
  }
  state.charts[id] = new window.Chart($(id), config);
  chartTheme(state.charts[id]);
}

function drawGapChart(direction, rows, color) {
  const canvasId = `chart-${direction}`;
  const empty = rows.length === 0;
  $(canvasId).parentElement.hidden = empty;
  $(`empty-${direction}`).hidden = !empty;
  if (empty) {
    $(`empty-${direction}`).textContent = `No players qualify yet. A player needs at least ${state.data.dashboard.minimum_price_votes} price votes and a median ${direction==='overpriced'?'below':'above'} their current price.`;
    if (state.charts[canvasId]) { state.charts[canvasId].destroy(); delete state.charts[canvasId]; }
    return;
  }
  $(canvasId).parentElement.style.height = `${Math.max(160, rows.length * 28 + 40)}px`;
  drawChart(canvasId, {
    type: 'bar',
    data: {
      labels: rows.map((row) => row.name),
      datasets: [{ data: rows.map((row) => row.gap), backgroundColor: color }],
    },
    options: {
      indexAxis: 'y',
      maintainAspectRatio: false,
      datasets: {bar: {borderRadius: 4, maxBarThickness: 13}},
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (item) => {
          const row = rows[item.dataIndex];
          return `${signedMoney(row.gap)} (current ${money(row.current_price)}, median ${money(row.median)}, ${row.votes} votes)`;
        } } },
      },
      scales: {
        x: { title: { display: true, text: 'Difference from current price ($)', color: '#87929c', font:{size:10} }, ticks: { color: '#87929c',font:{size:10} }, grid: { color: '#f0f2f5' },border:{display:false} },
        y: { ticks: { color: '#596875', autoSkip: false, font:{size:11} }, grid: { display: false },border:{display:false} },
      },
    },
  });
}

/* Sortable tables */

function sortRows(rows, sort, columns) {
  const column = columns.find((c) => c.key === sort.key);
  return [...rows].sort((a, b) => {
    const x = a[sort.key];
    const y = b[sort.key];
    const xEmpty = x === null || x === undefined;
    const yEmpty = y === null || y === undefined;
    if (xEmpty || yEmpty) {
      return xEmpty === yEmpty ? 0 : xEmpty ? 1 : -1;
    }
    if (column && column.text) {
      return String(x).localeCompare(String(y)) * sort.dir;
    }
    return (x - y) * sort.dir;
  });
}

function headerRow(columns, sort, onSort) {
  return make('tr', {}, columns.map((column) => {
    const button = make('button', { type: 'button', textContent: column.label });
    button.addEventListener('click', () => {
      if (sort.key === column.key) {
        sort.dir = -sort.dir;
      } else {
        sort.key = column.key;
        sort.dir = column.text ? 1 : -1;
      }
      onSort();
    });
    const th = make('th', { className: column.text || column.left ? '' : 'num' }, [button]);
    if (sort.key === column.key) {
      th.setAttribute('aria-sort', sort.dir === 1 ? 'ascending' : 'descending');
    }
    return th;
  }));
}

function cellFor(column, row) {
  const value = row[column.key];
  const td = make('td', { className: column.text ? '' : 'num' });
  if (column.range) {
    td.textContent = row.q1 === null || row.q1 === undefined ? '—' : `${money(row.q1)}–${money(row.q3)}`;
  } else if (column.key === 'votes') {
    const min=state.data.dashboard.minimum_price_votes;
    td.append(make('span',{className:`signal-pill ${row.votes===0?'none':row.votes<min?'early':'ready'}`,textContent:`${row.votes} votes`,title:row.votes<min?`Early signal: fewer than ${min} price votes`:`Meets the ${min}-price-vote comparison threshold`}));
  } else if (column.money) {
    td.textContent = money(value);
  } else if (column.gap) {
    td.textContent = signedMoney(value);
    if (value > 0) {
      td.classList.add('gap-up');
    } else if (value < 0) {
      td.classList.add('gap-down');
    }
  } else {
    td.textContent = value === null || value === undefined ? '' : String(value);
  }
  return td;
}

function renderPlayersTable() {
  if (!state.data) {
    return;
  }
  const query = $('player-search').value.trim().toLowerCase();
  const filter=$('player-filter').value, min=state.data.dashboard.minimum_price_votes;
  const rows = state.data.players.filter((row) => row.name.toLowerCase().includes(query) && (
    filter==='all' || filter==='qualified'&&row.votes>=min || filter==='low'&&row.votes<min ||
    filter==='none'&&row.votes===0 || filter==='higher'&&row.votes>=min&&row.gap>0 || filter==='lower'&&row.votes>=min&&row.gap<0));
  $('player-count').textContent=`${number(rows.length)} of ${number(state.data.players.length)} players`;
  const table = $('players-table');
  const body = make('tbody', {}, sortRows(rows, state.playerSort, PLAYER_COLUMNS).map((row) => {
    const cells = PLAYER_COLUMNS.map((column) => cellFor(column, row));
    const link = make('button', { type: 'button', className: 'link', textContent: row.name });
    link.addEventListener('click', () => openPlayer(row.id));
    cells[0].replaceChildren(link);
    return make('tr', {}, cells);
  }));
  if(!rows.length) body.append(emptyTable(PLAYER_COLUMNS.length,'No players match these filters. Try another search or choose All players.'));
  table.replaceChildren(make('thead', {}, [headerRow(PLAYER_COLUMNS, state.playerSort, renderPlayersTable)]), body);
}

/* One player */

async function openPlayer(playerId) {
  try {
  state.currentPlayer = playerId;
  showTab('player');
  const detail = await api('GET', `/api/admin/players/${playerId}`);
  const player = detail.player;
  $('player-title').textContent = player.name;
  const priced = detail.votes.filter((vote) => vote.answer_type !== 'dont_know').length;
  const skipped=detail.votes.length-priced;
  $('player-summary').textContent = `${priced} price ${priced===1?'vote':'votes'} · ${skipped} “Don't know” ${skipped===1?'answer':'answers'}`;
  const s=detail.summary;
  $('player-metrics').replaceChildren(
    metric(money(player.current_price),'Current starting price','The imported ADP price'),
    metric(money(s.median),'Community median',priced?`${priced} included price votes`:'No price votes yet'),
    metric(s.gap===null?'—':signedMoney(s.gap),'Difference from current','Median minus current price'),
    metric(s.q1===null?'—':`${money(s.q1)}–${money(s.q3)}`,'Middle 50% of votes',priced<5?'Early signal · fewer than 5 price votes':'Central range, excluding the outer quarters'),
  );

  drawChart('chart-player', {
    type: 'bar',
    data: {
      labels: detail.histogram.map((row) => (row.price === player.current_price ? `${money(row.price)} (current)` : money(row.price))),
      datasets: [{
        currentPriceIndex: detail.histogram.findIndex(row => row.price === player.current_price),
        data: detail.histogram.map((row) => row.count),
        backgroundColor: detail.histogram.map((row) => (row.price === player.current_price ? '#e87532' : '#b9c9cb')),
        borderRadius: 5, maxBarThickness: 45,
      }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { title: { display: true, text: 'Price', color: '#87929c' }, ticks: { color: '#596875' }, grid: { display: false } },
        y: { title: { display: true, text: 'Votes', color: '#87929c' }, beginAtZero: true, ticks: { color: '#87929c', precision: 0 }, grid: { color: '#f0f2f5' },border:{display:false} },
      },
    },
  });

  const rows = detail.votes.map((vote) => {
    const voterLink = make('button', { type: 'button', className: 'link', textContent: vote.username });
    voterLink.addEventListener('click', () => openVoter(vote.voter_id));
    const remove = make('button', { type: 'button', className: 'danger small', textContent: 'Delete' });
    remove.addEventListener('click', () => askDelete({
      title: 'Delete this vote?',
      detail: `${vote.username}: ${answerText(vote)} for ${player.name}.`,
      path: `/api/admin/votes/${vote.id}`,
      after: () => openPlayer(playerId),
    }));
    return make('tr', {}, [
      make('td', {}, [voterLink]),
      make('td', { textContent: answerText(vote) }),
      make('td', { textContent: timeText(vote.updated_at) }),
      make('td', {}, [remove]),
    ]);
  });
  $('player-votes').replaceChildren(
    make('thead', {}, [make('tr', {}, ['Username', 'Answer', 'Saved', ''].map((label) => make('th', { textContent: label })))]),
    make('tbody', {}, rows),
  );
  if(!rows.length) $('player-votes').querySelector('tbody').append(emptyTable(4,'No included answers for this player yet.'));
  return true;
  } catch(err) { if(err.status!==401) setStatus(err.message); return false; }
}

/* Voters */

async function loadVoters() {
  const data = await api('GET', '/api/admin/voters');
  state.voters = data.voters.map((voter) => ({ ...voter, flag_count: voter.flags.length }));
  const count=state.voters.filter(voter=>voter.flag_count>0).length;
  $('review-badge').textContent=count;
  $('review-badge').hidden=!count;
  renderVotersTable();
}

function renderVotersTable() {
  const query = $('voter-search').value.trim().toLowerCase();
  const filter=$('voter-filter').value;
  const rows = state.voters.filter((row) => row.username.toLowerCase().includes(query) && (
    filter==='all' || filter==='flagged'&&row.flag_count>0 || filter==='finished'&&row.finished ||
    filter==='progress'&&row.answered>0&&!row.finished || filter==='empty'&&row.answered===0));
  $('voter-count').textContent=`${number(rows.length)} of ${number(state.voters.length)} sessions`;
  const body = make('tbody', {}, sortRows(rows, state.voterSort, VOTER_COLUMNS).map((row) => {
    const cells = VOTER_COLUMNS.map((column) => cellFor(column, row));
    const link = make('button', { type: 'button', className: 'link', textContent: row.username });
    link.addEventListener('click', () => openVoter(row.id));
    cells[0].replaceChildren(link);
    cells[1].replaceChildren(make('div',{className:'row-progress'},[
      make('div',{},[make('span',{textContent:`${row.answered} / ${row.total}`}),make('span',{textContent:`${Math.round(row.total?row.answered/row.total*100:0)}%`})]),progressBar(row.total?row.answered/row.total*100:0)]));
    cells[4].className = 'wrap';
    cells[4].replaceChildren(...row.flags.map((flag) => make('span', { className: 'flag', textContent: flag.label, title:flag.detail||flag.label })));
    if(!row.flags.length) cells[4].append(make('span',{className:'fine',textContent:'No flags'}));
    cells[5].textContent = relativeTime(row.last_seen_at);
    cells[5].title = timeText(row.last_seen_at);
    const remove = make('button', {type:'button', className:'danger small', textContent:'Delete', ariaLabel:`Delete voter ${row.username}`});
    const resume = make('button', {type:'button', className:'secondary small', textContent:'Copy resume link', ariaLabel:`Copy resume link for ${row.username}`});
    const report = make('button', {type:'button', className:'secondary small', textContent:'View report', ariaLabel:`View report for ${row.username}`});
    report.onclick = () => openVoterReport(row.id);
    resume.onclick = () => copyVoterResume(row.id);
    remove.addEventListener('click', () => askDeleteVoter(row.id, row.username, row.votes_in_results));
    cells.push(make('td', {className:'voter-row-actions'}, [report, resume, remove]));
    return make('tr', {}, cells);
  }));
  if(!rows.length) body.append(emptyTable(VOTER_COLUMNS.length+1,filter==='flagged'?'No voter sessions need review.':'No voter sessions match these filters.'));
  const header = headerRow(VOTER_COLUMNS, state.voterSort, renderVotersTable);
  header.append(make('th', {textContent:'Actions'}));
  $('voters-table').replaceChildren(make('thead', {}, [header]), body);
}

function askDeleteVoter(id, username, count) {
  askDelete({title:`Delete voter ${username}?`,
    detail:`This removes this voter session from the admin list and excludes ${count} included answer(s) from results and exports. Its voting and resume links stop working until restored. You can restore the session from Deleted answers. Earlier answer deletions remain separate.`,
    confirmLabel:'Delete voter', reasonLabel:'Why are you deleting this voter?',
    path:`/api/admin/voters/${id}`, after:()=>openTab('voters')});
}

async function copyVoterResume(id) {
  try {
    const data = await api('GET', `/api/admin/voters/${id}/resume-link`);
    try {
      await navigator.clipboard.writeText(data.link);
      setStatus('Private resume link copied. Share it only with this voter; it gives access to their saved answers.');
    } catch (_) {
      setStatus(`Copy this private resume link and share it only with this voter: ${data.link}`);
    }
  } catch (err) { if(err.status!==401) setStatus(err.message); }
}

async function openVoter(voterId) {
  try {
  if (state.currentVoter !== voterId) {
    $('voter-difference-filter').value = 'all';
    $('voter-difference-sort').value = 'saved';
  }
  state.currentVoter = voterId;
  showTab('voter');
  const detail = await api('GET', `/api/admin/voters/${voterId}`);
  await loadVoters();
  $('voter-title').textContent = detail.voter.username;
  $('voter-copy-resume').onclick = () => copyVoterResume(voterId);
  $('voter-view-report').onclick = () => openVoterReport(voterId);
  const active = detail.votes.filter((vote) => !vote.deleted).length;
  $('voter-delete-session').onclick = () => askDeleteVoter(voterId, detail.voter.username, active);
  const info=state.voters.find(row=>row.id===voterId);
  $('voter-summary').textContent=`${detail.votes.length} of ${info?.total||state.data?.overview.total_players||313} answered · ${active} included · ${detail.votes.length-active} removed`;
  $('voter-flags').replaceChildren(...(info?.flags||[]).map(flag=>make('div',{className:'review-notice'},[
    make('b',{textContent:flag.label}),make('p',{textContent:flag.detail||'Review the answers below before taking action.'})])));
  $('voter-delete').disabled = active === 0;
  $('voter-delete').onclick = () => askDelete({
    title: `Delete ${active} votes from ${detail.voter.username}?`,
    detail: 'This deletes the votes still in results as one batch. Earlier deletions stay separate.',
    path: `/api/admin/voters/${voterId}/votes`,
    after: () => openVoter(voterId),
  });

  state.voterDetail = detail;
  renderVoterVotes();
  return true;
  } catch(err) { if(err.status!==401) setStatus(err.message); return false; }
}

const voteDifference = vote => vote.answer_type === 'dont_know' || vote.price === null ? null : vote.price - vote.current_price;

function renderVoterVotes() {
  const detail = state.voterDetail;
  if (!detail) return;
  const voterId = detail.voter.id;
  const filter = $('voter-difference-filter').value;
  const sort = $('voter-difference-sort').value;
  const visible = detail.votes.filter(vote => {
    const diff = voteDifference(vote);
    return filter === 'all' || (filter === 'unknown' ? diff === null : diff !== null && (
      filter === 'higher' ? diff > 0 : filter === 'lower' ? diff < 0 : diff === 0));
  });
  if (sort !== 'saved') visible.sort((a,b) => {
    const x = voteDifference(a), y = voteDifference(b);
    if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1;
    return sort === 'highest' ? y-x : sort === 'lowest' ? x-y : Math.abs(y)-Math.abs(x);
  });
  $('voter-answer-count').textContent = `${visible.length} of ${detail.votes.length} answers`;
  const rows = visible.map((vote) => {
    const action = make('td');
    if (!vote.deleted) {
      const remove = make('button', { type: 'button', className: 'danger small', textContent: 'Delete' });
      remove.addEventListener('click', () => askDelete({
        title: 'Delete this vote?',
        detail: `${detail.voter.username}: ${answerText(vote)} for ${vote.player}.`,
        path: `/api/admin/votes/${vote.id}`,
        after: () => openVoter(voterId),
      }));
      action.append(remove);
    } else {
      action.textContent = 'Deleted';
    }
    const difference = voteDifference(vote);
    return make('tr', { className: vote.deleted ? 'deleted' : '' }, [
      make('td', { textContent: vote.player }),
      make('td', { className: 'num', textContent: money(vote.current_price) }),
      make('td', { className:'num', textContent: answerText(vote) }),
      make('td', { className: `num${difference > 0 ? ' gap-up' : difference < 0 ? ' gap-down' : ''}`, textContent: difference === null ? '—' : signedMoney(difference), title:'Answer minus current ADP price' }),
      make('td', { textContent: timeText(vote.updated_at) }),
      action,
    ]);
  });
  $('voter-votes').replaceChildren(
    make('thead', {}, [make('tr', {}, ['Player', 'Current', 'Answer', 'Difference', 'Saved', ''].map((label) => {
      const header = make('th', { className:['Current','Answer','Difference'].includes(label) ? 'num' : '', textContent:label });
      if (label === 'Difference') {
        header.title = 'Answer minus current ADP price; click to sort';
        const button = make('button', {type:'button', textContent:'Difference'});
        button.onclick = () => { $('voter-difference-sort').value = sort === 'highest' ? 'lowest' : 'highest'; renderVoterVotes(); };
        header.replaceChildren(button);
        if (sort === 'highest' || sort === 'lowest') header.setAttribute('aria-sort',sort === 'highest' ? 'descending' : 'ascending');
      }
      return header;
    }))]),
    make('tbody', {}, rows),
  );
  if(!rows.length) $('voter-votes').querySelector('tbody').append(emptyTable(6,detail.votes.length ? 'No answers match this Difference filter.' : 'This voter has not submitted any answers yet.'));
}

/* Deleting and restoring */

async function openVoterReport(voterId) {
  try {
    if (state.reportVoter !== voterId) {
      $('report-search').value=''; $('report-filter').value='all'; $('report-sort').value='price';
    }
    state.currentVoter=voterId; state.reportVoter=voterId;
    const data=await api('GET',`/api/admin/voters/${voterId}/report`);
    state.voterReport=data;
    showTab('voter-report');
    $('report-title').textContent=`${data.username} — Scouting report`;
    $('report-subtitle').textContent=`${data.answered} of ${data.total} answered · ${data.excluded} excluded from results · Read-only admin view`;
    $('report-metrics').replaceChildren(
      metric(number(data.answered),'Answered','Saved player answers'),metric(number(data.priced),'Price votes','Includes $0 valuations'),
      metric(number(data.dont_know),"Don't know",'Unpriced answers'),metric(number(data.total-data.answered),'Remaining','Not answered yet'));
    for (const [id,rows] of [['report-increases',data.biggest_increases],['report-decreases',data.biggest_decreases]]) {
      $(id).replaceChildren(...rows.map(row=>make('div',{className:'report-highlight'},[
        make('div',{},[make('b',{textContent:row.name}),make('small',{className:'fine',textContent:`Current ${money(row.current_price)} → voted ${money(row.price)}${row.excluded?' · Excluded from results':''}`})]),
        make('strong',{className:row.difference>0?'gap-up':'gap-down',textContent:signedMoney(row.difference)})])));
      if(!rows.length) $(id).append(make('p',{className:'fine',textContent:'No saved prices in this category.'}));
    }
    renderVoterReport();
    return true;
  } catch(err) { if(err.status!==401) setStatus(err.message); return false; }
}

function renderVoterReport() {
  const data=state.voterReport;
  if(!data)return;
  const priceSort=(a,b)=>a.price===null||b.price===null ? a.price===b.price?a.name.localeCompare(b.name):a.price===null?1:-1 : b.price-a.price||a.name.localeCompare(b.name);
  const ranked=[...data.players].sort(priceSort);
  const ranks=new Map(ranked.filter(r=>r.price!==null).map((r,i)=>[r.player_id,i+1]));
  const filter=$('report-filter').value, query=$('report-search').value.trim().toLocaleLowerCase(), sort=$('report-sort').value;
  const rows=ranked.filter(row=>row.name.toLocaleLowerCase().includes(query) && (
    filter==='all'||filter==='answered'&&row.answered||filter==='unanswered'&&!row.answered||filter==='unknown'&&row.answer_type==='dont_know'||
    row.difference!==null&&(filter==='higher'&&row.difference>0||filter==='lower'&&row.difference<0||filter==='same'&&row.difference===0)));
  if(sort==='name')rows.sort((a,b)=>a.name.localeCompare(b.name));
  else if(sort!=='price')rows.sort((a,b)=>a.difference===null||b.difference===null ? a.difference===b.difference?0:a.difference===null?1:-1 : sort==='highest'?b.difference-a.difference:a.difference-b.difference);
  $('report-count').textContent=`${rows.length} of ${data.total} players · Rank uses saved voter prices. Equal prices sort by name; unpriced players stay at the bottom.`;
  const body=make('tbody',{},rows.map(row=>make('tr',{},[
    make('td',{className:'num',textContent:ranks.get(row.player_id)||'—'}),
    make('td',{},[make('span',{textContent:row.name}),...(row.excluded?[make('small',{className:'report-excluded fine',textContent:'Excluded from results'})]:[])]),
    make('td',{className:'num',textContent:money(row.current_price)}),
    make('td',{className:'num',textContent:!row.answered?'Not answered':row.answer_type==='dont_know'?"Don't know":money(row.price)}),
    make('td',{className:`num ${row.difference>0?'gap-up':row.difference<0?'gap-down':''}`,textContent:row.difference===null?'—':signedMoney(row.difference)})])));
  if(!rows.length)body.append(emptyTable(5,'No players match these filters.'));
  $('report-table').replaceChildren(make('thead',{},[make('tr',{},['Rank','Player','Current','Voter price','Difference'].map((text,i)=>make('th',{className:i===1?'':'num',textContent:text})))]),body);
}

function askDelete(request) {
  state.pendingDelete = request;
  $('delete-title').textContent = request.title;
  $('delete-detail').textContent = request.detail;
  $('delete-confirm').textContent = request.confirmLabel || 'Remove answers';
  document.querySelector('label[for="delete-reason"]').textContent = request.reasonLabel || 'Why are you removing these answers?';
  $('delete-reason').value = '';
  setText($('delete-error'), '');
  $('delete-dialog').showModal();
  $('delete-reason').focus();
}

async function confirmDelete(event) {
  event.preventDefault();
  const request = state.pendingDelete;
  const button = $('delete-confirm');
  button.disabled = true;
  try {
    const result = await api('DELETE', request.path, { reason: $('delete-reason').value });
    $('delete-dialog').close();
    await request.after();
    setStatus(`Deleted as batch ${result.batch_id}. You can restore it from the Deleted tab.`);
  } catch (err) {
    setText($('delete-error'), err.message);
  } finally {
    button.disabled = false;
  }
}

async function loadBatches() {
  const data = await api('GET', '/api/admin/deletion-batches');
  const rows = data.batches.map((batch) => {
    const action = make('td');
    if (batch.restored_at) {
      action.textContent = `Restored ${timeText(batch.restored_at)}`;
    } else {
      const restore = make('button', { type: 'button', className: 'secondary small', textContent: 'Restore' });
      restore.addEventListener('click', async () => {
        restore.disabled = true;
        try {
          const result = await api('POST', `/api/admin/deletion-batches/${batch.id}/restore`);
          await loadBatches();
          setStatus(`${batch.removes_voter ? 'Voter session restored. ' : ''}Restored ${result.restored} votes from batch ${batch.id}.`);
        } catch (err) {
          restore.disabled = false;
          setStatus(err.message);
        }
      });
      action.append(restore);
    }
    return make('tr', {}, [
      make('td', { className: 'num', textContent: String(batch.id) }),
      make('td', { textContent: batch.removes_voter ? 'Voter session + answers' : batch.scope === 'voter' ? 'All of a voter\'s votes' : 'One vote' }),
      make('td', { textContent: batch.username }),
      make('td', { className: 'num', textContent: String(batch.vote_count) }),
      make('td', { className: 'wrap', textContent: batch.reason }),
      make('td', { textContent: timeText(batch.created_at) }),
      action,
    ]);
  });
  $('batches-table').replaceChildren(
    make('thead', {}, [make('tr', {}, ['Batch', 'Type', 'Username', 'Votes', 'Reason', 'Deleted', ''].map((label) => make('th', { textContent: label })))]),
    make('tbody', {}, rows),
  );
  if(!rows.length) $('batches-table').querySelector('tbody').append(emptyTable(7,'No answers have been removed. Deletion reasons and restore actions will appear here.'));
}

async function refreshCurrent() {
  if(state.loading || $('app').hidden) return;
  state.loading=true; $('refresh').disabled=true; $('refresh').textContent='Refreshing…';
  setStatus('');
  try {
    if(state.tab==='voters') await loadVoters();
    else if(state.tab==='voter') { if(await openVoter(state.currentVoter)===false) return; }
    else if(state.tab==='voter-report') { if(await openVoterReport(state.currentVoter)===false) return; }
    else if(state.tab==='deleted') await loadBatches();
    else if(state.tab==='player') { if(await openPlayer(state.currentPlayer)===false) return; }
    else await loadResults();
    $('last-refreshed').textContent=`Updated ${new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;
  } catch(err) { if(err.status!==401) setStatus(`Could not refresh: ${err.message} Showing the last loaded data.`); }
  finally { state.loading=false; $('refresh').disabled=false; $('refresh').textContent='↻ Refresh'; }
}

/* Controls */

async function toggleVoting() {
  const open = !state.data.voting_open;
  try {
    await api('PUT', '/api/admin/settings/voting-open', { open });
    await loadResults();
  } catch (err) {
    setStatus(err.message);
  }
}

async function exportSheets() {
  const button = $('export-sheets');
  const result = $('export-result');
  button.disabled = true;
  state.exporting = true;
  setText(result, 'Exporting...');
  try {
    const data = await api('POST', '/api/admin/export/sheets');
    const link = make('a', { href: data.url, target: '_blank', rel: 'noopener', textContent: 'Open the results spreadsheet' });
    result.replaceChildren('Export finished. ', link);
    result.hidden = false;
  } catch (err) {
    setText(result, err.message);
  } finally {
    state.exporting = false;
    button.disabled = !state.data.sheets_ready;
  }
}

function bindEvents() {
  document.querySelectorAll('.theme-toggle').forEach(button=>button.addEventListener('click',toggleTheme));
  window.addEventListener('admin-theme-change',updateThemeControls);
  updateThemeControls();
  $('refresh').addEventListener('click',refreshCurrent);
  $('player-filter').addEventListener('change',()=>{
    const value=$('player-filter').value;
    state.playerSort=value==='lower'?{key:'gap',dir:1}:value==='higher'?{key:'gap',dir:-1}:value==='low'||value==='none'?{key:'votes',dir:1}:{key:'current_price',dir:-1};
    renderPlayersTable();
  });
  $('voter-filter').addEventListener('change',renderVotersTable);
  $('voter-difference-filter').addEventListener('change',renderVoterVotes);
  $('voter-difference-sort').addEventListener('change',renderVoterVotes);
  document.querySelectorAll('[data-player-filter]').forEach(button=>button.addEventListener('click',()=>filterPlayers(button.dataset.playerFilter)));
  $('login-form').addEventListener('submit', submitLogin);
  $('logout').addEventListener('click', async () => {
    try {
      await api('POST', '/api/admin/logout');
    } finally {
      showLogin();
    }
  });
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => openTab(tab.dataset.tab));
  });
  $('player-search').addEventListener('input', renderPlayersTable);
  $('voter-search').addEventListener('input', renderVotersTable);
  $('player-back').addEventListener('click', () => openTab('players'));
  $('voter-back').addEventListener('click', () => openTab('voters'));
  $('combined-search').addEventListener('input',renderCombinedAdp);
  $('combined-filter').addEventListener('change',renderCombinedAdp);
  $('combined-method').addEventListener('change',renderCombinedAdp);
  $('report-back').addEventListener('click', () => openVoter(state.currentVoter));
  $('report-search').addEventListener('input', renderVoterReport);
  $('report-filter').addEventListener('change', renderVoterReport);
  $('report-sort').addEventListener('change', renderVoterReport);
  $('toggle-voting').addEventListener('click', toggleVoting);
  $('export-sheets').addEventListener('click', exportSheets);
  $('delete-form').addEventListener('submit', confirmDelete);
  $('delete-cancel').addEventListener('click', () => $('delete-dialog').close());
}

async function start() {
  window.Chart.defaults.font.family='-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  window.Chart.defaults.color='#87929c';
  window.Chart.defaults.animation=false;
  bindEvents();
  try {
    await enterApp();
  } catch (err) {
    if (err.status !== 401) {
      showLogin();
      setText($('login-error'), err.message);
    }
  }
}

start();
setInterval(()=>{if(!document.hidden && ['overview','players','voters','combined'].includes(state.tab)) refreshCurrent();},60000);
