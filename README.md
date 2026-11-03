# Blue Agent

A set of agents that turn a tenant's API traffic into reusable outputs. The first agent, **entity attribution**, finds where the user, session and entity identifiers live and writes platform-ready GraphQL rule payloads plus a review file.

Agents run independently. An orchestrator reads the plan, checks what each agent needs and how fresh it is, and tells you what to run next.

## Layout
```
SKILL.md                      orchestrator entry point
orchestrator/                 plan.json (the dependency graph) and run.py (status, due, record)
agents/entity-attribution/    stages/profile and stages/attribution, each with SKILL.md and tools/
contracts/                    versioned artifact schemas and validators; the only coupling between agents
shared/                       generic code used by more than one agent (empty until needed)
evals/                        scoring code and frozen baselines, no customer data
docs/architecture.md          rules, node states, how to add an agent
```

## Working today
- **entity-attribution**: [profile](agents/entity-attribution/stages/profile/SKILL.md) then [attribution](agents/entity-attribution/stages/attribution/SKILL.md). Standard library Python 3 and read access to Trino through `kubectl`.
- **orchestrator**: reports which node is `blocked`, `stale`, `due` or `fresh`, and records a run only if its output passes the contract check.

```bash
python3 orchestrator/run.py status --tenant <tenant-uuid> --env <env>
```

## Not built yet
- The database. The run ledger is a local JSON-lines file until one is chosen.
- The recurring loop: a per-API ledger, gap-driven windows, new-API detection and misfire detection.
- Other agents. Each is added following [docs/architecture.md](docs/architecture.md).
- The orchestrator does not launch agents, it only reports and records.

## Rules
Tools and skills never name a tenant or an API. No raw values leave the cluster. Tenant data never enters this repo.
