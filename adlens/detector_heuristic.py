"""
detector_heuristic.py — Heuristic-based ad detection engine.
Uses DOM analysis, CSS selectors, keyword matching, iframe analysis,
provenance tracking, network correlation, HTML attribute scanning,
and a scoring system.
"""

import os
import re
from typing import List, Dict, Any, Set, Optional
from urllib.parse import urlparse

from playwright.sync_api import Page, ElementHandle

from .config import (
    AD_CSS_SELECTORS,
    AD_TEXT_KEYWORDS,
    AD_NETWORK_DOMAINS,
    AD_URL_KEYWORDS,
    AD_ATTRIBUTE_NAME_PATTERNS,
    AD_ATTRIBUTE_VALUE_PATTERNS,
    AD_COMMENT_KEYWORDS,
    AD_DIMENSION_CLASS_PATTERN,
    MIN_ELEMENT_WIDTH,
    MIN_ELEMENT_HEIGHT,
    TRACKING_PIXEL_MAX_WIDTH,
    TRACKING_PIXEL_MAX_HEIGHT,
    HEURISTIC_AD_SCORE_THRESHOLD,
    ELEMENT_SCREENSHOT_TIMEOUT_MS,
    MAX_ADS_PER_PAGE,
    NEWSLETTER_POPUP_KEYWORDS,
)
from .models import AdCandidate, BoundingRect
from .utils import extract_domain, is_third_party, is_same_site, is_ad_url, rects_overlap


