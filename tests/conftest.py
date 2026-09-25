import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _package_root in (_REPO_ROOT, _REPO_ROOT / "src", _REPO_ROOT / "evals"):
    if str(_package_root) not in sys.path:
        sys.path.insert(0, str(_package_root))
