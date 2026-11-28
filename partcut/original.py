"""mlt_o video from the ORIGINAL recording (one GPU encode), instead of copying the converted mlt_s video.

The full-lesson conversion (mpg.sh/mp4.sh) does not trim, so the original and mlt_s_..._full share one
timeline. That is confirmed per lesson by audio correlation (offset cached). For each part:
  original [cut_start+offset, +cut_duration] -> yadif=1 (50p) -> 1080 (4:3 pillarboxed like mpg.sh)
  -> h264_qsv high 4500k g100 (the live fhd settings) ; audio copied from the staged mlt_o
and the result is checked against the part's mlt_s video (frame-level motion signature).
"""
import glob
import json
import logging
import os
import subprocess
import threading

import numpy as np
import requests

from . import audio, config

log = logging.getLogger("partcut.original")

ORIGINAL_ROOT = "/mnt/filer/original"
SOURCE_KV = config.WFDB_KV + "/source/kv"
CACHE = os.path.join(config.CACHE, "originals.json")
BITRATE = os.environ.get("PARTCUT_O_BITRATE", "4500K")


def _load():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(c):
    with open(CACHE + ".tmp", "w") as f:
        json.dump(c, f, indent=1)
    os.replace(CACHE + ".tmp", CACHE)


def find_original(full_path):
    """Original recording of a converted full lesson (WFDB first, then by name)."""
    name = os.path.basename(full_path)[:-4]
    try:
        for r in requests.get(SOURCE_KV, params={"file_name": name}, timeout=30).json() or []:
            orig = ((r.get("source") or {}).get("parent") or {}).get("original_name")
            if orig:
                hits = glob.glob(os.path.join(ORIGINAL_ROOT, "*", "*", orig))
                if hits:
                    return hits[0]
    except (requests.RequestException, ValueError, IndexError):
        pass
    base = name.replace("_s_", "_o_", 1)
    for ext in (".mpg", ".mp4", ".mov", ".mxf"):
        hits = sorted(glob.glob(os.path.join(ORIGINAL_ROOT, "*", "*", base + ext)))
        if hits:
            return hits[0]
    return None


_TL_LOCK = threading.Lock()


def timeline(full_path):
    with _TL_LOCK:
        return _timeline(full_path)


def _timeline(full_path):
    """{'original', 'offset'}: original time = mlt_s time + offset. Cached per full lesson."""
    c = _load()
    if full_path in c:
        return c[full_path]
    orig = find_original(full_path)
    if not orig:
        c[full_path] = {"original": None}
        _save(c)
        return c[full_path]
    dur = audio.duration(full_path)
    heb = audio.heb_stream(full_path)
    offs, peaks = [], []
    for t in (min(300.0, dur * 0.1), dur * 0.5, max(0.0, dur - 300.0)):
        # needle from mlt_s, searched in the original's first audio track (+-10 s)
        hit, peak, _ = audio.locate(full_path, t, orig, t - 10, 20, 40, heb, "0:a:0")
        if hit is not None:
            offs.append(round(hit - t, 3))
            peaks.append(round(peak, 3))
    ok = len(offs) == 3 and min(peaks) >= config.XCORR_MIN_PEAK and max(offs) - min(offs) <= 0.1
    c[full_path] = {"original": orig, "offset": sorted(offs)[1] if ok else None, "offsets": offs, "peaks": peaks, "ok": ok}
    _save(c)
    log.info("timeline %s -> %s offsets %s peaks %s ok=%s", os.path.basename(full_path), orig, offs, peaks, ok)
    return c[full_path]


def _vfilter(orig):
    """Video profile for an original: SD -> native 576p50 square pixels (PAL); HD -> 1080p50 (fHD)."""
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,display_aspect_ratio", "-of", "json", orig],
                       capture_output=True, text=True, check=True)
    s = json.loads(p.stdout)["streams"][0]
    dar = s.get("display_aspect_ratio") or "%d:%d" % (s["width"], s["height"])
    w, h = (int(x) for x in dar.split(":"))
    sd = s["height"] <= 576
    # SD originals: enlarged to 1080 here with lanczos (sharper than the player's own scaling of 576p),
    # at 2500k - extra bitrate adds nothing visible for an SD source (tested). Label fHD (it is 1080).
    if abs(w / h - 16 / 9) < 0.05:
        scale = "scale=1920:1080:flags=lanczos"
    else:
        sw = int(round(1080 * w / h / 2)) * 2
        scale = "scale=%d:1080:flags=lanczos,pad=1920:1080:(ow-iw)/2:0" % sw
    return {"filter": "yadif=1,%s,setsar=1" % scale, "quality": "fHD", "tag": "fhd", "handler": "1080p",
            "bitrate": os.environ.get("PARTCUT_SD_BITRATE", "2500K") if sd else BITRATE, "size": "1920x1080",
            "nhd_bitrate": "500K" if sd else "600K", "qualities": ["nHD", "fHD"]}


