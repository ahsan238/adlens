"""
engine.py — Main orchestrator connecting all three subsystems:
  1. Crawl → 2. Detect → 3. Comply → 4. Generate Rules → 5. Report
"""

import os
import signal
import json
from datetime import datetime
from typing import List, Optional

from .config import (
    BASE_OUTPUT_DIR,
    VIEWPORTS,
    PHASE_CRAWL_TIMEOUT,
    PHASE_DETECT_TIMEOUT,
    PHASE_COMPLY_TIMEOUT,
    PHASE_GEN_TIMEOUT,
)
from .models import AdCandidate, AdReport, ScanReport
from .utils import (
    extract_domain,
    make_session_dir,
    get_session_id,
    save_json,
    classify_ad_size,
    timeout_phase,
    dedup_nested_ads,
)
from .crawler import StealthCrawler
from .detector_heuristic import HeuristicDetector
from .detector_llm import LLMDetector
from .compliance import ComplianceChecker
from .rule_generator import RuleGenerator, NetworkRuleMiner, format_rules_file
from .exception_auditor import ExceptionAuditor


class AdlensEngine:
    """
    The main orchestration engine for the AdLens Ad Intelligence System.

    Usage:
        engine = AdlensEngine(mode="heuristic")
        report = engine.scan("https://example.com")
    """

    def __init__(
        self,
        mode: str = "heuristic",
        api_key: str = "",
        output_dir: str = "",
        viewport: str = "desktop",
        headless: bool = True,
        timeout_ms: int = 45_000,
        scan_timeout: int = 180,
        skip_existing: bool = False,
        exception_list_path: str = "",
        record_video: bool = True,
    ):
        self.mode = mode
        self.api_key = api_key
        self.output_dir = output_dir or BASE_OUTPUT_DIR
        self.viewport = viewport
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.scan_timeout = scan_timeout
        self.skip_existing = skip_existing
        self.exception_list_path = exception_list_path
        self.record_video = record_video

        if mode == "llm" and not api_key:
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                raise ValueError(
                    "LLM mode requires an OpenAI API key. "
                    "Set via --api-key or OPENAI_API_KEY environment variable."
                )
            self.api_key = api_key

    def scan(self, url: str) -> Optional[ScanReport]:
        """
        Run the full pipeline on a single URL:
        Crawl → Detect → Comply → Generate → Report
        """
        domain = extract_domain(url)

        # Check if we should skip
        if self.skip_existing:
            domain_dir = os.path.join(self.output_dir, domain)
            if os.path.exists(domain_dir) and any(
                d.startswith("session_") for d in os.listdir(domain_dir)
            ):
                print(f"  [Skipping] Results already exist for {domain}")
                return None

        session_dir = make_session_dir(self.output_dir, domain)
        session_id = get_session_id(session_dir)

        print(f"\n{'='*60}")
        print(f"  ADLENS AD INTELLIGENCE SYSTEM")
        print(f"  Mode: {self.mode.upper()}")
        print(f"  URL: {url}")
        print(f"  Output: {session_dir}")
        print(f"{'='*60}\n")

        report = ScanReport(
            url=url,
            domain=domain,
            session_id=session_id,
            mode=self.mode,
            viewport=self.viewport,
            start_time=datetime.now().isoformat(),
            output_dir=session_dir,
        )

        crawler = StealthCrawler(
            viewport=self.viewport,
            headless=self.headless,
            timeout_ms=self.timeout_ms,
            record_video=self.record_video,
        )

        # Define timeout handler
        def timeout_handler(signum, frame):
            raise TimeoutError(f"Scan exceeded time limit of {self.scan_timeout}s")

        # Set alarm
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(self.scan_timeout)

        try:
            # ─── Phase 1: Crawl ─────────────────────────────
            print("[Phase 1] Crawling page...")
            with timeout_phase(PHASE_CRAWL_TIMEOUT, "Crawl"):
                page, network_log = crawler.crawl(url, session_dir)

            # ─── Phase 2: Detect ────────────────────────────
            print(f"\n[Phase 2] Detecting ads ({self.mode} mode)...")
            
            # Liveness check
            is_alive = False
            try:
                with timeout_phase(5, "Liveness Check"):
                    page.evaluate("1")
                    is_alive = True
            except Exception:
                print("  [Warning] Browser unresponsive after crawl. Skipping detection.")
            
            if is_alive:
                with timeout_phase(PHASE_DETECT_TIMEOUT, "Detect"):
                    ads, cost, tokens = self._detect(page, session_dir, network_log)
            else:
                ads, cost, tokens = [], 0.0, {}
            
            report.total_candidates = len(ads)
            report.confirmed_ads = len(ads)
            report.total_cost_usd = cost
            report.total_prompt_tokens = tokens.get("prompt", 0)
            report.total_completion_tokens = tokens.get("completion", 0)

            # Deduplicate nested ads (e.g. parent wrapper div + child iframe
            # for the same ad unit) before compliance to avoid double-counting
            if ads:
                ads = dedup_nested_ads(ads)
                report.confirmed_ads = len(ads)

            if not ads:
                print("  No ads detected on this page.")

                # Even with no visible ads, mine network log for tracker requests
                print(f"\n[Phase 4.1] Mining network log for tracker blocking rules...")
                all_rules = []
                try:
                    with timeout_phase(PHASE_GEN_TIMEOUT, "Network Rule Mining"):
                        miner = NetworkRuleMiner(domain)
                        mined_rules = miner.mine(network_log.requests)
                        all_rules.extend(mined_rules)
                        if mined_rules:
                            print(f"  Mined {len(mined_rules)} tracker/ad-script blocking rules from traffic log")
                        else:
                            print(f"  No ad/tracker requests found in network log")
                except Exception as e:
                    print(f"  [Warning] Network rule mining failed: {e}")
            else:
                print(f"  Detected {len(ads)} ads")

                # ─── Phase 3: Compliance ────────────────────
                print(f"\n[Phase 3] Checking compliance...")
                with timeout_phase(PHASE_COMPLY_TIMEOUT, "Compliance"):
                    checker = ComplianceChecker(viewport_type=self.viewport)
                    compliance_results = checker.check_batch(ads)

                # ─── Phase 4: Rule Generation ───────────────
                print(f"\n[Phase 4] Generating filter rules...")
                with timeout_phase(PHASE_GEN_TIMEOUT, "Rule Generation"):
                    rule_gen = RuleGenerator(domain)
                    all_rules = rule_gen.generate_batch(
                        list(zip(ads, compliance_results))
                    )

                # ─── Phase 4.1: Network Log Rule Mining ─────
                print(f"\n[Phase 4.1] Mining network log for ad/tracker blocking rules...")
                mined_rules: list = []
                try:
                    with timeout_phase(PHASE_GEN_TIMEOUT, "Network Rule Mining"):
                        miner = NetworkRuleMiner(domain)
                        mined_rules = miner.mine(network_log.requests)
                        # Deduplicate against rules already produced by RuleGenerator
                        existing_syntax = {r.syntax for r in all_rules}
                        mined_rules = [r for r in mined_rules if r.syntax not in existing_syntax]
                        all_rules.extend(mined_rules)
                        if mined_rules:
                            print(f"  Mined {len(mined_rules)} additional network blocking rules from traffic log")
                        else:
                            print(f"  No additional network rules beyond what RuleGenerator produced")
                except Exception as e:
                    print(f"  [Warning] Network rule mining failed: {e}")

                # ─── Phase 5: Build report ──────────────────
                print(f"\n[Phase 5] Building report...")
                for i, (ad, comp) in enumerate(zip(ads, compliance_results)):
                    ad_rules = rule_gen.generate(ad, comp)
                    ad_report = AdReport(
                        candidate=ad,
                        compliance=comp,
                        rules=ad_rules,
                    )
                    report.ads.append(ad_report)

                    # Console output
                    status = "✅ COMPLIANT" if comp.is_compliant else "❌ NON-COMPLIANT"
                    size_str = classify_ad_size(
                        ad.rect.width, ad.rect.height
                    ) if ad.rect else "unknown"

                    print(f"\n  Ad #{ad.index}: {ad.ad_type} ({size_str})")
                    print(f"    Status: {status}")
                    if not comp.is_compliant:
                        for v in comp.violations:
                            print(f"    ⚠ {v.category}: {v.description}")
                        for r in ad_rules:
                            print(f"    📋 Rule [{r.rule_type}]: {r.syntax}")

            report.compliant_ads = sum(
                1 for ar in report.ads if ar.compliance.is_compliant
            )
            report.non_compliant_ads = sum(
                1 for ar in report.ads if not ar.compliance.is_compliant
            )
            
            # Use empty list if variables not defined (case where ads was empty)
            # Actually, `all_rules` is only defined in the else block above.
            # We need to initialize it or handle it.
            if 'all_rules' not in locals():
                all_rules = []

            report.total_rules_generated = len(all_rules)

            # ─── Phase 4.5: Exception Rule Audit ──────────
            audit_results = []
            if self.exception_list_path and report.non_compliant_ads > 0:
                print(f"\n[Phase 4.5] Auditing exception rules...")
                try:
                    auditor = ExceptionAuditor(domain, self.exception_list_path)
                    audit_results = auditor.audit(ads, compliance_results)
                    report.exception_audit = audit_results
                    if audit_results:
                        print(f"  [Audit] Flagged {len(audit_results)} exception rule(s)\n")
                        for ar in audit_results:
                            rule_preview = ar.original_rule[:100] + "..." if len(ar.original_rule) > 100 else ar.original_rule
                            print(f"  🔍 Exception Rule (line {ar.line_number}):")
                            print(f"     Rule: {rule_preview}")
                            print(f"     Enabled ads: {ar.matched_ad_indices}")
                            print(f"     Violations: {', '.join(ar.violation_types)}")
                            print(f"     Action: {ar.recommendation}")
                            print(f"     {ar.reasoning}")
                            if ar.suggested_rule:
                                print(f"     Fix: {ar.suggested_rule}")
                            print()
                    else:
                        print(f"  [Audit] No exception rules matched the non-compliant ads")
                except Exception as e:
                    print(f"  [Audit] Warning: Exception audit failed: {e}")

            # ─── Save outputs ───────────────────────────
            # Compliance report
            report.end_time = datetime.now().isoformat()
            report_path = os.path.join(session_dir, "compliance_report.json")
            save_json(report.to_dict(), report_path)

            # Rules file
            if all_rules:
                rules_path = os.path.join(session_dir, "rules.txt")
                rules_content = format_rules_file(all_rules, url)
                with open(rules_path, "w") as f:
                    f.write(rules_content)

            # Exception audit file
            if report.exception_audit:
                audit_path = os.path.join(session_dir, "exception_audit.json")
                save_json({
                    "domain": domain,
                    "url": url,
                    "exception_list": self.exception_list_path,
                    "total_rules_scoped": len(auditor.domain_rules) if 'auditor' in locals() else 0,
                    "flagged_rules": len(report.exception_audit),
                    "results": [e.to_dict() for e in report.exception_audit],
                }, audit_path)

            print(f"\n{'='*60}")
            print(f"  SCAN COMPLETE")
            print(f"  Ads found: {report.confirmed_ads}")
            print(f"  Compliant: {report.compliant_ads}")
            print(f"  Non-compliant: {report.non_compliant_ads}")
            print(f"  Rules generated: {report.total_rules_generated}")
            print(f"  Report: {report_path}")
            if all_rules:
                print(f"  Rules: {rules_path}")
            print(f"{'='*60}\n")

        except TimeoutError as e:
            print(f"\n[ERROR] Scan timed out: {e}")
            report.end_time = datetime.now().isoformat()
            # Save partial report
            report_path = os.path.join(session_dir, "compliance_report.json")
            save_json(report.to_dict(), report_path)
            
        except Exception as e:
            print(f"\n[ERROR] Scan failed: {e}")
            import traceback
            traceback.print_exc()
            report.end_time = datetime.now().isoformat()
            # Still save partial report
            report_path = os.path.join(session_dir, "compliance_report.json")
            save_json(report.to_dict(), report_path)

        finally:
            # Disable alarm
            signal.alarm(0)
            crawler.close()

        return report

    def scan_batch(self, urls: List[str]) -> List[ScanReport]:
        """Scan multiple URLs sequentially, robust to individual failures."""
        reports = []
        try:
            for i, url in enumerate(urls):
                print(f"\n[Batch {i+1}/{len(urls)}] Scanning {url}")
                try:
                    report = self.scan(url)
                    if report:
                        reports.append(report)
                except KeyboardInterrupt:
                    print("\n[Batch] Interrupted by user. Stopping.")
                    break
                except Exception as e:
                    print(f"  [ERROR] Failed to scan {url}: {e}")
                    import traceback
                    traceback.print_exc()
        except KeyboardInterrupt:
            print("\n[Batch] Interrupted. Exiting.")
            
        return reports

    def _detect(self, page, session_dir, network_log):
        """Run the appropriate detector based on mode."""
        if self.mode == "llm":
            detector = LLMDetector(
                page=page,
                session_dir=session_dir,
                api_key=self.api_key,
                network_log=network_log,
            )
        else:
            detector = HeuristicDetector(
                page=page,
                session_dir=session_dir,
                network_log=network_log,
            )
        return detector.detect(), getattr(detector, "total_cost", 0.0), getattr(detector, "total_tokens", {})
