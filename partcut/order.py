"""Fix the order of lesson parts inside their MDB daily-lesson collections (type 1), from the real order in
the recording (lesson map). Dry-run by default.

Convention (user, 2026-11-26): name == str(position); 0 only for a preparation part (הכנה לשיעור), otherwise the
first part is 1; then other units (e.g. 'full' type 21) keep their name and go last.
  - collection with parts of ONE recording: matched parts by their start in the recording -> 0..k-1,
    then parts we could not match (no_source/no_media) in their current relative order,
    then other units (e.g. the full lesson, type 21, name "full") keep their name and go last.
  - parts of several recordings in one collection (e.g. Shabbat morning + evening): ordered by recording
    (date, morning < plain < evening, n1 < n2), then time; if two recordings can't be told apart -> manual.
  - one recording split over several collections of the same MDB date: merged into the collection of its
    first part (only collections holding nothing else); the emptied collections are deleted (user decision
    2026-11-26) after checking they are really empty.
  - collection "number" (lesson of the day): all daily collections of one MDB film_date numbered 1, 2, ...
    by where their first part happened (recording date, morning < evening, time in the recording) - also
    Shabbat lessons kept one collection per part; missing numbers set, contradicting ones shown as
    RENUMBER; a day with a collection we cannot place is left for a decision.
  - collections already in that order are not touched.
  - dates: date-shifted part units get the recording's date as film_date; a collection whose lesson parts
    all come from one real date gets it too (user request 2026-11-26); numbering uses the corrected date.
Writes (archive_admin token): PUT /rest/collections/{id}/content_units/{cu_id} {"name","position"};
PUT /rest/collections/{id}/ {"properties": {...}}; PUT /rest/content_units/{id}/ {"properties": {"film_date"}};
POST/DELETE collection content units and DELETE /rest/collections/{id}/ for merges.
"""
import logging
import re

import requests

from . import config, mdb

log = logging.getLogger("partcut.order")
DAILY_LESSON = 1
LESSON_PART = 11
import re as _re
PARASHA = _re.compile(r"parashat|parasha")


def _entries(col_id):
    out = []
    for x in mdb._get("/collections/%d/content_units/" % col_id) or []:
        cu = x["content_unit"]
        he = ((cu.get("i18n") or {}).get("he") or {}).get("name") or ""
        out.append({"cu_id": cu["id"], "uid": cu["uid"], "type_id": cu["type_id"], "name": x.get("name"),
                    "position": x.get("position"), "title": he,
                    "film_date": (cu.get("properties") or {}).get("film_date")})
    return out


def rec_key(name):
    """Order of recordings of one day: date, morning < plain < evening, then numbers (n1 < n2, part1 < part2)."""
    import re
    n = name.lower()
    date = (re.search(r"\d{4}-\d{2}-\d{2}", n) or [""])[0]
    # Friday: the weekly portion (parashat ha-shavua) is the day's 2nd lesson, after the lesson (user, 2026-11-26)
    rank = 0 if re.search(r"boker|morning", n) else 3 if re.search(r"erev|evening|seuda|layla", n) else \
        2 if PARASHA.search(n) else 1
    rest = n.replace(date, "")
    nums = tuple(int(x) for x in re.findall(r"(?:_n|part_?|_p)(\d+)", rest))
    return (date, rank, nums)


