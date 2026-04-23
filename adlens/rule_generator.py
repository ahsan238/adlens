"""
rule_generator.py — ABP filter rule generation for non-compliant ads.
Generates network, cosmetic, procedural, and exception rules
in EasyList-compatible syntax.

Enhanced with NetworkRuleMiner for standalone network-log analysis:
  - Ad-domain blocking (scripts, pixels, bid requests)
  - Loader-script blocking ($script type)
  - First-party ad-path rules
  - Provenance-chain tracing (initiator → loader script)
  - Resource-type annotations ($script, $image, $subdocument, $xmlhttprequest)
"""

import re
from typing import List, Optional, Dict, Any, Set
from urllib.parse import urlparse

from .config import AD_NETWORK_DOMAINS
from .models import AdCandidate, ComplianceResult, FilterRule
from .utils import extract_domain, is_third_party, is_same_site, is_ad_url


# ─── Resource type mapping: Playwright type → ABP $option ───
RESOURCE_TYPE_MAP = {
    "script":         "$script",
    "stylesheet":     "$stylesheet",
    "image":          "$image",
    "font":           "$font",
    "xhr":            "$xmlhttprequest",
    "fetch":          "$xmlhttprequest",
    "document":       "$document",
    "subdocument":    "$subdocument",       # iframes
    "media":          "$media",
    "websocket":      "$websocket",
    "ping":           "$ping",
    "other":          "",                    # no type narrowing
}

# Well-known ad loader scripts — blocking these prevents the entire ad
# cascade from starting (the same strategy EasyList/uBlock use).
AD_LOADER_SCRIPT_PATTERNS = [
    r"securepubads\.g\.doubleclick\.net/tag/js/gpt\.js",   # Google Publisher Tags
    r"pagead2\.googlesyndication\.com/tag/js/gpt\.js",
    r"pagead2\.googlesyndication\.com/pagead/js/adsbygoogle\.js",
    r"cdn\.taboola\.com/libtrc/",                            # Taboola loader
    r"widgets\.outbrain\.com/outbrain\.js",                  # Outbrain loader
    r"static\.criteo\.net/js/",                               # Criteo bidder
    r"c\.amazon-adsystem\.com/aax2/",                         # Amazon A9 bidder
    r"htlb\.casalemedia\.com/",                               # Index Exchange
    r"micro\.rubiconproject\.com/",                           # Magnite/Rubicon
    r"ads\.pubmatic\.com/",                                   # PubMatic
    r"eus\.rubiconproject\.com/",
    r"hbopenbid\.pubmatic\.com/",
    r"js-sec\.indexww\.com/",                                 # Index Exchange
    r"ap\.lijit\.com/",                                       # Sovrn
    r"cdn\.sharethrough\.com/",                               # Sharethrough
    r"t\.yieldmo\.com/",                                      # Yieldmo
    r"assets\.triplelift\.com/",                              # TripleLift
    r"s\.ntv\.io/",                                           # Nativo
    r"a\.teads\.tv/",                                         # Teads
    r"cdn\.medianet\.com/",                                   # Media.net
    r"js\.ad-score\.com/",                                    # Adelaide / ad measurement
    r"sb\.scorecardresearch\.com/",                           # Comscore tracker
]

_AD_LOADER_RE = re.compile("|".join(AD_LOADER_SCRIPT_PATTERNS), re.IGNORECASE)


