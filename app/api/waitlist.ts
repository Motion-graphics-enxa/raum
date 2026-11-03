/**
 * POST /api/waitlist {email}: the public website's one write (a Vercel Function, deployed with app/vercel.json).
 * Each sign-up is one small private JSON in the project's Vercel Blob store: {email, ts}. No IP, no user agent.
 * Answers {stored: true} only after the write succeeded; the page says "You're on the list" only then.
 */
import { put } from "@vercel/blob";

const MAX_EMAIL = 254;
const MAX_BODY = 1024;
// one @, a plausible local part, a dotted domain with a 2+ letter top level; no spaces, quotes or angle brackets
const EMAIL = /^[^\s@"'<>()[\]\\,;:]{1,64}@(?=[^\s@]{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/i;

const reply = (status: number, body: Record<string, unknown>) =>
  Response.json(body, { status, headers: { "Cache-Control": "no-store" } });

export async function POST(request: Request): Promise<Response> {
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  if (origin && host && new URL(origin).host !== host) return reply(403, { stored: false, error: "origin" });
  if (!(request.headers.get("content-type") ?? "").toLowerCase().startsWith("application/json"))
    return reply(415, { stored: false, error: "json" });

  const raw = await request.text();
  if (raw.length > MAX_BODY) return reply(413, { stored: false, error: "size" });
  let email: unknown;
  try {
    email = (JSON.parse(raw) as { email?: unknown } | null)?.email;
  } catch {
    return reply(400, { stored: false, error: "email" });
  }
  if (typeof email !== "string") return reply(400, { stored: false, error: "email" });
  const address = email.trim().toLowerCase();
  if (address.length > MAX_EMAIL || address.includes("..") || !EMAIL.test(address))
    return reply(400, { stored: false, error: "email" });

  if (!process.env.BLOB_READ_WRITE_TOKEN) return reply(503, { stored: false, error: "storage" });

  const ts = new Date().toISOString();
  try {
    await put(`waitlist/${ts.replace(/[:.]/g, "-")}.json`, JSON.stringify({ email: address, ts }), {
      access: "private",
      addRandomSuffix: true, // the pathname never repeats, and its URL is never returned
      contentType: "application/json",
    });
  } catch (err) {
    console.error("waitlist: blob write failed", err instanceof Error ? err.message : err);
    return reply(502, { stored: false, error: "storage" });
  }
  return reply(200, { stored: true });
}