def plan(state, dates):
    """-> list of collection plans: {col, uid, kind: change|ok|manual, reason, rows:[...]}"""
    cols = {}
    for r in state.values():
        if r.get("date") not in dates:
            continue
        for c in r.get("collections") or []:
            if c["type_id"] == DAILY_LESSON:
                cols[c["id"]] = c["uid"]
    dates = set(dates) | {mdb.real_date(r) for r in state.values() if r.get("date") in dates and r.get("date_shift")}
    for d in sorted(dates):        # every daily collection of the date (also those of unmatched parts)
        for c in mdb._get("/collections/", content_type="DAILY_LESSON", start_date=d, end_date=d,
                          page_size=50).get("data") or []:
            cols.setdefault(c["id"], c["uid"])
    by_uid = {r.get("part_uid"): r for r in state.values() if r.get("part_uid")}
    plans = []
    for col_id, col_uid in sorted(cols.items()):
        try:
            ents = _entries(col_id)
        except requests.HTTPError as e:      # deleted (e.g. merged by an earlier fix-order); state still names it
            if e.response is not None and e.response.status_code == 404:
                continue
            raise
        # parts without the Rav are out of scope: kept in place after the Rav's parts, never block a day
        norav = {e["uid"] for e in ents if e["type_id"] == LESSON_PART and
                 (by_uid.get(e["uid"]) or {}).get("status") in ("skip_norav", "no_media")}   # no_media: nothing to place
        parts = [e for e in ents if e["type_id"] == LESSON_PART and e["uid"] not in norav]
        others = [e for e in ents if e["type_id"] != LESSON_PART or e["uid"] in norav]
        matched, unmatched = [], []
        for e in parts:
            r = by_uid.get(e["uid"])
            if r and r.get("start") is not None and r.get("full") and r.get("lesson"):
                matched.append((e, r))
            else:
                unmatched.append(e)
        recs = sorted({r["lesson"]["recording"] for _, r in matched}, key=rec_key)
        split = sorted({u for _, r in matched for u in r["lesson"].get("daily_collections") or []} - {col_uid})
        p = {"col": col_id, "uid": col_uid, "recordings": recs, "split_with": split, "rows": [],
             "has_parts": bool(parts),
             # where this collection's first part happened: recording order of the day, then time in it
             "first": min(((rec_key(r["lesson"]["recording"]), r["start"]) for _, r in matched), default=None)}
        col = mdb._get("/collections/%d/" % col_id)
        p["film_date"] = (col.get("properties") or {}).get("film_date")
        p["number_now"] = (col.get("properties") or {}).get("number")
        p["number"] = {"now": p["number_now"], "set": None, "note": ""}
        if not parts:
            p.update(kind="skip", reason="no lesson part (types %s) - not numbered" % sorted({e["type_id"] for e in ents}))
        elif not matched:
            p.update(kind="manual", reason="no part of this collection was matched to a recording")
        elif len(recs) > 1 and len({rec_key(x) for x in recs}) < len(recs):
            p.update(kind="manual", reason="parts of %d recordings whose order is not clear: %s" % (len(recs), ", ".join(recs)))
        else:
            cur = lambda e: (e["position"] if e["position"] is not None else 10 ** 6, str(e["name"]))
            new = [e for e, r in sorted(matched, key=lambda x: (rec_key(x[1]["lesson"]["recording"]), x[1]["start"]))] \
                + sorted(unmatched, key=cur) \
                + sorted(others, key=cur)
            for i, e in enumerate(new):
                name = str(i) if e["type_id"] == LESSON_PART else e["name"]
                r = by_uid.get(e["uid"])
                p["rows"].append({**e, "new_position": i, "new_name": name,
                                  "status": "matched" if any(e is m for m, _ in matched) else
                                  ("out of scope: " + r["status"] if r and r.get("status") in ("skip_norav", "no_media") else
                                   "unmatched:" + (r.get("status") if r else "not in partcut") if e["type_id"] == LESSON_PART
                                   else "other unit type %d" % e["type_id"]),
                                  "rec_start": r.get("start") if r else None,
                                  "recording": r["lesson"]["recording"] if r and r.get("lesson") else None,
                                  "change": (e["position"], e["name"]) != (i, name)})
            changed = any(x["change"] for x in p["rows"])
            p.update(kind="change" if changed else "ok",
                     reason="split with %s (listed only; positions here still follow the recording)" % split
                     if split else "")
        plans.append(p)
    _splits(plans, by_uid)
    _merges(plans, by_uid)
    _real_dates(plans, by_uid)
    _number_days(plans)
    _names(plans)
    return plans


def _real_dates(plans, by_uid):
    """MDB dates from the recordings: date-shifted part units get the recording's date as film_date; a
    collection whose lesson parts all come from one real date gets that date too."""
    for p in plans:
        p["unit_dates"] = []
        p["date_new"] = None
        if p["kind"] in ("merged", "skip"):
            continue
        parts = [x for x in p["rows"] if x["type_id"] == LESSON_PART]
        for x in parts:
            r = by_uid.get(x["uid"])
            if r and r.get("date_shift") and x.get("film_date") != mdb.real_date(r):
                p["unit_dates"].append((x["uid"], x["cu_id"], x.get("film_date"), mdb.real_date(r)))
        real = {mdb.real_date(by_uid[x["uid"]]) for x in parts if x["status"] == "matched"}
        if len(real) == 1 and all(x["status"] == "matched" for x in parts):
            d = real.pop()
            if d != p["film_date"]:
                p["date_new"] = d
        elif len(real) > 1 or (real and real != {p["film_date"]}):
            p["date_note"] = "collection film_date %s kept: its parts come from %s%s" % (
                p["film_date"], sorted(real), " plus unmatched part(s)" if any(x["status"] != "matched" for x in parts) else "")
        if (p["unit_dates"] or p["date_new"]) and p["kind"] == "ok":
            p["kind"] = "change"


