"""
GGSS Zawachiki - Score Management System
Run with:  streamlit run app.py
"""
import base64
import hashlib
import io
import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:      # only needed when using the online database
    psycopg = None

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

# ----------------------------------------------------------------------
# CONFIGURATION  (edit these to suit the school)
# ----------------------------------------------------------------------
SCHOOL_NAME = "GGSS Zawachiki"
DB_PATH = "ggss_scores.db"
BADGE_PATH = Path("assets/badge.png")      # put the school badge here
BOARD_LOGO_PATH = Path("assets/board_logo.png")   # logo shown on the right of report sheets
ID_PREFIX = "GGSZ"                          # IDs look like GGSZ/26/0001

SESSIONS = ["2026/2027", "2027/2028", "2028/2029", "2029/2030", "2030/2031"]
TERMS = ["First Term", "Second Term", "Third Term"]

# Assessment name -> maximum score. Change these to match your school's scheme.
ASSESSMENTS = {"CA": 40, "Examination": 60}

CORE = ["English Language", "Mathematics", "Digital Technology", "Civic Education"]
STREAM_SUBJECTS = {
    "Science": CORE + ["Chemistry", "Biology", "Physics", "Geography",
                       "Food & Nutrition", "B/Cosmetics"],
    "Art": CORE + ["Literature in English", "Government", "Islamic Studies",
                   "Hausa Language", "Catering", "Fashion Design & Garment Making"],
    "Commercial": CORE + ["Account", "Commerce", "Economics", "Marketing",
                          "Catering", "Fashion Design & Garment Making"],
}
# Text printed at the top of the report sheet
BOARD_NAME = "KANO STATE SENIOR SECONDARY SCHOOL MANAGEMENT BOARD"
FULL_SCHOOL_NAME = "GOVERNMENT GIRLS SENIOR SECONDARY SCHOOL"
SCHOOL_TOWN = "ZAWACHIKI"
ZONE_NAME = "KUMBOTSO ZONE"

# How a subject is printed on the report sheet (if different from its normal name)
REPORT_NAMES = {
    "Catering": "Catering Craft",
    "Account": "Financial Accounting",
    "Fashion Design & Garment Making": "Fashion & Garment Making",
}
EXAM_KEY = "Examination"      # every other assessment is added together as C/A

# Grading scale: (lowest percentage, grade, remark)
GRADES = [(75, "A1", "Excellent"), (70, "B2", "Very Good"), (65, "B3", "Good"),
          (60, "C4", "Credit"), (55, "C5", "Credit"), (50, "C6", "Credit"),
          (45, "D7", "Pass"), (40, "E8", "Pass"), (0, "F9", "Fail")]

CLASSES = [f"SS{lvl}{arm}" for lvl in (1, 2, 3) for arm in "ABCD"]


def stream_of(class_name: str) -> str:
    arm = class_name[-1]
    return {"A": "Science", "B": "Art", "C": "Art", "D": "Commercial"}[arm]


def subjects_for(class_name: str) -> list:
    return STREAM_SUBJECTS[stream_of(class_name)]


# ----------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------
def get_db_url():
    """The online database address. If it is not set, a local file is used."""
    try:
        return st.secrets["DATABASE_URL"]
    except Exception:
        return os.environ.get("DATABASE_URL")


DB_URL = get_db_url()
USE_PG = bool(DB_URL)          # True = online (Neon/Postgres), False = local file


@st.cache_resource
def _pg_connection():
    return psycopg.connect(DB_URL, autocommit=True, row_factory=dict_row,
                           connect_timeout=20)


def _execute(sql, params=(), many=False):
    """Run one SQL command on whichever database is in use.
    Returns (rows, column_names). Rows are dictionaries."""
    if USE_PG:
        sql = sql.replace("?", "%s")
        for attempt in (1, 2):
            try:
                with _pg_connection().cursor() as cur:
                    if many:
                        cur.executemany(sql, params)
                        return [], []
                    cur.execute(sql, params)
                    if cur.description:
                        return cur.fetchall(), [d.name for d in cur.description]
                    return [], []
            except (psycopg.OperationalError, psycopg.InterfaceError):
                # the free database goes to sleep when idle: reconnect and retry
                _pg_connection.clear()
                if attempt == 2:
                    raise
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.executemany(sql, params) if many else conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        cols = [d[0] for d in cur.description] if cur.description else []
        conn.commit()
        return rows, cols
    finally:
        conn.close()


def run(sql, params=()):
    return _execute(sql, params)[0]


def run_many(sql, rows):
    _execute(sql, rows, many=True)


def read_df(sql, params=()):
    rows, cols = _execute(sql, params)
    return pd.DataFrame(rows, columns=cols)


def hash_pw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), 200_000
    ).hex()
    return f"{salt}${digest}"


def check_pw(password, stored):
    salt, digest = stored.split("$")
    return secrets.compare_digest(hash_pw(password, salt).split("$")[1], digest)


