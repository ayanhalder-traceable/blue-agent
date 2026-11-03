#!/usr/bin/env python3
"""Turn discover.py output into user-attribution, session-identification and entity definitions. No tenant, API or header names in here.
Every carrier is scored on measured tests; name hints are only a tiebreak and mark the result `inferred`. Machines are listed under `excluded`.
Usage: derive_defs.py --dir <discover out> --env <environment> [--apis apis.json (the profile stage)] --out <dir>
Writes: user-attribution.json, session-identification.json, entity-definitions.json, carriers.csv (the scorecard)."""
import argparse, csv, json, os, re, collections

ap = argparse.ArgumentParser()
ap.add_argument("--dir", required=True); ap.add_argument("--env", required=True); ap.add_argument("--apis"); ap.add_argument("--profile", help="the profile stage profile.json (preferred over --apis: the apis table gives lanes)"); ap.add_argument("--out", required=True)
ap.add_argument("--min-users", type=int, default=5, help="fewer distinct values than this cannot be a population of people")
ap.add_argument("--shared-ips-per-value", type=float, default=20, help="more distinct IPs per value than this: a shared credential, not one person")
ap.add_argument("--consumer-share", type=float, default=0.5, help="share of a carrier's spans on consumer-lane APIs to call it end-user")
ap.add_argument("--max-regex", type=int, default=20, help="scope regexes per definition; paths fold into families until they fit")
ap.add_argument("--max-sessions", type=int, default=40, help="request-side session definitions kept (ranked: real sessions, session-named, then most distinct sessions); the rest are listed in not_session")
ap.add_argument("--max-entities", type=int, default=15)
a = ap.parse_args()
DYN = re.compile(r"^(.*[-_.:])(?=[A-Za-z0-9]{10,}$)(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9]+$|^(.*[-_.:])\d{6,}$|^()(?=[A-Za-z0-9]{16,}$)(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9]+$")  # name = stable prefix + per-user/per-site id (or the id alone): fold into one carrier `prefix*`
def fold(rows, key, sums=("spans", "eq_uid", "with_uid"), maxes=("dv", "ips", "apis")):
    out = {}
    for r in rows:
        m = DYN.match(r[key]); r = dict(r); r[key] = (next(g for g in m.groups() if g is not None) + "*") if m else r[key]
        k = tuple(r[c] for c in r if c not in sums + maxes and c != "dv")
        o = out.setdefault(k, dict(r, **{c: 0 for c in sums + maxes if c in r}))
        for c in sums:
            if c in r: o[c] = int(o[c]) + int(float(r[c] or 0))
        for c in maxes:
            if c in r: o[c] = max(int(o[c]), int(float(r[c] or 0)))
    return list(out.values())
def rd(f):
    p = f"{a.dir}/{f}.csv"
    rows = [r for r in csv.DictReader(open(p)) if None not in r.values()] if os.path.exists(p) else []  # a truncated row (value holding a newline) is dropped, not fatal
    key = {"cookies": "cookie", "jwt_claims": "name", "headers": "header", "response": "rkey"}.get(f)
    if key and f == "jwt_claims": rows = fold([r for r in rows if r["carrier"] == "cookie"], key) + [r for r in rows if r["carrier"] != "cookie"]
    elif key: rows = fold(rows, key)
    return rows
I = lambda r, k: int(float(r.get(k) or 0))
win = rd("window")[0] if rd("window") else {}
WS = I(win, "spans")

# the profile stage context: lane and verdict per API. Absent: lane unknown, confidence drops to inferred.
lane, verdict, q3cat = {}, {}, {}
_api_rows = []
if a.profile:
    _t = json.load(open(a.profile)).get("tables", {}).get("apis")
    _api_rows = [r if isinstance(r, dict) else dict(zip(_t["columns"], r)) for r in _t["rows"]] if _t else []
elif a.apis: _api_rows = json.load(open(a.apis))["apis"]
if _api_rows:
    for r in _api_rows:
        lane[r["api_name"]] = r["lane"]; verdict[r["api_name"]] = r["hunt_verdict"]
        m = re.search(r"Q3 yes \(([^)]*)\)", r.get("evidence") or ""); q3cat[r["api_name"]] = m.group(1) if m else ""
