// Drives the real page against the test server, which answers from fixed test data, and checks what a viewer would see.
//   .venv/bin/python -m tests.serve 8765 &
//   node tests/e2e.cjs [http://localhost:8765] [screenshot dir, default tests/screenshots]
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const base = process.argv[2] || 'http://localhost:8765';
const shots = process.argv[3] || 'tests/screenshots';
fs.mkdirSync(shots, { recursive: true });
const { conversations } = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures.json'), 'utf8'));
const conversation = (id) => conversations.find((c) => c.id === id);
const problems = [];
const check = (ok, message) => { if (!ok) problems.push(message); console.log(`${ok ? 'ok  ' : 'FAIL'} ${message}`); };

// Sends lines as if they had been heard: a headless browser has no microphone, so the page exposes
// a hook for this under the test server only.
async function typeIn(page, lines) {
  for (const line of lines) {
    await page.evaluate(([speaker, text]) => window.commonGround.say(speaker, text), [line.speaker, line.text]);
    await page.waitForTimeout(250);
  }
}

async function settle(page, bubbles) {
  await page.waitForFunction((n) => document.querySelectorAll('.bubble').length >= n, bubbles, { timeout: 15000 });
  await page.waitForFunction(() => !document.querySelector('.card.checking, .card.provisional'), null, { timeout: 15000 });
  await page.waitForTimeout(800);
}

