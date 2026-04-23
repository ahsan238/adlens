"""
config.py — Global configuration and constants for the AdLens system.
"""

import os

# ─────────────────────────────────────────────
# API Keys — Hardcode here for convenience
# ─────────────────────────────────────────────
# Set OPENAI_API_KEY in your shell or pass --api-key at runtime.
# Keeping secrets out of source code prevents push protection violations.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
# ─────────────────────────────────────────────
# Directories
# ─────────────────────────────────────────────
BASE_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "adlens_results")

# ─────────────────────────────────────────────
# Viewport presets (Acceptable Ads common sizes)
# ─────────────────────────────────────────────
VIEWPORTS = {
    "desktop": {"width": 1366, "height": 768},
    "mobile":  {"width": 360,  "height": 640},
    "tablet":  {"width": 768,  "height": 1024},
}

DEFAULT_VIEWPORT = "desktop"

# ─────────────────────────────────────────────
# Timeouts (milliseconds)
# ─────────────────────────────────────────────
PAGE_LOAD_TIMEOUT_MS = 45_000
ELEMENT_SCREENSHOT_TIMEOUT_MS = 3_000
SCROLL_PAUSE_MS = 600

# Phase Timeouts (seconds)
PHASE_CRAWL_TIMEOUT = 150
PHASE_DETECT_TIMEOUT = 180
PHASE_COMPLY_TIMEOUT = 60
PHASE_GEN_TIMEOUT = 30


# ─────────────────────────────────────────────
# User-Agent rotation pool
# ─────────────────────────────────────────────
USER_AGENTS = {
    "desktop": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "mobile": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Mobile Safari/537.36"
    ),
    "tablet": (
        "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
}

# ─────────────────────────────────────────────
# Heuristic detection — known ad selectors
# ─────────────────────────────────────────────
AD_CSS_SELECTORS = [
    'iframe[id*="google_ads"]',
    'iframe[src*="doubleclick"]',
    'iframe[src*="googlesyndication"]',
    'iframe[src*="amazon-adsystem"]',
    'iframe[src*="taboola"]',
    'iframe[src*="outbrain"]',
    'div[id*="ad-"]',
    'div[id*="ad_"]',
    # Broad prefix match: catches IDs starting with "ad" followed by a hyphen,
    # e.g. "adm-homepage-mid-river-1", "ads-sidebar", "adx-wrapper".
    # Using [id^="ad"] + [id*="-"] avoids false positives on plain words
    # like "adventure-section" while still catching ad-management prefixes
    # (adm-, adw-, adh-, etc.) that a strict "ad-" substring match misses.
    'div[id^="ad"][id*="-"]',
    '[data-is-adhesion-ad]',
    'div[class*="ad-container"]',
    'div[class*="ad-wrapper"]',
    'div[class*="ad-slot"]',
    'div[class*="advert"]',
    'ins.adsbygoogle',
    '[data-ad-slot]',
    '[data-ad-client]',
    '[data-ad-format]',
    '[aria-label="Advertisement"]',
    '[aria-label="Sponsored"]',
    '.sponsored-post',
    '.sponsored-content',
    '.native-ad',
    '.promoted-content',
    'ytd-promoted-sparkles-web-renderer',
    'amp-ad',
    'amp-embed',
]

# ─────────────────────────────────────────────
# Heuristic detection — HTML attribute scanning
# (Strategy G: liberal DOM-level signals)
# ─────────────────────────────────────────────

# Regex patterns for attribute NAMES that signal ad content (case-insensitive).
# Checked against every attribute on every element in the DOM.
AD_ATTRIBUTE_NAME_PATTERNS = [
    r"data-(?:is-)?ad[s]?[\b_-]",       # data-ad-*, data-is-adhesion-ad, data-ads-*
    r"data-google-query-id",             # Google ad query ID
    r"data-(?:ad|ads)$",                 # data-ad, data-ads exactly
    r"data-(?:taboola|outbrain|criteo|sponsor|promo)",
    r"data-ad-(?:slot|client|format|unit|region|zone)",
]

# Regex patterns for attribute VALUES that signal ad content (case-insensitive).
AD_ATTRIBUTE_VALUE_PATTERNS = [
    r"(?:^|[\s_-])ad(?:s|vert(?:ise(?:ment)?)?)?(?:[\s_-]|$)",
    r"sponsor",
    r"google[_-]?ads?",
    r"doubleclick",
    r"adhesion[_-]?ad",
]

# Keywords to match inside HTML comments that mark ad sections.
# When a comment contains any of these, the next sibling element is a candidate.
# Example: <!-- marker for pmc-sticky-ad (aka Mobile Adhesion Ads) -->
AD_COMMENT_KEYWORDS = [
    "ad", "ads", "advert", "advertisement", "ad-slot",
    "ad-container", "ad-wrapper", "adhesion", "sticky-ad",
    "sponsor", "dfp", "gpt-ad", "google-ad", "ad-unit",
    "ad-zone", "ad-region", "ad-placement",
]

