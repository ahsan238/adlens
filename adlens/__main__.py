"""
__main__.py — CLI entry point for the AdLens Ad Intelligence System.

Usage:
    python -m adlens --url https://example.com --mode heuristic
    python -m adlens --url https://example.com --mode llm --api-key sk-...
    python -m adlens --urls urls.txt --mode heuristic
"""

import argparse
import sys
import os

from .engine import AdlensEngine
from .utils import read_urls_file


def main():
    parser = argparse.ArgumentParser(
    prog="adlens",
    description="AdLens Ad Intelligence System — Detect ads, check compliance, generate filter rules.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m adlens --url https://gsmarena.com --mode heuristic
    python -m adlens --url https://cnn.com --mode llm --api-key sk-...
    python -m adlens --urls websites.txt --mode heuristic --viewport mobile
        """,
    )

    # URL input
    url_group = parser.add_mutually_exclusive_group(required=True)
    url_group.add_argument("--url", help="Single URL to scan")
    url_group.add_argument("--urls", help="Text file with URLs (one per line)")

    # Mode
    parser.add_argument(
        "--mode",
        choices=["heuristic", "llm"],
        default="heuristic",
        help="Detection mode: 'heuristic' (default, no API) or 'llm' (OpenAI vision)",
    )

    # LLM config
    parser.add_argument(
        "--api-key",
        default="",
        help="OpenAI API key for LLM mode (or set OPENAI_API_KEY env var)",
    )
    parser.add_argument(
        "--model",
        default="",
        help="OpenAI model to use for LLM mode (e.g. 'gpt-4o', 'gpt-5-mini'). Overrides config default.",
    )

    # Output
    parser.add_argument(
        "--output-dir",
        default="",
        help="Output directory (default: adlens_results/)",
    )

    # Browser
    parser.add_argument(
        "--viewport",
        choices=["desktop", "mobile", "tablet"],
        default="desktop",
        help="Viewport preset (default: desktop)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browser in headless mode (default: True)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser with visible UI",
    )
    parser.add_argument(
        "--no-video",
        action="store_true",
        help="Disable video recording (saves disk space and speeds up crawls)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="Page load timeout in seconds (default: 45)",
    )
    parser.add_argument(
        "--scan-timeout",
        type=int,
        default=180,
        help="Total scan timeout in seconds (default: 180)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip URLs that already have results in the output directory",
    )

    # Parallel execution
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1,
        help="Number of parallel worker processes (default: 1 = serial mode)",
    )
    parser.add_argument(
        "--worker-timeout",
        type=int,
        default=300,
        help="Hard kill timeout per site in seconds when running in parallel (default: 300 = 5 min)",
    )
    parser.add_argument(
        "--exception-list",
        default="",
        help="Path to Acceptable Ads exception list (exceptionlist.txt) for auditing. "
             "Auto-detected from ad_filter_crawler/filters/ if available.",
    )

    args = parser.parse_args()

    # Resolve headless
    headless = not args.no_headless

    # Resolve API key: CLI arg > env var > hardcoded config
    from .config import OPENAI_API_KEY as CONFIG_API_KEY
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY", "") or CONFIG_API_KEY

    # Override model if specified via CLI
    if args.model:
        from . import config as cfg
        if args.model not in cfg.MODEL_PRICING:
            print(f"Warning: Unknown model '{args.model}'. Using default pricing for gpt-5-mini.")
        cfg.OPENAI_MODEL = args.model
        pricing = cfg.MODEL_PRICING.get(args.model, cfg.MODEL_PRICING["gpt-5-mini"])
        cfg.INPUT_COST_PER_1M = pricing["input"]
        cfg.OUTPUT_COST_PER_1M = pricing["output"]
        print(f"  [Config] Model: {cfg.OPENAI_MODEL}")
        print(f"  [Config] Pricing: ${cfg.INPUT_COST_PER_1M}/1M input, ${cfg.OUTPUT_COST_PER_1M}/1M output")

    if args.mode == "llm" and not api_key:
        print("Error: LLM mode requires an OpenAI API key.")
        print("  Set via --api-key or OPENAI_API_KEY environment variable.")
        sys.exit(1)

    # Resolve exception list path
    exception_list_path = args.exception_list
    if not exception_list_path:
        # Auto-detect from common locations
        for candidate in [
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "ad_filter_crawler", "filters", "exceptionlist.txt"),
            os.path.join(os.getcwd(), "ad_filter_crawler", "filters", "exceptionlist.txt"),
        ]:
            if os.path.isfile(candidate):
                exception_list_path = candidate
                break

    # Create engine
    engine = AdlensEngine(
        mode=args.mode,
        api_key=api_key,
        output_dir=args.output_dir,
        viewport=args.viewport,
        headless=headless,
        timeout_ms=args.timeout * 1000,
        scan_timeout=args.scan_timeout,
        skip_existing=args.skip_existing,
        exception_list_path=exception_list_path,
        record_video=not args.no_video,
    )

    # Run scan(s)
    if args.url:
        url = args.url
        if not url.startswith("http"):
            url = f"https://{url}"
        engine.scan(url)
    elif args.urls:
        urls = read_urls_file(args.urls)
        if not urls:
            print(f"Error: No URLs found in {args.urls}")
            sys.exit(1)
        print(f"Loaded {len(urls)} URLs from {args.urls}")

        if args.workers > 1:
            # Parallel mode
            from .parallel import run_parallel
            engine_kwargs = {
                "mode": args.mode,
                "api_key": api_key,
                "output_dir": args.output_dir or "",
                "viewport": args.viewport,
                "headless": headless,
                "timeout_ms": args.timeout * 1000,
                "scan_timeout": args.scan_timeout,
                "skip_existing": args.skip_existing,
                "exception_list_path": exception_list_path,
                "record_video": not args.no_video,
            }
            run_parallel(
                urls=urls,
                engine_kwargs=engine_kwargs,
                workers=args.workers,
                worker_timeout=args.worker_timeout,
            )
        else:
            # Serial mode (default)
            engine.scan_batch(urls)


if __name__ == "__main__":
    main()
