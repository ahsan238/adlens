"""
compliance.py — Acceptable Ads Standard compliance checker.
Validates detected ads against size, position, distinction, and behavior rules.
"""

from typing import List

from .config import (
    ATF_MAX_OCCUPANCY,
    BTF_MAX_OCCUPANCY,
    MAX_HEIGHT_ABOVE_PRIMARY,
    MAX_HEIGHT_BELOW_PRIMARY,
    MAX_HEIGHT_IN_CONTENT,
    MAX_WIDTH_ADJACENT,
    MAX_STICKY_HEIGHT_DESKTOP_FRAC,
    MAX_STICKY_HEIGHT_MOBILE_PX,
    OVERLAY_Z_INDEX_THRESHOLD,
    DISALLOWED_AD_TYPES,
)
from .models import AdCandidate, ComplianceResult, Violation


class ComplianceChecker:
    """
    Checks each detected ad against the Acceptable Ads Standard.
    Returns a ComplianceResult with any violations found.
    """

    def __init__(self, viewport_type: str = "desktop"):
        self.viewport_type = viewport_type

    def check(self, ad: AdCandidate) -> ComplianceResult:
        """Run all compliance checks on a single ad candidate."""
        violations: List[Violation] = []

        # 1. Disallowed ad type check
        violations.extend(self._check_disallowed_type(ad))

        # 2. Size / occupancy check
        violations.extend(self._check_size(ad))

        # 3. Height by position check
        violations.extend(self._check_height_by_position(ad))

        # 4. Sticky ad constraints
        violations.extend(self._check_sticky(ad))

        # 5. Overlay / z-index check
        violations.extend(self._check_overlay(ad))

        # 6. Distinction / labeling check
        violations.extend(self._check_distinction(ad))

        # 7. Behavior check (animation, autoplay)
        violations.extend(self._check_behavior(ad))

        is_compliant = len(violations) == 0
        reasoning = self._build_reasoning(ad, violations)

        return ComplianceResult(
            is_compliant=is_compliant,
            violations=violations,
            reasoning=reasoning,
        )

    def check_batch(self, ads: List[AdCandidate]) -> List[ComplianceResult]:
        """Check compliance for a batch of ads, including collective size checks."""
        results = [self.check(ad) for ad in ads]

        # Collective size check: ATF and BTF total occupancy
        atf_total = sum(
            ad.occupancy_pct for ad in ads if ad.is_above_fold
        )
        btf_total = sum(
            ad.occupancy_pct for ad in ads if not ad.is_above_fold
        )

        if atf_total > ATF_MAX_OCCUPANCY * 100:
            collective_violation = Violation(
                category="Size",
                description=(
                    f"Collective ATF ads occupy {atf_total:.1f}% of viewport "
                    f"(limit: {ATF_MAX_OCCUPANCY*100:.0f}%)"
                ),
                severity="high",
            )
            for i, ad in enumerate(ads):
                if ad.is_above_fold:
                    results[i].violations.append(collective_violation)
                    results[i].is_compliant = False

        if btf_total > BTF_MAX_OCCUPANCY * 100:
            collective_violation = Violation(
                category="Size",
                description=(
                    f"Collective BTF ads occupy {btf_total:.1f}% of viewport "
                    f"(limit: {BTF_MAX_OCCUPANCY*100:.0f}%)"
                ),
                severity="high",
            )
            for i, ad in enumerate(ads):
                if not ad.is_above_fold:
                    results[i].violations.append(collective_violation)
                    results[i].is_compliant = False

        # Fix 4: Rebuild reasoning for any result that had collective violations added
        for i, (ad, result) in enumerate(zip(ads, results)):
            if not result.is_compliant:
                result.reasoning = self._build_reasoning(ad, result.violations)

        return results

    # ─── Individual checks ──────────────────────────────────

    def _check_disallowed_type(self, ad: AdCandidate) -> List[Violation]:
        """Check if the ad type is in the disallowed list."""
        violations = []
        ad_type_lower = ad.ad_type.lower().replace(" ", "_")

        for disallowed in DISALLOWED_AD_TYPES:
            if disallowed in ad_type_lower:
                violations.append(Violation(
                    category="Disallowed",
                    description=f"Ad type '{ad.ad_type}' is not allowed under Acceptable Ads Standard",
                    severity="high",
                ))
                break

        # Check for interstitial-like behavior (covers most of viewport)
        if ad.occupancy_pct > 50:
            violations.append(Violation(
                category="Disallowed",
                description=f"Ad covers {ad.occupancy_pct:.0f}% of viewport — likely an interstitial/overlay",
                severity="high",
            ))

        return violations

    def _check_size(self, ad: AdCandidate) -> List[Violation]:
        """Check individual ad size against viewport occupancy limits."""
        violations = []

        if ad.is_above_fold:
            if ad.occupancy_pct > ATF_MAX_OCCUPANCY * 100:
                violations.append(Violation(
                    category="Size",
                    description=(
                        f"ATF ad occupies {ad.occupancy_pct:.1f}% of viewport "
                        f"(limit: {ATF_MAX_OCCUPANCY*100:.0f}%)"
                    ),
                    severity="high",
                ))
        else:
            if ad.occupancy_pct > BTF_MAX_OCCUPANCY * 100:
                violations.append(Violation(
                    category="Size",
                    description=(
                        f"BTF ad occupies {ad.occupancy_pct:.1f}% of viewport "
                        f"(limit: {BTF_MAX_OCCUPANCY*100:.0f}%)"
                    ),
                    severity="high",
                ))

        return violations

    def _get_creative_height(self, ad: AdCandidate) -> float:
        """Return the ad creative height, preferring declared data-height
        over the bounding box height.

        Ad wrapper divs often include padding, margins, and label areas that
        inflate the bounding box beyond the actual ad creative size. The
        data-height attribute (set by the ad server, e.g. GPT) gives the
        true intended creative height.
        """
        data_height_str = ad.data_attributes.get("data-height", "")
        if data_height_str:
            # Parse values like "250px", "90px"
            try:
                numeric = float(data_height_str.replace("px", "").strip())
                if numeric > 0:
                    return numeric
            except (ValueError, TypeError):
                pass
        return ad.rect.height

    def _check_height_by_position(self, ad: AdCandidate) -> List[Violation]:
        """Check ad height limits based on position relative to primary content."""
        violations = []
        if not ad.rect:
            return violations

        height = self._get_creative_height(ad)
        y_pos = ad.rect.y
        vp_h = ad.viewport_height

        # Above primary content (top of page)
        if y_pos < 100 and height > MAX_HEIGHT_ABOVE_PRIMARY:
            violations.append(Violation(
                category="Size",
                description=(
                    f"Ad above primary content is {height:.0f}px tall "
                    f"(limit: {MAX_HEIGHT_ABOVE_PRIMARY}px)"
                ),
                severity="high",
            ))

        # In-content ads (extended zone: up to 5× viewport height covers
        # most article bodies including long-scroll pages)
        if 100 <= y_pos < vp_h * 5 and height > MAX_HEIGHT_IN_CONTENT:
            if not ad.is_sticky:
                violations.append(Violation(
                    category="Size",
                    description=(
                        f"In-content ad is {height:.0f}px tall "
                        f"(limit: {MAX_HEIGHT_IN_CONTENT}px)"
                    ),
                    severity="medium",
                ))

        # Below primary content
        if y_pos >= vp_h * 5 and height > MAX_HEIGHT_BELOW_PRIMARY:
            violations.append(Violation(
                category="Size",
                description=(
                    f"Ad below primary content is {height:.0f}px tall "
                    f"(limit: {MAX_HEIGHT_BELOW_PRIMARY}px)"
                ),
                severity="medium",
            ))

        return violations

    def _check_sticky(self, ad: AdCandidate) -> List[Violation]:
        """Check sticky/floating ad constraints."""
        violations = []
        if not ad.is_sticky or not ad.rect:
            return violations

        height = ad.rect.height
        vp_h = ad.viewport_height

        if self.viewport_type == "mobile":
            if height > MAX_STICKY_HEIGHT_MOBILE_PX:
                violations.append(Violation(
                    category="Size",
                    description=(
                        f"Mobile sticky ad is {height:.0f}px tall "
                        f"(limit: {MAX_STICKY_HEIGHT_MOBILE_PX}px)"
                    ),
                    severity="high",
                ))
        else:
            max_height = vp_h * MAX_STICKY_HEIGHT_DESKTOP_FRAC
            if height > max_height:
                violations.append(Violation(
                    category="Size",
                    description=(
                        f"Desktop sticky ad is {height:.0f}px tall "
                        f"(covers {ad.height_pct:.1f}% of screen, "
                        f"limit: {MAX_STICKY_HEIGHT_DESKTOP_FRAC*100:.0f}%)"
                    ),
                    severity="high",
                ))

        return violations

    def _check_overlay(self, ad: AdCandidate) -> List[Violation]:
        """Check for overlay/popup behavior via z-index."""
        violations = []

        z_index_str = ad.styles.get("zIndex", "auto")
        try:
            z_index = int(z_index_str)
        except (ValueError, TypeError):
            return violations

        if z_index > OVERLAY_Z_INDEX_THRESHOLD:
            position = ad.styles.get("position", "static")
            if position in ("fixed", "absolute", "sticky"):
                violations.append(Violation(
                    category="Overlay",
                    description=(
                        f"High z-index ({z_index}) with {position} positioning — "
                        "likely an overlay/popup ad"
                    ),
                    severity="high",
                ))

        return violations

    def _check_distinction(self, ad: AdCandidate) -> List[Violation]:
        """Check if the ad is properly labeled."""
        violations = []

        # If ad marker is detected (AdChoices icon, aria labels, etc.),
        # the distinction requirement is met
        if ad.has_ad_marker:
            return violations

        # Check text content for labels
        text_lower = ad.text_snippet.lower()
        ad_labels = ["advertisement", "sponsored", "ad", "adchoices", "ads by", "paid"]
        has_label = any(label in text_lower for label in ad_labels)

        if not has_label:
            # Check classes and IDs for sponsored indicators
            all_attrs = " ".join(ad.classes).lower() + " " + ad.element_id.lower()
            has_attr_label = any(label in all_attrs for label in ["sponsor", "advert", "promo"])

            if not has_attr_label:
                violations.append(Violation(
                    category="Distinction",
                    description=(
                        "Ad lacks clear labeling — must be marked with "
                        "'Advertisement', 'Sponsored', 'Ad', or equivalent"
                    ),
                    severity="medium",
                ))

        return violations

    def _check_behavior(self, ad: AdCandidate) -> List[Violation]:
        """Check for disruptive behavior (animation, autoplay)."""
        violations = []

        # Animation check (from style data)
        # Note: detailed animation detection is done via JS in the crawler;
        # here we flag based on ad type classification
        if ad.ad_type == "Video":
            violations.append(Violation(
                category="Behavior",
                description="Video ad detected — autoplay video/audio ads are not acceptable",
                severity="high",
            ))

        return violations

    # ─── Helpers ────────────────────────────────────────────

    def _build_reasoning(self, ad: AdCandidate, violations: List[Violation]) -> str:
        """Build a human-readable reasoning string."""
        if not violations:
            return (
                f"Ad ({ad.ad_type}) is compliant. "
                f"Size: {ad.rect.width:.0f}×{ad.rect.height:.0f}px, "
                f"occupancy: {ad.occupancy_pct:.1f}%, "
                f"position: {'ATF' if ad.is_above_fold else 'BTF'}, "
                f"{'sticky' if ad.is_sticky else 'inline'}."
            ) if ad.rect else "Ad appears compliant."

        reasons = [v.description for v in violations]
        return f"Non-compliant: {'; '.join(reasons)}"
