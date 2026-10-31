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
  misleading: { label: 'Missing context', icon: '…' },
  unverifiable: { label: 'Unverified', icon: '?' },
};
const KIND = { contested: { label: 'Contested', icon: '⇄' }, value: { label: 'Value', icon: '◆' } };
const STANCE = { disagrees: 'Disagrees', agrees: 'Agrees', complicates: 'Complicates' };
const SPEAKER_COLORS = ['#c14c8a', '#337ea9', '#cb912f', '#9065b0', '#9f6b53', '#d9730d'];

const state = {
  config: {},
  ws: null,
  connected: false,
  lines: new Map(),
  claims: new Map(),
  speakers: new Map(),
  lastBubble: null,
  t0: null,
  running: null,
  pending: [], // messages sent while the connection was still opening
};

const empties = {}; // the empty-state hints, put back when the conversation is cleared

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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
  return `hsl(${hash}, 30%, 46%)`;
}

function clock(t) {
  state.t0 ??= t;
  const s = Math.max(0, Math.round(t - state.t0));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

// ---------- connection ----------

// One connection is one server session. Events from a connection that has been replaced
// (see clearConversation) are ignored.
function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  state.ws = ws;
  ws.addEventListener('open', () => {
    if (state.ws !== ws) return;
    state.connected = true;
    renderStatus();
    for (const [raw, s] of state.speakers) if (s.label !== raw) send({ type: 'rename', speaker: raw, name: s.label });
    for (const message of state.pending.splice(0)) send(message);
  });
  ws.addEventListener('close', () => {
    if (state.ws !== ws) return;
    state.connected = false;
    renderStatus();
    if (state.running) stop();
    setTimeout(connect, 1000);
  });
  ws.addEventListener('message', (event) => {
    if (state.ws === ws) handle(JSON.parse(event.data));
  });
}

