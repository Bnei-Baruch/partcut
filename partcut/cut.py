"""Stream-copy cutting from the converted full lesson."""
import hashlib
import os
import re
import subprocess

from . import config


def keyframe_at_or_before(path, t):
    """pts_time of the last video keyframe <= t (GOP is 1 s in our conversions)."""
    lo = max(0.0, t - 5.0)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-read_intervals", "%.3f%%%.3f" % (lo, t + 0.5), "-show_entries", "frame=pts_time",
         "-of", "csv=p=0", path], capture_output=True, text=True, check=True).stdout
    kf = []
    for line in out.split():
        v = line.strip().split(",")[0]      # ffprobe may append ",<side data>"
        if v and v != "N/A":
            kf.append(float(v))
    kf = [k for k in kf if k <= t + 1e-3]
    return max(kf) if kf else lo


# Known mislabelled tracks in mpg.sh conversions: until 2009-11-29 the recordings' 6th translation channel
# (tagged "ger" by mpg.sh's fixed language order) is Turkish; from 2009-11-30 it really is German.
GER_IS_TUR_UNTIL = "2009-11-29"


def label_fixes(full):
    """{audio stream index: correct language} for a converted full lesson."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(full))
    if not m or m.group(1) > GER_IS_TUR_UNTIL:
        return {}
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=comment:stream=index,codec_type:"
                          "stream_tags=language", "-of", "json", full], capture_output=True, text=True, check=True).stdout
    import json
    j = json.loads(out)
    if ((j.get("format") or {}).get("tags") or {}).get("comment") != "mpg":
        return {}
    tags = {st["index"]: (st.get("tags") or {}).get("language") for st in j["streams"] if st["codec_type"] == "audio"}
    if "tur" in tags.values():          # layout differs from the known one - leave it alone
        return {}
    return {i: "tur" for i, l in tags.items() if l == "ger"}


def audio_order(full):
    """audio stream indices of `full` in order (to turn a stream index into an a:N position)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", full], capture_output=True, text=True, check=True).stdout
    return [int(x) for x in out.split()]


def cut(full, start, end, out):
    """Copy [keyframe<=start, end] with all streams. Returns actual start (keyframe).
    Known label errors of the full lesson (label_fixes) are corrected in the cut."""
    k = keyframe_at_or_before(full, max(0.0, start))     # start may be < 0 (pre-roll not in the recording)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "%.3f" % k, "-i", full,
         "-t", "%.3f" % (end - k), "-map", "0", "-c", "copy",   # no make_zero: it shifts a/v ~80 ms off the cut point
         "-movflags", "+faststart", "-metadata", "comment=" + config.CUT_MARKER] + _fix_args(full) + [tmp],
        check=True)
    os.replace(tmp, out)
    return k


def _fix_args(full):
    fixes = label_fixes(full)
    if not fixes:
        return []
    pos = {idx: n for n, idx in enumerate(audio_order(full))}
    args = []
    for idx, lang in fixes.items():
        args += ["-metadata:s:a:%d" % pos[idx], "language=" + lang, "-metadata:s:a:%d" % pos[idx], "handler_name=" + lang]
    return args


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def hq_name(src_name):
    """heb_o_rav_x_2010-01-01_lesson_bb.mp4 -> mlt_s_rav_x_2010-01-01_lesson_bb"""
    base = os.path.splitext(os.path.basename(src_name))[0]
    base = re.sub(r"_hd$", "", base)
    base = "mlt_" + base.split("_", 1)[1] if "_" in base else "mlt_" + base
    return base.replace("_o_", "_s_", 1) if base.startswith("mlt_o_") else base
