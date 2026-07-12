from __future__ import annotations

import sys

# Must happen before any other import in this package: `dispatcher.py`
# imports `lumibot` at module scope to read its `__version__` for `health`,
# and LumiBot itself prints an unguarded startup banner directly to stdout
# the moment it is imported (confirmed via manual smoke test: "LumiBot
# v4.5.74 starting"). stdout is reserved exclusively for paper-runtime.v1
# JSON Lines responses, so the *real* stdout is captured here, before any
# other import runs, and handed to `run()` explicitly — `sys.stdout` itself
# is then redirected to stderr so no later import or log call can pollute
# the protocol stream.
_real_stdout = sys.stdout
sys.stdout = sys.stderr

from .main import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run(stdout=_real_stdout))
