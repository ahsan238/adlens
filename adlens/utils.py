"""
utils.py — Shared helper functions.
"""

import os
import csv
import json
import base64
import signal
import time
from typing import List, Optional
from urllib.parse import urlparse
from datetime import datetime
from contextlib import contextmanager


def extract_domain(url: str) -> str:
    """Extract the domain from a URL, stripping 'www.' prefix."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        domain = parsed.netloc or parsed.path
        return domain.replace("www.", "").strip("/")
    except Exception:
        return url


def is_third_party(src_url: str, page_domain: str) -> bool:
    """Check if a URL belongs to a different domain than the page."""
    try:
        src_domain = extract_domain(src_url)
        # Compare base domains (handle subdomains)
        src_parts = src_domain.rsplit(".", 2)
        page_parts = page_domain.rsplit(".", 2)
        src_base = ".".join(src_parts[-2:]) if len(src_parts) >= 2 else src_domain
        page_base = ".".join(page_parts[-2:]) if len(page_parts) >= 2 else page_domain
        return src_base != page_base
    except Exception:
        return True


def encode_image_base64(image_path: str) -> Optional[str]:
    """Encode an image file to base64 string."""
    if not os.path.exists(image_path):
        return None
    try:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None


def ensure_dir(path: str) -> str:
    """Create directory if it doesn't exist. Returns the path."""
    os.makedirs(path, exist_ok=True)
    return path


