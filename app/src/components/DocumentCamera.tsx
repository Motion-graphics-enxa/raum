/**
 * F-07 document camera. A button opens the phone camera; the photo goes to the box, which reads the
 * letter, contract or term sheet and explains it in the reader's language. The answer is drawn as a
 * sheet: title, plain-language text and every date, amount, dose and deadline dimensioned like a wall
 * (solid = the figure survived the fact guard, dotted = unsure, check it on the paper).
 *
 *   <DocumentCamera room="sz-4821" role="b" lang="ar" />
 *   <DocumentCard doc={envelope.data} lang={myLang} />     // a document.ready from the event stream
 *
 * The photo is downscaled on the phone (JPEG, long edge 2048 px, no EXIF location), sent once and
 * not kept: neither the phone nor the box stores it.
 */
import { Fragment, useEffect, useRef, useState } from "react";
import { ApiError, uploads } from "../lib/api";
import type { EventData, Role } from "../lib/events";
import { isRtl, pick } from "../lib/i18n";
import { Dimension, fmtMs } from "./Drawing";
import "./document.css";

export type DocumentReading = EventData["document.ready"];

const MAX_EDGE = 2048;

const S: Record<string, Record<string, string>> = {
  shoot: {
    en: "Photograph a document",
    de: "Dokument fotografieren",
    tr: "Belgenin fotoğrafını çek",
    ru: "Сфотографировать документ",
    ar: "صوّر مستندًا",
  },
  again: {
    en: "Photograph another",
    de: "Weiteres fotografieren",
    tr: "Başka bir belge çek",
    ru: "Сфотографировать ещё",
    ar: "صوّر مستندًا آخر",
  },
  privacy: {
    en: "The photo is read on this box and not kept.",
    de: "Das Foto wird auf dieser Box gelesen und nicht gespeichert.",
    tr: "Fotoğraf bu cihazda okunur ve saklanmaz.",
    ru: "Фото читается на этом устройстве и не сохраняется.",
    ar: "تُقرأ الصورة على هذا الجهاز ولا تُحفظ.",
  },
  reading: {
    en: "Reading the document",
    de: "Dokument wird gelesen",
    tr: "Belge okunuyor",
    ru: "Документ читается",
    ar: "جارٍ قراءة المستند",
  },
  document: { en: "Document", de: "Dokument", tr: "Belge", ru: "Документ", ar: "مستند" },
  photo: { en: "Photo", de: "Foto", tr: "Fotoğraf", ru: "Фото", ar: "الصورة" },
  not_kept: { en: "not kept", de: "nicht gespeichert", tr: "saklanmadı", ru: "не сохранено", ar: "لم تُحفظ" },
  read_in: { en: "Read in", de: "Gelesen in", tr: "Okuma süresi", ru: "Прочитано за", ar: "مدة القراءة" },
  model: { en: "Model", de: "Modell", tr: "Model", ru: "Модель", ar: "النموذج" },
  unsure: {
    en: "Dotted: not sure. Check it on the paper",
    de: "Gepunktet: unsicher. Bitte auf dem Papier prüfen",
    tr: "Noktalı: emin değil. Lütfen kâğıttan kontrol edin",
    ru: "Пунктир: не уверено. Проверьте по бумаге",
    ar: "المنقّط: غير مؤكد. تحقّق منه على الورقة",
  },
  printed: { en: "printed", de: "gedruckt", tr: "basılı", ru: "в документе", ar: "المطبوع" },
  no_advice: {
    en: "This explains the paper. It is not advice.",
    de: "Das erklärt das Papier. Es ist keine Beratung.",
    tr: "Bu, belgeyi açıklar. Tavsiye değildir.",
    ru: "Это объяснение документа, а не совет.",
    ar: "هذا شرح للورقة وليس نصيحة.",
  },
  untitled: { en: "Document", de: "Dokument", tr: "Belge", ru: "Документ", ar: "مستند" },
  err_off: {
    en: "The document camera is off on this box.",
    de: "Die Dokumentkamera ist auf dieser Box ausgeschaltet.",
    tr: "Belge kamerası bu cihazda kapalı.",
    ru: "Камера документов на этом устройстве выключена.",
    ar: "كاميرا المستندات متوقفة على هذا الجهاز.",
  },
  err_large: {
    en: "The photo is too large.",
    de: "Das Foto ist zu groß.",
    tr: "Fotoğraf çok büyük.",
    ru: "Фото слишком большое.",
    ar: "الصورة كبيرة جدًا.",
  },
  err_format: {
    en: "This picture format cannot be read. Take the photo with the camera.",
    de: "Dieses Bildformat kann nicht gelesen werden. Bitte mit der Kamera fotografieren.",
    tr: "Bu resim biçimi okunamıyor. Lütfen kamerayla çekin.",
    ru: "Этот формат изображения не читается. Сфотографируйте камерой.",
    ar: "لا يمكن قراءة صيغة هذه الصورة. التقط الصورة بالكاميرا.",
  },
  err_session: {
    en: "There is no conversation in this room right now.",
    de: "In diesem Raum läuft gerade kein Gespräch.",
    tr: "Bu odada şu anda görüşme yok.",
    ru: "Сейчас в этой комнате нет разговора.",
    ar: "لا توجد محادثة في هذه الغرفة الآن.",
  },
  err_failed: {
    en: "The document could not be read. Try again with more light and the whole page in view.",
    de: "Das Dokument konnte nicht gelesen werden. Bitte mit mehr Licht und der ganzen Seite im Bild erneut versuchen.",
    tr: "Belge okunamadı. Daha fazla ışıkla ve sayfanın tamamı görünecek şekilde tekrar deneyin.",
    ru: "Документ не удалось прочитать. Попробуйте ещё раз при хорошем свете, чтобы была видна вся страница.",
    ar: "تعذّرت قراءة المستند. حاول مرة أخرى بإضاءة أفضل مع ظهور الصفحة كاملة.",
  },
};

