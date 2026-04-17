import argparse
import email.utils
import html
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


USER_AGENT = "Mozilla/5.0 (compatible; ProductFeedScanner/1.0)"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_JSON_FILE = "feed_seed_products.json"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RETRIES = 2
DEFAULT_RETRY_DELAY_SECONDS = 5
RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


FEEDS = [
    {
        "id": "ph_ai",
        "name": "Product Hunt AI",
        "url": "https://www.producthunt.com/feed?category=artificial-intelligence",
        "kind": "atom_producthunt",
        "tier": "main",
    },
    {
        "id": "ph_devtools",
        "name": "Product Hunt Developer Tools",
        "url": "https://www.producthunt.com/feed?category=developer-tools",
        "kind": "atom_producthunt",
        "tier": "supplemental",
    },
    {
        "id": "hn_launches",
        "name": "HN Launches",
        "url": "https://hnrss.org/launches",
        "kind": "rss_hn_launches",
        "tier": "main",
    },
    {
        "id": "hn_show",
        "name": "HN Show",
        "url": "https://hnrss.org/show",
        "kind": "rss_hn_show",
        "tier": "exploratory",
    },
]


URL_RE = re.compile(r"https?://[^\s<>\)\"']+")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


@dataclass
class FeedItem:
    product_name: str
    initial_url: str
    source_id: str
    source_name: str
    source_tier: str
    published_at: str
    summary: str
    raw_title: str
    raw_link: str
    other_urls: list[str] = field(default_factory=list)


def is_retryable_fetch_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError)):
        return True

    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_HTTP_STATUS_CODES

    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout, ConnectionError)):
            return True

        message = str(reason).lower()
        retry_signals = (
            "timed out",
            "timeout",
            "temporary failure",
            "connection reset",
            "connection aborted",
            "connection refused",
            "network is unreachable",
            "reset by peer",
            "unexpected eof",
        )
        return any(signal in message for signal in retry_signals)

    return False


def format_fetch_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}: {exc.reason}"
    return str(exc)


def fetch_text(
    url: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    label: str | None = None,
) -> str:
    attempts = max(retries, 0) + 1
    target = label or url
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with NO_PROXY_OPENER.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            last_error = exc
            if not is_retryable_fetch_error(exc) or attempt >= attempts:
                break

            wait_seconds = max(retry_delay_seconds, 0) * attempt
            print(
                f"[retry] {target} attempt {attempt}/{attempts - 1} failed: "
                f"{format_fetch_error(exc)} ; waiting {wait_seconds}s before retry",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

    assert last_error is not None
    raise last_error


def clean_text(value: str) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = TAG_RE.sub(" ", value)
    return WS_RE.sub(" ", value).strip()


def parse_iso_or_rfc_date(value: str) -> tuple[float, str]:
    if not value:
        return 0.0, ""

    value = value.strip()

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp(), dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        pass

    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp(), dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, IndexError):
        return 0.0, value


def extract_all_urls(text: str) -> list[str]:
    return [match.group(0).rstrip(".") for match in URL_RE.finditer(text or "")]


def extract_hn_product_name(title: str, prefix: str) -> str:
    text = title.strip()
    if text.startswith(prefix):
        text = text[len(prefix):].strip()

    # Support titles like "Launch HN: Name - desc" and "Launch HN: Name 鈥?desc".
    text = re.split(r"\s+(?:-|\u2013|\u2014)\s+", text, maxsplit=1)[0]
    text = re.sub(r"\([^)]*\)", "", text).strip()
    return text or title.strip()