def motion_signature(path, start, length, fps=25):
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "%.3f" % start, "-t", "%.3f" % length, "-i", path,
                          "-map", "0:v:0", "-vf", "fps=%d,scale=64:36,format=gray" % fps, "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    f = np.frombuffer(raw, np.uint8).reshape(-1, 36, 64).astype(np.float32)
    return np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))


def sync_frames(a_path, b_path, start, length=60):
    """Frame lag of b relative to a (at 25 fps) from motion signatures; (lag, corr)."""
    a, b = motion_signature(a_path, start, length), motion_signature(b_path, start, length)
    n = min(len(a), len(b))
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    best = (0, -1.0)
    for lag in range(-12, 13):
        x, y = (a[lag:], b[:n - lag]) if lag >= 0 else (a[:n + lag], b[-lag:])
        c = float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9))
        if c > best[1]:
            best = (lag, round(c, 3))
    return best


def _frames_signature(path, start, length):
    """Motion signature per decoded frame (no fps filter: frame sequence only, timestamps ignored)."""
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "%.3f" % start, "-i", path,
                          "-frames:v", str(int(length * 25)), "-map", "0:v:0", "-vf", "scale=64:36,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    f = np.frombuffer(raw, np.uint8).reshape(-1, 36, 64).astype(np.float32)
    return np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))


def _lag(a, b, rng=25):
    n = min(len(a), len(b))
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    best = (0, -1.0)
    for lag in range(-rng, rng + 1):
        x, y = (a[lag:], b[:n - lag]) if lag >= 0 else (a[:n + lag], b[-lag:])
        c = float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9))
        if c > best[1]:
            best = (lag, round(c, 3))
    return best


PREROLL = 5.0


def start_frame(row, orig, start):
    """(coarse_seek, N): seeking the original to coarse_seek and dropping N decoded frames lands exactly
    on the part's first frame. ffmpeg's -ss into MPEG-PS snaps to a nearby GOP, but a given -ss value
    always lands on the same frame, so we count frames from there instead of trusting timestamps."""
    coarse = max(0.0, start - PREROLL)
    sig_o = _frames_signature(orig, coarse, PREROLL + 25)
    ref = _frames_signature(row["staging"], 0, 15)
    m = len(ref)
    best = (None, -1.0)
    for n in range(0, len(sig_o) - m):
        x = sig_o[n:n + m] - sig_o[n:n + m].mean()
        y = ref - ref.mean()
        c = float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9))
        if c > best[1]:
            best = (n, c)
    if best[1] < 0.6:      # first guess only: calibration probes + the final sync check (both >= 0.6) confirm it
        raise RuntimeError("cannot locate the part's first frame in the original (corr %.2f)" % best[1])
    return coarse, best[0], round(best[1], 3)


def _encode_cmd(orig, coarse, nskip, duration, out, segs=None):
    """nskip < 0: the part starts before the original's first frame (the converted full lesson repeats
    the first frame there) -> pad with that many copies of the first frame.
    segs (censored parts): recording frames [(a, b)] kept; taken from the same decode by frame count
    (frame a of the recording = decoded frame nskip + a - segs[0][0])."""
    if segs:
        if nskip < 0:
            raise RuntimeError("censored part starting before the original - not supported")
        K = len(segs)
        head = "[0:v:0]split=%d%s;" % (K, "".join("[s%d]" % i for i in range(K)))
        for i, (a, b) in enumerate(segs):
            st = nskip + a - segs[0][0]
            head += "[s%d]trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS[t%d];" % (i, st, st + b - a, i)
        head += "%sconcat=n=%d:v=1:a=0" % ("".join("[t%d]" % i for i in range(K)), K)
    else:
        pre = "trim=start_frame=%d,setpts=PTS-STARTPTS" % nskip if nskip >= 0 else \
              "setpts=PTS-STARTPTS,tpad=start=%d:start_mode=clone" % -nskip
        head = "[0:v:0]" + pre
    return ["nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-init_hw_device", "qsv=hw", "-filter_hw_device", "hw",
            "-ss", "%.3f" % coarse, "-i", orig,
            "-filter_complex", "%s,%s,hwupload=extra_hw_frames=64,format=qsv,split=2[v][n];"
            "[n]vpp_qsv=w=640:h=360[vn]" % (head, _vfilter(orig)["filter"]), "-t", "%.3f" % duration,
            # v:0 = fhd (1080), v:1 = nhd (360, scaled on the GPU - the site's default quality, needed by trim)
            "-map", "[v]", "-map", "[vn]", "-an", "-c:v", "h264_qsv", "-profile:v", "high",
            "-b:v:0", _vfilter(orig)["bitrate"], "-b:v:1", _vfilter(orig)["nhd_bitrate"], "-g", "100",
            "-movflags", "+faststart", out]


