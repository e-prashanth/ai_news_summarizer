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
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "config" / "sources.json"
OUTPUT_DIR = ROOT / "Dailynew"
USER_AGENT = "daily-ai-news-agent/1.0 (+https://github.com/)"
# USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
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
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,"Accept": "application/rss+xml, application/xml, text/xml, */*",})
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


def limit_per_source(articles: list[Article], maximum: int) -> list[Article]:
    """Keep a daily digest balanced instead of letting one busy feed dominate it."""
    source_counts: dict[str, int] = {}
    balanced: list[Article] = []
    for article in articles:
        count = source_counts.get(article.source, 0)
        if count < maximum:
            balanced.append(article)
            source_counts[article.source] = count + 1
    return balanced


def is_retryable_gemini_error(error: Exception) -> bool:
    """Retry connection and server failures, but not bad keys, prompts, or quota limits."""
    status_code = getattr(error, "code", None) or getattr(error, "status_code", None)
    return status_code not in {400, 401, 403, 404, 429}


def summarise(articles: list[Article], target_day: date, model: str) -> str:
    try:
        from google import genai
    except ImportError as error:
        raise RuntimeError("Gemini SDK is missing. Run: pip install -r requirements.txt") from error
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing. Add it to .env and try again.")
    evidence = "\n\n".join(
        f"SOURCE: {article.source}\nTITLE: {article.title}\nURL: {article.url}\nPUBLISHED (UTC): {article.published_at:%Y-%m-%d %H:%M}\nEXCERPT: {article.excerpt or '(no excerpt)'}"
        for article in articles
    )
    prompt = f"""Create the body of a concise daily AI-news digest for {target_day.isoformat()}.

Use only the supplied source records. Do not use outside knowledge, infer unreported facts, or treat a marketing claim as a verified capability. Prioritise model releases, major research, product/API changes, infrastructure or architecture changes, policy/safety announcements, and significant company news. Combine duplicates only when they describe the same announcement.

Return Markdown only. Start with `## Highlights`. For each real story, use a `###` heading, then 1-3 factual sentences and a final `Source: [publisher](exact-url)` line. Include a `## Watchlist` section only for clearly stated upcoming events. If nothing is newsworthy, say so under Highlights. Every claim must be supported by a linked supplied URL.

Source records:
{evidence}"""
    max_attempts = max(1, int(os.getenv("GEMINI_MAX_ATTEMPTS", "3")))
    for attempt in range(1, max_attempts + 1):
        client = genai.Client(api_key=api_key)
        try:
            interaction = client.interactions.create(model=model, input=prompt)
            if interaction.output_text:
                return interaction.output_text.strip()
            raise RuntimeError("Gemini returned no text for this digest.")
        except Exception as error:
            status_code = getattr(error, "code", None) or getattr(error, "status_code", None)
            if status_code == 429:
                raise RuntimeError(
                    "Gemini API quota was reached. Check the free-tier limit in Google AI Studio and retry later."
                ) from error
            if not is_retryable_gemini_error(error) or attempt == max_attempts:
                raise RuntimeError(f"Gemini API request failed after {attempt} attempt(s): {error}") from error
            delay_seconds = 2 ** (attempt - 1)
            LOGGER.warning(
                "Gemini request attempt %d/%d failed (%s). Retrying in %d second(s).",
                attempt,
                max_attempts,
                error,
                delay_seconds,
            )
            time.sleep(delay_seconds)
        finally:
            client.close()
    raise RuntimeError("Gemini API request did not return a response.")


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


def markdown_to_plaintext(markdown_text: str) -> str:
    text = re.sub(r"^#{1,6}\s+", "", markdown_text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"[*_`]+", "", text)
    return text


def inline_markdown_to_html(value: str) -> str:
    escaped = html.escape(value, quote=True)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" style="color:#2563eb;text-decoration:none;">\1</a>',
        escaped,
    )
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)


