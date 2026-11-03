# Attribution stage (key evaluator)

**Purpose:** Determine — empirically, not by name — which keys actually stitch an actor's activity into journeys. Output is a ranked key hierarchy, not an assumption.

## Inputs

- `cartographer-report.md` from the profile stage: read section 3 (data gaps), 4 (auth, `user_identifier` shape, token chain), 6 (traffic distribution) and 7 (candidate keys, filters). Its key census is the starting list; its junk list (section 2) is excluded before scoring
- Any known-good reference journeys (e.g., a session chain already trusted on one surface)

## Outputs

| Artifact | Contents |
|---|---|
| `key-hierarchy.md` | Ranked keys with labels: **person** / **session** / **device** / **request-flow** / **fallback** / **dead** |
| `key-scorecard.csv` | Per key: coverage, cardinality, fan-out stats, coherence score, verdict |
| `user-attribution.json` | Platform-shaped **user attribution** definitions (scope, userId/userRole/userScope/authType, conditions, transformations) plus an `excluded` list of machine carriers with the test that excluded each |
| `session-identification.json` | Platform-shaped **session identification** definitions (Request or Response identifier, token location, transformations, `aliases`, `multiple_per_user`) plus `not_session` |
| `entity-definitions.json` | **Broad entity** definitions driven by the tenant's threat model (see Entity attribution). The entity gap-fill stage (not in this repo) fills gaps; it does not repeat these |
| `<Tenant>-<env>-attribution.json` | **The deliverable**: the three definition sets above plus their live verification, in one document (`tools/package.py`) |
| `carriers.csv` | Scorecard of every credential, session and identity carrier found: shape, cardinality class, consumer-lane share, caller match, verdict |

## The candidate classes to test

Anything in the census that could join spans:

- Columns: `session_id`, `user_identifier`, `trace_id`, `parent_span_id`, `client_session_ids`, `api_trace_id`
- Cookies: session cookies, auth cookies, clearance/visitor/analytics cookies
- Headers: `authorization` (bearer/JWT), correlation headers (`x-request-id`, `x-appgw-trace-id`, vendor trace headers), app-specific identity headers
- Composites: `client-ip+ua` (fallback only)
- Embedded IDs: tokens and cookies often CONTAIN a stable identifier inside a rotating opaque value — test the extracted inner segment separately from the full value. A `guid|…#<uid>#…` style cookie may yield a durable person key from the segment even while the full value rotates.

## Scoring procedure (per candidate key)

1. **Coverage + cardinality** — kills most candidates instantly:
   - distinct ≈ spans → per-request value, chains nothing (dead)
   - distinct ≈ 1-100 → constant/batcher/system value, not an actor key
   - coverage < 5% of spans → can't stitch a population
2. **Classify what the key *is* before scoring it** — cardinality tiers per key, per API:
   - ~1-100 distinct across many spans → **static app-key / shared credential** — identifies the integration, never the caller
   - thousands+ distinct, ~1 per user → **per-user token/credential** — actor candidate
   - thousands+ distinct, ~1 per device → **per-device id** — device-tier candidate
   - ≈ span count → per-request nonce — dead for identity
   Record the classification in the scorecard — a "key" that turns out to be a static app key explains false "authenticated" posture downstream.
3. **Caller-vs-target test (mandatory for user-shaped fields).** Decode per-user tokens (JWT `sub`, base64 segments, embedded id fields) and compare the decoded identity against `user_identifier`/`session_id`-style columns: count spans where they match vs differ. If they differ on the vast majority of calls, the column is the **target**, not the caller — inverting this is the single most damaging key mistake. Record the verdict per API.
4. **Seeded fan-out** — take top values by span count; per value measure:
   - spans stitched, distinct APIs, distinct session_ids
   - distinct client IPs (IPv6→/64 first), distinct UAs, distinct ASNs
   - time spread
