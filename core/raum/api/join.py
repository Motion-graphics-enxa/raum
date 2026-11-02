"""``POST /api/join``: consent (C-03), LiveKit token, connection path (G-03)."""

from __future__ import annotations

import ipaddress
import json
import re
import warnings
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from raum.api.deps import CoreDep
from raum.config import RaumConfig
from raum.events import ParticipantInfo, Path, Role
from raum.modes import ModeConfig, ModeError, mode_for_room
from raum.sessions import CONSENT_VERSION

router = APIRouter(prefix="/api", tags=["join"])

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_LANG_RE = re.compile(r"^[a-z]{2,3}$")


class JoinRequest(BaseModel):
    room: str = Field(examples=["sz-4821"])
    role: Role
    lang: str = Field("auto", description="BCP-47 tag or 'auto'; reduced to the primary subtag")
    device: str = Field("phone", max_length=32)
    audio_out: Literal["headphones", "speaker", "room"] = Field(
        "speaker", description="How the voice plays on this device (C-14 echo control)")
    consent: bool = Field(False, description="Must be true: 'This is an AI interpreter' (EU AI Act Art. 50)")
    consent_version: str = Field(CONSENT_VERSION, max_length=64)


class JoinResponse(BaseModel):
    token: str
    livekit_url: str
    identity: str
    room: str
    role: Role
    lang: str
    mode: dict[str, Any]
    session_id: str
    path: Path


# ---------------------------------------------------------------- client address -> path


def _ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value:
        return None
    v = value.strip().strip('"')
    if v.startswith("[") and "]" in v:  # [v6]:port
        v = v[1:v.index("]")]
    elif v.count(":") == 1:  # v4:port
        v = v.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(v)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _is_loopback(conn_host: str | None) -> bool:
    ip = _ip(conn_host)
    return bool(ip and ip.is_loopback)


def client_ip(request: Request) -> str | None:
    """The phone's address. ``X-Forwarded-For`` is honoured only from a loopback proxy (Caddy, Vite);
    its right-most entry is the one that proxy added."""
    peer = request.client.host if request.client else None
    if _is_loopback(peer):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            last = xff.split(",")[-1].strip()
            if _ip(last):
                return str(_ip(last))
    return peer


def classify_ip(host: str | None) -> Path:
    """100.64.0.0/10 (and Tailscale's v6 range) -> tunnel; private/loopback/link-local -> direct;
    anything else -> relayed."""
    ip = _ip(host)
    if ip is None:
        return "direct"  # unix socket, test client: on the box
    tailnet = TAILNET_V4 if isinstance(ip, ipaddress.IPv4Address) else TAILNET_V6
    if ip in tailnet:
        return "tunnel"
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return "direct"
    return "relayed"


def detect_path(request: Request) -> Path:
    return classify_ip(client_ip(request))


def public_livekit_url(request: Request, cfg: RaumConfig) -> str:
    """``network.livekit_url`` or, when ``auto``, the origin the phone used (Caddy and Vite proxy
    ``/rtc`` to LiveKit, so app, API and signalling share one origin)."""
    configured = cfg.network.livekit_url
    if configured and configured != "auto":
        return configured
    origin = request.headers.get("origin")
    if origin and origin != "null" and "://" in origin:
        scheme, host = origin.split("://", 1)
    else:
        trusted = _is_loopback(request.client.host if request.client else None)
        fwd_host = request.headers.get("x-forwarded-host") if trusted else None
        fwd_proto = request.headers.get("x-forwarded-proto") if trusted else None
        host = (fwd_host or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
        scheme = (fwd_proto or request.url.scheme).split(",")[0].strip()
    ws = "wss" if scheme in ("https", "wss") else "ws"
    return f"{ws}://{host.rstrip('/')}"


# ---------------------------------------------------------------- token


def normalize_lang(lang: str | None, mode: ModeConfig, role: str) -> str:
    v = (lang or "").strip().replace("_", "-").split("-")[0].lower()
    if _LANG_RE.match(v) and v != "auto":
        return v
    if role in mode.roles and mode.roles[role].default_lang != "auto":  # type: ignore[index]
        return mode.roles[role].default_lang  # type: ignore[index]
    return "en"


def room_configuration(cfg: RaumConfig) -> Any:
    """LiveKit room settings for every token RAUM mints (C-12): a departure timeout long enough for
    phones to come back, and explicit agent dispatch when ``network.agent_name`` is set."""
    from livekit import api

    name = cfg.network.dispatch_name()
    return api.RoomConfiguration(
        departure_timeout=max(0, int(cfg.network.departure_timeout_s)),
        empty_timeout=max(0, int(cfg.network.empty_timeout_s)),
        agents=[api.RoomAgentDispatch(agent_name=name)] if name else [],
    )


def mint_token(cfg: RaumConfig, room: str, p: ParticipantInfo, name: str | None = None) -> str:
    from livekit import api

    meta = {"role": p.role, "lang": p.lang, "device": p.device, "path": p.path, "audio_out": p.audio_out}
    grants = api.VideoGrants(room_join=True, room=room, can_publish=p.role != "observer", can_subscribe=True,
                             can_publish_data=True)
    tok = (api.AccessToken(cfg.network.livekit_api_key, cfg.network.livekit_api_secret)
           .with_identity(p.id)
           .with_name(name or p.id)
           .with_metadata(json.dumps(meta))
           .with_attributes({k: str(v) for k, v in meta.items()})
           .with_grants(grants)
           .with_room_config(room_configuration(cfg)))
    with warnings.catch_warnings():  # livekit-server --dev uses a short secret
        warnings.filterwarnings("ignore", message=".*HMAC key.*")
        return tok.to_jwt()


@router.post("/join", response_model=JoinResponse, responses={400: {"description": "consent missing"},
                                                               404: {"description": "unknown room / mode"}})
async def join(body: JoinRequest, request: Request, core: CoreDep) -> JoinResponse:
    """Join a room: requires consent; returns a LiveKit token whose metadata and attributes carry
    ``{role, lang, device, path}`` for the agent."""
    if body.consent is not True:
        raise HTTPException(400, "consent required: this is an AI interpreter (EU AI Act Art. 50)")
    try:
        mode = mode_for_room(body.room)
    except ModeError as e:
        raise HTTPException(404, str(e)) from e
    lang = normalize_lang(body.lang, mode, body.role)
    path = detect_path(request)
    room, session, p = await core.sessions.join(body.room, role=body.role, lang=lang, device=body.device,
                                                path=path, consent_version=body.consent_version, audio_out=body.audio_out)
    name = mode.label(body.role, lang) if body.role in ("a", "b") else "Observer"
    return JoinResponse(
        token=mint_token(core.cfg, room.id, p, name=name),
        livekit_url=public_livekit_url(request, core.cfg),
        identity=p.id,
        room=room.id,
        role=body.role,
        lang=lang,
        mode=mode.model_dump(mode="json"),
        session_id=session.id,
        path=path,
    )
