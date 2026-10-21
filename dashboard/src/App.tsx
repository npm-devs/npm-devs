import { useState } from "react";
import { readFreights, readSnapshot } from "./api/client";
import { FleetMap } from "./components/FleetMap";
import { Freights } from "./components/Freights";
import { Num } from "./components/Num";
import { ProvenanceChips } from "./components/Provenance";
import { VehicleList } from "./components/VehicleList";
import { VehicleNotePanel } from "./components/VehicleNote";
import { horaLima } from "./format";
import { usePolling } from "./usePolling";

const entero = (v: number) => Math.round(v).toString();

/** El símbolo de Certera: seis señales que entran y salen como una sola dirección. */
function Simbolo() {
  const tinta = [[0, 0], [1, 1], [0, 2], [1, 2], [1, 3], [0, 4]];
  const lima = [[2, 2], [3, 2], [4, 2]];
  return (
    <svg className="simbolo" viewBox="0 0 100 100" aria-hidden="true">
      {tinta.map(([c, r], i) => <rect key={i} x={c * 20 + 1} y={r * 20 + 1} width="18" height="18" style={{ "--i": i } as React.CSSProperties} />)}
      {lima.map(([c, r], i) => <rect key={i} className="sale" x={c * 20 + 1} y={r * 20 + 1} width="18" height="18" style={{ "--i": i + 6 } as React.CSSProperties} />)}
    </svg>
  );
}

function Skeleton() {
  return (
    <div className="skeleton" aria-hidden="true">
      {Array.from({ length: 5 }, (_, i) => <span key={i} style={{ "--i": i } as React.CSSProperties} />)}
    </div>
  );
}

export function App() {
  const poll = usePolling(readSnapshot, 2000);
  const freights = usePolling(readFreights, 5000);
  const [selected, setSelected] = useState<string | null>(null);
  // Clave de notas del operador: solo en memoria, se pierde al recargar.
  const [notesKey, setNotesKey] = useState<string | null>(null);
  const [view, setView] = useState<"operacion" | "fletes">("operacion");
  const snap = poll.data;
  const vehicles = snap?.fleet.data.vehiculos ?? [];
  const playing = snap?.replay.data.estado === "playing";

  return (
    <div className="app">
      <header className="top">
        <div className="marca">
          <Simbolo />
          <h1>Certera</h1>
        </div>
        <nav className="tabs" role="tablist" aria-label="Vistas" data-view={view}>
          <span className="tabs-ind" aria-hidden="true" />
          <button type="button" role="tab" aria-selected={view === "operacion"} onClick={() => setView("operacion")}>Operación</button>
          <button type="button" role="tab" aria-selected={view === "fletes"} onClick={() => setView("fletes")}>Fletes</button>
        </nav>
        {snap && (
          <div className="replaybar" aria-label="Estado del replay">
            <span className={`live replay-${snap.replay.data.estado}`}>
              <i aria-hidden="true" />
              {playing ? "Replay en curso" : "Replay en pausa"}
            </span>
            <span className="dato">Hora de replay {horaLima(snap.replay.data.hora_replay)}</span>
            <span className="dato">Velocidad {snap.replay.data.velocidad}x</span>
            <ProvenanceChips items={snap.fleet.provenance} />
          </div>
        )}
      </header>
      <p className="banner" role="note">
        Replay de una flota simulada sobre carreteras reales de Perú: telemetría, fletes, conductores y mensajes sintéticos. No es tiempo real.
      </p>

      {view === "fletes" && (
        <div className="vista" key="fletes">
          {freights.status === "error" && <p className="alert" role="alert">No se pudo leer la Domain API</p>}
          {freights.status === "loading" && <div className="state"><p>Cargando…</p><Skeleton /></div>}
          {freights.data && <Freights board={freights.data} />}
        </div>
      )}

      {view === "operacion" && (
        <div className="vista" key="operacion">
          {poll.status === "error" && <p className="alert" role="alert">No se pudo leer la Domain API</p>}
          {poll.status === "loading" && <div className="state"><p>Cargando…</p><Skeleton /></div>}
          {snap && vehicles.length === 0 && <p className="state vacio">Sin vehículos en el replay</p>}
          {snap && vehicles.length > 0 && (
            <main className="grid">
              <section className="lateral" aria-label="Vehículos y viajes">
                <h2>Vehículos</h2>
                <dl className="resumen">
                  <div><dt>vehículos</dt><dd><Num value={snap.fleet.data.total_vehiculos} format={entero} /></dd></div>
                  <div><dt>viajes en curso</dt><dd><Num value={snap.fleet.data.viajes_en_curso} format={entero} /></dd></div>
                  <div className={snap.fleet.data.alertas_abiertas > 0 ? "hay-alertas" : ""}>
                    <dt>alertas abiertas</dt><dd><Num value={snap.fleet.data.alertas_abiertas} format={entero} /></dd>
                  </div>
                </dl>
                {selected && <VehicleNotePanel key={selected} alias={selected} notesKey={notesKey} onNotesKey={setNotesKey} />}
                <VehicleList snapshot={snap} selected={selected} onSelect={setSelected} />
              </section>
              <section className="mapa" aria-label="Mapa">
                <FleetMap vehicles={vehicles} trips={snap.trips} replay={snap.replay.data} selected={selected} onSelect={setSelected} />
              </section>
            </main>
          )}
        </div>
      )}
    </div>
  );
}
