import re

content = open("sre_agent/policy_gate.py").read()
new_content = re.sub(
    r"<<<<<<< HEAD\n=======\nimport math\n>>>>>>> master\n",
    r"import math\n",
    content
)
with open("sre_agent/policy_gate.py", "w") as f:
    f.write(new_content)
