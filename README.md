# agent-radio-content-manager

Find and extract internet radio stream URLs from four platforms
(`onlineradiobox`, `streema`, `radionet`, `tunein`), and use them to repair dead
links in a station database.

Two things it can do:

- **Query mode** (`--source query`) — search a station name and write matches to CSV.
- **DB workflow** (`--source db`) — read pending stations from a MySQL table,
  check whether each existing stream still plays, find a replacement when it
  doesn't, and write the result back.

Three extraction modes:

- **`rule`** — deterministic per-platform extractors (fast, no API cost). This is
  the recommended and most accurate mode.
- **`agent`** — an OpenAI-compatible LLM drives a fixed browser toolset via
  tool-calling, guided by per-platform hints. Works with hosted models
  (`gpt-4o-mini`) or a local server (llama.cpp / Qwen).
- **`mcp`** — a vision LLM drives the official Playwright MCP server and *sees*
  page screenshots (handles popups, reads on-screen info). Query mode only.
  Requires a vision model that supports tool-calling (see caveat below).

## Architecture: two-phase extraction

Each platform implements a `Platform` with two steps (`platforms/base.py`):

- **`search(page, query)`** — return ALL candidates (name, country, ref) for the
  query. No stream fetch, no cap.
- **`resolve(page, candidate)`** — fetch the actual stream URL for one candidate.

This split matters: search engines rank noisily, so the code extracts *every*
candidate, filters them by a name + country **relevance** check
(`relevance.py`), and only then resolves (and verifies) the survivors. This
avoids the common failure where the correct station is buried below a cap of
same-name / same-frequency stations worldwide.

`extract()` (search → relevance-filter → resolve, capped) is the default used by
query mode. The DB workflow drives search → filter → resolve → **playability
check** itself.

## How each platform is scraped (rule mode)

| Platform | Search | Country source | Stream source |
|---|---|---|---|
| onlineradiobox | `/search?q=` (name only) | ISO code in station URL, e.g. `/za/…` | `stream` attribute in the search-page DOM (inline) |
| radionet | `prod.radio-api.net/stations/search` JSON API | `country` field in the API | first entry of the API `streams` array |
| tunein | `opml.radiotime.com/Search.ashx` JSON; results **name-filtered** to the query | `Describe.ashx` `location` (`City, Country`) | `Tune.ashx?id=<gid>` (skips `notcompatible` placeholders) |
| streema | `/radios/search/?q=` | station page text | station page `#spot-play-button[data-url]`; `/go/?url=` unwrapped; popups sniffed; external stations flagged |

The station **name only** (frequency and country stripped) is typed into each
search box; country is applied afterwards by the relevance filter. This keeps a
noisy search from burying the target.

## Stream validation

A candidate stream is only accepted after **ffprobe** confirms it actually plays
(`validate.py`). ffprobe is used instead of HTTP header checks because endless
Icecast/SHOUTcast streams have no `Content-Length` and would look "dead" to a
plain HTTP read. **ffprobe must be installed** (`ffprobe -version`) for the DB
workflow's verification and the `validate` helpers.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env   # then edit (OpenAI key and/or DB credentials)
# ffprobe (ffmpeg) must be on PATH for playability checks:
#   Debian/Ubuntu: sudo apt install ffmpeg
```

## Usage

### Query mode (CSV)

```bash
# Rule mode, all platforms
python -m radiofinder --source query -q "Heart FM, South Africa" --mode rule

# Agent mode, specific platforms
python -m radiofinder --source query -q "BBC Radio 2, United Kingdom" \
    --mode agent --platforms radionet tunein

# Limit per platform and choose the output file
python -m radiofinder --source query -q "jazz" -m rule -n 5 -o output/jazz.csv
```

### DB workflow

```bash
# Process up to 50 pending stations (rule mode), writing results back
python -m radiofinder --source db --mode rule --db-limit 50

# See what it WOULD do without writing
python -m radiofinder --source db --mode rule --db-limit 50 --dry-run

