// Common Ground in the browser: capture speech, stream it to the server, and draw
// the transcript, verdicts and perspectives as events arrive. No build step, no framework.

const $ = (selector, root = document) => root.querySelector(selector);

function h(tag, className, text, attrs) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  if (attrs) for (const [key, value] of Object.entries(attrs)) if (value != null) node.setAttribute(key, value);
  return node;
}

const VERDICT = {
  true: { label: 'True', icon: '✓' },
  false: { label: 'False', icon: '✕' },
  misleading: { label: 'Misleading', icon: '!' },
  unverifiable: { label: 'Unverified', icon: '?' },
};
const KIND = { contested: { label: 'Contested', icon: '⇄' }, value: { label: 'Value', icon: '◆' } };
const STANCE = { disagrees: 'Disagrees', agrees: 'Agrees', complicates: 'Complicates' };
const SPEAKER_COLORS = ['#f472b6', '#38bdf8', '#fbbf24', '#34d399', '#c084fc', '#fb923c'];
const MODEL_NAMES = { 'claude-sonnet-5-5': 'Claude Sonnet 5.5', 'claude-opus-5-5': 'Claude Opus 5.5', 'claude-haiku-5-5': 'Claude Haiku 5.5' };

const state = {
  config: {},
  ws: null,
  connected: false,
  lines: new Map(),
  claims: new Map(),
  speakers: new Map(),
  lastBubble: null,
  t0: null,
  voices: [],
  filter: 'all',
  sound: false,
  running: null,
  cued: null, // a video chosen and shown, waiting for Play
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch {
    return null;
  }
  return value;
}

function safeUrl(url) {
  return /^https?:\/\//i.test(url || '') ? url : null; // sources come from the web: no javascript: links
}

function domain(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}

function initials(name) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join('');
}

function personColor(name) {
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.codePointAt(0)) % 360;
  return `hsl(${hash}, 45%, 42%)`;
}

function clock(t) {
  state.t0 ??= t;
  const s = Math.max(0, Math.round(t - state.t0));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

// ---------- connection ----------

function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.addEventListener('open', () => {
    state.connected = true;
    renderStatus();
    if (state.voices.length) send({ type: 'voices', names: state.voices });
    for (const [raw, s] of state.speakers) if (s.label !== raw) send({ type: 'rename', speaker: raw, name: s.label });
  });
  ws.addEventListener('close', () => {
    state.connected = false;
    renderStatus();
    if (state.running) stop();
    setTimeout(connect, 1000);
  });
  ws.addEventListener('message', (event) => handle(JSON.parse(event.data)));
  state.ws = ws;
}

function send(message) {
  if (state.ws?.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(message));
}

function say(speakerName, text) {
  if (text) send({ type: 'say', speaker: speakerName, text });
}

function handle(event) {
  switch (event.type) {
    case 'line': return addLine(event);
    case 'caption': return setCaption(event.speaker, event.text);
    case 'claim': return addClaim(event);
    case 'verdict': return setVerdict(event);
    case 'debate': return setDebate(event);
    case 'perspectives': return setPerspectives(event);
    case 'pushback_failed': return pushbackFailed(event);
    case 'speaker_guess': return suggestName(event);
    case 'notice': return toast(event.message, 'notice');
    case 'error': return toast(event.message, 'error');
    default: return undefined;
  }
}

// ---------- speakers ----------

function speaker(raw) {
  if (!state.speakers.has(raw)) {
    state.speakers.set(raw, { label: raw, color: SPEAKER_COLORS[state.speakers.size % SPEAKER_COLORS.length] });
    const list = $('#speaker-list');
    list.append(h('option', null, null, { value: raw }));
  }
  return state.speakers.get(raw);
}

function renameSpeaker(raw) {
  const s = speaker(raw);
  const name = prompt(`Who is ${s.label}?`, s.label === raw ? '' : s.label);
  if (name && name.trim()) applyName(raw, name);
}

// Diarization separates voices but can't name them; the server suggests names from cues like
// "Thanks, Yann", and a person confirms.
function suggestName(event) {
  if (speaker(event.speaker).label !== event.speaker) return; // already named by hand
  const card = h('div', 'toast suggest');
  card.append(h('div', 'suggest-title', `Name ${event.speaker} “${event.name}”?`), h('div', 'evidence', `From the conversation: “${event.evidence}”`));
  const row = h('div', 'row');
  const yes = h('button', null, 'Use it', { type: 'button' });
  const no = h('button', null, 'No', { type: 'button' });
  yes.addEventListener('click', () => { applyName(event.speaker, event.name); card.remove(); });
  no.addEventListener('click', () => card.remove());
  row.append(yes, no);
  card.append(row);
  $('#toasts').append(card);
  setTimeout(() => card.remove(), 30000);
}

