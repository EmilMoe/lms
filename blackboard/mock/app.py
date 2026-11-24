"""Mock af Blackboard Learn til test af Nexus' Blackboard-integration.

Blackboard Learn er lukket kode og kan ikke køres som Canvas og Moodle. Denne service svarer i stedet
på de kald, Nexus laver mod Learn og Anthology Developer Portal, med kurser, brugere og medlemskaber fra
CSV-filer (SEED_DIR). Det, en Learn-administrator sætter op, gøres i mockens Administrator Panel og
håndhæves som i Learn:

Administrator Panel (/ultra/admin, Basic auth admin / BB_UI_PASSWORD):
  Users → System Roles → Create Role       systemroller med privilegier (entitlements)
  Users → Users → Create User              brugere med en primær systemrolle
  Building Blocks → REST API Integrations  applikations-id bundet til en Learn-bruger
  Integrations → LTI Tool Providers        LTI 1.3-værktøj registreret på client ID; giver deployment ID

REST (Learn's public API, to-benet OAuth2 med applikationens key og secret):
  POST /learn/api/public/v1/oauth2/token                  kun når applikationen er en REST API-integration
  GET  /learn/api/public/v1/terms[/{id}]                  term.VIEW
  GET  /learn/api/public/v3/courses[/{id}]                system.course.VIEW; uuid kun med
                                                          course.configure-properties.EXECUTE
  GET  /learn/api/public/v1/courses/{id}/users[/{userId}] course.user.VIEW; user.uuid m.m. kun med system.user.VIEW
  GET  /learn/api/public/v1/users[/{id}]                  system.user.VIEW
  GET  /learn/api/public/v1/users/{id}/courses            system.user.VIEW og course.user.VIEW
  GET  /learn/api/public/v1/courseRoles                   system.courserole.manager.VIEW
  GET  /learn/api/public/v1/system/version
  Integrationens Learn-bruger bestemmer rettighederne: System Administrator har alle, User ingen, og en
  egen systemrolle de privilegier, der er slået til. Id'er i stier: _123_1, uuid:..., courseId:... /
  userName:..., externalId:... ?fields=, ?limit=, ?offset= og paging.nextPage som i Learn.

LTI 1.3 (det Developer Portal gør for en rigtig Learn), kun for et godkendt (Approved) værktøj:
  GET|POST /api/v1/gateway/oidcauth                           OIDC-auth: svarer med id_token (form_post)
  GET      /api/v1/management/applications/{id}/jwks.json     platformens offentlige nøgle

Testside (/, samme login): opsætningens status, værdierne til Nexus og "Åbn i Nexus" pr. medlem.

lastAccessed: Learn's REST-API har ingen aktivitetslog, kun hvert medlemskabs lastAccessed. Mocken
regner den ud fra en fast profil pr. medlemskab og dag (deterministisk tilfældighed): fortiden ændrer sig
aldrig, og den flytter sig kun frem, når tiden går. Medlemskaber, mocken ser første gang, får
BB_BACKFILL_DAYS dages historik (created sættes så langt tilbage).

Konfiguration (miljøvariabler):
  BB_PUBLIC_URL                         mockens offentlige rod-URL, fx https://blackboard.example.com
  BB_ISSUER                             LTI-issuer (standard: BB_PUBLIC_URL)
  BB_APPLICATION_ID                     applikationens id i "Developer Portal" = LTI client_id
  BB_APPLICATION_KEY, BB_SECRET         applikationens REST-login
  BB_TOOL_URL                           Nexus' rod-URL, fx https://nexus.example.com (login: /lti/login)
  BB_UI_PASSWORD                        kodeord til testsiden og Administrator Panel
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
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PUBLIC_URL = os.environ["BB_PUBLIC_URL"].rstrip("/")
ISSUER = os.environ.get("BB_ISSUER") or PUBLIC_URL
APPLICATION_ID = os.environ["BB_APPLICATION_ID"]
APPLICATION_KEY = os.environ["BB_APPLICATION_KEY"]
SECRET = os.environ["BB_SECRET"]
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

# Institutionsrolle (system_role i users.csv) -> LTI's institution-/systemroller.
INSTITUTION_ROLES = {
    "Student": [f"{LIS}/institution/person#Student", f"{LIS}/system/person#User"],
    "Faculty": [f"{LIS}/institution/person#Faculty", f"{LIS}/system/person#User"],
    "SystemAdmin": [f"{LIS}/institution/person#Administrator", f"{LIS}/system/person#SysAdmin"],
}

# Learn's indbyggede systemroller: id -> navn. SystemAdmin har alle privilegier, User ingen.
BUILTIN_SYSTEM_ROLES = {"SystemAdmin": "System Administrator", "User": "User"}

# Privilegier, som Administrator Panel viser dem: entitlement -> navn. Navnene ligner Learn's, men er
# mockens egne; det er entitlementet, der tæller. Med nogle, Nexus ikke skal bruge.
PRIVILEGES = {
    "term.VIEW": "Administrator Panel (Courses) > Terms > View",
    "term.MODIFY": "Administrator Panel (Courses) > Terms > Edit",
    "system.course.VIEW": "Administrator Panel (Courses) > Courses > View",
    "system.course.CREATE": "Administrator Panel (Courses) > Courses > Create Course",
    "course.configure-properties.EXECUTE": "Course/Organization Control Panel (Customization) > Properties",
    "course.user.VIEW": "Course/Organization Control Panel (Users and Groups) > Users",
    "course.user.MODIFY": "Course/Organization Control Panel (Users and Groups) > Users > Change User's Role",
    "course.gradebook.MODIFY": "Course/Organization Control Panel (Grade Center) > Full Control",
    "system.user.VIEW": "Administrator Panel (Users) > Users",
    "system.user.CREATE": "Administrator Panel (Users) > Users > Create User",
    "system.user.DELETE": "Administrator Panel (Users) > Users > Delete User",
    "system.courserole.manager.VIEW": "Administrator Panel (Users) > Course/Organization Roles",
    "system.systemrole.manager.VIEW": "Administrator Panel (Users) > System Roles",
    "system.datasource.VIEW": "Administrator Panel (Data Integration) > Data Sources",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
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
        expected = b64url(hmac.new(SIGNING_KEY, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, expected):
            return None
        payload = json.loads(b64url_decode(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("k") != kind or payload.get("exp", 0) < time.time():
        return None
    return payload


# ---------------------------------------------------------------------------------------------- data


class Store:
    """Kurser, brugere og medlemskaber fra SEED_DIR plus det, der er sat op i Administrator Panel.

    Alt ligger i SQLite (DATA_DIR): id'erne (_N_1 og uuid) og created for hver CSV-række, så de er de
    samme efter en genstart, og systemroller, oprettede brugere, REST-integrationer og LTI-værktøjer.
    Rækker, der fjernes fra CSV-filerne, forsvinder fra API'et; deres id'er bevares, hvis de kommer
    igen. Filerne læses igen, når de ændres.
    """

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DATA_DIR / "blackboard.sqlite", check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS ids (kind TEXT, key TEXT, pk INTEGER, uuid TEXT, created TEXT, PRIMARY KEY (kind, key));
            CREATE TABLE IF NOT EXISTS system_roles (role_id TEXT PRIMARY KEY, name TEXT, entitlements TEXT);
            CREATE TABLE IF NOT EXISTS admin_users (user_name TEXT PRIMARY KEY, given TEXT, family TEXT, email TEXT, system_role TEXT);
            CREATE TABLE IF NOT EXISTS seed_user_roles (user_name TEXT PRIMARY KEY, system_role TEXT);
            CREATE TABLE IF NOT EXISTS rest_integrations (application_id TEXT PRIMARY KEY, user_name TEXT, end_user_access INTEGER,
                act_as_user INTEGER, created TEXT);
            CREATE TABLE IF NOT EXISTS lti_tools (client_id TEXT PRIMARY KEY, deployment_id TEXT, status TEXT, send_role INTEGER,
                send_name INTEGER, send_email INTEGER, grades INTEGER, memberships INTEGER, created TEXT);
        """)
        self.lock = threading.RLock()
        self.mtimes: dict[str, float] | None = None
        self.terms: list[dict] = []
        self.courses: list[dict] = []
        self.users: list[dict] = []
        self.memberships: list[dict] = []
        self.reload()

    # -- id'er og indlæsning

    def ident(self, kind: str, key: str, created: datetime | None = None) -> tuple[str, str, datetime]:
        row = self.db.execute("SELECT pk, uuid, created FROM ids WHERE kind = ? AND key = ?", (kind, key)).fetchone()
        if row is None:
            pk = (self.db.execute("SELECT COALESCE(MAX(pk), 0) FROM ids WHERE kind = ?", (kind,)).fetchone()[0] or 0) + 1
            row = (pk, uuid.uuid4().hex, iso(created or now()))
            self.db.execute("INSERT INTO ids VALUES (?, ?, ?, ?, ?)", (kind, key, *row))
            self.db.commit()
        return f"_{row[0]}_1", row[1], parse_time(row[2])

    def reload(self, force: bool = False):
        files = {name: SEED_DIR / f"{name}.csv" for name in ("terms", "courses", "users", "memberships")}
        mtimes = {name: path.stat().st_mtime if path.exists() else 0 for name, path in files.items()}
        if mtimes == self.mtimes and not force:
            return
        with self.lock:
            rows = {name: list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else [] for name, path in files.items()}
            self.load(rows)
            self.mtimes = mtimes
        if not force:
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
            course = {
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
                "availability": {"available": (row.get("available") or "Yes").strip(), "duration": duration},
                "enrollment": {"type": "InstructorLed"},
                "locale": {"force": False},
                "hasChildren": False,
                "externalAccessUrl": f"{PUBLIC_URL}/ultra/courses/{pk}/outline",
            }
            if term_ids.get(term):
                course["termId"] = term_ids[term]
            courses.append(course)

        seed_roles = dict(self.db.execute("SELECT user_name, system_role FROM seed_user_roles"))
        users = []
        for row in rows["users"]:
            institution = (row.get("system_role") or "Student").strip()
            user_name = row.get("username") or row["user_id"]
            default_role = "SystemAdmin" if institution == "SystemAdmin" else "User"
            users.append(self.user_record("user", row["user_id"], user_name, row.get("given") or "", row.get("family") or "",
                                          row.get("email") or "", seed_roles.get(user_name, default_role), institution, seeded=True))
        for user_name, given, family, email, system_role in self.db.execute("SELECT * FROM admin_users ORDER BY rowid"):
            users.append(self.user_record("admin-user", user_name, user_name, given, family, email, system_role, None, seeded=False))

        course_by_key = {c["courseId"]: c for c in courses}
        user_by_key = {u["externalId"]: u for u in users if u["_seeded"]}
        memberships = []
        for row in rows["memberships"]:
            course, user = course_by_key.get(row["course_id"]), user_by_key.get(row["user_id"])
            if course is None or user is None:
                print(f"Springer medlemskab over uden kursus/bruger: {row}", flush=True)
                continue
            _, _, created = self.ident("membership", f"{row['course_id']}|{row['user_id']}", created=now() - timedelta(days=BACKFILL_DAYS))
            memberships.append({
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

    def user_record(self, kind, key, user_name, given, family, email, system_role, institution, seeded) -> dict:
        pk, uid, created = self.ident(kind, key)
        user = {
            "id": pk,
            "uuid": uid,
            "externalId": key,
            "dataSourceId": DATA_SOURCE,
            "userName": user_name,
            "created": iso(created),
            "modified": iso(created),
            "systemRoleIds": [system_role],
            "institutionRoleIds": [institution.upper()] if institution else [],
            "availability": {"available": "Yes"},
            "name": {"given": given, "family": family, "preferredDisplayName": "GivenName"},
            "contact": {"email": email},
            "_institution": institution,
            "_seeded": seeded,
        }
        if institution == "Student":
            user["studentId"] = key
        return user

    # -- opslag

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

    # -- Administrator Panel

    def system_roles(self) -> list[dict]:
        roles = [{"role_id": rid, "name": name, "builtin": True,
                  "entitlements": set(PRIVILEGES) if rid == "SystemAdmin" else set()} for rid, name in BUILTIN_SYSTEM_ROLES.items()]
        for role_id, name, entitlements in self.db.execute("SELECT role_id, name, entitlements FROM system_roles ORDER BY rowid"):
            roles.append({"role_id": role_id, "name": name, "builtin": False, "entitlements": set(json.loads(entitlements))})
        return roles

    def system_role(self, role_id: str) -> dict | None:
        return next((r for r in self.system_roles() if r["role_id"] == role_id), None)

    def entitlements_of(self, user: dict) -> set[str]:
        if "SystemAdmin" in user["systemRoleIds"]:
            return {"*"}
        role = self.system_role(user["systemRoleIds"][0])
        return role["entitlements"] if role else set()

    def write(self, sql: str, params: tuple = (), reload: bool = False):
        with self.lock:
            self.db.execute(sql, params)
            self.db.commit()
            if reload:
                self.reload(force=True)

    def rest_integration(self, application_id: str) -> dict | None:
        row = self.db.execute("SELECT application_id, user_name, end_user_access, act_as_user, created FROM rest_integrations WHERE application_id = ?",
                              (application_id,)).fetchone()
        return dict(zip(("application_id", "user_name", "end_user_access", "act_as_user", "created"), row)) if row else None

    def rest_integrations(self) -> list[dict]:
        return [self.rest_integration(r[0]) for r in self.db.execute("SELECT application_id FROM rest_integrations ORDER BY rowid")]

    def lti_tool(self, client_id: str) -> dict | None:
        cols = ("client_id", "deployment_id", "status", "send_role", "send_name", "send_email", "grades", "memberships", "created")
        row = self.db.execute(f"SELECT {', '.join(cols)} FROM lti_tools WHERE client_id = ?", (client_id,)).fetchone()
        return dict(zip(cols, row)) if row else None

    def lti_tools(self) -> list[dict]:
        return [self.lti_tool(r[0]) for r in self.db.execute("SELECT client_id FROM lti_tools ORDER BY rowid")]


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


# ----------------------------------------------------------------------------------------------- REST


class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers or {}


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


def page(path: str, query: dict[str, str], items: list[dict]) -> dict:
    try:
        offset = max(0, int(query.get("offset", "0")))
        limit = min(100, max(1, int(query.get("limit", "100"))))
    except ValueError:
        raise ApiError(400, "Invalid paging parameters")
    body: dict = {"results": [pick(i, query.get("fields")) for i in items[offset:offset + limit]]}
    if offset + limit < len(items):
        body["paging"] = {"nextPage": f"{API}{path}?{urlencode({**query, 'offset': str(offset + limit), 'limit': str(limit)})}"}
    return body


class Caller:
    """Integrationens Learn-bruger og dens entitlements, som afgør hvad kaldet må."""

    def __init__(self, user: dict):
        self.user = user
        self.entitlements = STORE.entitlements_of(user)

    def has(self, entitlement: str) -> bool:
        return "*" in self.entitlements or entitlement in self.entitlements

    def require(self, *entitlements: str):
        missing = [e for e in entitlements if not self.has(e)]
        if missing:
            raise ApiError(403, f"User {self.user['userName']} is not authorized: missing entitlement {', '.join(missing)}")

    def course(self, course: dict) -> dict:
        # Uden course.configure-properties.EXECUTE viser Learn ikke kursets uuid.
        if self.has("course.configure-properties.EXECUTE"):
            return course
        return {k: v for k, v in course.items() if k != "uuid"}

    def membership(self, m: dict, expand_user: bool) -> dict:
        view = dict(m)
        accessed = last_accessed(m, now())
        if accessed:
            view["lastAccessed"] = iso(accessed)
        if expand_user:
            # Uden system.user.VIEW kommer kun brugerens id med.
            view["user"] = pick(m["_user"], None) if self.has("system.user.VIEW") else {"id": m["_user"]["id"]}
        return view


def caller_for_application() -> Caller:
    """Integrationen for applikationen, eller 401 som i Learn, når den ikke (længere) er tilføjet."""
    integration = STORE.rest_integration(APPLICATION_ID)
    user = STORE.user(f"userName:{integration['user_name']}") if integration else None
    if integration is None or user is None:
        raise ApiError(401, "Application is not registered as a REST API integration on this Learn site")
    return Caller(user)


def filter_last_accessed(items: list[dict], query: dict[str, str]) -> list[dict]:
    if "lastAccessed" not in query:
        return items
    since = parse_time(query["lastAccessed"])
    compare = query.get("lastAccessedCompare", "greaterOrEqual")
    out = []
    for m in items:
        if m.get("lastAccessed") is None:
            continue
        accessed = parse_time(m["lastAccessed"])
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
            raise ApiError(429, "Rate limit exceeded", {"Retry-After": str(max(1, int((midnight - now()).total_seconds())))})


def rest(method: str, path: str, query: dict[str, str], headers, body: bytes) -> tuple[int, dict, dict]:
    count_call()
    STORE.reload()

    if path == "/v1/oauth2/token" and method == "POST":
        expected = "Basic " + base64.b64encode(f"{APPLICATION_KEY}:{SECRET}".encode()).decode()
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        if not hmac.compare_digest(headers.get("Authorization", ""), expected):
            return 401, {"error": "invalid_client", "error_description": "Invalid client credentials"}, {}
        if form.get("grant_type") != "client_credentials":
            return 400, {"error": "unsupported_grant_type", "error_description": "Unsupported grant type"}, {}
        try:
            caller_for_application()
        except ApiError as err:
            return 401, {"error": "invalid_client", "error_description": err.message}, {}
        token = sign({"k": "access", "exp": int(time.time()) + TOKEN_SECONDS, "n": secrets.token_hex(4)})
        return 200, {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_SECONDS - 1}, {}

    auth = headers.get("Authorization", "")
    if not auth.lower().startswith("bearer ") or verify(auth[7:].strip(), "access") is None:
        raise ApiError(401, "Bearer token is invalid")
    if method != "GET":
        raise ApiError(405, "Method not allowed (mocken kan kun læse)")
    caller = caller_for_application()

    parts = path.strip("/").split("/")
    version, resource, rest_parts = parts[0], parts[1] if len(parts) > 1 else "", parts[2:]

    if path == "/v1/system/version":
        return 200, {"learn": {"major": 4000, "minor": 0, "patch": 0, "build": "mock"}}, {}

    if version == "v1" and resource == "terms":
        caller.require("term.VIEW")
        if not rest_parts:
            return 200, page(path, query, STORE.terms), {}
        term = STORE.term(rest_parts[0])
        if term is None:
            raise ApiError(404, "Term not found")
        return 200, pick(term, query.get("fields")), {}

    if version == "v1" and resource == "courseRoles":
        caller.require("system.courserole.manager.VIEW")
        roles = [{
            "id": f"_{i + 1}_1", "roleId": role_id, "nameForCourses": names[0], "nameForOrganizations": names[1],
            "description": "", "custom": False, "actAsInstructor": role_id in ("Instructor", "TeachingAssistant"),
            "availability": {"available": "Yes"},
        } for i, (role_id, names) in enumerate(COURSE_ROLES.items())]
        return 200, page(path, query, roles), {}

    if version in ("v1", "v2", "v3") and resource == "courses":
        if not rest_parts:
            caller.require("system.course.VIEW")
            items = STORE.courses
            if "organization" in query:
                wanted = query["organization"].lower() == "true"
                items = [c for c in items if c["organization"] == wanted]
            if "courseId" in query:
                items = [c for c in items if query["courseId"].lower() in c["courseId"].lower()]
            return 200, page(path, query, [caller.course(c) for c in items]), {}
        course = STORE.course(rest_parts[0])
        if course is None:
            raise ApiError(404, "Course not found")
        if len(rest_parts) == 1:
            caller.require("system.course.VIEW")
            return 200, pick(caller.course(course), query.get("fields")), {}
        if version == "v1" and rest_parts[1] == "users":
            caller.require("course.user.VIEW")
            expand = "user" in query.get("expand", "").split(",")
            members = [m for m in STORE.memberships if m["_course"] is course]
            if len(rest_parts) == 3:
                user = STORE.user(rest_parts[2])
                member = next((m for m in members if user and m["_user"] is user), None)
                if member is None:
                    raise ApiError(404, "Membership not found")
                return 200, pick(caller.membership(member, expand), query.get("fields")), {}
            views = filter_last_accessed([caller.membership(m, expand) for m in members], query)
            return 200, page(path, query, views), {}

    if version == "v1" and resource == "users":
        caller.require("system.user.VIEW")
        if not rest_parts:
            return 200, page(path, query, STORE.users), {}
        user = STORE.user(rest_parts[0])
        if user is None:
            raise ApiError(404, "User not found")
        if len(rest_parts) == 1:
            return 200, pick(user, query.get("fields")), {}
        if rest_parts[1] == "courses":
            caller.require("course.user.VIEW")
            views = []
            for m in STORE.memberships:
                if m["_user"] is user:
                    view = caller.membership(m, False)
                    if "course" in query.get("expand", "").split(","):
                        view["course"] = pick(caller.course(m["_course"]), None)
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


DATA_DIR.mkdir(parents=True, exist_ok=True)
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


def approved_tool() -> dict | None:
    tool = STORE.lti_tool(APPLICATION_ID)
    return tool if tool and tool["status"] == "Approved" else None


def id_token(tool: dict, user: dict, course: dict | None, target: str, nonce: str) -> str:
    issued = int(time.time())
    membership = next((m for m in STORE.memberships if course and m["_course"] is course and m["_user"] is user), None)
    roles: list[str] = []
    if tool["send_role"]:
        roles = list(INSTITUTION_ROLES.get(user["_institution"] or "", []))
        if "SystemAdmin" in user["systemRoleIds"] and f"{LIS}/system/person#SysAdmin" not in roles:
            roles += [f"{LIS}/institution/person#Administrator", f"{LIS}/system/person#SysAdmin"]
        if not roles:
            roles = [f"{LIS}/system/person#User"]
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
        f"{LTI}/deployment_id": tool["deployment_id"],
        f"{LTI}/target_link_uri": target,
        f"{LTI}/roles": roles,
        f"{LTI}/tool_platform": {"guid": hashlib.sha256(PUBLIC_URL.encode()).hexdigest()[:32], "name": "Blackboard-mock",
                                  "product_family_code": "BlackboardLearn", "version": "4000.0.0-mock", "url": PUBLIC_URL},
        f"{LTI}/launch_presentation": {"document_target": "iframe", "return_url": f"{PUBLIC_URL}/", "locale": "da-DK"},
        f"{LTI}/custom": {},
        f"{LTI}/lis": {"person_sourcedid": user["externalId"]},
    }
    if tool["send_name"]:
        claims.update({"given_name": user["name"]["given"], "family_name": user["name"]["family"],
                       "name": f"{user['name']['given']} {user['name']['family']}".strip()})
    if tool["send_email"] and user["contact"]["email"]:
        claims["email"] = user["contact"]["email"]
    if course:
        claims[f"{LTI}/context"] = {"id": course["uuid"], "label": course["courseId"], "title": course["name"],
                                   "type": [f"{LIS}/course#CourseOffering"]}
        claims[f"{LTI}/resource_link"] = {"id": hashlib.sha256(f"{course['uuid']}|{target}".encode()).hexdigest()[:32], "title": "Nexus"}
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
    tool = approved_tool()
    if tool is None:
        return 403, "Værktøjet er ikke registreret og godkendt under Administrator Panel → Integrations → LTI Tool Providers"
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
    STORE.reload()
    user = STORE.user(f"uuid:{hint['u']}")
    course = STORE.course(f"uuid:{hint['c']}") if hint.get("c") else None
    if user is None or (hint.get("c") and course is None):
        return 400, "Brugeren eller kurset findes ikke længere"
    token = id_token(tool, user, course, hint["t"], params["nonce"])
    return 200, auto_post(redirect, {"id_token": token, "state": params["state"]}, "Sender id_token til Nexus")


def start_launch(params: dict[str, str]) -> tuple[int, str]:
    """Third-party initiated login mod Nexus' /lti/login, som når en bruger klikker på værktøjet i Learn."""
    if not TOOL_URL:
        return 400, "BB_TOOL_URL er ikke sat (--tool-url i blackboard.sh)"
    tool = approved_tool()
    if tool is None:
        return 403, "Værktøjet er ikke registreret og godkendt under Administrator Panel → Integrations → LTI Tool Providers"
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
        "lti_deployment_id": tool["deployment_id"],
    }, f"Åbner Nexus som {user['userName']}")


