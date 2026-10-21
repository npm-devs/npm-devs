"""Nota revisada por el gestor de flota sobre un vehículo, guardada en Atomic Memory (opcional).

Es una ayuda de memoria del gestor: nunca entra en la línea base, el sobreconsumo ni las alertas.
Es memoria compartida del operador de la flota: un único sujeto (ATOMICMEMORY_SUBJECT) que fija el servidor y
que el navegador no elige; no aísla por persona. El acceso lo protege NOTES_API_KEY (ver main.py). Sin
ATOMICMEMORY_API_URL y ATOMICMEMORY_SUBJECT la función queda apagada.

Contrato HTTP de atomicmemory-core (docs.atomicstrata.ai/api-reference/http): ingesta verbatim con
`metadata.externalId` (reingerir el mismo id corrige el registro en su lugar), lectura por externalId
y borrado por id, siempre con `user_id` = sujeto del servidor.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from urllib import error, request
from urllib.parse import quote, urlencode, urlsplit

SOURCE_SITE = "certera-dashboard"
DEFAULT_TIMEOUT_MS = 2500
MIN_TIMEOUT_MS, MAX_TIMEOUT_MS = 100, 5000  # cada llamada queda acotada aunque la variable venga mal


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MemoryUnavailable(Exception):
    """Atomic Memory no respondió bien: la nota no se leyó, guardó o borró."""


@dataclass(frozen=True)
class StoredNote:
    texto: str
    guardada_en: datetime | None


def _stamp(found: dict) -> datetime | None:
    """Hora de guardado si viene bien formada; una fecha rara no impide mostrar la nota."""
    stamp = found.get("updated_at") or found.get("created_at")
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _prefix(alias: str) -> str:
    return f"Vehículo {alias} · nota revisada por el gestor de flota: "


class NotesMemory:
    def __init__(self, api_url: str, subject: str, api_key: str | None = None, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        parsed = urlsplit(api_url)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("ATOMICMEMORY_API_URL inválida")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("Atomic Memory requiere HTTPS, salvo en loopback")
        self.api_url = api_url.rstrip("/")
        self._http = request.build_opener(NoRedirect())
        self.subject = subject
        self.api_key = api_key
        self.timeout = min(max(timeout_ms, MIN_TIMEOUT_MS), MAX_TIMEOUT_MS) / 1000
        # externalId con el sujeto (hash) para que dos gestores nunca compartan registro.
        self._ns = hashlib.sha256(subject.encode()).hexdigest()[:16]

    @classmethod
    def from_env(cls) -> "NotesMemory | None":
        url = os.environ.get("ATOMICMEMORY_API_URL", "").strip()
        subject = os.environ.get("ATOMICMEMORY_SUBJECT", "").strip()
        if not url or not subject:
            return None
        try:
            timeout = int(os.environ.get("ATOMICMEMORY_TIMEOUT_MS", DEFAULT_TIMEOUT_MS))
        except ValueError:
            timeout = DEFAULT_TIMEOUT_MS
        try:
            return cls(url, subject, os.environ.get("ATOMICMEMORY_API_KEY") or None, timeout)
        except ValueError:
            return None

    def _external_id(self, alias: str) -> str:
        return f"certera-{self._ns}-nota-{alias}"

    def _call(self, method: str, path: str, body: dict | None = None) -> dict | None:
        """Devuelve el objeto JSON, None ante 404 y MemoryUnavailable ante otra falla, timeout o respuesta inválida."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(body).encode() if body is not None else None
        req = request.Request(f"{self.api_url}{path}", data=data, method=method, headers=headers)
        try:
            with self._http.open(req, timeout=self.timeout) as res:
                if not 200 <= res.status < 300:
                    raise MemoryUnavailable(f"Atomic Memory respondió {res.status}")
                payload = json.loads(res.read() or b"{}")
        except error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise MemoryUnavailable(f"Atomic Memory respondió {exc.code}") from exc
        except ValueError as exc:
            raise MemoryUnavailable("Atomic Memory devolvió una respuesta inválida") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise MemoryUnavailable("Atomic Memory no respondió a tiempo") from exc
        if not isinstance(payload, dict):
            raise MemoryUnavailable("Atomic Memory devolvió una respuesta inválida")
        return payload

    def _find(self, alias: str) -> dict | None:
        qs = urlencode({"user_id": self.subject})
        return self._call("GET", f"/v1/memories/by-external-id/{quote(self._external_id(alias), safe='')}?{qs}")

    def get(self, alias: str) -> StoredNote | None:
        found = self._find(alias)
        if found is None:
            return None
        content = found.get("content")
        if not isinstance(content, str):
            raise MemoryUnavailable("Atomic Memory devolvió una nota sin contenido")
        return StoredNote(texto=content.removeprefix(_prefix(alias)), guardada_en=_stamp(found))

    def save(self, alias: str, texto: str) -> None:
        """Guarda o corrige (mismo externalId) la nota del vehículo."""
        content = _prefix(alias) + texto
        result = self._call("POST", "/v1/memories/ingest/quick", {
            "user_id": self.subject,
            "conversation": content,
            "source_site": SOURCE_SITE,
            "skip_extraction": True,
            "content_class": "summary",
            "metadata": {"externalId": self._external_id(alias)},
        })
        if result:
            confirmed = [result.get("stored_memory_ids", []), result.get("updated_memory_ids", [])]
            if all(isinstance(ids, list) and all(isinstance(id_, str) and id_ for id_ in ids) for ids in confirmed) and any(confirmed):
                return
        # Sin ids (p. ej. texto idéntico): solo se confirma si el registro ya tiene ese contenido.
        found = self._find(alias)
        if not found or found.get("content") != content:
            raise MemoryUnavailable("Atomic Memory no confirmó el guardado")

    def delete(self, alias: str) -> None:
        found = self._find(alias)
        if found is None:
            return
        if not isinstance(found.get("id"), str) or not found["id"]:
            raise MemoryUnavailable("Atomic Memory devolvió una nota sin id")
        qs = urlencode({"user_id": self.subject})
        self._call("DELETE", f"/v1/memories/{quote(str(found['id']), safe='')}?{qs}")
        if self._find(alias) is not None:
            raise MemoryUnavailable("Atomic Memory no confirmó el borrado")
