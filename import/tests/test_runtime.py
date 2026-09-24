"""Sources, ledger, client and runner -- the parts that touch disk or sockets."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from medikeep_import.client import MediKeepClient, MediKeepError, _first_patient_id
from medikeep_import.ledger import Ledger
from medikeep_import.mappers import map_everything
from medikeep_import.models import MappingResult, Record
from medikeep_import.runner import (
    apply,
    print_apply_summary,
    print_plan,
    records_by_entity,
    summarize,
    write_sidecar,
)
from medikeep_import.sources import load, load_all

FIXTURES = Path(__file__).parent / "fixtures"


# -- sources --------------------------------------------------------------


def test_embedded_newlines_do_not_split_a_record():
    # wc -l says 4 lines; there are 2 records.
    loaded = load(FIXTURES / "doctor_notes.csv")
    assert len(loaded) == 2
    assert "\n" in loaded[0].data["Description"]


def test_a_quoted_comma_stays_inside_one_field():
    loaded = load(FIXTURES / "medicine_tracking.csv")
    assert loaded[0].data["Prescribed by"] == "Jane A. Doe, M.D."


def test_source_keys_are_stable_and_namespaced():
    loaded = load(FIXTURES / "persona.csv")
    assert loaded[1].key("allergy") == "persona:1:allergy"


def test_a_missing_file_loads_as_empty(tmp_path):
    assert load_all(tmp_path)["health_tracker"] == []


def test_get_returns_empty_string_for_a_missing_column():
    loaded = load(FIXTURES / "persona.csv")
    assert loaded[0].get("Nope") == ""


# -- ledger ---------------------------------------------------------------


def test_the_ledger_remembers_across_instances(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with Ledger(path, "http://a") as ledger:
        ledger.record("k", "vitals", 11)
    with Ledger(path, "http://a") as ledger:
        assert ledger.remote_id("k") == 11
        assert ledger.counts() == {"vitals": 1}


def test_the_ledger_is_scoped_per_target(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with Ledger(path, "http://local") as ledger:
        ledger.record("k", "vitals", 11)
    with Ledger(path, "https://droplet") as ledger:
        # Importing locally must not convince the importer the droplet is done.
        assert ledger.remote_id("k") is None


def test_the_ledger_file_is_not_world_readable(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with Ledger(path, "http://a"):
        assert path.stat().st_mode & 0o077 == 0


def test_recording_the_same_key_twice_overwrites(tmp_path):
    with Ledger(tmp_path / "l.sqlite3", "http://a") as ledger:
        ledger.record("k", "vitals", 1)
        ledger.record("k", "vitals", 2)
        assert ledger.remote_id("k") == 2


# -- client ---------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, bytes]] = []
    fail_times = 0

    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Handler.calls.append(("POST", self.path, body))
        if self.path.endswith("/auth/login"):
            if b"wrong" in body:
                return self._send(401, {"detail": "nope"})
            if b"stale" in body:
                return self._send(200, {"access_token": "t", "must_change_password": True})
            if b"empty" in body:
                return self._send(200, {"token_type": "bearer"})
            return self._send(200, {"access_token": "t", "token_type": "bearer"})
        if self.path.endswith("/flaky/"):
            if _Handler.fail_times > 0:
                _Handler.fail_times -= 1
                return self._send(503, {"detail": "later"})
            return self._send(200, {"id": 5})
        if self.path.endswith("/bad/"):
            return self._send(422, {"detail": "oxygen_saturation out of range"})
        if self.path.endswith("/textual/"):
            body = b"[1, 2]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        return self._send(200, {"id": 42})

    def do_GET(self):
        if self.path.endswith("/patients/me"):
            return self._send(404, {"detail": "gone"})
        if self.path.endswith("/patients/recent"):
            return self._send(200, [{"id": 3}])
        if self.path.endswith("/nothing"):
            return self._send(200, [])
        return self._send(200, {"id": 9})


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _Handler.calls = []
    _Handler.fail_times = 0
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_login_is_form_encoded_not_json(server):
    client = MediKeepClient(server)
    client.login("admin", "secret")
    _, path, body = _Handler.calls[0]
    assert path == "/api/v1/auth/login"
    assert body == b"username=admin&password=secret"


def test_a_bad_password_raises_with_the_body(server):
    with pytest.raises(MediKeepError) as exc:
        MediKeepClient(server).login("wrong", "wrong")
    assert exc.value.status == 401
    assert "nope" in exc.value.body


def test_a_forced_password_change_is_reported_clearly(server):
    with pytest.raises(RuntimeError, match="must change its password"):
        MediKeepClient(server).login("stale", "x")


def test_a_login_without_a_token_is_an_error(server):
    with pytest.raises(RuntimeError, match="no access_token"):
        MediKeepClient(server).login("empty", "x")


def test_post_sends_the_bearer_token(server):
    client = MediKeepClient(server)
    client.login("admin", "secret")
    assert client.post("/vitals/", {"a": 1}) == {"id": 42}


def test_a_5xx_is_retried_and_a_4xx_is_not(server):
    client = MediKeepClient(server, backoff=0.0)
    _Handler.fail_times = 2
    assert client.post("/flaky/", {}) == {"id": 5}
    with pytest.raises(MediKeepError) as exc:
        client.post("/bad/", {})
    assert exc.value.status == 422
    assert "oxygen_saturation" in exc.value.body


def test_retries_give_up_eventually(server):
    client = MediKeepClient(server, backoff=0.0, max_retries=2)
    _Handler.fail_times = 5
    with pytest.raises(MediKeepError):
        client.post("/flaky/", {})


def test_a_non_object_response_to_a_post_is_an_error(server):
    with pytest.raises(RuntimeError, match="expected an object"):
        MediKeepClient(server).post("/textual/", {})


def test_the_patient_id_falls_through_to_the_next_known_route(server):
    assert MediKeepClient(server).resolve_patient_id() == 3


def test_an_unreachable_host_raises():
    client = MediKeepClient("http://127.0.0.1:1", backoff=0.0, max_retries=2, timeout=0.2)
    with pytest.raises(Exception):
        client.get("/anything")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [({"id": 4}, 4), ([{"id": 6}], 6), ([], None), (None, None), ({"id": "x"}, None)],
)
def test_first_patient_id(payload, expected):
    assert _first_patient_id(payload) == expected


class _FailingClient:
    """Answers resolve_patient_id with nothing at all."""

    def get(self, path):
        raise MediKeepError(404, path, "")


def test_an_unresolvable_patient_id_asks_the_user_to_pass_one(server, monkeypatch):
    client = MediKeepClient(server)
    monkeypatch.setattr(client, "get", _FailingClient().get)
    with pytest.raises(RuntimeError, match="--patient-id"):
        client.resolve_patient_id()


# -- runner ---------------------------------------------------------------


class FakeClient:
    def __init__(self, fail_on=(), idless=()):
        self.posted = []
        self.fail_on = fail_on
        self.idless = idless
        self._next = 100

    def post(self, path, payload):
        self.posted.append((path, payload))
        if path in self.fail_on:
            raise MediKeepError(422, path, "rejected")
        if path in self.idless:
            return {"ok": True}
        self._next += 1
        return {"id": self._next}


def _result():
    parent = Record("p", "symptom", "/symptoms/", {"symptom_name": "Dizzy"})
    child = Record(
        "c", "symptom_occurrence", "/symptoms/{parent_id}/occurrences", {"severity": "mild"}, "p"
    )
    return MappingResult(records=[parent, child])


def test_apply_substitutes_the_parent_id_into_the_child_path(tmp_path):
    client = FakeClient()
    with Ledger(tmp_path / "l.sqlite3", "http://a") as ledger:
        stats = apply(_result(), client, ledger)
    assert client.posted[1][0] == "/symptoms/101/occurrences"
    assert stats["created:symptom"] == 1


def test_a_child_gets_the_parent_id_in_its_body_when_asked(tmp_path):
    # LabTestComponentCreate requires lab_result_id in the payload even though
    # the path already carries it. Omitting it 422s every component.
    parent = Record("p", "lab_result", "/lab-results/", {"test_name": "Hematology"})
    child = Record(
        "c",
        "lab_test_component",
        "/lab-test-components/lab-result/{parent_id}/components",
        {"test_name": "WBC"},
        parent_key="p",
        parent_field="lab_result_id",
    )
    client = FakeClient()
    with Ledger(tmp_path / "l.sqlite3", "http://a") as ledger:
        apply(MappingResult(records=[parent, child]), client, ledger)
    path, payload = client.posted[1]
    assert path == "/lab-test-components/lab-result/101/components"
    assert payload["lab_result_id"] == 101


def test_a_child_without_a_parent_field_keeps_its_payload_clean(tmp_path):
    client = FakeClient()
    with Ledger(tmp_path / "l.sqlite3", "http://a") as ledger:
        apply(_result(), client, ledger)
    assert "lab_result_id" not in client.posted[1][1]


def test_a_second_apply_creates_nothing(tmp_path):
    path = tmp_path / "l.sqlite3"
    with Ledger(path, "http://a") as ledger:
        apply(_result(), FakeClient(), ledger)
    client = FakeClient()
    with Ledger(path, "http://a") as ledger:
        stats = apply(_result(), client, ledger)
    assert client.posted == []
    assert stats["skipped"] == 2


def test_a_child_whose_parent_failed_is_orphaned_not_crashed(tmp_path, capsys):
    client = FakeClient(fail_on=("/symptoms/",))
    with Ledger(tmp_path / "l.sqlite3", "http://a") as ledger:
        stats = apply(_result(), client, ledger)
    assert stats["failed"] == 1
    assert stats["orphaned"] == 1
    assert "parent p was not created" in capsys.readouterr().out


def test_a_create_without_an_id_is_flagged(tmp_path, capsys):
    client = FakeClient(idless=("/symptoms/",))
    with Ledger(tmp_path / "l.sqlite3", "http://a") as ledger:
        stats = apply(_result(), client, ledger)
    assert stats["created_without_id"] == 1
    assert "no id in the response" in capsys.readouterr().out


def test_limit_stops_early(tmp_path):
    with Ledger(tmp_path / "l.sqlite3", "http://a") as ledger:
        stats = apply(_result(), FakeClient(), ledger, limit=1)
    assert stats["stopped_at_limit"] == 1


def test_write_sidecar_locks_the_files_down(tmp_path):
    result = MappingResult(sidecar={"sleep_log": [{"a": 1}]})
    written = write_sidecar(result, tmp_path / "derived")
    assert written[0].name == "sleep_log.json"
    assert json.loads(written[0].read_text()) == [{"a": 1}]
    assert written[0].stat().st_mode & 0o077 == 0


def test_plan_prints_counts_warnings_and_sidecar(rows, tmp_path, capsys):
    result = map_everything(rows, patient_id=1)
    with Ledger(tmp_path / "l.sqlite3", "http://a") as ledger:
        ledger.record("seen", "vitals", 1)
        print_plan(result, ledger)
    out = capsys.readouterr().out
    assert "records to create" in out
    assert "already in the ledger" in out
    assert "sidecar" in out
    assert "[out-of-range]" in out


def test_plan_works_without_a_ledger(rows, capsys):
    print_plan(map_everything(rows, patient_id=1))
    assert "records to create" in capsys.readouterr().out


def test_apply_summary_lists_every_bucket(capsys):
    print_apply_summary({"created:vitals": 2, "skipped": 1, "failed": 3})
    out = capsys.readouterr().out
    assert "created 2 records" in out
    assert "failed" in out


def test_summarize_and_filter(rows):
    result = map_everything(rows, patient_id=1)
    assert summarize(result)["vitals"] == 2
    assert len(records_by_entity(result, "vitals")) == 2


def test_map_everything_wires_every_file_together(rows):
    result = map_everything(rows, patient_id=1)
    entities = summarize(result)
    assert entities["vitals"] == 2
    assert entities["lab_result"] == 6
    assert entities["medication"] == 5
    assert entities["allergy"] == 4  # 3 assessments + the persona drug allergy
    assert entities["encounter"] == 1
    assert set(result.sidecar) == {
        "medication_log",
        "sleep_log",
        "insights",
        "persona",
    }