# ---------------------------------------------------------------------------------------- HTML-sider

e = html.escape

CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#d2d2d7;--accent:#5a2a82;--ok:#1a7f37;--warn:#b35900;--panel:#f5f5f7}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f5f5f7;--muted:#a1a1a6;--line:#3a3a3c;--accent:#b48ae0;--ok:#4ac26b;--warn:#f0a050;--panel:#232326}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1000px;padding:16px}
a{color:var(--accent)} table{border-collapse:collapse;width:100%;margin-bottom:1rem;display:block;overflow-x:auto}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500} code{word-break:break-all} small,.muted{color:var(--muted);font-weight:400}
.btn,button{display:inline-block;padding:3px 12px;border:1px solid var(--accent);color:var(--accent);background:none;border-radius:6px;
  text-decoration:none;white-space:nowrap;font:inherit;cursor:pointer}
button.primary{background:var(--accent);color:var(--bg)} button.danger{border-color:var(--warn);color:var(--warn)}
input[type=text],select{font:inherit;padding:3px 6px;width:24rem;max-width:100%;box-sizing:border-box}
label{display:block;margin:.5rem 0} .crumbs{color:var(--muted);margin-bottom:.5rem} .crumbs a{text-decoration:none}
.ok{color:var(--ok)} .warn{color:var(--warn)} .panel{background:var(--panel);border-radius:8px;padding:12px 16px;margin:1rem 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px} form.inline{display:inline}
"""


def layout(title: str, body: str, crumbs: list[tuple[str, str | None]] | None = None) -> str:
    trail = ""
    if crumbs:
        trail = '<div class="crumbs">' + " › ".join(
            f'<a href="{e(href)}">{e(label)}</a>' if href else e(label) for label, href in crumbs) + "</div>"
    return (f'<!doctype html><html lang="da"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{e(title)} · Blackboard-mock</title><style>{CSS}</style></head><body>'
            f'<p class="muted"><a href="/">Testside</a> · <a href="/ultra/admin">Administrator Panel</a></p>{trail}'
            f'<h1>{e(title)}</h1>{body}</body></html>')


ADMIN = ("Administrator Panel", "/ultra/admin")


def check_tag(value: bool, yes: str = "Ja", no: str = "Nej") -> str:
    return f'<span class="ok">✓ {e(yes)}</span>' if value else f'<span class="warn">✗ {e(no)}</span>'


def setup_status() -> list[tuple[str, bool, str]]:
    integration = STORE.rest_integration(APPLICATION_ID)
    user = STORE.user(f"userName:{integration['user_name']}") if integration else None
    tool = STORE.lti_tool(APPLICATION_ID)
    missing = []
    if user:
        caller = Caller(user)
        needed = ["term.VIEW", "system.course.VIEW", "course.configure-properties.EXECUTE", "course.user.VIEW",
                  "system.user.VIEW", "system.courserole.manager.VIEW"]
        missing = [x for x in needed if not caller.has(x)]
    return [
        ("REST API-integration for applikationen", integration is not None,
         f"Learn-bruger {integration['user_name']}" if integration else "Building Blocks → REST API Integrations"),
        ("Integrationens Learn-bruger findes", user is not None, user["userName"] if user else "Users → Create User"),
        ("Brugerens systemrolle har Nexus' entitlements", user is not None and not missing,
         ("mangler " + ", ".join(missing)) if missing else (user["systemRoleIds"][0] if user else "")),
        ("LTI-værktøj registreret og godkendt", bool(tool and tool["status"] == "Approved"),
         f"deployment {tool['deployment_id']}, {tool['status']}" if tool else "Integrations → LTI Tool Providers"),
    ]


def test_page() -> str:
    STORE.reload()
    tool = STORE.lti_tool(APPLICATION_ID)
    settings = [
        ("Base URL (forbindelsen)", PUBLIC_URL),
        ("Application ID / client ID", APPLICATION_ID),
        ("Application key", APPLICATION_KEY),
        ("Secret", "sudo grep BB_MOCK_SECRET <path>/blackboard/.env"),
        ("Issuer", ISSUER),
        ("OIDC auth endpoint", f"{PUBLIC_URL}/api/v1/gateway/oidcauth"),
        ("Platform keyset URL", f"{PUBLIC_URL}/api/v1/management/applications/{APPLICATION_ID}/jwks.json"),
        ("Deployment ID", tool["deployment_id"] if tool else "registrér værktøjet først (LTI Tool Providers)"),
        ("Nexus (værktøjet)", TOOL_URL or "ikke sat: --tool-url"),
    ]
    rows = "".join(f"<tr><th>{e(k)}</th><td><code>{e(v)}</code></td></tr>" for k, v in settings)
    status = "".join(f"<tr><td>{check_tag(ok, 'OK', 'Mangler')}</td><td>{e(label)}</td><td class='muted'>{e(note)}</td></tr>"
                     for label, ok, note in setup_status())
    can_launch = approved_tool() is not None and bool(TOOL_URL)

    def launch(href: str, label: str) -> str:
        if not can_launch:
            return '<span class="muted">–</span>'
        return f'<a class="btn" target="_blank" data-launch href="{e(href)}">{e(label)}</a>'

    admins = [u for u in STORE.users if "SystemAdmin" in u["systemRoleIds"]]
    admin_links = " ".join(launch(f"launch?user={u['uuid']}", f"Åbn som {u['userName']} (uden kursus)") for u in admins)

    by_course = ""
    for c in STORE.courses:
        term = next((t["name"] for t in STORE.terms if t["id"] == c.get("termId")), "ingen term")
        kind = "organisation" if c["organization"] else term
        items = ""
        for m in (m for m in STORE.memberships if m["_course"] is c):
            u = m["_user"]
            accessed = last_accessed(m, now())
            items += (f'<tr><td>{e(u["userName"])}</td><td>{e(u["name"]["given"])} {e(u["name"]["family"])}</td>'
                      f'<td>{e(m["courseRoleId"])}</td><td>{e(m["availability"]["available"])}</td>'
                      f'<td>{e(accessed.strftime("%d.%m %H:%M") if accessed else "–")}</td>'
                      f'<td>{launch("launch?user=" + u["uuid"] + "&course=" + c["uuid"], "Åbn i Nexus")}</td></tr>')
        by_course += (f'<h3>{e(c["courseId"])} · {e(c["name"])} <small>({e(kind)}, {e(c["availability"]["available"])}, uuid {e(c["uuid"])})</small></h3>'
                      f'<table><tr><th>Bruger</th><th>Navn</th><th>Rolle</th><th>Tilgængelig</th><th>Sidst inde (UTC)</th><th></th></tr>{items}</table>')

    note = "" if can_launch else ('<p class="warn">Knapperne kommer, når værktøjet er registreret og godkendt under '
                                   '<a href="/ultra/admin/lti-tools">LTI Tool Providers</a>' + ("" if TOOL_URL else " og --tool-url er sat") + ".</p>")
    body = f"""
