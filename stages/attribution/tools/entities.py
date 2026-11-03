#!/usr/bin/env python3
"""Contextual entity attribution (the attribution stage). No tenant, API, header or key names in here.
An entity is an IDENTIFIER of a person or instrument that a hunter can search by or an investigator can pivot on:
email, phone, card number, first/last/full name, street address (+ billing/shipping), zip/postal code, date of birth, national id, bank account, username.
Opaque object ids (page, cart, form, nonce, token) are NOT entities; they are never emitted.

The same identifier means different things in different places, so an entity is named  <what>.<whose>  (both parts always, lowercase-hyphen):
  email.account-holder   email.guest-shopper   email.third-party-login   zip.billing-party   first-name.shipping-recipient ...
"what" is the identifier type; "whose" answers "who is this person to the tenant?". Where the data came from (feed, API, key) stays OUT of the name:
that is the separate `population` field. An entity whose "whose" cannot be determined is REJECTED and logged in not_entity with the reason; there is no "unclassified".
"whose" comes from, in this order:
  1. the sub-role from the nearest JSON ancestor name (billing, shipping, recipient, card-holder, contact),
  2. the API's journey class (login, signup, checkout, contact, profile, feed, third-party-login ...) from its path tokens,
  3. the SESSION FOLLOW: for the spans that carry the identifier, the other spans of the same session (platform session id, else caller IP)
     within +/-30 minutes. Whether the user was authenticated before or after (a user column appears, a login succeeded) separates a guest
     from an account holder on the same checkout API.
Population (measured, never by name): a group whose APIs are partner/internal lane, or whose few IPs deliver many distinct values, is a machine FEED:
other people's data, kept as population "feed", hunt_use "investigate-only", never used for end-user attribution.
Quality: every entity gets a confidence score (hard gates, then weighted components) and a band: extract (>= .85), review (.65-.85, investigate-only), reject (< .65 or a gate failed).
Rejected: whose undetermined; value is a label not a person; not an identifier (object ids, counts, flags); UI/form artifacts on login journeys; shape check under 90% at a location.
Shared values are NOT rejected: a baseline of typical devices per value is recorded so a rise can be detected.
A definition may combine keys (first+last name, street+city+zip) or pair a label with a value ({"name":"email","value":...}); both are supported.
Privacy: raw values are held in memory only, hashed for distinct counts, and NEVER written to disk or printed. Output is counts and key paths.

Usage: entities.py --customer <uuid> --env <env> --starts 'YYYY-MM-DD HH:MM' [more starts ...] --out <dir> [--minutes 3] [--per-api 30] [--apis apis.json]
Writes <out>/entity-definitions.json and <out>/entity-groups.csv"""
import argparse, collections, csv, hashlib, io, json, os, re, statistics, subprocess, sys

ap = argparse.ArgumentParser()
ap.add_argument("--customer", required=True); ap.add_argument("--env", required=True); ap.add_argument("--starts", nargs="+", required=True)
ap.add_argument("--minutes", type=int, default=3); ap.add_argument("--per-api", type=int, default=30); ap.add_argument("--apis", help="the profile stage apis.json (lane, payload_source); else lanes are measured here")
ap.add_argument("--out", required=True); ap.add_argument("--min-distinct", type=int, default=5); ap.add_argument("--max-regex", type=int, default=12); ap.add_argument("--follow", type=int, default=150, help="sessions followed per group")
a = ap.parse_args()
assert re.fullmatch(r"[0-9a-f-]{36}", a.customer) and re.fullmatch(r"[A-Za-z0-9._-]+", a.env) and all(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", s) for s in a.starts)
BASE = f"customer_id = '{a.customer}' AND environment = '{a.env}'"
win = lambda s, pad=0: f"start_time_millis_ts >= timestamp '{s}:00' - interval '{pad}' minute AND start_time_millis_ts < timestamp '{s}:00' + interval '{a.minutes + pad}' minute"

def trino(sql):
    r = subprocess.run(["kubectl", "-n", "traceable", "exec", "-i", "deployment/trino-coordinator-ro", "-c", "trino-coordinator-ro", "--", "trino", "--server", "localhost:8080", "--catalog", "iceberg", "--schema", "iceberg_datalake", "--output-format", "CSV_HEADER"], input=sql.rstrip().rstrip(";") + ";", capture_output=True, text=True)
    out = "\n".join(l for l in r.stdout.splitlines() if "JDK_JAVA_OPTIONS" not in l)
    if r.returncode or re.match(r"Query \S+ failed", out): sys.exit("query failed: " + " ".join((r.stderr + out).split())[:300])
    return list(csv.DictReader(io.StringIO(out)))

# ---- vocabularies (generic words, not tenant words) ---------------------------------------------------------------------------
TOK = lambda s: [t for t in re.split(r"[^a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", s).lower()) if t]
JOURNEY = [("feed", {"webhook", "webhooks", "events", "event", "ingest", "batch", "import", "sync", "callback", "notify", "notification", "notifications"}),
           ("third-party-login", {"sso", "oauth", "oauth2", "openid", "oidc", "saml", "social", "federated", "federation", "idp", "google", "facebook", "linkedin", "github"}),
           ("login", {"login", "signin", "logon", "authenticate", "auth", "otp", "mfa", "2fa", "password", "passwd", "reset", "forgot", "verify", "verification", "token", "session", "sessions", "credentials", "identify"}),
           ("signup", {"register", "registration", "signup", "enroll", "enrollment", "onboard", "onboarding", "join", "createaccount"}),
           ("checkout", {"checkout", "cart", "basket", "order", "orders", "payment", "payments", "pay", "billing", "purchase", "purchases", "invoice", "invoices", "subscription", "subscriptions"}),
           ("contact", {"contact", "lead", "leads", "newsletter", "subscribe", "feedback", "support", "inquiry", "enquiry", "form", "forms", "submit"}),
           ("profile", {"profile", "account", "accounts", "user", "users", "member", "members", "me", "customer", "customers", "person", "settings", "preferences"})]
