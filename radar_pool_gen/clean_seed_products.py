import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_INPUT = OUTPUT_DIR / "feed_seed_products.json"
DEFAULT_OUTPUT = OUTPUT_DIR / "cleaned_keep.json"
UTC = timezone.utc


def build_cn_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Asia/Shanghai")
        except Exception:
            pass
    return timezone(timedelta(hours=8))


CN_TZ = build_cn_tz()

AI_KEYWORDS = {
    "ai",
    "agent",
    "agents",
    "mcp",
    "llm",
    "copilot",
    "coding",
    "code",
    "rag",
    "automation",
    "automated",
    "model",
    "models",
    "inference",
    "prompt",
    "prompts",
    "workspace",
    "sandbox",
    "qa",
    "memory",
}

NEGATIVE_KEYWORDS = {
    "doorbell",
    "storage cleaner",
    "duplicate file",
    "duplicate photo",
    "wifi",
    "football coaching",
    "maps api",
}

URL_DROP_HOSTS = {
    "news.ycombinator.com",
}


def clean_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_published_at_utc(value: str) -> datetime | None:
    value = clean_text(value)
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M UTC").replace(tzinfo=UTC)
    except ValueError:
        return None


def utc_to_cn(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(CN_TZ)


def normalize_name(name: str) -> str:
    name = clean_text(name)
    name = re.sub(r"\s+", " ", name)
    return name


def normalize_url(url: str) -> str:
    url = clean_text(url)
    if not url:
        return ""

    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower().startswith("utm_"):
            continue
        if key.lower() in {"ref", "source"} and "producthunt" in netloc:
            continue
        query_pairs.append((key, value))

    path = parsed.path.rstrip("/")
    normalized = urlunparse((scheme, netloc, path, "", urlencode(query_pairs), ""))
    return normalized


def host_of(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def pick_canonical_url(item: dict) -> str:
    candidates = [item.get("initial_url", ""), *(item.get("all_urls") or [])]
    normalized = []
    seen = set()
    for candidate in candidates:
        url = normalize_url(candidate)
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append(url)

    if not normalized:
        return ""

    def score(url: str) -> tuple[int, int]:
        host = host_of(url)
        score_value = 0
        if host not in URL_DROP_HOSTS:
            score_value += 5
        if "producthunt.com" in host:
            score_value += 2
        if "github.com" in host:
            score_value += 1
        if url.endswith(".app") or url.endswith(".ai") or url.endswith(".sh"):
            score_value += 1
        return score_value, -len(url)

    normalized.sort(key=score, reverse=True)
    return normalized[0]


def contains_keyword(text: str, keywords: set[str]) -> list[str]:
    haystack = clean_text(text).lower()
    hits = []
    for keyword in sorted(keywords):
        if keyword in haystack:
            hits.append(keyword)
    return hits


def is_recent_cn(item: dict, today_cn: datetime) -> tuple[bool, datetime | None]:
    published_utc = parse_published_at_utc(item.get("published_at", ""))
    published_cn = utc_to_cn(published_utc)
    if published_cn is None:
        return False, None

    target_dates = {
        today_cn.date(),
        (today_cn - timedelta(days=1)).date(),
    }
    return published_cn.date() in target_dates, published_cn


def should_keep(item: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    source = item.get("primary_source", "")
    name = item.get("product_name", "")
    summary = item.get("summary", "")
    combined_text = f"{name} {summary}"

    keyword_hits = contains_keyword(combined_text, AI_KEYWORDS)
    negative_hits = contains_keyword(combined_text, NEGATIVE_KEYWORDS)

    if negative_hits:
        return False, [f"negative_keyword={hit}" for hit in negative_hits]

    if source == "HN Launches":
        reasons.append("source=HN Launches")

    if source == "Product Hunt AI":
        reasons.append("source=Product Hunt AI")

    if item.get("signal_strength") == "strong":
        reasons.append("signal=strong")

    if keyword_hits:
        reasons.extend(f"keyword={hit}" for hit in keyword_hits[:5])

    if source == "Product Hunt Developer Tools" and not keyword_hits:
        return False, ["source=Product Hunt Developer Tools without ai keyword"]

    if not reasons:
        return False, ["no keep signal"]

    return True, reasons


def dedupe_keep_items(items: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str], dict] = {}
    for item in items:
        key = (
            re.sub(r"[^a-z0-9]+", "", item["product_name"].lower()),
            host_of(item["canonical_url"]),
        )
        existing = buckets.get(key)
        if existing is None:
            buckets[key] = item
            continue

        existing_sources = set(existing.get("all_sources", []))
        new_sources = set(item.get("all_sources", []))
        if len(new_sources) > len(existing_sources):
            buckets[key] = item

    return sorted(
        buckets.values(),
        key=lambda x: x.get("published_at_utc", ""),
        reverse=True,
    )


def build_clean_item(item: dict, published_cn: datetime, keep_reasons: list[str]) -> dict:
    canonical_url = pick_canonical_url(item)
    return {
        "product_name": normalize_name(item.get("product_name", "")),
        "canonical_url": canonical_url,
        "published_at_utc": item.get("published_at", ""),
        "published_at_cn": published_cn.strftime("%Y-%m-%d %H:%M CST"),
        "published_date_cn": published_cn.strftime("%Y-%m-%d"),
        "summary": clean_text(item.get("summary", "")),
        "primary_source": item.get("primary_source", ""),
        "all_sources": item.get("all_sources", []),
        "signal_strength": item.get("signal_strength", ""),
        "keep_reason": keep_reasons,
        "all_urls": [normalize_url(url) for url in item.get("all_urls", []) if normalize_url(url)],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Keep only yesterday and today product candidates and save cleaned_keep.json."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help=f"Input JSON. Default: {DEFAULT_INPUT}")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"Output JSON. Default: {DEFAULT_OUTPUT}")
    parser.add_argument(
        "--today",
        type=str,
        default="",
        help="Override CN today date in YYYY-MM-DD format. Defaults to current Asia/Shanghai date.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    data = load_json(args.input)
    items = data.get("items", [])

    if args.today:
        today_cn = datetime.strptime(args.today, "%Y-%m-%d").replace(tzinfo=CN_TZ)
    else:
        today_cn = datetime.now(CN_TZ)

    kept_items = []
    for item in items:
        is_recent, published_cn = is_recent_cn(item, today_cn)
        if not is_recent or published_cn is None:
            continue

        keep, reasons = should_keep(item)
        if not keep:
            continue

        cleaned = build_clean_item(item, published_cn, reasons)
        if cleaned["canonical_url"]:
            kept_items.append(cleaned)

    deduped = dedupe_keep_items(kept_items)

    payload = {
        "generated_at_cn": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S CST"),
        "target_dates_cn": [
            (today_cn - timedelta(days=1)).strftime("%Y-%m-%d"),
            today_cn.strftime("%Y-%m-%d"),
        ],
        "input_file": str(args.input),
        "count": len(deduped),
        "items": deduped,
    }

    save_json(args.output, payload)

    print(f"输入产品数：{len(items)} 个")
    print(f"最终筛选产品数：{len(deduped)} 个")
    print(f"已保存到：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
