import type { JoinResponse } from "./api";
import { primaryLang } from "./i18n";

const KEY = (room: string) => `raum:join:${room}`;

export function saveJoin(j: JoinResponse): void {
  try {
    sessionStorage.setItem(KEY(j.room), JSON.stringify(j));
  } catch {
    /* private mode */
  }
}

export function loadJoin(room: string): JoinResponse | null {
  try {
    const raw = sessionStorage.getItem(KEY(room));
    return raw ? (JSON.parse(raw) as JoinResponse) : null;
  } catch {
    return null;
  }
}

export function clearJoin(room: string): void {
  try {
    sessionStorage.removeItem(KEY(room));
  } catch {
    /* ignore */
  }
}

/** C-02: language from the phone, editable, remembered per device. */
export function deviceLang(): string {
  try {
    const saved = localStorage.getItem("raum:lang");
    if (saved) return saved;
  } catch {
    /* ignore */
  }
  return primaryLang(navigator.language);
}

export function rememberLang(lang: string): void {
  try {
    localStorage.setItem("raum:lang", lang);
  } catch {
    /* ignore */
  }
}

export function deviceKind(): string {
  const ua = navigator.userAgent;
  if (/iPad|Tablet/i.test(ua)) return "tablet";
  if (/Android|iPhone|Mobile/i.test(ua)) return "phone";
  return "laptop";
}