def journey(api):
    toks = set(t for t in TOK(api.split(" ", 1)[-1]) if not t.startswith("v") or not t[1:].isdigit())
    for name, words in JOURNEY:
        if toks & words: return name
    return "general"
PERSON = r"customer|user|member|person|contact|billing|shipping|recipient|buyer|guest|applicant|holder|owner|account|employee|patient|passenger|traveler|traveller|author"
ROLES = [("billing", r"bill"), ("shipping", r"ship|deliver|dest"), ("recipient", r"recipient|payee|beneficiar|receiver|to$"), ("sender", r"sender|payer|from$"), ("card-holder", r"card ?holder|cardholder|owner"),
         ("contact", r"contact"), ("customer", r"customer|client|buyer|guest"), ("member", r"member|user|person|profile|applicant")]
def role_of(parents):
    for p in reversed(parents):
        t = " ".join(TOK(p))
        for name, rx in ROLES:
            if re.search(rx, t): return name
    return ""
LUHN = lambda s: (lambda d: len(d) >= 13 and sum(d[-1 - i] if i % 2 == 0 else (d[-1 - i] * 2 - 9 if d[-1 - i] * 2 > 9 else d[-1 - i] * 2) for i in range(len(d))) % 10 == 0)([int(c) for c in s])
EMAIL = re.compile(r"^[^@\s\"'<>]{1,64}@[^@\s\"'<>]+\.[A-Za-z]{2,}$")
# (type, kind, key hint on the LAST key segment, value check). Value checks keep a name-only guess from becoming an entity.
def typed(k, v, rowkind, parents=()):
    kt = " ".join(TOK(k)); v = v.strip()
    if not v or len(v) > 200 or "***" in v: return None
    if EMAIL.match(v): return "email"
    if re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", v):  # digest of an identifier: still a stable pivot when the key says what was hashed
        if re.search(r"\b(phone|mobile|msisdn|cell|tel|telephone|number)\b", kt) and not re.search(r"\b(id|token|session|request|trace)\b", kt): return "phone-hash"
        if re.search(r"\b(email|mail)\b", kt): return "email-hash"
    d = re.sub(r"[\s().-]", "", v)
    if re.search(r"\b(card|pan|ccnum|cc num|credit)\b", kt) and re.fullmatch(r"\d{13,19}", d) and LUHN(d): return "card-number"
    if re.search(r"\b(phone|mobile|msisdn|cell|tel|telephone|fax)\b", kt) and re.fullmatch(r"\+?\d{7,15}", d): return "phone"
    if re.fullmatch(r"\+\d{9,15}", re.sub(r"[\s().-]", "", v)) and not re.search(r"id|token|ts|time|date", kt): return "phone"
    if re.search(r"\b(first|given|fname|forename)\b", kt) and re.fullmatch(r"[^\W\d_][^\W\d_ '.-]{0,40}", v): return "first-name"
    if re.search(r"\b(last|family|surname|lname)\b", kt) and re.fullmatch(r"[^\W\d_][^\W\d_ '.-]{0,40}", v): return "last-name"
    shape_name = re.fullmatch(r"[^\W\d_]+([ '.-][^\W\d_]+){1,3}", v)
    if re.fullmatch(r"(full ?name|customer name|display name|cardholder name|card holder name)", kt) and shape_name: return "full-name"
    if kt == "name" and shape_name and any(re.search(PERSON, " ".join(TOK(p))) for p in parents[-2:]): return "full-name"  # a bare "name" counts only when an ancestor says it is a person
    if re.search(r"\b(zip|zipcode|postal|postcode|postalcode)\b", kt) and re.fullmatch(r"[A-Za-z0-9 -]{3,10}", v): return "zip"
    if re.search(r"\b(address|addr|street|address1|address2|line1|line2)\b", kt) and re.search(r"\d", v) and re.search(r"[A-Za-z]{3}", v) and " " in v: return "street-address"
    if re.search(r"\b(dob|birth|birthdate|birthday)\b", kt) and re.fullmatch(r"\d{4}-\d\d-\d\d|\d\d?/\d\d?/\d{4}", v): return "date-of-birth"
    if re.search(r"\b(ssn|social security|national id|tax id|nin|sin)\b", kt) and re.fullmatch(r"\d{3}-?\d{2}-?\d{4}|\d{8,12}", v): return "national-id"
    if re.search(r"\b(iban|account number|acct num|routing number|routing no|accountnumber|routingnumber)\b", kt) and re.fullmatch(r"[A-Z0-9]{8,34}", d.upper()): return "bank-account"
    if re.search(r"\b(username|user name|login|handle)\b", kt) and re.fullmatch(r"[A-Za-z0-9._-]{3,40}", v) and not re.fullmatch(r"[0-9a-fA-F-]{32,}", v): return "username"
    return None
