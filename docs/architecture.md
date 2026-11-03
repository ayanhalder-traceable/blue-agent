# Architecture

## Shape
One repo. `orchestrator/` decides what runs and when. `agents/<name>/` are self-contained agents. `contracts/` holds the versioned artifact schemas and validators and is the only coupling between agents. `shared/` holds generic code used by more than one agent.

## Agents run independently
Agents do not run as one pipeline. Each declares in `orchestrator/plan.json` what it needs (a contract and a maximum age) and what it produces. The orchestrator runs whatever is ready.

States per node: `never-run`, `blocked` (an input is missing or too old), `stale` (an input was produced after the last run), `due` (its schedule has elapsed), `fresh`.

## Rules
1. Agents talk only through contracts. No agent imports another agent's code.
2. Every agent can run alone. Inputs come only from declared contracts.
3. A run counts only after its output passes the contract check (`run.py record`).
4. Contract versions are backward compatible within a major version. A breaking change bumps the major version.
5. Tenant data never enters the repo.

## Adding an agent
1. Create `agents/<name>/` with `SKILL.md`, `tools/` and the contract it produces.
2. Add the contract schema and validator to `contracts/`.
3. Add the node to `orchestrator/plan.json` with its needs, produces and schedule.
4. Add `evals/<name>/` with a baseline.

## Open decisions
- Database for the handoff (until chosen, the ledger is `tmp/store/runs.jsonl`).
- Whether the orchestrator should also launch agents. Today it only reports and records.
- CI: `docs/ci-checks.yml` is the check workflow. Copy it to `.github/workflows/checks.yml` once the GitHub login has the `workflow` scope (`gh auth refresh -s workflow`).
