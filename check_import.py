"""Build-time smoke test: fail the build loudly if the app cannot even import.

Run from render.yaml's buildCommand after `pip install`. A failing build makes
Render keep the previously deployed (working) version live instead of
replacing it with one that crashes on start ("Worker failed to boot", exit
status 3). Needs no database and no network.
"""
import sys
import traceback

try:
    import greenlet  # noqa: F401  (SQLAlchemy's async engine needs it; not pulled in by every SQLAlchemy release)
    import app.main  # noqa: F401
except Exception:
    traceback.print_exc()
    print("IMPORT_FAILED: the app cannot start with the installed packages / files (see traceback above)")
    sys.exit(1)

print("import_ok")
