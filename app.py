"""ScoreHub - live presentation scoring (Flask).
Durable by design: every vote is committed to the database before the voter
sees "recorded", duplicates are blocked by a DB unique constraint, and a CSV
snapshot of all votes is written to ./backups every few minutes.
"""
import csv, glob, io, os, re, threading, time, unicodedata
from collections import defaultdict, deque
from datetime import datetime
from functools import wraps

import qrcode, qrcode.image.svg
from PIL import Image, ImageOps
from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

BASE = os.path.dirname(os.path.abspath(__file__))
try:  # read settings from a .env file next to app.py (no shell quoting problems)
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, ".env"))
except ImportError:
    pass
os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
BACKUP_DIR = os.path.join(BASE, "backups")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
db_url = os.environ.get("DATABASE_URL", "").strip()
if db_url.lower().startswith("psql"):  # Neon's copy button can include "psql '...'"
    db_url = db_url[4:].strip()
db_url = db_url.strip("\"'").strip() or "sqlite:///" + os.path.join(BASE, "data", "scores.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024  # bulk photo upload
Image.MAX_IMAGE_PIXELS = 60_000_000
db = SQLAlchemy(app)

if db_url.startswith("sqlite"):
    @event.listens_for(Engine, "connect")
    def _sqlite_durability(conn, _):  # survive crashes/power loss
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA busy_timeout=5000")
        c.close()


class Staff(db.Model):
    # staff_id is the matching key (see canon_sid): unique, so one row per person.
    staff_id = db.Column(db.String(40), primary_key=True)
    display_id = db.Column(db.String(40), default="")  # as written in the staff file, e.g. 004512
    name = db.Column(db.String(120), default="")


class Presenter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    portfolio = db.Column(db.String(200), default="")
    staff_id = db.Column(db.String(40), default="")  # blocks self-rating
    status = db.Column(db.String(10), default="waiting")  # waiting|open|closed


class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.String(40), nullable=False)
    presenter_id = db.Column(db.Integer, nullable=False, index=True)
    score = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.String(500), default="")
    ip = db.Column(db.String(64), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True)  # set when the voter changes their score
    __table_args__ = (UniqueConstraint("staff_id", "presenter_id", name="one_vote_per_person"),)


class Photo(db.Model):
    """Presenter photo, stored in the DB so it survives restarts on free hosts."""
    presenter_id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.LargeBinary, nullable=False)
    version = db.Column(db.Integer, default=0)


class Setting(db.Model):
    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.String(200), default="")


def get_setting(key, default=""):
    s = db.session.get(Setting, key)
    return s.value if s else default


def set_setting(key, value):
    s = db.session.get(Setting, key)
    if s:
        s.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()


_FLOAT_ID = re.compile(r"^(\d+)\.0+$")


def clean_sid(raw):
    """Tidy a staff ID as typed or as stored in a CSV: full-width and Bengali digits become 0-9,
    spaces and invisible characters are removed, letters become upper case."""
    s = unicodedata.normalize("NFKC", str(raw or ""))
    s = "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() else ch for ch in s
                if not (ch.isspace() or unicodedata.category(ch) in ("Cf", "Cc")))
    s = s.upper()
    m = _FLOAT_ID.match(s)  # Excel sometimes writes 4512 as 4512.0
    return m.group(1) if m else s


def canon_sid(raw):
    """Matching key for a staff ID. Number-only IDs ignore leading zeros, so 004512, 4512 and
    4512.0 are the same person. This key is what makes "one vote per staff ID" hold."""
    s = clean_sid(raw)[:40]
    return (s.lstrip("0") or "0") if (s.isascii() and s.isdigit()) else s


