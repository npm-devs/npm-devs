# dashboard

React + Vite + TypeScript (ADR 0010), una sola página (D34). Consume solo las rutas públicas `/v1/*` de la Domain API: `/v1/fleet`, `/v1/trips/{trip_id}` y `/v1/replay`, con polling cada 2 s. Los tipos están en `src/api/types.ts` (generados del contrato `backend/openapi.json`).

Este ticket (#21) cubre: barra de estado del replay (solo lectura), lista de vehículos y viajes, mapa MapLibre con la posición actual de cada vehículo, procedencia y estados de carga, vacío, error y desactualizado. Alertas y consumo (#22) y controles del replay (#23) vienen después.

## Correr con el servidor simulado

```sh
npm ci
../backend/scripts/mock-api.sh     # Prism en http://127.0.0.1:4010 (otra terminal)
npm run dev
```

## Variables (build)

| Variable | Por defecto | Uso |
| --- | --- | --- |
| `VITE_API_URL` | `http://127.0.0.1:4010` | Base de la Domain API |
| `VITE_MAP_STYLE` | CARTO dark-matter | URL de estilo MapLibre; `empty` = estilo vacío local (tests) |
| `VITE_MAPBOX_TOKEN` (o `NEXT_PUBLIC_MAPBOX_TOKEN`) | — | Token público de Mapbox (`pk.…`), restringido por URL en la cuenta. Con él el mapa usa Mapbox Standard de noche; sin él, MapLibre (CI y Playwright). Va en `dashboard/.env` (ignorado por git) o en las variables de Vercel, nunca en el repo |

## Mapa (docs/dev/plan-mapa-mapbox.md)

- `src/map/engine.ts` elige el motor; `src/map/motion.ts` (puro, con tests en Vitest) mueve cada camión sobre la vía real de su corredor (`GET /v1/corridors`) a su velocidad por la del replay, con el rumbo de la vía.
- Al elegir un tráiler la cámara vuela hasta él, se inclina a 60° mirando hacia donde va y lo sigue. Arrastrar o hacer zoom con la rueda suelta el seguimiento; "Seguir" lo retoma y "Ver flota" vuelve a la vista general. Con movimiento reducido, salta sin animar.
- El camión se acuesta sobre la pista y escala a su tamaño real (~18 m) con zoom alto.

## Nota revisada del operador (Atomic Memory, opcional)

Al elegir un vehículo, si el backend tiene notas configuradas (ver `backend/README.md`), aparece la nota revisada del operador sobre ese vehículo. Pide la clave de notas del operador una vez por pestaña (solo queda en memoria); guardar exige marcar el consentimiento y se puede corregir o borrar. Sin configuración en el backend no aparece nada.

## Tests

```sh
npx playwright install chromium   # una vez
npm test                          # vitest (src/**/*.test.ts) y luego Playwright (tests/)
```

Playwright levanta el servidor simulado (Prism) y el build servido con `vite preview` (con `VITE_API_URL` apuntando al simulado y mapa vacío), y sobrescribe respuestas con `page.route` para los estados vacío, carga, error y desactualizado.

## Desplegar en Vercel (ADR 0011)

1. Importar el repo en Vercel con **Root Directory** `dashboard/` (usa `vercel.json`: Vite, salida `dist`, rewrite SPA).
2. Definir `VITE_API_URL` con la URL pública de la Domain API.
3. Agregar el dominio del dashboard a `CORS_ORIGINS` del backend (los previews `*.vercel.app` ya se permiten).
