#!/usr/bin/env python3
"""Package the attribution stage's definitions and their live verification into the deliverable.
Two files. <out>.json holds ONLY the platform's own payloads, items of {"mutation": <GraphQL mutation name>, "variables": <exact variables, created disabled>}, submitted as-is.
<out>-review.json (same order) holds {kind, mutation, name, assessment} per item plus not_extracted: for the reviewer, never sent. The assessment carries
  entity_name, description, environment, services, apis, methods, confidence{score,components}, evidence, definitions (nested, every transformation and condition), ready_to_submit, notes
Every payload is checked against platform_shapes.json (the platform's own input types); one that does not fit is held in not_extracted, never shipped.
Entity scope is by API id: --api-ids <api_ids.csv from api_ids.py> resolves each definition's paths and methods to ids; without it entity_ids stay empty and ready_to_submit is false.
A definition that did not extract on the live window is held in not_extracted in the review file, never shipped. No tenant names in here.
Usage: package.py --defs <dir with the three json files and verify.csv> --customer <uuid> --out <file.json> [--api-ids <api_ids.csv>]"""
import argparse, csv, datetime, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gqlshape, platform_shapes

ap = argparse.ArgumentParser(); ap.add_argument("--defs", required=True); ap.add_argument("--customer", required=True); ap.add_argument("--out", required=True); ap.add_argument("--api-ids", default="")
a = ap.parse_args()
L = lambda f: json.load(open(f"{a.defs}/{f}.json"))
u, s, e = L("user-attribution"), L("session-identification"), L("entity-definitions")
vf = f"{a.defs}/verify.csv"
V = {r["name"]: r for r in csv.DictReader(open(vf))} if os.path.exists(vf) else {}
API = []
if a.api_ids:
    for r in csv.DictReader(open(a.api_ids)):
        m, _, path = r["api_name"].partition(" ")
        API.append((m.upper(), path, r["api_id"]))
def resolve(regexes, methods):
    """API paths (regexes) and methods -> the platform ids of the APIs seen in the window"""
    rx = [re.compile(x) for x in (regexes or [])] if isinstance(regexes, list) else []
    ms = {m.upper() for m in methods} if isinstance(methods, list) else set()
    return sorted({i for m, path, i in API if (not ms or m in ms) and (not rx or any(x.search(path) for x in rx))})
num = lambda r, k: int(r[k]) if (r.get(k) or "").isdigit() else None
BAND = lambda x: "extract" if x >= .85 else "review" if x >= .65 else "reject"
ENV = u["environment"]
# hard-gate limits. Not fitted on labelled data yet: set from the measured behaviour of true carriers (a person is on 1-3 IPs; a carrier on under 1% of its scoped spans cannot stitch a population)
COVERAGE_FLOOR, MAX_IPS_PER_VALUE = .01, 5
CONNECTION = re.compile(r"cookie-(__?cf|cf-|incap|visid|nlbi|reese|datadome|_abck|ak-bmsc|bm-|_px|awsalb|dtcookie|dt-?pc|rxvt)", re.I)  # negative control: bot-challenge, CDN, load-balancer and monitoring cookies

def verified(d):
    r = V.get(d["name"])
    if r is None: return None
    return dict(status=r["status"], spans_in_scope=num(r, "in_scope"), conditions_met=num(r, "cond_met"), extracted=num(r, "extracted"), distinct_values=num(r, "distinct_values"), equals_user_column=num(r, "eq_user_col"))

def conf_user_session(kind, d, v):
    """score for a user or session definition from its measured evidence and the live verification (equal weights)"""
    ev = d.get("evidence", {}); c = {}
    cov = (v["extracted"] / v["spans_in_scope"]) if v and v.get("spans_in_scope") and v.get("extracted") is not None else None
    c["live_coverage"] = round(min(1.0, cov / .5), 3) if cov is not None else .5  # half the scoped spans carrying it counts as full marks
    c["observed_directly"] = 1.0 if d.get("confidence") == "measured" else .6
    if kind == "user":
        eq = v.get("equals_user_column") if v else None
        c["agrees_with_platform_user_column"] = round(eq / v["extracted"], 3) if eq is not None and v.get("extracted") else .5
        c["many_distinct_users"] = round(min(1.0, (ev.get("distinct_users") or 0) / 50), 3)
        c["not_shared_across_ips"] = round(1.0 - min(1.0, max(0.0, ((ev.get("ips_per_value") or 1) - 1) / 5)), 3)
    else:
        c["session_grade"] = {"session": 1.0, "visitor": .7}.get(d.get("session_grade"), .5)
        c["rotates"] = 1.0 if (ev.get("spans_per_session") or 0) >= 2 else .5
        c["many_distinct_sessions"] = round(min(1.0, (ev.get("distinct_sessions") or 0) / 100), 3)
        c["not_shared_across_ips"] = round(1.0 - min(1.0, max(0.0, ((ev.get("ips_per_session") or 1) - 1) / 5)), 3)
    sc = round(sum(c.values()) / len(c), 3)
    # Hard gates: one failure rejects, whatever the mean says (an average hides a fatal weakness). Then a cap: a definition that was only inferred never auto-applies.
    ips = ev.get("ips_per_value") if kind == "user" else ev.get("ips_per_session")
    gates = dict(extracted_live=bool(v and (v.get("extracted") or 0) > 0),
                 coverage_floor=(cov is None or cov >= COVERAGE_FLOOR or (kind == "session" and d["identifiers"][0]["type"] == "Response")),  # a token the server issues appears once per session, on few requests: the floor does not apply to it
                 few_ips_per_value=((ips or 1) <= MAX_IPS_PER_VALUE),
                 not_connection_cookie=not CONNECTION.search(str(d.get("name", ""))))
    failed = [g for g, ok in gates.items() if not ok]
    band = "reject" if failed else BAND(sc)
    if band == "extract" and d.get("confidence") != "measured": band = "review"
    return dict(score=sc, band=band, components=c, gates=gates, failed=failed)