def migrate_schema():
    """Upgrade databases created by older versions (keeps all existing data)."""
    cols = {c["name"] for c in inspect(db.engine).get_columns("presenter")}
    if "portfolio" not in cols:
        if "topic" in cols:  # older version used "topic"
            db.session.execute(text("ALTER TABLE presenter RENAME COLUMN topic TO portfolio"))
        else:
            db.session.execute(text("ALTER TABLE presenter ADD COLUMN portfolio VARCHAR(200) DEFAULT ''"))
        db.session.commit()
    if "display_id" not in {c["name"] for c in inspect(db.engine).get_columns("staff")}:
        db.session.execute(text("ALTER TABLE staff ADD COLUMN display_id VARCHAR(40) DEFAULT ''"))
        db.session.commit()
    if "updated_at" not in {c["name"] for c in inspect(db.engine).get_columns("vote")}:
        db.session.execute(text("ALTER TABLE vote ADD COLUMN updated_at TIMESTAMP"))
        db.session.commit()


def canonicalize_ids():
    """Bring data saved by older versions onto the matching key. If one person ended up with two
    IDs (e.g. 4512 and 004512) and voted twice for a presenter, their earliest vote is kept."""
    staff_ids = [r[0] for r in db.session.query(Staff.staff_id)]
    vote_ids = [r[0] for r in db.session.query(Vote.staff_id).distinct()]
    pres = Presenter.query.filter(Presenter.staff_id != "").all()
    blank_display = Staff.query.filter((Staff.display_id == "") | (Staff.display_id.is_(None))).count()
    if (all(i == canon_sid(i) for i in staff_ids + vote_ids)
            and all(p.staff_id == canon_sid(p.staff_id) for p in pres) and not blank_display):
        return
    write_backup()
    merged = removed = 0
    groups = defaultdict(list)
    for st in Staff.query.all():
        groups[canon_sid(st.staff_id)].append(st)
    for key, rows in groups.items():
        if len(rows) == 1 and rows[0].staff_id == key:
            rows[0].display_id = rows[0].display_id or clean_sid(rows[0].staff_id)
            continue
        disp = max((clean_sid(r.display_id or r.staff_id) for r in rows), key=len)
        name = next((r.name for r in rows if r.name), "")
        merged += len(rows) - 1
        for r in rows:
            db.session.delete(r)
        db.session.flush()
        db.session.add(Staff(staff_id=key, display_id=disp, name=name))
    keep, drop = set(), []
    for v in Vote.query.order_by(Vote.created_at, Vote.id):
        k = (canon_sid(v.staff_id), v.presenter_id)
        (drop.append(v) if k in keep else keep.add(k))
    for v in drop:
        db.session.delete(v)
    removed = len(drop)
    db.session.flush()
    for v in Vote.query.all():
        if v.staff_id != canon_sid(v.staff_id):
            v.staff_id = canon_sid(v.staff_id)
    for p in pres:
        p.staff_id = canon_sid(p.staff_id)
    db.session.commit()
    if merged or removed:
        print(f"Staff IDs: merged {merged} duplicate ID(s), removed {removed} duplicate vote(s). Backup saved first.")




# ---------- helpers ----------
def photo_versions():
    return dict(db.session.query(Photo.presenter_id, Photo.version).all())


def photo_url(pid, versions=None):
    v = (versions if versions is not None else photo_versions()).get(pid)
    return f"/photo/{pid}?v={v}" if v is not None else None


app.jinja_env.globals["photo_url"] = photo_url


def event_name():
    return get_setting("event_name") or os.environ.get("EVENT_NAME", "Presentation Showcase")


def base_url():
    """Public address that goes inside the QR codes."""
    return (get_setting("base_url") or os.environ.get("BASE_URL", "") or request.url_root).strip().rstrip("/")


@app.context_processor
def _inject():
    return {"event_name": event_name(), "is_admin": bool(session.get("admin"))}


@app.template_filter("initials")
def _initials(name):
    parts = [w for w in str(name).replace(".", " ").split() if w]
    return ("".join(w[0] for w in parts[:2]) or "?").upper()