KIND = {"email": "Email Address", "phone": "Phone Number", "card-number": "Credit Card"}  # everything else is a String entity
ADDR_PARTS = {"street-address", "zip"}

def flat(o, path="", parents=(), depth=0):
    """yield (path, key, parents, value). Lists become [*]; {label,value} pairs also yield a synthetic pair path."""
    if depth > 8: return
    if isinstance(o, dict):
        lab = next((o[k] for k in ("name", "key", "type", "field", "label", "id") if isinstance(o.get(k), str)), None)
        val = next(((k, o[k]) for k in ("value", "val", "content", "text") if isinstance(o.get(k), (str, int))), None)
        if lab and val: yield (f"{path}[{lab}].{val[0]}" if path else f"[{lab}].{val[0]}", lab, parents, str(val[1]), ("pair", lab, val[0]))
        for k, v in o.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, (dict, list)): yield from flat(v, p, parents + (k,), depth + 1)
            elif isinstance(v, (str, int)) and not isinstance(v, bool): yield (p, k, parents, str(v), None)
    elif isinstance(o, list):
        for x in o[:5]:
            if isinstance(x, (dict, list)): yield from flat(x, path + "[*]", parents, depth + 1)
            elif isinstance(x, str): yield (path + "[*]", path.rsplit(".", 1)[-1], parents, x, None)

# ---- 1. sample: bodies, params, path params, email-valued headers; per window --------------------------------------------------
def sample(start):
    return trino(f"""SELECT api_name, ip_address ip, coalesce(user_agent,'') ua, coalesce(session_id,'') sid, coalesce(user_identifier,'') uid, status_code sc, to_unixtime(start_time_millis_ts) ts,
 IF(length(request_body) BETWEEN 3 AND 12000, regexp_replace(request_body,'[\\r\\n]+',' ')) rb, IF(length(response_body) BETWEEN 3 AND 12000, regexp_replace(response_body,'[\\r\\n]+',' ')) pb,
 json_format(CAST(map_concat(coalesce(request_params, MAP()), coalesce(query_params, MAP())) AS JSON)) qp, json_format(CAST(path_params AS JSON)) pp,
 json_format(CAST(map_filter(request_headers, (k, v) -> regexp_like(v, '^[^@ ]+@[^@ ]+\\.[A-Za-z]{{2,}}$')) AS JSON)) hh
 FROM (SELECT *, row_number() OVER (PARTITION BY api_name ORDER BY xxhash64(span_id)) rn FROM span_event_view WHERE {BASE} AND {win(start)} AND api_name IS NOT NULL
   AND (length(request_body) > 2 OR length(response_body) > 2 OR cardinality(query_params) > 0 OR cardinality(request_params) > 0 OR cardinality(path_params) > 0)) WHERE rn <= {a.per_api}""")
def apistats(start):
    return trino(f"SELECT coalesce(api_name,'<NULL>') api_name, count(*) spans, approx_distinct(ip_address) ips, round(avg(if(is_ip_external AND api_is_external, 1e0, 0e0)), 3) ext FROM span_event_view WHERE {BASE} AND {win(start)} GROUP BY 1")

