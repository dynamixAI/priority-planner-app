"""Minimal Turso client over the plain HTTP API (Hrana over HTTP).

Why this exists: the libsql native client froze the whole worker process on
Render. Plain HTTPS calls to Turso work fine there, so this talks to the same
database through its HTTP API using only the standard library.

It mirrors what app.py relied on from the old client:
  * conn.execute(sql, params) returns a cursor with .description / .fetchall()
  * INSERT / UPDATE / DELETE statements are queued and sent together, as one
    all-or-nothing transaction, when conn.commit() is called
  * SELECT / CREATE / ALTER etc. run immediately
"""
import base64
import http.client
import json


class TursoError(Exception):
    pass


class _Cursor:
    def __init__(self, columns=None, rows=None):
        self.description = [(c, None, None, None, None, None, None) for c in (columns or [])]
        self._rows = list(rows or [])

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


def _encode(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode()}
    return {"type": "text", "value": str(value)}


def _decode(value):
    kind = value.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(value["value"])
    if kind == "float":
        return float(value["value"])
    if kind == "blob":
        return base64.b64decode(value["base64"])
    return value.get("value")


_WRITE_WORDS = ("insert", "update", "delete", "replace")


def _is_write(sql):
    stripped = sql.lstrip()
    if not stripped:
        return False
    return stripped.split(None, 1)[0].lower() in _WRITE_WORDS


class TursoHTTPConnection:
    def __init__(self, url, token, timeout=15):
        self._host = url.replace("libsql://", "").replace("https://", "").split("/")[0]
        self._token = token
        self._timeout = timeout
        self._http = None
        self._pending = []

    def _connection(self):
        if self._http is None:
            self._http = http.client.HTTPSConnection(self._host, timeout=self._timeout)
        return self._http

    def close(self):
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None

    def _post(self, requests_list):
        body = json.dumps({"requests": requests_list}).encode()
        headers = {
            "Authorization": "Bearer " + self._token,
            "Content-Type": "application/json",
        }
        try:
            conn = self._connection()
            conn.request("POST", "/v2/pipeline", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except Exception:
            self.close()
            raise
        if resp.status != 200:
            self.close()
            raise TursoError("Turso HTTP %s: %s" % (resp.status, raw[:200].decode(errors="replace")))
        return json.loads(raw.decode()).get("results", [])

    @staticmethod
    def _stmt(sql, params):
        return {"sql": sql, "args": [_encode(p) for p in params]}

    def execute(self, sql, params=()):
        params = list(params or [])

        if _is_write(sql):
            self._pending.append((sql, params))
            return _Cursor()

        # Safety net: never read or change tables while writes are still queued.
        if self._pending:
            self.commit()

        results = self._post([
            {"type": "execute", "stmt": self._stmt(sql, params)},
            {"type": "close"},
        ])
        first = results[0]
        if first.get("type") == "error":
            raise TursoError(first.get("error", {}).get("message", "query failed"))

        result = first["response"]["result"]
        columns = [c.get("name") for c in result.get("cols", [])]
        rows = [tuple(_decode(v) for v in row) for row in result.get("rows", [])]
        return _Cursor(columns, rows)

    def commit(self):
        pending, self._pending = self._pending, []
        if not pending:
            return

        # BEGIN, then each statement only if the previous step succeeded, then
        # COMMIT only if every statement succeeded. Any failure skips the rest,
        # and closing the stream discards the open transaction (rollback).
        steps = [{"stmt": {"sql": "BEGIN"}}]
        for i, (sql, params) in enumerate(pending):
            steps.append({
                "condition": {"type": "ok", "step": i},
                "stmt": self._stmt(sql, params),
            })
        steps.append({
            "condition": {"type": "ok", "step": len(pending)},
            "stmt": {"sql": "COMMIT"},
        })

        results = self._post([
            {"type": "batch", "batch": {"steps": steps}},
            {"type": "close"},
        ])
        first = results[0]
        if first.get("type") == "error":
            raise TursoError(first.get("error", {}).get("message", "transaction failed"))

        result = first["response"]["result"]
        for err in (result.get("step_errors") or []):
            if err:
                raise TursoError(err.get("message", "statement failed"))
        step_results = result.get("step_results") or []
        if not step_results or step_results[-1] is None:
            raise TursoError("transaction was not committed")

    def rollback(self):
        self._pending = []