def make_session_dir(base_dir: str, domain: str) -> str:
    """Create a timestamped session directory under base_dir/domain/."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_id = f"session_{timestamp}"
    session_dir = os.path.join(base_dir, domain, session_id)
    ensure_dir(session_dir)
    ensure_dir(os.path.join(session_dir, "ads"))
    return session_dir


def get_session_id(session_dir: str) -> str:
    """Extract the session ID from a session directory path."""
    return os.path.basename(session_dir)


def save_json(data: dict, filepath: str) -> None:
    """Save dictionary as a formatted JSON file."""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def save_rules(rules: List[str], filepath: str, url: str = "") -> None:
    """Save filter rules in EasyList format with header."""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("[Adblock Plus 2.0]\n")
        f.write(f"! Title: AdLens Auto-Generated Rules\n")
        f.write(f"! Last modified: {datetime.now().isoformat()}\n")
        if url:
            f.write(f"! Source URL: {url}\n")
        f.write(f"! Total rules: {len(rules)}\n")
        f.write("!\n")
        for rule in rules:
            f.write(f"{rule}\n")


def classify_ad_size(width: float, height: float) -> str:
    """Classify an ad by its dimensions against IAB standard sizes."""
    from .config import COMMON_AD_SIZES
    w, h = int(round(width)), int(round(height))
    if (w, h) in COMMON_AD_SIZES:
        return f"{w}x{h} ({COMMON_AD_SIZES[(w, h)]})"
    # Approximate matching (±10px tolerance)
    for (sw, sh), name in COMMON_AD_SIZES.items():
        if abs(w - sw) <= 10 and abs(h - sh) <= 10:
            return f"{w}x{h} (~{name})"
    # Heuristic classification
    if w >= 900 and h <= 120:
        return f"{w}x{h} (leaderboard-ish)"
    if w <= 320 and h <= 100:
        return f"{w}x{h} (mobile)"
    if w >= 300 and h >= 250:
        return f"{w}x{h} (rectangle)"
    return f"{w}x{h}"


def read_urls_file(filepath: str) -> List[str]:
    """Read URLs from a text file (one per line)."""
    urls = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                if not line.startswith("http"):
                    line = f"https://{line}"
                urls.append(line)
    return urls


def read_tranco_csv(filepath: str, top_n: int = 100) -> List[str]:
    """Read the top N websites from a Tranco CSV file."""
    urls = []
    with open(filepath, "r") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i >= top_n:
                break
            if len(row) >= 2:
                urls.append(f"https://{row[1]}")
    return urls


def get_registerable_domain(domain: str) -> str:
    """Extract the registerable (base) domain, e.g. 'fdn.gsmarena.com' -> 'gsmarena.com'."""
    parts = domain.rsplit(".", 2)
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return domain


def is_same_site(url: str, page_domain: str) -> bool:
    """Check if a URL belongs to the same registerable domain as the page."""
    try:
        src_domain = extract_domain(url)
        return get_registerable_domain(src_domain) == get_registerable_domain(page_domain)
    except Exception:
        return False


import re as _re

# Pre-compiled pattern for word-boundary ad keyword matching
_AD_KEYWORD_PATTERN = _re.compile(
    r'(?:^|[/\-_.?&=])(?:ad|ads|advert|sponsor|doubleclick|googlesyndication|'
    r'adsystem|adservice|adserver|adsbygoogle|adtech|adform|taboola|outbrain|'
    r'criteo|indexexchange|nativead)(?:$|[/\-_.?&=])',
    _re.IGNORECASE,
)


def is_ad_url(url: str) -> bool:
    """Check if a URL matches ad-network patterns using word-boundary matching.
    Prevents false positives like 'gsmarena' matching 'ad' as a substring."""
    return bool(_AD_KEYWORD_PATTERN.search(url))


def rects_overlap(r1: dict, r2: dict, threshold: float = 0.60) -> bool:
    """Check if two bounding rectangles overlap by >= threshold (IoU-like).
    Each rect is a dict with x, y, width, height."""
    x1 = max(r1["x"], r2["x"])
    y1 = max(r1["y"], r2["y"])
    x2 = min(r1["x"] + r1["width"], r2["x"] + r2["width"])
    y2 = min(r1["y"] + r1["height"], r2["y"] + r2["height"])

    if x2 <= x1 or y2 <= y1:
        return False

    intersection = (x2 - x1) * (y2 - y1)
    area1 = r1["width"] * r1["height"]
    area2 = r2["width"] * r2["height"]
    smaller_area = min(area1, area2)

    if smaller_area == 0:
        return False

    # If the intersection covers >= threshold of the SMALLER element, they overlap
    return (intersection / smaller_area) >= threshold


def rect_contains(outer: dict, inner: dict, tolerance: float = 10.0) -> bool:
    """Check if outer rect fully contains inner rect (within a tolerance in px).
    Used to detect parent-child ad element relationships where the child iframe
    is contained inside the parent wrapper div."""
    return (
        inner["x"] >= outer["x"] - tolerance
        and inner["y"] >= outer["y"] - tolerance
        and inner["x"] + inner["width"] <= outer["x"] + outer["width"] + tolerance
        and inner["y"] + inner["height"] <= outer["y"] + outer["height"] + tolerance
    )


def dedup_nested_ads(ads):
    """Remove nested/contained ad elements, keeping only the outermost parent.

    When an ad wrapper div and its inner iframe are both detected as separate ads,
    they double-count occupancy. This function removes the inner (child) element,
    keeping the outer (parent) element which has more context (classes, data attrs).

    Args:
        ads: List of AdCandidate objects with rect attributes.

    Returns:
        Deduplicated list of AdCandidate objects.
    """
    if len(ads) <= 1:
        return ads

    to_remove = set()
    for i, ad_i in enumerate(ads):
        if not ad_i.rect or i in to_remove:
            continue
        ri = ad_i.rect.to_dict()

        for j, ad_j in enumerate(ads):
            if i == j or not ad_j.rect or j in to_remove:
                continue
            rj = ad_j.rect.to_dict()

            # Check if ad_j is contained within ad_i (ad_i is the parent)
            if rect_contains(ri, rj):
                # Keep the outer (parent) element — it typically has more
                # semantic context (data-name, classes, etc.)
                to_remove.add(j)
            # Check the reverse: ad_i contained within ad_j
            elif rect_contains(rj, ri):
                to_remove.add(i)
                break  # ad_i is removed, skip remaining comparisons

    result = [ad for idx, ad in enumerate(ads) if idx not in to_remove]
    if to_remove:
        print(f"  [Dedup] Removed {len(to_remove)} nested/child ad element(s) "
              f"({len(ads)} → {len(result)})")
    return result


@contextmanager
def timeout_phase(seconds: int, phase_name: str = "Phase"):
    """
    Context manager to enforce a time limit on a code block.
    Supports nested timeouts by restoring the previous alarm.
    """
    if seconds <= 0:
        yield
        return

    def handler(signum, frame):
        raise TimeoutError(f"{phase_name} timed out after {seconds}s")

    # Set the signal handler and alarm
    old_handler = signal.signal(signal.SIGALRM, handler)
    
    # Get remaining time of any existing alarm
    # signal.alarm(0) returns the previous alarm remaining time
    old_remaining = signal.alarm(0)
    
    # Check if we should use the new timeout or respect the old one?
    # Usually we want the *shorter* of the two effectively?
    # But for simplicity, let's enforce the new timeout for this block,
    # and restore the old one (minus elapsed) afterwards.
    
    start_time = time.time()
    signal.alarm(int(seconds))
    
    try:
        yield
    finally:
        # Disable current alarm
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        
        # Restore old alarm if it was set
        if old_remaining > 0:
            elapsed = time.time() - start_time
            new_remaining = max(1, int(old_remaining - elapsed))
            # If the old alarm would have expired by now, schedule it immediately
            # or realistically, just set it to 1s or let it fire?
            # If we set it to 1, it fires in 1s.
            signal.alarm(new_remaining)