class RuleGenerator:
    """
    Generates AdBlock Plus filter rules for non-compliant ads.
    
    Rule priority: Network > Cosmetic (ID) > Cosmetic (Class) > Cosmetic (Attribute) > Procedural
    """

    def __init__(self, page_domain: str):
        self.page_domain = page_domain

    def generate(self, ad: AdCandidate, compliance: ComplianceResult) -> List[FilterRule]:
        """Generate filter rules for a non-compliant ad."""
        if compliance.is_compliant:
            return []

        rules: List[FilterRule] = []
        violation_cats = compliance.violation_types

        # Try each rule strategy in priority order
        network_rule = self._try_network_rule(ad, violation_cats)
        if network_rule:
            rules.append(network_rule)

        cosmetic_rule = self._try_cosmetic_rule(ad, violation_cats)
        if cosmetic_rule:
            rules.append(cosmetic_rule)

        procedural_rule = self._try_procedural_rule(ad, violation_cats)
        if procedural_rule:
            rules.append(procedural_rule)

        # If no rules could be generated, try a fallback
        if not rules:
            fallback = self._fallback_rule(ad, violation_cats)
            if fallback:
                rules.append(fallback)

        return rules

    def generate_batch(self, ads_and_compliance: List[tuple]) -> List[FilterRule]:
        """Generate rules for a batch of (AdCandidate, ComplianceResult) pairs.
        Deduplicates rules by syntax."""
        all_rules: List[FilterRule] = []
        seen_syntax: set = set()

        for ad, compliance in ads_and_compliance:
            rules = self.generate(ad, compliance)
            for rule in rules:
                if rule.syntax not in seen_syntax:
                    seen_syntax.add(rule.syntax)
                    all_rules.append(rule)

        return all_rules

    # ─── Rule Strategies ────────────────────────────────────

    def _try_network_rule(self, ad: AdCandidate, violations: List[str]) -> Optional[FilterRule]:
        """
        PRIORITY 1: Network blocking rule.
        Best for third-party ad content. Blocks the entire request.
        
        Enhanced to:
          - Trace provenance chain to the initiator (loader) script
          - Prefer blocking the loader over the leaf resource
          - Add $script / $subdocument type options for precision

        Syntax: ||domain.com^
        With options: ||domain.com^$third-party,domain=page.com
        Typed:  ||domain.com/path$script
        """
        src = ad.src
        if not src or src == "Unknown" or not src.startswith("http"):
            # Check provenance
            src = ad.provenance if ad.provenance.startswith("http") else ""

        if not src:
            return None

        try:
            src_domain = extract_domain(src)
        except Exception:
            return None

        if not src_domain or not is_third_party(src, self.page_domain):
            # Even if same-site, check for first-party ad paths
            if src_domain and _has_ad_path(src):
                path = _extract_ad_path(src)
                if path:
                    rule_syntax = f"||{src_domain}{path}"
                    return FilterRule(
                        syntax=rule_syntax,
                        rule_type="network",
                        target_domain=src_domain,
                        reasoning=f"First-party ad path block: {src_domain}{path}",
                        violation_category=", ".join(violations),
                    )
            if not is_third_party(src, self.page_domain):
                return None

        # Determine resource type for ABP type option
        type_option = _infer_type_option(ad.tag_name, src)

        # Check provenance for loader-script blocking:
        # If the ad was injected by an ad loader, blocking the loader is more
        # effective than blocking the leaf resource.
        provenance_rule = self._try_provenance_loader_rule(ad, violations)
        if provenance_rule:
            return provenance_rule

        # Check if it's a known ad domain — use a simple blocking rule
        is_known_ad = any(ad_domain in src_domain for ad_domain in AD_NETWORK_DOMAINS)

        if is_known_ad:
            rule_syntax = f"||{src_domain}^"
            if type_option:
                rule_syntax = f"||{src_domain}^{type_option}"
            reasoning = f"Network block: known ad domain {src_domain}"
        else:
            # Third-party but not a known ad domain — scope to the page domain
            options = f"$third-party,domain={self.page_domain}"
            if type_option:
                options = f"{type_option},third-party,domain={self.page_domain}"
            rule_syntax = f"||{src_domain}^{options}"
            reasoning = f"Scoped network block: third-party content from {src_domain}"

        return FilterRule(
            syntax=rule_syntax,
            rule_type="network",
            target_domain=src_domain,
            reasoning=reasoning,
            violation_category=", ".join(violations),
        )

    def _try_provenance_loader_rule(
        self, ad: AdCandidate, violations: List[str]
    ) -> Optional[FilterRule]:
        """
        If the ad element has a data-provenance-source pointing to a known
        ad loader script, generate a rule that blocks the *loader* instead of
        (or in addition to) the leaf element.  Blocking the loader prevents
        the entire ad cascade from initialising.
        """
        prov = ad.provenance
        if not prov or prov == "unknown" or not prov.startswith("http"):
            # Also check the data_attributes dict for provenance
            prov = ad.data_attributes.get("data-provenance-source", "")
        if not prov or not prov.startswith("http"):
            return None

        # Is the provenance URL a known ad loader?
        if not _AD_LOADER_RE.search(prov):
            return None

        try:
            loader_domain = extract_domain(prov)
        except Exception:
            return None

        # Generate a precise script-blocking rule for the loader
        # e.g.  ||securepubads.g.doubleclick.net/tag/js/gpt.js$script
        parsed = urlparse(prov)
        loader_path = parsed.path.rstrip("/")
        if loader_path:
            rule_syntax = f"||{loader_domain}{loader_path}$script"
        else:
            rule_syntax = f"||{loader_domain}^$script"

        return FilterRule(
            syntax=rule_syntax,
            rule_type="network",
            target_domain=loader_domain,
            reasoning=f"Loader script block: provenance traces ad to {loader_domain}{loader_path}",
            violation_category=", ".join(violations),
        )

    def _try_cosmetic_rule(self, ad: AdCandidate, violations: List[str]) -> Optional[FilterRule]:
        """
        PRIORITY 2: Element hiding (cosmetic) rule.
        Hides the ad element from the page using CSS selectors.
        
        Uses smart ID generalization to produce robust rules:
        - Exact ID match for truly stable IDs (e.g., "adm-header")
        - Wildcard [id^="prefix"] for IDs with dynamic parts (e.g., uid suffixes)
        - Class-based fallback for elements without useful IDs
        
        Syntax: domain.com##.class-name
                domain.com##div#id-name
                domain.com##div[id^="prefix"]
                domain.com##[data-attr="value"]
        """
        # Strategy A: ID-based rules (with smart generalization)
        if ad.element_id and len(ad.element_id) > 2:
            generalized = self._generalize_id(ad.element_id)
            
            if generalized:
                tag = ad.tag_name.lower()
                
                if generalized["type"] == "exact":
                    # Truly stable ID — use exact match
                    safe_id = self._css_escape(generalized["value"])
                    rule_syntax = f"{self.page_domain}##{tag}#{safe_id}"
                    reasoning = f"Hide element by ID: #{safe_id}"
                elif generalized["type"] == "prefix":
                    # ID has dynamic suffix — use starts-with selector
                    prefix = self._css_escape(generalized["value"])
                    rule_syntax = f'{self.page_domain}##{tag}[id^="{prefix}"]'
                    reasoning = f'Hide elements by ID prefix: [id^="{prefix}"]'
                elif generalized["type"] == "contains":
                    # Use contains selector for less-structured IDs
                    fragment = self._css_escape(generalized["value"])
                    rule_syntax = f'{self.page_domain}##{tag}[id*="{fragment}"]'
                    reasoning = f'Hide elements by ID fragment: [id*="{fragment}"]'
                else:
                    rule_syntax = None
                
                if rule_syntax:
                    return FilterRule(
                        syntax=rule_syntax,
                        rule_type="cosmetic",
                        target_domain=self.page_domain,
                        reasoning=reasoning,
                        violation_category=", ".join(violations),
                    )

        # Strategy B: Class-based (good if classes are meaningful)
        meaningful_classes = self._find_meaningful_classes(ad.classes)
        if meaningful_classes:
            selector = ".".join(self._css_escape(c) for c in meaningful_classes[:2])
            rule_syntax = f"{self.page_domain}##.{selector}"
            return FilterRule(
                syntax=rule_syntax,
                rule_type="cosmetic",
                target_domain=self.page_domain,
                reasoning=f"Hide element by class: .{selector}",
                violation_category=", ".join(violations),
            )

        # Strategy C: Data attribute-based
        for attr, value in ad.data_attributes.items():
            ad_keywords = ["ad", "sponsor", "slot", "banner", "promo"]
            if any(kw in attr.lower() or kw in value.lower() for kw in ad_keywords):
                if value:
                    rule_syntax = f'{self.page_domain}##[{attr}="{value}"]'
                else:
                    rule_syntax = f"{self.page_domain}##[{attr}]"
                return FilterRule(
                    syntax=rule_syntax,
                    rule_type="cosmetic",
                    target_domain=self.page_domain,
                    reasoning=f"Hide element by attribute: [{attr}]",
                    violation_category=", ".join(violations),
                )

        return None

    def _try_procedural_rule(self, ad: AdCandidate, violations: List[str]) -> Optional[FilterRule]:
        """
        PRIORITY 3: Procedural (extended CSS) rule.
        For complex cases like sticky overlays or elements matched by style properties.
        
        Syntax: domain.com##div:-abp-properties(position: fixed)
                domain.com#?#div:-abp-has(> .ad-content)
        """
        # Overlay / sticky violations
        if "Overlay" in violations or ad.is_sticky:
            position = ad.styles.get("position", "static")
            if position in ("fixed", "sticky"):
                tag = ad.tag_name.lower()
                rule_syntax = f"{self.page_domain}##{tag}:-abp-properties(position: {position})"

                # Try to be more specific if we have classes
                meaningful = self._find_meaningful_classes(ad.classes)
                if meaningful:
                    first_class = self._css_escape(meaningful[0])
                    rule_syntax = f"{self.page_domain}##{tag}.{first_class}:-abp-properties(position: {position})"

                return FilterRule(
                    syntax=rule_syntax,
                    rule_type="procedural",
                    target_domain=self.page_domain,
                    reasoning=f"Procedural rule for {position} positioned ad",
                    violation_category=", ".join(violations),
                )

        # Content-based hide (for sponsored content without clear selectors)
        if "Distinction" in violations and ad.text_snippet:
            keywords = ["sponsored", "promoted", "advertisement"]
            for kw in keywords:
                if kw in ad.text_snippet.lower():
                    tag = ad.tag_name.lower()
                    rule_syntax = f'{self.page_domain}#?#{tag}:-abp-contains("{kw.capitalize()}")'
                    return FilterRule(
                        syntax=rule_syntax,
                        rule_type="procedural",
                        target_domain=self.page_domain,
                        reasoning=f"Content-based hide: contains '{kw}'",
                        violation_category=", ".join(violations),
                    )

        return None

    def _fallback_rule(self, ad: AdCandidate, violations: List[str]) -> Optional[FilterRule]:
        """
        Last resort: generate a rule from whatever information we have.
        """
        # Try tag + nth-child heuristic based on position
        if ad.tag_name:
            tag = ad.tag_name.lower()
            if ad.classes:
                first_class = self._css_escape(ad.classes[0])
                if len(first_class) > 2:
                    rule_syntax = f"{self.page_domain}##{tag}.{first_class}"
                    return FilterRule(
                        syntax=rule_syntax,
                        rule_type="cosmetic",
                        target_domain=self.page_domain,
                        reasoning=f"Fallback: hide by tag.class ({tag}.{first_class})",
                        violation_category=", ".join(violations),
                    )

        return None

    # ─── Helpers ────────────────────────────────────────────

    def _find_meaningful_classes(self, classes: List[str]) -> List[str]:
        """Filter out tiny utility classes, keep meaningful ad-related ones."""
        meaningful = []
        ad_signals = ["ad", "advert", "sponsor", "banner", "promo", "native", "slot"]

        # Prioritize ad-related classes
        for cls in classes:
            if len(cls) > 2 and any(sig in cls.lower() for sig in ad_signals):
                meaningful.insert(0, cls)
            elif len(cls) > 3:
                meaningful.append(cls)

        return meaningful[:3]  # Return at most 3

    def _css_escape(self, text: str) -> str:
        """Escape special characters for CSS selector use."""
        # Replace characters that are invalid in CSS selectors
        return re.sub(r'([!"#$%&\'()*+,./:;<=>?@\[\]^`{|}~])', r'\\\1', text)

    def _is_stable_id(self, element_id: str) -> bool:
        """Check if an element ID is fully stable across page loads.
        Returns True only for IDs that can be used as exact matches."""
        # IDs with 5+ consecutive digits are session-specific
        if re.search(r'\d{5,}', element_id):
            return False
        # IDs with 3+ separate numeric segments suggest dynamic generation
        numeric_segments = re.findall(r'\d+', element_id)
        if len(numeric_segments) >= 3:
            return False
        # IDs with uid/uuid suffixes are dynamic
        if re.search(r'uid\d+', element_id, re.IGNORECASE):
            return False
        # IDs with session/instance counters (e.g., _0, _1, -0, -1 at the end)
        if re.search(r'[-_]\d{1,2}$', element_id):
            return False
        # Google iframe containers are always dynamic
        if 'google_ads_iframe' in element_id:
            return False
        # IDs with page-type markers (hp, ros, etc.) may vary across pages
        # but are stable enough if they don't have other dynamic parts
        return True

    def _generalize_id(self, element_id: str) -> Optional[dict]:
        """Analyze an element ID and return the best selector strategy.
        
        Returns dict with:
          - type: "exact" | "prefix" | "contains"
          - value: the stable portion of the ID to match on
        
        Logic:
          - Fully stable IDs → exact match (e.g., "adm-header")
          - IDs with dynamic suffixes → prefix match after stripping
            (e.g., "div-gpt-dsk-tab-bb-hp-mid-banner4-uid8" → "div-gpt-")
          - Google iframe IDs → prefix match on "google_ads_iframe_"
          - Unrecoverable IDs → None (fall through to class/attribute)
        """
        # Skip completely garbage IDs (long hashes, base64-like)
        if re.search(r'[a-f0-9]{20,}', element_id, re.IGNORECASE):
            return None
        
        # Fully stable — use exact match
        if self._is_stable_id(element_id):
            return {"type": "exact", "value": element_id}
        
        # Google iframe containers — always use prefix
        if 'google_ads_iframe' in element_id:
            return {"type": "prefix", "value": "google_ads_iframe_"}
        
        # GPT ad slots (e.g., "div-gpt-dsk-tab-bb-hp-mid-banner4-uid8")
        # Strip uid suffix and page-type markers for a broad prefix
        gpt_match = re.match(r'((?:div-)?gpt[-_])', element_id, re.IGNORECASE)
        if gpt_match:
            return {"type": "prefix", "value": gpt_match.group(1)}
        
        # IDs with uid suffixes — strip the uid part
        uid_match = re.match(r'(.+?)[-_]uid\d+', element_id, re.IGNORECASE)
        if uid_match:
            prefix = uid_match.group(1)
            # Further strip trailing numeric counters from the prefix
            prefix = re.sub(r'[-_]\d+$', '', prefix)
            if len(prefix) > 3:
                return {"type": "prefix", "value": prefix}
        
        # IDs with trailing numeric counters (e.g., "ad-slot-3", "banner_1")
        counter_match = re.match(r'(.+?)[-_]\d{1,2}$', element_id)
        if counter_match:
            prefix = counter_match.group(1)
            if len(prefix) > 3:
                return {"type": "prefix", "value": prefix}
        
        # IDs starting with known ad prefixes — use prefix match
        ad_prefix_match = re.match(r'(adm[-_]|ads[-_]|ad[-_]|gpt[-_])', element_id, re.IGNORECASE)
        if ad_prefix_match:
            return {"type": "prefix", "value": ad_prefix_match.group(1)}
        
        # Nothing recoverable
        return None


