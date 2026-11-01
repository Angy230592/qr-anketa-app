from __future__ import annotations

import os
import socket
import subprocess
from datetime import datetime
from functools import wraps
from io import BytesIO
from pathlib import Path

import qrcode
from flask import (
    Flask,
    abort,
    flash,
    has_request_context,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from openpyxl import Workbook
from waitress import serve
from werkzeug.utils import secure_filename

import db
from phone import format_phone, national_part, normalize_phone, send_sms
from questions_io import parse_txt, parse_upload, questions_to_xlsx
from tunnel import public_url, start_tunnel

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "samples" / "questions.txt"
PORT = int(os.environ.get("PORT", "5050"))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")


def _load_secret() -> str:
    path = ROOT / "data" / "secret.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = os.urandom(24).hex()
    path.write_text(secret, encoding="utf-8")
    return secret


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY") or _load_secret()


def lan_ip() -> str:
    forced = os.environ.get("LAN_IP", "").strip()
    if forced:
        return forced
    skip = ("127.", "169.254.", "172.19.", "192.168.204.", "192.168.244.")
    found: list[str] = []
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["ipconfig"],
                text=True,
                encoding="cp866",
                errors="replace",
            )
        except (OSError, LookupError, subprocess.CalledProcessError):
            out = ""
        for line in out.splitlines():
            if "IPv4" not in line:
                continue
            ip = line.split(":")[-1].strip()
            if ip and not ip.startswith(skip):
                found.append(ip)
        for ip in found:
            if ip.startswith("192.168."):
                return ip
        if found:
            return found[0]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _request_public_base() -> str:
    if not has_request_context():
        return ""
    host = (request.host or "").strip()
    if not host:
        return ""
    hostname = host.split(":")[0].lower()
    if hostname in {"127.0.0.1", "localhost", "0.0.0.0"}:
        return ""
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",")[0].strip()
    return f"{proto}://{host}".rstrip("/")


def hosted_public() -> bool:
    return bool(
        _request_public_base()
        or os.environ.get("PUBLIC_URL", "").strip()
        or os.environ.get("DISABLE_TUNNEL")
    )


def survey_base_url() -> str:
    live = _request_public_base()
    if live:
        return live
    hosted = os.environ.get("PUBLIC_URL", "").strip()
    if hosted:
        return hosted.rstrip("/")
    return public_url() or f"http://{lan_ip()}:{PORT}"


def survey_url() -> str:
    return survey_base_url().rstrip("/") + "/"


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


def respondent_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        phone = session.get("phone")
        if not phone:
            return redirect(url_for("phone_login"))
        if db.phone_submitted(phone):
            return redirect(url_for("already"))
        return fn(*args, **kwargs)

    return wrapper


def grade(question: dict, selected: list[str]) -> tuple[str, int | None]:
    text = " | ".join(selected)
    if not question["scored"]:
        return text, None
    if question["qtype"] == "open":
        expected = [c.strip().lower() for c in question["correct"] if c.strip()]
        if not expected:
            return text, None
        ok = selected[0].strip().lower() in expected if selected else False
        return text, 1 if ok else 0
    correct = set(question["correct"])
    got = set(selected)
    return text, 1 if got == correct else 0


@app.context_processor
def inject_now():
    return {"survey_link": survey_url()}


@app.after_request
def add_headers(response):
    if request.path.startswith("/static/"):
        response.cache_control.public = True
        response.cache_control.max_age = 604800
    return response


@app.get("/login")
def phone_login():
    if session.get("phone"):
        if db.phone_submitted(session["phone"]):
            return redirect(url_for("already"))
        return redirect(url_for("survey"))
    pending = session.get("pending_phone", "")
    return render_template("phone.html", phone_local=national_part(pending))


@app.post("/login")
def phone_login_post():
    raw = request.form.get("phone_local")
    if raw is None:
        raw = request.form.get("phone", "")
    try:
        phone = normalize_phone(raw)
    except ValueError as exc:
        flash(str(exc), "error")
        return render_template("phone.html", phone_local=national_part(raw))
    if db.phone_submitted(phone):
        session["phone"] = phone
        return redirect(url_for("already"))
    try:
        code, reused = db.create_otp(phone)
    except ValueError as exc:
        flash(str(exc), "error")
        return render_template("phone.html", phone_local=national_part(phone))
    session["pending_phone"] = phone
    session.pop("otp_preview", None)
    if reused:
        flash("Код уже отправлен. Подождите около минуты, чтобы запросить новый.", "ok")
        return redirect(url_for("otp"))
    api_id = db.get_setting("smsru_api_id")
    if api_id:
        ok, err = send_sms(api_id, phone, f"Код для анкеты: {code}")
        if not ok:
            flash(f"Не удалось отправить SMS: {err}", "error")
            return render_template("phone.html", phone_local=national_part(phone))
    else:
        session["otp_preview"] = code
    return redirect(url_for("otp"))