def delete_presenter(pid):
    Vote.query.filter_by(presenter_id=pid).delete()
    Photo.query.filter_by(presenter_id=pid).delete()
    Presenter.query.filter_by(id=pid).delete()


def reset_presenter_numbers():
    """When no presenters are left, numbering restarts at 1 (PostgreSQL needs a nudge)."""
    if Presenter.query.count() == 0 and db.engine.dialect.name == "postgresql":
        db.session.execute(text("ALTER SEQUENCE IF EXISTS presenter_id_seq RESTART WITH 1"))


def reset_display():
    Setting.query.filter(Setting.key.in_(["shown_id", "final"])).delete(synchronize_session=False)


def duplicate_groups():
    groups = {}
    for p in Presenter.query.order_by(Presenter.id):
        groups.setdefault(p.name.strip().lower(), []).append(p)
    return [g for g in groups.values() if len(g) > 1]


def save_photo(pid, fs):
    """Square-crop, resize to 480px, store as JPEG. Returns True on success."""
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(fs.read()))).convert("RGB")
        img = ImageOps.fit(img, (480, 480), Image.LANCZOS, centering=(0.5, 0.35))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82, optimize=True)
    except Exception:
        return False
    row = db.session.get(Photo, pid)
    if row:
        row.data, row.version = buf.getvalue(), int(time.time())
    else:
        db.session.add(Photo(presenter_id=pid, data=buf.getvalue(), version=int(time.time())))
    db.session.commit()
    return True


def tally():
    """{presenter_id: (scores, total)} counted straight from the vote table, which holds exactly one
    row per staff ID per presenter. Counting on read means a changed score can never leave a
    stale total behind."""
    rows = (db.session.query(Vote.presenter_id, db.func.count(Vote.id), db.func.sum(Vote.score))
            .group_by(Vote.presenter_id).all())
    return {pid: (int(n), int(t or 0)) for pid, n, t in rows}


def leaderboard():
    t = tally()
    rows = [(p, *t[p.id]) for p in Presenter.query.all() if t.get(p.id, (0, 0))[0]]
    rows.sort(key=lambda r: (-(r[2] / r[1]), -r[1], r[0].id))
    vs = photo_versions()
    return [{"rank": i + 1, "id": p.id, "name": p.name, "portfolio": p.portfolio,
             "avg": round(tot / n, 2), "votes": n, "photo": photo_url(p.id, vs)}
            for i, (p, n, tot) in enumerate(rows)]


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            if request.path.startswith(("/admin/api", "/admin/presenter", "/admin/mode",
                                        "/admin/reveal", "/admin/next")):
                return jsonify(ok=False, error="Sign in again"), 401
            return redirect(url_for("admin_login"))
        return f(*a, **kw)
    return wrapper


def votes_csv():
    rows = (db.session.query(Vote, Staff, Presenter)
            .outerjoin(Staff, Staff.staff_id == Vote.staff_id)
            .join(Presenter, Presenter.id == Vote.presenter_id)
            .order_by(Vote.created_at).all())
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["time_utc", "changed_utc", "staff_id", "staff_name", "presenter_id", "presenter", "score", "comment"])
    for v, s, p in rows:
        w.writerow([v.created_at.isoformat(timespec="seconds"),
                    v.updated_at.isoformat(timespec="seconds") if v.updated_at else "",
                    (s.display_id or s.staff_id) if s else v.staff_id, s.name if s else "",
                    p.id, p.name, v.score, v.comment])
    return out.getvalue()


def write_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"votes_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(votes_csv())
    for old in sorted(glob.glob(os.path.join(BACKUP_DIR, "votes_*.csv")))[:-60]:
        os.remove(old)


def _backup_loop():
    while True:
        time.sleep(300)
        try:
            with app.app_context():
                write_backup()
        except Exception as e:  # never crash the app because of a backup
            print("backup failed:", e)


