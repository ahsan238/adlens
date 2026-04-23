"""
prefilter.py — Image deduplication and blank detection.
Filters ad screenshots before sending to the LLM to save API costs.

Pipeline: file_size -> pixel_std -> MD5 exact dedup -> pHash near-dedup
"""

import os
import hashlib
from typing import List, Dict, Tuple

import numpy as np
from PIL import Image
import imagehash

from .config import (
    MIN_IMAGE_SIZE_BYTES,
    MIN_PIXEL_STD,
    PHASH_DISTANCE_THRESHOLD,
)


def filter_ad_images(image_paths: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """
    Filter a list of ad screenshot paths.

    Returns:
        kept: list of unique, non-blank image paths to send to LLM
        skipped: dict mapping skipped path -> reason
    """
    skipped: Dict[str, str] = {}
    candidates: List[str] = []

    # Stage 1: File size filter
    for path in image_paths:
        if not os.path.isfile(path):
            skipped[path] = "file_not_found"
            continue
        size = os.path.getsize(path)
        if size < MIN_IMAGE_SIZE_BYTES:
            skipped[path] = f"too_small ({size}B < {MIN_IMAGE_SIZE_BYTES}B)"
            continue
        candidates.append(path)

    # Stage 2: Pixel variance filter
    stage2: List[Tuple[str, Image.Image]] = []
    for path in candidates:
        try:
            img = Image.open(path).convert("RGB")
            pixels = np.array(img, dtype=np.float32)
            std = float(np.std(pixels))
            if std < MIN_PIXEL_STD:
                skipped[path] = f"solid_color (std={std:.1f} < {MIN_PIXEL_STD})"
                continue
            stage2.append((path, img))
        except Exception as e:
            skipped[path] = f"image_error ({e})"

    # Stage 3: MD5 exact dedup
    seen_md5: Dict[str, str] = {}
    stage3: List[Tuple[str, Image.Image]] = []
    for path, img in stage2:
        with open(path, "rb") as f:
            md5 = hashlib.md5(f.read()).hexdigest()
        if md5 in seen_md5:
            skipped[path] = f"exact_duplicate_of ({os.path.basename(seen_md5[md5])})"
            continue
        seen_md5[md5] = path
        stage3.append((path, img))

    # Stage 4: pHash near-dedup
    kept: List[str] = []
    kept_hashes: List[Tuple[str, imagehash.ImageHash]] = []
    for path, img in stage3:
        phash = imagehash.phash(img)
        is_dup = False
        for existing_path, existing_hash in kept_hashes:
            dist = phash - existing_hash
            if dist <= PHASH_DISTANCE_THRESHOLD:
                skipped[path] = (
                    f"near_duplicate_of ({os.path.basename(existing_path)}, "
                    f"dist={dist})"
                )
                is_dup = True
                break
        if not is_dup:
            kept.append(path)
            kept_hashes.append((path, phash))

    return kept, skipped


def summarize_filtering(
    original_count: int,
    kept: List[str],
    skipped: Dict[str, str],
) -> Dict[str, object]:
    """Produce a summary of filtering results."""
    reasons: Dict[str, int] = {}
    for reason in skipped.values():
        category = reason.split(" ")[0].split("(")[0].strip("_")
        reasons[category] = reasons.get(category, 0) + 1

    return {
        "original_count": original_count,
        "kept_count": len(kept),
        "skipped_count": len(skipped),
        "reduction_pct": round(len(skipped) / max(original_count, 1) * 100, 1),
        "skip_reasons": reasons,
    }
