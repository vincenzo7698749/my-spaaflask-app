# -*- coding: utf-8 -*-
"""
Bulann Moonlight Spa @ Maison Hotel Bangkok - ระบบ Booking Sheet ออนไลน์ (ไฟล์เดียว)
Flask + SQLite  |  ติดตั้ง: pip install flask  |  รัน: python spa_app.py -> http://localhost:5000

สี: ฟ้า=Booking  เขียว=Check In  เทา=Check Out  ดำ=OFF Duty  เหลือง=Break
ลากเมาส์บนตารางเพื่อกำหนดเวลาเริ่ม-สิ้นสุดได้อิสระ (หรือแก้เวลาในฟอร์ม)
เข้าสู่ระบบ: ผู้ใช้เดียว  user = admin  password = spaa1234
"""
import base64, calendar, csv, io, os, re, sqlite3
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, abort, g, redirect, render_template, request, session,
                   url_for, flash, Response)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

DB = os.environ.get("SPA_DB", "bulann.db")
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

OPEN, CLOSE, PPM = 9 * 60, 22 * 60, 2  # ตาราง 09:00-22:00, 1 นาที = 2 พิกเซล
KINDS = {"bk": "Booking (จองทรีตเมนต์)", "break": "Break (พัก)", "off": "OFF Duty (หยุด)"}
SHORT = {"break": "Break", "off": "OFF Duty"}
BNOTES = ["ไปพัก", "เข้าห้องน้ำ", "อื่นๆ"]  # หมายเหตุของ Break / OFF Duty
STATUS = {"bk": "Booking", "in": "Checked In", "out": "Checked Out", "cancel": "Cancelled"}
ROLES = {"manager": "หัวหน้า", "reception": "ต้อนรับ", "therapist": "เทอราปิสต์"}

# ------------------------------------------------------------------ DB
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()

def q(sql, args=(), one=False):
    rows = db().execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows

def run(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()  # บันทึกทันทีทุกครั้ง ไม่ต้องกดเซฟ
    return cur.lastrowid

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def today():
    return datetime.now().strftime("%Y-%m-%d")

def log(action, detail=""):
    run("INSERT INTO logs(at,staff,action,detail) VALUES(?,?,?,?)", (now(), session.get("name", "-"), action, detail))

def mins(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])

def hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"

