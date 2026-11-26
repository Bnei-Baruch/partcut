"""Build the two final files per part, in the current site layout (stream copy only).

  mlt_s_<part>.mp4  -> sources/Y/M/D  (local, not published)
      v: 1 track lang=fhd handler=1080p
      a: src (stereo, "Source") first, then languages alphabetically (heb stereo, others mono)
  mlt_o_<part>.mp4  -> kmedia/Y/M/D   (HLS source, published)
      no src/ori (old recordings have no separate original track; src is only a copy of heb);
      heb first (nginx-vod makes the first audio track the default), then languages alphabetically
"""
import logging
import os
import subprocess

from . import audio, config, cut, langs, mdb

log = logging.getLogger("partcut.outputs")

# 3-letter track code -> MDB 2-letter language (as MDB stores them, e.g. slv->cs, ukr->ua)
MDB_LANG = {
    "ori": "or", "src": "or", "heb": "he", "eng": "en", "rus": "ru", "spa": "es", "fre": "fr", "ita": "it",
    "ger": "de", "tur": "tr", "por": "pt", "bul": "bg", "geo": "ka", "ron": "ro", "hun": "hu", "swe": "sv",
    "lit": "lt", "hrv": "hr", "jpn": "ja", "slv": "cs", "pol": "pl", "nor": "no", "lav": "lv", "ukr": "ua",
    "dut": "nl", "chi": "zh", "amh": "am", "hin": "hi", "per": "fa", "ara": "ar", "ind": "id", "arm": "hy",
    "dan": "da", "est": "et", "gre": "el", "tgl": "tl", "aze": "az",
}
SILENT_RMS = 0.003   # about -50 dBFS


def is_silent(path, stream, dur):
    pts = [dur * f for f in (0.1, 0.3, 0.5, 0.7, 0.9)]
    for t in pts:
        a = audio.pcm(path, max(0.0, t - 10), 20, 8000, stream)
        if len(a) and float((a.astype("float64") ** 2).mean() ** 0.5) >= SILENT_RMS:
            return False
    return True


def select_tracks(row, part):
    """[(stream_index, lang)] in output order: src first, then languages alphabetically."""
    path, dur = row["staging"], row["cut_duration"]
    have = langs.cut_langs(path)                      # {index: lang}
    unit = set(langs.unit_translations(part)) | {"heb"}
    keep, dropped, extra = [], [], []
    src = [i for i, l in have.items() if l == "src"]
    heb = [i for i, l in have.items() if l == "heb"]
    first = src[0] if src else heb[0]
    for i, l in have.items():
        if l == "src" or l is None:
            continue
        if l in unit:
            keep.append((i, l))
        elif is_silent(path, "0:%d" % i, dur):
            dropped.append(l)
        else:
            keep.append((i, l))
            extra.append(l)
    # one track per language (first wins)
    seen, ordered = set(), []
    for i, l in sorted(keep, key=lambda x: (x[1], x[0])):
        if l not in seen:
            seen.add(l)
            ordered.append((i, l))
    row["langs_dropped_silent"] = dropped
    row["langs_not_on_unit"] = extra          # audible in the full lesson but no file on the MDB unit
    row["langs_unit_missing"] = sorted(unit - seen - {"src"})
    return first, ordered


def remux(src_path, out, first, ordered, first_tag, first_handler):
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src_path, "-map", "0:v:0", "-map", "0:%d" % first]
    for i, _ in ordered:
        cmd += ["-map", "0:%d" % i]
    cmd += ["-c", "copy", "-map_metadata", "-1",
            "-metadata:s:v:0", "language=fhd", "-metadata:s:v:0", "handler_name=1080p",
            "-metadata:s:a:0", "language=" + first_tag, "-metadata:s:a:0", "handler_name=" + first_handler]
    for k, (_, l) in enumerate(ordered, start=1):
        cmd += ["-metadata:s:a:%d" % k, "language=" + l, "-metadata:s:a:%d" % k, "handler_name=" + l]
    cmd += ["-metadata", "comment=" + config.CUT_MARKER, "-metadata", "network=Bnei Baruch",
            "-movflags", "+faststart", out + ".tmp.mp4"]
    subprocess.run(cmd, check=True)
    os.replace(out + ".tmp.mp4", out)


def _set_comment(path, comment):
    tmp = path + ".c.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path, "-map", "0", "-c", "copy",
                    "-metadata", "comment=" + comment, "-movflags", "+faststart", tmp], check=True)
    os.replace(tmp, path)


def build_o(s_path, o_path, tracks, comment=None):
    """mlt_o audio from the final mlt_s: `tracks` = [(stream_index, lang)], heb first, then A-Z, no src."""
    tracks = sorted(tracks, key=lambda t: (t[1] != "heb", t[1]))
    remux(s_path, o_path, tracks[0][0], tracks[1:], tracks[0][1], tracks[0][1])
    if comment:   # keep the WFDB id of an already registered part
        _set_comment(o_path, comment)


