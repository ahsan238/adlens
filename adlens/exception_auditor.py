"""
exception_auditor.py — Audits Acceptable Ads exception rules against detected
non-compliant ads. Flags overly permissive @@-rules and recommends tighter
alternatives.

Usage:
    auditor = ExceptionAuditor("geology.com", "/path/to/exceptionlist.txt")
    results = auditor.audit(ads, compliance_results)
"""

import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from .models import AdCandidate, ComplianceResult, ExceptionAuditResult


class ExceptionAuditor:
    """
    Cross-references non-compliant ads with Acceptable Ads exception rules
    to find which @@-rules enabled the violations.
    """

    def __init__(self, domain: str, exception_list_path: str):
        self.domain = domain
        self.exception_list_path = exception_list_path
        self.domain_rules: List[dict] = []   # Parsed rules scoped to this domain
        self._parse_exception_list()

    # ─── Parsing ────────────────────────────────────────────

    def _parse_exception_list(self):
        """Parse the exception list and extract @@-rules scoped to our domain."""
        try:
            with open(self.exception_list_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line.startswith("@@"):
                        continue

                    # Check if this rule's domain= option includes our domain
                    domain_match = re.search(r'\$([^$]*)', line)
                    if not domain_match:
                        continue

                    options_str = domain_match.group(1)
                    # Extract domain= option
                    dm = re.search(r'domain=([^\s$]+)', options_str)
                    if not dm:
                        continue

                    domain_list = dm.group(1).split("|")
                    if self.domain not in domain_list:
                        continue

                    # Extract the URL pattern (between @@ and $)
                    url_pattern = line[2:].split("$")[0] if "$" in line else line[2:]

                    # Extract resource types
                    type_tokens = []
                    for opt in options_str.split(","):
                        opt = opt.strip()
                        if opt.startswith("domain="):
                            continue
                        if opt:
                            type_tokens.append(opt)

                    # Extract the whitelisted domain from the URL pattern
                    whitelisted_domain = ""
                    pat_match = re.match(r'\|\|([^/^]+)', url_pattern)
                    if pat_match:
                        whitelisted_domain = pat_match.group(1)

                    self.domain_rules.append({
                        "line_number": line_num,
                        "raw": line,
                        "url_pattern": url_pattern,
                        "whitelisted_domain": whitelisted_domain,
                        "resource_types": type_tokens,
                        "all_domains": domain_list,
                    })

        except FileNotFoundError:
            print(f"  [Audit] Warning: Exception list not found: {self.exception_list_path}")
        except Exception as e:
            print(f"  [Audit] Warning: Failed to parse exception list: {e}")

    # ─── Auditing ───────────────────────────────────────────

    def audit(
        self,
        ads: List[AdCandidate],
        compliance_results: List[ComplianceResult],
    ) -> List[ExceptionAuditResult]:
        """
        Cross-reference non-compliant ads with exception rules.

        For each non-compliant ad, check if any exception rule's whitelisted
        domain matches the ad's source URL or provenance. If so, flag
        that exception rule.
        """
        if not self.domain_rules:
            print(f"  [Audit] No exception rules found for {self.domain}")
            return []

        print(f"  [Audit] Found {len(self.domain_rules)} exception rules for {self.domain}")

        # Collect non-compliant ads with their violations
        nc_ads = []
        for ad, comp in zip(ads, compliance_results):
            if not comp.is_compliant:
                nc_ads.append((ad, comp))

        if not nc_ads:
            return []

        # For each exception rule, find which non-compliant ads it enabled
        rule_hits: dict = {}  # rule_line -> {rule_info, ad_indices, violations}

        for ad, comp in nc_ads:
            ad_domains = self._extract_ad_domains(ad)
            for rule in self.domain_rules:
                if self._rule_matches_ad(rule, ad_domains):
                    key = rule["line_number"]
                    if key not in rule_hits:
                        rule_hits[key] = {
                            "rule": rule,
                            "ad_indices": [],
                            "violation_types": set(),
                        }
                    rule_hits[key]["ad_indices"].append(ad.index)
                    for v in comp.violations:
                        rule_hits[key]["violation_types"].add(v.category)

        # Build audit results with recommendations
        results = []
        for line_num, hit in sorted(rule_hits.items()):
            rule = hit["rule"]
            violations = list(hit["violation_types"])
            recommendation, suggested, reasoning = self._recommend_fix(
                rule, violations
            )
            result = ExceptionAuditResult(
                original_rule=rule["raw"],
                line_number=line_num,
                matched_ad_indices=hit["ad_indices"],
                violation_types=violations,
                recommendation=recommendation,
                suggested_rule=suggested,
                reasoning=reasoning,
            )
            results.append(result)

        return results

    # ─── Matching ───────────────────────────────────────────

    def _extract_ad_domains(self, ad: AdCandidate) -> List[str]:
        """Extract all domains associated with an ad (src, provenance)."""
        domains = []
        for url in [ad.src, ad.provenance]:
            if url and url.startswith("http"):
                try:
                    parsed = urlparse(url)
                    if parsed.hostname:
                        domains.append(parsed.hostname)
                except Exception:
                    pass
        return domains

    def _rule_matches_ad(self, rule: dict, ad_domains: List[str]) -> bool:
        """
        Check if an exception rule's whitelisted domain matches any of
        the ad's source domains.

        Uses substring matching since the rule pattern may be a subdomain
        or parent domain of the ad's actual source.
        """
        wl_domain = rule["whitelisted_domain"]
        if not wl_domain:
            # Rules like @@^upapi=true$ match by URL pattern, not domain
            # These are broad network-level whitelists — always match
            return True

        for ad_domain in ad_domains:
            # Check both directions: rule domain in ad domain, or vice versa
            if wl_domain in ad_domain or ad_domain in wl_domain:
                return True

            # Also check if they share the same base domain
            # e.g., "pagead2.googlesyndication.com" vs "*.googlesyndication.com"
            wl_parts = wl_domain.split(".")
            ad_parts = ad_domain.split(".")
            if len(wl_parts) >= 2 and len(ad_parts) >= 2:
                if wl_parts[-2:] == ad_parts[-2:]:
                    return True

        return False

    # ─── Recommendations ────────────────────────────────────

    def _recommend_fix(
        self, rule: dict, violations: List[str]
    ) -> Tuple[str, str, str]:
        """
        Generate a recommendation for a flagged exception rule.

        Returns: (recommendation_type, suggested_rule, reasoning)
        """
        has_size = "Size" in violations
        has_sticky = any(v in violations for v in ["Overlay", "Sticky"])
        has_distinction = "Distinction" in violations
        has_behavior = "Behavior" in violations

        # Determine recommendation based on violation severity
        if has_sticky or has_size or has_behavior:
            # Structural violations — the exception rule is too permissive
            recommendation = "remove_domain"
            suggested = self._remove_domain_from_rule(rule)
            reasons = []
            if has_size:
                reasons.append("ads exceed size limits")
            if has_sticky:
                reasons.append("sticky/overlay ads violate positioning rules")
            if has_behavior:
                reasons.append("ads have disruptive behavior")
            reasoning = (
                f"This exception rule allows non-compliant ads on {self.domain}: "
                f"{', '.join(reasons)}. "
                f"Recommend removing {self.domain} from the domain list to "
                f"restore EasyList blocking for this site."
            )
        elif has_distinction:
            # Labeling issue — more of a publisher problem
            recommendation = "flag_only"
            suggested = ""
            reasoning = (
                f"Ads on {self.domain} lack proper labeling (no 'Advertisement' "
                f"marker). This is primarily a publisher compliance issue. "
                f"The exception rule itself is not the cause, but the site "
                f"should not be in the Acceptable Ads program without proper labeling."
            )
        else:
            recommendation = "flag_only"
            suggested = ""
            reasoning = f"Exception rule allows ads on {self.domain} that have minor violations."

        return recommendation, suggested, reasoning

    def _remove_domain_from_rule(self, rule: dict) -> str:
        """
        Generate a replacement rule with our domain removed from the domain= list.
        """
        raw = rule["raw"]
        all_domains = rule["all_domains"]
        new_domains = [d for d in all_domains if d != self.domain]

        if not new_domains:
            # If this was the only domain, the entire rule should be removed
            return f"! Remove: {raw[:80]}..."

        # Replace the domain= option with the new list
        old_domain_str = "|".join(all_domains)
        new_domain_str = "|".join(new_domains)

        # Since domain lists are huge, just note the change
        return (
            f"! Modify domain= list: remove '{self.domain}' "
            f"({len(all_domains)} domains → {len(new_domains)} domains)"
        )
