"""Mock af Blackboard Learn til test af Nexus' Blackboard-integration.

Blackboard Learn er lukket kode og kan ikke køres som Canvas og Moodle. Denne service svarer i stedet
på de kald, Nexus laver mod Learn og Anthology Developer Portal, med data fra CSV-filer (SEED_DIR):

REST (Learn's public API, to-benet OAuth2 med applikationens key og secret):
  POST /learn/api/public/v1/oauth2/token                  client credentials (Basic auth) -> bearer token
  GET  /learn/api/public/v1/terms[/{id}]
  GET  /learn/api/public/v3/courses[/{id}]                (også v1 og v2)
  GET  /learn/api/public/v1/courses/{id}/users[/{userId}] medlemskaber, ?expand=user, ?lastAccessed=
  GET  /learn/api/public/v1/users[/{id}]
  GET  /learn/api/public/v1/users/{id}/courses
  GET  /learn/api/public/v1/courseRoles
  GET  /learn/api/public/v1/system/version
  Id'er i stier: _123_1, uuid:..., courseId:... / userName:..., externalId:...
  ?fields=, ?limit=, ?offset= og paging.nextPage som i Learn.

LTI 1.3 (det Developer Portal gør for en rigtig Learn):
  GET|POST /api/v1/gateway/oidcauth                           OIDC-auth: svarer med id_token (form_post)
  GET      /api/v1/management/applications/{id}/jwks.json     platformens offentlige nøgle

Testside (Basic auth, admin / BB_UI_PASSWORD):
  GET /         opsætningsværdier til Nexus, kurser og medlemmer med "Åbn i Nexus"-knapper
  GET /launch   starter en LTI-launch (third-party login initiation) mod Nexus som en bruger

lastAccessed: Learn's REST-API har ingen aktivitetslog, kun hvert medlemskabs lastAccessed. Mocken
regner den ud fra en fast profil pr. medlemskab og dag (deterministisk tilfældighed): fortiden ændrer sig
aldrig, og den flytter sig kun frem, når tiden går. Medlemskaber, mocken ser første gang, får
BB_BACKFILL_DAYS dages historik (created sættes så langt tilbage).

Konfiguration (miljøvariabler):
  BB_PUBLIC_URL                         mockens offentlige rod-URL, fx https://blackboard.example.com
  BB_ISSUER                             LTI-issuer (standard: BB_PUBLIC_URL)
  BB_APPLICATION_ID                     applikationens id = LTI client_id
  BB_APPLICATION_KEY, BB_SECRET         REST-login
  BB_DEPLOYMENT_ID                      LTI deployment_id
  BB_TOOL_URL                           Nexus' rod-URL, fx https://nexus.example.com (login: /lti/login)
  BB_UI_PASSWORD                        kodeord til testsiden
  BB_BACKFILL_DAYS                      dages historik for nye medlemskaber (30)
  BB_DAILY_CALL_LIMIT                   >0: 429 med Retry-After til midnat UTC efter så mange kald pr. døgn
"""

import base64
import csv
import hashlib
import hmac
import html
import json
import os
import random
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PUBLIC_URL = os.environ["BB_PUBLIC_URL"].rstrip("/")
ISSUER = os.environ.get("BB_ISSUER") or PUBLIC_URL
APPLICATION_ID = os.environ["BB_APPLICATION_ID"]
APPLICATION_KEY = os.environ["BB_APPLICATION_KEY"]
SECRET = os.environ["BB_SECRET"]
DEPLOYMENT_ID = os.environ["BB_DEPLOYMENT_ID"]
TOOL_URL = os.environ.get("BB_TOOL_URL", "").rstrip("/")
UI_PASSWORD = os.environ.get("BB_UI_PASSWORD", "")
BACKFILL_DAYS = int(os.environ.get("BB_BACKFILL_DAYS", "30"))
DAILY_CALL_LIMIT = int(os.environ.get("BB_DAILY_CALL_LIMIT", "0"))
DATA_DIR = Path(os.environ.get("BB_DATA_DIR", "/data"))
SEED_DIR = Path(os.environ.get("BB_SEED_DIR", "/seed"))
PORT = int(os.environ.get("PORT", "8080"))

API = "/learn/api/public"
SIGNING_KEY = hashlib.sha256(f"bb-mock:{SECRET}".encode()).digest()
TOKEN_SECONDS = 3600
HINT_SECONDS = 600
DATA_SOURCE = "_2_1"  # SYSTEM