def nest(defs, apis, methods):
    """one extraction for every API -> common; different extractions -> broken down per API"""
    rx = lambda x: tuple(sorted(x.get("url_regexes") or []))
    strip = lambda x: {k: val for k, val in x.items() if k not in ("url_regexes", "methods")}
    if len({rx(x) for x in defs}) <= 1:
        return {"scope": "all-apis", "extract": [strip(x) for x in defs]}
    by = {}
    for x in defs: by.setdefault(rx(x), []).append(x)
    return {"scope": "per-api", "per_api": [dict(apis=list(k), methods=sorted({m for x in g for m in x.get("methods", [])}), extract=[strip(x) for x in g]) for k, g in by.items()]}

def evidence_of(v, extra=None):
    """the live check in plain words: how many requests were looked at, how many carried the value"""
    e = dict(requests_checked=v["spans_in_scope"], requests_matched=v["extracted"], distinct_values=v["distinct_values"]) if v else {}
    e.update(extra or {}); return e

def common(d, kind, sc, v, score):
    return dict(entity_name=d["name"], kind=kind, description=d["description"], environment=(sc.get("environments") or [ENV])[0], services=sc.get("services", "all"),
                apis=sc.get("url_regexes") or "all", methods=sc.get("methods") or "any", confidence=score)

def item_user(d):
    v = verified(d); c = conf_user_session("user", d, v)
    it = common(d, "user", d.get("scope", {}), v, c["score"]); it["_band"] = c["band"]; it["_weak"] = c["components"]; it["_failed"] = c["failed"]
    it["evidence"] = evidence_of(v); it["definitions"] = {"scope": "all-apis" if not d["scope"].get("url_regexes") else "listed-apis", "extract": [dict(attributes=d["attributes"], custom_json=d.get("custom_json"))]}
    it["graphql"] = gqlshape.user_rule(d)
    return it, v

def item_session(d):
    v = verified(d); c = conf_user_session("session", d, v)
    it = common(d, "session", d.get("scope", {}), v, c["score"]); it["_band"] = c["band"]; it["_weak"] = c["components"]; it["_failed"] = c["failed"]
    it["evidence"] = evidence_of(v); it["definitions"] = {"scope": "all-apis" if not d["scope"].get("url_regexes") else "listed-apis", "extract": d["identifiers"]}
    it["graphql"] = gqlshape.session_rule(d)
    return it, v

def item_entity(d, v):
    sc = d["scope"]; c = d["confidence"]
    it = common(d, "entity", sc, v, c["score"]); it["_band"] = c["band"]; it["_weak"] = c["components"]
    it.update(population=d["population"], use=d["hunt_use"], sensitive=d.get("sensitive"))
    b = d.get("baseline") or {}
    it["evidence"] = evidence_of(v, dict(typical_devices_per_value=b.get("devices_per_value_median"), share_of_values_on_multiple_devices=b.get("share_on_2plus_devices")))
    it["evidence"]["whose_basis"] = (d.get("whose_evidence") or {}).get("how")
    it["definitions"] = nest(d["definitions"], sc.get("url_regexes"), sc.get("methods"))
    it["graphql"] = gqlshape.entity_config(d, resolve if API else None)
    return it

def payload_item(it):
    """{kind, mutation, variables, assessment}: the payload on top, everything for the reviewer under assessment"""
    g = it.pop("graphql"); k = it.pop("kind")
    problems = platform_shapes.validate(g["mutation"], g["variables"])
    ev = g["variables"].get("input", {}).get("data", {}).get("span_projection", {}).get("event_derivation_configs") if k == "entity" else True
    ready = not problems and bool(ev) and (k != "entity" or all(c["scope"]["entity_scope"]["entity_ids"] for c in ev))
    it["notes"] = g["notes"]; it["ready_to_submit"] = ready
    return {"kind": k, "mutation": g["mutation"], "variables": g["variables"], "assessment": it}, problems

