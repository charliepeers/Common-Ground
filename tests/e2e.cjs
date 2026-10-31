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

// Types lines in by hand, the way anyone can check a statement without speech-to-text.
async function typeIn(page, lines, voices = []) {
  for (const name of voices) {
    await page.fill('#voice-input', name);
    await page.press('#voice-input', 'Enter');
  }
  for (const line of lines) {
    await page.fill('#say-speaker', line.speaker);
    await page.fill('#say-text', line.text);
    await page.press('#say-text', 'Enter');
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
  check(await count('#source, #filters, .legend, #chyron, #stage, #recap-show, #sound, #theme, #present, .latency') === 0,
    'microphone only: no source menu, filters, legend, pop-ups or header icons');
  const header = await page.locator('.topbar .controls button').allTextContents();
  check(header.join('|') === 'Clear|Start', `Clear and Start sit at the top right (${header.join(', ')})`);

  // Facts: a study-group conversation
  const study = conversation('study-group');
  await typeIn(page, study.lines.slice(0, 1), study.voices);
  await page.waitForSelector('.card.v-false', { timeout: 15000 });
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${shots}/1-first-false-claim.png` });
  await typeIn(page, study.lines.slice(1));
  await settle(page, study.lines.length);
  const strip = await page.evaluate(() => {
    const box = document.querySelector('#transcript');
    return { height: box.clientHeight, atEnd: box.scrollHeight - box.scrollTop - box.clientHeight < 8, overflows: box.scrollHeight > box.clientHeight };
  });
  check(strip.height < 80 && strip.overflows && strip.atEnd, `the transcript is a two-line strip on its latest lines (${strip.height}px)`);

  await openRecap(page);
  check(await count('#recap-facts li') === 8, `recap lists the 8 checked facts (${await count('#recap-facts li')})`);
  check((await page.locator('#recap-facts li .tag').first().textContent()).includes('False'), 'recap leads with what was false');
  check(await count('#recap #recap-values li') === 1 && await count('#recap #recap-questions li.recap-empty') === 1, 'recap: one value, no open questions');
  const order = await page.locator('#recap-facts li .tag').allTextContents();
  check(order.slice(0, 4).every((t) => t.includes('False')) && order.slice(4, 7).every((t) => t.includes('True')), `recap puts false claims first (${order.map((t) => t.split(' ')[1]).join(', ')})`);
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
  await page.screenshot({ path: `${shots}/2c-source-popover.png` });
  await page.mouse.move(5, 5);
  await page.waitForFunction(() => !document.querySelector('.sources.open'), null, { timeout: 3000 });
  const stats = await page.locator('#stats').textContent();
  check(stats.includes('8 facts checked') && stats.includes('4 false'), `stats: ${stats}`);
  check(!/\d\.\ds|median/.test(stats), 'no seconds are shown');
  check(await count('.score') === 3, `scoreboard has 3 speakers (${await count('.score')})`);
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
  check(await page.locator('.score .who', { hasText: 'Alex R.' }).count() === 1, 'renaming a speaker updates the scoreboard');

  // Dismiss removes a card entirely
  const robotics = page.locator('.card', { hasText: 'robotics' });
  await robotics.locator('button', { hasText: 'Dismiss' }).click();
  await page.waitForFunction(() => !document.querySelector('.card.v-unverifiable'), null, { timeout: 3000 });
  check(await count('.card.v-unverifiable') === 0 && await count('mark.m-unverifiable') === 0, 'dismiss removes the card and its highlight');
  check((await page.locator('#stats').textContent()).startsWith('7 facts checked'), 'dismiss takes the claim out of the tally');

  // Clear: within one conversation a repeated claim is remembered; after Clear it's checked fresh
  await typeIn(page, study.lines.slice(0, 1));
  await page.waitForFunction(() => document.querySelectorAll('.card.v-false').length === 5, null, { timeout: 15000 });
  check(await page.locator('.card.v-false', { hasText: 'Said before' }).count() === 1, 'a repeated claim is marked as said before');
  page.once('dialog', (d) => d.accept());
  await page.click('#clear');
  await page.waitForFunction(() => !document.querySelector('.bubble, .card'), null, { timeout: 5000 });
  check(await page.locator('#transcript-empty').isVisible() && await page.locator('#cards-empty').isVisible(), 'clear empties the transcript and the cards');
  check(await count('.score') === 0 && (await page.locator('#stats').textContent()).startsWith('0 facts checked'), 'clear resets the tally and the scoreboard');
  await typeIn(page, study.lines.slice(0, 1)); // typed at once, while the new connection opens
  await page.waitForSelector('.card.v-false', { timeout: 15000 });
  check(!(await page.locator('.card.v-false').textContent()).includes('Said before'), 'after clear, the same claim is checked fresh');

  // Worldviews: LeCun's own words, with Karpathy pinned as a voice
  await page.reload();
  await page.waitForSelector('body[data-connected="true"]');
  const lecture = conversation('ai-lecture');
  // Pin someone the test data lists after others, so "pinned voices first" is really tested
  await typeIn(page, lecture.lines.slice(0, 1), ['Ilya Sutskever']);
  check(await page.locator('#voice-chips .chip', { hasText: 'Ilya Sutskever' }).count() === 1, 'a pinned voice shows as a chip');
  await page.waitForSelector('.take', { timeout: 20000 });
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${shots}/4-lecture-perspectives.png` });
  await typeIn(page, lecture.lines.slice(1));
  await page.waitForFunction(() => document.querySelectorAll('.takes').length === 4, null, { timeout: 15000 });
  check(await count('.card.k-contested') === 3 && await count('.card.k-value') === 1, '3 contested claims and 1 value');
  check(await count('.take') === 10, `10 sourced takes (${await count('.take')})`);
  check(await count('.take-source[href^="https://"]') === 10, 'every take links to its source');
  check(await count('.take.s-disagrees') >= 1 && await count('.take.s-agrees') >= 1, 'takes span agree and disagree');
  check(await count('mark.m-contested') === 3, 'contested claims are highlighted in the transcript');
  const catTakes = await page.locator('.card', { hasText: 'house cat' }).locator('.take .person').allTextContents();
  check(catTakes[0] === 'Ilya Sutskever' && catTakes.length === 3, `a pinned voice's take comes first (${catTakes.join(', ')})`);
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
