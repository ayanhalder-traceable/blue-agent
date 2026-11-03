"""Convert the attribution stage's definitions into the exact input objects the platform's GraphQL takes, so a delivered definition can be submitted as-is.
Shapes come from introspecting the platform schema (read-only): createUserAttributionRuleV2, createSessionIdentificationRuleV2, CreateEntityDerivationConfig.
Every converted item is created DISABLED: a person enables it after review. Nothing here talks to the platform."""
import re

UA_LOC = {"Request Header": "REQUEST_HEADER", "Request Cookie": "REQUEST_COOKIE", "Request Body": "REQUEST_BODY", "Request Param": "REQUEST_QUERY_PARAMETER",
          "Response Header": "RESPONSE_HEADER", "Response Cookie": "RESPONSE_COOKIE", "Response Body": "RESPONSE_BODY"}
SESS_REQ = {"Request Header": "HEADER", "Request Cookie": "COOKIE", "Request Body": "BODY", "Request Param": "QUERY_PARAMETER"}
SESS_RESP = {"Response Header": "HEADER", "Response Cookie": "COOKIE", "Response Body": "BODY"}
ENT_LOC = {"Request Header": "REQUEST_HEADER", "Request Body": "REQUEST_BODY", "Request Param": "REQUEST_QUERY_PARAM", "Request Cookie": "REQUEST_COOKIE",
           "Response Header": "RESPONSE_HEADER", "Response Body": "RESPONSE_BODY", "Response Cookie": "RESPONSE_COOKIE"}
KIND = {"String": "string", "Email": "email", "Email Address": "email", "Phone Number": "phone_number", "Credit Card": "credit_card", "IP Address": "ip_address",
        "Integer": "integer", "Numeric": "numeric", "Float": "float", "Boolean": "boolean", "Timestamp": "timestamp", "MAC Address": "mac_address"}
CATEGORY = {"recommended": "ENTITY_CATEGORY_RECOMMENDED", "mandatory": "ENTITY_CATEGORY_MANDATORY", "custom": "ENTITY_CATEGORY_CUSTOM"}

def _scope(sc, with_service=True):
    s = {}
    if sc.get("url_regexes"): s["urlMatchRegexes"] = sc["url_regexes"]
    if sc.get("environments"): s["environmentNames"] = sc["environments"]
    if with_service and isinstance(sc.get("services"), list) and sc["services"]: s["serviceNames"] = sc["services"]
    return s

def _key_match(loc):
    return {"matchOperator": "MATCHES_REGEX", "stringValue": loc["key_pattern"]} if loc.get("key_pattern") else {"matchOperator": "EQUALS", "stringValue": loc["key"]}

def _projections(trs, notes):
    """the attribution stage value_transformations -> valueProjections. A JWT decode followed by a JSON path on a top-level claim collapses to one claim projection"""
    out, i = [], 0
    while i < len(trs):
        t = trs[i]; ty = t["type"]
        if ty == "JWT Payload Decode":
            nxt = trs[i + 1] if i + 1 < len(trs) else None
            m = re.fullmatch(r"\$\.([A-Za-z0-9_\-:]+)", nxt.get("value", "")) if nxt and nxt["type"] == "JSON Path" else None
            if m: out.append({"valueProjectionType": "JWT_PAYLOAD_CLAIM", "jwtPayloadClaimProjection": {"claim": m.group(1)}}); i += 2; continue
            notes.append("a JWT payload decode was not followed by a top-level claim; the platform has no bare decode step"); i += 1; continue
        if ty == "Regex Capture Group": out.append({"valueProjectionType": "REGEX_CAPTURE_GROUP", "regexCaptureGroupProjection": {"regexCaptureGroup": t["value"]}})
        elif ty == "Base64 Decode": out.append({"valueProjectionType": "BASE64"})
        elif ty == "JSON Path": out.append({"valueProjectionType": "JSON_PATH", "jsonPathProjection": {"path": t["value"]}})
        elif ty == "URL Decode": out.append({"valueProjectionType": "URL_DECODE_STRING", "urlDecodeStringProjection": {"quotePlus": False}})
        else: notes.append(f"transformation '{ty}' has no platform equivalent and was left out")
        i += 1
    return out

