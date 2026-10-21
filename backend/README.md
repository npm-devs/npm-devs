# backend · Domain API

FastAPI. Única pieza con lógica de negocio y única que toca la base de datos (ADR 0004). Dueño: Juan.

## Correr

```sh
uv sync
DATABASE_URL=postgresql://... uv run uvicorn app.main:create_app --factory --reload
```

## Tests

```sh
uv run pytest
```

Levantan un Postgres 16 + PostGIS en Docker (testcontainers), así que necesitan Docker corriendo. Prueban la API por HTTP, que es el punto de prueba principal del MVP: comportamiento externo, no detalles internos.

## Contrato (OpenAPI)

El contrato vive en `backend/openapi.json`, exportado de la app FastAPI (con datos de ejemplo estáticos; todavía no lee la base). Todas las respuestas van en un sobre:

```json
{ "data": {}, "provenance": ["fead_observed"], "time_range": {"from": "...", "to": "..."}, "updated_at": "...", "is_replay": true }
```

`time_range` es hora del evento (UTC). `provenance` usa los valores del glosario (`fead_observed`, `synthetic_demo`, `human_reported`, `external_enrichment`, `model_inferred`).

| Lectura pública (dashboard) | Tool (exige `X-API-Key`) |
|---|---|
| `GET /v1/fleet` | `GET /v1/tools/get_fleet_overview` |
| `GET /v1/vehicles/{alias}` | `GET /v1/tools/get_vehicle_status?alias=` |
| `GET /v1/trips/{trip_id}` | `GET /v1/tools/get_trip_status?trip_id=` |
| `GET /v1/trips/{trip_id}/fuel` | `GET /v1/tools/get_fuel_analysis?trip_id=` |
| `GET /v1/trips/{trip_id}/bottlenecks` | `GET /v1/tools/get_operational_bottlenecks?trip_id=` |
| `GET /v1/alerts` | `GET /v1/tools/get_alerts` |
| `GET /v1/replay` (estado) | `POST /v1/tools/control_replay` (`action`: play, pause, seek, set_speed; `speed`; `to_event_time`) |

Ids de ejemplo: vehículos `T03`, `T07`, `T28`, `T04`, `T22`, `T09`; viajes `V-001` (con anomalía y alerta) y `V-002`.

### Auth y CORS

- Las rutas `/v1/tools/*` comparan el header `X-API-Key` con la variable `API_KEY` (401 si falta o es incorrecta). Si `API_KEY` no está configurada responden 503: nunca quedan abiertas.
- Las lecturas `/v1/*` son públicas.
- CORS (ADR 0011): `CORS_ORIGINS` (lista separada por comas) más cualquier `https://*.vercel.app` (previews); permite el header `X-API-Key`.

### Notas revisadas (Atomic Memory, opcional)

El operador puede dejar una nota revisada por vehículo (p. ej. "sobreconsumo de T07 explicado por carga extra") y el dashboard la vuelve a mostrar al elegir ese vehículo. Se guarda en [Atomic Memory](https://docs.atomicstrata.ai) y nunca entra en la línea base, el sobreconsumo ni las alertas.

| Variable | Uso |
|---|---|
| `ATOMICMEMORY_API_URL` | URL HTTPS de Atomic Memory / atomicmemory-core (sin `/v1`; HTTP solo en loopback, sin credenciales en la URL) |
| `ATOMICMEMORY_API_KEY` | Clave del proyecto (`Authorization: Bearer`), solo en el servidor |
| `ATOMICMEMORY_SUBJECT` | Sujeto de la memoria, p. ej. `certera-flota-demo`. Lo fija el servidor; el navegador no lo elige |
| `NOTES_API_KEY` | Clave del operador para leer, guardar y borrar notas (`X-Notes-Key`). Usa una distinta de `API_KEY` |
| `ATOMICMEMORY_TIMEOUT_MS` | Tiempo máximo por llamada (por defecto 2500, acotado entre 100 y 5000) |

- Sin `ATOMICMEMORY_API_URL`, `ATOMICMEMORY_SUBJECT` o `NOTES_API_KEY`, `GET /v1/vehicles/{alias}/note` responde `habilitada: false` y el dashboard no muestra nada.
- Es **memoria compartida del operador de la flota**: un solo sujeto por despliegue, protegido con `NOTES_API_KEY`. No separa notas por persona. El dashboard pide la clave en pantalla y la guarda solo en memoria de la pestaña; no va en variables `VITE_*`.
- `GET`/`PUT`/`DELETE /v1/vehicles/{alias}/note` exigen `X-Notes-Key` (401 si falta o es incorrecta). `PUT` exige además `consentimiento: true` y corrige la nota en su lugar (ingesta verbatim con `metadata.externalId`); `DELETE` la borra por id.
- Si Atomic Memory falla, tarda más del límite o devuelve algo inválido, la API responde 503 con "No se guardó/leyó/borró la nota" y el resto de la API sigue igual.
- Los redireccionamientos del proveedor se rechazan para evitar reenviar la credencial. El borrado se confirma leyendo de nuevo hasta obtener ausencia.
- Tests: `tests/test_vehicle_notes.py` usa `tests/fake_atomicmemory.py`, un **fixture HTTP local** que imita las rutas de atomicmemory-core; no es el servicio real.

### Regenerar contrato y tipos

```sh
backend/scripts/gen-api.sh
```

Exporta `openapi.json` y genera los tipos con `openapi-typescript` en `mcp/src/api/types.ts`, `zavu/api/types.ts` y `dashboard/src/api/types.ts`. Si cambias rutas o modelos sin regenerar, `tests/test_openapi.py` falla. Versiona la salida.

### Servidor simulado

```sh
backend/scripts/mock-api.sh   # Prism en http://127.0.0.1:4010 (PORT para cambiarlo)
```

Sirve los ejemplos del `openapi.json`; no valida `X-API-Key`.

## Desplegar en GCP

```sh
tools/wizard-gcp.sh
```

Asistente paso a paso (#10): proyecto y facturación, Cloud SQL Postgres 16 + PostGIS, Secret Manager, imagen en Artifact Registry y Cloud Run con una sola instancia (ADR 0005). Es idempotente: volver a correrlo reutiliza lo que existe y redespliega la imagen del commit actual. Los valores locales quedan en `.env.gcp`, ignorado por git.

En Cloud Run, el backend recibe `DATABASE_URL` (conexión por socket a Cloud SQL) y `API_KEY` desde Secret Manager.

## Datos y replay

Las rutas de vehículo, flota y replay leen de Postgres; al arrancar con `DATABASE_URL` la app aplica las migraciones de `migrations/` (registradas en `schema_migrations`) y el replay queda en pausa. Para cargar la salida del pipeline de `data/` (nunca se copia al repositorio):

```bash
uv run python scripts/load_demo_data.py --source ../data/out --database-url "$DATABASE_URL"
```

Es idempotente (recarga desde cero). Días 1–12 van a `telemetry_reading` como historia y los días 13–19 a `telemetry_source`, que el replay reproduce.