function applyName(raw, name) {
  const s = speaker(raw);
  s.label = name.trim();
  send({ type: 'rename', speaker: raw, name: s.label });
  document.querySelectorAll('[data-speaker-label]').forEach((node) => {
    if (node.dataset.speakerLabel === raw) node.textContent = s.label;
  });
  renderScoreboard();
}

// ---------- transcript ----------

function addLine(event) {
  $('#transcript-empty')?.remove();
  const line = { ...event, marks: new Map(), node: h('span', 'line') };
  state.lines.set(event.id, line);
  let bubble = state.lastBubble;
  if (!bubble || bubble.dataset.speaker !== event.speaker) {
    const s = speaker(event.speaker);
    bubble = h('div', 'bubble');
    bubble.dataset.speaker = event.speaker;
    const name = h('button', 'speaker-name', s.label, { type: 'button', title: 'Click to name this speaker' });
    name.dataset.speakerLabel = event.speaker;
    name.style.color = s.color;
    name.addEventListener('click', () => renameSpeaker(event.speaker));
    const head = h('div', 'bubble-head');
    head.append(name, h('span', 'time', clock(event.t)));
    bubble.append(head, h('p', 'bubble-text'));
    $('#transcript').append(bubble);
    state.lastBubble = bubble;
  }
  $('.bubble-text', bubble).append(line.node, ' ');
  renderLine(line);
  setCaption('', '');
  scrollTranscript();
}