have_lanes = bool(lane)
RFC = {"iss", "aud", "exp", "nbf", "iat", "jti", "ver", "nonce", "at_hash", "c_hash", "auth_time", "amr", "acr", "azp", "typ", "alg", "kid", "scp"}  # registered or protocol claims: never a person
ROLE_HINT = re.compile(r"role|group|authorit|entitle|permission|privilege|type|level|tier", re.I)
SCOPE_HINT = re.compile(r"scope|scp|audience|grant", re.I)
SESSION_HINT = re.compile(r"sess|sid|jsessionid|phpsessid|token", re.I)
FIRST_PARTY_SESSION = re.compile(r"session|sess-?key|auth|ticket|login|jsession|bearer", re.I)  # a name that says session or auth; a bare `sid`/`id` does not count (trackers use those)
DEVICE_HINT = re.compile(r"gclid|msclkid|fbclid|utm|campaign|^_ga|^_gid|^_fbp|^_gcl|^ajs_|^amp_|^mp_|_uet|_clck|visitor|device|anon|fingerprint", re.I)
# Negative controls: carriers that look like credentials (per-visitor sized, reused) but only stitch a connection or a browser challenge, never a person's login.
# CDN / bot-challenge, load-balancer affinity and performance-monitoring cookies. Matched on the NAME only to say "not a session"; the measured tests still decide everything else.
CONNECTION_COOKIE = re.compile(r"^(__?cf|cf[-_]|_cfuvid|incap_ses|visid_incap|nlbi_|reese84|datadome|_abck|ak_bmsc|bm_(sz|sv|mi|s)\b|_px|awsalb|aws-waf|asl?bsa|ts[0-9a-f]{8,}|bigip|f5_|_?dtcookie|dt(pc|sa|ssc)\b|rxvt|___utmv|_dd_s\b|rum|newrelic|nr_)", re.I)
LOGIN_API = re.compile(r"login|log-in|logon|signin|sign-in|signon|authenticate|/auth|oauth|token|otp|sso|saml|enroll|regist|signup|sign-up|onboard", re.I)
USER_KEY = re.compile(r"^(user[-_]?(name|id|login)?|login([-_]?(id|name))?|account[-_]?(name|id|number)?|customer[-_]?(id|number|name)?|member[-_]?(id|number)|email([-_]?(address|id))?|userid)$", re.I)
TOKEN_SHAPES = {"jwt", "dotted3", "uuid", "hex", "base64", "opaque"}  # a session value looks like a credential; free text, numbers and short words never qualify
PHONE, CARD = re.compile(r"phone|mobile|msisdn|tel\b|cell", re.I), re.compile(r"card|pan\b|ccnum|cc_num|creditcard", re.I)

def share(apis_spans):  # spans-weighted share on consumer lane (None when lanes unknown)
    if not have_lanes or not apis_spans: return None  # no API breakdown for the carrier (headers are counted tenant-wide): lane unknown, not machine
    t = sum(apis_spans.values()) or 1
    return sum(s for n, s in apis_spans.items() if lane.get(n) == "consumer") / t

def card(shape, spans, dv, ips):
    if shape in ("masked", "empty"): return shape
    if spans >= 50 and dv / spans >= 0.9: return "per-request"
    if dv <= 3: return "constant"
    if dv < a.min_users: return "few-values"
    if ips / max(1, dv) > a.shared_ips_per_value: return "shared-credential"
    if dv <= 100 and spans / dv >= 200: return "small-population"
    return "per-user-or-device"

def kind_of(c, sh, conf_notes):  # end-user / machine / mixed / unknown, from the tests, never from a name
    c_, s = c["class"], c["consumer_share"]
    if c_ in ("masked", "empty"): return "unreadable"
    if c_ in ("per-request", "constant", "few-values", "shared-credential", "small-population"): return "machine"
    if s is None: conf_notes.append("lanes unknown"); return "end-user?"
    if s >= a.consumer_share: return "end-user"
    if s < 0.2:
        if c_ == "per-user-or-device" and c.get("dv", 0) >= 30 and c.get("ips_per_value", 99) <= 2:  # a gateway or app server calls on behalf of many people: per-user sized, one IP per value
            conf_notes.append("carried on machine-lane APIs but per-user sized: a gateway may be calling for end users"); return "mixed"
        return "machine"
    return "mixed"

# ---- 1. carriers ----------------------------------------------------------------------------------------------------------
C = {}
def add(key, api, spans, dv, ips, uid=0, eq=0):
    c = C.setdefault(key, dict(spans=0, dv=0, ips=0, with_uid=0, eq_uid=0, apis=collections.Counter()))
    c["spans"] += spans; c["dv"] = max(c["dv"], dv); c["ips"] = max(c["ips"], ips); c["with_uid"] += uid; c["eq_uid"] += eq; c["apis"][api] += spans
for r in rd("auth"): add(("header", f"authorization/{r['scheme']}", r["tok_shape"]), r["api_name"], I(r, "spans"), I(r, "dv"), I(r, "ips"), I(r, "with_uid"))
for r in rd("basic"): add(("basic-user", "authorization/Basic", r["user_shape"]), r["api_name"], I(r, "spans"), I(r, "dv"), I(r, "ips"), I(r, "with_uid"), I(r, "eq_uid"))
for r in rd("cookies"): add(("cookie", r["cookie"], r["v_shape"]), r["api_name"], I(r, "spans"), I(r, "dv"), I(r, "ips"), I(r, "with_uid"), I(r, "eq_uid"))
for r in rd("headers"):
    c = C.setdefault(("header", r["header"], r["v_shape"]), dict(spans=0, dv=0, ips=0, with_uid=0, eq_uid=0, apis=collections.Counter()))
    c["spans"] += I(r, "spans"); c["dv"] = max(c["dv"], I(r, "dv")); c["ips"] = max(c["ips"], I(r, "ips")); c["eq_uid"] += I(r, "eq_uid"); c["with_uid"] += I(r, "with_uid"); (c["apis"].__setitem__(r["api_name"], c["apis"][r["api_name"]] + I(r, "spans")) if r.get("api_name") else None)  # the APIs it rides on: scopes the rule to them
for k, c in C.items():
    c["class"] = card(k[2], c["spans"], c["dv"], c["ips"]); c["consumer_share"] = share(c["apis"])
    c["ips_per_value"] = round(c["ips"] / max(1, c["dv"]), 2); c["spans_per_value"] = round(c["spans"] / max(1, c["dv"]), 1)

