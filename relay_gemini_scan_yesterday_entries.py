import argparse
import json
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib import error, request


DEFAULT_BASE_URL = os.getenv("RELAY_BASE_URL", "").rstrip("/")
BASE_URL = DEFAULT_BASE_URL
DEFAULT_MODEL = "gemini-3.1-pro-preview-thinking"
DEFAULT_API_KEY = os.getenv("RELAY_API_KEY", "")
API_KEY = DEFAULT_API_KEY
DEFAULT_TIMEOUT_SECONDS = 240
DEFAULT_RETRIES = 2
DEFAULT_RETRY_DELAY_SECONDS = 8

BASE_DIR = Path(__file__).resolve().parent
ENTRIES_PATH = BASE_DIR / "output" / "cleaned_keep.json"
OUTPUT_DIR = BASE_DIR / "output" / "relay_gemini_product_scans"
INDEX_DIR = BASE_DIR / "output"
NO_PROXY_OPENER = request.build_opener(request.ProxyHandler({}))

PROMPT = """你是产品雷达扫描员。
输入通常包含：产品名、target_url、all_urls、发布日期、摘要、来源站点、保留理由。

任务：
基于 target_url、all_urls 和产品名搜索结果，对单个产品做一次可信扫描，尽量还原更详细的产品描述。

执行规则：
1. 优先检查 all_urls，优先使用官网、docs、GitHub、blog、发布页。
2. 如果 target_url 或 all_urls 主要是 Product Hunt、Hacker News、聚合页、讨论页、跳转页，不要停留在该页，继续搜索同品牌官网、docs、GitHub、blog、发布页。
3. 重点围绕 AI agent、coding、MCP、automation、developer AI tools 理解产品；如果最后发现并非这些方向，也要如实写。
4. 只有在已检查 target_url、all_urls，并且继续用产品名搜索后，仍无可靠信息时，才能判定“不可扫描”。
5. 只允许输出实际检索到的内容；我提供的摘要、来源、保留理由只能作线索，不能直接当结论。

输出格式：
扫描结论：
先写“可扫描 / 不可扫描”。
紧接着再用 2-3 句中文对产品做一个简洁介绍，说明它是什么、主要做什么、适合谁或适合什么场景。这里可以适度概括，但必须基于实际检索到的信息，不要空泛。

信息保留：
这是给后续评分 API 使用的重点部分。目标不是写成总结稿，而是尽量保留页面里的原始信息，使用高保真的中文转述，减少信息损失。
优先保留页面里能确认的具体事实，例如功能点、模块名、产品组成、集成对象、输入输出方式、使用场景、目标用户、页面中的明确短语、仓库说明、文档描述、按钮文字、发布说明、套餐或权限信息等。
尽量按“来源页面”分组来写，例如“来源：官网”“来源：GitHub”“来源：docs”“来源：Product Hunt”。每组下面写若干条信息点，尽量一条一句，不要展开成大段议论文。
不要使用“产品核心功能与机制”“具体事实与细节”“性能提升”“工作流优势”这类总结性小标题，也不要主动把零散信息整理成一篇完整说明文。
如果页面本身已经写得比较具体，优先按原文含义做高保真中文转述，尽量保留原句中的信息点、专有名词和顺序，不要为了精炼而压缩成泛化结论。
如果不同页面提供了互补信息，可以并列保留；如果信息存在不一致，也要明确指出，不要自行抹平差异。
总长度尽量控制在 1000 字以内；如果原始信息明显超过 1000 字，优先保留信息密度最高、最适合后续评分的部分，再做轻度压缩，但不要改写成纯摘要。
只写实际检索到的内容，不写泛泛分析，不要为了流畅补充未见信息；宁可更原始、更零散，也不要过度总结。

关键证据：
列出 3-6 条尽量可核对的短证据，优先写页面标题、页面可见日期、页面短语、链接文字或仓库/文档中的明确信息，不要编造，不要写泛泛总结。

相关链接与来源：
把能找到的高价值链接尽量都留下来，并简要说明各链接对应的信息来源或用途。优先保留官网、docs、GitHub、blog、发布页，其次再保留 Product Hunt、Hacker News、聚合页、讨论页。
"""


