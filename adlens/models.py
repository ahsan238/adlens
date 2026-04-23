"""
models.py — Data classes for structured data flow between subsystems.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime


@dataclass
class BoundingRect:
    """Element bounding rectangle in the viewport."""
    x: float
    y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def right(self) -> float:
        return self.x + self.width

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass
class AdCandidate:
    """A detected ad element with all gathered metadata."""
    index: int
    tag_name: str
    element_id: str = ""
    classes: List[str] = field(default_factory=list)
    rect: Optional[BoundingRect] = None
    src: str = "Unknown"
    provenance: str = "unknown"
    detection_reason: str = ""
    detection_strategies: List[str] = field(default_factory=list)
    detection_score: float = 0.0
    text_snippet: str = ""
    screenshot_path: str = ""
    styles: Dict[str, str] = field(default_factory=dict)
    data_attributes: Dict[str, str] = field(default_factory=dict)
    has_ad_marker: bool = False
    is_ad: bool = False
    confidence: float = 0.0
    ad_type: str = "unknown"   # Banner, Sticky, Interstitial, Video, Native, etc.
    viewport_height: float = 768.0
    viewport_width: float = 1366.0
    is_above_fold: bool = True
    is_sticky: bool = False
    outer_html: str = ""  # Raw HTML of the element (truncated), sent to LLM for analysis
    usage: Dict[str, Any] = field(default_factory=dict)

    @property
    def creative_width(self) -> float:
        """Return the ad creative width, preferring data-width over bounding box."""
        data_w = self.data_attributes.get("data-width", "")
        if data_w:
            try:
                w = float(data_w.replace("px", "").strip())
                if w > 0:
                    return w
            except (ValueError, TypeError):
                pass
        return self.rect.width if self.rect else 0.0

    @property
    def creative_height(self) -> float:
        """Return the ad creative height, preferring data-height over bounding box."""
        data_h = self.data_attributes.get("data-height", "")
        if data_h:
            try:
                h = float(data_h.replace("px", "").strip())
                if h > 0:
                    return h
            except (ValueError, TypeError):
                pass
        return self.rect.height if self.rect else 0.0

    @property
    def height_pct(self) -> float:
        """Height as percentage of viewport height."""
        if self.rect and self.viewport_height > 0:
            return (self.creative_height / self.viewport_height) * 100
        return 0.0

    @property
    def occupancy_pct(self) -> float:
        """Area as percentage of viewport area, using creative dimensions when available."""
        if self.rect and self.viewport_height > 0 and self.viewport_width > 0:
            vp_area = self.viewport_width * self.viewport_height
            creative_area = self.creative_width * self.creative_height
            return (creative_area / vp_area) * 100
        return 0.0

    def to_dict(self) -> dict:
        d = {
            "index": self.index,
            "tag_name": self.tag_name,
            "element_id": self.element_id,
            "classes": self.classes,
            "rect": self.rect.to_dict() if self.rect else None,
            "src": self.src,
            "provenance": self.provenance,
            "detection_reason": self.detection_reason,
            "detection_strategies": self.detection_strategies,
            "detection_score": self.detection_score,
            "text_snippet": self.text_snippet,
            "screenshot_path": self.screenshot_path,
            "styles": self.styles,
            "data_attributes": self.data_attributes,
            "has_ad_marker": self.has_ad_marker,
            "is_ad": self.is_ad,
            "confidence": self.confidence,
            "ad_type": self.ad_type,
            "height_pct": round(self.height_pct, 2),
            "occupancy_pct": round(self.occupancy_pct, 2),
            "is_above_fold": self.is_above_fold,
            "is_sticky": self.is_sticky,
            "outer_html": self.outer_html[:500] if self.outer_html else "",
            "usage": self.usage,
        }
        return d


@dataclass
class Violation:
    """A single compliance violation."""
    category: str           # Size, Distinction, Overlay, Animation, Behavior, Disallowed
    description: str
    severity: str = "high"  # low, medium, high

    def to_dict(self) -> dict:
        return {"category": self.category, "description": self.description, "severity": self.severity}


@dataclass
class ComplianceResult:
    """Compliance audit outcome for a single ad."""
    is_compliant: bool = True
    violations: List[Violation] = field(default_factory=list)
    reasoning: str = ""

    @property
    def violation_types(self) -> List[str]:
        return list(set(v.category for v in self.violations))

    def to_dict(self) -> dict:
        return {
            "is_compliant": self.is_compliant,
            "violations": [v.to_dict() for v in self.violations],
            "violation_types": self.violation_types,
            "reasoning": self.reasoning,
        }


@dataclass
class FilterRule:
    """A generated ABP filter rule."""
    syntax: str                          # The actual rule string
    rule_type: str = "cosmetic"          # network, cosmetic, procedural, exception
    target_domain: str = ""
    reasoning: str = ""
    violation_category: str = ""

    def to_dict(self) -> dict:
        return {
            "syntax": self.syntax,
            "rule_type": self.rule_type,
            "target_domain": self.target_domain,
            "reasoning": self.reasoning,
            "violation_category": self.violation_category,
        }


@dataclass
class ExceptionAuditResult:
    """Result of auditing an AAS exception rule against non-compliant ads."""
    original_rule: str = ""              # The full @@... rule text
    line_number: int = 0                  # Line in exceptionlist.txt
    matched_ad_indices: List[int] = field(default_factory=list)
    violation_types: List[str] = field(default_factory=list)
    recommendation: str = ""              # "remove_domain" | "narrow_type" | "flag_only"
    suggested_rule: str = ""              # Replacement rule (if applicable)
    reasoning: str = ""                   # Human-readable explanation

    def to_dict(self) -> dict:
        return {
            "original_rule": self.original_rule,
            "line_number": self.line_number,
            "matched_ad_indices": self.matched_ad_indices,
            "violation_types": self.violation_types,
            "recommendation": self.recommendation,
            "suggested_rule": self.suggested_rule,
            "reasoning": self.reasoning,
        }


@dataclass
class AdReport:
    """Complete report for a single detected ad."""
    candidate: AdCandidate
    compliance: ComplianceResult = field(default_factory=ComplianceResult)
    rules: List[FilterRule] = field(default_factory=list)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "candidate": self.candidate.to_dict(),
            "compliance": self.compliance.to_dict(),
            "rules": [r.to_dict() for r in self.rules],
        }


@dataclass
class ScanReport:
    """Full report for a single page scan session."""
    url: str
    domain: str
    session_id: str
    mode: str                           # "heuristic" or "llm"
    viewport: str                       # "desktop", "mobile", "tablet"
    start_time: str = ""
    end_time: str = ""
    total_candidates: int = 0
    confirmed_ads: int = 0
    compliant_ads: int = 0
    non_compliant_ads: int = 0
    total_rules_generated: int = 0
    ads: List[AdReport] = field(default_factory=list)
    ads: List[AdReport] = field(default_factory=list)
    output_dir: str = ""
    # Usage Stats
    total_cost_usd: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    exception_audit: List[ExceptionAuditResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "domain": self.domain,
            "session_id": self.session_id,
            "mode": self.mode,
            "viewport": self.viewport,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "summary": {
                "total_candidates": self.total_candidates,
                "confirmed_ads": self.confirmed_ads,
                "compliant_ads": self.compliant_ads,
                "non_compliant_ads": self.non_compliant_ads,
                "total_rules_generated": self.total_rules_generated,
                "compliant_ads": self.compliant_ads,
                "non_compliant_ads": self.non_compliant_ads,
                "total_rules_generated": self.total_rules_generated,
                "usage": {
                    "total_cost_usd": self.total_cost_usd,
                    "total_prompt_tokens": self.total_prompt_tokens,
                    "total_completion_tokens": self.total_completion_tokens,
                }
            },
            "ads": [a.to_dict() for a in self.ads],
            "exception_audit": [e.to_dict() for e in self.exception_audit],
        }