function findQuote(text, quote) {
  if (!quote) return null;
  let at = text.indexOf(quote);
  if (at < 0) at = text.toLowerCase().indexOf(quote.toLowerCase());
  if (at >= 0) return [at, at + quote.length];
  // Same words, different punctuation or spacing.
  const words = quote.match(/[\p{L}\p{N}']+/gu);
  if (!words) return null;
  const escaped = words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const match = new RegExp(escaped.join("[^\\p{L}\\p{N}']+"), 'iu').exec(text);
  return match ? [match.index, match.index + match[0].length] : null;
}

function markClass(claim) {
  if (claim.dismissed) return 'dismissed';
  if (claim.kind !== 'fact') return `m-${claim.kind}`;
  if (!claim.verdict) return 'm-checking';
  return `m-${claim.verdict.verdict}${claim.verdict.provisional ? ' provisional' : ''}`;
}

function renderLine(line) {
  const spans = [];
  for (const [id, quote] of line.marks) {
    const at = findQuote(line.text, quote);
    if (at) spans.push({ id, start: at[0], end: at[1] });
  }
  spans.sort((a, b) => a.start - b.start);
  line.node.replaceChildren();
  let pos = 0;
  for (const span of spans) {
    if (span.start < pos) continue; // overlapping quotes: the first one wins
    const claim = state.claims.get(span.id);
    line.node.append(line.text.slice(pos, span.start));
    const mark = h('mark', `claim-mark ${markClass(claim)}`, line.text.slice(span.start, span.end), { title: claim.claim });
    mark.dataset.claim = span.id;
    mark.addEventListener('click', () => focusCard(span.id));
    line.node.append(mark);
    pos = span.end;
  }
  line.node.append(line.text.slice(pos));
}

function refreshMark(claim, flash = false) {
  const line = state.lines.get(claim.line_id);
  if (!line) return;
  renderLine(line);
  if (flash) line.node.querySelector(`mark[data-claim="${claim.id}"]`)?.classList.add('flash');
}

function setCaption(who, text) {
  const box = $('#caption');
  box.hidden = !text;
  if (!text) return;
  $('.caption-speaker', box).textContent = who ? speaker(who).label : '';
  $('.caption-text', box).textContent = text;
}

function scrollTranscript() {
  const box = $('#transcript');
  box.scrollTop = box.scrollHeight;
}

// ---------- claims ----------

function addClaim(event) {
  $('#cards-empty')?.remove();
  const claim = { ...event, verdict: null, perspectives: null, crux: '', card: h('article', 'card') };
  claim.card.dataset.claim = event.id;
  state.claims.set(event.id, claim);
  const line = state.lines.get(event.line_id);
  if (line) {
    line.marks.set(event.id, event.quote);
    renderLine(line);
  }
  $('#cards').prepend(claim.card);
  renderCard(claim);
  updateStats();
}

function setVerdict(event) {
  const claim = state.claims.get(event.claim_id);
  if (!claim) return;
  claim.verdict = event;
  claim.pushbackPending = false;
  const alarming = !event.provisional && !event.repeat && (event.verdict === 'false' || event.verdict === 'misleading');
  refreshMark(claim, alarming);
  renderCard(claim, alarming);
  updateStats();
  const source = event.sources?.[0];
  if (event.pushback) {
    const now = VERDICT[event.verdict].label;
    chyron(event.verdict, event.pushback.changed ? 'Changed' : 'Held', `${claim.claim} → ${now}`, event.pushback.note);
  } else if (alarming) {
    const detail = [event.correction || event.explanation, source && domain(source.url)].filter(Boolean).join('  ·  ');
    chyron(event.verdict, `${VERDICT[event.verdict].icon} ${VERDICT[event.verdict].label}`, claim.claim, detail);
    beep();
  } else if (stageOn() && !event.provisional && !event.repeat) {
    // Over a video, like a broadcast fact-check, every settled claim gets its moment.
    const detail = [event.explanation, source && domain(source.url)].filter(Boolean).join('  ·  ');
    chyron(event.verdict, `${VERDICT[event.verdict].icon} ${VERDICT[event.verdict].label}`, claim.claim, detail, 4500);
  }
}

function setDebate(event) {
  const claim = state.claims.get(event.claim_id);
  if (!claim) return;
  claim.kind = event.kind;
  claim.crux = event.crux;
  refreshMark(claim);
  renderCard(claim);
  updateStats();
}

function setPerspectives(event) {
  const claim = state.claims.get(event.claim_id);
  if (!claim) return;
  claim.perspectives = event.perspectives;
  claim.perspectivesNote = event.note;
  claim.perspectivesTimings = event.timings;
  renderCard(claim);
  updateStageStatus();
  const lead = event.perspectives.find((p) => state.voices.includes(p.person)) ?? event.perspectives[0];
  if (lead) {
    chyron('contested', `⇄ ${lead.person}`, `“${lead.quote}”`, `${STANCE[lead.stance]} with ${speaker(claim.speaker).label}  ·  ${domain(lead.source?.url || '')}`);
  }
}

function pushbackFailed(event) {
  const claim = state.claims.get(event.claim_id);
  if (claim) {
    claim.pushbackPending = false;
    renderCard(claim);
  }
  toast(event.message, 'error');
}

function matchesFilter(claim) {
  return state.filter === 'all' || claim.kind === state.filter;
}

function renderCard(claim, fresh = false) {
  const v = claim.verdict;
  const tone = claim.kind !== 'fact' ? `k-${claim.kind}` : v ? `v-${v.verdict}` : 'checking';
  const flags = [v?.provisional && 'provisional', claim.dismissed && 'dismissed', fresh && 'fresh'].filter(Boolean);
  claim.card.className = ['card', tone, ...flags].join(' ');
  claim.card.hidden = !matchesFilter(claim);
  const text = h('p', 'card-claim', claim.claim, { title: 'Show in transcript' });
  text.addEventListener('click', () => refreshMark(claim, true));
  claim.card.replaceChildren(cardHead(claim), text);
  if (claim.kind === 'fact') factBody(claim);
  else debateBody(claim);
}

function cardHead(claim) {
  const v = claim.verdict;
  let label = 'Checking';
  let icon = '';
  if (claim.kind !== 'fact') ({ label, icon } = KIND[claim.kind]);
  else if (v) ({ label, icon } = VERDICT[v.verdict]);
  if (v?.provisional) label += ' · first read';
  const badge = h('span', 'badge');
  badge.append(h('span', 'ico', icon), label);
  const s = speaker(claim.speaker);
  const who = h('span', 'card-speaker', s.label);
  who.dataset.speakerLabel = claim.speaker;
  who.style.color = s.color;
  const head = h('div', 'card-head');
  head.append(badge, who);
  const chip = latencyChip(claim.kind === 'fact' ? v?.timings : claim.perspectivesTimings);
  if (chip) head.append(chip);
  return head;
}

function latencyChip(t) {
  // Only measured timings are ever shown, which means live mode.
  if (!t || !state.config.timings_measured) return null;
  const shown = t.since_speech_ms ?? t.total_ms;
  if (shown == null) return null;
  const chip = h('span', `latency${shown < 2500 ? ' fast' : ''}`, `⚡ ${(shown / 1000).toFixed(1)}s`);
  const parts = [];
  if (t.search_ms) parts.push(`search ${t.search_ms} ms`);
  if (t.llm_ms) parts.push(`Claude ${t.llm_ms} ms`);
  if (t.total_ms != null) parts.push(`${t.total_ms} ms from sentence to screen`);
  if (t.since_speech_ms != null) parts.push(`${t.since_speech_ms} ms after the words were spoken`);
  chip.title = parts.join(' · ');
  return chip;
}

function factBody(claim) {
  const card = claim.card;
  const v = claim.verdict;
  if (!v) {
    card.append(h('p', 'working', 'Searching and checking…'));
    return;
  }
  if (v.explanation) card.append(h('p', 'card-text', v.explanation));
  if (v.correction && (v.verdict === 'false' || v.verdict === 'misleading')) card.append(h('p', 'correction', v.correction));
  if (v.leaning) {
    const lean = VERDICT[v.leaning]?.label.toLowerCase() ?? v.leaning;
    card.append(h('p', 'leaning', `Leans ${lean}, but no source backed it up. No receipts, no verdict.`));
  }
  if (v.provisional) card.append(h('p', 'working', 'First read from what Claude already knows. Confirming with a targeted search…'));
  if (v.sources?.length) card.append(sourceList(v.sources));
  if (v.repeat) card.append(h('p', 'leaning', 'Said before: same verdict as last time.'));
  if (v.pushback) card.append(pushbackNote(v));
  if (claim.pushbackPending) card.append(h('p', 'working', 'Re-checking with a deeper web search…'));
  else if (!v.provisional) card.append(factActions(claim));
}

function sourceList(sources) {
  const list = h('ul', 'sources');
  for (const s of sources.slice(0, 3)) {
    const item = h('li', 'source');
    item.append(h('span', 'domain', domain(s.url)));
    item.append(h('a', null, s.title || s.url, { href: safeUrl(s.url), target: '_blank', rel: 'noopener noreferrer' }));
    if (s.published) item.append(h('span', 'time', ` · ${s.published}`));
    if (s.snippet) item.append(h('span', 'snippet', s.snippet));
    list.append(item);
  }
  return list;
}

function pushbackNote(v) {
  const box = h('div', 'pushback-note');
  const before = VERDICT[v.pushback.previous]?.label ?? v.pushback.previous;
  const now = VERDICT[v.verdict]?.label ?? v.verdict;
  box.append(h('div', 'you', `Pushed back: “${v.pushback.argument}”`));
  box.append(h('div', 'change', v.pushback.changed ? `Changed: ${before} → ${now}` : `Held: still ${now}`));
  if (v.pushback.note) box.append(h('div', null, v.pushback.note));
  return box;
}

function factActions(claim) {
  const row = h('div', 'actions');
  const push = h('button', null, 'Push back', { type: 'button' });
  push.addEventListener('click', () => openPushback(claim));
  const dismiss = h('button', null, claim.dismissed ? 'Restore' : 'Dismiss', {
    type: 'button',
    title: 'Humans get the last word: drop this verdict from the tally',
  });
  dismiss.addEventListener('click', () => {
    claim.dismissed = !claim.dismissed;
    refreshMark(claim);
    renderCard(claim);
    updateStats();
  });
  row.append(push, dismiss);
  return row;
}

function openPushback(claim) {
  const form = h('form', 'pushback-form');
  const area = h('textarea', null, null, { placeholder: 'What does this verdict get wrong?', 'aria-label': 'Your objection' });
  const row = h('div', 'row');
  const cancel = h('button', null, 'Cancel', { type: 'button' });
  row.append(cancel, h('button', null, 'Re-check', { type: 'submit' }));
  form.append(area, row);
  cancel.addEventListener('click', () => renderCard(claim));
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const argument = area.value.trim();
    if (!argument) return;
    claim.pushbackPending = true;
    send({ type: 'pushback', claim_id: claim.id, argument });
    renderCard(claim);
  });
  $('.actions', claim.card)?.replaceWith(form);
  area.focus();
}

