"""Mock af Canvas Data 2's Query API (DAP) til den selvhostede test-Canvas.

Canvas Data 2 er en hostet Instructure-tjeneste og findes ikke i open source-Canvas. Denne service
svarer som DAP på de kald, en klient som Nexus bruger, med data fra test-Canvas' egen database:

  POST /oauth/token                               client credentials (Basic auth) -> bearer token
  GET  /dap/query/{namespace}/table               tabellerne i et namespace
  POST /dap/query/{namespace}/table/{table}/data  snapshot ({"format": "jsonl"}) eller incremental (+ "since")
  GET  /dap/job/{id}                              jobbets status (jobs er færdige med det samme)
  POST /dap/object/url                            [{"id": ...}] -> midlertidige download-URL'er
  GET  /files/{signeret id}                       gzippet JSON Lines: {"key", "value", "meta"} pr. linje

canvas.enrollment_terms, courses, enrollments og roles læses direkte fra Canvas' tabeller (DAP's
canvas-namespace er næsten 1:1 med Canvas' database, med de samme id'er som REST-API'et).

canvas_logs.web_logs har open source-Canvas ikke. De bygges af Canvas' page_views (hvis de gemmes i
databasen) plus genererede besøg for de aktive tilmeldinger i publicerede kurser de seneste 30 dage:
faste pr. bruger, kursus og dag, så et nyt snapshot giver de samme besøg, og incremental-forespørgsler
giver præcis det nye siden sidst.

Konfiguration (miljøvariabler):
  DAP_CLIENT_ID, DAP_CLIENT_SECRET   det klienten logger ind med
  DAP_PUBLIC_URL                     den offentlige rod-URL, fx https://canvas.example.com/cd2
  DAP_COURSE_ACCESS                  true/false: om klienten må læse canvas_logs (som en rigtig nøgle med/uden web logs)
  DAP_SYNTHETIC_WEB_LOGS             true/false: generér besøg ud over page_views
  PGHOST, PGDATABASE, PGUSER, PGPASSWORD
"""

import base64
import gzip
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import psycopg
from psycopg.rows import dict_row

CLIENT_ID = os.environ["DAP_CLIENT_ID"]
CLIENT_SECRET = os.environ["DAP_CLIENT_SECRET"]
PUBLIC_URL = os.environ["DAP_PUBLIC_URL"].rstrip("/")
COURSE_ACCESS = os.environ.get("DAP_COURSE_ACCESS", "true").lower() == "true"
SYNTHETIC = os.environ.get("DAP_SYNTHETIC_WEB_LOGS", "true").lower() == "true"
DATA_DIR = Path(os.environ.get("DAP_DATA_DIR", "/data"))
PORT = int(os.environ.get("PORT", "8080"))

# Tokens og download-links signeres med en nøgle afledt af hemmeligheden, så de overlever en genstart.
SIGNING_KEY = hashlib.sha256(f"dap-mock:{CLIENT_SECRET}".encode()).digest()
TOKEN_SECONDS = 3600
URL_SECONDS = 900
WEB_LOG_RETENTION = timedelta(days=30)

TABLES = {
    "canvas": ["enrollment_terms", "courses", "enrollments", "roles"],
    "canvas_logs": ["web_logs"],
}

# Kolonner, der aldrig sendes med (store eller uinteressante for en DAP-klient).
SKIP_COLUMNS = {"syllabus_body", "settings", "stuck_sis_fields", "integration_id_hash"}

JOBS: dict[str, dict] = {}
OBJECTS: dict[str, Path] = {}


# --- Hjælpere ---------------------------------------------------------------------------------

