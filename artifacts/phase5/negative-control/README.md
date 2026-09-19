# Phase 5 negative control — the validator against the immutable baseline

A verifier that cannot fail is worthless. This is the real
`portal.validator`, run from published automation code
(`449edb36e6a3742f445c65107c6ad16cfaa94f67`, clean checkout) against the
isolated baseline stack of `../bootstrap/`, where both defects are known to be
present. The baseline must fail the two target contracts and pass every
control.

```bash
./run.sh          # writes the four JSON reports next to it
```

| report | exit | verdict | what it shows |
|---|---|---|---|
| `baseline.json` | 1 | failed | S2 and S1 target contracts fail; N1 and all controls pass |
| `blocked-web-unreachable.json` | 2 | blocked | no web service: no answer, so no verdict |
| `blocked-mcp-unreachable.json` | 2 | blocked | MCP readiness fails before S1 runs |
| `blocked-bad-credentials.json` | 2 | blocked | the restricted login fails: not a denial, an authentication failure |

Exit codes are `0` passed, `1` product-contract failed, `2` blocked.

## What failed, exactly

```
S2.new_exploration_does_not_reuse_a_discarded_key: expected False, observed True
S2.discarded_link_stays_dead_after_a_new_exploration: expected 404, observed 200
S1.an_omitted_row_limit_keeps_the_saved_value: expected 137, observed 1000
```

Those three lines are the only thing the feedback loop is allowed to quote
back to a repair session.

## What held

- **S2 setup** — create A, read A back byte-for-byte, discard, immediate 404,
  create B: all succeeded, so the failures are not setup artefacts.
- **S2 controls** — a second workspace gets its own key and its own state; a
  save in the same context *without* discarding updates in place and serves
  the latest state. A fix that simply stops reusing keys breaks these.
- **S1 controls** — the requested sort change persisted, the omitted palette
  survived, an explicit `row_limit` of 275 was applied, an explicit `1000`
  (indistinguishable from a reset unless it is checked) was applied, and a
  new chart still got the schema default.
- **N1** — the authenticated Gamma user lists charts (200) and is refused both
  the exploration-state write and chart data with 403. A 401 anywhere in this
  case blocks instead of passing: a lost session is not a permission control.

## What this does not show

No repaired code ran. There is no passing target contract anywhere in Phase 5,
and none is fabricated: the first genuine pass has to come from a Phase 6
API-created repair PR, verified through `portal.verification` against a
candidate stack built from that PR's head commit.
