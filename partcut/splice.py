"""Censored parts (user, 2026-11-28): kabbalahmedia's part was edited after the lesson - spans were cut out of
it - while the full lesson was not touched. The HQ files must be exactly the published part: the recording's
segments without the removed spans.

- splices(row): where the part's offset into the recording jumps (30 s map, then refined to 20 ms by short-window
  waveform correlation on both alignments) -> [{"part_t", "off1", "off2"}]
- segments(row, spl): the recording's kept segments on the 25 fps frame grid (sum == part duration)
- cut_spliced(full, segs, out): mlt_s = the segments joined, re-encoded exactly like mpg.sh (libx264, interlaced
  tff, GOP 25, aac 128k), every audio track and its language kept
- the mlt_o video takes the same segments from the original (original.rebuild_o_video, frame counts from one
  calibrated seek), and check(row) compares the audio on both sides of every splice with the MDB part.
"""
import logging
import os
import subprocess

import numpy as np

from . import audio, config, cut

log = logging.getLogger("partcut.splice")
FPS = 25
SR = 8000


def _off(row, t, guess, fstream):
    """exact offset (recording - part) at part time t near `guess`, or None"""
    hit, peak, _ = audio.locate(row["media_path"], t, row["full"], t + guess - 3, 10, 16, None, fstream)
    return (round(hit - t, 3), peak) if hit is not None and peak >= 0.8 else (None, peak)


def offset_map(row, step=30.0):
    fstream = audio.heb_stream(row["full"])
    pdur, out, guess = row["part_duration"], [], row["delta_head"]
    t = 2.0
    while t < pdur - 12:
        o, p = _off(row, t, guess, fstream)
        if o is None:                         # maybe right after a splice: search wider
            hit, p2, _ = audio.locate(row["media_path"], t, row["full"], t + guess - 10, 10, 400, None, fstream)
            if hit is not None and p2 >= 0.8:
                o = round(hit - t, 3)
        if o is not None:
            out.append((t, o))
            guess = o
        t += step
    return out


def _corr_curve(p, f, win, hop):
    n = min(len(p), len(f))
    res = []
    for i in range(0, n - win, hop):
        a, b = p[i:i + win], f[i:i + win]
        if float((a ** 2).mean() ** 0.5) < 0.003:
            res.append(None)                  # silence: says nothing
            continue
        a, b = a - a.mean(), b - b.mean()
        res.append(float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)))
    return res


def _refine(row, t1, t2, off1, off2, fstream):
    """part time where the content switches from alignment off1 to off2 (t1 < switch < t2+10)"""
    L = (t2 + 10) - t1
    p = audio.pcm(row["media_path"], t1, L, SR)
    f1 = audio.pcm(row["full"], t1 + off1, L, SR, fstream)
    f2 = audio.pcm(row["full"], t1 + off2, L, SR, fstream)
    win, hop = int(0.2 * SR), int(0.02 * SR)
    c1, c2 = _corr_curve(p, f1, win, hop), _corr_curve(p, f2, win, hop)
    last1 = max((i for i, c in enumerate(c1) if c is not None and c > 0.6), default=None)
    first2 = min((i for i, c in enumerate(c2) if c is not None and c > 0.6 and (last1 is None or i > last1 - 10)),
                 default=None)
    if last1 is None or first2 is None:
        raise RuntimeError("splice between part %.0f and %.0f s not found (%s/%s)" % (t1, t2, last1, first2))
    a = t1 + (last1 * hop + win) / SR         # content up to here is on off1
    b = t1 + first2 * hop / SR                # content from here is on off2
    if b < a - 0.1:
        raise RuntimeError("splice near part %.1f s: alignments overlap (%.2f > %.2f)" % (a, a, b))
    return round((a + b) / 2, 3), round(a, 3), round(b, 3)


def splices(row):
    if row.get("speed", 1.0) != 1.0:
        raise RuntimeError("censored part with a speed-shifted MDB file - not supported")
    fstream = audio.heb_stream(row["full"])
    m = offset_map(row)
    out = []
    for (ta, oa), (tb, ob) in zip(m, m[1:]):
        if abs(ob - oa) < 0.1:
            continue
        if ob < oa:
            raise RuntimeError("part %.0f-%.0f s: offset goes back %.1f s (repeated content, not a cut-out)" % (ta, tb, oa - ob))
        o1, _ = _off(row, ta, oa, fstream)
        o2, _ = _off(row, tb, ob, fstream)
        t, a, b = _refine(row, ta, tb, o1 or oa, o2 or ob, fstream)
        out.append({"part_t": t, "off1": o1 or oa, "off2": o2 or ob, "removed": round((o2 or ob) - (o1 or oa), 3),
                    "between": [a, b]})
        log.info("  %s: cut-out at part %.2f s: %.2f s removed (recording %.2f-%.2f)", row["part_uid"], t,
                 out[-1]["removed"], t + out[-1]["off1"], t + out[-1]["off2"])
    return out


