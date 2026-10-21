#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TGC availability sync.
Runs on GitHub Actions. Authenticates to the promoter's Supabase with a
refresh token (the portal uses phone-OTP login, so there is no password grant),
pulls the current The Grand Central catalogue, and regenerates units.json with
ONLY the available apartments. The public inbati.com/residences page fetches
this units.json, so the live site always matches the promoter.

No third-party deps: standard library only.
"""
import json, os, sys, urllib.request, urllib.error, collections

SUPABASE = "https://kdjngbkgumcihyawqbhu.supabase.co"
ANON = "sb_publishable_k0B8vIHTqxO0zBjZBKk1uw_qcsvbiKm"   # public publishable key
PROJECT_SPLITS = [2, 10, 15, 32]                           # The Grand Central
IMG = "https://api.horizonmorocco.com/storage/v1/object/public/unit-images/"

RT_CACHE = os.environ.get("RT_CACHE_FILE", ".rt_cache")    # persisted rotating token
OUT = os.environ.get("OUT_FILE", "units.json")


def http(method, url, headers, body=None, timeout=60):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode()


def refresh(rt):
    """Exchange a refresh token for a fresh access token. Returns (access, new_refresh) or None."""
    url = SUPABASE + "/auth/v1/token?grant_type=refresh_token"
    headers = {"apikey": ANON, "Content-Type": "application/json"}
    try:
        st, txt = http("POST", url, headers, json.dumps({"refresh_token": rt}))
        j = json.loads(txt)
        if st == 200 and j.get("access_token") and j.get("refresh_token"):
            return j["access_token"], j["refresh_token"]
        print("refresh failed: status", st, txt[:200], file=sys.stderr)
    except urllib.error.HTTPError as e:
        print("refresh HTTPError", e.code, e.read()[:200], file=sys.stderr)
    except Exception as e:
        print("refresh error", repr(e), file=sys.stderr)
    return None


def pull_units(access):
    """Authenticated pull of all TGC units."""
    splits = ",".join(str(x) for x in PROJECT_SPLITS)
    url = (SUPABASE + "/rest/v1/units?select=*"
           "&project_split_id=in.(" + splits + ")"
           "&order=reference.asc&limit=2000")
    headers = {"apikey": ANON, "Authorization": "Bearer " + access,
               "Accept": "application/json"}
    st, txt = http("GET", url, headers)
    if st != 200:
        raise RuntimeError("units pull status %s: %s" % (st, txt[:200]))
    return json.loads(txt)


def imgs_by_source(u):
    d = {}
    bif = u.get("b2c_image_files") or {}
    for it in (bif.get("images") or []):
        src, p = it.get("source"), it.get("path")
        if src and p:
            d.setdefault(src, []).append(IMG + p)
    return d


def build_catalog(raw):
    apts = [u for u in raw if u.get("type") == "APPARTEMENT" and not u.get("reserved")]
    cat = []
    for u in apts:
        im = imgs_by_source(u)
        cat.append({
            "ref": u.get("reference"), "beds": u.get("bedrooms_count"),
            "baths": u.get("bathrooms_count"), "park": u.get("number_included_parking_bays"),
            "floor": u.get("floor"), "levels": u.get("levels_jsonb"),
            "view": u.get("view"), "orient": u.get("orientation"),
            "int": u.get("size_interior_sqm"), "ext": u.get("size_exterior_sqm"),
            "land": u.get("size_land_sqm"), "price": u.get("price_asking_mad"),
            "disc": u.get("price_discounted_mad"), "fee": u.get("b2c_reservation_fee_mad"),
            "deliv": u.get("delivery_date"),
            "rshort": u.get("mthly_rental_income_short_term_mad"),
            "rlong": u.get("mthly_rental_income_long_term_mad"),
            "img3d": (im.get("sales_3d_opaque") or [None])[0],
            "img2d": (im.get("sales_2d") or [None])[0],
            "plan": (im.get("floor_apartment_2d") or [None])[0],
        })
    # stable order: price ascending
    cat.sort(key=lambda c: (c["price"] is None, c["price"]))
    return cat


def main():
    cache_rt = None
    if os.path.exists(RT_CACHE):
        cache_rt = open(RT_CACHE).read().strip() or None
    seed_rt = (os.environ.get("TGC_REFRESH_TOKEN") or "").strip() or None

    # Try the cached (rotated) token first, fall back to the seed secret.
    res = None
    for label, rt in (("cache", cache_rt), ("seed", seed_rt)):
        if not rt:
            continue
        res = refresh(rt)
        if res:
            print("auth ok via", label)
            break
    if not res:
        print("FATAL: could not authenticate (both cache and seed refresh tokens failed).",
              file=sys.stderr)
        print("Re-seed the TGC_REFRESH_TOKEN secret with a fresh token and clear the rt caches.",
              file=sys.stderr)
        sys.exit(2)

    access, new_rt = res
    # Persist the rotated refresh token immediately so the chain survives a later failure.
    with open(RT_CACHE, "w") as f:
        f.write(new_rt)

    raw = pull_units(access)
    cat = build_catalog(raw)
    if not cat:
        print("FATAL: catalogue came back empty — aborting so we don't wipe the live page.",
              file=sys.stderr)
        sys.exit(3)

    blocks = collections.Counter((c["ref"] or "?")[0] for c in cat)
    print("available apartments:", len(cat),
          "| price sum:", sum(c["price"] or 0 for c in cat),
          "| blocks:", dict(blocks))

    new = json.dumps(cat, ensure_ascii=False)
    old = open(OUT).read() if os.path.exists(OUT) else None
    if new == old:
        print("no change")
    else:
        with open(OUT, "w") as f:
            f.write(new)
        print("units.json updated (%d apartments)" % len(cat))


if __name__ == "__main__":
    main()