function debateBody(claim) {
  const card = claim.card;
  if (claim.crux) {
    const crux = h('p', 'crux');
    crux.append(h('b', null, claim.kind === 'value' ? 'A value call: ' : 'The crux: '), claim.crux);
    card.append(crux);
  }
  if (claim.perspectives == null) {
    const first = state.voices.length ? `, ${state.voices.join(', ')} first` : '';
    card.append(h('p', 'working', `Finding how others see it${first}…`));
    return;
  }
  if (!claim.perspectives.length) {
    card.append(h('p', 'no-takes', claim.perspectivesNote || 'No sourced takes found. This one is yours to weigh.'));
    return;
  }
  const takes = h('div', 'takes');
  claim.perspectives.forEach((p, i) => {
    const take = h('div', `take s-${p.stance}`);
    take.style.animationDelay = `${i * 90}ms`;
    const head = h('div', 'take-head');
    const avatar = h('span', 'avatar', initials(p.person));
    avatar.style.background = personColor(p.person);
    head.append(avatar, h('span', 'person', p.person), h('span', 'stance', STANCE[p.stance] ?? p.stance));
    take.append(head, h('p', 'take-summary', p.summary), h('blockquote', 'take-quote', `“${p.quote}”`));
    const src = p.source || {};
    const where = [domain(src.url || ''), src.published, src.title].filter(Boolean).join(' · ');
    take.append(h('a', 'take-source', `Source: ${where} ↗`, { href: safeUrl(src.url), target: '_blank', rel: 'noopener noreferrer' }));
    takes.append(take);
  });
  card.append(takes);
}