DATABASE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        full_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('admin','teacher')),
        password_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS students (
        student_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        class_name TEXT NOT NULL,
        gender TEXT,
        guardian_phone TEXT,
        registered_on TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS assignments (
        username TEXT NOT NULL,
        class_name TEXT NOT NULL,
        subject TEXT NOT NULL,
        PRIMARY KEY (username, class_name, subject))""",
    """CREATE TABLE IF NOT EXISTS scores (
        student_id TEXT NOT NULL,
        session TEXT NOT NULL,
        term TEXT NOT NULL,
        subject TEXT NOT NULL,
        assessment TEXT NOT NULL,
        score DOUBLE PRECISION NOT NULL,
        entered_by TEXT,
        updated_on TEXT,
        PRIMARY KEY (student_id, session, term, subject, assessment))""",
)


def init_db():
    for statement in DATABASE_SCHEMA:
        run(statement)

    if not run("SELECT 1 FROM users WHERE role='admin'"):
        try:
            default_pw = st.secrets["ADMIN_PASSWORD"]
        except Exception:
            default_pw = "admin123"
        run("INSERT INTO users VALUES (?,?,?,?)",
            ("admin", "Administrator", "admin", hash_pw(default_pw)))


def create_full_backup():
    """Return an Excel backup workbook containing the app's school data."""
    sheets = (
        ("Students", read_df("""SELECT student_id AS "Student ID", name AS "Name",
                                      class_name AS "Class", gender AS "Gender",
                                      guardian_phone AS "Guardian Phone",
                                      registered_on AS "Registered On"
                               FROM students ORDER BY class_name, name""")),
        ("Scores", read_df("""SELECT sc.student_id AS "Student ID",
                                    s.name AS "Student Name",
                                    s.class_name AS "Class",
                                    sc.session AS "Session", sc.term AS "Term",
                                    sc.subject AS "Subject",
                                    sc.assessment AS "Assessment",
                                    sc.score AS "Score",
                                    sc.entered_by AS "Entered By",
                                    sc.updated_on AS "Updated On"
                             FROM scores sc
                             LEFT JOIN students s ON s.student_id=sc.student_id
                             ORDER BY s.class_name, s.name, sc.session,
                                      sc.term, sc.subject, sc.assessment""")),
        ("Teachers and Admins", read_df("""SELECT username AS "Username",
                                                  full_name AS "Full Name",
                                                  role AS "Role"
                                           FROM users
                                           WHERE role IN ('teacher', 'admin')
                                           ORDER BY role, full_name""")),
        ("Assignments", read_df("""SELECT u.full_name AS "Teacher",
                                          a.username AS "Username",
                                          a.class_name AS "Class",
                                          a.subject AS "Subject"
                                   FROM assignments a
                                   JOIN users u ON u.username=a.username
                                   WHERE u.role='teacher'
                                   ORDER BY u.full_name, a.class_name, a.subject""")),
    )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, dataframe in sheets:
            dataframe.to_excel(writer, index=False, sheet_name=sheet_name)
    return buffer.getvalue()


@st.cache_resource(show_spinner=False)
def init_db_once():
    init_db()
    return True


def generate_student_id():
    yy = datetime.now().strftime("%y")
    prefix = f"{ID_PREFIX}/{yy}/"
    seq = run("SELECT COUNT(*) AS n FROM students WHERE student_id LIKE ?",
              (prefix + "%",))[0]["n"] + 1
    while run("SELECT 1 FROM students WHERE student_id=?", (f"{prefix}{seq:04d}",)):
        seq += 1
    return f"{prefix}{seq:04d}"


def register_student(name, class_name, gender="", phone=""):
    sid = generate_student_id()
    run("INSERT INTO students VALUES (?,?,?,?,?,?)",
        (sid, name.strip().title(), class_name, gender, phone.strip(),
         datetime.now().strftime("%Y-%m-%d %H:%M")))
    return sid


def transfer_student(student_id, target_class):
    if target_class not in CLASSES:
        raise ValueError("Please select a valid destination class.")

    students = run("SELECT name, class_name FROM students WHERE student_id=?",
                   (student_id,))
    if not students:
        raise ValueError("The selected student could not be found.")

    student = students[0]
    source_class = student["class_name"]
    if source_class == target_class:
        raise ValueError("The student is already in that class.")

    classmates = read_df(
        "SELECT student_id, name FROM students WHERE class_name=?",
        (target_class,))
    duplicates = classmates[
        classmates["name"].map(normalized_student_name)
        == normalized_student_name(student["name"])
    ]
    if not duplicates.empty:
        ids = ", ".join(duplicates["student_id"])
        raise ValueError(
            f"A student with this name is already registered in "
            f"{target_class} (Student ID: {ids})."
        )

    run("UPDATE students SET class_name=? WHERE student_id=?",
        (target_class, student_id))
    return source_class


def normalized_student_name(name):
    return " ".join(str(name).split()).casefold()


def duplicate_student_records(students):
    keys = [
        (normalized_student_name(row["name"]),
         " ".join(str(row["class_name"]).split()).upper())
        for _, row in students.iterrows()
    ]
    return students.loc[pd.Series(keys, index=students.index).duplicated(keep=False)]


def student_upload_review(upload_rows, existing):
    existing_by_key = {}
    for _, student in existing.iterrows():
        key = (normalized_student_name(student["name"]),
               " ".join(str(student["class_name"]).split()).upper())
        existing_by_key.setdefault(key, []).append(student["student_id"])

    seen_in_upload = {}
    reasons = []
    duplicate_flags = []
    for index, row in upload_rows.iterrows():
        key = (normalized_student_name(row["name"]), row["class"])
        if key in existing_by_key:
            reasons.append("Already registered: " + ", ".join(existing_by_key[key]))
            duplicate_flags.append(True)
        elif key in seen_in_upload:
            reasons.append(f"Repeated in this file (row {seen_in_upload[key]})")
            duplicate_flags.append(True)
        else:
            reasons.append("")
            duplicate_flags.append(False)
        seen_in_upload.setdefault(key, index + 1)

    return pd.DataFrame({
        "Student name": upload_rows["name"],
        "Class": upload_rows["class"],
        "Possible duplicate": duplicate_flags,
        "Reason": reasons,
    })


# ----------------------------------------------------------------------
# UI HELPERS
# ----------------------------------------------------------------------
def show_badge(width=140):
    if BADGE_PATH.exists():
        st.image(str(BADGE_PATH), width=width)
    else:
        st.markdown("<div style='font-size:80px;text-align:center'>🏫</div>",
                    unsafe_allow_html=True)
        st.caption("Add your badge at assets/badge.png")


def apply_theme(login_mode=False, mode="Colour"):
    css = Path("assets/styles.css").read_text(encoding="utf-8")
    badge_image = "none"
    if BADGE_PATH.exists():
        badge_data = base64.b64encode(BADGE_PATH.read_bytes()).decode("ascii")
        badge_image = f'url("data:image/png;base64,{badge_data}")'

    palettes = {
        "Colour": {
            "color_scheme": "light",
            "page": "#e5eee8",
            "page_gradient": (
                "radial-gradient(ellipse at 8% 12%, rgba(74,171,143,.28), transparent 34%), "
                "radial-gradient(ellipse at 92% 8%, rgba(244,190,103,.30), transparent 31%), "
                "radial-gradient(ellipse at 88% 88%, rgba(109,159,190,.20), transparent 36%), "
                "linear-gradient(135deg, #e5f0e9 0%, #f2eadb 48%, #e2edef 100%)"
            ),
            "text": "#183b36", "heading": "#174b3e", "muted": "#526d64",
            "sidebar_text": "#21483e", "sidebar_panel": "rgba(235,245,239,.90)",
            "sidebar_border": "rgba(31,91,76,.16)",
            "button_start": "#137c68", "button_end": "#d59635",
            "button_hover_start": "#0e6655", "button_hover_end": "#bd7920",
            "input": "#f0f6f2", "input_border": "#c6d8d1",
            "form": "linear-gradient(140deg, rgba(232,244,237,.96), rgba(250,239,218,.94))",
            "form_border": "rgba(31,91,76,.14)",
            "alert": "rgba(235,245,239,.92)", "table": "rgba(233,243,237,.94)",
            "table_border": "#bdd7ca",
            "login_overlay": "rgba(241,247,250,.78)",
            "sidebar_overlay": "rgba(246,250,249,.74)",
            "login_gradient": "linear-gradient(135deg, #d8eee3, #f2dfb9 48%, #d7e9ed)",
            "sidebar_gradient": "linear-gradient(160deg, #d5eee2, #eee0c4 52%, #d9e8ed)",
        },
        "White": {
            "color_scheme": "light",
            "page": "#ffffff",
            "page_gradient": "linear-gradient(135deg, #ffffff 0%, #f8fbf9 58%, #f4f8fb 100%)",
            "text": "#263b36", "heading": "#174b3e", "muted": "#5b6d66",
            "sidebar_text": "#21483e", "sidebar_panel": "rgba(255,255,255,.94)",
            "sidebar_border": "rgba(31,91,76,.14)",
            "button_start": "#137c68", "button_end": "#d59635",
            "button_hover_start": "#0e6655", "button_hover_end": "#bd7920",
            "input": "#ffffff", "input_border": "#cbd9d2",
            "form": "linear-gradient(140deg, #ffffff, #f5faf7)",
            "form_border": "#d7e4dd",
            "alert": "#f4f8f5", "table": "#f7faf8",
            "table_border": "#d7e4dd",
            "login_overlay": "rgba(255,255,255,.86)",
            "sidebar_overlay": "rgba(255,255,255,.84)",
            "login_gradient": "linear-gradient(135deg, #ffffff, #f4f8f5 55%, #f5f8fa)",
            "sidebar_gradient": "linear-gradient(160deg, #f3f8f5, #fffaf0 52%, #f2f7f9)",
        },
        "Dark": {
            "color_scheme": "dark",
            "page": "#14221e",
            "page_gradient": (
                "radial-gradient(ellipse at 8% 12%, rgba(30,112,91,.34), transparent 35%), "
                "radial-gradient(ellipse at 92% 8%, rgba(153,104,39,.22), transparent 32%), "
                "radial-gradient(ellipse at 88% 88%, rgba(44,91,116,.30), transparent 36%), "
                "linear-gradient(135deg, #14241f 0%, #25251e 52%, #17262b 100%)"
            ),
            "text": "#e7f1eb", "heading": "#c4ead9", "muted": "#b2c6bc",
            "sidebar_text": "#e2efe8", "sidebar_panel": "rgba(25,47,40,.88)",
            "sidebar_border": "rgba(169,213,190,.2)",
            "button_start": "#147b66", "button_end": "#b77b2d",
            "button_hover_start": "#189477", "button_hover_end": "#d09540",
            "input": "#20372f", "input_border": "#4a6b5d",
            "form": "linear-gradient(140deg, rgba(31,56,47,.97), rgba(59,49,32,.96))",
            "form_border": "rgba(169,213,190,.2)",
            "alert": "rgba(34,59,49,.94)", "table": "rgba(29,51,44,.96)",
            "table_border": "#456757",
            "login_overlay": "rgba(17,32,27,.80)",
            "sidebar_overlay": "rgba(17,32,27,.79)",
            "login_gradient": "linear-gradient(135deg, #132a23, #30291d 50%, #182c33)",
            "sidebar_gradient": "linear-gradient(160deg, #18372c, #332b1d 52%, #1b3036)",
        },
    }
    palette = palettes.get(mode, palettes["Colour"])
    css_variables = {
        "color-scheme": palette["color_scheme"],
        "page": palette["page"],
        "page-gradient": palette["page_gradient"],
        "text": palette["text"],
        "heading": palette["heading"],
        "muted": palette["muted"],
        "sidebar-text": palette["sidebar_text"],
        "sidebar-panel": palette["sidebar_panel"],
        "sidebar-border": palette["sidebar_border"],
        "button-start": palette["button_start"],
        "button-end": palette["button_end"],
        "button-hover-start": palette["button_hover_start"],
        "button-hover-end": palette["button_hover_end"],
        "input": palette["input"],
        "input-border": palette["input_border"],
        "form": palette["form"],
        "form-border": palette["form_border"],
        "alert": palette["alert"],
        "table": palette["table"],
        "table-border": palette["table_border"],
    }
    variable_css = ";".join(
        f"--school-{name}: {value}" for name, value in css_variables.items()
    )
    login_watermark = "none"
    if login_mode and badge_image != "none":
        login_watermark = (
            f"linear-gradient({palette['login_overlay']}, "
            f"{palette['login_overlay']}), {badge_image}, "
            f"{palette['login_gradient']}"
        )
    sidebar_watermark = (
        f"linear-gradient({palette['sidebar_overlay']}, "
        f"{palette['sidebar_overlay']}), {badge_image}, "
        f"{palette['sidebar_gradient']}"
    )
    st.markdown(
        f"<style>{css}\n:root {{{variable_css};"
        f"--school-login-watermark: {login_watermark};"
        f"--school-sidebar-watermark: {sidebar_watermark};"
        "}</style>",
        unsafe_allow_html=True,
    )


def to_excel(df, sheet="Sheet1"):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet)
    return buf.getvalue()


