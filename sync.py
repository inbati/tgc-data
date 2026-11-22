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


def rpc_split_units(access, split_ids):
    """Fetch the units of the given project splits via the promoter's b2c RPC.
    The promoter locked direct reads on the `units` table (RLS now returns 0 to
    the b2c token), so its own portal and this sync pull units through the
    SECURITY DEFINER function b2c_list_split_units(p_split_ids). It returns the
    same snake_case unit shape build_catalog already expects (reference,
    price_asking_mad, reserved, bedrooms_count, b2c_image_files, ...), plus
    parking rows and reserved units, which build_catalog filters out."""
    if not split_ids:
        return []
    url = SUPABASE + "/rest/v1/rpc/b2c_list_split_units"
    headers = {"apikey": ANON, "Authorization": "Bearer " + access,
               "Content-Type": "application/json", "Accept": "application/json"}
    st, txt = http("POST", url, headers, json.dumps({"p_split_ids": list(split_ids)}))
    if st != 200:
        raise RuntimeError("b2c_list_split_units status %s: %s" % (st, txt[:200]))
    return json.loads(txt)


def pull_units(access):
    """Available, commercialised TGC units, pulled via the promoter b2c RPC."""
    splits = enabled_splits(access)
    if not splits:
        raise RuntimeError("no commercialised splits returned")
    print("commercialised splits:", splits)
    return rpc_split_units(access, splits)


def imgs_by_source(u):
    d = {}
    bif = u.get("b2c_image_files") or {}
    for it in (bif.get("images") or []):
        src, p = it.get("source"), it.get("path")
        if src and p:
            d.setdefault(src, []).append(IMG + p)
    return d


# ---------------------------------------------------------------------------
# Expositions relevees a la main, plan par plan, lot par lot et fenetre par
# fenetre (numero du lot -> clic sur chaque ouverture -> boussole).
#   * lots du REZ (ref ..0..): duplex rez-de-chaussee + mezzanine -> union des
#     deux niveaux (EXPO_G).
#   * lots d ETAGE (ref ..1.. a ..5..): niveau courant, identique du 1er au 5e
#     (EXPO_F, indexe par la colonne du lot).
# expoCard est une LISTE de directions (une par facade vitree): un lot d angle
# ressort sous CHAQUE exposition dans le filtre et s affiche en double/triple
# exposition. Les 8 blocs sont presents; E/F/G/H restent dormants tant qu ils
# ne sont pas commercialises.
_FR = {'NO': 'Nord-Ouest', 'NE': 'Nord-Est', 'SE': 'Sud-Est', 'SO': 'Sud-Ouest'}

EXPO_G = {
    'A': {1: ['NE', 'SE'], 2: ['NO', 'NE', 'SO'], 3: ['SO'], 4: ['NO'], 5: ['NO'], 6: ['SE'], 7: ['SE']},
    'B': {1: ['SE'], 2: ['SE'], 3: ['SE'], 4: ['NO'], 5: ['NO'], 6: ['NE'], 7: ['SO'], 8: ['NO', 'SO'], 9: ['NO', 'SO'], 10: ['SE', 'SO'], 11: ['SE']},
    'C': {1: ['SE'], 2: ['SE'], 3: ['SO'], 4: ['SE', 'SO'], 5: ['NE', 'SE'], 6: ['NO', 'NE'], 7: ['NE', 'SE'], 8: ['NO', 'NE'], 9: ['SO'], 10: ['NO'], 11: ['NO']},
    'D': {1: ['SO'], 2: ['SO'], 3: ['NE'], 4: ['NE', 'SE'], 5: ['SE'], 6: ['SE'], 7: ['NO'], 8: ['NO'], 9: ['NO', 'NE'], 10: ['NE'], 11: ['SO'], 12: ['SO']},
    'E': {1: ['SE'], 2: ['SE'], 3: ['SE'], 4: ['SO'], 5: ['NE', 'SE'], 6: ['NE', 'SE'], 7: ['NO', 'NE'], 8: ['NE', 'SE'], 9: ['NO', 'NE'], 10: ['SO'], 11: ['NO', 'SO'], 12: ['NO'], 13: ['NO'], 14: ['NO']},
    'F': {1: ['SE'], 2: ['SO'], 3: ['NE'], 4: ['SE'], 5: ['NO', 'SO'], 6: ['NO', 'NE']},
    'G': {1: ['SE'], 2: ['SE'], 3: ['SO'], 4: ['NE', 'SE']},
    'H': {1: ['NE'], 2: ['NE'], 3: ['SE']},
}