def _fr(t):
    return int(round(t * FPS))


def segments(row, spl):
    """recording frames [(start_frame, end_frame)] kept; the part is their concatenation"""
    if not spl:
        raise RuntimeError("no cut-out found")
    start = row["delta_head"]
    if start < -0.02:
        raise RuntimeError("censored part starting before the recording - not supported")
    segs, cur = [], _fr(max(0.0, start))
    for s in spl:
        e = _fr(s["part_t"] + s["off1"])
        segs.append((cur, e))
        cur = e + _fr(s["removed"])
    end = _fr(row["part_duration"] + spl[-1]["off2"])
    segs.append((cur, min(end, _fr(row["full_duration"]))))
    total = sum(b - a for a, b in segs) / FPS
    if abs(total - row["part_duration"]) > 0.25:
        raise RuntimeError("segments add up to %.2f s, the part is %.2f s" % (total, row["part_duration"]))
    return segs


def cut_spliced(full, segs, out):
    """mlt_s from the full lesson's segments, encoded like mpg.sh. Returns the full's time of the first frame."""
    astreams = cut.audio_order(full)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream_tags=language",
                            "-of", "csv=p=0", full], capture_output=True, text=True, check=True).stdout.split()
    k0 = cut.keyframe_at_or_before(full, max(0.0, segs[0][0] / FPS - 2))
    f0 = _fr(k0)
    K, N = len(segs), len(astreams)
    fc = ["[0:v:0]split=%d%s" % (K, "".join("[vs%d]" % i for i in range(K)))]
    for j in range(N):
        fc.append("[0:a:%d]asplit=%d%s" % (j, K, "".join("[as%d_%d]" % (j, i) for i in range(K))))
    order = []
    for i, (a, b) in enumerate(segs):
        fc.append("[vs%d]trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS[v%d]" % (i, a - f0, b - f0, i))
        order.append("[v%d]" % i)
        for j in range(N):
            fc.append("[as%d_%d]atrim=start=%.3f:end=%.3f,asetpts=PTS-STARTPTS[a%d_%d]"
                      % (j, i, (a - f0) / FPS, (b - f0) / FPS, j, i))
            order.append("[a%d_%d]" % (j, i))
    fc.append("%sconcat=n=%d:v=1:a=%d[vo]%s" % ("".join(order), K, N, "".join("[ao%d]" % j for j in range(N))))
    maps = ["-map", "[vo]"]
    meta = []
    for j in range(N):
        maps += ["-map", "[ao%d]" % j]
        lang = probe[j] if j < len(probe) else "und"
        meta += ["-metadata:s:a:%d" % j, "language=" + lang, "-metadata:s:a:%d" % j, "handler_name=SoundHandler"]
    tmp = out + ".tmp.mp4"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    subprocess.run(["nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "%.3f" % k0, "-i", full,
                    "-filter_complex", ";".join(fc)] + maps +
                   ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-flags", "+ildct+ilme", "-top", "1",
                    "-x264-params", "tff=1", "-r", "25", "-g", "25", "-c:a", "aac", "-b:a", "128k"] + meta +
                   ["-map_metadata", "-1", "-metadata", "network=mltcap", "-metadata", "comment=" + config.CUT_MARKER]
                   + cut._fix_args(full) + ["-movflags", "+faststart", tmp], check=True)
    os.replace(tmp, out)
    return segs[0][0] / FPS


def out_splice_times(segs):
    """times in the cut where a splice is"""
    t, res = 0.0, []
    for a, b in segs[:-1]:
        t += (b - a) / FPS
        res.append(round(t, 3))
    return res


def check(row):
    """audio of the cut vs the MDB part on both sides of every splice (same time in both) -> list of fails"""
    fails, res = [], []
    ost = audio.heb_stream(row["staging"])
    for T in out_splice_times(row["segments"]):
        for t0 in (T - 7, T + 2):
            if t0 < 0 or t0 + 5 > row["part_duration"]:
                continue
            hit, peak, _ = audio.locate(row["media_path"], t0, row["staging"], t0 - 2, 5, 9, None, ost)
            off = None if hit is None else round(hit - t0, 3)
            res.append((T, t0, off, round(peak, 3)))
            if peak >= 0.5 and (off is None or abs(off) > 0.1):
                fails.append("splice %.1f s: audio %s s off (peak %.2f)" % (T, off, peak))
    row["splice_check"] = res
    return fails