async function openRecap(page) {
  await page.evaluate(() => document.activeElement?.blur()); // S types into a focused input instead
  await page.keyboard.press('s');
  await page.waitForSelector('#recap:not([hidden])', { timeout: 5000 });
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on('pageerror', (err) => problems.push(`page error: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') problems.push(`console error: ${msg.text()}`); });
  const count = (sel) => page.locator(sel).count();

  await page.goto(base);
  await page.waitForSelector('body[data-connected="true"]', { timeout: 10000 });
  check(await page.locator('.pill.warn', { hasText: 'test data, not model output' }).count() === 1, 'test data is labeled as such');
  check(await count('#status .pill') === 1, 'no status pills beyond the test-data label');
  check(await count('#source, #say, #filters, .legend, #chyron, #stage, #recap-show, #sound, #theme, #present, .latency, #voice-input') === 0,
    'microphone only: no source menu, typed-line box, voices bar, filters, legend, pop-ups or header icons');
  const header = await page.locator('.topbar .controls button').allTextContents();
  check(header.join('|') === 'Start', `Start sits at the top right (${header.join(', ')})`);
  const panelButtons = await page.locator('.transcript-pane .pane-head button').allTextContents();
  check(panelButtons.join('|') === 'Clear', `Clear sits on the transcript panel (${panelButtons.join(', ')})`);
  const heads = await page.evaluate(() => [...document.querySelectorAll('.pane-head')].map((head) => head.getBoundingClientRect().bottom));
  check(heads[0] === heads[1], `the two panel heads line up (${heads.join(' / ')})`);

  // Facts: a study-group conversation
  const study = conversation('study-group');
  await typeIn(page, study.lines.slice(0, 1));
  await page.waitForSelector('.card.v-false', { timeout: 15000 });
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${shots}/1-first-false-claim.png` });
  await typeIn(page, study.lines.slice(1));
  await settle(page, study.lines.filter((line, i) => line.speaker !== study.lines[i - 1]?.speaker).length);
  const panel = await page.evaluate(() => {
    const box = document.querySelector('#transcript');
    const side = (sel) => document.querySelector(sel).getBoundingClientRect();
    return {
      height: box.clientHeight, atEnd: box.scrollHeight - box.scrollTop - box.clientHeight < 8,
      sideBySide: side('.transcript-pane').right <= side('.claims-pane').left,
    };
  });
  check(panel.sideBySide && panel.height > 300 && panel.atEnd, `two panels side by side; the transcript is a full panel on its latest lines (${panel.height}px)`);

  // A plain list, newest card on top
  check(await count('.stack, .deck, .stack-toggle') === 0, 'cards are a plain list, not stacks');
  check((await page.locator('#cards .card').first().textContent()).includes('robotics'), 'the newest card is on top');
  const orange = await page.evaluate(() => getComputedStyle(document.querySelector('.card.v-unverifiable .badge')).color);
  check(orange === 'rgb(217, 115, 13)', `an unverified claim is orange (${orange})`);
  check(await count('.card .badge .ico svg') === 9 && await count('.card.checking') === 0, `every card has its icon (${await count('.card .badge .ico svg')})`);

  await openRecap(page);
  check(await count('#recap-facts li .tag .ico svg') === 8, 'recap tags carry the icons');
  check(await count('#recap-facts li') === 8, `recap lists the 8 checked facts (${await count('#recap-facts li')})`);
  check((await page.locator('#recap-facts li .tag').first().textContent()).includes('False'), 'recap leads with what was false');
  check(await count('#recap #recap-values li') === 1 && await count('#recap #recap-questions li.recap-empty') === 1, 'recap: one value, no open questions');
  const order = await page.locator('#recap-facts li .tag').allTextContents();
  check(order.slice(0, 4).every((t) => t.includes('False')) && order[4].includes('Unverified') && order.slice(5, 8).every((t) => t.includes('True')), `recap puts false claims first (${order.join(', ')})`);
  await page.waitForTimeout(900); // let the entrance animations finish before the picture
  await page.screenshot({ path: `${shots}/2b-recap.png` });
  await page.keyboard.press('Escape');
  check(await page.locator('#recap').isHidden(), 'Escape closes the recap');
  check(await count('.bubble') >= 8, `transcript has speaker bubbles (${await count('.bubble')})`);
  check(await count('.card.v-false') === 4, `4 false verdicts (${await count('.card.v-false')})`);
  check(await count('.card.v-true') === 3, `3 true verdicts (${await count('.card.v-true')})`);
  check(await count('.card.v-unverifiable') === 1, `1 unverified (${await count('.card.v-unverifiable')})`);
  check(await count('.card.k-value') === 1, `1 value card (${await count('.card.k-value')})`);
  check(await count('mark.m-false') === 4 && await count('mark.m-true') === 3, 'transcript highlights match verdicts');
  check(await count('mark.m-checking') === 0, 'no highlight left in the checking state');
  check(await count('.card .source-chip[href^="https://"]') === 7, `each sourced verdict has one source chip (${await count('.card .source-chip')})`);
  check(await count('.card .source-chip .site') === 7 && !(await page.locator('.card').first().textContent()).includes('##'), 'chips show the site, and no raw excerpts are on the cards');
  const chip = page.locator('.card.v-false', { hasText: 'Joe Biden' }).locator('.sources');
  await chip.hover();
  await page.waitForSelector('.sources.open .source-pop .pop-title', { timeout: 3000 });
  check((await page.locator('.sources.open .pop-title').textContent()).length > 0 && await count('.sources.open .pop-icons img') >= 1, 'hovering a source shows its title and site logos');
  await page.waitForTimeout(250); // past the popover's fade-in
  await page.screenshot({ path: `${shots}/2c-source-popover.png` });
  await page.mouse.move(5, 5);
  await page.waitForFunction(() => !document.querySelector('.sources.open'), null, { timeout: 3000 });
  const stats = await page.locator('#stats').textContent();
  check(stats.includes('8 facts checked') && stats.includes('4 false'), `stats: ${stats}`);
  check(stats.includes('0 contested') && stats.includes('1 value'), `contested claims and values are counted apart (${stats})`);
  check(stats.includes('1 unverified') && !stats.includes('missing context'), `the tally says unverified (${stats})`);
  check(!/\d\.\ds|median/.test(stats), 'no seconds are shown');
  const tallyColors = await page.evaluate(() => {
    const color = (sel) => getComputedStyle(document.querySelector(sel)).color;
    const tally = [...document.querySelectorAll('#stats span')].map((span) => getComputedStyle(span).color);
    return { tally, labels: [color('.card.v-true .badge'), color('.card.v-false .badge'), color('.card.v-unverifiable .badge'), null, color('.card.k-value .badge')] };
  });
  check(tallyColors.labels.every((color, i) => color === null || color === tallyColors.tally[i]), `the tally uses the title label colors (${tallyColors.tally.join(' / ')})`);
  check(await count('#scoreboard, .score') === 0, 'no per-speaker tally');
  check(await count('.card.fresh') === 0 && await count('.card.v-false .badge.changed') === 4, 'a false verdict lands like any other: its badge settles in, no ring');
  await page.screenshot({ path: `${shots}/2-study-group-done.png` });

  // Push back on the Great Wall verdict
  const wall = page.locator('.card', { hasText: 'Great Wall' });
  await wall.locator('button', { hasText: 'Push back' }).click();
  await wall.locator('textarea').fill('Astronauts have photographed it from the ISS though.');
  await wall.locator('button', { hasText: 'Re-check' }).click();
  await wall.locator('.pushback-note').waitFor({ timeout: 15000 });
  check((await wall.locator('.pushback-note .change').textContent()).includes('Held'), 'push-back holds the verdict and explains why');
  check(await wall.locator('.badge.changed').count() === 1, 'the re-checked verdict badge animates in');
  await wall.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${shots}/3-pushback.png` });

  // Rename a speaker: the label changes everywhere
  page.once('dialog', (d) => d.accept('Alex R.'));
  await page.locator('.speaker-name', { hasText: 'Alex' }).first().click();
  check(await page.locator('.card .card-speaker', { hasText: 'Alex R.' }).count() === 3 && await page.locator('.speaker-name', { hasText: 'Alex R.' }).count() >= 1,
    'renaming a speaker updates the transcript and every card');

  // Dismiss removes a card entirely
  const robotics = page.locator('.card', { hasText: 'robotics' });
  await robotics.locator('button', { hasText: 'Dismiss' }).click();
  await page.waitForFunction(() => !document.querySelector('.card.v-unverifiable'), null, { timeout: 3000 });
  check(await count('.card.v-unverifiable') === 0 && await count('mark.m-unverifiable') === 0, 'dismiss removes the card and its highlight');
  check((await page.locator('#stats').textContent()).startsWith('7 facts checked'), 'dismiss takes the claim out of the tally');

  // Clear: within one conversation a repeated claim gets no second card; after Clear it's checked fresh
  const cardsBefore = await count('.card');
  await typeIn(page, study.lines.slice(0, 1));
  await page.waitForTimeout(600);
  check(await count('.card') === cardsBefore, 'a repeated claim gets no second card');
  page.once('dialog', (d) => d.accept());
  await page.click('#clear');
  await page.waitForFunction(() => !document.querySelector('.bubble, .card'), null, { timeout: 5000 });
  check(await page.locator('#transcript-empty').isVisible() && await page.locator('#cards-empty').isVisible(), 'clear empties the transcript and the cards');
  check((await page.locator('#stats').textContent()).startsWith('0 facts checked'), 'clear resets the tally');
  await typeIn(page, study.lines.slice(0, 1)); // typed at once, while the new connection opens
  await page.waitForSelector('.card.v-false', { timeout: 15000 });
  check(await count('.card.v-false') === 1 && await count('.card') === 1, 'after clear, the same claim is checked fresh');

  // Worldviews: LeCun's own words, and how the most credible people see them
  await page.reload();
  await page.waitForSelector('body[data-connected="true"]');
  const lecture = conversation('ai-lecture');
  await typeIn(page, lecture.lines.slice(0, 1));
  await page.waitForSelector('.take', { timeout: 20000 });
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${shots}/4-lecture-perspectives.png` });
  await typeIn(page, lecture.lines.slice(1));
  await page.waitForFunction(() => document.querySelectorAll('.takes').length === 3, null, { timeout: 15000 });
  check(await count('.card.k-contested') === 3 && await count('.card.k-value') === 1, '3 contested claims and 1 value');
  check(await count('.take') === 6, `6 sourced takes (${await count('.take')})`);
  const names = await page.evaluate(() => [...document.querySelectorAll('.takes')].map((takes) => [...takes.querySelectorAll('.person')].map((p) => p.textContent)));
  check(names.every((row) => row.length === 2 && new Set(row).size === 2), `two voices on a card, no name twice (${names.join(' / ')})`);
  check(await count('.card.k-value .take') === 0 && (await page.locator('.card.k-value .no-takes').textContent()).includes('yours to weigh'), 'a value cites nobody: it is yours to weigh');
  check(await count('.take .take-row .source-chip[href^="https://"]') === 6, 'every take cites its source with the same chip as facts');
  const opened = await page.evaluate(async () => {
    const take = document.querySelector('.take');
    const shown = (sel) => take.querySelector(sel).getClientRects().length > 0;
    const before = { quote: shown('.take-quote'), summaryLines: take.querySelector('.take-summary').getClientRects().length };
    const chipVisible = () => getComputedStyle(take.querySelector('.take-row .sources')).visibility === 'visible';
    const wait = () => new Promise((resolve) => setTimeout(resolve, 400)); // past the animation
    take.querySelector('.take-text').click();
    await wait();
    const result = { before, quote: shown('.take-quote'), evidence: !!take.querySelector('.take-evidence a[href^="https://"]'),
      open: take.classList.contains('open'), chipWhileOpen: chipVisible() };
    take.querySelector('.take-text').click();
    await wait();
    result.chipAfterClose = chipVisible();
    take.querySelector('.take-text').click(); // open again for the screenshot
    await wait();
    return result;
  });
  check(!opened.before.quote && opened.open && opened.quote && opened.evidence,
    'a take shows a one-line summary; clicking opens the exact quote and the page it comes from');
  check(!opened.chipWhileOpen && opened.chipAfterClose, 'the small source chip hides while a take is open and returns when it closes');
  await page.screenshot({ path: `${shots}/4b-take-opened.png` });
  check(await count('.take.s-disagrees') >= 1 && await count('.take.s-agrees') >= 1, 'takes span agree and disagree');
  const stanceColors = await page.evaluate(() => ['disagrees', 'complicates', 'agrees'].map((s) => getComputedStyle(document.querySelector(`.take.s-${s} .stance`)).color));
  const labelColor = await page.evaluate(() => getComputedStyle(document.querySelector('.card.k-contested .badge')).color);
  check(stanceColors.every((color) => color === labelColor), `stance tags match the card's title label (${stanceColors.join(' / ')})`);
  check(await count('mark.m-contested') === 3, 'contested claims are highlighted in the transcript');
  await openRecap(page);
  check(await count('#recap #recap-questions li') === 3 && await count('#recap #recap-questions .voice') >= 6, 'recap shows the open questions and who stands where');
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${shots}/5b-lecture-recap.png` });
  await page.keyboard.press('Escape');
  await page.screenshot({ path: `${shots}/5-lecture-done.png` });

  // Full width, then a phone-width layout
  await page.screenshot({ path: `${shots}/6-wide.png` });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(300);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(overflow <= 0, `no horizontal scroll on a phone (${overflow}px)`);
  await page.screenshot({ path: `${shots}/7-phone.png`, fullPage: false });

  // A line the test data doesn't cover gets an error, never a made-up answer
  await page.setViewportSize({ width: 1440, height: 900 });
  const cards = await count('.card');
  await typeIn(page, [{ speaker: 'Me', text: 'The Eiffel Tower is in Rome.' }]);
  await page.waitForSelector('.toast.error', { timeout: 5000 });
  await page.waitForTimeout(300);
  check(await count('.card') === cards, 'a line outside the test data gets an error and no card');

  await browser.close();
  console.log(problems.length ? `\n${problems.length} problem(s):\n- ${problems.join('\n- ')}` : '\nall checks passed');
  process.exit(problems.length ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(2); });
