# shared

Code that two or more agents need. Nothing here names a tenant, an API or a header.

| Folder | Holds | Status |
|---|---|---|
| `trino/` | Query runner, windowing, counts-only guard | Empty. Each agent's tools run their own queries today. Move the common runner here when the second agent needs it. |
| `platform/` | GraphQL payload builders, API id resolution | Empty. Lives in `agents/entity-attribution` today. |
| `store/` | Read and write layer for the database | Empty. Waiting on the database choice. `orchestrator/run.py` uses a JSON-lines ledger until then. |

Rule: code moves here only when a second agent needs it, never in advance.
