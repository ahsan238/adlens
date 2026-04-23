"""
parallel.py — Multiprocessing-based parallel crawl orchestrator for AdLens.

Uses Python multiprocessing (not threads) so each worker gets:
  - Its own Playwright browser instance (no shared state)
  - Its own signal.SIGALRM (used by existing timeout logic)
  - Its own stdout/stderr redirected to a per-domain log file

Two-level timeout protection:
  Level 1: Existing scan_timeout SIGALRM inside each worker (default 180s)
  Level 2: Orchestrator hard-kill via result.get(timeout=worker_timeout)
"""

import os
import sys
import json
import time
import multiprocessing
from datetime import datetime
from typing import List, Dict, Any, Optional


class ProgressTracker:
    """Thread-safe progress tracker that journals to a JSONL file."""

    def __init__(self, output_dir: str, total: int):
        self.output_dir = output_dir
        self.total = total
        self.completed = 0
        self.succeeded = 0
        self.failed = 0
        self.timed_out = 0
        self.skipped = 0
        self.start_time = time.time()

        os.makedirs(output_dir, exist_ok=True)
        self.journal_path = os.path.join(output_dir, "progress.jsonl")
        # Write header line
        self._append({"event": "START", "total_urls": total,
                       "timestamp": datetime.now().isoformat()})

    def _append(self, record: dict):
        with open(self.journal_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def record(self, result: dict):
        """Record the outcome of a single URL scan."""
        self.completed += 1
        status = result.get("status", "UNKNOWN")
        if status == "SUCCESS":
            self.succeeded += 1
        elif status == "TIMEOUT_KILLED":
            self.timed_out += 1
        elif status == "SKIPPED":
            self.skipped += 1
        else:
            self.failed += 1

        result["timestamp"] = datetime.now().isoformat()
        self._append(result)

        # Print live progress every completion
        elapsed = time.time() - self.start_time
        rate = self.completed / elapsed if elapsed > 0 else 0
        remaining = self.total - self.completed
        eta_s = remaining / rate if rate > 0 else 0
        eta_min = eta_s / 60

        print(
            f"\r[Progress] {self.completed}/{self.total} "
            f"(✓{self.succeeded} ✗{self.failed} ⏰{self.timed_out} ⏭{self.skipped}) "
            f"| {rate:.1f} sites/min | ETA: {eta_min:.0f}m",
            flush=True,
        )

    def summary(self) -> dict:
        elapsed = time.time() - self.start_time
        return {
            "total_urls": self.total,
            "completed": self.completed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "timed_out": self.timed_out,
            "skipped": self.skipped,
            "total_duration_s": round(elapsed, 1),
            "avg_duration_s": round(elapsed / max(self.completed, 1), 1),
            "timestamp": datetime.now().isoformat(),
        }

    def save_summary(self):
        summary = self.summary()
        summary_path = os.path.join(self.output_dir, "parallel_summary.json")
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2)
        self._append({"event": "END", **summary})
        return summary


def _worker_scan(args: dict) -> dict:
    """
    Top-level worker function that runs in a child process.
    Must be a module-level function (picklable).

    Redirects all stdout/stderr to a per-domain log file, then runs
    a single AdlensEngine.scan() call.
    """
    url = args["url"]
    index = args["index"]
    total = args["total"]
    engine_kwargs = args["engine_kwargs"]
    log_dir = args["log_dir"]

    # Extract domain for log filename
    from .utils import extract_domain
    domain = extract_domain(url)

    # Set up per-domain log file
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{domain}.log")

    start_time = time.time()
    result = {
        "url": url,
        "domain": domain,
        "index": index,
        "status": "UNKNOWN",
        "error": None,
        "duration_s": 0,
        "log_file": log_path,
    }

    # Redirect stdout and stderr to the log file
    original_stdout = sys.stdout
    original_stderr = sys.stderr

    try:
        log_file = open(log_path, "w", buffering=1)  # line-buffered
        sys.stdout = log_file
        sys.stderr = log_file

        print(f"[Worker] PID={os.getpid()} | Site {index}/{total} | {url}")
        print(f"[Worker] Started at {datetime.now().isoformat()}")
        print(f"[Worker] Log: {log_path}")
        print()

        # Create engine instance in this process
        from .engine import AdlensEngine
        engine = AdlensEngine(**engine_kwargs)

        # Check skip_existing before scanning
        if engine.skip_existing:
            domain_dir = os.path.join(engine.output_dir, domain)
            if os.path.exists(domain_dir) and any(
                d.startswith("session_") for d in os.listdir(domain_dir)
            ):
                print(f"  [Skipping] Results already exist for {domain}")
                result["status"] = "SKIPPED"
                result["duration_s"] = round(time.time() - start_time, 1)
                return result

        report = engine.scan(url)

        result["status"] = "SUCCESS"
        if report:
            result["ads_found"] = report.confirmed_ads
            result["rules_generated"] = report.total_rules_generated

        print(f"\n[Worker] Completed successfully in {time.time() - start_time:.1f}s")

    except TimeoutError as e:
        result["status"] = "TIMEOUT"
        result["error"] = str(e)
        print(f"\n[Worker] Timed out: {e}")

    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)
        import traceback
        print(f"\n[Worker] Failed: {e}")
        traceback.print_exc()

    finally:
        result["duration_s"] = round(time.time() - start_time, 1)
        print(f"[Worker] Duration: {result['duration_s']}s | Status: {result['status']}")

        # Restore stdout/stderr
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        try:
            log_file.close()
        except Exception:
            pass

    return result


