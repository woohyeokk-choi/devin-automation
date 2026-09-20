# PREVIEW — the defect in Superset's own chart (not merged)

These frames are a **preview of the unmerged repair**. The baseline frames were
captured against the **pinned baseline**
`394bca55c792b7b3547e23f6e175a7cb0f0757e8`, on the builder's isolated loopback
demo (`http://127.0.0.1:8088`, compose project `superset`), not on the repair
session's VM and not on any deployed environment. The candidate frame was
captured against PR #6's head commit in a separate isolated stack; it is a
preview of an **open, unmerged** pull request and is not post-merge evidence.

Captured `2026-09-20T00:43Z` (aggregate attempt), `2026-09-20T00:49Z`
(raw-records baseline) and `2026-09-20T01:07Z` (candidate preview).

## Why a second chart exists

The canonical S1 incident (saved `row_limit` 137 reset to 1000 by a sort-only
update) is unchanged, and so are its fixtures and the independent validator. It
groups by `region`, which has four values, so the reset is invisible in the
rendered table — correct as a contract check, useless as a demonstration.

The presentation chart exists only in this isolated demo. It is the **same
defect**, chosen so a viewer can see it. It is not a new product issue, no
repair was dispatched for it, and no Superset source was modified to produce
it.

## The scenario shown: raw records (chart id 9)

`Order revenue - 10-row view (presentation)` on `public.synthetic_orders`,
`query_mode = raw`, columns `id, region, channel, product, revenue`, saved row
limit 10, ordered by `revenue`. The user action is the same sort-only MCP
`update_chart` call the portal uses, carrying `sort_by` and **no `row_limit`
key at all**.

Measured in Superset's Explore UI, not inferred:

| | row limit | rows rendered | ordering |
| --- | --- | --- | --- |
| before | 10 | 10 | `revenue [asc]`, 10 → 19 |
| after a sort-only update | 1000 | 600 | `revenue [desc]`, 509 → … |

600 is the measured row count of the whole table, read from Superset's own
`600 rows` badge; the demonstration never assumes it. The requested ordering
did change, so the only thing this frame attributes to the defect is the lost
row limit.

1. `03-raw-records-before-limit10-10rows-asc.png`
2. `04-raw-records-after-sort-only-limit1000-600rows-desc.png`

A short replay of exactly this baseline sequence was recorded on the builder VM
after detection and posted to the incident thread as Slack file
`F0C2XU6V475`. It is a later replay of the RAW presentation chart, not the
original canonical S1 discovery and not post-merge footage; the earlier custom
portal clip `F0C324CM3KQ` stays in the thread alongside it.

## The same scenario on candidate PR #6 (preview, unmerged)

Same dataset, same raw-records chart, same sort-only MCP call with no
`row_limit` key, run against head
`7bb8de7b136f9afdc39d31b4c6809b3de467c461` in the isolated stack
`candidate7bb8de7b136f` (`http://127.0.0.1:8488`). Both containers report that
exact commit from a clean tree, measured per service.

| | row limit | rows rendered | ordering |
| --- | --- | --- | --- |
| before | 10 | 10 | `revenue [asc]`, 10 → 14 |
| after a sort-only update | 10 | 10 | `revenue [desc]`, 509 → 503 |

Superset's own Explore UI agrees with the readback: row-limit control `10`, a
`10 rows` badge, `Ordering: revenue [desc]`
(`candidate-7bb8de7b136f-05-after-sort-only.png`). The full record, including
the measured provenance document, is
`candidate-7bb8de7b136f-raw-presentation.json`.

This is a **supplemental visual check**, deliberately separate from the
registered verification: the 13 registered S1/N1 checks and the 137-row fixture
are untouched by it.

## The aggregate attempt, and why it is not the scenario

The first attempt (`Top 10 sales segments (presentation)`, chart id 7,
`region × channel × product`, `SUM(revenue)`, limit 10) showed the same lost
limit — 10 rows at limit 10 became 60 rows at limit 1000 — but the requested
ascending sort never reached the rendered table.

That is a **separate, unrepaired behaviour**, not part of this incident and not
addressed by PR #6: in aggregate mode `plugin-chart-table`'s `buildQuery`
rebuilds `orderby` from `timeseries_limit_metric`/`order_desc` (falling back to
the first metric, descending), the table control panel only exposes
`order_by_cols` in raw mode, and the MCP `map_table_config` emits `sort_by` as
`order_by_cols`. Demonstrating the row-limit defect on top of that mismatch
would wrongly suggest the pull request fixes rendered sort order. Frames
`01-…` and `02-…` are kept as the record of that finding.

## Frontend provenance

The light stack ships no compiled assets, so they were built from the pinned
source with the repository's own Docker target
(`docker build --target superset-node`) and mounted into the running web
container. No upstream change, no replacement frontend, no tunnel.

The candidate stack serves that same bundle, and may: PR #6 changes only
`superset/mcp_service`, so `superset-frontend` at
`7bb8de7b136f9afdc39d31b4c6809b3de467c461` and at the baseline are the same
tree object (`ece7678a1c2b5a6d1f8c76039c3f83a9c9a312b4`). The equality is
recorded in the candidate JSON; assets are reused only where that identity
holds, never copied across differing trees.

## What is still missing

The post-merge counterpart — limit 10 held, 10 rows rendered, requested sort
applied, captured at the merged SHA — cannot exist until a human merges PR #6
and the retained deployment is rebuilt from GitHub's merge commit. Nothing here
may be relabelled as that evidence.
