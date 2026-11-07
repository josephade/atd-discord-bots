'use strict';

const BATCH_SIZE = 10;
const SCREENS = ['welcome', 'username', 'vote', 'later', 'done', 'closed', 'my-votes-screen'];

const $ = (id) => document.getElementById(id);

const state = {
  votingOpen: false,
  card: null,
  saving: false,
  pendingAnswer: null,
  link: null,
  afterWelcome: null,
  savedMessage: '',
  myVotes: null,
  reviewEdit: false,
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
    throw new ApiFailure(response.status, data.error || 'Something went wrong.');
  }
  return data;
}

function show(screen) {
  for (const id of SCREENS) {
    $(id).hidden = id !== screen;
  }
  window.scrollTo(0, 0);
}

function setText(element, text) {
  element.textContent = text || '';
  element.hidden = !text;
}

/* Welcome */

function buildSparks() {
  const group = $('sparks');
  group.replaceChildren();
  const colors = ['#ffb066', '#ff7a2b', '#ffffff', '#ffd2b0'];
  const count = 22;
  for (let i = 0; i < count; i += 1) {
    const angle = (Math.PI * 2 * i) / count;
    const distance = 110 + ((i * 53) % 130);
    const spark = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    spark.setAttribute('r', String(3 + (i % 4)));
    spark.setAttribute('fill', colors[i % colors.length]);
    spark.setAttribute('class', 'spark');
    spark.style.setProperty('--dx', `${Math.round(Math.cos(angle) * distance)}px`);
    spark.style.setProperty('--dy', `${Math.round(Math.sin(angle) * distance)}px`);
    group.appendChild(spark);
  }
}

function playWelcome(next) {
  state.afterWelcome = next;
  buildSparks();
  show('welcome');
  $('enter-voting').textContent = state.votingOpen ? 'Start voting ↗' : 'Check voting status ↗';
}

function endWelcome() {
  const next = state.afterWelcome;
  state.afterWelcome = null;
  if (next) {
    next();
  }
}

/* Username */

function showUsername() {
  if (!state.votingOpen) {
    show('closed');
    return;
  }
  show('username');
  $('username-input').focus();
}

async function submitUsername(event) {
  event.preventDefault();
  const button = event.target.querySelector('button');
  setText($('username-error'), '');
  button.disabled = true;
  try {
    await api('POST', '/api/session', { username: $('username-input').value });
    state.link = null;
    await rememberSession();
    await loadCard();
  } catch (err) {
    if (err.status === 403) {
      state.votingOpen = false;
      show('closed');
    } else {
      setText($('username-error'), err.message);
      if (err.status === 409) {
        $('returning-voter').open = true;
        $('resume-link-input').focus();
      }
    }
  } finally {
    button.disabled = false;
  }
}

/* Voting */

async function loadCard(position) {
  const path = position ? `/api/card?position=${position}` : '/api/card';
  try {
    const card = await api('GET', path);
    $('connection').hidden = true;
    if (card.done) {
      state.card = null;
      $('done-total').textContent = String(card.total);
      await openMyVotes();
      return;
    }
    state.card = card;
    renderCard();
    show('vote');
  } catch (err) {
    handleFailure(err, () => loadCard(position));
  }
}

function handleFailure(err, retry) {
  if (err.status === 401) {
    showUsername();
  } else if (err.status === 403) {
    state.votingOpen = false;
    show('closed');
  } else {
    $('connection').hidden = false;
    $('connection-retry').onclick = () => {
      $('connection').hidden = true;
      retry();
    };
  }
}

function makeOption(label, className, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.textContent = label;
  button.addEventListener('click', onClick);
  return button;
}

