"""Originals (ShiurBoker/Special) for parts that match no converted full lesson.

1. is_converted(): an original counts as converted if ANY of these has it
     - /backup/files/sources/<year>/**/mlt_s_<name>.mp4
     - WFDB source registry (WFDB_KV/source/kv?file_name=...), the mpg.sh dedup check
     - an MDB file named mlt_s_<name>
   If found, the existing conversion is used, never converted again.
2. find_part(): locate a part's audio in the unconverted originals. Coarse search on a 200 Hz
   log-energy envelope of the whole original (cached), confirmed by 8 kHz waveform correlation.
"""
import glob
import hashlib
import logging
import os
import subprocess

import numpy as np
import requests
from scipy.signal import fftconvolve

from . import audio, config, mdb

log = logging.getLogger("partcut.sources")

ORIGINAL_ROOT = "/mnt/filer/original"
FOLDERS = ("ShiurBoker", "Special")
EXTS = (".mpg", ".mp4")
SOURCE_KV = config.WFDB_KV + "/source/kv"
ENV_SR = 200
NEEDLE = 20.0


def converted_name(orig):
    base = os.path.splitext(os.path.basename(orig))[0]
    return base.replace("_o_", "_s_", 1)


def is_converted(orig):
    """Path of an existing conversion of `orig`, 'mdb:<name>' if only MDB knows it, or None."""
    name = converted_name(orig)
    year = name.split("_")[3][:4] if len(name.split("_")) > 3 else ""
    for p in glob.glob(os.path.join(config.SOURCES, year, "*", "*", name + ".mp4")):
        return p
    try:
        rec = requests.get(SOURCE_KV, params={"file_name": name}, timeout=30).json() or []
        for r in rec:
            f = ((r.get("source") or {}).get("converted") or {}).get("filename")
            if f and os.path.exists(f):
                return f
        if rec:
            return "wfdb:" + name
    except (requests.RequestException, ValueError):
        log.warning("WFDB source kv lookup failed for %s", name)
    try:
        files = mdb._get("/files/", query=name).get("data") or []
        if any(f["name"] in (name, name + ".mp4") for f in files):
            return "mdb:" + name
    except requests.RequestException:
        log.warning("MDB lookup failed for %s", name)
    return None


def originals(date):
    """(unconverted, converted_paths) originals for `date` in ShiurBoker/Special."""
    y = date[:4]
    todo, done = [], []
    for folder in FOLDERS:
        for p in sorted(glob.glob(os.path.join(ORIGINAL_ROOT, folder, y, "*%s*" % date))):
            if not p.lower().endswith(EXTS) or not os.path.isfile(p):
                continue
            c = is_converted(p)
            if c is None:
                todo.append(p)
            elif os.path.exists(c):
                done.append(c)
            else:
                log.info("%s already converted (%s) but file not found locally", os.path.basename(p), c)
    return todo, done


def envelope_of(path):
    """Envelope of the first audio track of the whole file (cached; reading a 16 GB mpg is the slow part)."""
    st = os.stat(path)
    key = hashlib.sha1(("%s|%d" % (path, st.st_size)).encode()).hexdigest()[:12]
    cpath = os.path.join(config.CACHE, "env", "%s.%s.npy" % (os.path.basename(path), key))
    if os.path.exists(cpath):
        return np.load(cpath)
    os.makedirs(os.path.dirname(cpath), exist_ok=True)
    log.info("reading audio of %s", path)
    proc = subprocess.Popen(["nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:a:0",
                             "-ac", "1", "-ar", "8000", "-f", "f32le", "-"], stdout=subprocess.PIPE)
    hop, chunks, rest = 8000 // ENV_SR, [], np.zeros(0, np.float32)
    while True:
        buf = proc.stdout.read(8000 * 4 * 60)          # one minute at a time
        if not buf:
            break
        x = np.concatenate([rest, np.frombuffer(buf, np.float32)])
        n = len(x) // hop
        chunks.append(audio.envelope(x[: n * hop], 8000, ENV_SR))
        rest = x[n * hop:]
    proc.wait()
    env = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    np.save(cpath, env)
    return env


def _score(env, ne):
    n = len(ne)
    a = (ne - ne.mean()).astype(np.float64)
    e = env.astype(np.float64)
    num = fftconvolve(e, a[::-1], mode="valid")
    c1 = np.concatenate([[0.0], np.cumsum(e)])
    c2 = np.concatenate([[0.0], np.cumsum(e * e)])
    ws = c1[n:] - c1[:-n]
    wv = (c2[n:] - c2[:-n]) - ws * ws / n
    return num / (np.sqrt(np.maximum(wv, 1e-12)) * np.linalg.norm(a))


def _top(score, k=5, sep=ENV_SR * 5):
    s, idx = score.copy(), []
    for _ in range(k):
        i = int(np.argmax(s))
        if s[i] <= -1:
            break
        idx.append(i)
        s[max(0, i - sep): i + sep] = -2
    return idx


def find_part(media, t0, orig_paths):
    """Locate part audio [t0, t0+20s] in the originals -> (path, time_in_original, peak) or None."""
    from . import amatch
    ne = audio.envelope(audio.pcm(media, t0, NEEDLE, 8000), 8000, ENV_SR)
    best = None
    for path in orig_paths:
        env = amatch.envelope_of(path, "0:a:0")     # gap-filled, like the full lessons
        if len(env) < len(ne):
            continue
        for i in _top(_score(env, ne)):
            t, peak, _ = audio.locate(media, t0, path, i / ENV_SR - 5, NEEDLE, NEEDLE + 10, None, "0:a:0")
            # real matches score 0.97+; 0.5 let through false positives (2016 hitkalelut n3, 0.50/0.51)
            if t is not None and peak >= 0.8 and (best is None or peak > best[2]):
                best = (path, round(t, 3), round(peak, 3))
    return best


OTHER_FOLDERS = ("Rawmaterial", "LT")
MEDIA_EXTS = (".mpg", ".mp4", ".mov", ".mxf", ".mts", ".avi", ".wmv")


def other_candidates(date, hint=""):
    """Recordings of `date` in Rawmaterial/LT (incl. date-named subfolders), best name match first.
    Report-only: these are never queued for conversion."""
    y = date[:4]
    found = set()
    for folder in OTHER_FOLDERS:
        base = os.path.join(ORIGINAL_ROOT, folder, y)
        for pat in ("*%s*" % date, "*%s*/*" % date):
            for p in glob.glob(os.path.join(base, pat)):
                if os.path.isfile(p) and p.lower().endswith(MEDIA_EXTS):
                    found.add(p)
    words = set(w for w in os.path.basename(hint).lower().replace("-", "_").split("_") if len(w) > 2)

    def score(p):
        name = os.path.basename(p).lower().replace("-", "_")
        return -sum(w in name for w in words)
    return sorted(found, key=lambda p: (score(p), p))
