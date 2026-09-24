"""A small MediKeep API client, stdlib only.

Nothing here is clever. It exists because the alternative -- inserting straight
into Postgres -- skips Pydantic validation, the activity log and the sequence
handling, and those are exactly the things that caught the two impossible SpO2
readings in this dataset.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

# Every create route is declared as "/" in the routers, and
# TrailingSlashMiddleware 307-redirects the bare path. Sending the slash from
# the start avoids a redirect that would drop the POST body on some clients.
API_PREFIX = "/api/v1"

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class MediKeepError(RuntimeError):
    """A request MediKeep rejected. Carries the body, because 422s explain."""

    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"{status} from {path}: {body}")
        self.status = status
        self.path = path
        self.body = body


class MediKeepClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff: float = 1.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self._token: str | None = None

    # -- plumbing ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> dict | list | None:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code not in _RETRY_STATUSES:
                    # 4xx is a mapping bug. Retrying just makes it slower.
                    raise MediKeepError(exc.code, path, body) from exc
                last_error = MediKeepError(exc.code, path, body)
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
            if attempt < self.max_retries - 1:
                time.sleep(self.backoff**attempt)
        raise last_error if last_error else RuntimeError(f"no response from {url}")

    # -- api ---------------------------------------------------------------

    def login(self, username: str, password: str) -> None:
        """POST /api/v1/auth/login.

        Form-encoded, not JSON: the endpoint takes FastAPI's
        OAuth2PasswordRequestForm.
        """
        body = urllib.parse.urlencode({"username": username, "password": password}).encode()
        payload = self._request(
            "POST",
            f"{API_PREFIX}/auth/login",
            data=body,
            content_type="application/x-www-form-urlencoded",
        )
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise RuntimeError("login succeeded but returned no access_token")
        if payload.get("must_change_password"):
            raise RuntimeError(
                "this account must change its password before the API will answer;"
                " log into the web UI once and set a new one"
            )
        self._token = payload["access_token"]

    def get(self, path: str) -> dict | list | None:
        return self._request("GET", f"{API_PREFIX}{path}")

    def post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        result = self._request(
            "POST", f"{API_PREFIX}{path}", data=body, content_type="application/json"
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"expected an object from {path}, got {type(result).__name__}")
        return result

    def resolve_patient_id(self) -> int:
        """Find the patient every record hangs off.

        The self-record route has moved around between releases, so try the
        known spellings before giving up and telling the user to pass the id.
        """
        for path in ("/patients/me", "/patients/recent", "/patient-management/"):
            try:
                payload = self.get(path)
            except MediKeepError:
                continue
            candidate = _first_patient_id(payload)
            if candidate is not None:
                return candidate
        raise RuntimeError(
            "could not work out the patient id from the API; pass --patient-id explicitly"
        )


def _first_patient_id(payload: dict | list | None) -> int | None:
    if isinstance(payload, dict):
        value = payload.get("id")
        return int(value) if isinstance(value, int) else None
    if isinstance(payload, list) and payload:
        return _first_patient_id(payload[0])
    return None