def _add_files(row, o_path, adds, offset):
    """Append MDB translation files [(lang, path)] to o_path (aligned by offset), then re-sort heb-first/A-Z."""
    have = langs.cut_langs(o_path)
    n_audio = len(have)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", o_path]
    for _, path in adds:
        cmd += ["-itsoffset", "%.3f" % offset, "-i", path]
    cmd += ["-map", "0"] + sum([["-map", "%d:a:0" % (k + 1)] for k in range(len(adds))], []) + ["-c", "copy"]
    for k, (lang, _) in enumerate(adds):
        a = n_audio + k
        cmd += ["-c:a:%d" % a, "aac", "-b:a:%d" % a, "128k", "-ac:a:%d" % a, "1", "-ar:a:%d" % a, "48000",
                "-metadata:s:a:%d" % a, "language=%s" % lang, "-metadata:s:a:%d" % a, "handler_name=%s" % lang]
    tmp = o_path + ".add.mp4"
    subprocess.run(cmd + ["-t", "%.3f" % row["cut_duration"], "-movflags", "+faststart", tmp], check=True)
    have = langs.cut_langs(tmp)
    build_o(tmp, o_path, [(i, l) for i, l in have.items()])   # re-sort (video copied by remux)
    os.remove(tmp)


def o_audio(row, part, o_path):
    """Build mlt_o's audio per the track classification (see tracks.py). Returns MDB 2-letter languages."""
    from . import tracks
    if row.get("original_language") is None:
        props = mdb._get("/content_units/%d/" % row["part_id"]).get("properties") or {}
        row["original_language"] = props.get("original_language") or ""
    have = langs.cut_langs(row["staging"])                 # final mlt_s: 1 = src, 2.. = languages
    ordered = [(i, l) for i, l in sorted(have.items()) if i >= 2]
    dec = tracks.classify(row, part, row["original_language"])
    keep = [(i, l) for i, l in ordered if dec.get(l, ("keep", ""))[0] == "keep"]
    build_o(row["staging"], o_path, keep)
    repl = [l for l, (a, _) in dec.items() if a == "replace"]
    if repl:
        trans = langs.unit_translations(part)
        _add_files(row, o_path, [(l, langs.fetch(trans[l])) for l in repl], row.get("track_offset") or 0.0)
    if row.get("sid"):
        _set_comment(o_path, row["sid"])
    final = [l for _, l in sorted(langs.cut_langs(o_path).items())]
    return [MDB_LANG.get(l, l) for l in final]


def rebuild_o_audio(row):
    """Keep mlt_o's (final) video, rebuild only its audio per the track classification."""
    part = {"files": mdb._get("/content_units/%d/files/" % row["part_id"]) or []}
    o = row["o_staging"]
    tmp = o + ".audio.mp4"
    langs_mdb = o_audio(row, part, tmp)
    out = o + ".new.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", o, "-i", tmp, "-map", "0:v", "-map", "1:a",
                    "-c", "copy", "-map_metadata", "0", "-movflags", "+faststart", out], check=True)
    os.replace(out, o)
    os.remove(tmp)
    row.update(o_size=os.path.getsize(o), o_sha1=cut.sha1(o), o_duration=round(audio.duration(o), 3),
               languages=langs_mdb)


def rebuild_o(row):
    """Rebuild mlt_o from the staged mlt_s: track classification + video from the original."""
    part = {"files": mdb._get("/content_units/%d/files/" % row["part_id"]) or []}
    langs_mdb = o_audio(row, part, row["o_staging"])
    from . import original
    original.rebuild_o_video(row)
    row.update(o_size=os.path.getsize(row["o_staging"]), o_sha1=cut.sha1(row["o_staging"]),
               o_duration=round(audio.duration(row["o_staging"]), 3), languages=langs_mdb, o_layout="heb-first")


def build(row, part):
    """Rewrite row['staging'] as the final mlt_s file and derive the mlt_o file next to kmedia/."""
    y, m, d = mdb.real_date(row).split("-")
    first, ordered = select_tracks(row, part)
    s_path = row["staging"]
    remux(s_path, s_path, first, ordered, "src", "Source")      # in place: reorder/retag only
    o_name = "mlt_o_" + row["out_name"][len("mlt_s_"):] if row["out_name"].startswith("mlt_s_") else row["out_name"] + "_o"
    o_path = os.path.join(config.STAGING, "kmedia", y, m, d, o_name + ".mp4")
    os.makedirs(os.path.dirname(o_path), exist_ok=True)
    row["o_staging"] = o_path
    langs_mdb = o_audio(row, part, o_path)
    from . import original
    original.rebuild_o_video(row)
    row.update(
        cut_duration=round(audio.duration(s_path), 3), size=os.path.getsize(s_path), sha1=cut.sha1(s_path),
        o_name=o_name, o_staging=o_path, o_size=os.path.getsize(o_path), o_sha1=cut.sha1(o_path),
        o_duration=round(audio.duration(o_path), 3), languages=langs_mdb, outputs_built=True, o_layout="heb-first")
    log.info("  %s: built mlt_s + %s  langs=%s dropped_silent=%s not_on_unit=%s",
             row["part_uid"], o_name, langs_mdb, row["langs_dropped_silent"], row["langs_not_on_unit"])
