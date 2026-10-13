"""Run isolated Reference Library maintenance from the bundled Python runtime."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Avoid the script's basename shadowing the repository maintenance package.
sys.path.remove(str(Path(__file__).resolve().parent))
from maintenance import main
sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == '__main__':
    raise SystemExit(main())
