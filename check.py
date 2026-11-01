"""Domain Listing Watch: 6-hourly check, run by GitHub Actions.

Reads each watched name's Dynadot marketplace listing (official API, read-only
restricted key in the DYNADOT_API_KEY secret) and its public nameservers
(dns.google), compares with the last state, and appends any changes.
Standard library only. Writes domains.json, state.json, changes.json, checks.json.
"""
import base64
import concurrent.futures as cf
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = "https://api.dynadot.com/restful/v2/aftermarket/listings/{}?currency=USD"
UA = "domain-listing-watch/1.0 (+github actions)"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def norm_price(v):
    s = str(v).replace("$", "").replace(",", "").replace("USD", "").strip()
    f = round(float(s), 2)
    if f < 0:  # Dynadot uses -1 for "make offer" listings
        return "o"
    return str(int(f)) if f.is_integer() else repr(f)


def bootstrap():
    """Build domains.json and state.json from watch.json on the first run."""
    w = load("watch.json", None)
    rows = []
    for t in w["rows"].split(" "):
        n, a, f, i = t.split(":")
        d = n if "." in n else n + ".si"
        rows.append({
            "domain": d, "tld": "." + d.split(".")[-1],
            "priority": "Priority 101" if "p" in f else "Portfolio",
            "venue": "Spaceship only" if "s" in f else "Dynadot",
            "ask_oct31": float(a) if a else None,
            "ask_note": "Make offer" if "o" in f else ("Spaceship" if "s" in f else ""),
            "ns_oct31": w["ns"][int(i)],
        })
    if not os.path.exists("domains.json"):
        save("domains.json", rows)
    if not os.path.exists("state.json"):
        st = {}
        for r in rows:
            p = "" if r["venue"] != "Dynadot" else ("o" if r["ask_note"] == "Make offer" else norm_price(r["ask_oct31"]))
            st[r["domain"]] = {"p": p, "ns": r["ns_oct31"]}
        for e in sorted(load("changes.json", []), key=lambda e: e["at"]):
            s = st.get(e["domain"])
            if not s:
                continue
            if e["kind"] in ("price", "on_dynadot"):
                s["p"] = "o" if e["after"] == "Make offer" else norm_price(e["after"])
            elif e["kind"] == "off_dynadot":
                s["p"] = ""
            elif e["kind"] == "nameservers":
                s["ns"] = e["after"]
        save("state.json", {"source": "bootstrap", "at": now(), "domains": st})


def calibrate():
    """One-time fix after the first Dynadot API read (2026-11-01T20:05:17Z).

    The API's ask is the list price plus the buyer's 1-year renewal fee (non-.si TLDs), "make offer"
    is -1, and the 27 Spaceship-registered names are listed on Dynadot via its other-registrar
    network. Those 94 format-only differences are dropped; real events (employer.si) are kept.
    """
    AT = "2026-11-01T20:05:17Z"
    st = load("state.json", {})
    if st.get("calibrated") or st.get("source") != "api":
        return
    S, fees, keep = st["domains"], {}, []
    for e in load("changes.json", []):
        if e["at"] != AT:
            keep.append(e)
        elif e["kind"] == "price" and e["after"] == "-1":
            S[e["domain"]]["p"] = "o"
        elif e["kind"] == "price" and 0 < float(e["after"]) - float(e["before"]) <= 1500:
            fees[e["domain"]] = round(float(e["after"]) - float(e["before"]), 2)
            S[e["domain"]]["p"] = e["before"]
        elif e["kind"] == "on_dynadot":
            continue
        else:
            keep.append(e)
    dm = load("domains.json", [])
    for r in dm:
        if r["venue"] == "Spaceship only":
            r["venue"], r["ask_note"] = "Dynadot", "Spaceship-registered"
    ck = load("checks.json", [])
    for c in ck:
        if c["at"] == AT:
            c["changes"] = sum(1 for e in keep if e["at"] == AT)
            c["note"] = ("First read through the Dynadot API: renewal fees, make-offer codes and other-registrar "
                         "listings calibrated (format-only differences dropped)")
    st["fees"], st["calibrated"] = fees, AT
    save("state.json", st)
    save("changes.json", keep)
    save("domains.json", dm)
    save("checks.json", ck)
    print(f"calibrated: {len(fees)} renewal fees, kept {sum(1 for e in keep if e['at'] == AT)} real event(s)")