# Regex pattern to detect class-encoded IAB ad dimensions.
# Matches e.g. "adw-728", "adh-90" — width/height baked into class names.
AD_DIMENSION_CLASS_PATTERN = r"\bad[wh]-\d+"

# ─────────────────────────────────────────────
# Heuristic detection — keyword matching
# ─────────────────────────────────────────────
AD_TEXT_KEYWORDS = [
    "Sponsored", "Ad", "Promoted", "Advertisement",
    "Paid", "Brought to you by",
]

# ─────────────────────────────────────────────
# Known ad-network domains (for provenance &
# network-rule generation)
# ─────────────────────────────────────────────
AD_NETWORK_DOMAINS = [
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "google-analytics.com",
    "googletagmanager.com",
    "amazon-adsystem.com",
    "taboola.com",
    "outbrain.com",
    "criteo.com",
    "indexexchange.com",
    "rubiconproject.com",
    "pubmatic.com",
    "openx.net",
    "adnxs.com",
    "adsrvr.org",
    "33across.com",
    "ad-delivery.net",
    "hadronid.net",
    "rlcdn.com",
    "receptivity.io",
    "pbxai.com",
    "id5-sync.com",
    "media.net",
    "revcontent.com",
    "mgid.com",
    "zergnet.com",
    "sharethrough.com",
    "yieldmo.com",
    "sovrn.com",
    "triplelift.com",
    "nativo.com",
    "teads.com",
]

# URL-level keyword patterns for ad requests
AD_URL_KEYWORDS = [
    "ad", "ads", "advert", "sponsor", "doubleclick",
    "googlesyndication", "adsystem", "adservice", "adserver",
    "adsbygoogle", "adtech", "adform", "taboola", "outbrain",
    "criteo", "nativead", "promo",
]

# ─────────────────────────────────────────────
# Acceptable Ads Standard — size thresholds
# (Source: https://acceptableads.com/standard/)
# ─────────────────────────────────────────────
# Maximum collective ad area as fraction of viewport
ATF_MAX_OCCUPANCY = 0.15          # Above the fold: 15%
BTF_MAX_OCCUPANCY = 0.25          # Below the fold: 25%

# Maximum heights by position (px)
MAX_HEIGHT_ABOVE_PRIMARY = 200    # Ads above primary content
MAX_WIDTH_ADJACENT = 350          # Ads adjacent to primary content
MAX_HEIGHT_BELOW_PRIMARY = 400    # Ads below primary content
MAX_HEIGHT_IN_CONTENT = 250       # Ads within primary content

# Sticky / floating ad limits
MAX_STICKY_HEIGHT_DESKTOP_FRAC = 0.15   # 15% of screen height
MAX_STICKY_HEIGHT_MOBILE_PX = 75        # 75px on mobile

# Z-index threshold for overlay detection
OVERLAY_Z_INDEX_THRESHOLD = 1000

# ─────────────────────────────────────────────
# Detection limits and false-positive filters
# ─────────────────────────────────────────────
MAX_ADS_PER_PAGE = 50             # Hard cap on detected ads per page

# Keywords indicating a newsletter/signup popup, NOT an ad
NEWSLETTER_POPUP_KEYWORDS = [
    "newsletter", "subscribe", "sign up", "sign me up",
    "email address", "your email", "enter your email",
    "unsubscribe", "inbox", "notifications",
]

# ─────────────────────────────────────────────
# Disallowed ad types (always non-compliant)
# ─────────────────────────────────────────────
DISALLOWED_AD_TYPES = [
    "popup",
    "popunder",
    "interstitial",
    "overlay",
    "expanding",
    "autoplay_video",
    "autoplay_audio",
    "pre_roll_video",
    "rich_media",
    "overlay_in_video",
    "tracking_pixel",
]

# ─────────────────────────────────────────────
# Common ad sizes (IAB standard)
# ─────────────────────────────────────────────
COMMON_AD_SIZES = {
    (300, 250): "medium_rectangle",
    (336, 280): "large_rectangle",
    (728, 90):  "leaderboard",
    (970, 90):  "large_leaderboard",
    (970, 250): "billboard",
    (160, 600): "wide_skyscraper",
    (120, 600): "skyscraper",
    (300, 600): "half_page",
    (320, 50):  "mobile_banner",
    (320, 100): "large_mobile_banner",
    (300, 50):  "mobile_banner_small",
    (468, 60):  "banner",
    (234, 60):  "half_banner",
    (250, 250): "square",
    (200, 200): "small_square",
}