def markdown_to_email_html(markdown_text: str) -> str:
    """Render the controlled report Markdown as a simple, email-safe HTML digest."""
    blocks: list[str] = []
    list_items: list[str] = []

    def finish_list() -> None:
        if list_items:
            blocks.append("<ul>" + "".join(list_items) + "</ul>")
            list_items.clear()

    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line:
            finish_list()
            continue
        if line.startswith("# "):
            finish_list()
            blocks.append(f'<h1>{inline_markdown_to_html(line[2:])}</h1>')
        elif line.startswith("## "):
            finish_list()
            blocks.append(f'<h2>{inline_markdown_to_html(line[3:])}</h2>')
        elif line.startswith("### "):
            finish_list()
            blocks.append(f'<h3>{inline_markdown_to_html(line[4:])}</h3>')
        elif re.match(r"[-*]\s+", line):
            # ✅ FIX: Separate the regex operation from the f-string
            cleaned_line = re.sub(r"^[-*]\s+", "", line)
            list_items.append(f'<li>{inline_markdown_to_html(cleaned_line)}</li>')  
        elif line.startswith("_") and line.endswith("_"):
            finish_list()
            blocks.append(f'<p class="coverage">{inline_markdown_to_html(line.strip("_"))}</p>')
        elif line.startswith("Source:"):
            finish_list()
            blocks.append(f'<p class="source">{inline_markdown_to_html(line)}</p>')
        else:
            finish_list()
            blocks.append(f'<p>{inline_markdown_to_html(line)}</p>')
    finish_list()
    body = "\n".join(blocks)
    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f3f6fb;font-family:Arial,sans-serif;color:#172033;">
  <main style="max-width:720px;margin:0 auto;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 4px 16px rgba(23,32,51,.10);">
    <div style="padding:8px 32px;background:linear-gradient(120deg,#173b7a,#2563eb);color:#ffffff;font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;">Daily AI Briefing</div>
    <article style="padding:26px 32px;line-height:1.6;font-size:16px;">{body}</article>
    <footer style="padding:18px 32px;background:#f8fafc;color:#64748b;font-size:12px;">Generated from the configured AI news sources. Open each source link for the full story.</footer>
  </main>
  <style>h1{{margin:0 0 6px;font-size:28px;line-height:1.2;color:#0f172a}} h2{{margin:28px 0 12px;font-size:19px;color:#173b7a}} h3{{margin:22px 0 5px;font-size:17px;color:#0f172a}} p{{margin:7px 0}} .coverage{{color:#64748b;font-size:13px}} .source{{margin-top:7px;font-size:13px;font-weight:600}} ul{{padding-left:22px}}</style>
</body></html>"""


def parse_recipients(value: str) -> list[str]:
    recipients = [address for _, address in getaddresses([value.replace(";", ",")]) if address]
    if not recipients:
        raise ValueError("EMAIL_TO must contain at least one valid email address.")
    return recipients


def send_email(subject: str, markdown: str) -> bool:
    required = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"]
    if not all(os.getenv(name) for name in required):
        LOGGER.info("SMTP is not configured; skipping email delivery.")
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ["EMAIL_FROM"]
    recipients = parse_recipients(os.environ["EMAIL_TO"])
    message["To"] = ", ".join(recipients)
    message.set_content(markdown_to_plaintext(markdown))
    message.add_alternative(markdown_to_email_html(markdown), subtype="html")
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=30) as server:
        if (os.getenv("SMTP_USE_TLS") or "true").lower() == "true":
            server.starttls(context=ssl.create_default_context())
        server.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        refused = server.send_message(message, to_addrs=recipients)
    if refused:
        raise RuntimeError(f"SMTP server refused {len(refused)} recipient(s).")
    LOGGER.info("SMTP server accepted the email for %d recipient(s).", len(recipients))
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
    max_per_source = max(1, int(os.getenv("MAX_ARTICLES_PER_SOURCE", "4")))
    selected = limit_per_source(selected, max_per_source)[:max_articles]
    if selected:
        try:
            body = summarise(selected, target_day, os.getenv("GEMINI_MODEL", "gemini-3.6-flash"))
        except RuntimeError as error:
            LOGGER.error("%s", error)
            return 1
    else:
        body = "## Highlights\n\nNo qualifying items were found in the configured source feeds."
    report = render_report(target_day, timezone_name, body, failures)
    output_path = write_report(target_day, report)
    send_email(f"Daily AI News — {target_day.isoformat()}", report)
    LOGGER.info("Wrote %s using %d source articles.", output_path.relative_to(ROOT), len(selected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