def parse_producthunt_atom(feed: dict, xml_text: str) -> list[tuple[float, FeedItem]]:
    root = ET.fromstring(xml_text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    parsed_items: list[tuple[float, FeedItem]] = []

    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        published_raw = (
            entry.findtext("atom:published", default="", namespaces=ns)
            or entry.findtext("atom:updated", default="", namespaces=ns)
        )
        published_ts, published_display = parse_iso_or_rfc_date(published_raw)

        raw_link = ""
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("rel") == "alternate":
                raw_link = link.attrib.get("href", "").strip()
                break

        content_html = entry.findtext("atom:content", default="", namespaces=ns) or ""
        summary = clean_text(content_html)
        urls = extract_all_urls(html.unescape(content_html))

        parsed_items.append(
            (
                published_ts,
                FeedItem(
                    product_name=title,
                    initial_url=raw_link,
                    source_id=feed["id"],
                    source_name=feed["name"],
                    source_tier=feed["tier"],
                    published_at=published_display,
                    summary=summary,
                    raw_title=title,
                    raw_link=raw_link,
                    other_urls=[url for url in urls if url != raw_link],
                ),
            )
        )

    return parsed_items


def parse_hn_rss(feed: dict, xml_text: str, prefix: str) -> list[tuple[float, FeedItem]]:
    root = ET.fromstring(xml_text)
    parsed_items: list[tuple[float, FeedItem]] = []

    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        raw_link = (item.findtext("link") or "").strip()
        description = item.findtext("description") or ""
        summary = clean_text(description)
        published_raw = item.findtext("pubDate") or ""
        published_ts, published_display = parse_iso_or_rfc_date(published_raw)

        urls = extract_all_urls(html.unescape(description))
        initial_url = urls[0] if urls else raw_link
        product_name = extract_hn_product_name(title, prefix)

        parsed_items.append(
            (
                published_ts,
                FeedItem(
                    product_name=product_name,
                    initial_url=initial_url,
                    source_id=feed["id"],
                    source_name=feed["name"],
                    source_tier=feed["tier"],
                    published_at=published_display,
                    summary=summary,
                    raw_title=title,
                    raw_link=raw_link,
                    other_urls=[url for url in urls if url != initial_url],
                ),
            )
        )

    return parsed_items


def parse_feed(feed: dict, xml_text: str) -> list[tuple[float, FeedItem]]:
    kind = feed["kind"]
    if kind == "atom_producthunt":
        return parse_producthunt_atom(feed, xml_text)
    if kind == "rss_hn_launches":
        return parse_hn_rss(feed, xml_text, prefix="Launch HN:")
    if kind == "rss_hn_show":
        return parse_hn_rss(feed, xml_text, prefix="Show HN:")
    raise ValueError(f"Unsupported feed kind: {kind}")


def dedupe_key(item: FeedItem) -> str:
    if item.product_name:
        return re.sub(r"[^a-z0-9]+", "", item.product_name.lower())
    return re.sub(r"[^a-z0-9]+", "", item.initial_url.lower())


def merge_items(items: Iterable[tuple[float, FeedItem]]) -> list[dict]:
    groups: dict[str, list[tuple[float, FeedItem]]] = defaultdict(list)
    for timestamp, item in items:
        groups[dedupe_key(item)].append((timestamp, item))

    merged_items = []
    tier_rank = {"main": 3, "supplemental": 2, "exploratory": 1}

    for bucket in groups.values():
        bucket.sort(key=lambda row: (row[0], tier_rank.get(row[1].source_tier, 0)), reverse=True)
        _, primary = bucket[0]
        all_sources = sorted({entry.source_name for _, entry in bucket})

        all_urls = []
        seen_urls = set()
        for _, entry in bucket:
            for candidate in [entry.initial_url, *entry.other_urls]:
                if candidate and candidate not in seen_urls:
                    all_urls.append(candidate)
                    seen_urls.add(candidate)

        merged_items.append(
            {
                "product_name": primary.product_name,
                "initial_url": primary.initial_url,
                "published_at": primary.published_at,
                "summary": primary.summary,
                "primary_source": primary.source_name,
                "source_tier": primary.source_tier,
                "all_sources": all_sources,
                "all_urls": all_urls,
                "raw_titles": sorted({entry.raw_title for _, entry in bucket}),
                "signal_strength": "strong" if len(all_sources) >= 2 else "single-source",
                "sort_ts": bucket[0][0],
            }
        )

    merged_items.sort(key=lambda item: item["sort_ts"], reverse=True)
    return merged_items


def build_payload(all_items: list[tuple[float, FeedItem]], merged_items: list[dict]) -> dict:
    feed_counts = defaultdict(int)
    for _, item in all_items:
        feed_counts[item.source_name] += 1

    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "feed_counts": dict(feed_counts),
        "merged_item_count": len(merged_items),
        "items": [{key: value for key, value in item.items() if key != "sort_ts"} for item in merged_items],
    }


def print_preview(payload: dict, limit: int) -> None:
    print("Feed scan summary")
    print("=" * 80)
    for feed in FEEDS:
        print(f"- {feed['name']}: {payload['feed_counts'].get(feed['name'], 0)} items")

    print()
    print("Merged product candidates")
    print("=" * 80)

    for index, item in enumerate(payload["items"][:limit], start=1):
        print(f"{index}. {item['product_name']}")
        print(f"   URL: {item['initial_url']}")
        print(f"   Sources: {', '.join(item['all_sources'])}")
        print(f"   Published: {item['published_at']}")
        print(f"   Signal: {item['signal_strength']} ({item['source_tier']})")
        print(f"   Summary: {item['summary'][:220]}")
        print()


def save_json(payload: dict, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan built-in product feeds and extract product names plus seed URLs."
    )
    parser.add_argument("--limit", type=int, default=20, help="How many merged products to preview.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload to stdout.")
    parser.add_argument(
        "--include-exploratory",
        action="store_true",
        help="Include exploratory feeds like HN Show in the merged output.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to save JSON output. Defaults to {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--output-file",
        type=str,
        default=DEFAULT_JSON_FILE,
        help=f"JSON file name inside the output directory. Defaults to {DEFAULT_JSON_FILE}",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not save JSON to disk.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-feed request timeout in seconds. Default: {DEFAULT_TIMEOUT_SECONDS}",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Retry count for timeout/network failures. Default: {DEFAULT_RETRIES}",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=DEFAULT_RETRY_DELAY_SECONDS,
        help=f"Base wait seconds between retries. Default: {DEFAULT_RETRY_DELAY_SECONDS}",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    all_items: list[tuple[float, FeedItem]] = []
    errors: list[str] = []

    for feed in FEEDS:
        if feed["tier"] == "exploratory" and not args.include_exploratory:
            continue

        try:
            xml_text = fetch_text(
                feed["url"],
                timeout=args.timeout,
                retries=args.retries,
                retry_delay_seconds=args.retry_delay,
                label=feed["name"],
            )
            all_items.extend(parse_feed(feed, xml_text))
        except (OSError, urllib.error.URLError, ET.ParseError, ValueError) as exc:
            errors.append(f"{feed['name']}: {exc}")

    merged_items = merge_items(all_items)
    payload = build_payload(all_items, merged_items)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_preview(payload, args.limit)

    if not args.no_save:
        output_path = save_json(payload, args.output_dir, args.output_file)
        print(f"Saved JSON: {output_path}")

    if errors:
        print("\nWarnings", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)

    return 0 if all_items else 1


if __name__ == "__main__":
    raise SystemExit(main())