const s = (key: string, lang: string) => pick(S[key], lang);

function errorText(err: unknown, lang: string): string {
  if (err instanceof ApiError) {
    if (err.status === 503) return s("err_off", lang);
    if (err.status === 413) return s("err_large", lang);
    if (err.status === 415) return s("err_format", lang);
    if (err.status === 404 || err.status === 409) return s("err_session", lang);
  }
  return s("err_failed", lang);
}

/** Downscale and re-encode on the phone: smaller upload, upright, and no EXIF (location) leaves it. */
async function preparePhoto(file: File): Promise<Blob> {
  try {
    const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, MAX_EDGE / Math.max(bmp.width, bmp.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bmp.width * scale));
    canvas.height = Math.max(1, Math.round(bmp.height * scale));
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      bmp.close();
      return file;
    }
    ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
    bmp.close();
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.88));
    canvas.width = canvas.height = 0; // release the pixels now
    return blob ?? file;
  } catch {
    return file; // the browser cannot decode it (e.g. HEIC on desktop Chrome): let the box say so
  }
}

const Camera = () => (
  <svg viewBox="0 0 24 24" className="icon" aria-hidden="true">
    <path d="M3.5 7.5h4l1.5-2.5h6l1.5 2.5h4v11.5h-17z" />
    <circle cx="12" cy="13.25" r="3.75" />
  </svg>
);

