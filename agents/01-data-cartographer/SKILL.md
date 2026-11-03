# Agent 01 — Data Cartographer

**Purpose:** Understand the business and the data before anyone hunts. Answer seven questions about the tenant, with numbers, and keep the answers in one **living tenant profile**. Agent 02 and every later agent read that profile instead of guessing.

**Cadence:** this agent runs **weekly** on the **last 7 complete days** of traffic (the window ends yesterday) and **updates** the tenant profile. It does not start from scratch and it does not overwrite blindly (see "The living profile"). Agents that run more often read the current profile and never run the cartographer themselves.

## Inputs

- `tenant_id`, `environment` (picked upstream)
- `window_end` = the last complete day (yesterday); `window` = the 7 days ending then. A first run on a tenant with less than 7 days of real data uses what exists and says so.
- The previous profile for this tenant and environment, if any
- Read access to `iceberg_datalake.span_event_view` via Trino
- Profiling tables (`api_profiling_*` in `iceberg_gcs`), if they exist. They are a fast hint only (see "Profiling rule")

## The living profile (output)

The tenant's fundamentals change slowly. They are kept as **one profile that each weekly run updates**, not as a new report each week.

```
profiles/<tenant>-<environment>/
    profile.json      CURRENT contract. Agents 02+ read only this. Same shape as any handoff (contracts/README.md).
    profile.md        CURRENT evidence report (sections below), rewritten each run from the updated profile
    runs/<window_end>/
        handoff.json  what this run alone measured (immutable)
        report.md     this run's evidence (immutable)
        delta.json    what changed against the previous profile
```

- `profile.json` is the **contract**. `profile.md` and `runs/` are evidence and history. Do not hand a later agent a markdown file as its input.
- Every entry and table row in `profile.json` keeps `first_seen` and `last_confirmed` (window end dates), and `changed_in` when a run changed it.
- Every run **re-measures everything**. Then it merges:
  - **Unchanged** (same value inside tolerance): keep the id, set `last_confirmed` to this window.
  - **Changed:** replace the value, set `changed_in`, keep the old value in `delta.json`.
  - **New** (new API, key, auth type, PII field): add it with `first_seen`.
  - **Gone:** an API not seen in 7 days stays in the profile marked `seen_days: 0` and `last_confirmed` frozen. Remove it only after 4 runs absent. Never delete on one quiet week.
- **Stable** facts (naming quirks, auth shapes, PII masking, keys, business summary) change rarely: a change is a finding, say it in section 0. **Volatile** facts (volumes, caller counts, lanes, hunt verdicts) are expected to move: record the new value and the previous one.
- `profile_version` goes up by 1 every run. `prior_runs` counts earlier runs of this profile; the `delta` table is required when it is above 0. On a first run write `n/a` (not null) for `week_over_week`. `fact.profile-run` carries it, the window, and `baseline_valid` (below).
- If this run's window is a `ramp`, `decline` or `step`, **do not replace volumes, baselines or lanes** with this window's numbers. Update only the stable facts, carry the previous volatile values forward, mark them `stale`, and set `baseline_valid` to false.

Two files per run, as before:

1. `handoff.json`: the short contract the next agent must read in full. Shape and rules: `contracts/README.md`. Required entries and tables: `contracts/schemas/01-data-cartographer.json`. The merged result is `profile.json`.
2. `report.md`: the evidence for this run. Every contract entry points to a section here. Do not open another tenant's report while you work. Every value comes from this tenant's spans.

Write the report first, then the contract. Before you finish, run:

```bash
python3 contracts/validate.py <handoff.json>
python3 contracts/validate.py <profile.json>
```

Fix every failure. Do not hand off a contract that fails. Agent 00 will not start agent 02 until it passes.

Nothing is left out for being "too detailed". No high-level summaries in place of numbers. Every claim has the count behind it. Use plain words.

Fixed sections, in this order:

