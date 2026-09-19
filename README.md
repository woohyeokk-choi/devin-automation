# devin-automation

Automation controller for the **Devin Runtime Repair** project: a synthetic-data
analytics portal backed by a fork of Apache Superset, where failing user actions
become deduplicated incidents that drive API-created Devin repair sessions and
independent verification.

Target repository: [`woohyeokk-choi/superset`](https://github.com/woohyeokk-choi/superset)
(baseline `394bca55c792b7b3547e23f6e175a7cb0f0757e8`).

The single living plan — decisions, phase status, commands and blockers — is in
[docs/EXECUTION_PLAN.md](docs/EXECUTION_PLAN.md).

Status: Phase 0 (planning). No application code yet. `AUTO_REPAIR_ENABLED=false`.