def scope_regexes(apis_spans, only=("consumer", "partner", "other")):  # api_name -> path regexes, folded into path families
    segs = set()
    for n in apis_spans:
        if have_lanes and lane.get(n) not in only: continue
        path = n.split(" ", 1)[-1]
        segs.add(tuple("[^/]+" if re.fullmatch(r"\{[^}]*\}|\*|:[A-Za-z_]\w*", s) else re.escape(s) for s in path.strip("/").split("/")))
    # Paths sharing a prefix become one family regex `^/prefix(/.*)?$`. Fold from the deepest prefix up, and stop once the
    # list fits --max-regex, so a small API set keeps exact paths and a large one collapses to its families.
    # A family prefix needs a static (non-template) segment, so `^/[^/]+(/.*)?$` style catch-alls are never produced.
    items = {(s, False) for s in segs}  # (prefix tuple, is_family)
    for d in range(max((len(s) for s in segs), default=0) - 1, 0, -1):
        if len(items) <= a.max_regex: break
        g = collections.defaultdict(list)
        for p, f in items: g[p[:d] if len(p) > d and any(x != "[^/]+" for x in p[:d]) else (p, f)].append((p, f))
        items = {(k, True) if len(m) > 1 else m[0] for k, m in g.items()}
    return sorted("^/" + "/".join(p) + ("(/.*)?$" if f else "$") for p, f in items)

def loc(kind, name):
    l = _loc(kind, name)
    if "*" in name: l["key_pattern"] = "^" + re.escape(name).replace(r"\*", "[A-Za-z0-9]+") + "$"  # name carries a per-user/per-site id: match by pattern
    return l
def _loc(kind, name): return {"type": {"header": "Request Header", "cookie": "Request Cookie", "basic-user": "Request Header", "param": "Request Param", "body": "Request Body", "resp-cookie": "Response Cookie", "resp-body": "Response Body"}[kind], "key": name}
def attr(kind, name, cond_value=None, transforms=None, obfuscate=False):
    return {"obfuscate": obfuscate, "location": loc(kind, name),
            "conditions": ([{"location": loc(kind, name), "operator": "Matches", "value": cond_value}] if cond_value else []), "value_transformations": transforms or []}

# ---- 2. user attribution -----------------------------------------------------------------------------------------------------
users, excluded, scorecard = [], [], []
claims = collections.defaultdict(list)
for r in rd("jwt_claims"): claims[(r["carrier"], r["name"])].append(r)
jwt_carriers = {(k[0], k[1]) for k in claims}

def merge_claims(rows):
    m = {}
    for r in rows:
        x = m.setdefault(r["claim"], dict(r, spans=0, dv=0, ips=0, eq_uid=0, with_uid=0, _sh=collections.Counter()))
        sp = I(r, "spans"); x["spans"] += sp; x["dv"] = max(x["dv"], I(r, "dv")); x["ips"] = max(x["ips"], I(r, "ips")); x["eq_uid"] += I(r, "eq_uid"); x["with_uid"] += I(r, "with_uid"); x["_sh"][r["cv_shape"]] += sp
    for x in m.values(): x["cv_shape"] = x["_sh"].most_common(1)[0][0]
    return list(m.values())

def pick_user_claim(rows):  # evidence first: equals the platform user column; then person-shaped; then RFC subject; then spread
    best = None
    for r in rows:
        n, sp, dv = r["claim"], I(r, "spans"), I(r, "dv")
        if n in RFC and n != "sub": continue
        if r["cv_shape"] in ("array", "object", "null", "empty", "masked") or dv < a.min_users or dv / max(1, sp) > 0.5: continue
        eq = I(r, "eq_uid") / max(1, I(r, "with_uid")) if I(r, "with_uid") >= 10 else None
        score = (eq or 0) * 10 + {"email": 3, "uuid": 2}.get(r["cv_shape"], 1) + (1 if n == "sub" else 0) + min(dv, 1000) / 1e4
        if best is None or score > best[0]: best = (score, r, eq)
    return best

