import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib import error, request


BASE_URL = os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1").rstrip("/")
API_KEY = os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY") or ""
MODEL = "kimi-k2.5"
THINKING = {"type": "disabled"}
DEFAULT_RETRIES = 4
DEFAULT_RETRY_DELAY_SECONDS = 10
BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "output" / "relay_gemini_product_scans"
OUTPUT_DIR = BASE_DIR / "output"
ITEM_DIR = OUTPUT_DIR / "kimi_radar_item_scores"
NO_PROXY_OPENER = request.build_opener(request.ProxyHandler({}))

SYSTEM_PROMPT = """你是三湘问道的 AI 产品雷达分析师。

你的任务：从我给你的产品扫描结果里，做一轮非常保守的初筛，输出结构化判断。

## 硬过滤

- 先判断这个候选是不是明确的 AI 相关产品、AI 工具、AI 功能或 AI 公司。
- 如果不是 AI 相关，直接判定为“丢弃”。
- 非 AI 相关时，不进入正常打分逻辑，q1-q5 全部给 0，score=0，decision="丢弃"。
- 这里的“AI 相关”包括：生成式 AI、AI agent、AI coding、MCP、AI 自动化、AI 工作流、AI 搜索/推理/识别、以 AI 为核心卖点的功能或产品。
- 这里的“非 AI 相关”包括：普通 SaaS、普通插件、普通开发工具、普通设计工具，只是碰巧出现在 AI 分类页但产品本身没有明确 AI 核心能力。

## 打分铁律（必须严格遵守）

- 信息不足以判断的维度，默认给 0 分，绝对不给 1 分。
- 宁可漏掉好产品，不可放进平庸产品。🔴待拆解应该是少数，🟡观察才是多数。
- 每次扫描，🔴产品不应超过总数的 30%。如果超过了，说明你打分太松。

## 初筛5问（每题0或1分）

Q1 小团队吗？
- 1分标准：有明确信息显示团队 ≤ 50 人
- 0分：大公司产品、团队规模未知、未提及团队信息
- 注意："未知"就是 0 分，不要猜

Q2 有中国市场机会吗？
- 1分标准：能具体说出"哪群中国用户 + 什么场景 + 为什么现在没人认真做"
- 0分：只能泛泛说"中国市场大"但说不出具体被忽视的用户群
- 注意：不是"中国也能用"就给 1 分，是"中国有一群人特别需要但没人认真做"才给 1 分

Q3 痛点够硬吗？
- 1分标准：能明确看出它解决的是一个真实、具体、高频或高成本的问题；目标用户清楚，使用场景清楚，产品是在省时间、省钱、降风险或明显提效
- 0分：更像一个有趣功能、普通 feature、锦上添花工具、低频需求，或公开信息里看不出明确硬痛点
- 注意：这里判断的不是中国机会，不是大厂会不会做，也不是方向相关性，而是"用户现在是不是已经很痛"

Q4 有反直觉的地方吗？
- 1分标准：大多数人会觉得这条路走不通，但它偏偏走通了；或者它的做法跟同赛道所有人都不同且有效
- 0分：做法是主流做法、常规思路、"听起来有意思"但没有真正违反常识
- 注意：这是最容易打松的一项。"用AI做XX"本身不算反直觉。要问：它做的事，有没有让行内人说"这不可能"？

Q5 时间窗口还开着吗？
- 1分标准：目前没有任何大公司（Google/Microsoft/字节/腾讯/阿里）在做同样的事，中国市场也没有明确的强竞争者
- 0分：能想到一个大公司已经在做类似的事，或者赛道里已经有多个融资过亿的玩家
- 注意：如果你需要想超过3秒才能确定"有没有大公司在做"，大概率是有的，给0分

## 判定

- ≥3分 = 🔴待拆解（真正值得深度研究的）
- 1-2分 = 🟡观察（记录，暂不深究）
- 0分 = 丢弃

## 你的输出要求

- 你这次只需要处理一个产品候选。
- 请你只基于我提供的产品扫描 Markdown 内容来判断，不要假装看过我没提供的内容。
- 允许你基于扫描稿里已经列出的证据做判断，但不要脑补团队规模、融资、中国机会、竞争格局。
- 你必须只输出一个 JSON 对象，不要输出 Markdown，不要输出代码块，不要解释。

JSON 字段必须包含：
- product_name: 字符串
- one_liner: 字符串，不超过20字
- website: 字符串，优先官网，没有就填最可信链接
- team_financing: 字符串，已知信息或"未知"
- source: 字符串
- q1: 0或1
- q2: 0或1
- q3: 0或1
- q4: 0或1
- q5: 0或1
- score: 整数，等于五题之和
- decision: 只能是 "🔴待拆解" / "🟡观察" / "丢弃"
- comment: 字符串，一句话点评
- evidence_gaps: 数组，列出哪些信息不足

注意：
- 如果候选不是 AI 相关，直接输出"丢弃"，q1-q5 全为 0
- 如果信息不足，对应维度直接给0
- score 必须等于 q1+q2+q3+q4+q5
- 如果 score=0，decision 必须是 "丢弃"
- 如果 score>=3，decision 必须是 "🔴待拆解"
- 如果 score 在 1 到 2，decision 必须是 "🟡观察"
"""


