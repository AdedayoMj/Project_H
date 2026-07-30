Construct the minimal self-contained preservation package for the approved VFX cut in `/app/input/cut.json` from the contaminated repository at `/app/repository/`. `/app/input/policy.json` is the normative specification: it defines the other input ledgers, path and format rules, drop-frame and audio arithmetic, dependency composition, evidence and rename resolution, revision-group election, canonical paths, exclusions, ordering, source-copy equivalence, archive safety, and the exact output schemas. Do not modify `/app/repository/` or `/app/input/`.

Write all five artifacts required by the policy:

- `/app/package.tar`, containing exactly one regular entry at each selected canonical path with the selected source bytes. TAR entry order, header format, modes, ownership, and timestamps are non-normative.
- `/app/selection.json`, containing every and only selected logical asset with its elected revision, content, source representative, and coalesced active ranges.
- `/app/provenance.json`, containing all cut roots and every effective dependency edge with coalesced record-frame ranges.
- `/app/exclusions.json`, accounting exactly once for every inventoried non-selected repository entry under the policy's disposition precedence.
- `/app/validation.json`, reconciling the cut, audio ranges, selection, exclusions, dependency completeness, and archive.

All JSON schemas and array ordering are exact as specified in `/app/input/policy.json`; do not add fields. JSON object-key order and whitespace are immaterial. A permitted byte-identical source representative is an equivalence choice, but every other selection, range, edge, revision, disposition, canonical path, and packaged byte is exact.