class HeuristicDetector:
    """
    Detects ads using multiple heuristic signals without requiring an LLM.
    Each strategy adds a score; elements above the threshold are classified as ads.
    """

    def __init__(self, page: Page, session_dir: str, network_log=None):
        self.page = page
        self.session_dir = session_dir
        self.ads_dir = os.path.join(session_dir, "ads")
        self.network_log = network_log
        self.page_domain = extract_domain(page.url)
        self._seen_positions: Set[str] = set()
        self._candidate_boxes: List[dict] = []  # for IoU overlap dedup
        self._candidate_scores: List[float] = []

    def detect(self) -> List[AdCandidate]:
        """Run all detection strategies and return deduplicated ad candidates."""
        print("  [Heuristic] Running ad detection...")
        candidates: List[AdCandidate] = []
        index = [0]   # mutable counter for closures
        strategy_name = [""]  # tracks which strategy is currently running

        def add_candidate(handle: ElementHandle, reason: str, score: float,
                          provenance: str = "unknown") -> Optional[AdCandidate]:
            """Validate and add a candidate, deduplicating by position."""
            try:
                if not handle.is_visible():
                    return None
                box = handle.bounding_box()
                if not box:
                    return None
                if box["width"] < MIN_ELEMENT_WIDTH or box["height"] < MIN_ELEMENT_HEIGHT:
                    if not self._is_tracking_pixel_candidate(handle, box):
                        return None
                    # Promote tracking pixels so they are not dropped by thresholding.
                    reason = f"Tracking Pixel Candidate: {reason}"
                    score = max(score, 7.0)

                # Hard cap — stop adding after MAX_ADS_PER_PAGE candidates
                if len([c for c in candidates if c is not None]) >= MAX_ADS_PER_PAGE:
                    return None

                # Convert viewport-relative Y to absolute page Y.
                # bounding_box() returns getBoundingClientRect() coords which
                # shift as the page scrolls. We add window.scrollY to get the
                # absolute position on the page, ensuring is_above_fold is
                # correct regardless of current scroll position.
                try:
                    scroll_y = self.page.evaluate("() => window.scrollY || window.pageYOffset || 0")
                except Exception:
                    scroll_y = 0
                abs_y = box["y"] + scroll_y

                # Skip truly off-screen elements (above page top)
                if abs_y < -50:
                    return None

                # Deduplicate by exact position signature (use absolute Y)
                pos_key = f"{int(box['x'])}_{int(abs_y)}_{int(box['width'])}_{int(box['height'])}"
                if pos_key in self._seen_positions:
                    return None

                # Fix 1: IoU-based overlap dedup — if new element overlaps >=60%
                # with an existing candidate, keep only the higher-scoring one
                overlap_idx = None
                for i, existing_box in enumerate(self._candidate_boxes):
                    if rects_overlap(box, existing_box, threshold=0.60):
                        overlap_idx = i
                        break
                if overlap_idx is not None:
                    if score <= self._candidate_scores[overlap_idx]:
                        return None  # existing is better, skip
                    else:
                        # Remove the weaker existing candidate
                        old = candidates[overlap_idx]
                        candidates[overlap_idx] = None  # mark for removal
                        self._candidate_boxes[overlap_idx] = {"x": -9999, "y": -9999, "width": 0, "height": 0}
                        self._candidate_scores[overlap_idx] = -1

                self._seen_positions.add(pos_key)
                self._candidate_boxes.append(box)
                self._candidate_scores.append(score)

                # Extract element context
                ctx = self._extract_context(handle)

                viewport = self.page.viewport_size or {"width": 1366, "height": 768}

                candidate = AdCandidate(
                    index=index[0],
                    tag_name=ctx.get("tagName", "UNKNOWN"),
                    element_id=ctx.get("id", ""),
                    classes=ctx.get("classes", []),
                    rect=BoundingRect(
                        x=box["x"], y=abs_y,
                        width=box["width"], height=box["height"],
                    ),
                    src=ctx.get("src", "Unknown"),
                    provenance=provenance,
                    detection_reason=reason,
                    detection_score=score,
                    detection_strategies=[strategy_name[0]] if strategy_name[0] else [],
                    text_snippet=ctx.get("text_snippet", ""),
                    styles=ctx.get("styles", {}),
                    data_attributes=ctx.get("data_attributes", {}),
                    has_ad_marker=ctx.get("has_ad_marker", False),
                    outer_html=ctx.get("outer_html", ""),
                    viewport_height=viewport["height"],
                    viewport_width=viewport["width"],
                    is_above_fold=abs_y < viewport["height"],
                    is_sticky=ctx.get("styles", {}).get("position") in ("fixed", "sticky"),
                )

                # Filter: skip newsletter/signup popups
                if self._is_newsletter_popup(candidate.text_snippet):
                    return None

                # Filter: skip empty containers with no visible content
                if self._is_empty_container(handle):
                    return None

                # Take screenshot
                shot_path = os.path.join(self.ads_dir, f"ad_{index[0]:03d}.png")
                try:
                    if handle.evaluate("el => el.isConnected"):
                        handle.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
                        handle.evaluate("el => el.style.outline = '3px solid red'")
                        handle.screenshot(path=shot_path, timeout=ELEMENT_SCREENSHOT_TIMEOUT_MS)
                        handle.evaluate("el => el.style.outline = ''")
                        candidate.screenshot_path = shot_path
                except Exception:
                    pass

                # Mark as ad based on score
                candidate.is_ad = score >= HEURISTIC_AD_SCORE_THRESHOLD
                candidate.confidence = min(score / 10.0, 1.0)
                candidate.ad_type = self._classify_ad_type(candidate)

                candidates.append(candidate)
                index[0] += 1
                return candidate
            except Exception:
                return None

        # === Strategy A: CSS Selector matching ===
        strategy_name[0] = "css_selector"
        self._detect_by_selectors(add_candidate)

        # === Strategy B: Keyword matching (text-based) ===
        strategy_name[0] = "keyword"
        self._detect_by_keywords(add_candidate)

        # === Strategy C: Cross-origin iframes ===
        strategy_name[0] = "iframe"
        self._detect_by_iframes(add_candidate)

        # === Strategy D: Provenance tracking (third-party injected elements) ===
        strategy_name[0] = "provenance"
        self._detect_by_provenance(add_candidate)

        # === Strategy E: Sticky / Fixed position elements ===
        strategy_name[0] = "sticky"
        self._detect_by_sticky(add_candidate)

        # === Strategy F: Network correlation (DOM elements matching ad requests) ===
        strategy_name[0] = "network"
        self._detect_by_network(add_candidate)

        # === Strategy G: HTML attribute scanning (liberal DOM-level signals) ===
        strategy_name[0] = "html_attribute"
        self._detect_by_html_attributes(add_candidate)

        # Filter to only confirmed ads (and remove None placeholders from overlap dedup)
        confirmed = [c for c in candidates if c is not None and c.is_ad]
        print(f"  [Heuristic] Found {len([c for c in candidates if c is not None])} candidates, {len(confirmed)} confirmed as ads")
        return confirmed

    # ─── False-Positive Filters ──────────────────────────────

    def _is_newsletter_popup(self, text_snippet: str) -> bool:
        """Check if text indicates a newsletter/signup popup, not an ad."""
        if not text_snippet:
            return False
        text_lower = text_snippet.lower()
        matches = sum(1 for kw in NEWSLETTER_POPUP_KEYWORDS if kw in text_lower)
        return matches >= 2

    def _is_empty_container(self, handle: ElementHandle) -> bool:
        """Check if element is an empty container with no visible ad content.
        
        Note: If the element itself is an IFRAME, it is never empty — the ad
        content lives inside the iframe's own document, not as child DOM nodes.
        """
        try:
            info = handle.evaluate("""el => {
                // An iframe element IS the ad content container — never treat
                // it as empty. Its content lives in a separate browsing context,
                // so querySelectorAll on the iframe element won't find anything.
                if (el.tagName === 'IFRAME') {
                    return { tagName: 'IFRAME', textLen: 0, imgCount: 0, iframeCount: 1 };
                }
                const text = (el.innerText || '').trim();
                const imgs = el.querySelectorAll('img, video, canvas, svg');
                const iframes = el.querySelectorAll('iframe');
                return {
                    tagName: el.tagName,
                    textLen: text.length,
                    imgCount: imgs.length,
                    iframeCount: iframes.length
                };
            }""")
            # Empty if no text, no images, and no iframes
            return (info["textLen"] < 3 and
                    info["imgCount"] == 0 and
                    info["iframeCount"] == 0)
        except Exception:
            return False

    # ─── Detection Strategies ───────────────────────────────

    def _detect_by_selectors(self, add_fn):
        """Strategy A: Query known ad CSS selectors."""
        for selector in AD_CSS_SELECTORS:
            try:
                elements = self.page.query_selector_all(selector)
                for el in elements:
                    add_fn(el, f"CSS Selector: {selector}", 5.0)
            except Exception:
                continue

    def _detect_by_keywords(self, add_fn):
        """Strategy B: Find elements containing ad keywords and traverse to parent."""
        for keyword in AD_TEXT_KEYWORDS:
            try:
                locators = self.page.get_by_text(keyword, exact=True).all()
                for loc in locators:
                    try:
                        # Traverse up to a container element (2 levels up)
                        parent = loc.locator("..").locator("..").first
                        handle = parent.element_handle(timeout=1000)
                        if handle:
                            add_fn(handle, f"Keyword: {keyword}", 3.0)
                    except Exception:
                        continue
            except Exception:
                continue

    def _detect_by_iframes(self, add_fn):
        """Strategy C: Find cross-origin iframes from ad networks."""
        for frame in self.page.frames:
            if frame == self.page.main_frame:
                continue
            frame_url = frame.url or ""
            if not frame_url or frame_url == "about:blank":
                continue

            try:
                frame_domain = extract_domain(frame_url)
                is_ad_domain = any(ad in frame_domain for ad in AD_NETWORK_DOMAINS)
                is_third = is_third_party(frame_url, self.page_domain)

                if is_ad_domain or is_third:
                    score = 7.0 if is_ad_domain else 4.0
                    try:
                        el = frame.frame_element()
                        add_fn(el, f"Cross-Origin Iframe: {frame_domain}", score, frame_url)
                    except Exception:
                        pass
            except Exception:
                continue

    def _detect_by_provenance(self, add_fn):
        """Strategy D: Elements tagged by the provenance tracker as third-party injected."""
        try:
            elements = self.page.query_selector_all("[data-provenance-source]")
            for el in elements:
                try:
                    src = el.get_attribute("data-provenance-source") or ""
                    if src and src != "unknown" and is_third_party(src, self.page_domain):
                        is_ad_src = any(ad in src.lower() for ad in AD_URL_KEYWORDS)
                        score = 6.0 if is_ad_src else 3.0
                        add_fn(el, f"Third-Party Provenance: {src[:80]}", score, src)
                except Exception:
                    continue
        except Exception:
            pass

    def _detect_by_sticky(self, add_fn):
        """Strategy E: Fixed/sticky positioned elements (potential sticky ads)."""
        try:
            elements = self.page.query_selector_all(
                'div[style*="position: fixed"], div[style*="position:fixed"], '
                'div[style*="position: sticky"], div[style*="position:sticky"]'
            )
            for el in elements:
                try:
                    box = el.bounding_box()
                    if box and box["height"] > 40:
                        add_fn(el, "Sticky/Fixed Element", 2.0)
                except Exception:
                    continue

            # Also check computed styles
            sticky_handles = self.page.evaluate_handle("""
                () => {
                    const results = [];
                    document.querySelectorAll('div, aside, section').forEach(el => {
                        const style = window.getComputedStyle(el);
                        if (style.position === 'fixed' || style.position === 'sticky') {
                            results.push(el);
                        }
                    });
                    return results;
                }
            """)
            try:
                length = sticky_handles.evaluate("arr => arr.length")
                for i in range(min(length, 20)):
                    try:
                        el = sticky_handles.evaluate_handle(f"arr => arr[{i}]")
                        add_fn(el.as_element(), "Computed Sticky/Fixed", 2.0)
                    except Exception:
                        continue
            except Exception:
                pass
        except Exception:
            pass

    def _detect_by_network(self, add_fn):
        """Strategy F: Match known ad-network URL patterns in captured requests."""
        if not self.network_log:
            return

        ad_request_domains = set()
        for req in self.network_log.requests:
            url = req.get("url", "")
            # Fix 2: Use word-boundary matching instead of substring 'in'
            if is_ad_url(url):
                try:
                    domain = extract_domain(url)
                    # Fix 2: Exclude same-site domains (first-party content)
                    if not is_same_site(url, self.page_domain):
                        ad_request_domains.add(domain)
                except Exception:
                    pass

        # Find DOM elements whose src matches captured ad requests
        if ad_request_domains:
            try:
                elements = self.page.query_selector_all("iframe[src], img[src], script[src]")
                for el in elements:
                    try:
                        src = el.get_attribute("src") or ""
                        if src:
                            # Fix 2: Also skip first-party element sources
                            if is_same_site(src, self.page_domain):
                                continue
                            src_domain = extract_domain(src)
                            if src_domain in ad_request_domains:
                                add_fn(el, f"Network Match: {src_domain}", 5.0, src)
                    except Exception:
                        continue
            except Exception:
                pass

    def _detect_by_html_attributes(self, add_fn):
        """Strategy G: Liberal HTML attribute and comment scanning.

        Goes beyond CSS selectors by inspecting ALL attributes of every element
        in the DOM for ad-related patterns. Also scans HTML comments for ad
        markers (e.g. <!-- marker for pmc-sticky-ad -->) and targets adjacent
        elements. Detects class-encoded IAB ad dimensions (e.g. adw-728 adh-90).

        This runs a single JavaScript function in the browser context for
        efficiency, returning matched element indices back to Python.
        """
        # Build the JS-side regex patterns from config
        attr_name_patterns = '|'.join(AD_ATTRIBUTE_NAME_PATTERNS)
        attr_value_patterns = '|'.join(AD_ATTRIBUTE_VALUE_PATTERNS)
        dim_pattern = AD_DIMENSION_CLASS_PATTERN

        try:
            # Run a single JS function that returns matched elements with reasons
            results_handle = self.page.evaluate_handle("""
                (config) => {
                    const attrNameRe = new RegExp(config.attrNamePatterns, 'i');
                    const attrValueRe = new RegExp(config.attrValuePatterns, 'i');
                    const dimRe = new RegExp(config.dimPattern, 'i');
                    const commentKeywords = config.commentKeywords;

                    const results = [];
                    const seen = new Set();

                    function addResult(el, reason, score) {
                        if (!el || el.nodeType !== 1) return;
                        if (seen.has(el)) return;
                        seen.add(el);
                        results.push({ element: el, reason: reason, score: score });
                    }

                    // --- Sub-scanner 1: Attribute name/value scanning ---
                    // Walk all elements and check every attribute.
                    const allElements = document.querySelectorAll('*');
                    for (const el of allElements) {
                        if (!el.attributes || el.attributes.length === 0) continue;
                        let attrScore = 0;
                        let matchedAttr = '';

                        for (const attr of el.attributes) {
                            // Check attribute name
                            if (attrNameRe.test(attr.name)) {
                                attrScore += 5.0;
                                matchedAttr = attr.name + '=' + (attr.value || '').substring(0, 50);
                                break;  // One strong name match is enough
                            }
                            // Check attribute value (skip very long values like inline styles)
                            if (attr.value && attr.value.length < 200 && attrValueRe.test(attr.value)) {
                                // Skip if this is a common non-ad attribute
                                if (attr.name === 'href' || attr.name === 'src') continue;
                                attrScore += 3.0;
                                matchedAttr = attr.name + '=' + attr.value.substring(0, 50);
                            }
                        }

                        // --- Sub-scanner 3: Class-encoded IAB dimensions ---
                        const className = el.className;
                        if (typeof className === 'string' && dimRe.test(className)) {
                            attrScore += 4.0;
                            matchedAttr = matchedAttr || 'class=' + className.substring(0, 60);
                        }

                        if (attrScore > 0) {
                            addResult(el, 'HTML Attr: ' + matchedAttr, attrScore);
                        }
                    }

                    // --- Sub-scanner 2: HTML comment scanning ---
                    // Walk all nodes using TreeWalker to find comment nodes.
                    const walker = document.createTreeWalker(
                        document.body || document.documentElement,
                        NodeFilter.SHOW_COMMENT,
                        null,
                        false
                    );
                    let comment;
                    while ((comment = walker.nextNode())) {
                        const text = comment.textContent.toLowerCase();
                        // Check if comment contains any ad keyword using word boundaries
                        const hasAdKeyword = commentKeywords.some(kw => {
                            // Use word-boundary-like matching: keyword surrounded by
                            // non-alphanumeric chars (or start/end of string)
                            const escaped = kw.replace(/[-]/g, '[-]');
                            const re = new RegExp('(?:^|[^a-z])' + escaped + '(?:$|[^a-z])', 'i');
                            return re.test(text);
                        });
                        if (hasAdKeyword) {
                            // Target the next sibling element after this comment
                            let sibling = comment.nextSibling;
                            while (sibling && sibling.nodeType !== 1) {
                                sibling = sibling.nextSibling;
                            }
                            if (sibling) {
                                addResult(sibling, 'HTML Comment: ' + text.trim().substring(0, 80), 5.0);
                            }
                            // Also check parent element as a fallback
                            if (comment.parentElement) {
                                addResult(comment.parentElement, 'HTML Comment Parent: ' + text.trim().substring(0, 80), 4.0);
                            }
                        }
                    }

                    return results;
                }
            """, {
                "attrNamePatterns": attr_name_patterns,
                "attrValuePatterns": attr_value_patterns,
                "dimPattern": dim_pattern,
                "commentKeywords": AD_COMMENT_KEYWORDS,
            })

            # Iterate over JS results and feed each matched element into add_fn
            try:
                length = results_handle.evaluate("arr => arr.length")
                for i in range(min(length, 30)):  # Cap at 30 to avoid runaway
                    try:
                        reason = results_handle.evaluate(f"arr => arr[{i}].reason")
                        score = results_handle.evaluate(f"arr => arr[{i}].score")
                        el_handle = results_handle.evaluate_handle(f"arr => arr[{i}].element")
                        element = el_handle.as_element()
                        if element:
                            add_fn(element, reason, score)
                    except Exception:
                        continue
            except Exception:
                pass
        except Exception as e:
            print(f"  [Heuristic] Strategy G error: {e}")

    # ─── Helpers ────────────────────────────────────────────

    def _extract_context(self, handle: ElementHandle) -> Dict[str, Any]:
        """Extract rich context from a DOM element."""
        try:
            return handle.evaluate("""(el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);

                // Text extraction (element + siblings)
                const text = (el.innerText || '').substring(0, 200);

                // Ad marker detection
                const hasAdMarker = (
                    !!el.querySelector('.g-ads-ad-badge, .ap-x, .abg-icon, .ad-marker, .sponsored-label') ||
                    el.matches('[aria-label*="Ad"], [aria-label*="Sponsored"]') ||
                    !!el.querySelector('[aria-label*="Ad"], [aria-label*="Sponsored"], [aria-label="AdChoices"]') ||
                    !!el.querySelector('a[href*="adchoices"], a[href*="google_ads"]')
                );

                // Data attributes
                const dataAttrs = {};
                if (el.attributes) {
                    for (const a of el.attributes) {
                        if (a.name && a.name.startsWith('data-')) {
                            dataAttrs[a.name] = a.value;
                        }
                    }
                }

                // Outer HTML extraction (truncated for efficiency)
                const outerHtml = (el.outerHTML || '').substring(0, 2000);

                return {
                    tagName: el.tagName,
                    id: el.id || '',
                    classes: Array.from(el.classList || []),
                    src: el.src || el.querySelector('iframe')?.src || el.getAttribute('data-src') || 'Unknown',
                    text_snippet: text,
                    has_ad_marker: hasAdMarker,
                    data_attributes: dataAttrs,
                    outer_html: outerHtml,
                    styles: {
                        position: style.position,
                        zIndex: style.zIndex,
                        display: style.display,
                        overflow: style.overflow,
                    },
                };
            }""")
        except Exception:
            return {"tagName": "UNKNOWN", "id": "", "classes": [], "styles": {}}

    def _classify_ad_type(self, candidate: AdCandidate) -> str:
        """Classify the ad type based on its properties."""
        if self._is_tracking_pixel_from_candidate(candidate):
            return "Tracking Pixel"

        if candidate.is_sticky:
            return "Sticky"
        if candidate.tag_name == "IFRAME":
            return "Banner"
        if candidate.tag_name == "VIDEO":
            return "Video"

        # Check dimensions against known types
        if candidate.rect:
            w, h = candidate.rect.width, candidate.rect.height
            if w >= 900 and h <= 120:
                return "Leaderboard"
            if w <= 320 and h <= 100:
                return "Mobile Banner"
            if 290 <= w <= 340 and 240 <= h <= 260:
                return "Medium Rectangle"
            if candidate.occupancy_pct > 50:
                return "Interstitial"

        # Check text/class for clues
        text_lower = candidate.text_snippet.lower()
        classes_str = " ".join(candidate.classes).lower()
        if "native" in classes_str or "native" in text_lower:
            return "Native"
        if "sponsor" in text_lower:
            return "Sponsored Content"

        return "Banner"

    def _is_tracking_pixel_candidate(self, handle: ElementHandle, box: Dict[str, float]) -> bool:
        """Allow only strict tiny tracker beacons (1x1/2x2) through size gating."""
        if box["width"] > TRACKING_PIXEL_MAX_WIDTH or box["height"] > TRACKING_PIXEL_MAX_HEIGHT:
            return False

        try:
            info = handle.evaluate("""el => {
                const src = el.currentSrc || el.src || el.getAttribute('src') || el.getAttribute('data-src') || '';
                const dataProv = el.getAttribute('data-provenance-source') || '';
                return {
                    tagName: el.tagName || '',
                    src,
                    dataProv,
                };
            }""")
        except Exception:
            return False

        tag_name = (info.get("tagName") or "").upper()
        src = (info.get("src") or "").strip()
        provenance = (info.get("dataProv") or "").strip()

        if tag_name not in {"IMG", "IFRAME", "SCRIPT"}:
            return False

        signal_url = src or provenance
        if not signal_url:
            return False

        url_lower = signal_url.lower()
        if any(token in url_lower for token in ("pixel", "beacon", "track", "tracking", "collect", "open")):
            return True

        if is_ad_url(signal_url):
            return True

        try:
            return is_third_party(signal_url, self.page_domain)
        except Exception:
            return False

    def _is_tracking_pixel_from_candidate(self, candidate: AdCandidate) -> bool:
        """Classify tiny ad/tracker resources as tracking pixels for compliance."""
        if not candidate.rect:
            return False

        if candidate.rect.width > TRACKING_PIXEL_MAX_WIDTH or candidate.rect.height > TRACKING_PIXEL_MAX_HEIGHT:
            return False

        signal_url = ""
        if candidate.src and candidate.src != "Unknown":
            signal_url = candidate.src
        elif candidate.provenance and candidate.provenance != "unknown":
            signal_url = candidate.provenance

        if not signal_url:
            return False

        url_lower = signal_url.lower()
        if any(token in url_lower for token in ("pixel", "beacon", "track", "tracking", "collect", "open")):
            return True

        if is_ad_url(signal_url):
            return True

        try:
            return is_third_party(signal_url, self.page_domain)
        except Exception:
            return False