function focusCard(id) {
  const claim = state.claims.get(id);
  if (!claim) return;
  if (!matchesFilter(claim)) setFilter('all');
  claim.card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  claim.card.classList.add('focus');
  setTimeout(() => claim.card.classList.remove('focus'), 1500);
}

function setFilter(filter) {
  state.filter = filter;
  document.querySelectorAll('#filters button').forEach((b) => b.classList.toggle('on', b.dataset.filter === filter));
  for (const claim of state.claims.values()) claim.card.hidden = !matchesFilter(claim);
}

// ---------- tallies ----------

function updateStats() {
  const all = [...state.claims.values()].filter((c) => !c.dismissed);
  const settled = all.filter((c) => c.kind === 'fact' && c.verdict && !c.verdict.provisional);
  const count = (verdict) => settled.filter((c) => c.verdict.verdict === verdict).length;
  const times = settled
    .filter((c) => !c.verdict.repeat && !c.verdict.pushback)
    .map((c) => c.verdict.timings?.since_speech_ms ?? c.verdict.timings?.total_ms)
    .filter((t) => t != null)
    .sort((a, b) => a - b);
  const items = [
    [settled.length, 'facts checked'],
    [count('false'), 'false'],
    [count('misleading'), 'misleading'],
    [all.filter((c) => c.kind !== 'fact').length, 'contested & values'],
  ];
  updateStageStatus();
  if (times.length && state.config.timings_measured) {
    items.push([`${(times[Math.floor((times.length - 1) / 2)] / 1000).toFixed(1)}s`, 'median to verdict']);
  }
  $('#stats').replaceChildren(...items.map(([n, label]) => {
    const span = h('span');
    span.append(h('b', null, String(n)), ` ${label}`);
    return span;
  }));
  renderScoreboard();
}

function renderScoreboard() {
  const tally = new Map();
  for (const claim of state.claims.values()) {
    if (claim.dismissed) continue;
    const key = claim.kind !== 'fact' ? claim.kind : claim.verdict && !claim.verdict.provisional ? claim.verdict.verdict : null;
    if (!key) continue;
    const row = tally.get(claim.speaker) ?? {};
    row[key] = (row[key] ?? 0) + 1;
    tally.set(claim.speaker, row);
  }
  const order = [['true', '✓', 'var(--true)'], ['false', '✕', 'var(--false)'], ['misleading', '!', 'var(--misleading)'],
    ['unverifiable', '?', 'var(--unverifiable)'], ['contested', '⇄', 'var(--contested)'], ['value', '◆', 'var(--value)']];
  $('#scoreboard').replaceChildren(...[...tally].map(([raw, row]) => {
    const s = speaker(raw);
    const box = h('div', 'score');
    const who = h('span', 'who', s.label);
    who.dataset.speakerLabel = raw;
    who.style.color = s.color;
    box.append(who);
    for (const [key, icon, color] of order) {
      if (!row[key]) continue;
      const n = h('span', 'n', `${icon}${row[key]}`, { title: key });
      n.style.color = color;
      box.append(n);
    }
    return box;
  }));
}

// ---------- chyron, toasts, sound ----------

// The lower third shows one announcement at a time, so verdicts in quick succession queue up
// instead of overwriting each other. A backlog is shown faster so it never falls far behind.
const ticker = { queue: [], timer: null };
function chyron(tone, tag, title, detail, ms = 6500) {
  ticker.queue.push({ tone, tag, title, detail, ms });
  if (ticker.queue.length > 4) ticker.queue.splice(0, ticker.queue.length - 4);
  if (!ticker.timer) nextChyron();
}

function nextChyron() {
  const box = $('#chyron');
  const item = ticker.queue.shift();
  if (!item) {
    ticker.timer = null;
    box.hidden = true;
    return;
  }
  const body = h('div', 'body');
  body.append(h('div', 'title', item.title));
  if (item.detail) body.append(h('div', 'detail', item.detail));
  box.className = `chyron t-${item.tone}`;
  box.replaceChildren(h('div', 'tag', item.tag), body);
  box.hidden = false;
  ticker.timer = setTimeout(nextChyron, ticker.queue.length ? Math.min(item.ms, 3500) : item.ms);
}