lanes = {}
if a.apis:
    for r in json.load(open(a.apis))["apis"]: lanes[r["api_name"]] = (r["lane"], r.get("payload_source", "unknown"))
G = {}  # group key -> stats
class Grp:
    def __init__(s): s.vals = set(); s.ips = set(); s.top = collections.Counter(); s.spans = 0; s.apis = collections.Counter(); s.keys = collections.defaultdict(set); s.win = collections.Counter(); s.masked = 0; s.sess = []; s.win_vals = collections.defaultdict(set); s.parents = {}; s.pairs = {}; s.locvals = collections.defaultdict(set); s.locspans = collections.Counter(); s.dev = collections.defaultdict(set); s.methods = collections.defaultdict(set)
hv = lambda v: hashlib.sha1(v.lower().encode()).hexdigest()[:12]
astat = {}
tot = collections.Counter(); hit = collections.Counter(); rows_api = collections.Counter()  # per (side,path): non-empty values seen / values that pass the type check; sampled rows per API
by_span = {}  # (api, window) -> list of (grpkey, sesskey, ts) for the session follow
for wi, start in enumerate(a.starts):
    for r in apistats(start):
        s = astat.setdefault(r["api_name"], dict(spans=0, ips=0, ext=0.0)); n = int(r["spans"]); s["ext"] = (s["ext"] * s["spans"] + float(r["ext"] or 0) * n) / max(1, s["spans"] + n); s["spans"] += n; s["ips"] = max(s["ips"], int(r["ips"]))
    print(f"window {start}: sampling", file=sys.stderr)
    for r in sample(start):
        api = r["api_name"]; jc = journey(api); found = []; rows_api[api] += 1
        def add(side, path, key, parents, val, extra=None):
            t = typed(key, val, side, parents)
            if t: found.append((side, path, key, parents, val, t, extra))
        for side, col in (("Request Body", "rb"), ("Response Body", "pb")):
            if (r[col] or "")[:1] in "{[":
                try: o = json.loads(r[col])
                except Exception: continue
                for path, key, parents, val, ex in flat(o):
                    if val.strip(): tot[(side, path)] += 1
                    add(side, path, key, parents, val, ex)
        for side, col in (("Request Param", "qp"), ("Path Param", "pp"), ("Request Header", "hh")):
            if r[col] and r[col] != "{}":
                try: m = json.loads(r[col])
                except Exception: continue
                for k, v in (m or {}).items():
                    if isinstance(v, str):
                        if v.strip(): tot[(side, k)] += 1
                        add(side, k, k, (), v)
        sesskey = r["sid"] or "ip:" + r["ip"]
        for side, path, key, parents, val, t, ex in found:
            role = role_of(parents) if t in ADDR_PARTS | {"first-name", "last-name", "full-name", "phone", "email", "phone-hash", "email-hash"} else ""
            if ex and ex[0] == "pair": role = role or ""
            gk = (t, role, jc, side.startswith("Response"))
            g = G.setdefault(gk, Grp()); h = hv(val)
            g.vals.add(h); g.ips.add(r["ip"]); g.top[r["ip"]] += 1; g.spans += 1; g.apis[api] += 1; g.win[wi] += 1; g.win_vals[wi].add(h)
            g.keys[(side, path)].add(api); g.parents[(side, path)] = parents; hit[(side, path)] += 1
            g.locvals[(side, path)].add(h); g.locspans[(side, path)] += 1; g.dev[h].add(hv(r["ip"] + "|" + r["ua"])); g.methods[(side, path)].add(api.split(" ", 1)[0] if " " in api else "")
            if ex: g.pairs[(side, path)] = ex
            if len(g.sess) < 4000: g.sess.append((sesskey, float(r["ts"]), api))
        # sibling combos: remember per span for name / address merging
        sib = collections.defaultdict(dict)
        for side, path, key, parents, val, t, ex in found:
            if t in ("first-name", "last-name", "street-address", "zip"): sib[(side, "/".join(parents), role_of(parents))][t + "@" + path] = path
        for (side, par, role), d in sib.items():
            types = {k.split("@")[0] for k in d}
            if {"first-name", "last-name"} <= types or len(types & ADDR_PARTS) >= 2:
                gk = ("combo:" + ("full-name" if {"first-name", "last-name"} <= types else "address"), role, jc, side.startswith("Response"))
                g = G.setdefault(gk, Grp()); g.apis[api] += 1; g.spans += 1; g.ips.add(r["ip"]); g.top[r["ip"]] += 1; g.keys[(side, par)].add(api)
                g.vals.add(hv(" ".join(sorted(d)) + str(r["ts"])[:3]))  # placeholder cardinality; the combined value itself is verified on live data

