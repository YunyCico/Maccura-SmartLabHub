from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"

for path in (PROJECT_ROOT, BACKEND_DIR):
    path_string = str(path)
    if path_string not in sys.path:
        sys.path.insert(0, path_string)