# ─── Module-level helpers (used by both RuleGenerator & NetworkRuleMiner) ───

def _infer_type_option(tag_name: str, url: str) -> str:
    """Return an ABP type option string ($script, $image, …) from context."""
    tag = tag_name.lower() if tag_name else ""
    if tag == "script" or url.endswith(".js"):
        return "$script"
    if tag == "iframe":
        return "$subdocument"
    if tag in ("img", "image") or any(url.endswith(ext) for ext in (".png", ".jpg", ".gif", ".webp", ".svg")):
        return "$image"
    return ""


def _has_ad_path(url: str) -> bool:
    """Check if a URL has an ad-related path component (first-party ad code)."""
    try:
        path = urlparse(url).path.lower()
        # Matches paths like /ads/, /ad/, /adserver/, /adtech/
        return bool(re.search(r'/(?:ads?|adserver|adtech|advert|sponsor|promo)(?:/|$)', path))
    except Exception:
        return False


def _extract_ad_path(url: str) -> str:
    """Extract the ad-related path prefix for a first-party rule."""
    try:
        path = urlparse(url).path
        m = re.search(r'(/(?:ads?|adserver|adtech|advert|sponsor|promo)/)', path, re.IGNORECASE)
        if m:
            return m.group(1) + "*"
        # Fall back: return up to 3rd path segment
        parts = path.strip("/").split("/")
        if parts and any(kw in parts[0].lower() for kw in ("ad", "ads", "adserver")):
            return "/" + parts[0] + "/*"
    except Exception:
        pass
    return ""