<p>Svarer som Blackboard Learn's REST-API og som Anthology Developer Portal i LTI 1.3. Sæt Nexus op med værdierne her,
og følg Nexus' guide i <a href="/ultra/admin">Administrator Panel</a>.</p>
<table>{rows}</table>
<h2>Opsætning i Blackboard</h2>
<table>{status}</table>
<h2>Åbn i Nexus</h2>
{note}
<p><label>Target link (sti i Nexus): <input type="text" id="target" value="lti/launch/course-insights"></label></p>
<p>{admin_links}</p>
<h2>Kurser</h2>
{by_course}
<script>
const t=document.getElementById('target');
try{{t.value=localStorage.getItem('target')||t.value}}catch(e){{}}
t.addEventListener('change',()=>{{try{{localStorage.setItem('target',t.value)}}catch(e){{}}}});
document.querySelectorAll('[data-launch]').forEach(a=>a.addEventListener('click',()=>{{
  const u=new URL(a.href);u.searchParams.set('target',t.value);a.href=u.toString();}}));
</script>"""
    return layout("Blackboard-mock", body)


def admin_home() -> str:
    body = """
<div class="grid">
  <div class="panel"><h3>Users</h3><p><a href="/ultra/admin/users">Users</a><br><a href="/ultra/admin/system-roles">System Roles</a></p></div>
  <div class="panel"><h3>Integrations</h3><p><a href="/ultra/admin/lti-tools">LTI Tool Providers</a></p></div>
  <div class="panel"><h3>Building Blocks</h3><p><a href="/ultra/admin/rest-integrations">REST API Integrations</a></p></div>