for (ct, name), rows in claims.items():
    spans = max(I(r, "spans") for r in rows); ips = max(I(r, "ips") for r in rows)
    rows = merge_claims(rows); pick = pick_user_claim(rows)
    carrier_key = next((k for k in C if k[0] == ("header" if ct == "header" else "cookie") and k[1].split("/")[0] == name and k[2] == "jwt"), None)
    c = C.get(carrier_key, dict(spans=spans, dv=0, ips=ips, apis=collections.Counter(), consumer_share=None, **{"class": "per-user-or-device"}))
    notes = []
    if pick is None:
        excluded.append(dict(carrier=f"{ct}:{name} (jwt)", reason="no claim is person-shaped with enough distinct values: identity is not in this token", claims=sorted({r["claim"] for r in rows})[:30])); continue
    _, ur, eq = pick
    # the credential's population is the claim's own distinct count, not the token's
    pop = dict(spans=I(ur, "spans"), dv=I(ur, "dv"), ips=I(ur, "ips"), **{"apis": c["apis"], "consumer_share": c["consumer_share"]})
    cls = card(ur["cv_shape"], pop["spans"], pop["dv"], pop["ips"])
    k = kind_of(dict(**{"class": cls}, consumer_share=pop["consumer_share"], dv=pop["dv"], ips_per_value=pop["ips"] / max(1, pop["dv"])), ur["cv_shape"], notes)
    scorecard.append(dict(carrier=f"{ct}:{name}", extraction=f"jwt claim {ur['claim']}", shape=ur["cv_shape"], spans=pop["spans"], distinct=pop["dv"], ips_per_value=round(pop["ips"] / max(1, pop["dv"]), 2),
                          cls=cls, consumer_share=pop["consumer_share"], caller_matches_user_column=eq, verdict=k))
    if k in ("machine", "unreadable"):
        excluded.append(dict(carrier=f"{ct}:{name} (jwt claim {ur['claim']})", reason=f"{cls}; consumer-lane share {pop['consumer_share']}", tests=dict(distinct=pop["dv"], ips_per_value=round(pop["ips"] / max(1, pop["dv"]), 2)))); continue
    role = [r for r in rows if r["claim"] not in RFC and ROLE_HINT.search(r["claim"]) and I(r, "dv") <= 50 and r["cv_shape"] not in ("empty", "null")]
    scope = [r for r in rows if SCOPE_HINT.search(r["claim"]) and r["cv_shape"] != "empty"]
    jp = lambda cl: {"type": "JSON Path", "value": f"$.{cl}"}
    dec = [{"type": "JWT Payload Decode", "platform_support": "assumed"}]
    base_tr = ([{"type": "Regex Capture Group", "value": "^(?:[A-Za-z0-9_-]+ )?(.+)$", "group": 1}] if ct == "header" else [])
    attrs = {"userId": attr("header" if ct == "header" else "cookie", name, "^(?:[A-Za-z0-9_-]+ )?eyJ", base_tr + dec + [jp(ur["claim"])]),
             "authType": {"constant": "JWT Bearer" if ct == "header" else f"JWT Cookie"}}
    if role: attrs["userRole"] = attr("header" if ct == "header" else "cookie", name, None, base_tr + dec + [jp(role[0]["claim"])])
    if scope: attrs["userScope"] = attr("header" if ct == "header" else "cookie", name, None, base_tr + dec + [jp(scope[0]["claim"])])
    conf = "measured" if (not notes and eq is not None) else "inferred"
    users.append(dict(name=f"user-{ct}-{name}-jwt-{ur['claim']}", description=f"End user from JWT claim `{ur['claim']}` carried in {ct} `{name}`", scope=dict(environments=[a.env], services="all", url_regexes=[]),  # a signed token names its person wherever it appears (the eyJ condition gates it): no API list, so APIs not yet seen are covered
                      any_api=True, attributes=attrs, custom_json=False, verdict=k, confidence=conf, evidence=dict(claim=ur["claim"], shape=ur["cv_shape"], distinct_users=pop["dv"], ips_per_value=round(pop["ips"] / max(1, pop["dv"]), 2),
                      consumer_lane_share=pop["consumer_share"], matches_platform_user_column=eq, alternates=[r["claim"] for r in rows if r["claim"] not in RFC and r is not ur and I(r, "dv") >= a.min_users][:6], notes=notes)))

for k, c in sorted(C.items(), key=lambda kv: -kv[1]["spans"]):
    kind, name, sh = k
    if kind == "basic-user" or (kind == "header" and name.startswith("authorization/") and sh != "jwt"):
        if kind == "header" and name.endswith("/Basic"): continue  # handled through its decoded user part
        notes = []; kd = kind_of(c, sh, notes)
        scorecard.append(dict(carrier=f"{kind}:{name}", extraction="whole value" if kind == "header" else "base64 user part", shape=sh, spans=c["spans"], distinct=c["dv"], ips_per_value=c["ips_per_value"], cls=c["class"], consumer_share=c["consumer_share"], caller_matches_user_column=None, verdict=kd))
        if kd in ("machine", "unreadable") or c["spans"] < 20:
            excluded.append(dict(carrier=f"{kind}:{name} ({sh})", reason=f"{c['class']}; consumer-lane share {c['consumer_share']}", tests=dict(distinct=c["dv"], ips_per_value=c["ips_per_value"], spans=c["spans"]))); continue
        tr = [{"type": "Regex Capture Group", "value": "^[A-Za-z0-9_-]+ (.+)$", "group": 1}]
        if kind == "basic-user": tr += [{"type": "Base64 Decode", "platform_support": "assumed"}, {"type": "Regex Capture Group", "value": "^([^:]*):", "group": 1}]
        if kind == "header" and sh in ("hex", "opaque", "base64", "uuid", "other"): notes.append("opaque per-user token: rotates on refresh, so it is a session-grade key, not a durable person key; prefer the platform user column where present")
        users.append(dict(name=f"user-{name.replace('/', '-')}-{sh}", description=f"End user from {name} ({sh})", scope=dict(environments=[a.env], services="all", url_regexes=scope_regexes(c["apis"])),
                          attributes={"userId": attr("header", "authorization", f"^{name.split('/')[1]} ", tr, obfuscate=True), "authType": {"constant": name.split("/")[1]}}, custom_json=False, verdict=kd,
                          confidence="measured" if not notes else "inferred", evidence=dict(shape=sh, distinct=c["dv"], ips_per_value=c["ips_per_value"], consumer_lane_share=c["consumer_share"], notes=notes)))

# Login-body identities: the name a person types at a sign-in, sign-up or token call. It exists before any credential is issued, so no header or cookie carries it.
# Chosen by the key's shape, its variety (a typed name differs per request) and a login-like path, never by tenant. A typed name is a claim, not proof, so confidence stays `inferred`.
by_key = collections.defaultdict(lambda: dict(spans=0, dv=0, ips=0, apis=collections.Counter(), shapes=collections.Counter(), src="body"))
for r in rd("params"):
    leaf = r["pkey"].split(".")[-1]
    if r["src"] != "body" or not USER_KEY.match(leaf) or not LOGIN_API.search(r["api_name"].split(" ", 1)[-1]): continue
    if r["v_shape"] not in ("email", "short", "other", "digits", "uuid") or I(r, "dv") < a.min_users or I(r, "dv") / max(1, I(r, "spans")) < 0.3: continue
    b = by_key[r["pkey"]]; b["spans"] += I(r, "spans"); b["dv"] = max(b["dv"], I(r, "dv")); b["ips"] = max(b["ips"], I(r, "ips")); b["apis"][r["api_name"]] += I(r, "spans"); b["shapes"][r["v_shape"]] += I(r, "spans")