def run_parallel(
    urls: List[str],
    engine_kwargs: dict,
    workers: int = 4,
    worker_timeout: int = 300,
) -> dict:
    """
    Run AdLens scans in parallel using multiprocessing.

    Args:
        urls: List of URLs to scan.
        engine_kwargs: Keyword arguments for AdlensEngine constructor.
        workers: Number of parallel worker processes.
        worker_timeout: Hard kill timeout per site in seconds (default 300 = 5 min).

    Returns:
        Summary dict with overall statistics.
    """
    from .config import BASE_OUTPUT_DIR
    output_dir = engine_kwargs.get("output_dir") or BASE_OUTPUT_DIR
    # Ensure engine_kwargs also has the resolved path
    engine_kwargs["output_dir"] = output_dir
    log_dir = os.path.join(output_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    total = len(urls)
    tracker = ProgressTracker(output_dir, total)

    print(f"\n{'='*60}")
    print(f"  ADLENS PARALLEL CRAWL")
    print(f"  Workers: {workers}")
    print(f"  URLs: {total}")
    print(f"  Worker timeout: {worker_timeout}s ({worker_timeout // 60}m)")
    print(f"  Output: {output_dir}")
    print(f"  Logs: {log_dir}")
    print(f"{'='*60}\n")

    # Build work items
    work_items = [
        {
            "url": url,
            "index": i + 1,
            "total": total,
            "engine_kwargs": engine_kwargs,
            "log_dir": log_dir,
        }
        for i, url in enumerate(urls)
    ]

    # Process in chunks to survive mass worker deaths.
    # If all workers in a pool die, we spawn a new pool for the next chunk.
    chunk_size = workers * 3
    chunks = [work_items[i:i + chunk_size] for i in range(0, len(work_items), chunk_size)]

    interrupted = False
    active_pool = None

    try:
        for chunk_idx, chunk in enumerate(chunks):
            print(f"\n[Chunk {chunk_idx + 1}/{len(chunks)}] Processing {len(chunk)} URLs...")

            # Use 'spawn' to get clean child processes (avoids fork issues with Playwright)
            ctx = multiprocessing.get_context("spawn")
            pool = ctx.Pool(processes=workers)
            active_pool = pool

            async_results = []
            for item in chunk:
                ar = pool.apply_async(_worker_scan, (item,))
                async_results.append((item, ar))

            # Close pool to new submissions; workers keep running
            pool.close()

            for item, ar in async_results:
                url = item["url"]
                domain = item["url"]
                try:
                    from .utils import extract_domain
                    domain = extract_domain(url)
                except Exception:
                    pass

                try:
                    result = ar.get(timeout=worker_timeout)
                    tracker.record(result)
                except multiprocessing.TimeoutError:
                    # Worker exceeded hard timeout — force kill
                    result = {
                        "url": url,
                        "domain": domain,
                        "index": item["index"],
                        "status": "TIMEOUT_KILLED",
                        "error": f"Worker exceeded {worker_timeout}s hard timeout",
                        "duration_s": worker_timeout,
                        "log_file": os.path.join(log_dir, f"{domain}.log"),
                    }
                    tracker.record(result)

                    # Append a marker to the log file
                    try:
                        log_path = os.path.join(log_dir, f"{domain}.log")
                        with open(log_path, "a") as f:
                            f.write(f"\n\n[KILLED] Worker forcibly terminated after {worker_timeout}s\n")
                            f.write(f"[KILLED] at {datetime.now().isoformat()}\n")
                    except Exception:
                        pass

                    print(f"\n  ⚠ KILLED: {domain} exceeded {worker_timeout}s timeout")

                except Exception as e:
                    result = {
                        "url": url,
                        "domain": domain,
                        "index": item["index"],
                        "status": "ERROR",
                        "error": str(e),
                        "duration_s": 0,
                    }
                    tracker.record(result)

            # Terminate any remaining workers in this chunk (especially timed-out ones)
            try:
                pool.terminate()
                pool.join()
            except Exception:
                pass
            active_pool = None

    except KeyboardInterrupt:
        interrupted = True
        print(f"\n\n{'='*60}")
        print(f"  ⚠ INTERRUPTED BY USER (Ctrl+C)")
        print(f"  Terminating workers and saving progress...")
        print(f"{'='*60}")

        # Kill all remaining workers
        if active_pool:
            try:
                active_pool.terminate()
                active_pool.join()
            except Exception:
                pass

    # Save summary (works for both normal completion and interruption)
    summary = tracker.save_summary()
    if interrupted:
        summary["interrupted"] = True

    status_label = "INTERRUPTED" if interrupted else "COMPLETE"
    print(f"\n\n{'='*60}")
    print(f"  PARALLEL CRAWL {status_label}")
    print(f"  Total:      {summary['total_urls']}")
    print(f"  Succeeded:  {summary['succeeded']}")
    print(f"  Failed:     {summary['failed']}")
    print(f"  Timed out:  {summary['timed_out']}")
    print(f"  Skipped:    {summary['skipped']}")
    print(f"  Duration:   {summary['total_duration_s']}s ({summary['total_duration_s'] / 60:.1f}m)")
    print(f"  Avg/site:   {summary['avg_duration_s']}s")
    print(f"  Progress:   {tracker.journal_path}")
    if interrupted:
        print(f"  ℹ Resume with: --skip-existing")
    print(f"{'='*60}\n")

    return summary
