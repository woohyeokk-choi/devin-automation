# Phase 5 bootstrap — published automation code, clean checkouts

`run.sh` clones both repositories from scratch and runs the stack from what is
actually pushed: no working-tree overlay is copied in, so `checkout_dirty` is
`false` on both sides in `provenance.json`. The Phase 4 run under
`../../phase4/bootstrap/` could not say that; its correction note explains why.

| | measured |
|---|---|
| automation | `c7ba960acd5d6646a623bad5f0e516a7b5040536`, clean |
| Superset | `394bca55c792b7b3547e23f6e175a7cb0f0757e8` (`baseline-394bca5`), clean |
| Compose project | `phase5check`, ports 8288 / 5208 / 8290, loopback only |
| cold build | 3m54s; web healthy 30s later |
| MCP container | `healthy` |
| fixtures | 600 rows, `sha256:67ff039835f9890c` |

Existing environments (`superset`, `bootstrapcheck`) kept running throughout;
this project has its own network, volumes, database and image.

## The MCP container's `unhealthy` status

The MCP sidecar inherits `docker/docker-healthcheck.sh` from the Superset
image, which asks the *web* application for `/health`. The MCP process is a
different listener in a different container and answers no such route, so the
probe failed forever while the service itself was fine — Phase 4 recorded that
as an open question.

`stack/docker-compose.ports.yml` replaces it with a probe in the protocol the
service actually speaks: an MCP `initialize` against `127.0.0.1:5008/mcp`,
required to return a `serverInfo`. `mcp-health.json` is the resulting Docker
health record (`healthy`, exit code 0).

Readiness is also checked independently of any scenario by
`portal.validator.mcp_readiness`, which performs `initialize` then
`tools/list`. Against this stack it reported:

```json
{"ready": true,
 "server": {"name": "Superset MCP Server", "version": "3.4.7"},
 "protocol_version": "2025-06-18",
 "tools": ["get_instance_info", "health_check", "search_tools", "call_tool"]}
```

S1 runs only after that succeeds, and records the server identity and the
discovered tool list in its own facts — see `../negative-control/baseline.json`.

## Re-running

```bash
AUTOMATION_SHA=<pushed sha> ./run.sh      # ~4 min cold, idempotent
```
