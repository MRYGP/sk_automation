import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
SCAN_FEEDS = BASE_DIR / "scan_product_feeds.py"
CLEAN_SEEDS = BASE_DIR / "clean_seed_products.py"
SCAN_ENTRIES = BASE_DIR / "relay_gemini_scan_yesterday_entries.py"
BUILD_POOLS = BASE_DIR / "kimi_build_radar_pools.py"
BUILD_TABLE = BASE_DIR / "build_radar_pool_table.py"
OUTPUT_BASE_DIR = BASE_DIR / "output"
DEFAULT_FEED_TIMEOUT_SECONDS = 30
DEFAULT_FEED_RETRIES = 2
DEFAULT_FEED_RETRY_DELAY_SECONDS = 5


def run_step(
    label: str,
    script: Path,
    extra_args: list[str] | None = None,
    allowed_returncodes: set[int] | None = None,
) -> int:
    command = [sys.executable, str(script)]
    if extra_args:
        command.extend(extra_args)

    print(f"\n=== {label} ===")
    print(" ".join(command))
    completed = subprocess.run(command, check=False, cwd=BASE_DIR)
    allowed = {0} if allowed_returncodes is None else set(allowed_returncodes)
    if completed.returncode not in allowed:
        raise subprocess.CalledProcessError(completed.returncode, command)
    if completed.returncode != 0:
        print(f"{label} completed with warnings (exit code {completed.returncode}).")
    return completed.returncode


def find_latest_kimi_json(output_dir: Path) -> Path:
    candidates = sorted(output_dir.glob("kimi_radar_pools_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError(f"No kimi_radar_pools_*.json found in {output_dir}")
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the full radar-pool generation pipeline in automation/radar_pool_gen."
    )
    parser.add_argument(
        "--overwrite-scan",
        action="store_true",
        help="Pass --overwrite to relay_gemini_scan_yesterday_entries.py",
    )
    parser.add_argument(
        "--overwrite-score",
        action="store_true",
        help="Pass --overwrite to kimi_build_radar_pools.py",
    )
    parser.add_argument(
        "--skip-feed-scan",
        action="store_true",
        help="Skip scan_product_feeds.py",
    )
    parser.add_argument(
        "--skip-clean",
        action="store_true",
        help="Skip clean_seed_products.py",
    )
    parser.add_argument(
        "--skip-relay-scan",
        action="store_true",
        help="Skip relay_gemini_scan_yesterday_entries.py",
    )
    parser.add_argument(
        "--skip-kimi-score",
        action="store_true",
        help="Skip kimi_build_radar_pools.py",
    )
    parser.add_argument(
        "--skip-final-table",
        action="store_true",
        help="Skip build_radar_pool_table.py",
    )
    parser.add_argument(
        "--feed-timeout",
        type=int,
        default=DEFAULT_FEED_TIMEOUT_SECONDS,
        help=f"Per-feed request timeout in seconds for scan_product_feeds.py. Default: {DEFAULT_FEED_TIMEOUT_SECONDS}",
    )
    parser.add_argument(
        "--feed-retries",
        type=int,
        default=DEFAULT_FEED_RETRIES,
        help=f"Retry count for timeout/network failures in scan_product_feeds.py. Default: {DEFAULT_FEED_RETRIES}",
    )
    parser.add_argument(
        "--feed-retry-delay",
        type=int,
        default=DEFAULT_FEED_RETRY_DELAY_SECONDS,
        help=f"Base wait seconds between scan_product_feeds.py retries. Default: {DEFAULT_FEED_RETRY_DELAY_SECONDS}",
    )
    args = parser.parse_args()

    run_output_dir = OUTPUT_BASE_DIR / f"output_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    relay_scan_dir = run_output_dir / "relay_gemini_product_scans"
    kimi_item_dir = run_output_dir / "kimi_radar_item_scores"
    run_output_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_feed_scan:
        run_step(
            "Step 1/5 scan feeds",
            SCAN_FEEDS,
            [
                "--output-dir",
                str(run_output_dir),
                "--output-file",
                "feed_seed_products.json",
                "--timeout",
                str(args.feed_timeout),
                "--retries",
                str(args.feed_retries),
                "--retry-delay",
                str(args.feed_retry_delay),
            ],
        )

    if not args.skip_clean:
        run_step(
            "Step 2/5 clean seed products",
            CLEAN_SEEDS,
            ["--input", str(run_output_dir / "feed_seed_products.json"), "--output", str(run_output_dir / "cleaned_keep.json")],
        )

    if not args.skip_relay_scan:
        relay_args: list[str] = []
        if args.overwrite_scan:
            relay_args.append("--overwrite")
        relay_args.extend(
            [
                "--entries-path",
                str(run_output_dir / "cleaned_keep.json"),
                "--output-dir",
                str(relay_scan_dir),
                "--index-dir",
                str(run_output_dir),
            ]
        )
        run_step("Step 3/5 relay gemini scan", SCAN_ENTRIES, relay_args, allowed_returncodes={0, 2})

    if not args.skip_kimi_score:
        kimi_args: list[str] = []
        if args.overwrite_score:
            kimi_args.append("--overwrite")
        kimi_args.extend(
            [
                "--input-dir",
                str(relay_scan_dir),
                "--output-dir",
                str(run_output_dir),
                "--item-dir",
                str(kimi_item_dir),
            ]
        )
        run_step("Step 4/5 kimi build radar pools", BUILD_POOLS, kimi_args, allowed_returncodes={0, 2})

    if not args.skip_final_table:
        final_input = find_latest_kimi_json(run_output_dir)
        run_step(
            "Step 5/5 build final radar table",
            BUILD_TABLE,
            ["--input", str(final_input), "--output", str(run_output_dir / "radar_pool_table.md")],
        )

    print("\nPipeline completed.")
    print(f"Output root: {run_output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
