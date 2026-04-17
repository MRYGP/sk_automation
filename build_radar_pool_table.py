import argparse
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_OUTPUT = OUTPUT_DIR / "radar_pool_table.md"

DISCARD_DECISIONS = {"丢弃", "涓㈠純"}


def find_latest_kimi_json(output_dir: Path) -> Path:
    candidates = sorted(output_dir.glob("kimi_radar_pools_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError(f"No kimi_radar_pools_*.json found in {output_dir}")
    return candidates[0]


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def load_results(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results") or []
    if not isinstance(results, list):
        raise RuntimeError("Invalid kimi radar json: results is not a list.")
    return results


def keep_row(item: dict) -> bool:
    decision = clean_text(item.get("decision"))
    score = int(item.get("score", 0) or 0)
    return decision not in DISCARD_DECISIONS and score > 0


def sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda x: (
            -int(x.get("score", 0) or 0),
            clean_text(x.get("published_date_cn")),
            clean_text(x.get("product_name")),
        ),
        reverse=False,
    )


def render_table(rows: list[dict]) -> str:
    lines = [
        "| 日期 | 产品 | 来源 | 一句话 | 分数 | 去向 |",
        "|------|------|------|--------|------|------|",
    ]
    for item in rows:
        lines.append(
            "| {date} | {product} | {source} | {one_liner} | {score} | {decision} |".format(
                date=clean_text(item.get("published_date_cn")),
                product=clean_text(item.get("product_name")),
                source=clean_text(item.get("source")),
                one_liner=clean_text(item.get("one_liner")).replace("|", "/"),
                score=int(item.get("score", 0) or 0),
                decision=clean_text(item.get("decision")),
            )
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the final radar-pool markdown table from kimi_radar_pools json."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Optional kimi_radar_pools json path. Defaults to the latest file in output/.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Markdown table output path. Default: {DEFAULT_OUTPUT}",
    )
    args = parser.parse_args()

    input_path = args.input or find_latest_kimi_json(OUTPUT_DIR)
    rows = [item for item in load_results(input_path) if keep_row(item)]
    rows = sort_rows(rows)
    table = render_table(rows)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(table, encoding="utf-8")

    print(f"输入文件：{input_path}")
    print(f"保留条数：{len(rows)}")
    print(f"已保存到：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
