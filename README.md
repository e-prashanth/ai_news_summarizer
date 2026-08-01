# Daily AI News Agent

This Python agent collects AI news from a small allowlist of primary sources, selects items published on the previous calendar day, asks Gemini to create a concise, source-linked digest, and saves it as Markdown in `Dailynew/`.

It is designed to run at 8:00 AM India Standard Time (IST) using GitHub Actions. The workflow commits the generated report back to the repository. Email delivery is optional and uses SMTP when the required secrets are present.

## What it covers

- Model launches and updates
- Research, product, API, and architecture announcements
- Major AI announcements from OpenAI, Google DeepMind, Meta AI, Hugging Face, Microsoft Research, and reputable AI-focused reporting outlets

Only items from the configured sources are used. The model is instructed to retain source links, distinguish announcements from speculation, and say when there were no qualifying stories.

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# add GEMINI_API_KEY to .env
python -m app.daily_news
```

The command creates yesterday's report by default. To generate a particular date, use:

```bash
python -m app.daily_news --date 2026-07-31
```

Reports are written to `Dailynew/YYYY-MM-DD.md`. `Dailynew/` is intentionally committed so the repository forms an archive.

## Configuration

Required:

- `GEMINI_API_KEY` — API key for the Gemini summarisation step.

Optional:

- `GEMINI_MODEL` — defaults to `gemini-3.6-flash`.
- `GEMINI_MAX_ATTEMPTS` — retries temporary Gemini network/server errors; defaults to `3` total attempts.
- `NEWS_TIMEZONE` — IANA timezone used to decide what “previous day” means; defaults to `Asia/Kolkata`.
- `MAX_ARTICLES` — maximum source articles given to the summariser; defaults to `20`.
- `MAX_ARTICLES_PER_SOURCE` — limits a busy publisher to four articles so one company or outlet cannot dominate the digest.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO` — enable email delivery. `SMTP_USE_TLS` defaults to `true`.

Source feeds live in `config/sources.json`. Prefer first-party newsrooms and RSS/Atom feeds; add a source only after checking that it has a stable feed and an identifiable publisher.

## GitHub Actions setup

1. Push this directory to a GitHub repository.
2. In **Settings → Secrets and variables → Actions**, add `GEMINI_API_KEY` as a repository secret. Your local `.env` file is not uploaded to GitHub.
3. For email, also add the SMTP/`EMAIL_*` variables above as repository secrets. `EMAIL_TO` accepts one or more comma-separated addresses, for example `person1@example.com, person2@example.com`.
4. The included workflow runs at `08:00 IST` (02:30 UTC) every day and can be run manually from the Actions tab.

GitHub Actions cron uses UTC. IST is UTC+05:30 and does not observe daylight saving time, so `30 2 * * *` always means 8:00 AM IST. If you later choose another timezone, update `.github/workflows/daily-news.yml` for its UTC time.

The workflow has `contents: write` permission and commits only files under `Dailynew/`. For protected branches, allow GitHub Actions to create commits or change the workflow to open a pull request.

## Development

```bash
python -m unittest discover -s tests -v
```

The collector uses the Python standard library for HTTP and RSS/Atom parsing. The only package dependencies are the official Google Gen AI Python SDK and `python-dotenv`. Email messages are sent as clean HTML with a plain-text fallback.

## Safety and quality notes

- Do not put API keys or SMTP passwords in `.env` under version control.
- The generated digest is a summary of linked primary-source material, not independent reporting.
- Feed publication dates can be missing or inaccurate. Such entries are excluded rather than silently assigned to the target day.
