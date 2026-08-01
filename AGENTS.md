# Repository guidance

## Purpose

This repository generates one Markdown digest of AI news per day. It gathers only configured, reputable sources, filters for the previous calendar day in `NEWS_TIMEZONE`, summarizes with Gemini, optionally emails the result, and stores the report in `Dailynew/`.

## Layout

- `app/daily_news.py` — collector, filter, summariser, writer, and optional email sender.
- `config/sources.json` — allowlisted RSS/Atom source configuration.
- `Dailynew/` — committed generated reports; never hand-edit a generated report unless correcting it deliberately.
- `.github/workflows/daily-news.yml` — 8 AM UTC scheduled run and archive commit.
- `tests/` — standard-library unit tests.

## Engineering rules

- Keep the runtime compatible with Python 3.11+.
- Do not introduce a scraper for arbitrary webpages. Add sources as explicit first-party RSS or Atom feeds in `config/sources.json`.
- Do not make up facts, dates, release details, or citations. Preserve every source URL in the final report.
- Exclude entries without parseable publication dates instead of guessing their day.
- Never print secrets or commit `.env` files.
- Retry transient Gemini network/server failures with a bounded backoff; do not retry invalid credentials or quota errors.
- Keep email delivery optional; report generation must work without SMTP configuration.
- `EMAIL_TO` may contain comma-separated recipients; send both an HTML email and a plain-text fallback.
- Keep the source mix balanced: do not allow one source to exceed `MAX_ARTICLES_PER_SOURCE` in a digest.
- Generated file names must be `Dailynew/YYYY-MM-DD.md`.

## Verification

Run `python -m unittest discover -s tests -v` after behavior changes. For a manual smoke test without calling Gemini, test collector/filter functions with fixture XML. A full run needs `GEMINI_API_KEY` and network access.
