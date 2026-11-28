#!/usr/bin/env python3
"""Prints the value columns of phase_line that exist in ClickHouse but are no longer generated from segments.json.
argv[1] is "table.column,table.column" (Nullable columns of vllm_pd.phase_line)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from segtree import Tree  # noqa: E402
import generate  # noqa: E402

tree = Tree()
known = {"phase_line." + c for c, _ in generate.line_columns(tree)}
known |= {"phase_line.http_status", "phase_line.first_mono_ns", "phase_line.api_start_ts", "phase_line.api_end_ts",
          "phase_line.core_mono_ns"}
print(" ".join(c for c in sys.argv[1].split(",") if c and c not in known))