def _attr_projection(loc, trs, notes, locmap=UA_LOC):
    if loc["type"] not in locmap: notes.append(f"location '{loc['type']}' is not accepted here"); return None
    return {"attributeKeyLocation": locmap[loc["type"]], "matchCondition": _key_match(loc), "valueProjections": _projections(trs, notes)}

def _predicate(conds, notes):
    ps = []
    for c in conds:
        ap = _attr_projection(c["location"], [], notes)
        if ap is None: continue
        op = {"Matches": "MATCHES_REGEX", "Equals": "EQUALS", "equals": "EQUALS", "Contains": "CONTAINS", "Starts With": "STARTS_WITH"}.get(c["operator"])
        if op is None: notes.append(f"condition operator '{c['operator']}' unsupported"); continue
        ap.pop("valueProjections", None)
        ps.append({"predicateType": "ATTRIBUTE", "attributePredicate": {"attributeProjection": ap, "valueMatchCondition": {"matchOperator": op, "stringValue": c["value"]}}})
    if not ps: return None
    return ps[0] if len(ps) == 1 else {"predicateType": "LOGICAL", "logicalPredicate": {"operator": "AND", "children": ps}}

def _token_rule(a, notes):
    if a is None: return None
    if "constant" in a: return {"projectionType": "LITERAL_VALUE", "literalValueProjection": {"literalValue": a["constant"]}}
    ap = _attr_projection(a["location"], a.get("value_transformations", []), notes)
    if ap is None: return None
    r = {"projectionType": "ATTRIBUTE", "attributeProjection": ap}
    p = _predicate(a.get("conditions", []), notes)
    if p: r["predicate"] = p
    if a.get("obfuscate"): r["obfuscationStrategy"] = "HASH"
    return r

def user_rule(d):
    """-> createUserAttributionRuleV2(create: ...)"""
    notes, at = [], d["attributes"]
    uid = at.get("userId") or {}
    jwt = any(t["type"] == "JWT Payload Decode" for t in uid.get("value_transformations", []))
    cr = {"template": "JWT" if jwt else "CUSTOM" if d.get("custom_json") else "BASIC", "status": {"disabled": True}, "customTokenRules": [], "name": d["name"], "description": d["description"], "scope": _scope(d["scope"])}
    for k, f in (("userIdRule", "userId"), ("userRoleRule", "userRole"), ("userScopeRule", "userScope"), ("authTypeRule", "authType")):
        r = _token_rule(at.get(f), notes)
        if r: cr[k] = r
    if "userIdRule" not in cr: notes.append("no user id rule could be built")
    for loc_key in ("Request Body", "Response Body"):
        if any((at.get(f) or {}).get("location", {}).get("type") == loc_key for f in at): notes.append("body key is passed as the attribute key; confirm on a preview that the platform reads dotted keys the same way")
    return {"mutation": "createUserAttributionRuleV2", "variables": {"create": cr}, "notes": sorted(set(notes))}

def session_rule(d):
    """-> createSessionIdentificationRuleV2(create: ...). One token rule per identifier"""
    notes, rules = [], []
    for i in d["identifiers"]:
        loc = i["token"]["location"]; resp = i["type"] == "Response"
        locmap = SESS_RESP if resp else SESS_REQ
        if loc["type"] not in locmap: notes.append(f"location '{loc['type']}' is not accepted for a session token"); continue
        ap = {"matchCondition": _key_match(loc), "valueProjections": _projections(i.get("value_transformations", []), notes)}
        if any(p["valueProjectionType"] == "URL_DECODE_STRING" for p in ap["valueProjections"]): notes.append("the session schema has no URL decode step; it was left out"); ap["valueProjections"] = [p for p in ap["valueProjections"] if p["valueProjectionType"] != "URL_DECODE_STRING"]
        tr = {"tokenType": "RESPONSE" if resp else "REQUEST",
              "sessionTokenValueRule": {"projectionRoot": {"projectionType": "ATTRIBUTE", "attributeProjection": ap}}}
        if i.get("obfuscated"): tr["sessionTokenValueRule"]["obfuscationStrategy"] = "HASH"
        if resp: tr["responseSessionTokenDetails"] = {"responseAttributeKeyLocation": locmap[loc["type"]]}
        else: tr["requestSessionTokenDetails"] = {"requestAttributeKeyLocation": locmap[loc["type"]]}
        p = _predicate(i.get("conditions", []), notes)
        if p: tr["predicate"] = p
        rules.append(tr)
    if not rules: notes.append("no token rule could be built")
    return {"mutation": "createSessionIdentificationRuleV2", "variables": {"create": {"name": d["name"], "description": d["description"], "scope": _scope(d["scope"]), "sessionTokenRules": rules}}, "notes": sorted(set(notes))}

