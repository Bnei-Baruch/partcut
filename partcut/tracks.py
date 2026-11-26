"""Which audio tracks of the part go into mlt_o (HLS). mlt_s is never changed.

Per language track of the cut (reference = the original track: src, else heb):
  silent                              -> drop
  copy of the original (>= 4 of 5)    -> replace from the unit's MDB file, else drop
  same voice as other tracks (group)  -> member matching its MDB file: keep; not matching: replace from
                                         the MDB file if there is one, else drop
  unique speech                       -> keep (real translation, even without an MDB file)
Never dropped: heb/src (original), the unit's original_language, tracks added from MDB files.
"""
import logging
import subprocess

import numpy as np

from . import audio, langs, mdb

log = logging.getLogger("partcut.tracks")
POINTS = (0.15, 0.35, 0.55, 0.75, 0.9)
COPY_CORR = 0.8
SAME_CORR = 0.9
MATCH_FILE = 0.5
SILENT_RMS = 0.003
ISO = {"he": "heb", "ru": "rus", "en": "eng", "es": "spa", "de": "ger", "fr": "fre", "it": "ita", "tr": "tur"}


def _mmss(t):
    return "%d:%02d" % (t // 60, t % 60)


def _pcm(path, idx, t, length=20.0):
    return audio.pcm(path, max(0.0, t), length, 8000, "0:%d" % idx).astype(np.float64)


def _corr(a, b):
    """max normalized correlation of a inside b (b padded +-1 s)."""
    if len(a) == 0 or len(b) < len(a):
        return 0.0
    return audio.xcorr(a.astype(np.float32), b.astype(np.float32))[1]


TL_SR = 2000          # decode rate for the timeline (enough to tell copies/speech apart)
TL_WIN = 10.0        # seconds per window


def _decode(path, idx):
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:%d" % idx, "-ac", "1",
                          "-ar", str(TL_SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(np.float64)


MAX_DELAY = 3.0      # the original can reach a channel through another path, delayed (seen: +1.078 s)


def copy_delays(ref, trk):
    """Delays (in samples) at which trk carries the original: 0 plus any found by a wide search."""
    n = int(20 * TL_SR)
    d = int(MAX_DELAY * TL_SR)
    found = {0}
    L = min(len(ref), len(trk))
    for p in (0.1, 0.3, 0.5, 0.7, 0.9):
        s = int(L * p)
        b = trk[s:s + n]
        a = ref[max(0, s - d):s + n + d]
        if len(b) < n or float((b ** 2).mean() ** 0.5) < SILENT_RMS or len(a) < n:
            continue
        lag, pk = audio.xcorr(b.astype(np.float32), a.astype(np.float32))
        if pk >= COPY_CORR:
            found.add(int(lag - (s - max(0, s - d))))     # ref index = trk index + delay
    return sorted(found)


def timeline(ref, trk, delays=(0,)):
    """Per 10 s window: 0 = silent, 1 = copy of the original (at any known delay), 2 = translation."""
    n = int(TL_WIN * TL_SR)
    m = min(len(ref), len(trk)) // n
    out = np.zeros(m, np.int8)
    for k in range(m):
        b = trk[k * n:(k + 1) * n]
        if float((b ** 2).mean() ** 0.5) < SILENT_RMS:
            continue
        best = 0.0
        for dly in delays:
            for j in range(-8, 9):                   # +-4 ms around each delay
                s = k * n + dly + j
                if s < 0 or s + n > len(ref):
                    continue
                a = ref[s:s + n]
                c = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
                best = max(best, c)
        out[k] = 1 if best >= COPY_CORR else 2
    return out


def classify(row, part, original_language=None):
    S, dur = row["staging"], row["cut_duration"]
    have = langs.cut_langs(S)                        # {index: lang}
    ref = next((i for i, l in have.items() if l == "src"), None) or next(i for i, l in have.items() if l == "heb")
    protect = {"heb", "src"} | {a["lang"] for a in row.get("added_langs") or []}
    if original_language:
        protect.add(ISO.get(original_language, original_language))
    cand = {i: l for i, l in have.items() if l not in protect}

    refsig = _decode(S, ref)
    info = {}
    tls = {}
    for i, l in cand.items():
        sig = _decode(S, i)
        delays = copy_delays(refsig, sig)
        tl = timeline(refsig, sig, delays)
        tls[i] = tl
        trans = np.flatnonzero(tl == 2)
        info[i] = {"lang": l, "rms": 1.0 if (tl > 0).any() else 0.0, "windows": len(tl),
                   "translated": round(float((tl == 2).mean()) if len(tl) else 0.0, 3),
                   "copy": round(float((tl == 1).mean()) if len(tl) else 0.0, 3),
                   "first": float(trans[0] * TL_WIN) if len(trans) else None,
                   "copy_delays_ms": [round(1000 * x / TL_SR) for x in delays if x]}

    # same voice on several tracks, measured at 9 points: >= 2/3 -> duplicate group; >= 25% -> flag only
    pts9 = [dur * p for p in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)]
    live = [i for i in cand if info[i]["translated"] >= 0.02]          # copies/silence never form "voices"
    w9 = {i: {t: _pcm(S, i, t) for t in pts9} for i in live}
    pad = np.zeros(8000)
    share = {}

    def translating(i, t):
        k = int(t // TL_WIN)
        return k < len(tls[i]) and tls[i][k] == 2

    for a in live:
        for b in live:
            if a < b:
                both = [t for t in pts9 if translating(a, t) and translating(b, t)]
                if len(both) < 3:
                    share[(a, b)] = share[(b, a)] = 0.0
                    continue
                n = sum(_corr(w9[a][t], np.concatenate([pad, w9[b][t], pad])) >= SAME_CORR for t in both)
                share[(a, b)] = share[(b, a)] = n / len(both)
    in_group, partly = {}, {}
    for a in live:
        dup = [b for b in live if b != a and share.get((a, b), 0) >= 2 / 3]
        if dup:
            in_group[a] = [a] + dup
        part_ = [(b, share[(a, b)]) for b in live if b != a and 0.25 <= share.get((a, b), 0) < 2 / 3]
        if part_:
            partly[a] = part_

    trans = langs.unit_translations(part)
    offset = None
    decisions = {}
    for i, l in sorted(cand.items(), key=lambda x: x[1]):
        d = info[i]
        if d["rms"] < SILENT_RMS:
            decisions[l] = ("drop", "silent")
            continue
        frac, first = d["translated"], d["first"]
        start_limit = max(300.0, 0.10 * dur)
        where = "%d%% translated%s" % (round(100 * frac), "" if first is None else ", from %s" % _mmss(first))
        if frac >= 0.02 and first is not None and first > start_limit and frac < 0.10:
            decisions[l] = ("drop", "partial translation not from the start (%s)" % where)
            continue
        is_copy = frac < 0.02
        group = in_group.get(i)
        if not is_copy and not group:
            if i in partly:
                decisions[l] = ("keep", "LISTEN: partly the same voice as %s" %
                                ", ".join("%s (%d%%)" % (info[b]["lang"], round(100 * f)) for b, f in partly[i]))
            else:
                decisions[l] = ("keep", "translation (%s)" % where)
            continue
        why = ("no translation (%d%% copy of the original%s)" % (round(100 * d["copy"]),
               "" if not d["copy_delays_ms"] else ", delayed %s ms" % d["copy_delays_ms"])) if is_copy else \
              "same voice as %s" % ",".join(info[j]["lang"] for j in group if j != i)
        if l in trans:
            if offset is None:
                ref_off = langs.measure_offset(row, trans, have)
                offset = ref_off["offset"] if ref_off else (row.get("head_offset") or 0.0)
            path = langs.fetch(trans[l])
            t = dur * 0.5
            hit, pk, method = audio.locate(path, t, S, t + offset - 3, 20, 26, None, "0:%d" % i)
            if hit is not None and method == "wave" and pk >= MATCH_FILE and not is_copy:
                decisions[l] = ("keep", why + "; matches its MDB file (%.2f)" % pk)
            else:
                decisions[l] = ("replace", why + "; track does not match its MDB file (%.2f) -> MDB file %s"
                                % (pk, trans[l]["name"]))
        else:
            decisions[l] = ("drop", why + "; no MDB file for %s" % l)
    row["track_decisions"] = {l: {"action": a, "why": w} for l, (a, w) in decisions.items()}
    row["track_offset"] = offset
    for l, (a, w) in sorted(decisions.items()):
        if True:
            log.info("  %s track %s: %s (%s)", row["part_uid"], l, a, w)
    return decisions