def _splits(plans, by_uid):
    """A daily collection holding the day's lesson AND the weekly-portion (parashat ha-shavua) lesson: the
    portion's parts move to a new daily collection of the same date - the day's next lesson (user, 2026-11-26)."""
    for p in list(plans):
        if p["kind"] not in ("change", "ok") or len(p["recordings"]) < 2:
            continue
        par = [x for x in p["recordings"] if PARASHA.search(x.lower())]
        if not par or len(par) == len(p["recordings"]):
            continue
        out = [x for x in p["rows"] if x.get("recording") in par]
        keep = [x for x in p["rows"] if x not in out]
        def renum(rows):
            res, i = [], 0
            for x in rows:
                name = str(i) if x["type_id"] == LESSON_PART else x["name"]
                res.append({**x, "new_position": i, "new_name": name, "change": (x["position"], x["name"]) != (i, name)})
                i += 1
            return res
        p["rows"] = renum(keep)
        p["recordings"] = [x for x in p["recordings"] if x not in par]
        p["first"] = min(((rec_key(x["recording"]), x["rec_start"]) for x in keep if x.get("recording")), default=None)
        p["kind"] = "change"
        p["split_out"] = [x["uid"] for x in out]
        p["reason"] = "weekly portion %s split out to a new collection (next lesson of the day)" % par
        new = {"col": None, "uid": "NEW(%s)" % p["uid"], "kind": "new", "recordings": par, "split_with": [],
               "has_parts": True, "film_date": p["film_date"], "number_now": None,
               "number": {"now": None, "set": None, "note": ""}, "split_from": p["uid"],
               "first": min((rec_key(x["recording"]), x["rec_start"]) for x in out),
               "rows": [{**x, "moved_from": p["uid"]} for x in renum(out)],
               "reason": "new daily collection for the weekly portion, parts moved from %s" % p["uid"]}
        for x in new["rows"]:
            x["change"] = True
        plans.insert(plans.index(p) + 1, new)


def _merges(plans, by_uid):
    """Parts of ONE recording spread over several daily collections of the same MDB date (e.g. Shabbat lessons
    kept one collection per part): merge them into the collection of the recording's first part. Only
    collections holding nothing but parts of that recording are merged (and then deleted, emptied)."""
    groups = {}
    for p in plans:
        if p["kind"] in ("change", "ok") and len(p["recordings"]) == 1:
            groups.setdefault((p["film_date"], p["recordings"][0]), []).append(p)
    for (fd, rec), ps in groups.items():
        if len(ps) < 2:
            continue
        pure = [p for p in ps if all(x["status"] == "matched" for x in p["rows"])]
        target = min(ps, key=lambda p: p["first"])
        sources = [p for p in pure if p is not target]
        if not sources:
            continue
        moved = [x for p in sources for x in p["rows"]]
        mine = [x for x in target["rows"] if x["status"] == "matched"]
        rest = [x for x in target["rows"] if x["status"] != "matched"]
        allp = sorted(mine + moved, key=lambda x: (by_uid[x["uid"]]["start"]))
        rows = []
        for i, x in enumerate(allp + rest):
            name = str(i) if x["type_id"] == LESSON_PART else x["name"]
            new = x in moved
            rows.append({**x, "new_position": i, "new_name": name, "moved_from": x.get("moved_from") or
                         (next(p["uid"] for p in sources if x in p["rows"]) if new else None),
                         "change": new or (x["position"], x["name"]) != (i, name)})
        target["rows"] = rows
        target["kind"] = "change"
        target["merge_from"] = [p["uid"] for p in sources]
        target["reason"] = "parts of %s merged in (same recording)" % target["merge_from"]
        for p in sources:
            p["kind"] = "merged"
            p["merge_into"] = target["uid"]
            p["reason"] = "merged into %s (same recording %s), then deleted" % (target["uid"], rec)
            p["has_parts"] = False          # no longer a lesson of the day


def _merged_targets(plans):
    return {p["uid"]: p for p in plans if p.get("merge_from")}