</div>
<p class="muted">Kurser, brugere og medlemskaber kommer fra CSV-filerne i seed/. Her sættes det op, en Learn-administrator gør for Nexus.</p>"""
    return layout("Administrator Panel", body)


def system_roles_page() -> str:
    rows = ""
    for r in STORE.system_roles():
        count = "alle" if r["role_id"] == "SystemAdmin" else str(len(r["entitlements"]))
        actions = "" if r["builtin"] else (
            f'<a class="btn" href="/ultra/admin/system-roles/{quote(r["role_id"])}">Privileges</a> '
            f'<form class="inline" method="post" action="/ultra/admin/system-roles/{quote(r["role_id"])}/delete">'
            f'<button class="danger">Delete</button></form>')
        rows += f"<tr><td>{e(r['name'])}</td><td><code>{e(r['role_id'])}</code></td><td>{count}</td><td>{actions}</td></tr>"
    body = f"""
<table><tr><th>Role Name</th><th>Role ID</th><th>Privilegier</th><th></th></tr>{rows}</table>
<div class="panel"><h3>Create Role</h3>
<form method="post" action="/ultra/admin/system-roles">
<label>Role Name<br><input type="text" name="name" required></label>
<label>Role ID<br><input type="text" name="role_id" required pattern="[A-Za-z0-9_.\\- ]+"></label>
<button class="primary">Submit</button></form></div>"""
    return layout("System Roles", body, [ADMIN, ("Users", None), ("System Roles", None)])


def privileges_page(role: dict) -> str:
    rows = "".join(
        f'<tr data-row><td><input type="checkbox" name="entitlement" value="{e(ent)}" id="p{i}"{" checked" if ent in role["entitlements"] else ""}></td>'
        f'<td><label for="p{i}" title="{e(ent)}" style="margin:0">{e(name)}</label></td><td><code>{e(ent)}</code></td></tr>'
        for i, (ent, name) in enumerate(PRIVILEGES.items()))
    body = f"""