@app.get("/otp")
def otp():
    phone = session.get("pending_phone")
    if not phone:
        return redirect(url_for("phone_login"))
    return render_template(
        "otp.html",
        phone=phone,
        phone_label=format_phone(phone),
        preview_code=session.get("otp_preview"),
    )


@app.post("/otp")
def otp_post():
    phone = session.get("pending_phone")
    if not phone:
        return redirect(url_for("phone_login"))
    try:
        ok = db.verify_otp(phone, request.form.get("code", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("otp"))
    if not ok:
        flash("Неверный или просроченный код", "error")
        return redirect(url_for("otp"))
    session["phone"] = phone
    session.pop("pending_phone", None)
    session.pop("otp_preview", None)
    session["started_at"] = datetime.now().isoformat(timespec="seconds")
    return redirect(url_for("survey"))


@app.get("/")
def survey():
    phone = session.get("phone")
    if not phone:
        return redirect(url_for("phone_login"))
    if db.phone_submitted(phone):
        return redirect(url_for("already"))
    questions = db.list_questions()
    if not questions:
        abort(503, "Вопросы ещё не загружены")
    session.setdefault("started_at", datetime.now().isoformat(timespec="seconds"))
    return render_template("survey.html", questions=questions)


@app.post("/submit")
@respondent_required
def submit():
    questions = db.list_questions()
    if not questions:
        abort(400)
    answers = []
    correct_n = 0
    total_n = 0
    for q in questions:
        if q["qtype"] == "open":
            selected = [request.form.get(f"q-{q['id']}", "").strip()]
            if not selected[0]:
                flash("Заполните все открытые вопросы", "error")
                return redirect(url_for("survey"))
        else:
            selected = request.form.getlist(f"q-{q['id']}")
            if not selected:
                flash("Отметьте ответы во всех вопросах с вариантами", "error")
                return redirect(url_for("survey"))
            if q["qtype"] == "single" and len(selected) != 1:
                flash("В вопросе с одним ответом можно выбрать только один вариант", "error")
                return redirect(url_for("survey"))
        text, is_correct = grade(q, selected)
        if is_correct is not None:
            total_n += 1
            correct_n += is_correct
        answers.append(
            {
                "question_id": q["id"],
                "prompt": q["prompt"],
                "qtype": q["qtype"],
                "answer_text": text,
                "is_correct": is_correct,
            }
        )
    started_raw = session.get("started_at") or datetime.now().isoformat(timespec="seconds")
    started_at = datetime.fromisoformat(started_raw)
    submitted_at = datetime.now()
    duration = max(0, int((submitted_at - started_at).total_seconds()))
    percent = round(100 * correct_n / total_n, 1) if total_n else None
    try:
        db.save_submission(
            started_at,
            submitted_at,
            duration,
            percent,
            correct_n,
            total_n,
            request.headers.get("X-Forwarded-For", request.remote_addr),
            answers,
            session.get("phone"),
        )
    except Exception:
        flash("Эта анкета с вашего номера уже сохранена", "error")
        return redirect(url_for("already"))
    session.pop("started_at", None)
    session["last_duration"] = duration
    return redirect(url_for("thanks"))


@app.get("/thanks")
def thanks():
    if not session.get("phone"):
        return redirect(url_for("phone_login"))
    return render_template(
        "thanks.html",
        duration=session.pop("last_duration", None),
    )


@app.get("/already")
def already():
    phone = session.get("phone")
    if not phone:
        return redirect(url_for("phone_login"))
    if not db.phone_submitted(phone):
        return redirect(url_for("survey"))
    return render_template("already.html", phone_label=format_phone(phone))


@app.get("/qr")
def qr_page():
    return render_template(
        "qr.html",
        link=survey_url(),
        public=hosted_public(),
        lan=f"http://{lan_ip()}:{PORT}/",
    )


@app.get("/qr.png")
def qr_png():
    img = qrcode.make(survey_url(), box_size=10, border=2)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.get("/admin/login")
def admin_login():
    if session.get("admin"):
        return redirect(url_for("admin_home"))
    return render_template("admin_login.html")


@app.post("/admin/login")
def admin_login_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if db.verify_admin(username, password):
        session["admin"] = True
        return redirect(url_for("admin_home"))
    flash("Неверный логин или пароль", "error")
    return render_template("admin_login.html")


@app.post("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.get("/admin")
@admin_required
def admin_home():
    date_from = request.args.get("from") or ""
    date_to = request.args.get("to") or ""
    submissions = db.list_submissions(date_from or None, date_to or None)
    return render_template(
        "admin.html",
        questions=db.list_questions(),
        submissions=submissions,
        stats=db.stats(date_from or None, date_to or None),
        date_from=date_from,
        date_to=date_to,
        link=survey_url(),
        public=hosted_public(),
        smsru_api_id=db.get_setting("smsru_api_id"),
    )


@app.post("/admin/settings")
@admin_required
def admin_settings():
    db.set_setting("smsru_api_id", request.form.get("smsru_api_id", "").strip())
    flash("Настройки SMS сохранены", "ok")
    return redirect(url_for("admin_home"))


@app.post("/admin/upload")
@admin_required
def admin_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Выберите файл с вопросами", "error")
        return redirect(url_for("admin_home"))
    try:
        questions = parse_upload(secure_filename(file.filename), file.read())
        db.replace_questions(questions)
        flash(f"Загружено вопросов: {len(questions)}", "ok")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin_home"))


@app.get("/admin/questions.xlsx")
@admin_required
def download_questions():
    data = questions_to_xlsx(db.list_questions())
    return send_file(BytesIO(data), as_attachment=True, download_name="questions.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/admin/template.xlsx")
@admin_required
def download_template():
    data = questions_to_xlsx(
        [
            {"qtype": "open", "prompt": "Ваше имя", "options": [], "correct": [], "scored": False},
            {
                "qtype": "single",
                "prompt": "Пример вопроса с одним ответом",
                "options": ["A", "B", "C"],
                "correct": ["A"],
                "scored": True,
            },
            {
                "qtype": "multiple",
                "prompt": "Пример вопроса с несколькими ответами",
                "options": ["A", "B", "C"],
                "correct": ["A", "C"],
                "scored": True,
            },
        ]
    )
    return send_file(BytesIO(data), as_attachment=True, download_name="template-questions.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/admin/export")
@admin_required
def admin_export():
    date_from = request.form.get("from") or None
    date_to = request.form.get("to") or None
    submissions = db.list_submissions(date_from, date_to)
    wb = Workbook()
    summary = wb.active
    summary.title = "Сводка"
    summary.append(
        [
            "ID",
            "Телефон",
            "Начало",
            "Отправка",
            "Время (сек)",
            "Время (мин:сек)",
            "Правильных",
            "Всего оцениваемых",
            "% правильности",
        ]
    )
    for s in submissions:
        mins, secs = divmod(s["duration_seconds"], 60)
        summary.append(
            [
                s["id"],
                s.get("phone") or "",
                s["started_at"],
                s["submitted_at"],
                s["duration_seconds"],
                f"{mins}:{secs:02d}",
                s["scored_correct"],
                s["scored_total"],
                s["score_percent"],
            ]
        )
    detail = wb.create_sheet("Ответы")
    detail.append(["ID анкеты", "Телефон", "Время отправки", "%", "Вопрос", "Тип", "Ответ пользователя", "Правильно"])
    for s in submissions:
        for a in s["answers"]:
            flag = "" if a["is_correct"] is None else ("да" if a["is_correct"] else "нет")
            detail.append(
                [
                    s["id"],
                    s.get("phone") or "",
                    s["submitted_at"],
                    s["score_percent"],
                    a["prompt"],
                    a["qtype"],
                    a["answer_text"],
                    flag,
                ]
            )
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = "answers.xlsx"
    if date_from or date_to:
        name = f"answers_{date_from or 'start'}_{date_to or 'end'}.xlsx"
    return send_file(buf, as_attachment=True, download_name=name, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/admin/clear")
@admin_required
def admin_clear():
    deleted = db.clear_responses()
    flash(f"Удалено анкет: {deleted}", "ok")
    return redirect(url_for("admin_home"))


def seed_if_empty() -> None:
    if db.list_questions():
        return
    db.replace_questions(parse_txt(SAMPLES.read_text(encoding="utf-8")))


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    mins, secs = divmod(int(seconds), 60)
    if mins:
        return f"{mins} мин {secs} с"
    return f"{secs} с"


app.jinja_env.filters["duration"] = format_duration


if __name__ == "__main__":
    db.init_db(ADMIN_USER, ADMIN_PASSWORD)
    seed_if_empty()
    print(f"Локально:  http://127.0.0.1:{PORT}/")
    print(f"В сети:    http://{lan_ip()}:{PORT}/")
    print(f"Админка:   http://127.0.0.1:{PORT}/admin  (логин {ADMIN_USER})")
    disable_tunnel = os.environ.get("DISABLE_TUNNEL", "").strip() in {"1", "true", "yes"}
    if disable_tunnel or os.environ.get("PUBLIC_URL", "").strip():
        print("Туннель отключён: используется адрес хостинга.")
    else:
        print("Поднимаю публичный туннель Serveo...")
        start_tunnel(PORT)
    serve(app, host="0.0.0.0", port=PORT)
