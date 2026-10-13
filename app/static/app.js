'use strict';

const $ = (id) => document.getElementById(id);
const START = 1500; // matches the server's prior, where uncompared items sit

const state = {
  voter: localStorage.getItem('voter') || '',
  category: localStorage.getItem('category') || 'all',
  pair: null,
  next: null,       // prefetched pair, so a vote swaps in instantly
  config: null,
  busy: false,
};

/* Mobile browsers shrink the visual viewport when the URL bar shows; 100vh
   ignores that and pushes the action buttons under it. */
const setVH = () => document.documentElement.style.setProperty('--vh', `${window.innerHeight / 100}px`);
setVH();
addEventListener('resize', setVH);
addEventListener('orientationchange', setVH);

const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
};

const post = (path, body) =>
  api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });

const esc = (t) => String(t).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

/* ---------------- boot ---------------- */

async function boot() {
  state.config = await api('/api/config');
  const cfg = state.config.settings;
  document.title = cfg.title;
  $('title').textContent = cfg.title;
  document.querySelector('meta[name=theme-color]').content = cfg.color;
  document.documentElement.style.setProperty('--red', cfg.color);
  if (cfg.icon) for (const l of document.querySelectorAll('link[rel=icon], link[rel=apple-touch-icon]')) l.href = cfg.icon;

  const cat = $('category');
  cat.hidden = state.config.categories.length === 0;
  if (cat.hidden) state.category = 'all';
  cat.innerHTML =
    `<option value="all">All ${esc(cfg.nouns)} (${state.config.counts.all})</option>` +
    state.config.categories
      .map((c) => `<option value="${esc(c)}">${esc(c)} (${state.config.counts[c]})</option>`)
      .join('');
  cat.value = state.category;
  cat.onchange = () => {
    state.category = cat.value;
    localStorage.setItem('category', state.category);
    state.next = null;
    loadPair();
    if (!$('view-rank').hidden) loadRanking();
  };

  if (!state.voter) askWho();
  else start();
}

function askWho() {
  const dlg = $('who');
  const existing = $('who-existing');
  existing.innerHTML = '';
  for (const v of state.config.voters) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = v;
    b.onclick = () => {
      $('who-name').value = v;
      dlg.close('ok');
      setVoter(v);
    };
    existing.append(b);
  }
  $('who-name').value = state.voter;
  dlg.showModal();
  dlg.querySelector('form').onsubmit = () => {
    const name = $('who-name').value.trim();
    if (name) setVoter(name);
  };
}

function setVoter(name) {
  state.voter = name;
  localStorage.setItem('voter', name);
  state.next = null;
  start();
}

function start() {
  $('voter-chip').textContent = state.voter;
  loadPair();
}

$('voter-chip').onclick = askWho;

/* ---------------- voting ---------------- */

function paint(p) {
  state.pair = p;
  const empty = $('vote-empty');
  const arena = $('arena');

  if (!p || !p.a) {
    arena.hidden = true;
    empty.hidden = false;
    empty.textContent = "That's every pair in this category — check the Ranking tab.";
  } else {
    arena.hidden = false;
    empty.hidden = true;
    for (const [side, f] of [['a', p.a], ['b', p.b]]) {
      const img = $(`img-${side}`);
      img.hidden = !f.image;
      if (f.image) img.src = f.image;
      img.alt = f.name;
      $(`name-${side}`).textContent = f.name;
      $(`cat-${side}`).textContent = state.category === 'all' ? f.category : '';
      const card = $(`card-${side}`);
      card.classList.remove('win', 'lose');
    }
  }

  const pr = (p && p.progress) || null;
  if (pr) {
    $('bar').style.width = `${pr.pct}%`;
    $('progress-text').textContent =
      pr.unseen ? `${pr.votes} picks · ${pr.unseen} ${state.config.settings.nouns} still unseen`
      : pr.phase === 'resolution' ? `${pr.votes} picks · settling the close calls`
      : `${pr.votes} picks · every ${state.config.settings.noun} seen at least ${pr.least_seen}×`;
  }
  $('btn-undo').disabled = !(pr && pr.votes);
}

async function loadPair() {
  const q = `?voter=${encodeURIComponent(state.voter)}&category=${encodeURIComponent(state.category)}`;
  paint(await api('/api/pair' + q));
  prefetch();
}

/* Ask for the following pair now and warm its images, so tapping a card shows
   the next one with no network wait or image pop-in. */
async function prefetch() {
  const q = `?voter=${encodeURIComponent(state.voter)}&category=${encodeURIComponent(state.category)}`;
  try {
    const p = await api('/api/pair' + q);
    state.next = p;
    for (const f of [p.a, p.b]) if (f && f.image) new Image().src = f.image;
  } catch {
    state.next = null;
  }
}

