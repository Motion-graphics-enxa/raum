"""Deterministic "this sentence is not finished yet" rules for the end of turn (~0 ms, no model).

The text turn model (turn.py) can rate a cut German sentence as finished: "Sie dürfen das Metformin ab
morgen" [pause] "nicht mehr nehmen." In German the infinitive / participle and the negation come last,
so ending the turn at that pause reverses the meaning. These rules only ever make the end of turn wait
longer (``max_s``); they never end a turn.

``unfinished(text, lang)`` -> a short reason, or None:

* ``verb-final`` (de): the last clause has a finite modal verb (dürfen, müssen, können, sollen, wollen,
  mögen/möchten) and does not end in a verb form: an infinitive
  (a common verb, also with a separable / inseparable prefix, "-ieren", "zu" + infinitive), a participle
  ("ge...t/en", "-iert", "be-/ver-/er-/ent-..."), the finite verb itself (a subordinate clause:
  "..., dass Sie das nehmen dürfen") or a separable particle ("Ist das Fenster zu?").
* ``aux-open`` (de): the same with only haben / sein / werden, when nothing but function words follow the
  auxiliary ("Sie haben das", "Das ist") or the clause ends in "nicht"; with a predicate complement
  ("Ich bin allergisch gegen Penicillin.", "Haben Sie Schmerzen?") no opinion: the model decides.
* ``neg-scope`` (de): the last clause ends in a time adverb where a negation commonly follows
  ("Nehmen Sie das ab morgen" [pause] "nicht mehr").
* ``aux-final`` (en): ends in a modal / auxiliary / "not" / "to" ("You may" [pause] "not take it").
* ``verb-final`` (ru): ends in a modal / predicative without its infinitive ("Вам нельзя" [pause] "...");
  ``neg-scope`` (ru): such a word, no infinitive after it, and a time phrase at the end ("Вам можно с
  завтрашнего дня" [pause] "не принимать"); ``neg-open`` (ru): ends in "не" / "ни".
* ``conj-open`` (de, en, ru): ends in a conjunction, a preposition or (en) an article ("und", "gegen",
  "of the", "because", "потому что", "на"). Not when the text is a question ("Where are you from?").
* ``number-open`` (de, en, ru), the only SOFT reason: ends in a number without a unit ("принимайте по
  500"). It never makes a turn wait longer; it only keeps the fast path (turn.py ``fast-complete``) off.

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

_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")  # numbers are words too ("Ich bin 45.")
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

_NEG_DE = _set("nicht nie niemals keineswegs")
# words after haben / sein / werden that are not a predicate complement
_FUNCTION_DE = _set("""
ich du er sie es wir ihr man mich dich ihn uns euch ihm ihnen sich mir dir
das der die den dem des dies diese dieser dieses diesen diesem ein eine einen einem einer eines
kein keine keinen keinem keiner keines mein meine meinen meinem meiner dein deine deinen deinem deiner
sein seine seinen seinem seiner ihre ihren ihrem ihrer ihres unser unsere unseren unserem euer eure
noch schon auch nur doch ja nein denn mal hier da dort dann sehr so wieder immer nie nicht mehr bitte also
eigentlich vielleicht wohl gerade gern gerne oft manchmal selten etwa ganz gestern vorgestern bereits
was wie wo wer wen wem wann warum
mit von bei für gegen an auf in im am zum zur vom beim aus nach seit über unter vor durch ohne um zu
und oder aber
""")
_POLITE_DE = _set("sie ihnen ihr ihre ihren ihrem ihrer ihres")


def _complement_de(raw: str, w: str) -> bool:
    """A noun (capitalised, not the polite "Sie"), adjective or other content word."""
    if raw[:1].isupper():
        return w not in _POLITE_DE
    return w not in _FUNCTION_DE and w not in _FINITE_DE


def _last_clause(text: str) -> tuple[list[str], bool]:
    """Words of the last clause, and whether a comma (not a sentence end) came right before it."""
    pieces = _CLAUSE_SPLIT.split(text)
    seps = _CLAUSE_SPLIT.findall(text)
    for i in range(len(pieces) - 1, -1, -1):
        words = _WORD.findall(pieces[i])
        if words:
            return words, i > 0 and seps[i - 1].startswith(",")
    return [], False


_OPEN_DE = _set("""
und oder aber sondern denn weil dass daß wenn ob obwohl damit falls sowie als
von für gegen in im am zum zur vom beim bei ohne seit zwischen wegen trotz unter
""")  # not the separable particles an, auf, aus, mit, nach, vor, zu, ab, durch, um ("Rufen Sie mich an.")


def _unfinished_de(text: str) -> str | None:
    """See the module doc. Returns ``conj-open``, ``verb-final``, ``aux-open`` (only haben / sein / werden,
    no modal) or ``neg-scope``."""
    words, after_comma = _last_clause(text)
    if not words:
        return None
    lower = [w.lower() for w in words]
    last_raw, last = words[-1], lower[-1]
    if last in _OPEN_DE and not _question(text):
        return "conj-open"  # "Ich bin allergisch gegen" [pause] "Penicillin"
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
    if any(w in _MODAL_DE for w in finite):
        return "verb-final"
    # only haben / sein / werden: as the full verb with a predicate complement ("Ich bin allergisch gegen
    # Penicillin.", "Haben Sie Schmerzen?") the clause is finished; it is open when only function words
    # follow the auxiliary ("Sie haben das" [pause] "nicht genommen") or it ends in a negation
    if last in _NEG_DE:
        return "aux-open"
    at = next(i for i, w in enumerate(lower) if w in _AUX_DE)
    if any(_complement_de(raw, w) for raw, w in zip(words[at + 1:], lower[at + 1:], strict=True)):
        return None
    return "aux-open"


# ---------------------------------------------------------------- English

_AUX_EN = _set("""
can could may might must shall should will would do does did is are was were am be been being have has
had not to cannot can't won't don't doesn't didn't isn't aren't wasn't weren't shouldn't mustn't
""")


_ARTICLE_CONJ_EN = _set("the a an and or but nor because if unless although whereas my your our their")
_PREP_EN = _set("of for with from into onto than about at per via")  # not in / on / off / up: "Come in."


def _unfinished_en(text: str) -> str | None:
    words, _ = _last_clause(text)
    if not words:
        return None
    last = words[-1].lower().replace("’", "'")
    if last in _ARTICLE_CONJ_EN and (len(words) >= 2 or last not in ("a",)):
        return "conj-open"  # "take 500 of the", "and", "because"
    if last in _PREP_EN and not _question(text):
        return "conj-open"  # but "Where are you from?" is finished
    if len(words) < 2:
        return None
    if last in _AUX_EN:
        if last in ("is", "are", "was", "were", "am", "do", "does", "did", "have", "has", "had", "can", "will",
                    "would", "should", "could", "may", "might", "must", "shall") and text.rstrip().endswith("?"):
            return None  # "Where is it?" ... "Who does?" — a question ending in the auxiliary is finished
        return "aux-final"
    return None


# ---------------------------------------------------------------- Russian

_MODAL_RU = _set("""
можно нельзя надо нужно должен должна должно должны могу можешь может можем можете могут
хочу хочешь хочет хотим хотите хотят следует придётся придется
""")
_OPEN_RU = _set("""
и но а или что чтобы если потому когда как чем так в во на с со по до после к ко у о об обо от из за для
без при под над через про между перед
""")
_QWORDS_RU = _set("что как когда")  # "Простите, что?" is a question
_TIME_RU = _set("""
завтра сегодня теперь сейчас больше дня утра вечера понедельника вторника среды четверга пятницы субботы
воскресенья недели месяца
""")
_INF_RU = re.compile(r"\w+(?:ть|ти|чь|ться|тись)$")


def _unfinished_ru(text: str) -> str | None:
    words, _ = _last_clause(text)
    if not words:
        return None
    lower = [w.lower().replace("ё", "е") for w in words]
    last = lower[-1]
    question = _question(text)
    if last in ("не", "ни"):
        return "neg-open"  # "Вам можно" ... "не" [pause] "принимать"
    if last in _OPEN_RU and not (question and last in _QWORDS_RU):
        return "conj-open"
    if len(words) < 2:
        return None
    modal = [i for i, w in enumerate(lower) if w in _MODAL_RU]
    if not modal:
        return None
    if last in _MODAL_RU:
        return None if question else "verb-final"  # "Вам нельзя" [pause]; but "Мне можно?" is finished
    rest = lower[modal[-1] + 1:]
    if not any(_INF_RU.fullmatch(w) for w in rest) and last in _TIME_RU:
        return "neg-scope"  # "Вам можно с завтрашнего дня" [pause] "не принимать метформин"
    return None


_RULES = {"de": _unfinished_de, "en": _unfinished_en, "ru": _unfinished_ru}
SOFT = frozenset({"number-open"})  # keeps the fast path off; never a longer wait
_TRAILING_NUMBER = re.compile(r"\d(?:[\d.,]*\d)?\s*[.!?…]*$")


def _question(text: str) -> bool:
    return text.rstrip().rstrip("\"'»«”’)").endswith(("?", "？", "؟"))


def unfinished(text: str, lang: str | None) -> str | None:
    """A reason if a deterministic rule says the utterance cannot have ended here; None = no opinion.
    Reasons in ``SOFT`` only keep the fast path off (see the module doc)."""
    code = (lang or "").split("-")[0].lower()
    rule = _RULES.get(code)
    text = (text or "").strip()
    if not rule or not text:
        return None
    why = rule(text)
    if why is None and _TRAILING_NUMBER.search(text):
        return "number-open"  # "принимайте по 500", "take 500"
    return why