def clean(value: str) -> str:
    return " ".join((value or "").split()).strip()


def slugify(value: str, limit: int = 80) -> str:
    text = clean(value).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text)
    text = text.strip("-")
    return text[:limit] or "item"


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
        sys.stdout.write("\r" + " " * 56 + "\r")
        sys.stdout.flush()

    return stop


def post_json(path: str, payload: dict, show_timer: bool = True) -> tuple[dict, float]:
    if not API_KEY:
        raise RuntimeError("Missing KIMI_API_KEY or MOONSHOT_API_KEY.")

    req = request.Request(
        url=f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    stop_timer = with_live_timer("正在等待 Kimi 判池并返回", enabled=show_timer)
    try:
        with NO_PROXY_OPENER.open(req, timeout=240) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Network error: {exc}") from exc
    finally:
        stop_timer()
    return result, time.perf_counter() - started


def extract_text(result: dict) -> str:
    choices = result.get("choices") or []
    if not choices:
        raise RuntimeError("Model response has no choices.")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = [
            item.get("text", "").strip()
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            and item.get("text").strip()
        ]
        if parts:
            return "\n".join(parts)
    raise RuntimeError("Model response content is empty.")


def parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def parse_scan_markdown(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")

    def find(pattern: str) -> str:
        match = re.search(pattern, text, flags=re.M)
        return clean(match.group(1)) if match else ""

    product_name = find(r"^#\s+(.+)$")
    product_key = find(r"^- product_key:\s*`([^`]+)`")
    canonical_url = find(r"^- canonical_url:\s*(.+)$")
    published_date_cn = find(r"^- published_date_cn:\s*`([^`]+)`")
    primary_source = find(r"^- primary_source:\s*`([^`]+)`")
    summary_hint = find(r"^- summary_hint:\s*(.+)$")

    return {
        "path": str(path),
        "filename": path.name,
        "product_name": product_name,
        "product_key": product_key or slugify(product_name),
        "canonical_url": canonical_url,
        "published_date_cn": published_date_cn,
        "primary_source": primary_source,
        "summary_hint": summary_hint,
        "markdown": text,
    }


def load_scan_files(input_dir: Path) -> list[dict]:
    if not input_dir.exists():
        raise RuntimeError(f"Input dir not found: {input_dir}")
    items = []
    for path in sorted(input_dir.glob("*.md")):
        items.append(parse_scan_markdown(path))
    if not items:
        raise RuntimeError(f"No markdown files found in {input_dir}")
    return items


def classify_item(item: dict, show_timer: bool = True) -> tuple[dict, float]:
    user_payload = {
        "product_name": item["product_name"],
        "published_date_cn": item["published_date_cn"],
        "primary_source": item["primary_source"],
        "canonical_url": item["canonical_url"],
        "summary_hint": item["summary_hint"],
        "scan_markdown": item["markdown"],
        "task": "请根据这份产品扫描稿，严格按初筛5问打分，并只返回 JSON。",
    }

    result, elapsed = post_json(
        "/chat/completions",
        {
            "model": MODEL,
            "thinking": THINKING,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
            ],
            "max_completion_tokens": 1800,
        },
        show_timer=show_timer,
    )
    text = extract_text(result)
    payload = parse_json_object(text)
    return payload, elapsed


def is_retryable_error(exc: Exception) -> bool:
    message = str(exc).lower()
    retry_signals = (
        "http 429",
        "engine_overloaded_error",
        "rate limit",
        "network error",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "try again later",
    )
    return any(signal in message for signal in retry_signals)


def classify_item_with_retry(
    item: dict,
    show_timer: bool = True,
    retries: int = DEFAULT_RETRIES,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
) -> tuple[dict, float]:
    attempts = max(retries, 0) + 1
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return classify_item(item, show_timer=show_timer)
        except Exception as exc:
            last_error = exc
            if not is_retryable_error(exc) or attempt >= attempts:
                break

            wait_seconds = max(retry_delay_seconds, 0) * attempt
            print(
                f"  retry {attempt}/{attempts - 1}: {exc} ; waiting {wait_seconds}s before retry"
            )
            time.sleep(wait_seconds)

    assert last_error is not None
    raise RuntimeError(f"{last_error} (retried {attempts - 1} times)") from last_error


def normalize_decision(payload: dict) -> dict:
    for key in ["q1", "q2", "q3", "q4", "q5", "score"]:
        payload[key] = int(payload.get(key, 0) or 0)
    payload["score"] = payload["q1"] + payload["q2"] + payload["q3"] + payload["q4"] + payload["q5"]

    if payload["score"] >= 3:
        payload["decision"] = "🔴待拆解"
    elif payload["score"] >= 1:
        payload["decision"] = "🟡观察"
    else:
        payload["decision"] = "丢弃"

    payload["product_name"] = clean(str(payload.get("product_name", "")))
    payload["one_liner"] = clean(str(payload.get("one_liner", "")))
    payload["website"] = clean(str(payload.get("website", "")))
    payload["team_financing"] = clean(str(payload.get("team_financing", ""))) or "未知"
    payload["source"] = clean(str(payload.get("source", "")))
    payload["comment"] = clean(str(payload.get("comment", "")))
    evidence_gaps = payload.get("evidence_gaps", [])
    if not isinstance(evidence_gaps, list):
        evidence_gaps = [clean(str(evidence_gaps))]
    payload["evidence_gaps"] = [clean(str(x)) for x in evidence_gaps if clean(str(x))]
    return payload


def save_item_result(scan_item: dict, payload: dict, elapsed: float, item_dir: Path) -> Path:
    item_dir.mkdir(parents=True, exist_ok=True)
    path = item_dir / f"{slugify(scan_item['product_name'], 60)}.json"
    wrapper = {
        "source_file": scan_item["filename"],
        "product_key": scan_item["product_key"],
        "timing_seconds": round(elapsed, 2),
        "result": payload,
    }
    path.write_text(json.dumps(wrapper, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def render_product_block(item: dict, decision_symbol: str) -> str:
    return (
        f"### {item['product_name']}\n"
        f"- **一句话**：{item['one_liner']}\n"
        f"- **官网**：{item['website']}\n"
        f"- **团队/融资**：{item['team_financing']}\n"
        f"- **来源**：{item['source']}\n"
        f"- **打分**：Q1[{item['q1']}] Q2[{item['q2']}] Q3[{item['q3']}] Q4[{item['q4']}] Q5[{item['q5']}] = {item['score']}分\n"
        f"- **判定**：{decision_symbol}\n"
        f"- **点评**：{item['comment']}\n"
        f"---\n"
    )


def render_summary_md(results: list[dict]) -> str:
    teardown = [x for x in results if x["decision"] == "🔴待拆解"]
    observe = [x for x in results if x["decision"] == "🟡观察"]

    lines = []
    lines.append("# 雷达初筛结果")
    lines.append("")
    lines.append(f"- 生成时间：`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`")
    lines.append(f"- 总扫描数：`{len(results)}`")
    lines.append(f"- 🔴待拆解：`{len(teardown)}`")
    lines.append(f"- 🟡观察池：`{len(observe)}`")
    lines.append("")

    lines.append("## 🔴 待拆解")
    lines.append("")
    if teardown:
        for item in teardown:
            lines.append(render_product_block(item, "🔴待拆解"))
    else:
        lines.append("本轮无待拆解产品。")
        lines.append("")

    lines.append("## 🟡 观察池")
    lines.append("")
    if observe:
        for item in observe:
            lines.append(render_product_block(item, "🟡观察"))
    else:
        lines.append("本轮无观察池产品。")
        lines.append("")

    lines.append("## 汇总表")
    lines.append("")
    lines.append("| 日期 | 产品 | 来源 | 一句话 | 分数 | 去向 |")
    lines.append("|------|------|------|--------|------|------|")
    for item in results:
        if item["decision"] == "丢弃":
            continue
        lines.append(
            f"| {item['published_date_cn']} | {item['product_name']} | {item['source']} | {item['one_liner']} | {item['score']} | {item['decision']} |"
        )
    lines.append("")
    return "\n".join(lines)


def save_outputs(results: list[dict], raw_results: list[dict], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = output_dir / f"kimi_radar_pools_{stamp}.md"
    json_path = output_dir / f"kimi_radar_pools_{stamp}.json"
    md_path.write_text(render_summary_md(results), encoding="utf-8")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(results),
        "results": results,
        "raw_results": raw_results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Use Kimi to score gemini_product_scans and generate teardown/watch pools.")
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR, help=f"Input markdown directory. Default: {INPUT_DIR}")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help=f"Directory to save summary outputs. Default: {OUTPUT_DIR}")
    parser.add_argument("--item-dir", type=Path, default=ITEM_DIR, help=f"Directory to save per-item scoring json. Default: {ITEM_DIR}")
    parser.add_argument("--start", type=int, help="1-based start index in files.")
    parser.add_argument("--end", type=int, help="1-based end index in files, inclusive.")
    parser.add_argument("--overwrite", action="store_true", help="Re-score items even if item json already exists.")
    parser.add_argument("--no-timer", action="store_true", help="Hide live timer while waiting for each Kimi response.")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help=f"Retry count for overload/network failures. Default: {DEFAULT_RETRIES}")
    parser.add_argument("--retry-delay", type=int, default=DEFAULT_RETRY_DELAY_SECONDS, help=f"Base wait seconds between retries. Default: {DEFAULT_RETRY_DELAY_SECONDS}")
    args = parser.parse_args()

    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    item_dir = args.item_dir.resolve()

    try:
        items = load_scan_files(input_dir)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    if args.start is not None or args.end is not None:
        start_index = 0 if args.start is None else max(args.start - 1, 0)
        end_index = len(items) if args.end is None else max(args.end, 0)
        items = items[start_index:end_index]

    if not items:
        print(json.dumps({"error": "No matching markdown files found"}, ensure_ascii=False, indent=2))
        return 1

    results = []
    raw_results = []
    failed = False
    total = len(items)

    for index, item in enumerate(items, start=1):
        item_json_path = item_dir / f"{slugify(item['product_name'], 60)}.json"
        print(f"[{index}/{total}] scoring {item['product_name']} -> {item['filename']}")

        if item_json_path.exists() and not args.overwrite:
            print("  skip: already scored")
            wrapper = json.loads(item_json_path.read_text(encoding="utf-8"))
            payload = normalize_decision(dict(wrapper["result"]))
            payload["published_date_cn"] = item["published_date_cn"]
            results.append(payload)
            raw_results.append(wrapper)
            continue

        try:
            payload, elapsed = classify_item_with_retry(
                item,
                show_timer=not args.no_timer,
                retries=args.retries,
                retry_delay_seconds=args.retry_delay,
            )
            payload = normalize_decision(payload)
            payload["published_date_cn"] = item["published_date_cn"]
            if not payload.get("website"):
                payload["website"] = item["canonical_url"]
            if not payload.get("source"):
                payload["source"] = item["primary_source"]
            saved = save_item_result(item, payload, elapsed, item_dir)
            print(f"  done: {round(elapsed, 2)}s -> {saved.name}")
            results.append(payload)
            raw_results.append(json.loads(saved.read_text(encoding="utf-8")))
        except Exception as exc:
            failed = True
            print(f"  error: {exc}")

    results.sort(key=lambda x: (x["decision"] != "🔴待拆解", x["decision"] != "🟡观察", -x["score"], x["product_name"]))
    md_path, json_path = save_outputs(results, raw_results, output_dir)

    print(
        json.dumps(
            {
                "count": len(results),
                "failed": failed,
                "markdown_file": str(md_path),
                "json_file": str(json_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
