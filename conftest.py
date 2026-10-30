"""Root pytest conftest.

lib/test_resolver.py and lib/test_validator.py import their targets as
bare `from resolver import ...` / `from validator import ...` (not
`from lib.resolver import ...`), which only resolves if lib/ itself is on
sys.path. Pytest's rootdir-based import of lib/__init__.py (lib is a
package) does not add lib/ itself to sys.path, only the repo root -- so
collecting the full suite from the repo root fails without this. This
file exists solely to make `pytest` runnable from the repo root across
every test file at once (needed to catch cross-file schema drift per
project policy), not to change any test's behavior.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))