def clean(value: str) -> str:
    return " ".join((value or "").split()).strip()


def slugify(value: str, limit: int = 80) -> str:
    text = clean(value).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text)
    text = text.strip("-")
    return text[:limit] or "product"


def with_live_timer(label: str, enabled: bool = True):
    if not enabled:
        def noop() -> None:
            return None

        return noop

    stop_event = threading.Event()
    started = time.perf_counter()

    def render() -> None:
        while not stop_event.wait(0.2):
            sys.stdout.write(f"\r{label}... {time.perf_counter() - started:5.1f}s")
            sys.stdout.flush()

    thread = threading.Thread(target=render, daemon=True)
    thread.start()

    def stop() -> None:
        stop_event.set()
        thread.join(timeout=0.5)
        sys.stdout.write("\r" + " " * 72 + "\r")
        sys.stdout.flush()

    return stop


def product_key(item: dict[str, object]) -> str:
    product_name = clean(str(item.get("product_name", "")))
    published_date = clean(str(item.get("published_date_cn", "")))
    canonical_url = clean(str(item.get("canonical_url", "")))
    base = "|".join([product_name, published_date, canonical_url]).lower()
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-") or slugify(product_name or canonical_url)


def load_entries(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise RuntimeError(f"Entries file not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("items") or []
    if not isinstance(entries, list):
        raise RuntimeError("Invalid cleaned_keep payload: items is not a list.")

    normalized = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        canonical_url = clean(str(item.get("canonical_url") or ""))
        product_name = clean(str(item.get("product_name") or ""))
        if not canonical_url or not product_name:
            continue
        entry = dict(item)
        entry["product_key"] = product_key(entry)
        normalized.append(entry)
    return normalized


def http_json(
    method: str,
    url: str,
    payload: dict | None = None,
    show_timer: bool = True,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[dict, float]:
    if not BASE_URL:
        raise RuntimeError("Missing relay base URL. Set RELAY_BASE_URL or pass --base-url.")
    if not API_KEY:
        raise RuntimeError("Missing relay API key. Set RELAY_API_KEY or pass --api-key.")

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        url=url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )

    started = time.perf_counter()
    stop_timer = with_live_timer("等待 relay Gemini 返回", enabled=show_timer)
    try:
        with NO_PROXY_OPENER.open(req, timeout=timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8")), time.perf_counter() - started
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 503 and "model_not_found" in detail:
            raise RuntimeError(
                "HTTP 503: relay provider has no available channel for the requested model. "
                f"Try a relay-supported model such as {DEFAULT_MODEL}. Raw detail: {detail}"
            ) from exc
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"请求超时：{timeout_seconds}s 内未收到完整响应。") from exc
    except error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise RuntimeError(f"请求超时：{timeout_seconds}s 内未收到完整响应。") from exc
        raise RuntimeError(f"网络错误：{reason}") from exc
    finally:
        stop_timer()


def extract_text(result: dict) -> str:
    choices = result.get("choices") or []
    if not choices:
        raise RuntimeError("Relay response has no choices.")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        texts = [item.get("text", "").strip() for item in content if isinstance(item, dict) and item.get("text")]
        if texts:
            return "\n".join(texts).strip()
    raise RuntimeError("Relay response text is empty.")


def build_prompt(entry: dict[str, object]) -> str:
    hint_payload = {
        "product_key": entry.get("product_key", ""),
        "product_name": entry.get("product_name", ""),
        "target_url": entry.get("canonical_url", ""),
        "published_date_cn": entry.get("published_date_cn", ""),
        "published_at_cn": entry.get("published_at_cn", ""),
        "published_at_utc": entry.get("published_at_utc", ""),
        "summary_hint": entry.get("summary", ""),
        "primary_source": entry.get("primary_source", ""),
        "all_sources": entry.get("all_sources", []),
        "signal_strength": entry.get("signal_strength", ""),
        "keep_reason": entry.get("keep_reason", []),
        "all_urls": entry.get("all_urls", []),
        "task": "请优先检查 target_url 和 all_urls，并结合产品名继续搜索真实落地页，输出可信的中文扫描结果。",
    }
    return PROMPT + "\n\n" + json.dumps(hint_payload, ensure_ascii=False, indent=2)


def scan_entry(
    entry: dict[str, object],
    model: str,
    show_timer: bool = True,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
) -> tuple[str, float]:
    prompt_text = build_prompt(entry)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_text}],
    }

    attempts = max(retries, 0) + 1
    last_error: Exception | None = None
    last_elapsed = 0.0

    for attempt in range(1, attempts + 1):
        try:
            result, elapsed = http_json(
                "POST",
                f"{BASE_URL}/chat/completions",
                payload=payload,
                show_timer=show_timer,
                timeout_seconds=timeout_seconds,
            )
            return extract_text(result), elapsed
        except Exception as exc:
            last_error = exc
            if "超时" not in str(exc) and "网络错误" not in str(exc):
                raise
            if attempt >= attempts:
                break
            last_elapsed = 0.0
            print(f"  重试 {attempt}/{attempts - 1}：{exc}；{retry_delay_seconds}s 后再试")
            time.sleep(max(retry_delay_seconds, 0))

    assert last_error is not None
    raise RuntimeError(f"{last_error}（已重试 {attempts - 1} 次）") from last_error