# ---- 2. session follow: authenticated before/after, journey neighbours ---------------------------------------------------------
def follow(gks):
    keys = {}
    for gk in gks:
        for sk, ts, api in G[gk].sess[: a.follow * 3]:
            keys.setdefault(gk, {})[sk] = ts
    for gk in list(keys): keys[gk] = dict(list(keys[gk].items())[: a.follow])
    sids = sorted({k for d in keys.values() for k in d if not k.startswith("ip:")}); ips = sorted({k[3:] for d in keys.values() for k in d if k.startswith("ip:")})
    nb = collections.defaultdict(list)  # sesskey -> [(ts, api, uid_present, ok)]
    q = lambda xs: ",".join("'" + x.replace("'", "") + "'" for x in xs)
    qs = [f"session_id IN ({q(sids[i:i + 300])})" for i in range(0, len(sids), 300)] + [f"ip_address IN ({q(ips[i:i + 300])})" for i in range(0, len(ips), 300)]
    for start in a.starts:
        for cond in qs:
            for r in trino(f"SELECT coalesce(nullif(session_id,''), 'ip:' || ip_address) sk, to_unixtime(start_time_millis_ts) ts, api_name, IF(user_identifier IS NOT NULL AND user_identifier <> '', 1, 0) uid, substr(status_code,1,1) sc FROM span_event_view WHERE {BASE} AND {win(start, 30)} AND {cond} AND api_name IS NOT NULL LIMIT 200000"):
                if not (r.get("ts") and (r.get("uid") or "").isdigit()): continue  # a truncated row is dropped, not guessed
                nb[r["sk"]].append((float(r["ts"]), r["api_name"], int(r["uid"]), r["sc"] == "2"))
    res = {}
    for gk, d in keys.items():
        auth_b = auth_a = n = login_ok_after = 0; cls = collections.Counter()
        for sk, ts in d.items():
            ev = nb.get(sk)
            if not ev: continue
            n += 1
            before = [e for e in ev if e[0] < ts - 1]; after = [e for e in ev if e[0] > ts + 1]; near = [e for e in ev if abs(e[0] - ts) <= 1]
            auth_b += any(e[2] for e in before) ; auth_a += any(e[2] for e in after) or any(e[2] for e in near)
            login_ok_after += any(journey(e[1]) in ("login", "third-party-login") and e[3] for e in after)
            for e in before + after:
                c = journey(e[1])
                if c != "general": cls[c] += 1
        res[gk] = dict(sessions_followed=n, authenticated_before=round(auth_b / n, 2) if n else None, authenticated_after=round(auth_a / n, 2) if n else None, login_success_after=round(login_ok_after / n, 2) if n else None, neighbour_journeys=[c for c, _ in cls.most_common(3)])
    return res

# ---- 3. population, whose, naming, confidence ----------------------------------------------------------------------------------
def lane_of(api):
    if api in lanes: return lanes[api]
    s = astat.get(api)
    if not s: return ("other", "unknown")
    # quick in-window measure: only "internal" is trusted (no external reach); caller spread is judged on the group, not a 3-minute per-API ip count
    return ("internal", "internal") if s["ext"] < .05 else ("unknown", "unknown")
WHAT = {"email": "Email address", "phone": "Phone number", "first-name": "First name", "last-name": "Last name", "full-name": "Full name", "street-address": "Street address", "address": "Postal address (street, city, zip together)",
        "zip": "Zip or postal code", "date-of-birth": "Date of birth", "card-number": "Payment card number", "bank-account": "Bank account number", "national-id": "National identity number", "username": "Username",
        "email-hash": "Hashed email address", "phone-hash": "Hashed phone number"}
WHO = {"account-holder": "the signed-in account holder", "guest-shopper": "a guest checking out without an account", "third-party-login": "a person signing in through an outside identity provider",
       "billing-party": "the billing party on an order", "shipping-recipient": "the person an order ships to", "card-holder": "the payment card holder", "site-member": "a member of the site",
       "contact": "a person who contacted the business", "recipient": "a recipient of a message or payment", "internal-staff": "an employee or operator", "service-account": "a machine or service account",
       "shopper": "a shopper on an order (guest or account cannot be told apart)", "login-claimant": "a person claiming an identity at a login attempt (not yet proven)", "new-registrant": "a person signing up"}
