"""Deterministic "this sentence is not finished yet" rules for the end of turn (~0 ms, no model).

The text turn model (turn.py) can rate a cut German sentence as finished: "Sie dürfen das Metformin ab
morgen" [pause] "nicht mehr nehmen." In German the infinitive / participle and the negation come last,
so ending the turn at that pause reverses the meaning. These rules only ever make the end of turn wait
longer (``max_s``); they never end a turn.

``unfinished(text, lang)`` -> a short reason, or None:

* ``verb-final`` (de): the last clause has a finite modal or auxiliary verb (dürfen, müssen, können,
  sollen, wollen, mögen/möchten, haben, sein, werden) and does not end in a verb form: an infinitive
  (a common verb, also with a separable / inseparable prefix, "-ieren", "zu" + infinitive), a participle
  ("ge...t/en", "-iert", "be-/ver-/er-/ent-..."), the finite verb itself (a subordinate clause:
  "..., dass Sie das nehmen dürfen") or a separable particle ("Ist das Fenster zu?").
* ``neg-scope`` (de): the last clause ends in a time adverb where a negation commonly follows
  ("Nehmen Sie das ab morgen" [pause] "nicht mehr").
* ``aux-final`` (en): ends in a modal / auxiliary / "not" / "to" ("You may" [pause] "not take it").

A false "unfinished" costs latency (the turn waits ``max_s``); a missed one falls back to the model.
So the verb list errs on the side of "not a verb": an unknown word (e.g. ASR garbage "abnorgen") is not
taken as the closing verb.
"""

from __future__ import annotations

import re


def _set(words: str) -> frozenset[str]:
    return frozenset(words.split())


# ---------------------------------------------------------------- German

_MODAL_DE = _set("""
darf darfst dürfen dürft durfte durftest durften durftet dürfte dürftest dürften dürftet
muss muß musst mußt müssen müsst müßt musste mußte musstest mussten mußten musstet müsste müßte müsstest
müssten müßten müsstet
kann kannst können könnt konnte konntest konnten konntet könnte könntest könnten könntet
soll sollst sollen sollt sollte solltest sollten solltet
will willst wollen wollt wollte wolltest wollten wolltet
mag magst mögen mögt mochte mochtest mochten mochtet möchte möchtest möchten möchtet
""")
_AUX_DE = _set("""
habe hab hast hat haben habt hatte hattest hatten hattet hätte hättest hätten hättet
bin bist ist sind seid war warst waren wart wäre wärst wären wärt sei seist seien
werde wirst wird werden werdet wurde wurdest wurden wurdet würde würdest würden würdet
""")
_FINITE_DE = _MODAL_DE | _AUX_DE

# bare infinitives (prefixes are stripped before the lookup: "einnehmen" -> "nehmen", "absetzen" -> "setzen")
_VERBS_DE = _set("""
sein haben werden tun können müssen dürfen sollen wollen mögen möchten lassen
nehmen geben kommen gehen sehen stehen liegen sitzen bleiben fahren laufen fallen halten hängen
machen sagen sprechen reden fragen antworten erzählen zeigen bringen holen tragen schicken senden
essen trinken schlafen atmen husten niesen schlucken kauen spucken bluten schwitzen frieren zittern
leben sterben wachsen heilen helfen pflegen waschen duschen baden putzen spülen cremen schmieren
setzen stellen legen hängen ziehen drücken heben schieben werfen fangen tasten fühlen spüren merken
denken glauben wissen kennen meinen hoffen fürchten warten suchen finden verlieren lernen lesen
schreiben rechnen zählen messen wiegen prüfen testen impfen spritzen stechen schneiden nähen
kaufen zahlen bezahlen kosten leihen mieten wohnen ziehen arbeiten feiern spielen singen tanzen
rufen anrufen telefonieren melden schließen öffnen packen füllen leeren teilen mischen rühren kochen
braten backen schmecken riechen hören klingen tönen brennen jucken schmerzen pochen stechen drehen
wenden biegen brechen reißen beißen kratzen reiben streichen tropfen gießen fließen laufen rennen
springen steigen klettern schwimmen fliegen reisen landen starten stoppen enden beginnen anfangen
hören aufhören warten dauern brauchen benutzen nutzen gebrauchen üben treffen grüßen danken bitten
beten weinen lachen lächeln schreien flüstern rauchen kiffen saufen naschen hungern fasten
kriegen bekommen erhalten unterschreiben zeichnen malen drucken kopieren scannen speichern löschen
tippen klicken senden mailen zahlen sparen leisten liefern bestellen buchen planen ordnen regeln
klagen verklagen fordern zahlen schulden haften erben kündigen mahnen pfänden prozessieren
handeln verhandeln bieten liefern einigen streiten zanken wählen stimmen gewinnen verlieren
führen leiten folgen fehlen passen stimmen gelten zählen reichen klappen gelingen scheitern
schaffen kümmern sorgen lohnen trauen wagen schämen freuen ärgern wundern beeilen erholen
ruhen rasten schonen bewegen strecken beugen dehnen sitzen knien hocken stützen tragen
schalten schauen gucken blicken starren sehen lesen kontrollieren
stören nerven wehtun
klären erklären melden ändern wechseln tauschen sammeln wandern fordern äußern erinnern
hindern verhindern wundern bessern verbessern verschlechtern steigern mindern lindern sichern
handeln wechseln zweifeln lächeln sammeln schütteln wackeln klingeln regeln würfeln kitzeln
""")

