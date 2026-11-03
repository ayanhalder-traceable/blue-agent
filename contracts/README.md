# Agent hand-off contracts

How one agent hands its work to the next, so the next agent cannot skim past it.

## The idea in one minute

Every agent writes **two things** for the next agent:

| File | Who reads it | How long |
|---|---|---|
| `handoff.json` (the **contract**) | The next agent. It must read all of it. | Short. Only decisions, rules, and tables it has to act on |
| `report.md` (the **evidence**) | Anyone who wants to check a claim | As long as needed |

Every item in the contract points to the exact part of the report that proves it. The next agent reads the contract in full and opens the report only to check something.

The next agent then writes a third file, `ack.json` (the **acknowledgement**), saying what it did with each item. The orchestrator (the orchestrator) refuses to start a stage until the earlier stage's contract and acknowledgement both pass `validate.py`.

```
agent N  ──writes──►  handoff.json + report.md
                          │
agent N+1 ──reads all of handoff.json──►  writes ack.json (used / overridden / not-applicable, with a reason)
                          │
the orchestrator runs:  python3 contracts/validate.py handoff.json --ack ack.json   (must pass)
```

## Where files live

```
runs/<tenant>-<env>/<NN-agent>/
    handoff.json     the contract (this agent's output)
    report.md        the evidence
    data/            csv and other files the report cites
    ack/<NN-reader>.json    how each later agent used this contract
```

(For a one-off evaluation the folder can be `Customer Evaluations/`; the file names stay the same.)

## The same shape for every agent

A contract has an **envelope** and two kinds of content: **entries** and **tables**. This is the only structure. Agents differ only in which entries and tables they must fill, and that is written in `schemas/<agent>.json`.

### Envelope (always the same)

```json
{
  "contract": "blue-agent/handoff@1",
  "agent": "profile",
  "tenant": "...", "environment": "...",
  "window": {"from": "2026-10-22", "to": "2026-10-30"},
  "source": "spans",                      // spans | profiling | mixed
  "produced_at": "2026-10-31",
  "report": "path/to/report.md",
  "entries": [ ... ],
  "tables":  { "<table name>": { "columns": [...], "rows": [ ... ] } }
}
```

### Entries: one decision, rule, or fact each

```json
{
  "id": "exclude.egress-copy",
  "kind": "exclude",
  "statement": "Drop unnamed spans (api_name IS NULL). 99.95% are gateway egress copies of a named request.",
  "value": {"spans_per_day": 10618404, "share": 0.5},
  "evidence": {"files": ["data/f_blank_paths.csv"]},
  "confidence": 0.9,
  "consumers": ["attribution", "03-view-builder"]
}
```

| Field | Rule |
|---|---|
| `id` | Unique, stable, `kind.short-name`. The next agent cites this id |
| `kind` | One of the closed list below. No other kinds |
| `statement` | One or two plain sentences. What to believe or do |
| `value` | The numbers or the exact SQL. Machine-usable. Required for `filter` and `exclude` |
| `evidence` | `files` = the data files behind it. Required on every entry |
| `confidence` | A number from 0 to 1: how sure the agent is. About 0.9 = a query result sits behind it; about 0.6 = reasoned from measured facts (say from what); under 0.5 = no data, must also appear as an `open_item` |
| `consumers` | Agent ids that must act on it and answer it in their `ack.json`. Empty only for `fact` entries meant for humans |

### The closed list of entry kinds

| Kind | Means | Example |
|---|---|---|
| `fact` | A measured truth about the tenant | "Real data starts 2026-10-22" |
| `decision` | A call the agent made that later agents build on | "`user_identifier` is a client id, not a person" |
| `filter` | A condition every later query must apply. `value.sql` is exact | base tenant, env, date filter |
| `exclude` | Traffic to drop, with count and evidence. `value.sql` is exact | the egress copy |
| `lane` | Traffic to keep apart and handle separately | scanner probes |
| `gap` | Data that is missing or masked, and what it blocks | "no cookies on partner APIs → no journey view" |
| `risk` | A way this output can mislead the next agent | "a 10% sample hides small APIs" |
| `open_item` | Something the run could not answer, and why | "`caller of base-path calls unknown`" |

Tables hold lists of the same thing (all APIs, all keys, all masked fields). A table can list its `consumers`; each of them must acknowledge it as a whole.

## How the next agent proves it read the contract: `ack.json`

```json
{
  "contract_ref": "runs/.../profile/handoff.json",
  "agent": "attribution",
  "answers": [
    {"ref": "exclude.egress-copy", "status": "used", "where": "key-scorecard.csv header; every query"},
    {"ref": "decision.user-identifier-is-client", "status": "overridden", "reason": "tested: 3% of values are per-person"}
  ]
}
```

- `status` is `used`, `overridden` (a reason is mandatory and must cite a measurement), or `not_applicable` (reason mandatory).
- Every entry or table that lists the reader in `consumers` needs an answer. Silence fails the check.
- Every claim in the reader's own output that depends on the contract cites the entry id (for example "filter: filter.base").

## Rules the schemas follow

1. **Required slots, not free text.** `schemas/<agent>.json` lists the entry ids and tables an agent must produce. An empty required slot fails validation.
2. **Contract is small.** A table row or entry is a decision. Long reasoning stays in `report.md`.
3. **Evidence or it did not happen.** No entry without `evidence`.
4. **Numbers are measured or marked.** Entries under 0.5 confidence must also be `open_item`s.
5. **Each agent reads by id.** An agent's `SKILL.md` has a `## Reads` list of `<agent>:<entry id or table>` it needs. Nothing else is guaranteed to reach it.
6. **New agent = new schema file.** Adding or changing an agent never changes the envelope, the kinds, or the validator.

## Roadmap: what each agent's contract carries

| Agent | Main entries | Main tables |
|---|---|---|
| profile | filters, excludes, lanes, decisions on auth and keys, gaps, open items | apis, unnamed_groups, clubbed_names, naming_quirks, auth_types, keys, pii_fields, distribution, long_tail (when tail_apis > 0; `fact.api-count` keys: named_total, listed, tail_apis, tail_spans, unnamed_groups) |
| attribution | key ranking decisions, dead-key facts | keys (ranked, with tier and coverage) |

Only the profile stage has a schema file so far. The others follow the same steps: pick the entries and tables from their existing `## Outputs`, write `schemas/<agent>.json`, add a `## Reads` list to the next agent.

## Checking a contract

```bash
python3 contracts/validate.py <handoff.json>
python3 contracts/validate.py <handoff.json> --ack <ack.json> --reader attribution
```

## Attribution definitions

Agents 02 and 06 also write three platform-shaped files: `user-attribution.json`, `session-identification.json`, `entity-definitions.json`. They are not hand-off entries; they are the configuration the platform consumes. Their shape is in `schemas/definitions.json` and is checked by `validate_defs.py`:

```bash
python3 contracts/validate_defs.py <dir> [--verify verify.csv]
```

- Every definition carries `confidence` (a number from 0 to 1) and `evidence`. Every excluded machine carrier names the test that excluded it.
- `--verify` takes the `verify.csv` that `stages/attribution/tools/verify_defs.py` writes: a definition that extracts nothing on the window fails.
- Entities carry `source_agent`: `"02"` for the broad, threat-model-driven set, `"06"` for gap-fill only. An the entity gap-fill stage (not in this repo) definition must name the `capability` and `reason` it fills.
- The shapes are inferred from the platform's screens, not confirmed against an export.
