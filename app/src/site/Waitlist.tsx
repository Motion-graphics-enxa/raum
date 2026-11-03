/**
 * The website's one action. A sheet lifted off the wall: the plate above the Begleitstrich, the email field on the
 * paint below it. It POSTs {email} to /api/waitlist and says "You're on the list" only once the server has stored it.
 */
import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type MouseEvent, type ReactNode } from "react";
import { Check, Cross } from "../components/Icons";
import { WaitlistContext } from "./waitlistContext";
import "./waitlist.css";

type State =
  | { s: "idle" }
  | { s: "sending" }
  | { s: "invalid" }
  | { s: "failed" }
  | { s: "soon" } // the server answered but has nowhere to store it yet
  | { s: "done"; email: string };

const MAX = 254;
const EMAIL = /^[^\s@]{1,64}@[^\s@.]+(\.[^\s@.]+)*\.[^\s@.]{2,}$/;

export function WaitlistProvider({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  const open = useCallback(() => {
    const d = ref.current;
    if (!d || d.open) return;
    d.showModal();
    d.querySelector<HTMLElement>("[data-autofocus]")?.focus();
  }, []);
  return (
    <WaitlistContext.Provider value={open}>
      {children}
      <Waitlist dialog={ref} />
    </WaitlistContext.Provider>
  );
}

function Waitlist({ dialog }: { dialog: React.RefObject<HTMLDialogElement | null> }) {
  const [state, setState] = useState<State>({ s: "idle" });
  const [email, setEmail] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const id = useId();
  const sending = state.s === "sending";
  const error = state.s === "invalid" || state.s === "failed";

  // On a phone the sheet stands on the bottom edge; lift it above the on-screen keyboard while that is open.
  useEffect(() => {
    const vv = window.visualViewport;
    const d = dialog.current;
    if (!vv || !d) return;
    const sync = () => d.style.setProperty("--kb", `${Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop))}px`);
    vv.addEventListener("resize", sync);
    vv.addEventListener("scroll", sync);
    return () => {
      vv.removeEventListener("resize", sync);
      vv.removeEventListener("scroll", sync);
    };
  }, [dialog]);

  const close = () => dialog.current?.close();
  // a click that lands on the dialog itself fell on the backdrop: the sheet fills the dialog edge to edge
  const onBackdrop = (e: MouseEvent<HTMLDialogElement>) => {
    if (e.target === e.currentTarget) close();
  };

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (sending) return;
    const value = email.trim();
    if (value.length > MAX || !EMAIL.test(value)) {
      setState({ s: "invalid" });
      input.current?.focus();
      return;
    }
    setState({ s: "sending" });
    try {
      const res = await fetch("/api/waitlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: value }),
      });
      const body = (await res.json().catch(() => ({}))) as { stored?: boolean };
      if (res.ok && body.stored === true) setState({ s: "done", email: value });
      else if (res.status === 400) setState({ s: "invalid" });
      else if (res.ok || res.status === 503) setState({ s: "soon" });
      else setState({ s: "failed" });
    } catch {
      setState({ s: "failed" });
    }
  };

  return (
    <dialog ref={dialog} className={`wl is-${state.s}`} aria-labelledby={`${id}-h`} onClick={onBackdrop}>
      <div className="wl__sheet">
        <div className="wl__wall">
          <h2 id={`${id}-h`} className="wl__h">
            Join the waitlist
          </h2>
          <p className="wl__lead">Be the first to hear when you can put RAUM in your own room.</p>
          <button type="button" className="wl__close" aria-label="Close" onClick={close}>
            <Cross />
          </button>
        </div>

        <div className="wl__dado">
          {state.s === "done" ? (
            <div className="wl__done" role="status">
              <span className="wl__tick" aria-hidden="true">
                <Check />
              </span>
              <p className="wl__done-h">You’re on the list.</p>
              <p className="wl__done-p">
                We’ll write to <span className="wl__addr">{state.email}</span> when RAUM opens.
              </p>
              <button type="button" className="btn btn--wall wl__ok" data-autofocus onClick={close}>
                Done
              </button>
            </div>
          ) : state.s === "soon" ? (
            <div className="wl__done wl__done--soon" role="status">
              <span className="wl__tick wl__tick--soon" aria-hidden="true" />
              <p className="wl__done-h">Waitlist opens shortly.</p>
              <p className="wl__done-p">Nothing was saved yet. Please come back in a little while.</p>
              <button type="button" className="btn btn--wall wl__ok" data-autofocus onClick={close}>
                Close
              </button>
            </div>
          ) : (
            <form className="wl__form" noValidate onSubmit={submit}>
              <label className="wl__label" htmlFor={`${id}-email`}>
                Your email
              </label>
              <div className={`wl__field ${error ? "is-bad" : ""}`}>
                <input
                  ref={input}
                  id={`${id}-email`}
                  name="email"
                  type="email"
                  inputMode="email"
                  autoComplete="email"
                  autoCapitalize="off"
                  autoCorrect="off"
                  spellCheck={false}
                  enterKeyHint="send"
                  maxLength={MAX}
                  placeholder="name@practice.de"
                  required
                  data-autofocus
                  readOnly={sending}
                  value={email}
                  aria-invalid={error ? state.s === "invalid" : undefined}
                  aria-describedby={`${error ? `${id}-err ` : ""}${id}-why`}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    if (error) setState({ s: "idle" });
                  }}
                />
                <button type="submit" className="wl__go" disabled={sending} aria-disabled={sending}>
                  {sending ? <span className="wl__spin" aria-hidden="true" /> : null}
                  {sending ? "Sending" : "Join"}
                </button>
              </div>
              {error ? (
                <p id={`${id}-err`} className="wl__err" role="alert">
                  <Cross />
                  {state.s === "invalid"
                    ? "That doesn’t look like an email address. Check it and try again."
                    : "We couldn’t save it just now. Try again in a minute."}
                </p>
              ) : null}
            </form>
          )}
          <p id={`${id}-why`} className="wl__why">
            We keep your email only to tell you when RAUM opens. Nothing else, never shared.
          </p>
        </div>
      </div>
    </dialog>
  );
}