| # | Section | What goes in it |
|---|---|---|
| 0 | Verdict | 10–12 lines: what a reader must know before touching the data |
| 1 | The business | What the company does, who calls it, **every API** with: what it does, callers, daily volume, status mix. Data horizon. 1.5 names inferred for unnamed spans. 1.6 clubbed names split by URL. 1.7 naming quirks. 1.8 one card per API |
| 2 | Junk traffic | What to drop or ignore, each with the evidence and a keep/drop verdict (see goal 2) |
| 3 | Data issues | Per-API coverage table of every field; missing headers, cookies, bodies; what each gap blocks |
| 4 | Auth and tokens | Every auth type, how it looks, whether it decodes, machine or user, how to get the user out of each |
| 5 | Masked and unmasked PII | Field-by-field, API-by-API |
| 6 | Traffic distribution | Who sends the traffic, how concentrated, whether it is worth hunting |
| 7 | Hand-off to agent 02 | Candidate keys with measured cardinality, tests agent 02 must run, the filters every downstream query must use, internal-IP list |
| 8 | Open items | What this run could not answer, and why |

## Window and traffic shape (do this first)

**Window:** the last 7 complete days. Never include today (partial). Record it in `filter.window` and in the envelope.

**Chunking:** a one-shot query over 7 days does not finish on a large tenant. Pull **one day per query set** and merge:

```bash
tools/pull.sh <customer_id> <environment> <window_end> 7 <out_dir> [slice_hours]   # one folder per day; a rerun skips finished files.
# A busy tenant's full-day query can take ~10 min or stall: pass slice_hours (e.g. 6) to split each day into <day>/sHH/ folders.
# merge_window.py adds the slices back together; distinct counts (IPs, UAs, paths) then take the largest slice, a lower bound, so say so in the gaps.
python3 tools/merge_window.py --dir <out_dir> --out <out_dir>/merged    # typical-day view + daily.csv
python3 tools/shape_window.py --daily <out_dir>/merged/daily.csv [--prior <previous profile's daily.csv>]
```

Distinct counts (IPs, users) cannot be added across days, so the merge uses the **typical day** (the median-volume day) for per-API numbers, and the busiest day for an API missing from it. It also gives `days_seen` and window spans per API. Per-day numbers are what the lane thresholds were set for. Heavy per-day queries still run as separate queries (see Trino tips); if one day does not finish, split that day by hour for the span counts only.

**Shape check:** before any API work, decide what the 7 days are. `shape_window.py` gives the class and the numbers; copy them into `fact.traffic-shape`:

| Class | Meaning | The window is a baseline? |
|---|---|---|
| `continuous` | Daily spread under 15% (cv), no trend, no jump | Yes |
| `weekly-cycle` | Weekends differ from weekdays by 20% or more | Yes, but compare with the same weekdays, not the whole week |
| `ramp` | Last 3 days at least 1.3x the first 3 and 4%+ a day | No |
| `decline` | Last 3 days at most 0.77x the first 3 | No |
| `step` | A day is 3x or more (or a third or less) of the day before: onboarding, outage, migration, test traffic ending | No |
| `irregular` | Noisy with no trend or pattern | No: widen to 14 days |

- One week cannot prove **seasonality**. Say `weekly-cycle` only with the weekend ratio, and state that confirmation needs the previous run's window (`week_over_week`). After 4 runs, the profile may record a longer cycle (monthly, quarterly) from its own history.
- Also read the hourly shape (`series.csv`) of the typical day: a flat or reversed day/night curve on a consumer API is itself a finding.
- If the shape is not `continuous` or `weekly-cycle`, find out why before anything else (a new customer onboarded, a release, an outage, test traffic) and write it in section 0. Set `baseline_valid` false in `fact.profile-run`. Agents that build baselines must not use this window.
- Compare with the previous profile: `week_over_week` volume, new and vanished APIs, lane changes. These go in `delta.json` and in section 0.

## The seven goals (goal 7 is listed first below, because it feeds goal 1)

### 1. Understand the business
- Read the API names, hosts, services, user agents, and sample bodies. Say in plain words what the company sells and who calls it.
- List **every** API (not just the top ones): purpose, callers (partner server, consumer app, browser, scanner), spans per day, status mix, distinct IPs, distinct clients.
- Say what the "customer" and the "victim" are in this data (an app? a person? an account? a device?) and what abuse is even possible.
- Find the **real data horizon**: daily spans, apis, and IPs over the full retention. Mark where real volume starts. Test traffic before that is not a baseline.