function renderCard() {
  const card = state.card;
  const answer = card.answer;
  const palette = ['#f0a270', '#78c8d4', '#c3abed', '#d8bd73'];
  document.querySelector('.vote-main').style.setProperty('--card-accent', palette[Number(card.player.id) % palette.length]);

  $('player-name').textContent = card.player.name;
  $('current-player-price').textContent = `$${card.player.current_price}`;
  $('player-position').textContent = `PLAYER ${card.position} / ${card.total}`;
  setText($('save-status'), state.savedMessage || (answer ? 'Your saved answer. Choose another price to update it.' : 'Choose a price to save and continue.'));
  $('save-status').classList.toggle('is-saved', Boolean(state.savedMessage));
  renderPhoto(card.player);
  renderProgress(card.answered, card.total);

  const options = $('options');
  options.replaceChildren();
  for (const price of card.options) {
    const selected = answer && answer.answer_type === 'option' && answer.price === price;
    const button = makeOption(`$${price}`, `option${selected ? ' selected' : ''}`, () => saveAnswer('option', price));
    button.setAttribute('aria-pressed', String(Boolean(selected)));
    options.appendChild(button);
  }
  const otherSelected = answer && answer.answer_type === 'other';
  const otherLabel = otherSelected ? `Other: $${answer.price}` : 'Other';
  const other = makeOption(otherLabel, `option option-other${otherSelected ? ' selected' : ''}`, toggleOther);
  other.setAttribute('aria-pressed', String(Boolean(otherSelected)));
  options.appendChild(other);

  const otherInput = $('other-input');
  otherInput.max = String(card.other_max);
  otherInput.value = otherSelected ? String(answer.price) : '';
  $('other-form').hidden = true;

  const dontKnow = $('dont-know');
  const dontKnowSelected = answer && answer.answer_type === 'dont_know';
  dontKnow.classList.toggle('selected', Boolean(dontKnowSelected));
  dontKnow.setAttribute('aria-pressed', String(Boolean(dontKnowSelected)));

  $('back').disabled = card.position <= 1;
  $('resume-voting').hidden = !answer;
  $('retry').hidden = true;
  setText($('vote-error'), '');
  setText($('copy-status'), '');
  setBusy(false);
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    for (const element of document.querySelectorAll('.player-head, .options')) {
      element.getAnimations().forEach(animation => animation.cancel());
      element.animate([{ opacity: .25, transform: 'translateY(12px)' }, { opacity: 1, transform: 'translateY(0)' }],
        { duration: 260, easing: 'ease-out' });
    }
  }
}

function renderPhoto(player) {
  const photo = $('player-photo');
  const fallback = $('photo-fallback');
  const credit = $('photo-credit');
  const info = player.photo;

  // Clear the last photo first, so it can't sit above the next player's name while loading.
  photo.hidden = true;
  photo.removeAttribute('src');
  photo.alt = '';
  fallback.hidden = false;
  credit.hidden = true;
  if (!info) {
    return;
  }
  photo.onload = () => {
    photo.hidden = false;
    fallback.hidden = true;
    if (info.credit_url) {
      credit.href = info.credit_url;
      credit.hidden = false;
    }
  };
  photo.onerror = () => {
    photo.hidden = true;
    fallback.hidden = false;
  };
  photo.alt = `Photo of ${player.name}`;
  photo.src = info.url;
}

function renderProgress(answered, total) {
  $('counter').textContent = `${answered} of ${total} answered · ${Math.round(answered / total * 100)}% complete`;
  $('progress-fill').style.width = `${(answered / total) * 100}%`;
  const bar = $('progress');
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', String(total));
  bar.setAttribute('aria-valuenow', String(answered));
}

function setBusy(busy) {
  state.saving = busy;
  $('options').setAttribute('aria-busy', String(busy));
  document.querySelectorAll('#back, #resume-voting, #continue-later, #copy-link, #my-votes').forEach(button => {
    button.disabled = busy || (button.id === 'back' && state.card && state.card.position <= 1);
  });
  document.querySelectorAll('.option, #dont-know, #other-form button').forEach((button) => {
    button.disabled = busy;
  });
}

function toggleOther() {
  const form = $('other-form');
  form.hidden = !form.hidden;
  if (!form.hidden) {
    $('other-input').focus();
  }
}