EXPO_F = {
    'A': {1: ['SE'], 2: ['NE', 'SE'], 3: ['NE', 'SE'], 4: ['NO', 'SO'], 5: ['NE', 'SO'], 6: ['SO'], 7: ['NO'], 8: ['NO'], 9: ['SE'], 10: ['SE']},
    'B': {1: ['NO'], 2: ['NO'], 3: ['NE'], 4: ['SO'], 5: ['NO', 'SO'], 6: ['NO', 'SO'], 7: ['SE', 'SO'], 8: ['SE'], 9: ['SE'], 10: ['SE'], 11: ['SE']},
    'C': {1: ['NE'], 2: ['NE'], 3: ['NE'], 4: ['SO'], 5: ['NO'], 6: ['NO'], 7: ['SE'], 8: ['SE'], 9: ['SO'], 10: ['NE', 'SO'], 11: ['NO', 'SE'], 12: ['NE']},
    'D': {1: ['NO'], 2: ['NO'], 3: ['NO', 'NE'], 4: ['NE'], 5: ['SO'], 6: ['SO'], 7: ['SO'], 8: ['SO'], 9: ['SO'], 10: ['NE'], 11: ['NE', 'SE'], 12: ['SE'], 13: ['SE']},
    'E': {1: ['NE'], 2: ['NE'], 3: ['NE'], 4: ['NE'], 5: ['NE'], 6: ['SO'], 7: ['NO', 'SO'], 8: ['NO'], 9: ['NO'], 10: ['SE'], 11: ['SE'], 12: ['SE'], 13: ['SO']},
    'F': {1: ['NO', 'SE', 'SO'], 2: ['NE', 'SO'], 3: ['NO'], 4: ['NO', 'SO'], 5: ['SE', 'SO'], 6: ['SE'], 7: ['SO'], 8: ['NE']},
    'G': {1: ['NO'], 2: ['NO'], 3: ['NO'], 4: ['NO'], 5: ['SE'], 6: ['SE'], 7: ['SO'], 8: ['NE', 'SE'], 9: ['SE']},
    'H': {1: ['SE'], 2: ['NO'], 3: ['NO'], 4: ['NO', 'SO'], 5: ['SO'], 6: ['SO'], 7: ['NE'], 8: ['NE']},
}


def _expo_label(dirs):
    names = [_FR[d] for d in dirs]
    if len(names) == 1:
        return names[0]
    tag = 'double exposition' if len(names) == 2 else 'triple exposition'
    return ' / '.join(names) + ' · ' + tag


def exposure(ref):
    try:
        table = EXPO_G if ref[1] == '0' else EXPO_F
        dirs = table[ref[0]][int(ref[2:4])]
        return [_FR[d] for d in dirs], _expo_label(dirs)
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


def disabled_splits(access):
    """Project splits NOT yet commercialised (b2c_is_enabled false) = 'tranche 2'.
    Inverse of enabled_splits; e.g. Immeuble D sits here until it goes on sale."""
    url = (SUPABASE + "/rest/v1/project_splits?select=id"
           "&project_id=eq." + str(PROJECT_ID) + "&b2c_is_enabled=eq.false")
    headers = {"apikey": ANON, "Authorization": "Bearer " + access,
               "Accept": "application/json"}
    st, txt = http("GET", url, headers)
    if st != 200:
        raise RuntimeError("disabled splits pull status %s: %s" % (st, txt[:200]))
    return [row["id"] for row in json.loads(txt)]


def build_tranche2(access):
    """Catalogue of available units in not-yet-commercialised splits (tranche 2)."""
    splits = disabled_splits(access)
    if not splits:
        print("tranche2: no not-yet-commercialised splits")
        return []
    print("tranche2 splits:", splits)
    return build_catalog(rpc_split_units(access, splits))