# Verbose logging
python -m radiofinder --source db --mode rule --db-limit 50 -v
```

A live progress bar shows per-station progress with a running tally
(`ok`/`new`/`dead`/`err`); log lines are timestamped and don't corrupt the bar.

### CLI options

| Flag | Purpose |
|---|---|
| `--source {query,db}` | `query` = CSV search; `db` = repair the DB table |
| `--query, -q` | Station search text (query mode) |
| `--mode, -m {rule,agent,mcp}` | Extraction mode |
| `--platforms, -p` | Subset of `onlineradiobox radionet streema tunein` (default: all) |
| `--limit, -n` | Max stations per platform (query mode) |
| `--db-limit` | Max pending stations to process (DB mode) |
| `--dry-run` | DB mode: compute decisions, write nothing |
| `--output, -o` | CSV path (query mode; default from `OUTPUT_CSV`) |
| `--verbose, -v` | DEBUG-level logging |
| `--no-relevance-filter` | Keep every candidate, even off-query ones |

## DB workflow: status semantics

The worker reads rows with **`status = 0`** from `down_station_automation`,
groups them by `st_id` (duplicate rows for one station are updated together),
and for each station:

1. If the existing `stream_link` still plays → **`status = 1`**;
   `new_stream_link`/`source` are cleared (the existing link is the good one).
2. If it's dead, search all platforms, relevance-filter, resolve + ffprobe-verify:
   - a working replacement is found → **`status = 1`**, `new_stream_link` = the
     stream, `source` = the platform it came from.
   - nothing works → **`status = 2`**, `new_stream_link`/`source` cleared.

`updated_dt` is bumped on every write; `added_dt` is never touched. A station
that errors mid-processing is logged and left at `status = 0` (still pending), so
one failure never aborts the batch and nothing is silently lost.

Schema is in `schema.sql`. URL columns are `varchar(512)` (some HLS/GUID stream
URLs exceed 255 chars).

## Environment (.env)

| Var | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | - | Agent/MCP mode (use `local` for a key-less local server) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Chat model for agent/MCP mode |
| `OPENAI_BASE_URL` | - | OpenAI-compatible endpoint (local llama.cpp, Azure, …) |
| `HEADLESS` | `true` | Run the browser headless |
| `NAV_TIMEOUT_MS` | `30000` | Per-navigation timeout |
| `STREAM_WAIT_SECONDS` | `6` | Wait after clicking play (streema/network sniffing) |
| `OUTPUT_CSV` | `output/streams.csv` | Default CSV path (query mode) |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | `127.0.0.1` / `3306` / `root` / - / `new_schema` | MySQL connection for the DB workflow |

## Query format (precision)

```
"<Station Name>, <Country>"      e.g. "Heart FM - 104.9 FM, South Africa"
```

The country (last comma-separated segment) enforces the correct country via the
relevance filter; frequency/callsign clutter is stripped from the search string
automatically. Country codes/aliases work (`USA`, `England`, `uk`).

## Local models (agent/MCP mode without a cloud key)

`docker-compose.llama.yml` runs a local llama.cpp OpenAI-compatible server tuned
for a 12 GB GPU. Point `OPENAI_BASE_URL` at it and set `OPENAI_API_KEY=local`
(the SDK requires a non-empty key; local servers ignore it).

- **Text/tool-calling models** (agent mode): Qwen3 GGUF (e.g.
  `Qwen/Qwen3-8B-GGUF:Q4_K_M`) do tool-calling well.
- **Vision (MCP mode):** needs a VL model *with* its `mmproj` projector (e.g.
  `unsloth/Qwen2.5-VL-7B-Instruct-GGUF:Q4_K_M`). Caveat: Qwen2.5-VL's GGUF chat
  template does **not** support tool-calling in llama.cpp today, so MCP mode
  cannot currently drive tools with that model. Tool-calling (Qwen3 text) and
  vision (Qwen2.5-VL) aren't available in one local model yet.

## CSV columns (query mode)

`platform, query, name, country, stream_url, stream_type, page_url, station_id, mode, extracted_at`

Rows are de-duplicated on `(platform, stream_url)`, including across reruns.

## Project layout

```
radiofinder/
  __main__.py       CLI entry (argparse, logging setup)
  runner.py         query-mode orchestration (search -> filter -> resolve)
  db_workflow.py    DB workflow (status logic, progress bar, ffprobe verify)
  db.py             MySQL access (by-st_id updates)
  relevance.py      name + country relevance filter, search-term cleaning
  countries.py      country name/alias/code canonicalisation
  validate.py       ffprobe playability check
  browser.py        Playwright helpers, consent dismissal, stream sniffing
  agent.py          agent mode (fixed toolset, tool-calling loop)
  agent_tools.py    the browser tools exposed to the agent
  agent_mcp.py      mcp mode (Playwright MCP + vision)
  logging_setup.py  console logger that cooperates with the progress bar
  models.py         StationResult dataclass
  storage.py        CSV storage (Storage interface for future backends)
  platforms/        base.py + onlineradiobox/radionet/tunein/streema
```