5. **Coherence judgment:**
   - **Person key**: ~1-3 IPs (post-/64), 1-3 UAs, hours-long spread, many APIs — a plausible single human
   - **Session key**: shorter window, 1-2 IPs, fewer APIs
   - **Request-flow key**: millisecond spread, multiple services — bridges tiers, not people
   - **Device key**: survives across multiple person keys
6. **Ground-truth check** — where a trusted key exists, verify the candidate recovers the same span sets: does one candidate value cover exactly the sessions it should (precision), or drag in strangers (collision)?
7. **Population split** — check whether one key type mixes two populations. Human values chain many APIs + sessions; service values chain few APIs + zero sessions. A "person" key carrying services must not silently enter the human baseline.

## Quality gates

- Every label (`person`, `device`, …) must cite measured evidence, not plausibility — including the caller-vs-target verdict per API (caller, target, or ambiguous).
- Static app-keys are labeled `static-credential` in the scorecard — they're allowed as entity/service keys but banned from actor tiers.
- A key that aggregates a channel (gateway cookie, shared service token, receiver identity on inbound webhooks) must be marked **channel**, not actor — it groups a channel, not a caller.
- The hierarchy must end with an explicit coalesce order: which key wins when several are present, and what's the flagged fallback.

## Embedded lessons

- The best person key is often a session cookie's *embedded* user-id segment, not the raw value — extraction beats whole-value for both durability and debuggability.
- Per-request ids (native trace ids, edge ray ids, generic request ids) are request-flow tier only — useful for cross-tier stitching, useless for identity.
- Vendor trace headers can bridge tiers within one request (single-digit millisecond spreads across services) — but synthetic-monitor values in the same header must be excluded from journey logic.
- App-specific id headers are often entity/service keys, not person keys (one value can equal an entire backend service).
- `user_identifier`-style columns frequently hold partner tokens — aggregates of thousands of end-users, not individuals.
- The user-shaped field is the *target* more often than intuition suggests — count matches vs differs before trusting it as an actor key; a near-total mismatch means it's who the call is *about*, not who made it.
- A field called `authorization` with ~10 distinct values is a shared app key — "authenticated" posture on that API means nothing about who's calling.
- Basic-auth usernames are usually service accounts: they come from few IPs, hit internal-lane APIs, and repeat across huge span counts. A tenant whose only header credentials are Basic and opaque Bearer can legitimately end with zero header-based user definitions; the platform user column is then the person key.
- Opaque Bearer tokens (hex or random) are per-user but rotate on refresh: they make sessions, not durable person keys.
- Tenants differ most in what they mask. When `authorization` is masked, a JWT carried in a cookie is usually the next best person key: decode it and test `sub` against the user column.
- A JWT in the `Authorization` header is rarer than in cookies on browser-facing tenants; it shows up mostly on gateway and developer environments.
- Cookie names that embed a site or user id (`prefix-<id>`) can multiply into thousands of carriers; fold them before scoring or the session list drowns.
- Several headers often carry the same value (gateway copies). Identical spans, distinct count and IPs means one carrier with aliases, not several sessions.
- Response bodies issue tokens (`access_token`, `cartToken`-style, csrf) worth a `Response` identifier; they rank last until a later request is seen presenting them.

## Attribution definitions (user, session, entity)

The key hierarchy says which keys stitch journeys. The definitions turn that into the platform's attribution configuration. They are derived by rules, never by tenant: the same tools run on any tenant of any shape and size.

**Tools** (`tools/`, standard library plus `kubectl` access to Trino; aggregates only, no raw credential or user value is written):