def _val(s): return {"string_value": s}

def entity_config(d, resolve=None):
    """-> CreateEntityDerivationConfig(input: {data: ...}). The platform scopes by API entity id: `resolve(url_regexes, methods)` returns the ids (api_ids.py); a location whose APIs match no id is left out and said in notes"""
    notes, evs = [], []
    kid = "system_event_kind_" + KIND.get(d.get("entity_kind", "String"), "string")
    for x in d["definitions"]:
        loc = x["location"]
        if loc["type"] not in ENT_LOC: notes.append(f"location '{loc['type']}' unsupported"); continue
        pipe = []
        for t in x.get("value_transformations", []):
            ty = t["type"]
            if ty == "Regex Capture Group": pipe.append({"function_id": "system_defined_function_regex_extract", "parameter_values": [{"key": "pattern", "value": _val(t["value"])}]})
            elif ty == "JSON Path": pipe.append({"function_id": "system_defined_function_json_path", "parameter_values": [{"key": "path", "value": _val(t["value"])}]})
            elif ty == "Base64 Decode": pipe.append({"function_id": "system_defined_function_base64_decode", "parameter_values": []})
            elif ty == "URL Decode": pipe.append({"function_id": "system_defined_function_url_decode", "parameter_values": []})
            else: notes.append(f"transformation '{ty}' has no platform function and was left out")
        if kid != "system_event_kind_string": pipe.append({"function_id": "type_cast_to_" + kid, "parameter_values": []})
        sb = []
        for c in x.get("conditions", []):
            if c["location"]["type"] not in ENT_LOC: continue
            op = {"Matches": "FILTER_OPERATOR_REGEX_MATCHES", "Equals": "FILTER_OPERATOR_EQ", "equals": "FILTER_OPERATOR_EQ", "Contains": "FILTER_OPERATOR_CONTAINS"}.get(c["operator"])
            if op: sb.append({"operator": op, "location": {"location_type": "EXTRACTION_LOCATION_TYPE_" + ENT_LOC[c["location"]["type"]], "key": c["location"]["key"], "key_match_type": "KEY_MATCH_TYPE_EXACT"}, "value": _val(c["value"])})
        rx, ms = x.get("url_regexes") or d["scope"].get("url_regexes"), x.get("methods") or d["scope"].get("methods")
        ids = resolve(rx, ms) if resolve else []
        if resolve and not ids: notes.append("a location's APIs matched no API id seen in the window: it was left out"); continue
        scope = {"environment_scope": {"environments": d["scope"].get("environments", [])}, "entity_scope": {"entity_type": "ENTITY_TYPE_API", "entity_ids": ids}}
        if sb: scope["span_based_scope"] = {"filters": sb}
        evs.append({"name": f"{d['name']}#{len(evs) + 1}", "disabled": False, "scope": scope,
                    "span_extraction": {"location": {"location_type": "EXTRACTION_LOCATION_TYPE_" + ENT_LOC[loc["type"]], "key": loc["key"], "key_match_type": "KEY_MATCH_TYPE_EXACT"}},
                    "pipeline": {"transformation_pipeline": pipe}})
    data = {"display_name": d["name"], "description": d["description"], "disabled": True, "internal": False, "category": CATEGORY.get(d.get("tab"), "ENTITY_CATEGORY_CUSTOM"),
            "event_kind": {"kind_id": kid}, "span_projection": {"event_derivation_configs": evs}}
    if not resolve: notes.append("entity_scope.entity_ids is empty: run api_ids.py and pass --api-ids to package.py")
    return {"mutation": "CreateEntityDerivationConfig", "variables": {"input": {"data": data}}, "notes": sorted(set(notes))}