WHOSE_ROLE = {"billing": "billing-party", "shipping": "shipping-recipient", "recipient": "recipient", "card-holder": "card-holder", "contact": "contact", "sender": "contact"}
CRED = {"email", "phone", "username", "email-hash", "phone-hash"}  # the only types that mean something on a login journey; anything else there is a form artifact
def whose_of(t, role, jc, feed, authed, f):
    """-> (whose or None, how it was decided, clarity 0..1). None means undetermined and the entity is rejected."""
    n = f.get("sessions_followed", 0); fol = "followed %d sessions" % n
    if role in WHOSE_ROLE: return WHOSE_ROLE[role], f"nearest JSON ancestor says {role}", 1.0
    if t == "username" and feed: return "service-account", "usernames delivered by a machine feed", .7
    if feed:
        if role == "member": return "site-member", "ancestor says member; delivered by a feed", .8
        if role == "customer": return "shopper", "ancestor says customer; delivered by a feed", .7
        d = {"profile": "site-member", "checkout": "shopper", "contact": "contact", "signup": "new-registrant"}
        if jc in d: return d[jc], f"feed on a {jc} API", .6
        return None, "feed values on an API whose journey is unknown, so whose they are cannot be determined", 0
    if jc == "login":
        if t not in CRED: return None, "form artifact on a login journey (not an identifier of a person)", 0
        ok = (f.get("login_success_after") or 0) >= .2 or (authed or 0) >= .5
        return ("account-holder" if ok else "login-claimant"), f"login journey, {fol}: " + ("login succeeded afterwards" if ok else "no success afterwards"), .9 if n >= 10 else .6
    if jc == "third-party-login": return "third-party-login", "API path names an outside identity provider", .9
    if jc == "signup": return "new-registrant", "signup journey", .8
    hi = authed is not None and authed >= .6; lo = authed is not None and authed <= .2; cl = 1.0 if n >= 10 else .6
    if jc == "checkout":
        if hi: return "account-holder", f"checkout, {fol}: signed in before or after", cl
        if lo: return "guest-shopper", f"checkout, {fol}: never signed in", cl
        return "shopper", "checkout, sign-in state mixed or unknown", .5
    if jc == "contact": return ("account-holder", f"contact form, {fol}: signed in", cl) if hi else ("contact", "contact form", .8)
    if jc == "profile": return ("account-holder", f"profile API, {fol}: signed in", cl) if hi else ("site-member", "profile API, not clearly signed in", .6)
    if hi: return "account-holder", f"{fol}: signed in", .6
    return None, f"journey unknown and sign-in state {'unknown' if authed is None else 'mixed'}, so whose cannot be determined", 0
STRONG_K = {"email", "mail"}; MED_K = {"user", "member", "owner", "account", "login", "contact", "recipient", "customer", "principal", "author", "assignee", "reporter", "requester", "invitee"}
def kstrength(t, path):
    """how clearly the key says what the value is: 1.0 says it outright, .6 hints, 0 does not say (rejected as a location)"""
    m = re.search(r"\[([^\]]+)\]\.\w+$", path); last = TOK(m.group(1) if m else re.sub(r"\[\*\]", "", path).rsplit(".", 1)[-1])
    if t in ("email", "email-hash"):
        if set(last) & STRONG_K: return 1.0
        if set(last) & MED_K: return .5 if last[-1:] == ["id"] else .6
        return 0.0
    if t == "full-name": return .7 if last == ["name"] else 1.0
    if t == "phone": return 1.0 if set(last) & {"phone", "mobile", "msisdn", "cell", "tel", "telephone", "fax"} else .6
    if t == "username": return 1.0 if set(last) & {"username", "login", "handle"} else .8
    return 1.0
W = dict(key=.20, whose=.20, shape=.15, cardinality=.15, scope=.10, agreement=.10, stability=.10)
cand = [gk for gk, g in G.items() if len(g.vals) >= a.min_distinct or gk[0].startswith("combo:")]
need = [gk for gk in cand if not gk[0].startswith("combo:") and gk[2] in ("checkout", "contact", "profile", "general", "login", "signup")]
F = follow(need) if need else {}
ents, dropped, rows = [], [], []
def scope_rx(apis):
    segs = {tuple("[^/]+" if re.fullmatch(r"\{[^}]*\}|\*|:[A-Za-z_]\w*", s) else re.escape(s) for s in n.split(" ", 1)[-1].strip("/").split("/")) for n in apis}
    items = {(s, False) for s in segs}
    for d in range(max((len(s) for s in segs), default=0) - 1, 0, -1):
        if len(items) <= a.max_regex: break
        g = collections.defaultdict(list)
        for p, f in items: g[p[:d] if len(p) > d and any(x != "[^/]+" for x in p[:d]) else (p, f)].append((p, f))
        items = {(k, True) if len(m) > 1 else m[0] for k, m in g.items()}
    return sorted("^/" + "/".join(p) + ("(/.*)?$" if f else "$") for p, f in items)
