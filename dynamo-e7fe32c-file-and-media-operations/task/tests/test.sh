#!/bin/bash
verification_root="$(mktemp -d)"
trap 'rm -rf "$verification_root"' EXIT
cp -al /app/input "$verification_root/input"
cp -al /app/evidence "$verification_root/evidence"
# Agent artifacts may be bind-mounted on a different filesystem in CI, where
# hard-linking fails with EXDEV. Copy this small directory portably instead.
cp -a /app/output "$verification_root/output"

FONT_REVIVAL_APP_ROOT="$verification_root" \
    pytest -p no:cacheprovider --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA

if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
