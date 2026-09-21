# Integrated baseline run through the real candidate-stack path

This run is the one the live verifier takes. The coordinator called
`IsolatedStack.prepare()`, which cloned the commit into its own checkout,
started its own Compose project, seeded it, measured provenance, and then the
pinned validator replayed S2, S1 and N1 through that stack. Nothing here was
prepared by hand, and no `run.sh` wrapper stood in for the code path.

- automation ref: `58ab5be503be453f02eb3a9506c63759e1106202`, published, clean
  tree, fresh checkout at `/home/ubuntu/phase5-integrated/devin-automation`
- Superset commit under test: `394bca55c792b7b3547e23f6e175a7cb0f0757e8`
  (the immutable baseline; no product code was modified)
- Compose project: `candidate394bca55c792`, ports 8388 (web) / 5308 (MCP)
- result: `baseline-result.json`, exit code 1

Exit code 1 is a product-contract failure, not a blocked run. `provenance_problem`
was empty: the checkout was clean and at the tested commit, both containers ran
the candidate source (equal `code_hash` inside each container and on the host),
the MCP sidecar reported `healthy`, and the fixture was measured through the
chart-data API as 60 grouped rows — the region/channel/product revenue
aggregate of the 600 seeded records, not the records themselves.

## What the validator found

| case | verdict | notes |
| --- | --- | --- |
| S2 | failed | 2 target assertions fail; every setup and both normal-reuse controls pass |
| S1 | failed | omitted `row_limit` resets 137 → 1000; palette, explicit limit, schema-default limit and new-chart default all pass |
| N1 | passed | authenticated restricted role is denied the write and the data read with 403, and can still list |

```
S2.new_exploration_does_not_reuse_a_discarded_key: expected False, observed True
S2.discarded_link_stays_dead_after_a_new_exploration: expected 404, observed 200
S1.an_omitted_row_limit_keeps_the_saved_value: expected 137, observed 1000
```

This is the negative control: the validator catches the three known baseline
defects while every control passes, so a later pass on a repaired commit means
something. No repaired commit exists yet — Phase 6 owns the first genuine one.

## Two defects this run exposed in the automation, since fixed

- `SupersetClient.find_dataset` read only the first page of `/api/v1/dataset/`.
  A freshly built stack also carries the example datasets, so the fixture fell
  off page one and provenance blocked with "synthetic_orders is not present".
  The lookup now filters by name. The gate behaved correctly: it refused to
  validate a stack whose fixture it could not measure.
- Teardown left the checkout behind. The container compiles bytecode as root,
  and the host user cannot unlink it, so the next attempt on the same commit
  would inherit a half-deleted tree. A container now removes what a container
  wrote. After this run the workspace, the Compose project and its volumes are
  all gone.
