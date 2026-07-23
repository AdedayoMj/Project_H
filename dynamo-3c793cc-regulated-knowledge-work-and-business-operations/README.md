# T&E Reimbursement Audit

This task asks an agent to compute exact reimbursements and compliance dispositions for one executive's international trip against a written expense policy.

## Inputs

`task/environment/input/` is baked into `/app/input/` in the container and includes:

- `itinerary.json`
- `policy.json`
- `per_diem_rates.json`
- `expense_lines.csv`
- `fx_rates.csv`
- `vat_reclaim_table.json`
- `mileage_rates.json`
- `distance_matrix.csv`
- `counterfactual_fares.json`

## Verification

`task/tests/test_outputs.py` checks that:

- `/app/output.json` exists, is a JSON object, and is not a symlink
- `/app/input/` matches the golden copy byte-for-byte
- every output line matches the reference output exactly
- the trip summary matches the reference output exactly