# ----------------------------------------------------------------------
# LOGIN
# ----------------------------------------------------------------------
def login_page():
    st.radio("Appearance", ["Colour", "White", "Dark"], horizontal=True,
             key="appearance", label_visibility="collapsed")
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        c1, c2, c3 = st.columns([1, 2, 1])
        with c2:
            show_badge(250)
        st.markdown(f"<h1 style='text-align:center; padding-top:0; margin-top:-30px'>{SCHOOL_NAME}</h1>"
                    "<h4 style='text-align:center'>Score Management System</h4>",
                    unsafe_allow_html=True)
        with st.form("login"):
            username = st.text_input("Username").strip().lower()
            password = st.text_input("Password", type="password")
            ok = st.form_submit_button("Log in")
        if ok:
            rows = run("SELECT * FROM users WHERE username=?", (username,))
            if rows and check_pw(password, rows[0]["password_hash"]):
                st.session_state.user = {
                    "username": rows[0]["username"],
                    "full_name": rows[0]["full_name"],
                    "role": rows[0]["role"],
                }
                st.rerun()
            else:
                st.error("Incorrect username or password.")


# ----------------------------------------------------------------------
# TEACHER PAGES
# ----------------------------------------------------------------------
def teacher_enter_scores(user):
    st.header("Enter Scores")
    assigns = run("SELECT class_name, subject FROM assignments WHERE username=? "
                  "ORDER BY class_name, subject", (user["username"],))
    if not assigns:
        st.info("No class/subject has been assigned to you yet. "
                "Please contact the administrator.")
        return

    c1, c2 = st.columns(2)
    session = c1.selectbox("Academic Session", SESSIONS)
    term = c2.selectbox("Term", TERMS)
    c3, c4 = st.columns(2)
    labels = [f"{a['class_name']} - {a['subject']}" for a in assigns]
    choice = c3.selectbox("Class and Subject", labels)
    assessment = c4.selectbox("Assessment", list(ASSESSMENTS))
    class_name, subject = choice.split(" - ", 1)
    max_score = ASSESSMENTS[assessment]

    students = read_df("SELECT student_id, name FROM students WHERE class_name=? "
                       "ORDER BY name", (class_name,))
    if students.empty:
        st.warning(f"No students are registered in {class_name} yet.")
        return

    existing = read_df(
        "SELECT student_id, score FROM scores WHERE session=? AND term=? "
        "AND subject=? AND assessment=?", (session, term, subject, assessment))
    sheet = students.merge(existing, on="student_id", how="left")
    sheet.columns = ["Student ID", "Student Name", "Score"]
    sheet["Score"] = pd.to_numeric(sheet["Score"], errors="coerce")

    st.caption(f"{class_name} | {subject} | {assessment} | "
               f"Maximum score: **{max_score}** | Leave blank if not yet scored "
               f"(use 0 for absent).")
    edited = st.data_editor(
        sheet,
        hide_index=True,
        disabled=["Student ID", "Student Name"],
        column_config={"Score": st.column_config.NumberColumn(
            f"Score (max {max_score})", min_value=0.0,
            max_value=float(max_score), step=0.5)},
        key=f"editor_{session}_{term}_{choice}_{assessment}",
    )

    if st.button("Save Scores", type="primary"):
        scored = edited.dropna(subset=["Score"])
        if (scored["Score"] > max_score).any() or (scored["Score"] < 0).any():
            st.error(f"Scores must be between 0 and {max_score}.")
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        batch = [(r["Student ID"], session, term, subject, assessment,
                  float(r["Score"]), user["username"], now)
                 for _, r in scored.iterrows()]
        run_many("""INSERT INTO scores VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(student_id, session, term, subject, assessment)
                    DO UPDATE SET score=excluded.score,
                                  entered_by=excluded.entered_by,
                                  updated_on=excluded.updated_on""", batch)
        st.success(f"Saved {len(scored)} score(s) for {subject}, {class_name}.")