def _number_days(plans):
    """Lessons of one day (MDB film_date) numbered 1, 2, ... in recording order (Shabbat: morning, evening, ...).
    Collections without any lesson part (virtual lesson, empty) are left out."""
    by_date = {}
    for p in plans:
        fd = p.get("date_new") or p["film_date"]
        if fd and p["has_parts"]:
            by_date.setdefault(fd, []).append(p)
    for fd, ps in by_date.items():
        placed = [p for p in ps if p["recordings"]]
        unplaced = [p for p in ps if not p["recordings"]]
        if unplaced:
            for p in ps:
                if p["number_now"] is None:
                    p["number"]["note"] = "no number; day %s has collection(s) with no matched part %s -> decide" % (
                        fd, [x.get("uid") for x in unplaced])
            continue
        order = sorted(placed, key=lambda p: p["first"])
        for n, p in enumerate(order, start=1):
            if p["number_now"] != n:
                p["number"]["set"] = n
                p["number"]["note"] = ("no number -> %d" % n if p["number_now"] is None
                                       else "RENUMBER %s -> %d (recording order of %s)" % (p["number_now"], n, fd))
    for p in plans:
        if p["number"]["set"] and p["kind"] == "ok":
            p["kind"] = "change"
    return plans


PREP = re.compile(r"^\s*(הכנה לשיעור|הכנה)\b|preparation to the lesson|lesson preparation", re.I)


def _names(plans):
    """Final names/positions in the order already decided: part 0 only for a preparation part, otherwise the
    parts are 1, 2, ...; other units follow with their own name."""
    for p in plans:
        if p["kind"] in ("merged", "skip", "manual") or not p.get("rows"):
            continue
        parts = [x for x in p["rows"] if x["type_id"] == LESSON_PART]
        k = 0 if parts and PREP.search(parts[0].get("title") or "") else 1
        rows = parts + [x for x in p["rows"] if x["type_id"] != LESSON_PART]
        for x in rows:
            x["new_position"] = k
            x["new_name"] = str(k) if x["type_id"] == LESSON_PART else x["name"]
            x["change"] = bool(x.get("moved_from")) or (x["position"], x["name"]) != (k, x["new_name"])
            k += 1
        p["rows"] = rows
        if p["kind"] == "ok" and any(x["change"] for x in rows):
            p["kind"] = "change"


def show(plans):
    out = []
    for p in plans:
        out.append("=== collection %s (id %s)  %s  %s %s" % (p["uid"], p["col"], p["kind"].upper(),
                                                          " ".join(p["recordings"]), p.get("reason") or ""))
        n = p.get("number") or {}
        if n.get("set") or n.get("note"):
            out.append("  collection number: %s" % n["note"])
        if p.get("date_new"):
            out.append("  collection film_date: %s -> %s (real date of its lesson)" % (p["film_date"], p["date_new"]))
        if p.get("date_note"):
            out.append("  %s" % p["date_note"])
        for uid, _, old, new in p.get("unit_dates") or []:
            out.append("  part %s film_date: %s -> %s (recording date)" % (uid, old, new))
        if p.get("split_out"):
            out.append("  SPLIT: parts %s move to a new collection" % p["split_out"])
        if p.get("merge_from"):
            out.append("  MERGE: parts of %s move into this collection; those collections are then deleted" % p["merge_from"])
        if p["kind"] == "merged":
            out.append("  -> its part(s) move to %s; this collection is DELETED once empty" % p["merge_into"])
            continue
        for x in p["rows"]:
            t = "  %-8s pos %s name %-6s -> pos %d name %-6s %-22s %s" % (
                x["uid"], x["position"], repr(x["name"]), x["new_position"], repr(x["new_name"]), x["status"],
                x["title"][:45])
            out.append(t + (("   (moved here from %s)" % x["moved_from"]) if x.get("moved_from") else
                            "" if x["change"] else "   (unchanged)"))
    return "\n".join(out)


