# Defensible ESI Cull

## One-sentence problem

The task is done when `/app/output.json` contains the unique, complete, and auditable production manifest, privilege log, exclusion report, and volume summary required by the ESI protocol without changing `/app/corpus/` or `/app/input/`.

## Success criteria (numbered, mirror instruction.md)

1. Apply every normative rule in `/app/input/protocol.json` using the custodian roster and attachment-family manifest, while leaving `/app/corpus/` and `/app/input/` unchanged.
2. Emit `production` in protocol order with exactly the required sequence, path, media, custodian, family, hash, effective-date, size, and volume fields.
3. Emit the UTF-8-byte-ordered `privilege_log` with every directly or family-withheld item and all direct trigger paths.
4. Emit the UTF-8-byte-ordered `exclusions` with every non-produced item, its protocol disposition, and the elected representative for duplicates only.
5. Emit a reconciled `summary` containing the four totals, every disposition counter, and accurate ordered volume byte/item totals.
6. Place every inventoried item exactly once in `production` or `exclusions`, place every privileged exclusion exactly once in `privilege_log`, and preserve all normative array ordering.

## Calibration results

- Golden solve.sh: reward 1.0
- Bad / nop solution: reward 0.0

## How to run

```bash
harbor run -p . --agent oracle   # reward 1.0
harbor run -p . --agent nop      # reward < 1.0
```

## Notes / open questions

No unresolved interpretation remains. `/app/input/protocol.json` is normative for signature and disposition precedence, archive-depth semantics, date handling, normalized hashing, representative election, and volume packing. Physical corpus ZIPs are depth 0, their direct members are depth 1, and `maximum_depth` is inclusive. UTF-8 and UTF-16LE signature BOMs are excluded before text decoding. Exact path comparisons use the UTF-8 bytes of the preserved corpus or synthetic archive path; JSON object-key order and whitespace are immaterial.
