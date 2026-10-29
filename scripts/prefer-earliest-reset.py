#!/usr/bin/env python3
"""Reconcile the saved dashboard routing choice using account priorities."""
from pathlib import Path
import sys
root = Path(__file__).resolve().parent
sys.path.insert(0, str((root.parent if root.name == 'scripts' else root) / 'dashboard'))
from routing_policy import *
if __name__ == '__main__':
    raise SystemExit(main())
