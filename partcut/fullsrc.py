"""Full-lesson mlt_s with every language.

Old recordings (e.g. heb_o_... originals, early 2009) carry only Hebrew, so mpg.sh produces a full lesson
with heb + src. Their translations exist only as MDB files per part, which partcut already aligned into
each part's mlt_s. This module rebuilds the full lesson as mlt_s_..._full.mp4 with one track per language:

  inside a verified part   -> that part's (aligned) translation track
  outside parts / no track -> Hebrew (mono), like a live translation channel with no translator

Video, Hebrew, existing translations and src are stream-copied; only the assembled tracks are encoded
(aac 128k mono, as mpg.sh does for translations). Track order follows mpg.sh: heb, translations in mpg.sh's
language order, src last. Nothing here writes to MDB/WFDB.
"""
import logging
import os
import re
import subprocess

import numpy as np

from . import audio, config, cut, langs

log = logging.getLogger("partcut.fullsrc")

# mpg.sh order of translation tracks
MPG_ORDER = ["heb", "eng", "rus", "spa", "fre", "ita", "ger", "por", "tur", "bul", "geo", "ron", "hun", "swe", "lit",
             "hrv", "jpn", "slv", "pol", "nor", "lav", "ukr", "dut", "chi", "amh", "hin", "per", "ara", "ind"]
SR = 8000


def _order(lang):
    if lang == "src":
        return (2, 0, "")
    return (0, MPG_ORDER.index(lang), "") if lang in MPG_ORDER else (1, 0, lang)


def full_name(path):
    base = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"^[a-z]{3}_s_", "mlt_s_", base)


def _stream(path, lang):
    return next(("0:%d" % i for i, l in sorted(langs.cut_langs(path).items()) if l == lang), None)


def part_file(row):
    reg = row.get("registered") or {}
    p = (reg.get("mlt_s") or {}).get("path")
    return p if p and os.path.exists(p) else row.get("staging")


def part_offset(full, row, pfile):
    """full_t = part_t + offset, measured on Hebrew (expected ~cut_start)."""
    dur = row["cut_duration"]
    best = None
    for t in (min(60.0, dur / 3), dur / 2, max(0.0, dur - 80)):
        nd = audio.pcm(pfile, t, 20, SR, _stream(pfile, "heb"))
        lo = max(0.0, row["cut_start"] + t - 5)
        hay = audio.pcm(full, lo, 30, SR, _stream(full, "heb"))
        lag, pk = audio.xcorr(nd, hay)
        if lag is not None and (best is None or pk > best[1]):
            best = (lo + lag / SR - t, pk)
    if not best or best[1] < 0.8:
        raise RuntimeError("%s: cannot place part in full (peak %s)" % (row["part_uid"], best))
    return round(best[0], 3), round(best[1], 3)


def plan(full, rows):
    """-> dict(full, out, existing{idx:lang}, new{lang:[(part_uid, file, stream, off, dur)]}, parts[...])"""
    existing = langs.cut_langs(full)
    existing.update(cut.label_fixes(full))        # e.g. "ger" that is really Turkish (until 2009-11-29)
    have = set(existing.values())
    parts, new = [], {}
    for r in sorted(rows, key=lambda x: x["start"]):
        pf = part_file(r)
        if not pf or not os.path.exists(pf):
            raise RuntimeError("%s: mlt_s file missing (%s)" % (r["part_uid"], pf))
        off, pk = part_offset(full, r, pf)
        dur = round(audio.duration(pf), 3)
        pl = langs.cut_langs(pf)
        parts.append({"part_uid": r["part_uid"], "file": pf, "offset": off, "peak": pk, "duration": dur,
                      "langs": sorted(l for l in pl.values() if l not in ("src", "heb"))})
        for i, l in sorted(pl.items()):
            if l in ("src", "heb", None) or l in have:
                continue
            new.setdefault(l, []).append((r["part_uid"], pf, i, off, dur))
    for i, p in enumerate(parts[1:], start=1):      # gate ranges must not overlap
        prev = parts[i - 1]
        if prev["offset"] + prev["duration"] > p["offset"] + 0.5:
            raise RuntimeError("parts overlap in full: %s / %s" % (prev["part_uid"], p["part_uid"]))
    y, m, d = re.search(r"(\d{4})-(\d{2})-(\d{2})", os.path.basename(full)).groups()
    out = os.path.join(config.STAGING, "full", y, m, d, full_name(full) + ".mp4")
    return {"full": full, "out": out, "existing": existing, "new": new, "parts": parts}


