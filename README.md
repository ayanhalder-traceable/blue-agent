# Entity Attribution Agent

Finds, for one tenant, where the **user**, **session** and **entity** identifiers live in its API traffic, and writes them as platform-ready GraphQL rule payloads plus a separate review file.

It is two agents that run in order:

| Agent | What it does | Output |
|---|---|---|
| `agents/01-data-cartographer` | Profiles the tenant: business, API lanes (consumer / partner / machine), auth carriers, PII, traffic shape | `profile.json` (the "living profile") |
| `agents/02-key-evaluator` | Reads that profile and derives user attribution, session identification and entity rules from span data; proves each rule on live spans before packaging | `<Tenant>-<env>-attribution.json` (payloads) and `...-review.json` (evidence) |

Both are written to work for any tenant: nothing in the tools or skills is tenant-specific.

## How to run

Each agent's procedure, inputs, quality gates and exact commands are in its `SKILL.md`:
- [`agents/01-data-cartographer/SKILL.md`](agents/01-data-cartographer/SKILL.md)
- [`agents/02-key-evaluator/SKILL.md`](agents/02-key-evaluator/SKILL.md) (see "Attribution definitions")

Agent 02 uses `profile.json` from agent 01, so run 01 first.

Check an output with the validators in `contracts/`:
```bash
python3 contracts/validate_attribution.py <Tenant>-<env>-attribution.json
```

## Requirements

- Python 3 (standard library only).
- Read access to the tenant's span data through Trino (the tools run `trino` via `kubectl exec` on the read-only coordinator).
- `TRACEABLE_TOKEN` in the environment, only if you refresh `platform_shapes.json` (read-only introspection). The token is never written to disk.

## Privacy

The tools output counts and hashes only; no raw user values leave the cluster.

## Status and roadmap

Working today: the two agents above, producing payload files.
Not built yet:
- Writing the output to a database.
- A recurring loop that tracks which APIs are covered, picks windows for the gaps, finds new APIs, and flags rules that misfire after a schema change.

Other agents will build on this output; they are not part of this repository yet.
