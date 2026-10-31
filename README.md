# Common Ground

**Facts get checked. Worldviews get heard. You decide.**

One device listens to a conversation, talk or broadcast, and works through every statement as it's spoken:

| Lane | What it is | What you see |
|---|---|---|
| **Fact** | Settled and checkable: dates, numbers, records, who holds an office | A verdict (true, false, misleading, unverified) with the sources behind it |
| **Contested** | About how the world is, but informed people disagree ("LLMs can't really reason") | No verdict. How credible people see it, **in their own words**, each quote linked to its page |
| **Value** | What should be done ("we should spend more on space than on defense") | No verdict. The other side's case, and the reminder that this one is yours |

## The idea

The hackathon prompt is that one must remain human, and for us the human core is our values. AI is very good at finding and synthesizing information. It shouldn't decide what you value. So the app splits the job:

- **Facts are checkable.** The app checks them fast and shows the sources.
- **Worldviews aren't.** The app never scores them. It shows the strongest takes from people who see it differently, and you weigh them.

Fact-checking is the easy part. The interesting moment is when you're listening to Yann LeCun say LLMs can't reason, and Andrej Karpathy's own words on that question appear beside it, with the link.

## Status: what is verified and what isn't

- **Not yet run against the live APIs.** The build environment had no API keys, so no real Claude, Exa, AssemblyAI or Deepgram call has been made. **No latency has been measured.** The page shows a timing only when it was measured, which means live mode.
- **Request formats.**
  - The Claude calls are checked through the real `anthropic` 2.3.0 SDK against a mocked HTTP transport (`tests/test_wire.py`). That covers headers, body and the parsing of streamed events.
  - The Exa, AssemblyAI and Deepgram formats are checked against their official API references only.
- **Tests.**
  - Python: segmenter rules, streaming JSON reader, receipts rule, verbatim-quote check, speaker naming, push-back, fast-mode fallback.
  - Browser: `tests/e2e.cjs` drives the page against `tests/serve.py`, a test server that answers from the **fixed, hand-written data** in `tests/fixtures.json` and labels it as test data on screen. The app itself never serves that data. With `CLIP` set, the test also plays a video on the stage; so far only an 8-second clip, without speech-to-text.