def build(p):
    full, out = p["full"], p["out"]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    inputs = sorted({f for segs in p["new"].values() for _, f, _, _, _ in segs})
    idx = {f: k + 1 for k, f in enumerate(inputs)}
    heb = _stream(full, "heb")
    fdur = audio.duration(full)
    graph, labels = [], {}
    langs_new = sorted(p["new"], key=_order)
    graph.append("[%s]aformat=sample_rates=48000:channel_layouts=mono,asplit=%d%s"
                 % (heb, len(langs_new), "".join("[h%d]" % k for k in range(len(langs_new)))))
    for k, lang in enumerate(langs_new):
        segs = p["new"][lang]
        gate = "+".join("between(t,%.3f,%.3f)" % (off, off + dur) for _, _, _, off, dur in segs)
        graph.append("[h%d]volume=volume=0:enable='%s'[g%d]" % (k, gate, k))
        mix = ["[g%d]" % k]
        for j, (_, f, si, off, dur) in enumerate(segs):
            lab = "t%d_%d" % (k, j)
            st = _start_time(f, si)        # track's own start (translations were added with an offset)
            graph.append("[%d:%d]aresample=48000,aformat=sample_rates=48000:channel_layouts=mono,asetpts=N/SR/TB,"
                         "atrim=end=%.3f,adelay=delays=%d:all=1[%s]"
                         % (idx[f], si, max(0.0, dur - st), round((off + st) * 1000), lab))
            mix.append("[%s]" % lab)
        graph.append("%samix=inputs=%d:normalize=0:duration=first,atrim=0:%.3f[n%d]"
                     % ("".join(mix), len(mix), fdur, k))
        labels[lang] = "[n%d]" % k
    tracks = [(l, "0:%d" % i) for i, l in p["existing"].items()] + [(l, labels[l]) for l in langs_new]
    tracks.sort(key=lambda t: _order(t[0]))
    cmd = ["nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", full]
    for f in inputs:
        cmd += ["-i", f]
    cmd += ["-filter_complex", ";".join(graph), "-map", "0:v:0"]
    for _, m in tracks:
        cmd += ["-map", m]
    cmd += ["-c:v", "copy", "-map_metadata", "0"]
    for a, (l, m) in enumerate(tracks):
        if m.startswith("["):
            cmd += ["-c:a:%d" % a, "aac", "-b:a:%d" % a, "128k", "-ac:a:%d" % a, "1"]
        else:
            cmd += ["-c:a:%d" % a, "copy"]
        cmd += ["-metadata:s:a:%d" % a, "language=" + l]
    tmp = out + ".tmp.mp4"
    cmd += ["-movflags", "+faststart", tmp]
    log.info("building %s: +%s", os.path.basename(out), " ".join(langs_new))
    subprocess.run(cmd, check=True)
    os.replace(tmp, out)
    return out


def _start_time(path, index):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", str(index), "-show_entries",
                          "stream=start_time", "-of", "csv=p=0", path], capture_output=True, text=True, check=True).stdout
    try:
        return float(out.strip().split(",")[0])
    except ValueError:
        return 0.0


def _rms(a):
    return float((a.astype("float64") ** 2).mean() ** 0.5) if len(a) else 0.0


def verify(p):
    """Every new track: the part's audio at its place (3 points per part) and Hebrew in the gaps."""
    full, out = p["full"], p["out"]
    res = {"ok": True, "checks": [], "fail": []}
    d0, d1 = audio.duration(full), audio.duration(out)
    if abs(d0 - d1) > 0.1:
        res["fail"].append("duration %.3f vs %.3f" % (d1, d0))
    got = langs.cut_langs(out)
    want = set(p["existing"].values()) | set(p["new"])
    if set(got.values()) != want or len(got) != len(want):
        res["fail"].append("tracks %s, expected %s" % (sorted(got.values()), sorted(want)))
    for i, l in p["existing"].items():          # copied tracks unchanged
        o = _stream(out, l)
        a, b = audio.pcm(full, d0 / 2, 10, SR, "0:%d" % i), audio.pcm(out, d0 / 2, 10, SR, o)
        lag, pk = audio.xcorr(a, np.concatenate([np.zeros(SR), b, np.zeros(SR)]))
        if lag is None or abs(lag - SR) > 8 or pk < 0.99:
            res["fail"].append("copied %s differs (lag %s peak %.3f)" % (l, lag, pk))
    heb_full = _stream(full, "heb")
    for lang, segs in p["new"].items():
        o = _stream(out, lang)
        for uid, f, si, off, dur in segs:
            for frac in (0.1, 0.5, 0.9):
                t = dur * frac
                nd = audio.pcm(f, t, 10, SR, "0:%d" % si)
                if _rms(nd) < 0.003:
                    continue
                lo = off + t - 2
                lag, pk = audio.xcorr(nd, audio.pcm(out, lo, 14, SR, o))
                err = None if lag is None else round(lo + lag / SR - (off + t), 3)
                res["checks"].append({"lang": lang, "part": uid, "t": round(off + t, 1), "err": err, "peak": round(pk, 3)})
                if err is None or abs(err) > 0.1 or pk < 0.8:      # translation voice: 0.1 s is inaudible
                    res["fail"].append("%s %s @%.0f: err %s peak %.3f" % (lang, uid, off + t, err, pk))
        # one gap per language: Hebrew there
        ranges = sorted((off, off + dur) for _, _, _, off, dur in segs)
        gaps = [(a, b) for a, b in zip([0.0] + [r[1] for r in ranges], [r[0] for r in ranges] + [d0]) if b - a > 40]
        for a, b in gaps[:2]:
            t = (a + b) / 2 - 5
            h = audio.pcm(full, t, 10, SR, heb_full)
            if _rms(h) < 0.003:
                continue
            lag, pk = audio.xcorr(h, audio.pcm(out, t - 1, 12, SR, o))
            res["checks"].append({"lang": lang, "gap": round(t, 1), "peak": round(pk, 3)})
            if lag is None or abs(lag - SR) > 40 or pk < 0.8:
                res["fail"].append("%s gap @%.0f is not Hebrew (peak %.3f)" % (lang, t, pk))
    res["ok"] = not res["fail"]
    res["sha1"], res["size"], res["duration"] = cut.sha1(out), os.path.getsize(out), round(d1, 3)
    res["tracks"] = [l for _, l in sorted(got.items())]
    return res
