import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Callable, Literal

import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from psycopg_pool import PoolTimeout

from . import examples as ex
from . import corridors as corridors_mod
from . import freights as freights_mod
from . import fuel as fuel_mod
from . import metrics as metrics_mod
from . import notes as notes_mod
from . import reads
from .db import STATEMENT_TIMEOUT_MS, close_pools, migrate, read_url
from .replay import Clock, ReplayEngine, system_clock
from .schemas import (
    AlertList, Bottlenecks, CorridorList, ControlReplayRequest, Envelope, ErrorBody, FleetOverview, FreightBoard, FuelAnalysis,
    NotaRevisada, Provenance, ReplayState, SaveVehicleNoteRequest, TimeRange, TripStatus, VehicleMetrics, VehicleNote,
    VehicleStatus,
)

# Previews y producción del dashboard y la landing en Vercel (ADR 0011).
VERCEL_ORIGIN_REGEX = r"^https://[a-z0-9-]+\.vercel\.app$"

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Clave de la Domain API (variable API_KEY)")
notes_key_header = APIKeyHeader(
    name="X-Notes-Key", auto_error=False, scheme_name="NotesKey",
    description="Clave del operador para las notas revisadas (variable NOTES_API_KEY); el dashboard la pide en pantalla",
)


def require_api_key(request: Request, key: str | None = Security(api_key_header)) -> None:
    expected: str | None = request.app.state.api_key
    if not expected:  # nunca quedan abiertas si falta la configuración
        raise HTTPException(status_code=503, detail="API_KEY no está configurada en el servidor")
    if key is None or not hmac.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="X-API-Key ausente o incorrecta")


def envelope(data, provenance: list[Provenance], time_range: TimeRange, updated_at: datetime = ex.UPDATED_AT) -> dict:
    return Envelope(
        data=data, provenance=provenance, time_range=time_range, updated_at=updated_at, is_replay=True
    ).model_dump(mode="json", by_alias=True)


def example_response(model, body: dict) -> dict:
    return {
        200: {"model": model, "content": {"application/json": {"example": body}}},
    }


FREIGHT_PROVENANCE = [Provenance.synthetic_demo, Provenance.model_inferred]

NOT_FOUND = {404: {"model": ErrorBody, "description": "Alias o viaje desconocido"}}
AUTH_ERRORS = {
    401: {"model": ErrorBody, "description": "X-API-Key ausente o incorrecta"},
    503: {"model": ErrorBody, "description": "API_KEY no configurada en el servidor"},
}


def _trip_id(trip_id: str) -> str:
    if trip_id not in ex.TRIPS:
        raise HTTPException(status_code=404, detail=f"Viaje desconocido: {trip_id}")
    return trip_id


