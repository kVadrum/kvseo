# kvseo

**The AI layer over your existing SEO stack.**

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

> **Status: v0.1 feature-complete.** The full pipeline (audit → advise → report)
> runs end-to-end and is code-reviewed, type-checked, and tested throughout. Not
> yet on PyPI — install from source today (see [Install](#install)); the tagged
> release is imminent.

---

## Quickstart

Once installed (see [Install](#install)):

```console
$ kvseo init                        # create config + a local SQLite database
$ kvseo audit https://example.com   # on-page audit + Core Web Vitals → HTML report
```

That loop needs **no API keys and no accounts**. `audit` writes a self-contained
HTML report to the current directory and prints the findings to your terminal.
Add data sources and an LLM to go deeper:

```console
$ kvseo connect psi --api-key <key>                          # PageSpeed Insights (free; raises CWV limits)
$ kvseo connect csv export.csv --site https://example.com/   # import a GSC CSV export — no OAuth
$ kvseo connect gsc                                          # …or connect Search Console via OAuth
$ export ANTHROPIC_API_KEY=…                                 # bring your own LLM key (any LiteLLM provider)
$ kvseo audit https://example.com                            # now with GSC context + an AI action list
$ kvseo cost                                                 # what the advisor spent, by provider / model
```

## What it does

- **On-page audit** — 19 checks across title, meta, headings, content, schema,
  images, and internal links, rolled into a single 0–100 score.
- **Core Web Vitals** — CrUX field data + Lighthouse lab metrics via the free
  PageSpeed Insights API (works keyless at lower rate limits).
- **Search Console context** — real query / click / impression / position data
  per URL, via OAuth **or** a plain CSV export (the no-API escape hatch).
- **AI advisor** — a prioritized "what to do next" list where every
  recommendation is traceable to a specific finding. **Bring your own LLM key**
  (Anthropic, OpenAI, Gemini, or a local Ollama — anything LiteLLM speaks to).
  kvseo never proxies your key or marks up tokens, and `kvseo cost` shows exactly
  what each run spent.
- **Reports** — a self-contained, single-file HTML report (inline CSS, embedded
  data, zero external requests, print-to-PDF friendly) plus clean Markdown.
  PDF/DOCX land in v0.2.

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
# from PyPI (once published):
pipx install kvseo

# from source (works today):
git clone https://github.com/kVadrum/kvseo && cd kvseo
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
kvseo --version
```

Config lives in `~/.config/kvseo/` and data in `~/.local/share/kvseo/` (XDG;
`%APPDATA%` / `%LOCALAPPDATA%` on Windows). Override either with the
`KVSEO_CONFIG_DIR` / `KVSEO_DATA_DIR` environment variables.

## License

[MIT](./LICENSE) — free to use, modify, and distribute. KeMeK Network © 2026.

### Trademarks

The "kvseo" and "kVadrum" names are trademarks of KeMeK Network. They are not
covered by the MIT license, and no rights to use the names are granted by this
repository. Independent forks must replace the brand name with their own.

A [kVadrum](https://kvadrum.com) Lab project, built in public.