def execute(plans):
    """Write only collections of kind 'change'; read each back and check. -> list of (collection uid, ok, detail)"""
    res = []
    by_uid = {p["uid"]: p for p in plans}
    for p in plans:                       # 1. merges: add parts to the target, remove them from the sources
        if not p.get("merge_from"):
            continue
        moved = [x for x in p["rows"] if x.get("moved_from")]
        r = mdb._s.post("%s/rest/collections/%d/content_units/" % (config.MDB, p["col"]),
                        json=[{"content_unit_id": x["cu_id"], "name": x["new_name"], "position": x["new_position"]}
                              for x in moved], timeout=60)
        r.raise_for_status()
        here = {e["uid"] for e in _entries(p["col"])}
        if any(x["uid"] not in here for x in moved):
            raise RuntimeError("merge into %s: parts missing after adding them" % p["uid"])
        for x in moved:
            src = by_uid[x["moved_from"]]
            r = mdb._s.delete("%s/rest/collections/%d/content_units/%d" % (config.MDB, src["col"], x["cu_id"]), timeout=60)
            r.raise_for_status()
        for su in p["merge_from"]:
            src = by_uid[su]
            left = _entries(src["col"])
            if left:                       # MDB's delete would silently drop what is still attached
                raise RuntimeError("collection %s not empty after the merge (%s) - not deleted" % (su, [e["uid"] for e in left]))
            r = mdb._s.delete("%s/rest/collections/%d/" % (config.MDB, src["col"]), timeout=60)
            r.raise_for_status()
            log.info("collection %s merged into %s and deleted", su, p["uid"])
            res.append((su, True, "merged into %s, deleted" % p["uid"]))
    for p in plans:                       # 2. weekly-portion splits: new collection, move the parts there
        if p["kind"] != "new":
            continue
        src = by_uid[p["split_from"]]
        props = {"film_date": p.get("date_new") or p["film_date"], "original_language": "he"}
        if p["number"]["set"]:
            props["number"] = p["number"]["set"]
        r = mdb._s.post("%s/rest/collections/" % config.MDB, json={"type_id": DAILY_LESSON, "properties": props},
                        timeout=60)
        r.raise_for_status()
        c = r.json()
        p["col"], p["uid"] = c["id"], c["uid"]
        log.info("collection %s created (%s) for the weekly portion split from %s", c["uid"], props, src["uid"])
        r = mdb._s.post("%s/rest/collections/%d/content_units/" % (config.MDB, p["col"]),
                        json=[{"content_unit_id": x["cu_id"], "name": x["new_name"], "position": x["new_position"]}
                              for x in p["rows"]], timeout=60)
        r.raise_for_status()
        now = {e["uid"]: (e["position"], e["name"]) for e in _entries(p["col"])}
        bad = [x["uid"] for x in p["rows"] if now.get(x["uid"]) != (x["new_position"], x["new_name"])]
        if bad:
            raise RuntimeError("split into %s: parts %s missing/wrong after adding them" % (p["uid"], bad))
        for x in p["rows"]:
            r = mdb._s.delete("%s/rest/collections/%d/content_units/%d" % (config.MDB, src["col"], x["cu_id"]), timeout=60)
            r.raise_for_status()
        cprops = mdb._get("/collections/%d/" % p["col"]).get("properties") or {}
        bad = [k for k, v in props.items() if cprops.get(k) != v]
        res.append((p["uid"], not bad, "new collection, split from %s%s" % (src["uid"], ", mismatch %s" % bad if bad else "")))
    for p in plans:
        if p["kind"] != "change":
            continue
        props = {}
        if p["number"]["set"]:
            props["number"] = p["number"]["set"]
        if p.get("date_new"):
            props["film_date"] = p["date_new"]
        if props:
            r = mdb._s.put("%s/rest/collections/%d/" % (config.MDB, p["col"]), json={"properties": props}, timeout=60)
            r.raise_for_status()
        for uid, cu_id, old, new in p.get("unit_dates") or []:
            r = mdb._s.put("%s/rest/content_units/%d/" % (config.MDB, cu_id), json={"properties": {"film_date": new}},
                           timeout=60)
            r.raise_for_status()
        for x in p["rows"]:
            if not x["change"] or x.get("moved_from"):     # moved parts were added with their position
                continue
            r = mdb._s.put("%s/rest/collections/%d/content_units/%d" % (config.MDB, p["col"], x["cu_id"]),
                           json={"name": x["new_name"], "position": x["new_position"]}, timeout=60)
            r.raise_for_status()
        now = {e["uid"]: (e["position"], e["name"]) for e in _entries(p["col"])}
        bad = [x["uid"] for x in p["rows"] if now.get(x["uid"]) != (x["new_position"], x["new_name"])]
        cprops = mdb._get("/collections/%d/" % p["col"]).get("properties") or {}
        if p["number"]["set"] and cprops.get("number") != p["number"]["set"]:
            bad.append("collection number")
        if p.get("date_new") and cprops.get("film_date") != p["date_new"]:
            bad.append("collection film_date")
        for uid, cu_id, old, new in p.get("unit_dates") or []:
            if (mdb._get("/content_units/%d/" % cu_id).get("properties") or {}).get("film_date") != new:
                bad.append("film_date of %s" % uid)
        res.append((p["uid"], not bad, "mismatch %s" % bad if bad else "ok"))
        log.info("collection %s: %s", p["uid"], res[-1][2])
    return res
