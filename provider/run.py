#!/usr/bin/env python3
"""Start the HAMA provider: python3 run.py  (same as python3 -m hama_provider)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hama_provider.__main__ import main  # noqa: E402

main()