pct = lambda xs, q: sorted(xs)[min(len(xs) - 1, int(q * len(xs)))] if xs else 0
def reject(name, reason, kind="rejected"): dropped.append(dict(entity=name, kind=kind, reason=reason))
for gk in sorted(cand, key=lambda k: -G[k].spans):
    t, role, jc, resp = gk; g = G[gk]; combo = t.startswith("combo:"); base_t = t.split(":")[-1]
    lanes_of = collections.Counter(lane_of(n)[1] for n in g.apis); top1 = (g.top.most_common(1)[0][1] / max(1, g.spans)) if g.top else 0
    feed = lanes_of.most_common(1)[0][0] in ("partner-feed", "internal") or (len(g.ips) <= 10 and len(g.vals) >= 30) or top1 >= .5 or jc == "feed"
    f = F.get(gk, {}); ab, aa = f.get("authenticated_before"), f.get("authenticated_after")
    authed = None if ab is None else max(ab, aa or 0)
    who, how, clarity = whose_of(base_t, role, jc, feed, authed, f)
    label = f"{base_t}.{who}" if who else f"{base_t}.?"
    row = dict(group=label, type=base_t, role=role, journey=jc, side="response" if resp else "request", spans=g.spans, distinct=len(g.vals), ips=len(g.ips), top1_ip=round(top1, 2), apis=len(g.apis), population="feed" if feed else "end-user",
               lane_mix=dict(lanes_of), authenticated=authed, windows_seen=len(g.win), followed=f.get("sessions_followed", 0))
    rows.append(row)
    if not who: reject(f"{base_t} ({jc})", how); continue
    # --- locations: each must say what it is (key semantics) and pass the shape check on >= 90% of its values
    locs, shape_rates, kst = [], [], []
    for (side, path), apis in sorted(g.keys.items(), key=lambda kv: -g.locspans.get(kv[0], len(kv[1]))):
        if combo: locs.append(((side, path), apis, 1.0, .8)); continue
        rate = hit[(side, path)] / max(1, tot[(side, path)]); ks = kstrength(base_t, path)
        if rate >= .9 and ks > 0: locs.append(((side, path), apis, rate, ks))
    locs = locs[:10]
    if not locs: reject(label, "no location both names the value clearly and passes the 90% shape check", "rejected"); continue
    if g.spans < 10: reject(label, f"only {g.spans} sampled values: retest on a longer window", "retest"); continue
    if len(g.win) < min(2, len(a.starts)): reject(label, f"seen in {len(g.win)} of {len(a.starts)} windows: needs at least {min(2, len(a.starts))}", "rejected"); continue
    # --- confidence components (0..1)
    ls = [g.locspans.get(l[0], 1) for l in locs]; wsum = max(1, sum(ls))
    c_key = sum(l[3] * w for l, w in zip(locs, ls)) / wsum; c_shape = sum(l[2] * w for l, w in zip(locs, ls)) / wsum
    ratio = len(g.vals) / max(1, g.spans); c_card = 1.0 if feed and len(g.vals) >= 30 else min(1.0, ratio / .5) if len(g.vals) >= a.min_distinct else .3
    napis = len({x for l in locs for x in l[1]}); c_scope = 1 - min(.5, max(0, napis - 5) / 50)
    sets = [g.locvals[l[0]] for l in locs if l[0] in g.locvals]
    c_agree = .7 if len(sets) < 2 else sum(len(x & set().union(*[y for y in sets if y is not x])) / max(1, len(x)) for x in sets) / len(sets)
    burst = len(a.starts) > 1 and len(g.win) == 1
    c_stab = (len(g.win) / len(a.starts)) * (.5 if burst else 1.0)
    comp = dict(key=c_key, whose=clarity, shape=c_shape, cardinality=c_card, scope=c_scope, agreement=c_agree, stability=c_stab)
    score = sum(W[k] * comp[k] for k in W)
    defs = []
    for (loc, apis, rate, ks) in locs:
        side, path = loc
        d = dict(location=dict(type=side, key=path or "$", **({"pair": g.pairs[loc][1:]} if loc in g.pairs else {})), conditions=[], value_transformations=[], methods=sorted(m for m in g.methods.get(loc, ()) if m), url_regexes=scope_rx(apis))
        if combo: d["combine"] = dict(op="concat", separator=" ", of="children-of-parent")
        defs.append(d)
    cnt = [len(x) for x in g.dev.values()]
    e = dict(name=f"{base_t}.{who}", description=f"{WHAT.get(base_t, base_t)} of {WHO[who]}." + (" Delivered by a partner or internal feed, so it is for investigation only." if feed else ""),
             entity_kind=(KIND.get(base_t, "String") if not combo else "String"), entity_type=base_t, whose=who, role=role or None, population=row["population"],
             hunt_use="investigate-only", sensitive=base_t in ("card-number", "national-id", "date-of-birth", "bank-account", "email", "phone", "phone-hash", "email-hash", "street-address", "address", "zip"),
             tab="review", active=False, definitions=defs, scope=dict(environments=[a.env], services="all", url_regexes=scope_rx(g.apis), methods=sorted({m for d in defs for m in d["methods"]})),
             source_agent="02", confidence=dict(score=round(score, 3), band="", components={k: round(v, 3) for k, v in comp.items()}, gates=dict(shape_pass=True, windows=f"{len(g.win)}/{len(a.starts)}", not_machine=not feed or "feed (investigate-only)")),
             whose_evidence=dict(how=how, journey_class=jc, session_follow=f or None),
             baseline=dict(values=len(g.vals), devices_per_value_median=statistics.median(cnt) if cnt else 0, devices_per_value_p95=pct(cnt, .95), share_on_2plus_devices=round(sum(c >= 2 for c in cnt) / max(1, len(cnt)), 3)),
             evidence=dict(shape_pass_rate=round(c_shape, 3), distinct=len(g.vals), spans=g.spans, apis=napis, top1_ip_share=round(top1, 2), distinct_ips=len(g.ips), lane_mix=dict(lanes_of),
                           coverage_in_api=round(sum(hit[l[0]] for l in locs) / max(1, sum(rows_api[x] for x in {y for l in locs for y in l[1]})), 3) if not combo else None),
             _w=g.spans)
    ents.append(e)