export function DocumentCamera({
  room,
  role,
  lang,
  onRead,
}: {
  room: string;
  role: Role | string;
  lang: string;
  onRead?: (doc: DocumentReading) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState(0);
  const [doc, setDoc] = useState<DocumentReading | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rtl = isRtl(lang);

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // drop the reference; the same paper can be photographed again
    if (!file || busy) return;
    setBusy(true);
    setStarted(Date.now());
    setError(null);
    try {
      const photo = await preparePhoto(file);
      const d = await uploads.document(room, photo, role, lang);
      setDoc(d);
      onRead?.(d);
    } catch (err) {
      setError(errorText(err, lang));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="doccam" dir={rtl ? "rtl" : "ltr"} lang={lang}>
      <input
        ref={input}
        type="file"
        accept="image/*"
        capture="environment"
        hidden
        onChange={(e) => void onFile(e)}
      />
      {busy ? <ReadingSheet lang={lang} since={started} /> : doc ? <DocumentCard doc={doc} lang={lang} /> : null}
      {error ? (
        <p className="doccam__error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="doccam__row">
        <button
          type="button"
          className={`btn ${doc ? "btn--ghost" : ""} doccam__shoot`}
          onClick={() => input.current?.click()}
          disabled={busy}
        >
          <Camera />
          {doc ? s("again", lang) : s("shoot", lang)}
        </button>
        <p className="doccam__privacy">{s("privacy", lang)}</p>
      </div>
    </div>
  );
}

/** While the model reads: the sheet drawn in the dashed line type of work in progress. */
function ReadingSheet({ lang, since }: { lang: string; since: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 200);
    return () => window.clearInterval(id);
  }, []);
  return (
    <div className="docsheet docsheet--reading" role="status" aria-live="polite" aria-busy="true">
      <div className="docsheet__head">
        <span>{s("reading", lang)}</span>
        <bdi className="mono docsheet__ms" dir="ltr">
          {fmtMs(Math.max(0, now - since))}
        </bdi>
      </div>
      <div className="docsheet__placeholder" aria-hidden="true">
        <Dimension value="…" state="pending" lang={lang} />
        <Dimension value="…" state="pending" lang={lang} delay={60} />
      </div>
    </div>
  );
}

/** Uncertain readings ("(?)") as ochre marks inside the text. */
function withMarks(text: string): React.ReactNode[] {
  return text.split("(?)").flatMap((part, i) =>
    i === 0
      ? [part]
      : [
          <span key={i} className="docsheet__q mono">
            (?)
          </span>,
          part,
        ],
  );
}

/** One read document, drawn as a sheet. Use it for document.ready events from the stream too. */
export function DocumentCard({ doc, lang }: { doc: DocumentReading; lang: string }) {
  // The frame (head, notes, title block) speaks the viewer's language; title, text and facts keep
  // the reading's own language and direction (a German lawyer may see an Arabic reading).
  const dir = isRtl(doc.lang) ? "rtl" : "ltr";
  const unsure = doc.facts.filter((f) => f.uncertain);
  const pair = doc.source_lang && doc.source_lang !== doc.lang ? `${doc.source_lang} → ${doc.lang}` : doc.lang;
  return (
    <article className="docsheet" lang={lang} dir={isRtl(lang) ? "rtl" : "ltr"}>
      <header className="docsheet__head">
        <span>{s("document", lang)}</span>
        <bdi className="mono docsheet__pair" dir="ltr">
          {pair.toUpperCase()}
        </bdi>
      </header>
      <h3 className="docsheet__title" lang={doc.title ? doc.lang : lang} dir={doc.title ? dir : undefined}>
        {doc.title || s("untitled", lang)}
      </h3>
      <p className="docsheet__text" lang={doc.lang} dir={dir}>
        {withMarks(doc.text)}
      </p>
      {doc.facts.length ? (
        <div className="facts docsheet__facts" dir={dir}>
          {doc.facts.map((f, i) => (
            <Dimension
              key={`${f.kind}-${i}`}
              value={f.value}
              kind={f.kind}
              lang={lang}
              delay={i * 60}
              state={f.uncertain ? "unsure" : "ok"}
            />
          ))}
        </div>
      ) : null}
      {unsure.length ? (
        <p className="docsheet__note">
          <span className="docsheet__swatch" aria-hidden="true" />
          <span>
            {s("unsure", lang)}
            {unsure.some((f) => f.source) ? (
              <>
                {" · "}
                {s("printed", lang)}:{" "}
                {unsure
                  .filter((f) => f.source)
                  .map((f, i) => (
                    <Fragment key={i}>
                      {i ? ", " : ""}
                      <bdi className="mono docsheet__src">{f.source}</bdi>
                    </Fragment>
                  ))}
              </>
            ) : null}
          </span>
        </p>
      ) : null}
      <p className="docsheet__advice">{s("no_advice", lang)}</p>
      <footer className="docsheet__tb">
        <dl>
          <div>
            <dt>{s("photo", lang)}</dt>
            <dd>{s("not_kept", lang)}</dd>
          </div>
          <div>
            <dt>{s("read_in", lang)}</dt>
            <dd className="mono">
              <bdi dir="ltr">{doc.ms != null ? fmtMs(doc.ms) : "—"}</bdi>
            </dd>
          </div>
          <div>
            <dt>{s("model", lang)}</dt>
            <dd className="mono">
              <bdi dir="ltr">{doc.model ?? "—"}</bdi>
            </dd>
          </div>
        </dl>
      </footer>
    </article>
  );
}
