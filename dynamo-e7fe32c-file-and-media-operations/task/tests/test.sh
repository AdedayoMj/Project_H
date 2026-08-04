#!/bin/bash
verification_root="$(mktemp -d)"
trap 'rm -rf "$verification_root"' EXIT
# CI may bind-mount inputs, evidence, and agent artifacts on a filesystem that
# differs from /tmp. Portable copies avoid cross-device hard-link failures.
cp -a /app/input "$verification_root/input"
cp -a /app/evidence "$verification_root/evidence"
cp -a /app/output "$verification_root/output"

FONT_REVIVAL_APP_ROOT="$verification_root" \
    pytest -p no:cacheprovider --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA

if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