def change_password_page(user):
    st.header("Change Password")
    with st.form("pw"):
        old = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password")
        new2 = st.text_input("Confirm new password", type="password")
        ok = st.form_submit_button("Update password")
    if ok:
        row = run("SELECT password_hash FROM users WHERE username=?",
                  (user["username"],))[0]
        if not check_pw(old, row["password_hash"]):
            st.error("Current password is incorrect.")
        elif len(new) < 6:
            st.error("New password must be at least 6 characters.")
        elif new != new2:
            st.error("Passwords do not match.")
        else:
            run("UPDATE users SET password_hash=? WHERE username=?",
                (hash_pw(new), user["username"]))
            st.success("Password updated.")


# ----------------------------------------------------------------------
# ADMIN PAGES
# ----------------------------------------------------------------------
def admin_register_student():
    st.header("Register Student")
    st.caption("A unique Student ID is generated automatically.")
    with st.form("reg", clear_on_submit=True):
        name = st.text_input("Full name")
        c1, c2 = st.columns(2)
        class_name = c1.selectbox("Class", CLASSES)
        gender = c2.selectbox("Gender", ["Female", "Male"])
        phone = st.text_input("Guardian phone (optional)")
        ok = st.form_submit_button("Register")
    if ok:
        if not name.strip():
            st.error("Please enter the student's name.")
        else:
            same_class = read_df(
                "SELECT student_id, name FROM students WHERE class_name=?",
                (class_name,))
            matches = same_class[
                same_class["name"].map(normalized_student_name)
                == normalized_student_name(name)
            ]
            if not matches.empty:
                ids = ", ".join(matches["student_id"])
                st.error(f"A student with this name is already registered in "
                         f"{class_name} (Student ID: {ids}).")
            else:
                sid = register_student(name, class_name, gender, phone)
                st.success(f"Registered **{name.strip().title()}** in {class_name}.")
                st.info(f"Student ID: **{sid}**")

    st.divider()
    st.subheader("Bulk registration (CSV or Excel)")
    st.caption("File needs columns: **name**, **class** (e.g. SS1A). "
               "Optional: gender, phone.")
    up = st.file_uploader("Upload file", type=["csv", "xlsx"])
    if up is not None:
        df = pd.read_csv(up) if up.name.endswith(".csv") else pd.read_excel(up)
        df.columns = [c.strip().lower() for c in df.columns]
        st.dataframe(df)
        if not {"name", "class"}.issubset(df.columns):
            st.error("The file must have 'name' and 'class' columns.")
            return
        bad = df[~df["class"].astype(str).str.strip().str.upper().isin(CLASSES)]
        if not bad.empty:
            st.error("Some rows have an invalid class: "
                     + ", ".join(bad["class"].astype(str).unique()))
            return

        upload_rows = df.dropna(subset=["name"]).copy()
        upload_rows["name"] = upload_rows["name"].map(
            lambda name: " ".join(str(name).split()))
        upload_rows = upload_rows[upload_rows["name"] != ""].reset_index(drop=True)
        if upload_rows.empty:
            st.info("No nonblank student names were found in this file.")
            return
        upload_rows["class"] = upload_rows["class"].astype(str).str.strip().str.upper()

        existing = read_df("SELECT student_id, name, class_name FROM students")
        review = student_upload_review(upload_rows, existing)
        duplicate_flags = review["Possible duplicate"].tolist()
        if any(duplicate_flags):
            st.warning("Possible duplicates will be skipped automatically. "
                       "Review the matches below before continuing.")
            st.dataframe(
                review.loc[review["Possible duplicate"]],
                hide_index=True,
            )
        else:
            st.success("No duplicate name-and-class matches found.")

        if st.button("Register new students"):
            created = []
            for index, row in upload_rows.iterrows():
                if duplicate_flags[index]:
                    continue
                sid = register_student(
                    str(row["name"]), row["class"],
                    str(row.get("gender", "") or ""), str(row.get("phone", "") or ""))
                created.append({"Student ID": sid, "Name": row["name"],
                                "Class": row["class"]})
            out = pd.DataFrame(created)
            skipped = len(upload_rows) - len(out)
            st.success(f"Registered {len(out)} student(s); skipped {skipped}.")
            if not out.empty:
                st.dataframe(out)
                st.download_button("Download ID list (Excel)", to_excel(out),
                                   "new_student_ids.xlsx")