def build_output_path(entry: dict[str, object], index: int, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{index:03d}_{slugify(str(entry.get('product_name', '')), 60)}.md"
    return output_dir / filename


def save_output(
    entry: dict[str, object],
    model_summary: str,
    elapsed: float,
    index: int,
    model: str,
    output_dir: Path,
) -> Path:
    path = build_output_path(entry, index, output_dir)
    content = (
        f"# {entry.get('product_name', '')}\n\n"
        f"- product_key: `{entry.get('product_key', '')}`\n"
        f"- canonical_url: {entry.get('canonical_url', '')}\n"
        f"- published_date_cn: `{entry.get('published_date_cn', '')}`\n"
        f"- published_at_cn: `{entry.get('published_at_cn', '')}`\n"
        f"- published_at_utc: `{entry.get('published_at_utc', '')}`\n"
        f"- primary_source: `{entry.get('primary_source', '')}`\n"
        f"- all_sources: {', '.join(entry.get('all_sources', []))}\n"
        f"- signal_strength: `{entry.get('signal_strength', '')}`\n"
        f"- keep_reason: {', '.join(entry.get('keep_reason', []))}\n"
        f"- summary_hint: {entry.get('summary', '')}\n"
        f"- model: `{model}`\n"
        f"- timing_seconds: `{round(elapsed, 2)}`\n\n"
        f"---\n\n"
        f"{model_summary.strip()}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def pick_entries(
    all_entries: list[dict[str, object]],
    start: int | None,
    end: int | None,
    product_key_value: str | None,
) -> list[dict[str, object]]:
    selected = all_entries
    if product_key_value:
        selected = [entry for entry in selected if entry.get("product_key") == product_key_value]
    if start is not None or end is not None:
        start_index = 0 if start is None else max(start - 1, 0)
        end_index = len(selected) if end is None else max(end, 0)
        selected = selected[start_index:end_index]
    return selected


def load_completed_keys(output_dir: Path) -> set[str]:
    completed: set[str] = set()
    if not output_dir.exists():
        return completed
    for path in output_dir.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        match = re.search(r"^- product_key: `([^`]+)`", text, flags=re.MULTILINE)
        if match:
            completed.add(match.group(1))
    return completed


def save_index(
    results: list[dict[str, object]],
    failed: bool,
    entries_path: Path,
    model: str,
    index_dir: Path,
) -> Path:
    index_dir.mkdir(parents=True, exist_ok=True)
    path = index_dir / f"relay_gemini_scan_index_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "entries_path": str(entries_path),
        "model": model,
        "count": len(results),
        "failed": failed,
        "results": results,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    global API_KEY, BASE_URL

    parser = argparse.ArgumentParser(description="Scan cleaned product candidates with relay Gemini and save one markdown per product.")
    parser.add_argument("--api-key", help="Override the relay API key. Defaults to RELAY_API_KEY.")
    parser.add_argument("--base-url", help="Override the relay base URL. Defaults to RELAY_BASE_URL.")
    parser.add_argument("--entries-path", help="Override the default cleaned_keep json path.")
    parser.add_argument("--start", type=int, help="1-based start index in entries.")
    parser.add_argument("--end", type=int, help="1-based end index in entries, inclusive.")
    parser.add_argument("--product-key", help="Only run one product by product_key.")
    parser.add_argument("--overwrite", action="store_true", help="Rescan entries even if markdown output already exists.")
    parser.add_argument("--no-timer", action="store_true", help="Hide the live timer while waiting for each relay Gemini response.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Relay Gemini model name.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help=f"Per-request timeout in seconds. Default: {DEFAULT_TIMEOUT_SECONDS}")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help=f"Retry count for timeout/network failures. Default: {DEFAULT_RETRIES}")
    parser.add_argument("--retry-delay", type=int, default=DEFAULT_RETRY_DELAY_SECONDS, help=f"Wait seconds between retries. Default: {DEFAULT_RETRY_DELAY_SECONDS}")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help=f"Directory to save per-product markdown outputs. Default: {OUTPUT_DIR}")
    parser.add_argument("--index-dir", type=Path, default=INDEX_DIR, help=f"Directory to save relay scan index json. Default: {INDEX_DIR}")
    args = parser.parse_args()

    API_KEY = clean(args.api_key) if args.api_key else DEFAULT_API_KEY
    BASE_URL = clean(args.base_url).rstrip("/") if args.base_url else DEFAULT_BASE_URL

    entries_path = Path(args.entries_path).resolve() if args.entries_path else ENTRIES_PATH
    output_dir = args.output_dir.resolve()
    index_dir = args.index_dir.resolve()
    try:
        all_entries = load_entries(entries_path)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    entries = pick_entries(all_entries, start=args.start, end=args.end, product_key_value=args.product_key)
    if not entries:
        print(json.dumps({"error": "No matching entries found in json"}, ensure_ascii=False, indent=2))
        return 1

    completed_keys = set() if args.overwrite else load_completed_keys(output_dir)
    results = []
    failed = False
    total = len(entries)

    for index, entry in enumerate(entries, start=1):
        current_key = str(entry.get("product_key", ""))
        current_url = str(entry.get("canonical_url", ""))
        current_name = str(entry.get("product_name", ""))
        print(f"[{index}/{total}] scanning {current_name} -> {current_url}")

        if current_key and current_key in completed_keys:
            print("  skip: already scanned")
            results.append(
                {
                    "product_key": current_key,
                    "product_name": current_name,
                    "canonical_url": current_url,
                    "status": "skipped",
                    "reason": "already_scanned",
                }
            )
            continue

        try:
            model_summary, model_elapsed = scan_entry(
                entry=entry,
                model=args.model,
                show_timer=not args.no_timer,
                timeout_seconds=args.timeout,
                retries=args.retries,
                retry_delay_seconds=args.retry_delay,
            )
            output_path = save_output(entry, model_summary, model_elapsed, index, args.model, output_dir)
            print(f"  done: {round(model_elapsed, 2)}s -> {output_path.name}")
            results.append(
                {
                    "product_key": current_key,
                    "product_name": current_name,
                    "canonical_url": current_url,
                    "status": "ok",
                    "output_file": str(output_path),
                    "timing_seconds": {
                        "extract_with_relay_gemini": round(model_elapsed, 2),
                        "total": round(model_elapsed, 2),
                    },
                }
            )
        except Exception as exc:
            failed = True
            print(f"  error: {exc}")
            results.append(
                {
                    "product_key": current_key,
                    "product_name": current_name,
                    "canonical_url": current_url,
                    "status": "error",
                    "error": str(exc),
                }
            )

    index_path = save_index(results, failed, entries_path, args.model, index_dir)
    print(
        json.dumps(
            {
                "count": len(results),
                "failed": failed,
                "index_file": str(index_path),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
