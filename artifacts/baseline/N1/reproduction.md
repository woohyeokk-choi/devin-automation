# N1 (control) — expected permission denial

Status: **PASS** — the denial is preserved.

## User action

`restricted_analyst` (role `Gamma`, no access to the synthetic dataset) performs
the same explore-state write as S2.

## Steps

1. `POST /api/v1/explore/form_data?tab_id=552266` as the restricted user →
   **HTTP 403**
2. `GET /api/v1/chart/` as the same user (an action the role *is* entitled to) →
   HTTP 200

## Expected vs observed

- Expected: 403 (or equivalent denial) with no incident and no repair path
- Observed: HTTP 403 on the restricted explore write

## Pipeline contract

This event must be classified `expected_denial`: it is a correct authorization
outcome, not a defect. No incident is created, no issue is opened and no repair
session is started. Authorization and CSRF protection were left enabled for the
whole run — the only change was creating a low-privilege user.

## Evidence

- `transcript.json` — sanitized requests/responses (no cookies, no CSRF tokens).
- `result.json` — machine-readable result and source revision.