def admin_manage_students():
    st.header("Manage Students")
    cls = st.selectbox("Class", ["All"] + CLASSES)
    sql = "SELECT student_id, name, class_name, gender, guardian_phone FROM students"
    df = read_df(sql + (" WHERE class_name=?" if cls != "All" else "")
                 + " ORDER BY class_name, name", (cls,) if cls != "All" else ())
    st.write(f"{len(df)} student(s)")
    st.dataframe(df, hide_index=True)
    if df.empty:
        return
    duplicates = duplicate_student_records(df)
    if not duplicates.empty:
        st.warning(f"Found {len(duplicates)} records with a matching name and class.")
        st.dataframe(
            duplicates[["student_id", "name", "class_name"]],
            hide_index=True,
        )
        st.caption("To remove an extra record, select its Student ID below and "
                   "confirm deletion. Its scores will also be deleted.")
    st.download_button("Download list (Excel)", to_excel(df), "students.xlsx")

    st.subheader("Edit a student")
    pick = st.selectbox("Student", df["student_id"] + " - " + df["name"])
    sid = pick.split(" - ")[0]
    row = df[df["student_id"] == sid].iloc[0]
    c1, c2 = st.columns(2)
    new_name = c1.text_input("Name", row["name"])
    new_class = c2.selectbox("Class", CLASSES, index=CLASSES.index(row["class_name"]))
    b1, b2 = st.columns(2)
    if b1.button("Save changes"):
        run("UPDATE students SET name=?, class_name=? WHERE student_id=?",
            (new_name.strip().title(), new_class, sid))
        st.success("Updated.")
        st.rerun()
    confirm = b2.checkbox("I confirm deletion (this also deletes the student's scores)")
    if confirm and b2.button("Delete student"):
        run("DELETE FROM scores WHERE student_id=?", (sid,))
        run("DELETE FROM students WHERE student_id=?", (sid,))
        st.success("Deleted.")
        st.rerun()


def admin_transfer_student():
    st.header("Transfer Student")
    st.caption("Move a student to another class without creating a new record.")

    if "transfer_success" in st.session_state:
        st.success(st.session_state.pop("transfer_success"))

    source_class = st.selectbox("Current class", CLASSES, key="transfer_source")
    students = read_df(
        "SELECT student_id, name FROM students WHERE class_name=? ORDER BY name",
        (source_class,))
    if students.empty:
        st.info(f"No students are registered in {source_class}.")
        return

    student_by_id = students.set_index("student_id")["name"].to_dict()
    student_id = st.selectbox(
        "Student",
        list(student_by_id),
        format_func=lambda sid: f"{sid} - {student_by_id[sid]}",
        key="transfer_student",
    )
    target_classes = [cls for cls in CLASSES if cls != source_class]
    target_class = st.selectbox(
        "Transfer to class", target_classes, key="transfer_destination")
    st.info("The student's ID and saved scores will remain unchanged.")

    confirmed = st.checkbox(
        "I confirm that I want to transfer this student",
        key="confirm_student_transfer",
    )
    if st.button("Transfer student", type="primary"):
        if not confirmed:
            st.error("Please confirm the transfer before continuing.")
            return
        try:
            previous_class = transfer_student(student_id, target_class)
        except ValueError as exc:
            st.error(str(exc))
            return

        st.session_state["transfer_success"] = (
            f"Transferred {student_by_id[student_id]} "
            f"({student_id}) from {previous_class} to {target_class}. "
            "The student record and scores were retained."
        )
        st.rerun()