def calibrate(row, orig, coarse, nskip):
    """Probe-encode 20 s with the real pipeline and correct nskip until the picture lines up with mlt_s."""
    probe = row["o_staging"] + ".probe.mp4"
    env = dict(os.environ, LIBVA_MESSAGING_LEVEL="0")
    tried = {}                   # nskip -> (|lag|, -corr)
    try:
        for _ in range(4):
            if nskip in tried:       # oscillating between offsets one frame apart (e.g. -1/+1)
                break
            subprocess.run(_encode_cmd(orig, coarse, nskip, 20, probe), check=True, env=env)
            lag, corr = _lag(motion_signature(row["staging"], 1, 15), motion_signature(probe, 1, 15))
            if corr < 0.6:      # same threshold as the final sync check (low-motion lessons score ~0.7)
                raise RuntimeError("probe does not match mlt_s (corr %.2f)" % corr)
            tried[nskip] = (abs(lag), -corr)
            if lag == 0:
                return nskip
            nskip -= lag
            if nskip < -50:          # more than 2 s of padding would not be the first-frame case
                raise RuntimeError("calibration out of range")
        best = min(tried, key=lambda k: tried[k])
        if tried[best][0] <= 1:      # within the final sync check's tolerance (that check still runs)
            log.info("  %s: calibration settled at 1 frame (tried %s)", row["part_uid"], tried)
            row["calibration_note"] = "1-frame (oscillating %s)" % sorted(tried)
            return best
        raise RuntimeError("calibration did not converge (tried %s)" % tried)
    finally:
        if os.path.exists(probe):
            os.remove(probe)


def rebuild_o_video(row):
    """Replace the video of row['o_staging'] with a fresh encode from the original. Updates row."""
    tl = timeline(row["full"])
    if not tl.get("original") or not tl.get("ok"):
        raise RuntimeError("no aligned original for %s: %s" % (row["full_name"], tl))
    o = row["o_staging"]
    vid = o + ".v.mp4"
    coarse, nskip, corr = start_frame(row, tl["original"], row["cut_start"] + tl["offset"])
    nskip2 = calibrate(row, tl["original"], coarse, nskip)
    row["original_seek"] = {"coarse": coarse, "skip_frames": nskip2, "first_guess": nskip, "corr": corr}
    segs = row.get("segments")
    if segs and (segs[0][1] - segs[0][0]) < 25 * 25:
        raise RuntimeError("censored part: first segment shorter than the 25 s calibration window")
    cmd = _encode_cmd(tl["original"], coarse, nskip2, row["cut_duration"], vid, segs)
    subprocess.run(cmd, check=True, env=dict(os.environ, LIBVA_MESSAGING_LEVEL="0"))
    mux = o + ".mux.mp4"
    prof = _vfilter(tl["original"])
    meta = ["-metadata:s:v:0", "language=" + prof["tag"], "-metadata:s:v:0", "handler_name=" + prof["handler"],
            "-metadata:s:v:1", "language=nhd", "-metadata:s:v:1", "handler_name=360p",
            "-metadata", "network=Bnei Baruch",
            "-metadata", "comment=" + (row.get("sid") or config.CUT_MARKER)]
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", vid, "-i", o, "-map", "0:v:0", "-map", "0:v:1", "-map", "1:a",
                    "-c", "copy", "-map_metadata", "1"] + meta + ["-movflags", "+faststart", mux], check=True)
    # picture must line up with the part's mlt_s (same audio there) - check twice
    at = [5.0, max(5.0, row["cut_duration"] - 70)]
    if segs:                                # and on both sides of every splice
        from . import splice
        for T in splice.out_splice_times(segs):
            at += [max(5.0, T - 62), T + 2]
    checks = [sync_frames(row["staging"], mux, t) for t in at]
    bad = [c for c in checks if abs(c[0]) > 1 or c[1] < 0.6]
    if bad:
        os.remove(vid)
        os.remove(mux)
        raise RuntimeError("video from original not in sync with mlt_s: %s" % checks)
    os.replace(mux, o)
    os.remove(vid)
    from . import cut
    row.update(o_video="original", o_quality=prof["quality"], o_qualities=prof["qualities"], o_size_px=prof["size"], original=tl["original"], original_offset=tl["offset"], o_video_sync=checks,
               o_size=os.path.getsize(o), o_sha1=cut.sha1(o), o_duration=round(audio.duration(o), 3))
    log.info("  %s: mlt_o video from %s (offset %.3f) sync %s", row["part_uid"], os.path.basename(tl["original"]),
             tl["offset"], checks)