function toast(message, kind = 'notice') {
  const box = $('#toasts');
  if (box.lastElementChild?.textContent === message) return;
  const note = h('div', `toast ${kind}`, message);
  box.append(note);
  setTimeout(() => note.remove(), kind === 'error' ? 9000 : 5000);
}

let audioOut;
function beep() {
  if (!state.sound) return;
  audioOut ??= new AudioContext();
  const now = audioOut.currentTime;
  [523, 392].forEach((freq, i) => {
    const osc = audioOut.createOscillator();
    const gain = audioOut.createGain();
    const at = now + i * 0.13;
    osc.type = 'triangle';
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(0.18, at + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.12);
    osc.connect(gain).connect(audioOut.destination);
    osc.start(at);
    osc.stop(at + 0.13);
  });
}

// ---------- status and settings ----------

function renderStatus() {
  const c = state.config;
  const pill = (text, cls = '') => h('span', `pill ${cls}`, text);
  const pills = [];
  if (c.test_data) pills.push(pill('Test data, not model output', 'warn')); // only under tests/serve.py
  pills.push(pill(`${MODEL_NAMES[c.model] ?? c.model ?? '…'}${c.fast_mode ? ' · fast' : ''}`));
  const evidence = { exa: 'Exa search', 'claude-web-search': 'Claude web search' }[c.evidence];
  if (evidence) pills.push(pill(`Evidence: ${evidence}`));
  const speech = c.server_stt ? `Speech: ${c.stt_name ?? 'server'}` : 'Speech: browser only';
  pills.push(pill(speech, c.server_stt ? '' : 'off'));
  pills.push(pill(state.connected ? '● Connected' : '○ Reconnecting', state.connected ? 'live' : 'warn'));
  $('#status').replaceChildren(...pills);
}

function renderVoices() {
  $('#voice-chips').replaceChildren(...state.voices.map((name) => {
    const chip = h('span', 'chip', name);
    const remove = h('button', null, '×', { type: 'button', 'aria-label': `Remove ${name}` });
    remove.addEventListener('click', () => setVoices(state.voices.filter((v) => v !== name)));
    chip.append(remove);
    return chip;
  }));
}

function setVoices(names) {
  state.voices = [...new Set(names.map((n) => n.trim()).filter(Boolean))].slice(0, 4);
  renderVoices();
  send({ type: 'voices', names: state.voices });
}

function hasBrowserSpeech() {
  return Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
}

function buildSourceMenu() {
  const stt = state.config.stt_name ?? 'server speech-to-text';
  const options = [
    ['mic', `Microphone (${stt}, tells speakers apart)`, state.config.server_stt],
    // A clip plays without speech-to-text too; it just isn't checked.
    ['video', state.config.server_stt ? `Video file, plays here (${stt})` : 'Video file, plays here (not checked: no speech-to-text)', true],
    ['tab', `Browser tab or video (${stt})`, state.config.server_stt],
    ['browser', 'Microphone (browser speech, Chrome)', hasBrowserSpeech()],
  ];
  $('#source').replaceChildren(...options.map(([value, label, ok]) => {
    const option = h('option', null, ok ? label : `${label}: unavailable`, { value });
    option.disabled = !ok;
    return option;
  }));
  const mic = options.find(([value, , ok]) => ok && value === 'mic');
  $('#source').value = (mic ?? options.find(([, , ok]) => ok) ?? options[0])[0];
}

// ---------- input sources ----------

// Streams an audio source to the server as 16 kHz PCM; returns a function that stops it.
async function pipeAudio(makeSource, { audible = false } = {}) {
  const ctx = new AudioContext();
  await ctx.audioWorklet.addModule('/static/pcm-worklet.js');
  const pcm = new AudioWorkletNode(ctx, 'pcm16k');
  const silent = ctx.createGain();
  silent.gain.value = 0; // keeps the worklet running without playing its input a second time
  const source = makeSource(ctx);
  source.connect(pcm).connect(silent).connect(ctx.destination);
  if (audible) source.connect(ctx.destination); // a clip playing in the page still has to be heard
  pcm.port.onmessage = (event) => {
    if (state.ws?.readyState === WebSocket.OPEN) state.ws.send(event.data);
  };
  await ctx.resume();
  send({ type: 'start_audio' });
  return () => {
    send({ type: 'stop_audio' });
    pcm.port.onmessage = null;
    ctx.close();
  };
}

