"""Step 2: register a verified part in WFDB/MDB and hand mlt_o to the nightly kmedia publisher.

Order (each step is checked before the next):
  1. new WFDB source id s<epoch>; retag both staged files comment=<sid> (the cron reads it)
  2. mkdir the date folder on stp.kab.sh (old dates don't have one)
  3. mlt_s -> /backup/files/sources/Y/M/D/ ; MDB operations/insert (insert_type=partcut, no parent, unpublished)
  4. MDB operations/convert sha1=mlt_s, output=[mlt_o] -> mlt_o child of mlt_s, in the part unit,
     with video_size HLS, video_qualities [fHD], languages
  5. WFDB PUT source/<sid> (line, languages, inpoints/outpoints, converted, kmedia)
  6. publish now, exactly like the nightly cron (/opt/scripts/kmedia.sh on WF_HOST) would:
     scp mlt_o to stp.kab.sh:/backup/files/kmedia/Y/M/D/ (sha1 checked), MDB operations/upload with
     url=files.kab.sh/hls/...,.urlset (publishes), and file it in /backup/files/kmedia/Y/M/D/.
     It never goes to the kmedia root, so the cron won't process it again.
"""
import json
import logging
import os
import shutil
import subprocess
import time

import requests

from . import audio, config, cut, mdb

log = logging.getLogger("partcut.publish")

INSERT_TYPE = "partcut"
STATION = config.STATION
USER = "operator@dev.com"
WF_HOST = config.WF_HOST
STP = "stp.kab.sh"
KMEDIA = "/backup/files/kmedia"


def _tracks(path):
    """3-letter audio languages in track order (first track ori/src)."""
    return [(s.get("tags") or {}).get("language") for s in audio.probe(path)["streams"] if s["codec_type"] == "audio"]


def _retag(path, sid):
    tmp = path + ".retag.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path, "-map", "0", "-c", "copy",
                    "-map_metadata", "0", "-metadata", "comment=" + sid, "-movflags", "+faststart", tmp], check=True)
    os.replace(tmp, path)


def _meta(path):
    p = audio.probe(path)
    return {"sha1": cut.sha1(path), "size": os.path.getsize(path), "duration": round(float(p["format"]["duration"]), 3),
            "created_at": int(os.stat(path).st_mtime)}


def _mdb_file(sha1):
    data = mdb._get("/files/", query=sha1).get("data") or []
    return next((f for f in data if f.get("sha1") == sha1), None) or (data[0] if data else None)


