# kvseo

**The AI layer over your existing SEO stack.**

`kvseo` is an AI-native SEO copilot for solo operators and small agencies. It
doesn't replace your data tools — it sits on top of them. Point it at a URL,
let it pull Google Search Console and Core Web Vitals context, and it produces
a prioritized, *reasoned* action list and a client-ready report.

The data layer is a commodity. What `kvseo` adds is the layer the others skip:
**synthesis, AI-assisted prioritization, and agency-shaped reporting.**

> **Status: early development (pre-alpha).** Not yet released to PyPI. The CLI
> scaffold runs (`kvseo init`, `kvseo --version`); the audit engine, GSC/PSI
> connectors, advisor, and report renderer are in active build toward v0.1.

---

## What it does (v0.1 target)

```console
$ pipx install kvseo
$ kvseo init                       # creates config + local SQLite database
$ kvseo connect gsc                # OAuth flow for Google Search Console
$ kvseo audit https://example.com  # on-page audit + Core Web Vitals + GSC context
$ kvseo report --format html       # self-contained HTML report (print-to-PDF in your browser)
```

- **On-page audit** — title, meta, schema, heading hierarchy, internal links.
- **Core Web Vitals** — via the free PageSpeed Insights API.
- **GSC context** — last-90-day query/click/impression data for the URL.
- **AI advisor** — a prioritized "what to do this week" list, every claim
  traceable to a source. **Bring your own LLM key** (Anthropic, OpenAI, Gemini,
  or local Ollama — anything LiteLLM speaks to). We never proxy your keys or
  mark up tokens.
- **Reports** — a self-contained HTML report (embedded images and fonts) plus
  the Markdown source. PDF/DOCX land in v0.2.

The audit and HTML report work with **no LLM key at all** — the advisor is an
optional enhancement, unlocked when you add a provider key.

## Principles

- **BYO-key.** kvseo never ships or proxies an LLM key.
- **Local-first.** Your data lives in a single SQLite file on your machine.
- **Read-only.** kvseo pulls from APIs you authorize; it never writes upstream.
- **No telemetry.** No phone-home, no usage metrics. Ever, unless you opt in.

## Install

Requires Python 3.12+.

```console
# once published:
pipx install kvseo
# or, from source (development):
git clone https://github.com/kvadrum/kvseo && cd kvseo
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## License

MIT — see [LICENSE](LICENSE). Copyright © 2026 KeMeK Network.

"kvseo" and "kVadrum" are trademarks of KeMeK Network; the MIT license covers
the code, not the marks.

A [kVadrum](https://kvadrum.com) Lab project.