function submitOther(event) {
  event.preventDefault();
  const raw = $('other-input').value.trim();
  const price = Number(raw);
  const max = state.card.other_max;
  if (raw === '' || !Number.isInteger(price) || price < 0 || price > max) {
    setText($('vote-error'), `Enter a whole dollar amount from $0 to $${max}.`);
    return;
  }
  saveAnswer('other', price);
}

async function saveAnswer(answerType, price) {
  if (state.saving || !state.card) {
    return;
  }
  const card = state.card;
  const wasAnswered = Boolean(card.answer);
  state.pendingAnswer = [answerType, price];
  markChosen(answerType, price);
  setBusy(true);
  setText($('save-status'), 'Saving your answer…');
  $('save-status').classList.remove('is-saved');
  setText($('vote-error'), '');
  setText($('milestone'), '');
  $('retry').hidden = true;

  try {
    const body = answerType === 'dont_know' ? { answer_type: answerType } : { answer_type: answerType, price };
    const saved = await api('PUT', `/api/votes/${card.player.id}`, body);
    state.pendingAnswer = null;
    state.savedMessage = `Locked in ✓ ${card.player.name}${answerType === 'dont_know' ? ' · skipped' : ` · $${price}`}`;
    if (state.reviewEdit) {
      state.reviewEdit = false;
      await openMyVotes();
      return;
    }
    showMilestone(saved.answered, saved.total, wasAnswered);
    if (wasAnswered && card.position < card.total) {
      await loadCard(card.position + 1);
    } else {
      await loadCard();
    }
  } catch (err) {
    setBusy(false);
    setText($('save-status'), 'Not saved yet. Please try again.');
    if (err.status === 401 || err.status === 403) {
      handleFailure(err);
    } else if (err.status === 400 || err.status === 429) {
      setText($('vote-error'), err.message);
      $('retry').hidden = err.status !== 429;
    } else {
      setText($('vote-error'), 'That answer didn\'t save. Check your connection and retry.');
      $('retry').hidden = false;
    }
  }
}

// Keeps the chosen answer on screen while it saves, and if the save fails.
function markChosen(answerType, price) {
  document.querySelectorAll('.option').forEach((button) => {
    const isOther = button.classList.contains('option-other');
    const chosen = answerType === 'other' ? isOther : answerType === 'option' && !isOther && button.textContent === `$${price}`;
    button.classList.toggle('selected', chosen);
    button.setAttribute('aria-pressed', String(chosen));
  });
  $('dont-know').classList.toggle('selected', answerType === 'dont_know');
  $('dont-know').setAttribute('aria-pressed', String(answerType === 'dont_know'));
}

function showMilestone(answered, total, wasAnswered) {
  const element = $('milestone');
  if (!wasAnswered && answered % BATCH_SIZE === 0 && answered < total) {
    setText(element, `🏀 ${answered} players answered. Your scouting report is growing! ${total - answered} left — come back whenever you like.`);
  } else {
    setText(element, '');
  }
}

/* Resume link */

function voteNode(tag, text, className='') {
  const element = document.createElement(tag);
  element.textContent = text;
  element.className = className;
  return element;
}

function deltaText(value) {
  return value === null ? '—' : value > 0 ? `+$${value}` : value < 0 ? `−$${Math.abs(value)}` : '$0';
}