for key, b in sorted(by_key.items(), key=lambda kv: -kv[1]["spans"]):
    ipv = round(b["ips"] / max(1, b["dv"]), 2)
    if ipv > a.shared_ips_per_value: continue
    shp = b["shapes"].most_common(1)[0][0]
    scorecard.append(dict(carrier=f"body:{key}", extraction="whole value", shape=shp, spans=b["spans"], distinct=b["dv"], ips_per_value=ipv, cls="per-user-or-device", consumer_share=share(b["apis"]), caller_matches_user_column=None, verdict="end-user (typed at login)"))
    users.append(dict(name=f"user-body-{re.sub(r'[^A-Za-z0-9]+','-',key).strip('-')}", description=f"Name a person types at a sign-in or sign-up call, in request body `{key}`", scope=dict(environments=[a.env], services="all", url_regexes=scope_regexes(b["apis"])),
                      attributes={"userId": attr("body", key), "authType": {"constant": "Login Form"}}, custom_json=False, verdict="end-user", confidence="inferred",
                      evidence=dict(shape=shp, distinct_users=b["dv"], ips_per_value=ipv, consumer_lane_share=share(b["apis"]), notes=["typed at a login or sign-up call: a claim before authentication, so it can include mistyped or attacker-supplied names"])))

# Field identities from the profile stage's anchors (tools/anchors.py): request-body, response-body and query fields on any API that name a person.
# Server-side and pre-login calls often carry no credential the browser holds, so the person is named only there. `person` means its values are
# user_identifier values (measured); a `person-candidate` is kept only when its name is a user key, and stays inferred (it may be another id space).
_done = {("body", k) for k in by_key}
_fa = []
if a.profile:
    _t = json.load(open(a.profile)).get("tables", {}).get("anchors")
    _fa = [r if isinstance(r, dict) else dict(zip(_t["columns"], r)) for r in _t["rows"]] if _t else []
FKIND = {"body": "body", "response_body": "resp-body", "query": "param"}
for r in _fa:
    if r["location"] not in FKIND or (r["location"], r["key"]) in _done or I(r, "distinct") < a.min_users: continue
    if r["verdict"] != "person" and not (r["verdict"] == "person-candidate" and USER_KEY.match(r["key"].split(".")[-1])): continue
    apis = collections.Counter({x: 1 for x in r["claim"].split(";") if x}); m = float(r["eq_user_identifier"] or 0)
    if r["verdict"] != "person":  # another id space: as a user rule it would give the same person a second id and split the journey; keygraph still links it
        excluded.append(dict(carrier=f"{r['location']}:{r['key']}", reason="names a person in another id space (its values are not user_identifier values): a rule would give the same person a second user id; usable only once a mapping to the platform user exists", tests=dict(distinct=I(r, "distinct"), spans=I(r, "spans"), matches_platform_user_column=m))); continue
    if not scope_regexes(apis):
        excluded.append(dict(carrier=f"{r['location']}:{r['key']}", reason="no consumer, partner or other-lane API carries it, so no rule scope", tests=dict(distinct=I(r, "distinct"), spans=I(r, "spans")))); continue
    scorecard.append(dict(carrier=f"{r['location']}:{r['key']}", extraction="whole value", shape="", spans=I(r, "spans"), distinct=I(r, "distinct"), ips_per_value=None, cls="per-user-or-device",
                          consumer_share=share(apis), caller_matches_user_column=m, verdict="end-user" if r["verdict"] == "person" else "end-user (candidate)"))
    users.append(dict(name=f"user-{r['location'].replace('_', '-')}-{re.sub(r'[^A-Za-z0-9]+', '-', r['key']).strip('-')}",
                      description=f"End user named in {r['location'].replace('_', ' ')} field `{r['key']}`", scope=dict(environments=[a.env], services="all", url_regexes=scope_regexes(apis)),
                      attributes={"userId": attr(FKIND[r["location"]], r["key"]), "authType": {"constant": "Request Field" if r["location"] != "response_body" else "Response Field"}}, custom_json=False,
                      verdict="end-user", confidence="measured" if r["verdict"] == "person" else "inferred",
                      evidence=dict(distinct_users=I(r, "distinct"), spans=I(r, "spans"), apis=I(r, "apis"), matches_platform_user_column=m, source="the profile stage anchors", notes=[r["evidence"]])))

_other = {r["key"]: r for r in _fa if r["location"] == "body" and r["verdict"] != "person"}  # the profile stage measured these login-body names as another id space
for k in [k for k in by_key if k in _other]:
    users[:] = [u for u in users if u["name"] != f"user-body-{re.sub(r'[^A-Za-z0-9]+','-',k).strip('-')}"]
    excluded.append(dict(carrier=f"body:{k}", reason="typed at a login call, but the profile stage measured its values as another id space (not user_identifier values): a rule would give the same person a second user id", tests=dict(matches_platform_user_column=float(_other[k]["eq_user_identifier"] or 0))))
for u in [u for u in users if not u["scope"]["url_regexes"] and not u.get("any_api")]:  # an empty scope would apply the rule to every API
    users.remove(u); excluded.append(dict(carrier=u["name"], reason="no consumer, partner or other-lane API carries it, so no rule scope"))

