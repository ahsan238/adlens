"""
detector_llm.py — LLM-powered ad detection using OpenAI vision models.
Sends element screenshots + DOM context to the LLM for classification.
Falls back to heuristic mode on errors.
"""

import os
import json
from typing import List, Dict, Any, Optional

from playwright.sync_api import Page, ElementHandle
from openai import OpenAI

from .prefilter import filter_ad_images

from .config import (
    OPENAI_MODEL,
    OPENAI_MAX_TOKENS,
    MIN_ELEMENT_WIDTH,
    MIN_ELEMENT_HEIGHT,
    ELEMENT_SCREENSHOT_TIMEOUT_MS,
    AD_CSS_SELECTORS,
    AD_TEXT_KEYWORDS,
    AD_NETWORK_DOMAINS,
    MAX_ADS_PER_PAGE,
    MAX_ADS_PER_PAGE,
    NEWSLETTER_POPUP_KEYWORDS,
    INPUT_COST_PER_1M,
    OUTPUT_COST_PER_1M,
)
from .models import AdCandidate, BoundingRect
from .utils import extract_domain, is_third_party, encode_image_base64
from .detector_heuristic import HeuristicDetector


# ─── LLM System Prompt ─────────────────────────────────────

LLM_DETECTION_PROMPT = """
# ROLE
You are an expert Ad Detection Agent. You analyze web page elements to determine
if they are advertisements.

# TASK
Given a screenshot of a web element, its DOM metadata, and its raw HTML, determine:
1. Is this element an advertisement?
2. What type of ad is it?
3. How confident are you?

# SIGNALS TO LOOK FOR
- AdChoices icons, "Sponsored" labels, "Ad" labels, "Advertisement" text
- Ad network iframes (doubleclick, googlesyndication, taboola, outbrain, etc.)
- Common ad dimensions (300×250, 728×90, 160×600, etc.)
- Tracking pixels / beacons (often 1×1 or 2×2, usually IMG/IFRAME with tracking URLs)
- Elements with ad-related CSS classes or IDs
- Third-party content served from ad networks
- Banner-like visual appearance
- **HTML structure clues**: data attributes mentioning ads (data-ad-*, data-google-query-id,
  data-is-adhesion-ad), class names encoding ad dimensions (adw-728, adh-90),
  HTML comments marking ad sections, ad-management prefixes in IDs (adm-, gpt-ad-)

# IMPORTANT: NOT ADS
The following are NOT advertisements and must be classified as is_ad=false:
- Newsletter signup forms or email subscription popups 
- The website's own editorial promotions (e.g., "Play fantasy baseball", "Watch live", "Download our app")
- Site navigation, menus, headers, footers
- Content recommendation widgets that promote the same site's own articles
- Cookie consent banners
- Login/registration prompts

# INPUT
You receive JSON metadata with fields:
- tagName, id, classes, rect (dimensions), src (source URL)
- provenance (creator script URL)
- styles (CSS position, z-index)
- text_snippet (visible text)
- has_ad_marker (boolean — indicates AdChoices/AdSense markers found)

You also receive the element's raw HTML (may be truncated). Use this to identify
ad signals that may not be visible in the screenshot, such as data attributes,
class naming patterns, iframe sources, or structural clues.

# OUTPUT FORMAT (strict JSON)
{
    "is_ad": boolean,
    "confidence": float (0.0 to 1.0),
    "ad_type": "Banner | Leaderboard | Medium Rectangle | Sticky | Interstitial | Video | Native | Sponsored Content | Tracking Pixel | Unknown",
    "reasoning": "Brief explanation of your decision"
}
"""