<p>Slå privilegierne til, rollen skal have. Filtrér på navn eller entitlement.</p>
<p><input type="text" id="filter" placeholder="Filter, fx system.user"></p>
<form method="post" action="/ultra/admin/system-roles/{quote(role['role_id'])}">
<table><tr><th>Allow</th><th>Privilege</th><th>Entitlement</th></tr>{rows}</table>
<button class="primary">Submit</button></form>
<script>
document.getElementById('filter').addEventListener('input',ev=>{{const q=ev.target.value.toLowerCase();
document.querySelectorAll('[data-row]').forEach(r=>r.style.display=r.textContent.toLowerCase().includes(q)?'':'none');}});
</script>"""
    return layout(f"Privileges: {role['name']}", body, [ADMIN, ("Users", None), ("System Roles", "/ultra/admin/system-roles"), (role["name"], None)])


def users_page() -> str:
    roles = STORE.system_roles()
    role_names = {r["role_id"]: r["name"] for r in roles}
    rows = ""
    for u in STORE.users:
        role_id = u["systemRoleIds"][0]
        options = "".join(f'<option value="{e(r["role_id"])}"{" selected" if r["role_id"] == role_id else ""}>{e(r["name"])}</option>' for r in roles)
        change = (f'<form class="inline" method="post" action="/ultra/admin/users/{quote(u["userName"])}/role">'
                  f'<select name="system_role" style="width:auto">{options}</select> <button>Gem</button></form>')
        delete = "" if u["_seeded"] else (f' <form class="inline" method="post" action="/ultra/admin/users/{quote(u["userName"])}/delete">'
                                         f'<button class="danger">Delete</button></form>')
        origin = "seed" if u["_seeded"] else "oprettet her"
        rows += (f"<tr><td>{e(u['userName'])}</td><td>{e(u['name']['given'])} {e(u['name']['family'])}</td>"
                 f"<td>{e(role_names.get(role_id, role_id))}</td><td class='muted'>{origin}</td><td>{change}{delete}</td></tr>")
    options = "".join(f'<option value="{e(r["role_id"])}">{e(r["name"])}</option>' for r in roles)
    body = f"""