def plan(row, sid, s_meta, o_meta):
    rd = mdb.real_date(row)
    y, m, d = rd.split("-")
    s_name, o_name = row["out_name"] + ".mp4", row["o_name"] + ".mp4"
    langs3 = _tracks(row["o_staging"])          # mlt_o: heb first, no ori
    insert = {
        "station": STATION, "user": USER, "workflow_id": sid,
        "insert_type": INSERT_TYPE, "mode": "new", "content_unit_uid": row["part_uid"],
        "file_name": s_name, "sha1": s_meta["sha1"], "size": s_meta["size"], "created_at": s_meta["created_at"],
        "duration": s_meta["duration"], "video_size": "FHD",
        "type": "video", "mime_type": "video/mp4", "language": "zz",
    }
    convert = {
        "station": STATION, "user": USER, "workflow_id": sid, "sha1": s_meta["sha1"],
        "output": [{
            "file_name": o_name, "sha1": o_meta["sha1"], "size": o_meta["size"], "created_at": o_meta["created_at"],
            "type": "video", "mime_type": "video/mp4", "language": "zz", "duration": o_meta["duration"],
            "video_size": "HLS", "languages": langs3, "video_qualities": row.get("o_qualities") or [row.get("o_quality", "fHD")],
        }],
    }
    wfdb = {
        "source_id": sid, "sha1": s_meta["sha1"], "file_name": row["out_name"], "date": rd,
        "line": {
            "lid": sid, "uid": row["part_uid"], "unit_id": row["part_id"], "content_type": "LESSON_PART",
            "collection_type": "DAILY_LESSON", "language": "heb", "lecturer": "rav",
            "auto_name": row["out_name"], "final_name": row["out_name"], "manual_name": None,
            "capture_date": rd, "film_date": rd, "require_test": False, "has_translation": True,
        },
        "parent": {}, "wfstatus": {"converted": True, "wfsend": True, "partcut": True},
        "source": {
            "parent": {"full_lesson": row["full_name"], "full_path": row["full"]},
            "inpoints": [row["cut_start"]], "outpoints": [row["end"]],
            "languages": {l: True for l in langs3},
            "converted": dict(s_meta, file_name=row["out_name"],
                              filename="%s/%s/%s/%s/%s" % (config.SOURCES, y, m, d, s_name)),
            "kmedia": dict(o_meta, file_name=row["o_name"], filename="%s/%s/%s/%s/%s" % (KMEDIA, y, m, d, o_name)),
            "lesson": row.get("lesson"),
            "partcut": {"verify": {k: row.get(k) for k in ("head_offset", "tail_offset", "head_peak", "tail_peak",
                                                             "start_edge_peak", "end_edge_peak", "head_text",
                                                             "tail_text", "dur_diff")},
                        "added_langs": row.get("added_langs"), "lang_ref": row.get("lang_ref")},
        },
    }
    url = "https://files.kab.sh/hls/%s/%s/%s/%s,.urlset/master.m3u8" % (y, m, d, o_name)
    upload = {  # same payload as mdbJson() in /opt/scripts/kmedia.sh
        "station": STATION, "user": USER, "workflow_id": sid, "duration": o_meta["duration"], "video_size": "HLS",
        "file_name": row["o_name"], "sha1": o_meta["sha1"], "size": o_meta["size"],
        "created_at": o_meta["created_at"], "mime_type": "video/mp4", "type": "video", "sub_type": "",
        "language": "zz", "url": url,
    }
    return {
        "sid": sid, "date": (y, m, d), "upload": upload,
        "s_dest": "%s/%s/%s/%s/%s" % (config.SOURCES, y, m, d, s_name),
        "o_dest": "%s/%s/%s/%s/%s" % (KMEDIA, y, m, d, o_name),
        "stp_dir": "%s/%s/%s/%s" % (KMEDIA, y, m, d),
        "insert": insert, "convert": convert, "wfdb": wfdb,
        "publish_url": url,
    }


def show(p, row):
    out = []
    out.append("=== %s  %s  (unit %s / id %s)" % (row["date"], row["out_name"], row["part_uid"], row["part_id"]))
    out.append("1. WFDB id %s; retag comment=%s on both staged files" % (p["sid"], p["sid"]))
    out.append("2. ssh %s mkdir -p %s" % (STP, p["stp_dir"]))
    out.append("3. move %s -> %s" % (row["staging"], p["s_dest"]))
    out.append("   POST %s/operations/insert\n%s" % (config.MDB, json.dumps(p["insert"], indent=2, ensure_ascii=False)))
    out.append("4. POST %s/operations/convert\n%s" % (config.MDB, json.dumps(p["convert"], indent=2, ensure_ascii=False)))
    out.append("5. PUT %s/source/%s\n%s" % (config.WFDB, p["sid"], json.dumps(p["wfdb"], indent=2, ensure_ascii=False)))
    out.append("6. scp %s -> %s:%s/  (sha1 checked on stp)" % (row["o_staging"], STP, p["stp_dir"]))
    out.append("   POST %s/operations/upload   (publishes)\n%s" % (config.MDB, json.dumps(p["upload"], indent=2, ensure_ascii=False)))
    out.append("   copy -> %s" % p["o_dest"])
    out.append("   => %s" % p["publish_url"])
    return "\n".join(out)