async function startCapture(kind) {
  const media = kind === 'tab'
    ? await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true })
    : await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  const tracks = media.getAudioTracks();
  if (!tracks.length) {
    media.getTracks().forEach((t) => t.stop());
    throw new Error('That share has no audio. Pick a browser tab and tick "Share tab audio".');
  }
  const stopAudio = await pipeAudio((ctx) => ctx.createMediaStreamSource(new MediaStream(tracks)));
  if (media.getVideoTracks().length) {
    const video = h('video', null, null, { autoplay: '', playsinline: '' });
    video.muted = true; // the shared tab is already playing the sound
    video.srcObject = new MediaStream(media.getVideoTracks());
    showStage(video);
  }
  tracks[0].addEventListener('ended', () => stop());
  return () => {
    stopAudio();
    media.getTracks().forEach((t) => t.stop());
    hideStage();
  };
}

// ---------- the stage: a clip playing in the page, with verdicts over it ----------

function stageOn() {
  return !$('#stage').hidden;
}

function showStage(video) {
  $('#stage-media').replaceChildren(video);
  $('#stage').hidden = false;
  $('#stage').append($('#chyron')); // the lower third moves onto the picture
  $('.transcript-pane').classList.add('staged');
  updateStageStatus();
}

function hideStage() {
  if (!stageOn()) return;
  $('#stage').hidden = true;
  $('#stage-media').replaceChildren();
  $('.transcript-pane').append($('#chyron'));
  $('.transcript-pane').classList.remove('staged');
}

function pendingClaims() {
  return [...state.claims.values()].filter((c) => !c.dismissed
    && (c.kind === 'fact' ? !c.verdict || c.verdict.provisional : c.perspectives == null)).length;
}

function updateStageStatus() {
  const pending = pendingClaims();
  $('#stage-status .text').textContent = pending ? `Listening · checking ${pending}` : 'Listening';
}

function pickVideoFile() {
  return new Promise((resolve, reject) => {
    const input = $('#video-file');
    input.value = '';
    input.onchange = () => (input.files[0] ? resolve(input.files[0]) : reject(new Error('No video chosen.')));
    input.oncancel = () => reject(new Error('No video chosen.'));
    input.click();
  });
}

// First press: choose a clip and cue it, paused on its first frame. Second press plays it and
// starts checking; being a fresh click, it is always allowed to play sound.
async function cueVideo() {
  const file = await pickVideoFile();
  const video = h('video', null, null, { controls: '', playsinline: '', preload: 'auto' });
  video.src = URL.createObjectURL(file);
  showStage(video);
  state.cued = video;
  $('.label', $('#start')).textContent = 'Play';
}

async function playCuedVideo() {
  const video = state.cued;
  state.cued = null;
  const stopAudio = state.config.server_stt
    ? await pipeAudio((ctx) => ctx.createMediaElementSource(video), { audible: true })
    : () => {};
  if (!state.config.server_stt) toast("No server speech-to-text is set up, so the clip plays but what it says isn't checked.", 'notice');
  video.addEventListener('ended', () => {
    if (state.running) stop();
    showRecapWhenSettled();
  }, { once: true });
  await video.play();
  return () => {
    stopAudio();
    video.pause();
    URL.revokeObjectURL(video.src);
    hideStage();
  };
}

function uncue() {
  if (!state.cued) return;
  URL.revokeObjectURL(state.cued.src);
  state.cued = null;
  hideStage();
  $('.label', $('#start')).textContent = 'Start';
}

function startBrowserSpeech() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const rec = new Recognition();
  rec.continuous = true;
  rec.interimResults = true;
  rec.lang = 'en-US';
  let active = true;
  const who = () => $('#say-speaker').value.trim() || 'Me';
  rec.onresult = (event) => {
    let interim = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      if (result.isFinal) say(who(), result[0].transcript.trim());
      else interim += result[0].transcript;
    }
    setCaption(who(), interim.trim());
  };
  rec.onerror = (event) => {
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      toast('Microphone access was blocked.', 'error');
      stop();
    }
  };
  rec.onend = () => { if (active) rec.start(); }; // Chrome ends recognition after a silence; keep listening
  rec.start();
  return () => {
    active = false;
    rec.stop();
  };
}

async function start() {
  if (state.running) {
    stop();
    return;
  }
  const choice = $('#source').value;
  try {
    if (choice === 'video' && !state.cued) {
      await cueVideo();
      return;
    }
    let stopper;
    if (choice === 'browser') stopper = startBrowserSpeech();
    else if (choice === 'video') stopper = await playCuedVideo();
    else stopper = await startCapture(choice);
    state.running = { stop: stopper };
    setRunning(true);
  } catch (err) {
    toast(err.message || String(err), 'error');
    stop();
  }
}

