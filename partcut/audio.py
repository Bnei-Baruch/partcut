"""ffmpeg/ffprobe helpers, PCM extraction and normalized cross-correlation."""
import json
import subprocess

import numpy as np
from scipy.signal import fftconvolve


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:format_tags=comment:stream=index,codec_type:stream_tags=language",
         "-of", "json", path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def duration(path):
    return float(probe(path)["format"]["duration"])


def heb_stream(path):
    """ffmpeg map spec for the Hebrew audio track (by language tag, not index)."""
    audio = [s for s in probe(path)["streams"] if s["codec_type"] == "audio"]
    for s in audio:
        if (s.get("tags") or {}).get("language") == "heb":
            return "0:%d" % s["index"]
    return "0:a:0"


def pcm(path, start=0.0, length=None, sr=16000, stream=None):
    """Decode a mono float32 PCM window from `path`."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start > 0:
        cmd += ["-ss", "%.3f" % start]
    cmd += ["-i", path]
    if length:
        cmd += ["-t", "%.3f" % length]
    cmd += ["-map", stream or "0:a:0", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 and not r.stdout:
        # e.g. a window past the end of a shorter (partial) translation file: no audio there, not a crash
        if start > 0 and (b"Invalid data" in r.stderr or b"Header missing" in r.stderr):
            return np.zeros(0, dtype=np.float32)
        raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
    return np.frombuffer(r.stdout, dtype=np.float32)


def xcorr(needle, hay):
    """Best position of `needle` inside `hay` -> (lag_samples, normalized_peak)."""
    n = len(needle)
    if n == 0 or len(hay) < n:
        return None, 0.0
    needle = needle - needle.mean()
    hay = hay.astype(np.float64)
    num = fftconvolve(hay, needle[::-1], mode="valid")
    csum = np.concatenate([[0.0], np.cumsum(hay)])
    csum2 = np.concatenate([[0.0], np.cumsum(hay * hay)])
    wsum = csum[n:] - csum[:-n]
    wvar = (csum2[n:] - csum2[:-n]) - wsum * wsum / n
    den = np.sqrt(np.maximum(wvar, 1e-12)) * np.linalg.norm(needle)
    score = num / den
    i = int(np.argmax(score))
    return i, float(score[i])


def envelope(x, sr, out_sr=200):
    """Log-energy envelope; robust to codec/phase differences between encodes."""
    hop = sr // out_sr
    m = len(x) // hop
    e = (x[: m * hop].reshape(m, hop) ** 2).mean(axis=1)
    return np.log(e + 1e-8).astype(np.float32)


def locate(needle_path, needle_start, full_path, full_start, length, window, needle_stream=None, full_stream=None):
    """Find where needle[needle_start:+length] sits within full[full_start:+window].

    Returns (time_in_full, peak, method) or (None, peak, method).
    Tries the 8 kHz waveform first, then the energy envelope.
    """
    sr = 8000
    full_start = max(0.0, full_start)
    a = pcm(needle_path, needle_start, length, sr, needle_stream)
    b = pcm(full_path, full_start, window, sr, full_stream)
    lag, peak = xcorr(a, b)
    if lag is not None and peak >= 0.5:
        return full_start + lag / sr, peak, "wave"
    ea, eb = envelope(a, sr), envelope(b, sr)
    lag2, peak2 = xcorr(ea, eb)
    if lag2 is None:
        return None, max(peak, peak2), "none"
    return full_start + lag2 / 200.0, peak2, "env"
