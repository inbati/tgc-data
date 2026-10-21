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
PROJECT_ID = 5                                             # The Grand Central
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


def enabled_splits(access):
    """Project splits (tranches/immeubles) that are actually commercialised.
    A split is on sale when b2c_is_enabled is true; 'Pas encore commercialisé'
    blocks (e.g. Immeuble D) have it false and must be excluded."""
    url = (SUPABASE + "/rest/v1/project_splits?select=id"
           "&project_id=eq." + str(PROJECT_ID) + "&b2c_is_enabled=eq.true")
    headers = {"apikey": ANON, "Authorization": "Bearer " + access,
               "Accept": "application/json"}
    st, txt = http("GET", url, headers)
    if st != 200:
        raise RuntimeError("splits pull status %s: %s" % (st, txt[:200]))
    ids = [row["id"] for row in json.loads(txt)]
    return ids


def pull_units(access):
    """Authenticated pull of available, commercialised TGC units."""
    splits = enabled_splits(access)
    if not splits:
        raise RuntimeError("no commercialised splits returned")
    print("commercialised splits:", splits)
    inlist = ",".join(str(x) for x in splits)
    url = (SUPABASE + "/rest/v1/units?select=*"
           "&project_split_id=in.(" + inlist + ")"
           "&type=eq.APPARTEMENT&reserved=eq.false"
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


# Exposure per column (block letter + the unit number = last 2 digits of the
# reference; units with the same number stack vertically, so exposure is per column).
#
# Orientation framework — confirmed three ways (floor-plan compass, Vertex 3D
# street labels, and the Airbus satellite view): Avenue Hassan II runs SW->NE on
# the NW side of the plot, so on the official floor plans (north points upper-left)
#   plan LEFT = Nord-Ouest (Av Hassan II)   plan RIGHT = Sud-Est
#   plan TOP  = Nord-Est                    plan BOTTOM = Sud-Ouest
# Blocks G/H sit on the Av Hassan II (NW) facade — NOT commercialised. The sellable
# A/B/C blocks are the eastern half: A/B form the east bar around the pool courtyard,
# C is the central spine between the middle patio and the pool courtyard.
#
# Each column's window/loggia/terrasse side was read on the official floor plans and
# cross-checked lot-by-lot in the Vertex 3D viewer (individual unit plans). Spot-checks
# that anchor the whole mapping: A103 terrasse -> NE exterior; A105 terrasse -> SW pool
# patio; C104 & C110 loggias -> patios; B103 loggia -> pool patio (N edge); B105 balcon
# -> SW street; C111 terrasse -> NE exterior. Each tuple is (filter cardinal, full label).
# expoCard is a LIST of cardinal directions (so a corner lot with two aspects
# surfaces under BOTH exposures in the filter and is shown as a double exposition).
_SE_AV  = (["Sud-Est"], "Sud-Est · côté avenue")
_NE_EXT = (["Nord-Est"], "Nord-Est · soleil du matin")
_SW_RUE = (["Sud-Ouest"], "Sud-Ouest · côté Rue Zineb Nafzia")
_NW_PIS = (["Nord-Ouest"], "Nord-Ouest · côté patio & piscine")
_NE_PIS = (["Nord-Est"], "Nord-Est · côté patio & piscine")
_SW_PIS = (["Sud-Ouest"], "Sud-Ouest · côté patio & piscine")
_SW_CORE = (["Sud-Ouest"], "Sud-Ouest · cœur de résidence")
_NW_CORE = (["Nord-Ouest"], "Nord-Ouest · cœur de résidence")
_SE_CORE = (["Sud-Est"], "Sud-Est · cœur de résidence")
# double-exposition corner lots (verified lot-by-lot in the Vertex 3D model):
# A col 2 is the NE/SE outer corner (terrasse 16 m² + balcon 8 m²);
# B col 7 is the SW/SE outer corner (balcon 28 m² on the Rue Zineb Nafzia side).
_NE_SE_ANGLE = (["Nord-Est", "Sud-Est"], "Nord-Est / Sud-Est · angle, double exposition")
_SW_SE_ANGLE = (["Sud-Ouest", "Sud-Est"], "Sud-Ouest / Sud-Est · angle, double exposition")
# A east bar: top row (cols 2-4) faces NE perimeter; cols 5-7 wrap the pool courtyard
# (5-6 look S into it, 7 looks W into it); col 1 faces the SE avenue.
# B east bar: cols 1-2 look W into the pool courtyard, col 3 looks N into it; cols 4-7
# front the SW street (Rue Zineb Nafzia); cols 8-11 front the SE avenue.
# C central spine: top row (cols 1-3, 11-12) faces the NE perimeter; cols 4-6 overlook
# the middle patio (4 looks S, 5-6 look W); cols 7-10 overlook the pool courtyard.
EXPO = {
    "A": {1: _SE_AV, 2: _NE_SE_ANGLE, 3: _NE_EXT, 4: _NE_EXT, 5: _SW_PIS, 6: _SW_PIS, 7: _NW_PIS},
    "B": {1: _NW_PIS, 2: _NW_PIS, 3: _NE_PIS, 4: _SW_RUE, 5: _SW_RUE, 6: _SW_RUE,
          7: _SW_SE_ANGLE, 8: _SE_AV, 9: _SE_AV, 10: _SE_AV, 11: _SE_AV},
    "C": {1: _NE_EXT, 2: _NE_EXT, 3: _NE_EXT, 4: _SW_CORE, 5: _NW_CORE, 6: _NW_CORE,
          7: _SE_CORE, 8: _SE_CORE, 9: _SE_CORE, 10: _SW_CORE, 11: _NE_EXT, 12: _NE_EXT},
}


def exposure(ref):
    try:
        card, full = EXPO[ref[0]][int(ref[2:4])]
        return card, full
    except Exception:
        return None, None


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
    for c in cat:
        card, full = exposure(c["ref"] or "")
        c["expoCard"], c["expo"] = card, full
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
