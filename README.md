# partcut

High-quality HLS files for kabbalahmedia **lesson parts** (MDB `LESSON_PART` units), cut from the converted full
lessons and published to the archive. Runs on the archive's conversion station; scope 2008–2022.

For every part it produces two files:

| File | Where | Content |
|---|---|---|
| `mlt_s_<part>.mp4` | `/backup/files/sources/Y/M/D` (local, not published) | stream-copy cut of the full lesson: interlaced 1080, all audio (src first) |
| `mlt_o_<part>.mp4` | `/backup/files/kmedia/Y/M/D` + stp.kab.sh, published as HLS | video re-encoded from the original mpg (yadif, lanczos 1080p + 360p on the GPU, h264_qsv, `fHD`/`nHD`), Hebrew first, no src |

## Workflow

```
./parts.sh run --year 2010 --month 8 [--convert]   # step 1: match, cut, verify, languages, build - publishes nothing
#   -> reports/<run>.summary.txt (verified / flagged / review / no_source), lesson_map_<run>.json/.csv
./parts.sh register --dates ...           # dry run
./parts.sh register --dates ... --execute # step 2: MDB insert+convert, WFDB, stp upload, publish, HLS check
./parts.sh fix-order --year 2010 --month 8           # dry run: daily-collection order, numbers, dates, merges
./parts.sh fix-order --year 2010 --month 8 --execute # write to MDB
```

Fixes for published parts: `rebuild-o --uids … --force [--video-only|--audio-only]`, then
`register --uids … --replace-o --execute`; a part re-cut with corrected languages: mark `recut` in the state,
`cut --dates …`, then `register --uids … --replace-both --execute`.

### Step 1 per date
1. MDB `LESSON_PART` units of the date (+ collections, positions).
2. Converted full lessons in `sources/Y/M/D`; parts not found there are searched in the full lessons and
   unconverted ShiurBoker/Special originals of **±3 days** (Shabbat/holiday parts are often dated another day on
   kabbalahmedia; a match on another day needs both ends ≥ 0.8 and no overlap). Unconverted originals are
   converted with the unchanged `mpg.sh`/`mp4.sh` (`--convert`). Rawmaterial/LT is searched report-only.
3. Audio matching (no transcription): 200 Hz envelope → waveform confirmation at both ends; speed-shifted MDB
   files (≈0.3 % slow) handled by a line fit; up to 10 s of pre-roll may lie outside the recording.
4. Cut mlt_s (stream copy). Known label error: until 2009-11-29 the mpg channel tagged `ger` is Turkish → relabelled.
5. Verify: Hebrew at both ends, 15 s edges, duration. Failing parts go to `review`.
6. Languages: translations in MDB but missing from the cut are added (shared offset, picture alignment of the
   language's video file, or speed correction), checked after mixing (±1 s). Tracks that are only the original
   language, or partial translations not from the start under 10 %, are dropped from mlt_o.
7. mlt_o video from the original: frame-exact start (calibration probes), final sync check (±1 frame).
8. State (`cache/state.json`, per part, only changed rows are written), report, summary, lesson map
   (recording order of the parts, split/unordered/bad_order collections). Stops a run if > 50 % of a date fails.

### fix-order (MDB daily collections)
- Parts in recording order, `name == str(position)` from 0 (current MDB convention), full lesson/excerpts after.
- One lesson spread over several collections of a date → merged into the collection of its first part; emptied
  collections are deleted (only after reading them back empty).
- Date-shifted parts get the recording's date as `film_date`; a collection whose parts all come from one real
  date gets it too.
- Collection `number` = lesson of the day 1, 2, … in the order the lessons happened (missing set,
  contradicting ones shown as RENUMBER). Unclear cases are listed, never guessed.

## Layout
- `partcut/` – the package (`cli.py` entry point; `amatch` matching, `cut`, `langs`, `tracks`, `outputs`,
  `original` video from the original, `publish` MDB/WFDB/stp, `order` collections, `lessons` lesson map,
  `fullsrc` full-lesson mlt_s with all languages, `sources` originals).
- `parts.sh` – launcher (`/opt/filer/parts.sh` links here); sets OpenBLAS single-threaded (its thread pool made
  small numpy ops ~2000× slower and could hang worker threads on this host).
- `serve_staging.py` – local range HTTP server (127.0.0.1:8090) to preview staged files through an SSH tunnel.

Not in git: `cache/` (state, envelopes, downloads), `staging/`, `reports/`, `venv/`, `local.json` (internal
endpoints and hosts; copy `local.json.example` and fill it in). The MDB token is read from `/opt/filer/.mdb_token`
(or `MDB_TOKEN`) and never stored in the code.

## Setup
```
python3.9 -m venv venv && venv/bin/pip install -r requirements.txt
```
Needs ffmpeg with QSV (Intel Arc), ssh access to stp.kab.sh, the MDB token file.