uidrows = rd("uid"); uid_by_shape = collections.Counter(); [uid_by_shape.update({r["uid_shape"]: I(r, "spans")}) for r in uidrows]

# ---- 3. session identification -----------------------------------------------------------------------------------------------
sessions, seen_sess, alias_of = [], [], {}
users_dv = max([I(r, "dv") for r in uidrows if r["uid_shape"] not in ("empty",)] + [0])
for k, c in sorted(C.items(), key=lambda kv: -kv[1]["spans"]):
    kind, name, sh = k
    if kind not in ("cookie", "header") or name.startswith("authorization") or sh not in TOKEN_SHAPES: continue
    if c["class"] in ("constant", "few-values", "per-request") or c["spans"] < 50 or c["spans_per_value"] < 2: continue   # a session value is REUSED across requests
    if c["ips_per_value"] > a.shared_ips_per_value: continue
    if kind == "cookie" and CONNECTION_COOKIE.search(name): seen_sess.append(f"cookie:{name} is a CDN, bot-challenge, load-balancer or monitoring cookie: it stitches a connection, not a person"); continue
    lane_note = None
    if have_lanes and c["consumer_share"] is not None and c["consumer_share"] < 0.2:
        if c["class"] == "per-user-or-device" and c["dv"] >= 30 and c["ips_per_value"] <= 2:  # per-user sized, one IP per value, but only on machine-lane APIs: a gateway or app server calls for the end user
            lane_note = f"carried by machine-lane APIs (consumer share {c['consumer_share']:.2f}) but per-user sized with {c['ips_per_value']} IP per value: likely a gateway passing the end user's session; kept at inferred confidence"
        else:
            seen_sess.append(f"{kind}:{name} is reused and per-visitor-sized but carried by machine-lane APIs (consumer share {c['consumer_share']:.2f}): not an end-user session"); continue
    sig = (c["spans"], c["dv"], c["ips"])
    if sig in alias_of: alias_of[sig]["aliases"].append(f"{kind}:{name}"); continue
    eqshare = c["eq_uid"] / max(1, c["with_uid"]) if c["with_uid"] >= 10 else None
    device = bool(DEVICE_HINT.search(name))
    role = "device-or-visitor" if device else "session"
    per_user = round(c["dv"] / users_dv, 2) if users_dv >= a.min_users else None
    conf = "measured" if (per_user is not None and not device and lane_note is None) else "inferred"
    auth_share = round(c["with_uid"] / c["spans"], 3) if c["with_uid"] and c["spans"] else None  # share of its spans where the platform already knows the user
    scorecard.append(dict(carrier=f"{kind}:{name}", extraction="whole value", shape=sh, spans=c["spans"], distinct=c["dv"], ips_per_value=c["ips_per_value"], cls=c["class"], consumer_share=c["consumer_share"], caller_matches_user_column=eqshare, verdict=role))
    if eqshare is not None and eqshare >= .8: seen_sess.append(f"{kind}:{name} equals the user column: a user key, not a session key"); continue
    tok = {"location": loc(kind, name), "operator": "equals", "key": name}
    sessions.append(alias_of.setdefault(sig, dict(name=f"session-{kind}-{re.sub(r'[^A-Za-z0-9]+','-',name)[:40]}", description=f"Session identifier in {kind} `{name}` ({sh})", aliases=[], scope=dict(environments=[a.env], services="all", url_regexes=scope_regexes(c["apis"])),
                         identifiers=[dict(type="Request", conditions=[], token=tok, value_transformations=[], obfuscated=True)], confidence=conf, multiple_per_user=(per_user is not None and per_user > 1.0), session_grade="visitor" if device else "session",
                         evidence=dict(shape=sh, distinct_sessions=c["dv"], spans_per_session=c["spans_per_value"], ips_per_session=c["ips_per_value"], sessions_per_user=per_user, consumer_lane_share=c["consumer_share"], signed_in_share=auth_share,
                                       notes=([] if per_user is not None else ["no user column in window: sessions-per-user not measurable"])
                                       + ([lane_note] if lane_note else []) + (["the platform has no user on any of these requests: this definition would fill an attribution gap"] if auth_share == 0 else []) + (["visitor or analytics id (often third-party): outlives one login session, so it stitches a visitor across visits; ranked after first-party session carriers"] if device else [])))))
for (ct, name), rows in claims.items():  # session ids carried INSIDE a jwt (any claim that is reused across requests, per-session sized, not a person key)
    for r in rows:
        if r["claim"] in RFC or r["cv_shape"] in ("array", "object", "null", "empty", "masked") or not SESSION_HINT.search(r["claim"]): continue
        if I(r, "dv") >= a.min_users and I(r, "spans") / max(1, I(r, "dv")) >= 2:
            sessions.append(dict(name=f"session-{ct}-{name}-claim-{r['claim']}", description=f"Session id in JWT claim `{r['claim']}` ({ct} `{name}`)", scope=dict(environments=[a.env], services="all", url_regexes=[]),
                                 identifiers=[dict(type="Request", conditions=[], token={"location": loc("header" if ct == "header" else "cookie", name), "operator": "equals", "key": name},
                                                   value_transformations=[{"type": "JWT Payload Decode", "platform_support": "assumed"}, {"type": "JSON Path", "value": f"$.{r['claim']}"}], obfuscated=True)],
                                 confidence="inferred", multiple_per_user=None, evidence=dict(distinct_sessions=I(r, "dv"), notes=["selected on a name hint plus reuse; verify with a time-spread check"])))
