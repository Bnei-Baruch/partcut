"""Add translation tracks that exist on the MDB part unit but not in the full lesson.

The unit's translation files are cut with the same in-point as its Hebrew file. We measure the
exact offset by correlating a translation that exists in BOTH places (e.g. MDB eng.mp3 vs the
cut's eng track); the missing languages are then muxed in with that offset.
"""
import json
import logging
import os
import subprocess

import numpy as np
import requests

from . import audio, config, cut, mdb

log = logging.getLogger("partcut.langs")

# archive filename prefixes that differ from the track codes used by mp4.sh/mpg.sh
NORMALIZE = {"trk": "tur", "chn": "chi"}
MAX_DUR_DIFF = 3.0


def file_lang(name):
    pre = name.split("_", 1)[0].lower()
    return NORMALIZE.get(pre, pre)


def cut_langs(path):
    return {s["index"]: (s.get("tags") or {}).get("language")
            for s in audio.probe(path)["streams"] if s["codec_type"] == "audio"}


def unit_translations(part):
    """{lang: best media file} for translation files (_t_) of the unit; mp3 preferred."""
    best = {}
    for f in part["files"]:
        n = f["name"].lower()
        if "_t_" not in n or not n.endswith((".mp3", ".mp4", ".m4a")):
            continue
        lang = file_lang(n)
        rank = (0 if n.endswith(".mp3") else 1, -(f.get("size") or 0))
        if lang not in best or rank < best[lang][0]:
            best[lang] = (rank, f)
    return {k: v[1] for k, v in best.items()}


