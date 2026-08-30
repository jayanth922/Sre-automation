import re

content = open("sre_agent/severity_engine.py").read()

content = re.sub(
    r"<<<<<<< HEAD\nfrom dataclasses import dataclass, field\nfrom datetime import datetime, timezone\n=======\nfrom dataclasses import dataclass\n>>>>>>> master\n",
    r"from dataclasses import dataclass, field\nfrom datetime import datetime, timezone\n",
    content
)

c2 = """<<<<<<< HEAD
    hypothesis_confidence: Optional[float] = None  # Reflector confidence
    evidence: List[EvidenceLink] = field(default_factory=list)

    def unknown_fields(self) -> List[str]:
        measured = (
            "affected_services",
            "error_rate",
            "slo_burn_rate",
            "slo_breached",
            "saturation",
            "hypothesis_confidence",
        )
        return [name for name in measured if getattr(self, name) is None]
=======
    # Only empirically calibrated diagnosis probability belongs here. Raw
    # model self-confidence must remain outside severity policy.
    hypothesis_confidence: float = 0.0
    hypothesis_confidence_calibrated: bool = False
>>>>>>> master
"""
r2 = """    # Only empirically calibrated diagnosis probability belongs here. Raw
    # model self-confidence must remain outside severity policy.
    hypothesis_confidence: float = 0.0
    hypothesis_confidence_calibrated: bool = False
    evidence: List[EvidenceLink] = field(default_factory=list)

    def unknown_fields(self) -> List[str]:
        measured = (
            "affected_services",
            "error_rate",
            "slo_burn_rate",
            "slo_breached",
            "saturation",
        )
        return [name for name in measured if getattr(self, name) is None]
"""
content = content.replace(c2, r2)

c3 = """<<<<<<< HEAD
    confidence = signals.hypothesis_confidence
    if confidence is None or confidence < _CONFIDENCE_ROUNDUP_THRESHOLD:
=======
    if (
        not signals.hypothesis_confidence_calibrated
        or signals.hypothesis_confidence < _CONFIDENCE_ROUNDUP_THRESHOLD
    ):
>>>>>>> master
"""
r3 = """    if (
        not signals.hypothesis_confidence_calibrated
        or signals.hypothesis_confidence < _CONFIDENCE_ROUNDUP_THRESHOLD
    ):
"""
content = content.replace(c3, r3)

c4 = """<<<<<<< HEAD
        conf_txt = "missing" if confidence is None else f"{confidence:.2f}"
        rationale += (
            f"; escalated to {severity.name} "
            f"(confidence {conf_txt} < {_CONFIDENCE_ROUNDUP_THRESHOLD})"
        )
    if unknown_telemetry:
        rationale += "; partial unknown fields present"
=======
        if signals.hypothesis_confidence_calibrated:
            rationale += (
                f"; escalated to {severity.name} "
                f"(calibrated diagnosis probability "
                f"{signals.hypothesis_confidence:.2f} < "
                f"{_CONFIDENCE_ROUNDUP_THRESHOLD})"
            )
        else:
            rationale += (
                f"; escalated to {severity.name} "
                "(diagnosis confidence is uncalibrated)"
            )
>>>>>>> master
"""
r4 = """        if signals.hypothesis_confidence_calibrated:
            rationale += (
                f"; escalated to {severity.name} "
                f"(calibrated diagnosis probability "
                f"{signals.hypothesis_confidence:.2f} < "
                f"{_CONFIDENCE_ROUNDUP_THRESHOLD})"
            )
        else:
            rationale += (
                f"; escalated to {severity.name} "
                "(diagnosis confidence is uncalibrated)"
            )
    if unknown_telemetry:
        rationale += "; partial unknown fields present"
"""
content = content.replace(c4, r4)

with open("sre_agent/severity_engine.py", "w") as f:
    f.write(content)
