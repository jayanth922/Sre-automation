import re

text = open("sre_agent/act_phase.py").read()

# Conflict 1
c1 = """<<<<<<< HEAD
=======
from .confidence_calibration import (
    ConfidenceCalibrationError,
    calibrate_confidence,
    load_calibration_artifact,
)
from .executor import Executor
>>>>>>> master
"""
r1 = """from .confidence_calibration import (
    ConfidenceCalibrationError,
    calibrate_confidence,
    load_calibration_artifact,
)
"""
text = text.replace(c1, r1)

# Conflict 2
c2 = """<<<<<<< HEAD
def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
"""
text = re.sub(r'<<<<<<< HEAD\n(def _as_float.*?\n)\n?=======\n(.*?)\n>>>>>>> master\n', r'\1\2\n', text, flags=re.DOTALL)

# Conflict 5
c5 = """<<<<<<< HEAD
        actions, assessment, env, risk_score, evaluate_fn
=======
        actions,
        assessment,
        env,
        risk_score,
        evaluate_fn,
        calibrated_action_probability,
        minimum_autonomy_probability,
>>>>>>> master"""
r5 = """        actions,
        assessment,
        env,
        risk_score,
        evaluate_fn,
        calibrated_action_probability,
        minimum_autonomy_probability,"""
text = text.replace(c5, r5)

# Conflict 7
c7 = """<<<<<<< HEAD
    state: Any, report: ActReport, store: Any = None
) -> Dict[str, Any]:
    \"\"\"Self-improving loop (project #2): propose prior skills, record this one.
=======
    state: Any,
    report: ActReport,
    store: Any = None,
    *,
    verification_outcome: Any = None,
    live_results: Any = None,
    incident_status: Any = None,
    reviewer_id: Optional[str] = None,
    run_manifest_sha256: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    \"\"\"Self-improving loop: propose prior skills; record only verified successes.
>>>>>>> master"""
r7 = """    state: Any,
    report: ActReport,
    store: Any = None,
    *,
    verification_outcome: Any = None,
    live_results: Any = None,
    incident_status: Any = None,
    reviewer_id: Optional[str] = None,
    run_manifest_sha256: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    \"\"\"Self-improving loop: propose prior skills; record only verified successes."""
text = text.replace(c7, r7)

# Conflict 9
text = re.sub(r'<<<<<<< HEAD\n(\n    proposed = propose_skills.*?    )\n=======\n(.*?)>>>>>>> master\n', r'\2\1', text, flags=re.DOTALL)

with open("sre_agent/act_phase.py", "w") as f:
    f.write(text)