def fetch(f):
    """CDN by uid; if the CDN doesn't have it (404), the file's own URL from MDB."""
    os.makedirs(os.path.join(config.CACHE, "langs"), exist_ok=True)
    ext = os.path.splitext(f["name"])[1].lower()
    dest = os.path.join(config.CACHE, "langs", f["uid"] + ext)
    try:
        return mdb.download(f["uid"], dest)
    except requests.HTTPError:
        url = (f.get("properties") or {}).get("url")
        if not url:
            raise
        r = requests.get(url, stream=True, timeout=120)
        r.raise_for_status()
        with open(dest + ".part", "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
        os.replace(dest + ".part", dest)
        return dest


def _video_offset(ours, theirs, t_ours, t_theirs, L=60.0, W=40.0):
    """Where theirs(t_theirs) lands in ours, by picture motion (+-W s). -> (offset, corr)"""
    from . import original
    a = original.motion_signature(theirs, t_theirs, L)
    b = original.motion_signature(ours, max(0.0, t_ours - W), L + 2 * W)
    a = (a - a.mean()) / (a.std() + 1e-9)
    best = (-1.0, 0)
    for k in range(0, len(b) - len(a)):
        y = b[k:k + len(a)]
        c = float(np.dot(a, (y - y.mean()) / (y.std() + 1e-9)) / len(a))
        if c > best[0]:
            best = (c, k)
    return round(max(0.0, t_ours - W) + best[1] / 25.0 - t_theirs, 3), round(best[0], 3)


def align_by_video(row, part, lang):
    """Translation whose mp3 doesn't fit the part: align the language's video file (mp4/wmv/flv) by picture
    at start and end; both offsets must agree. -> (path_with_audio, offset, file, why) or (None, None, None, why)"""
    vids = [f for f in part["files"] if file_lang(f["name"]) == lang and "_t_" in f["name"].lower()
            and f["name"].lower().endswith((".mp4", ".wmv", ".flv"))]
    vids.sort(key=lambda f: [".mp4", ".wmv", ".flv"].index(os.path.splitext(f["name"].lower())[1]))
    why = []
    for f in vids:
        try:
            path = fetch(f)
        except requests.HTTPError as e:
            why.append("%s not downloadable (%s)" % (f["name"], e.response.status_code))
            continue
        d = audio.duration(path)
        # 0. local: expected near offset 0 (same cut points, slightly different length)
        if d > row["cut_duration"] * 0.5:
            o1, c1 = _video_offset(row["staging"], path, 30.0, 30.0)
            te = min(row["cut_duration"], d + o1) - 120
            o2, c2 = _video_offset(row["staging"], path, te, te - o1, W=10.0)
            if min(c1, c2) >= 0.4 and abs(o1 - o2) <= 0.5:
                return path, round((o1 + o2) / 2, 3), f, "aligned by picture %s: %+.2f/%+.2f s (%.2f/%.2f)" % (
                    f["name"], o1, o2, c1, c2)
        # 1. where does our first minute sit anywhere in their file (whole-file search by picture)
        from . import original
        ours = original.motion_signature(row["staging"], 30.0, 60.0)
        theirs = original.motion_signature(path, 0.0, d)
        if len(theirs) < len(ours):
            why.append("%s too short" % f["name"])
            continue
        lag, c1 = audio.xcorr(ours.astype(np.float32), theirs.astype(np.float32))
        o1, c1 = round(30.0 - lag / 25.0, 3), round(c1, 3)          # our time = their time + o1
        # 2. the same offset must hold near the end of the overlap (their file may cover only part of ours)
        ov0, ov1 = max(0.0, o1), min(row["cut_duration"], d + o1)
        cover = (ov1 - ov0) / row["cut_duration"]
        if cover < 0.10 and ov0 > max(300.0, 0.1 * row["cut_duration"]):
            why.append("%s covers only %d%% of the part, not from the start" % (f["name"], round(100 * cover)))
            continue
        t2 = ov1 - 120
        if t2 - 60 <= 30:
            why.append("%s overlap too short to confirm (offset %+.2f)" % (f["name"], o1))
            continue
        o2, c2 = _video_offset(row["staging"], path, t2, t2 - o1, W=10.0)
        if min(c1, c2) >= 0.4 and abs(o1 - o2) <= 0.5:
            note = "" if cover >= 0.99 else ", covers %d%% of the part (%s-%s)" % (
                round(100 * cover), "%d:%02d" % (ov0 // 60, ov0 % 60), "%d:%02d" % (ov1 // 60, ov1 % 60))
            return path, round((o1 + o2) / 2, 3), f, "aligned by picture %s: %+.2f/%+.2f s (%.2f/%.2f)%s" % (
                f["name"], o1, o2, c1, c2, note)
        why.append("%s picture offsets %+.2f/%+.2f (%.2f/%.2f) disagree" % (f["name"], o1, o2, c1, c2))
    return None, None, None, "; ".join(why) or "no video file for %s" % lang


def measure_offset(row, trans, have):
    """Offset of the unit's translation files inside the cut, from a shared language."""
    by_lang = {lang: idx for idx, lang in have.items()}
    pdur = row["part_duration"]
    for lang in sorted(set(trans) & set(by_lang) - {"heb", "src"}):
        try:
            path = fetch(trans[lang])
        except requests.HTTPError:
            continue
        offs, peaks = [], []
        for t0 in (min(60.0, pdur * 0.1), pdur * 0.5, max(0.0, pdur - 60.0)):
            t, pk, _ = audio.locate(path, t0, row["staging"], t0 + row["lead"] - 3, 20, 26,
                                    None, "0:%d" % by_lang[lang])
            if t is not None:
                offs.append(t - t0)
                peaks.append(pk)
        if len(offs) == 3 and min(peaks) >= config.XCORR_MIN_PEAK and max(offs) - min(offs) <= 0.1:
            return {"lang": lang, "offset": round(sorted(offs)[1], 3), "peaks": [round(p, 3) for p in peaks]}
        log.info("  %s: shared lang %s did not align (offs %s peaks %s)", row["part_uid"], lang, offs, peaks)
    return None


def add_missing(row, part):
    """Mux missing translation tracks into row['staging']; updates sha1/size/duration in row."""
    out = row["staging"]
    have = cut_langs(out)
    trans = unit_translations(part)
    missing = sorted(set(trans) - set(have.values()) - {"heb", "src"})
    row["added_langs"], row["lang_ref"] = [], None
    row.pop("lang_skipped", None)
    if not missing:
        return
    ref = measure_offset(row, trans, have)
    if ref is None:
        # no shared translation to align on: fall back to the Hebrew offset, flag for review
        ref = {"lang": "heb", "offset": row.get("head_offset") or row["lead"], "peaks": None}
        row["lang_note"] = "added languages aligned on Hebrew offset only (no shared translation)"
    row["lang_ref"] = ref

    adds, skipped = [], []
    speed = row.get("speed", 1.0)
    # measure by picture where the "same timing as the part" assumption is unsafe
    risky = speed != 1.0 or ref["lang"] == "heb"
    for lang in missing:
        try:
            path = fetch(trans[lang])
        except requests.HTTPError as e:      # listed in MDB but not on the CDN
            path = None
            why_dl = "%s not downloadable (%s)" % (trans[lang]["name"], e.response.status_code)
        d = audio.duration(path) if path else None
        same_len = d is not None and abs(d - row["part_duration"]) <= MAX_DUR_DIFF
        if same_len and not risky:
            adds.append((lang, path, trans[lang], d, None, "same length as the part (offset from %s)" % ref["lang"]))
            continue
        vpath, voff, vf, why = align_by_video(row, part, lang)
        if vpath:
            hit, pk, method = (None, 0.0, "none")
            if path and abs(d - audio.duration(vpath)) <= 60:          # prefer the mp3 (better audio)
                t0 = min(300.0, d / 2)
                hit, pk, method = audio.locate(path, t0, vpath, t0 - 30, 20, 80, None, "0:a:0")
            if hit is not None and method == "wave" and pk >= 0.8:
                off = round(voff + (hit - t0), 3)
                adds.append((lang, path, trans[lang], d, off, why + "; mp3 matched to it (%.2f) -> offset %+.2f s" % (pk, off)))
            else:
                adds.append((lang, vpath, vf, audio.duration(vpath), voff, why + "; audio from the video file"))
        elif same_len:
            adds.append((lang, path, trans[lang], d, None, "same length as the part; picture not available (%s) - "
                         "ASSUMED part timing" % why))
        else:
            skipped.append("%s: %s; %s" % (lang, "duration %.1fs vs part %.1fs" % (d, row["part_duration"])
                                           if d else why_dl, why))
    if skipped:
        row["lang_skipped"] = skipped
    if not adds:
        return

    n_audio = len(have)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", out]
    for _, path, _, _, off, _ in adds:
        cmd += ["-itsoffset", "%.3f" % (ref["offset"] if off is None else off), "-i", path]
    cmd += ["-map", "0"]
    for i in range(len(adds)):
        cmd += ["-map", "%d:a:0" % (i + 1)]
    cmd += ["-c", "copy"]
    speed = row.get("speed", 1.0)
    for i, (lang, _, _, _, _, _) in enumerate(adds):
        k = n_audio + i
        # only files that take the MDB part's timing share its speed error; files aligned by picture were
        # measured on our timeline at both ends and are already at the right speed
        if speed != 1.0 and adds[i][4] is None:
            # resample (not atempo): a pure 0.3% speed change keeps the waveform comparable for check_added
            sr = int(json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                     "stream=sample_rate", "-of", "json", adds[i][1]], capture_output=True, text=True).stdout)
                     ["streams"][0]["sample_rate"])
            cmd += ["-filter:a:%d" % k, "asetrate=%.3f,aresample=48000" % (sr / speed)]
        cmd += ["-c:a:%d" % k, "aac", "-b:a:%d" % k, "128k", "-ac:a:%d" % k, "1", "-ar:a:%d" % k, "48000",
                "-metadata:s:a:%d" % k, "language=%s" % lang]
    tmp = out + ".langs.mp4"
    cmd += ["-t", "%.3f" % row["cut_duration"], "-movflags", "+faststart",
            "-metadata", "comment=" + config.CUT_MARKER, tmp]
    subprocess.run(cmd, check=True)
    os.replace(tmp, out)
    check_added(row, adds, ref)
    row["added_langs"] = [{"lang": l, "file": f["name"], "uid": f["uid"], "duration": round(d, 3),
                           "offset": ref["offset"] if off is None else off, "how": how}
                          for l, _, f, d, off, how in adds]
    row.update(cut_duration=round(audio.duration(out), 3), size=os.path.getsize(out), sha1=cut.sha1(out))
    log.info("  %s: added %s", row["part_uid"],
             ["%s %+.2fs (%s)" % (a[0], ref["offset"] if a[4] is None else a[4], a[5]) for a in adds])


