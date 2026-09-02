"""Station presence API — self-registration and heartbeat.

Stations announce themselves; nothing here is provisioned by hand. That keeps
"which machines can capture, and what is plugged into them" a live fact instead
of a wiki page that rots.

There is no command endpoint here on purpose. This router makes stations
*visible*; making them *drivable* from the cluster is meklab/home#178, whose
transport (server-to-agent vs agent-polling) is still undecided.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from vinyl_server.db.models import Station
from vinyl_server.db.session import get_session
from vinyl_server.schemas import StationHeartbeat, StationOut, StationRegister

router = APIRouter(prefix="/api/stations", tags=["stations"])

#: A station is "online" if it has been heard from within this window. Chosen as
#: a small multiple of the agent's heartbeat interval so one dropped beat (a
#: blipped Wi-Fi link, a slow API call) does not flap the station offline.
ONLINE_WINDOW = timedelta(seconds=180)


def _out(station: Station, *, now: datetime | None = None) -> StationOut:
    now = now or datetime.now(UTC)
    seen = station.last_seen_at
    online = seen is not None and (now - seen) <= ONLINE_WINDOW
    return StationOut.model_validate({
        **{c.name: getattr(station, c.name) for c in Station.__table__.columns},
        "online": online,
    })


@router.put("/{station_id}", response_model=StationOut)
def register_station(
    station_id: str, body: StationRegister, session: Session = Depends(get_session)
):
    """Idempotent registration.

    PUT rather than POST because the agent calls this on every start with the
    same station_id: a restart, a redeploy, or a crash loop must update the row,
    not 409. The station_id is the identity, so this is a upsert by definition.
    """
    station = session.get(Station, station_id)
    if station is None:
        station = Station(station_id=station_id)
        session.add(station)
    station.display_name = body.display_name
    station.hostname = body.hostname
    station.capabilities = body.capabilities
    station.agent_version = body.agent_version
    # Registration is itself proof of life, so the station is online the moment
    # it registers rather than one heartbeat interval later.
    station.last_seen_at = datetime.now(UTC)
    session.flush()
    return _out(station)


@router.post("/{station_id}/heartbeat", response_model=StationOut)
def heartbeat(
    station_id: str, body: StationHeartbeat, session: Session = Depends(get_session)
):
    """Liveness plus a little state.

    404s an unknown station rather than creating one: a heartbeat carries no
    capabilities, so autovivifying here would produce a station row that claims
    no hardware. The agent treats a 404 as "re-register", which self-heals the
    case where the row was deleted while the agent kept running.
    """
    station = session.get(Station, station_id)
    if station is None:
        raise HTTPException(404, f"no such station: {station_id} — register first")
    station.phase = body.phase
    station.current_side_id = body.current_side_id
    station.pending_uploads = body.pending_uploads
    station.last_seen_at = datetime.now(UTC)
    session.flush()
    return _out(station)


@router.get("", response_model=list[StationOut])
def list_stations(session: Session = Depends(get_session)):
    stations = session.scalars(select(Station).order_by(Station.station_id)).all()
    now = datetime.now(UTC)
    return [_out(s, now=now) for s in stations]


@router.get("/{station_id}", response_model=StationOut)
def get_station(station_id: str, session: Session = Depends(get_session)):
    station = session.get(Station, station_id)
    if station is None:
        raise HTTPException(404, f"no such station: {station_id}")
    return _out(station)