async function advance() {
  if (state.next && state.next.a) {
    paint(state.next);
    state.next = null;
    prefetch();
  } else {
    await loadPair();
  }
}

async function choose(side) {
  if (state.busy || !state.pair || !state.pair.a) return;
  state.busy = true;
  const arena = $('arena');
  arena.classList.add('busy');

  const winner = side === 'a' ? state.pair.a : state.pair.b;
  const loser = side === 'a' ? state.pair.b : state.pair.a;
  $(`card-${side}`).classList.add('win');
  $(`card-${side === 'a' ? 'b' : 'a'}`).classList.add('lose');

  try {
    await post('/api/vote', { voter: state.voter, winner: winner.slug, loser: loser.slug });
    await new Promise((r) => setTimeout(r, 140));
    await advance();
  } finally {
    arena.classList.remove('busy');
    state.busy = false;
  }
}

$('card-a').onclick = () => choose('a');
$('card-b').onclick = () => choose('b');

$('btn-undo').onclick = async () => {
  if (state.busy) return;
  state.busy = true;
  try {
    await post(`/api/undo?voter=${encodeURIComponent(state.voter)}`);
    state.next = null;
    await loadPair();
  } finally {
    state.busy = false;
  }
};

addEventListener('keydown', (e) => {
  if ($('view-vote').hidden || $('who').open) return;
  if (e.key === 'ArrowLeft' || e.key === '1') choose('a');
  else if (e.key === 'ArrowRight' || e.key === '2') choose('b');
  else if (e.key === 'u') $('btn-undo').click();
});

/* ---------------- ranking ---------------- */

async function loadRanking() {
  const sel = $('rank-voter');
  const cfg = await api('/api/config');
  const who = sel.value || state.voter;
  const voters = [...new Set([state.voter, ...cfg.voters])].filter(Boolean);
  sel.innerHTML =
    voters.map((v) => `<option value="${v}">${v === state.voter ? v + ' (you)' : v}</option>`).join('') +
    '<option value="everyone">Everyone combined</option>';
  sel.value = voters.includes(who) || who === 'everyone' ? who : state.voter;

  const q = `?voter=${encodeURIComponent(sel.value)}&category=${encodeURIComponent(state.category)}`;
  const data = await api('/api/ranking' + q);
  const items = data.items;
  const rated = items.filter((x) => x.n > 0);

  $('rank-empty').hidden = rated.length > 0;
  const max = Math.max(...rated.map((x) => x.rating), START);
  const min = Math.min(...rated.map((x) => x.rating), START);
  const span = Math.max(max - min, 1);

  const row = (x) => `<li class="${x.tasted ? '' : 'untasted'}">
      <span class="pos">${x.rank === null ? '·' : x.rank}</span>
      ${x.image ? `<img src="${x.image}" alt="" loading="lazy">` : ''}
      <span class="who">
        <b>${esc(x.name)}</b>
        ${x.tasted ? `<span class="meter"><i style="width:${Math.round((100 * (x.rating - min)) / span)}%"></i></span>` : ''}
      </span>
      <span class="rec">${x.tasted ? `${x.wins}\u2013${x.losses}` : ''}</span>
    </li>`;

  const untasted = items.filter((x) => !x.tasted);
  $('rank-list').innerHTML =
    rated.map(row).join('') +
    (untasted.length
      ? `<li class="divider"><span>Not compared yet · ${untasted.length}</span></li>` +
        untasted.map(row).join('')
      : '');

  $('rank-note').textContent = rated.length
    ? `${rated.length} ranked from ${data.total_votes} picks` +
      (untasted.length ? ` · ${untasted.length} to go` : ' · complete')
    : '';

  sel.onchange = loadRanking;
}

$('btn-reset').onclick = async () => {
  if (!confirm(`Delete all of ${state.voter}'s picks? This can't be undone.`)) return;
  await post(`/api/reset?voter=${encodeURIComponent(state.voter)}`);
  state.next = null;
  await loadRanking();
  await loadPair();
};

/* ---------------- tabs ---------------- */

for (const btn of document.querySelectorAll('.tabs button')) {
  btn.onclick = () => {
    for (const b of document.querySelectorAll('.tabs button')) b.classList.toggle('on', b === btn);
    const view = btn.dataset.view;
    $('view-vote').hidden = view !== 'vote';
    $('view-rank').hidden = view !== 'rank';
    if (view === 'rank') loadRanking();
    else loadPair();
  };
}

boot();
