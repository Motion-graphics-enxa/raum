"""``raum audit ...`` (T-04, T-05).

    raum audit verify [--path P] [--expect-head H]   recompute the hash chain
        -> "ok: N entries, head <hash>" (exit 0) or "BROKEN at line K: <reason>" (exit 1)
    raum audit show --session ID [--path P]          compliance export (JSON) of one session

The default path is ``raum.config.get_config().audit_path()``. ``--expect-head`` takes a head hash
kept elsewhere (e.g. from ``burn.done``) and fails if that line is no longer in the chain, which is
how a cut-off tail shows up.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from raum.events import now_ms
from raum.security.audit import GENESIS, read_entries, verify_chain

AI_DISCLOSURE = ("AI interpreter (EU AI Act Art. 50); no recording; content encrypted (AES-256-GCM), "
                 "key in memory only")


def _default_path() -> Path:
    from raum import config

    return config.get_config().audit_path()


def compliance_export(path: str | Path, session_id: str) -> dict[str, Any] | None:
    """One session's audit trail as ``raum.compliance.v1`` (same shape as raum-core's export,
    built from the log alone). ``None`` if the log has no entry for that session."""
    entries = read_entries(path, session=session_id)
    if not entries:
        return None
    consents, off_record, interp, confirms, approvals = [], [], [], [], []
    models: dict[str, Any] = {}
    started = ended = burn = None
    for e in entries:
        ev, ts = e.get("event"), e.get("ts")
        m = e.get("meta") if isinstance(e.get("meta"), dict) else {}
        if ev == "session_start":
            started = {"ts": ts, **{k: m.get(k) for k in ("room", "mode", "profile", "reason")}}
        elif ev == "consent":
            consents.append({"ts": ts, **{k: m.get(k) for k in ("role", "lang", "device", "consent_version",
                                                                 "voice")}})
        elif ev == "models":
            models = m.get("models") or dict(m)
        elif ev == "off_record_on":
            off_record.append({"on": ts, "off": None, "by": m.get("by")})
        elif ev == "off_record_off":
            if off_record and off_record[-1]["off"] is None:
                off_record[-1]["off"] = ts
            else:
                off_record.append({"on": None, "off": ts, "by": m.get("by")})
        elif ev == "human_interpreter":
            interp.append({"ts": ts, "by": m.get("by"), "requested": m.get("requested", True)})
        elif ev == "confirm":
            confirms.append({"ts": ts, "by": m.get("by"), "accepted": m.get("accepted"),
                             "flag_id": m.get("flag_id")})
        elif ev == "summary_approved":
            approvals.append({"ts": ts, "by": m.get("by")})
        elif ev == "session_end":
            ended = {"ts": ts, "reason": m.get("reason"), "duration_ms": m.get("duration_ms")}
        elif ev == "key_destroyed":
            burn = {"ts": ts, "hash": e.get("hash"), "records": m.get("records"),
                    "file_deleted": m.get("file_deleted")}
    report = verify_chain(path)
    all_entries = read_entries(path)
    return {
        "kind": "raum.compliance.v1",
        "generated_at": now_ms(),
        "session_id": session_id,
        "room": (started or {}).get("room"),
        "mode": (started or {}).get("mode"),
        "profile": (started or {}).get("profile"),
        "ai_disclosure": AI_DISCLOSURE,
        "started": started,
        "consents": consents,
        "models": models,
        "off_record": off_record,
        "human_interpreter": interp,
        "confirmations": confirms,
        "summary_approvals": approvals,
        "ended": ended,
        "burn": burn,
        "audit": {
            "path": str(path),
            "entries": len(entries),
            "head_hash": all_entries[-1].get("hash") if all_entries else GENESIS,
            "chain_ok": report.ok,
            "chain_lines": report.lines,
            "first_bad": report.first_bad,
            "reason": report.reason,
        },
        "entries": entries,
    }


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="raum audit", description="Verify and export the RAUM audit log.")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="recompute the hash chain")
    v.add_argument("--path", help="audit log (default: from raum.yaml)")
    v.add_argument("--expect-head", help="a head hash recorded earlier that must still be in the chain")
    s = sub.add_parser("show", help="compliance export of one session (JSON)")
    s.add_argument("--session", required=True, help="session id")
    s.add_argument("--path", help="audit log (default: from raum.yaml)")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as e:  # argparse: --help -> 0, usage error -> 2
        return e.code if isinstance(e.code, int) else 2
    path = Path(args.path) if args.path else _default_path()

    if args.cmd == "verify":
        if not path.exists():
            print(f"no audit log at {path}", file=sys.stderr)
            return 1
        report = verify_chain(path)
        if not report.ok:
            print(f"BROKEN at line {report.first_bad}: {report.reason}")
            return 1
        entries = read_entries(path)
        head = entries[-1]["hash"] if entries else GENESIS
        if args.expect_head and args.expect_head != head and \
                args.expect_head not in {e.get("hash") for e in entries}:
            print(f"BROKEN at line {report.lines + 1}: expected head {args.expect_head} is not in the chain "
                  f"(log truncated or rewritten)")
            return 1
        print(f"ok: {report.lines} entries, head {head}")
        return 0

    export = compliance_export(path, args.session)
    if export is None:
        print(f"no audit entries for session {args.session} in {path}", file=sys.stderr)
        return 1
    print(json.dumps(export, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
