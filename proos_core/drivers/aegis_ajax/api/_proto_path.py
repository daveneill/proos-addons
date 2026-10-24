"""Ensure the proto directory is on sys.path for generated stub imports."""

import sys
from pathlib import Path

_proto_path = str(Path(__file__).parent.parent / "proto")
if _proto_path not in sys.path:
    sys.path.append(_proto_path)
