"""faster-whisper transcription with word timestamps (CPU, int8)."""
import hashlib
import json
import logging
import os
import time

from . import audio, config

log = logging.getLogger("partcut.stt")
_model = None


def model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(config.MODEL_DIR, device="cpu", compute_type="int8",
                              cpu_threads=config.CPU_THREADS)
    return _model


def transcribe_pcm(samples, offset=0.0):
    """Words [{w,s,e}] for a 16 kHz mono float32 array; times shifted by `offset`."""
    if len(samples) < 16000:
        return []
    segs, _ = model().transcribe(
        samples, language="he", beam_size=1, condition_on_previous_text=False,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 500}, word_timestamps=True)
    words = []
    for seg in segs:
        for w in seg.words or []:
            words.append({"w": w.word.strip(), "s": round(w.start + offset, 3), "e": round(w.end + offset, 3)})
    return words


def transcribe_window(path, start, length, stream=None):
    return transcribe_pcm(audio.pcm(path, start, length, 16000, stream), offset=start)


def full_transcript(path):
    """Whole-lesson transcript of the Hebrew track, cached on disk."""
    st = os.stat(path)
    key = hashlib.sha1(("%s|%d|%d" % (path, st.st_size, int(st.st_mtime))).encode()).hexdigest()[:12]
    cpath = os.path.join(config.CACHE, "transcripts", "%s.%s.json" % (os.path.basename(path), key))
    if os.path.exists(cpath):
        with open(cpath) as f:
            return json.load(f)
    os.makedirs(os.path.dirname(cpath), exist_ok=True)
    t0 = time.time()
    dur = audio.duration(path)
    stream = audio.heb_stream(path)
    log.info("transcribing full lesson %s (%.0f s, stream %s)", os.path.basename(path), dur, stream)
    # Decode in 30-min chunks to bound memory; VAD inside each chunk.
    words, chunk = [], 1800.0
    pos = 0.0
    while pos < dur:
        words += transcribe_window(path, pos, chunk, stream)
        pos += chunk
    log.info("transcribed %s: %d words in %.0f s (RTF %.2f)", os.path.basename(path), len(words),
             time.time() - t0, (time.time() - t0) / max(dur, 1))
    with open(cpath + ".tmp", "w") as f:
        json.dump(words, f, ensure_ascii=False)
    os.replace(cpath + ".tmp", cpath)
    return words