RESP_HINT = re.compile(r"token|sess|sid|jwt|auth|bearer|ticket|credential|csrf|ssid", re.I)
resp_seen = {}
for r in rd("response"):  # tokens a RESPONSE issues: Response-type session identifiers (a body key needs a credential-like name; Set-Cookie does not)
    if I(r, "dv") < a.min_users or (r["src"] == "body" and not RESP_HINT.search(r["rkey"].split(".")[-1])): continue
    if r["src"] == "cookie" and CONNECTION_COOKIE.search(r["rkey"]): seen_sess.append(f"response cookie {r['rkey']} is a CDN, bot-challenge or load-balancer cookie: not a session token"); continue
    if (r["src"], r["rkey"]) in resp_seen: resp_seen[(r["src"], r["rkey"])]["scope"]["services"].append(r["api_name"]); continue
    sessions.append(resp_seen.setdefault((r["src"], r["rkey"]), dict(name=f"session-response-{r['src']}-{re.sub(r'[^A-Za-z0-9]+','-',r['rkey'])[:40]}", description=f"Token issued in response {r['src']} `{r['rkey']}` by `{r['api_name']}`",
                         scope=dict(environments=[a.env], services=[r["api_name"]], url_regexes=[]), identifiers=[dict(type="Response", conditions=[], token={"location": loc("resp-cookie" if r["src"] == "cookie" else "resp-body", r["rkey"]), "operator": "equals", "key": r["rkey"]}, value_transformations=[], obfuscated=True)],
                         confidence="inferred", multiple_per_user=None, evidence=dict(shape=r["v_shape"], distinct=I(r, "dv"), notes=["issued on a successful response; the follow-up request must present it to become a session key"]
                         + [f"about as many distinct values as request carrier {k[0]}:{k[1]} ({c['dv']}): it is probably what presents this token" for k, c in C.items() if k[0] in ("cookie", "header") and not k[1].startswith("authorization") and k[2] in TOKEN_SHAPES and I(r, "dv") >= 50 and 0.75 <= c["dv"] / max(1, I(r, "dv")) <= 1.33][:2]))))

JOURNEY = {(r["carrier"], r["name"]): r for r in rd("journey")}  # journey.py: how many APIs one value of the carrier links
def _reach(d):
    i = d["identifiers"][0]; kind = "cookie" if "Cookie" in i["token"]["location"]["type"] else "header"
    r = JOURNEY.get((kind, i["token"]["location"]["key"])) or JOURNEY.get((kind, i["token"]["location"]["key"].lower()))
    return float(r["apis_per_value_p95"] or 0) if r else 0.0
# keygraph.py: the chain of keys that stitches the longest person-anchored journeys, and the keys whose values cross people
_gp = f"{a.dir}/journey-graph.json"
GRAPH = json.load(open(_gp)) if os.path.exists(_gp) else {}
CHAIN = {(s["carrier"], s["key"]): s for s in GRAPH.get("chain", [])[1:]}
MERGES = {(r["carrier"], r["key"]): r for r in GRAPH.get("rejected", []) if (r.get("collision_rate_if_added") or 0) > GRAPH.get("collision_budget", 1)}
def _ck(d):
    i = d["identifiers"][0]; key = i["token"]["location"]["key"]
    t = i["token"]["location"]["type"]; kind = "cookie" if "Cookie" in t else "query" if "Param" in t else "header"
    return (kind, key if kind in ("cookie", "query") else key.lower())
def _step(d): return CHAIN[_ck(d)]["step"] if _ck(d) in CHAIN else 10 ** 6
_seen, _kept = set(), []
for d in sessions:  # one definition per carrier name; request side capped by rank, the tail is reported rather than silently dropped
    k = (d["identifiers"][0]["type"], d["identifiers"][0]["token"]["location"]["key"])
    if k in _seen: continue
    _seen.add(k)
    if d["identifiers"][0]["type"] == "Request" and _ck(d) in MERGES:
        m = MERGES[_ck(d)]; seen_sess.append(f"{d['name']}: its values cross people: adding it puts {m['collision_rate_if_added']:.1%} of person-anchored requests in groups with more than one person (keygraph.py, budget {GRAPH['collision_budget']:.0%})"); continue
    if _ck(d) in CHAIN: d["evidence"]["journey_chain"] = {k: CHAIN[_ck(d)][k] for k in ("step", "gain_spans", "attributed_share", "collision_rate", "journey_apis_p95")}
    _kept.append(d)
_req = sorted((d for d in _kept if d["identifiers"][0]["type"] == "Request"), key=lambda d: (_step(d), d.get("session_grade") == "visitor", -_reach(d), not FIRST_PARTY_SESSION.search(d["identifiers"][0]["token"]["location"]["key"]), -d["evidence"].get("distinct_sessions", 0))); _resp = [d for d in _kept if d["identifiers"][0]["type"] == "Response"]
def _cover(d):  # the APIs a session definition stitches: the key may sit on a few APIs and still be the only thread across them
    i = d["identifiers"][0]
    if i["type"] == "Response": return set(d["scope"]["services"] or [])
    kind = "cookie" if "Cookie" in i["token"]["location"]["type"] else "header" if "Header" in i["token"]["location"]["type"] else "param"
    return {x for k, c in C.items() if k[0] == kind and k[1] == i["token"]["location"]["key"] for x in c["apis"]}