```bash
python3 tools/discover.py --customer <uuid> --env <env> --start 'YYYY-MM-DD HH:MM' --minutes 5 --out <dir>
python3 tools/journey.py --customer <uuid> --env <env> --start 'YYYY-MM-DD HH:MM' --minutes <n> --dir <dir>   # key exploration: APIs linked per key value
python3 tools/keygraph.py --customer <uuid> --env <env> --profile <the profile stage profile.json> --dir <dir> [--collision-budget 0.02]   # chain of keys that stitches the longest person-anchored journeys
python3 tools/derive_defs.py --dir <dir> --env <env> --profile <the profile stage profile.json> [--apis <apis.json> fallback] --out <out>
python3 tools/entities.py --customer <uuid> --env <env> --starts '<YYYY-MM-DD HH:MM>' '<...>' '<...>' [--apis <the profile stage apis.json>] --out <out>   # entity-definitions.json + entity-groups.csv (replaces derive_defs' entity section)
python3 tools/verify_defs.py --customer <uuid> --env <env> --start 'YYYY-MM-DD HH:MM' --defs <out> --out <out>
python3 tools/api_ids.py --customer <uuid> --env <env> --start 'YYYY-MM-DD HH:MM' --minutes 60 --out <out>   # api_ids.csv
python3 tools/package.py --defs <out> --customer <uuid> --api-ids <out>/api_ids.csv --out '<Customer Evaluations>/<Tenant>-<env>-attribution.json'
```

