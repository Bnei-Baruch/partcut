"""Audio matching: locate an MDB part inside a full recording without transcription.

1. envelope_of(): 200 Hz log-energy envelope of the whole recording, read with gap filling
   (aresample=async=1) so positions follow the file's timestamp timeline - the same timeline
   ffmpeg -ss / the cut uses (some recordings have seconds of timestamp gaps). Cached per file.
2. anchors(): first and last 20 s of the part with speech-level energy.
3. search(): envelope cross-correlation -> top candidates -> 8 kHz waveform confirmation.
4. The tail anchor is first confirmed at its expected place (head delta + t0_tail), and only
   searched globally if that fails (parts often end in a song that resembles other moments).
Proof of concept on the pilot: 17/17 verified parts identical to the STT method (0.000 s).
"""
import hashlib
import logging
import os
import subprocess

import numpy as np

from . import audio, config, sources

log = logging.getLogger("partcut.amatch")
ENV_SR = 200
NEEDLE = 20.0
MIN_PEAK = 0.5
SPEECH_RMS = 0.01


def envelope_of(path, stream):
    st = os.stat(path)
    key = hashlib.sha1(("%s|%s|%d|gapfill" % (path, stream, st.st_size)).encode()).hexdigest()[:12]
    cpath = os.path.join(config.CACHE, "env", "%s.%s.npy" % (os.path.basename(path), key))
    if os.path.exists(cpath):
        return np.load(cpath)
    os.makedirs(os.path.dirname(cpath), exist_ok=True)
    proc = subprocess.Popen(["nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", stream,
                             "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                            stdout=subprocess.PIPE)
    hop, chunks, rest = 8000 // ENV_SR, [], np.zeros(0, np.float32)
    while True:
        buf = proc.stdout.read(8000 * 4 * 60)
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


def _rms(media, t, length=NEEDLE):
    a = audio.pcm(media, t, length, 8000)
    return float((a.astype("float64") ** 2).mean() ** 0.5) if len(a) else 0.0


def anchors(media, pdur):
    span = max(0.0, pdur - NEEDLE)
    head = next((float(t) for t in np.arange(0, min(120, span), 5) if _rms(media, t) > SPEECH_RMS), 0.0)
    tail = next((float(t) for t in np.arange(max(0, span - 1), max(0, span - 121), -5) if _rms(media, t) > SPEECH_RMS),
                max(0.0, span - 1))
    return head, tail


def confirm(media, t0, full, stream, est):
    """Waveform check around an estimated position -> (delta, peak) or None."""
    t, pk, method = audio.locate(media, t0, full, est - 10, NEEDLE, NEEDLE + 20, None, stream)
    if t is not None and method == "wave" and pk >= MIN_PEAK:
        return round(t - t0, 3), round(pk, 3)
    return None


def search(media, t0, fulls, k=10):
    """fulls: [(path, stream, env)] -> (path, delta, peak) or None."""
    ne = audio.envelope(audio.pcm(media, t0, NEEDLE, 8000), 8000, ENV_SR)
    best = None
    for path, stream, env in fulls:
        if len(env) < len(ne):
            continue
        for i in sources._top(sources._score(env, ne), k):
            c = confirm(media, t0, path, stream, i / ENV_SR)
            if c and (best is None or c[1] > best[2]):
                best = (path, c[0], c[1])
    return best


def locate(media, pdur, fulls):
    """-> dict(full, t0_head, t0_tail, delta_head, delta_tail, peak_head, peak_tail) or None."""
    h, t = anchors(media, pdur)
    hit = search(media, h, fulls)
    if hit:
        path, dh, ph = hit
        stream = next(s for p, s, _ in fulls if p == path)
        tl = confirm(media, t, path, stream, dh + t)          # tail where it should be
        if not tl:
            g = search(media, t, [x for x in fulls if x[0] == path])
            tl = (g[1], g[2]) if g else None
        return {"full": path, "t0_head": h, "t0_tail": t, "delta_head": dh, "peak_head": ph,
                "delta_tail": tl[0] if tl else None, "peak_tail": tl[1] if tl else 0.0}
    g = search(media, t, fulls)
    if g:
        return {"full": g[0], "t0_head": h, "t0_tail": t, "delta_head": None, "peak_head": 0.0,
                "delta_tail": g[1], "peak_tail": g[2]}
    return None


# ---------------------------------------------------------------- speed-shifted parts
# Some old MDB part files run slightly slow/fast (seen: 0.32% slow in 2009-01). Waveforms then drift apart
# within seconds while the loudness envelope still matches. We fit full_t = a + b * part_t on the envelope.

MAX_SPEED_DEV = 0.01


def scaled_needle(media, t0, speed, length=NEEDLE, sr=8000):
    """Part audio [t0, t0+length] resampled to the recording's timeline (length*speed seconds)."""
    a = audio.pcm(media, t0, length, sr).astype(np.float64)
    if len(a) == 0:
        return a.astype(np.float32)
    n = int(round(len(a) * speed))
    return np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)


def confirm_speed(media, t0, full, stream, est, speed, window=20.0):
    """Waveform check of a speed-corrected needle around est (= full time of part time t0)."""
    nd = scaled_needle(media, t0, speed)
    hay = audio.pcm(full, max(0.0, est - window / 2), len(nd) / 8000 + window, 8000, stream)
    lag, pk = audio.xcorr(nd, hay)
    if lag is None:
        return None, 0.0
    return max(0.0, est - window / 2) + lag / 8000, round(pk, 3)


def speed_fit(media, pdur, path, env, n=7):
    """-> dict(a, b, resid, min_score) or None. Whole-envelope match at n points, robust line fit."""
    pts = []
    for t0 in np.linspace(min(60.0, pdur * 0.1), max(0.0, pdur - NEEDLE - 30), n):
        ne = audio.envelope(audio.pcm(media, float(t0), NEEDLE, 8000), 8000, ENV_SR)
        if len(ne) == 0 or len(env) < len(ne):
            continue
        sc = np.nan_to_num(sources._score(env, ne), nan=-2.0)
        i = int(np.argmax(sc))
        pts.append((float(t0), i / ENV_SR, float(sc[i])))
    pts = [p for p in pts if p[2] >= 0.7]
    while len(pts) >= 4:
        t = np.array([p[0] for p in pts]); f = np.array([p[1] for p in pts])
        b, a = np.polyfit(t, f, 1)
        res = np.abs(f - (a + b * t))
        if res.max() <= 0.1 or len(pts) == 4:
            return {"a": float(a), "b": float(b), "resid": float(res.max()),
                    "min_score": min(p[2] for p in pts), "points": len(pts)}
        pts.pop(int(np.argmax(res)))             # drop the worst point (e.g. a repeated song) and refit
    return None


def locate_speed(media, pdur, fulls):
    """Fallback for speed-shifted parts. -> same dict as locate() plus 'speed', or None."""
    best = None
    for path, stream, env in fulls:
        fit = speed_fit(media, pdur, path, env)
        if not fit or abs(fit["b"] - 1) > MAX_SPEED_DEV or fit["resid"] > 0.1 or fit["min_score"] < 0.7:
            continue
        h, t = anchors(media, pdur)
        b = fit["b"]
        th, ph = confirm_speed(media, h, path, stream, fit["a"] + b * h, b)
        tt, pt = confirm_speed(media, t, path, stream, fit["a"] + b * t, b)
        if ph < MIN_PEAK or pt < MIN_PEAK:
            continue
        # start and end are placed independently (waveform, near the fit); if they disagree the part is not
        # continuous (edited) and goes to review via the drift check - never force them to agree
        a_h, a_t = th - b * h, tt - b * t
        if best is None or ph + pt > best["peak_head"] + best["peak_tail"]:
            best = {"full": path, "t0_head": h, "t0_tail": t, "speed": round(fit["b"], 6),
                    "delta_head": round(a_h, 3), "delta_tail": round(a_t + (b - 1) * pdur, 3),
                    "peak_head": ph, "peak_tail": pt, "fit_resid": round(fit["resid"], 3)}
    return best