def ship(defs, mk):
    out, held = [], []
    for d in defs:
        it, v = mk(d)
        if v is not None and not (v["status"] == "ok" and (v["extracted"] or 0) > 0): held.append(dict(kind=it["kind"], entity_name=d["name"], reason="matched no requests in the live check"))
        elif it["_band"] == "reject": held.append(dict(kind=it["kind"], entity_name=d["name"], reason=(f"failed a hard check: {', '.join(g.replace('_', ' ') for g in it['_failed'])}" if it["_failed"] else f"confidence {it['confidence']} is under 0.65; weakest check: {min(it['_weak'], key=it['_weak'].get).replace('_', ' ')}")))
        else:
            name = d["name"]; pi, problems = payload_item(it)
            if problems: held.append(dict(kind=pi["kind"], entity_name=name, reason="payload does not fit the platform input: " + "; ".join(problems[:3])))
            else: out.append(pi)
    return out, held

def ship_entities(defs):
    """as ship(), plus: a location that extracted nothing on the live windows is dropped from the entity; a combined or first-element-checked location counts as alive when it extracts.
    A verified score is final only when live data agrees: an entity that extracts on under a tenth of its scoped spans is moved down one band."""
    out, held = [], []
    for d in defs:
        v = verified(d); r = V.get(d["name"], {})
        if v is not None and not (v["status"] == "ok" and (v["extracted"] or 0) > 0): held.append(dict(kind="entity", entity_name=d["name"], reason="matched no requests in the live check")); continue
        alive = [int(i) for i in (r.get("locations_alive") or "").split(",") if i.isdigit()]
        if alive and len(alive) < len(d["definitions"]): d["definitions"] = [x for i, x in enumerate(d["definitions"]) if i in alive]
        if v: v["locations_alive"] = len(d["definitions"]); v["locations_total"] = num(r, "locations_total")
        it = item_entity(d, v)
        if v and v.get("spans_in_scope") and v["extracted"] / v["spans_in_scope"] < .02: it["use"] = "investigate-only"  # matches under 2% of the requests: too thin to hunt on
        pi, problems = payload_item(it)
        evs = pi["variables"]["input"]["data"]["span_projection"]["event_derivation_configs"]
        if problems: held.append(dict(kind="entity", entity_name=d["name"], reason="payload does not fit the platform input: " + "; ".join(problems[:3])))
        elif not evs: held.append(dict(kind="entity", entity_name=d["name"], reason="none of its APIs matched an API id seen in the window"))
        else: out.append(pi)
    return out, held

users, uh = ship(u["definitions"], item_user); sess, sh = ship(s["definitions"], item_session); ents, eh = ship_entities(e["definitions"])
def clean(xs):
    """drop the working keys; keep the per-check scores inside evidence so a low or surprising score can be debugged"""
    for x in xs: x["assessment"]["evidence"]["confidence_breakdown"] = x["assessment"]["_weak"]
    for x in xs: x["assessment"] = {k: val for k, val in x["assessment"].items() if not k.startswith("_")}
    return xs
rej = [dict(kind="user", entity_name=x["carrier"], reason=x["reason"]) for x in u.get("excluded", [])] + [dict(kind="session", entity_name=str(x), reason="not a session key") for x in s.get("not_session", [])] \
    + [dict(kind="entity", entity_name=x["entity"], reason=x["reason"]) if isinstance(x, dict) else dict(kind="entity", entity_name=str(x).split(":")[0], reason=str(x).split(": ", 1)[-1]) for x in e.get("not_entity", [])]
gen = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
users, sess, ents = clean(users), clean(sess), clean(ents)
head = dict(customer=a.customer, environment=ENV, generated=gen, window=u["window"])
# the deliverable holds ONLY what the platform takes; scores and evidence go to the review file beside it
doc = dict(schema="blue-agent/attribution@4", **head, **{k: [dict(mutation=x["mutation"], variables=x["variables"]) for x in xs] for k, xs in (("users", users), ("sessions", sess), ("entities", ents))})
rev = dict(schema="blue-agent/attribution-review@4", **head, **{k: [dict(kind=x["kind"], mutation=x["mutation"], name=x["assessment"]["entity_name"], assessment=x["assessment"]) for x in xs] for k, xs in (("users", users), ("sessions", sess), ("entities", ents))}, not_extracted=rej + uh + sh + eh)
json.dump(doc, open(a.out, "w"), indent=2)
rf = re.sub(r"\.json$", "", a.out) + "-review.json"; json.dump(rev, open(rf, "w"), indent=2)
print(a.out, rf, {k: len(doc[k]) for k in ("users", "sessions", "entities")}, "not_extracted", len(rev["not_extracted"]))