LIS = "http://purl.imsglobal.org/vocab/lis/v2"
LTI = "https://purl.imsglobal.org/spec/lti/claim"

# Learn's standardroller i kurser: roleId -> (nameForCourses, nameForOrganizations, LTI-roller)
COURSE_ROLES = {
    "Instructor": ("Instructor", "Leader", [f"{LIS}/membership#Instructor"]),
    "TeachingAssistant": ("Teaching Assistant", "Assistant", [f"{LIS}/membership#Instructor", f"{LIS}/membership/Instructor#TeachingAssistant"]),
    "CourseBuilder": ("Course Builder", "Organization Builder", [f"{LIS}/membership#ContentDeveloper"]),
    "Grader": ("Grader", "Grader", [f"{LIS}/membership/Instructor#Grader"]),
    "Student": ("Student", "Participant", [f"{LIS}/membership#Learner"]),
    "Guest": ("Guest", "Guest", [f"{LIS}/membership#Learner", f"{LIS}/membership/Learner#GuestLearner"]),
}

# Systemrolle (users.csv) -> Learn's systemRoleIds og LTI's institution-/systemroller.
SYSTEM_ROLES = {
    "Student": (["User"], [f"{LIS}/institution/person#Student", f"{LIS}/system/person#User"]),
    "Faculty": (["User"], [f"{LIS}/institution/person#Faculty", f"{LIS}/system/person#User"]),
    "SystemAdmin": (["SystemAdmin"], [f"{LIS}/institution/person#Administrator", f"{LIS}/system/person#SysAdmin"]),
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def parse_time(value: str) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign(payload: dict) -> str:
    """Et kort, HMAC-signeret token (bearer tokens og LTI-hints), der overlever en genstart."""
    body = b64url(json.dumps(payload, separators=(",", ":")).encode())
    mac = b64url(hmac.new(SIGNING_KEY, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def verify(token: str, kind: str) -> dict | None:
    try:
        body, mac = token.split(".")
    except ValueError:
        return None
    expected = b64url(hmac.new(SIGNING_KEY, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return None
    payload = json.loads(b64url_decode(body))
    if payload.get("k") != kind or payload.get("exp", 0) < time.time():
        return None
    return payload


# ---------------------------------------------------------------------------------------------- data


class Store:
    """Kurser, brugere og medlemskaber fra SEED_DIR, med faste id'er i SQLite (DATA_DIR).

    Id'erne (_N_1 og uuid) og created gemmes første gang en række ses, så de er de samme efter en
    genstart. Rækker, der fjernes fra CSV-filerne, forsvinder fra API'et; deres id'er bevares, hvis de
    kommer igen. Filerne læses igen, når de ændres.
    """

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DATA_DIR / "blackboard.sqlite", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS ids (kind TEXT, key TEXT, pk INTEGER, uuid TEXT, created TEXT, PRIMARY KEY (kind, key))")
        self.lock = threading.Lock()
        self.mtimes: dict[str, float] = {}
        self.terms: list[dict] = []
        self.courses: list[dict] = []
        self.users: list[dict] = []
        self.memberships: list[dict] = []
        self.reload_if_changed()

    def ident(self, kind: str, key: str, created: datetime | None = None) -> tuple[str, str, datetime]:
        row = self.db.execute("SELECT pk, uuid, created FROM ids WHERE kind = ? AND key = ?", (kind, key)).fetchone()
        if row is None:
            pk = (self.db.execute("SELECT COALESCE(MAX(pk), 0) FROM ids WHERE kind = ?", (kind,)).fetchone()[0] or 0) + 1
            row = (pk, uuid.uuid4().hex, iso(created or now()))
            self.db.execute("INSERT INTO ids VALUES (?, ?, ?, ?, ?)", (kind, key, *row))
            self.db.commit()
        return f"_{row[0]}_1", row[1], parse_time(row[2])

    def reload_if_changed(self):
        files = {name: SEED_DIR / f"{name}.csv" for name in ("terms", "courses", "users", "memberships")}
        mtimes = {name: path.stat().st_mtime if path.exists() else 0 for name, path in files.items()}
        if mtimes == self.mtimes:
            return
        with self.lock:
            rows = {name: list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else [] for name, path in files.items()}
            self.load(rows)
            self.mtimes = mtimes
        print(f"Indlæst {len(self.terms)} termer, {len(self.courses)} kurser, {len(self.users)} brugere og {len(self.memberships)} medlemskaber", flush=True)

    def load(self, rows: dict[str, list[dict]]):
        terms = []
        for row in rows["terms"]:
            pk, _, _ = self.ident("term", row["term_id"])
            start, end = (row.get("start") or "").strip(), (row.get("end") or "").strip()
            terms.append({
                "id": pk,
                "externalId": row["term_id"],
                "dataSourceId": DATA_SOURCE,
                "name": row.get("name") or row["term_id"],
                "availability": {
                    "available": "Yes",
                    "duration": {"type": "DateRange", "start": iso(parse_time(start)), "end": iso(parse_time(end) + timedelta(hours=23, minutes=59))}
                    if start and end else {"type": "Continuous"},
                },
            })
        term_ids = {t["externalId"]: t["id"] for t in terms}

        courses = []
        for row in rows["courses"]:
            pk, uid, created = self.ident("course", row["course_id"])
            term = (row.get("term_id") or "").strip()
            start, end = (row.get("start") or "").strip(), (row.get("end") or "").strip()
            if start and end:
                duration = {"type": "DateRange", "start": iso(parse_time(start)), "end": iso(parse_time(end))}
            else:
                duration = {"type": "Term" if term else "Continuous"}
            courses.append({
                "id": pk,
                "uuid": uid,
                "externalId": row["course_id"],
                "dataSourceId": DATA_SOURCE,
                "courseId": row["course_id"],
                "name": row.get("name") or row["course_id"],
                "description": "",
                "created": iso(created),
                "modified": iso(created),
                "organization": (row.get("organization") or "").strip().lower() == "true",
                "ultraStatus": "Ultra",
                "allowGuests": False,
                "allowObservers": False,
                "closedComplete": (row.get("closed_complete") or "").strip().lower() == "true",
                "termId": term_ids.get(term) if term else None,
                "availability": {"available": (row.get("available") or "Yes").strip(), "duration": duration},
                "enrollment": {"type": "InstructorLed"},
                "locale": {"force": False},
                "hasChildren": False,
                "externalAccessUrl": f"{PUBLIC_URL}/ultra/courses/{pk}/outline",
            })
            if not courses[-1]["termId"]:
                del courses[-1]["termId"]

        users = []
        for row in rows["users"]:
            pk, uid, created = self.ident("user", row["user_id"])
            system_role = (row.get("system_role") or "Student").strip()
            users.append({
                "id": pk,
                "uuid": uid,
                "externalId": row["user_id"],
                "dataSourceId": DATA_SOURCE,
                "userName": row.get("username") or row["user_id"],
                "studentId": row["user_id"] if system_role == "Student" else None,
                "created": iso(created),
                "modified": iso(created),
                "systemRoleIds": SYSTEM_ROLES.get(system_role, SYSTEM_ROLES["Student"])[0],
                "institutionRoleIds": [system_role.upper()],
                "availability": {"available": "Yes"},
                "name": {"given": row.get("given") or "", "family": row.get("family") or "", "preferredDisplayName": "GivenName"},
                "contact": {"email": row.get("email") or ""},
                "_systemRole": system_role,
            })
            if users[-1]["studentId"] is None:
                del users[-1]["studentId"]

        course_by_key = {c["courseId"]: c for c in courses}
        user_by_key = {u["externalId"]: u for u in users}
        memberships = []
        for row in rows["memberships"]:
            course, user = course_by_key.get(row["course_id"]), user_by_key.get(row["user_id"])
            if course is None or user is None:
                print(f"Springer medlemskab over uden kursus/bruger: {row}", flush=True)
                continue
            backfilled = now() - timedelta(days=BACKFILL_DAYS)
            _, _, created = self.ident("membership", f"{row['course_id']}|{row['user_id']}", created=backfilled)
            memberships.append({
                "id": f"{course['id']}|{user['id']}",
                "userId": user["id"],
                "courseId": course["id"],
                "dataSourceId": DATA_SOURCE,
                "created": iso(created),
                "modified": iso(created),
                "availability": {"available": (row.get("available") or "Yes").strip()},
                "courseRoleId": (row.get("role") or "Student").strip(),
                "_course": course,
                "_user": user,
                "_created": created,
            })

        self.terms, self.courses, self.users, self.memberships = terms, courses, users, memberships

    def find(self, items: list[dict], ref: str, keys: dict[str, str]) -> dict | None:
        """Et opslag på _N_1 eller prefix:værdi (uuid:, courseId:, userName:, externalId:)."""
        if ":" in ref:
            prefix, value = ref.split(":", 1)
            field = keys.get(prefix)
            if field is None:
                return None
            return next((i for i in items if str(i.get(field)) == value), None)
        return next((i for i in items if i["id"] == ref), None)

    def course(self, ref: str) -> dict | None:
        return self.find(self.courses, ref, {"uuid": "uuid", "courseId": "courseId", "externalId": "externalId"})

    def user(self, ref: str) -> dict | None:
        return self.find(self.users, ref, {"uuid": "uuid", "userName": "userName", "externalId": "externalId"})

    def term(self, ref: str) -> dict | None:
        return self.find(self.terms, ref, {"externalId": "externalId"})


def last_accessed(membership: dict, at: datetime) -> datetime | None:
    """Seneste besøg i kurset til og med `at`, ud fra medlemskabets faste profil.

    Hver dag afgøres af en tilfældighed seedet med medlemskab og dato, så en dag i fortiden altid giver
    det samme. Lærere er inde de fleste hverdage i arbejdstiden, elever sjældnere og også om aftenen.
    """
    course, user = membership["_course"], membership["_user"]
    if course["availability"]["available"] == "Disabled":
        return None
    profile = random.Random(f"{course['uuid']}|{user['uuid']}")
    teaching = membership["courseRoleId"] in ("Instructor", "TeachingAssistant", "Grader", "CourseBuilder")
    weekday_chance = profile.uniform(0.55, 0.95) if teaching else profile.uniform(0.15, 0.8)
    weekend_chance = weekday_chance * (0.15 if teaching else profile.uniform(0.2, 0.6))
    hours = (7, 17) if teaching else (8, 23)

    created = membership["_created"]
    day = at.date()
    while day >= created.date():
        rng = random.Random(f"{course['uuid']}|{user['uuid']}|{day.isoformat()}")
        chance = weekend_chance if day.weekday() >= 5 else weekday_chance
        if rng.random() < chance:
            moment = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(
                hours=rng.randint(*hours), minutes=rng.randint(0, 59), seconds=rng.randint(0, 59), milliseconds=rng.randint(0, 999))
            if created <= moment <= at:
                return moment
        day -= timedelta(days=1)
    return None


STORE: Store


# ----------------------------------------------------------------------------------------- REST-hjælp


def pick(obj: dict, fields: str | None) -> dict:
    """Learn's ?fields=: kun de nævnte (evt. prikkede) felter."""
    obj = {k: v for k, v in obj.items() if not k.startswith("_")}
    if not fields:
        return obj
    out: dict = {}
    for path in (f.strip() for f in fields.split(",") if f.strip()):
        source, target, parts = obj, out, path.split(".")
        for i, part in enumerate(parts):
            if not isinstance(source, dict) or part not in source:
                break
            if i == len(parts) - 1:
                target[part] = source[part]
            else:
                source = source[part]
                target = target.setdefault(part, {})
    return out


def page(path: str, query: dict[str, str], items: list[dict], default_limit: int = 100, max_limit: int = 100) -> dict:
    try:
        offset = max(0, int(query.get("offset", "0")))
        limit = min(max_limit, max(1, int(query.get("limit", str(default_limit)))))
    except ValueError:
        raise ApiError(400, "Invalid paging parameters")
    results = items[offset:offset + limit]
    body: dict = {"results": [pick(i, query.get("fields")) for i in results]}
    if offset + limit < len(items):
        next_query = {**query, "offset": str(offset + limit), "limit": str(limit)}
        body["paging"] = {"nextPage": f"{API}{path}?{urlencode(next_query)}"}
    return body


class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers or {}


def membership_view(m: dict, expand_user: bool) -> dict:
    view = dict(m)
    view.pop("id")
    accessed = last_accessed(m, now())
    if accessed:
        view["lastAccessed"] = iso(accessed)
    if expand_user:
        view["user"] = pick(m["_user"], None)
    return view


def filter_last_accessed(items: list[dict], query: dict[str, str]) -> list[dict]:
    if "lastAccessed" not in query:
        return items
    since = parse_time(query["lastAccessed"])
    compare = query.get("lastAccessedCompare", "greaterOrEqual")
    out = []
    for m in items:
        value = m.get("lastAccessed")
        if value is None:
            continue
        accessed = parse_time(value)
        if (compare == "greaterOrEqual" and accessed >= since) or (compare == "lessThan" and accessed < since):
            out.append(m)
    return out


CALLS = {"day": None, "count": 0}
CALLS_LOCK = threading.Lock()


def count_call():
    if DAILY_CALL_LIMIT <= 0:
        return
    today = now().date()
    with CALLS_LOCK:
        if CALLS["day"] != today:
            CALLS["day"], CALLS["count"] = today, 0
        CALLS["count"] += 1
        if CALLS["count"] > DAILY_CALL_LIMIT:
            midnight = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) + timedelta(days=1)
            wait = max(1, int((midnight - now()).total_seconds()))
            raise ApiError(429, "Rate limit exceeded", {"Retry-After": str(wait)})


def rest(method: str, path: str, query: dict[str, str], headers, body: bytes) -> tuple[int, dict, dict]:
    count_call()

    if path == "/v1/oauth2/token" and method == "POST":
        expected = "Basic " + base64.b64encode(f"{APPLICATION_KEY}:{SECRET}".encode()).decode()
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        if not hmac.compare_digest(headers.get("Authorization", ""), expected):
            return 401, {"error": "invalid_client", "error_description": "Invalid client credentials"}, {}
        if form.get("grant_type") != "client_credentials":
            return 400, {"error": "unsupported_grant_type", "error_description": "Unsupported grant type"}, {}
        token = sign({"k": "access", "exp": int(time.time()) + TOKEN_SECONDS, "n": secrets.token_hex(4)})
        return 200, {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_SECONDS - 1}, {}

    auth = headers.get("Authorization", "")
    if not auth.lower().startswith("bearer ") or verify(auth[7:].strip(), "access") is None:
        raise ApiError(401, "Bearer token is invalid")
    if method != "GET":
        raise ApiError(405, "Method not allowed (mocken kan kun læse)")

    STORE.reload_if_changed()
    parts = path.strip("/").split("/")
    version, resource, rest_parts = parts[0], parts[1] if len(parts) > 1 else "", parts[2:]

    if path == "/v1/system/version":
        return 200, {"learn": {"major": 4000, "minor": 0, "patch": 0, "build": "mock"}}, {}

    if version == "v1" and resource == "terms":
        if not rest_parts:
            return 200, page(path, query, STORE.terms), {}
        term = STORE.term(rest_parts[0])
        if term is None:
            raise ApiError(404, "Term not found")
        return 200, pick(term, query.get("fields")), {}

    if version == "v1" and resource == "courseRoles":
        roles = [{
            "id": f"_{i + 1}_1", "roleId": role_id, "nameForCourses": names[0], "nameForOrganizations": names[1],
            "description": "", "custom": False, "actAsInstructor": role_id in ("Instructor", "TeachingAssistant"),
            "availability": {"available": "Yes"},
        } for i, (role_id, names) in enumerate(COURSE_ROLES.items())]
        return 200, page(path, query, roles), {}

    if version in ("v1", "v2", "v3") and resource == "courses":
        if not rest_parts:
            items = STORE.courses
            if "organization" in query:
                wanted = query["organization"].lower() == "true"
                items = [c for c in items if c["organization"] == wanted]
            if "courseId" in query:
                items = [c for c in items if query["courseId"].lower() in c["courseId"].lower()]
            return 200, page(path, query, items), {}
        course = STORE.course(rest_parts[0])
        if course is None:
            raise ApiError(404, "Course not found")
        if len(rest_parts) == 1:
            return 200, pick(course, query.get("fields")), {}
        if version == "v1" and rest_parts[1] == "users":
            expand = "user" in query.get("expand", "").split(",")
            members = [m for m in STORE.memberships if m["_course"] is course]
            if len(rest_parts) == 3:
                user = STORE.user(rest_parts[2])
                member = next((m for m in members if user and m["_user"] is user), None)
                if member is None:
                    raise ApiError(404, "Membership not found")
                return 200, pick(membership_view(member, expand), query.get("fields")), {}
            views = filter_last_accessed([membership_view(m, expand) for m in members], query)
            return 200, page(path, query, views), {}

    if version == "v1" and resource == "users":
        if not rest_parts:
            return 200, page(path, query, STORE.users), {}
        user = STORE.user(rest_parts[0])
        if user is None:
            raise ApiError(404, "User not found")
        if len(rest_parts) == 1:
            return 200, pick(user, query.get("fields")), {}
        if rest_parts[1] == "courses":
            views = []
            for m in STORE.memberships:
                if m["_user"] is user:
                    view = membership_view(m, False)
                    if "course" in query.get("expand", "").split(","):
                        view["course"] = pick(m["_course"], None)
                    views.append(view)
            return 200, page(path, query, views), {}

    raise ApiError(404, f"Mocken kender ikke {path}")


# ----------------------------------------------------------------------------------------------- LTI


def load_key() -> rsa.RSAPrivateKey:
    path = DATA_DIR / "lti-private.pem"
    if not path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        path.chmod(0o600)
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


LTI_KEY = load_key()
_numbers = LTI_KEY.public_key().public_numbers()
_n = _numbers.n.to_bytes((_numbers.n.bit_length() + 7) // 8, "big")
KID = hashlib.sha256(_n).hexdigest()[:16]
JWK = {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": KID, "n": b64url(_n), "e": b64url(_numbers.e.to_bytes(3, "big"))}


def jwt(claims: dict) -> str:
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": KID}).encode())
    payload = b64url(json.dumps(claims, ensure_ascii=False).encode())
    signature = LTI_KEY.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{b64url(signature)}"


def tool_login_url() -> str:
    return f"{TOOL_URL}/lti/login"


def tool_launch_url() -> str:
    return f"{TOOL_URL}/lti/launch"


def id_token(user: dict, course: dict | None, target: str, nonce: str) -> str:
    issued = int(time.time())
    membership = next((m for m in STORE.memberships if course and m["_course"] is course and m["_user"] is user), None)
    roles = list(SYSTEM_ROLES.get(user["_systemRole"], SYSTEM_ROLES["Student"])[1])
    if membership:
        roles = COURSE_ROLES.get(membership["courseRoleId"], ("", "", [f"{LIS}/membership#Learner"]))[2] + roles
    claims = {
        "iss": ISSUER,
        "aud": APPLICATION_ID,
        "azp": APPLICATION_ID,
        "sub": user["uuid"],
        "iat": issued,
        "exp": issued + 300,
        "nonce": nonce,
        f"{LTI}/message_type": "LtiResourceLinkRequest",
        f"{LTI}/version": "1.3.0",
        f"{LTI}/deployment_id": DEPLOYMENT_ID,
        f"{LTI}/target_link_uri": target,
        f"{LTI}/roles": roles,
        f"{LTI}/tool_platform": {"guid": hashlib.sha256(PUBLIC_URL.encode()).hexdigest()[:32], "name": "Blackboard-mock",
                                  "product_family_code": "BlackboardLearn", "version": "4000.0.0-mock", "url": PUBLIC_URL},
        f"{LTI}/launch_presentation": {"document_target": "iframe", "return_url": f"{PUBLIC_URL}/", "locale": "da-DK"},
        f"{LTI}/custom": {},
        f"{LTI}/lis": {"person_sourcedid": user["externalId"]},
    }
    if course:
        link = hashlib.sha256(f"{course['uuid']}|{target}".encode()).hexdigest()[:32]
        claims[f"{LTI}/context"] = {"id": course["uuid"], "label": course["courseId"], "title": course["name"],
                                   "type": ["http://purl.imsglobal.org/vocab/lis/v2/course#CourseOffering"]}
        claims[f"{LTI}/resource_link"] = {"id": link, "title": "Nexus"}
        claims[f"{LTI}/lis"]["course_section_sourcedid"] = course["externalId"]
    else:
        claims[f"{LTI}/resource_link"] = {"id": "system-tool", "title": "Nexus (Administrator Panel)"}
    return jwt(claims)


def auto_post(url: str, fields: dict[str, str], title: str) -> str:
    inputs = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in fields.items() if v)
    return (f'<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f'<body onload="document.forms[0].submit()" style="font-family:system-ui;padding:2rem">'
            f'<form method="post" action="{html.escape(url)}">{inputs}<p>{html.escape(title)} …</p>'
            f'<noscript><button>Fortsæt</button></noscript></form></body>')


def oidc_auth(params: dict[str, str]) -> tuple[int, str]:
    """Det Developer Portal gør: tjekker login-svaret fra værktøjet og sender id_token tilbage."""
    if params.get("client_id") != APPLICATION_ID:
        return 400, "Ukendt client_id"
    if params.get("response_type") != "id_token" or params.get("scope") != "openid":
        return 400, "response_type skal være id_token og scope openid"
    redirect = params.get("redirect_uri", "")
    if not TOOL_URL or not redirect.startswith(tool_launch_url()):
        return 400, f"redirect_uri skal ligge under {tool_launch_url()}"
    if not params.get("nonce") or not params.get("state"):
        return 400, "nonce og state mangler"
    hint = verify(params.get("lti_message_hint", ""), "launch")
    if hint is None or hint.get("u") != params.get("login_hint"):
        return 400, "Launch-linket er udløbet eller ugyldigt. Start igen fra Blackboard-mocken."
    STORE.reload_if_changed()
    user = STORE.user(f"uuid:{hint['u']}")
    course = STORE.course(f"uuid:{hint['c']}") if hint.get("c") else None
    if user is None or (hint.get("c") and course is None):
        return 400, "Brugeren eller kurset findes ikke længere"
    token = id_token(user, course, hint["t"], params["nonce"])
    return 200, auto_post(redirect, {"id_token": token, "state": params["state"]}, "Sender id_token til Nexus")


def start_launch(params: dict[str, str]) -> tuple[int, str]:
    """Third-party initiated login mod Nexus' /lti/login, som når en bruger klikker på værktøjet i Learn."""
    if not TOOL_URL:
        return 400, "BB_TOOL_URL er ikke sat (--tool-url i blackboard.sh)"
    user = STORE.user(f"uuid:{params.get('user', '')}")
    course = STORE.course(f"uuid:{params['course']}") if params.get("course") else None
    if user is None or (params.get("course") and course is None):
        return 404, "Ukendt bruger eller kursus"
    target = TOOL_URL + "/" + params.get("target", "lti/launch").lstrip("/")
    hint = sign({"k": "launch", "exp": int(time.time()) + HINT_SECONDS, "u": user["uuid"], "c": course["uuid"] if course else None, "t": target})
    return 200, auto_post(tool_login_url(), {
        "iss": ISSUER,
        "login_hint": user["uuid"],
        "target_link_uri": target,
        "lti_message_hint": hint,
        "client_id": APPLICATION_ID,
        "lti_deployment_id": DEPLOYMENT_ID,
    }, f"Åbner Nexus som {user['userName']}")


# ------------------------------------------------------------------------------------------ testside


def ui_page() -> str:
    STORE.reload_if_changed()
    e = html.escape
    jwks = f"{PUBLIC_URL}/api/v1/management/applications/{APPLICATION_ID}/jwks.json"
    settings = [
        ("Base URL (forbindelsen)", PUBLIC_URL),
        ("Application ID", APPLICATION_ID),
        ("Application key", APPLICATION_KEY),
        ("Secret", "sudo grep BB_MOCK_SECRET <path>/blackboard/.env"),
        ("Issuer", ISSUER),
        ("OIDC auth endpoint", f"{PUBLIC_URL}/api/v1/gateway/oidcauth"),
        ("Platform keyset URL", jwks),
        ("Deployment ID", DEPLOYMENT_ID),
        ("Nexus (værktøjet)", TOOL_URL or "ikke sat: --tool-url"),
    ]
    rows = "".join(f"<tr><th>{e(k)}</th><td><code>{e(v)}</code></td></tr>" for k, v in settings)

    admins = [u for u in STORE.users if u["_systemRole"] == "SystemAdmin"]
    admin_links = "".join(
        f'<a class="btn" target="_blank" data-launch href="launch?user={u["uuid"]}">Åbn som {e(u["userName"])} (uden kursus)</a> '
        for u in admins) or "<p>Ingen brugere med system_role SystemAdmin i users.csv.</p>"

    by_course = ""
    for c in STORE.courses:
        members = [m for m in STORE.memberships if m["_course"] is c]
        term = next((t["name"] for t in STORE.terms if t["id"] == c.get("termId")), "ingen term")
        kind = "organisation" if c["organization"] else term
        items = ""
        for m in members:
            u = m["_user"]
            accessed = last_accessed(m, now())
            items += (f'<tr><td>{e(u["userName"])}</td><td>{e(u["name"]["given"])} {e(u["name"]["family"])}</td>'
                      f'<td>{e(m["courseRoleId"])}</td><td>{e(m["availability"]["available"])}</td>'
                      f'<td>{e(accessed.strftime("%d.%m %H:%M") if accessed else "–")}</td>'
                      f'<td><a class="btn" target="_blank" data-launch href="launch?user={u["uuid"]}&course={c["uuid"]}">Åbn i Nexus</a></td></tr>')
        by_course += (f'<h3>{e(c["courseId"])} · {e(c["name"])} <small>({e(kind)}, {e(c["availability"]["available"])}, uuid {e(c["uuid"])})</small></h3>'
                      f'<table><tr><th>Bruger</th><th>Navn</th><th>Rolle</th><th>Tilgængelig</th><th>Sidst inde (UTC)</th><th></th></tr>{items}</table>')

    return f"""<!doctype html><html lang="da"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blackboard-mock</title><style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#d2d2d7;--accent:#5a2a82}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161618;--fg:#f5f5f7;--muted:#a1a1a6;--line:#3a3a3c;--accent:#b48ae0}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1000px;padding:16px}}
table{{border-collapse:collapse;width:100%;margin-bottom:1rem;display:block;overflow-x:auto}}
th,td{{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--muted);font-weight:500}} code{{word-break:break-all}} small{{color:var(--muted);font-weight:400}}
.btn{{display:inline-block;padding:2px 10px;border:1px solid var(--accent);color:var(--accent);border-radius:6px;text-decoration:none;white-space:nowrap}}
input{{font:inherit;padding:2px 6px;width:22rem;max-width:100%}}
</style></head><body>
<h1>Blackboard-mock</h1>
<p>Svarer som Blackboard Learn's REST-API og som Anthology Developer Portal i LTI 1.3. Indtast værdierne her i Nexus.</p>
<table>{rows}</table>
<h2>Åbn i Nexus</h2>
<p><label>Target link (sti i Nexus): <input id="target" value="lti/launch/course-insights"></label></p>
<p>{admin_links}</p>
<h2>Kurser</h2>
{by_course}
<script>
const t=document.getElementById('target');
try{{t.value=localStorage.getItem('target')||t.value}}catch(e){{}}
t.addEventListener('change',()=>{{try{{localStorage.setItem('target',t.value)}}catch(e){{}}}});
document.querySelectorAll('[data-launch]').forEach(a=>a.addEventListener('click',()=>{{
  const u=new URL(a.href);u.searchParams.set('target',t.value);a.href=u.toString();}}));
</script>
</body></html>"""


# ----------------------------------------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "BlackboardMock/1"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def send(self, status: int, body: bytes, content_type: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, status: int, data, headers: dict | None = None):
        self.send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", headers)

    def page(self, status: int, text: str):
        self.send(status, text.encode(), "text/html; charset=utf-8")

    def authorized_ui(self) -> bool:
        expected = "Basic " + base64.b64encode(f"admin:{UI_PASSWORD}".encode()).decode()
        if UI_PASSWORD and hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            return True
        self.send(401, b"Log ind som admin med BB_MOCK_UI_PASSWORD", "text/plain; charset=utf-8",
                  {"WWW-Authenticate": 'Basic realm="Blackboard-mock", charset="UTF-8"'})
        return False

    def handle_any(self, method: str):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else b""
        path = url.path

        try:
            if path == "/health":
                return self.json(200, {"ok": True})

            if path.startswith(API + "/"):
                status, data, headers = rest(method, path[len(API):], query, self.headers, body)
                return self.json(status, data, headers)

            if path == "/api/v1/gateway/oidcauth":
                params = query if method == "GET" else {**query, **{k: v[0] for k, v in parse_qs(body.decode()).items()}}
                status, text = oidc_auth(params)
                return self.page(status, text) if status == 200 else self.send(status, text.encode(), "text/plain; charset=utf-8")

            if path in (f"/api/v1/management/applications/{APPLICATION_ID}/jwks.json", "/.well-known/jwks.json"):
                return self.json(200, {"keys": [JWK]})

            if path in ("/", "/launch"):
                if not self.authorized_ui():
                    return
                if path == "/":
                    return self.page(200, ui_page())
                status, text = start_launch(query)
                return self.page(status, text) if status == 200 else self.send(status, text.encode(), "text/plain; charset=utf-8")

            return self.json(404, {"status": 404, "message": f"Mocken kender ikke {path}"})
        except ApiError as err:
            return self.json(err.status, {"status": err.status, "message": err.message}, err.headers)
        except Exception as err:  # noqa: BLE001
            print(f"Fejl i {method} {self.path}: {err!r}", flush=True)
            return self.json(500, {"status": 500, "message": "Mock-fejl, se docker compose logs"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")


if __name__ == "__main__":
    STORE = Store()
    print(f"Blackboard-mock på :{PORT}, offentlig som {PUBLIC_URL}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
