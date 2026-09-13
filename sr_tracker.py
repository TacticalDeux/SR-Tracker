#!/usr/bin/env python3
"""Entry-point shim. Use `python -m srt.app` or `python sr_tracker.py`."""
import sys

# Velopack hook handling (install/update/uninstall) must run before
# anything else. No-op on unpacked dev runs; never break startup.
try:
    import velopack
    velopack.App().run()
except Exception:
    pass

from srt.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