async function openMyVotes() {
  try {
    const data = await api('GET', '/api/my-votes');
    const firstCompletedReport = data.answered === data.total && (!state.myVotes || state.myVotes.answered < state.myVotes.total);
    if (firstCompletedReport) {
      $('my-votes-filter').value = 'all';
      $('my-votes-sort').value = 'price';
    }
    state.myVotes = data;
    state.reviewEdit = false;
    state.votingOpen = data.voting_open;
    $('connection').hidden = true;
    $('my-votes-title').textContent = data.answered === data.total ? 'Scouting complete.' : 'Your scouting report.';
    $('my-votes-subtitle').textContent = `${data.username} · ${data.answered} of ${data.total} players answered. Search any player to revisit your price.`;
    $('my-votes-continue').hidden = !data.voting_open || data.answered === data.total;
    $('my-votes-notice').textContent = `${data.voting_open ? 'You can edit your prices while voting is open.' : 'Voting is closed. Your saved answers are available to view.'} ${data.excluded ? `${data.excluded} answer(s) were excluded by an admin; editing does not restore them to results.` : ''}`;
    $('my-votes-feedback').textContent = state.savedMessage;
    const metrics = $('my-votes-metrics'); metrics.replaceChildren();
    for (const [value, label] of [[data.answered,'Players answered'],[data.priced,'Price votes'],[data.dont_know,"Don't know"],[data.total-data.answered,'Still to price']]) {
      const box = document.createElement('div');box.append(voteNode('strong',value),voteNode('span',label));metrics.append(box);
    }
    for (const [id, rows] of [['my-increases',data.biggest_increases],['my-decreases',data.biggest_decreases]]) {
      const host = $(id);host.replaceChildren();
      for (const row of rows) {
        const button=voteNode('button','', 'my-price-highlight');button.type='button';button.disabled=!data.voting_open;
        button.append(voteNode('span',row.name),voteNode('strong',deltaText(row.difference)));
        button.onclick=()=>editMyPlayer(row.position);host.append(button);
      }
      if (!rows.length) host.append(voteNode('p','No saved prices in this category yet.','fine'));
    }
    renderMyVotes();
    show('my-votes-screen');
  } catch (err) { handleFailure(err, openMyVotes); }
}

function editMyPlayer(position) {
  state.reviewEdit=true;
  state.savedMessage='';
  setText($('milestone'),'');
  loadCard(position);
}

function renderMyVotes() {
  const data = state.myVotes;
  if (!data) return;
  const query=$('my-votes-search').value.trim().toLocaleLowerCase();
  const filter=$('my-votes-filter').value;
  const rows=data.players.filter(row=>row.name.toLocaleLowerCase().includes(query) && (
    filter==='all' || filter==='answered'&&row.answered || filter==='unanswered'&&!row.answered ||
    filter==='unknown'&&row.answer_type==='dont_know' || row.difference!==null && (
      filter==='higher'&&row.difference>0 || filter==='lower'&&row.difference<0 || filter==='same'&&row.difference===0)));
  const sort=$('my-votes-sort').value;
  const ranking = [...data.players].sort((a,b)=>a.price===null||b.price===null ? a.price===b.price?a.name.localeCompare(b.name):a.price===null?1:-1 : b.price-a.price || a.name.localeCompare(b.name));
  const ranks = new Map(ranking.filter(row=>row.price!==null).map((row,index)=>[row.player_id,index+1]));
  if (sort==='name') rows.sort((a,b)=>a.name.localeCompare(b.name));
  else if (sort==='price') rows.sort((a,b)=>a.price===null||b.price===null ? a.price===b.price?a.name.localeCompare(b.name):a.price===null?1:-1 : b.price-a.price || a.name.localeCompare(b.name));
  else if (sort!=='order') rows.sort((a,b)=>a.difference===null||b.difference===null ? a.difference===b.difference?0:a.difference===null?1:-1 : sort==='highest'?b.difference-a.difference:a.difference-b.difference);
  $('my-adp-heading').textContent = sort==='price' ? 'Your ADP — highest price first' : 'Your player list';
  $('my-votes-count').textContent=`${rows.length} of ${data.total} players shown · Your rank uses your saved price. Equal prices sort by name; unpriced players have no rank. Difference = your price minus current ADP price.`;
  const body=$('my-votes-rows');body.replaceChildren();
  for (const row of rows) {
    const tr=document.createElement('tr');
    tr.append(voteNode('td',ranks.get(row.player_id) || '—'));
    const answer=!row.answered?'Not answered':row.answer_type==='dont_know'?"Don't know":`$${row.price}`;
    [row.name,`$${row.current_price}`,answer,deltaText(row.difference)].forEach((text,index)=>{
      const td=voteNode('td',text);
      if(index===3) td.className=row.difference>0?'my-diff-up':row.difference<0?'my-diff-down':'';
      if(index===0&&row.excluded) td.append(voteNode('small','Excluded from results'));
      tr.append(td);
    });
    const td=document.createElement('td');const button=voteNode('button',row.answered?'Edit':'Price','secondary');button.type='button';button.disabled=!data.voting_open;
    button.setAttribute('aria-label',`${row.answered?'Edit':'Price'} ${row.name}`);button.onclick=()=>editMyPlayer(row.position);td.append(button);tr.append(td);body.append(tr);
  }
  if (!rows.length) { const tr=document.createElement('tr');const td=voteNode('td','No players match. Choose All players or change your search.');td.colSpan=6;tr.append(td);body.append(tr); }
}