def get_json(url, headers, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body[:200]}
    except Exception as e:  # network error
        return 0, {"error": str(e)[:200]}


samples = []


def headers(url, key):
    """Bearer key, plus X-Signature (HMAC-SHA256 of key, path, request id, body) when a secret is set."""
    h = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    secret = os.environ.get("DYNADOT_API_SECRET", "").strip()
    if secret:
        rid = str(uuid.uuid4())
        p = urllib.parse.urlsplit(url)
        path = p.path + ("?" + p.query if p.query else "")
        msg = key + "\n" + path + "\n" + rid + "\n"
        h["X-Request-ID"] = rid
        h["X-Signature"] = base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    return h


def listing(domain, key):
    """Returns '' (not listed), 'o' (listed, no fixed price), a price string, or None (error)."""
    url = API.format(urllib.parse.quote(domain))
    for attempt in range(4):
        code, j = get_json(url, headers(url, key))
        inner = j.get("code") if isinstance(j, dict) else None
        if code in (401, 403) or inner in (401, 403):
            body = json.dumps(j)[:400].replace(key, "<key>")
            raise PermissionError(f"Dynadot refused the request (HTTP {code}, code {inner}, "
                                  f"signed={'X-Signature' in headers(url, key)}): {body}")
        if code == 404 or inner == 404:
            return ""
        if code == 200 and inner in (None, 200, 0, "200"):
            data = j.get("data") or {}
            item = data.get("listing_item") if isinstance(data, dict) and "listing_item" in data else data
            if len(samples) < 2:
                samples.append({"domain": domain, "response": j})
            if not item:
                return ""
            if isinstance(item, dict):
                for k in ("price", "buy_now_price", "buy_it_now_price", "usd_price", "listing_price", "asking_price"):
                    v = item.get(k)
                    if v not in (None, "", 0, "0", -1, "-1"):
                        try:
                            return norm_price(v)
                        except ValueError:
                            pass
            return "o"
        if code in (429, 500, 502, 503, 0) or inner in (429, 500, 503):
            time.sleep(5 * (attempt + 1))
            continue
        return None
    return None


def label(answer):
    a = [x["data"].lower().rstrip(".").split(".")[-2:] for x in answer if x.get("type") == 2]
    return "+".join(sorted({".".join(p) for p in a})) or "none"


def nameservers(domain):
    for attempt in range(3):
        code, j = get_json("https://dns.google/resolve?type=NS&name=" + urllib.parse.quote(domain), {}, timeout=15)
        if code == 200 and isinstance(j, dict):
            if j.get("Status") == 0:
                return label(j.get("Answer") or [])
            if j.get("Status") == 3:
                return "NXDOMAIN"
        time.sleep(2)
    return None


def money(p):
    return None if p == "" else ("Make offer" if p == "o" else p)