def register(row, dry_run=True):
    """Run (or print) the whole step 2 for one verified row. Returns a result dict."""
    part = {"files": mdb._get("/content_units/%d/files/" % row["part_id"]) or []}
    if mdb.has_hq_file(part) or any(f["name"].startswith(("mlt_s_", "mlt_o_")) for f in part["files"]):
        raise RuntimeError("unit %s already has an mlt_s/mlt_o or HLS file" % row["part_uid"])
    for k in ("staging", "o_staging"):
        if not os.path.exists(row[k]):
            raise RuntimeError("missing staged file %s" % row[k])

    sid = row.get("sid") or "s%d" % int(time.time())
    if dry_run:
        s_meta = dict(sha1="<sha1 after retag>", size=row["size"], duration=row["cut_duration"], created_at=int(time.time()))
        o_meta = dict(sha1="<sha1 after retag>", size=row["o_size"], duration=row["o_duration"], created_at=int(time.time()))
        p = plan(row, sid, s_meta, o_meta)
        print(show(p, row))
        return {"dry_run": True, "sid": sid}

    existing = requests.get("%s/source/%s" % (config.WFDB, sid), timeout=30)
    if existing.ok and existing.text.strip() not in ("", "null", "{}"):
        if not row.get("sid"):
            raise RuntimeError("WFDB id %s already exists" % sid)

    # 1. retag + final checksums
    for k in ("staging", "o_staging"):
        _retag(row[k], sid)
    s_meta, o_meta = _meta(row["staging"]), _meta(row["o_staging"])
    p = plan(row, sid, s_meta, o_meta)
    log.info("\n%s", show(p, row))

    # 2. date folder on the public HLS server
    subprocess.run(["ssh", "-o", "BatchMode=yes", STP, "mkdir -p %s" % p["stp_dir"]], check=True, timeout=120)

    # 3. mlt_s into sources + MDB insert
    if os.path.exists(p["s_dest"]):
        raise RuntimeError("%s already exists" % p["s_dest"])
    os.makedirs(os.path.dirname(p["s_dest"]), exist_ok=True)
    shutil.copyfile(row["staging"], p["s_dest"] + ".part")
    os.replace(p["s_dest"] + ".part", p["s_dest"])
    os.chmod(p["s_dest"], 0o644)
    if cut.sha1(p["s_dest"]) != s_meta["sha1"]:
        raise RuntimeError("copy of mlt_s corrupted")
    r = requests.post(config.MDB + "/operations/insert", json=p["insert"], timeout=120)
    log.info("insert -> %s %s", r.status_code, r.text[:300])
    r.raise_for_status()
    s_file = _mdb_file(s_meta["sha1"])
    if not s_file or s_file.get("content_unit_id") != row["part_id"] or s_file.get("parent_id"):
        raise RuntimeError("mlt_s not registered as expected: %s" % s_file)

    # 4. MDB convert -> mlt_o child of mlt_s
    r = requests.post(config.MDB + "/operations/convert", json=p["convert"], timeout=120)
    log.info("convert -> %s %s", r.status_code, r.text[:300])
    r.raise_for_status()
    o_file = _mdb_file(o_meta["sha1"])
    if not o_file or o_file.get("parent_id") != s_file["id"] or o_file.get("content_unit_id") != row["part_id"]:
        raise RuntimeError("mlt_o not registered as expected: %s" % o_file)

    # 5. WFDB source record (the cron reads capture_date and source.kmedia from it)
    p["wfdb"]["source"]["converted"]["file_uid"] = s_file["uid"]
    p["wfdb"]["source"]["kmedia"]["file_uid"] = o_file["uid"]
    r = requests.put("%s/source/%s" % (config.WFDB, sid), data=json.dumps(p["wfdb"]),
                     headers={"Content-Type": "application/json"}, timeout=60)
    log.info("wfdb source %s -> %s", sid, r.status_code)
    r.raise_for_status()
    chk = requests.get("%s/source/%s" % (config.WFDB_KV, sid), timeout=30).json()
    if chk.get("line", {}).get("capture_date") != mdb.real_date(row):
        raise RuntimeError("WFDB record not readable by the cron endpoint: %s" % str(chk)[:200])

    # 6. publish now (what the nightly cron does): stp.kab.sh, MDB upload, local kmedia/Y/M/D
    remote = "%s/%s" % (p["stp_dir"], os.path.basename(p["o_dest"]))
    subprocess.run(["scp", "-q", "-o", "BatchMode=yes", row["o_staging"], "%s:%s.part" % (STP, remote)], check=True)
    rsha = subprocess.run(["ssh", "-o", "BatchMode=yes", STP, "sha1sum %s.part" % remote],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    if rsha != o_meta["sha1"]:
        raise RuntimeError("mlt_o on stp.kab.sh has sha1 %s, expected %s" % (rsha, o_meta["sha1"]))
    subprocess.run(["ssh", "-o", "BatchMode=yes", STP, "chmod 644 %s.part && mv %s.part %s" % (remote, remote, remote)],
                   check=True)
    r = requests.post(config.MDB + "/operations/upload", json=p["upload"], timeout=120)
    log.info("upload -> %s %s", r.status_code, r.text[:300])
    r.raise_for_status()
    o_file = _mdb_file(o_meta["sha1"])
    if not o_file.get("published") or (o_file.get("properties") or {}).get("url") != p["publish_url"]:
        raise RuntimeError("mlt_o not published as expected: %s" % o_file)
    os.makedirs(os.path.dirname(p["o_dest"]), exist_ok=True)
    shutil.copyfile(row["o_staging"], p["o_dest"] + ".part")
    os.chmod(p["o_dest"] + ".part", 0o644)
    os.replace(p["o_dest"] + ".part", p["o_dest"])

    return {"sid": sid, "mlt_s": {"id": s_file["id"], "uid": s_file["uid"], "path": p["s_dest"]},
            "mlt_o": {"id": o_file["id"], "uid": o_file["uid"], "path": p["o_dest"]},
            "publish_url": p["publish_url"], "registered_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def replace_o(row, dry_run=True):
    """Replace the published mlt_o of a registered part with the rebuilt staged one (MDB operations/replace)."""
    reg = row["registered"]
    sid = row["sid"]
    y, m, d = mdb.real_date(row).split("-")
    o_name = row["o_name"] + ".mp4"
    old = mdb._get("/files/%d/" % reg["mlt_o"]["id"])
    o_meta = _meta(row["o_staging"])
    if o_meta["sha1"] == old["sha1"]:
        raise RuntimeError("staged mlt_o is identical to the published one; rebuild it first")
    langs3 = _tracks(row["o_staging"])
    url = "https://files.kab.sh/hls/%s/%s/%s/%s,.urlset/master.m3u8" % (y, m, d, o_name)
    replace = {
        "station": STATION, "user": USER, "workflow_id": sid, "old_sha1": old["sha1"],
        "content_unit_uid": row["part_uid"], "file_name": o_name, "sha1": o_meta["sha1"], "size": o_meta["size"],
        "created_at": o_meta["created_at"], "type": "video", "mime_type": "video/mp4", "language": "zz",
        "duration": o_meta["duration"], "video_size": "HLS", "languages": langs3, "video_qualities": row.get("o_qualities") or [row.get("o_quality", "fHD")],
    }
    upload = {
        "station": STATION, "user": USER, "workflow_id": sid, "duration": o_meta["duration"], "video_size": "HLS",
        "file_name": row["o_name"], "sha1": o_meta["sha1"], "size": o_meta["size"],
        "created_at": o_meta["created_at"], "mime_type": "video/mp4", "type": "video", "sub_type": "",
        "language": "zz", "url": url,
    }
    stp_path = "%s/%s/%s/%s/%s" % (KMEDIA, y, m, d, o_name)
    print("=== replace mlt_o of %s (%s): old file %s sha1 %s -> new sha1 %s, audio %s"
          % (row["part_uid"], row["o_name"], old["id"], old["sha1"], o_meta["sha1"], " ".join(langs3)))
    print("1. scp new mlt_o -> %s:%s.new (sha1 checked)" % (STP, stp_path))
    print("2. POST %s/operations/replace\n%s" % (config.MDB, json.dumps(replace, indent=2)))
    print("3. on stp: mv %s.new %s   (in-place, same URL)" % (stp_path, stp_path))
    print("4. POST %s/operations/upload (publishes new, old already removed by replace)\n%s"
          % (config.MDB, json.dumps(upload, indent=2)))
    print("5. replace local %s ; update WFDB source/%s kmedia+languages" % (reg["mlt_o"]["path"], sid))
    if dry_run:
        return {"dry_run": True}

    # 1. upload to stp first (so the gap between unpublish and publish is short)
    subprocess.run(["scp", "-q", "-o", "BatchMode=yes", row["o_staging"], "%s:%s.new" % (STP, stp_path)], check=True)
    rsha = subprocess.run(["ssh", "-o", "BatchMode=yes", STP, "sha1sum %s.new" % stp_path],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    if rsha != o_meta["sha1"]:
        raise RuntimeError("new mlt_o on stp has sha1 %s" % rsha)
    # 2. MDB replace (old mlt_o removed+unpublished, new one created under mlt_s, unpublished)
    r = requests.post(config.MDB + "/operations/replace", json=replace, timeout=120)
    log.info("replace -> %s %s", r.status_code, r.text[:300])
    r.raise_for_status()
    new = _mdb_file(o_meta["sha1"])
    if not new or new.get("parent_id") != reg["mlt_s"]["id"] or new.get("content_unit_id") != row["part_id"]:
        raise RuntimeError("new mlt_o not created as expected: %s" % new)
    # 3. swap the file on stp
    subprocess.run(["ssh", "-o", "BatchMode=yes", STP, "chmod 644 %s.new && mv %s.new %s" % (stp_path, stp_path, stp_path)],
                   check=True)
    # 4. publish
    r = requests.post(config.MDB + "/operations/upload", json=upload, timeout=120)
    log.info("upload -> %s %s", r.status_code, r.text[:300])
    r.raise_for_status()
    new = _mdb_file(o_meta["sha1"])
    oldnow = mdb._get("/files/%d/" % old["id"])
    if not new.get("published") or (new.get("properties") or {}).get("url") != url or oldnow.get("published"):
        raise RuntimeError("publish state unexpected: new=%s old_published=%s" % (new, oldnow.get("published")))
    # 5. local copy + WFDB
    shutil.copyfile(row["o_staging"], reg["mlt_o"]["path"] + ".part")
    os.chmod(reg["mlt_o"]["path"] + ".part", 0o644)
    os.replace(reg["mlt_o"]["path"] + ".part", reg["mlt_o"]["path"])
    rec = requests.get("%s/source/%s" % (config.WFDB, sid), timeout=30).json()
    rec["source"]["kmedia"].update(o_meta, file_uid=new["uid"])
    rec["source"]["languages"] = {l: True for l in langs3}
    r = requests.put("%s/source/%s" % (config.WFDB, sid), data=json.dumps(rec),
                     headers={"Content-Type": "application/json"}, timeout=60)
    r.raise_for_status()
    reg["mlt_o"].update(id=new["id"], uid=new["uid"])
    reg["replaced_o"] = {"old_id": old["id"], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    purge_muxed(row["o_name"])
    return reg


def check_hls(url, n_langs):
    """Fresh-token master playlist + one decoded segment. -> (ok, detail)"""
    import random
    path = url.replace("https://files.kab.sh/hls/", "").replace(",.urlset/master.m3u8", "")
    base = "https://files.kab.sh/hls/c%d/%s" % (random.randint(1, 10 ** 9), path)
    try:
        m = requests.get(base + "/master.m3u8", timeout=30).text
        idx = requests.get(base + "/index-v1-a1.m3u8", timeout=30).text
        seg = next((l for l in idx.splitlines() if l.startswith("http")), None)
        data = requests.get(seg, timeout=60).content if seg else b""
    except requests.RequestException as e:
        return False, "HLS request failed: %s" % e
    tmp = "/tmp/partcut_seg_%d.ts" % os.getpid()
    open(tmp, "wb").write(data)
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0",
                         tmp], capture_output=True, text=True).stdout
    os.remove(tmp)
    ok = "RESOLUTION=1920x1080" in m and m.count("TYPE=AUDIO") == n_langs and "h264,1920,1080" in pr
    return ok, "audio tracks %d/%d, segment %s" % (m.count("TYPE=AUDIO"), n_langs, " ".join(pr.split()))


def cleanup_staging(row):
    """After a checked publish the staged copies are not needed (a later fix re-cuts from the full lesson)."""
    freed = 0
    for k in ("staging", "o_staging"):
        pth = row.get(k)
        if pth and os.path.exists(pth):
            freed += os.path.getsize(pth)
            os.remove(pth)
    row["staging_deleted"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return freed


def replace_both(row, dry_run=True):
    """Re-made part (e.g. corrected languages): replace the registered mlt_s AND mlt_o.
    MDB replace of mlt_s removes+unpublishes the old mlt_s with its descendants (the old mlt_o);
    then convert creates the new mlt_o as child of the new mlt_s and upload publishes it (same URL)."""
    reg, sid = row["registered"], row["sid"]
    y, m, d = mdb.real_date(row).split("-")
    old_s = mdb._get("/files/%d/" % reg["mlt_s"]["id"])
    old_o = mdb._get("/files/%d/" % reg["mlt_o"]["id"])
    for k in ("staging", "o_staging"):
        if not os.path.exists(row[k]) or os.path.islink(row[k]):
            raise RuntimeError("missing (or only linked) staged file %s" % row[k])
    if dry_run:
        s_meta = dict(sha1="<after retag>", size=row["size"], duration=row["cut_duration"], created_at=0)
        o_meta = dict(sha1="<after retag>", size=row["o_size"], duration=row["o_duration"], created_at=0)
    else:
        for k in ("staging", "o_staging"):
            _retag(row[k], sid)
        s_meta, o_meta = _meta(row["staging"]), _meta(row["o_staging"])
        if s_meta["sha1"] == old_s["sha1"] or o_meta["sha1"] == old_o["sha1"]:
            raise RuntimeError("staged files identical to the registered ones")
    p = plan(row, sid, s_meta, o_meta)
    replace = {
        "station": STATION, "user": USER, "workflow_id": sid, "old_sha1": old_s["sha1"],
        "content_unit_uid": row["part_uid"], "file_name": row["out_name"] + ".mp4", "sha1": s_meta["sha1"],
        "size": s_meta["size"], "created_at": s_meta["created_at"], "type": "video", "mime_type": "video/mp4",
        "language": "zz", "duration": s_meta["duration"], "video_size": "FHD",
    }
    remote = "%s/%s" % (p["stp_dir"], os.path.basename(p["o_dest"]))
    print("=== replace mlt_s+mlt_o of %s (%s): old mlt_s %s, old mlt_o %s; audio %s"
          % (row["part_uid"], row["out_name"], old_s["id"], old_o["id"], " ".join(_tracks(row["o_staging"]))))
    print("1. scp new mlt_o -> %s:%s.new (sha1 checked)" % (STP, remote))
    print("2. POST %s/operations/replace (mlt_s; removes old mlt_s + old mlt_o)\n%s" % (config.MDB, json.dumps(replace, indent=2)))
    print("3. POST %s/operations/convert (new mlt_o child of new mlt_s)\n%s" % (config.MDB, json.dumps(p["convert"], indent=2)))
    print("4. on stp: mv .new -> %s ; POST operations/upload (publishes, same URL %s)" % (remote, p["publish_url"]))
    print("5. local %s and %s replaced; WFDB source/%s updated" % (p["s_dest"], p["o_dest"], sid))
    if dry_run:
        return {"dry_run": True}

    # 1. new mlt_o onto stp first, next to the live one
    subprocess.run(["scp", "-q", "-o", "BatchMode=yes", row["o_staging"], "%s:%s.new" % (STP, remote)], check=True)
    rsha = subprocess.run(["ssh", "-o", "BatchMode=yes", STP, "sha1sum %s.new" % remote],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    if rsha != o_meta["sha1"]:
        raise RuntimeError("new mlt_o on stp has sha1 %s" % rsha)
    # local mlt_s first (MDB must never point at a file we don't have)
    shutil.copyfile(row["staging"], p["s_dest"] + ".new")
    if cut.sha1(p["s_dest"] + ".new") != s_meta["sha1"]:
        raise RuntimeError("copy of mlt_s corrupted")
    # 2. replace mlt_s
    r = requests.post(config.MDB + "/operations/replace", json=replace, timeout=120)
    log.info("replace mlt_s -> %s %s", r.status_code, r.text[:300])
    r.raise_for_status()
    new_s = _mdb_file(s_meta["sha1"])
    if not new_s or new_s.get("content_unit_id") != row["part_id"] or new_s.get("parent_id"):
        raise RuntimeError("new mlt_s not created as expected: %s" % new_s)
    os.chmod(p["s_dest"] + ".new", 0o644)
    os.replace(p["s_dest"] + ".new", p["s_dest"])
    # 3. convert -> new mlt_o
    r = requests.post(config.MDB + "/operations/convert", json=p["convert"], timeout=120)
    log.info("convert -> %s %s", r.status_code, r.text[:300])
    r.raise_for_status()
    new_o = _mdb_file(o_meta["sha1"])
    if not new_o or new_o.get("parent_id") != new_s["id"] or new_o.get("content_unit_id") != row["part_id"]:
        raise RuntimeError("new mlt_o not created as expected: %s" % new_o)
    # 4. swap on stp + publish
    subprocess.run(["ssh", "-o", "BatchMode=yes", STP, "chmod 644 %s.new && mv %s.new %s" % (remote, remote, remote)],
                   check=True)
    r = requests.post(config.MDB + "/operations/upload", json=p["upload"], timeout=120)
    log.info("upload -> %s %s", r.status_code, r.text[:300])
    r.raise_for_status()
    new_o = _mdb_file(o_meta["sha1"])
    gone = [mdb._get("/files/%d/" % f["id"]) for f in (old_s, old_o)]
    if not new_o.get("published") or (new_o.get("properties") or {}).get("url") != p["publish_url"] \
            or any(g.get("published") for g in gone):
        raise RuntimeError("publish state unexpected: new=%s old=%s" % (new_o, [g.get("published") for g in gone]))
    shutil.copyfile(row["o_staging"], p["o_dest"] + ".part")
    os.chmod(p["o_dest"] + ".part", 0o644)
    os.replace(p["o_dest"] + ".part", p["o_dest"])
    # 5. WFDB: the full new record (languages, added_langs, files)
    p["wfdb"]["source"]["converted"]["file_uid"] = new_s["uid"]
    p["wfdb"]["source"]["kmedia"]["file_uid"] = new_o["uid"]
    r = requests.put("%s/source/%s" % (config.WFDB, sid), data=json.dumps(p["wfdb"]),
                     headers={"Content-Type": "application/json"}, timeout=60)
    r.raise_for_status()
    reg.update(mlt_s={"id": new_s["id"], "uid": new_s["uid"], "path": p["s_dest"]},
               mlt_o={"id": new_o["id"], "uid": new_o["uid"], "path": p["o_dest"]})
    reg["replaced_both"] = {"old_s": old_s["id"], "old_o": old_o["id"], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    purge_muxed(row["o_name"])
    return reg


def purge_muxed(o_name):
    """stp caches per-language downloads (wf-srv /get -> /opt/wfexec/remux.sh) under the FILE NAME in
    /opt/kmedia/muxed as <lang>_o<rest>[_<quality>].mp4 / .m4a - after a replace they would serve the old file."""
    rest = o_name[len("mlt_o"):] if o_name.startswith("mlt_o") else o_name[5:]
    pat = "/opt/kmedia/muxed/[a-z][a-z][a-z]_o%s_*.mp4 /opt/kmedia/muxed/[a-z][a-z][a-z]_o%s.m4a" % (rest, rest)
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", STP, "rm -fv %s" % pat], capture_output=True, text=True, timeout=120)
    n = len([l for l in r.stdout.splitlines() if l.strip()])
    log.info("muxed cache for %s: %d file(s) removed", o_name, n)
    return n