<div class="panel"><h3>Create User</h3>
<form method="post" action="/ultra/admin/users">
<label>Username<br><input type="text" name="user_name" required pattern="[A-Za-z0-9_.\\-@ ]+"></label>
<label>First Name<br><input type="text" name="given" required></label>
<label>Last Name<br><input type="text" name="family" required></label>
<label>Primary System Role<br><select name="system_role">{options}</select></label>
<button class="primary">Submit</button></form></div>
<table><tr><th>Username</th><th>Navn</th><th>Primary System Role</th><th></th><th></th></tr>{rows}</table>"""
    return layout("Users", body, [ADMIN, ("Users", None), ("Users", None)])


def rest_integrations_page() -> str:
    rows = "".join(
        f"<tr><td><code>{e(i['application_id'])}</code></td><td>{e(i['user_name'])}</td>"
        f"<td>{'Yes' if i['end_user_access'] else 'No'}</td><td>{'Yes' if i['act_as_user'] else 'No'}</td>"
        f"<td>{'' if STORE.user('userName:' + i['user_name']) else '<span class=warn>brugeren findes ikke</span>'}</td>"
        f"<td><form class='inline' method='post' action='/ultra/admin/rest-integrations/{quote(i['application_id'])}/delete'>"
        f"<button class='danger'>Delete</button></form></td></tr>"
        for i in STORE.rest_integrations()) or "<tr><td colspan=6 class='muted'>Ingen integrationer.</td></tr>"
    body = f"""