def diagnose_catalogue(access):
    """Read-only snapshot printed when the catalogue comes back empty, so the
    failure is self-explanatory. Lists project-5 splits and, via the b2c RPC,
    how many units/apartments (free vs reserved) each block holds. Never raises."""
    try:
        headers = {"apikey": ANON, "Authorization": "Bearer " + access,
                   "Accept": "application/json"}
        st, txt = http("GET", SUPABASE + "/rest/v1/project_splits?select=id,split_name,"
                       "b2c_is_enabled&project_id=eq." + str(PROJECT_ID), headers)
        splits = json.loads(txt) if st == 200 else []
        print("DIAG project_splits status=%s count=%d" % (st, len(splits)), file=sys.stderr)
        ids = []
        for s in splits:
            ids.append(s.get("id"))
            print("DIAG split id=%s b2c_is_enabled=%s name=%r"
                  % (s.get("id"), s.get("b2c_is_enabled"), s.get("split_name")), file=sys.stderr)
        if not ids:
            return
        try:
            units = rpc_split_units(access, ids)
        except Exception as e:
            print("DIAG rpc error:", repr(e), file=sys.stderr)
            return
        print("DIAG units via b2c_list_split_units:", len(units), file=sys.stderr)
        per = {}
        for u in units:
            b = (u.get("reference") or "?")[:1]
            d = per.setdefault(b, {"total": 0, "appt": 0, "appt_free": 0, "appt_reserved": 0})
            d["total"] += 1
            if u.get("type") == "APPARTEMENT":
                d["appt"] += 1
                if u.get("reserved"):
                    d["appt_reserved"] += 1
                else:
                    d["appt_free"] += 1
        for b in sorted(per):
            d = per[b]
            print("DIAG block %s -> total=%d appt=%d appt_free=%d appt_reserved=%d"
                  % (b, d["total"], d["appt"], d["appt_free"], d["appt_reserved"]), file=sys.stderr)
    except Exception as e:
        print("DIAG error:", repr(e), file=sys.stderr)


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
        diagnose_catalogue(access)
        sys.exit(3)

    # --- Tranche 2 (not-yet-commercialised units) + bloc D promotion ---
    # Immeuble D is on sale on inbati.com even though the promoter's b2c_is_enabled
    # is still false, so it comes back in the trache-2 (disabled) splits. We move
    # block D into the main availability list (units.json) and keep the rest
    # (E/F/G/H) in units_tranche2.json. This whole step is non-fatal: a tranche-2
    # hiccup must never block the main availability sync below, and must never wipe
    # units_tranche2.json. When the promoter finally flips D to commercialised it
    # arrives via pull_units() instead; the dedup by reference avoids any double.
    # Block D's per-unit exposure mapping is now rebuilt — read lot by lot, window
    # by window on the official floor plans (rez-de-chaussée + mezzanine for the
    # ground duplexes, étage courant for levels 1-5; see EXPO_G / EXPO_F above). It
    # goes back on the live /residences page.
    PROMOTE_BLOCKS = ("D",)
    t2 = None                 # None => leave units_tranche2.json untouched this run
    try:
        t2_all = build_tranche2(access)
        seen = {c.get("ref") for c in cat}
        promoted = 0
        for u in t2_all:
            ref = u.get("ref") or ""
            if ref[:1] in PROMOTE_BLOCKS and ref not in seen:
                cat.append(u)
                seen.add(ref)
                promoted += 1
        if promoted:
            cat.sort(key=lambda c: (c["price"] is None, c["price"]))
            print("promoted %d unit(s) from block(s) %s into units.json"
                  % (promoted, "/".join(PROMOTE_BLOCKS)))
        t2 = [u for u in t2_all if (u.get("ref") or "")[:1] not in PROMOTE_BLOCKS]
    except Exception as e:
        print("tranche2/promote: skipped due to error:", repr(e), file=sys.stderr)

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

    # Write the remaining tranche-2 catalogue (E/F/G/H) only when the pull above
    # actually succeeded (t2 is not None), so a failure never wipes the file.
    if t2 is not None:
        t2new = json.dumps(t2, ensure_ascii=False)
        t2out = os.environ.get("OUT_FILE_T2", "units_tranche2.json")
        t2old = open(t2out).read() if os.path.exists(t2out) else None
        if t2new == t2old:
            print("tranche2: no change (%d units)" % len(t2))
        else:
            with open(t2out, "w") as f:
                f.write(t2new)
            print("units_tranche2.json updated (%d units)" % len(t2))


if __name__ == "__main__":
    main()