async function getLink() {
  if (!state.link) {
    const data = await api('GET', '/api/resume-link');
    state.link = data.link;
  }
  try { localStorage.setItem('tfm-voter-resume', new URL(state.link, location.origin).pathname); } catch (_) {}
  return state.link;
}

async function rememberSession() {
  try { await getLink(); } catch (_) { /* A backup failure must never block saved progress. */ }
}

function resumePath(value) {
  try {
    const url = new URL(value.trim(), location.origin);
    return url.origin === location.origin && /^\/r\/\d+\.[A-Za-z0-9_-]{20,100}$/.test(url.pathname) ? url.pathname : null;
  } catch (_) { return null; }
}

function resumeFromInput() {
  const path = resumePath($('resume-link-input').value);
  if (!path) {
    setText($('resume-link-error'), 'Paste your full private resume link for this site. Ask the commissioner if you have lost it.');
    return;
  }
  try { localStorage.removeItem('tfm-voter-resume'); } catch (_) {}
  location.assign(path);
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const field = document.createElement('textarea');
  field.value = text;
  field.setAttribute('readonly', '');
  field.className = 'copy-field';
  document.body.appendChild(field);
  field.select();
  const copied = document.execCommand('copy');
  field.remove();
  if (!copied) {
    throw new Error('Copy failed');
  }
}

async function copyLinkFromVote() {
  try {
    await copyText(await getLink());
    setText($('copy-status'), 'Copied. Don\'t share it: anyone with this link can vote as you.');
  } catch (err) {
    // Clipboard blocked: the Continue Later screen shows the link to copy by hand.
    openLater();
  }
}

async function openLater() {
  setText($('email-status'), '');
  setText($('later-copy-status'), '');
  $('email-input').value = '';
  try {
    $('later-link').value = await getLink();
    show('later');
    $('later-link').select();
  } catch (err) {
    handleFailure(err, openLater);
  }
}

async function copyLinkFromLater() {
  try {
    await copyText($('later-link').value);
    setText($('later-copy-status'), 'Copied.');
  } catch (err) {
    $('later-link').select();
    setText($('later-copy-status'), 'Press and hold the link to copy it.');
  }
}

async function submitEmail(event) {
  event.preventDefault();
  const button = event.target.querySelector('button');
  button.disabled = true;
  setText($('email-status'), 'Sending...');
  try {
    const result = await api('POST', '/api/continue-later', { email: $('email-input').value });
    if (result.sent) {
      setText($('email-status'), 'Sent. If you don\'t see it soon, check your spam folder.');
      $('email-input').value = '';
    } else {
      setText($('email-status'), result.error);
    }
  } catch (err) {
    setText($('email-status'), err.status === 0 ? 'That didn\'t send. Copy your link instead.' : err.message);
  } finally {
    button.disabled = false;
  }
}

/* Start */