_SEP_PREFIXES = ("zusammen", "zurück", "weiter", "wieder", "vorbei", "heraus", "herein", "hinaus", "hinein",
                 "dazu", "hinzu", "fest", "fort", "frei", "heim", "hoch", "nach", "ab", "an", "auf", "aus", "bei",
                 "ein", "mit", "vor", "weg", "her", "hin", "los", "um", "durch", "zu", "statt", "teil", "weh")
_INSEP_PREFIXES = ("hinter", "wider", "über", "unter", "miss", "emp", "ent", "zer", "ver", "be", "er", "ge")
# separable particles / pronominal adverbs that close a clause ("Ist das Fenster zu?", "Ich bin dagegen.")
_CLOSERS_DE = _set("""
ab an auf aus zu zurück weg los vorbei fertig dabei dafür dagegen damit darauf daraus davon dazu daran
darüber hin her mit ein
""")
_NOT_VERB_DE = _set("""
gegen morgen übermorgen gestern vorgestern oben unten innen außen drinnen draußen hinten vorne neben
zwischen wegen entgegen eben sieben ihnen denen einen keinen meinen deinen seinen ihren unseren euren
diesen jenen jeden allen vielen wenigen anderen beiden manchen solchen welchen modern sondern genauen
""")
# time adverbs after which a negation commonly follows ("ab morgen" -> "nicht mehr")
_TIME_DE = _set("""
morgen übermorgen heute jetzt sofort vorerst weiterhin zunächst erstmal vorläufig künftig zukünftig
demnächst morgens abends mittags nachts
""")

_RX_PART_GE = re.compile(r"(?:" + "|".join(_SEP_PREFIXES) + r")?ge[a-zäöüß]{2,}(?:t|en)")
_RX_PART_INSEP = re.compile(r"(?:be|ver|er|ent|zer|emp|miss)[a-zäöüß]{3,}(?:t|en)")
_RX_IEREN = re.compile(r"[a-zäöüß]{3,}ier(?:en|t)")

_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
_CLAUSE_SPLIT = re.compile(r"[.!?;:…]+(?=\s|$)|,\s|\s[-–—]\s")


def _infinitive_de(w: str) -> bool:
    if w in _VERBS_DE:
        return True
    for p in _SEP_PREFIXES:  # "einnehmen", "absetzen", "einzunehmen", "aufhören"
        if w.startswith(p) and len(w) > len(p) + 2:
            rest = w[len(p):]
            if rest.startswith("zu") and rest[2:] in _VERBS_DE:
                return True
            if rest in _VERBS_DE or any(rest.startswith(q) and rest[len(q):] in _VERBS_DE for q in _INSEP_PREFIXES):
                return True
    return any(w.startswith(q) and w[len(q):] in _VERBS_DE for q in _INSEP_PREFIXES)


