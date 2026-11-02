/**
 * Live draft translation text (core/raum/pipeline/draft.py, event `translation.draft`): while the speaker talks,
 * the stable part of what they said, translated, shown in place and never voiced. The newest `seq` wins; the
 * final translation (or, in simultaneous mode, the segment that covers it) takes its place.
 */
import type { EventData } from "./events";
import type { Turn } from "./store";

export interface Draft {
  text: string;
  seq: number;
  seg: number | null; // simultaneous mode: the tail after committed segments 0 .. seg-1
}

/** Reducer step: the draft replaces the previous one of its language, never an older `seq` a newer one. */
export function applyDraft(
  drafts: Record<string, Draft> | undefined,
  d: EventData["translation.draft"],
): Record<string, Draft> | undefined {
  const cur = drafts?.[d.target_lang];
  if (cur && cur.seq >= d.seq) return drafts;
  return { ...(drafts ?? {}), [d.target_lang]: { text: d.text, seq: d.seq, seg: d.seg ?? null } };
}

/** What a device shows of `t` in `lang`: the translation so far (`text`) and the live draft after it (`draft`).
 * Consecutive: the draft until the streaming final says at least as much, so the text never shrinks. Simultaneous:
 * the committed segments, then the draft of the tail once every segment before it has started; a segment at or
 * after the draft's place covers it (the next draft follows that segment). */
export function shownTranslation(t: Turn, lang: string): { text: string; draft: string } {
  const tr = t.translations[lang];
  const text = tr?.text ?? "";
  const d = t.drafts?.[lang];
  if (!d?.text.trim() || tr?.final || t.continuedIn || t.uncertain || t.ignored) return { text, draft: "" };
  const sg = t.segs?.[lang];
  if (sg || d.seg != null) {
    if (d.seg == null) return { text, draft: "" }; // from before the first commit: the segments say it now
    const parts = Object.keys(sg?.parts ?? {}).map(Number);
    if (parts.some((k) => k >= d.seg!)) return { text, draft: "" };
    if (parts.length < d.seg) return { text, draft: "" }; // a segment before it has not started: no gap, no jump
    return { text, draft: d.text };
  }
  if (text.trim().length >= d.text.trim().length) return { text, draft: "" };
  return { text: "", draft: d.text };
}
