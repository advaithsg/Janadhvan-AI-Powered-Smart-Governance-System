import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from emergency_chain import dispatch_emergency_chain, get_dispatches_for_complaint, get_recent_dispatches
from i18n import SUPPORTED_LANGS, translate
from image_ai import compute_phash, find_similar_image_in_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "janadhvani-dev-secret-change-in-production")
app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "static", "uploads")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

EMERGENCY_KEYWORDS = [
    "fire", "accident", "gas leak", "electric shock", "severe flooding",
    "explosion", "collapse", "electrocution",
]
HIGH_KEYWORDS = ["flooding", "injury", "blocked road", "sewage overflow", "power outage"]

CATEGORY_KEYWORDS = {
    "Road Damage": ["pothole", "road", "crack", "asphalt", "pavement", "speed breaker"],
    "Garbage": ["garbage", "waste", "trash", "dump", "litter", "sanitation"],
    "Water Leakage": ["water leak", "pipe burst", "tap", "water supply", "leakage"],
    "Drainage": ["drain", "sewer", "blockage", "overflow", "manhole"],
    "Streetlight Issues": ["streetlight", "street light", "lamp", "dark street", "lighting"],
}

DEFAULT_ADMIN = {"username": "admin", "password": "admin123", "full_name": "System Admin", "email": "admin@janadhvani.gov.in"}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect("janadhvani.db")
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'citizen'
        );
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            area TEXT NOT NULL,
            category TEXT NOT NULL,
            priority TEXT DEFAULT 'Medium',
            smart_label TEXT DEFAULT 'Normal',
            image TEXT,
            status TEXT DEFAULT 'Pending',
            validation_count INTEGER DEFAULT 0,
            is_emergency INTEGER DEFAULT 0,
            latitude REAL,
            longitude REAL,
            gps_address TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS validations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            complaint_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            UNIQUE(complaint_id, user_id),
            FOREIGN KEY (complaint_id) REFERENCES complaints(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            complaint_id INTEGER NOT NULL,
            user_id INTEGER,
            rating INTEGER NOT NULL,
            feedback TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (complaint_id) REFERENCES complaints(id)
        );
        CREATE TABLE IF NOT EXISTS follows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            complaint_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, complaint_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (complaint_id) REFERENCES complaints(id)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            complaint_id INTEGER,
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS emergency_dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            complaint_id INTEGER NOT NULL,
            agency_type TEXT NOT NULL,
            agency_name TEXT NOT NULL,
            contact_phone TEXT,
            unit TEXT,
            message TEXT,
            status TEXT DEFAULT 'Dispatched',
            dispatched_at TEXT NOT NULL,
            FOREIGN KEY (complaint_id) REFERENCES complaints(id)
        );
        """
    )
    _migrate_columns(db)
    admin = db.execute("SELECT id FROM users WHERE username = ?", (DEFAULT_ADMIN["username"],)).fetchone()
    if not admin:
        db.execute(
            "INSERT INTO users (full_name, email, phone, username, password, role) VALUES (?, ?, ?, ?, ?, ?)",
            (
                DEFAULT_ADMIN["full_name"],
                DEFAULT_ADMIN["email"],
                "",
                DEFAULT_ADMIN["username"],
                generate_password_hash(DEFAULT_ADMIN["password"]),
                "admin",
            ),
        )
    db.commit()


def _migrate_columns(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(complaints)").fetchall()}
    if "image_hash" not in cols:
        db.execute("ALTER TABLE complaints ADD COLUMN image_hash TEXT")
        db.commit()


_db_ready = False


@app.before_request
def before_request():
    global _db_ready
    get_db()
    if not _db_ready:
        init_db()
        _db_ready = True
    if request.endpoint and request.endpoint != "static":
        apply_escalation_rules()
        if session.get("user_id"):
            push_status_notifications()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def login_required(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


def admin_required(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Admin access required.", "danger")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


def auto_categorize(text):
    text_lower = text.lower()
    scores = {cat: 0 for cat in CATEGORY_KEYWORDS}
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                scores[category] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def calculate_smart_priority(description, is_emergency=False):
    desc = description.lower()
    if is_emergency:
        return "Critical", "Emergency"
    for kw in EMERGENCY_KEYWORDS:
        if kw in desc:
            return "Critical", "Emergency"
    for kw in HIGH_KEYWORDS:
        if kw in desc:
            return "High", "High Risk"
    return "Medium", "Normal"


def similar_title(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.75


def find_duplicate(title, category, area):
    db = get_db()
    rows = db.execute(
        "SELECT id, title FROM complaints WHERE category = ? AND area = ? AND status != 'Resolved'",
        (category, area),
    ).fetchall()
    for row in rows:
        if similar_title(row["title"], title):
            return row["id"]
    return None


def apply_escalation_rules():
    db = get_db()
    now = datetime.utcnow()
    pending = db.execute(
        "SELECT id, priority, smart_label, created_at FROM complaints WHERE status = 'Pending'"
    ).fetchall()
    for c in pending:
        try:
            created = datetime.fromisoformat(c["created_at"])
        except ValueError:
            continue
        days = (now - created).days
        new_priority, new_label = c["priority"], c["smart_label"]
        if days >= 7:
            new_priority, new_label = "Critical", "Escalated"
        elif days >= 5:
            new_priority, new_label = "High", "Escalated"
        elif days >= 3:
            new_label = "Warning"
        if new_priority != c["priority"] or new_label != c["smart_label"]:
            db.execute(
                "UPDATE complaints SET priority = ?, smart_label = ? WHERE id = ?",
                (new_priority, new_label, c["id"]),
            )
    db.commit()


def push_status_notifications():
    if "user_id" not in session:
        return
    db = get_db()
    uid = session["user_id"]
    followed = db.execute(
        """
        SELECT c.id, c.title, c.status FROM follows f
        JOIN complaints c ON c.id = f.complaint_id
        WHERE f.user_id = ?
        """,
        (uid,),
    ).fetchall()
    for row in followed:
        existing = db.execute(
            "SELECT id FROM notifications WHERE user_id = ? AND complaint_id = ? AND message LIKE ?",
            (uid, row["id"], f"%{row['status']}%"),
        ).fetchone()
        if not existing and row["status"] in ("In Progress", "Resolved"):
            add_notification(
                uid,
                f"Complaint #{row['id']} ({row['title']}) is now: {row['status']}",
                row["id"],
            )


def add_notification(user_id, message, complaint_id=None):
    db = get_db()
    db.execute(
        "INSERT INTO notifications (user_id, message, complaint_id, created_at) VALUES (?, ?, ?, ?)",
        (user_id, message, complaint_id, datetime.utcnow().isoformat()),
    )
    db.commit()


def save_upload(file):
    if not file or file.filename == "":
        return None, None
    if not allowed_file(file.filename):
        return None, None
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    name = secure_filename(file.filename)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    filename = f"{stamp}_{name}"
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(path)
    phash = compute_phash(path)
    return filename, phash


def check_image_duplicate(file, force_new=False):
    if not file or file.filename == "" or force_new:
        return None
    if not allowed_file(file.filename):
        return None
    with tempfile.NamedTemporaryFile(delete=False, suffix=".img") as tmp:
        file.save(tmp.name)
        phash = compute_phash(tmp.name)
        os.unlink(tmp.name)
    if not phash:
        return None
    return find_similar_image_in_db(get_db(), phash)


def get_lang():
    lang = session.get("lang", "en")
    return lang if lang in SUPPORTED_LANGS else "en"


def t(key):
    return translate(key, get_lang())


def get_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) AS c FROM complaints").fetchone()["c"]
    resolved = db.execute("SELECT COUNT(*) AS c FROM complaints WHERE status = 'Resolved'").fetchone()["c"]
    active = db.execute(
        "SELECT COUNT(*) AS c FROM complaints WHERE status IN ('Pending', 'In Progress')"
    ).fetchone()["c"]
    emergency = db.execute("SELECT COUNT(*) AS c FROM complaints WHERE is_emergency = 1").fetchone()["c"]
    return {"total": total, "resolved": resolved, "active": active, "emergency": emergency}


def get_hotspots(limit=5):
    db = get_db()
    return db.execute(
        """
        SELECT area, COUNT(*) AS count FROM complaints
        GROUP BY area ORDER BY count DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


