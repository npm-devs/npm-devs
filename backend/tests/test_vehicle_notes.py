"""Nota revisada por vehículo en la memoria compartida del operador (contra un fixture HTTP local de Atomic Memory).

No necesitan la base: las notas no dependen del replay.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.notes import NotesMemory
from tests.fake_atomicmemory import FakeAtomicMemory

NOTE = "/v1/vehicles/T07/note"
OP = {"X-Notes-Key": "clave-operador"}
OK = {"texto": "Sobreconsumo en Lima–Arequipa revisado: iba con 4 t más. Sin acción.", "consentimiento": True}


@pytest.fixture
def memory():
    fake = FakeAtomicMemory().start()
    yield fake
    fake.stop()


def api_for(memory: FakeAtomicMemory, subject: str = "flota-a", timeout_ms: int = 2500, key: str = "clave-operador") -> TestClient:
    notes = NotesMemory(memory.url, subject, "clave-de-prueba", timeout_ms)
    return TestClient(create_app(api_key="clave-tools", notes=notes, notes_api_key=key))


def test_feature_is_off_and_hidden_by_default(monkeypatch):
    for var in ("ATOMICMEMORY_API_URL", "ATOMICMEMORY_SUBJECT", "ATOMICMEMORY_API_KEY", "NOTES_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    api = TestClient(create_app())

    assert api.get(NOTE).json() == {"habilitada": False, "nota": None}
    saved = api.put(NOTE, json=OK, headers=OP)
    assert saved.status_code == 503
    assert "No se guardó" in saved.json()["detail"]


def test_memory_without_notes_api_key_stays_off(memory):
    api = TestClient(create_app(notes=NotesMemory(memory.url, "flota-a")))

    assert api.get(NOTE).json() == {"habilitada": False, "nota": None}
    assert api.put(NOTE, json=OK, headers=OP).status_code == 503
    assert memory.requests == []


@pytest.mark.parametrize("headers", [{}, {"X-Notes-Key": "otra"}, {"X-API-Key": "clave-tools"}])
def test_reads_writes_and_deletes_require_the_operator_key(memory, headers):
    api = api_for(memory)
    api.put(NOTE, json=OK, headers=OP)
    memory.requests.clear()

    assert api.get(NOTE, headers=headers).status_code == 401
    assert api.put(NOTE, json={"texto": "pisada", "consentimiento": True}, headers=headers).status_code == 401
    assert api.delete(NOTE, headers=headers).status_code == 401
    assert memory.requests == []  # sin clave no se toca Atomic Memory
    assert api.get(NOTE, headers=OP).json()["nota"]["texto"] == OK["texto"]


def test_saved_note_is_recalled_when_the_vehicle_is_opened_again(memory):
    api = api_for(memory)

    assert api.get(NOTE, headers=OP).json() == {"habilitada": True, "nota": None}
    assert api.put(NOTE, json=OK, headers=OP).status_code == 200

    nota = api_for(memory).get(NOTE, headers=OP).json()["nota"]  # otra instancia de la API, misma memoria
    assert nota["texto"] == OK["texto"]
    assert nota["procedencia"] == "human_reported"
    assert nota["guardada_en"]


def test_save_requires_explicit_consent(memory):
    api = api_for(memory)

    assert api.put(NOTE, json={"texto": "x"}, headers=OP).status_code == 422
    assert api.put(NOTE, json={"texto": "x", "consentimiento": False}, headers=OP).status_code == 422
    assert api.put(NOTE, json={"texto": "   ", "consentimiento": True}, headers=OP).status_code == 422
    assert memory.requests == []


def test_correcting_replaces_the_note_instead_of_adding_another(memory):
    api = api_for(memory)
    api.put(NOTE, json=OK, headers=OP)

    api.put(NOTE, json={"texto": "Corrección: eran 2 t de más, no 4.", "consentimiento": True}, headers=OP)

    assert api.get(NOTE, headers=OP).json()["nota"]["texto"] == "Corrección: eran 2 t de más, no 4."
    assert len(memory.memories["flota-a"]) == 1


def test_delete_removes_the_note(memory):
    api = api_for(memory)
    api.put(NOTE, json=OK, headers=OP)

    assert api.delete(NOTE, headers=OP).json() == {"habilitada": True, "nota": None}
    assert api.get(NOTE, headers=OP).json()["nota"] is None
    assert memory.memories["flota-a"] == {}
    assert api.delete(NOTE, headers=OP).status_code == 200  # borrar dos veces no falla


def test_notes_are_per_vehicle(memory):
    api = api_for(memory)
    api.put(NOTE, json=OK, headers=OP)

    assert api.get("/v1/vehicles/T04/note", headers=OP).json()["nota"] is None


def test_subject_comes_from_the_server_never_from_the_request(memory):
    flota_a, flota_b = api_for(memory, "flota-a"), api_for(memory, "flota-b")
    flota_a.put(NOTE, json=OK, headers=OP)

    # Ni un user_id en la query ni un header cambian el sujeto de la memoria.
    spoof = {**OP, "X-User-Id": "flota-a"}
    assert flota_b.get(NOTE, params={"user_id": "flota-a"}, headers=spoof).json()["nota"] is None
    flota_b.delete(NOTE, params={"user_id": "flota-a"}, headers=spoof)
    assert flota_a.get(NOTE, headers=OP).json()["nota"]["texto"] == OK["texto"]
    assert {r[2]["user_id"] for r in memory.requests} == {"flota-a", "flota-b"}


def test_provider_failure_says_the_note_was_not_saved(memory):
    api = api_for(memory)
    memory.fail_status = 500

    saved = api.put(NOTE, json=OK, headers=OP)

    assert saved.status_code == 503
    assert saved.json()["detail"].startswith("No se guardó la nota")
    assert api.get(NOTE, headers=OP).status_code == 503
    assert api.delete(NOTE, headers=OP).json()["detail"].startswith("No se borró la nota")


@pytest.mark.parametrize("body", [b"<html>proxy</html>", b"[]", b'{"id": "x", "content": 42}', b"{}"])
def test_malformed_provider_payload_is_a_failure_not_an_empty_note(memory, body):
    api = api_for(memory)
    memory.raw_body = body

    assert api.get(NOTE, headers=OP).status_code == 503
    assert api.put(NOTE, json=OK, headers=OP).status_code == 503
    assert api.delete(NOTE, headers=OP).status_code == 503


def test_slow_provider_is_cut_at_the_timeout(memory):
    api = api_for(memory, timeout_ms=300)
    memory.delay_s = 2

    start = time.monotonic()
    saved = api.put(NOTE, json=OK, headers=OP)

    assert time.monotonic() - start < 1.5
    assert saved.status_code == 503
    assert "No se guardó" in saved.json()["detail"]


@pytest.mark.parametrize(("raw", "seconds"), [("999999", 5.0), ("0", 0.1), ("no-es-numero", 2.5)])
def test_timeout_from_env_is_bounded(monkeypatch, raw, seconds):
    monkeypatch.setenv("ATOMICMEMORY_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATOMICMEMORY_SUBJECT", "flota-a")
    monkeypatch.setenv("ATOMICMEMORY_TIMEOUT_MS", raw)

    assert NotesMemory.from_env().timeout == seconds


def test_unknown_alias_shape_is_rejected_without_calling_the_provider(memory):
    assert api_for(memory).put("/v1/vehicles/..%2Fx/note", json=OK, headers=OP).status_code in (404, 422)
    assert memory.requests == []


def test_dashboard_reads_still_work_while_memory_is_down(memory):
    memory.fail_status = 503
    api = api_for(memory)

    assert api.get("/v1/alerts").status_code == 200


def test_created_response_confirms_save_and_recall(memory):
    memory.ingest_status = 201
    api = api_for(memory)
    assert api.put(NOTE, json=OK, headers=OP).status_code == 200
    assert api.get(NOTE, headers=OP).json()["nota"]["texto"] == OK["texto"]


@pytest.mark.parametrize("response", [{"success": False}, {}, {"success": True}])
def test_delete_requires_record_to_be_absent(memory, response):
    api = api_for(memory)
    assert api.put(NOTE, json=OK, headers=OP).status_code == 200
    memory.delete_body = response  # provider acknowledges without deleting the record
    assert api.delete(NOTE, headers=OP).status_code == 503
    assert api.get(NOTE, headers=OP).json()["nota"]["texto"] == OK["texto"]


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "https://example.com?key=x", "file:///tmp/memory"])
def test_invalid_provider_url_disables_optional_memory(monkeypatch, url):
    monkeypatch.setenv("ATOMICMEMORY_API_URL", url)
    monkeypatch.setenv("ATOMICMEMORY_SUBJECT", "flota-a")
    assert NotesMemory.from_env() is None
    assert TestClient(create_app()).get(NOTE).json() == {"habilitada": False, "nota": None}


def test_redirect_never_reaches_another_server(memory):
    other = FakeAtomicMemory().start()
    try:
        memory.redirect_to = other.url + "/redirected"
        api = api_for(memory)
        assert api.get(NOTE, headers=OP).status_code == 503
        assert other.requests == []
    finally:
        other.stop()
