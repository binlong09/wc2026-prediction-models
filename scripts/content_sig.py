"""Content signature of the committed, published text artifacts.

Used by the refresh-backtest workflow's commit-on-change gate. Hashes the durable
state CSVs (deterministic) plus companion.json with its `generated_at` timestamp
removed (that changes every run). A real data change moves the signature; a no-op
run — only a fresh timestamp — does not.

Lives in its own file (not an inline heredoc) so the workflow YAML stays valid:
unindented Python inside a `run: |` block scalar breaks the YAML parser.

Stdlib only — runnable with a bare `python`.
"""
import glob
import hashlib
import json
import os

parts = []
for path in sorted(glob.glob("data/state/*.csv")):
    with open(path) as f:
        parts.append(f.read())

companion = "report/companion.json"
if os.path.exists(companion):
    with open(companion) as f:
        data = json.load(f)
    data.pop("generated_at", None)
    parts.append(json.dumps(data, sort_keys=True, ensure_ascii=False))

print(hashlib.sha256("\n".join(parts).encode()).hexdigest())
