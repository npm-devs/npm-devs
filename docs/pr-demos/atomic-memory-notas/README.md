# Demo: nota revisada del operador (Atomic Memory)

- `screenshot.png` (1280×800): el dashboard real, con T07 elegido, tras recargar y volver a ingresar la clave de notas; la nota guardada antes se recupera.
- `demo.webm` (≈20 s): elegir T07 → panel bloqueado → clave de notas → guardar con consentimiento → recargar (la clave se olvida) → desbloquear y recordar → corregir → borrar.

**Fixture:** el backend real (FastAPI) apunta a `backend/tests/fake_atomicmemory.py`, un servidor HTTP local que imita las rutas de atomicmemory-core. No es Atomic Memory real. Las lecturas de flota vienen del servidor simulado del contrato (Prism) y la clave de notas es de demo.
