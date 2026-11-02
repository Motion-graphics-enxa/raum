# RAUM build status

_Read by the ops session. Updated at every milestone._

## Now
- Milestone 0 (contract) done: scaffold, event protocol, room schema, raum.yaml + profiles, prompts, glossaries.
- In progress: security + fact guard, pipeline + agent, core API, eval + deploy (parallel), design + app.

## Contract
- Events: `core/raum/events.py` <-> `app/src/lib/events.ts` (plan §6.5, extensions marked).
- Rooms: `modes/*.yaml`, schema `core/raum/modes/schema.py`. Room id prefix encodes the mode: `sz-` Sprechzimmer, `kz-` Kanzlei, `vr-` Verhandlungsraum.
- Engines: `core/raum/pipeline/base.py`; selected by `raum.yaml` `profile:` (`mock` now, `spark` on the GX10).
- Agent <-> core link: `core/raum/link.py`.

## Blockers
- GX10 not reachable yet (ops session). Everything here runs on the mock profile.
