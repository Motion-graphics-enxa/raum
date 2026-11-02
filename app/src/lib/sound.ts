/** Signature tick when a sentence is verified (§4.3). Synthesised, no asset, no network. */
let ctx: AudioContext | null = null;

export function unlockSound(): void {
  try {
    ctx ??= new AudioContext();
    if (ctx.state === "suspended") void ctx.resume();
  } catch {
    ctx = null;
  }
}

export function tick(): void {
  if (!ctx || ctx.state !== "running") return;
  const t0 = ctx.currentTime;
  const o = ctx.createOscillator();
  const g = ctx.createGain();
  o.type = "triangle";
  o.frequency.setValueAtTime(1760, t0);
  o.frequency.exponentialRampToValueAtTime(1180, t0 + 0.06);
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime(0.12, t0 + 0.004);
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.11);
  o.connect(g).connect(ctx.destination);
  o.start(t0);
  o.stop(t0 + 0.12);
}
