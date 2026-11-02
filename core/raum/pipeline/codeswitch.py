"""A phrase in another script that the final transcript dropped, put back from the partials.

Box (mix2-ru.wav: Russian, then "but I think it is not working" in an English voice, then Russian again): the
partials had the English phrase ("…каждый день. But I think it is not working."), but once the Russian went on
whisper dropped it from every later pass, the final included (language ru, with or without the glossary prompt).
The speaker said it, so the listener must hear it.

For a speaker of a language in a non-Latin script, a run of Latin words (two or more) in one of the
utterance's partials that the final lacks goes back in at the same place: after the words that came before it
in that partial, when the final has them. Nothing is guessed: without that anchor the final stays as it is.
"""

from __future__ import annotations

import re

NON_LATIN = frozenset({"ru", "uk", "bg", "sr", "mk", "be", "kk", "ar", "fa", "ur", "he", "el", "hi", "th", "ka", "hy",
                       "zh", "ja", "ko"})
_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
_LATIN = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
_SEP = re.compile(r"[\s,;:]*")  # what may stand between the words of one run
ANCHOR_WORDS = 2  # words before the run that must be in the final, in this order
MIN_LETTERS = 6


def _norm(w: str) -> str:
    return w.casefold().replace("’", "'")


def latin_runs(text: str) -> list[tuple[int, int]]:
    """(start, end) of each run of 2+ Latin words with only spaces / commas between them."""
    runs: list[tuple[int, int]] = []
    words = list(_WORD.finditer(text))
    i = 0
    while i < len(words):
        if not _LATIN.fullmatch(words[i].group()):
            i += 1
            continue
        j = i
        while j + 1 < len(words) and _LATIN.fullmatch(words[j + 1].group()) and \
                _SEP.fullmatch(text[words[j].end():words[j + 1].start()]):
            j += 1
        if j > i and sum(len(w.group()) for w in words[i:j + 1]) >= MIN_LETTERS:
            runs.append((words[i].start(), words[j].end()))
        i = j + 1
    return runs


def _has(final_words: list[str], run_words: list[str]) -> bool:
    """Is (a two-word piece of) the run in the final already?"""
    pairs = {tuple(final_words[k:k + 2]) for k in range(len(final_words) - 1)}
    return any(tuple(run_words[k:k + 2]) in pairs for k in range(len(run_words) - 1))


def restore(final: str, partials: list[str], lang: str | None) -> str:
    """``final`` with the Latin phrases the partials had and it dropped (see module doc)."""
    code = (lang or "").split("-")[0].lower()
    if code not in NON_LATIN or not final.strip() or not partials:
        return final
    out = final
    done: set[tuple[str, ...]] = set()
    for p in reversed(partials):  # the newest partial that had the phrase: its longest version
        for a, b in latin_runs(p):
            run = p[a:b]
            run_words = [_norm(w) for w in _WORD.findall(run)]
            out_words = [_norm(m.group()) for m in _WORD.finditer(out)]
            if _has(out_words, run_words):
                continue
            before = [m for m in _WORD.finditer(p[:a])][-ANCHOR_WORDS:]
            if len(before) < ANCHOR_WORDS or any(_LATIN.fullmatch(m.group()) for m in before):
                continue
            anchor = tuple(_norm(m.group()) for m in before)
            if anchor in done:
                continue  # an older partial's shorter version of the same phrase
            found = [m for m in _WORD.finditer(out)]
            pos = None
            for k in range(len(found) - ANCHOR_WORDS + 1):
                if tuple(_norm(m.group()) for m in found[k:k + ANCHOR_WORDS]) == anchor:
                    pos = found[k + ANCHOR_WORDS - 1].end()
            if pos is None:
                continue
            phrase = run
            if phrase[:1].isupper() and not re.match(r"I\b", phrase) and phrase[1:2].islower():
                phrase = phrase[:1].lower() + phrase[1:]  # it goes mid-sentence
            out = f"{out[:pos]}, {phrase}{out[pos:]}"
            done.add(anchor)
    return out
