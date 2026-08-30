import re

text = open("sre_agent/act_phase.py").read()

def replacer(m):
    head = m.group(1)
    master = m.group(2)
    
    # 1. imports
    if "from typing import" in head or "from .confidence_calibration" in master:
        return head + "\n" + master.replace("from .executor import Executor\n", "")
    
    # 2. _as_float, _as_int
    if "def _as_float" in head:
        return head + "\n"
        
    # 3. fields
    if "unknown_telemetry: bool = False" in head:
        return head + "\n" + master + "\n"
        
    # 4. annotations / severity_label
    if "annotations =" in head:
        return head + "\n" + master + "\n"
        
    # 5. agent_results / measured
    if "# Prefer structured metrics" in head:
        master_calib = re.search(r'(    # Raw reflector confidence.*?    \))', master, re.DOTALL).group(1)
        return head + "\n" + master_calib + "\n"
        
    # 6. IncidentSignals init
    if "error_rate=measured" in head:
        head_fields = head.strip()
        master_conf = re.search(r'(        hypothesis_confidence=.*)', master, re.DOTALL).group(1)
        head_fields = re.sub(r'        hypothesis_confidence=.*?\n', '', head_fields + '\n')
        return head_fields + "\n" + master_conf + "\n        evidence=links,\n"
        
    # 7. decide_plan
    if "aggregate, per_action = decide_plan(" in head:
        return head.replace("evaluate_fn", "evaluate_fn,\n        calibrated_action_probability,\n        minimum_autonomy_probability,") + "\n"
        
    # 8. ActReport init
    if "unknown_telemetry=assessment.unknown_telemetry," in head:
        return head + "\n" + master + "\n"
        
    # 9. apply_skill_learning signature
    if "state: Any, report: ActReport, store: Any = None" in head:
        return master + "\n"
        
    # 10. apply_skill_learning imports
    if "record_successful_remediation," in head:
        return head + "\n" + master + "\n"
        
    # 11. apply_skill_learning body
    if "proposed = propose_skills(" in head:
        return master + "\n"

    return "<<<<<<< ours\n" + head + "\n=======\n" + master + "\n>>>>>>> theirs\n"

new_text = re.sub(r'<<<<<<< ours\n(.*?)\n=======\n(.*?)\n>>>>>>> theirs\n', replacer, text, flags=re.DOTALL)

with open("sre_agent/act_phase.py", "w") as f:
    f.write(new_text)