with app.app_context():
    db.create_all()  # creates any missing tables (e.g. photo)
    migrate_schema()
    canonicalize_ids()

if os.environ.get("DISABLE_BACKUP") != "1":
    threading.Thread(target=_backup_loop, daemon=True).start()


# ---------- audience ----------
@app.get("/")
def home():
    return render_template("home.html", presenters=Presenter.query.order_by(Presenter.id).all())


@app.get("/rate/<int:pid>")
def rate(pid):
    p = db.session.get(Presenter, pid) or abort(404)
    ids = [r[0] for r in db.session.query(Staff.staff_id)]
    numeric = bool(ids) and all(i.isdigit() for i in ids)
    return render_template("rate.html", p=p, photo=photo_url(pid), numeric_ids=numeric)


@app.get("/photo/<int:pid>")
def photo(pid):
    row = db.session.get(Photo, pid) or abort(404)
    return Response(row.data, mimetype="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/status/<int:pid>")
def api_status(pid):
    p = db.session.get(Presenter, pid) or abort(404)
    return jsonify(status=p.status)


# Unknown staff IDs per network address: stops someone guessing IDs, while a whole venue on one
# Wi-Fi address can still make plenty of typos.
FAIL_LIMIT, FAIL_WINDOW = 200, 600
_fails, _fails_lock = defaultdict(deque), threading.Lock()


def client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()[:64]


def too_many_fails():
    now, q = time.time(), _fails[client_ip()]
    with _fails_lock:
        while q and now - q[0] > FAIL_WINDOW:
            q.popleft()
        return len(q) >= FAIL_LIMIT


def note_fail():
    with _fails_lock:
        _fails[client_ip()].append(time.time())


SLOW_DOWN = "Too many unknown staff IDs from this network. Wait a few minutes, then try again."
NOT_FOUND = "Staff ID not found. Check it and try again."


@app.get("/api/whoami/<int:pid>")
def api_whoami(pid):
    """Name check while typing, so a voter notices a typo before scoring as someone else."""
    p = db.session.get(Presenter, pid) or abort(404)
    sid = canon_sid(request.args.get("id"))
    if not sid:
        return jsonify(ok=False)
    if too_many_fails():
        return jsonify(ok=False, error=SLOW_DOWN), 429
    st = db.session.get(Staff, sid)
    if not st:
        note_fail()
        return jsonify(ok=False, error=NOT_FOUND)
    voted = db.session.query(Vote.id).filter_by(staff_id=sid, presenter_id=pid).first() is not None
    return jsonify(ok=True, name=st.name or "", voted=voted,
                   own=bool(p.staff_id) and canon_sid(p.staff_id) == sid)


def record_vote(sid, pid, score, comment, ip):
    """Save a score. The vote table allows one row per staff ID per presenter, so a second
    submission while voting is open replaces the first instead of adding a vote.
    Returns True when an earlier score was changed."""
    values = {"score": score, "ip": ip, "updated_at": datetime.utcnow()}
    if comment:
        values["comment"] = comment
    upd = db.update(Vote).where(Vote.staff_id == sid, Vote.presenter_id == pid).values(**values)
    if db.session.execute(upd).rowcount:
        db.session.commit()
        return True
    try:
        db.session.add(Vote(staff_id=sid, presenter_id=pid, score=score, comment=comment, ip=ip))
        db.session.commit()
        return False
    except IntegrityError:  # the same person submitted twice at the same instant
        db.session.rollback()
        db.session.execute(upd)
        db.session.commit()
        return True


