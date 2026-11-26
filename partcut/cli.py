"""partcut: cut HQ lesson parts out of converted full lessons.

  step 1:  python -m partcut.cli cut --dates 2010-01-01,2010-01-02
           python -m partcut.cli cut --year 2010 --limit 3
           -> staging files + reports/<run>.json/.csv   (no MDB writes)
  step 2:  python -m partcut.cli register --report reports/<run>.json [--uids a,b]
"""
import argparse
import datetime
import csv
import glob
import json
import logging
import os
import re
import fcntl
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
import sys
import time

from . import amatch, audio, config, cut, langs, lessons, mdb, outputs, sources
import subprocess

log = logging.getLogger("partcut")
STATE = os.path.join(config.CACHE, "state.json")


def setup_logging(run):
    os.makedirs(config.LOGDIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.FileHandler(os.path.join(config.LOGDIR, "parts_%s.log" % run)), logging.StreamHandler()):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)


_SNAP = {}       # id(state dict) -> {uid: json of the row as loaded/last saved by this process}


def _dump(row):
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def load_state():
    try:
        with open(STATE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    _SNAP[id(state)] = {u: _dump(r) for u, r in state.items()}
    return state


_STATE_LOCK = threading.Lock()


def save_state(state):
    """Write the state under a file lock, merged with what is on disk: entries written by another command
    (or worker) are kept, and a registration on disk is never lost."""
    with _STATE_LOCK, open(STATE + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            with open(STATE) as f:
                disk = json.load(f)
        except (OSError, ValueError):
            disk = {}
        snap = _SNAP.setdefault(id(state), {})
        for uid, d in disk.items():
            if uid not in state:
                state[uid] = d
            elif uid in snap and _dump(state[uid]) == snap[uid]:
                state[uid] = d          # not changed by this process: never write back a stale copy
            elif d.get("registered") and not state[uid].get("registered"):
                state[uid]["registered"], state[uid]["sid"] = d["registered"], d.get("sid")
        with open(STATE + ".tmp", "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(STATE + ".tmp", STATE)
        _SNAP[id(state)] = {u: _dump(r) for u, r in state.items()}


def full_lessons(date, state=None):
    y, m, d = date.split("-")
    ours = {r.get("out_name") for r in (state or {}).values()} | {r.get("o_name") for r in (state or {}).values()}
    out = []
    for p in sorted(glob.glob(os.path.join(config.SOURCES, y, m, d, "*_s_*.mp4"))):   # mlt_s_, heb_s_, ...
        if os.path.basename(p)[:-4] in ours:
            continue
        tags = audio.probe(p)["format"].get("tags") or {}
        if tags.get("comment") != config.CUT_MARKER:
            out.append(p)
    return out


def hms(t):
    sign, t = ("-" if t < 0 else ""), int(round(abs(t)))
    return "%s%d:%02d:%02d" % (sign, t // 3600, t % 3600 // 60, t % 60)


# ------------------------------------------------------------------ locate

NEAR_DAYS = 3        # Shabbat/holiday parts can carry a date a few days off the recording
NEAR_MIN_PEAK = 0.8  # ... but a match on another day needs both ends found with real-match scores


def near_dates(date, k=NEAR_DAYS):
    d0 = datetime.date.fromisoformat(date)
    return [(d0 + datetime.timedelta(days=i)).isoformat() for i in sorted(range(-k, k + 1), key=abs) if i]


class Full:
    def __init__(self, path):
        self.path, self.name = path, os.path.basename(path)
        self.duration = audio.duration(path)
        self.stream = audio.heb_stream(path)
        self._env = None

    @property
    def env(self):
        if self._env is None:
            self._env = amatch.envelope_of(self.path, self.stream)
        return self._env


def locate_part(row, part, fulls):
    """Audio matching (no transcription): see amatch.py."""
    media = mdb.pick_media(part)
    if not media:
        row.update(status="no_media")
        return
    ext = os.path.splitext(media["name"])[1].lower()
    os.makedirs(os.path.join(config.CACHE, "parts"), exist_ok=True)
    mpath = mdb.download(media["uid"], os.path.join(config.CACHE, "parts", media["uid"] + ext))
    pdur = audio.duration(mpath)
    row.update(media_name=media["name"], media_uid=media["uid"], media_path=mpath, part_duration=round(pdur, 3))

    cands = [(f.path, f.stream, f.env) for f in fulls]
    m = amatch.locate(mpath, pdur, cands)
    if not m:
        m = amatch.locate_speed(mpath, pdur, cands)      # old MDB files that run slightly slow/fast
    if not m:
        row.update(status="no_match", reason="no full lesson on this date contains the part audio",
                   t0_head=amatch.anchors(mpath, pdur)[0])
        return
    f = next(x for x in fulls if x.path == m["full"])
    dh = m["delta_head"] if m["delta_head"] is not None else m["delta_tail"]
    dt = m["delta_tail"] if m["delta_tail"] is not None else m["delta_head"]
    start, end = dh, dt + pdur
    speed = m.get("speed", 1.0)
    row.update(full=f.path, full_name=f.name, full_duration=round(f.duration, 3), match="audio", speed=speed,
               delta_head=dh, delta_tail=dt, xcorr_head=[m["peak_head"], "wave", True],
               xcorr_tail=[m["peak_tail"], "wave", True], t0_head=m["t0_head"], t0_tail=m["t0_tail"],
               start=round(start, 3), end=round(end, 3), drift=round(dt - dh, 3))
    reasons, warnings = [], []
    if m["delta_head"] is None or m["delta_tail"] is None:
        warnings.append("only %s matched" % ("tail" if m["delta_head"] is None else "head"))
    if speed != 1.0:
        warnings.append("MDB part file runs at %.3f%% of the recording's speed (corrected)" % (100 * speed))
    if abs((dt - dh) - (speed - 1) * pdur) > config.MAX_HEAD_TAIL_DRIFT:
        reasons.append("not_continuous: head/tail offsets differ by %.1fs (part edited?)" % (dt - dh))
    # up to PREROLL_MAX s of the MDB part may lie outside the recording (e.g. a spliced pre-roll):
    # cut what the recording has and note it; more than that -> review (probably another recording)
    if start < -0.5:
        if start >= -config.PREROLL_MAX:
            warnings.append("MDB part starts %.1fs before the recording (not in the archive) - cut from the recording start" % -start)
        else:
            reasons.append("range outside full lesson (starts %.1fs before the recording)" % -start)
    if end > f.duration + 0.5:
        if end <= f.duration + config.PREROLL_MAX:
            warnings.append("MDB part ends %.1fs after the recording - cut at the recording end" % (end - f.duration))
        else:
            reasons.append("range outside full lesson (ends %.1fs after the recording)" % (end - f.duration))
    row.update(status="review" if reasons else "ok", reason="; ".join(reasons), warnings=warnings)
    row["start"] = round(start, 3) if start >= -config.PREROLL_MAX else round(max(0.0, start), 3)
    row["end"] = round(min(f.duration, end), 3)
    log.info("  %s -> %s %s-%s peaks %.2f/%.2f %s", part["uid"], f.name, hms(row["start"]), hms(row["end"]),
             m["peak_head"], m["peak_tail"], row["status"])


def check_sequence(rows):
    """Parts of one full lesson must be in collection order and not overlap."""
    by_full = {}
    for r in rows:
        if r.get("status") in ("ok", "review") and r.get("full"):
            by_full.setdefault(r["full"], []).append(r)
    timelines = {}
    for full, rs in by_full.items():
        rs.sort(key=lambda r: r["start"])
        prev = None
        for r in rs:
            if prev is not None:
                if prev["end"] - r["start"] > config.ORDER_TOLERANCE:
                    for x in (prev, r):
                        x["status"] = "review"
                        x["reason"] = (x.get("reason", "") + "; overlaps %s" % (r if x is prev else prev)["part_uid"]).strip("; ")
                pp, rp = prev.get("position"), r.get("position")
                if pp is not None and rp is not None and prev.get("collection") == r.get("collection") and rp < pp:
                    r.setdefault("warnings", []).append("out of collection order")
            prev = r
        tl, last = [], 0.0
        for r in rs:
            if r["start"] - last > 2:
                tl.append("gap %s" % hms(r["start"] - last))
            tl.append("%s[%s] %s-%s" % (r.get("position"), r["part_uid"], hms(r["start"]), hms(r["end"])))
            last = max(last, r["end"])
        fd = rs[0]["full_duration"]
        if fd - last > 2:
            tl.append("tail %s" % hms(fd - last))
        timelines[os.path.basename(full)] = " | ".join(tl)
    return timelines


# ------------------------------------------------------------------ missing full lessons

QUEUE = os.path.join(config.CACHE, "convert_queue.json")


def load_queue():
    try:
        with open(QUEUE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_queue(q):
    with open(QUEUE + ".tmp", "w") as f:
        json.dump(q, f, ensure_ascii=False, indent=1)
    os.replace(QUEUE + ".tmp", QUEUE)


def search_sources(date, rows, parts_by_uid):
    """Unmatched parts: find which unconverted ShiurBoker/Special original contains them."""
    lost = [r for r in rows if r.get("status") in ("no_match", "no_full")]
    if not lost:
        return
    todo, _ = sources.originals(date)
    near = [p for nd in near_dates(date) for p in sources.originals(nd)[0] if p not in todo]
    log.info("%d unmatched part(s); %d unconverted original(s) on the date, %d within +-%d days", len(lost),
             len(todo), len(near), NEAR_DAYS)
    todo = todo + near
    q = load_queue()
    for r in lost:
        if not todo:
            r.update(status="no_source", reason="no unconverted original in ShiurBoker/Special within +-%d days" % NEAR_DAYS)
            continue
        try:
            if not r.get("media_path"):
                media = mdb.pick_media(parts_by_uid[r["part_uid"]])
                if not media:
                    r.update(status="no_media")
                    continue
                ext = os.path.splitext(media["name"])[1].lower()
                os.makedirs(os.path.join(config.CACHE, "parts"), exist_ok=True)
                r["media_path"] = mdb.download(media["uid"], os.path.join(config.CACHE, "parts", media["uid"] + ext))
                r["media_name"] = media["name"]
            hit = sources.find_part(r["media_path"], r.get("t0_head", 5.0), todo)
        except Exception as e:
            log.exception("source search %s failed", r["part_uid"])
            r.update(status="error", reason="source search: %s" % str(e)[:200])
            continue
        if hit:
            path, t, peak = hit
            r.update(status="needs_convert", original=path, original_peak=peak,
                     reason="part found in unconverted %s at %s (peak %.2f)" % (os.path.basename(path), hms(t), peak))
            rec = re.search(r"\d{4}-\d{2}-\d{2}", os.path.basename(path))
            e = q.setdefault(path, {"date": rec.group(0) if rec else date, "parts": [], "status": "pending"})
            if r["part_uid"] not in e["parts"]:
                e["parts"].append(r["part_uid"])
            e.setdefault("dates", [])
            if date not in e["dates"]:          # MDB date(s) of the parts: re-processed after the conversion
                e["dates"].append(date)
        else:
            r.update(status="no_source", reason="not found in any unconverted ShiurBoker/Special original")
        log.info("  %s -> %s %s", r["part_uid"], r["status"], r.get("reason", ""))
    save_queue(q)
    # report-only: parts still not found -> look in Rawmaterial / LT recordings of the same date
    for r in rows:
        if r.get("status") not in ("no_source", "no_match") or not r.get("media_path"):
            continue
        other = sources.other_candidates(date, r.get("media_name") or "")
        if not other:
            continue
        try:
            hit = sources.find_part(r["media_path"], r.get("t0_head", 5.0), other)
        except Exception as e:
            log.warning("  %s: other-folder search failed: %s", r["part_uid"], str(e)[:200])
            continue
        if hit:
            path, t, peak = hit
            r.update(status="found_in_other", original=path, original_peak=peak,
                     reason="found in %s at %s (peak %.2f) - Rawmaterial/LT, not converted (report only)"
                            % (path.replace(sources.ORIGINAL_ROOT + "/", ""), hms(t), peak))
        else:
            r["reason"] = (r.get("reason", "") + "; also not in %d Rawmaterial/LT file(s)" % len(other)).strip("; ")
        log.info("  %s -> %s %s", r["part_uid"], r["status"], r.get("reason", ""))


def batch_busy():
    out = subprocess.run(["pgrep", "-f", r"(batch|mpg|mp4|16mpg|man_mp4)\.sh"], capture_output=True, text=True).stdout
    return bool(out.strip())


def cmd_convert(args, run):
    """Convert queued originals with the existing /opt/filer scripts, one at a time, then cut their parts."""
    q = load_queue()
    only = set(d.strip() for d in args.dates.split(",")) if args.dates else None
    pending = [p for p, e in q.items() if e["status"] == "pending"
               and (only is None or e["date"] in only or only & set(e.get("dates", [])))]
    log.info("%d original(s) queued for conversion", len(pending))
    state = load_state()
    for path in pending:
        e = q[path]
        existing = sources.is_converted(path)
        if existing:
            log.info("%s already converted (%s), skipping conversion", path, existing)
        else:
            while batch_busy():
                log.info("batch.sh/mpg.sh/mp4.sh running, waiting before converting %s", os.path.basename(path))
                time.sleep(600)
            script = "./mpg.sh" if path.lower().endswith(".mpg") else "./mp4.sh"
            log.info("converting %s with %s", path, script)
            e.update(status="converting", started=time.strftime("%Y-%m-%d %H:%M:%S"))
            save_queue(q)
            rc = subprocess.run([script, path], cwd="/opt/filer").returncode
            existing = sources.is_converted(path)
            if rc != 0 or not existing or not os.path.exists(existing):
                e.update(status="failed", rc=rc)
                save_queue(q)
                log.error("conversion of %s failed (rc=%s, output=%s)", path, rc, existing)
                continue
        e.update(status="converted", output=existing, finished=time.strftime("%Y-%m-%d %H:%M:%S"))
        save_queue(q)
        for pd in [e["date"]] + [x for x in e.get("dates", []) if x != e["date"]]:   # recording date + part dates
            rows, timelines = process_date(pd, state)
        write_report("%s_%s" % (run, e["date"]), [e["date"]], rows, timelines)


# ------------------------------------------------------------------ verify

def verify(row):
    """Compare the cut file with the MDB part file (audio offset, text, duration)."""
    out, mpath, pdur, lead = row["staging"], row["media_path"], row["part_duration"], row["lead"]
    ostream = audio.heb_stream(out)
    res = {}
    trim = max(0.0, -lead)          # MDB-part seconds before the recording start (not in the cut)
    speed = row.get("speed", 1.0)
    if speed != 1.0:
        return verify_speed(row, out, mpath, pdur, lead, ostream, speed, trim)
    for side, t0 in (("head", max(row["t0_head"], trim + 0.5)), ("tail", row["t0_tail"])):
        t, peak, method = audio.locate(mpath, t0, out, t0 + lead - 3, config.XCORR_LEN, config.XCORR_LEN + 6,
                                       None, ostream)
        res[side + "_offset"] = None if t is None else round(t - t0, 3)
        res[side + "_peak"] = round(peak, 3)
    # Edges: the first/last seconds of the part, often a song or music with no speech.
    # Compared by waveform (works for music too). Near-silent windows can't be correlated.
    L = min(config.EDGE_CHECK_SEC, pdur / 2)
    for side, t0 in (("start_edge", trim + 0.5), ("end_edge", max(0.0, pdur - L - 0.5))):
        a = audio.pcm(mpath, t0, L, 8000)
        if len(a) == 0 or float((a ** 2).mean() ** 0.5) < config.EDGE_SILENT_RMS:
            res[side + "_offset"], res[side + "_peak"] = "silent", None
            continue
        t, peak, method = audio.locate(mpath, t0, out, t0 + lead - 3, L, L + 6, None, ostream)
        res[side + "_offset"] = None if t is None else round(t - t0, 3)
        res[side + "_peak"] = round(peak, 3)
    res["dur_diff"] = round(row["cut_duration"] - (pdur + lead), 3)
    fails = []
    for side in ("head", "tail"):
        o = res[side + "_offset"]
        if o is None or abs(o - lead) > 0.5 or res[side + "_peak"] < config.XCORR_MIN_PEAK:
            fails.append("%s audio offset %s (peak %s)" % (side, o, res[side + "_peak"]))
    for side in ("start_edge", "end_edge"):
        o = res[side + "_offset"]
        if o == "silent":
            continue
        if o is None or abs(o - lead) > 0.5 or res[side + "_peak"] < config.XCORR_MIN_PEAK:
            fails.append("%s audio offset %s (peak %s)" % (side, o, res[side + "_peak"]))
    if abs(res["dur_diff"]) > config.VERIFY_MAX_DUR_DIFF:
        fails.append("duration diff %.2fs" % res["dur_diff"])
    res["verify_fail"] = "; ".join(fails)
    return res, not fails


def verify_speed(row, out, mpath, pdur, lead, ostream, speed, trim):
    """verify() for speed-shifted MDB parts: part time t <-> cut time lead + speed * t."""
    res, fails = {}, []
    L = min(config.EDGE_CHECK_SEC, pdur / 2)
    checks = (("head", max(row["t0_head"], trim / speed + 0.5)), ("tail", row["t0_tail"]),
              ("start_edge", trim / speed + 0.5), ("end_edge", max(0.0, pdur - amatch.NEEDLE - 0.5)))
    for side, t0 in checks:
        est = lead + speed * t0
        t, pk = amatch.confirm_speed(mpath, t0, out, ostream, est, speed, window=6.0)
        res[side + "_offset"] = None if t is None else round(t - speed * t0, 3)
        res[side + "_peak"] = pk
        o = res[side + "_offset"]
        if o is None or abs(o - lead) > 0.5 or pk < config.XCORR_MIN_PEAK:
            fails.append("%s audio offset %s (peak %s)" % (side, o, pk))
    res["dur_diff"] = round(row["cut_duration"] - (speed * pdur + lead), 3)
    if abs(res["dur_diff"]) > config.VERIFY_MAX_DUR_DIFF:
        fails.append("duration diff %.2fs" % res["dur_diff"])
    res["verify_fail"] = "; ".join(fails)
    return res, not fails


# ------------------------------------------------------------------ step 1

# match results and identity survive a re-cut; everything derived from the cut file is rebuilt
RECUT_KEEP = {"date", "part_id", "part_uid", "title", "collection", "position", "media_name", "media_uid",
              "media_path", "part_duration", "full", "full_name", "full_duration", "head_vote", "tail_vote",
              "delta_head", "delta_tail", "xcorr_head", "xcorr_tail", "start", "end", "drift", "t0_head", "t0_tail",
              "status", "reason", "warnings", "out_name", "sid", "registered", "speed", "match"}


def refresh_state(state):
    """Take rows other commands (e.g. the conversion step, a parallel cut) wrote to disk since we loaded them,
    for every row this process has not changed itself."""
    disk = load_state()
    snap = _SNAP.setdefault(id(state), {})
    for uid, d in disk.items():
        if uid not in state or (uid in snap and _dump(state[uid]) == snap[uid]):
            state[uid] = d
            snap[uid] = _dump(d)


def process_date(date, state):
    refresh_state(state)
    log.info("=== %s", date)
    y, m, d = date.split("-")
    parts = mdb.list_parts(date)
    fulls = [Full(p) for p in full_lessons(date, state)]
    log.info("%d full lesson(s): %s; %d part unit(s)", len(fulls), [f.name for f in fulls], len(parts))
    rows, used = [], set()
    near_fulls = None
    for part in parts:
        row = {"date": date, "part_id": part["id"], "part_uid": part["uid"], "title": part["title"],
               "collection": part["collection"], "position": part["position"], "verified": False}
        rows.append(row)
        prev = state.get(part["uid"])
        if prev and prev.get("recut") and prev.get("start") is not None and prev.get("full"):
            # re-cut with the existing match (in/out unchanged); keep registration info
            keep = {k: v for k, v in prev.items() if k in RECUT_KEEP}
            row.update(keep)
            row.pop("reason", None)            # the new cut is judged afresh
            row["recut_done"] = True
            if row.get("out_name"):
                used.add(row["out_name"])
            continue
        if prev and prev.get("registered") and not prev.get("recut"):
            # already published: never re-cut here (its staging is cleaned after publishing); fixes go through
            # recut + register --replace-o/--replace-both
            row.update(prev)
            used.add(prev.get("out_name"))
            continue
        if prev and prev.get("verified") and os.path.exists(prev.get("staging", "")):
            row.update(prev)
            row["status"] = prev.get("status", "ok")
            used.add(prev["out_name"])
            if "end_edge_offset" not in prev:
                res, ok = verify(row)
                row.update(res)
                row["verified"] = ok and row["status"] == "ok"
                state[part["uid"]] = row
                save_state(state)
                log.info("re-verified %s with edge check: verified=%s start_edge=%s/%s end_edge=%s/%s %s",
                         part["uid"], row["verified"], res["start_edge_offset"], res["start_edge_peak"],
                         res["end_edge_offset"], res["end_edge_peak"], res["verify_fail"])
            else:
                log.info("part %s already cut+verified -> %s", part["uid"], prev["staging"])
            try:
                if "added_langs" not in row:
                    langs.add_missing(row, part)
                if not row.get("outputs_built"):
                    outputs.build(row, part)
            except Exception as e:
                log.exception("langs/outputs %s failed", part["uid"])
                row.update(verified=False, status="error", reason=str(e)[:300])
            state[part["uid"]] = row
            save_state(state)
            continue
        if mdb.has_hq_file(part):
            row.update(status="skip_has_hq")
            continue
        try:
            if fulls:
                locate_part(row, part, fulls)
            if not fulls or row.get("status") == "no_match":
                # Shabbat/holiday lessons: kabbalahmedia may date them on another day than the recording
                if near_fulls is None:
                    near_fulls = [Full(p) for nd in near_dates(date) for p in full_lessons(nd, state)]
                if near_fulls:
                    row.pop("status", None); row.pop("reason", None)
                    locate_part(row, part, near_fulls)
                    if row.get("full") and row.get("status") != "no_match":
                        # another day's recording: strong evidence only (seen: a shared opening melody matched
                        # 0.58/0.00 at the start of the next day's lesson, where that day's own part is)
                        ph = (row.get("xcorr_head") or [0])[0] or 0
                        pt = (row.get("xcorr_tail") or [0])[0] or 0
                        taken = [r2 for r2 in state.values() if r2.get("full") == row["full"] and
                                 r2.get("part_uid") != row["part_uid"] and r2.get("start") is not None and
                                 r2["start"] < row["end"] - 5 and row["start"] < r2["end"] - 5]
                        if ph < NEAR_MIN_PEAK or pt < NEAR_MIN_PEAK or taken:
                            log.info("  %s: nearby-day candidate in %s rejected (peaks %.2f/%.2f%s)", part["uid"],
                                     row["full_name"], ph, pt,
                                     "; overlaps %s" % [t["part_uid"] for t in taken] if taken else "")
                            for k in ("full", "full_name", "full_duration", "start", "end", "delta_head", "delta_tail",
                                      "xcorr_head", "xcorr_tail", "drift", "match", "speed"):
                                row.pop(k, None)
                            row["status"] = "no_match"
                    if row.get("full") and row.get("status") != "no_match":
                        rec = re.search(r"\d{4}-\d{2}-\d{2}", os.path.basename(row["full"]))
                        row["date_shift"] = {"mdb_date": date, "recording_date": rec.group(0) if rec else None}
                        log.info("  %s: found in %s (date shift %s -> %s)", part["uid"], row["full_name"], date,
                                 row["date_shift"]["recording_date"])
                if row.get("status") == "no_match" or not row.get("full"):
                    row.update(status="no_full" if not fulls and not near_fulls else "no_match",
                               reason="not in any converted full lesson of %s +-%d days" % (date, NEAR_DAYS))
        except Exception as e:  # keep going with the other parts
            log.exception("part %s failed", part["uid"])
            row.update(status="error", reason=str(e)[:300])
        src = mdb.name_source(part)
        name = cut.hq_name(src) if src else "mlt_s_rav_%s_part_%s" % (date, part["uid"])
        rd = mdb.real_date(row)            # date-shifted parts: files carry the recording's date
        if rd != date:
            name = name.replace(date, rd) if date in name else name
        ry, rm, rdd = rd.split("-")
        if name in used or os.path.exists(os.path.join(config.SOURCES, ry, rm, rdd, name + ".mp4")):
            name = "%s_%s" % (name, part["uid"])
        used.add(name)
        row["out_name"] = name

    parts_by_uid = {p["uid"]: p for p in parts}
    timelines = check_sequence(rows)
    search_sources(date, rows, parts_by_uid)
    # every MDB part of the date stays in the state with its status (nothing forgotten); never overwrite
    # a verified/published entry with an unmatched one
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        r["last_checked"] = now
        old = state.get(r["part_uid"]) or {}
        if r.get("status") in ("no_source", "no_match", "no_media", "no_full", "needs_convert", "no_speech",
                               "found_in_other") \
                and not old.get("verified") and not old.get("registered"):
            if r.get("status") != "needs_convert" and r.get("media_path"):
                r.setdefault("part_duration", round(audio.duration(r["media_path"]), 3))
            state[r["part_uid"]] = dict(r)
    save_state(state)
    for tl in timelines.items():
        log.info("timeline %s: %s", *tl)

    todo = [r for r in rows if r.get("status") in ("ok", "review") and not r.get("verified")]

    def work(row):
        try:
            finish_part(row, parts_by_uid[row["part_uid"]], y, m, d)
        except Exception as e:
            log.exception("cut/verify %s failed", row["part_uid"])
            row.update(status="error", verified=False, reason=str(e)[:300])
        state[row["part_uid"]] = row
        save_state(state)

    with ThreadPoolExecutor(max_workers=config.ENCODE_WORKERS) as pool:
        list(pool.map(work, todo))
    return rows, timelines


def finish_part(row, part, y, m, d):
    """Cut, verify, add languages, build mlt_s/mlt_o (video from the original), flag."""
    y, m, d = mdb.real_date(row).split("-")       # date-shifted parts are staged under the recording's date
    out = os.path.join(config.STAGING, y, m, d, row["out_name"] + ".mp4")
    k = cut.cut(row["full"], row["start"], row["end"], out)
    row.update(staging=out, cut_start=round(k, 3), lead=round(row["start"] - k, 3),
               cut_duration=round(audio.duration(out), 3), size=os.path.getsize(out), sha1=cut.sha1(out))
    res, ok = verify(row)
    row.update(res)
    row["verified"] = ok and row["status"] == "ok"
    langs.add_missing(row, part)
    if row.get("lang_check") not in (None, "ok"):
        row["verified"] = False
        row["reason"] = ("added language not where intended: " + row["lang_check"])[:300]
    outputs.build(row, part)
    row["pipeline"] = {"version": config.PIPELINE_VERSION, "rules": pipeline_flags(row)}
    log.info("cut %s %s-%s -> %s verified=%s edges=%s/%s %s", row["part_uid"], hms(row["start"]), hms(row["end"]),
             row["out_name"], row["verified"], res["start_edge_peak"], res["end_edge_peak"],
             res["verify_fail"] or row.get("reason", ""))


def pipeline_flags(row):
    """What this part went through - lets a later fix find exactly the affected parts."""
    f = ["audio-match"]
    if row.get("speed", 1.0) != 1.0:
        f.append("speed-corrected:%.5f" % row["speed"])
    if (row.get("lead") or 0) < -0.5:
        f.append("preroll-cut:%.1fs" % -row["lead"])
    for w in row.get("warnings") or []:
        if w.startswith("only "):
            f.append("one-anchor-match")
    for a in row.get("added_langs") or []:
        how = a.get("how", "")
        kind = "picture" if "aligned by picture" in how else "assumed" if "ASSUMED" in how else \
               "hebrew-offset" if "(offset from heb)" in how else "shared-offset"
        f.append("lang-%s:%s" % (kind, a["lang"]))
    for l, dct in (row.get("track_decisions") or {}).items():
        if dct["action"] != "keep":
            f.append("track-%s:%s" % (dct["action"], l))
        elif dct["why"].startswith("LISTEN"):
            f.append("listen:%s" % l)
    if row.get("date_shift"):
        f.append("date-shift:%s->%s" % (row["date_shift"]["mdb_date"], row["date_shift"]["recording_date"]))
    if row.get("full"):
        for idx, lang in cut.label_fixes(row["full"]).items():
            f.append("label-fix:ger->%s" % lang)
    if row.get("o_video") == "original":
        f.append("video-from-original")
    if row.get("calibration_note"):
        f.append("video-calibration-1-frame")
    if row.get("lang_check") not in (None, "ok"):
        f.append("lang-check-failed")
    return f


CSV_COLS = ["date", "part_uid", "part_id", "position", "title", "status", "verified", "reason", "verify_fail",
            "media_name", "part_duration", "original", "full_name", "start", "end", "drift", "cut_duration", "dur_diff",
            "head_offset", "tail_offset", "head_peak", "tail_peak", "head_text", "tail_text", "start_edge_offset", "start_edge_peak", "end_edge_offset", "end_edge_peak", "lead", "added_langs", "lang_ref", "warnings", "languages", "langs_dropped_silent", "langs_not_on_unit", "langs_unit_missing", "out_name", "o_name", "o_staging", "staging"]


def dates_for(args):
    if args.dates:
        return [d.strip() for d in args.dates.split(",") if d.strip()]
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    else:
        # dates with a converted full lesson OR an original in ShiurBoker/Special (not converted yet)
        dirs = glob.glob(os.path.join(config.SOURCES, str(args.year), "[0-9][0-9]", "[0-9][0-9]"))
        dates = {"-".join(p.split("/")[-3:]) for p in dirs if glob.glob(os.path.join(p, "mlt_s_*.mp4"))}
        for folder in sources.FOLDERS:
            for p in glob.glob(os.path.join(sources.ORIGINAL_ROOT, folder, str(args.year), "*")):
                m = re.search(r"(%d-\d{2}-\d{2})" % args.year, os.path.basename(p))
                if m:
                    dates.add(m.group(1))
        dates = sorted(dates)
        if getattr(args, "month", None):
            dates = [x for x in dates if x[5:7] == "%02d" % args.month]
        dates = dates[: args.limit] if args.limit else dates
    return dates


def cmd_cut(args, run):
    dates = dates_for(args)
    state = load_state()
    rows, timelines = [], {}
    for date in dates:
        r, t = process_date(date, state)
        rows += r
        timelines.update(t)
    write_report(run, dates, rows, timelines)
    # which parts belong to which recording, vs their MDB daily-lesson collections
    lm = lessons.build(state, set(dates))
    save_state(state)
    base = lessons.write(lm, os.path.join(config.REPORTS, "lesson_map_%s" % run))
    log.info("lesson map %s.json/.csv: %d recording(s), %d split across collections, %d with wrong order",
             base, len(lm), sum(L["split"] for L in lm), sum(L["bad_order"] for L in lm))


def cmd_lesson_map(args, run):
    """Read-only: build the lesson map from the current state (does not modify state)."""
    state = load_state()
    dates = set(d.strip() for d in args.dates.split(",")) if args.dates else None
    lm = lessons.build(state, dates)
    base = lessons.write(lm, os.path.join(config.REPORTS, "lesson_map_%s" % run))
    for L in lm:
        flags = " ".join(f for f in ("SPLIT" if L["split"] else "", "UNORDERED" if L.get("unordered") else "", "BAD_ORDER" if L["bad_order"] else "") if f)
        log.info("%s %s  %s  collections=%s", L["date"], L["recording"], flags or "ok", L["daily_collections"])
        for p in L["parts"]:
            log.info("    %d. %s %-55s %s-%s  in %s pos %s", p["order"], p["part_uid"], (p["file"] or "")[:55],
                     hms(p["start"]), hms(p["end"]), p["daily_collection"], p["daily_position"])
    log.info("lesson map: %s.json/.csv", base)


ANOMALY_SHARE = 0.5


def cmd_run(args, run):
    """Yearly/monthly runner: for each date match+cut+verify+build; optionally convert missing full lessons
    first (mpg.sh, waits for batch.sh); stops on an anomalous date. Never publishes."""
    dates = dates_for(args)
    state = load_state()
    log.info("run over %d date(s): %s .. %s", len(dates), dates[0] if dates else "-", dates[-1] if dates else "-")
    all_rows, timelines = [], {}
    for date in dates:
        rows, t = process_date(date, state)
        if args.convert:
            q = load_queue()
            if any(e["status"] == "pending" and (e["date"] == date or date in e.get("dates", [])) for e in q.values()):
                cmd_convert(argparse.Namespace(dates=date), run)
                rows, t = process_date(date, state)
        all_rows += rows
        timelines.update(t)
        matched = [r for r in rows if r.get("full") and r.get("status") in ("ok", "review", "error")]
        bad = [r for r in matched if not r.get("verified")]
        if len(matched) >= 3 and len(bad) / len(matched) > ANOMALY_SHARE:
            log.error("ANOMALY on %s: %d of %d matched parts failed verification - stopping the run here",
                      date, len(bad), len(matched))
            for r in bad:
                log.error("   %s %s", r["part_uid"], (r.get("verify_fail") or r.get("reason") or "")[:200])
            break
    write_report(run, dates, all_rows, timelines)
    lm = lessons.build(state, set(dates))
    save_state(state)
    lessons.write(lm, os.path.join(config.REPORTS, "lesson_map_%s" % run))


def write_report(run, dates, rows, timelines):
    os.makedirs(config.REPORTS, exist_ok=True)
    base = os.path.join(config.REPORTS, run)
    with open(base + ".json", "w") as f:
        json.dump({"run": run, "dates": dates, "timelines": timelines, "rows": rows}, f, ensure_ascii=False, indent=1)
    with open(base + ".csv", "w", newline="") as f:
        w = csv.DictWriter(f, CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    n = {}
    for r in rows:
        k = "verified" if r.get("verified") else r.get("status", "?")
        n[k] = n.get(k, 0) + 1
    log.info("report %s.json/.csv  summary %s", base, n)
    write_summary(base + ".summary.txt", rows, n)


FLAG_PREFIXES = ("speed-corrected", "preroll-cut", "one-anchor-match", "lang-assumed", "lang-hebrew-offset",
                 "lang-picture", "track-replace", "listen", "lang-check-failed")


def write_summary(path, rows, counts):
    import random
    lines = ["partcut summary %s" % time.strftime("%Y-%m-%d %H:%M"), "", "COUNTS: %s" % counts, ""]
    flagged = [r for r in rows if r.get("verified") and any(
        f.startswith(FLAG_PREFIXES) for f in (r.get("pipeline") or {}).get("rules", []))]
    lines.append("== VERIFIED WITH FLAGS - check these first (%d)" % len(flagged))
    for r in flagged:
        fl = [f for f in r["pipeline"]["rules"] if f.startswith(FLAG_PREFIXES)]
        lines.append("  %s %s %-55s %s" % (r["date"], r["part_uid"], (r.get("out_name") or "")[:55], " ".join(fl)))
    lines += ["", "== NOT PUBLISHABLE (by status)"]
    for st in ("review", "error", "needs_convert", "found_in_other", "no_match", "no_source", "no_media", "no_full"):
        rs = [r for r in rows if r.get("status") == st and not r.get("verified")]
        if rs:
            lines.append("  -- %s (%d)" % (st, len(rs)))
            for r in rs:
                lines.append("     %s %s %-45s %s" % (r["date"], r["part_uid"], (r.get("media_name") or "")[:45],
                                                     (r.get("verify_fail") or r.get("reason") or "")[:150]))
    clean = [r for r in rows if r.get("verified") and r not in flagged]
    sample = random.Random(len(rows)).sample(clean, max(1, len(clean) // 50)) if clean else []
    lines += ["", "== RANDOM SAMPLE of clean verified parts for spot checks (%d of %d)" % (len(sample), len(clean))]
    for r in sample:
        lines.append("  %s %s %s" % (r["date"], r["part_uid"], r.get("o_staging") or ""))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    log.info("summary %s", path)


# ------------------------------------------------------------------ step 2

def restore_staging(row):
    """Published part whose staging was cleaned: mlt_o copied back from kmedia, mlt_s linked (never copied/removed)."""
    reg = row["registered"]
    os.makedirs(os.path.dirname(row["o_staging"]), exist_ok=True)
    shutil.copyfile(reg["mlt_o"]["path"], row["o_staging"])
    if not os.path.exists(row["staging"]):
        os.makedirs(os.path.dirname(row["staging"]), exist_ok=True)
        os.symlink(reg["mlt_s"]["path"], row["staging"])
    log.info("%s: staging restored from the published files", row["part_uid"])


def cmd_rebuild_o(args, run):
    state = load_state()
    for uid, row in state.items():
        if args.uids and uid not in args.uids.split(","):
            continue
        if row.get("registered") and args.force and row.get("o_staging") and not os.path.exists(row["o_staging"]):
            restore_staging(row)
        if row.get("outputs_built") and (args.force or row.get("o_video") != "original") and os.path.exists(row.get("o_staging", "")):
            try:
                if args.video_only:
                    from . import original
                    original.rebuild_o_video(row)
                elif args.audio_only:
                    outputs.rebuild_o_audio(row)
                else:
                    outputs.rebuild_o(row)
            except Exception:
                log.exception("%s: rebuild failed", uid)
                continue
            save_state(state)
            log.info("%s: mlt_o rebuilt  languages=%s video=%s", uid, row["languages"], row.get("o_video"))


def cmd_fix_order(args, run):
    """Daily-collection order from the recordings (lesson map). Dry-run unless --execute."""
    from . import order
    state = load_state()
    dates = set(dates_for(args))
    plans = order.plan(state, dates)
    text = order.show(plans)
    out = os.path.join(config.REPORTS, "fix_order_%s.txt" % run)
    with open(out, "w") as f:
        f.write(text + "\n")
    kinds = {k: sum(1 for p in plans if p["kind"] == k) for k in ("change", "ok", "manual", "skip")}
    print(text)
    log.info("fix-order %s: %s collection(s) -> %s", "EXECUTE" if args.execute else "dry-run", kinds, out)
    if args.execute:
        res = order.execute(plans)
        log.info("fix-order written: %d ok, %d with mismatches", sum(1 for r in res if r[1]), sum(1 for r in res if not r[1]))


def cmd_register(args, run):
    """Step 2 for explicitly named, verified parts. Dry-run unless --execute."""
    from . import publish
    state = load_state()
    done = []
    if args.uids:
        uids = [u.strip() for u in args.uids.split(",") if u.strip()]
    else:
        want = (lambda d: d[:4] == str(args.year)) if args.year else (lambda d: d in set(args.dates.split(",")))
        uids = sorted((u for u, r in state.items() if r.get("date") and want(r["date"]) and r.get("verified")
                       and r.get("status") == "ok" and not r.get("registered")
                       and "lang-check-failed" not in (r.get("pipeline") or {}).get("rules", [])),
                      key=lambda u: (state[u]["date"], state[u].get("start") or 0))
        log.info("%d verified, unpublished part(s) selected", len(uids))
    for uid in uids:
        row = state.get(uid)
        if not row or not row.get("verified") or not row.get("outputs_built"):
            log.error("%s: not verified/built in state, skipping", uid)
            continue
        if row.get("registered"):
            if args.replace_both:
                try:
                    res = publish.replace_both(row, dry_run=not args.execute)
                    if args.execute:
                        row["registered"] = res
                        save_state(state)
                        ok, detail = publish.check_hls(res["publish_url"], len(row.get("languages") or []))
                        row["hls_check"] = {"ok": ok, "detail": detail}
                        save_state(state)
                        log.info("%s: mlt_s+mlt_o replaced | HLS %s %s", uid, "OK" if ok else "CHECK", detail)
                except Exception:
                    log.exception("%s: replace failed", uid)
            elif args.replace_o:
                try:
                    res = publish.replace_o(row, dry_run=not args.execute)
                    if args.execute:
                        row["registered"] = res
                        save_state(state)
                        log.info("%s: mlt_o replaced %s", uid, res)
                except Exception:
                    log.exception("%s: replace failed", uid)
            else:
                log.info("%s: already registered %s", uid, row["registered"])
            continue
        if not row.get("lesson"):
            lessons.build(state, {row["date"]})        # attach the recording/order info for WFDB
        try:
            res = publish.register(row, dry_run=not args.execute)
        except Exception as e:
            log.exception("%s: register failed", uid)
            row["register_error"] = str(e)[:300]
            save_state(state)
            continue
        if args.execute:
            row["sid"], row["registered"] = res["sid"], res
            row.pop("register_error", None)
            ok, detail = publish.check_hls(res["publish_url"], len(row.get("languages") or []))
            row["hls_check"] = {"ok": ok, "detail": detail, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            if ok and not args.keep_staging:
                publish.cleanup_staging(row)
            save_state(state)
            log.info("%s registered: %s | HLS %s %s", uid, res["publish_url"], "OK" if ok else "CHECK", detail)
            done.append(res)
    if args.execute:
        out = os.path.join(config.REPORTS, "registered_%s.json" % run)
        with open(out, "w") as f:
            json.dump(done, f, ensure_ascii=False, indent=1)
        log.info("registered %d part(s); %s", len(done), out)


def main():
    ap = argparse.ArgumentParser(prog="partcut")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cut", help="step 1: locate, cut to staging and verify (no MDB writes)")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--dates")
    g.add_argument("--year", type=int)
    c.add_argument("--limit", type=int)
    c.add_argument("--month", type=int)
    cv = sub.add_parser("convert", help="convert queued originals with mpg.sh/mp4.sh (waits for batch.sh), then cut")
    cv.add_argument("--dates", help="only originals queued for these dates")
    rn = sub.add_parser("run", help="yearly/monthly runner (match, cut, verify, build; never publishes)")
    g2 = rn.add_mutually_exclusive_group(required=True)
    g2.add_argument("--dates")
    g2.add_argument("--year", type=int)
    rn.add_argument("--month", type=int)
    rn.add_argument("--limit", type=int)
    rn.add_argument("--convert", action="store_true", help="convert missing full lessons (mpg.sh) when parts need them")
    r = sub.add_parser("register", help="step 2: move verified files to sources and register in MDB")
    rg = r.add_mutually_exclusive_group(required=True)
    rg.add_argument("--uids", help="comma list of part unit uids")
    rg.add_argument("--year", type=int, help="all verified, unpublished parts of a year")
    rg.add_argument("--dates", help="all verified, unpublished parts of these dates")
    r.add_argument("--keep-staging", action="store_true", help="do not delete staged files after a checked publish")
    r.add_argument("--execute", action="store_true", help="really write (default: print the plan only)")
    r.add_argument("--replace-o", action="store_true", help="registered parts: replace the published mlt_o")
    r.add_argument("--replace-both", action="store_true",
                   help="registered parts re-made (e.g. corrected languages): replace mlt_s and mlt_o")
    lm = sub.add_parser("lesson-map", help="read-only: parts per recording vs MDB daily-lesson collections")
    lm.add_argument("--dates")
    rb = sub.add_parser("rebuild-o", help="rebuild mlt_o (video from original, heb first) for built parts")
    rb.add_argument("--uids", help="comma list (default: all built parts)")
    rb.add_argument("--force", action="store_true", help="rebuild even if already from the original")
    rb.add_argument("--audio-only", action="store_true", help="keep the video, rebuild the audio tracks only")
    rb.add_argument("--video-only", action="store_true", help="keep the audio, re-encode the video from the original")
    fo = sub.add_parser("fix-order", help="daily-collection positions from the recording order (dry-run unless --execute)")
    g = fo.add_mutually_exclusive_group(required=True)
    g.add_argument("--dates")
    g.add_argument("--year", type=int)
    fo.add_argument("--month", type=int)
    fo.add_argument("--limit", type=int)
    fo.add_argument("--execute", action="store_true", help="really write the positions to MDB")
    args = ap.parse_args()
    run = time.strftime("%Y-%m-%d_%H-%M-%S")
    setup_logging(run)
    log.info("partcut %s run=%s", " ".join(sys.argv[1:]), run)
    {"cut": cmd_cut, "convert": cmd_convert, "register": cmd_register, "rebuild-o": cmd_rebuild_o, "lesson-map": cmd_lesson_map, "run": cmd_run,
     "fix-order": cmd_fix_order}[args.cmd](args, run)


if __name__ == "__main__":
    main()
