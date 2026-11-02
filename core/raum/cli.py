"""``raum`` command line.

    raum core [--host H --port P]        run raum-core (FastAPI)       -> raum.api.server:run
    raum agent [dev|start]               run raum-agent (LiveKit)      -> raum.agent.worker:run
    raum audit verify [--path P]         recompute the audit hash chain -> raum.security.audit_cli:main
    raum modes check                     validate modes/*.yaml
    raum config                          print the resolved raum.yaml + profile
    raum voicelock check [--engine E]    load the F-10 speaker model (download once) and time it
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "core":
        from raum.api.server import run

        return run(rest) or 0
    if cmd == "agent":
        from raum.agent.worker import run

        return run(rest) or 0
    if cmd == "audit":
        from raum.security.audit_cli import main as audit_main

        return audit_main(rest)
    if cmd == "modes" and rest[:1] == ["check"]:
        from raum.modes import load_modes

        for m in load_modes().values():
            print(f"ok  {m.id:18} prefix={m.prefix} kinds={','.join(m.guard.kinds)}")
        return 0
    if cmd == "config":
        import json

        from raum.config import load_config

        print(json.dumps(load_config().model_dump(mode="json"), indent=2))
        return 0
    if cmd == "voicelock":
        from raum.pipeline.voicelock import main as voicelock_main

        return voicelock_main(rest)
    print(f"unknown command: {' '.join(argv)}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
