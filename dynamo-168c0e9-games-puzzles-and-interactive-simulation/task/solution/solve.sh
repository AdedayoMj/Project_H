#!/bin/bash
set -euo pipefail
mkdir -p /app/output
cp /solution/solve.py /app/output/solver.py
python3 /app/output/solver.py