def main():
    bootstrap()
    calibrate()
    key = os.environ.get("DYNADOT_API_KEY", "").strip()
    st = load("state.json", {})
    S = st["domains"]
    fees = st.get("fees", {})  # Dynadot's API ask = list price + the buyer's 1-year renewal fee
    changes = load("changes.json", [])
    checks = load("checks.json", [])
    at = now()
    names = list(S)

    # 1) Dynadot listings, one request per name (API limit: 1 request at a time, 60 per minute).
    cur, errors, dyn = {}, 0, "ok"
    if not key:
        dyn = "no_api_key"
    else:
        try:
            for i, d in enumerate(names):
                t0 = time.time()
                v = listing(d, key)
                if v is None:
                    errors += 1
                else:
                    fee = fees.get(d)
                    if fee and v not in ("", "o"):
                        v = norm_price(float(v) - fee)
                    cur[d] = v
                time.sleep(max(0, 1.1 - (time.time() - t0)))
                if i == 30 and errors > 25:
                    dyn = "api_errors"
                    break
        except PermissionError as e:
            dyn = "api_key_rejected"
            print(str(e))
            for probe in ("https://api.dynadot.com/restful/v2/aftermarket/listings?currency=USD&page=1&page_size=5",
                          "https://api.dynadot.com/restful/v2/aftermarket/listings?currency=USD&page=1&page_size=5&show_other_registrar=true"):
                c, j = get_json(probe, headers(probe, key))
                print("probe", probe.split("?")[1], "->", c, json.dumps(j)[:600].replace(key, "<key>"))
    print("sample responses (public listing data):", json.dumps(samples)[:1500])

    # 2) Public nameservers.
    with cf.ThreadPoolExecutor(12) as ex:
        ns = dict(zip(names, ex.map(nameservers, names)))
    dns_ok = sum(v is not None for v in ns.values())

    # 3) Sanity checks before trusting the listing data.
    was = sum(1 for d in names if S[d]["p"] != "")
    now_listed = sum(1 for d in names if cur.get(d, "") != "")
    note = ""
    if dyn == "ok" and errors > len(names) * 0.1:
        dyn = "api_errors"
    if dyn == "ok" and now_listed < was * 0.9:
        dyn, note = "export_incomplete", f"only {now_listed} of {was} listings returned; ignored"
    if dyn == "ok":
        moved = sum(1 for d in names if d in cur and cur[d] not in ("", S[d]["p"]) and S[d]["p"] != "")
        if st.get("source") != "api" and moved > len(names) * 0.05:
            dyn, note = "price_format_check", f"{moved} prices differ on the first API run; held for review"

    # 4) Diff and record.
    ev = []
    for d in names:
        s = S[d]
        if dyn == "ok" and d in cur:
            p = cur[d]
            if s["p"] != "" and p == "":
                ev.append({"at": at, "domain": d, "kind": "off_dynadot", "before": money(s["p"]), "after": None})
            elif s["p"] == "" and p != "":
                ev.append({"at": at, "domain": d, "kind": "on_dynadot", "before": None, "after": money(p)})
            elif p != s["p"]:
                ev.append({"at": at, "domain": d, "kind": "price", "before": money(s["p"]), "after": money(p)})
            s["p"] = p
        n = ns.get(d)
        if n and s["ns"] and n != s["ns"]:
            ev.append({"at": at, "domain": d, "kind": "nameservers", "before": s["ns"], "after": n})
        if n:
            s["ns"] = n
    ns_ev = [e for e in ev if e["kind"] == "nameservers"]
    if len(ns_ev) > len(names) * 0.1:  # a resolver glitch, not hundreds of real moves
        for e in ns_ev:
            S[e["domain"]]["ns"] = e["before"]
        ev = [e for e in ev if e["kind"] != "nameservers"]
        note = (note + "; " if note else "") + f"{len(ns_ev)} nameserver changes at once looked like a DNS glitch; ignored"
    for e in ev:
        e["source"] = "Public DNS (dns.google)" if e["kind"] == "nameservers" else "Dynadot API"
    changes.extend(ev)

    bad_dns = len(names) - dns_ok
    status = "ok" if dyn == "ok" and bad_dns <= len(names) * 0.1 else ("failed" if dyn != "ok" and bad_dns > len(names) * 0.1 else "partial")
    notes = {"no_api_key": "DYNADOT_API_KEY secret missing; nameservers only",
             "api_key_rejected": "Dynadot rejected the API key; nameservers only",
             "api_errors": "Dynadot API errors; nameservers only"}
    checks.append({"at": at, "status": status, "dynadot": dyn, "on_dynadot": now_listed if dyn == "ok" else None,
                   "dns_ok": dns_ok, "dns_failed": bad_dns, "changes": len(ev),
                   "note": note or notes.get(dyn, ""), "source": "github-actions"})
    if dyn == "ok":
        st["source"] = "api"
    st["at"] = at
    save("state.json", st)
    save("changes.json", changes)
    save("checks.json", checks[-500:])
    print(json.dumps(checks[-1]))
    for e in ev:
        print("CHANGE", json.dumps(e))


if __name__ == "__main__":
    sys.exit(main())