def get_unread_notifications(user_id):
    db = get_db()
    return db.execute(
        "SELECT * FROM notifications WHERE user_id = ? AND is_read = 0 ORDER BY created_at DESC LIMIT 10",
        (user_id,),
    ).fetchall()


@app.route("/")
def index():
    return render_template("index.html", stats=get_stats())


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not all([full_name, email, username, password]):
            flash("All required fields must be filled.", "danger")
            return render_template("signup.html")
        if not re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email):
            flash("Enter a valid email address.", "danger")
            return render_template("signup.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return render_template("signup.html")
        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("signup.html")

        db = get_db()
        if db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
            flash("Username already taken.", "danger")
            return render_template("signup.html")

        db.execute(
            "INSERT INTO users (full_name, email, phone, username, password, role) VALUES (?, ?, ?, ?, ?, ?)",
            (full_name, email, phone, username, generate_password_hash(password), "citizen"),
        )
        db.commit()
        flash("Account created successfully. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ? OR email = ?",
            (identifier, identifier),
        ).fetchone()
        if user and check_password_hash(user["password"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            flash(f"Welcome back, {user['full_name']}!", "success")
            if user["role"] == "admin":
                return redirect(url_for("dashboard"))
            return redirect(url_for("index"))
        flash("Invalid username/email or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/set-language/<lang>")
def set_language(lang):
    if lang in SUPPORTED_LANGS:
        session["lang"] = lang
        flash(f"Language set to {SUPPORTED_LANGS[lang]}.", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/api/check-image-duplicate", methods=["POST"])
@login_required
def api_check_image_duplicate():
    file = request.files.get("image")
    force = request.form.get("force_new") == "1"
    dup_id = check_image_duplicate(file, force_new=force)
    if dup_id:
        return jsonify(
            {
                "duplicate": True,
                "complaint_id": dup_id,
                "message": t("report.image_dup"),
            }
        )
    return jsonify({"duplicate": False})


@app.route("/notifications/read/<int:nid>")
@login_required
def mark_notification_read(nid):
    db = get_db()
    db.execute(
        "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
        (nid, session["user_id"]),
    )
    db.commit()
    return redirect(request.referrer or url_for("history"))


@app.route("/report", methods=["GET", "POST"])
@login_required
def report():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        area = request.form.get("area", "").strip()
        priority = request.form.get("priority", "Medium")
        latitude = request.form.get("latitude")
        longitude = request.form.get("longitude")
        gps_address = request.form.get("gps_address", "").strip()
        force_new = request.form.get("force_new") == "1"

        if not all([title, description, category, area]):
            flash("Please complete all required fields.", "danger")
            return render_template("report.html")

        if not latitude or not longitude:
            flash("GPS location is required. Use 'Detect My Location' button.", "danger")
            return render_template("report.html")

        dup_id = None if force_new else find_duplicate(title, category, area)
        if dup_id:
            flash(
                f"Similar issue already reported (ID #{dup_id}). Support existing complaint?",
                "warning",
            )
            return render_template("report.html", duplicate_id=dup_id)

        auto_cat = auto_categorize(f"{title} {description}")
        if auto_cat and category != auto_cat:
            category = auto_cat

        smart_priority, smart_label = calculate_smart_priority(description)
        if priority == "Low" and smart_priority in ("High", "Critical"):
            priority = smart_priority
        elif smart_priority == "Critical":
            priority = "Critical"

        upload_file = request.files.get("image")
        img_dup = check_image_duplicate(upload_file, force_new=force_new)
        if img_dup:
            flash(f"{t('report.image_dup')} (Complaint #{img_dup}).", "warning")
            return render_template("report.html", duplicate_id=img_dup, image_duplicate_id=img_dup)
        if upload_file:
            upload_file.seek(0)

        image_name, image_hash = save_upload(upload_file)
        db = get_db()
        db.execute(
            """
            INSERT INTO complaints (
                user_id, title, description, area, category, priority, smart_label,
                image, image_hash, status, validation_count, is_emergency, latitude, longitude,
                gps_address, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', 0, 0, ?, ?, ?, ?)
            """,
            (
                session["user_id"],
                title,
                description,
                area,
                category,
                priority,
                smart_label,
                image_name,
                image_hash,
                float(latitude),
                float(longitude),
                gps_address,
                datetime.utcnow().isoformat(),
            ),
        )
        db.commit()
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        add_notification(
            session["user_id"],
            f"Your complaint #{cid} was submitted successfully. Status: Pending",
            cid,
        )
        flash(f"Complaint submitted successfully. Your ID is #{cid}.", "success")
        return redirect(url_for("track"))

    return render_template("report.html")


@app.route("/emergency", methods=["GET", "POST"])
@login_required
def emergency():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "Emergency").strip()
        area = request.form.get("area", "").strip()
        latitude = request.form.get("latitude")
        longitude = request.form.get("longitude")
        gps_address = request.form.get("gps_address", "").strip()

        if not all([title, description, area]):
            flash("Please complete all required fields.", "danger")
            return render_template("emergency.html")

        if not latitude or not longitude:
            flash("GPS location is required for emergency reports.", "danger")
            return render_template("emergency.html")

        upload_file = request.files.get("image")
        img_dup = check_image_duplicate(upload_file)
        if img_dup:
            flash(f"{t('report.image_dup')} (#{img_dup}).", "warning")
        if upload_file:
            upload_file.seek(0)

        image_name, image_hash = save_upload(upload_file)
        db = get_db()
        db.execute(
            """
            INSERT INTO complaints (
                user_id, title, description, area, category, priority, smart_label,
                image, image_hash, status, validation_count, is_emergency, latitude, longitude,
                gps_address, created_at
            ) VALUES (?, ?, ?, ?, ?, 'Critical', 'Emergency', ?, ?, 'Pending', 0, 1, ?, ?, ?, ?)
            """,
            (
                session["user_id"],
                title,
                description,
                area,
                category,
                image_name,
                image_hash,
                float(latitude),
                float(longitude),
                gps_address,
                datetime.utcnow().isoformat(),
            ),
        )
        db.commit()
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        dispatch_emergency_chain(db, cid, category, float(latitude), float(longitude))
        chain = get_dispatches_for_complaint(db, cid)

        admins = db.execute("SELECT id FROM users WHERE role = 'admin'").fetchall()
        for admin in admins:
            add_notification(
                admin["id"],
                f"CRITICAL EMERGENCY #{cid}: {category} at {area}. Emergency chain activated.",
                cid,
            )
        add_notification(
            session["user_id"],
            f"{t('emergency.dispatched')} — Reference #{cid}",
            cid,
        )
        session["last_emergency_chain"] = cid
        flash(f"{t('emergency.dispatched')}! ID: #{cid}", "danger")
        return render_template(
            "emergency_success.html",
            complaint_id=cid,
            chain=chain,
            category=category,
        )

    return render_template("emergency.html")


@app.route("/track", methods=["GET", "POST"])
def track():
    complaint = None
    if request.method == "POST":
        cid = request.form.get("complaint_id", "").strip()
        if cid.isdigit():
            db = get_db()
            complaint = db.execute(
                """
                SELECT c.*, u.full_name FROM complaints c
                JOIN users u ON u.id = c.user_id WHERE c.id = ?
                """,
                (int(cid),),
            ).fetchone()
        if not complaint:
            flash("Complaint not found. Check your ID.", "warning")
    chain = []
    if complaint and complaint["is_emergency"]:
        chain = get_dispatches_for_complaint(get_db(), complaint["id"])
    return render_template("track.html", complaint=complaint, emergency_chain=chain)


@app.route("/history")
@login_required
def history():
    db = get_db()
    complaints = db.execute(
        "SELECT * FROM complaints WHERE user_id = ? ORDER BY created_at DESC",
        (session["user_id"],),
    ).fetchall()
    followed_ids = {
        r["complaint_id"]
        for r in db.execute(
            "SELECT complaint_id FROM follows WHERE user_id = ?", (session["user_id"],)
        ).fetchall()
    }
    notifications = get_unread_notifications(session["user_id"])
    return render_template(
        "history.html",
        complaints=complaints,
        followed_ids=followed_ids,
        notifications=notifications,
    )


@app.route("/follow/<int:cid>", methods=["POST"])
@login_required
def follow_complaint(cid):
    db = get_db()
    exists = db.execute("SELECT id FROM complaints WHERE id = ?", (cid,)).fetchone()
    if not exists:
        flash("Complaint not found.", "danger")
        return redirect(url_for("history"))
    try:
        db.execute(
            "INSERT INTO follows (user_id, complaint_id, created_at) VALUES (?, ?, ?)",
            (session["user_id"], cid, datetime.utcnow().isoformat()),
        )
        db.commit()
        add_notification(session["user_id"], f"You are now following complaint #{cid}.", cid)
        flash(f"You are now following complaint #{cid}.", "success")
    except sqlite3.IntegrityError:
        flash("You already follow this complaint.", "info")
    return redirect(request.referrer or url_for("history"))


@app.route("/unfollow/<int:cid>", methods=["POST"])
@login_required
def unfollow_complaint(cid):
    db = get_db()
    db.execute(
        "DELETE FROM follows WHERE user_id = ? AND complaint_id = ?",
        (session["user_id"], cid),
    )
    db.commit()
    flash("Unfollowed complaint.", "info")
    return redirect(request.referrer or url_for("history"))


@app.route("/validate/<int:cid>", methods=["POST"])
@login_required
def validate(cid):
    db = get_db()
    complaint = db.execute("SELECT * FROM complaints WHERE id = ?", (cid,)).fetchone()
    if not complaint:
        flash("Complaint not found.", "danger")
        return redirect(url_for("index"))
    try:
        db.execute(
            "INSERT INTO validations (complaint_id, user_id) VALUES (?, ?)",
            (cid, session["user_id"]),
        )
        new_count = complaint["validation_count"] + 1
        priority = complaint["priority"]
        if new_count >= 20:
            priority = "Critical"
        elif new_count >= 10:
            priority = "High"
        db.execute(
            "UPDATE complaints SET validation_count = ?, priority = ? WHERE id = ?",
            (new_count, priority, cid),
        )
        db.commit()
        owner_id = complaint["user_id"]
        if owner_id != session["user_id"]:
            add_notification(
                owner_id,
                f"{new_count} citizens have validated your complaint #{cid}.",
                cid,
            )
        flash(f"Thank you! {new_count} citizens affected.", "success")
    except sqlite3.IntegrityError:
        flash("You have already validated this complaint.", "info")
    return redirect(request.referrer or url_for("track"))


@app.route("/dashboard")
@admin_required
def dashboard():
    db = get_db()
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")
    sql = """
        SELECT c.*, u.full_name, u.username FROM complaints c
        JOIN users u ON u.id = c.user_id WHERE 1=1
    """
    params = []
    if q:
        sql += " AND (c.title LIKE ? OR c.area LIKE ? OR CAST(c.id AS TEXT) LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if status_filter:
        sql += " AND c.status = ?"
        params.append(status_filter)
    sql += " ORDER BY c.is_emergency DESC, CASE c.priority WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 ELSE 4 END, c.created_at DESC"
    complaints = db.execute(sql, params).fetchall()
    hotspots = get_hotspots(8)
    emergency_chain_log = get_recent_dispatches(db, 15)
    return render_template(
        "dashboard.html",
        complaints=complaints,
        stats=get_stats(),
        hotspots=hotspots,
        emergency_chain_log=emergency_chain_log,
    )


@app.route("/dashboard/update/<int:cid>", methods=["POST"])
@admin_required
def update_complaint(cid):
    status = request.form.get("status")
    priority = request.form.get("priority")
    db = get_db()
    old = db.execute("SELECT user_id, status, title FROM complaints WHERE id = ?", (cid,)).fetchone()
    if old and status:
        db.execute("UPDATE complaints SET status = ? WHERE id = ?", (status, cid))
        if priority:
            db.execute("UPDATE complaints SET priority = ? WHERE id = ?", (priority, cid))
        db.commit()
        if old["status"] != status:
            add_notification(
                old["user_id"],
                f"Complaint #{cid} ({old['title']}) status updated to: {status}",
                cid,
            )
            followers = db.execute(
                "SELECT user_id FROM follows WHERE complaint_id = ?", (cid,)
            ).fetchall()
            for f in followers:
                if f["user_id"] != old["user_id"]:
                    add_notification(
                        f["user_id"],
                        f"Followed complaint #{cid} is now: {status}",
                        cid,
                    )
        flash("Complaint updated.", "success")
    return redirect(url_for("dashboard"))


@app.route("/dashboard/delete/<int:cid>", methods=["POST"])
@admin_required
def delete_complaint(cid):
    db = get_db()
    db.execute("DELETE FROM validations WHERE complaint_id = ?", (cid,))
    db.execute("DELETE FROM follows WHERE complaint_id = ?", (cid,))
    db.execute("DELETE FROM feedback WHERE complaint_id = ?", (cid,))
    db.execute("DELETE FROM emergency_dispatches WHERE complaint_id = ?", (cid,))
    db.execute("DELETE FROM complaints WHERE id = ?", (cid,))
    db.commit()
    flash("Complaint deleted.", "success")
    return redirect(url_for("dashboard"))


@app.route("/analytics")
@admin_required
def analytics():

    db = get_db()

    # =========================
    # CATEGORY ANALYTICS
    # =========================
    categories_raw = db.execute("""
        SELECT category, COUNT(*) AS count
        FROM complaints
        GROUP BY category
        ORDER BY count DESC
    """).fetchall()

    categories = [dict(row) for row in categories_raw]

    # =========================
    # STATUS ANALYTICS
    # =========================
    status_raw = db.execute("""
        SELECT status, COUNT(*) AS count
        FROM complaints
        GROUP BY status
    """).fetchall()

    status_data = [dict(row) for row in status_raw]

    # =========================
    # EMERGENCY TRENDS
    # =========================
    emergencies_raw = db.execute("""
        SELECT DATE(created_at) AS day,
               COUNT(*) AS count
        FROM complaints
        WHERE is_emergency = 1
        GROUP BY DATE(created_at)
        ORDER BY day ASC
        LIMIT 14
    """).fetchall()

    emergencies = [dict(row) for row in emergencies_raw]

    # =========================
    # HOTSPOT ANALYTICS
    # =========================
    hotspots_raw = db.execute("""
        SELECT area,
               COUNT(*) AS count
        FROM complaints
        GROUP BY area
        ORDER BY count DESC
        LIMIT 6
    """).fetchall()

    hotspots = [dict(row) for row in hotspots_raw]

    # =========================
    # DEPARTMENT PERFORMANCE
    # =========================
    departments = [
        "Drainage",
        "Road Damage",
        "Garbage",
        "Water Leakage",
        "Streetlight Issues"
    ]

    dept_perf = []

    for dept in departments:

        total = db.execute("""
            SELECT COUNT(*) AS c
            FROM complaints
            WHERE category = ?
        """, (dept,)).fetchone()["c"]

        resolved = db.execute("""
            SELECT COUNT(*) AS c
            FROM complaints
            WHERE category = ?
            AND status = 'Resolved'
        """, (dept,)).fetchone()["c"]

        pending = total - resolved

        efficiency = round(
            (resolved / total) * 100,
            1
        ) if total else 0

        dept_perf.append({
            "department": dept,
            "efficiency": efficiency,
            "pending": pending
        })

    # =========================
    # OVERALL STATS
    # =========================
    total_complaints = db.execute("""
        SELECT COUNT(*) AS c
        FROM complaints
    """).fetchone()["c"]

    resolved_complaints = db.execute("""
        SELECT COUNT(*) AS c
        FROM complaints
        WHERE status = 'Resolved'
    """).fetchone()["c"]

    pending_complaints = db.execute("""
        SELECT COUNT(*) AS c
        FROM complaints
        WHERE status = 'Pending'
    """).fetchone()["c"]

    emergency_count = db.execute("""
        SELECT COUNT(*) AS c
        FROM complaints
        WHERE is_emergency = 1
    """).fetchone()["c"]

    # =========================
    # RESOLUTION PERCENTAGE
    # =========================
    resolution_pct = round(
        (resolved_complaints / total_complaints) * 100,
        1
    ) if total_complaints else 0

    # =========================
    # MOST COMMON ISSUE
    # =========================
    most_common = (
        categories[0]["category"]
        if categories else "N/A"
    )

    # =========================
    # RENDER TEMPLATE
    # =========================
    return render_template(
        "analytics.html",

        categories=categories,
        status_data=status_data,
        emergencies=emergencies,
        hotspots=hotspots,
        dept_perf=dept_perf,

        resolution_pct=resolution_pct,
        most_common=most_common,

        stats={
            "total": total_complaints,
            "resolved": resolved_complaints,
            "pending": pending_complaints,
            "emergency": emergency_count
        }
    )
@app.route("/transparency")
def transparency():
    db = get_db()
    resolved = db.execute(
        """
        SELECT id, title, category, area, status, created_at,
               CAST((julianday('now') - julianday(created_at)) AS INTEGER) AS days_open
        FROM complaints WHERE status = 'Resolved' ORDER BY created_at DESC LIMIT 50
        """
    ).fetchall()
    dept = []
    for cat in ["Drainage", "Road Damage", "Garbage", "Water Leakage", "Streetlight Issues"]:
        total = db.execute(
            "SELECT COUNT(*) AS c FROM complaints WHERE category = ?", (cat,)
        ).fetchone()["c"]
        resolved_count = db.execute(
            "SELECT COUNT(*) AS c FROM complaints WHERE category = ? AND status = 'Resolved'",
            (cat,),
        ).fetchone()["c"]
        dept.append(
            {
                "name": cat,
                "efficiency": round((resolved_count / total * 100) if total else 0),
                "resolved": resolved_count,
            }
        )
    stats = get_stats()
    return render_template("transparency.html", resolved=resolved, dept=dept, stats=stats)


@app.route("/feedback", methods=["GET", "POST"])
@login_required
def feedback():
    db = get_db()
    if request.method == "POST":
        cid = request.form.get("complaint_id")
        rating = request.form.get("rating")
        text = request.form.get("feedback", "").strip()
        if cid and rating:
            db.execute(
                "INSERT INTO feedback (complaint_id, user_id, rating, feedback, created_at) VALUES (?, ?, ?, ?, ?)",
                (int(cid), session["user_id"], int(rating), text, datetime.utcnow().isoformat()),
            )
            db.commit()
            flash("Thank you for your feedback!", "success")
            return redirect(url_for("feedback"))
    resolved = db.execute(
        """
        SELECT id, title FROM complaints
        WHERE user_id = ? AND status = 'Resolved' ORDER BY created_at DESC
        """,
        (session["user_id"],),
    ).fetchall()
    return render_template("feedback.html", resolved_complaints=resolved)


@app.context_processor
def inject_globals():
    notifs = []
    if session.get("user_id"):
        notifs = get_unread_notifications(session["user_id"])
    lang = get_lang()
    return dict(
        unread_notifications=notifs,
        t=t,
        current_lang=lang,
        supported_langs=SUPPORTED_LANGS,
    )


if __name__ == "__main__":
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=True)