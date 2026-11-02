/** A live draft translation (translation.draft) inside the text it continues: quieter, replaced in place, and
 * gone once the final translation is there. Never a live region: the final text is what a screen reader gets;
 * reading the turn, it hears "Draft:" first. Styling hooks only (`translation--draft`, `data-draft`): the theme
 * styles them. */
import { t } from "../lib/i18n";
import "./draft.css";

export function DraftText({ text, ui, lead = false, mark = false }: {
  text: string;
  ui: string; // the device's interface language, for the hidden label
  lead?: boolean; // translated text before it: a space first
  mark?: boolean; // no live caret beside it: a quiet "…" says it is still coming
}) {
  return (
    <span className="translation--draft" data-draft="" aria-live="off">
      {lead ? " " : null}
      <span className="visually-hidden">{t(ui, "talk.draft")}: </span>
      {text}
      {mark ? <span className="translation__mark" aria-hidden="true">…</span> : null}
    </span>
  );
}