def create_app(
    database_url: str | None = None,
    api_key: str | None = None,
    cors_origins: list[str] | None = None,
    clock: Clock = system_clock,
    tick_interval: float = 1.0,
    statement_timeout_ms: int | None = None,
    notes: notes_mod.NotesMemory | None = None,
    notes_api_key: str | None = None,
) -> FastAPI:
    database_url = database_url or os.environ.get("DATABASE_URL")
    api_key = api_key or os.environ.get("API_KEY") or None
    notes = notes or notes_mod.NotesMemory.from_env()
    notes_api_key = notes_api_key or os.environ.get("NOTES_API_KEY") or None
    if cors_origins is None:
        cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]

    # Las lecturas van por un pool con statement_timeout; el replay y las migraciones usan la URL tal cual
    # porque un seek largo o una migración pueden pasar del timeout.
    statement_timeout_ms = statement_timeout_ms or int(os.environ.get("STATEMENT_TIMEOUT_MS", STATEMENT_TIMEOUT_MS))
    reads_url = read_url(database_url, statement_timeout_ms) if database_url else None
    engine = ReplayEngine(database_url, clock, reads_url) if database_url else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = None
        if engine:
            await asyncio.to_thread(migrate, database_url)  # el replay arranca en pausa
            task = asyncio.create_task(engine.run(tick_interval))
        yield
        if task:
            task.cancel()
        close_pools()

    app = FastAPI(
        lifespan=lifespan,
        title="Certera · Domain API",
        version="0.1.0",
        description=(
            "Contrato de la Domain API con datos de ejemplo. Toda respuesta va en un sobre con "
            "procedencia, rango de tiempo (hora del evento, UTC) y hora de última actualización. "
            "Las rutas `/v1/tools/*` (MCP, Zavu y control del replay) exigen `X-API-Key`; "
            "las lecturas `/v1/*` del dashboard son públicas, salvo las notas revisadas "
            "(`/v1/vehicles/{alias}/note`, opcionales), que exigen `X-Notes-Key`."
        ),
    )
    app.state.api_key = api_key
    app.state.database_url = database_url
    app.state.replay_engine = engine

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=VERCEL_ORIGIN_REGEX,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["X-API-Key", "X-Notes-Key", "Content-Type"],
    )

    # Base saturada: fallar rápido con 503 libera la instancia; esperar sin fin es lo que la tumbó (test_db_busy).
    @app.exception_handler(PoolTimeout)
    @app.exception_handler(psycopg.errors.QueryCanceled)
    async def database_busy(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "La base de datos está ocupada; reintenta en unos segundos"})

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        if not database_url:
            raise HTTPException(status_code=503, detail="database unavailable: DATABASE_URL not set")
        try:
            with psycopg.connect(database_url, connect_timeout=5) as conn:
                postgis = conn.execute("SELECT postgis_version()").fetchone()[0]
        except psycopg.Error as exc:
            raise HTTPException(status_code=503, detail=f"database unavailable: {exc.__class__.__name__}")
        return {"status": "ok", "postgis": postgis}

    # --- Handlers compartidos por las rutas públicas y las de tools ---

    def need_engine() -> ReplayEngine:
        if engine is None:
            raise HTTPException(status_code=503, detail="database unavailable: DATABASE_URL not set")
        return engine

    def telemetry() -> list[Provenance]:
        """Procedencia de la telemetría cargada: synthetic_demo con la flota de Perú (ADR 0012)."""
        return [Provenance(p) for p in reads.telemetry_provenance(reads_url)] or [Provenance.synthetic_demo]

    def real() -> list[Provenance]:
        return list(dict.fromkeys([*telemetry(), Provenance.synthetic_demo]))

    def fleet() -> dict:
        need_engine()
        data, desde, hasta, updated = reads.fleet_overview(reads_url)
        return envelope(data, real(), TimeRange(from_=desde, to=hasta), updated)

    def vehicle(alias: str) -> dict:
        need_engine()
        found = reads.vehicle_status(reads_url, alias)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Vehículo desconocido: {alias}")
        data, desde, hasta, updated = found
        return envelope(data, real(), TimeRange(from_=desde, to=hasta), updated)

    def trip(trip_id: str) -> dict:
        need_engine()
        found = fuel_mod.trip_status(reads_url, trip_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Viaje desconocido: {trip_id}")
        data, desde, hasta, updated = found
        return envelope(data, real(), TimeRange(from_=desde, to=hasta), updated)

    def fuel(trip_id: str) -> dict:
        need_engine()
        found = fuel_mod.fuel_analysis(reads_url, trip_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Viaje desconocido: {trip_id}")
        data, desde, hasta, updated = found
        return envelope(data, [*real(), Provenance.model_inferred], TimeRange(from_=desde, to=hasta), updated)

    def metrics(alias: str, desde: date | None, hasta: date | None, agrupar_por: str) -> dict:
        need_engine()
        if desde and hasta and desde > hasta:
            raise HTTPException(status_code=422, detail="desde no puede ser posterior a hasta")
        found = metrics_mod.vehicle_metrics(reads_url, alias, desde, hasta, agrupar_por)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Vehículo desconocido: {alias}")
        data, inicio, fin, updated = found
        return envelope(data, [*real(), Provenance.model_inferred], TimeRange(from_=inicio, to=fin), updated)

    def bottlenecks(trip_id: str) -> dict:
        t = ex.TRIPS[_trip_id(trip_id)]
        return envelope(ex.BOTTLENECKS[trip_id], ex.PROVENANCE_ALERTS, TimeRange(from_=t.inicio, to=ex.NOW))

    def alerts() -> dict:
        return envelope(AlertList(alertas=[ex.ALERTA]), ex.PROVENANCE_ALERTS, TimeRange(from_=ex.T0, to=ex.NOW))

    def replay_state() -> dict:
        r, updated = need_engine().state()
        return envelope(r, telemetry(), TimeRange(from_=r.inicio, to=r.fin), updated)

    def corridors() -> dict:
        r, updated = need_engine().state()
        data = corridors_mod.corridor_list(reads_url)
        return envelope(data, [Provenance.external_enrichment], TimeRange(from_=r.inicio, to=r.fin), updated)

    def freights() -> dict:
        need_engine()
        data, desde, hasta, updated = freights_mod.freight_board(reads_url)
        return envelope(data, FREIGHT_PROVENANCE, TimeRange(from_=desde, to=hasta), updated)

    # Ejemplos del contrato (OpenAPI y mock): no dependen de la base.
    v_ex = ex.VEHICLE_STATUS["T03"]
    ex_fleet = envelope(ex.FLEET, ex.PROVENANCE, TimeRange(from_=ex.T0, to=ex.NOW))
    ex_vehicle = envelope(v_ex, ex.PROVENANCE, TimeRange(from_=ex.T0, to=v_ex.hora_evento))
    ex_trip_t = ex.TRIPS["V-001"]
    ex_trip = envelope(ex_trip_t, ex.PROVENANCE, TimeRange(from_=ex_trip_t.inicio, to=ex.NOW))
    ex_fuel = envelope(ex.FUEL["V-001"], ex.PROVENANCE, TimeRange(from_=ex_trip_t.inicio, to=ex.NOW))
    ex_bottlenecks = bottlenecks("V-001")
    ex_alerts = alerts()
    r_ex = ex.INITIAL_REPLAY
    ex_replay = envelope(r_ex, [Provenance.synthetic_demo], TimeRange(from_=r_ex.inicio, to=r_ex.fin), ex.UPDATED_AT)
    ex_corridors = envelope(ex.CORRIDORS, [Provenance.external_enrichment], TimeRange(from_=r_ex.inicio, to=r_ex.fin), ex.UPDATED_AT)
    ex_freights = envelope(ex.FREIGHT_BOARD, FREIGHT_PROVENANCE, TimeRange(from_=ex.T0, to=ex.NOW))
    ex_metrics = envelope(ex.VEHICLE_METRICS, ex.PROVENANCE, TimeRange(from_=ex.REPLAY_START, to=ex.NOW))

    public = APIRouter(prefix="/v1", tags=["dashboard (público)"])
    tools = APIRouter(prefix="/v1/tools", tags=["tools (X-API-Key)"], dependencies=[Depends(require_api_key)])

    Alias = Annotated[str, Query(description="Alias del vehículo, p. ej. T03")]
    TripId = Annotated[str, Query(description="Identificador del viaje, p. ej. V-001")]
    Desde = Annotated[date | None, Query(description="Primer día (YYYY-MM-DD, hora de Lima); por defecto, el primer viaje")]
    Hasta = Annotated[date | None, Query(description="Último día (YYYY-MM-DD, hora de Lima); se recorta a la hora de replay")]
    AgruparPor = Annotated[Literal["dia", "viaje"], Query(description="Una fila por día o por viaje")]

    def add(router: APIRouter, path: str, op: str, fn: Callable, model, example: dict, summary: str, errors: dict):
        router.add_api_route(
            path, fn, methods=["GET"], operation_id=op, summary=summary,
            response_model=Envelope[model], responses={**example_response(Envelope[model], example), **errors},
        )

    # Públicas (lecturas del dashboard)
    add(public, "/fleet", "read_fleet", lambda: fleet(), FleetOverview, ex_fleet, "Vista general de la flota", {})
    add(public, "/vehicles/{alias}", "read_vehicle", lambda alias: vehicle(alias), VehicleStatus, ex_vehicle, "Estado de un vehículo", NOT_FOUND)
    def read_metrics(alias: str, desde: Desde = None, hasta: Hasta = None, agrupar_por: AgruparPor = "dia"):
        return metrics(alias, desde, hasta, agrupar_por)

    add(public, "/vehicles/{alias}/metrics", "read_vehicle_metrics", read_metrics, VehicleMetrics, ex_metrics,
        "Métricas históricas de un vehículo por día o por viaje", NOT_FOUND)
    add(public, "/trips/{trip_id}", "read_trip", lambda trip_id: trip(trip_id), TripStatus, ex_trip, "Estado de un viaje", NOT_FOUND)
    add(public, "/trips/{trip_id}/fuel", "read_trip_fuel", lambda trip_id: fuel(trip_id), FuelAnalysis, ex_fuel, "Análisis de combustible de un viaje", NOT_FOUND)
    add(public, "/trips/{trip_id}/bottlenecks", "read_trip_bottlenecks", lambda trip_id: bottlenecks(trip_id), Bottlenecks, ex_bottlenecks, "Cuellos de botella de un viaje", NOT_FOUND)
    add(public, "/alerts", "read_alerts", lambda: alerts(), AlertList, ex_alerts, "Alertas enviadas al gestor de flota", {})
    add(public, "/freights", "read_freights", lambda: freights(), FreightBoard, ex_freights, "Tablero de fletes: ingresos, combustible y margen", {})
    add(public, "/corridors", "read_corridors", lambda: corridors(), CorridorList, ex_corridors,
        "Vía real de cada corredor (OpenStreetMap), para el mapa", {})
    add(public, "/replay", "read_replay", lambda: replay_state(), ReplayState, ex_replay, "Estado del replay", {})

    # Tools (MCP, Zavu): operationId = nombre de la tool
    def tool_vehicle(alias: Alias): return vehicle(alias)
    def tool_trip(trip_id: TripId): return trip(trip_id)
    def tool_fuel(trip_id: TripId): return fuel(trip_id)
    def tool_bottlenecks(trip_id: TripId): return bottlenecks(trip_id)
    def tool_metrics(alias: Alias, desde: Desde = None, hasta: Hasta = None, agrupar_por: AgruparPor = "dia"):
        return metrics(alias, desde, hasta, agrupar_por)

    add(tools, "/get_fleet_overview", "get_fleet_overview", lambda: fleet(), FleetOverview, ex_fleet, "Tool get_fleet_overview", AUTH_ERRORS)
    add(tools, "/get_vehicle_status", "get_vehicle_status", tool_vehicle, VehicleStatus, ex_vehicle, "Tool get_vehicle_status", {**NOT_FOUND, **AUTH_ERRORS})
    add(tools, "/get_trip_status", "get_trip_status", tool_trip, TripStatus, ex_trip, "Tool get_trip_status", {**NOT_FOUND, **AUTH_ERRORS})
    add(tools, "/get_fuel_analysis", "get_fuel_analysis", tool_fuel, FuelAnalysis, ex_fuel, "Tool get_fuel_analysis (desglose por tramo y sentido)", {**NOT_FOUND, **AUTH_ERRORS})
    add(tools, "/get_operational_bottlenecks", "get_operational_bottlenecks", tool_bottlenecks, Bottlenecks, ex_bottlenecks, "Tool get_operational_bottlenecks", {**NOT_FOUND, **AUTH_ERRORS})
    add(tools, "/get_vehicle_metrics", "get_vehicle_metrics", tool_metrics, VehicleMetrics, ex_metrics,
        "Tool get_vehicle_metrics (historial por día o por viaje, para gráficos)", {**NOT_FOUND, **AUTH_ERRORS})
    add(tools, "/get_alerts", "get_alerts", lambda: alerts(), AlertList, ex_alerts, "Tool get_alerts", AUTH_ERRORS)

    def control_replay(body: ControlReplayRequest) -> dict:
        eng = need_engine()
        if body.action == "play":
            eng.play()
        elif body.action == "pause":
            eng.pause()
        elif body.action == "set_speed":
            if body.speed is None:
                raise HTTPException(status_code=422, detail="set_speed requiere speed")
            eng.set_speed(body.speed)
        else:
            if body.to_event_time is None:
                raise HTTPException(status_code=422, detail="seek requiere to_event_time")
            r, _ = eng.state()
            if not r.inicio <= body.to_event_time <= r.fin:
                raise HTTPException(status_code=422, detail="to_event_time fuera del rango del replay")
            eng.seek(body.to_event_time)
        return replay_state()

    tools.add_api_route(
        "/control_replay", control_replay, methods=["POST"], operation_id="control_replay",
        summary="Tool control_replay (escritura)", response_model=Envelope[ReplayState],
        responses={**example_response(Envelope[ReplayState], ex_replay), **AUTH_ERRORS},
    )

    # Nota revisada (Atomic Memory, opcional): memoria compartida del operador de la flota, con un único sujeto que fija
    # el servidor. Leer, guardar y borrar exige X-Notes-Key (NOTES_API_KEY, distinta de API_KEY); el dashboard la pide
    # al operador y la guarda solo en memoria. Nunca toca alertas, línea base ni sobreconsumo.
    notas = APIRouter(prefix="/v1/vehicles/{alias}/note", tags=["notas revisadas (Atomic Memory)"])
    AliasPath = Annotated[str, Path(pattern=r"^T\d{2}$", description="Alias del vehículo, p. ej. T07")]
    NotesKey = Annotated[str | None, Security(notes_key_header)]
    notes_enabled = notes is not None and bool(notes_api_key)
    NOTE_ERRORS = {
        401: {"model": ErrorBody, "description": "X-Notes-Key ausente o incorrecta"},
        503: {"model": ErrorBody, "description": "Notas desactivadas o Atomic Memory sin respuesta: no se leyó, guardó ni borró"},
    }
    ex_note = VehicleNote(habilitada=True, nota=NotaRevisada(
        alias="T07", texto="Sobreconsumo en Lima–Arequipa revisado: carga 4 t sobre lo declarado. Sin acción.",
        guardada_en=ex.NOW)).model_dump(mode="json")

    def operator_store(key: str | None, action: str) -> notes_mod.NotesMemory:
        if not notes_enabled:
            raise HTTPException(status_code=503, detail=f"No se {action}: las notas revisadas están desactivadas en el servidor")
        if key is None or not hmac.compare_digest(key.encode(), notes_api_key.encode()):
            raise HTTPException(status_code=401, detail="Clave de notas del operador ausente o incorrecta")
        return notes

    def read_note(alias: AliasPath, key: NotesKey = None) -> dict:
        if not notes_enabled:  # apagada: no hay nada que proteger ni que mostrar
            return VehicleNote(habilitada=False, nota=None).model_dump(mode="json")
        store = operator_store(key, "leyó la nota")
        try:
            found = store.get(alias)
        except notes_mod.MemoryUnavailable as exc:
            raise HTTPException(status_code=503, detail=f"No se pudo leer la nota: {exc}")
        nota = NotaRevisada(alias=alias, texto=found.texto, guardada_en=found.guardada_en) if found else None
        return VehicleNote(habilitada=True, nota=nota).model_dump(mode="json")

    def save_note(alias: AliasPath, body: SaveVehicleNoteRequest, key: NotesKey = None) -> dict:
        store = operator_store(key, "guardó la nota")
        texto = body.texto.strip()
        if not texto:
            raise HTTPException(status_code=422, detail="La nota está vacía")
        try:
            store.save(alias, texto)
        except notes_mod.MemoryUnavailable as exc:
            raise HTTPException(status_code=503, detail=f"No se guardó la nota: {exc}")
        nota = NotaRevisada(alias=alias, texto=texto, guardada_en=datetime.now(UTC))
        return VehicleNote(habilitada=True, nota=nota).model_dump(mode="json")

    def delete_note(alias: AliasPath, key: NotesKey = None) -> dict:
        store = operator_store(key, "borró la nota")
        try:
            store.delete(alias)
        except notes_mod.MemoryUnavailable as exc:
            raise HTTPException(status_code=503, detail=f"No se borró la nota: {exc}")
        return VehicleNote(habilitada=True, nota=None).model_dump(mode="json")

    note_responses = {**example_response(VehicleNote, ex_note), **NOTE_ERRORS}
    notas.add_api_route("", read_note, methods=["GET"], operation_id="read_vehicle_note", response_model=VehicleNote,
                        summary="Nota revisada del operador sobre un vehículo (X-Notes-Key)", responses=note_responses)
    notas.add_api_route("", save_note, methods=["PUT"], operation_id="save_vehicle_note", response_model=VehicleNote,
                        summary="Guardar o corregir la nota revisada (X-Notes-Key y consentimiento)", responses=note_responses)
    notas.add_api_route("", delete_note, methods=["DELETE"], operation_id="delete_vehicle_note", response_model=VehicleNote,
                        summary="Borrar la nota revisada (X-Notes-Key)", responses={
                            **example_response(VehicleNote, VehicleNote(habilitada=True, nota=None).model_dump(mode="json")),
                            **NOTE_ERRORS})

    app.include_router(public)
    app.include_router(tools)
    app.include_router(notas)
    return app
