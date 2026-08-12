"""Cheap, dependency-free keyword-overlap matching - no OpenAI calls. Used for
on-the-spot notification triggers (a new job goes live -> notify candidates
whose profile overlaps it; a candidate goes open-to-work -> notify employers
whose open jobs overlap them) where firing a real AI ranking synchronously
inside the request would be too slow/costly for something this frequent."""
import re

_WORD_RE = re.compile(r'\b\w{3,}\b')

NOTIFY_SCORE_THRESHOLD = 30  # keyword-overlap % below this isn't worth a notification
NOTIFY_MAX_RECIPIENTS = 15   # cap per trigger so a broad/generic posting doesn't mass-notify


def keyword_set(*parts):
    text = ' '.join(p for p in parts if p).lower()
    return set(_WORD_RE.findall(text))


def overlap_score(words_a, words_b):
    if not words_a or not words_b:
        return 0
    return round(len(words_a & words_b) / len(words_a) * 100)
