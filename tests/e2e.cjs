// Drives the real page against a server replaying the UI test data, and checks what a viewer would see.
//   OFFLINE=1 .venv/bin/uvicorn server.app:app --port 8765 &
//   node tests/e2e.cjs [http://localhost:8765] [screenshot dir, default tests/screenshots]
const fs = require('fs');
const { chromium } = require('playwright');

const base = process.argv[2] || 'http://localhost:8765';
const shots = process.argv[3] || 'tests/screenshots';
fs.mkdirSync(shots, { recursive: true });
const problems = [];
const check = (ok, message) => { if (!ok) problems.push(message); console.log(`${ok ? 'ok  ' : 'FAIL'} ${message}`); };

(async () => {
  const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
  const clip = process.env.CLIP; // a short video for the video-file source
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on('pageerror', (err) => problems.push(`page error: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') problems.push(`console error: ${msg.text()}`); });

  await page.goto(`${base}/?speed=3`);
  await page.waitForSelector('.pill.live', { timeout: 10000 });
  check(await page.locator('.pill.warn', { hasText: 'test data, not model output' }).count() === 1, 'test data is labeled as such');
  check(await page.locator('#chyron').isHidden(), 'no empty lower third before the first verdict');
  const options = await page.locator('#source option').allTextContents();
  check(options[0].includes('Study group') && options[1].includes('AI lecture'), `demo sources listed: ${options.slice(0, 2).join(' | ')}`);

  // Demo 1: study group fact-check
  await page.selectOption('#source', 'demo:study-group');
  await page.click('#start');
  await page.waitForSelector('.card.v-false', { timeout: 15000 });
  await page.waitForSelector('#chyron:not([hidden])', { timeout: 5000 });
  await page.screenshot({ path: `${shots}/1-first-false-claim.png` });
  check((await page.locator('#chyron .tag').textContent()).includes('False'), 'chyron announces the false claim');
  await page.waitForFunction(() => document.querySelector('#start .label').textContent === 'Start', null, { timeout: 60000 });
  await page.waitForFunction(() => !document.querySelector('.card.checking, .card.provisional'), null, { timeout: 15000 });
  await page.waitForTimeout(800);

  const count = (sel) => page.locator(sel).count();
  await page.waitForSelector('#recap:not([hidden])', { timeout: 15000 });
  check(await count('#recap-facts li') === 8, `recap lists the 8 checked facts (${await count('#recap-facts li')})`);
  check((await page.locator('#recap-facts li .tag').first().textContent()).includes('False'), 'recap leads with what was false');
  check(await count('#recap #recap-values li') === 1 && await count('#recap #recap-questions li.recap-empty') === 1, 'recap: one value, no open questions');
  const order = await page.locator('#recap-facts li .tag').allTextContents();
  check(order.slice(0, 4).every((t) => t.includes('False')) && order.slice(4, 7).every((t) => t.includes('True')), `recap puts false claims first (${order.map((t) => t.split(' ')[1]).join(', ')})`);
  check(await count('#recap-show li') === 0, 'the recap button holds no list items');
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
  check(await count('.card .sources a[href^="https://"]') >= 7, 'verdict cards carry source links');
  const stats = await page.locator('#stats').textContent();
  check(stats.includes('8 facts checked') && stats.includes('4 false'), `stats: ${stats}`);
  check(!stats.includes('median') && await count('.latency') === 0, 'no latency is shown for unmeasured data');
  check(await count('.score') === 3, `scoreboard has 3 speakers (${await count('.score')})`);
  await page.screenshot({ path: `${shots}/2-study-group-done.png` });

  // Push back on the Great Wall verdict
  const wall = page.locator('.card', { hasText: 'Great Wall' });
  await wall.locator('button', { hasText: 'Push back' }).click();
  await wall.locator('textarea').fill('Astronauts have photographed it from the ISS though.');
  await wall.locator('button', { hasText: 'Re-check' }).click();
  await wall.locator('.pushback-note').waitFor({ timeout: 15000 });
  check((await wall.locator('.pushback-note .change').textContent()).includes('Held'), 'push-back holds the verdict and explains why');
  await wall.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${shots}/3-pushback.png` });

  // Rename a speaker: the label changes everywhere
  page.once('dialog', (d) => d.accept('Alex R.'));
  await page.locator('.speaker-name', { hasText: 'Alex' }).first().click();
  check(await page.locator('.score .who', { hasText: 'Alex R.' }).count() === 1, 'renaming a speaker updates the scoreboard');

  // Filters
  await page.click('#filters [data-filter="value"]');
  check(await page.locator('.card:not([hidden])').count() === 1, 'values filter shows only the value card');
  await page.click('#filters [data-filter="all"]');

  // Demo 2: AI lecture with perspectives
  await page.reload();
  await page.waitForSelector('.pill.live');
  await page.selectOption('#source', 'demo:ai-lecture');
  await page.click('#start');
  await page.waitForSelector('.take', { timeout: 20000 });
  check(await page.locator('#voice-chips .chip', { hasText: 'Andrej Karpathy' }).count() === 1, 'demo pins Karpathy as a voice');
  await page.waitForSelector('#chyron:not([hidden])', { timeout: 5000 });
  check((await page.locator('#chyron .tag').textContent()).includes('Karpathy'), 'chyron leads with the pinned voice');
  await page.screenshot({ path: `${shots}/4-lecture-perspectives.png` });
  await page.waitForFunction(() => document.querySelector('#start .label').textContent === 'Start', null, { timeout: 60000 });
  await page.waitForFunction(() => document.querySelectorAll('.takes').length === 4, null, { timeout: 15000 });
  check(await count('.card.k-contested') === 3 && await count('.card.k-value') === 1, '3 contested claims and 1 value');
  check(await count('.take') === 10, `10 sourced takes (${await count('.take')})`);
  check(await count('.take-source[href^="https://"]') === 10, 'every take links to its source');
  check(await count('.take.s-disagrees') >= 1 && await count('.take.s-agrees') >= 1, 'takes span agree and disagree');
  check(await count('mark.m-contested') === 3, 'contested claims are highlighted in the transcript');
  await page.waitForSelector('#recap:not([hidden])', { timeout: 15000 });
  check(await count('#recap #recap-questions li') === 3 && await count('#recap #recap-questions .voice') >= 6, 'recap shows the open questions and who stands where');
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${shots}/5b-lecture-recap.png` });
  await page.keyboard.press('Escape');
  await page.screenshot({ path: `${shots}/5-lecture-done.png` });

  // A video file plays on the stage, and verdicts appear over it
  if (clip) {
    await page.reload();
    await page.waitForSelector('.pill.live');
    await page.selectOption('#source', 'video');
    const [chooser] = await Promise.all([page.waitForEvent('filechooser'), page.click('#start')]);
    await chooser.setFiles(clip);
    await page.waitForSelector('#stage:not([hidden]) video');
    check((await page.locator('#start .label').textContent()) === 'Play', 'a chosen clip is cued, waiting for Play');
    await page.click('#start');
    await page.waitForFunction(() => document.querySelector('#stage video').currentTime > 0.5, null, { timeout: 8000 });
    check(true, 'the clip plays on the stage');
    await page.fill('#say-text', 'Okay, quick sanity check for the civics slides. Joe Biden is the 47th president, right?');
    await page.press('#say-text', 'Enter');
    await page.waitForSelector('#stage #chyron:not([hidden])', { timeout: 15000 });
    check((await page.locator('#stage #chyron .tag').textContent()).includes('False'), 'the verdict appears over the video');
    await page.waitForTimeout(700);
    await page.screenshot({ path: `${shots}/6a-video-stage.png` });
    await page.waitForSelector('#recap:not([hidden])', { timeout: 30000 }); // the clip ends, the recap opens
    check(await page.locator('#stage').isHidden(), 'the stage closes when the clip ends');
    await page.keyboard.press('Escape');
  }

  // Light theme and a phone-width layout
  await page.click('#theme');
  await page.screenshot({ path: `${shots}/6-light.png` });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(300);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(overflow <= 0, `no horizontal scroll on a phone (${overflow}px)`);
  await page.screenshot({ path: `${shots}/7-phone.png`, fullPage: false });

  // Typed input outside the test data gets an honest notice offline
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.fill('#say-text', 'The Eiffel Tower is in Rome.');
  await page.press('#say-text', 'Enter');
  await page.waitForSelector('.toast.notice', { timeout: 5000 });
  check(true, 'a typed line outside the test data produces a notice');

  await browser.close();
  console.log(problems.length ? `\n${problems.length} problem(s):\n- ${problems.join('\n- ')}` : '\nall checks passed');
  process.exit(problems.length ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(2); });
