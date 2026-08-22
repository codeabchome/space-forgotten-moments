# Space's Forgotten Moments

Automated documentary pipeline for an English-language YouTube channel built on
NASA's public-domain archive. One 8–10 minute narrated episode plus three
vertical Shorts per run, three runs a week, entirely on free tiers.

## Sourcing rule

The narration model receives NASA archive metadata and nothing else. It is
instructed never to introduce a fact absent from that material, and a
fact-check pass then extracts every date and number from the draft and
re-verifies it against the same source, rewriting anything unsupported.
No news sites, no wikis, no model recall. Every frame is NASA public domain.

## Pipeline

| Stage | Module | What it does |
|---|---|---|
| discover | `src/discover.py` | Sweeps the archive for new subjects when the queue runs low |
| topic | `main.py` | Picks a single-topic episode or a themed compilation of 3–4 pieces |
| assets | `src/nasa.py` | Searches images.nasa.gov, classifies depth, downloads media |
| script | `src/script_gen.py` | Groq LLM writes 1100–1400 words grounded in the metadata |
| factcheck | `src/factcheck.py` | Verifies dates/numbers, surgically rewrites unsupported claims |
| voiceover | `src/tts.py` | Kokoro-82M, sentence-by-sentence, with derived word timings |
| subtitles | `src/subtitles.py` | ASS karaoke captions from the word timeline |
| visuals | `src/visuals.py` | LLM matches assets to scenes; Ken Burns push on stills |
| render | `src/render.py` | Branded intro/outro, concat, caption burn-in, loudness normalisation |
| shorts | `src/shorts.py` | Three 9:16 cuts at LLM-selected moments, captions re-timed |
| thumbnail | `src/thumbnail.py` | One strong still, navy grade, heavy title |
| upload | `src/upload.py` | YouTube Data API v3, resumable, thumbnail attached |

Every stage records completion in `state.json`. A run that dies to a runner
timeout resumes at the first incomplete stage instead of re-spending API quota.

## Word timestamps

Kokoro does not emit word-level timings. Rather than trust a TTS engine's
timestamp field, the pipeline synthesises **one sentence at a time** and measures
each sentence's duration from its own audio buffer. Words are then distributed
inside that measured window by syllable weight, with extra cost for digits and
trailing punctuation.

Timing error therefore cannot accumulate: every sentence boundary is a hard
resync point, and the worst case is a few tens of milliseconds of drift inside a
single sentence.

## Setup

### 1. Secrets

Repository → Settings → Secrets and variables → Actions:

| Secret | Where from |
|---|---|
| `GROQ_API_KEY` | console.groq.com — free tier |
| `YT_CLIENT_ID` | Google Cloud Console → OAuth client (Desktop app) |
| `YT_CLIENT_SECRET` | same |
| `YT_REFRESH_TOKEN` | `python -m src.upload --authorize` (run locally, once) |

Enable **YouTube Data API v3** in the same Google Cloud project.

### 2. Local run

```bash
pip install -r requirements.txt
python -m src.tts --fetch          # ~350 MB Kokoro weights, one time
export GROQ_API_KEY=...
python main.py --dry-run           # builds everything, uploads nothing
```

Outputs land in `output/episodes`, `output/shorts`, `output/thumbnails`.

### 3. Schedule

`.github/workflows/publish.yml` runs Mon/Wed/Fri at 13:40 UTC. Trigger manually
from the Actions tab with the dry-run box ticked to test without publishing.

## Topic queue

`config/topics.yaml` is a **cache of discovered subjects, not a hand-written
list**. `src/discover.py` sweeps 78 seed surfaces across the archive — mission
families, aeronautics testing, centers, instruments, operations — clusters what
comes back, and has the LLM name an episode only where the metadata genuinely
supports one. Clusters too thin or too generic are dropped.

`main.py` calls `discover.replenish()` before every episode and tops the queue
back up whenever fewer than 8 subjects remain, so it cannot run dry.

```bash
python -m src.discover --stats        # queue health
python -m src.discover --dry          # propose without saving
python -m src.discover --sweep 15     # discover 15 and save
```

Entries are shaped like this:

- `kind: single` — enough archive material to carry a full episode alone
- `kind: piece` — 1–4 minutes of material; three or more sharing a `theme` get
  compiled into one episode with chapter timestamps in the description

Kind is assigned from the actual asset count, not guessed:
`single_topic_min_assets` (25) and `compilation_min_assets` (6) in
`config.yaml` set the thresholds.

The pipeline marks topics `used` after a successful publish and commits the
change, so subjects never repeat. Discovery also rejects proposals whose titles
overlap an existing entry by more than 60%, which is what stops the same
subject arriving twice under different seed terms.

## Quota notes

- **images.nasa.gov** — no key, no published limit. The client still paces
  itself at ~0.4 s between pages.
- **Groq free tier** — the binding constraint. Roughly 6–9 calls per episode.
  `config.yaml` lists three models; the client walks the chain on rate-limit or
  deprecation, which is what the primary model failing looks like in practice.
- **YouTube** — an upload costs ~1600 quota units against a 10,000/day default.
  Four uploads per run (episode + three Shorts) is about 6,400. Publishing more
  than once a day needs a quota increase request.

## Brand assets

`assets/brand.py` regenerates the profile picture and channel banner
(2560×1440 with the 1546×423 safe area respected). Palette and typography live
in `config.yaml` under `brand:` and are shared with the intro card, thumbnails
and captions.

## Known constraints

- GitHub Actions runners have no GPU. Kokoro runs on ONNX CPU: budget roughly
  4–7 minutes of synthesis for a 9-minute episode.
- `zoompan` oversamples 2× before the push to stop fine archive detail from
  shimmering. This is the slowest render step; `preset: veryfast` and `crf: 23`
  in `config.yaml` are tuned to keep the job inside the runner's limits.
- Mission control audio beds are supported by `render.finalize(bed=...)` but no
  bed is wired into `main.py` yet — drop a file in and pass it through when you
  want atmosphere under the narration.