<table><tr><th>Application ID</th><th>Learn User</th><th>End User Access</th><th>Authorized To Act As User</th><th></th><th></th></tr>{rows}</table>
<div class="panel"><h3>Create Integration</h3>
<form method="post" action="/ultra/admin/rest-integrations">
<label>Application ID<br><input type="text" name="application_id" required></label>
<label>Learn User<br><input type="text" name="user_name" required></label>
<label>End User Access<br><select name="end_user_access"><option>No</option><option>Yes</option></select></label>
<label>Authorized To Act As User<br><select name="act_as_user"><option>Service Default (No)</option><option>Yes</option><option>No</option></select></label>
<button class="primary">Submit</button></form></div>
<p class="muted">Mockens applikation i "Developer Portal" har id <code>{e(APPLICATION_ID)}</code>. Andre id'er kan tilføjes, men ingen nøgle passer til dem.</p>"""
    return layout("REST API Integrations", body, [ADMIN, ("Building Blocks", None), ("REST API Integrations", None)])


def lti_tools_page() -> str:
    rows = "".join(
        f"<tr><td><code>{e(t['client_id'])}</code></td><td>{e(t['status'])}</td><td><code>{e(t['deployment_id'])}</code></td>"
        f"<td><a class='btn' href='/ultra/admin/lti-tools/{quote(t['client_id'])}'>Edit</a> "
        f"<form class='inline' method='post' action='/ultra/admin/lti-tools/{quote(t['client_id'])}/delete'><button class='danger'>Delete</button></form></td></tr>"
        for t in STORE.lti_tools()) or "<tr><td colspan=4 class='muted'>Ingen værktøjer.</td></tr>"
    body = f"""
<table><tr><th>Client ID</th><th>Tool status</th><th>Deployment ID</th><th></th></tr>{rows}</table>
<div class="panel"><h3>Register LTI 1.3/Advantage Tool</h3>
<form method="post" action="/ultra/admin/lti-tools">
<label>Client ID<br><input type="text" name="client_id" required></label>
<button class="primary">Submit</button></form></div>"""
    return layout("LTI Tool Providers", body, [ADMIN, ("Integrations", None), ("LTI Tool Providers", None)])


def lti_tool_page(tool: dict, created: bool = False) -> str:
    def box(name: str, label: str) -> str:
        return f'<label><input type="checkbox" name="{name}" value="1"{" checked" if tool[name] else ""}> {e(label)}</label>'

    status = "".join(f'<option{" selected" if tool["status"] == s else ""}>{s}</option>' for s in ("Approved", "Excluded"))
    domain = urlparse(TOOL_URL).hostname if TOOL_URL else "(--tool-url er ikke sat)"
    banner = f'<p class="panel">Værktøjet er registreret. Deployment ID: <code>{e(tool["deployment_id"])}</code></p>' if created else ""
    body = f"""{banner}
<table>
<tr><th>Client ID</th><td><code>{e(tool['client_id'])}</code></td></tr>
<tr><th>Deployment ID</th><td><code>{e(tool['deployment_id'])}</code></td></tr>
<tr><th>Tool Provider Domain</th><td>{e(domain or '')}</td></tr>
<tr><th>Login Initiation URL</th><td><code>{e(tool_login_url() if TOOL_URL else '')}</code></td></tr>
<tr><th>Tool Redirect URL(s)</th><td><code>{e(tool_launch_url() + '*' if TOOL_URL else '')}</code></td></tr>
</table>
<form method="post" action="/ultra/admin/lti-tools/{quote(tool['client_id'])}">
<label>Tool status<br><select name="status">{status}</select></label>
<h3>Institution Policies</h3>
<p class="muted">User Fields to Send</p>
{box('send_role', 'Role in Course')}{box('send_name', 'Name')}{box('send_email', 'Email Address')}
{box('grades', 'Allow grade service access')}{box('memberships', 'Allow Membership Service access')}
<p class="muted">Grade- og membership-tjenesterne findes ikke i mocken; valget gemmes kun.</p>
<button class="primary">Submit</button></form>"""
    return layout("LTI Tool", body, [ADMIN, ("Integrations", None), ("LTI Tool Providers", "/ultra/admin/lti-tools"), (tool["client_id"], None)])