def _by_coverage(ranked, cap, what):
    """the first `cap` by rank are kept; past it a carrier is kept only if it reaches APIs no kept carrier reaches (a different key on a different set of APIs extends the journey). The rest are reported, not dropped silently"""
    keep, covered = [], set()
    for d in ranked:
        apis = _cover(d); new = apis - covered
        if len(keep) < cap or new or not apis: keep.append(d); covered |= apis
        else: seen_sess.append(f"{d['name']}: every API it is on ({len(apis)}) is already stitched by a higher-ranked {what} carrier")
    return keep
sessions = _by_coverage(_req, a.max_sessions, "request-side") + _by_coverage(_resp, a.max_sessions // 2, "response-issued")

# ---- 4. entities (the attribution stage: broad, from the tenant's threat model = the profile stage's Q3 categories on its hunt APIs) ----------------------
ents = collections.defaultdict(lambda: dict(spans=0, dv=0, apis=collections.Counter(), srcs=set(), shapes=collections.Counter()))
for r in rd("params"):
    fam = re.sub(r"[^a-z0-9]", "", r["pkey"].lower())
    if not fam or r["v_shape"] in ("masked", "empty", "null", "array", "object"): continue
    e = ents[fam]; e["spans"] += I(r, "spans"); e["dv"] = max(e["dv"], I(r, "dv")); e["apis"][r["api_name"]] += I(r, "spans"); e["srcs"].add((r["src"], r["pkey"])); e["shapes"][r["v_shape"]] += I(r, "spans")
entities, ent_apis = [], {}
GENERIC = re.compile(r"^(id|key|name|context|request|created|code|type|status|value|data|count|index|insertatindex|page|limit|offset|cursor|nextpagecursor|sort|order|filter|query|format|lang|locale|version|timestamp|ts|time|date|env|environment|source|callback|state|result|message|title|description)$")  # generic field words are not entities
for fam, e in ents.items():
    if GENERIC.match(fam): continue
    shape = e["shapes"].most_common(1)[0][0]
    if shape not in ("email", "ip", "uuid") and (e["dv"] < 10 or e["dv"] / max(1, e["spans"]) < 0.02): continue  # constants and categorical fields (few values, many repeats) are not entities
    if shape in ("other", "short") and not re.search(r"id|key|num|no\b|code|ref|name|addr|mail|phone|acct|account|user", fam): continue  # free text needs a name that says it is an identifier
    kind = "Email Address" if shape == "email" else "Phone Number" if (PHONE.search(fam) and shape in ("digits", "digits13+", "short", "other")) else "Credit Card" if (CARD.search(fam) and shape in ("digits", "digits13+")) else "IP Address" if shape == "ip" else "String"
    hunt = [n for n in e["apis"] if verdict.get(n) == "hunt"]
    cats = collections.Counter(q3cat.get(n, "") for n in hunt if q3cat.get(n))
    if have_lanes and not hunt: continue         # entity only on APIs that the profile stage did not call business-value worthy: out of scope for the threat model
    ent_apis[fam] = set(hunt or e["apis"])
    entities.append(dict(name=fam, entity_kind=kind, tab="recommended", active=False, definitions=[dict(location=loc("param" if s == "param" else "body", k), conditions=[]) for s, k in sorted(e["srcs"])][:8],
                         value_transformations=[], scope=dict(environments=[a.env], services="all", url_regexes=scope_regexes(collections.Counter({n: e["apis"][n] for n in (hunt or e["apis"])}))),
                         source_agent="02", threat_categories=[c for c, _ in cats.most_common(3)], confidence="measured" if have_lanes else "inferred",
                         evidence=dict(shape=shape, distinct=e["dv"], spans=e["spans"], hunt_apis=len(hunt), apis=len(e["apis"]))))
entities.sort(key=lambda x: (-x["evidence"]["hunt_apis"], -x["evidence"]["distinct"]))
not_entity, _cov, _keep = [], set(), []
for x in entities:  # the first max_entities by hunt-API reach; past that, an entity stays only if it reaches APIs no kept entity reaches
    if len(_keep) < a.max_entities or ent_apis[x["name"]] - _cov: _keep.append(x); _cov |= ent_apis[x["name"]]
    else: not_entity.append(f"{x['name']}: every API it is on is already covered by a higher-ranked entity")
entities = _keep

# ---- 5. write ----------------------------------------------------------------------------------------------------------------
os.makedirs(a.out, exist_ok=True)
meta = dict(schema="blue-agent/definitions@1", environment=a.env, window=dict(spans=WS, source=a.dir.rsplit("/", 1)[-1]),
            caveat="one short window: rare carriers can be missed; transformation types marked platform_support=assumed are not confirmed against a platform export")
json.dump(dict(meta, kind="user-attribution", definitions=users, excluded=excluded), open(f"{a.out}/user-attribution.json", "w"), indent=1)
json.dump(dict(meta, kind="session-identification", definitions=sessions, not_session=seen_sess), open(f"{a.out}/session-identification.json", "w"), indent=1)
json.dump(dict(meta, kind="entity", definitions=entities, not_entity=not_entity), open(f"{a.out}/entity-definitions.json", "w"), indent=1)
with open(f"{a.out}/carriers.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["carrier", "extraction", "shape", "spans", "distinct", "ips_per_value", "cls", "consumer_share", "caller_matches_user_column", "verdict"]); w.writeheader(); w.writerows(sorted(scorecard, key=lambda r: -r["spans"]))
print(f"{a.env}: window {WS} spans; carriers scored {len(scorecard)}; user defs {len(users)} (excluded {len(excluded)}); session defs {len(sessions)}; entity defs {len(entities)}")