def admin_teachers():
    st.header("Teachers and Assignments")
    tab1, tab2 = st.tabs(["Accounts (teachers and admins)", "Assign class and subject"])

    with tab1:
        st.subheader("Create an account")
        with st.form("newaccount", clear_on_submit=True):
            kind = st.selectbox("Account type", ["Teacher", "Admin"])
            full = st.text_input("Full name")
            uname = st.text_input("Username").strip().lower()
            pw = st.text_input("Password", type="password")
            ok = st.form_submit_button("Create account")
        if ok:
            if not (full.strip() and uname and len(pw) >= 6):
                st.error("Fill every field (password at least 6 characters).")
            elif run("SELECT 1 FROM users WHERE username=?", (uname,)):
                st.error("That username already exists.")
            else:
                run("INSERT INTO users VALUES (?,?,?,?)",
                    (uname, full.strip(), kind.lower(), hash_pw(pw)))
                st.success(f"{kind} account created for {full.strip()}.")

        accounts = run("SELECT username, full_name, role FROM users "
                       "ORDER BY role, full_name")
        st.subheader("All accounts")
        st.dataframe(pd.DataFrame([dict(a) for a in accounts]).rename(
            columns={"username": "Username", "full_name": "Full name", "role": "Type"}),
            hide_index=True)

        def label(u):
            a = next(x for x in accounts if x["username"] == u)
            return f"{a['full_name']} ({u}) - {a['role']}"

        st.subheader("Reset a password")
        who = st.selectbox("Account", [a["username"] for a in accounts],
                           format_func=label, key="reset_who")
        newpw = st.text_input("New password", type="password", key="reset_pw")
        if st.button("Reset password"):
            if len(newpw) < 6:
                st.error("At least 6 characters.")
            else:
                run("UPDATE users SET password_hash=? WHERE username=?",
                    (hash_pw(newpw), who))
                st.success("Password reset.")

        st.subheader("Delete an account")
        me = st.session_state.user["username"]
        others = [a["username"] for a in accounts if a["username"] != me]
        if others:
            gone = st.selectbox("Account to delete", others, format_func=label,
                                key="delete_who")
            sure = st.checkbox("I am sure I want to delete this account")
            if sure and st.button("Delete account"):
                run("DELETE FROM assignments WHERE username=?", (gone,))
                run("DELETE FROM users WHERE username=?", (gone,))
                st.success("Account deleted.")
                st.rerun()
        else:
            st.caption("There are no other accounts to delete. "
                       "You cannot delete the account you are using.")

    with tab2:
        teachers = run("SELECT username, full_name FROM users WHERE role='teacher' "
                       "ORDER BY full_name")
        if not teachers:
            st.info("Create a teacher account first.")
            return
        who = st.selectbox("Teacher", [t["username"] for t in teachers],
                           format_func=lambda u: next(
                               t["full_name"] + f" ({u})" for t in teachers
                               if t["username"] == u))
        classes = st.multiselect("Class(es)", CLASSES)
        # offer subjects common to all selected classes
        if classes:
            options = [s for s in subjects_for(classes[0])
                       if all(s in subjects_for(c) for c in classes)]
        else:
            options = []
        subs = st.multiselect("Subject(s)", options)
        if st.button("Assign"):
            for c in classes:
                for s in subs:
                    run("INSERT INTO assignments VALUES (?,?,?) ON CONFLICT DO NOTHING",
                        (who, c, s))
            st.success("Assignments saved.")

        st.subheader("All assignments")
        adf = read_df("""SELECT u.full_name AS "Teacher", a.username, a.class_name AS "Class",
                                a.subject AS "Subject"
                         FROM assignments a JOIN users u ON u.username=a.username
                         ORDER BY u.full_name, a.class_name""")
        st.dataframe(adf, hide_index=True)
        if not adf.empty:
            label = adf["username"] + " | " + adf["Class"] + " | " + adf["Subject"]
            rm = st.selectbox("Remove an assignment", label)
            if st.button("Remove"):
                u, c, s = [p.strip() for p in rm.split("|")]
                run("DELETE FROM assignments WHERE username=? AND class_name=? "
                    "AND subject=?", (u, c, s))
                st.rerun()


def admin_results():
    st.header("Results and Reports")
    c1, c2, c3 = st.columns(3)
    session = c1.selectbox("Session", SESSIONS)
    term = c2.selectbox("Term", TERMS)
    cls = c3.selectbox("Class", CLASSES)

    students = read_df('SELECT student_id AS "Student ID", name AS "Name" '
                       'FROM students WHERE class_name=? ORDER BY name', (cls,))
    if students.empty:
        st.info(f"No students in {cls}.")
        return
    subs = subjects_for(cls)

    tab1, tab2, tab3 = st.tabs(["Class result sheet", "Score entry status",
                                "Detailed scores"])

    with tab1:
        sc = read_df("""SELECT sc.student_id AS "Student ID", sc.subject, SUM(sc.score) AS total
                        FROM scores sc JOIN students s ON s.student_id=sc.student_id
                        WHERE s.class_name=? AND sc.session=? AND sc.term=?
                        GROUP BY sc.student_id, sc.subject""", (cls, session, term))
        sheet = students.set_index("Student ID")
        if not sc.empty:
            wide = sc.pivot(index="Student ID", columns="subject", values="total")
            wide = wide[[s for s in subs if s in wide.columns]]
            sheet = sheet.join(wide)
            sheet["Total"] = wide.sum(axis=1, min_count=1)
            sheet["Average"] = wide.mean(axis=1).round(1)
            sheet["Position"] = sheet["Total"].rank(
                ascending=False, method="min").astype("Int64")
            sheet = sheet.sort_values("Position")
        sheet = sheet.reset_index()
        st.dataframe(sheet, hide_index=True)
        st.caption("Subject columns show CA + Exam totals entered so far.")
        st.download_button("Download result sheet (Excel)", to_excel(sheet),
                           f"{cls}_{term}_{session.replace('/', '-')}_results.xlsx")

    with tab2:
        n = len(students)
        cnt = read_df("""SELECT sc.subject, sc.assessment, COUNT(*) AS n
                         FROM scores sc JOIN students s ON s.student_id=sc.student_id
                         WHERE s.class_name=? AND sc.session=? AND sc.term=?
                         GROUP BY sc.subject, sc.assessment""", (cls, session, term))
        joiner = "STRING_AGG" if USE_PG else "GROUP_CONCAT"
        teach = read_df(f"""SELECT a.subject, {joiner}(u.full_name, ', ') AS teacher
                           FROM assignments a JOIN users u ON u.username=a.username
                           WHERE a.class_name=? GROUP BY a.subject""", (cls,))
        tmap = dict(zip(teach["subject"], teach["teacher"]))
        rows = []
        for s in subs:
            row = {"Subject": s, "Teacher": tmap.get(s, "Not assigned")}
            for a in ASSESSMENTS:
                m = cnt[(cnt["subject"] == s) & (cnt["assessment"] == a)]
                row[a] = f"{int(m['n'].iloc[0]) if not m.empty else 0}/{n}"
            rows.append(row)
        st.dataframe(pd.DataFrame(rows), hide_index=True)
        st.caption("Number of students scored out of the class total.")

    with tab3:
        subject = st.selectbox("Subject", subs)
        det = read_df("""SELECT s.student_id AS "Student ID", s.name AS "Name",
                                sc.assessment, sc.score, sc.entered_by, sc.updated_on
                         FROM students s JOIN scores sc ON sc.student_id=s.student_id
                         WHERE s.class_name=? AND sc.session=? AND sc.term=?
                           AND sc.subject=?""", (cls, session, term, subject))
        if det.empty:
            st.info("No scores entered yet for this subject.")
        else:
            piv = det.pivot(index=["Student ID", "Name"], columns="assessment",
                            values="score").reset_index()
            cols = [a for a in ASSESSMENTS if a in piv.columns]
            piv["Total"] = piv[cols].sum(axis=1)
            st.dataframe(piv[["Student ID", "Name"] + cols + ["Total"]],
                         hide_index=True)