function bindEvents() {
  const themeButton = $('theme-toggle');
  const updateTheme = () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    themeButton.textContent = `${next === 'light' ? '☀' : '☾'} ${next === 'light' ? 'Light' : 'Dark'} mode`;
    themeButton.setAttribute('aria-label', `Switch to ${next} mode`);
    document.querySelector('meta[name="theme-color"]').content = next === 'light' ? '#10161f' : '#f5f6f8';
  };
  themeButton.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('tfm-vote-theme', next); } catch (_) {}
    updateTheme();
  });
  window.addEventListener('vote-theme-change', updateTheme);
  updateTheme();
  $('skip').addEventListener('click', endWelcome);
  $('enter-voting').addEventListener('click', endWelcome);
  $('replay-intro').addEventListener('click', () => {
    const arena = document.querySelector('.arena');
    const replacement = arena.cloneNode(true);
    arena.replaceWith(replacement);
  });
  $('username-form').addEventListener('submit', submitUsername);
  $('resume-from-link').addEventListener('click', resumeFromInput);
  $('resume-link-input').addEventListener('keydown', event => { if(event.key==='Enter') { event.preventDefault(); resumeFromInput(); } });
  $('other-form').addEventListener('submit', submitOther);
  $('dont-know').addEventListener('click', () => saveAnswer('dont_know', null));
  $('retry').addEventListener('click', () => {
    if (state.pendingAnswer) {
      saveAnswer(state.pendingAnswer[0], state.pendingAnswer[1]);
    }
  });
  $('back').addEventListener('click', () => {
    if (state.card && state.card.position > 1) {
      state.savedMessage = '';
      setText($('milestone'), '');
      loadCard(state.card.position - 1);
    }
  });
  $('resume-voting').addEventListener('click', () => {
    state.savedMessage = '';
    setText($('milestone'), '');
    loadCard();
  });
  $('copy-link').addEventListener('click', copyLinkFromVote);
  $('continue-later').addEventListener('click', openLater);
  $('my-votes').addEventListener('click', openMyVotes);
  $('my-votes-search').addEventListener('input', renderMyVotes);
  $('my-votes-filter').addEventListener('change', renderMyVotes);
  $('my-votes-sort').addEventListener('change', renderMyVotes);
  $('view-my-adp').addEventListener('click', () => {
    $('my-votes-search').value=''; $('my-votes-filter').value='all'; $('my-votes-sort').value='price';
    renderMyVotes(); $('my-adp-heading').scrollIntoView({block:'start'});
  });
  $('my-votes-continue').addEventListener('click', () => { state.reviewEdit=false; state.savedMessage=''; loadCard(); });
  $('my-votes-link').addEventListener('click', openLater);
  $('closed-my-votes').addEventListener('click', openMyVotes);
  $('later-copy').addEventListener('click', copyLinkFromLater);
  $('email-form').addEventListener('submit', submitEmail);
  $('later-back').addEventListener('click', () => {
    if (state.card) {
      loadCard(state.card.position);
    } else {
      loadCard();
    }
  });
  $('review').addEventListener('click', openMyVotes);
}

async function start() {
  bindEvents();
  const params = new URLSearchParams(window.location.search);
  if (params.get('link') === 'invalid') {
    try { localStorage.removeItem('tfm-voter-resume'); } catch (_) {}
    $('link-notice').hidden = false;
    window.history.replaceState(null, '', '/');
  }
  let me;
  try {
    me = await api('GET', '/api/me');
  } catch (err) {
    handleFailure(err, start);
    return;
  }
  state.votingOpen = me.voting_open;
  if (me.voter) {
    rememberSession();
    $('closed-my-votes').hidden = false;
    // A returning voter skips the animation and goes straight to their next player.
    if (state.votingOpen) {
      loadCard();
    } else {
      openMyVotes();
    }
    return;
  }
  let saved;
  try { saved = localStorage.getItem('tfm-voter-resume'); } catch (_) {}
  const savedPath = saved && resumePath(saved);
  if (savedPath) {
    try { localStorage.removeItem('tfm-voter-resume'); } catch (_) {}
    location.replace(savedPath);
    return;
  }
  playWelcome(showUsername);
}

start();
