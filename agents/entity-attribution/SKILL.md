# Entity Attribution Agent

**Purpose:** for one tenant, find where the **user**, **session** and **entity** identifiers live in its API traffic, prove each rule on live spans, and hand over platform-ready GraphQL payloads plus a review file. The longest user journey that can be stitched from those identifiers is the measure of success.

It runs two stages in order. Each has its own `SKILL.md` with the full procedure, commands and quality gates.

| Stage | Folder | Reads | Writes |
|---|---|---|---|
| 1. Profile | [`stages/profile`](stages/profile/SKILL.md) | The tenant's spans | `profile.json`: business, API lanes, auth carriers, PII, traffic shape, anchors and bridge APIs |
| 2. Attribution | [`stages/attribution`](stages/attribution/SKILL.md) | `profile.json` and the tenant's spans | `<Tenant>-<env>-attribution.json` (payloads) and `...-review.json` (evidence) |

## Rules that hold for both stages
- **Generic.** No tenant, API, header or key names in tools or skills. Everything is measured.
- **Use every carrier.** Headers, cookies, JWT claims, request and response bodies, query parameters. A key need not appear on every API, and an anti-forgery token is still a valid key if it links requests.
- **No raw values leave the cluster.** Tools output counts and hashes only.
- **No default windows.** The window is named by the run, or taken from the profile.
- **One id space.** A field that names a person in another id space is never made a user rule, because it would give the same person a second user id. It can still link journeys through the key graph.
- **A rule is written only after it extracts on live spans.** Everything rejected is listed with a reason in the review file.

## Run order
1. Run the profile stage for the tenant and check `profile.json` with `python3 contracts/validate.py`.
2. Run the attribution stage with that profile. It discovers carriers, builds the key graph, derives rules, verifies them on live spans and packages the output.
3. Check the output with `python3 contracts/validate_attribution.py <file>` and `python3 contracts/validate_defs.py <dir>`.

## Done when
Both validators pass, every rule in the payload file has been verified on live spans, and the review file lists each excluded or unmatched carrier with its reason.