# ----------------------------------------------------------------------
# REPORT SHEETS
# ----------------------------------------------------------------------
def grade_of(total):
    pct = total / sum(ASSESSMENTS.values()) * 100
    for low, g, remark in GRADES:
        if pct >= low:
            return g, remark
    return "F9", "Fail"


def fmt(x):
    return "-" if x is None or pd.isna(x) else f"{x:g}"


def load_class_scores(session, term, cls):
    students = read_df("SELECT student_id, name, gender FROM students "
                       "WHERE class_name=? ORDER BY name", (cls,))
    sc = read_df("""SELECT sc.student_id, sc.subject, sc.assessment, sc.score
                    FROM scores sc JOIN students s ON s.student_id=sc.student_id
                    WHERE s.class_name=? AND sc.session=? AND sc.term=?""",
                 (cls, session, term))
    return students, sc


def ordinal(n):
    n = int(n)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def build_report_pdf(session, term, cls, only_student_id=None):
    students, sc = load_class_scores(session, term, cls)
    subs = subjects_for(cls)
    ca_keys = [a for a in ASSESSMENTS if a != EXAM_KEY]
    totals = (sc.groupby("student_id")["score"].sum()
              if not sc.empty else pd.Series(dtype=float))
    positions = totals.rank(ascending=False, method="min").astype(int)
    class_size = len(students)
    if only_student_id:
        students = students[students["student_id"] == only_student_id]

    def P(text, size=10, bold=False, align=1, color=colors.black):
        return Paragraph(text, ParagraphStyle(
            "x", fontName="Times-Bold" if bold else "Times-Roman", fontSize=size,
            leading=size * 1.25, alignment=align, textColor=color))

    def logo(path, max_w, max_h):
        """Shrink the picture to fit the box, keeping its shape."""
        if Path(path).exists():
            iw, ih = ImageReader(str(path)).getSize()
            scale = min(max_w / iw, max_h / ih)
            return Image(str(path), width=iw * scale, height=ih * scale)
        return ""

    def fields(cells, widths, value_cols, size=12):
        """A row of labels and values; values get an underline."""
        t = Table([cells], colWidths=[w * cm * 0.978 for w in widths])
        t.hAlign = "LEFT"
        style = [("FONTSIZE", (0, 0), (-1, -1), size),
                 ("FONTNAME", (0, 0), (-1, -1), "Times-Bold"),
                 ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                 ("TOPPADDING", (0, 0), (-1, -1), 10),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
        for c in value_cols:
            style += [("FONTNAME", (c, 0), (c, 0), "Times-Roman"),
                      ("LINEBELOW", (c, 0), (c, 0), 0.7, colors.black)]
        t.setStyle(TableStyle(style))
        return t

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.3 * cm, bottomMargin=1.2 * cm,
                            title=f"{cls} Report Sheets")
    story = []
    for n, (_, stu) in enumerate(students.iterrows()):
        sid = stu["student_id"]
        mine = sc[sc["student_id"] == sid]

        # ---------- header ----------
        story += [P(BOARD_NAME, 13.5, True), Spacer(1, 8)]
        banner = Table([[P("STUDENTS REPORT SHEET", 13.5, True, color=colors.white)]],
                       colWidths=[8.2 * cm])
        banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.black),
                                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        banner.hAlign = "CENTER"
        centre = [P(FULL_SCHOOL_NAME, 10, True), P(SCHOOL_TOWN, 13, True),
                  Spacer(1, 6), P(ZONE_NAME, 11, True), Spacer(1, 8), banner]
        head = Table([[logo(BADGE_PATH, 4.0 * cm, 5.0 * cm), centre,
                      logo(BOARD_LOGO_PATH, 4.0 * cm, 4.0 * cm)]],
                     colWidths=[4.0 * cm, 9.6 * cm, 4.0 * cm])
        head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                  ("ALIGN", (0, 0), (0, 0), "LEFT"),
                                  ("ALIGN", (2, 0), (2, 0), "RIGHT")]))
        story += [head, Spacer(1, 10)]

        # ---------- student details ----------
        pos = ordinal(positions[sid]) if sid in positions.index else "-"
        story += [
            fields(["NAME:", stu["name"].upper(), "CLASS:", cls],
                   [2.0, 10.0, 1.9, 4.1], [1, 3]),
            fields(["ADMISSION NO:", sid, "NO. IN CLASS:", str(class_size),
                    "POSITION:", pos], [3.6, 4.4, 3.4, 1.8, 2.7, 2.1], [1, 3, 5]),
            fields(["TERM:", f"{term.upper()}  -  {session} SESSION"],
                   [1.7, 9.0], [1]),
            Spacer(1, 14),
        ]

        # ---------- scores table ----------
        rows = [["S/N", "SUBJECT", "C/A", "EXAM", "TOTAL", "GRADE", "REMARK"]]
        grand, counted = 0.0, 0
        for i, subj in enumerate(subs, 1):
            m = mine[mine["subject"] == subj]
            vals = {r["assessment"]: r["score"] for _, r in m.iterrows()}
            ca_vals = [vals[a] for a in ca_keys if a in vals]
            ca = sum(ca_vals) if ca_vals else None
            exam = vals.get(EXAM_KEY)
            if vals:
                tot = sum(vals.values())
                g, remark = grade_of(tot)
                grand += tot
                counted += 1
            else:
                tot, g, remark = None, "", ""
            rows.append([f"{i}.", REPORT_NAMES.get(subj, subj).upper(),
                         fmt(ca) if ca is not None else "", fmt(exam) if exam is not None else "",
                         fmt(tot) if tot is not None else "", g, remark])
        widths = [1.3, 7.0, 1.7, 1.7, 1.7, 1.7, 2.9]
        t = Table(rows, colWidths=[w * cm * 0.978 for w in widths],
                  rowHeights=[0.9 * cm] + [1.02 * cm] * len(subs))
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Times-Bold"),
            ("FONTNAME", (0, 1), (-1, -1), "Times-Roman"),
            ("FONTSIZE", (0, 0), (-1, -1), 12),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("ALIGN", (1, 1), (1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.8, colors.black),
            ("BOX", (0, 0), (-1, -1), 1.6, colors.black)]))
        story += [t, Spacer(1, 22)]

        # ---------- totals, comments, signatures ----------
        avg = grand / counted if counted else None
        story += [
            fields(["TOTAL MARKS:", fmt(grand) if counted else "",
                    "AVERAGE MARKS:", f"{avg:.1f}" if avg is not None else ""],
                   [3.4, 4.8, 4.4, 5.4], [1, 3]),
            fields(["FORM MASTER'S COMMENT:", ""], [6.6, 11.4], [1]),
            fields(["SIGN:", "", "DATE:", ""], [1.4, 8.0, 1.5, 7.1], [1, 3]),
            fields(["PRINCIPAL'S COMMENT:", ""], [6.6, 11.4], [1]),
            fields(["SIGN:", "", "DATE:", ""], [1.4, 8.0, 1.5, 7.1], [1, 3]),
            Spacer(1, 22),
        ]
        key = "  |  ".join(f"{g}: {low}%+" for low, g, _ in GRADES)
        story.append(P(f"<b>Grading key:</b> {key}", 9, False, align=0))

        if n < len(students) - 1:
            story.append(PageBreak())

    if not story:
        story = [Paragraph("No students found.", getSampleStyleSheet()["Normal"])]
    doc.build(story)
    return buf.getvalue()


