"""MDB / WFDB access. Read helpers are used by step 1; register() only by step 2."""
import json
import logging
import os
import time

import requests

from . import config

log = logging.getLogger("partcut.mdb")
_s = requests.Session()
_s.headers["Authorization"] = "Bearer " + config.token()

MEDIA_EXT = (".mp3", ".mp4", ".flv", ".wmv", ".mpg", ".m4a")


def real_date(row):
    """Date of the recording the part was cut from (Shabbat/holiday parts may carry another date in MDB)."""
    return (row.get("date_shift") or {}).get("recording_date") or row["date"]


def _get(path, **params):
    r = _s.get(config.MDB + "/rest" + path, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def list_parts(date):
    """LESSON_PART units filmed on `date`, with files and collection position."""
    units, page = [], 1
    while True:
        d = _get("/content_units/", content_type="LESSON_PART", start_date=date,
                 end_date=date, page_size=100, page_no=page)
        units += d.get("data") or []
        if len(units) >= d.get("total", 0) or not d.get("data"):
            break
        page += 1
    out = []
    for u in units:
        files = _get("/content_units/%d/files/" % u["id"]) or []
        cols = _get("/content_units/%d/collections/" % u["id"]) or []
        daily = [c for c in cols if c["collection"]["type_id"] == 1]
        i18n = u.get("i18n") or {}
        out.append({
            "id": u["id"], "uid": u["uid"],
            "title": (i18n.get("he") or i18n.get("en") or {}).get("name", ""),
            "mdb_duration": (u.get("properties") or {}).get("duration"),
            "collection": daily[0]["collection"]["uid"] if daily else None,
            "position": daily[0]["position"] if daily else None,
            "files": files,
        })
    out.sort(key=lambda p: (p["collection"] or "", p["position"] if p["position"] is not None else 1e9, p["id"]))
    return out


def has_hq_file(part):
    """The unit already has an HLS file: old ones on HLS_BASE, partcut's on files.kab.sh/hls, or our
    mlt_s/mlt_o files (published or not)."""
    for f in part["files"]:
        url = (f.get("properties") or {}).get("url") or ""
        if f.get("removed_at"):
            continue
        if config.HLS_BASE.split("//")[1] in url or "files.kab.sh/hls/" in url or \
                f["name"].startswith(("mlt_s_", "mlt_o_")) and f["name"].endswith(".mp4") and "_full" not in f["name"]:
            return True
    return False


def pick_media(part):
    """Original-language media for STT/xcorr: heb mp3 > heb mp4 > other heb > any *_o_*."""
    files = [f for f in part["files"] if f["name"].lower().endswith(MEDIA_EXT)]

    def rank(f):
        n = f["name"].lower()
        he = f.get("language") == "he"
        return (0 if he else 1 if "_o_" in n else 2,
                {".mp3": 0, ".mp4": 1}.get(os.path.splitext(n)[1], 2),
                -(f.get("size") or 0))
    files = [f for f in files if f.get("language") == "he" or "_o_" in f["name"]]
    return sorted(files, key=rank)[0] if files else None


def name_source(part):
    """File name the HQ name is derived from: heb video name, else picked media."""
    vids = [f for f in part["files"] if f.get("language") == "he" and f["name"].lower().endswith(".mp4")]
    if vids:
        return vids[0]["name"]
    m = pick_media(part)
    return m["name"] if m else None


def download(file_uid, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    tmp = dest + ".part"
    with requests.get("%s/%s" % (config.CDN, file_uid), stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------- step 2 only

def register(row):
    """Register a moved HQ part file in MDB and WFDB. Returns {file_id, file_uid}."""
    y, m, d = row["date"].split("-")
    name = row["out_name"]
    src_id = "s%d" % int(time.time())
    url = "%s/%s/%s/%s/%s.mp4/master.m3u8" % (config.HLS_BASE, y, m, d, name)
    upload = {
        "station": config.STATION, "user": "operator@dev.com", "workflow_id": src_id,
        "duration": row["cut_duration"], "video_size": "PAL", "file_name": name,
        "sha1": row["sha1"], "size": row["size"], "created_at": int(time.time()),
        "mime_type": "video/mp4", "type": "video", "sub_type": "", "language": "zz", "url": url,
    }
    r = requests.post(config.MDB + "/operations/upload", json=upload, timeout=60)
    log.info("operations/upload %s -> %s %s", name, r.status_code, r.text[:300])
    r.raise_for_status()

    fd = requests.get(config.WFSRV_MDB + "/files/", params={"sha1": row["sha1"]}, timeout=60).json()
    f = (fd.get("data") or [None])[0]
    if not f:
        raise RuntimeError("file not found in MDB after upload: sha1=%s" % row["sha1"])

    r = _s.put("%s/rest/files/%d/" % (config.MDB, f["id"]), json={"content_unit_id": row["part_id"]}, timeout=60)
    log.info("attach file %s to unit %s -> %s", f["id"], row["part_id"], r.status_code)
    r.raise_for_status()

    src = {
        "sha1": row["sha1"], "source_id": src_id, "file_name": name, "date": row["date"],
        "line": {"lid": src_id, "uid": row["part_uid"], "unit_id": row["part_id"],
                 "content_type": "LESSON_PART", "collection_type": "DAILY_LESSON",
                 "language": "heb", "lecturer": "rav", "final_name": name,
                 "capture_date": row["date"], "require_test": False},
        "parent": {}, "wfstatus": {"converted": True},
        "source": {
            "parent": {"original_name": row["full_name"], "original_sha1": row.get("full_sha1")},
            "inpoints": [row["start"]], "outpoints": [row["end"]],
            "converted": {"file_uid": f["uid"], "file_id": f["id"], "file_name": name,
                          "duration": row["cut_duration"], "size": row["size"], "sha1": row["sha1"]},
        },
    }
    r = requests.put("%s/source/%s" % (config.WFDB, src_id), data=json.dumps(src),
                     headers={"Content-Type": "application/json"}, timeout=60)
    log.info("wfdb source %s -> %s", src_id, r.status_code)
    return {"file_id": f["id"], "file_uid": f["uid"], "url": url, "source_id": src_id}