# ═══════════════════════════════════════════════════════════════
# NetworkRuleMiner — standalone network-log analysis for rules
# ═══════════════════════════════════════════════════════════════

class NetworkRuleMiner:
    """
    Mines the captured NetworkLog for ad / tracker requests and produces
    ABP-compatible network blocking rules *independently* of the DOM-level
    ad detection pipeline.

    This is how EasyList maintainers work: observe network traffic, identify
    ad-serving domains / URL patterns, and write rules to block those
    requests before they execute.

    The miner produces three categories of rules:

      1. **Ad-domain rules**  — ``||ad-domain.com^`` for every third-party
         request to a known ad network (trackers, bid endpoints, pixels).

      2. **Loader-script rules** — ``||domain.com/path/loader.js$script``
         for well-known ad loader URLs that bootstrap the entire ad pipeline.
         Blocking the loader prevents all downstream ad requests.

      3. **First-party ad-path rules** — ``||site.com/ads/*`` for same-site
         requests whose URL path betrays ad content.

    All rules are annotated with ABP type options ($script, $image, …) when
    the resource type is available from the Playwright network log.
    """

    def __init__(self, page_domain: str):
        self.page_domain = page_domain

    def mine(
        self,
        network_requests: List[Dict[str, Any]],
    ) -> List[FilterRule]:
        """Analyse captured requests and return deduplicated network rules."""
        rules: List[FilterRule] = []
        seen_syntax: Set[str] = set()

        def _add(rule: FilterRule):
            if rule.syntax not in seen_syntax:
                seen_syntax.add(rule.syntax)
                rules.append(rule)

        for req in network_requests:
            url = req.get("url", "")
            resource_type = req.get("resource_type", "other")
            if not url or not url.startswith("http"):
                continue

            try:
                req_domain = extract_domain(url)
            except Exception:
                continue

            third_party = not is_same_site(url, self.page_domain)
            type_option = RESOURCE_TYPE_MAP.get(resource_type, "")

            # ── Category 1: Known ad-loader scripts ───────────
            if _AD_LOADER_RE.search(url):
                parsed = urlparse(url)
                path = parsed.path.rstrip("/")
                # Strip query params for a stable rule
                syntax = f"||{req_domain}{path}$script" if path else f"||{req_domain}^$script"
                _add(FilterRule(
                    syntax=syntax,
                    rule_type="network",
                    target_domain=req_domain,
                    reasoning=f"Ad loader script: {req_domain}{path}",
                    violation_category="tracker",
                ))
                continue   # Don't double-count as a generic ad-domain rule

            # ── Category 2: Third-party known ad domains ──────
            if third_party:
                is_known = any(ad_dom in req_domain for ad_dom in AD_NETWORK_DOMAINS)
                if is_known:
                    # Domain-level block with optional type
                    syntax = f"||{req_domain}^"
                    if type_option:
                        syntax = f"||{req_domain}^{type_option}"
                    _add(FilterRule(
                        syntax=syntax,
                        rule_type="network",
                        target_domain=req_domain,
                        reasoning=f"Known ad/tracker domain: {req_domain} ({resource_type})",
                        violation_category="tracker",
                    ))
                    continue

                # Not in our known list, but URL contains ad keywords
                if is_ad_url(url):
                    syntax = f"||{req_domain}^$third-party,domain={self.page_domain}"
                    if type_option:
                        syntax = f"||{req_domain}^{type_option},third-party,domain={self.page_domain}"
                    _add(FilterRule(
                        syntax=syntax,
                        rule_type="network",
                        target_domain=req_domain,
                        reasoning=f"Third-party ad URL pattern: {req_domain} ({resource_type})",
                        violation_category="tracker",
                    ))
                    continue

            # ── Category 3: First-party ad paths ──────────────
            if not third_party and _has_ad_path(url):
                path = _extract_ad_path(url)
                if path:
                    syntax = f"||{req_domain}{path}"
                    if type_option:
                        syntax = f"||{req_domain}{path}{type_option}"
                    _add(FilterRule(
                        syntax=syntax,
                        rule_type="network",
                        target_domain=req_domain,
                        reasoning=f"First-party ad path: {req_domain}{path}",
                        violation_category="tracker",
                    ))

        return rules


