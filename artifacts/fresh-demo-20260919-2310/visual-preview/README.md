# PREVIEW — the defect in Superset's own chart (baseline, not merged)

These two frames are a **preview of the unmerged repair** and prove nothing about
candidate PR #6. They were captured against the **pinned baseline**
`394bca55c792b7b3547e23f6e175a7cb0f0757e8`, on the builder's isolated loopback
demo (`http://127.0.0.1:8088`, compose project `superset`), not on the repair
session's VM and not on any deployed environment.

Captured `2026-09-20T00:43Z`.

## Why a second chart exists

The canonical S1 incident (saved `row_limit` 137 reset to 1000 by a sort-only
update) is unchanged, and so are its fixtures and the independent validator. It
groups by `region`, which has four values, so the reset is invisible in the
rendered table — correct as a contract check, useless as a demonstration.

`Top 10 sales segments (presentation)` (chart id 7, `public.synthetic_orders`,
dimensions `region` × `channel` × `product`, metric `SUM(revenue)`, row limit
10) exists only in this isolated demo. It is the **same defect**, chosen so a
viewer can see it. It is not a new product issue, no repair was dispatched for
it, and no Superset source was modified to produce it.

## Measured, not assumed

| | row limit | rows rendered |
| --- | --- | --- |
| before | 10 | 10 |
| after a sort-only update | 1000 | 60 |

The dataset yields 60 groups at limit 1000 (measured by query, not inferred).
The update sent `sort_by` only, with no `row_limit` key, over the same MCP
`update_chart` path the portal uses. The requested ascending sort was also not
applied — the table still descends from 2.99k.

1. `01-native-explore-before-limit10-10rows.png`
2. `02-native-explore-after-sort-only-limit1000-60rows.png`

## Frontend provenance

The light stack ships no compiled assets, so they were built from the pinned
source with the repository's own Docker target
(`docker build --target superset-node`) and mounted into the running web
container. No upstream change, no replacement frontend, no tunnel.

## What is still missing

The post-merge counterpart — limit 10 held, 10 rows rendered, requested sort
applied, captured at the merged SHA — cannot exist until a human merges PR #6
and the retained deployment is rebuilt from GitHub's merge commit. Nothing here
may be relabelled as that evidence.