def load_count(cls):
    return run("SELECT COUNT(*) AS n FROM students WHERE class_name=?", (cls,))[0]["n"]


def admin_report_sheets():
    st.header("Student Report Sheets")
    c1, c2, c3 = st.columns(3)
    session = c1.selectbox("Session", SESSIONS)
    term = c2.selectbox("Term", TERMS)
    cls = c3.selectbox("Class", CLASSES)

    students, sc = load_class_scores(session, term, cls)
    if students.empty:
        st.info(f"No students in {cls}.")
        return

    subs = subjects_for(cls)
    expected = len(students) * len(subs) * len(ASSESSMENTS)
    entered = len(sc)
    st.progress(min(entered / expected, 1.0) if expected else 0.0)
    st.write(f"Scores entered: **{entered} of {expected}**")
    if entered < expected:
        st.warning("Some scores are not entered yet. Missing scores will show as "
                   "'-' on the report sheet. Check the **Score entry status** tab "
                   "in Results & Reports to see which teachers are still pending.")
    else:
        st.success("All scores are entered. The report sheets are ready.")

    mode = st.radio("Which report sheets?",
                    ["Whole class (one PDF)", "One student"], horizontal=True)
    only = None
    if mode == "One student":
        pick = st.selectbox("Student", students["student_id"] + " - " + students["name"])
        only = pick.split(" - ")[0]

    if st.button("Create report sheets", type="primary"):
        st.session_state.report = {
            "pdf": build_report_pdf(session, term, cls, only),
            "name": (f"{only.replace('/', '-')}_report.pdf" if only
                     else f"{cls}_{term.replace(' ', '')}_{session.replace('/', '-')}_reports.pdf"),
        }
    rep = st.session_state.get("report")
    if rep:
        st.success("Report sheets are ready. Click the button to download.")
        st.download_button("Download PDF", rep["pdf"], rep["name"],
                           mime="application/pdf")


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
def main():
    st.set_page_config(page_title=f"{SCHOOL_NAME} Score System",
                       page_icon="📚", layout="wide")
    init_db_once()

    st.session_state.setdefault("appearance", "Colour")
    apply_theme(
        login_mode="user" not in st.session_state,
        mode=st.session_state.appearance,
    )

    if "user" not in st.session_state:
        login_page()
        return

    user = st.session_state.user
    with st.sidebar:
        show_badge(90)
        st.markdown(f"**{SCHOOL_NAME}**")
        st.write(f"👤 {user['full_name']} ({user['role']})")
        st.radio("Appearance", ["Colour", "White", "Dark"], key="appearance")
        if user["role"] == "admin":
            page = st.radio("Menu", ["Register Student", "Manage Students",
                                     "Transfer Student",
                                     "Teachers & Assignments",
                                     "Results & Reports", "Report Sheets",
                                     "Change Password"])
            st.download_button(
                "Download full backup",
                data=create_full_backup,
                file_name=f"ggss_scores_backup_{datetime.now():%Y%m%d-%H%M%S}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                help="Download students, scores, accounts, and teacher assignments.",
            )
        else:
            page = st.radio("Menu", ["Enter Scores", "Change Password"])
        if st.button("Log out"):
            del st.session_state.user
            st.rerun()

    if page == "Register Student":
        admin_register_student()
    elif page == "Manage Students":
        admin_manage_students()
    elif page == "Transfer Student":
        admin_transfer_student()
    elif page == "Teachers & Assignments":
        admin_teachers()
    elif page == "Results & Reports":
        admin_results()
    elif page == "Report Sheets":
        admin_report_sheets()
    elif page == "Enter Scores":
        teacher_enter_scores(user)
    else:
        change_password_page(user)


main()