def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # Canvas gemmer UTC uden tidszone
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def parse_iso(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def jsonable(value):
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (bytes, memoryview)):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, dict)):
        return value
    return str(value)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict) -> str:
    body = b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = b64(hmac.new(SIGNING_KEY, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def verify(token: str) -> dict | None:
    try:
        body, mac = token.split(".", 1)
    except ValueError:
        return None
    expected = b64(hmac.new(SIGNING_KEY, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return None
    payload = json.loads(unb64(body))
    return payload if payload.get("exp", 0) > time.time() else None


def db():
    # Skrivebeskyttet: mocken må kun læse Canvas' data.
    return psycopg.connect(row_factory=dict_row, options="-c default_transaction_read_only=on", connect_timeout=10)


class ApiError(Exception):
    def __init__(self, status: int, error_type: str, message: str):
        super().__init__(message)
        self.status, self.error_type, self.message = status, error_type, message


# --- canvas-namespace: Canvas' egne tabeller ----------------------------------------------------

def canvas_records(table: str, since: datetime | None, until: datetime):
    """Rækkerne som DAP-records; incremental: dem der er ændret i (since, until]."""
    with db() as conn:
        sql = f"select * from {table}"  # tabelnavnet er altid et fra TABLES
        params: list = []
        if since is not None:
            sql += " where updated_at > %s and updated_at <= %s"
            params = [since.replace(tzinfo=None), until.replace(tzinfo=None)]
        for row in conn.execute(sql + " order by id", params):
            key = {"id": row.pop("id")}
            value = {k: jsonable(v) for k, v in row.items() if k not in SKIP_COLUMNS}
            yield {"key": key, "value": value, "meta": {"action": "U", "ts": iso(row.get("updated_at") or until)}}


# --- canvas_logs.web_logs ----------------------------------------------------------------------

BROWSERS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
]
APPS = [
    "candroid/7.9.0 (Android 14; Pixel 8)",
    "iCanvas/7.12.0 (com.instructure.icanvas; build:1; iOS 18.1.0) Alamofire/5.9.1",
    "Canvas Student/7.12.0 (iPhone; iOS 18.1)",
]
OTHER = ["python-requests/2.32.3", "curl/8.7.1"]

# Relative vægte for timer i døgnet (UTC; Danmark er UTC+1 om vinteren).
HOUR_WEIGHTS = [0.2, 0.1, 0.05, 0.05, 0.05, 0.2, 0.8, 2.5, 4, 4.5, 4, 3, 2.5, 3.5, 3.5, 3, 2.5, 2, 2.5, 3, 3.5, 2.5, 1.2, 0.5]

WEB_PATHS = [
    "/courses/{c}", "/courses/{c}/modules", "/courses/{c}/assignments", "/courses/{c}/assignments/{n}",
    "/courses/{c}/pages/uge-{n}", "/courses/{c}/discussion_topics/{n}", "/courses/{c}/files", "/courses/{c}/grades",
    "/courses/{c}/announcements", "/courses/{c}/quizzes/{n}",
]
API_PATHS = [
    "/api/v1/courses/{c}", "/api/v1/courses/{c}/modules", "/api/v1/courses/{c}/assignments",
    "/api/v1/courses/{c}/discussion_topics", "/api/v1/courses/{c}/activity_stream",
]


def profile(user_id: int, course_id: int, enrollment_type: str) -> dict:
    """En fast "personlighed" pr. bruger og kursus: hvor aktiv, hvornår på dagen og hvor meget på mobil."""
    rng = random.Random(f"profile:{user_id}:{course_id}")
    teacher = enrollment_type in ("TeacherEnrollment", "TaEnrollment", "DesignerEnrollment")
    engagement = rng.choice([0.08, 0.25, 0.45, 0.6, 0.75, 0.9]) if not teacher else rng.uniform(0.55, 0.9)
    return {
        "engagement": engagement,
        "shift": rng.choice([-3, -1, 0, 0, 1, 3]),  # morgen- eller aftenmenneske
        "mobile": rng.choice([0.0, 0.1, 0.25, 0.45]),
        "weekend": rng.uniform(0.2, 0.7),
    }


def synthetic_day(enrollment: dict, day: date):
    user_id, course_id = enrollment["user_id"], enrollment["course_id"]
    p = profile(user_id, course_id, enrollment["type"])
    rng = random.Random(f"day:{user_id}:{course_id}:{day.isoformat()}")
    chance = p["engagement"] * (p["weekend"] if day.weekday() >= 5 else 1.0)
    if rng.random() >= chance:
        return
    weights = HOUR_WEIGHTS[-p["shift"]:] + HOUR_WEIGHTS[:-p["shift"]] if p["shift"] else HOUR_WEIGHTS
    for session_no in range(rng.choice([1, 1, 1, 2, 2, 3])):
        hour = rng.choices(range(24), weights)[0]
        at = datetime(day.year, day.month, day.day, hour, rng.randrange(60), rng.randrange(60), tzinfo=timezone.utc)
        roll = rng.random()
        if roll < 0.03:
            agent, paths = rng.choice(OTHER), API_PATHS
        elif roll < 0.03 + p["mobile"]:
            agent, paths = rng.choice(APPS), API_PATHS
        else:
            agent, paths = rng.choice(BROWSERS), WEB_PATHS
        session = str(uuid.UUID(int=rng.getrandbits(128)))
        for request_no in range(rng.randint(2, 14)):
            method = "POST" if rng.random() < 0.07 else "GET"
            status = rng.choices([200, 302, 304, 404, 401], [88, 4, 5, 2, 1])[0]
            url = rng.choice(paths).format(c=course_id, n=rng.randint(1, 12))
            request_id = str(uuid.UUID(int=rng.getrandbits(128)))
            yield {
                "key": {"id": request_id},
                "value": {
                    "timestamp": iso(at),
                    "user_id": user_id,
                    "course_id": course_id,
                    "context_type": "Course",
                    "context_id": course_id,
                    "http_method": method,
                    "http_status": status,
                    "url": f"https://{os.environ.get('CANVAS_DOMAIN', 'canvas.local')}{url}",
                    "user_agent": agent,
                    "session_id": session,
                    "request_id": request_id,
                    "remote_ip": f"10.{(user_id >> 8) & 255}.{user_id & 255}.{session_no + 1}",
                    "controller": "courses",
                    "action": "show",
                    "participated": method == "POST",
                    "interaction_seconds": rng.randint(5, 600),
                },
                "meta": {"action": "U", "ts": iso(at)},
            }
            at += timedelta(seconds=rng.randint(15, 240))


def web_log_records(since: datetime, until: datetime):
    """Besøg med tidsstempel i (since, until]."""
    with db() as conn:
        if conn.execute("select to_regclass('public.page_views') is not null as ok").fetchone()["ok"]:
            rows = conn.execute(
                """select * from page_views
                   where context_type = 'Course' and created_at > %s and created_at <= %s
                   order by created_at""",
                [since.replace(tzinfo=None), until.replace(tzinfo=None)],
            )
            keep = ("session_id", "user_id", "url", "user_agent", "http_method", "remote_ip",
                    "controller", "action", "participated", "interaction_seconds")
            for row in rows:
                yield {
                    "key": {"id": row["request_id"]},
                    "value": {
                        **{k: jsonable(row[k]) for k in keep if k in row},
                        "request_id": row["request_id"],
                        "timestamp": iso(row["created_at"]),
                        "course_id": row["context_id"],
                        "context_type": "Course",
                        "http_status": 200,
                    },
                    "meta": {"action": "U", "ts": iso(row["created_at"])},
                }

        if not SYNTHETIC:
            return

        enrollments = conn.execute(
            """select e.user_id, e.course_id, e.type
               from enrollments e join courses c on c.id = e.course_id
               where e.workflow_state = 'active' and c.workflow_state = 'available'
                 and e.type in ('StudentEnrollment', 'TeacherEnrollment', 'TaEnrollment')
               group by e.user_id, e.course_id, e.type
               order by e.course_id, e.user_id"""
        ).fetchall()

    day = since.date()
    while day <= until.date():
        for enrollment in enrollments:
            for record in synthetic_day(enrollment, day):
                if since < parse_iso(record["value"]["timestamp"]) <= until:
                    yield record
        day += timedelta(days=1)


# --- Jobs og filer ----------------------------------------------------------------------------

def cleanup():
    cutoff = time.time() - 3600
    for job_id, job in list(JOBS.items()):
        if job["_created"] < cutoff:
            for obj in job["objects"]:
                path = OBJECTS.pop(obj["id"], None)
                if path:
                    path.unlink(missing_ok=True)
            del JOBS[job_id]


def run_query(namespace: str, table: str, body: dict) -> dict:
    if body.get("format", "jsonl") != "jsonl":
        raise ApiError(400, "ValidationError", "Only the jsonl format is supported by this mock.")

    until = now()
    since = parse_iso(body["since"]) if body.get("since") else None

    if namespace == "canvas_logs":
        if since is not None and since < until - WEB_LOG_RETENTION:
            raise ApiError(400, "SnapshotRequiredError", f"since {iso(since)} is outside the retention range; take a new snapshot.")
        records = web_log_records(since or until - WEB_LOG_RETENTION, until)
    else:
        records = canvas_records(table, since, until)

    cleanup()
    job_id = str(uuid.uuid4())
    object_id = f"{job_id}/part-00000-{secrets.token_hex(4)}.json.gz"
    path = DATA_DIR / f"{job_id}.json.gz"
    count = 0
    with gzip.open(path, "wt", encoding="utf-8") as out:
        for record in records:
            out.write(json.dumps(record, separators=(",", ":")) + "\n")
            count += 1

    if since is not None and count == 0:
        path.unlink(missing_ok=True)
        raise ApiError(400, "ValidationError", f"No data since {iso(since)} in {namespace}.{table}.")

    OBJECTS[object_id] = path
    job = {
        "id": job_id,
        "status": "complete",
        "expires_at": iso(until + timedelta(hours=24)),
        "objects": [{"id": object_id}],
        "schema_version": 1,
        "_created": time.time(),
        "_count": count,
    }
    if since is None:
        job["at"] = iso(until)
    else:
        job["since"], job["until"] = iso(since), iso(until)
    JOBS[job_id] = job
    print(f"job {job_id}: {namespace}.{table} {'incremental since ' + iso(since) if since else 'snapshot'}, {count} records", flush=True)
    return job


def public_job(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


# --- HTTP -------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "dap-mock/1"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def send_json(self, status: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def json_body(self):
        raw = self.read_body()
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise ApiError(400, "ValidationError", "The body is not valid JSON.")

    def namespaces(self) -> dict:
        auth = self.headers.get("Authorization", "")
        claims = verify(auth[7:]) if auth.startswith("Bearer ") else None
        if claims is None:
            raise ApiError(401, "AuthenticationError", "Missing, invalid or expired token.")
        return claims["ns"]

    def handle_any(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if method == "GET" and path == "/health":
                return self.send_json(200, {"ok": True})
            if method == "POST" and path == "/oauth/token":
                return self.token()
            if method == "GET" and path.startswith("/files/"):
                return self.download(path[len("/files/"):])

            namespaces = self.namespaces()

            if method == "GET" and (m := re.fullmatch(r"/dap/query/([a-z_]+)/table", path)):
                namespace = m[1]
                if namespace not in namespaces:
                    raise ApiError(403, "AuthorizationError", f"Not authorized for namespace {namespace}.")
                return self.send_json(200, {"tables": [t for t in TABLES.get(namespace, []) if t in namespaces[namespace]]})

            if method == "POST" and (m := re.fullmatch(r"/dap/query/([a-z_]+)/table/([a-z_]+)/data", path)):
                namespace, table = m[1], m[2]
                if table not in TABLES.get(namespace, []):
                    raise ApiError(404, "NotFoundError", f"{namespace}.{table} is not served by this mock.")
                if table not in namespaces.get(namespace, []):
                    raise ApiError(403, "AuthorizationError", f"Not authorized for {namespace}.{table}.")
                return self.send_json(200, public_job(run_query(namespace, table, self.json_body())))

            if method == "GET" and (m := re.fullmatch(r"/dap/job/([0-9a-f-]+)", path)):
                job = JOBS.get(m[1]) or {"id": m[1], "status": "failed", "error": {"message": "Unknown or expired job."}}
                return self.send_json(200, public_job(job))

            if method == "POST" and path == "/dap/object/url":
                urls = {}
                for item in self.json_body():
                    object_id = item.get("id") if isinstance(item, dict) else None
                    if object_id not in OBJECTS:
                        raise ApiError(404, "NotFoundError", f"Unknown object {object_id}.")
                    expires = int(time.time()) + URL_SECONDS
                    urls[object_id] = {"url": f"{PUBLIC_URL}/files/{sign({'o': object_id, 'exp': expires})}",
                                       "expires_at": iso(datetime.fromtimestamp(expires, timezone.utc))}
                return self.send_json(200, {"urls": urls})

            raise ApiError(404, "NotFoundError", f"No route for {method} {path}.")
        except ApiError as e:
            self.send_json(e.status, {"error": {"type": e.error_type, "message": e.message}})
        except psycopg.Error as e:
            print(f"database error: {e}", flush=True)
            self.send_json(503, {"error": {"type": "ServiceUnavailable", "message": "The Canvas database could not be read."}})

    def token(self) -> None:
        auth = self.headers.get("Authorization", "")
        try:
            client_id, secret = base64.b64decode(auth[6:]).decode().split(":", 1) if auth.startswith("Basic ") else ("", "")
        except ValueError:
            client_id, secret = "", ""
        if not (hmac.compare_digest(client_id, CLIENT_ID) and hmac.compare_digest(secret, CLIENT_SECRET)):
            raise ApiError(401, "invalid_client", "Unknown client ID or secret.")

        form = {k: v[0] for k, v in parse_qs(self.read_body().decode()).items()}
        requested = {"canvas": TABLES["canvas"], **({"canvas_logs": TABLES["canvas_logs"]} if COURSE_ACCESS else {})}
        if form.get("authorization_details"):
            try:
                details = json.loads(form["authorization_details"])
                requested = {}
                for detail in details:
                    requested.update(detail.get("namespaces") or {})
            except (json.JSONDecodeError, AttributeError, TypeError):
                raise ApiError(400, "invalid_request", "authorization_details is not valid.")

        for namespace, tables in requested.items():
            allowed = TABLES.get(namespace, []) if namespace != "canvas_logs" or COURSE_ACCESS else []
            denied = [t for t in tables if t not in allowed]
            if denied:
                raise ApiError(403, "access_denied", f"Not authorized for {namespace}: {', '.join(denied)}.")

        token = sign({"ns": requested, "exp": int(time.time()) + TOKEN_SECONDS})
        self.send_json(200, {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_SECONDS})

    def download(self, signed: str) -> None:
        claims = verify(signed)
        path = OBJECTS.get(claims["o"]) if claims else None
        if path is None or not path.exists():
            raise ApiError(403, "AccessDenied", "The link is invalid or has expired.")
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                self.wfile.write(chunk)

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for stale in DATA_DIR.glob("*.json.gz"):
        stale.unlink(missing_ok=True)
    print(f"dap-mock on :{PORT}, public URL {PUBLIC_URL}, course access {'on' if COURSE_ACCESS else 'off'}, "
          f"synthetic web logs {'on' if SYNTHETIC else 'off'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
