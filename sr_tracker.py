#!/usr/bin/env python3
"""Entry-point shim. Use `python -m srt.app` or `python sr_tracker.py`."""
import sys
from srt.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