# ─────────────────────────────────────────────
# Cookie / privacy consent dismissal selectors
# ─────────────────────────────────────────────
COOKIE_CONSENT_SELECTORS = [
    # --- Generic attribute matches ---
    'button[id*="accept"]',
    'button[class*="accept"]',
    'button[id*="agree"]',
    'button[class*="agree"]',
    'button[id*="consent"]',
    'button[class*="consent"]',
    'a[id*="accept"]',
    'a[class*="accept"]',
    '[data-testid*="accept"]',
    '[data-testid*="consent"]',

    # --- OneTrust ---
    '#onetrust-accept-btn-handler',

    # --- Cookiebot ---
    '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',

    # --- Fides (Ethyca) – used by Condé Nast (vogue, wired, gq …) ---
    '.fides-acknowledge-button',
    'button.fides-banner-button-primary',

    # --- Sourcepoint ---
    'button[title="Accept"]',
    'button[title="Accept All"]',
    '.sp_choice_type_11',

    # --- Didomi ---
    '#didomi-notice-agree-button',

    # --- Quantcast / TCFv2 ---
    '.qc-cmp2-summary-buttons button[mode="primary"]',
    'button.css-47sehv',                    # QC generic primary

    # --- TrustArc / TrustE ---
    '#truste-consent-button',
    '.truste_popframe .pdynamicbutton',

    # --- Complianz ---
    '.cmplz-accept',
    '.cmplz-btn.cmplz-accept',

    # --- CookieYes ---
    '.cky-btn-accept',

    # --- Moove GDPR ---
    '.moove-gdpr-infobar-allow-all',

    # --- Generic class patterns ---
    '.cookie-accept',
    '.accept-cookies',
    '.js-accept-cookies',

    # --- Playwright :has-text() fallback (checked last) ---
    'button:has-text("Accept All")',
    'button:has-text("Accept all")',
    'button:has-text("Accept Cookies")',
    'button:has-text("I Accept")',
    'button:has-text("I agree")',
    'button:has-text("Allow All")',
    'button:has-text("OK")',
    'button:has-text("Got it")',
    'button:has-text("Agree")',
]

# JS snippets that programmatically fire "accept all" on common CMPs.
# Each entry is evaluated via page.evaluate(); failures are silently ignored.
CONSENT_JS_DISMISS = [
    # Fides (Ethyca)
    "typeof Fides !== 'undefined' && Fides.consent && Fides.consent({opt_in_to_all: true})",
    # OneTrust
    "typeof OneTrust !== 'undefined' && OneTrust.AllowAll && OneTrust.AllowAll()",
    # Cookiebot
    "typeof Cookiebot !== 'undefined' && Cookiebot.submitCustomConsent && "
    "Cookiebot.submitCustomConsent(true, true, true)",
    # TCF v2
    "typeof __tcfapi === 'function' && __tcfapi('acceptAllConsent', 2, function(){})",
]

# ─────────────────────────────────────────────
# LLM configuration
# ─────────────────────────────────────────────
OPENAI_MODEL = "gpt-5-mini"
OPENAI_MAX_TOKENS = 600

# Model pricing registry (USD per 1M tokens)
MODEL_PRICING = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "gpt-5.1": {"input": 1.25, "output": 10.00},
}

# Convenience accessors (derived from OPENAI_MODEL)
INPUT_COST_PER_1M = MODEL_PRICING.get(OPENAI_MODEL, MODEL_PRICING["gpt-5-mini"])["input"]
OUTPUT_COST_PER_1M = MODEL_PRICING.get(OPENAI_MODEL, MODEL_PRICING["gpt-5-mini"])["output"]

# Minimum element dimensions to consider for normal ad candidates.
# Tiny elements are skipped unless they match strict tracking-pixel signals.
MIN_ELEMENT_WIDTH = 20
MIN_ELEMENT_HEIGHT = 20

# Tracking-pixel exception bounds (e.g., 1x1 beacons).
TRACKING_PIXEL_MAX_WIDTH = 2
TRACKING_PIXEL_MAX_HEIGHT = 2

# Score threshold for heuristic detection
HEURISTIC_AD_SCORE_THRESHOLD = 2

# ─────────────────────────────────────────────
# LLM prefilter thresholds
# ─────────────────────────────────────────────
MIN_IMAGE_SIZE_BYTES = 1024       # Skip images < 1KB (likely blank)
MIN_PIXEL_STD = 5.0               # Skip images with low variance (solid color)
PHASH_DISTANCE_THRESHOLD = 5      # Near-duplicate hamming distance threshold

# ─────────────────────────────────────────────
# Provenance tracking JavaScript
# ─────────────────────────────────────────────
PROVENANCE_SCRIPT = """
(() => {
    const origAppend = Node.prototype.appendChild;
    const origInsert = Node.prototype.insertBefore;

    function getInitiator() {
        try { throw new Error(); } catch (e) {
            const stack = e.stack.split('\\n');
            for (let i = 2; i < stack.length; i++) {
                const match = stack[i].match(/(https?:\\/\\/[^):]+)/);
                if (match) return match[1];
            }
        }
        return 'unknown';
    }

    function tag(el, initiator) {
        if (el && el.nodeType === 1) {
            try { el.setAttribute('data-provenance-source', initiator); } catch(e) {}
        }
    }

    Node.prototype.appendChild = function(child) {
        tag(child, getInitiator());
        return origAppend.apply(this, arguments);
    };

    Node.prototype.insertBefore = function(newNode, ref) {
        tag(newNode, getInitiator());
        return origInsert.apply(this, arguments);
    };
})();
"""
