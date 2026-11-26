"""Lesson map: which MDB parts belong to which recording (full lesson), in what order, at what times,
and how that compares with their MDB daily-lesson collections (type 1).

Flags per recording:
  split      parts of this one recording sit in more than one daily-lesson collection
  unordered  all parts have the same collection position (old collections: every position 0) - no order in MDB
  bad_order  collection positions are set but contradict the real time order (or are partly duplicated)
The recording order ("order") is what a later MDB fix should write as the collection position.
"""
import csv
import json
import logging
import os

from . import config, mdb

log = logging.getLogger("partcut.lessons")
DAILY_LESSON = 1


def collections_of(part_id):
    out = []
    for c in mdb._get("/content_units/%d/collections/" % part_id) or []:
        col = c["collection"]
        i18n = col.get("i18n") or {}
        out.append({"uid": col["uid"], "id": col["id"], "type_id": col["type_id"], "position": c.get("position"),
                    "name": c.get("name"), "title": (i18n.get("he") or i18n.get("en") or {}).get("name")})
    return out


def build(state, dates=None):
    recs = {}
    for uid, r in state.items():
        if dates and r.get("date") not in dates:
            continue
        # every part that matched a recording, whatever happened later (errors, review)
        if r.get("start") is None or not r.get("full") or r.get("status") in ("no_match", "no_source", "no_full"):
            continue
        if "collections" not in r:
            r["collections"] = collections_of(r["part_id"])
        recs.setdefault(r["full"], []).append(r)

    lessons = []
    for full, rows in sorted(recs.items()):
        rows.sort(key=lambda x: x["start"])
        daily = sorted({c["uid"] for r in rows for c in r["collections"] if c["type_id"] == DAILY_LESSON})
        positions = []
        for r in rows:
            p = [c["position"] for c in r["collections"] if c["type_id"] == DAILY_LESSON]
            positions.append(p[0] if p else None)
        known = [p for p in positions if p is not None]
        unordered = len(known) > 1 and len(set(known)) == 1
        bad_order = not unordered and (len(set(known)) != len(known) or known != sorted(known))
        parts, prev_end = [], 0.0
        for i, r in enumerate(rows, start=1):
            dc = [c for c in r["collections"] if c["type_id"] == DAILY_LESSON]
            parts.append({
                "order": i, "part_uid": r["part_uid"], "part_id": r["part_id"], "title": r.get("title"),
                "file": r.get("media_name"), "start": max(0.0, r["start"]), "end": r["end"],
                "gap_before": round(r["start"] - prev_end, 3), "verified": bool(r.get("verified")),
                "daily_collection": dc[0]["uid"] if dc else None,
                "daily_position": dc[0]["position"] if dc else None,
                "other_collections": [{"uid": c["uid"], "type_id": c["type_id"], "position": c["position"]}
                                      for c in r["collections"] if c["type_id"] != DAILY_LESSON],
            })
            prev_end = r["end"]
            r["lesson"] = {"recording": os.path.basename(full), "order": i, "of": len(rows),
                           "start": r["start"], "end": r["end"],
                           "siblings": [x["part_uid"] for x in rows if x is not r],
                           "daily_collections": daily}
        lessons.append({
            "date": rows[0]["date"], "recording": os.path.basename(full), "recording_path": full,
            "duration": rows[0].get("full_duration"), "parts": parts,
            "daily_collections": daily, "split": len(daily) > 1, "unordered": unordered, "bad_order": bad_order,
        })
    return lessons


def write(lessons, base=None):
    base = base or os.path.join(config.REPORTS, "lesson_map")
    with open(base + ".json", "w") as f:
        json.dump(lessons, f, ensure_ascii=False, indent=1)
    with open(base + ".csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "recording", "split", "unordered", "bad_order", "order", "part_uid", "title", "start", "end",
                    "gap_before", "daily_collection", "daily_position", "other_collections", "verified"])
        for L in lessons:
            for p in L["parts"]:
                w.writerow([L["date"], L["recording"], L["split"], L["unordered"], L["bad_order"], p["order"], p["part_uid"],
                            p["title"], p["start"], p["end"], p["gap_before"], p["daily_collection"],
                            p["daily_position"], " ".join(c["uid"] for c in p["other_collections"]), p["verified"]])
    return base
