"""Standalone backoffice web application sharing the main app database and sessions."""
import os
from datetime import datetime, timedelta

from flask import Flask, Response, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from models import (
    db, User, HeroBanner, LotteryRoom, DepositRequest, WithdrawalRequest,
    CommissionLedger, PartnerPresence, ThaiLotteryBet, WalletTransaction,
)
from app import app as main_app

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SECRET_KEY = os.environ.get("SECRET_KEY") or "loyalty-app-session-secret-v1"
# Trusted-internal marker: main_app_proxy stamps every request it forwards to
# the main app with this header, and app.py's admin_required/partner_required
# decorators refuse any /admin or /partner request that doesn't carry it — so
# those routes are only reachable by going through the backoffice.
INTERNAL_PROXY_HEADER = "X-Backoffice-Internal"
INTERNAL_PROXY_SECRET = SECRET_KEY
backoffice_app = Flask(__name__, template_folder="templates", static_folder="static")
backoffice_app.config["SECRET_KEY"] = SECRET_KEY
backoffice_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
backoffice_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
backoffice_app.config["SESSION_REFRESH_EACH_REQUEST"] = True
backoffice_app.config["SESSION_COOKIE_HTTPONLY"] = True
backoffice_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
database_url = os.environ.get("DATABASE_URL", "").strip()
if database_url.startswith("postgres://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
sqlite_filename = os.environ.get("SQLITE_FILENAME", "").strip() or (
    "/data/loyalty.db" if os.path.isdir("/data") else "loyalty.db"
)
backoffice_app.config["SQLALCHEMY_DATABASE_URI"] = database_url or (
    "sqlite:///" + (sqlite_filename if os.path.isabs(sqlite_filename) else os.path.join(BASE_DIR, sqlite_filename))
)
db.init_app(backoffice_app)
with backoffice_app.app_context():
    db.create_all()


def logged_user():
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if user_id else None


@backoffice_app.context_processor
def inject_backoffice_globals():
    main_app_url = request.script_root.rstrip("/") + "/main"
    return {"current_user": logged_user(), "main_app_url": main_app_url}


@backoffice_app.route("/")
def home():
    return redirect(url_for("dashboard"))


@backoffice_app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.is_active and check_password_hash(user.password_hash, password):
            partner_inactive = user.is_partner and user.partner_profile and user.partner_profile.status != "active"
            if not user.is_admin and not user.is_partner:
                flash("บัญชีนี้ไม่มีสิทธิ์เข้าหลังบ้าน", "error")
            elif partner_inactive:
                flash("บัญชี Partner นี้ถูกพักการใช้งาน", "error")
            else:
                session.permanent = True
                session["user_id"] = user.id
                return redirect(url_for("dashboard"))
        else:
            flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "error")
    return render_template("backoffice_login.html")


@backoffice_app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("login"))


@backoffice_app.route("/main", defaults={"path": "admin"}, methods=["GET", "POST"])
@backoffice_app.route("/main/", defaults={"path": ""}, methods=["GET", "POST"])
@backoffice_app.route("/main/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def main_app_proxy(path):
    user = logged_user()
    if not user:
        return redirect(url_for("login"))
    if not user.is_admin and not user.is_partner:
        return "Forbidden", 403
    is_admin_path = path == "admin" or path.startswith("admin/")
    if is_admin_path and not user.is_admin:
        return "Forbidden", 403

    forwarded_headers = {
        key: value for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "cookie"}
    }
    forwarded_headers[INTERNAL_PROXY_HEADER] = INTERNAL_PROXY_SECRET
    with main_app.test_client() as client:
        for key, value in request.cookies.items():
            client.set_cookie(key, value)
        response = client.open(
            "/" + path,
            method=request.method,
            query_string=request.query_string.decode("latin-1"),
            data=request.get_data(),
            headers=forwarded_headers,
            content_type=request.content_type,
        )

    main_prefix = request.script_root.rstrip("/") + "/main"

    response_headers = {
        key: value for key, value in response.headers.items()
        if key.lower() not in {"content-length", "transfer-encoding", "connection", "set-cookie"}
    }
    location = response_headers.get("Location")
    if location and location.startswith("/") and not location.startswith(main_prefix + "/"):
        response_headers["Location"] = main_prefix + location
    content = response.get_data()
    if response.headers.get("Content-Type", "").startswith("text/html"):
        rendered = content.decode("utf-8", errors="replace")
        rendered = rendered.replace('href="/', f'href="{main_prefix}/').replace('action="/', f'action="{main_prefix}/')
        rendered = rendered.replace(f'href="{main_prefix}/static/', 'href="/static/').replace(f'src="{main_prefix}/static/', 'src="/static/')
        backoffice_root = request.script_root.rstrip("/")
        rendered = rendered.replace("__BACKOFFICE_HOME__", f"{backoffice_root}/dashboard")
        rendered = rendered.replace("__BACKOFFICE_LOGOUT__", f"{backoffice_root}/logout")
        content = rendered.encode("utf-8")
    return Response(content, status=response.status_code, headers=response_headers)


@backoffice_app.route("/dashboard")
def dashboard():
    user = logged_user()
    if not user:
        return redirect(url_for("login"))
    if not user.is_admin and not user.is_partner:
        return "Forbidden", 403
    if user.is_partner:
        members = User.query.filter_by(partner_id=user.id, role="member").order_by(User.created_at.desc()).all()
        online_cutoff = datetime.utcnow() - timedelta(minutes=10)
        online_count = PartnerPresence.query.filter_by(partner_id=user.id).filter(
            PartnerPresence.last_seen_at >= online_cutoff
        ).count()
        entries = CommissionLedger.query.filter_by(partner_id=user.id).order_by(
            CommissionLedger.created_at.desc()
        ).limit(8).all()
        bets = ThaiLotteryBet.query.join(User, ThaiLotteryBet.user_id == User.id).filter(
            User.partner_id == user.id
        ).order_by(ThaiLotteryBet.created_at.desc()).limit(200).all()
        transactions = WalletTransaction.query.filter_by(user_id=user.id).order_by(
            WalletTransaction.created_at.desc()
        ).limit(8).all()
        return render_template(
            "backoffice_partner.html",
            partner=user,
            profile=user.partner_profile,
            members=members,
            online_count=online_count,
            entries=entries,
            transactions=transactions,
            report={
                "bets": len(bets),
                "amount": sum(float(bet.amount) for bet in bets),
                "wins": sum(1 for bet in bets if bet.status == "win"),
                "losses": sum(1 for bet in bets if bet.status == "lose"),
            },
        )
    stats = {
        "total_users": User.query.filter_by(role="member").count(),
        "total_rooms": LotteryRoom.query.filter_by(is_active=True).count(),
        "pending_deposits": DepositRequest.query.filter_by(status="pending").count(),
        "pending_withdrawals": WithdrawalRequest.query.filter_by(status="pending").count(),
    }
    announcements = HeroBanner.query.order_by(HeroBanner.created_at.desc()).limit(5).all()
    recent_users = User.query.order_by(User.created_at.desc()).limit(5).all()
    return render_template(
        "backoffice_admin_collapsible.html",
        stats=stats,
        announcements=announcements,
        recent_users=recent_users,
    )


if __name__ == "__main__":
    backoffice_app.run(host="0.0.0.0", port=int(os.environ.get("BACKOFFICE_PORT", "5004")), debug=False)
