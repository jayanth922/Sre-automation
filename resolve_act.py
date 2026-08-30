import sys

def parse_conflicts(text):
    import re
    pattern = re.compile(r'<<<<<<< HEAD\n(.*?)\n=======\n(.*?)\n>>>>>>> master\n', re.DOTALL)
    
    def repl(m):
        head = m.group(1)
        master = m.group(2)
        
        # Conflict 1
        if "unknown_telemetry: bool = False" in head:
            return head + "\n" + master + "\n"
            
        # Conflict 2
        if "annotations =" in head and "severity_label =" in master:
            return head + "\n" + master + "\n"
            
        # Conflict 3
        if "# Prefer structured metrics" in head:
            # We want both, but master redefined agent_results. We'll extract master's calibration logic.
            # Master's part:
            import re
            master_calib = re.search(r'(    # Raw reflector confidence.*?    \))', master, re.DOTALL).group(1)
            return head + "\n" + master_calib + "\n"
            
        # Conflict 4
        if "error_rate=measured" in head:
            # Merge fields
            head_fields = head.strip()
            # find master's calibrated lines
            import re
            master_conf = re.search(r'(        hypothesis_confidence=.*)', master, re.DOTALL).group(1)
            
            # replace head's hypothesis_confidence with master's
            head_fields = re.sub(r'        hypothesis_confidence=.*?\n', '', head_fields + '\n')
            return head_fields + master_conf + "\n        evidence=links,\n"
            
        # Conflict 5
        if "aggregate, per_action = decide_plan(" in head:
            return head.replace("evaluate_fn", "evaluate_fn,\n        calibrated_action_probability,\n        minimum_autonomy_probability,") + "\n"
            
        # Conflict 6
        if "unknown_telemetry=assessment.unknown_telemetry," in head:
            return head + "\n" + master + "\n"
            
        # Conflict 7
        if "def apply_skill_learning(" in head:
            return master + "\n"
            
        # Conflict 8
        if "from .verified_learning import" in master:
            return head + "\n" + master + "\n"
            
        # Conflict 9
        if "proposed = propose_skills(" in head:
            return master + "\n" + head + "\n"

        return "<<<<<<< HEAD\n" + head + "\n=======\n" + master + "\n>>>>>>> master\n"

    new_text = pattern.sub(repl, text)
    return new_text

text = open("sre_agent/act_phase.py").read()
new_text = parse_conflicts(text)
with open("sre_agent/act_phase.py", "w") as f:
    f.write(new_text)