- **The first thing to do with keys** is measure. See "Measuring speed and accuracy".

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Fill in the keys in `.env` (git-ignored, so they can't be committed):

- `ANTHROPIC_API_KEY` (line 2): Claude. Required.
- `EXA_API_KEY` (line 6): search, and the takes from other voices. Without it, facts go to Claude's own web search (slower) and the takes are off.
- `ASSEMBLYAI_API_KEY` (line 10), or `DEEPGRAM_API_KEY` (line 11): speech-to-text for a video file, a shared tab or the microphone. Without one, only Chrome's browser speech works.

Then start the server:

```bash
.venv/bin/uvicorn server.app:app --port 8000
```

Open http://localhost:8000 and pick a source:

- **Video file, plays here:** press Start to choose a clip, then Play. It plays in the page with its sound while the audio streams to speech-to-text, and verdicts and takes appear over the picture. Without a speech-to-text key the clip still plays, but nothing in it is checked.
- **Microphone (AssemblyAI or Deepgram):** one device for the whole room; voices are separated by diarization (see "Telling voices apart").
- **Browser tab or video:** a YouTube talk, a livestream, a call. In Chrome, pick the tab and tick **Share tab audio**. The shared tab's picture shows on the stage.
- **Browser speech (Chrome):** needs no speech key, but has no speaker labels and no punctuation.
- **Typing a line** into the box under the transcript checks it the same way, with no speech-to-text.

On screen:

- **The lower third** announces verdicts and takes one at a time. When several land together they queue, at most four with the oldest dropped, and a backlog moves faster (3.5 s each instead of 6.5 s).
- **Common ground**, the recap, opens by itself when a clip ends, or with ◎ or the S key. It lists the facts checked (false first), the open questions with who stands where, and the values that are yours to weigh.
- **Presentation mode** (the P key) hides the controls and enlarges the text. Esc closes the recap.

**On a phone:** browsers only allow the microphone over HTTPS, so open the app through a tunnel. Then open the https link it prints:

```bash
cloudflared tunnel --url http://localhost:8000
```

## Architecture

```mermaid
flowchart LR
  A[Mic or tab audio] -->|16 kHz PCM over WebSocket| S[FastAPI server]
  S --> T[AssemblyAI or Deepgram streaming]
  T --> G[Segmenter: is the statement complete?]
  G -->|one sentence| X[Exa search on the raw sentence]
  X --> C[One streamed LLM call, Claude Sonnet 5.5 by default: find, sort and judge the claims]
  C -->|fact with sources| V[Verdict]
  C -->|fact the search missed| E[Targeted search, then verdict] --> V
  C -->|contested or value| P[Search named voices, X posts included] --> Q[Claude picks takes; quotes checked verbatim] --> W[Perspectives]
  V --> UI[Browser]
  W --> UI
  UI -->|push back| D[Claude searches the web itself] --> V
```

The pieces:

1. **Browser (`web/`, no build step).**
   - An AudioWorklet turns the mic, tab or video-file audio into 16 kHz mono PCM and streams it to the server over one WebSocket. A video file plays in the page and its sound is tapped with `createMediaElementSource`, so the audience hears what is being checked.
   - Events come back the same way and are drawn as they arrive: lines, claims, verdicts and perspectives.
2. **Speech-to-text (`server/stt.py`).** The server relays the audio to AssemblyAI Universal-3.6 Pro (`speaker_labels=true`) or Deepgram Nova-3 (`diarize_model=latest`), and gets back words with speaker labels.
3. **Segmenter (`server/segmenter.py`).** Decides when a statement is complete enough to check (rules below).
4. **Session (`server/session.py`).** For each statement it does four things:
   - shows the line;
   - searches Exa with the raw sentence and makes **one** streamed Claude call that finds the claims, sorts them into lanes and judges the facts;
   - escalates facts the first search didn't cover;
   - fetches perspectives for contested claims and values.

   It also handles push-back and suggests names for anonymous speakers.
5. **Claude layer (`server/llm.py`, `prompts.py`, `schemas.py`).** Each role has its own model (see "Model and vendor choices"); all calls use JSON-schema outputs. Push-back uses Claude's own web search tool, with a strict `submit_verdict` tool for the answer.
6. **Search (`server/search.py`).** Exa `lite`, Exa's type for real-time and voice use. Each request sends only what `lite` accepts, and the server logs any `warnings` Exa returns about ignored fields.

### When does a statement get sent?

Interim transcripts are never checked; they only drive the live caption. Finished words collect into the current speaker's statement, and it is sent as soon as any of these happens:

1. **Another speaker starts talking.**
2. **A sentence ends** (`.` `?` `!`) and the statement has at least 3 words. Abbreviations like "U.S." don't count.
3. **35 words go by without a sentence end.**
4. **The speaker pauses** (Deepgram: 300 ms) and the statement has 6+ words.
5. **The utterance is over** (Deepgram: 1 s without words).

The unit is the sentence because a claim rarely spans two of them, and waiting for a whole turn would hold every claim until the speaker stops. The previous six lines go along as context, so "He was the 45th too" still resolves.

AssemblyAI streams turns instead of segments, so `TurnSegmenter` sends each finished turn sentence by sentence, and cuts long turns at 35 words.

### Latency design, all unmeasured

What each choice is meant to do, and why:

1. **One Claude call per sentence instead of two.** Extraction and judging share a single round trip.
2. **The search starts before Claude.** It runs on the raw sentence the moment the sentence is complete.
3. **Streaming JSON** (`server/jsonstream.py`). Each claim is drawn as soon as its quote has streamed in, and gets its verdict when its JSON object closes, without waiting for the rest of the answer.
4. **Fast mode** is used when a role runs on Opus 5, 5.5 or 4.8, the only models that have it (Anthropic documents up to 2.5× output speed, at a premium price). The live path's default, Sonnet 5.5, doesn't have it.
5. **Effort `low`,** and explanations capped at 20 words.
6. **Warm-up at startup.** Anthropic documents a one-time compile cost for each new JSON schema; the warm-up pays it and opens connections before anyone is watching.
7. **Hard cases don't block.** When the first search missed a fact, Claude's first read is shown as unconfirmed while a targeted search runs.

### Measuring speed and accuracy

`bench/claims.jsonl` holds 37 labeled statements: true and false facts, private claims, contested claims, values, and non-claims. The runner reports:

- whether each statement was sorted into the right lane and given the right verdict;
- **false alarms:** true statements shown as false or misleading;
- p50 and p90 latency for search, first highlight, the Claude call, and time to verdict.

```bash
.venv/bin/python -m bench.run_bench --repeat 3
```

Compare settings by re-running it with one variable changed:

```bash
FAST_MODE=0 .venv/bin/python -m bench.run_bench --repeat 3
```

```bash
CHECK_EFFORT=medium .venv/bin/python -m bench.run_bench --repeat 3
```

```bash
.venv/bin/python -m bench.run_bench --no-search --repeat 3
```

The last one uses Claude's own web search instead of Exa.

The benchmark starts from text, so speech-to-text time is not included. Speech-to-text knobs worth measuring live:

- AssemblyAI's `max_turn_silence`, set with `ASSEMBLYAI_MAX_TURN_SILENCE`. Its documented default with speaker labels is 768 ms on Universal-3.5 Pro.
- Deepgram's `endpointing`, currently 300 ms.

### Trust rules

- **No receipts, no red.** A true, false or misleading verdict must cite a page the search actually returned. Otherwise it shows as unverified, along with which way it was leaning.
- **A wrong red flag is worse than a missed one.** The prompts say so, and the benchmark counts false alarms separately.
- **Quotes are verbatim or dropped.** Every perspective quote is checked in code against the text of its page.
- **Contested isn't false.** Perspectives use one neutral color, and stance is a label, never red or green.
- **Humans get the last word.**
  - Anyone can push back on a verdict. Claude re-checks with a deeper web search and says whether it changed its mind.
  - Anyone can dismiss a verdict from the tally.
- **Dates matter.** Today's date is in every prompt, because claims about the present depend on it.

## Telling voices apart with one device

Google Meet and Zoom don't recognize voices. Every participant joins on their own device, so each voice arrives on its own channel. One device in a room records **one mixed stream**, and splitting it is *speaker diarization*: an acoustic model groups voices into "Speaker A, B, C". Nobody has measured how well that works in this app yet. What others have measured:

- **Streaming APIs on DIHARD III (hard, real-world audio).** In a [benchmark run by pyannoteAI](https://www.pyannote.ai/blog/streaming-diarization-benchmark), a competitor of all three vendors, published 2026-10-30:

  | System | Diarization error rate | Of which, wrong speaker |
  |---|---|---|
  | Speechmatics real-time v2 | 31.3% | 5.5% |
  | Deepgram Nova-3 | 39.1% | 9.3% |
  | AssemblyAI Universal Streaming v3 | 39.2% | 11.8% |

  That AssemblyAI model may not be the Universal-3.6 Pro we use.
- **AssemblyAI's own documented limits** ([docs](https://www.assemblyai.com/docs/streaming/label-speakers-and-separate-channels)):
  - Turns shorter than about 1 s are labeled `PENDING`.
  - Overlapping speech goes to one speaker.
  - The first one or two turns may be misassigned.
  - Corrected labels arrive at most once every 2 minutes of audio, so they can't fix a live tally.

Diarization separates voices but can't say whose they are, so the app suggests names:

- Every few lines, Claude proposes names for unnamed speakers from explicit cues, and the cue decides who the name points at:
  - "I'm Sam" names the person speaking.
  - "Thanks, Yann" usually names the previous speaker.
  - "Yann, what do you think?" names the next speaker.
  - A name of someone outside the conversation names nobody.
- It never guesses from what someone talks about, and its evidence must appear verbatim in the transcript.
- A person confirms each suggestion with one click, or clicks a speaker's name to set it by hand.
- Names are passed to Claude with each statement. Whether that improves the perspectives lane is **untested**; see "Next steps".

**Better when the speakers are known in advance:** [Speechmatics real-time speaker identification](https://docs.speechmatics.com/speech-to-text/realtime/speaker-identification) takes enrolled voices, recorded from each person speaking alone, and returns their names live, with unknown voices labeled S1, S2. It isn't integrated yet.

## Model and vendor choices

### Language model, by role

The live path makes one call per sentence and needs the earliest possible first token. Push-back needs a model that holds its position under pressure. No role needs the single most capable model.

Speed and capability as measured by [Artificial Analysis](https://artificialanalysis.ai/leaderboards/models) (fetched 2026-10-31). These are medians on each provider's own API, with Artificial Analysis's prompts, not ours:

| Model (setting) | First chunk | Output speed | Intelligence Index |
|---|---|---|---|
| GPT-6.1 Terra (non-reasoning) | 0.96 s | 90 tok/s | 30 |
| DeepSeek V4.1 Flash (non-reasoning) | 1.15 s | 1,486 tok/s | 19 |
| Claude Sonnet 5.5 (low) | 1.30 s | 105 tok/s | 25 |
| GPT-6 Astra (low) | 2.02 s | 58 tok/s | 43 |
| Claude Fable 5.1 (low) | 2.27 s | 55 tok/s | 38 |
| Grok 4.7 (low) | 5.74 s | 72 tok/s | 31 |
| Claude Opus 5.5 (low) | 6.16 s | 78 tok/s | 35 |
| Claude Haiku 5.5 (low) | 7.89 s | 158 tok/s | 19 |

How to read it:

- **Reasoning rows may flatter themselves.** For reasoning settings, "first chunk" can be a reasoning token rather than the answer, so the non-reasoning rows are the clean comparisons. Our benchmark measures time to the first claim on screen, which is what counts.
- **The Intelligence Index is general** (math, coding, knowledge), not claim checking.

The choices:

- **Live path: Claude Sonnet 5.5 at low effort,** for now.
  - Among the earliest first chunks.
  - Already wired in, with structured outputs and the server-side refusal fallback.
  - The two that could beat it are GPT-6.1 Terra and DeepSeek V4.1 Flash (both non-reasoning). Deciding between them takes our benchmark plus an adapter for their APIs, which isn't built yet.
- **Not Opus 5.5 in the live path.** It takes 6.16 s to its first chunk even at low effort.
- **Push-back: Claude Sonnet 5.5 at high effort.** Push-back is user pressure. On MASK, which measures whether a model contradicts its own belief when pushed:
  - The [Opus 5.5 system card](https://www.anthropic.com/claude-opus-5-5-system-card) places Opus 5.5 below Opus 5, Sonnet 5 and Mythos 5, and reports "a modest countervailing increase in susceptibility to user pressure".
  - The [Haiku 5.5 system card](https://www.anthropic.com/claude-haiku-5-5-system-card) places Haiku 5.5 "approximately tied" with Sonnet 5.5, and above Opus 5.5.
  - The push-back prompt also states that being challenged is not evidence.
- **Facts from memory matter less here.** Opus 5.5 leads Claude models on closed-book factuality (AA-Omniscience net score 0.58, against Haiku 5.5's 0.12, per the system cards). But our verdicts must cite the search results, so memory matters less than speed.
- **Every role can be switched** for comparison: `LIVE_MODEL`, `CHECK_EFFORT`, `PUSHBACK_MODEL`, `PUSHBACK_EFFORT`.

**Specialized verifiers** are strong but not a fit yet. [LLM-AggreFact](https://llm-aggrefact.github.io/), which checks a claim against a document, is led by 7–27B specialized models:

| Model | Score |
|---|---|
| nimble-v4-max | 79.5 |
| Bespoke-MiniCheck-7B | 77.4 |
| Claude 3.5 Sonnet | 77.2 |
| GPT-4o | 75.9 |

No 2026 frontier model appears in that table. These models only answer supported or not: they don't find claims, sort them into lanes, or explain. They also need GPU hosting. Worth revisiting if the benchmark shows the verdicts are the weak point.

### Speech-to-text

**AssemblyAI Universal-3.6 Pro, with Deepgram Nova-3 supported.** Both diarize while streaming, per their API references.

On the independent [Pipecat streaming benchmark](https://github.com/pipecat-ai/stt-benchmark) (1,000 voice-agent turns, September 2026 scoring):

| Model | Mean WER | Perfect transcripts | Median time to final |
|---|---|---|---|
| AssemblyAI universal-3-6-pro | 0.89% | 88.7% | 307 ms |
| Soniox stt-rt-v5 | 1.11% | 83.6% | 260 ms |
| Speechmatics linden-1 | 1.11% | 83.9% | 369 ms |
| Deepgram nova-3-general | 1.32% | 77.7% | 247 ms |
| ElevenLabs scribe_v2_realtime | 3.05% | 80.3% | 281 ms |

- The benchmark notes that differences under about 0.25 points are within its 95% confidence interval.
- For a fact-checker a misheard number costs more than 60 ms, so AssemblyAI is the default. Set `STT_PROVIDER=deepgram` to prefer Deepgram.

### Search

**Exa `lite`** (launched 2026-10-19, replacing `instant` and `fast`). On the independent [Artificial Analysis Search Index](https://artificialanalysis.ai/agents/search-api) (data from 2026-10-19):

| API | Score |
|---|---|
| Perplexity Search (medium) | 80 |
| Exa auto | 80 |
| Exa lite | 79 |
| Perplexity Search (high) | 79 |
| Brave LLM Context | 75 |
| Parallel (advanced) | 75 |
| Parallel (basic) | 73 |

- **Exa lite and Perplexity Search tie on accuracy.** That board reports time per agentic task, not per query. Exa's own published figure for `fast`, which `lite` runs the same search as, is p50 under 425 ms. I found no per-query figure for Perplexity that I could verify.
- **Exa stays.** Racing both on every sentence is a cheap option if Perplexity proves fast in our measurements.
- **Perplexity's Sonar models**, which search and answer in one call, aren't on the Artificial Analysis leaderboard, so there is no data to choose them on.

## A three-minute walkthrough

1. **A clip, checked as it plays.** Pin *Andrej Karpathy* as a voice, choose *Video file*, and play two minutes of a LeCun talk. Verdicts land over the picture; the contested cards fill with what the search finds, each quote linked.
2. **Push back.** Dispute a verdict and watch the re-check.
3. **The room.** Switch to the microphone. One person says *"Joe Biden is the 47th president, right?"* (he was the 46th) and another answers.
4. **Common ground.** Open the recap: what was false, which questions stay open and who stands where, and the values the app leaves to you.

## Known limits

- **Nothing measured.** No live API calls yet, no measured latency, and no measured accuracy. Run the benchmark first.
- **Diarization from one microphone** has not been tested here. Overlapping speech, short interjections and similar voices are the usual trouble.
- **Long clips are untested with live speech-to-text.** How the lower third keeps up over two or three minutes of real speech, with many claims, is unknown.
- **Browser speech** has no speaker labels and no punctuation.
- **X coverage is unknown.** It's unclear how much of X Exa's index covers; perspectives also search the open web.
- **Verdicts are only as good as the sources the search finds.** That's why "unverified" exists.

## Repo map

| Path | What it is |
|---|---|
| `server/app.py` | HTTP and WebSocket server; one session per connection |
| `server/session.py` | Statements in; claims, verdicts and perspectives out; push-back; the receipts rule; speaker naming |
| `server/segmenter.py` | When a statement is complete enough to check |
| `server/llm.py`, `prompts.py`, `schemas.py` | Every Claude call, its prompt and its JSON schema |
| `server/jsonstream.py` | Reads claims out of the streaming answer as they complete |
| `server/search.py`, `backends.py` | Exa, and the backend that runs every check |
| `server/stt.py` | AssemblyAI and Deepgram streaming relays |
| `web/` | The page: `app.js`, `styles.css`, `pcm-worklet.js` |
| `bench/` | 37 labeled statements and the benchmark runner |
| `tests/` | Python tests, real-SDK wire tests, the browser test, its test server (`serve.py`) and its fixed test data |

## Tests

```bash
.venv/bin/python -m pytest
```

For the browser test, start the test server, which answers from the fixed data in `tests/fixtures.json`:

```bash
.venv/bin/python -m tests.serve 8765
```

Then run the script. It needs Playwright with Chromium (`npm install playwright` and `npx playwright install chromium`), and `CLIP` adds the video-stage checks:

```bash
CLIP=path/to/short-clip.webm node tests/e2e.cjs http://localhost:8765
```

## Working on it

- Branch off `main` and open a pull request; run both test suites first.
- `.env` holds the keys and is git-ignored. Never commit a key, and never put one in a screenshot or a log you paste.
- Numbers in this README are either measured, with how, or cited, with a link. Anything else is marked unmeasured.

## Open work, most valuable first

1. **Measure.** Run the benchmark (fast mode on and off, effort low and medium, Exa and Claude search), and time speech-to-text on real audio.
2. **Run 2–3 minute clips with live keys.** Check that the lower third keeps up, that sentences are cut where they should be, and that the recap reads well at the end.
3. **Speechmatics with enrolled voices** for when the speakers are known in advance.
4. **Test the naming hypothesis.** Run perspectives on the same contested claims with the speaker named and anonymous, and compare the verified quotes found.
5. **Compare other live models.** An adapter for GPT-6.1 Terra and DeepSeek V4.1 Flash, run through the same benchmark.
6. **Race two search providers.** Run Exa and Brave on the first search and take whichever answers first.
7. **YouTube by URL.** Check a video from its captions ahead of time.