function stop() {
  state.running?.stop();
  state.running = null;
  setRunning(false);
  setCaption('', '');
}

function setRunning(on) {
  const button = $('#start');
  button.classList.toggle('running', on);
  $('.label', button).textContent = on ? 'Stop' : 'Start';
  $('#source').disabled = on;
}

// ---------- common ground: what's settled, and where people differ ----------

async function showRecapWhenSettled(maxMs = 20000) {
  const started = performance.now();
  while (pendingClaims() && performance.now() - started < maxMs) await sleep(250);
  if (state.claims.size) openRecap();
}

function recapWho(raw) {
  const s = speaker(raw);
  const who = h('span', 'who', ` ${s.label}`);
  who.style.color = s.color;
  return who;
}

function recapList(target, claims, render, empty) {
  $(target).replaceChildren(...(claims.length
    ? claims.map((claim, i) => {
      const item = h('li');
      item.style.animationDelay = `${i * 70}ms`;
      render(item, claim);
      return item;
    })
    : [h('li', 'recap-empty', empty)]));
}

function openRecap() {
  const claims = [...state.claims.values()].filter((c) => !c.dismissed);
  const order = { false: 0, misleading: 1, true: 2, unverifiable: 3 };
  const facts = claims.filter((c) => c.kind === 'fact' && c.verdict && !c.verdict.provisional)
    .sort((a, b) => order[a.verdict.verdict] - order[b.verdict.verdict]); // what was wrong comes first
  const open = claims.filter((c) => c.kind === 'contested');
  const values = claims.filter((c) => c.kind === 'value');
  recapList('#recap-facts', facts, (item, c) => {
    const v = c.verdict;
    item.append(h('span', `tag v-${v.verdict}`, `${VERDICT[v.verdict].icon} ${VERDICT[v.verdict].label}`), c.claim, recapWho(c.speaker));
    if (v.correction && (v.verdict === 'false' || v.verdict === 'misleading')) item.append(h('span', 'fix', `→ ${v.correction}`));
  }, 'No facts checked yet.');
  recapList('#recap-questions', open, (item, c) => {
    item.append(c.claim, recapWho(c.speaker));
    const voices = h('div', 'voices');
    for (const p of c.perspectives ?? []) voices.append(h('span', 'voice', `${p.person} · ${(STANCE[p.stance] ?? p.stance).toLowerCase()}`));
    if (voices.childElementCount) item.append(voices);
  }, 'No contested claims yet.');
  recapList('#recap-values', values, (item, c) => item.append(c.claim, recapWho(c.speaker)), 'No value claims yet.');
  const falses = facts.filter((c) => ['false', 'misleading'].includes(c.verdict.verdict)).length;
  $('#recap-sub').textContent = `${plural(facts.length, 'fact')} checked, ${falses} false or misleading · `
    + `${plural(open.length, 'open question')} · ${plural(values.length, 'value')}`;
  $('#recap').hidden = false;
}

function closeRecap() {
  $('#recap').hidden = true;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

// ---------- wiring ----------

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  store('cg-theme', theme);
}

async function init() {
  applyTheme(store('cg-theme') || 'dark');
  state.config = await fetch('/api/config').then((r) => r.json());
  buildSourceMenu();
  renderStatus();
  updateStats();

  $('#start').addEventListener('click', start);
  $('#say').addEventListener('submit', (event) => {
    event.preventDefault();
    const text = $('#say-text').value.trim();
    if (!text) return;
    say($('#say-speaker').value.trim() || 'Me', text);
    $('#say-text').value = '';
  });
  $('#voice-input').addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    const added = event.target.value.split(',');
    event.target.value = '';
    setVoices([...state.voices, ...added]);
  });
  $('#filters').addEventListener('click', (event) => {
    if (event.target.dataset.filter) setFilter(event.target.dataset.filter);
  });
  $('#theme').addEventListener('click', () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
  $('#present').addEventListener('click', () => document.body.classList.toggle('present'));
  $('#sound').addEventListener('click', () => {
    state.sound = !state.sound;
    $('#sound').textContent = state.sound ? '🔔' : '🔇';
    if (state.sound) beep();
  });
  $('#recap-show').addEventListener('click', openRecap);
  $('#recap-close').addEventListener('click', closeRecap);
  $('#source').addEventListener('change', uncue);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeRecap();
    const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
    if (typing) return;
    if (event.key.toLowerCase() === 'p') document.body.classList.toggle('present');
    if (event.key.toLowerCase() === 's') {
      if ($('#recap').hidden) openRecap();
      else closeRecap();
    }
  });
  connect();
}

init();