# groups that resolve to the same final name are one entity with several locations; a feed location makes the whole entity investigate-only
M = {}
for e in ents:
    m = M.setdefault(e["name"], e)
    if m is e: continue
    seen = {json.dumps(d["location"], sort_keys=True) for d in m["definitions"]}
    m["definitions"] += [d for d in e["definitions"] if json.dumps(d["location"], sort_keys=True) not in seen][:max(0, 12 - len(m["definitions"]))]
    m["scope"]["url_regexes"] = sorted(set(m["scope"]["url_regexes"]) | set(e["scope"]["url_regexes"]))[:a.max_regex * 2]
    m["scope"]["methods"] = sorted(set(m["scope"]["methods"]) | set(e["scope"]["methods"]))
    wa, wb = m["_w"], e["_w"]
    for k in m["confidence"]["components"]: m["confidence"]["components"][k] = round((m["confidence"]["components"][k] * wa + e["confidence"]["components"][k] * wb) / (wa + wb), 3)
    for k in ("distinct", "spans", "apis", "distinct_ips"): m["evidence"][k] += e["evidence"][k]
    m["baseline"]["values"] += e["baseline"]["values"]
    if e["population"] == "feed": m["population"] = "feed"
    m["_w"] = wa + wb
ents = list(M.values()); keep = []
for e in ents:
    c = e["confidence"]; c["score"] = round(sum(W[k] * c["components"][k] for k in W), 3); e.pop("_w")
    c["band"] = "extract" if c["score"] >= .85 else "review" if c["score"] >= .65 else "reject"
    if c["band"] == "reject": reject(e["name"], f"confidence {c['score']} is under .65; weakest part: {min(c['components'], key=c['components'].get)}"); continue
    e["hunt_use"] = "hunt" if c["band"] == "extract" and e["population"] == "end-user" else "investigate-only"
    e["active"] = e["hunt_use"] == "hunt"; e["tab"] = "recommended" if c["band"] == "extract" else "review"
    keep.append(e)
ents = keep; ents.sort(key=lambda x: (x["population"] != "end-user", -x["confidence"]["score"]))
os.makedirs(a.out, exist_ok=True)
json.dump(dict(schema="blue-agent/definitions@1", kind="entity", environment=a.env, window=dict(spans=sum(s["spans"] for s in astat.values()), source=f"{len(a.starts)} sampled windows of {a.minutes} min"),
               caveat="sampled windows; values never stored; whose is measured on the followed sessions only", definitions=ents, not_entity=dropped), open(f"{a.out}/entity-definitions.json", "w"), indent=1)
with open(f"{a.out}/entity-groups.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["group"]); w.writeheader(); w.writerows(rows)
print(f"{a.env}: {len(G)} groups, {len(ents)} entities ({sum(e['population'] == 'end-user' for e in ents)} end-user, {sum(e['population'] == 'feed' for e in ents)} feed; {sum(e['confidence']['band'] == 'extract' for e in ents)} extract, {sum(e['confidence']['band'] == 'review' for e in ents)} review), {len(dropped)} not entities")
