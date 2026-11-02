/**
 * N-02 deal glossary upload, for Admin (per room) and the Verhandlungsraum menu.
 *
 *   <GlossaryUpload room="vr-3305" lang="de" role="a" />          // panel: current glossary, upload, preview
 *   <GlossaryUpload room="vr-3305" lang="de" role="a" compact />  // one row for a menu sheet
 *
 * Accepts the glossaries/*.yaml format or CSV with a header row de,<lang>,... (several accepted forms
 * in one cell: a|b). The terms are merged into the room's session glossary (translation prompt and
 * fact guard) and forwarded to the interpreter; they are destroyed with the session.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, uploads, type GlossaryInfo, type GlossaryTerm, type GlossaryUploadResult } from "../lib/api";
import type { Role } from "../lib/events";
import { isRtl, pick } from "../lib/i18n";
import "./glossary.css";

const ACCEPT = ".yaml,.yml,.csv,.tsv,text/csv,text/tab-separated-values,application/yaml,application/x-yaml,text/yaml";
const PREVIEW_ROWS = 8;
const PREVIEW_LANGS = 4;

const S: Record<string, Record<string, string>> = {
  title: { en: "Glossary", de: "Glossar", tr: "Sözlük", ru: "Глоссарий", ar: "المسرد" },
  upload: {
    en: "Upload glossary",
    de: "Glossar hochladen",
    tr: "Sözlük yükle",
    ru: "Загрузить глоссарий",
    ar: "رفع المسرد",
  },
  uploading: { en: "Uploading…", de: "Wird hochgeladen…", tr: "Yükleniyor…", ru: "Загрузка…", ar: "جارٍ الرفع…" },
  terms: { en: "terms", de: "Begriffe", tr: "terim", ru: "терм.", ar: "مصطلحات" },
  added: { en: "new", de: "neu", tr: "yeni", ru: "новых", ar: "جديد" },
  updated: { en: "updated", de: "aktualisiert", tr: "güncellendi", ru: "обновлено", ar: "محدَّث" },
  uploaded: { en: "uploaded", de: "hochgeladen", tr: "yüklendi", ru: "загружено", ar: "مرفوع" },
  forwarded: {
    en: "The interpreter uses them from now on.",
    de: "Der Dolmetscher verwendet sie ab jetzt.",
    tr: "Tercüman bunları bundan sonra kullanıyor.",
    ru: "Переводчик использует их с этого момента.",
    ar: "يستخدمها المترجم من الآن.",
  },
  pending: {
    en: "The interpreter gets them as soon as it connects.",
    de: "Der Dolmetscher erhält sie, sobald er verbunden ist.",
    tr: "Tercüman bağlanır bağlanmaz bunları alır.",
    ru: "Переводчик получит их, как только подключится.",
    ar: "سيستلمها المترجم فور اتصاله.",
  },
  burn: {
    en: "Destroyed with the session.",
    de: "Wird mit der Sitzung vernichtet.",
    tr: "Oturumla birlikte silinir.",
    ru: "Удаляется вместе с сеансом.",
    ar: "يُحذف مع الجلسة.",
  },
  format: { en: "Format", de: "Format", tr: "Biçim", ru: "Формат", ar: "الصيغة" },
  format_body: {
    en: "CSV with a header row de,<language>,… (several forms in one cell: a|b), or YAML in the format of glossaries/deal-de.yaml.",
    de: "CSV mit Kopfzeile de,<Sprache>,… (mehrere Formen in einer Zelle: a|b) oder YAML im Format von glossaries/deal-de.yaml.",
    tr: "Başlık satırı de,<dil>,… olan CSV (bir hücrede birden fazla biçim: a|b) veya glossaries/deal-de.yaml biçiminde YAML.",
    ru: "CSV с заголовком de,<язык>,… (несколько форм в одной ячейке: a|b) или YAML в формате glossaries/deal-de.yaml.",
    ar: "ملف CSV بسطر عناوين de,<اللغة>,… (عدة صيغ في خلية واحدة: a|b) أو YAML بصيغة glossaries/deal-de.yaml.",
  },
  more: { en: "more", de: "weitere", tr: "daha", ru: "ещё", ar: "أخرى" },
  error: {
    en: "The glossary could not be uploaded.",
    de: "Das Glossar konnte nicht hochgeladen werden.",
    tr: "Sözlük yüklenemedi.",
    ru: "Не удалось загрузить глоссарий.",
    ar: "تعذّر رفع المسرد.",
  },
};

const s = (key: string, lang: string) => pick(S[key], lang);

function formsOf(t: GlossaryTerm, lang: string): string {
  const v = t[lang];
  return Array.isArray(v) ? v.join(" | ") : typeof v === "string" ? v : "";
}

function fileName(path: string | null | undefined): string {
  return path ? path.split("/").pop() ?? path : "—";
}

const UploadIcon = () => (
  <svg viewBox="0 0 24 24" className="icon" aria-hidden="true">
    <path d="M12 15.5V4M7.5 8.5L12 4l4.5 4.5M4.5 14.5v5h15v-5" />
  </svg>
);

export function GlossaryUpload({
  room,
  lang = "de",
  role,
  compact = false,
  onUploaded,
}: {
  room: string;
  lang?: string;
  role?: Role | string;
  compact?: boolean;
  onUploaded?: (r: GlossaryUploadResult) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [info, setInfo] = useState<GlossaryInfo | null>(null);
  const [busy, setBusy] = useState(false);
  // outcome of the last upload, kept with its room so switching rooms never shows another room's result
  const [last, setLast] = useState<{ room: string; result?: GlossaryUploadResult; error?: string } | null>(null);
  const result = last?.room === room ? last.result ?? null : null;
  const error = last?.room === room ? last.error ?? null : null;

  const refresh = useCallback(() => {
    uploads.glossaryInfo(room).then(setInfo, () => setInfo(null));
  }, [room]);

  useEffect(refresh, [refresh]);

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || busy) return;
    setBusy(true);
    setLast(null);
    try {
      const r = await uploads.glossary(room, file, file.name, role);
      setLast({ room, result: r });
      onUploaded?.(r);
      refresh();
    } catch (err) {
      const detail = err instanceof ApiError && err.status === 400 ? err.message.replace(/^glossary:\s*/, "") : null;
      setLast({ room, error: detail ? `${s("error", lang)} ${detail}` : s("error", lang) });
    } finally {
      setBusy(false);
    }
  }

  const rtl = isRtl(lang);
  const button = (
    <button
      type="button"
      className={compact ? "gloss__row" : "btn btn--ghost gloss__btn"}
      onClick={() => input.current?.click()}
      disabled={busy}
    >
      <UploadIcon />
      <span>{busy ? s("uploading", lang) : s("upload", lang)}</span>
      {compact && info ? (
        <span className="gloss__count mono">
          {info.terms.length} {s("terms", lang)}
        </span>
      ) : null}
    </button>
  );

  return (
    <section className={`gloss ${compact ? "gloss--compact" : ""}`} dir={rtl ? "rtl" : "ltr"} lang={lang}>
      <input ref={input} type="file" accept={ACCEPT} hidden onChange={(e) => void onFile(e)} />
      {!compact ? (
        <header className="gloss__head">
          <h3 className="gloss__h">{s("title", lang)}</h3>
          {info ? (
            <p className="gloss__meta mono">
              <bdi dir="ltr">{fileName(info.source)}</bdi>
              <span>
                {info.terms.length} {s("terms", lang)}
                {info.uploaded ? ` · ${info.uploaded} ${s("uploaded", lang)}` : ""}
              </span>
              <bdi dir="ltr">{info.langs.map((l) => l.toUpperCase()).join(" ")}</bdi>
            </p>
          ) : null}
        </header>
      ) : null}

      {button}

      {error ? (
        <p className="gloss__error" role="alert">
          {error}
        </p>
      ) : null}

      {result ? (
        <div className="gloss__result" role="status">
          <p className="gloss__sum mono">
            <span>
              {result.terms} {s("terms", lang)}
            </span>
            <span>
              {result.added} {s("added", lang)}
            </span>
            <span>
              {result.updated} {s("updated", lang)}
            </span>
            <span>{result.langs.map((l) => l.toUpperCase()).join(" ")}</span>
          </p>
          <p className="gloss__state">
            {result.forwarded ? s("forwarded", lang) : s("pending", lang)} {s("burn", lang)}
          </p>
          {!compact ? <Preview terms={result.uploaded} langs={result.langs} lang={lang} /> : null}
        </div>
      ) : null}

      {!compact && !result ? (
        <details className="gloss__format">
          <summary className="label">{s("format", lang)}</summary>
          <p>{s("format_body", lang)}</p>
          <pre className="mono" dir="ltr">
            {"de,en,tr\nLiefertermin,delivery date,teslim tarihi\nSkonto,cash discount|early payment discount,iskonto"}
          </pre>
        </details>
      ) : null}
    </section>
  );
}

function Preview({ terms, langs, lang }: { terms: GlossaryTerm[]; langs: string[]; lang: string }) {
  const cols = langs.filter((l) => l !== "de").slice(0, PREVIEW_LANGS);
  const rows = terms.slice(0, PREVIEW_ROWS);
  return (
    <div className="gloss__table">
      <table>
        <thead>
          <tr>
            <th scope="col">DE</th>
            {cols.map((l) => (
              <th key={l} scope="col">
                {l.toUpperCase()}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((t, i) => (
            <tr key={`${t.de}-${i}`}>
              <th scope="row" lang="de" dir="ltr">
                {t.de}
              </th>
              {cols.map((l) => (
                <td key={l} lang={l} dir={isRtl(l) ? "rtl" : "ltr"}>
                  {formsOf(t, l) || "—"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {terms.length > rows.length ? (
        <p className="gloss__more mono">
          + {terms.length - rows.length} {s("more", lang)}
        </p>
      ) : null}
    </div>
  );
}
