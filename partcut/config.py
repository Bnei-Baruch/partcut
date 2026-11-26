"""Paths, endpoints and thresholds for partcut."""
import os

ROOT = "/opt/filer/parts"
CACHE = os.path.join(ROOT, "cache")
STAGING = os.path.join(ROOT, "staging")
REPORTS = os.path.join(ROOT, "reports")
LOGDIR = "/opt/filer/log"
SOURCES = "/backup/files/sources"
MODEL_DIR = "/opt/filer/models/whisper-he-kabbalah-ct2"

# Internal endpoints/hosts come from local.json next to the package (not in git; see local.json.example).
import json as _json
_LOCAL = _json.load(open(os.path.join(ROOT, "local.json")))
MDB = _LOCAL["MDB"]
WFSRV_MDB = _LOCAL["WFSRV_MDB"]
WFDB = _LOCAL["WFDB"]
WFDB_KV = _LOCAL["WFDB_KV"]          # WFDB key/value service (source/kv, source/<id>)
WF_HOST = _LOCAL["WF_HOST"]          # host of the nightly kmedia cron
CDN = "https://cdn.kabbalahmedia.info"
HLS_BASE = _LOCAL["HLS_BASE"]       # host of the older HLS files
STATION = _LOCAL["STATION"]

# CPU budget: the conversion batch shares this host.
CPU_THREADS = int(os.environ.get("PARTCUT_THREADS", "6"))

# Marker written into every cut file so it is never mistaken for a full lesson.
CUT_MARKER = "partcut"

# Matching / verification thresholds
EDGE_SEC = 90          # seconds of part head/tail transcribed for matching
NGRAM = 3
MIN_VOTES = 4          # minimum n-gram votes for a head/tail location
MIN_CONF = 0.25        # votes_in_peak / ngrams
MAX_HEAD_TAIL_DRIFT = 2.0
XCORR_LEN = 20.0
XCORR_MIN_PEAK = 0.5
XCORR_MAX_DIFF = 3.0
VERIFY_TEXT_SEC = 30.0
VERIFY_MIN_TEXT = 0.8
VERIFY_MAX_OFFSET = 1.2   # stream-copy may start up to one GOP (1 s) early
VERIFY_MAX_DUR_DIFF = 1.5
ORDER_TOLERANCE = 5.0
PIPELINE_VERSION = "2026-11-26.2"   # ger->tur label fix (mpg until 2009-11-29); nHD rendition
ENCODE_WORKERS = int(os.environ.get("PARTCUT_WORKERS", "3"))   # GPU saturates at ~3 encodes (13.5x vs 14.5x at 4)
# No uploads to stp in this daily window (local time of the station, Asia/Jerusalem) - user request 2026-11-26.
UPLOAD_PAUSE = os.environ.get("PARTCUT_UPLOAD_PAUSE", "05:00-08:00")
PREROLL_MAX = 10.0        # s of an MDB part allowed outside the recording (cut what exists)
EDGE_CHECK_SEC = 15.0     # first/last seconds compared by waveform (songs, music)
EDGE_SILENT_RMS = 0.003


def token():
    t = os.environ.get("MDB_TOKEN")
    if t:
        return t.strip()
    with open("/opt/filer/.mdb_token") as f:
        return f.read().strip()