@app.post("/api/vote")
def api_vote():
    d = request.get_json(silent=True) or {}
    sid = canon_sid(d.get("staff_id"))
    try:
        pid, score = int(d.get("presenter_id")), int(d.get("score"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Pick a score from 1 to 10."), 400
    if not 1 <= score <= 10:
        return jsonify(ok=False, error="Pick a score from 1 to 10."), 400
    p = db.session.get(Presenter, pid)
    if not p:
        return jsonify(ok=False, error="Presenter not found."), 404
    if p.status != "open":
        return jsonify(ok=False, error="Voting is not open for this presenter."), 403
    if too_many_fails():
        return jsonify(ok=False, error=SLOW_DOWN), 429
    if not sid or not db.session.get(Staff, sid):
        note_fail()
        return jsonify(ok=False, error=NOT_FOUND), 403
    if p.staff_id and canon_sid(p.staff_id) == sid:
        return jsonify(ok=False, error="You can't score your own presentation."), 403
    changed = record_vote(sid, pid, score, str(d.get("comment") or "").strip()[:500], client_ip())
    return jsonify(ok=True, changed=changed)


@app.get("/qr/<int:pid>.svg")
def qr_svg(pid):
    db.session.get(Presenter, pid) or abort(404)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=0)
    qr.add_data(f"{base_url()}/rate/{pid}")
    buf = io.BytesIO()
    qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buf)
    return Response(buf.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.get("/display")
def display():
    return render_template("display.html")


@app.get("/api/display")
def api_display():
    mode, final = get_setting("mode", "live"), get_setting("final", "0") == "1"
    total = Staff.query.count()
    op = Presenter.query.filter_by(status="open").first()
    out = {"mode": mode, "final": final, "open": None, "shown": None, "board": [],
           "event": event_name(), "count": Presenter.query.count(),
           "done": Presenter.query.filter_by(status="closed").count()}
    if op:
        link = f"{base_url()}/rate/{op.id}"
        out["open"] = {"id": op.id, "name": op.name, "portfolio": op.portfolio,
                       "votes": tally().get(op.id, (0, 0))[0],
                       "total": total, "photo": photo_url(op.id), "qr": f"/qr/{op.id}.svg",
                       "link": link.split("://", 1)[-1]}
    if mode == "live" or final:
        board = leaderboard()
        sid = get_setting("shown_id")
        if sid and not final:
            hit = next((r for r in board if r["id"] == int(sid)), None)
            out["shown"] = hit
        out["ranked"] = len(board)
        out["board"] = board if final else board[:10]
    return jsonify(out)


# ---------- admin ----------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    err = ""
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin"))
        err = "Wrong password."
    return render_template("login.html", err=err)


@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.get("/admin")
@admin_required
def admin():
    return render_template("admin.html")


@app.get("/admin/api/state")
@admin_required
def admin_state():
    total = Staff.query.count()
    shown = get_setting("shown_id")
    vs = photo_versions()
    base = base_url()
    nxt = Presenter.query.filter_by(status="waiting").order_by(Presenter.id).first()
    t = tally()
    return jsonify(
        event=event_name(), base=base, next_id=nxt.id if nxt else None,
        base_ok=not any(h in base for h in ("localhost", "127.0.0.1", "0.0.0.0")),
        mode=get_setting("mode", "live"), final=get_setting("final", "0") == "1", total=total,
        presenters=[{"id": p.id, "name": p.name, "portfolio": p.portfolio, "status": p.status,
                     "votes": t.get(p.id, (0, 0))[0],
                     "avg": round(t[p.id][1] / t[p.id][0], 2) if p.id in t else 0,
                     "shown": str(p.id) == shown, "photo": photo_url(p.id, vs)}
                    for p in Presenter.query.order_by(Presenter.id)])


@app.post("/admin/presenter/<int:pid>/<action>")
@admin_required
def admin_action(pid, action):
    p = db.session.get(Presenter, pid) or abort(404)
    if action == "open":
        Presenter.query.filter_by(status="open").update({"status": "closed"})
        p.status = "open"
    elif action in ("close", "close_show"):
        p.status = "closed"
        if action == "close_show":
            set_setting("shown_id", str(pid))
    elif action == "show":
        set_setting("shown_id", str(pid))
    else:
        abort(400)
    db.session.commit()
    return jsonify(ok=True)


@app.post("/admin/next")
@admin_required
def admin_next():
    nxt = Presenter.query.filter_by(status="waiting").order_by(Presenter.id).first()
    if not nxt:
        return jsonify(ok=False, error="No presenters are waiting."), 404
    Presenter.query.filter_by(status="open").update({"status": "closed"})
    nxt.status = "open"
    db.session.commit()
    return jsonify(ok=True, id=nxt.id, name=nxt.name)


@app.post("/admin/mode/<mode>")
@admin_required
def admin_mode(mode):
    if mode not in ("live", "final"):
        abort(400)
    set_setting("mode", mode)
    return jsonify(ok=True)


@app.post("/admin/reveal/<flag>")
@admin_required
def admin_reveal(flag):
    set_setting("final", "1" if flag == "on" else "0")
    return jsonify(ok=True)


def _read_csv(field):
    f = request.files.get(field)
    if not f or not f.filename:
        return []
    raw = f.stream.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:  # older Excel "CSV" files
        text = raw.decode("cp1252", errors="replace")
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        row = {}
        for k, v in r.items():
            if isinstance(v, list):  # extra cells beyond the header
                continue
            key = re.sub(r"[^a-z0-9]+", "_", (k or "").strip().lower()).strip("_")
            row[STAFF_ID_HEADERS.get(key, key)] = (v or "").strip()
        out.append(row)
    return out


STAFF_ID_HEADERS = {k: "staff_id" for k in (
    "staffid", "staff_no", "staff_number", "employee_id", "employeeid", "employee_no",
    "emp_id", "empid", "emp_no", "id")}


def check_staff_rows(rows):
    """Validate a staff file before anything is saved. Returns (records, problems)."""
    seen, problems = defaultdict(list), []
    for line, r in enumerate(rows, start=2):  # line 1 is the header
        if not any(r.values()):
            continue
        key = canon_sid(r.get("staff_id"))
        if not key:
            problems.append(f"Row {line} has no staff ID.")
            continue
        seen[key].append((line, clean_sid(r.get("staff_id")), r.get("name", "")))
    for key, hits in seen.items():
        if len(hits) > 1:
            lines = [str(h[0]) for h in hits]
            written = sorted({h[1] for h in hits})
            how = f" (written as {' and '.join(written)})" if len(written) > 1 else ""
            problems.append(f"Staff ID {hits[0][1]} is on rows {', '.join(lines[:-1])} and {lines[-1]}{how}.")
    records = [(key, hits[0][1], hits[0][2]) for key, hits in seen.items()]
    return records, problems


@app.route("/admin/import", methods=["GET", "POST"])
@admin_required
def admin_import():
    msg, bad, problems = "", False, []
    if request.method == "POST":
        staff_rows, pres_rows = _read_csv("staff"), _read_csv("presenters")
        records, problems = check_staff_rows(staff_rows)
        if staff_rows and not records and not problems:
            problems = ["The staff file has no rows with a staff ID. Check that it has a staff_id column."]
        replace_staff = request.form.get("staff_mode") == "replace" and records
        replace_pres = request.form.get("pres_mode") == "replace" and pres_rows
        if problems:
            msg, bad = "Nothing was imported. Fix these rows in the staff file, then import it again.", True
        elif replace_pres and Vote.query.count():
            msg, bad = ("Presenters were not replaced because scores already exist. "
                        "Open Data and reset scores first, or choose Add."), True
        else:
            write_backup() if (replace_staff or replace_pres) else None
            if replace_staff:
                Staff.query.delete()
            if replace_pres:
                Vote.query.delete(); Photo.query.delete(); Presenter.query.delete()
                reset_display(); reset_presenter_numbers()
            existing = {r[0] for r in db.session.query(Staff.staff_id)}
            n_new = n_upd = n_pres = n_skip = 0
            for key, shown, name in records:
                st = db.session.get(Staff, key) if key in existing else None
                if st:  # same person already loaded: update, keep the zero-padded form of the ID
                    n_upd += 1
                    st.display_id = max([st.display_id or "", shown], key=len)
                    st.name = name or st.name
                else:
                    n_new += 1
                    db.session.add(Staff(staff_id=key, display_id=shown, name=name))
            have = {p.name.strip().lower() for p in Presenter.query}
            for r in pres_rows:
                nm = r.get("name", "")
                if not nm:
                    continue
                if nm.strip().lower() in have:
                    n_skip += 1
                    continue
                have.add(nm.strip().lower())
                db.session.add(Presenter(name=nm, portfolio=r.get("portfolio") or r.get("topic", ""),
                                         staff_id=canon_sid(r.get("staff_id"))))
                n_pres += 1
            db.session.commit()
            parts = []
            if records:
                parts.append(f"{n_new} new staff ID{'' if n_new == 1 else 's'}"
                             + (f" and {n_upd} updated" if n_upd else ""))
            if pres_rows:
                parts.append(f"{n_pres} presenter{'' if n_pres == 1 else 's'}")
            msg = ("Imported " + " and ".join(parts) + "." if parts else "Choose a file to import.") + (
                f" Skipped {n_skip} presenters that were already in the list." if n_skip else "")
    return render_template("import.html", msg=msg, bad=bad, problems=problems[:30],
                           more=max(0, len(problems) - 30), staff=Staff.query.count(),
                           presenters=Presenter.query.count())


@app.route("/admin/photos", methods=["GET", "POST"])
@admin_required
def admin_photos():
    msg, skipped = "", []
    if request.method == "POST":
        files = [f for f in request.files.getlist("files") if f and f.filename]
        pid = request.form.get("pid", type=int)
        ok = 0
        if pid and files:  # single upload for one presenter
            ok = int(bool(db.session.get(Presenter, pid)) and save_photo(pid, files[0]))
            if not ok:
                skipped.append(files[0].filename)
        else:  # bulk: file name = presenter number, staff ID, or presenter name
            ps = Presenter.query.all()
            by_id = {str(p.id): p.id for p in ps}
            by_staff = {canon_sid(p.staff_id): p.id for p in ps if p.staff_id}
            by_name = {p.name.strip().lower(): p.id for p in ps}
            for f in files:
                stem = os.path.splitext(os.path.basename(f.filename))[0].strip()
                target = by_id.get(stem) or by_staff.get(canon_sid(stem)) or by_name.get(stem.lower())
                if target and save_photo(target, f):
                    ok += 1
                else:
                    skipped.append(f.filename)
        msg = f"Saved {ok} photo(s)." + (f" Not matched or unreadable: {', '.join(skipped)}" if skipped else "")
    vs = photo_versions()
    return render_template("photos.html", msg=msg, bad=bool(skipped),
                           items=[(p, photo_url(p.id, vs)) for p in Presenter.query.order_by(Presenter.id)])


def render_data(msg="", bad=False):
    q = request.args.get("q", "").strip()[:60]
    sq = (Staff.query.filter(Staff.staff_id.like(f"%{canon_sid(q)}%") | Staff.display_id.like(f"%{clean_sid(q)}%")
                             | Staff.name.ilike(f"%{q}%")) if q else Staff.query)
    dups = duplicate_groups()
    return render_template(
        "data.html", msg=msg, bad=bad, q=q, base=base_url(),
        counts={"staff": Staff.query.count(), "presenters": Presenter.query.count(),
                "votes": Vote.query.count(), "photos": Photo.query.count()},
        presenters=Presenter.query.order_by(Presenter.id).all(), votes_by={k: v[0] for k, v in tally().items()},
        staff=sq.order_by(Staff.staff_id).limit(40).all(), staff_matches=sq.count(),
        dup_count=sum(len(g) - 1 for g in dups))


@app.get("/admin/data")
@admin_required
def admin_data():
    return render_data()


@app.post("/admin/data/settings")
@admin_required
def admin_data_settings():
    set_setting("event_name", request.form.get("event_name", "").strip()[:120])
    addr = request.form.get("base_url", "").strip().rstrip("/")
    if addr and not addr.lower().startswith(("http://", "https://")):
        addr = "https://" + addr
    set_setting("base_url", addr[:190])
    return render_data("Saved. QR codes now use " + base_url() + ".")


@app.post("/admin/clear/<what>")
@admin_required
def admin_clear(what):
    """Delete data (typed confirmation required). A CSV backup of votes is written first."""
    if what not in ("votes", "presenters", "staff", "all", "duplicates"):
        abort(400)
    if request.form.get("confirm", "").strip() != "DELETE":
        return render_data("Nothing deleted. Type DELETE (capital letters) to confirm.", True)
    write_backup()
    if what == "duplicates":
        removed = 0
        for g in duplicate_groups():
            t = tally()
            keep = max(g, key=lambda p: (t.get(p.id, (0, 0))[0], -p.id))  # keep the one with votes, else lowest number
            for p in g:
                if p is not keep:
                    delete_presenter(p.id); removed += 1
        msg = f"Removed {removed} duplicate presenter(s)."
    else:
        reset_display()
        if what == "votes":
            Vote.query.delete()
            Presenter.query.update({"status": "waiting"})
            msg = "All votes cleared and results reset. Presenters, photos and staff IDs are kept."
        if what in ("presenters", "all"):
            Vote.query.delete(); Photo.query.delete(); Presenter.query.delete()
            msg = "All presenters, their photos and all votes cleared. Numbering restarts at 1."
        if what in ("staff", "all"):
            Staff.query.delete()
            msg = "All staff IDs cleared." if what == "staff" else "Everything cleared."
    db.session.commit()
    reset_presenter_numbers(); db.session.commit()
    return render_data(msg)


@app.post("/admin/delete/presenter/<int:pid>")
@admin_required
def admin_delete_presenter(pid):
    p = db.session.get(Presenter, pid) or abort(404)
    name = p.name
    write_backup()
    delete_presenter(pid)
    db.session.commit()
    reset_presenter_numbers(); db.session.commit()
    return render_data(f"Deleted presenter {name} with their photo and votes.")


@app.post("/admin/delete/staff/<sid>")
@admin_required
def admin_delete_staff(sid):
    st = db.session.get(Staff, canon_sid(sid))
    shown = (st.display_id or st.staff_id) if st else clean_sid(sid)
    Staff.query.filter_by(staff_id=canon_sid(sid)).delete()
    db.session.commit()
    return render_data(f"Deleted staff ID {shown}. Their earlier scores are kept.")


@app.get("/admin/export.csv")
@admin_required
def admin_export():
    return Response(votes_csv(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=votes.csv"})


@app.get("/admin/results.csv")
@admin_required
def admin_results():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["rank", "presenter", "portfolio", "average", "votes"])
    for r in leaderboard():
        w.writerow([r["rank"], r["name"], r["portfolio"], r["avg"], r["votes"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=results.csv"})


@app.get("/admin/qr")
@admin_required
def admin_qr():
    return render_template("qr.html")


# ---------- CLI ----------
@app.cli.command("seed-demo")
def seed_demo():
    """Create 300 sample staff and 40 sample presenters for testing."""
    for i in range(1, 301):
        db.session.merge(Staff(staff_id=canon_sid(f"S{i:04d}"), display_id=f"S{i:04d}", name=f"Staff {i}"))
    if not Presenter.query.count():
        for i in range(1, 41):
            db.session.add(Presenter(name=f"Presenter {i}", portfolio=f"Portfolio {i}"))
    db.session.commit()
    print("Seeded demo data.")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