function send(message) {
  if (state.ws?.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(message));
  else if (state.ws?.readyState === WebSocket.CONNECTING) state.pending.push(message);
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

// The transcript stays on its latest lines, unless someone has scrolled up to read back.
function scrollTranscript() {
  const box = $('#transcript');
  if (box.dataset.pinned !== 'false') box.scrollTop = box.scrollHeight;
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
  refreshMark(claim);
  renderCard(claim);
  // Every verdict lands the same way, true or false: the badge settles in. No rings, no flashing.
  if (!event.provisional) $('.badge', claim.card)?.classList.add('changed');
  updateStats();
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
  renderCard(claim);
}

function pushbackFailed(event) {
  const claim = state.claims.get(event.claim_id);
  if (claim) {
    claim.pushbackPending = false;
    renderCard(claim);
  }
  toast(event.message, 'error');
}

// A card leads with the words that were said. A plain fact then gets a small verdict and the sites
// checked; anything else gets its context: what's actually so, or the people who see it differently.
function renderCard(claim) {
  const v = claim.verdict;
  const tone = claim.kind !== 'fact' ? `k-${claim.kind}` : v ? `v-${v.verdict}` : 'checking';
  const flags = [v?.provisional && 'provisional'].filter(Boolean);
  claim.card.className = ['card', tone, ...flags].join(' ');
  const quote = h('blockquote', 'card-quote', quoted(claim), { title: 'Show in transcript' });
  quote.addEventListener('click', () => refreshMark(claim, true));
  claim.card.replaceChildren(cardMeta(claim), quote);
  if (claim.kind === 'fact') factBody(claim);
  else debateBody(claim);
}

function quoted(claim) {
  return `“${claim.quote || claim.claim}”`;
}

function cardMeta(claim) {
  const s = speaker(claim.speaker);
  const meta = h('div', 'card-meta');
  const dot = h('span', 'speaker-dot');
  dot.style.background = s.color;
  const who = h('span', 'card-speaker', s.label);
  who.dataset.speakerLabel = claim.speaker;
  meta.append(dot, who);
  const line = state.lines.get(claim.line_id);
  if (line) meta.append(h('span', 'time', clock(line.t)));
  return meta;
}

function badge(claim) {
  const v = claim.verdict;
  let label = 'Checking';
  let icon = '';
  if (claim.kind !== 'fact') ({ label, icon } = KIND[claim.kind]);
  else if (v) ({ label, icon } = VERDICT[v.verdict]);
  if (v?.provisional) label += ' · first read';
  const node = h('span', 'badge');
  node.append(h('span', 'ico', icon), label);
  return node;
}

function factBody(claim) {
  const card = claim.card;
  const v = claim.verdict;
  const row = h('div', 'verdict-row');
  row.append(badge(claim));
  card.append(row);
  if (!v) return;
  // True needs nothing but its sources; anything else says what's actually so.
  const context = v.verdict === 'true' ? '' : v.verdict === 'unverifiable' ? v.explanation : v.correction || v.explanation;
  const lean = v.leaning && `Leans ${VERDICT[v.leaning]?.label.toLowerCase() ?? v.leaning}, but no source confirmed it.`;
  const text = h('span', 'verdict-text', lean || context);
  // The sources sit inline at the end of the sentence, like a citation.
  if (v.sources?.length) text.append(text.textContent ? ' ' : '', sourceList(v.sources));
  if (text.childNodes.length) row.append(text);
  if (v.provisional) card.append(h('p', 'working', 'First read. Confirming with a search…'));
  if (v.repeat) card.append(h('p', 'note', 'Said before: same verdict as last time.'));
  if (v.pushback) card.append(pushbackNote(v));
  if (claim.pushbackPending) card.append(h('p', 'working', 'Re-checking with a deeper web search…'));
  else if (!v.provisional) {
    const foot = h('div', 'card-foot');
    foot.append(cardActions(claim, { pushBack: true }));
    card.append(foot);
  }
}

// ---------- sources: one quiet chip, the details on hover ----------

function siteName(url) {
  const parts = domain(url).split('.');
  if (parts.length > 1) parts.pop(); // nasa.gov -> nasa
  if (parts.length > 1 && ['co', 'com', 'ac', 'gov', 'org', 'net', 'edu'].includes(parts.at(-1))) parts.pop(); // bbc.co.uk -> bbc
  return parts.join('.');
}

function favicon(url) {
  const src = `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain(url))}&sz=64`;
  const img = h('img', null, null, { src, alt: '', referrerpolicy: 'no-referrer' });
  img.addEventListener('error', () => { img.hidden = true; }); // no logo is better than a broken one
  return img;
}

// Exa excerpts can carry the page's markdown (headings, table pipes); show them as plain text.
function plainSnippet(text) {
  return (text || '')
    .replace(/(^|\s)#{1,6}\s+/g, '$1')
    .replace(/\|?(\s*:?-{3,}:?\s*\|)+\s*:?-{0,}:?\s*/g, ' | ')
    .replace(/\s*\|\s*/g, ' · ')
    .replace(/\*\*|__|`/g, '')
    .replace(/(\s*·\s*){2,}/g, ' · ')
    .replace(/\s+/g, ' ')
    .replace(/^\s*·\s*|\s*·\s*$/g, '')
    .trim();
}

function sourceList(sources) {
  const list = sources.slice(0, 5);
  const wrap = h('div', 'sources');
  const chip = h('a', 'source-chip', null, { href: safeUrl(list[0].url), target: '_blank', rel: 'noopener noreferrer' });
  chip.append(h('span', 'site', siteName(list[0].url)));
  if (list.length > 1) chip.append(h('span', 'more', `+${list.length - 1}`));
  wrap.append(chip);
  let pop = null;
  let hiding = null;
  const show = () => {
    clearTimeout(hiding);
    pop ??= sourcePopover(list);
    if (!pop.isConnected) wrap.append(pop);
    placePopover(pop, chip);
    wrap.classList.add('open');
  };
  const hide = () => { hiding = setTimeout(() => wrap.classList.remove('open'), 150); };
  wrap.addEventListener('mouseenter', show);
  wrap.addEventListener('mouseleave', hide);
  chip.addEventListener('focus', show);
  wrap.addEventListener('focusout', (event) => { if (!wrap.contains(event.relatedTarget)) hide(); });
  return wrap;
}

// Fixed, so the scrolling card list can't clip it; above the chip when there's no room below.
function placePopover(pop, anchor) {
  const r = anchor.getBoundingClientRect();
  const width = Math.min(340, window.innerWidth - 32);
  pop.style.left = `${Math.min(Math.max(16, r.left), window.innerWidth - width - 16)}px`;
  const below = window.innerHeight - r.bottom > 280;
  pop.classList.toggle('below', below);
  pop.classList.toggle('above', !below);
  pop.style.top = below ? `${r.bottom + 8}px` : '';
  pop.style.bottom = below ? '' : `${window.innerHeight - r.top + 8}px`;
}

function sourcePopover(sources) {
  const pop = h('div', 'source-pop');
  const head = h('div', 'pop-head');
  const prev = h('button', null, '‹', { type: 'button', 'aria-label': 'Previous source' });
  const count = h('span', 'pop-count');
  const next = h('button', null, '›', { type: 'button', 'aria-label': 'Next source' });
  const icons = h('span', 'pop-icons');
  for (const s of sources) icons.append(favicon(s.url));
  head.append(prev, count, next, icons, h('span', null, plural(sources.length, 'source')));
  const body = h('a', 'pop-body', null, { target: '_blank', rel: 'noopener noreferrer' });
  pop.append(head, body);
  let i = 0;
  const render = () => {
    const s = sources[i];
    const url = safeUrl(s.url);
    if (url) body.href = url;
    else body.removeAttribute('href');
    const site = h('div', 'pop-site');
    site.append(favicon(s.url), siteName(s.url));
    body.replaceChildren(site, h('div', 'pop-title', s.title || domain(s.url)));
    const snippet = plainSnippet(s.snippet);
    if (snippet) body.append(h('div', 'pop-snippet', snippet));
    if (s.published) body.append(h('div', 'pop-date', s.published));
    count.textContent = `${i + 1}/${sources.length}`;
    prev.disabled = i === 0;
    next.disabled = i === sources.length - 1;
  };
  prev.addEventListener('click', () => { i = Math.max(0, i - 1); render(); });
  next.addEventListener('click', () => { i = Math.min(sources.length - 1, i + 1); render(); });
  render();
  return pop;
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

function cardActions(claim, { pushBack = false } = {}) {
  const row = h('div', 'actions');
  if (pushBack) {
    const push = h('button', null, 'Push back', { type: 'button' });
    push.addEventListener('click', () => openPushback(claim));
    row.append(push);
  }
  const dismiss = h('button', null, 'Dismiss', { type: 'button', title: 'Remove this card' });
  dismiss.addEventListener('click', () => dismissClaim(claim));
  row.append(dismiss);
  return row;
}

// Humans get the last word: a dismissed claim leaves the page, the transcript highlight, the tallies
// and the recap.
function dismissClaim(claim) {
  state.claims.delete(claim.id);
  const line = state.lines.get(claim.line_id);
  if (line) {
    line.marks.delete(claim.id);
    renderLine(line);
  }
  updateStats();
  leave(claim.card).then(() => {
    if (!state.claims.size) $('#cards').replaceChildren(empties.cards);
  });
}

// Fades and shrinks a card a little, then closes the space it took, so the cards below slide up
// instead of jumping. Exits are quicker than entrances; with reduced motion it just goes.
async function leave(node) {
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const ease = 'cubic-bezier(.23, 1, .32, 1)';
    node.style.pointerEvents = 'none';
    await node.animate([{ opacity: 1, transform: 'scale(1)' }, { opacity: 0, transform: 'scale(.97)' }],
      { duration: 140, easing: ease, fill: 'forwards' }).finished;
    const style = getComputedStyle(node);
    const gap = parseFloat(getComputedStyle(node.parentElement).rowGap) || 0;
    node.style.overflow = 'hidden';
    await node.animate([
      { height: style.height, paddingTop: style.paddingTop, paddingBottom: style.paddingBottom, borderTopWidth: style.borderTopWidth, borderBottomWidth: style.borderBottomWidth, marginBottom: '0px' },
      { height: '0px', paddingTop: '0px', paddingBottom: '0px', borderTopWidth: '0px', borderBottomWidth: '0px', marginBottom: `${-gap}px` },
    ], { duration: 180, easing: ease, fill: 'forwards' }).finished;
  }
  node.remove();
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
  const row = h('div', 'verdict-row');
  row.append(badge(claim));
  if (claim.crux) row.append(h('span', 'verdict-text', claim.crux));
  card.append(row);
  if (claim.perspectives == null) {
    card.append(h('p', 'working', 'Finding the most credible voices on this…'));
    return;
  }
  if (!claim.perspectives.length) {
    card.append(h('p', 'no-takes', claim.perspectivesNote || 'No sourced takes found. This one is yours to weigh.'));
  } else {
    const takes = h('div', 'takes');
    claim.perspectives.forEach((p, i) => {
      const take = h('div', `take s-${p.stance}`);
      take.style.animationDelay = `${i * 90}ms`;
      const head = h('div', 'take-head');
      const avatar = h('span', 'avatar', initials(p.person));
      avatar.style.background = personColor(p.person);
      head.append(avatar, h('span', 'person', p.person), h('span', 'stance', STANCE[p.stance] ?? p.stance));
      const quote = h('blockquote', 'take-quote', `“${p.quote}”`);
      if (p.source?.url) quote.append(' ', sourceList([p.source])); // cited like a fact: a chip, details on hover
      take.append(head, quote);
      if (p.summary) take.append(h('p', 'take-summary', p.summary));
      takes.append(take);
    });
    card.append(takes);
  }
  const foot = h('div', 'card-foot');
  foot.append(cardActions(claim));
  card.append(foot);
}

function focusCard(id) {
  const claim = state.claims.get(id);
  if (!claim) return;
  claim.card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  claim.card.classList.add('focus');
  setTimeout(() => claim.card.classList.remove('focus'), 1500);
}

// ---------- tallies ----------

function updateStats() {
  const all = [...state.claims.values()];
  const settled = all.filter((c) => c.kind === 'fact' && c.verdict && !c.verdict.provisional);
  const count = (verdict) => settled.filter((c) => c.verdict.verdict === verdict).length;
  const items = [
    [settled.length, 'facts checked'],
    [count('false'), 'false'],
    [count('misleading'), 'missing context'],
    [all.filter((c) => c.kind !== 'fact').length, 'contested & values'],
  ];
  $('#stats').replaceChildren(...items.map(([n, label]) => {
    const span = h('span');
    span.append(h('b', null, String(n)), ` ${label}`);
    return span;
  }));
}

// ---------- toasts: only errors, and speaker-name suggestions ----------

function toast(message, kind = 'notice') {
  const box = $('#toasts');
  if (box.lastElementChild?.textContent === message) return;
  const note = h('div', `toast ${kind}`, message);
  box.append(note);
  setTimeout(() => note.remove(), kind === 'error' ? 9000 : 5000);
}

// ---------- status and settings ----------

function renderStatus() {
  document.body.dataset.connected = state.connected; // read by the browser test
  const pills = state.config.test_data ? [h('span', 'pill warn', 'Test data, not model output')] : []; // only under tests/serve.py
  $('#status').replaceChildren(...pills);
}

// ---------- the microphone ----------

// Streams the microphone to the server as 16 kHz PCM; returns a function that stops it.
async function startMic() {
  if (!state.config.server_stt) throw new Error('Speech-to-text needs ASSEMBLYAI_API_KEY in .env. Add it and restart the server.');
  const media = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  const ctx = new AudioContext();
  await ctx.audioWorklet.addModule('/static/pcm-worklet.js');
  const pcm = new AudioWorkletNode(ctx, 'pcm16k');
  const silent = ctx.createGain();
  silent.gain.value = 0; // keeps the worklet running without playing the room back
  ctx.createMediaStreamSource(media).connect(pcm).connect(silent).connect(ctx.destination);
  pcm.port.onmessage = (event) => {
    if (state.ws?.readyState === WebSocket.OPEN) state.ws.send(event.data);
  };
  await ctx.resume();
  send({ type: 'start_audio' });
  media.getAudioTracks()[0]?.addEventListener('ended', () => stop());
  return () => {
    send({ type: 'stop_audio' });
    pcm.port.onmessage = null;
    ctx.close();
    media.getTracks().forEach((t) => t.stop());
  };
}

async function start() {
  if (state.running) {
    stop();
    return;
  }
  try {
    state.running = { stop: await startMic() };
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
}

// ---------- common ground: what's settled, and where people differ ----------

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
  const claims = [...state.claims.values()];
  const order = { false: 0, misleading: 1, true: 2, unverifiable: 3 };
  const facts = claims.filter((c) => c.kind === 'fact' && c.verdict && !c.verdict.provisional)
    .sort((a, b) => order[a.verdict.verdict] - order[b.verdict.verdict]); // what was wrong comes first
  const open = claims.filter((c) => c.kind === 'contested');
  const values = claims.filter((c) => c.kind === 'value');
  recapList('#recap-facts', facts, (item, c) => {
    const v = c.verdict;
    item.append(h('span', `tag v-${v.verdict}`, `${VERDICT[v.verdict].icon} ${VERDICT[v.verdict].label}`), quoted(c), recapWho(c.speaker));
    if (v.correction && (v.verdict === 'false' || v.verdict === 'misleading')) item.append(h('span', 'fix', `→ ${v.correction}`));
  }, 'No facts checked yet.');
  recapList('#recap-questions', open, (item, c) => {
    item.append(quoted(c), recapWho(c.speaker));
    const voices = h('div', 'voices');
    for (const p of c.perspectives ?? []) voices.append(h('span', `voice s-${p.stance}`, `${p.person} · ${(STANCE[p.stance] ?? p.stance).toLowerCase()}`));
    if (voices.childElementCount) item.append(voices);
  }, 'No contested claims yet.');
  recapList('#recap-values', values, (item, c) => item.append(quoted(c), recapWho(c.speaker)), 'No value claims yet.');
  const falses = facts.filter((c) => ['false', 'misleading'].includes(c.verdict.verdict)).length;
  $('#recap-sub').textContent = `${plural(facts.length, 'fact')} checked, ${falses} false or missing context · `
    + `${plural(open.length, 'open question')} · ${plural(values.length, 'value')}`;
  $('#recap').hidden = false;
}

function closeRecap() {
  $('#recap').hidden = true;
}

// ---------- clear: start over ----------

function clearConversation() {
  if (state.lines.size && !confirm('Clear the conversation and start over?')) return;
  if (state.running) stop();
  closeRecap();
  // A new connection is a new server session: earlier lines stop being context and repeats, and
  // results still on their way for the old conversation can't land in the new one.
  const old = state.ws;
  state.pending = [];
  connect();
  old?.close();
  state.connected = false;
  renderStatus();
  state.lines.clear();
  state.claims.clear();
  state.speakers.clear();
  state.lastBubble = null;
  state.t0 = null;
  setCaption('', '');
  $('#transcript').replaceChildren(empties.transcript);
  $('#cards').replaceChildren(empties.cards);
  $('#toasts').replaceChildren();
  updateStats();
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

// ---------- wiring ----------

async function init() {
  empties.transcript = $('#transcript-empty');
  empties.cards = $('#cards-empty');
  state.config = await fetch('/api/config').then((r) => r.json());
  renderStatus();
  updateStats();

  $('#start').addEventListener('click', start);
  $('#transcript').addEventListener('scroll', (event) => {
    const box = event.currentTarget;
    box.dataset.pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 8;
  }, { passive: true });
  $('#cards').addEventListener('scroll', () => { // an open source popover follows its chip
    for (const wrap of document.querySelectorAll('.sources.open')) placePopover($('.source-pop', wrap), $('.source-chip', wrap));
  }, { passive: true });
  $('#clear').addEventListener('click', clearConversation);
  $('#recap-close').addEventListener('click', closeRecap);
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
  if (state.config.test_data) window.commonGround = { say }; // the browser test sends its lines here (tests/serve.py only)
  connect();
}

init();
