"""Hebrew text normalization and n-gram offset voting."""
import re
from collections import defaultdict
from difflib import SequenceMatcher

from . import config

_NIQQUD = re.compile(r"[֑-ׇ]")
_NONWORD = re.compile(r"[^א-תa-z0-9 ]+")
_FINALS = str.maketrans("ךםןףץ", "כמנפצ")


def norm(text):
    t = _NIQQUD.sub("", text.lower()).translate(_FINALS)
    return _NONWORD.sub(" ", t).split()


def tokens(words):
    """[(token, time)] with each whisper word split into normalized tokens."""
    out = []
    for w in words:
        for t in norm(w["w"]):
            out.append((t, float(w["s"])))
    return out


def ngram_index(toks, n):
    idx = defaultdict(list)
    for i in range(len(toks) - n + 1):
        idx[" ".join(t for t, _ in toks[i:i + n])].append(toks[i][1])
    return idx


def offset_vote(snippet_words, full_index, n=config.NGRAM, max_occ=8):
    """Vote for delta = t_full - t_snippet.

    Returns dict(delta, votes, ngrams, conf) or None. `full_index` from ngram_index(tokens(full), n).
    """
    st = tokens(snippet_words)
    deltas, total = [], 0
    for i in range(len(st) - n + 1):
        key = " ".join(t for t, _ in st[i:i + n])
        total += 1
        occ = full_index.get(key)
        if not occ or len(occ) > max_occ:   # skip absent and very common phrases
            continue
        deltas += [tf - st[i][1] for tf in occ]
    if not deltas or not total:
        return None
    deltas.sort()
    # densest 2-second window
    best, j = (0, 0, 0), 0
    for i in range(len(deltas)):
        while deltas[i] - deltas[j] > 2.0:
            j += 1
        if i - j + 1 > best[0]:
            best = (i - j + 1, j, i)
    cnt, a, b = best
    cluster = deltas[a:b + 1]
    delta = cluster[len(cluster) // 2]
    return {"delta": round(delta, 3), "votes": cnt, "ngrams": total, "conf": round(cnt / total, 3)}


def text_similarity(words_a, words_b):
    a = [t for t, _ in tokens(words_a)]
    b = [t for t, _ in tokens(words_b)]
    if not a or not b:
        return 0.0
    # character-level on joined text: robust to whisper glueing/splitting words
    return round(SequenceMatcher(None, "".join(a), "".join(b), autojunk=False).ratio(), 3)


def good(v):
    return v is not None and v["votes"] >= config.MIN_VOTES and v["conf"] >= config.MIN_CONF