def format_rules_file(rules: List[FilterRule], url: str = "") -> str:
    """Format rules into a complete EasyList-compatible filter file."""
    from datetime import datetime

    lines = [
        "[Adblock Plus 2.0]",
        f"! Title: AdLens Auto-Generated Rules",
        f"! Last modified: {datetime.now().isoformat()}",
    ]
    if url:
        lines.append(f"! Source URL: {url}")
    lines.append(f"! Total rules: {len(rules)}")
    lines.append("!")

    # Group by rule type
    network_rules = [r for r in rules if r.rule_type == "network" and r.violation_category != "tracker"]
    mined_network_rules = [r for r in rules if r.rule_type == "network" and r.violation_category == "tracker"]
    cosmetic_rules = [r for r in rules if r.rule_type == "cosmetic"]
    procedural_rules = [r for r in rules if r.rule_type == "procedural"]
    exception_rules = [r for r in rules if r.rule_type == "exception"]

    if network_rules:
        lines.append("! --- Network blocking rules (ad-element based) ---")
        for r in network_rules:
            lines.append(f"! {r.reasoning}")
            lines.append(r.syntax)

    if mined_network_rules:
        lines.append("! --- Network blocking rules (mined from traffic log) ---")
        for r in mined_network_rules:
            lines.append(f"! {r.reasoning}")
            lines.append(r.syntax)

    if cosmetic_rules:
        lines.append("! --- Element hiding rules ---")
        for r in cosmetic_rules:
            lines.append(f"! {r.reasoning}")
            lines.append(r.syntax)

    if procedural_rules:
        lines.append("! --- Procedural/extended rules ---")
        for r in procedural_rules:
            lines.append(f"! {r.reasoning}")
            lines.append(r.syntax)

    if exception_rules:
        lines.append("! --- Exception rules ---")
        for r in exception_rules:
            lines.append(f"! {r.reasoning}")
            lines.append(r.syntax)

    return "\n".join(lines) + "\n"
