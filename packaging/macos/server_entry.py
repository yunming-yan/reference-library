"""Keep system/user Python paths isolated while adding this application's modules."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import runpy

repo = Path(__file__).resolve().parents[2]
app = repo / "app"
sys.path.insert(0, str(app))
runpy.run_path(str(app / "server.py"), run_name="__main__")
