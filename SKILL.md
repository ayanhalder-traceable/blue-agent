# Blue Agent (orchestrator)

Blue is a set of agents that each turn a tenant's API traffic into one reusable output. They share contracts, not code, and they do not have to run together. This file is how you decide what to run.

## Steps
1. Ask the orchestrator what needs to run for the tenant and environment:
   `python3 orchestrator/run.py due --tenant <T> --env <E>`
   Use `status` instead to see every node and why it is in its state.
2. For each node it lists, in the order printed, open that node's `skill` file from `orchestrator/plan.json` and follow it.
3. When the node's output exists, record it. The record only goes in if the output passes its contract check:
   `python3 orchestrator/run.py record <node> --tenant <T> --env <E> --output <contract>=<path>`
4. Run `due` again. Stop when nothing is listed.

A node in state `blocked` is waiting on an input that is missing or too old. Run the node that produces that input first.

## Agents today
| Agent | Nodes | Produces |
|---|---|---|
| [entity-attribution](agents/entity-attribution/SKILL.md) | `profile`, `attribution` | `profile`, `attribution` |

More agents are added by following `docs/architecture.md`.

## Rules
- Tools and skills are generic. No tenant, API, header or key names.
- No raw values leave the cluster. Counts and hashes only.
- Tenant data never goes into this repo.