### 7. Name every API and map its business value (goal 1 detail)
Do this for **every** API, small ones too. Never skip an API as "too small for a card".
- **Unnamed spans** (`api_name IS NULL`; check NULL, not `''`): group by span kind, service, backend, method, URL pattern. Look at the URL, headers, request and response bodies, then infer a name and its business value. Match each group to a named API by method + path first (most are gateway egress copies of a named request). Report the match rate. Whatever does not match (cloud metadata calls, rejected requests, base-path calls) gets its own inferred name and business value. Keep real-actor groups in the map; drop plumbing with a count.
- **Clubbed names** (one name, many URLs: `{id}` placeholders, names built from a GraphQL operation, spelling variants): list the URLs under the name, tier them (by volume, callers, status), infer a business value per tier, and map each separately.
- **Naming quirks** go in section 1.7: NULL vs empty, names from different sources, placeholders that hide objects, variants that split one API, versions of one function, host not in the name, scheduled jobs that make equal daily counts, what `session_id` means.
- **Per-API card** in section 1.8 for every API: business value, name quality, host, URL-pattern count, volume and callers, status mix and why it errors, auth scheme and decode result, `user_identifier` shape, coverage of body/header/cookie fields, origin concentration, PII (raw or masked, per field), hunting verdict. A card with a blank field is not done.

### 2. Find the junk traffic
Junk = not a real request, not expected to be abused, or abuse we could not detect because of missing data. For each item write: what it is, the count, the evidence, and **keep or drop**.
- **Duplicates and plumbing:** blank `api_name` spans, gateway egress legs, metadata calls, health checkers, monitoring IPs. Compare the count to the named APIs to see if they double-count.
- **Internal APIs:** use `api_is_external` and `is_ip_external` together. Drop only when both say internal. Do not regex IPs. Do not use a null ASN alone.
- **Tiny or test APIs:** list them. Keep them in the census (a low-volume write on money, identity or messaging is still in scope) but give them no statistics.
- **APIs with many 4xx or 5xx errors:** per API, status mix on the full day. For each, read the **response bodies** (if present) and say *why* (rate limit, bad input, bad token, overload, not found). Then say whether the API is useful for hunting. A high error rate from the platform itself (503, status 0) is noise. Errors spread over many IPs or many identifiers are signal.
- **Probes are junk and also attacks:** scanners, GraphQL introspection, password guessing. Keep them in their own lane.

### 3. Find the data issues in the APIs
For each API, measure the share of spans that have: request body, response body, `user_identifier`, `session_id`, `authorization`, cookies, query params, path params, geo, user agent.
- **Headers:** list the keys present per API with distinct counts. Say which are missing that would matter (client-IP headers, correlation ids, device ids, referer, origin). Say which are per-request and dead as keys.
- **Cookies:** which APIs have any, which names, whether they can join calls into a user journey. Say plainly when none exist.
- **Span bodies:** missing request or response bodies, and why (GET, error only, platform drops them).
- **Columns that mislead:** trace ids that are ~1 per span, flags that are never set, `session_id` that is per request.
- Say what each gap **blocks** (for example: "no cookies, so no journey view on partner APIs").

### 4. Find the auth headers and tokens
- Per API, count spans by scheme (Basic, Bearer, API key, none, other). Check shape and length (hex, JWT, base64, opaque). Check whether any value decodes.
- If values are JWTs: decode the header and payload (never store the signature), list the claims, say which claim names the user, which the machine, the issuer, the scope, the expiry.
- If values are hashed or opaque: say so, show the cardinality (distinct values per span), and say whether the value is a client key, a token key, or a per-call key.
- **Machine or user:** decide per auth type, with the evidence (grant type, the shape of `user_identifier`, source IPs, user agents).
- **Multiple auth types:** do each one separately. For each: how it looks, how to decode it, how to pull out the user or client, what you get.
- **Token chain:** if the tenant has no token-issuing API, write `decision.token-chain` as not applicable, with the evidence. Otherwise, if tokens are issued by a token API, test whether the issued token appears later in `authorization` (same hash or value). If it joins, measure the join rate and the share where the issuing client equals the calling client. This turns a resource call into its token request.
- Check request-body credentials (`username`, `password`, `client_id`, `client_secret`, `grant_type`, `scope`, `login_hint`). Count grant types.
- What is `user_identifier` really: an end user, an app, or empty? Show the shapes and counts.