**Key graph (`keygraph.py`), built on the profile stage.** It reads the profile's `anchors` (person values), `bridge_apis` (carriers and issued cookies) and `fact.journey-window` (the window; there is no default), plus `journey.csv`.
- Each request links every value it carries; person anchors and key values become nodes in a union-find.
- It adds keys greedily: at each step it takes the key that attributes the most new requests to exactly one person. Collision rate is the share of person-anchored requests that land in groups holding 2+ people; the key is added only while that stays within `--collision-budget`.
- A key need not be everywhere. A key that reaches APIs the chain does not is kept.
- Candidates are request cookies, headers **and URL query parameters**. A query value that repeats across requests (a user or customer id, or a visit or tracking id passed in links and redirects) stitches like a cookie and is scored the same way. A query parameter that names the user is also a user-attribution source (below).
- Rejected keys are listed with a reason: not present, no gain, only merges people, or over budget.
- Output is `journey-graph.json` (the chain with per-step attributed share, journey APIs p50/p95, the rejected keys and the unreached APIs with what they carry) and `chain.csv`.
- `derive_defs.py` ranks request-side session carriers by chain step first. It moves carriers whose values cross people above the budget into `not_session`, with the rate as the reason. Anti-forgery tokens are judged like any key: they stay if they stitch without crossing people.
- **Check every body for the user, not only login bodies.** Request bodies, response bodies (a profile or account read returns the user's id) and query parameters are user-attribution sources on any API. The profile stage's `anchors` table lists each field with its APIs and how well it matches `user_identifier`.
  - `derive_defs.py` turns each `person` field (its values are `user_identifier` values) into a `measured` user rule scoped to those APIs.
  - A `person-candidate` field names the person in another id space (an internal user id, an email where the platform user is a username). It is not a user rule: it would give the same person a second user id and split the journey. It goes to `excluded` with that reason.
  - A user rule whose scope comes out empty is never written: an empty scope applies to every API.
  - `keygraph.py` uses `person` fields as anchors, so requests that carry no credential still start a journey. It groups same-named id fields across bodies, responses and query parameters into one `field:<name>` key. A call that returns both ids, such as a sign-in response with the internal id and the username, links the two spaces. That key is held to the same collision budget as any other.
  - Path parameters cannot be read by the platform's rules. A user id that sits only in the path is a gap: report it; do not write a rule for it.
- **Unreached APIs** (`journey-graph.json`) are mostly user-attribution work, not session work. An unreached API that carries no chain key can only be reached by a rule that reads the user from its own request or response.
- **Platform propagation:** the platform does not copy a session's user onto that session's other spans. Mixed sessions (some spans with a user, some without) are common, and a platform session never holds two users. So the chain is what extends attribution beyond the spans that carry the user.

**The output is the platform's own payload, and nothing else.** The deliverable `<Tenant>-<env>-attribution.json` holds items of exactly `{mutation, variables}`: what the platform's GraphQL takes (`tools/gqlshape.py`), created **disabled**, submitted as-is. User -> `createUserAttributionRuleV2(create: ...)`, session -> `createSessionIdentificationRuleV2(create: ...)`, entity -> `CreateEntityDerivationConfig(input: ...)`. No confidence, evidence or notes in it.
- Scores and the reviewer's view go in the file beside it, `<Tenant>-<env>-attribution-review.json` (same order): per item `{kind, mutation, name, assessment}`, plus `not_extracted`. The assessment has `entity_name, description, environment, services, apis, methods, confidence, evidence, definitions, ready_to_submit, notes` (entities add `population, use, sensitive`). It is never sent.
- This agent never writes to the platform: it reads only.
- Session rules never carry an expiration: do not measure or set one.

**What the platform accepts** is kept in `tools/platform_shapes.json` (the platform's own input types; refresh with `python3 tools/platform_shapes.py refresh --token-file <file>` or `TRACEABLE_TOKEN`, read-only introspection; `platform_shapes.py show` lists everything). `package.py` and `contracts/validate_attribution.py` check every payload against it; a payload that does not fit is held in `not_extracted`. Build to these limits:
- Locations: user rules take request header, cookie, body, query parameter and the response header, cookie, body; session rules take header, cookie, body, query parameter (response side: header, cookie, body); entities take the seven request/response locations. Path parameters are not accepted anywhere.
- Value steps: regex capture group, base64, JSON path, URL decode (not in a session rule: dropped), and a JWT claim (a decode plus a top-level claim collapses to one `JWT_PAYLOAD_CLAIM`; the platform has no bare decode step). Entities use functions instead (regex extract, JSON path, base64 decode, URL decode, type casts).
- Key match: equals or regex. Conditions: equals, contains, starts with, regex. Obfuscation: hash.
- Anything with no platform equivalent is dropped and said in `assessment.notes`; do not invent a step the snapshot does not list.

**API ids.** The platform scopes an entity by API id, not by path. `tools/api_ids.py` reads them from the traffic table (`span_event_view.api_id` and `service_id`, the same uuids the platform stores in `entity_scope.entity_ids`) into `api_ids.csv`; `package.py --api-ids` turns each definition's paths and methods into ids. A location whose APIs match no id is left out; an entity with none is held; without `--api-ids` the ids stay empty and `ready_to_submit` is false. The ids come from the traffic, not from the platform's own API table, so they are format-matched, not cross-checked.


**The deliverable is the packaged `<Tenant>-<env>-attribution.json`** (`blue-agent/attribution@4`). Top level is exactly `schema, customer, environment, generated, window, users, sessions, entities`; `not_extracted` is in the review file. `not_extracted` is one list of `{kind, entity_name, reason}`: everything rejected, excluded, or that matched nothing in the live check. Check it with `python3 contracts/validate_attribution.py <file>`; the contract is `attribution_envelope` in `contracts/schemas/definitions.json`. Do not add keys the contract does not list.

`discover.py` finds every carrier: the `authorization` scheme × token shape per API, decoded Basic usernames (shape only), cookies, other headers, the `user_identifier` shape, every JWT claim in a header or cookie (name, shape, distinct count, how often it equals `user_identifier`), request params and top-level body keys, and tokens a response issues. `journey.py` explores the keys: for each token-like cookie or header it groups the window by the key's value and measures how many APIs, and how many requests, one value links (`journey.csv`). The goal is the longest user journey that attribution can stitch, so no key is judged by its name or by being an anti-forgery token: any key that stitches requests counts, and the same key need not be everywhere. `derive_defs.py` ranks carriers by that reach, keeps the first `--max-sessions` (40), and past that keeps a carrier only if it reaches APIs no kept carrier reaches (entities follow the same rule past `--max-entities`); a different key on a different set of APIs extends the journey, so both are delivered. Header carriers are scoped to the APIs they ride on, and a response-issued token is exempt from the 1% coverage floor (it appears once per session). `derive_defs.py` scores them and writes the definitions. `verify_defs.py` compiles each definition (location, conditions, transformations) to SQL and runs it on the window: spans in scope, extraction rate, distinct values, and agreement with the user column. A definition that does not extract is not delivered.

**Auth types to cover, standard and not.** Basic (decode the username), opaque Bearer (per-user but rotating: session-grade), Bearer JWT, JWT in a cookie, `userId`-style headers, API-key headers, signed cookies, embedded ids inside a rotating value, tokens in the body or query, mTLS or gateway-injected identity headers, and the masked case (below). Never branch on a tenant. Branch on the shape class: `masked, empty, jwt, dotted3, uuid, email, digits, ip, hex, base64, opaque, short, other`.

**True end user, not machine.** Every carrier is judged on four measured tests; names are only a tiebreak, and a name-led call is marked `inferred`:

| Test | Reads | Machine when |
|---|---|---|
| T1 cardinality class | distinct values against spans | per-request, constant, few values, or a small population hammering many spans |
| T2 fan-out | client IPs per value | more than 20 IPs per value (shared credential) |
| T3 lane | share of the carrier's spans on the profile stage consumer-lane APIs | below 0.2 (end user at 0.5 or more; mixed between) |
| T4 caller vs target | share of spans where the value equals `user_identifier` | n/a: informs whether the column is caller or target |

Registered and protocol claims (`iss, aud, exp, nbf, iat, jti, nonce, scp ...`) are never a person key. Without the profile stage's lanes, T3 is unknown and the confidence drops to `inferred`.

**T3 is a tiebreak, not a veto.** A per-user-sized carrier (30 or more distinct values in an API, at most 2 IPs per value) that sits only on machine-lane APIs is a gateway or app server calling for end users. It is kept as `mixed` / `inferred` with a note, never dropped on the lane alone. Dropping it on the lane lost the real session key on a tenant whose lanes were computed with `--auth-usable no`.

**Negative controls.** Names only ever say "not": CDN and bot-challenge, load-balancer affinity and performance-monitoring cookies (`cf`-style clearance, `incap`-style, `AWSALB`-style, `dt`-style) stitch a connection or a browser challenge, never a login. They are listed in `not_extracted` with that reason and can never be a session or user definition, whatever their score. Analytics and campaign cookies stay `visitor` grade and rank after first-party session carriers, ordered by how often the platform already knows the user on their spans (`signed_in_share`).

**Where identity hides when headers do not carry it.** Run the same tests on four more places, because a tenant's real keys are often there:

- **Login and sign-up bodies.** The name a person types (`username`, `login`, `email`, ...) on a login-like path (login, sign-in, authenticate, token, otp, sso, enrol, register). Chosen by key leaf, shape, variety (distinct values at least 30% of spans) and at most 20 IPs per value, never by tenant. Case variants (`Username`, `username`, `UserName`) are separate keys on the platform and are kept separate. It is a claim typed before authentication, so it is `inferred` and carries a note.
- **Response bodies, one level nested.** A session token is often `data.sessionKey`-style. Discovery lists top-level and one-level-nested keys (`data.key`). A response token that matches the distinct count of a request carrier is probably what that carrier presents; the pairing is written in the note.
- **Response cookies.** A JWT cookie the server sets is the same identity as the one a later request presents. Keep both directions.
- **Basic auth.** A handful of usernames over a few IPs and many APIs is a service account. Verified on one tenant: 11 usernames, 22 IPs, 8 APIs. It is never an end-user rule, even when the platform labels it so.

**Masked authorization.** When a tenant masks the `authorization` header (shape `masked`), header extraction is impossible and is stated as a gap. Definitions then come from cookies, the request body, or the platform's own `user_identifier` column. Do not invent a header definition.

**Dynamic names.** A cookie or header name that carries a per-user or per-site id (`prefix-<id>`, or an id alone) is folded to one carrier `prefix*`, emitted with a `key_pattern` regex.

**Session identification.** A session value is reused across requests (at least 2 spans per value), is credential-shaped (not free text or a number), is not shared across IPs, and is carried by end-user APIs. Headers that carry the same value (identical spans, distinct count and IPs) are one definition with `aliases`. Visitor and analytics ids, first- or third-party (`_ga`-style, click and campaign ids), are kept when they pass the same tests, marked `session_grade: "visitor"` and ranked after first-party session carriers: they outlive one login, so they stitch a visitor across visits. `multiple_per_user` is measured as distinct sessions per distinct user. Response-issued tokens are `Response` identifiers and are the lowest confidence until a follow-up request presents them.

**Entity attribution, the attribution stage: contextual identifiers.** An entity is an identifier a hunt or an investigation can pivot on: email, phone, card number, first/last/full name, street address, zip, date of birth, national id, bank account, username, and digests of these (`phone-hash`, `email-hash`, only when the key names what was hashed). Opaque ids, tokens, nonces, counters and free text are never entities. Run `tools/entities.py` over several spaced windows (sampling per API, so one chatty API cannot outvote the rest).

- **Name = `<what>.<whose>`**, both parts always, lowercase-hyphen: `email.account-holder`, `email.guest-shopper`, `email.third-party-login`, `zip.billing-party`, `phone.shipping-recipient`. *What* is the identifier type (email, phone, first-name, last-name, full-name, street-address, address, zip, date-of-birth, card-number, bank-account, national-id, username, email-hash, phone-hash). *Whose* answers "who is this person to the tenant?": account-holder, guest-shopper, shopper (guest vs account unknowable), third-party-login, login-claimant (typed at a login attempt, not yet proven), new-registrant, billing-party, shipping-recipient, card-holder, site-member, contact, recipient, internal-staff, service-account. Names are unique per tenant (same name = one entity with several locations) and mean the same thing in every tenant. A combined value is named for the whole (`address.billing-party`). Where the data came from (feed, API, key) is never in the name: it is the `population` field. Every entity carries a one-line plain-English `description` and a `whose_evidence` that says how *whose* was decided.
- **Whose** comes from, in order: the nearest JSON ancestor (billing, shipping, recipient, card-holder, contact); the API's journey class from its path tokens (login, signup, checkout, contact, profile, third-party-login, feed); and a session follow (same `session_id`, else caller ip, ±30 min: was the user column populated before or after, did a login succeed). A checkout with sign-in before or after is `account-holder`; never signed in is `guest-shopper`. On a login journey only email, phone, username and their hashes are identifiers; anything else there (address parts, names) is a form artifact and is rejected.
- **No `unclassified`.** If whose cannot be determined the entity is rejected and logged in `not_entity` with the reason. Also rejected: a bare `name` key unless an ancestor says it is a person; values that are labels or titles; object ids, counts and flags; keys that do not say what the value is (an email in a key called `id` is weak, in `fileName` is out); any location whose values pass the type check under 90% of the time. Staff and service accounts are kept as `internal-staff` / `service-account`, investigate-only, and are rejected only as end-user attribution. Merge duplicates.
- **Shared values are not rejected.** Record a baseline per value of the devices (ip + user agent) seen with it (median, p95, share on 2+ devices) so a later rise, e.g. a third device on a value normally seen with two, can be detected. Such an entity is not used for attribution.
- **No fixed volume floor.** Judge coverage inside the API (share of that API's requests that carry the value) and whether there are enough samples. Too few samples is "retest on a longer window" (logged with kind `retest`), never a reject.
- **Confidence (users and sessions).** Hard gates first, any one fails and the definition is rejected whatever the mean says: extracts live; at least 1% of its scoped spans carry it; at most 5 IPs per value; not a connection cookie. Then the mean of the measured checks. A definition whose confidence is `inferred` is capped at `review`: only `measured` ones reach `extract`. The 0.85 / 0.65 cut-offs and the 1% / 5-IP limits come from measured behaviour of true carriers, not yet from a labelled fit: re-check them against a labelled tenant (its own rules, with false positives graded) before trusting the cut-off at a new scale.
- **Confidence (entities).** Hard gates first: seen in at least 2 of the sampled windows (1 if only one); shape check at least 90%; not a machine for end-user use; a session value is not a per-request nonce; an attribution value is not shared by many people. Then a weighted score: key semantics .20, whose clarity .20, shape pass rate .15, cardinality fit .15, scope specificity .10, cross-location agreement .10, stability across windows .10. Score at least .85 and end-user population: extract, hunt allowed, recommended tab. .65 to .85: extract investigate-only, flagged for review. Below .65 or any gate failed: do not extract, log the reason. Feed entities are capped at investigate-only. Users and sessions that end under 0.65 or fail a gate go to `not_extracted` with the failed gate or weakest check named.
- **Population test.** A group is a machine feed (`population: feed`, `hunt_use: investigate-only`, never end-user) when its APIs are partner or internal, or it has ≤10 ips with ≥30 values, or one ip carries ≥50% of it. Judge by the group's ip spread first: a few partner ips asking about many subjects is a feed, hundreds of ips with no dominant one is end-user whatever the API's name says.
- **Quirks to expect.** Nested JSON (paths with `[*]`), label/value pair lists (`[label].value`), path params, response bodies (profile reads), values in headers, digests of identifiers, and several keys that only mean something together (first+last name, street+zip). The meaning of a field is often only visible from the session around it, so follow it.
- **Output shape.** The deliverable is payloads only (`{mutation, variables}`). The review file's `assessment` has exactly: `entity_name, description, environment, services, apis, methods, confidence, evidence, definitions, ready_to_submit, notes`; entities add `population` (end-user or feed), `use` (hunt or investigate-only) and `sensitive`. `confidence` is one number from 0 to 1. `evidence` keeps only what helps debug a decision: `requests_checked, requests_matched, distinct_values, confidence_breakdown`, and for entities `typical_devices_per_value, share_of_values_on_multiple_devices, whose_basis`.
- Values are hashed in memory and never written; output is counts and key paths.
- Limits to know: the combined-key cardinality is a placeholder until verified on live data; `services` is always "all" because the platform gives no per-service signal.
The entity gap-fill stage (not in this repo) does the other half: it defines an entity only where the attribution stage has none and a policy or capability needs it.

**Scope regexes.** Each definition's `url_regexes` is built from the APIs that carry it, on consumer, partner or other lanes. Paths are folded into families (`^/prefix(/.*)?$`) from the deepest shared prefix up until the list fits `--max-regex` (20). A family prefix always holds a static segment, so a catch-all like `^/[^/]+(/.*)?$` is never produced.

**Schema status.** The definition shapes are modelled on the platform's Edit User Attribution, Define Scope, Session Identification and Entities screens. The `JWT Payload Decode` and `Base64 Decode` transformations are marked `platform_support: "assumed"` until confirmed against a platform export.

**Lessons from grading a tenant's own rules on live traffic** (counts only, three-day span samples):

- A rule is a true positive when its carrier is per-user sized, on few IPs per value, and appears on many APIs. Rules that match nothing in a week are dead and can be dropped. Header or body-token rules that match a handful of spans are dormant, not wrong.
- Case-variant clones of one login-body key are not duplicates: each matched a different slice of the same endpoint, and together they cover it. Merge them in a report, not in the rules.
- `equals the platform user column` proves nothing for a rule that built that column. Use it to check a candidate agrees with the platform, not to prove the platform is right.
- A user JWT claim that equals the user column on nearly every span (`sub`) is the durable key. A second claim (email) on the same token matches the column on a small share, so it is an alias, not a second person key.
- Session keys can sit behind a gateway: one IP serves thousands of people, yet each key value is used by one person. Judge IPs per value, not values per IP.

**Caveats.** One short window can miss a rare carrier: run it across two or three windows. Expiration cannot be measured from a short window. Opaque Bearer tokens rotate, so prefer the user column for the durable person key.

## Done when

The View Builder can copy a `coalesce(...)` expression from `key-hierarchy.md` and every tier's claims are backed by a scorecard row. Every definition in the three json files has been run by `verify_defs.py` and extracts on the window, and every excluded carrier names its test.
