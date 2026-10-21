import { useEffect, useState } from "react";
import { deleteVehicleNote, NoteError, readVehicleNote, saveVehicleNote, type VehicleNote } from "../api/client";
import { horaLima } from "../format";

type Estado =
  | { fase: "cargando" }
  | { fase: "bloqueada"; error?: string }
  | { fase: "lista"; data: VehicleNote }
  | { fase: "sin-lectura"; error: string };

type Props = {
  alias: string;
  /** Clave de notas que el operador escribió en esta pestaña; nunca se guarda. */
  notesKey: string | null;
  onNotesKey: (key: string | null) => void;
};

const mensaje = (e: unknown, porDefecto: string) => (e instanceof Error ? e.message : porDefecto);

/**
 * Nota revisada sobre el vehículo elegido, en la memoria compartida del operador de la flota (Atomic Memory).
 * Leer, guardar y borrar exige la clave de notas del operador; guardar exige además su consentimiento.
 * Si el servidor no tiene la función configurada, no se muestra nada.
 */
export function VehicleNotePanel({ alias, notesKey, onNotesKey }: Props) {
  const [estado, setEstado] = useState<Estado>({ fase: "cargando" });
  const [editando, setEditando] = useState(false);
  const [texto, setTexto] = useState("");
  const [consiente, setConsiente] = useState(false);
  const [clave, setClave] = useState("");
  const [ocupado, setOcupado] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let vivo = true;
    readVehicleNote(alias, notesKey)
      .then((data) => vivo && setEstado({ fase: "lista", data }))
      .catch((e) => {
        if (!vivo) return;
        if (e instanceof NoteError && e.status === 401) setEstado({ fase: "bloqueada" });
        else setEstado({ fase: "sin-lectura", error: mensaje(e, "No se pudo leer la nota") });
      });
    return () => {
      vivo = false;
    };
  }, [alias, notesKey]);

  if (estado.fase === "cargando") return null;
  if (estado.fase === "lista" && !estado.data.habilitada) return null;

  const cabecera = (
    <header className="nota-head">
      <h3>Nota revisada · {alias}</h3>
      <span className="chip chip-human_reported">Memoria del operador</span>
    </header>
  );

  if (estado.fase === "bloqueada") {
    const desbloquear = async () => {
      setOcupado(true);
      try {
        setEstado({ fase: "lista", data: await readVehicleNote(alias, clave) });
        onNotesKey(clave);
        setClave("");
      } catch (e) {
        const incorrecta = e instanceof NoteError && e.status === 401;
        setEstado({ fase: "bloqueada", error: incorrecta ? "Clave de notas incorrecta." : mensaje(e, "No se pudo leer la nota") });
      } finally {
        setOcupado(false);
      }
    };
    return (
      <section className="nota" aria-label={`Nota revisada de ${alias}`}>
        {cabecera}
        <form
          className="nota-form"
          onSubmit={(e) => {
            e.preventDefault();
            void desbloquear();
          }}
        >
          <p className="dato">Notas compartidas del operador de la flota. Ingresa la clave de notas; solo queda en esta pestaña.</p>
          <div className="nota-acciones">
            <input
              type="password"
              aria-label="Clave de notas del operador"
              autoComplete="off"
              value={clave}
              onChange={(e) => setClave(e.target.value)}
            />
            <button type="submit" disabled={!clave || ocupado}>Desbloquear</button>
          </div>
        </form>
        {estado.error && <p className="nota-error" role="alert">{estado.error}</p>}
      </section>
    );
  }

  if (estado.fase === "sin-lectura") {
    return (
      <section className="nota" aria-label={`Nota revisada de ${alias}`}>
        {cabecera}
        <p className="nota-error" role="alert">{estado.error}</p>
      </section>
    );
  }

  const nota = estado.data.nota;
  const key = notesKey ?? "";

  const abrir = () => {
    setTexto(nota?.texto ?? "");
    setConsiente(false);
    setError(null);
    setEditando(true);
  };

  const ejecutar = async (accion: () => Promise<VehicleNote>) => {
    setOcupado(true);
    setError(null);
    try {
      setEstado({ fase: "lista", data: await accion() });
      setEditando(false);
    } catch (e) {
      if (e instanceof NoteError && e.status === 401) {
        onNotesKey(null); // la clave cambió en el servidor: se vuelve a pedir
        return;
      }
      setError(mensaje(e, "No se guardó la nota"));
    } finally {
      setOcupado(false);
    }
  };

  return (
    <section className="nota" aria-label={`Nota revisada de ${alias}`}>
      {cabecera}

      {!editando && nota && (
        <>
          <p className="nota-texto">{nota.texto}</p>
          <p className="dato">
            Atomic Memory{nota.guardada_en ? ` · ${horaLima(nota.guardada_en)}` : ""}
          </p>
          <div className="nota-acciones">
            <button type="button" onClick={abrir} disabled={ocupado}>Corregir</button>
            <button type="button" onClick={() => ejecutar(() => deleteVehicleNote(alias, key))} disabled={ocupado}>Borrar</button>
            <button type="button" className="nota-bloquear" onClick={() => onNotesKey(null)}>Bloquear</button>
          </div>
        </>
      )}

      {!editando && !nota && (
        <div className="nota-acciones">
          <span className="dato">Sin nota revisada para {alias}.</span>
          <button type="button" onClick={abrir}>Agregar nota</button>
        </div>
      )}

      {editando && (
        <form
          className="nota-form"
          onSubmit={(e) => {
            e.preventDefault();
            void ejecutar(() => saveVehicleNote(alias, key, texto.trim()));
          }}
        >
          <label>
            <span className="dato">Lo que revisaste (sin datos personales)</span>
            <textarea value={texto} maxLength={500} rows={3} onChange={(e) => setTexto(e.target.value)} />
          </label>
          <label className="nota-consentimiento">
            <input type="checkbox" checked={consiente} onChange={(e) => setConsiente(e.target.checked)} />
            Revisé la nota y autorizo guardarla en Atomic Memory
          </label>
          <div className="nota-acciones">
            <button type="submit" disabled={!consiente || !texto.trim() || ocupado}>
              {ocupado ? "Guardando…" : "Guardar nota"}
            </button>
            <button type="button" onClick={() => setEditando(false)} disabled={ocupado}>Cancelar</button>
          </div>
        </form>
      )}

      {error && <p className="nota-error" role="alert">{error}</p>}
    </section>
  );
}