def admin_post(path: str, form: dict[str, list[str]]) -> str:
    """Gemmer en formular fra Administrator Panel og returnerer stien, der skal vises bagefter."""
    one = lambda key: (form.get(key) or [""])[0].strip()  # noqa: E731
    yes = lambda key: one(key) in ("1", "Yes")  # noqa: E731

    if path == "/ultra/admin/system-roles":
        role_id, name = one("role_id"), one("name")
        if role_id and name and role_id not in BUILTIN_SYSTEM_ROLES:
            STORE.write("INSERT OR IGNORE INTO system_roles VALUES (?, ?, '[]')", (role_id, name))
            return f"/ultra/admin/system-roles/{quote(role_id)}"
        return "/ultra/admin/system-roles"

    if m := re.fullmatch(r"/ultra/admin/system-roles/([^/]+)/delete", path):
        STORE.write("DELETE FROM system_roles WHERE role_id = ?", (unquote(m[1]),), reload=True)
        return "/ultra/admin/system-roles"

    if m := re.fullmatch(r"/ultra/admin/system-roles/([^/]+)", path):
        entitlements = sorted(x for x in form.get("entitlement", []) if x in PRIVILEGES)
        STORE.write("UPDATE system_roles SET entitlements = ? WHERE role_id = ?", (json.dumps(entitlements), unquote(m[1])))
        return "/ultra/admin/system-roles"

    if path == "/ultra/admin/users":
        user_name = one("user_name")
        if user_name and STORE.user(f"userName:{user_name}") is None and STORE.system_role(one("system_role")):
            STORE.write("INSERT INTO admin_users VALUES (?, ?, ?, '', ?)", (user_name, one("given"), one("family"), one("system_role")), reload=True)
        return "/ultra/admin/users"

    if m := re.fullmatch(r"/ultra/admin/users/([^/]+)/role", path):
        user = STORE.user(f"userName:{unquote(m[1])}")
        role = one("system_role")
        if user and STORE.system_role(role):
            if user["_seeded"]:
                STORE.write("INSERT OR REPLACE INTO seed_user_roles VALUES (?, ?)", (user["userName"], role), reload=True)
            else:
                STORE.write("UPDATE admin_users SET system_role = ? WHERE user_name = ?", (role, user["userName"]), reload=True)
        return "/ultra/admin/users"

    if m := re.fullmatch(r"/ultra/admin/users/([^/]+)/delete", path):
        STORE.write("DELETE FROM admin_users WHERE user_name = ?", (unquote(m[1]),), reload=True)
        return "/ultra/admin/users"

    if path == "/ultra/admin/rest-integrations":
        if one("application_id") and one("user_name"):
            STORE.write("INSERT OR REPLACE INTO rest_integrations VALUES (?, ?, ?, ?, ?)",
                        (one("application_id"), one("user_name"), int(yes("end_user_access")), int(yes("act_as_user")), iso(now())))
        return "/ultra/admin/rest-integrations"

    if m := re.fullmatch(r"/ultra/admin/rest-integrations/([^/]+)/delete", path):
        STORE.write("DELETE FROM rest_integrations WHERE application_id = ?", (unquote(m[1]),))
        return "/ultra/admin/rest-integrations"

    if path == "/ultra/admin/lti-tools":
        client_id = one("client_id")
        if not client_id:
            return "/ultra/admin/lti-tools"
        if STORE.lti_tool(client_id) is None:
            deployment = str(uuid.uuid4())
            STORE.write("INSERT INTO lti_tools VALUES (?, ?, 'Approved', 1, 1, 1, 0, 0, ?)", (client_id, deployment, iso(now())))
        return f"/ultra/admin/lti-tools/{quote(client_id)}?created=1"

    if m := re.fullmatch(r"/ultra/admin/lti-tools/([^/]+)/delete", path):
        STORE.write("DELETE FROM lti_tools WHERE client_id = ?", (unquote(m[1]),))
        return "/ultra/admin/lti-tools"

    if m := re.fullmatch(r"/ultra/admin/lti-tools/([^/]+)", path):
        status = one("status") if one("status") in ("Approved", "Excluded") else "Excluded"
        STORE.write("UPDATE lti_tools SET status = ?, send_role = ?, send_name = ?, send_email = ?, grades = ?, memberships = ? WHERE client_id = ?",
                    (status, int(yes("send_role")), int(yes("send_name")), int(yes("send_email")), int(yes("grades")), int(yes("memberships")), unquote(m[1])))
        return "/ultra/admin/lti-tools"

    raise ApiError(404, "Ukendt side")


def admin_get(path: str, query: dict[str, str]) -> str | None:
    if path in ("/ultra/admin", "/ultra/admin/"):
        return admin_home()
    if path == "/ultra/admin/system-roles":
        return system_roles_page()
    if m := re.fullmatch(r"/ultra/admin/system-roles/([^/]+)", path):
        role = STORE.system_role(unquote(m[1]))
        return privileges_page(role) if role and not role["builtin"] else None
    if path == "/ultra/admin/users":
        return users_page()
    if path == "/ultra/admin/rest-integrations":
        return rest_integrations_page()
    if path == "/ultra/admin/lti-tools":
        return lti_tools_page()
    if m := re.fullmatch(r"/ultra/admin/lti-tools/([^/]+)", path):
        tool = STORE.lti_tool(unquote(m[1]))
        return lti_tool_page(tool, created=query.get("created") == "1") if tool else None
    return None


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

    def text(self, status: int, text: str):
        self.send(status, text.encode(), "text/plain; charset=utf-8")

    def authorized_ui(self) -> bool:
        expected = "Basic " + base64.b64encode(f"admin:{UI_PASSWORD}".encode()).decode()
        if UI_PASSWORD and hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            return True
        self.send(401, b"Log ind som admin med BB_MOCK_UI_PASSWORD", "text/plain; charset=utf-8",
                  {"WWW-Authenticate": 'Basic realm="Blackboard-mock", charset="UTF-8"'})
        return False

    def same_origin(self) -> bool:
        """Formularer i Administrator Panel må kun sendes fra mockens egne sider."""
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        public = urlparse(PUBLIC_URL)
        given = urlparse(origin)
        return not origin or (given.scheme, given.netloc) == (public.scheme, public.netloc)

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
                return self.page(status, text) if status == 200 else self.text(status, text)

            if path in (f"/api/v1/management/applications/{APPLICATION_ID}/jwks.json", "/.well-known/jwks.json"):
                return self.json(200, {"keys": [JWK]})

            if path in ("/", "/launch") or path.startswith("/ultra/admin"):
                if not self.authorized_ui():
                    return
                STORE.reload()
                if path == "/":
                    return self.page(200, test_page())
                if path == "/launch":
                    status, text = start_launch(query)
                    return self.page(status, text) if status == 200 else self.text(status, text)
                if method == "POST":
                    if not self.same_origin():
                        return self.text(403, "Formularen kom ikke fra mocken")
                    target = admin_post(path, parse_qs(body.decode(), keep_blank_values=True))
                    return self.send(303, b"", "text/plain", {"Location": target})
                text = admin_get(path, query)
                return self.page(200, text) if text else self.text(404, "Ukendt side")

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