### 5. Find PII that is masked, and call out each one
- Sample request and response bodies on every API. Per field: raw or masked (`***`, hash, tokenised)?
- Name each field and each API. Do not write "some PII is masked". Format: "<field>: raw in A and B, 64-hex hash in C, D".
- State the effect: raw = usable for hunting; hashed = can count and link by equality but cannot recognise or look up; masked = unusable.
- Warn about false detections (the platform's datatype tag fires on hashes as "password"). Do not copy raw values into the report: shapes and counts only.
- Say which entities (person, device, account, or any business id you found) can and cannot be derived.

### 6. Understand traffic distribution and consistency
- Per API: distinct IPs, top-1 and top-10 IP share, organisation and connection type mix, countries, user-agent families.
- Decide per API: is this one origin, a few partner servers, or real internet and user traffic?
- Check consistency: daily volume, weekday vs weekend, hourly shape, distinct IPs per day.
- Say which surfaces are worth hunting on, and what kind of hunt fits (per IP, per client, per person key, per token).
- Rule to record: on an API with only partner servers, an IP spike is a partner change, not an attack.

## Lane and verdict rubric (for the `apis` table)

Decide each API's `lane` from numbers you measured for this tenant. Never from the API name alone, and never copy another tenant's lanes. Write the numbers in the `evidence` column (example: "3 IPs, client credentials, top-1 IP 0.7, 2% 4xx"). A row with no numbers fails validation.

Measure these for every API first: spans per day, distinct IPs, distinct client keys, top-1 IP share, error share, auth present (yes/no and scheme), cookies present, body present, user-agent mix, share of callers in internal or cloud ranges.

| Lane | Pick it when | Verdicts allowed |
|---|---|---|
| `probe` | Mostly 4xx or 404, many distinct paths from few clients, scanner user agents, or paths that are not in the API's own routes | `probe-lane` |
| `health` | No auth, no body, fixed path, steady timing, no business action | `drop` |
| `internal` | Under 5% of spans have an external API *and* an external IP (`api_is_external`, `is_ip_external`), or it is the gateway's own second-leg copy | `drop` |
| `tiny` | Under 100 spans a day (absolute floor, not a share of the tenant: on a small tenant a share would swallow real partner APIs) | `baseline-only`, `drop` |
| `consumer` | 1000+ callers (or 1%+ of the tenant's IPs) with mixed user agents, cookies or ids | `hunt`, `baseline-only` |
| `partner` | Few callers with client credentials or tokens, traffic concentrated per client | `hunt`, `baseline-only` |
| `other` | Fits none of the above. The evidence must say why | any |

Check in this order and stop at the first match: probe, health, internal, tiny, consumer, partner, other.

**Burstiness and payload source** (tool output, added to every `apis` row). `burst_ratio` = the window's busiest minute for the API over its median active minute (`burst.csv`, pulled per day; the peak is the max over all days, so a burst that missed the typical day still shows). `bursty` is true at 20x or more with at least 50 spans in the peak minute (`--burst-ratio`). A bursty API is a campaign, a batch job or a partner flush: say which in the report and do not let its peak set a baseline. `payload_source` is `end-user` (consumer lane), `partner-feed` (partner lane), `internal`, or `unknown`. On a `partner-feed` or `internal` API the body fields come from a machine, not from the person who owns them: downstream agents must not read those fields as end-user entities, or count them against the end-user population.

Pick the verdict inside the allowed set with three questions. Write the answers in `evidence`:
1. Is there a usable key (a client, device or person key from the key census)?
2. Are there enough callers or spans to build a baseline?
3. Does the API do something worth abusing for this business (money, identity, access, data lookup)? This one needs your judgment of the business. Say it in one line.

If any answer is no, use `baseline-only`. Then set `hunt_worthy` in the `distribution` table to match: `hunt` needs `yes`, `limited` or `per-client only`.

### Tool and scale

Lanes and verdicts come from `tools/`, not by hand. Nothing in `tools/` names a tenant or an API.

```bash
python3 tools/assign_lanes.py --dir <out_dir>/merged --auth-usable yes|no --out <out_dir>/apis.json [--q3 <out_dir>/q3.csv]
```

- `--auth-usable`: `yes` only when goal 4 found an auth value that is a stable client or user key. Decide it from the auth analysis, not by default: `no` makes every API without a stable client key look machine-driven, and agent 02 then sees real session carriers (a session header, a signed cookie) as machine-lane. If goal 4 found any per-user credential in a header or cookie, even an opaque one, say `yes` and name the carrier in the report. A per-user token carried by a gateway or app server on behalf of end users is the other trap: write it in the report under auth, so the lane of the APIs that carry it is read as "server-side caller for end users", not as a machine.
- Q3 rules: `tools/q3_default.csv` holds business-neutral capability rules (credentials, money, account data, messaging, admin, file upload, webhooks, schema discovery, static and telemetry). It always applies. Your optional `--q3` file holds this tenant's rules and is checked first. Write it after the business read (goal 1), only for API families the defaults miss or get wrong.
- An API that matches no rule gets Q3 `unknown` and `baseline-only`. The tool lists those APIs, heaviest first, in `summary.q3_unmatched`. Read every one above 0.1% of spans and either write a rule or say in the report why it stays unknown.
- Thresholds are flags with the rubric values as defaults (`--tiny-spans 100`, `--consumer-ips 1000`, `--min-callers 5`). Change one only with a reason in the report.
- Scale: a tenant with 600 named APIs or fewer lists every API. A bigger one lists every API at or above 0.01% of the day's spans and groups the rest into `long_tail` families (table `long_tail`, required when `fact.api-count.tail_apis` > 0). Put the counts in `fact.api-count`.
- The tool gives lane, verdict, `evidence` and `hunt_worthy`. `function`, `business_value` and `pii` are yours, written from the spans of this tenant.

## Profiling rule

Profiling tables are a hint, not a source.
- They may let something in (rank an API, shortcut a lookup). They may **never** keep something out. Every drop needs a raw-span check.
- Check the table's last-built day. If older than about 7 days, or it covers less than 80% of live APIs (anti-join: live APIs in spans minus APIs in profiling), call it advisory-only in section 8 and take every number from spans.
- Re-measure old claims ("bodies are empty", "field is masked", "API is internal"). Do not inherit them.

## Procedure

0. **Window and shape.** Pull the 7 days in chunks, merge, classify the shape (see above). Load the previous profile.
1. **Horizon.** `min`, `max`, `count(*)`, daily spans, apis, IPs over the full retention. Never assume retention. The 7-day window is inside it; real data may start inside the window (that is a `step`).
2. **Profiling freshness** (rule above). Decides if you use it as a hint at all.
3. **API list.** `api_name` with spans, distinct IPs, distinct `user_identifier`, status mix, for the full real day. Include blank `api_name` as a bucket and explain it.
4. **Business read.** From names, hosts, UAs, and a few bodies per API.
4b. **Name every API.** Unnamed spans, clubbed names, cards (goal 7). Generate the cards from data (merge the coverage, status, auth and caller outputs), plus hand-written business value per API.
5. **Junk and errors.** Status mix per API. Read error bodies (`substr` the body, group by the error message). Check duplicates, probes, health checkers, tiny APIs.
6. **Coverage table.** One row per API, share of spans with each field. Run on the full day for small APIs (a 10% sample hides them). Use a sample only for the largest APIs, and say so.
7. **Key census.** `CROSS JOIN UNNEST(request_headers)` (also cookies, path_params, query_params). Per key: spans, `approx_distinct(v)`. Use `element_at(map,'key')`, never `map['key']`.
8. **Auth.** Scheme counts, shape and length checks, decode attempts, `user_identifier` shape per API, grant types from bodies, token-chain join.
9. **Masking.** Body samples per API, regex shape counts (10-digit, E.164, 64-hex, `***`, email, lat/long). Name every field.
10. **Distribution.** IP concentration, org and connection-type mix, UA families, countries, daily and hourly shape.
11. **Quirks.** Is `ip_address` the real client or an edge? Check `x-forwarded-for`, `cf-connecting-ip`, `x-real-ip`, `true-client-ip`. What does `session_id` mean per API (distinct sessions vs spans)? IPv6 present (use /64)? Do enrichment flags (`ip_is_vpn/proxy/bot`, org, connection type) populate?
11b. **Anchors and bridges** (feeds agent 02's journey stitching). Run on one window you name (no default window):
    `python3 tools/anchors.py --customer <uuid> --env <env> --start 'YYYY-MM-DD HH:MM' --minutes <n> --profile profiles/<tenant>-<env>/profile.json [--apis <apis.json>]`
    It adds three things to the profile, from aggregates and hashes only:
    - `anchors` table: every value that names a person. Sources are the `user_identifier` column, JWT claims decoded from bearer-shaped headers, and **every request-body, response-body and query-parameter field on every API** whose name can name a person (top level and one level nested, e.g. `data.userId`). Do not limit the field scan to login calls: server-side, back-office and pre-login calls often name the user only in a body. Path parameters are not scanned, because the platform's attribution rules cannot read them. A value is `person` when it equals `user_identifier` on ≥ 90% of the spans that carry both. Where the span has no `user_identifier`, it is `person` when ≥ 90% of its distinct values appear as `user_identifier` values in the window (the same id space). A person-named field below that may hold an id from another id space (an internal customer number). It stays `person-candidate`, and the evidence says so. `person-candidate` means person-shaped but not proven; `not-person` covers the rest.
    - `bridge_apis` table: per API, its role (`bridge` carries reusable token-shaped cookies or headers; `issuer` sets cookies before any anchor exists; `anchored`), plus its carriers (reusable cookies, headers and URL query parameters) and issued cookie names.
    - `fact.journey-window`: the window measured. Agent 02 reuses it for its key graph.
12. **Verify.** Pull 5 raw spans of a known API and confirm the report matches them.
13. **Write the file.** Sections 0–8. Fill section 0 last.

## Trino tips

- `element_at(map,'key')` for map reads. Use `strpos(x,'…')>0` (there is no `contains` for strings).
- Heavy full-day scans can exceed 2 minutes: run them in the background and wait for them. Run big ones as separate queries.
- Use `approx_distinct`, and group by the smallest set of columns.

## Quality gates

- The window is the last 7 complete days, pulled one day at a time. `fact.traffic-shape` has the class and its numbers.
- `profile.json` was merged from the previous profile (not rewritten from nothing), has `profile_version` + 1, and `delta.json` lists every change.
- Stop if `span_event_view` has zero spans for the tenant. (Empty profiling tables are not a stop.)
- All seven goals have their own section with numbers. A section with no numbers is not done.
- Every API is listed once in section 1, even the tiny ones, and has a card in 1.8.
- Unnamed spans are named by URL and body, with a match rate. No "blank bucket, drop" without it.
- Every name that clubs more than one URL is split and mapped by business value.
- Naming quirks are written down in 1.7.
- Every drop decision has a raw-span count. No drop on profiling alone.
- Every PII field is named. Every masked field is named.
- Every API row has a lane and verdict chosen with the rubric, with numbers in `evidence`. No lane from the name alone.
- Every claim about a quirk has a query result behind it.
- No raw PII values, tokens, or secrets in the file (shapes and counts only).

## Embedded lessons

- A column named like the client IP is often the CDN or gateway IP. The real client may be in a forwarded header.
- `session_id` can mean one request, one token, or one login, and it can differ per API in one tenant.
- Native trace ids are usually ~1 per span and are useless as journey keys.
- Masking is uneven: a field can be raw in one API and hashed in another.
- `user_identifier` is often an app id, not a person. Check the shape before trusting it.
- Bearer hashes are often one per call. They are token keys, not user keys, but they may join back to the token request.
- A blank API name can be half the table. Find out what it is before counting anything. It is usually the gateway's egress copy of a named request: it keeps user agent, auth and bodies but loses client IP and country, so it can never name a caller.
- Names built from a GraphQL operation or an `{id}` placeholder club many URLs. Spelling and encoding variants split one API into several names. Look at the URL, not just the name.
- A sample (10%) hides small APIs. Use the full day for them.
- The platform's datatype tags are noisy. Check against the body.

## Done when

`profile.json` passes `validate.py`, and agent 02 can read it (and `profile.md` to check a claim) and know: whether this week is a valid baseline, what the business is, what to drop, which fields are missing, what each auth value is and whether it can be decoded, which PII is usable, and where real users or partners sit in the traffic. Nothing in it is a guess.