def init_db():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS staff(id INTEGER PRIMARY KEY, username TEXT UNIQUE, pw TEXT, name TEXT, role TEXT);
    CREATE TABLE IF NOT EXISTS therapists(id INTEGER PRIMARY KEY, name TEXT, active INT DEFAULT 1);
    CREATE TABLE IF NOT EXISTS rooms(id INTEGER PRIMARY KEY, name TEXT, active INT DEFAULT 1);
    CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, kind TEXT DEFAULT 'bk', guest_name TEXT,
        hotel_room TEXT, service_id INT, treatment TEXT, therapist_id INT, room_id INT,
        start_dt TEXT, end_dt TEXT, status TEXT DEFAULT 'bk', note TEXT, cancel_reason TEXT,
        created_by TEXT, created_at TEXT, cancelled_by TEXT, cancelled_at TEXT, phone TEXT);
    CREATE TABLE IF NOT EXISTS extras(id INTEGER PRIMARY KEY, day TEXT, kind TEXT, tm TEXT, text TEXT, by_name TEXT);
    CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY, at TEXT, staff TEXT, action TEXT, detail TEXT);
    """)
    for col in ("cancelled_by", "cancelled_at", "phone"):  # รองรับฐานข้อมูลเวอร์ชันก่อนหน้า
        try:
            c.execute(f"ALTER TABLE bookings ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    if not c.execute("SELECT 1 FROM staff").fetchone():  # ติดตั้งครั้งแรก
        c.executemany("INSERT INTO therapists(name) VALUES(?)", [("Karn",), ("Aey",), ("Panda",)])
        c.executemany("INSERT INTO rooms(name) VALUES(?)", [("ห้อง 1",), ("ห้อง 2",), ("ห้อง 3",)])
    # ใช้ผู้ใช้เดียว: admin / spaa1234 (ฐานข้อมูลเก่าที่มีหลายบัญชีจะถูกปรับให้เหลือบัญชีเดียวอัตโนมัติ)
    us = c.execute("SELECT id,username,pw FROM staff").fetchall()
    if not (len(us) == 1 and us[0][1] == "admin"):
        c.execute("DELETE FROM staff")
        c.execute("INSERT INTO staff(username,pw,name,role) VALUES('admin',?,'Admin','manager')",
                  (generate_password_hash("spaa1234"),))
    elif check_password_hash(us[0][2], "admin1234"):
        c.execute("UPDATE staff SET pw=? WHERE id=?", (generate_password_hash("spaa1234"), us[0][0]))
    c.commit()
    c.close()

# ------------------------------------------------------------------ auth
def login_required(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if "uid" not in session:
                return redirect(url_for("login"))
            if roles and session["role"] not in roles:
                flash("คุณไม่มีสิทธิ์ทำรายการนี้", "err")
                return redirect(url_for("day"))
            return f(*a, **k)
        return w
    return deco

def can_edit():
    return session.get("role") in ("manager", "reception")

@app.route("/logo/<n>.png")
def logo(n):
    if n not in LOGO_B64:
        abort(404)
    return Response(base64.b64decode("".join(LOGO_B64[n].split())), mimetype="image/png",
                    headers={"Cache-Control": "max-age=86400"})

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM staff WHERE username=?", (request.form["username"].strip(),), one=True)
        if u and check_password_hash(u["pw"], request.form["password"]):
            session.update(uid=u["id"], name=u["name"], role=u["role"])
            log("Login")
            return redirect(url_for("day"))
        flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "err")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/pw", methods=["GET", "POST"])
@login_required()
def pw():
    if request.method == "POST":
        u = q("SELECT * FROM staff WHERE id=?", (session["uid"],), one=True)
        if not check_password_hash(u["pw"], request.form["old"]) or len(request.form["new"]) < 6:
            flash("รหัสผ่านเดิมไม่ถูกต้อง หรือรหัสใหม่สั้นเกินไป (อย่างน้อย 6 ตัว)", "err")
        else:
            run("UPDATE staff SET pw=? WHERE id=?", (generate_password_hash(request.form["new"]), u["id"]))
            log("เปลี่ยนรหัสผ่าน")
            flash("เปลี่ยนรหัสผ่านแล้ว", "ok")
            return redirect(url_for("day"))
    return render_template("pw.html")

# ------------------------------------------------------------------ day sheet
BOOK = """SELECT b.*, t.name tname, r.name rname FROM bookings b
 LEFT JOIN therapists t ON t.id=b.therapist_id LEFT JOIN rooms r ON r.id=b.room_id"""

def layout(items):
    items.sort(key=lambda x: x["s"])
    clusters, cur, end = [], [], 0
    for x in items:
        if cur and x["s"] >= end:
            clusters.append(cur)
            cur, end = [], 0
        cur.append(x)
        end = max(end, x["e"])
    if cur:
        clusters.append(cur)
    for cl in clusters:
        lanes = []
        for x in sorted(cl, key=lambda y: (y["r"]["kind"] != "bk", y["s"])):
            for i, ln in enumerate(lanes):
                if all(x["e"] <= y["s"] or x["s"] >= y["e"] for y in ln):
                    ln.append(x)
                    x["lane"] = i
                    break
            else:
                lanes.append([x])
                x["lane"] = len(lanes) - 1
        for x in cl:
            x["w"], x["left"] = 100 / len(lanes), x["lane"] * 100 / len(lanes)

@app.route("/")
@login_required()
def day():
    d = request.args.get("d") or today()
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return redirect(url_for("day"))
    v = "room" if request.args.get("v") == "room" else "th"
    cols = q("SELECT id,name FROM therapists WHERE active=1 ORDER BY id") if v == "th" else \
        q("SELECT id,name FROM rooms WHERE active=1 ORDER BY id")
    key = "therapist_id" if v == "th" else "room_id"
    rows = q(BOOK + " WHERE substr(b.start_dt,1,10)=? AND b.status!='cancel' ORDER BY b.start_dt", (d,))
    blocks = {c["id"]: [] for c in cols}
    for r in rows:
        if r[key] not in blocks or (v == "room" and r["kind"] != "bk"):
            continue
        s, e = max(OPEN, mins(r["start_dt"][11:])), min(CLOSE, mins(r["end_dt"][11:]))
        if e > s:
            blocks[r[key]].append(dict(r=r, s=s, e=e, top=(s - OPEN) * PPM, h=max(22, (e - s) * PPM)))
    for items in blocks.values():  # Break/OFF Duty ซ้อนกับคิวลูกค้าได้ -> แบ่งช่องซ้าย-ขวา
        layout(items)
    brks = [r for r in rows if r["kind"] != "bk"]
    nm = datetime.now().hour * 60 + datetime.now().minute
    on_now = [r for r in brks if d == today() and mins(r["start_dt"][11:]) <= nm < mins(r["end_dt"][11:])]
    return render_template("day.html", d=d, v=v, cols=cols, blocks=blocks, brks=brks, on_now=on_now, H=(CLOSE - OPEN) * PPM, PPM=PPM,
                           hours=range(OPEN // 60, CLOSE // 60), can=can_edit(), SHORT=SHORT,
                           prev=(dt - timedelta(days=1)).strftime("%Y-%m-%d"),
                           nxt=(dt + timedelta(days=1)).strftime("%Y-%m-%d"), dname=dt.strftime("%A %d %B %Y"),
                           cancelled=q(BOOK + " WHERE substr(b.start_dt,1,10)=? AND b.status='cancel' ORDER BY b.start_dt", (d,)),
                           extras=q("SELECT * FROM extras WHERE day=? ORDER BY id", (d,)))

@app.route("/extra", methods=["POST"])
@login_required()
def extra():
    f = request.form
    if f.get("text", "").strip():
        run("INSERT INTO extras(day,kind,tm,text,by_name) VALUES(?,?,?,?,?)",
            (f["d"], f["kind"], f.get("tm", "").strip(), f["text"].strip(), session["name"]))
        log("เพิ่ม " + f["kind"], f["text"].strip())
    return redirect(url_for("day", d=f["d"]))

@app.route("/extra/del/<int:eid>", methods=["POST"])
@login_required("manager", "reception")
def extra_del(eid):
    e = q("SELECT * FROM extras WHERE id=?", (eid,), one=True)
    if e:
        run("DELETE FROM extras WHERE id=?", (eid,))
        log("ลบ " + e["kind"], e["text"])
    return redirect(url_for("day", d=e["day"] if e else today()))

# ------------------------------------------------------------------ booking form (เวลาเริ่ม-สิ้นสุดอิสระ)
def overlap(col, val, st, en, ignore):
    return q(f"SELECT guest_name,kind,start_dt,end_dt FROM bookings WHERE {col}=? AND status!='cancel' AND id!=? "
             "AND kind='bk' AND start_dt<? AND end_dt>?", (val, ignore, en, st), one=True)

def valid_time(t):
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t or ""))

@app.route("/new", methods=["GET", "POST"])
@app.route("/edit/<int:bid>", methods=["GET", "POST"])
@login_required("manager", "reception")
def form(bid=None):
    ths = q("SELECT * FROM therapists WHERE active=1 ORDER BY id")
    rms = q("SELECT * FROM rooms WHERE active=1 ORDER BY id")
    b = q("SELECT * FROM bookings WHERE id=?", (bid,), one=True) if bid else None
    if request.method == "POST":
        f, errs = request.form, []
        kind = f.get("kind", "bk")
        d, t, e = f.get("d", ""), f.get("t", ""), f.get("e", "")
        th, room = f.get("therapist_id"), f.get("room_id") or None
        name = f.get("guest_name", "").strip()
        if not th:
            errs.append("กรุณาเลือกเทอราปิสต์")
        if kind == "bk" and not name:
            errs.append("กรุณากรอกชื่อแขก")
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            errs.append("วันที่ไม่ถูกต้อง")
        if not (valid_time(t) and valid_time(e)):
            errs.append("กรุณาเลือกเวลาเริ่มและเวลาสิ้นสุด")
        elif mins(e) <= mins(t):
            errs.append("เวลาสิ้นสุดต้องหลังเวลาเริ่ม")
        elif mins(t) < OPEN or mins(e) > CLOSE:
            errs.append(f"เวลาต้องอยู่ระหว่าง {hm(OPEN)}-{hm(CLOSE)}")
        note = f.get("note", "").strip()
        phone = re.sub(r"[\s-]", "", f.get("phone", "")) if kind == "bk" else ""
        if phone and not re.fullmatch(r"\+?\d{8,15}", phone):
            errs.append("เบอร์โทรไม่ถูกต้อง (ตัวเลข 8-15 หลัก)")
        if kind != "bk":
            room, name = None, ""
            pick, oth = f.get("bnote", BNOTES[0]), f.get("bother", "").strip()
            note = pick if pick != "อื่นๆ" else ("อื่นๆ: " + oth if oth else "อื่นๆ")
        st, en = f"{d} {t}", f"{d} {e}"
        if not errs:
            for col, label, val in (("therapist_id", "เทอราปิสต์", th), ("room_id", "ห้อง", room)):
                c = overlap(col, val, st, en, bid or 0) if val and kind == "bk" else None
                if c:
                    errs.append(f"{label}ไม่ว่าง ชนกับ {c['guest_name'] or SHORT.get(c['kind'], '')} "
                                f"({c['start_dt'][11:]}-{c['end_dt'][11:]})")
        if errs:
            for m in errs:
                flash(m, "err")
            v = dict(f, kind=kind)
        else:
            vals = (kind, name, f.get("hotel_room", "").strip() if kind == "bk" else "", SHORT.get(kind, ""),
                    th, room, st, en, note, phone)
            if bid:
                run("UPDATE bookings SET kind=?,guest_name=?,hotel_room=?,treatment=?,therapist_id=?,room_id=?,"
                    "start_dt=?,end_dt=?,note=?,phone=? WHERE id=?", vals + (bid,))
                log("แก้ไขคิว", f"#{bid} {name or SHORT.get(kind)} {st}-{e}")
            else:
                nid = run("INSERT INTO bookings(kind,guest_name,hotel_room,treatment,therapist_id,room_id,start_dt,"
                          "end_dt,note,phone,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          vals + (session["name"], now()))
                log("สร้างคิว", f"#{nid} {name or SHORT.get(kind)} {st}-{e}")
            flash("บันทึกเรียบร้อย", "ok")
            return redirect(url_for("day", d=d))
    elif b:
        n0 = b["note"] or ""
        bp = n0 if n0 in BNOTES else "อื่นๆ"
        bo = n0[5:].lstrip(": ") if n0.startswith("อื่นๆ") else ("" if n0 in BNOTES else n0)
        v = dict(b, d=b["start_dt"][:10], t=b["start_dt"][11:], e=b["end_dt"][11:], bnote=bp, bother=bo,
                 therapist_id=str(b["therapist_id"]), room_id=str(b["room_id"] or ""))
    else:
        a = request.args
        t = a.get("t") if valid_time(a.get("t")) else "10:00"
        e = a.get("e") if valid_time(a.get("e")) else hm(min(CLOSE, mins(t) + 60))
        v = dict(kind="bk", d=a.get("d") or today(), t=t, e=e, therapist_id=a.get("th", ""), room_id=a.get("rm", ""))
    return render_template("form.html", v=v, ths=ths, rms=rms, KINDS=KINDS, BNOTES=BNOTES, bid=bid)

# ------------------------------------------------------------------ booking detail / status
@app.route("/b/<int:bid>")
@login_required()
def show(bid):
    b = q(BOOK + " WHERE b.id=?", (bid,), one=True)
    if not b:
        return redirect(url_for("day"))
    dur = mins(b["end_dt"][11:]) - mins(b["start_dt"][11:])
    return render_template("show.html", b=b, can=can_edit(), dur=f"{dur // 60} ชม. {dur % 60} นาที" if dur >= 60 else f"{dur} นาที",
                           SHORT=SHORT)

@app.route("/s/<int:bid>/<st>", methods=["POST"])
@login_required()
def setst(bid, st):
    b = q("SELECT * FROM bookings WHERE id=?", (bid,), one=True)
    if not b or st not in STATUS or b["kind"] != "bk":
        return redirect(url_for("day"))
    if st == "cancel" and session["role"] == "therapist":
        flash("เทอราปิสต์ยกเลิกคิวไม่ได้", "err")
        return redirect(url_for("show", bid=bid))
    if st == "cancel":
        who = request.form.get("staff", "").strip()
        if not who:
            flash("กรุณากรอกชื่อพนักงานที่ยกเลิก", "err")
            return redirect(url_for("show", bid=bid))
        run("UPDATE bookings SET status='cancel',cancel_reason=?,cancelled_by=?,cancelled_at=? WHERE id=?",
            (request.form.get("reason", "").strip(), who, now(), bid))
    else:
        run("UPDATE bookings SET status=?,cancel_reason=NULL,cancelled_by=NULL,cancelled_at=NULL WHERE id=?", (st, bid))
    log("เปลี่ยนสถานะ", f"#{bid} {b['guest_name']} -> {STATUS[st]} {request.form.get('reason', '')}")
    return redirect(url_for("day", d=b["start_dt"][:10]))

@app.route("/del/<int:bid>", methods=["POST"])
@login_required("manager", "reception")
def delete(bid):
    b = q("SELECT * FROM bookings WHERE id=?", (bid,), one=True)
    if not b or (b["kind"] == "bk" and session["role"] != "manager"):
        flash("ลบได้เฉพาะหัวหน้า (พนักงานต้อนรับใช้ 'Cancel' แทน)", "err")
        return redirect(url_for("day"))
    run("DELETE FROM bookings WHERE id=?", (bid,))
    log("ลบคิว", f"#{bid} {b['guest_name'] or b['treatment']} {b['start_dt']}")
    return redirect(url_for("day", d=b["start_dt"][:10]))

# ------------------------------------------------------------------ ประวัติลูกค้า / ประวัติยกเลิก
@app.route("/customers")
@login_required()
def customers():
    kw = request.args.get("s", "").strip()
    rows = q("""SELECT MIN(guest_name) name, lower(trim(guest_name)) k, COUNT(*) total,
        SUM(status IN ('in','out')) visits, SUM(status='cancel') cancels, MAX(start_dt) last, MAX(NULLIF(phone,'')) phone
        FROM bookings WHERE kind='bk' AND (guest_name LIKE ? OR phone LIKE ?) GROUP BY k ORDER BY last DESC LIMIT 200""", (f"%{kw}%", f"%{kw}%"))
    return render_template("customers.html", rows=rows, kw=kw)

@app.route("/customer")
@login_required()
def customer():
    n = request.args.get("n", "")
    rows = q(BOOK + " WHERE b.kind='bk' AND lower(trim(b.guest_name))=lower(?) ORDER BY b.start_dt DESC", (n.strip(),))
    if not rows:
        return redirect(url_for("customers"))
    st = {s: sum(1 for r in rows if r["status"] == s) for s in STATUS}
    return render_template("customer.html", rows=rows, name=rows[0]["guest_name"], st=st)

@app.route("/cancels")
@login_required("manager", "reception")
def cancels():
    kw, m = request.args.get("s", "").strip(), request.args.get("m", "").strip()
    sql, args = BOOK + " WHERE b.status='cancel' AND (b.guest_name LIKE ? OR b.phone LIKE ?)", [f"%{kw}%", f"%{kw}%"]
    if re.fullmatch(r"\d{4}-\d{2}", m):
        sql += " AND substr(b.start_dt,1,7)=?"
        args.append(m)
    return render_template("cancels.html", rows=q(sql + " ORDER BY b.start_dt DESC LIMIT 300", args), kw=kw, m=m)

# ------------------------------------------------------------------ รายงาน วัน/สัปดาห์/เดือน/ปี
def period(p, d):
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        dt = datetime.now()
    if p == "day":
        a, b = dt, dt + timedelta(days=1)
        label, keys = dt.strftime("%A %d %B %Y"), [(f"{h:02d}:00", f"{h:02d}:00") for h in range(9, 22)]
        kf = lambda s: s[11:13] + ":00"
    elif p == "week":
        a = dt - timedelta(days=dt.weekday())
        b = a + timedelta(days=7)
        label = f"{a:%d %b} - {b - timedelta(days=1):%d %b %Y} (จันทร์-อาทิตย์)"
        keys = [((a + timedelta(days=i)).strftime("%Y-%m-%d"), (a + timedelta(days=i)).strftime("%a %d/%m")) for i in range(7)]
        kf = lambda s: s[:10]
    elif p == "month":
        a = dt.replace(day=1)
        b = (a + timedelta(days=32)).replace(day=1)
        label = a.strftime("%B %Y")
        keys = [((a + timedelta(days=i)).strftime("%Y-%m-%d"), (a + timedelta(days=i)).strftime("%a %d"))
                for i in range((b - a).days)]
        kf = lambda s: s[:10]
    else:
        p = "year"
        a, b = dt.replace(month=1, day=1), dt.replace(year=dt.year + 1, month=1, day=1)
        label = str(a.year)
        keys = [(f"{a.year}-{i:02d}", datetime(a.year, i, 1).strftime("%B")) for i in range(1, 13)]
        kf = lambda s: s[:7]
    return p, a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d"), label, keys, kf

def report_data(p, d):
    p, a, b, label, keys, kf = period(p, d)
    rows = q(BOOK + " WHERE b.kind='bk' AND substr(b.start_dt,1,10)>=? AND substr(b.start_dt,1,10)<? "
             "ORDER BY b.start_dt", (a, b))
    ths = [t for t in q("SELECT id,name,active FROM therapists ORDER BY id")]
    ths = [t for t in ths if t["active"] or any(r["therapist_id"] == t["id"] for r in rows)]
    bk = {k: dict(text=t, n=0, c=0, h=0.0, th={x["id"]: 0.0 for x in ths}) for k, t in keys}
    tth = {x["id"]: dict(name=x["name"], n=0, h=0.0, days=set()) for x in ths}
    rm, guests, done, canc, n_all, hours = {}, set(), 0, 0, 0, 0.0
    for r in rows:
        h = (mins(r["end_dt"][11:]) - mins(r["start_dt"][11:])) / 60
        x = bk.get(kf(r["start_dt"]))
        if r["status"] == "cancel":
            canc += 1
            if x: x["c"] += 1
            continue
        n_all += 1
        if x: x["n"] += 1
        if r["status"] in ("in", "out"):
            done += 1
            hours += h
            guests.add((r["guest_name"] or "").strip().lower())
            if x and r["therapist_id"] in x["th"]: x["h"] += h; x["th"][r["therapist_id"]] += h
            if r["therapist_id"] in tth:
                tth[r["therapist_id"]]["n"] += 1; tth[r["therapist_id"]]["h"] += h
                tth[r["therapist_id"]]["days"].add(r["start_dt"][:10])
            z = rm.setdefault(r["rname"] or "ไม่ระบุห้อง", dict(n=0, h=0.0))
            z["n"] += 1; z["h"] += h
    ta = q("SELECT COUNT(*) n FROM extras WHERE kind='turnaway' AND day>=? AND day<?", (a, b), one=True)["n"]
    return dict(p=p, a=a, b=b, label=label, ths=ths, buckets=list(bk.values()), tth=list(tth.values()),
                rooms=sorted(rm.items()), n=n_all, done=done, cancel=canc, ta=ta, guests=len(guests), hours=hours)

@app.route("/report")
@login_required("manager")
def report():
    d = request.args.get("d") or today()
    r = report_data(request.args.get("p", "day"), d)
    return render_template("report.html", r=r, p=r["p"], d=d)

@app.route("/export")
@login_required("manager")
def export():
    d = request.args.get("d") or today()
    r, out = report_data(request.args.get("p", "day"), d), io.StringIO()
    w = csv.writer(out)
    if request.args.get("k") == "bookings":
        w.writerow(["ID", "Date", "Start", "End", "Type", "Guest", "Phone", "Therapist", "Room", "Hotel room", "Status",
                    "Note", "Cancel reason", "Created by"])
        for x in q(BOOK + " WHERE substr(b.start_dt,1,10)>=? AND substr(b.start_dt,1,10)<? ORDER BY b.start_dt",
                   (r["a"], r["b"])):
            w.writerow([x["id"], x["start_dt"][:10], x["start_dt"][11:], x["end_dt"][11:], x["kind"], x["guest_name"],
                        x["phone"], x["tname"], x["rname"], x["hotel_room"], STATUS[x["status"]], x["note"], x["cancel_reason"],
                        x["created_by"]])
        name = f"bookings_{r['p']}_{r['a']}.csv"
    else:
        w.writerow([r["p"], r["label"]])
        w.writerow(["Period", "Bookings", "Cancelled", "Hours"] + [t["name"] for t in r["ths"]])
        for x in r["buckets"]:
            w.writerow([x["text"], x["n"], x["c"], round(x["h"], 2)] + [round(v, 2) for v in x["th"].values()])
        w.writerow(["TOTAL", r["n"], r["cancel"], round(r["hours"], 2)] + [round(t["h"], 2) for t in r["tth"]])
        w.writerow(["Work Day", "", "", ""] + [len(t["days"]) for t in r["tth"]])
        w.writerow(["Turn Away", r["ta"]])
        name = f"report_{r['p']}_{r['a']}.csv"
    log("Export", name)
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})

@app.route("/logs")
@login_required("manager")
def logs():
    return render_template("logs.html", rows=q("SELECT * FROM logs ORDER BY id DESC LIMIT 300"))

# ------------------------------------------------------------------ settings (เพิ่ม/แก้ไข/ลบ)
@app.route("/settings", methods=["GET", "POST"])
@login_required("manager")
def settings():
    if request.method == "POST":
        f = request.form
        t, a, name, i = f.get("t"), f.get("a"), f.get("name", "").strip(), f.get("id")
        if t in ("therapists", "rooms"):
            col = "therapist_id" if t == "therapists" else "room_id"
            if a == "add" and name:
                run(f"INSERT INTO {t}(name) VALUES(?)", (name,))
            elif a == "edit" and name:
                run(f"UPDATE {t} SET name=? WHERE id=?", (name, i))
            elif a == "toggle":
                run(f"UPDATE {t} SET active=1-active WHERE id=?", (i,))
            elif a == "delete":
                n = q(f"SELECT COUNT(*) n FROM bookings WHERE {col}=?", (i,), one=True)["n"]
                if n:
                    flash(f"มีประวัติการจอง {n} รายการ ลบไม่ได้ (กด 'ซ่อน' แทนเพื่อเก็บประวัติไว้)", "err")
                else:
                    run(f"DELETE FROM {t} WHERE id=?", (i,))
            else:
                flash("กรุณากรอกชื่อ", "err")
            log("ตั้งค่า", f"{t} {a} {name or i}")
        return redirect(url_for("settings"))
    return render_template("settings.html", ths=q("SELECT * FROM therapists"), rms=q("SELECT * FROM rooms"))

# ------------------------------------------------------------------ templates
BASE = """<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bulann Moonlight Spa</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@300;400;600&family=Sarabun:wght@300;400;600&display=swap" rel="stylesheet">
<style>
:root{--dark:#4a4039;--taupe:#8f8173;--sand:#d9cfc2;--cream:#f1ebe2;--ink:#2f2924;--gold:#a58f6f}
*{box-sizing:border-box}body{margin:0;font:16px/1.45 "Sarabun","Montserrat",system-ui,sans-serif;background:var(--cream);color:var(--ink)}
nav{background:var(--dark);color:#fff;display:flex;flex-wrap:wrap;gap:2px;padding:8px 12px;align-items:center}
nav img{height:24px;margin-right:12px}nav a{color:#fff;text-decoration:none;padding:8px 11px;border-radius:8px;font-size:15px}nav a:hover{background:#ffffff22}
main{max-width:1200px;margin:0 auto;padding:12px}
.card{background:#fff;border-radius:12px;padding:14px;margin-bottom:12px;box-shadow:0 1px 4px #0001}
h1,h2{font-family:"Montserrat","Sarabun",sans-serif;font-weight:600;letter-spacing:.04em}h1{font-size:21px;margin:4px 0 12px}h2{font-size:17px;margin:0 0 8px}
label{display:block;font-weight:600;margin:10px 0 4px}
input,select,textarea{width:100%;padding:11px;border:1.5px solid var(--sand);border-radius:8px;font:inherit;background:#fff}
.btn{display:inline-block;border:0;border-radius:8px;padding:11px 18px;font:inherit;font-weight:600;background:var(--dark);color:#fff;cursor:pointer;text-decoration:none}
.btn.gold{background:var(--gold)}.btn.sm{padding:7px 12px;font-size:14px}.btn.red{background:#b5544a}.btn.ghost{background:var(--sand);color:var(--ink)}.btn.big{padding:15px 22px;font-size:18px}
.flash{padding:11px 14px;border-radius:8px;margin-bottom:10px}.flash.err{background:#f6dcd8;color:#8c2f25}.flash.ok{background:#e1ecd9;color:#33612a}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.grow{flex:1;min-width:130px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}@media(max-width:640px){.grid2{grid-template-columns:1fr}}
.muted{color:#7a6f65;font-size:14px}
.bk{background:#9dc3e6}.in{background:#a9d18e}.out{background:#9e9e9e;color:#fff}.off{background:#1c1c1c;color:#fff}.break{background:#ffe27a}
.chip{display:inline-block;padding:3px 12px;border-radius:99px;font-size:13px;font-weight:600;margin-right:4px}
.wrap{overflow:auto;max-height:78vh;background:#fff;border-radius:10px;border:1px solid var(--sand)}
.dg{display:flex;min-width:100%}.col{flex:1;min-width:140px;border-left:1px solid var(--sand)}
.tc{flex:none;width:58px;position:sticky;left:0;z-index:6;background:var(--cream)}
.ch{position:sticky;top:0;z-index:5;height:38px;line-height:38px;text-align:center;background:var(--taupe);color:#fff;font-weight:600;font-family:"Montserrat","Sarabun",sans-serif}
.tc .ch{z-index:7}
.cb{position:relative;user-select:none;background-image:linear-gradient(#cfc4b6 1px,transparent 1px),linear-gradient(#ebe4da 1px,transparent 1px);background-size:100% 120px,100% 30px;cursor:crosshair}
.tc .cb{cursor:default}.tc span{position:absolute;left:0;right:0;text-align:center;font-size:12px;color:#6b5f55;transform:translateY(1px)}
.blk{position:absolute;left:2px;right:2px;overflow:hidden;border-radius:6px;padding:2px 6px;font-size:13px;line-height:1.25;text-decoration:none;color:inherit;border:1px solid #0002;z-index:2;cursor:pointer}
.dragbox{position:absolute;left:2px;right:2px;background:#4a403944;border:2px dashed var(--dark);border-radius:6px;z-index:3;font-size:12px;padding:1px 5px;pointer-events:none}
table.t{width:100%;border-collapse:collapse}.t th,.t td{text-align:left;padding:7px;border-bottom:1px solid #ece5db}.t .n{text-align:right}.t tr.sum{font-weight:700;background:#f4eee5}
.note{background:#fbf1d3;border-radius:6px;padding:2px 8px;display:inline-block}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}.stat{background:var(--cream);border-radius:10px;padding:10px;text-align:center;font-size:14px}.stat b{display:block;font-size:24px;font-family:"Montserrat",sans-serif}
body.login{min-height:100vh;background:linear-gradient(90deg,#3f3631 0%,#7d7066 49.8%,#a99d90 50%,#cdc0b1 100%);display:flex;align-items:center;justify-content:center;overflow:hidden;position:relative}
.moon{position:absolute;left:-16vmin;top:30%;width:66vmin;height:66vmin;border-radius:50%;border:7px solid #5a4b3f;background:radial-gradient(circle at 45% 40%,#e3dbd2,#9a8f85 75%);opacity:.5}
.lg{position:relative;z-index:1;width:min(440px,92vw);text-align:center;color:#fff;padding:20px}
.lg .brand{width:min(380px,80vw)}.lg h1{font-weight:600;letter-spacing:.3em;font-size:22px;border-top:1px solid #fff;border-bottom:1px solid #fff;padding:20px 0;margin:36px 0 20px}
.lg input{background:#ffffffd9;border:0;text-align:center}.lg .btn{width:100%;background:transparent;border:1.5px solid #fff;letter-spacing:.2em;margin-top:14px}.lg .btn:hover{background:#ffffff33}
.lg label{color:#fff;font-weight:400;letter-spacing:.1em;font-size:13px;text-align:left}.lg .maison{width:130px;margin-top:34px}
</style></head><body class="{{ 'login' if request.endpoint=='login' else '' }}">
{% if session.uid %}<nav><img src="{{ url_for('logo',n='bulann') }}" alt="BULANN"><a href="{{ url_for('day') }}">📅 ตารางรายวัน</a>
{% if session.role in ('manager','reception') %}<a href="{{ url_for('form') }}">➕ จองคิว</a>{% endif %}
<a href="{{ url_for('customers') }}">👤 ประวัติลูกค้า</a>
{% if session.role in ('manager','reception') %}<a href="{{ url_for('cancels') }}">❌ ประวัติยกเลิก</a>{% endif %}
{% if session.role=='manager' %}<a href="{{ url_for('report') }}">📊 รายงาน</a><a href="{{ url_for('settings') }}">⚙ ตั้งค่า</a><a href="{{ url_for('logs') }}">🕘 Log</a>{% endif %}
<span style="margin-left:auto;font-size:14px">{{ session.name }} · <a href="{{ url_for('pw') }}">รหัสผ่าน</a> · <a href="{{ url_for('logout') }}">ออก</a></span></nav>{% endif %}
<main>{% if session.uid and request.endpoint not in ('day','login') %}<p style="margin:0 0 10px"><a class="btn ghost sm" href="{{ url_for('day') }}" onclick="if(history.length>1){history.back();return false}">◀ กลับ</a></p>{% endif %}
{% for c,m in get_flashed_messages(with_categories=true) %}<div class="flash {{c}}">{{ m }}</div>{% endfor %}
{% block body %}{% endblock %}</main></body></html>"""

LOGIN = """{% extends 'base.html' %}{% block body %}<div class="moon"></div>
<div class="lg"><img class="brand" src="{{ url_for('logo',n='bulann') }}" alt="BULANN MOONLIGHT SPA">
<h1>STAFF LOGIN</h1>
{% for c,m in get_flashed_messages(with_categories=true) %}{% endfor %}
<form method="post"><label>USERNAME</label><input name="username" autofocus required>
<label>PASSWORD</label><input name="password" type="password" required>
<button class="btn">LOGIN</button></form>
<img class="maison" src="{{ url_for('logo',n='maison') }}" alt="MAISON HOTEL BANGKOK"></div>
<style>main{max-width:none;padding:0}</style>{% endblock %}"""

PW = """{% extends 'base.html' %}{% block body %}<div class="card" style="max-width:400px"><h1>เปลี่ยนรหัสผ่าน</h1>
<form method="post"><label>รหัสผ่านเดิม</label><input type="password" name="old" required>
<label>รหัสผ่านใหม่ (อย่างน้อย 6 ตัว)</label><input type="password" name="new" required>
<p><button class="btn">บันทึก</button></p></form></div>{% endblock %}"""

DAY = """{% extends 'base.html' %}{% block body %}
<div class="card row">
<a class="btn ghost sm" href="{{ url_for('day',d=prev,v=v) }}">◀</a>
<form method="get" class="grow" style="margin:0"><input type="hidden" name="v" value="{{v}}">
<input type="date" name="d" value="{{d}}" onchange="this.form.submit()"></form>
<a class="btn ghost sm" href="{{ url_for('day',d=nxt,v=v) }}">▶</a><a class="btn sm" href="{{ url_for('day',v=v) }}">วันนี้</a>
<a class="btn sm {{ '' if v=='th' else 'ghost' }}" href="{{ url_for('day',d=d) }}">ตามเทอราปิสต์</a>
<a class="btn sm {{ 'ghost' if v=='th' else '' }}" href="{{ url_for('day',d=d,v='room') }}">ตามห้อง</a>
{% if can %}<a class="btn gold sm" href="{{ url_for('form',d=d) }}">➕ จองคิว</a>{% endif %}</div>
<div class="row" style="margin-bottom:8px"><b>{{ dname }}</b>
<span class="chip bk">Booking</span><span class="chip in">Check In</span><span class="chip out">Check Out</span><span class="chip off">OFF Duty</span><span class="chip break">Break</span>
{% if can %}<span class="muted">💡 ลากเมาส์บนช่องว่างเพื่อกำหนดเวลาเริ่ม-สิ้นสุด</span>{% endif %}</div>
{% if on_now %}<div class="card" style="background:#fff3c4;padding:10px 14px">🟡 ขณะนี้พัก/ไม่อยู่: {% for r in on_now %}<b>{{ r.tname }}</b> ({{ SHORT[r.kind] }}{% if r.note %}: {{ r.note }}{% endif %} {{ r.start_dt[11:] }}–{{ r.end_dt[11:] }}){{ ', ' if not loop.last }}{% endfor %}</div>{% endif %}
<div class="wrap"><div class="dg">
<div class="tc"><div class="ch">เวลา</div><div class="cb" style="height:{{H}}px">{% for h in hours %}<span style="top:{{ (h-9)*60*PPM }}px">{{ '%02d:00'%h }}</span>{% endfor %}</div></div>
{% for c in cols %}<div class="col"><div class="ch">{{ c.name }}</div>
<div class="cb" data-col="{{ c.id }}" style="height:{{H}}px">
{% for x in blocks[c.id] %}{% set r = x.r %}<a class="blk {{ r.kind if r.kind!='bk' else r.status }}" style="top:{{x.top}}px;height:{{x.h}}px;left:calc({{ '%.2f'|format(x.left) }}% + 2px);width:calc({{ '%.2f'|format(x.w) }}% - 4px);right:auto" href="{{ url_for('show',bid=r.id) }}">
{% if r.kind=='bk' %}<b>{{ r.guest_name }}</b> <small>{{ r.start_dt[11:] }}–{{ r.end_dt[11:] }}</small>{% if r.note %}<br>{{ r.note }}{% endif %}<br>{% if r.hotel_room %}🏨 {{ r.hotel_room }} · {% endif %}{{ (r.rname if v=='th' else r.tname) or '' }}
{% else %}<b>{{ SHORT[r.kind] }}</b> <small>{{ r.start_dt[11:] }}–{{ r.end_dt[11:] }}</small>{% if r.note %}<br>{{ r.note }}{% endif %}{% endif %}</a>{% endfor %}
</div></div>{% endfor %}</div></div>
<div class="card" style="margin-top:12px"><h2>Break / OFF Duty ของวันนี้</h2>
{% for r in brks %}<div class="row" style="margin-bottom:4px"><span class="chip {{ r.kind }}">{{ SHORT[r.kind] }}</span><b>{{ r.tname }}</b> {{ r.start_dt[11:] }}–{{ r.end_dt[11:] }} {% if r.note %}<span class="note">{{ r.note }}</span>{% endif %} <a class="btn sm ghost" href="{{ url_for('show',bid=r.id) }}">ดู/แก้ไข</a></div>
{% else %}<span class="muted">ไม่มีรายการ</span>{% endfor %}</div>
<div class="card"><h2>Cancelled</h2>
{% for r in cancelled %}<div>{{ loop.index }}. <a href="{{ url_for('show',bid=r.id) }}"><b>{{ r.guest_name }}</b></a> · {{ r.start_dt[11:] }}–{{ r.end_dt[11:] }} · {{ r.tname }} {% if r.cancel_reason %}<span class="note">{{ r.cancel_reason }}</span>{% endif %}</div>
{% else %}<span class="muted">ไม่มีรายการยกเลิก</span>{% endfor %}</div>
<div class="card"><h2>Turn Away / บันทึกเพิ่มเติม</h2>
{% for e in extras %}<div class="row"><span class="chip {{ 'off' if e.kind=='turnaway' else 'break' }}">{{ 'Turn Away' if e.kind=='turnaway' else 'Note' }}</span>
{{ e.tm }} {{ e.text }} <span class="muted">({{ e.by_name }})</span>
{% if can %}<form method="post" action="{{ url_for('extra_del',eid=e.id) }}" onsubmit="return confirm('ลบรายการนี้?')"><button class="btn sm red">✖</button></form>{% endif %}</div>{% endfor %}
<form method="post" action="{{ url_for('extra') }}" class="row" style="margin-top:8px"><input type="hidden" name="d" value="{{d}}">
<select name="kind" style="width:auto"><option value="turnaway">Turn Away</option><option value="note">Note</option></select>
<input name="tm" placeholder="เวลา" style="width:90px"><input name="text" placeholder="รายละเอียด เช่น ชื่อแขก / เหตุผล" class="grow" required><button class="btn sm">เพิ่ม</button></form></div>
<script>
const CAN={{ 'true' if can else 'false' }},PPM={{PPM}},OPEN=540,CLOSE=1320,KEY={{ ('th' if v=='th' else 'rm')|tojson }},NEW={{ url_for('form',d=d)|tojson }};
const hm=m=>String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0');
if(CAN)document.querySelectorAll('.cb[data-col]').forEach(col=>{
 let st=null,g=null;
 const mm=e=>{const y=e.clientY-col.getBoundingClientRect().top;return Math.max(OPEN,Math.min(CLOSE,OPEN+Math.round(y/PPM/5)*5))};
 const upd=e=>{const a=Math.min(st,mm(e)),b=Math.max(st,mm(e));g.style.top=(a-OPEN)*PPM+'px';g.style.height=Math.max(5,b-a)*PPM+'px';g.textContent=hm(a)+'–'+hm(b)};
 col.addEventListener('mousedown',e=>{if(e.button||e.target.closest('.blk'))return;st=mm(e);g=document.createElement('div');g.className='dragbox';col.appendChild(g);upd(e);e.preventDefault()});
 document.addEventListener('mousemove',e=>{if(st!==null)upd(e)});
 document.addEventListener('mouseup',e=>{if(st===null)return;const en=mm(e);let a=Math.min(st,en),b=Math.max(st,en);st=null;g.remove();
  if(b-a<15){a=Math.min(a,CLOSE-60);b=a+60}
  location.href=NEW+'&t='+hm(a)+'&e='+hm(b)+'&'+KEY+'='+col.dataset.col});
});
setInterval(()=>{if(!document.activeElement||!document.activeElement.matches('input,select,textarea'))location.reload()},30000);
</script>{% endblock %}"""

FORM = """{% extends 'base.html' %}{% block body %}<h1>{{ 'แก้ไขคิว #%s'%bid if bid else '➕ จองคิว / ลงตารางเวลา' }}</h1>
<form method="post" class="card"><label>ประเภท</label>
<select name="kind" id="kind" onchange="tg()">{% for k,l in KINDS.items() %}<option value="{{k}}" {{ 'selected' if v.kind==k }}>{{l}}</option>{% endfor %}</select>
<div class="grid2"><div><label>วันที่ *</label><input type="date" name="d" value="{{ v.d }}" required></div>
<div><label>เทอราปิสต์ *</label><select name="therapist_id" required><option value="">-- เลือก --</option>
{% for t in ths %}<option value="{{t.id}}" {{ 'selected' if v.therapist_id==t.id|string }}>{{t.name}}</option>{% endfor %}</select></div>
<div><label>เวลาเริ่ม *</label><input type="time" name="t" id="t" value="{{ v.t }}" min="09:00" max="22:00" required></div>
<div><label>เวลาสิ้นสุด *</label><input type="time" name="e" id="e" value="{{ v.e }}" min="09:00" max="22:00" required></div></div>
<div class="row" style="margin-top:8px"><span class="muted">ตั้งเวลาสิ้นสุดเร็ว ๆ:</span>
{% for m,l in [(30,'+30 น.'),(60,'+1 ชม.'),(90,'+1.5 ชม.'),(120,'+2 ชม.')] %}<button type="button" class="btn sm ghost" onclick="plus({{m}})">{{l}}</button>{% endfor %}</div>
<div class="grid2"><div class="bkonly"><label>ชื่อแขก (Guest Name) *</label><input name="guest_name" value="{{ v.guest_name or '' }}"></div>
<div class="bkonly"><label>เลขห้องพัก (Room Number)</label><input name="hotel_room" value="{{ v.hotel_room or '' }}"></div>
<div class="bkonly"><label>เบอร์โทรลูกค้า</label><input name="phone" type="tel" inputmode="tel" value="{{ v.phone or '' }}" placeholder="เช่น 0812345678"></div>
<div class="bkonly"><label>ห้องทรีตเมนต์</label><select name="room_id"><option value="">-- ไม่ระบุ --</option>
{% for r in rms %}<option value="{{r.id}}" {{ 'selected' if v.room_id==r.id|string }}>{{r.name}}</option>{% endfor %}</select></div></div>
<div class="bkonly"><label>หมายเหตุ (เช่น Disc.30%, แพ้น้ำมัน, ประเภทนวด)</label><textarea name="note" rows="2">{{ v.note or '' }}</textarea></div>
<div class="brkonly"><label>หมายเหตุ</label><select name="bnote" id="bnote" onchange="tg()">{% for n in BNOTES %}<option {{ 'selected' if v.bnote==n }}>{{n}}</option>{% endfor %}</select>
<input name="bother" id="bother" placeholder="ระบุเหตุผลอื่นๆ" value="{{ v.bother or '' }}" style="margin-top:6px"></div>
<p><button class="btn big" style="width:100%">บันทึก</button></p></form>
<script>function tg(){const k=document.getElementById('kind').value;document.querySelectorAll('.bkonly').forEach(e=>e.style.display=k=='bk'?'':'none');document.querySelectorAll('.brkonly').forEach(e=>e.style.display=k=='bk'?'none':'');document.getElementById('bother').style.display=document.getElementById('bnote').value=='อื่นๆ'?'':'none'}
function plus(m){const t=document.getElementById('t').value;if(!t)return;let x=(+t.slice(0,2))*60+(+t.slice(3))+m;x=Math.min(x,1320);
document.getElementById('e').value=String(Math.floor(x/60)).padStart(2,'0')+':'+String(x%60).padStart(2,'0')}tg();</script>{% endblock %}"""

SHOW = """{% extends 'base.html' %}{% block body %}
<div class="card {{ b.kind if b.kind!='bk' else b.status }}">
<h1>{{ b.guest_name or SHORT[b.kind] }} {% if b.kind=='bk' %}<span class="chip" style="background:#fff;color:#333">{{ STATUS[b.status] }}</span>{% endif %}</h1>
<div>🕐 {{ b.start_dt[:10] }} {{ b.start_dt[11:] }}–{{ b.end_dt[11:] }} ({{ dur }}) · 👩 {{ b.tname }}
{% if b.kind=='bk' %}{% if b.rname %} · 🚪 {{ b.rname }}{% endif %}{% if b.hotel_room %} · 🏨 ห้องพัก {{ b.hotel_room }}{% endif %}{% if b.phone %} · 📞 {{ b.phone }}{% endif %}{% endif %}
{% if b.note %}<br>📝 {{ b.note }}{% endif %}
{% if b.status=='cancel' %}<br>❌ ยกเลิกโดย {{ b.cancelled_by or '-' }} {{ b.cancelled_at or '' }}{% if b.cancel_reason %} · เหตุผล: {{ b.cancel_reason }}{% endif %}{% endif %}</div>
<div style="opacity:.8;font-size:14px">บันทึกโดย {{ b.created_by }} · {{ b.created_at }}</div></div>
<div class="row">
{% if b.kind=='bk' %}
{% set opts=[('in','✔ Check In','btn'),('out','🚪 Check Out','btn gold'),('bk','↩ กลับเป็น Booking','btn ghost')] %}
{% for st,label,cls in opts if st!=b.status and (b.status!='cancel' or st=='bk') %}
<form method="post" action="{{ url_for('setst',bid=b.id,st=st) }}"><button class="{{cls}} big">{{label}}</button></form>{% endfor %}
{% endif %}
{% if can %}<a class="btn ghost big" href="{{ url_for('form',bid=b.id) }}">✏ แก้ไข</a>
{% if b.kind!='bk' or session.role=='manager' %}<form method="post" action="{{ url_for('delete',bid=b.id) }}" onsubmit="return confirm('ยืนยันลบรายการนี้?')"><button class="btn red big">🗑 ลบ</button></form>{% endif %}{% endif %}
<a class="btn ghost big" href="{{ url_for('day',d=b.start_dt[:10]) }}">กลับตาราง</a></div>
{% if b.kind=='bk' and b.status!='cancel' and session.role!='therapist' %}<form method="post" action="{{ url_for('setst',bid=b.id,st='cancel') }}" class="card" style="margin-top:12px"
 onsubmit="localStorage.setItem('staffName',this.staff.value);return confirm('ยืนยันยกเลิกคิวนี้?')"><h2>ยกเลิกคิวนี้</h2>
<div class="grid2"><div><label>ชื่อพนักงานที่ยกเลิก *</label><input name="staff" id="staff" required></div>
<div><label>เหตุผลที่ยกเลิก</label><input name="reason"></div></div>
<p><button class="btn red">✖ ยืนยันยกเลิก</button></p></form>
<script>document.getElementById('staff').value=localStorage.getItem('staffName')||''</script>{% endif %}{% endblock %}"""

CUSTOMERS = """{% extends 'base.html' %}{% block body %}<h1>👤 ประวัติลูกค้า</h1>
<form class="row card"><input name="s" value="{{kw}}" placeholder="ค้นหาจากชื่อหรือเบอร์โทร" class="grow" autofocus><button class="btn">ค้นหา</button></form>
<div class="card" style="overflow-x:auto"><table class="t"><tr><th>ชื่อ</th><th>เบอร์โทร</th><th class="n">จองทั้งหมด</th><th class="n">มาใช้บริการ</th><th class="n">ยกเลิก</th><th>ล่าสุด</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('customer',n=r.k) }}"><b>{{ r.name }}</b></a></td><td>{{ r.phone or '-' }}</td><td class="n">{{ r.total }}</td><td class="n">{{ r.visits }}</td><td class="n">{{ r.cancels }}</td><td>{{ r.last[:10] }}</td></tr>
{% else %}<tr><td class="muted">ไม่พบข้อมูล</td></tr>{% endfor %}</table></div>{% endblock %}"""

CUSTOMER = """{% extends 'base.html' %}{% block body %}<h1>👤 {{ name }} {% for r in rows if r.phone %}{% if loop.first %}<small class="muted">📞 {{ r.phone }}</small>{% endif %}{% endfor %}</h1>
<div class="card"><div class="stats"><div class="stat">จองทั้งหมด<b>{{ rows|length }}</b></div><div class="stat">Checked In<b>{{ st['in'] }}</b></div>
<div class="stat">Checked Out<b>{{ st.out }}</b></div><div class="stat">ยกเลิก<b>{{ st.cancel }}</b></div></div></div>
<div class="card" style="overflow-x:auto"><table class="t"><tr><th>วันที่</th><th>เวลา</th><th>เบอร์โทร</th><th>เทอราปิสต์</th><th>ห้อง</th><th>สถานะ</th><th>หมายเหตุ</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('show',bid=r.id) }}">{{ r.start_dt[:10] }}</a></td><td>{{ r.start_dt[11:] }}–{{ r.end_dt[11:] }}</td><td>{{ r.phone or '' }}</td><td>{{ r.tname }}</td><td>{{ r.rname or '' }}{% if r.hotel_room %} (พัก {{ r.hotel_room }}){% endif %}</td>
<td><span class="chip {{ r.status }}">{{ STATUS[r.status] }}</span></td><td>{{ r.note or '' }}{% if r.cancel_reason %} <span class="note">ยกเลิก: {{ r.cancel_reason }}</span>{% endif %}</td></tr>{% endfor %}</table></div>
<a class="btn ghost" href="{{ url_for('customers') }}">◀ กลับ</a>{% endblock %}"""

CANCELS = """{% extends 'base.html' %}{% block body %}<h1>❌ ประวัติยกเลิกรายการ</h1>
<form class="row card"><input name="s" value="{{kw}}" placeholder="ค้นหาจากชื่อหรือเบอร์โทร" class="grow"><input type="month" name="m" value="{{m}}" style="width:auto"><button class="btn">ค้นหา</button></form>
<div class="card" style="overflow-x:auto"><table class="t"><tr><th>วัน-เวลานัด</th><th>จองเมื่อ</th><th>ลูกค้า</th><th>เบอร์โทร</th><th>เทอราปิสต์</th><th>เหตุผล</th><th>พนักงานที่ยกเลิก</th><th>ยกเลิกเมื่อ</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('show',bid=r.id) }}">{{ r.start_dt|dt }}–{{ r.end_dt[11:] }}</a></td><td>{{ r.created_at|dt }}</td><td><b>{{ r.guest_name }}</b></td><td>{{ r.phone or '-' }}</td><td>{{ r.tname }}</td><td>{{ r.cancel_reason or '-' }}</td><td>{{ r.cancelled_by or '-' }}</td><td>{{ r.cancelled_at|dt }}</td></tr>
{% else %}<tr><td class="muted">ไม่พบรายการยกเลิก</td></tr>{% endfor %}</table></div>{% endblock %}"""

REPORT = """{% extends 'base.html' %}{% block body %}<h1>📊 รายงานสรุป</h1>
<div class="card row">
{% for k,l in [('day','รายวัน'),('week','รายสัปดาห์'),('month','รายเดือน'),('year','รายปี')] %}<a class="btn sm {{ '' if p==k else 'ghost' }}" href="{{ url_for('report',p=k,d=d) }}">{{l}}</a>{% endfor %}
<form method="get" class="grow" style="margin:0"><input type="hidden" name="p" value="{{p}}"><input type="date" name="d" value="{{d}}" onchange="this.form.submit()"></form>
<a class="btn gold sm" href="{{ url_for('export',p=p,d=d) }}">⬇ สรุป (CSV/Excel)</a><a class="btn ghost sm" href="{{ url_for('export',p=p,d=d,k='bookings') }}">⬇ รายการจอง</a></div>
<div class="card"><h2>{{ r.label }}</h2><div class="stats">
<div class="stat">จอง (ไม่รวมยกเลิก)<b>{{ r.n }}</b></div><div class="stat">มาใช้บริการ<b>{{ r.done }}</b></div><div class="stat">ยกเลิก<b>{{ r.cancel }}</b></div>
<div class="stat">Turn Away<b>{{ r.ta }}</b></div><div class="stat">ลูกค้า (คน)<b>{{ r.guests }}</b></div><div class="stat">ชั่วโมงทรีตเมนต์<b>{{ '%.1f'%r.hours }}</b></div></div>
<div class="muted" style="margin-top:6px">ชั่วโมงและจำนวนใช้บริการนับเฉพาะคิวที่ Checked In / Checked Out</div></div>
<div class="card" style="overflow-x:auto"><h2>แยกตามช่วงเวลา</h2><table class="t"><tr><th>ช่วง</th><th class="n">จอง</th><th class="n">ยกเลิก</th><th class="n">ชม.รวม</th>{% for t in r.ths %}<th class="n">{{ t.name }}</th>{% endfor %}</tr>
{% for x in r.buckets %}<tr><td>{{ x.text }}</td><td class="n">{{ x.n or '' }}</td><td class="n">{{ x.c or '' }}</td><td class="n">{{ '%.2f'%x.h if x.h else '' }}</td>{% for v in x.th.values() %}<td class="n">{{ '%.2f'%v if v else '' }}</td>{% endfor %}</tr>{% endfor %}
<tr class="sum"><td>TOTAL</td><td class="n">{{ r.n }}</td><td class="n">{{ r.cancel }}</td><td class="n">{{ '%.2f'%r.hours }}</td>{% for t in r.tth %}<td class="n">{{ '%.2f'%t.h }}</td>{% endfor %}</tr>
<tr class="sum"><td colspan="4">Work Day</td>{% for t in r.tth %}<td class="n">{{ t.days|length }}</td>{% endfor %}</tr></table></div>
<div class="grid2"><div class="card"><h2>ตามเทอราปิสต์</h2><table class="t"><tr><th></th><th class="n">คิว</th><th class="n">ชม.</th><th class="n">วันทำงาน</th></tr>
{% for t in r.tth %}<tr><td>{{ t.name }}</td><td class="n">{{ t.n }}</td><td class="n">{{ '%.2f'%t.h }}</td><td class="n">{{ t.days|length }}</td></tr>{% endfor %}</table></div>
<div class="card"><h2>ตามห้อง</h2><table class="t"><tr><th></th><th class="n">คิว</th><th class="n">ชม.</th></tr>
{% for name,z in r.rooms %}<tr><td>{{ name }}</td><td class="n">{{ z.n }}</td><td class="n">{{ '%.2f'%z.h }}</td></tr>{% else %}<tr><td class="muted">ไม่มีข้อมูล</td></tr>{% endfor %}</table></div></div>{% endblock %}"""

LOGS = """{% extends 'base.html' %}{% block body %}<h1>🕘 ประวัติการแก้ไข (300 รายการล่าสุด)</h1>
<div class="card" style="overflow-x:auto"><table class="t"><tr><th>เวลา</th><th>ผู้ใช้</th><th>การกระทำ</th><th>รายละเอียด</th></tr>
{% for r in rows %}<tr><td>{{r.at}}</td><td>{{r.staff}}</td><td>{{r.action}}</td><td>{{r.detail}}</td></tr>{% endfor %}</table></div>{% endblock %}"""

SETTINGS = """{% extends 'base.html' %}{% block body %}<h1>⚙ ตั้งค่า</h1>
{% for title,t,items in [('เทอราปิสต์ (คอลัมน์ในตารางเวลา)','therapists',ths),('ห้องทรีตเมนต์','rooms',rms)] %}
<div class="card"><h2>{{ title }}</h2>
{% for i in items %}<form method="post" class="row" style="margin-bottom:6px"><input type="hidden" name="t" value="{{t}}"><input type="hidden" name="id" value="{{i.id}}">
<input name="name" value="{{ i.name }}" style="width:240px;padding:7px 10px;{{ '' if i.active else 'opacity:.5' }}" required>
<button class="btn sm" name="a" value="edit">บันทึกชื่อ</button>
<button class="btn sm ghost" name="a" value="toggle">{{ 'ซ่อน' if i.active else 'แสดงอีกครั้ง' }}</button>
<button class="btn sm red" name="a" value="delete" onclick="return confirm('ยืนยันลบรายการนี้?')">ลบ</button></form>{% endfor %}
<form method="post" class="row" style="margin-top:10px"><input type="hidden" name="t" value="{{t}}"><input type="hidden" name="a" value="add">
<input name="name" placeholder="เพิ่มรายการใหม่" style="width:240px;padding:7px 10px" required><button class="btn sm gold">➕ เพิ่ม</button></form></div>{% endfor %}
{% endblock %}"""

app.jinja_loader = DictLoader({"base.html": BASE, "login.html": LOGIN, "pw.html": PW, "day.html": DAY, "form.html": FORM,
                               "show.html": SHOW, "customers.html": CUSTOMERS, "customer.html": CUSTOMER,
                               "cancels.html": CANCELS, "report.html": REPORT, "logs.html": LOGS, "settings.html": SETTINGS})
app.jinja_env.globals["STATUS"] = STATUS

def fmt_dt(s):  # 2026-10-15 09:03:12 -> 15/10/2026 09:03
    try:
        return datetime.strptime(s[:16], "%Y-%m-%d %H:%M").strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return "-"

app.jinja_env.filters["dt"] = fmt_dt

# ------------------------------------------------------------------ โลโก้ (ฝังเป็น base64 ในไฟล์นี้ ไม่ต้องมีไฟล์รูปแยก)
LOGO_B64 = {
    "bulann": """
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
""",
    "maison": """
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
""",
}

# ---------------------------------------------------------
# ระบบอัปโหลดและอัปเดตไฟล์โค้ด (.py)
# ---------------------------------------------------------
app.config['UPLOAD_FOLDER'] = os.getcwd()


@app.route('/upload-page')
def upload_page():
    return render_template('upload.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return 'ไม่พบไฟล์ที่อัปโหลด', 400

    file = request.files['file']

    if file.filename == '':
        return 'ยังไม่ได้เลือกไฟล์', 400

    if file and file.filename.endswith('.py'):
        filename = file.filename
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return f'อัปโหลดไฟล์ {filename} เรียบร้อยแล้ว!'

    return 'อนุญาตให้อัปโหลดเฉพาะไฟล์ .py เท่านั้น', 400


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