class LLMDetector:
    """
    Detects ads using OpenAI vision model analysis.
    Uses the heuristic detector to gather candidates, then sends each to the LLM.
    """

    def __init__(self, page: Page, session_dir: str, api_key: str, network_log=None):
        self.page = page
        self.session_dir = session_dir
        self.ads_dir = os.path.join(session_dir, "ads")
        self.network_log = network_log
        self.page_domain = extract_domain(page.url)
        self.client = OpenAI(api_key=api_key)

        # Use heuristic detector to gather candidates first
        self.heuristic = HeuristicDetector(page, session_dir, network_log)

        # Cost tracking
        self.total_cost = 0.0
        self.total_tokens = {"prompt": 0, "completion": 0}

    def detect(self) -> List[AdCandidate]:
        """
        Gather candidates using heuristics, then verify each with the LLM.
        The LLM can promote low-score candidates and demote false positives.
        """
        print("  [LLM] Running LLM-powered ad detection...")

        # Step 1: Gather all candidates (lower threshold for LLM verification)
        all_candidates = self._gather_all_candidates()
        print(f"  [LLM] Gathered {len(all_candidates)} candidates for LLM verification")

        confirmed: List[AdCandidate] = []

        # Step 1.5: Pre-filter images (Duplicate/Blank Detection)
        candidate_paths = [c.screenshot_path for c in all_candidates if c.screenshot_path and os.path.exists(c.screenshot_path)]
        kept_paths, skipped_reasons = filter_ad_images(candidate_paths)
        print(f"  [Filter] Kept {len(kept_paths)}/{len(candidate_paths)} images. Skipped {len(skipped_reasons)}.")

        for candidate in all_candidates:
            if not candidate.screenshot_path or not os.path.exists(candidate.screenshot_path):
                continue
            
            # Check if this candidates image was skipped by the filter
            if candidate.screenshot_path in skipped_reasons:
                reason = skipped_reasons[candidate.screenshot_path]
                print(f"    [Filter] Skipping candidate (Reason: {reason})")
                continue

            # Step 2: Send to LLM for verification
            llm_result = self._llm_verify(candidate)
            if llm_result:
                candidate.is_ad = llm_result.get("is_ad", False)
                candidate.confidence = llm_result.get("confidence", 0.0)
                candidate.ad_type = llm_result.get("ad_type", "Unknown")
                candidate.usage = llm_result.get("usage", {})

                # Tiny tracker beacons are easy for vision models to miss; keep
                # strict heuristic tracking-pixel classification authoritative.
                if self.heuristic._is_tracking_pixel_from_candidate(candidate):
                    candidate.is_ad = True
                    candidate.ad_type = "Tracking Pixel"
                    candidate.confidence = max(candidate.confidence, 0.80)

                # Accumulate costs
                if candidate.usage:
                    self.total_tokens["prompt"] += candidate.usage.get("prompt_tokens", 0)
                    self.total_tokens["completion"] += candidate.usage.get("completion_tokens", 0)
                    self.total_cost += candidate.usage.get("cost_usd", 0.0)

                if candidate.is_ad:
                    confirmed.append(candidate)
                    print(f"    [LLM] ✓ Ad confirmed: {candidate.ad_type} "
                          f"(confidence: {candidate.confidence:.0%})")
                else:
                    print(f"    [LLM] ✗ Not an ad (confidence: {candidate.confidence:.0%})")
            else:
                # LLM failed — fall back to heuristic decision
                if candidate.detection_score >= 4.0:
                    candidate.is_ad = True
                    confirmed.append(candidate)

        print(f"  [LLM] Confirmed {len(confirmed)} ads after LLM verification")
        
        # Print Session Cost Summary
        print("\n  ==========================================")
        print("  [COST] Session Usage Summary")
        print(f"  [COST] Prompt Tokens:     {self.total_tokens['prompt']:,}")
        print(f"  [COST] Completion Tokens: {self.total_tokens['completion']:,}")
        print(f"  [COST] Total Estimated:   ${self.total_cost:.6f}")
        print("  ==========================================\n")
        
        return confirmed

    def _gather_all_candidates(self) -> List[AdCandidate]:
        """
        Gather candidates using a lower threshold than the standard heuristic
        to let the LLM make the final decision.
        """
        # Temporarily lower the threshold
        from . import config as cfg
        original_threshold = cfg.HEURISTIC_AD_SCORE_THRESHOLD
        cfg.HEURISTIC_AD_SCORE_THRESHOLD = 2  # Lower for LLM mode

        # Create a fresh heuristic detector with the lower threshold
        detector = HeuristicDetector(self.page, self.session_dir, self.network_log)
        # We want ALL candidates, not just confirmed ones
        candidates = []
        index_counter = [0]
        seen_positions = set()

        def add_candidate(handle, reason, score, provenance="unknown"):
            try:
                if not handle.is_visible():
                    return None
                box = handle.bounding_box()
                if not box:
                    return None
                if box["width"] < MIN_ELEMENT_WIDTH or box["height"] < MIN_ELEMENT_HEIGHT:
                    if not detector._is_tracking_pixel_candidate(handle, box):
                        return None
                    reason = f"Tracking Pixel Candidate: {reason}"
                    score = max(score, 7.0)

                # Convert viewport-relative Y to absolute page Y
                try:
                    scroll_y = self.page.evaluate("() => window.scrollY || window.pageYOffset || 0")
                except Exception:
                    scroll_y = 0
                abs_y = box["y"] + scroll_y

                pos_key = f"{int(box['x'])}_{int(abs_y)}_{int(box['width'])}_{int(box['height'])}"
                if pos_key in seen_positions:
                    return None
                seen_positions.add(pos_key)

                # Hard cap: stop after MAX_ADS_PER_PAGE candidates
                if len(candidates) >= MAX_ADS_PER_PAGE:
                    return None

                ctx = detector._extract_context(handle)
                viewport = self.page.viewport_size or {"width": 1366, "height": 768}

                candidate = AdCandidate(
                    index=index_counter[0],
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

                candidate.ad_type = detector._classify_ad_type(candidate)

                # Take screenshot
                shot_path = os.path.join(self.ads_dir, f"ad_{index_counter[0]:03d}.png")
                try:
                    if handle.evaluate("el => el.isConnected"):
                        handle.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
                        handle.evaluate("el => el.style.outline = '3px solid red'")
                        handle.screenshot(path=shot_path, timeout=ELEMENT_SCREENSHOT_TIMEOUT_MS)
                        handle.evaluate("el => el.style.outline = ''")
                        candidate.screenshot_path = shot_path
                except Exception:
                    pass

                # Filter: skip newsletter/signup popups
                if detector._is_newsletter_popup(candidate.text_snippet):
                    return None

                # Filter: skip empty containers
                if detector._is_empty_container(handle):
                    return None

                candidates.append(candidate)
                index_counter[0] += 1
                return candidate
            except Exception:
                return None

        # Run all strategies
        detector._detect_by_selectors(add_candidate)
        detector._detect_by_keywords(add_candidate)
        detector._detect_by_iframes(add_candidate)
        detector._detect_by_provenance(add_candidate)
        detector._detect_by_sticky(add_candidate)
        detector._detect_by_network(add_candidate)
        detector._detect_by_html_attributes(add_candidate)

        # Restore threshold
        cfg.HEURISTIC_AD_SCORE_THRESHOLD = original_threshold

        return candidates

    def _llm_verify(self, candidate: AdCandidate) -> Optional[Dict[str, Any]]:
        """Send a candidate to the configured OpenAI model for verification."""
        b64_image = encode_image_base64(candidate.screenshot_path)
        if not b64_image:
            return None

        # Build context payload
        context = {
            "tagName": candidate.tag_name,
            "id": candidate.element_id,
            "classes": candidate.classes,
            "rect": candidate.rect.to_dict() if candidate.rect else {},
            "src": candidate.src,
            "provenance": candidate.provenance,
            "styles": candidate.styles,
            "text_snippet": candidate.text_snippet,
            "has_ad_marker": candidate.has_ad_marker,
            "detection_reason": candidate.detection_reason,
            "height_pct": round(candidate.height_pct, 1),
            "is_above_fold": candidate.is_above_fold,
        }

        user_prompt = f"""Analyze this web element from {self.page.url}.

--- METADATA ---
{json.dumps(context, indent=2)}

--- RAW HTML (may be truncated) ---
{candidate.outer_html[:1500] if candidate.outer_html else '(not available)'}

Is this element an advertisement? Respond with the JSON format specified."""

        try:
            # GPT-5+ models use max_completion_tokens instead of max_tokens
            token_limit_param = {}
            if OPENAI_MODEL.startswith("gpt-5") or OPENAI_MODEL.startswith("gpt-6"):
                token_limit_param["max_completion_tokens"] = OPENAI_MAX_TOKENS
            else:
                token_limit_param["max_tokens"] = OPENAI_MAX_TOKENS

            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": LLM_DETECTION_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{b64_image}"
                        }},
                    ]},
                ],
                **token_limit_param,
                response_format={"type": "json_object"},
            )
            
            # Usage tracking
            usage = response.usage
            prompt_tokens = usage.prompt_tokens
            completion_tokens = usage.completion_tokens
            
            cost = (prompt_tokens / 1_000_000 * INPUT_COST_PER_1M) + \
                   (completion_tokens / 1_000_000 * OUTPUT_COST_PER_1M)

            result = json.loads(response.choices[0].message.content)
            
            # Inject usage data into result
            result["usage"] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost
            }
            
            return result
        except Exception as e:
            print(f"    [LLM] API error: {e}")
            return None
