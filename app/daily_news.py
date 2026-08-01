"""Collect, summarise, archive, and optionally email daily AI news."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "config" / "sources.json"
OUTPUT_DIR = ROOT / "Dailynew"
USER_AGENT = "daily-ai-news-agent/1.0 (+https://github.com/)"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Article:
    source: str
    title: str
    url: str
    published_at: datetime
    excerpt: str


def strip_html(value: str) -> str:
    """Make an RSS description compact enough for a model prompt."""
    text = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def element_text(parent: ET.Element, names: set[str]) -> str:
    for child in parent.iter():
        if local_name(child) in names and child.text:
            return child.text.strip()
    return ""


def atom_link(entry: ET.Element) -> str:
    for child in entry:
        if local_name(child) == "link" and child.get("href") and child.get("rel", "alternate") == "alternate":
            return child.get("href", "")
    for child in entry:
        if local_name(child) == "link" and child.get("href"):
            return child.get("href", "")
    return ""


def parse_feed(xml: bytes, source: str) -> list[Article]:
    """Parse both RSS and Atom without trusting a third-party HTML scraper."""
    root = ET.fromstring(xml)
    articles: list[Article] = []
    for entry in root.iter():
        tag = local_name(entry)
        if tag not in {"item", "entry"}:
            continue
        title = element_text(entry, {"title"})
        url = atom_link(entry) if tag == "entry" else element_text(entry, {"link"})
        published = parse_datetime(element_text(entry, {"pubdate", "published", "updated", "date"}))
        excerpt = strip_html(element_text(entry, {"description", "summary", "content"}))
        if title and url and published:
            articles.append(Article(source, title, url, published, excerpt[:900]))
    return articles


def load_sources(path: Path = SOURCES_PATH) -> list[dict[str, str]]:
    sources = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(sources, list) or not all({"name", "feed_url"} <= set(item) for item in sources):
        raise ValueError("config/sources.json must be a list with name and feed_url fields")
    return sources


def fetch_feed(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def collect_articles(sources: list[dict[str, str]]) -> tuple[list[Article], list[str]]:
    articles: list[Article] = []
    failures: list[str] = []
    for source in sources:
        try:
            articles.extend(parse_feed(fetch_feed(source["feed_url"]), source["name"]))
        except (OSError, ET.ParseError, ValueError) as error:
            LOGGER.warning("Could not collect %s: %s", source["name"], error)
            failures.append(source["name"])
    return articles, failures


def select_for_day(articles: list[Article], target_day: date, report_timezone: ZoneInfo) -> list[Article]:
    seen: set[str] = set()
    selected: list[Article] = []
    for article in sorted(articles, key=lambda item: item.published_at, reverse=True):
        key = article.url.rstrip("/")
        if article.published_at.astimezone(report_timezone).date() == target_day and key not in seen:
            seen.add(key)
            selected.append(article)
    return selected


def summarise(articles: list[Article], target_day: date, model: str) -> str:
    try:
        from openai import OpenAI, RateLimitError
    except ImportError as error:
        raise RuntimeError("OpenAI SDK is missing. Run: pip install -r requirements.txt") from error
    evidence = "\n\n".join(
        f"SOURCE: {article.source}\nTITLE: {article.title}\nURL: {article.url}\nPUBLISHED (UTC): {article.published_at:%Y-%m-%d %H:%M}\nEXCERPT: {article.excerpt or '(no excerpt)'}"
        for article in articles
    )
    prompt = f"""Create the body of a concise daily AI-news digest for {target_day.isoformat()}.

Use only the supplied source records. Do not use outside knowledge, infer unreported facts, or treat a marketing claim as a verified capability. Prioritise model releases, major research, product/API changes, infrastructure or architecture changes, policy/safety announcements, and significant company news. Combine duplicates only when they describe the same announcement.

Return Markdown only. Start with `## Highlights`. For each real story, use a `###` heading, then 1-3 factual sentences and a final `Source: [publisher](exact-url)` line. Include a `## Watchlist` section only for clearly stated upcoming events. If nothing is newsworthy, say so under Highlights. Every claim must be supported by a linked supplied URL.

Source records:
{evidence}"""
    try:
        response = OpenAI().responses.create(model=model, input=prompt)
    except RateLimitError as error:
        if getattr(error, "code", None) == "insufficient_quota":
            raise RuntimeError(
                "The OpenAI API key has no available quota. Add API billing or credits in "
                "the OpenAI Platform, then run the command again."
            ) from error
        raise RuntimeError("The OpenAI API rate limit was reached. Wait a few minutes and retry.") from error
    return response.output_text.strip()


def render_report(target_day: date, report_timezone: str, body: str, failures: list[str]) -> str:
    report = f"# Daily AI News — {target_day.isoformat()}\n\n"
    report += f"_Coverage: items published on {target_day.isoformat()} in `{report_timezone}`._\n\n"
    report += body.strip() + "\n"
    if failures:
        report += "\n## Collection notes\n\n"
        report += "The following configured feeds could not be collected: " + ", ".join(sorted(failures)) + ".\n"
    return report


def write_report(target_day: date, content: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"{target_day.isoformat()}.md"
    output_path.write_text(content, encoding="utf-8")
    return output_path


def send_email(subject: str, markdown: str) -> bool:
    required = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"]
    if not all(os.getenv(name) for name in required):
        LOGGER.info("SMTP is not configured; skipping email delivery.")
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ["EMAIL_FROM"]
    message["To"] = os.environ["EMAIL_TO"]
    message.set_content(markdown)
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=30) as server:
        if (os.getenv("SMTP_USE_TLS") or "true").lower() == "true":
            server.starttls(context=ssl.create_default_context())
        server.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        server.send_message(message)
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="Report date in YYYY-MM-DD; defaults to yesterday.")
    return parser.parse_args()


def main() -> int:
    # Local development uses .env; GitHub Actions supplies the same values as secrets.
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    timezone_name = os.getenv("NEWS_TIMEZONE", "Asia/Kolkata")
    try:
        report_timezone = ZoneInfo(timezone_name)
    except Exception as error:
        raise SystemExit(f"Invalid NEWS_TIMEZONE {timezone_name!r}: {error}") from error
    target_day = args.date or (datetime.now(report_timezone).date() - timedelta(days=1))
    articles, failures = collect_articles(load_sources())
    selected = select_for_day(articles, target_day, report_timezone)
    max_articles = max(1, int(os.getenv("MAX_ARTICLES", "20")))
    selected = selected[:max_articles]
    if selected:
        body = summarise(selected, target_day, os.getenv("OPENAI_MODEL", "gpt-5"))
    else:
        body = "## Highlights\n\nNo qualifying items were found in the configured source feeds."
    report = render_report(target_day, timezone_name, body, failures)
    output_path = write_report(target_day, report)
    send_email(f"Daily AI News — {target_day.isoformat()}", report)
    LOGGER.info("Wrote %s using %d source articles.", output_path.relative_to(ROOT), len(selected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
