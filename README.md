# kvseo

**The AI layer over your existing SEO stack.**

[![PyPI](https://img.shields.io/pypi/v/kvseo.svg)](https://pypi.org/project/kvseo/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![Built in public](https://img.shields.io/badge/built%20in%20public-kVadrum-e2725b.svg)](https://kvadrum.com)

`kvseo` is an AI-native SEO copilot for solo operators and small agencies. It
doesn't replace your data tools — it sits on top of them. Point it at a URL and
it runs an on-page audit, folds in Core Web Vitals and Search Console context,
and produces a prioritized, *reasoned* action list plus a client-ready report.

The data layer is a commodity. What `kvseo` adds is the layer the others skip:
**synthesis, AI-assisted prioritization, and agency-shaped reporting** — as a
fast, local, single-command CLI.

> **Status: pre-1.0, in active development — [on PyPI](https://pypi.org/project/kvseo/).**
> Everything planned for the first milestone is built: the full pipeline
> (audit → advise → report) runs end-to-end and is code-reviewed, type-checked,
> and tested throughout.

---

## Quickstart

```console
$ pipx install kvseo
$ kvseo init                        # create config + a local SQLite database
$ kvseo audit https://example.com   # on-page audit + Core Web Vitals → HTML report
```

That loop needs **no API keys and no accounts**. `audit` writes a self-contained
HTML report to the current directory and prints the findings to your terminal.
Add data sources and an LLM to go deeper:

```console
$ kvseo connect psi --api-key <key>                          # PageSpeed Insights (free; raises CWV limits)
$ kvseo connect csv export.csv --site https://example.com/   # import a GSC CSV export — no OAuth
$ kvseo connect gsc --client-secrets client_secret.json      # …or connect Search Console live (see below)
$ export ANTHROPIC_API_KEY=…                                 # bring your own LLM key (any LiteLLM provider)
$ kvseo audit https://example.com                            # now with GSC context + an AI action list
$ kvseo cost                                                 # what the advisor spent, by provider / model
```

### Connecting Search Console

kvseo does not ship a shared Google OAuth client yet, so a live connection uses
your own (it stays read-only: the only scope requested is
`webmasters.readonly`):

1. In the [Google Cloud console](https://console.cloud.google.com/), enable the
   **Google Search Console API** and create an OAuth client of type
   **Desktop app**.
2. Download its JSON and run `kvseo connect gsc --client-secrets <file>`
   (or set `KVSEO_GSC_CLIENT_ID` and `KVSEO_GSC_CLIENT_SECRET` and run
   `kvseo connect gsc`). A browser opens for consent; kvseo then lists the
   properties it can read.

From then on every `kvseo audit` pulls the last 90 days of queries for the
audited page from whichever of your properties covers it (`--no-gsc` skips
that). A page outside all of them is audited as usual, without GSC context.
The CSV import above needs none of this.

### Rankings and keyword data

Three more sources feed the advisor, each optional and each read-only:

```console
$ kvseo connect serpbear --url https://serpbear.example.com --token <APIKEY>   # your SerpBear's tracked rankings
$ kvseo connect dataforseo --login <login> --password <password>               # your DataForSEO account
$ kvseo connect openseo --token oseo_…                                         # OpenSEO, hosted or self-hosted (--url)
$ kvseo connectors                                                             # what is connected
```

With [SerpBear](https://github.com/towfiqi/serpbear) connected, an audit reads
the rankings it already tracks for the audited domain — the page's own keywords
and the `--keyword` target. With `--keyword`, an audit also fetches that
keyword's monthly search volume, CPC and competition from DataForSEO, or from
[OpenSEO](https://github.com/every-app/open-seo) when only that is connected
(OpenSEO is built on DataForSEO, so kvseo never asks both). DataForSEO bills
per call and OpenSEO spends credits, so keyword data is cached for a week.
`connect` checks the credentials before storing them; they live in your OS
keyring, or come from `SERPBEAR_URL` / `SERPBEAR_API_KEY`, `DATAFORSEO_LOGIN` /
`DATAFORSEO_PASSWORD`, `OPENSEO_API_KEY` / `OPENSEO_URL`.

No account for any of them? `kvseo connect csv` also imports a rank tracker's
export (`--schema rankings`) or a keyword-volume export such as Google Ads
Keyword Planner's (`--schema keywords`).

## What it does

- **On-page audit** — 24 checks across title, meta, headings, content, schema,
  images, and links, rolled into a single 0–100 score. An opt-in probe
  (`--check-internal-links`) HEAD-checks internal links and reports the broken
  ones.
- **Core Web Vitals** — CrUX field data + Lighthouse lab metrics via the free
  PageSpeed Insights API (works keyless at lower rate limits).
- **Search Console context** — real query / click / impression / position data
  per URL, via OAuth **or** a plain CSV export (the no-API escape hatch).
  Striking-distance queries (positions 5–20) get their own report section.
- **Rankings and demand** — tracked positions from SerpBear, and search volume
  for your target keyword from DataForSEO or OpenSEO (or CSV exports of either).
- **AI advisor** — a prioritized "what to do next" list where every
  recommendation is traceable to a specific finding. **Bring your own LLM key**
  (Anthropic, OpenAI, Gemini, or a local Ollama — anything LiteLLM speaks to).
  kvseo never proxies your key or marks up tokens, and `kvseo cost` shows exactly
  what each run spent.
- **Reports** — a self-contained, single-file HTML report (inline CSS, embedded
  data, zero external requests, print-to-PDF friendly) plus clean Markdown.
  PDF/DOCX land in v0.2.
- **Scriptable** — `kvseo audit --json` emits the full audit (score, per-check
  findings, advisor status) as JSON for pipelines and AI agents.

Everything **degrades gracefully**: the audit and HTML report work with no LLM
key at all — the advisor is an optional layer you unlock by adding a provider
key. No PSI key → the Core Web Vitals checks skip; no Search Console → the audit
still scores the page.

## Principles

- **BYO-key.** kvseo never ships, proxies, or marks up an LLM key.
- **Local-first.** Everything lives in one SQLite file on your machine — no
  account, no cloud.
- **Read-only.** kvseo pulls from APIs you authorize; it never writes upstream.
- **No telemetry.** No phone-home, no usage metrics — not unless you opt in.
- **Reproducible.** Every audit and advisor run is stored and re-runnable; the
  advisor works from stored data, so you can re-prioritize without re-fetching.

## Install

Requires **Python 3.12+**.

```console
pipx install kvseo        # or: pip install kvseo

# from source, for development:
git clone https://github.com/kVadrum/kvseo && cd kvseo
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
kvseo --version
```

Config lives in `~/.config/kvseo/` and data in `~/.local/share/kvseo/` (XDG;
`%APPDATA%` / `%LOCALAPPDATA%` on Windows). Override either with the
`KVSEO_CONFIG_DIR` / `KVSEO_DATA_DIR` environment variables.

## Backups

Your history is one SQLite file, so keeping a copy is cheap:

```console
$ kvseo db backup                        # timestamped copy into <data dir>/backups/
$ kvseo db backup --output ~/kvseo.db    # …or wherever you want it
$ kvseo db vacuum                        # compact the file and reclaim free space
```

`db backup` uses SQLite's online backup API, so it is safe to run while kvseo is
working — unlike copying the file yourself. Filesystem snapshots (ZFS, btrfs,
Time Machine) cover you too, for the same single-file reason.

## License

[MIT](./LICENSE) — free to use, modify, and distribute. KeMeK Network © 2026.

### Names

The "kvseo" and "kVadrum" names are not covered by the MIT license. KeMeK
Network reserves all rights in them. Independent forks must replace the name
with their own.

A [kVadrum](https://kvadrum.com) Lab project, built in public.