def check_added(row, adds, ref):
    """After mixing: each added track must sit where intended (offset and speed) at two points."""
    from . import amatch
    out = row["staging"]
    have = {l: i for i, l in cut_langs(out).items()}
    speed = row.get("speed", 1.0)
    fails = []
    for lang, path, f, d, off, how in adds:
        tempo = speed if (speed != 1.0 and off is None) else 1.0
        o = ref["offset"] if off is None else off
        res = []
        for t in (min(120.0, d * 0.2), max(0.0, d * 0.8)):
            est = o + tempo * t
            if est < 0 or est > row["cut_duration"] - 25:
                continue
            got, pk = amatch.confirm_speed(path, t, out, "0:%d" % have[lang], est, tempo, window=4.0)
            if got is None or pk < 0.5:
                # translations are judged leniently: the loudness shape survives any audio processing
                nd = amatch.scaled_needle(path, t, tempo)
                hay = audio.pcm(out, max(0.0, est - 2), len(nd) / 8000 + 4, 8000, "0:%d" % have[lang])
                lag, ep = audio.xcorr(audio.envelope(nd, 8000, 200), audio.envelope(hay, 8000, 200))
                if lag is not None:
                    got, pk = max(0.0, est - 2) + lag / 200.0, round(ep, 3)
            res.append((None if got is None else round(got - est, 3), pk))
        # a translator lags the speaker anyway: catch errors of seconds, not tenths
        bad = [r for r in res if r[0] is None or abs(r[0]) > 1.0 or r[1] < 0.5]
        if not res or bad:
            fails.append("%s: %s" % (lang, res))
    row["lang_check"] = "ok" if not fails else "; ".join(fails)
    if fails:
        log.warning("  %s: added-language check failed: %s", row["part_uid"], row["lang_check"])