def _verb_form_de(w: str) -> bool:
    """Can ``w`` (lowercase, as written) close a German clause as its verb?"""
    if w in _NOT_VERB_DE:
        return False
    if w in _FINITE_DE or w in _CLOSERS_DE or _infinitive_de(w):
        return True
    if w in ("getan", "gewusst", "gekonnt", "gemusst", "gedurft", "gesollt", "gewollt", "gemocht"):
        return True
    return bool(_RX_PART_GE.fullmatch(w) or _RX_PART_INSEP.fullmatch(w) or _RX_IEREN.fullmatch(w))


_SUBORD_DE = _set("""
dass daß weil obwohl obgleich ob wenn falls sofern nachdem bevor ehe damit sodass seitdem während als bis
seit da indem sobald solange
""")
# only after a comma: relative pronouns and indirect questions ("..., die Sie nehmen müssen")
_REL_DE = _set("""
der die das dem den deren dessen denen welche welcher welches welchem welchen wie was wo wohin woher
warum wieso weshalb wann wer wen wem womit wofür worauf
""")

def _last_clause(text: str) -> tuple[list[str], bool]:
    """Words of the last clause, and whether a comma (not a sentence end) came right before it."""
    pieces = _CLAUSE_SPLIT.split(text)
    seps = _CLAUSE_SPLIT.findall(text)
    for i in range(len(pieces) - 1, -1, -1):
        words = _WORD.findall(pieces[i])
        if words:
            return words, i > 0 and seps[i - 1].startswith(",")
    return [], False


def _unfinished_de(text: str) -> str | None:
    """See the module doc. Returns ``verb-final``, ``aux-open`` (only haben / sein / werden, no modal) or
    ``neg-scope``."""
    words, after_comma = _last_clause(text)
    if not words:
        return None
    lower = [w.lower() for w in words]
    last_raw, last = words[-1], lower[-1]
    if len(words) >= 2 and (last in _TIME_DE or (len(words) >= 3 and lower[-2] == "ab")):
        return "neg-scope"  # "ab morgen", "ab Montag" [pause] "nicht mehr"
    finite = [w for w in lower if w in _FINITE_DE]
    if not finite or len(words) == 1:
        return None
    subordinate = lower[0] in _SUBORD_DE or (after_comma and lower[0] in _REL_DE)
    if last in _FINITE_DE:
        # verb-final subordinate clause ("..., dass Sie das nehmen dürfen") is finished; a main clause that
        # stops right after its finite verb ("Sie dürfen" [pause] "das nicht") is not
        return None if subordinate else ("verb-final" if any(w in _MODAL_DE for w in finite) else "aux-open")
    # a capitalised word that is not the first is a noun in German, never the closing verb
    if not last_raw[:1].isupper() and _verb_form_de(last):
        return None
    if subordinate:
        return None  # its own finite verb would be last; leave it to the model
    return "verb-final" if any(w in _MODAL_DE for w in finite) else "aux-open"


# ---------------------------------------------------------------- English

_AUX_EN = _set("""
can could may might must shall should will would do does did is are was were am be been being have has
had not to cannot can't won't don't doesn't didn't isn't aren't wasn't weren't shouldn't mustn't
""")


def _unfinished_en(text: str) -> str | None:
    words, _ = _last_clause(text)
    if len(words) < 2:
        return None
    last = words[-1].lower().replace("’", "'")
    if last in _AUX_EN:
        if last in ("is", "are", "was", "were", "am", "do", "does", "did", "have", "has", "had", "can", "will",
                    "would", "should", "could", "may", "might", "must", "shall") and text.rstrip().endswith("?"):
            return None  # "Where is it?" ... "Who does?" — a question ending in the auxiliary is finished
        return "aux-final"
    return None


_RULES = {"de": _unfinished_de, "en": _unfinished_en}


def unfinished(text: str, lang: str | None) -> str | None:
    """A reason if a deterministic rule says the utterance cannot have ended here; None = no opinion."""
    rule = _RULES.get((lang or "").split("-")[0].lower())
    text = (text or "").strip()
    return rule(text) if rule and text else None
