"""
app.py — ไฟล์หลักของระบบสมาชิกสะสมแต้ม + ระบบหวยรัฐบาลไทย
รันด้วยคำสั่ง:  python app.py
"""
import os
import random
import secrets
import string
import csv
import io
import itertools
from collections import Counter
import re
import shutil
from datetime import datetime, timedelta
from functools import wraps
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from sqlalchemy import inspect, text, or_, func

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, abort, jsonify, Response
)
from werkzeug.utils import secure_filename
from lottery_api import absolute_api_url, fetch_results, flatten_result_items

ORIGINAL_FETCH_RESULTS = fetch_results

# นำเข้า Models ทั้งหมดจาก models.py
from models import (
    db, User, Reward, RedemptionHistory, PointLog,
    HeroBanner, MediaImage, LotteryRoom, BlockedNumber,
    LotteryPayoutRule, LotteryTypeRule, LotteryRateSet, ThaiLotteryPeriod,
    MemberGroupRate, MemberGroupLimit, MemberGroupAccess, MemberGroupStock, ThaiLotteryBet,
    PartnerProfile, PartnerAssistant, PartnerMemberLimit, PartnerRoomSetting, PartnerPayoutRule,
    PartnerAcceptanceLimit, PartnerAcceptanceNumber, PartnerStockShare, PartnerStockLedger,
    PartnerBlockedNumber, PartnerPresence, CommissionLedger, VipTier,
    SeniorProfile, SeniorStockShare, SeniorStockLedger, SeniorCommissionLedger,
    SeniorMemberLimit, SeniorRoomSetting, SeniorBlockedNumber, SeniorAcceptanceLimit, SeniorAcceptanceNumber,
    SeniorAssistant, SeniorPayoutRule,
    PartnerMemberRate, SeniorMemberRate, PartnerMemberStockShare, SeniorMemberStockShare,
    WalletTransaction, Notification, AdminAuditLog, ResponsiblePlayProfile,
    SystemSetting, DepositRequest, WithdrawalRequest, UserBankAccount, LotteryCategory,
    LoginHistory, Announcement
)

# ==========================================================
# CONFIG
# ==========================================================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
# Use a stable fallback so sessions remain valid across app restarts when the
# deployment environment has not yet been configured with SECRET_KEY.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "loyalty-app-session-secret-v1"
app.config["TEMPLATES_AUTO_RELOAD"] = True
database_url = os.environ.get("DATABASE_URL", "").strip()
if database_url.startswith("postgres://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
sqlite_filename = os.environ.get("SQLITE_FILENAME", "").strip()
if not sqlite_filename:
    sqlite_filename = "/data/loyalty.db" if os.path.isdir("/data") else "loyalty.db"
app.config["SQLALCHEMY_DATABASE_URI"] = database_url or (
    "sqlite:///" + (sqlite_filename if os.path.isabs(sqlite_filename) else os.path.join(BASE_DIR, sqlite_filename))
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["POINTS_REWARDS_ENABLED"] = False
app.config["BACKOFFICE_URL"] = os.environ.get("BACKOFFICE_URL", "/backoffice").rstrip("/")
ACCOUNT_TEXT_PATTERN = r"[A-Za-z0-9!@#$%^&*._+\-]+"
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

# กำหนดโฟลเดอร์สำหรับเก็บรูปอัปโหลด
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(BASE_DIR, "static", "uploads"))
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def backup_database_before_startup():
    """Keep a small rolling backup before startup seeding or API sync."""
    database_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if database_uri.startswith("sqlite:///"):
        database_path = database_uri[len("sqlite:///"):]
        if not os.path.isabs(database_path):
            database_path = os.path.join(BASE_DIR, database_path)
    else:
        database_path = os.path.join(BASE_DIR, "loyalty.db")
    if not os.path.isfile(database_path):
        return None

    backup_dir = os.path.join(BASE_DIR, "database-backups")
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(backup_dir, f"loyalty-before-startup-{timestamp}.db")
    shutil.copy2(database_path, backup_path)

    backups = sorted(
        (os.path.join(backup_dir, name) for name in os.listdir(backup_dir)
         if name.startswith("loyalty-before-startup-") and name.endswith(".db")),
        key=os.path.getmtime,
        reverse=True,
    )
    for old_backup in backups[10:]:
        os.remove(old_backup)
    return backup_path

db.init_app(app)


# ==========================================================
# HELPERS
# ==========================================================
def current_user():
    """ดึงข้อมูลผู้ใช้ที่ล็อกอินอยู่ ถ้าไม่มีคืน None"""
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


def app_now():
    """Return the current time in the timezone used by lottery schedules."""
    return datetime.now(BANGKOK_TZ).replace(tzinfo=None)


def ensure_default_admin_accounts():
    """Keep the documented local admin accounts available after a DB reset."""
    changed = False
    accounts = (("admin", "ผู้ดูแลระบบ", "admin1234"), ("admin2", "ผู้ดูแลระบบสำรอง", "a12345"))
    for username, full_name, password in accounts:
        user = User.query.filter_by(username=username).first()
        if user is None:
            user = User(username=username, full_name=full_name, role="admin", points=0, credit_balance=0.0)
            user.set_password(password)
            db.session.add(user)
            changed = True
    if changed:
        db.session.commit()


@app.context_processor
def inject_globals():
    """ส่งตัวแปรเข้า template ทุกหน้า"""
    user = current_user()
    contact_setting = SystemSetting.query.filter_by(key="admin_contact_url").first()
    return {
        "current_user": user,
        "unread_notifications": Notification.query.filter_by(user_id=user.id, is_read=False).count() if user else 0,
        "now": app_now(),
        "HeroBanner": HeroBanner,
        "LotteryRoom": LotteryRoom
        ,"admin_contact_url": contact_setting.value if contact_setting and contact_setting.value else "",
        "brand_name": get_setting("brand_name", "mklotto"),
        "brand_tagline": get_setting("brand_tagline", "LOTTERY NETWORK"),
        "brand_icon_url": get_setting("brand_icon_url", "/static/brand-logo.png"),
        "points_rewards_enabled": app.config["POINTS_REWARDS_ENABLED"],
        "backoffice_url": app.config["BACKOFFICE_URL"],
    }


def get_setting(key, default=""):
    setting = SystemSetting.query.filter_by(key=key).first()
    return setting.value if setting else default


def save_setting(key, value):
    setting = SystemSetting.query.filter_by(key=key).first()
    if setting is None:
        setting = SystemSetting(key=key, value=value)
        db.session.add(setting)
    else:
        setting.value = value
    return setting


API_CATEGORY_NAMES = {
    "lottothaihot": "หวยยอดฮิต",
    "lottoforeign": "หวยต่างประเทศ",
    "maekhong": "หวยแม่โขง",
    "stock": "หวยหุ้นต่างประเทศ",
}

API_CATEGORY_COLORS = {
    "lottothaihot": "#14532d",
    "lottoforeign": "#172554",
    "maekhong": "#164e63",
    "stock": "#3f2d1f",
}


def active_lottery_rooms():
    """Return public lottery rooms while keeping the retired LIW category hidden."""
    return LotteryRoom.query.filter(
        LotteryRoom.is_active.is_(True),
        LotteryRoom.api_category.is_(None) | (LotteryRoom.api_category != "liw"),
        LotteryRoom.category != "หวย LIW",
    ).order_by(LotteryRoom.sort_order.asc())


def _api_datetime(date_value, time_value, fallback):
    if not time_value:
        return fallback
    try:
        return datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M")
    except ValueError:
        return fallback


def _api_draw_datetime(value, date_value):
    raw_value = str(value or "").strip()
    if raw_value:
        try:
            return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    return datetime.strptime(date_value, "%Y-%m-%d")


def sync_lottery_api_results(date_value=None):
    """Import API rooms, schedules, and settle successful results automatically."""
    payload = fetch_results(date_value)
    query_date = payload.get("date") or date_value or app_now().strftime("%Y-%m-%d")
    payloads = [payload]
    if not date_value:
        previous_date = (datetime.strptime(query_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            payloads.append(fetch_results(previous_date))
        except Exception:
            pass
    imported_rooms = 0
    imported_periods = 0
    updated_results = 0

    fallback_keys = {
        "หวยรัฐบาลไทย": "thailotto",
        "หวยฮานอย": "hanoylotto",
        "หวยลาว": "laoslotto",
        "หวยมาเลเซีย": "malaylotto",
        "หวยออมสิน": "gsblotto",
    }

    items = []
    for source_payload in payloads:
        items.extend(flatten_result_items(source_payload))

    for item in items:
        api_key = str(item.get("key") or "").strip()
        label = str(item.get("label") or api_key).strip()
        status = str(item.get("status") or "").strip().lower()
        category_key = str(item.get("category") or "").strip()
        if not api_key or category_key == "liw":
            continue

        room = LotteryRoom.query.filter_by(api_key=api_key).first()
        if room is None:
            for room_name, known_key in fallback_keys.items():
                if known_key == api_key:
                    room = LotteryRoom.query.filter_by(name=room_name).first()
                    break

        category_name = API_CATEGORY_NAMES.get(category_key, category_key or "หวยอื่นๆ")
        category = LotteryCategory.query.filter_by(name=category_name).first()
        if category is None:
            category = LotteryCategory(name=category_name, description="นำเข้าจาก thailottoapi.com")
            db.session.add(category)
            db.session.flush()

        if room is None:
            room = LotteryRoom(
                name=label,
                description="",
                category=category_name,
                category_id=category.id,
                badge_text=api_key[:8].upper(),
                link_url="/lottery/thai",
                button_text="เข้าสู่ห้อง",
                is_active=True,
                sort_order=LotteryRoom.query.count() + 1,
            )
            db.session.add(room)
            db.session.flush()
            imported_rooms += 1

        room.api_key = api_key
        room.api_category = category_key
        room.image_url = absolute_api_url(item.get("icon")) or room.image_url
        room.bg_color = API_CATEGORY_COLORS.get(category_key, room.bg_color or "#172337")
        if room.description and "ข้อมูลตารางหวยจาก" in room.description:
            room.description = ""
        room.category = category_name
        room.category_id = category.id
        room.link_url = f"/lottery/thai?room_id={room.id}"

        draw_date = str(item.get("drawDate") or query_date)[:10]
        try:
            datetime.strptime(draw_date, "%Y-%m-%d")
        except ValueError:
            continue
        draw_datetime = _api_draw_datetime(item.get("drawDate"), draw_date)
        schedule = item.get("schedule") or {}
        open_time = _api_datetime(
            draw_date,
            schedule.get("openTime"),
            datetime.strptime(draw_date, "%Y-%m-%d"),
        )
        close_fallback = draw_datetime - timedelta(minutes=1) if not schedule.get("closeTime") else open_time + timedelta(hours=1)
        close_time = _api_datetime(draw_date, schedule.get("closeTime"), close_fallback)
        if close_time <= open_time:
            close_time += timedelta(days=1)

        period = ThaiLotteryPeriod.query.filter_by(
            room_id=room.id, api_key=api_key, period_date=draw_date
        ).first()
        if period is None:
            period = ThaiLotteryPeriod.query.filter_by(
                room_id=room.id, period_date=draw_date, api_key=None
            ).first()
        if period is None:
            period = ThaiLotteryPeriod(
                room_id=room.id,
                period_date=draw_date,
                open_time=open_time,
                close_time=close_time,
                is_open=status == "pending",
                api_key=api_key,
            )
            db.session.add(period)
            imported_periods += 1
        elif not period.is_checked:
            period.open_time = open_time
            period.close_time = close_time
            period.is_open = status == "pending"

        period.api_status = status
        period.api_key = api_key
        if status == "success" and not period.is_checked:
            period.result_3up = str(item.get("top3") or "").strip() or None
            period.result_2down = str(item.get("bottom2") or "").strip() or None
            # thailottoapi.com returns "เลขหน้า 3 ตัว" and "เลขท้าย 3 ตัว" bundled
            # together in one "bottom3" field (2 front + 2 back for the Thai
            # government lottery). Split them into their own result fields
            # instead of dumping all 4 numbers into result_3back, which used to
            # make "3 หน้า" bets always lose and "3 หลัง" bets settle against
            # numbers that were never actually the back prize.
            bottom3_raw = str(item.get("bottom3") or "").strip()
            bottom3_numbers = [n.strip() for n in bottom3_raw.split(",") if n.strip()]
            if len(bottom3_numbers) == 4:
                period.result_3front = ", ".join(bottom3_numbers[:2])
                period.result_3back = ", ".join(bottom3_numbers[2:])
            else:
                period.result_3front = None
                period.result_3back = bottom3_raw or None
            updated_results += 1

    settlement_admin = User.query.filter_by(role="admin").order_by(User.id.asc()).first()
    if settlement_admin:
        for period in ThaiLotteryPeriod.query.filter_by(is_checked=False, api_status="success").all():
            settle_lottery_period(period, settlement_admin)

    db.session.commit()
    return {"date": query_date, "rooms": imported_rooms, "periods": imported_periods, "results": updated_results}

def extra_bet_wins(bet_type, number, period):
    """ตรวจประเภทที่ไม่อยู่ในเงื่อนไขหลัก: 3 ตัวล่าง, 2 ตัวโต๊ด, คู่คี่, สูงต่ำ"""
    up3 = period.result_3up or ""
    down2 = period.result_2down or ""
    if bet_type == "3down":  # 3 ตัวล่าง = เลขหน้า 3 ตัว + เลขท้าย 3 ตัว (หวยไทยมีอย่างละ 2 รางวัล)
        codes = f"{period.result_3front or ''},{period.result_3back or ''}"
        return number in {c.strip() for c in codes.split(",") if c.strip()}
    if bet_type == "2toad":  # เลข 2 ตัวถูกสลับตำแหน่งได้จาก 3 ตัวบน
        return len(up3) == 3 and not (Counter(number) - Counter(up3))
    parity = lambda d: "คู่" if int(d) % 2 == 0 else "คี่"
    height = lambda d: "ต่ำ" if int(d) <= 4 else "สูง"
    if bet_type == "oddeven_up":
        return len(up3) == 3 and number == parity(up3[-1])
    if bet_type == "oddeven_down":
        return len(down2) == 2 and number == parity(down2[-1])
    if bet_type == "highlow_up":
        return len(up3) == 3 and number == height(up3[-2])
    if bet_type == "highlow_down":
        return len(down2) == 2 and number == height(down2[0])
    return False


def settle_lottery_period(period, admin_user):
    """ตรวจโพยและจ่ายรางวัลของงวดที่มีผลครบแล้ว โดยยังไม่ commit"""
    if period.is_checked:
        return 0
    if not period.result_3up or len(period.result_3up) != 3 or not period.result_2down or len(period.result_2down) != 2:
        return None

    result_3toad = {"".join(item) for item in itertools.permutations(period.result_3up)}
    result_3front = {item.strip() for item in (period.result_3front or "").split(",") if item.strip()}
    result_3back = {item.strip() for item in (period.result_3back or "").split(",") if item.strip()}
    settled = 0
    for bet in ThaiLotteryBet.query.filter_by(period_id=period.id, status="pending").order_by(ThaiLotteryBet.id).all():
        is_win = (
            (bet.bet_type == "3up" and bet.number == period.result_3up)
            or (bet.bet_type == "3toad" and bet.number in result_3toad)
            or (bet.bet_type == "2up" and bet.number == period.result_3up[-2:])
            or (bet.bet_type == "2down" and bet.number == period.result_2down)
            or (bet.bet_type == "runup" and bet.number in period.result_3up)
            or (bet.bet_type == "rundown" and bet.number in period.result_2down)
            or (bet.bet_type == "3front" and bet.number in result_3front)
            or (bet.bet_type == "3back" and bet.number in result_3back)
            or extra_bet_wins(bet.bet_type, bet.number, period)
        )
        bet.status = "win" if is_win else "lose"
        apply_partner_stock_holding(bet, is_win)
        apply_agent_upline_stock_holding(bet, is_win)
        apply_senior_stock_holding(bet, is_win)
        charge_rate_excess(bet, is_win)
        if is_win:
            adjust_credit(
                bet.user, bet.reward_amount,
                f"ถูกรางวัลหวยงวด {period.period_date} ({bet.bet_type}: {bet.number})",
                admin=admin_user,
            )
            notify_user(
                bet.user, "ยินดีด้วย คุณถูกรางวัล",
                f"ได้รับ {bet.reward_amount:,} เครดิตจากเลข {bet.number}", "win",
            )
            settled += 1
    period.is_open = False
    period.is_checked = True
    audit_admin(admin_user, "settle_lottery_period", "lottery_period", period.id,
                f"ผล 3บน={period.result_3up}, 2ล่าง={period.result_2down}")
    return settled


@app.before_request
def refresh_lottery_catalog_on_each_request():
    """Force-refresh API-backed lottery rooms on each normal page load."""
    if (app.testing and fetch_results is ORIGINAL_FETCH_RESULTS) or request.endpoint in {"static", "login", "logout"}:
        return
    try:
        sync_lottery_api_results()
    except Exception:
        db.session.rollback()


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("กรุณาเข้าสู่ระบบก่อนใช้งาน", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def points_rewards_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not app.config["POINTS_REWARDS_ENABLED"]:
            abort(404)
        return view(*args, **kwargs)
    return wrapper


def _require_backoffice_proxy():
    """Admin/partner pages are only ever meant to be reached through the
    backoffice — main_app_proxy() stamps every request it forwards with this
    header. A direct hit on /admin or /partner (bypassing /backoffice) bounces
    to the backoffice login instead of rendering."""
    expected = app.config["SECRET_KEY"]
    provided = request.headers.get("X-Backoffice-Internal")
    if not provided or provided != expected:
        return redirect(f"{app.config['BACKOFFICE_URL']}/login")
    return None


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        gate = _require_backoffice_proxy()
        if gate is not None:
            return gate
        user = current_user()
        if not user:
            flash("กรุณาเข้าสู่ระบบก่อนใช้งาน", "warning")
            return redirect(url_for("login"))
        if not user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


ASSISTANT_PERMISSION_LABELS = {
    "bets": "รายการแทง",
    "members": "จัดการสมาชิก/สาย",
    "takelist": "รายการเก็บของสมาชิก",
    "reports": "รายงานแพ้ชนะ",
    "transfer": "โอนเงิน/การเงิน",
}
# หน้าไหน (ตามชื่อ endpoint หลังตัดคำนำหน้า partner_/senior_) ต้องใช้สิทธิ์อะไร — หน้าที่ไม่อยู่ในรายการเปิดให้ผู้ช่วยทุกคน
ASSISTANT_ENDPOINT_PERMISSIONS = (
    (("bets", "bet_", "pending_bets", "overall"), "bets"),
    (("member", "agents", "online", "settings", "stock", "blocked"), "members"),
    (("takelist",), "takelist"),
    (("report", "pnl", "results"), "reports"),
    (("topup", "deposit", "finance", "statement"), "transfer"),
)


def assistant_permission_for(endpoint):
    name = endpoint or ""
    for prefix in ("partner_", "senior_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for prefixes, permission in ASSISTANT_ENDPOINT_PERMISSIONS:
        if name.startswith(prefixes):
            return permission
    return None


def assistant_allowed(assistant, endpoint):
    """ผู้ช่วยเข้าหน้านี้ได้ไหม ตามสิทธิ์ที่เจ้าของบัญชีติ๊กไว้ (senior เดิมใช้คำว่า agents แทน members)"""
    needed = assistant_permission_for(endpoint)
    if needed is None:
        return True
    granted = {item.strip() for item in (assistant.permissions or "").split(",") if item.strip()}
    if needed == "members" and "agents" in granted:
        return True
    return needed in granted


def selected_permissions(form):
    picked = [key for key in ASSISTANT_PERMISSION_LABELS if key in form.getlist("perm")]
    return ",".join(picked)


def partner_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        gate = _require_backoffice_proxy()
        if gate is not None:
            return gate
        user = current_user()
        if not user:
            flash("กรุณาเข้าสู่ระบบก่อนใช้งาน", "warning")
            return redirect(url_for("login"))
        if not user.is_partner and not user.is_admin:
            abort(403)
        if user.is_partner and user.partner_profile and user.partner_profile.status != "active":
            flash("บัญชี Agent นี้ถูกพักการใช้งาน", "error")
            return redirect(url_for("index"))
        assistant = PartnerAssistant.query.filter_by(assistant_user_id=user.id).first() if user else None
        if assistant and not assistant.is_active:
            flash("บัญชีผู้ช่วยนี้ถูกระงับการใช้งาน", "error")
            return redirect(url_for("index"))
        if assistant and not assistant_allowed(assistant, request.endpoint):
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def senior_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        gate = _require_backoffice_proxy()
        if gate is not None:
            return gate
        user = current_user()
        if not user:
            flash("กรุณาเข้าสู่ระบบก่อนใช้งาน", "warning")
            return redirect(url_for("login"))
        if not user.is_senior and not user.is_admin:
            abort(403)
        if user.is_senior and user.senior_profile and user.senior_profile.status != "active":
            flash("บัญชี Senior นี้ถูกพักการใช้งาน", "error")
            return redirect(url_for("index"))
        assistant = SeniorAssistant.query.filter_by(assistant_user_id=user.id).first() if user else None
        if assistant and not assistant.is_active:
            flash("บัญชีผู้ช่วยนี้ถูกระงับการใช้งาน", "error")
            return redirect(url_for("index"))
        if assistant and not assistant_allowed(assistant, request.endpoint):
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def partner_owner(user=None):
    """Return the owning Partner for either an owner or assistant account."""
    user = user or current_user()
    assistant = PartnerAssistant.query.filter_by(
        assistant_user_id=user.id, is_active=True
    ).first() if user else None
    return assistant.partner if assistant else user


def agent_upline_chain(agent):
    """เดินสายขึ้นจาก Agent คนหนึ่งไปเรื่อยๆ ตาม partner_id (Agent เพิ่ม Agent ย่อย
    ของตัวเองได้ ซ้อนได้ไม่จำกัดชั้น — ใช้คอลัมน์ partner_id ตัวเดียวกับที่ Member ใช้
    ชี้หา Agent ของตัวเอง เพราะเป็นความสัมพันธ์แบบเดียวกัน แค่คนละ role) คืนค่าเป็น
    list ของ Agent ระดับที่สูงกว่า agent คนนี้ (ไม่รวมตัว agent เอง), เรียงจากใกล้สุด
    ไปไกลสุด. กันวนซ้ำ (loop) ด้วย seen set."""
    chain = []
    seen = {agent.id}
    current = agent
    while current.is_partner and current.partner_id:
        upline = current.partner
        if not upline or not upline.is_partner or upline.id in seen:
            break
        chain.append(upline)
        seen.add(upline.id)
        current = upline
    return chain


def agent_upline_senior(agent):
    """หา Senior ที่ดูแลสาย Agent นี้อยู่ — เดินขึ้นไปสุดสาย Agent (ถ้ามีการซ้อนชั้น)
    แล้วดูว่า Agent บนสุดของสายมี Senior คนไหน (เฉพาะ Agent ระดับบนสุดเท่านั้นที่ต้อง
    ผูก senior_id ไว้ตอนสร้างโดย Admin — Agent ย่อยที่ถูกสร้างซ้อนไม่ต้องมี senior_id
    ของตัวเอง เพราะสืบทอดจากต้นสายแทน)"""
    chain = agent_upline_chain(agent)
    top = chain[-1] if chain else agent
    return top.senior


def refresh_vip_status(user):
    """เลื่อนระดับ VIP จากแต้มสะสม โดยแอดมินเป็นผู้กำหนดเกณฑ์"""
    tier = VipTier.query.filter_by(is_active=True).filter(
        VipTier.min_points <= user.points
    ).order_by(VipTier.min_points.desc(), VipTier.rank.desc()).first()
    if tier and user.vip_tier_id != tier.id:
        user.vip_tier_id = tier.id
    return tier


def create_partner_commission(bet):
    member = bet.user
    partner = member.partner
    if not partner or not partner.partner_profile or partner.partner_profile.status != "active":
        return None
    rate = float(partner.partner_profile.commission_rate)
    amount = round(float(bet.amount) * rate / 100, 2)
    if amount <= 0:
        return None
    partner.partner_profile.commission_balance += amount
    record_wallet_transaction(
        partner, "commission", amount,
        partner.partner_profile.commission_balance,
        f"คอมมิชชันโพย {bet.number} ({bet.bet_type})",
        reference_type="bet", reference_id=bet.id,
    )
    entry = CommissionLedger(
        partner_id=partner.id,
        member_id=member.id,
        bet_id=bet.id,
        base_amount=bet.amount,
        rate=rate,
        commission_amount=amount,
        reason=f"คอมมิชชันโพย {bet.number} ({bet.bet_type})",
    )
    db.session.add(entry)
    return entry


def create_agent_upline_commissions(bet):
    """Agent เพิ่ม Agent ย่อยของตัวเองได้ ซ้อนได้ไม่จำกัดชั้น (agent_upline_chain) —
    ฟังก์ชันนี้จ่ายคอมมิชชันให้ Agent ทุกคนที่อยู่ "เหนือ" Agent ที่ดูแลสมาชิกคนนี้
    โดยตรง (ซึ่งได้ค่าคอมไปแล้วจาก create_partner_commission) แต่ละคนคิดจากยอดแทง
    เดียวกัน (bet.amount) ตามอัตราของตัวเอง เป็นคนละก้อนไม่หักลบกัน เหมือนหลักการ
    เดียวกับที่ Senior ได้คอมแยกจาก Agent"""
    member = bet.user
    direct_agent = member.partner
    if not direct_agent:
        return []
    entries = []
    for agent in agent_upline_chain(direct_agent):
        if not agent.partner_profile or agent.partner_profile.status != "active":
            continue
        rate = float(agent.partner_profile.commission_rate)
        amount = round(float(bet.amount) * rate / 100, 2)
        if amount <= 0:
            continue
        agent.partner_profile.commission_balance += amount
        record_wallet_transaction(
            agent, "commission", amount,
            agent.partner_profile.commission_balance,
            f"คอมมิชชันสาย (Agent ย่อย) โพย {bet.number} ({bet.bet_type}) จาก {direct_agent.username}",
            reference_type="bet", reference_id=bet.id,
        )
        entry = CommissionLedger(
            partner_id=agent.id,
            member_id=member.id,
            bet_id=bet.id,
            base_amount=bet.amount,
            rate=rate,
            commission_amount=amount,
            reason=f"คอมมิชชันสาย (Agent ย่อย) โพย {bet.number} ({bet.bet_type})",
        )
        db.session.add(entry)
        entries.append(entry)
    return entries


def create_senior_commission(bet):
    """เหมือน create_partner_commission ทุกประการ แต่เป็นของ Senior ที่ดูแลสาย
    Agent ของสมาชิกคนนี้ (เดินหาจนสุดสาย Agent ก่อน เผื่อ Agent ซ้อนกันหลายชั้น) —
    แยกอิสระจากคอมมิชชันของ Agent โดยสิ้นเชิง (แบ่งจากยอดแทงเดียวกัน ไม่ได้หักจาก
    คอมมิชชันที่ Agent ได้ไปแล้ว)"""
    member = bet.user
    agent = member.partner
    senior = agent_upline_senior(agent) if agent else None
    if not senior or not senior.senior_profile or senior.senior_profile.status != "active":
        return None
    rate = float(senior.senior_profile.commission_rate)
    amount = round(float(bet.amount) * rate / 100, 2)
    if amount <= 0:
        return None
    senior.senior_profile.commission_balance += amount
    record_wallet_transaction(
        senior, "commission", amount,
        senior.senior_profile.commission_balance,
        f"คอมมิชชัน Senior โพย {bet.number} ({bet.bet_type}) จากเอเจ้น {agent.username}",
        reference_type="bet", reference_id=bet.id,
    )
    entry = SeniorCommissionLedger(
        senior_id=senior.id,
        agent_id=agent.id,
        member_id=member.id,
        bet_id=bet.id,
        base_amount=bet.amount,
        rate=rate,
        commission_amount=amount,
        reason=f"คอมมิชชัน Senior โพย {bet.number} ({bet.bet_type})",
    )
    db.session.add(entry)
    return entry


def apply_hold_cap(role, owner_id, bet, room_id, hold_percent):
    """"ตั้งสู้": ยอดสูงสุดที่ผู้ดูแลถือไว้ต่อเลขต่อประเภทในงวดนี้ (ตามสัดส่วน % ถือหุ้น) — ส่วนที่เกินส่งต่อให้บริษัท
    ไม่มีการตั้งค่า = ไม่จำกัด, ตั้งเป็น 0 = ไม่ถือเลย คืน % ที่ถือจริงของโพยนี้ (คิดสะสมตามลำดับโพย)"""
    limit_model, owner_field = (
        (PartnerAcceptanceLimit, "partner_id") if role == "partner" else (SeniorAcceptanceLimit, "senior_id")
    )
    row = limit_model.query.filter_by(**{owner_field: owner_id, "room_id": room_id, "bet_type": bet.bet_type}).first()
    stake = float(bet.amount)
    if row is None or stake <= 0:
        return hold_percent
    ledger = PartnerStockLedger if role == "partner" else SeniorStockLedger
    owner_col = ledger.partner_id if role == "partner" else ledger.senior_id
    prior = db.session.query(
        func.coalesce(func.sum(ledger.stake_amount * ledger.hold_percent / 100.0), 0.0)
    ).join(ThaiLotteryBet, ThaiLotteryBet.id == ledger.bet_id).filter(
        owner_col == owner_id, ThaiLotteryBet.period_id == bet.period_id,
        ThaiLotteryBet.bet_type == bet.bet_type, ThaiLotteryBet.number == bet.number,
        ThaiLotteryBet.id < bet.id,
    ).scalar() or 0.0
    remaining = max(0.0, float(row.amount_limit) - float(prior))
    held = min(stake * hold_percent / 100, remaining)
    return held / stake * 100


def charge_rate_excess(bet, is_win):
    """ผู้ดูแลที่ให้สมาชิกได้อัตราจ่าย/ส่วนลดเกินกว่าที่ตัวเองได้รับมา ต้องจ่ายส่วนต่างเอง
    — หักจากยอดคอมมิชชั่นของผู้ตั้งค่า พร้อมบันทึกลงสมุดคอมมิชชั่น (รายการติดลบ)
    ส่วนลดส่วนเกินหักทุกโพยที่ออกผล ส่วนอัตราจ่ายส่วนเกินหักเฉพาะโพยที่ถูกรางวัล"""
    charges = {}
    if bet.discount_grantor_id and (bet.discount_excess_pct or 0) > 0:
        charges[bet.discount_grantor_id] = charges.get(bet.discount_grantor_id, 0.0) + float(bet.amount) * bet.discount_excess_pct / 100
    if is_win and bet.payout_grantor_id and (bet.payout_excess or 0) > 0:
        charges[bet.payout_grantor_id] = charges.get(bet.payout_grantor_id, 0.0) + float(bet.amount) * bet.payout_excess
    for owner_id, raw_amount in charges.items():
        amount = round(raw_amount, 2)
        owner = db.session.get(User, owner_id)
        if owner is None or amount <= 0:
            continue
        profile = owner.partner_profile if owner.is_partner else owner.senior_profile
        if profile is None:
            continue
        reason = f"ส่วนต่างอัตราจ่าย/ส่วนลดที่ให้สมาชิกเกินที่ได้รับมา โพย {bet.number} ({bet.bet_type})"
        profile.commission_balance = round(profile.commission_balance - amount, 2)
        record_wallet_transaction(
            owner, "commission", -amount, profile.commission_balance, reason,
            reference_type="bet", reference_id=bet.id,
        )
        if owner.is_partner:
            db.session.add(CommissionLedger(
                partner_id=owner.id, member_id=bet.user_id, bet_id=bet.id, base_amount=bet.amount,
                rate=0.0, commission_amount=-amount, reason=reason,
            ))
        else:
            db.session.add(SeniorCommissionLedger(
                senior_id=owner.id, agent_id=bet.user.partner_id, member_id=bet.user_id, bet_id=bet.id,
                base_amount=bet.amount, rate=0.0, commission_amount=-amount, reason=reason,
            ))


def apply_partner_stock_holding(bet, is_win):
    """บันทึกกำไร/ขาดทุนของ Partner ที่เลือก "ถือหุ้น" บางส่วนของห้องนี้ไว้เอง
    แยกจากคอมมิชชันโดยสิ้นเชิง — ถ้าไม่ได้ตั้งค่า % ถือหุ้นไว้ จะไม่มีผลใดๆ
    ถ้าตั้ง % ถือหุ้นเฉพาะสมาชิกคนนี้ไว้ (PartnerMemberStockShare) ใช้ค่านั้นแทนค่า
    ระดับห้องปกติ (เฉพาะเจาะจงกว่าชนะ ไม่รวมกัน)"""
    member = bet.user
    partner = member.partner
    if not partner or not partner.partner_profile or partner.partner_profile.status != "active":
        return
    room_id = bet.period.room_id if bet.period else None
    if not room_id:
        return
    member_share = PartnerMemberStockShare.query.filter_by(
        partner_id=partner.id, member_id=member.id, room_id=room_id
    ).first()
    if member_share is not None:
        hold_percent = member_share.hold_percent
    else:
        group_percent = member_group_stock_percent(partner.id, member.id, room_category_name(bet.period.room))
        if group_percent is not None:
            hold_percent = group_percent
        else:
            share = PartnerStockShare.query.filter_by(partner_id=partner.id, room_id=room_id).first()
            hold_percent = share.hold_percent if share else 0.0
    if hold_percent <= 0:
        return
    hold_percent = apply_hold_cap("partner", partner.id, bet, room_id, hold_percent)
    if hold_percent <= 0:
        return
    house_pnl = float(bet.amount) if not is_win else (float(bet.amount) - float(bet.reward_amount))
    pnl = round(house_pnl * hold_percent / 100, 2)
    if pnl == 0:
        return
    partner.partner_profile.stock_balance = round(partner.partner_profile.stock_balance + pnl, 2)
    record_wallet_transaction(
        partner, "stock", pnl, partner.partner_profile.stock_balance,
        f"ถือหุ้น {hold_percent:g}% โพย {bet.number} ({bet.bet_type})",
        reference_type="bet", reference_id=bet.id,
    )
    db.session.add(PartnerStockLedger(
        partner_id=partner.id, member_id=member.id, bet_id=bet.id, room_id=room_id,
        hold_percent=hold_percent, stake_amount=bet.amount, pnl_amount=pnl,
    ))


def apply_agent_upline_stock_holding(bet, is_win):
    """เหมือน create_agent_upline_commissions แต่เป็นฝั่งถือหุ้น — Agent ทุกคนที่อยู่
    เหนือ Agent ที่ดูแลสมาชิกคนนี้โดยตรง คำนวณจาก house_pnl เดียวกัน คนละก้อน
    ไม่หักลบกัน (เหมือนหลักการเดียวกับ apply_senior_stock_holding)"""
    member = bet.user
    direct_agent = member.partner
    if not direct_agent:
        return
    room_id = bet.period.room_id if bet.period else None
    if not room_id:
        return
    house_pnl = float(bet.amount) if not is_win else (float(bet.amount) - float(bet.reward_amount))
    for agent in agent_upline_chain(direct_agent):
        if not agent.partner_profile or agent.partner_profile.status != "active":
            continue
        share = PartnerStockShare.query.filter_by(partner_id=agent.id, room_id=room_id).first()
        if not share or share.hold_percent <= 0:
            continue
        pnl = round(house_pnl * share.hold_percent / 100, 2)
        if pnl == 0:
            continue
        agent.partner_profile.stock_balance = round(agent.partner_profile.stock_balance + pnl, 2)
        record_wallet_transaction(
            agent, "stock", pnl, agent.partner_profile.stock_balance,
            f"ถือหุ้นสาย (Agent ย่อย) {share.hold_percent:g}% โพย {bet.number} ({bet.bet_type}) จาก {direct_agent.username}",
            reference_type="bet", reference_id=bet.id,
        )
        db.session.add(PartnerStockLedger(
            partner_id=agent.id, member_id=member.id, bet_id=bet.id, room_id=room_id,
            hold_percent=share.hold_percent, stake_amount=bet.amount, pnl_amount=pnl,
        ))


def apply_senior_stock_holding(bet, is_win):
    """เหมือน apply_partner_stock_holding ทุกประการ แต่คำนวณจาก house_pnl เดียวกัน
    แยกอิสระจากหุ้นของ Agent — ไม่ได้หักลบซึ่งกันและกัน (Senior กับ Agent อาจถือหุ้น
    ห้องเดียวกันคนละ % พร้อมกันได้ โดยไม่กระทบกัน) หา Senior จากบนสุดของสาย Agent
    เผื่อ Agent ซ้อนกันหลายชั้น"""
    member = bet.user
    agent = member.partner
    senior = agent_upline_senior(agent) if agent else None
    if not senior or not senior.senior_profile or senior.senior_profile.status != "active":
        return
    room_id = bet.period.room_id if bet.period else None
    if not room_id:
        return
    member_share = SeniorMemberStockShare.query.filter_by(
        senior_id=senior.id, member_id=member.id, room_id=room_id
    ).first()
    if member_share is not None:
        hold_percent = member_share.hold_percent
    else:
        group_percent = member_group_stock_percent(senior.id, member.id, room_category_name(bet.period.room))
        if group_percent is not None:
            hold_percent = group_percent
        else:
            share = SeniorStockShare.query.filter_by(senior_id=senior.id, room_id=room_id).first()
            hold_percent = share.hold_percent if share else 0.0
    if hold_percent <= 0:
        return
    hold_percent = apply_hold_cap("senior", senior.id, bet, room_id, hold_percent)
    if hold_percent <= 0:
        return
    house_pnl = float(bet.amount) if not is_win else (float(bet.amount) - float(bet.reward_amount))
    pnl = round(house_pnl * hold_percent / 100, 2)
    if pnl == 0:
        return
    senior.senior_profile.stock_balance = round(senior.senior_profile.stock_balance + pnl, 2)
    record_wallet_transaction(
        senior, "stock", pnl, senior.senior_profile.stock_balance,
        f"ถือหุ้น Senior {hold_percent:g}% โพย {bet.number} ({bet.bet_type}) จากเอเจ้น {agent.username}",
        reference_type="bet", reference_id=bet.id,
    )
    db.session.add(SeniorStockLedger(
        senior_id=senior.id, agent_id=agent.id, member_id=member.id, bet_id=bet.id, room_id=room_id,
        hold_percent=hold_percent, stake_amount=bet.amount, pnl_amount=pnl,
    ))


def record_wallet_transaction(user, wallet_type, change, balance_after, reason,
                              reference_type="", reference_id=None, admin=None):
    db.session.add(WalletTransaction(
        user_id=user.id,
        wallet_type=wallet_type,
        change=float(change),
        balance_after=float(balance_after),
        reference_type=reference_type,
        reference_id=reference_id,
        reason=reason,
        admin_id=admin.id if admin else None,
    ))


def notify_user(user, title, message, category="system"):
    db.session.add(Notification(
        user_id=user.id, title=title, message=message, category=category
    ))


def audit_admin(admin, action, target_type="", target_id=None, details=""):
    if admin:
        db.session.add(AdminAuditLog(
            admin_id=admin.id, action=action, target_type=target_type,
            target_id=target_id, details=details,
        ))


def get_responsible_profile(user):
    profile = user.responsible_play
    if profile is None:
        profile = ResponsiblePlayProfile(user_id=user.id)
        db.session.add(profile)
        db.session.flush()
    return profile


BANK_CATALOG = {
    "kbank": {"name": "กสิกรไทย", "logo": "/static/banks/kbank.svg"},
    "scb": {"name": "ไทยพาณิชย์", "logo": "/static/banks/scb.svg"},
    "bbl": {"name": "กรุงเทพ", "logo": "/static/banks/bbl.svg"},
    "ktb": {"name": "กรุงไทย", "logo": "/static/banks/ktb.svg"},
    "ttb": {"name": "ทีทีบี", "logo": "/static/banks/ttb.svg"},
    "bay": {"name": "กรุงศรีอยุธยา", "logo": "/static/banks/bay.svg"},
}


def get_bank_catalog():
    return BANK_CATALOG


def ensure_betting_allowed(user, amount, room_id=None):
    if not partner_room_is_enabled(user, room_id):
        raise ValueError("Agent ปิดการให้บริการห้องหวยนี้ไว้")
    profile = user.responsible_play
    if profile and profile.self_excluded_until and profile.self_excluded_until > datetime.now():
        raise ValueError("บัญชีของคุณอยู่ในช่วงพักการเล่น")
    if profile and profile.daily_bet_limit is not None:
        start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        spent = db.session.query(db.func.coalesce(db.func.sum(ThaiLotteryBet.amount), 0)).filter(
            ThaiLotteryBet.user_id == user.id,
            ThaiLotteryBet.created_at >= start_of_day,
        ).scalar() or 0
        if float(spent) + float(amount) > float(profile.daily_bet_limit):
            raise ValueError(f"เกินวงเงินเดิมพันต่อวันที่ตั้งไว้ ({profile.daily_bet_limit:,.2f} เครดิต)")


def generate_redeem_code(length: int = 8) -> str:
    """สร้างรหัสรับของรางวัลแบบไม่ซ้ำ เช่น RD-A7K2M9"""
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "RD-" + "".join(random.choices(chars, k=length))
        exists = RedemptionHistory.query.filter_by(redeem_code=code).first()
        if not exists:
            return code


def normalize_bet_type(raw_value):
    if raw_value is None:
        return None
    key = str(raw_value).strip().lower().replace(" ", "")
    aliases = {
        "3ตัวบน": "3up",
        "3up": "3up",
        "3ตัวล่าง": "3down",
        "3down": "3down",
        "3ตัวโต๊ด": "3toad",
        "3toad": "3toad",
        "2ตัวบน": "2up",
        "2up": "2up",
        "2ตัวล่าง": "2down",
        "2down": "2down",
        "วิ่งบน": "runup",
        "runup": "runup",
        "วิ่งล่าง": "rundown",
        "rundown": "rundown",
        "2ตัวโต๊ด": "2toad",
        "2toad": "2toad",
        "คู่คี่บน": "oddeven_up",
        "oddeven_up": "oddeven_up",
        "คู่คี่ล่าง": "oddeven_down",
        "oddeven_down": "oddeven_down",
        "สูงต่ำบน": "highlow_up",
        "highlow_up": "highlow_up",
        "สูงต่ำล่าง": "highlow_down",
        "highlow_down": "highlow_down",
    }
    return aliases.get(key) or aliases.get(raw_value.strip().lower())


def get_lottery_rates():
    defaults = {
        "3up": 900,
        "3toad": 150,
        "3down": 145,
        "2up": 90,
        "2down": 90,
        "runup": 3,
        "rundown": 4,
    }

    rules = LotteryPayoutRule.query.filter_by(is_active=True).all()
    merged = defaults.copy()
    for rule in rules:
        key = (rule.bet_type or "").strip()
        if key:
            merged[key] = float(rule.payout_multiplier)
    return merged


BET_NUMBER_LENGTHS = {
    "3up": 3,
    "3toad": 3,
    "3down": 3,
    "2up": 2,
    "2down": 2,
    "runup": 1,
    "rundown": 1,
    "2toad": 2,
}
# ประเภทที่ "เลข" เป็นคำเลือก ไม่ใช่ตัวเลข — เปิดให้แทงได้เมื่อแอดมินตั้งอัตราจ่ายของประเภทนั้นแล้วเท่านั้น
EXTRA_BET_CHOICES = {
    "oddeven_up": ("คู่", "คี่"),
    "oddeven_down": ("คู่", "คี่"),
    "highlow_up": ("สูง", "ต่ำ"),
    "highlow_down": ("สูง", "ต่ำ"),
}
EXTRA_BET_TYPES = ("2toad", "oddeven_up", "oddeven_down", "highlow_up", "highlow_down")
MAX_BET_ENTRIES = 1300
BET_TYPE_LABELS = {
    "3up": "3 ตัวบน",
    "3toad": "3 ตัวโต๊ด",
    "3down": "3 ตัวล่าง",
    "2up": "2 ตัวบน",
    "2down": "2 ตัวล่าง",
    "runup": "วิ่งบน",
    "rundown": "วิ่งล่าง",
    "2toad": "2 ตัวโต๊ด",
    "oddeven_up": "คู่คี่บน",
    "oddeven_down": "คู่คี่ล่าง",
    "highlow_up": "สูงต่ำบน",
    "highlow_down": "สูงต่ำล่าง",
}
MAX_BET_AMOUNT = 1_000_000


# ค่าเริ่มต้นต่อประเภทการแทง (ส่วนลด %, ขั้นต่ำ, ขั้นสูงต่อรายการ) — ตั้งใหม่ได้ที่หน้าแอดมิน (LotteryTypeRule)
# ส่วนลดเริ่มต้น 0% เพื่อไม่ให้ยอดเงินเปลี่ยนเองตอน deploy — แอดมินต้องตั้งเองก่อนถึงจะมีผล
TYPE_RULE_DEFAULTS = {
    "3up": (0.0, 1, 300),
    "3toad": (0.0, 1, 1000),
    "3down": (0.0, 1, 300),
    "2up": (0.0, 1, 2000),
    "2down": (0.0, 1, 2000),
    "runup": (0.0, 1, 10000),
    "rundown": (0.0, 1, 10000),
    "2toad": (0.0, 1, 2000),
    "oddeven_up": (0.0, 1, 10000),
    "oddeven_down": (0.0, 1, 10000),
    "highlow_up": (0.0, 1, 10000),
    "highlow_down": (0.0, 1, 10000),
}


def get_type_rules():
    """คืน {bet_type: {"discount": %, "min": int, "max": int}} — ค่าที่แอดมินตั้งไว้ทับค่าเริ่มต้น"""
    rules = {
        key: {"discount": d, "min": lo, "max": hi}
        for key, (d, lo, hi) in TYPE_RULE_DEFAULTS.items()
    }
    for row in LotteryTypeRule.query.all():
        rules[row.bet_type] = {"discount": row.discount_pct, "min": row.min_bet, "max": row.max_bet}
    return rules


def validate_lottery_entry(item, rates):
    raw_type = item.get("bet_type")
    raw_number = str(item.get("number", "")).strip()
    raw_amount = item.get("amount")
    bet_type = normalize_bet_type(raw_type)

    try:
        amount = int(raw_amount)
    except (TypeError, ValueError):
        raise ValueError("จำนวนเงินเดิมพันต้องเป็นจำนวนเต็ม")

    expected_length = BET_NUMBER_LENGTHS.get(bet_type)
    choices = EXTRA_BET_CHOICES.get(bet_type)
    if not bet_type or (expected_length is None and choices is None) or bet_type not in rates:
        raise ValueError("ประเภทการแทงไม่ถูกต้อง")
    if choices:
        if raw_number not in choices:
            raise ValueError(f"{BET_TYPE_LABELS[bet_type]} เลือกได้เฉพาะ {' หรือ '.join(choices)}")
    elif not raw_number.isdigit() or len(raw_number) != expected_length:
        raise ValueError(f"เลขสำหรับ {bet_type} ต้องเป็นตัวเลข {expected_length} หลัก")
    if amount <= 0:
        raise ValueError("จำนวนเงินเดิมพันต้องมากกว่า 0")

    return bet_type, raw_number, amount


def close_expired_periods(room_id=None):
    """ปิดงวดที่เลยเวลารับแทงแล้ว ก่อนให้ผู้ใช้เห็นหรือส่งโพย"""
    query = ThaiLotteryPeriod.query.filter(
        ThaiLotteryPeriod.is_open.is_(True),
        ThaiLotteryPeriod.close_time <= app_now(),
    )
    if room_id is not None:
        query = query.filter_by(room_id=room_id)

    expired_periods = query.all()
    if expired_periods:
        for period in expired_periods:
            period.is_open = False
        db.session.commit()
    return len(expired_periods)


RATE_TIER_NAMES = {1: "อัตราจ่ายเริ่มต้น", 2: "อัตราจ่ายชุดที่ 2"}
RATE_TABLE_ORDER = ("rundown", "2down", "3toad", "2up", "3up", "runup", "3down")

# ตัวเลขอ้างอิงจากเว็บตัวอย่าง (ใช้ตอน seed ครั้งแรกเท่านั้น — หลังจากนั้นแอดมินแก้ได้เอง)
REFERENCE_TIER1 = {  # bet_type: (จ่าย, ลด %, ขั้นต่ำ, ขั้นสูง)
    "rundown": (4, 12, 1, 10000), "2down": (95, 5, 1, 2000), "3toad": (125, 15, 1, 1000),
    "2up": (95, 5, 1, 2000), "3up": (800, 15, 1, 300), "runup": (3, 12, 1, 10000), "3down": (145, 15, 1, 300),
}
REFERENCE_TIER2_GENERAL = {  # หวยรายวัน / DC / ต่างประเทศ
    "rundown": (4, 12), "2down": (100, 0), "3toad": (140, 4), "2up": (100, 0), "3up": (900, 5), "runup": (3, 12), "3down": (153, 7),
}
REFERENCE_TIER2_STOCK = {  # หวยหุ้น
    "rundown": (4, 12), "2down": (70, 30), "3toad": (105, 33), "2up": (70, 30), "3up": (550, 35), "runup": (3, 12), "3down": (125, 33),
}


def room_category_name(room):
    if room is None:
        return ""
    return room.category_ref.name if room.category_ref else (room.category or "")


def get_rate_set(category, tier):
    """แถวอัตราจ่ายของชุดที่ tier (ชุด 1 = ค่าเริ่มต้นกลาง ไม่มีแถวเฉพาะหมวด)"""
    if tier == 1 or not category:
        return {}
    return {row.bet_type: row for row in LotteryRateSet.query.filter_by(category=category, tier=tier)}


def available_rate_tiers(category):
    tiers = [1]
    if category and LotteryRateSet.query.filter_by(category=category, tier=2).first():
        tiers.append(2)
    return tiers


def user_lottery_rates(user, category=None, tier=1):
    """อัตราจ่ายของห้อง (ชุดที่เลือก) + เรทของ Senior/Agent ที่ดูแลสมาชิกคนนี้ (ยังไม่รวมเลขอั้นและเรทเฉพาะรายคน)"""
    rates = get_lottery_rates()
    for bet_type, row in get_rate_set(category, tier).items():
        if row.payout_multiplier > 0:
            rates[bet_type] = float(row.payout_multiplier)
    if user.partner_id:
        senior = agent_upline_senior(user.partner)
        if senior:
            rates.update(get_senior_payout_rates(senior))  # ใช้ก่อน — Agent เฉพาะเจาะจงกว่าจะทับทีหลัง
        rates.update(get_partner_payout_rates(user.partner))
    return rates


def member_setting_owner_ids(member):
    """ผู้ดูแลที่ตั้งค่ารายกลุ่มหวยให้สมาชิกคนนี้ได้ เรียงจากใกล้สุด: Agent ที่ดูแลตรง → Senior ในสาย
    (Agent ที่อยู่สูงกว่าในสายไม่ได้ตั้งค่ารายสมาชิก — ทำผ่าน Agent ย่อยโดยตรง)"""
    agent = getattr(member, "partner", None)
    if agent is None or not getattr(member, "id", None):
        return []
    owners = [agent.id]
    senior = agent_upline_senior(agent)
    if senior is not None:
        owners.append(senior.id)
    return owners


def member_group_enabled(member, category, tier=0):
    """tier = 0: กลุ่มหวยเปิดอยู่ไหม / tier >= 1: ชุดอัตราจ่ายนั้นเปิดอยู่ไหม — ปิดโดยคนใดคนหนึ่ง = ปิด"""
    owners = member_setting_owner_ids(member)
    if not owners:
        return True
    return MemberGroupAccess.query.filter(
        MemberGroupAccess.member_id == member.id, MemberGroupAccess.owner_id.in_(owners),
        MemberGroupAccess.category == category, MemberGroupAccess.tier == tier,
        MemberGroupAccess.is_enabled.is_(False),
    ).first() is None


def member_group_limits(member, category):
    """{bet_type: {"min", "max", "per_number"}} รวมทุกผู้ดูแล — เข้มงวดที่สุดชนะ (ขั้นต่ำเอาสูงสุด, ขั้นสูงเอาต่ำสุด)"""
    owners = member_setting_owner_ids(member)
    result = {}
    if not owners:
        return result
    for row in MemberGroupLimit.query.filter(
        MemberGroupLimit.member_id == member.id, MemberGroupLimit.owner_id.in_(owners),
        MemberGroupLimit.category == category,
    ):
        cur = result.setdefault(row.bet_type, {"min": None, "max": None, "per_number": None})
        if row.min_bet is not None:
            cur["min"] = row.min_bet if cur["min"] is None else max(cur["min"], row.min_bet)
        if row.max_bet is not None:
            cur["max"] = row.max_bet if cur["max"] is None else min(cur["max"], row.max_bet)
        if row.max_number_bet is not None:
            cur["per_number"] = row.max_number_bet if cur["per_number"] is None else min(cur["per_number"], row.max_number_bet)
    return result


def member_group_rate_overrides(member, category, tier):
    """{bet_type: (payout|None, discount|None, payout_owner_id|None, discount_owner_id|None)}
    — ผู้ดูแลที่ใกล้สมาชิกที่สุดชนะ (Agent ก่อน Senior) แยกกันรายช่อง และบอกด้วยว่าใครเป็นคนตั้ง"""
    owners = member_setting_owner_ids(member)
    result = {}
    if not owners:
        return result
    rows = MemberGroupRate.query.filter(
        MemberGroupRate.member_id == member.id, MemberGroupRate.owner_id.in_(owners),
        MemberGroupRate.category == category, MemberGroupRate.tier == tier,
    ).all()
    for owner_id in owners:
        for row in rows:
            if row.owner_id != owner_id:
                continue
            payout, discount, payout_owner, discount_owner = result.get(row.bet_type, (None, None, None, None))
            if payout is None and row.payout_multiplier is not None:
                payout, payout_owner = row.payout_multiplier, row.owner_id
            if discount is None and row.discount_pct is not None:
                discount, discount_owner = row.discount_pct, row.owner_id
            result[row.bet_type] = (payout, discount, payout_owner, discount_owner)
    return result


def member_number_total(user, period, bet_type, number):
    """ยอดแทงเลขนี้ (ประเภทนี้ งวดนี้) ที่สมาชิกส่งไปแล้วและยังไม่ถูกยกเลิก — ใช้ตรวจ 'สูงสุดต่อเลข'"""
    total = db.session.query(func.coalesce(func.sum(ThaiLotteryBet.amount), 0)).filter(
        ThaiLotteryBet.user_id == user.id, ThaiLotteryBet.period_id == period.id,
        ThaiLotteryBet.bet_type == bet_type, ThaiLotteryBet.number == number,
        ThaiLotteryBet.status != "cancelled",
    ).scalar()
    return int(total or 0)


def member_group_stock_percent(owner_id, member_id, category):
    row = MemberGroupStock.query.filter_by(owner_id=owner_id, member_id=member_id, category=category).first()
    return row.hold_percent if row else None


def build_rate_tables(user, category, member_min, member_max, type_rules=None):
    """ตารางอัตราจ่ายของแต่ละชุดที่หมวดนี้มี — ค่าที่สมาชิกได้จริง (ใช้แสดงและให้หน้าเว็บคำนวณส่วนลด)"""
    type_rules = type_rules or get_type_rules()
    group_limits = member_group_limits(user, category)
    tables = []
    for tier in available_rate_tiers(category):
        if tier != 1 and not member_group_enabled(user, category, tier):
            continue
        rates = user_lottery_rates(user, category, tier)
        for bet_type in list(rates):
            personal = member_specific_rate(user, bet_type)
            if personal is not None:
                rates[bet_type] = personal
        overrides = member_group_rate_overrides(user, category, tier)
        tier_set = get_rate_set(category, tier)
        rows = []
        for key in RATE_TABLE_ORDER + tuple(t for t in EXTRA_BET_TYPES if rates.get(t)):
            rule = type_rules.get(key, {"discount": 0.0, "min": 1, "max": MAX_BET_AMOUNT})
            payout_ov, discount_ov = overrides.get(key, (None, None, None, None))[:2]
            glim = group_limits.get(key) or {}
            rows.append({
                "key": key,
                "label": BET_TYPE_LABELS[key],
                "rate": payout_ov if payout_ov is not None else rates.get(key, 0),
                "discount": discount_ov if discount_ov is not None else (tier_set[key].discount_pct if key in tier_set else rule["discount"]),
                "min": max(rule["min"], member_min, glim.get("min") or 0),
                "max": min(rule["max"], member_max, glim.get("max") or MAX_BET_AMOUNT),
            })
        tables.append({"tier": tier, "name": RATE_TIER_NAMES[tier], "rows": rows})
    return tables


def add_thai_lottery_bets(user, period, entries, rate_tier=1, remark=""):
    if not entries:
        return 0
    if len(entries) > MAX_BET_ENTRIES:
        raise ValueError(f"โพยหนึ่งใบมีรายการได้ไม่เกิน {MAX_BET_ENTRIES} รายการ")

    now = app_now()
    if not period or not period.is_open or period.close_time <= now:
        raise ValueError("งวดนี้ปิดรับแทงแล้ว")
    if period.open_time and period.open_time > now:
        raise ValueError("งวดนี้ยังไม่เปิดรับแทง")

    category = room_category_name(period.room) if period.room_id else ""
    if rate_tier not in available_rate_tiers(category):
        raise ValueError("ไม่พบอัตราจ่ายที่เลือก กรุณาลองใหม่")
    if not member_group_enabled(user, category):
        raise ValueError("กลุ่มหวยนี้ถูกปิดสำหรับบัญชีของคุณ กรุณาติดต่อเอเย่นต์")
    if rate_tier != 1 and not member_group_enabled(user, category, rate_tier):
        raise ValueError("อัตราจ่ายที่เลือกถูกปิดสำหรับบัญชีของคุณ")
    tier_set = get_rate_set(category, rate_tier)
    rates = user_lottery_rates(user, category, rate_tier)
    group_limits = member_group_limits(user, category)
    group_overrides = member_group_rate_overrides(user, category, rate_tier)
    number_totals = {}
    validated_entries = []
    total_amount = 0
    total_paid = 0.0  # ยอดที่หักจากเครดิตจริง = ยอดแทง − ส่วนลด
    type_rules = get_type_rules()
    for item in entries:
        bet_type, raw_number, amount = validate_lottery_entry(item, rates)
        if amount > MAX_BET_AMOUNT:
            raise ValueError(f"จำนวนเงินเดิมพันต่อรายการต้องไม่เกิน {MAX_BET_AMOUNT:,} เครดิต")
        type_rule = type_rules.get(bet_type)
        if type_rule:
            label = BET_TYPE_LABELS.get(bet_type, bet_type)
            if amount < type_rule["min"]:
                raise ValueError(f"{label} แทงขั้นต่ำ {type_rule['min']:,} บาทต่อรายการ")
            if amount > type_rule["max"]:
                raise ValueError(f"{label} แทงได้สูงสุด {type_rule['max']:,} บาทต่อรายการ")
        glim = group_limits.get(bet_type)
        if glim:
            label = BET_TYPE_LABELS.get(bet_type, bet_type)
            if glim["min"] is not None and amount < glim["min"]:
                raise ValueError(f"{label} แทงขั้นต่ำ {glim['min']:,} บาทต่อรายการ (ตามที่ผู้ดูแลตั้งไว้)")
            if glim["max"] is not None and amount > glim["max"]:
                raise ValueError(f"{label} แทงได้สูงสุด {glim['max']:,} บาทต่อรายการ (ตามที่ผู้ดูแลตั้งไว้)")
            if glim["per_number"] is not None:
                key = (bet_type, raw_number)
                number_totals[key] = number_totals.get(key, 0) + amount
                if member_number_total(user, period, bet_type, raw_number) + number_totals[key] > glim["per_number"]:
                    raise ValueError(f"{label} เลข {raw_number} แทงรวมได้สูงสุด {glim['per_number']:,} บาทต่อเลข")
        group_payout, group_discount, payout_owner, discount_owner = group_overrides.get(bet_type, (None, None, None, None))
        base_discount = tier_set[bet_type].discount_pct if bet_type in tier_set else (type_rule["discount"] if type_rule else 0.0)
        discount_pct = group_discount if group_discount is not None else base_discount
        discount = round(amount * discount_pct / 100, 2)
        validated_entries.append({
            "bet_type": bet_type, "number": raw_number, "amount": amount, "discount": discount,
            "group_payout": group_payout, "payout_owner": payout_owner,
            "discount_excess_pct": max(0.0, discount_pct - base_discount) if group_discount is not None else 0.0,
            "discount_owner": discount_owner if group_discount is not None else None,
        })
        total_amount += amount
        total_paid += amount - discount

    if not validated_entries:
        return 0

    ensure_betting_allowed(user, total_amount, period.room_id)
    if user.credit_balance < total_paid:
        raise ValueError(f"เครดิตของคุณไม่พอ! (มีอยู่ {user.credit_balance:,.2f} เครดิต)")

    ticket_code = f"TK-{app_now().strftime('%Y%m%d%H%M%S%f')}-{random.randint(100, 999)}"
    created = 0
    for entry in validated_entries:
        bet_type, raw_number, amount, discount = entry["bet_type"], entry["number"], entry["amount"], entry["discount"]
        group_payout = entry["group_payout"]
        payout_excess, payout_grantor = 0.0, None

        blocked_rate = partner_blocked_rate(user, period.room_id, bet_type, raw_number)
        blocked = None
        if period.room_id and blocked_rate is None:
            blocked = BlockedNumber.query.filter_by(
                room_id=period.room_id, bet_type=bet_type, number=raw_number
            ).first()
        limit = get_partner_member_limit(user.partner, user) if user.partner_id else None
        if limit and amount < limit.min_bet:
            raise ValueError(f"ยอดแทงขั้นต่ำของคุณคือ {limit.min_bet:,.2f} เครดิต")
        if limit and amount > limit.max_bet:
            raise ValueError(f"ยอดแทงสูงสุดต่อรายการคือ {limit.max_bet:,.2f} เครดิต")
        if limit and amount > limit.max_number_bet:
            raise ValueError(f"ยอดแทงสูงสุดต่อเลขคือ {limit.max_number_bet:,.2f} เครดิต")

        if blocked_rate is not None and blocked_rate > 0:
            rate = blocked_rate
        elif blocked and blocked.payout_multiplier > 0:
            rate = blocked.payout_multiplier
        else:
            personal_rate = group_payout if group_payout is not None else member_specific_rate(user, bet_type)
            rate = personal_rate if personal_rate is not None else rates[bet_type]
            if group_payout is not None and rate == group_payout:
                payout_excess = max(0.0, group_payout - rates[bet_type])
                payout_grantor = entry["payout_owner"] if payout_excess > 0 else None

        total_reward = int(amount * rate)
        adjust_credit(user, -(amount - discount), f"แทงหวยรัฐบาล งวด {period.period_date} ({bet_type}: {raw_number})")

        new_bet = ThaiLotteryBet(
            user_id=user.id,
            period_id=period.id,
            bet_type=bet_type,
            number=raw_number,
            amount=amount,
            discount_amount=discount,
            payout_grantor_id=payout_grantor,
            payout_excess=payout_excess,
            discount_grantor_id=entry["discount_owner"] if entry["discount_excess_pct"] > 0 else None,
            discount_excess_pct=entry["discount_excess_pct"],
            remark=(remark or "").strip()[:50] or None,
            rate=rate,
            ticket_code=ticket_code,
            reward_amount=total_reward,
            status="pending"
        )
        db.session.add(new_bet)
        db.session.flush()
        create_partner_commission(new_bet)
        create_agent_upline_commissions(new_bet)
        create_senior_commission(new_bet)
        notify_user(user, "ส่งโพยสำเร็จ", f"เลข {raw_number} ({bet_type}) ใช้ {amount:,} เครดิต", "bet")
        created += 1

    if created:
        db.session.commit()
    return created


def adjust_points(user: User, change: int, reason: str, admin: User | None = None):
    """เพิ่ม/ลดแต้ม พร้อมบันทึก log"""
    user.points = max(0, user.points + change)
    refresh_vip_status(user)
    record_wallet_transaction(user, "points", change, user.points, reason, admin=admin)
    log = PointLog(
        user_id=user.id,
        admin_id=admin.id if admin else None,
        change=change,
        balance_after=user.points,
        reason=reason,
    )
    db.session.add(log)
    return log


def adjust_credit(user: User, change: float, reason: str, admin: User | None = None):
    """เพิ่ม/ลดเครดิตสำหรับการเดิมพัน แยกจากแต้มสะสม"""
    user.credit_balance = max(0.0, float(user.credit_balance) + float(change))
    record_wallet_transaction(user, "credit", change, user.credit_balance, reason, admin=admin)
    return user.credit_balance


def get_partner_member_limit(user, member):
    """user = Agent ที่ดูแลสมาชิกคนนี้โดยตรง, member = สมาชิกที่กำลังจะแทง
    รวมวงเงินของ Agent ตัวเอง กับของ Senior บนสุดของสาย (ถ้ามี) โดยเอาค่าที่
    "เข้มงวดที่สุด" ของแต่ละช่องชนะ (ขั้นต่ำเอาค่ามาก, สูงสุดเอาค่าน้อย) — ไม่มีการ
    ไล่ระดับ Agent ระหว่างทาง (Agent ย่อยของ Agent) เพราะของเดิมก็ไม่เคยรองรับ
    เฉพาะ Agent ตรง + Senior บนสุดเท่านั้นที่ตั้งวงเงินแบบนี้ได้"""
    if not user or not member or not user.is_partner or member.partner_id != user.id:
        return None
    agent_limit = PartnerMemberLimit.query.filter_by(
        partner_id=user.id, member_id=member.id
    ).first()
    senior = agent_upline_senior(user)
    senior_limit = SeniorMemberLimit.query.filter_by(
        senior_id=senior.id, member_id=member.id
    ).first() if senior else None
    if not agent_limit and not senior_limit:
        return None
    if agent_limit and not senior_limit:
        return agent_limit
    if senior_limit and not agent_limit:
        return senior_limit
    return SimpleNamespace(
        min_bet=max(agent_limit.min_bet, senior_limit.min_bet),
        max_bet=min(agent_limit.max_bet, senior_limit.max_bet),
        max_number_bet=min(agent_limit.max_number_bet, senior_limit.max_number_bet),
    )


def member_specific_rate(user, bet_type):
    """เรทพิเศษเฉพาะสมาชิกคนนี้คนเดียว — Agent ตรงตั้งไว้ก่อน ถ้าไม่มีค่อยเช็คของ
    Senior บนสุดของสาย เอาค่าที่เจอก่อนเลย ไม่รวมกัน (เป็นอัตราจ่าย ไม่ใช่วงเงิน)
    ใช้เป็นชั้นรองจาก "เลขอั้น" เสมอ — ถ้าเลขนั้นถูกอั้นไว้ เลขอั้นชนะเสมอไม่ว่าจะตั้ง
    เรทพิเศษเฉพาะคนไว้หรือไม่ (ดู add_thai_lottery_bets)"""
    if not user or not user.partner_id:
        return None
    agent_rate = PartnerMemberRate.query.filter_by(
        partner_id=user.partner_id, member_id=user.id, bet_type=bet_type
    ).first()
    if agent_rate:
        return agent_rate.payout_multiplier
    agent = user.partner
    senior = agent_upline_senior(agent) if agent else None
    if senior:
        senior_rate = SeniorMemberRate.query.filter_by(
            senior_id=senior.id, member_id=user.id, bet_type=bet_type
        ).first()
        if senior_rate:
            return senior_rate.payout_multiplier
    return None


def partner_room_is_enabled(user, room_id):
    """user = สมาชิกที่กำลังจะแทง — เช็คทั้งการตั้งค่าของ Agent ตรง (user.partner_id)
    และของ Senior บนสุดของสาย ถ้าฝั่งไหนปิดห้องนี้ไว้ ถือว่าปิด (เข้มงวดที่สุดชนะ)"""
    if not user or not user.partner_id or not room_id:
        return True
    setting = PartnerRoomSetting.query.filter_by(
        partner_id=user.partner_id, room_id=room_id
    ).first()
    if setting is not None and not setting.is_enabled:
        return False
    agent = user.partner
    senior = agent_upline_senior(agent) if agent else None
    if senior:
        senior_setting = SeniorRoomSetting.query.filter_by(senior_id=senior.id, room_id=room_id).first()
        if senior_setting is not None and not senior_setting.is_enabled:
            return False
    return True


def get_partner_payout_rates(partner):
    if not partner:
        return {}
    return {
        rule.bet_type: float(rule.payout_multiplier)
        for rule in PartnerPayoutRule.query.filter_by(partner_id=partner.id).all()
    }


def get_senior_payout_rates(senior):
    if not senior:
        return {}
    return {
        rule.bet_type: float(rule.payout_multiplier)
        for rule in SeniorPayoutRule.query.filter_by(senior_id=senior.id).all()
    }


def partner_blocked_rate(user, room_id, bet_type, number):
    """user = สมาชิกที่กำลังจะแทง — เช็คเลขอั้นของ Agent ตรงก่อน ถ้าไม่เจอค่อยเช็ค
    ของ Senior บนสุดของสาย (ใครตั้งไว้ก่อนใช้ค่านั้น ไม่รวมกัน เพราะเป็นอัตราจ่าย
    ไม่ใช่วงเงิน จะ "รวม" แบบ min/max ไม่ได้)"""
    if not user or not user.partner_id:
        return None
    blocked = PartnerBlockedNumber.query.filter_by(
        partner_id=user.partner_id, room_id=room_id,
        bet_type=bet_type, number=number,
    ).first()
    if blocked:
        return blocked.payout_multiplier
    agent = user.partner
    senior = agent_upline_senior(agent) if agent else None
    if senior:
        senior_blocked = SeniorBlockedNumber.query.filter_by(
            senior_id=senior.id, room_id=room_id, bet_type=bet_type, number=number,
        ).first()
        if senior_blocked:
            return senior_blocked.payout_multiplier
    return None


def touch_partner_presence(user):
    if not user or user.role != "member" or not user.partner_id:
        return
    presence = PartnerPresence.query.filter_by(member_id=user.id).first()
    if presence is None:
        presence = PartnerPresence(partner_id=user.partner_id, member_id=user.id)
        db.session.add(presence)
    else:
        presence.partner_id = user.partner_id
        presence.last_seen_at = datetime.now()
    db.session.commit()


@app.before_request
def track_partner_member_presence():
    user = current_user()
    if user and user.role == "member" and user.partner_id:
        touch_partner_presence(user)


# ==========================================================
# ROUTES — หน้าร้าน (Public / Member)
# ==========================================================
@app.route("/")
def index():
    if current_user():
        return redirect(url_for("lottery_rooms"))

    keyword = request.args.get("q", "").strip()
    query = Reward.query.filter_by(is_active=True)
    if keyword:
        query = query.filter(Reward.name.contains(keyword))
    rewards = query.order_by(Reward.points_required.asc()).all()
    
    banners = HeroBanner.query.filter_by(is_active=True).order_by(HeroBanner.created_at.desc()).all()
    if not app.config["POINTS_REWARDS_ENABLED"]:
        reward_terms = ("แต้ม", "แลก", "รางวัล")
        banners = [
            banner for banner in banners
            if not any(term in (banner.title or "") for term in reward_terms)
        ]
    if not banners:
        uploaded_banner_names = sorted(
            (name for name in os.listdir(UPLOAD_FOLDER) if name.startswith("banner_")),
            reverse=True,
        )
        banners = [
            {"image_url": f"/static/uploads/{name}", "title": ""}
            for name in uploaded_banner_names[:5]
        ]
    featured_rooms = active_lottery_rooms().limit(6).all()
    featured_periods = {}
    for room in featured_rooms:
        featured_periods[room.id] = ThaiLotteryPeriod.query.filter_by(room_id=room.id).order_by(ThaiLotteryPeriod.id.desc()).first()
    
    return render_template("index.html", rewards=rewards, banners=banners, keyword=keyword, featured_rooms=featured_rooms, featured_periods=featured_periods)


@app.route("/results")
def lottery_results():
    rooms = active_lottery_rooms().all()
    latest_periods = {}
    grouped_results = {}
    for room in rooms:
        period = ThaiLotteryPeriod.query.filter_by(room_id=room.id).order_by(ThaiLotteryPeriod.id.desc()).first()
        latest_periods[room.id] = period
        category_name = room.category_ref.name if room.category_ref else (room.category or "อื่นๆ")
        grouped_results.setdefault(category_name, []).append(room)
    return render_template("lottery_results.html", rooms=rooms, grouped_results=grouped_results, latest_periods=latest_periods)


@app.route("/results/<int:room_id>")
def lottery_result_detail(room_id):
    room = db.session.get(LotteryRoom, room_id)
    if not room or not room.is_active or room.api_category == "liw" or room.category == "หวย LIW":
        flash("ไม่พบห้องหวยนี้", "error")
        return redirect(url_for("lottery_results"))
    page = request.args.get("page", 1, type=int)
    pagination = ThaiLotteryPeriod.query.filter_by(room_id=room.id).order_by(ThaiLotteryPeriod.id.desc()).paginate(page=page, per_page=10, error_out=False)
    periods = pagination.items
    latest_period = periods[0] if periods else None
    return render_template("lottery_result_detail.html", room=room, periods=periods, latest_period=latest_period, pagination=pagination)


def _grouped_rate_sets():
    """{หมวด: {bet_type: LotteryRateSet}} ของชุดที่ 2 — ใช้แสดงในหน้าแอดมิน"""
    grouped = {}
    for row in LotteryRateSet.query.filter_by(tier=2).order_by(LotteryRateSet.category):
        grouped.setdefault(row.category, {})[row.bet_type] = row
    return grouped


@app.route("/payout-rates")
def payout_rates_page():
    """อัตราจ่ายของทุกหมวดหมู่หวย (ชุดเริ่มต้น + ชุดที่ 2) — สมาชิกที่ล็อกอินเห็นค่าที่ตัวเองได้จริง"""
    user = current_user()
    categories = []
    for room in active_lottery_rooms().all():
        name = room_category_name(room) or "อื่นๆ"
        if name not in categories:
            categories.append(name)
    class _Anon:  # ผู้ใช้ที่ยังไม่ล็อกอิน = ไม่มี Agent/เรทเฉพาะราย
        partner_id = None
    who = user or _Anon()
    sections = [
        {"category": name, "tables": build_rate_tables(who, name, 1, MAX_BET_AMOUNT)}
        for name in categories
    ]
    return render_template("payout_rates.html", sections=sections)


@app.route("/rules")
def rules_page():
    return render_template("rules.html")


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


@app.route("/affiliate-guide")
def affiliate_guide_page():
    return render_template("affiliate_guide.html")


@app.route("/contact")
def contact_page():
    return render_template("contact.html", admin_contact_url=get_setting("admin_contact_url"))


@app.route("/lottery/rooms")
@login_required
def lottery_rooms():
    rooms = active_lottery_rooms().all()
    close_expired_periods()
    now = app_now()
    room_schedules = {}
    room_groups = {}
    for room in rooms:
        period = ThaiLotteryPeriod.query.filter(
            ThaiLotteryPeriod.room_id == room.id,
            ThaiLotteryPeriod.is_open.is_(True),
            ThaiLotteryPeriod.close_time > now,
            or_(ThaiLotteryPeriod.open_time.is_(None), ThaiLotteryPeriod.open_time <= now),
        ).order_by(ThaiLotteryPeriod.id.desc()).first()
        if period is None:
            period = ThaiLotteryPeriod.query.filter(
                ThaiLotteryPeriod.room_id == room.id,
                ThaiLotteryPeriod.is_open.is_(True),
                ThaiLotteryPeriod.open_time > now,
                ThaiLotteryPeriod.close_time > now,
            ).order_by(ThaiLotteryPeriod.open_time.asc()).first()
        room_schedules[room.id] = period
        category_name = room.category_ref.name if room.category_ref else (room.category or "อื่นๆ")
        room_groups.setdefault(category_name, []).append(room)
    return render_template(
        "lottery_rooms.html",
        rooms=rooms,
        room_groups=room_groups,
        room_schedules=room_schedules,
        now=now,
    )


@app.route("/lottery/thai", methods=["GET", "POST"])
@login_required
def lottery_thai():
    user = current_user()
    room_id = request.args.get("room_id", type=int) or request.form.get("room_id", type=int)
    room = db.session.get(LotteryRoom, room_id) if room_id else None
    if room is None:
        room = active_lottery_rooms().first()
    if room is None or not room.is_active or room.api_category == "liw" or room.category == "หวย LIW":
        flash("ขณะนี้ยังไม่มีห้องหวยเปิดให้บริการ", "error")
        return redirect(url_for("lottery_rooms"))

    close_expired_periods(room.id)
    now = app_now()
    active_period = ThaiLotteryPeriod.query.filter(
        ThaiLotteryPeriod.room_id == room.id,
        ThaiLotteryPeriod.is_open.is_(True),
        or_(ThaiLotteryPeriod.open_time.is_(None), ThaiLotteryPeriod.open_time <= now),
        ThaiLotteryPeriod.close_time > now,
    ).order_by(ThaiLotteryPeriod.id.desc()).first()
    room_url = url_for("lottery_thai", room_id=room.id)

    if request.method == "POST":
        if not active_period:
            flash("ขณะนี้ยังไม่มีงวดเปิดรับแทง", "error")
            return redirect(room_url)

        bet_types = request.form.getlist("bet_types[]")
        numbers = request.form.getlist("numbers[]")
        amounts = request.form.getlist("amounts[]")
        rate_tier = request.form.get("rate_tier", 1, type=int)
        remark = request.form.get("remark", "")

        if bet_types or numbers or amounts:
            entries = []
            for idx, bet_type in enumerate(bet_types):
                entries.append({
                    "bet_type": bet_type,
                    "number": numbers[idx] if idx < len(numbers) else "",
                    "amount": amounts[idx] if idx < len(amounts) else 0,
                })
            if not entries:
                flash("กรุณาเลือกประเภทและเลขที่ต้องการแทง", "error")
                return redirect(room_url)
            try:
                created = add_thai_lottery_bets(user, active_period, entries, rate_tier=rate_tier, remark=remark)
                if created:
                    flash(f"ส่งโพยหวยสำเร็จ {created} รายการ", "success")
                else:
                    flash("ไม่พบรายการแทงที่ถูกต้อง", "error")
                return redirect(room_url)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(room_url)

        bet_type = request.form.get("bet_type")
        number = request.form.get("number", "").strip()
        try:
            amount = int(request.form.get("amount", 0))
        except ValueError:
            amount = 0

        if not number or amount <= 0:
            flash("กรุณากรอกตัวเลขและจำนวนเครดิตให้ถูกต้อง", "error")
            return redirect(room_url)

        try:
            created = add_thai_lottery_bets(user, active_period, [{"bet_type": bet_type, "number": number, "amount": amount}], rate_tier=rate_tier, remark=remark)
            if created:
                flash(f"ส่งโพยหวยสำเร็จ! เลข {number} ({bet_type}) จำนวน {amount:,} เครดิต", "success")
            else:
                flash("ประเภทการแทงไม่ถูกต้อง", "error")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(room_url)

    # บิลล่าสุดของสมาชิกในห้องนี้ — จัดกลุ่มตามโพย (ticket_code) เอา 15 ใบล่าสุด
    recent_bets = ThaiLotteryBet.query.join(ThaiLotteryPeriod).filter(
        ThaiLotteryBet.user_id == user.id, ThaiLotteryPeriod.room_id == room.id
    ).order_by(ThaiLotteryBet.created_at.desc(), ThaiLotteryBet.id.desc()).limit(600).all()
    tickets = {}
    for bet in recent_bets:
        code = bet.ticket_code or f"bet-{bet.id}"
        if code not in tickets:
            if len(tickets) >= 15:
                continue
            tickets[code] = {
                "code": bet.ticket_code,
                "period_id": bet.period_id,
                "time": (bet.created_at + timedelta(hours=7)).strftime("%d/%m %H:%M") if bet.created_at else "-",
                "count": 0,
                "amount": 0,
                "pending": 0,
                "remark": bet.remark or "",
            }
        ticket = tickets[code]
        if bet.status == "cancelled":
            continue
        ticket["count"] += 1
        ticket["amount"] += bet.amount
        if bet.status == "pending":
            ticket["pending"] += 1
    period_by_id = {p.id: p for p in ThaiLotteryPeriod.query.filter(
        ThaiLotteryPeriod.id.in_({t["period_id"] for t in tickets.values()})
    )} if tickets else {}
    for ticket in tickets.values():
        period = period_by_id.get(ticket["period_id"])
        ticket["can_cancel"] = bool(
            ticket["code"] and ticket["pending"] and period and period.is_open
            and period.close_time > now
        )

    # ผลรางวัล 5 งวดล่าสุดที่ออกแล้ว
    result_rows = []
    for p in ThaiLotteryPeriod.query.filter(
        ThaiLotteryPeriod.room_id == room.id, ThaiLotteryPeriod.result_3up.isnot(None)
    ).order_by(ThaiLotteryPeriod.id.desc()).limit(5):
        result_rows.append({
            "date": p.period_date,
            "up3": p.result_3up,
            "up2": p.result_3up[-2:] if p.result_3up else "",
            "down2": p.result_2down or "",
            "down3": p.result_3back or "",
        })

    # อัตราจ่ายที่สมาชิกคนนี้ได้จริงของแต่ละชุด (ฐาน/ชุดของหมวด + Senior/Agent + เรทเฉพาะรายคน) และวงเงินต่อรายการ
    limit = get_partner_member_limit(user.partner, user) if user.partner_id else None
    member_min = int(limit.min_bet) if limit and limit.min_bet > 1 else 1
    member_max = int(min(limit.max_bet, MAX_BET_AMOUNT)) if limit else MAX_BET_AMOUNT
    type_rules = get_type_rules()
    category = room_category_name(room)
    group_blocked = not member_group_enabled(user, category)
    if group_blocked:
        active_period = None  # กลุ่มหวยนี้ถูกผู้ดูแลปิดไว้ — แสดงเหมือนไม่มีงวดเปิดรับ
    tier_tables = build_rate_tables(user, category, member_min, member_max, type_rules)
    my_rates = {row["key"]: row["rate"] for row in tier_tables[0]["rows"]}

    blocked_labels = BET_TYPE_LABELS
    blocked_rates = {}
    for item in room.blocked_numbers:
        label = blocked_labels.get(item.bet_type)
        if label:
            blocked_rates.setdefault(label, {})[item.number] = item.payout_multiplier

    return render_template(
        "lottery_thai.html",
        room=room,
        active_period=active_period,
        tickets=list(tickets.values()),
        result_rows=result_rows,
        payout_rates=my_rates,
        blocked_rates=blocked_rates,
        tier_tables=tier_tables,
        group_blocked=group_blocked,
        max_entries=MAX_BET_ENTRIES,
        remaining_seconds=max(0, int((active_period.close_time - now).total_seconds())) if active_period else 0,
    )


@app.route("/lotto/room/<int:room_id>/period/<int:period_id>/submit", methods=["POST"])
@login_required
def submit_lotto_action(room_id, period_id):
    """รองรับการส่งโพยหวยจากหน้า HTML ที่เรียกใช้ url_for('submit_lotto_action', ...)"""
    user = current_user()
    room = db.session.get(LotteryRoom, room_id)
    period = db.session.get(ThaiLotteryPeriod, period_id)
    close_expired_periods(room_id)

    if not room or not period or period.room_id != room.id or not period.is_open:
        flash("ขณะนี้ยังไม่มีงวดเปิดรับแทง หรือไม่พบงวดนี้", "error")
        return redirect(url_for("lottery_thai", room_id=room_id))

    bet_types = request.form.getlist("bet_types[]")
    numbers = request.form.getlist("numbers[]")
    amounts = request.form.getlist("amounts[]")

    if bet_types or numbers or amounts:
        entries = []
        for idx, bet_type in enumerate(bet_types):
            entries.append({
                "bet_type": bet_type,
                "number": numbers[idx] if idx < len(numbers) else "",
                "amount": amounts[idx] if idx < len(amounts) else 0,
            })
        try:
            created = add_thai_lottery_bets(user, period, entries)
            if created:
                flash(f"ส่งโพยหวยสำเร็จ {created} รายการ", "success")
            else:
                flash("ไม่พบรายการแทงที่ถูกต้อง", "error")
            return redirect(url_for("lottery_thai", room_id=room.id))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("lottery_thai", room_id=room.id))

    bet_type = request.form.get("bet_type")
    number = request.form.get("number", "").strip()
    try:
        amount = int(request.form.get("amount", 0))
    except ValueError:
        amount = 0

    if not number or amount <= 0:
        flash("กรุณากรอกตัวเลขและจำนวนเครดิตให้ถูกต้อง", "error")
        return redirect(url_for("lottery_thai", room_id=room.id))

    try:
        created = add_thai_lottery_bets(user, period, [{"bet_type": bet_type, "number": number, "amount": amount}])
        if created:
            flash(f"ส่งโพยหวยสำเร็จ! เลข {number} ({bet_type}) จำนวน {amount:,} เครดิต", "success")
        else:
            flash("ประเภทการแทงไม่ถูกต้อง", "error")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("lottery_thai", room_id=room.id))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        phone = request.form.get("phone", "").strip()
        invite_code = request.form.get("invite_code", "").strip().upper()
        partner_profile = PartnerProfile.query.filter_by(
            invite_code=invite_code, status="active"
        ).first() if invite_code else None

        if len(username) < 4 or not re.fullmatch(ACCOUNT_TEXT_PATTERN, username):
            flash("ชื่อผู้ใช้ต้องมีอย่างน้อย 4 ตัว และใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษเท่านั้น", "error")
        elif len(password) < 6:
            flash("รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร", "error")
        elif not re.fullmatch(ACCOUNT_TEXT_PATTERN, password):
            flash("รหัสผ่านต้องใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษเท่านั้น", "error")
        elif password != confirm:
            flash("รหัสผ่านทั้งสองช่องไม่ตรงกัน", "error")
        elif User.query.filter(func.lower(User.username) == username.lower()).first():
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        elif invite_code and not partner_profile:
            flash("ไม่พบรหัสแนะนำ หรือรหัสนี้ถูกปิดใช้งาน", "error")
        else:
            user = User(
                username=username, phone=phone,
                points=0, credit_balance=0.0,
                partner_id=partner_profile.user_id if partner_profile else None,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("สมัครสมาชิกสำเร็จ! เข้าสู่ระบบได้เลย", "success")
            return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("lottery_rooms"))

    if request.method == "POST":
        ensure_default_admin_accounts()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not re.fullmatch(ACCOUNT_TEXT_PATTERN, username) or not re.fullmatch(ACCOUNT_TEXT_PATTERN, password):
            flash("ชื่อผู้ใช้และรหัสผ่านต้องใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษเท่านั้น", "error")
            return render_template("login.html")
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            if not user.is_active:
                flash("บัญชีนี้ถูกระงับการใช้งาน", "error")
                return render_template("login.html")
            session["user_id"] = user.id
            session.permanent = True
            db.session.add(LoginHistory(
                user_id=user.id,
                ip_address=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
                user_agent=request.headers.get("User-Agent", "")[:255],
            ))
            db.session.commit()
            flash(f"ยินดีต้อนรับ {user.username}", "success")
            return redirect(url_for("lottery_rooms"))

        flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("ออกจากระบบเรียบร้อย", "success")
    return redirect(url_for("index"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    user = current_user()
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not user.check_password(current_password):
            flash("รหัสผ่านเดิมไม่ถูกต้อง", "error")
        elif len(new_password) < 6 or not re.fullmatch(ACCOUNT_TEXT_PATTERN, new_password):
            flash("รหัสผ่านใหม่ต้องใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษ และมีอย่างน้อย 6 ตัว", "error")
        elif new_password != confirm_password:
            flash("รหัสผ่านใหม่และการยืนยันไม่ตรงกัน", "error")
        elif new_password == current_password:
            flash("รหัสผ่านใหม่ต้องแตกต่างจากรหัสผ่านเดิม", "error")
        else:
            user.set_password(new_password)
            db.session.commit()
            flash("เปลี่ยนรหัสผ่านเรียบร้อยแล้ว", "success")
            return redirect(url_for("change_password"))
    return render_template("change_password.html")


@app.route("/redeem/<int:reward_id>", methods=["POST"])
@login_required
@points_rewards_required
def redeem(reward_id):
    user = current_user()
    reward = db.session.get(Reward, reward_id)

    if not reward or not reward.is_active:
        flash("ไม่พบของรางวัลชิ้นนี้", "error")
        return redirect(url_for("index"))

    if reward.stock <= 0:
        flash(f"ขออภัย '{reward.name}' หมดสต็อกแล้ว", "error")
        return redirect(url_for("index"))

    if user.points < reward.points_required:
        need = reward.points_required - user.points
        flash(f"แต้มไม่พอ ต้องการอีก {need:,} แต้ม", "error")
        return redirect(url_for("index"))

    code = generate_redeem_code()
    adjust_points(user, -reward.points_required, f"แลกของรางวัล: {reward.name}")
    reward.stock -= 1

    record = RedemptionHistory(
        user_id=user.id,
        reward_id=reward.id,
        reward_name=reward.name,
        points_used=reward.points_required,
        redeem_code=code,
        status="pending",
    )
    db.session.add(record)
    notify_user(user, "แลกของรางวัลสำเร็จ", f"แลก {reward.name} ใช้ {reward.points_required:,} แต้ม", "reward")
    db.session.commit()

    flash(f"แลกสำเร็จ! รหัสรับของรางวัลของคุณคือ {code}", "success")
    return redirect(url_for("history"))


def _bet_history_range(args):
    """ช่วงวันที่ (เวลาไทย) ของหน้ารายการแทง: today / yesterday / week / lastweek / month / กำหนดเอง"""
    today = app_now().date()
    key = args.get("range", "month")
    start_arg, end_arg = args.get("start", "").strip(), args.get("end", "").strip()
    if start_arg or end_arg:
        try:
            start = datetime.strptime(start_arg, "%Y-%m-%d").date() if start_arg else today.replace(day=1)
            end = datetime.strptime(end_arg, "%Y-%m-%d").date() if end_arg else today
            return "custom", min(start, end), max(start, end)
        except ValueError:
            pass
    if key == "today":
        return key, today, today
    if key == "yesterday":
        day = today - timedelta(days=1)
        return key, day, day
    monday = today - timedelta(days=today.weekday())
    if key == "week":
        return key, monday, monday + timedelta(days=6)
    if key == "lastweek":
        return key, monday - timedelta(days=7), monday - timedelta(days=1)
    return "month", today.replace(day=1), today


@app.route("/history")
@login_required
def history():
    user = current_user()
    range_key, start_day, end_day = _bet_history_range(request.args)
    # created_at เก็บเป็นเวลา UTC — ช่วงวันของผู้ใช้เป็นเวลาไทย (UTC+7)
    start_utc = datetime.combine(start_day, datetime.min.time()) - timedelta(hours=7)
    end_utc = datetime.combine(end_day + timedelta(days=1), datetime.min.time()) - timedelta(hours=7)
    bets = ThaiLotteryBet.query.filter(
        ThaiLotteryBet.user_id == user.id,
        ThaiLotteryBet.created_at >= start_utc,
        ThaiLotteryBet.created_at < end_utc,
    ).order_by(ThaiLotteryBet.created_at.desc(), ThaiLotteryBet.id.desc()).all()

    tickets = {}
    for bet in bets:
        key = bet.ticket_code or f"legacy-{bet.period_id}-{bet.id}"
        ticket = tickets.setdefault(key, {
            "code": bet.ticket_code, "period": bet.period, "time": bet.created_at + timedelta(hours=7),
            "stake": 0, "discount": 0.0, "reward": 0, "count": 0, "statuses": set(), "remark": bet.remark or "",
        })
        ticket["statuses"].add(bet.status)
        if bet.status == "cancelled":
            continue
        ticket["count"] += 1
        ticket["stake"] += bet.amount
        ticket["discount"] += bet.discount_amount
        if bet.status == "win":
            ticket["reward"] += bet.reward_amount
    now = app_now()
    rows = []
    for ticket in tickets.values():
        statuses = ticket["statuses"]
        if statuses == {"cancelled"}:
            ticket["state"] = "cancelled"
        elif "pending" in statuses:
            ticket["state"] = "pending"
        elif "win" in statuses:
            ticket["state"] = "win"
        else:
            ticket["state"] = "lose"
        ticket["paid"] = ticket["stake"] - ticket["discount"]
        ticket["profit"] = ticket["reward"] - ticket["paid"]
        period = ticket["period"]
        ticket["can_cancel"] = bool(
            ticket["code"] and ticket["state"] == "pending" and period and period.is_open
            and period.close_time > now
        )
        rows.append(ticket)
    settled = [t for t in rows if t["state"] in ("win", "lose")]
    totals = {
        "stake": sum(t["stake"] for t in rows if t["state"] != "cancelled"),
        "discount": sum(t["discount"] for t in rows if t["state"] != "cancelled"),
        "reward": sum(t["reward"] for t in rows),
        "profit": sum(t["profit"] for t in settled),
    }
    return render_template(
        "history.html", tickets=rows, totals=totals, range_key=range_key,
        start_day=start_day, end_day=end_day,
    )


@app.route("/lottery/ticket/<int:period_id>")
@app.route("/lottery/ticket/<int:period_id>/<string:ticket_code>")
@login_required
def lottery_ticket(period_id, ticket_code=None):
    user = current_user()
    period = db.session.get(ThaiLotteryPeriod, period_id)
    if not period:
        abort(404)

    query = ThaiLotteryBet.query.filter_by(user_id=user.id, period_id=period.id)
    if ticket_code:
        query = query.filter_by(ticket_code=ticket_code)
    bets = query.order_by(ThaiLotteryBet.created_at.asc(), ThaiLotteryBet.id.asc()).all()
    if not bets:
        abort(404)

    refundable = bool(
        ticket_code
        and period.is_open
        and period.close_time
        and period.close_time > app_now()
        and all(bet.status == "pending" for bet in bets)
    )

    return render_template(
        "lottery_ticket.html",
        period=period,
        bets=bets,
        total_amount=sum(bet.amount for bet in bets),
        total_discount=sum(bet.discount_amount for bet in bets),
        total_reward=sum(bet.reward_amount for bet in bets if bet.status == "win"),
        ticket_code=ticket_code,
        refundable=refundable,
        is_admin_view=False,
    )


@app.route("/lottery/ticket/<int:period_id>/<string:ticket_code>/download")
@login_required
def download_lottery_ticket(period_id, ticket_code):
    """โหลดโพยเป็นไฟล์ CSV (เปิดใน Excel ได้ ภาษาไทยไม่เพี้ยน) เพื่อเก็บเป็นหลักฐาน"""
    user = current_user()
    period = db.session.get(ThaiLotteryPeriod, period_id)
    bets = ThaiLotteryBet.query.filter_by(
        user_id=user.id, period_id=period_id, ticket_code=ticket_code
    ).order_by(ThaiLotteryBet.id.asc()).all() if period else []
    if not bets:
        abort(404)
    status_text = {"pending": "รอผล", "win": "ถูกรางวัล", "lose": "ไม่ถูกรางวัล", "cancelled": "ยกเลิก"}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["ตลาด", "งวด", "รหัสโพย", "เวลาแทง", "ประเภท", "เลข", "ยอดแทง", "ส่วนลด", "อัตราจ่าย", "สถานะ", "เงินรางวัล", "หมายเหตุ"])
    for bet in bets:
        writer.writerow([
            period.room.name if period.room else "", period.period_date, ticket_code,
            (bet.created_at + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S"),
            BET_TYPE_LABELS.get(bet.bet_type, bet.bet_type), bet.number, bet.amount, f"{bet.discount_amount:.2f}",
            f"{bet.rate:g}", status_text.get(bet.status, bet.status),
            bet.reward_amount if bet.status == "win" else 0, bet.remark or "",
        ])
    return Response(
        chr(0xFEFF) + out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ticket-{ticket_code}.csv"'},
    )


@app.route("/lottery/ticket/<int:period_id>/<string:ticket_code>/cancel", methods=["POST"])
@login_required
def cancel_lottery_ticket(period_id, ticket_code):
    user = current_user()
    period = db.session.get(ThaiLotteryPeriod, period_id)
    if not period or not period.is_open or not period.close_time or period.close_time <= app_now():
        abort(404)

    bets = ThaiLotteryBet.query.filter_by(
        user_id=user.id, period_id=period.id, ticket_code=ticket_code, status="pending"
    ).all()
    if not bets:
        abort(404)

    refund_amount = sum(bet.amount - bet.discount_amount for bet in bets)
    for bet in bets:
        bet.status = "cancelled"
    adjust_credit(user, refund_amount, f"คืนโพยหวย {ticket_code}")
    db.session.commit()
    flash("คืนโพยเรียบร้อยแล้ว", "success")
    return redirect(url_for("lottery_ticket", period_id=period.id, ticket_code=ticket_code))


@app.route("/admin/lottery-tickets")
@admin_required
def admin_lottery_tickets():
    period_id = request.args.get("period_id", type=int)
    user_id = request.args.get("user_id", type=int)
    status = request.args.get("status", "").strip()
    query = ThaiLotteryBet.query.join(User).join(ThaiLotteryPeriod)
    if period_id:
        query = query.filter(ThaiLotteryBet.period_id == period_id)
    if user_id:
        query = query.filter(ThaiLotteryBet.user_id == user_id)
    if status in {"pending", "win", "lose"}:
        query = query.filter(ThaiLotteryBet.status == status)

    bets = query.order_by(ThaiLotteryBet.created_at.desc(), ThaiLotteryBet.id.desc()).limit(500).all()
    periods = ThaiLotteryPeriod.query.order_by(ThaiLotteryPeriod.id.desc()).all()
    users = User.query.filter(User.role != "admin").order_by(User.username.asc()).all()
    return render_template(
        "admin_lottery_tickets.html",
        bets=bets,
        periods=periods,
        users=users,
        selected_period_id=period_id,
        selected_user_id=user_id,
        selected_status=status,
    )


@app.route("/admin/lottery-tickets/<int:period_id>/<int:user_id>")
@app.route("/admin/lottery-tickets/<int:period_id>/<int:user_id>/<string:ticket_code>")
@admin_required
def admin_lottery_ticket(period_id, user_id, ticket_code=None):
    period = db.session.get(ThaiLotteryPeriod, period_id)
    user = db.session.get(User, user_id)
    if not period or not user:
        abort(404)
    query = ThaiLotteryBet.query.filter_by(user_id=user.id, period_id=period.id)
    if ticket_code:
        query = query.filter_by(ticket_code=ticket_code)
    bets = query.order_by(ThaiLotteryBet.created_at.asc(), ThaiLotteryBet.id.asc()).all()
    if not bets:
        abort(404)
    return render_template(
        "lottery_ticket.html",
        period=period,
        ticket_user=user,
        bets=bets,
        total_amount=sum(bet.amount for bet in bets),
        total_discount=sum(bet.discount_amount for bet in bets),
        total_reward=sum(bet.reward_amount for bet in bets if bet.status == "win"),
        is_admin_view=True,
    )


@app.route("/notifications", methods=["GET", "POST"])
@login_required
def notifications():
    user = current_user()
    if request.method == "POST":
        Notification.query.filter_by(user_id=user.id, is_read=False).update(
            {Notification.is_read: True}, synchronize_session=False
        )
        db.session.commit()
        flash("ทำเครื่องหมายการแจ้งเตือนว่าอ่านแล้ว", "success")
        return redirect(url_for("notifications"))
    items = Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc()).limit(100).all()
    return render_template("notifications.html", notifications=items)


@app.route("/responsible-play", methods=["GET", "POST"])
@login_required
def responsible_play():
    user = current_user()
    profile = get_responsible_profile(user)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "save_settings":
            limit_raw = request.form.get("daily_bet_limit", "").strip()
            try:
                profile.daily_bet_limit = float(limit_raw) if limit_raw else None
            except ValueError:
                flash("วงเงินต่อวันไม่ถูกต้อง", "error")
                return redirect(url_for("responsible_play"))
            profile.age_verified = request.form.get("age_verified") == "on"
            db.session.commit()
            flash("บันทึกการตั้งค่าการเล่นแล้ว", "success")
        elif action == "self_exclude":
            try:
                days = max(1, min(365, int(request.form.get("days", 1))))
            except ValueError:
                days = 1
            profile.self_excluded_until = datetime.now() + timedelta(days=days)
            db.session.commit()
            flash(f"พักการเล่น {days} วันแล้ว", "success")
        return redirect(url_for("responsible_play"))
    return render_template("responsible_play.html", profile=profile)


@app.route("/bank-accounts", methods=["GET", "POST"])
@login_required
def bank_accounts():
    user = current_user()
    if not user.is_admin:
        flash("ระบบฝาก-ถอนปิดให้บริการชั่วคราว กรุณาติดต่อแอดมินเพื่อเพิ่มเครดิต", "info")
        return redirect(url_for("lottery_rooms"))
    catalog = get_bank_catalog()
    if request.method == "POST":
        bank_code = request.form.get("bank_code", "").strip()
        account_number = request.form.get("account_number", "").strip()
        account_name = request.form.get("account_name", "").strip()
        bank = catalog.get(bank_code)
        if not bank or not account_number or not account_name:
            flash("กรุณากรอกข้อมูลบัญชีธนาคารให้ครบถ้วน", "error")
            return redirect(url_for("bank_accounts"))

        existing = UserBankAccount.query.filter_by(
            user_id=user.id,
            bank_code=bank_code,
            account_number=account_number,
            is_active=True,
        ).first()
        if existing:
            flash("บัญชีธนาคารนี้ถูกผูกไว้แล้ว", "error")
            return redirect(url_for("bank_accounts"))

        db.session.add(UserBankAccount(
            user_id=user.id,
            bank_code=bank_code,
            bank_name=bank["name"],
            account_number=account_number,
            account_name=account_name,
            logo_url=bank["logo"],
        ))
        db.session.commit()
        flash("ผูกบัญชีธนาคารเรียบร้อยแล้ว", "success")
        return redirect(url_for("bank_accounts"))

    accounts = UserBankAccount.query.filter_by(user_id=user.id).order_by(UserBankAccount.created_at.desc()).all()
    return render_template("bank_accounts.html", accounts=accounts, bank_catalog=catalog)


@app.route("/bank-accounts/<int:account_id>/delete", methods=["POST"])
@login_required
def delete_bank_account(account_id):
    user = current_user()
    account = db.session.get(UserBankAccount, account_id)
    if account and account.user_id == user.id:
        account.is_active = False
        db.session.commit()
        flash("ลบบัญชีธนาคารแล้ว", "success")
    return redirect(url_for("bank_accounts"))


@app.route("/admin/bank-accounts/<int:account_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_bank_account(account_id):
    account = db.session.get(UserBankAccount, account_id)
    if account:
        account.is_active = not account.is_active
        db.session.commit()
        flash("อัปเดตสถานะบัญชีธนาคารสมาชิกแล้ว", "success")
    return redirect(url_for("admin"))


@app.route("/wallet", methods=["GET", "POST"])
@login_required
def wallet():
    abort(404)


@app.route("/deposit", methods=["GET", "POST"])
@login_required
def deposit_page():
    abort(404)


@app.route("/withdraw", methods=["GET", "POST"])
@login_required
def withdraw_page():
    abort(404)


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    recent_logs = PointLog.query.filter_by(user_id=user.id).order_by(PointLog.created_at.desc()).limit(5).all()
    recent_redemptions = RedemptionHistory.query.filter_by(user_id=user.id).order_by(RedemptionHistory.created_at.desc()).limit(5).all()
    recent_bets = ThaiLotteryBet.query.filter_by(user_id=user.id).order_by(ThaiLotteryBet.created_at.desc()).limit(5).all()
    rooms = active_lottery_rooms().all()

    stats = {
        "points": user.points,
        "rewards": RedemptionHistory.query.filter_by(user_id=user.id).count(),
        "bets": ThaiLotteryBet.query.filter_by(user_id=user.id).count(),
        "rooms": len(rooms),
    }

    return render_template(
        "dashboard.html",
        user=user,
        recent_logs=recent_logs,
        recent_redemptions=recent_redemptions,
        recent_bets=recent_bets,
        rooms=rooms,
        stats=stats,
    )


@app.route("/games/treasure-chest")
@login_required
@points_rewards_required
def treasure_chest():
    return render_template(
        "treasure_chest.html",
        game_history=session.get("treasure_chest_history", [])[-8:],
    )


@app.route("/games/treasure-chest/open", methods=["POST"])
@login_required
@points_rewards_required
def open_treasure_chest():
    chest_index = request.json.get("chest") if request.is_json else request.form.get("chest")
    try:
        chest_index = int(chest_index)
    except (TypeError, ValueError):
        return jsonify({"error": "หีบที่เลือกไม่ถูกต้อง"}), 400
    if chest_index not in {0, 1, 2}:
        return jsonify({"error": "หีบที่เลือกไม่ถูกต้อง"}), 400

    prizes = [
        {"name": "เหรียญทองแห่งโชค", "tier": "LEGENDARY", "icon": "fa-coins", "color": "gold"},
        {"name": "อัญมณีจันทรา", "tier": "EPIC", "icon": "fa-gem", "color": "violet"},
        {"name": "ตราประทับจักรพรรดิ", "tier": "RARE", "icon": "fa-crown", "color": "blue"},
        {"name": "กุญแจแห่งโชคดี", "tier": "SPECIAL", "icon": "fa-key", "color": "green"},
    ]
    prize = random.SystemRandom().choices(prizes, weights=[8, 18, 32, 42], k=1)[0]
    history = session.get("treasure_chest_history", [])
    history.append({"name": prize["name"], "tier": prize["tier"]})
    session["treasure_chest_history"] = history[-8:]
    return jsonify({"prize": prize, "chest": chest_index})


# ==========================================================
# ROUTES — Admin Dashboard
# ==========================================================
@app.route("/admin")
@admin_required
def admin():
    username = request.args.get("username", "").strip()
    users_query = User.query.order_by(User.created_at.desc())
    if username:
        users_query = users_query.filter(User.username.ilike(f"%{username}%"))
    users = users_query.all()
    rewards = Reward.query.order_by(Reward.created_at.desc()).all()
    records = RedemptionHistory.query.order_by(RedemptionHistory.created_at.desc()).limit(30).all()
    
    rooms = LotteryRoom.query.order_by(LotteryRoom.sort_order.asc()).all()
    thai_periods = ThaiLotteryPeriod.query.order_by(ThaiLotteryPeriod.id.desc()).all()
    banners = HeroBanner.query.order_by(HeroBanner.created_at.desc()).all()

    stats = {
        "total_users": User.query.filter_by(role="member").count(),
        "total_rewards": Reward.query.filter_by(is_active=True).count(),
        "total_redeem": RedemptionHistory.query.count(),
        "pending": RedemptionHistory.query.filter_by(status="pending").count(),
    }
    uploaded_banners = [
        {
            "filename": name,
            "image_url": f"/static/uploads/{name}",
        }
        for name in sorted(
            (name for name in os.listdir(app.config["UPLOAD_FOLDER"]) if name.startswith("banner_")),
            reverse=True,
        )
    ]
    return render_template("admin.html", users=users, username=username, rewards=rewards,
                           records=records, rooms=rooms, thai_periods=thai_periods,
                           stats=stats, banners=banners, uploaded_banners=uploaded_banners)


@app.route("/admin/points/<int:user_id>", methods=["POST"])
@admin_required
@points_rewards_required
def admin_adjust_points(user_id):
    admin_user = current_user()
    target = db.session.get(User, user_id)
    if not target:
        flash("ไม่พบสมาชิก", "error")
        return redirect(url_for("admin"))

    try:
        amount = int(request.form.get("amount", 0))
    except ValueError:
        flash("จำนวนแต้มไม่ถูกต้อง", "error")
        return redirect(url_for("admin"))

    action = request.form.get("action", "add")      
    reason = request.form.get("reason", "").strip() or "ปรับแต้มโดยแอดมิน"

    if amount <= 0:
        flash("กรุณาระบุจำนวนแต้มมากกว่า 0", "error")
        return redirect(url_for("admin"))

    change = amount if action == "add" else -amount
    adjust_points(target, change, reason, admin=admin_user)
    audit_admin(admin_user, "adjust_points", "user", target.id, f"{change:+d} แต้ม: {reason}")
    db.session.commit()

    flash(f"ปรับแต้ม {target.username} แล้ว ({change:+,}) คงเหลือ {target.points:,}", "success")
    return redirect(url_for("admin"))


@app.route("/admin/partners", methods=["GET", "POST"])
@admin_required
def admin_partners():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        invite_code = request.form.get("invite_code", "").strip().upper()
        senior_id = request.form.get("senior_id", type=int)
        senior = db.session.get(User, senior_id) if senior_id else None
        try:
            commission_rate = float(request.form.get("commission_rate", 3))
        except ValueError:
            commission_rate = 0

        if len(username) < 4 or len(password) < 6 or not full_name:
            flash("กรุณากรอกชื่อผู้ใช้ ชื่อ Agent และรหัสผ่านให้ถูกต้อง", "error")
        elif commission_rate < 0 or commission_rate > 100:
            flash("เปอร์เซ็นต์คอมต้องอยู่ระหว่าง 0 ถึง 100", "error")
        elif User.query.filter_by(username=username).first():
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        elif invite_code and PartnerProfile.query.filter_by(invite_code=invite_code).first():
            flash("รหัสแนะนำนี้ถูกใช้แล้ว", "error")
        elif not senior or not senior.is_senior or not senior.senior_profile or senior.senior_profile.status != "active":
            flash("กรุณาเลือก Senior ที่ใช้งานอยู่ให้ Agent นี้", "error")
        else:
            if not invite_code:
                invite_code = f"FLEET{random.randint(10000, 99999)}"
            partner_user = User(
                username=username, full_name=full_name, phone=phone,
                role="partner", senior_id=senior.id, points=0, credit_balance=0.0,
            )
            partner_user.set_password(password)
            db.session.add(partner_user)
            db.session.flush()
            db.session.add(PartnerProfile(
                user_id=partner_user.id,
                invite_code=invite_code,
                commission_rate=commission_rate,
            ))
            audit_admin(current_user(), "create_partner", "user", partner_user.id,
                        f"invite={invite_code}, senior_id={senior.id}")
            db.session.commit()
            flash(f"สร้าง Agent {username} สำเร็จ รหัสแนะนำ: {invite_code}", "success")
        return redirect(url_for("admin_partners"))

    partners = User.query.filter_by(role="partner").order_by(User.created_at.desc()).all()
    partner_bank_accounts = {
        partner.id: UserBankAccount.query.filter_by(user_id=partner.id).order_by(UserBankAccount.created_at.asc()).all()
        for partner in partners
    }
    seniors = User.query.filter_by(role="senior").order_by(User.username.asc()).all()
    # แสดงผลว่าแต่ละ Agent อยู่ใต้ Senior โดยตรง หรือเป็น Agent ย่อยของ Agent อีกคน
    # (agent_upline_chain รองรับการซ้อนชั้นไม่จำกัด — ที่นี่แค่โชว์ผลลัพธ์ให้แอดมินดู)
    upline_labels = {}
    for partner in partners:
        chain = agent_upline_chain(partner)
        if chain:
            upline_labels[partner.id] = f"ใต้ Agent: {chain[0].full_name or chain[0].username}"
        elif partner.senior:
            upline_labels[partner.id] = partner.senior.full_name or partner.senior.username
        else:
            upline_labels[partner.id] = "ยังไม่มี Senior"
    return render_template("admin_partners.html", partners=partners,
                           partner_bank_accounts=partner_bank_accounts, bank_catalog=get_bank_catalog(),
                           seniors=seniors, upline_labels=upline_labels)


@app.route("/admin/partners/<int:user_id>/bank-account", methods=["POST"])
@admin_required
def admin_add_partner_bank_account(user_id):
    partner = db.session.get(User, user_id)
    if not partner or not partner.is_partner:
        abort(404)
    bank_code = request.form.get("bank_code", "").strip()
    account_number = request.form.get("account_number", "").strip()
    account_name = request.form.get("account_name", "").strip()
    bank = get_bank_catalog().get(bank_code)
    if not bank or not account_number or not account_name:
        flash("กรุณากรอกข้อมูลบัญชีธนาคารให้ครบถ้วน", "error")
    elif UserBankAccount.query.filter_by(user_id=partner.id, bank_code=bank_code,
                                          account_number=account_number).first():
        flash("มีบัญชีธนาคารนี้อยู่แล้ว", "error")
    else:
        db.session.add(UserBankAccount(
            user_id=partner.id, bank_code=bank_code, bank_name=bank["name"],
            account_number=account_number, account_name=account_name,
            logo_url=bank.get("logo", ""),
        ))
        audit_admin(current_user(), "add_partner_bank_account", "user", partner.id,
                    f"{bank['name']} {account_number}")
        db.session.commit()
        flash(f"เพิ่มบัญชีธนาคารให้ {partner.username} แล้ว", "success")
    return redirect(url_for("admin_partners"))


@app.route("/admin/partners/<int:user_id>/bank-account/<int:account_id>/delete", methods=["POST"])
@admin_required
def admin_delete_partner_bank_account(user_id, account_id):
    account = db.session.get(UserBankAccount, account_id)
    if account and account.user_id == user_id:
        db.session.delete(account)
        audit_admin(current_user(), "delete_partner_bank_account", "user", user_id, str(account_id))
        db.session.commit()
        flash("ลบบัญชีธนาคารแล้ว", "success")
    return redirect(url_for("admin_partners"))


@app.route("/admin/reports")
@admin_required
def admin_reports():
    total_bet_credit = db.session.query(db.func.coalesce(db.func.sum(ThaiLotteryBet.amount), 0)).scalar() or 0
    total_prize_points = db.session.query(db.func.coalesce(db.func.sum(ThaiLotteryBet.reward_amount), 0)).filter(
        ThaiLotteryBet.status == "win"
    ).scalar() or 0
    total_commission = db.session.query(db.func.coalesce(db.func.sum(CommissionLedger.commission_amount), 0)).scalar() or 0
    total_redemptions = db.session.query(db.func.coalesce(db.func.sum(RedemptionHistory.points_used), 0)).scalar() or 0
    room_rows = db.session.query(
        LotteryRoom.name,
        db.func.count(ThaiLotteryBet.id),
        db.func.coalesce(db.func.sum(ThaiLotteryBet.amount), 0),
    ).join(ThaiLotteryPeriod, ThaiLotteryPeriod.room_id == LotteryRoom.id).join(
        ThaiLotteryBet, ThaiLotteryBet.period_id == ThaiLotteryPeriod.id
    ).group_by(LotteryRoom.id).all()
    return render_template(
        "admin_reports.html",
        total_bet_credit=total_bet_credit,
        total_prize_points=total_prize_points,
        total_commission=total_commission,
        total_redemptions=total_redemptions,
        room_rows=room_rows,
    )


@app.route("/admin/wallet")
@admin_required
def admin_wallet():
    deposits = DepositRequest.query.filter_by(status="pending").order_by(DepositRequest.created_at.asc()).all()
    withdrawals = WithdrawalRequest.query.filter_by(status="pending").order_by(WithdrawalRequest.created_at.asc()).all()
    return render_template("admin_wallet.html", deposits=deposits, withdrawals=withdrawals)


@app.route("/admin/wallet/deposit/<int:request_id>/<action>", methods=["POST"])
@admin_required
def admin_process_deposit(request_id, action):
    item = db.session.get(DepositRequest, request_id)
    if not item or item.status != "pending" or action not in {"approve", "reject"}:
        flash("คำขอฝากนี้ถูกดำเนินการไปแล้วหรือไม่ถูกต้อง", "error")
        return redirect(url_for("admin_wallet"))
    admin_user = current_user()
    item.status = "approved" if action == "approve" else "rejected"
    item.processed_by = admin_user.id
    item.processed_at = datetime.now()
    item.admin_note = request.form.get("admin_note", "").strip()
    if action == "approve":
        user = item.user
        adjust_credit(user, item.amount, f"อนุมัติฝากเครดิตคำขอ #{item.id}", admin=admin_user)
        notify_user(user, "ฝากเครดิตสำเร็จ", f"เครดิตเพิ่ม {item.amount:,.2f}", "wallet")
    else:
        notify_user(item.user, "คำขอฝากเครดิตไม่ผ่านการอนุมัติ", item.admin_note or "กรุณาติดต่อแอดมิน", "wallet")
    audit_admin(admin_user, f"{action}_deposit", "deposit_request", item.id, item.admin_note)
    db.session.commit()
    flash("ดำเนินการคำขอฝากเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_wallet"))


@app.route("/admin/wallet/withdrawal/<int:request_id>/<action>", methods=["POST"])
@admin_required
def admin_process_withdrawal(request_id, action):
    item = db.session.get(WithdrawalRequest, request_id)
    if not item or item.status != "pending" or action not in {"approve", "reject"}:
        flash("คำขอถอนนี้ถูกดำเนินการไปแล้วหรือไม่ถูกต้อง", "error")
        return redirect(url_for("admin_wallet"))
    admin_user = current_user()
    commission_profile = item.user.partner_profile or item.user.senior_profile
    commission_payout = item.method == "commission_payout" and commission_profile
    if action == "approve" and commission_payout and commission_profile.commission_balance < item.amount:
        flash("คอมมิชชันไม่พอสำหรับอนุมัติคำขอนี้", "error")
        return redirect(url_for("admin_wallet"))
    if action == "approve" and not commission_payout and item.user.credit_balance < item.amount:
        flash("เครดิตสมาชิกไม่พอสำหรับอนุมัติคำขอถอนนี้", "error")
        return redirect(url_for("admin_wallet"))
    item.status = "approved" if action == "approve" else "rejected"
    item.processed_by = admin_user.id
    item.processed_at = datetime.now()
    item.admin_note = request.form.get("admin_note", "").strip()
    if action == "approve":
        if commission_payout:
            commission_profile.commission_balance = round(
                commission_profile.commission_balance - item.amount, 2
            )
            db.session.add(WalletTransaction(
                user_id=item.user.id, wallet_type="commission", change=-item.amount,
                balance_after=commission_profile.commission_balance,
                reference_type="commission_payout", reference_id=item.id,
                reason=f"อนุมัติเบิกคอมมิชชันคำขอ #{item.id}", admin_id=admin_user.id,
            ))
        else:
            adjust_credit(item.user, -item.amount, f"อนุมัติถอนเครดิตคำขอ #{item.id}", admin=admin_user)
        notify_user(item.user, "ถอนเครดิตสำเร็จ", f"ดำเนินการถอน {item.amount:,.2f} เครดิตแล้ว", "wallet")
    else:
        notify_user(item.user, "คำขอถอนเครดิตไม่ผ่านการอนุมัติ", item.admin_note or "กรุณาติดต่อแอดมิน", "wallet")
    audit_admin(admin_user, f"{action}_withdrawal", "withdrawal_request", item.id, item.admin_note)
    db.session.commit()
    flash("ดำเนินการคำขอถอนเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_wallet"))


@app.route("/admin/audit")
@admin_required
def admin_audit():
    logs = AdminAuditLog.query.order_by(AdminAuditLog.created_at.desc()).limit(200).all()
    transactions = WalletTransaction.query.order_by(WalletTransaction.created_at.desc()).limit(200).all()
    return render_template("admin_audit.html", logs=logs, transactions=transactions)


@app.route("/admin/vip", methods=["GET", "POST"])
@admin_required
@points_rewards_required
def admin_vip():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        badge_text = request.form.get("badge_text", "").strip()
        benefits = request.form.get("benefits", "").strip()
        try:
            rank = int(request.form.get("rank", 1))
            min_points = int(request.form.get("min_points", 0))
        except ValueError:
            rank, min_points = 1, 0
        if not name or rank < 1 or min_points < 0:
            flash("กรุณากรอกข้อมูลระดับ VIP ให้ถูกต้อง", "error")
        else:
            db.session.add(VipTier(
                name=name, rank=rank, min_points=min_points,
                badge_text=badge_text, benefits=benefits, is_active=True,
            ))
            audit_admin(current_user(), "create_vip_tier", "vip_tier", None, f"{name}: {min_points} แต้ม")
            db.session.commit()
            flash(f"เพิ่มระดับ {name} แล้ว", "success")
        return redirect(url_for("admin_vip"))

    tiers = VipTier.query.order_by(VipTier.min_points.asc()).all()
    return render_template("admin_vip.html", tiers=tiers)


@app.route("/admin/partners/<int:user_id>/update", methods=["POST"])
@admin_required
def admin_update_partner(user_id):
    partner = db.session.get(User, user_id)
    if not partner or not partner.is_partner or not partner.partner_profile:
        flash("ไม่พบ Agent", "error")
        return redirect(url_for("admin_partners"))
    try:
        rate = float(request.form.get("commission_rate", partner.partner_profile.commission_rate))
    except ValueError:
        rate = -1
    senior_id = request.form.get("senior_id", type=int)
    senior = db.session.get(User, senior_id) if senior_id else None
    if not 0 <= rate <= 100:
        flash("เปอร์เซ็นต์คอมต้องอยู่ระหว่าง 0 ถึง 100", "error")
    elif not senior or not senior.is_senior or not senior.senior_profile or senior.senior_profile.status != "active":
        flash("กรุณาเลือก Senior ที่ใช้งานอยู่ให้ Agent นี้", "error")
    else:
        status = request.form.get("status", "active")
        if status not in {"active", "suspended"}:
            status = "active"
        partner.partner_profile.commission_rate = rate
        partner.partner_profile.status = status
        partner.senior_id = senior.id
        audit_admin(current_user(), "update_partner", "user", partner.id,
                    f"rate={rate}, status={status}, senior_id={senior.id}")
        db.session.commit()
        flash(f"อัปเดต Agent {partner.username} แล้ว", "success")
    return redirect(url_for("admin_partners"))


@app.route("/admin/partners/<int:user_id>/payout", methods=["POST"])
@admin_required
def admin_partner_payout(user_id):
    partner = db.session.get(User, user_id)
    if not partner or not partner.partner_profile:
        flash("ไม่พบ Agent", "error")
        return redirect(url_for("admin_partners"))
    partner.partner_profile.commission_balance = 0.0
    CommissionLedger.query.filter_by(partner_id=partner.id, status="approved").update(
        {CommissionLedger.status: "paid"}, synchronize_session=False
    )
    audit_admin(current_user(), "payout_partner", "user", partner.id, "mark commission paid")
    db.session.commit()
    flash(f"บันทึกการจ่ายคอมมิชชันของ {partner.username} แล้ว", "success")
    return redirect(url_for("admin_partners"))


@app.route("/admin/seniors", methods=["GET", "POST"])
@admin_required
def admin_seniors():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        invite_code = request.form.get("invite_code", "").strip().upper()
        try:
            commission_rate = float(request.form.get("commission_rate", 1))
        except ValueError:
            commission_rate = 0

        if len(username) < 4 or len(password) < 6 or not full_name:
            flash("กรุณากรอกชื่อผู้ใช้ ชื่อ Senior และรหัสผ่านให้ถูกต้อง", "error")
        elif commission_rate < 0 or commission_rate > 100:
            flash("เปอร์เซ็นต์คอมต้องอยู่ระหว่าง 0 ถึง 100", "error")
        elif User.query.filter_by(username=username).first():
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        elif invite_code and SeniorProfile.query.filter_by(invite_code=invite_code).first():
            flash("รหัสแนะนำนี้ถูกใช้แล้ว", "error")
        else:
            if not invite_code:
                invite_code = f"SNR{random.randint(10000, 99999)}"
            senior_user = User(
                username=username, full_name=full_name, phone=phone,
                role="senior", points=0, credit_balance=0.0,
            )
            senior_user.set_password(password)
            db.session.add(senior_user)
            db.session.flush()
            db.session.add(SeniorProfile(
                user_id=senior_user.id,
                invite_code=invite_code,
                commission_rate=commission_rate,
            ))
            audit_admin(current_user(), "create_senior", "user", senior_user.id, f"invite={invite_code}")
            db.session.commit()
            flash(f"สร้าง Senior {username} สำเร็จ รหัสแนะนำ: {invite_code}", "success")
        return redirect(url_for("admin_seniors"))

    seniors = User.query.filter_by(role="senior").order_by(User.created_at.desc()).all()
    senior_bank_accounts = {
        senior.id: UserBankAccount.query.filter_by(user_id=senior.id).order_by(UserBankAccount.created_at.asc()).all()
        for senior in seniors
    }
    return render_template("admin_seniors.html", seniors=seniors,
                           senior_bank_accounts=senior_bank_accounts, bank_catalog=get_bank_catalog())


@app.route("/admin/seniors/<int:user_id>/bank-account", methods=["POST"])
@admin_required
def admin_add_senior_bank_account(user_id):
    senior = db.session.get(User, user_id)
    if not senior or not senior.is_senior:
        abort(404)
    bank_code = request.form.get("bank_code", "").strip()
    account_number = request.form.get("account_number", "").strip()
    account_name = request.form.get("account_name", "").strip()
    bank = get_bank_catalog().get(bank_code)
    if not bank or not account_number or not account_name:
        flash("กรุณากรอกข้อมูลบัญชีธนาคารให้ครบถ้วน", "error")
    elif UserBankAccount.query.filter_by(user_id=senior.id, bank_code=bank_code,
                                          account_number=account_number).first():
        flash("มีบัญชีธนาคารนี้อยู่แล้ว", "error")
    else:
        db.session.add(UserBankAccount(
            user_id=senior.id, bank_code=bank_code, bank_name=bank["name"],
            account_number=account_number, account_name=account_name,
            logo_url=bank.get("logo", ""),
        ))
        audit_admin(current_user(), "add_senior_bank_account", "user", senior.id,
                    f"{bank['name']} {account_number}")
        db.session.commit()
        flash(f"เพิ่มบัญชีธนาคารให้ {senior.username} แล้ว", "success")
    return redirect(url_for("admin_seniors"))


@app.route("/admin/seniors/<int:user_id>/bank-account/<int:account_id>/delete", methods=["POST"])
@admin_required
def admin_delete_senior_bank_account(user_id, account_id):
    account = db.session.get(UserBankAccount, account_id)
    if account and account.user_id == user_id:
        db.session.delete(account)
        audit_admin(current_user(), "delete_senior_bank_account", "user", user_id, str(account_id))
        db.session.commit()
        flash("ลบบัญชีธนาคารแล้ว", "success")
    return redirect(url_for("admin_seniors"))


@app.route("/admin/seniors/<int:user_id>/update", methods=["POST"])
@admin_required
def admin_update_senior(user_id):
    senior = db.session.get(User, user_id)
    if not senior or not senior.is_senior or not senior.senior_profile:
        flash("ไม่พบ Senior", "error")
        return redirect(url_for("admin_seniors"))
    try:
        rate = float(request.form.get("commission_rate", senior.senior_profile.commission_rate))
    except ValueError:
        rate = -1
    if not 0 <= rate <= 100:
        flash("เปอร์เซ็นต์คอมต้องอยู่ระหว่าง 0 ถึง 100", "error")
    else:
        status = request.form.get("status", "active")
        if status not in {"active", "suspended"}:
            status = "active"
        senior.senior_profile.commission_rate = rate
        senior.senior_profile.status = status
        audit_admin(current_user(), "update_senior", "user", senior.id, f"rate={rate}, status={status}")
        db.session.commit()
        flash(f"อัปเดต Senior {senior.username} แล้ว", "success")
    return redirect(url_for("admin_seniors"))


@app.route("/admin/seniors/<int:user_id>/payout", methods=["POST"])
@admin_required
def admin_senior_payout(user_id):
    senior = db.session.get(User, user_id)
    if not senior or not senior.senior_profile:
        flash("ไม่พบ Senior", "error")
        return redirect(url_for("admin_seniors"))
    senior.senior_profile.commission_balance = 0.0
    SeniorCommissionLedger.query.filter_by(senior_id=senior.id, status="approved").update(
        {SeniorCommissionLedger.status: "paid"}, synchronize_session=False
    )
    audit_admin(current_user(), "payout_senior", "user", senior.id, "mark commission paid")
    db.session.commit()
    flash(f"บันทึกการจ่ายคอมมิชชันของ {senior.username} แล้ว", "success")
    return redirect(url_for("admin_seniors"))


@app.route("/partner")
@partner_required
def partner_dashboard():
    user = current_user()
    if user.is_admin and not user.partner_profile:
        flash("กรุณาเข้าสู่ระบบด้วยบัญชี Agent เพื่อเปิดหน้านี้", "info")
        return redirect(url_for("admin"))
    partner = partner_owner(user)
    members = User.query.filter_by(partner_id=partner.id).order_by(User.created_at.desc()).all()
    entries = CommissionLedger.query.filter_by(partner_id=partner.id).order_by(
        CommissionLedger.created_at.desc()
    ).limit(50).all()
    profile = partner.partner_profile
    online_cutoff = datetime.now() - timedelta(minutes=10)
    online_count = PartnerPresence.query.filter_by(partner_id=partner.id).filter(
        PartnerPresence.last_seen_at >= online_cutoff
    ).count()
    return render_template("partner_dashboard.html", partner=partner, profile=profile,
                           members=members, entries=entries, online_count=online_count)


@app.route("/partner/assistants", methods=["GET", "POST"])
@partner_required
def partner_assistants():
    owner = current_user()
    partner = partner_owner(owner)
    if owner.id != partner.id:
        abort(403)
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        if len(username) < 4 or not re.fullmatch(r"[A-Za-z0-9]+", username):
            flash("ชื่อผู้ช่วยต้องเป็นภาษาอังกฤษหรือตัวเลขอย่างน้อย 4 ตัว", "error")
        elif len(password) < 6 or not re.fullmatch(r"[A-Za-z0-9]+", password):
            flash("รหัสผ่านต้องเป็นภาษาอังกฤษและตัวเลขอย่างน้อย 6 ตัว", "error")
        elif not full_name:
            flash("กรุณากรอกชื่อผู้ช่วย", "error")
        elif User.query.filter_by(username=username).first():
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        else:
            assistant_user = User(
                username=username, full_name=full_name, phone=phone,
                role="partner", partner_id=partner.id, points=0, credit_balance=0.0,
            )
            assistant_user.set_password(password)
            db.session.add(assistant_user)
            db.session.flush()
            db.session.add(PartnerProfile(
                user_id=assistant_user.id,
                invite_code=f"ASST{random.randint(10000, 99999)}",
                commission_rate=0,
            ))
            db.session.add(PartnerAssistant(
                partner_id=partner.id, assistant_user_id=assistant_user.id,
                permissions=selected_permissions(request.form) or "members,bets,reports",
            ))
            db.session.commit()
            flash(f"สร้างผู้ช่วย {username} สำเร็จ", "success")
        return redirect(url_for("partner_assistants"))
    assistants = PartnerAssistant.query.filter_by(partner_id=partner.id).order_by(
        PartnerAssistant.created_at.desc()
    ).all()
    return render_template("partner_manage.html", view="assistants", partner=partner, assistants=assistants,
                           perm_labels=ASSISTANT_PERMISSION_LABELS)


@app.route("/partner/assistants/<int:assistant_id>/permissions", methods=["POST"])
@partner_required
def partner_assistant_permissions(assistant_id):
    owner = current_user()
    partner = partner_owner(owner)
    if owner.id != partner.id:
        abort(403)
    item = PartnerAssistant.query.filter_by(id=assistant_id, partner_id=partner.id).first_or_404()
    item.permissions = selected_permissions(request.form)
    db.session.commit()
    flash(f"บันทึกสิทธิ์ของผู้ช่วย {item.assistant.username} แล้ว", "success")
    return redirect(url_for("partner_assistants"))


@app.route("/partner/assistants/<int:assistant_id>/toggle", methods=["POST"])
@partner_required
def partner_toggle_assistant(assistant_id):
    owner = current_user()
    partner = partner_owner(owner)
    if owner.id != partner.id:
        abort(403)
    assistant = PartnerAssistant.query.filter_by(id=assistant_id, partner_id=partner.id).first()
    if assistant:
        assistant.is_active = not assistant.is_active
        assistant.assistant.is_active = assistant.is_active
        db.session.commit()
        flash("อัปเดตสถานะผู้ช่วยแล้ว", "success")
    return redirect(url_for("partner_assistants"))


def partner_bet_query(partner):
    """Build the Partner-only bet query used by the bet list and reports."""
    query = ThaiLotteryBet.query.join(User, ThaiLotteryBet.user_id == User.id).join(
        ThaiLotteryPeriod, ThaiLotteryBet.period_id == ThaiLotteryPeriod.id
    ).filter(User.partner_id == partner.id)
    member_id = request.args.get("member_id", type=int)
    room_id = request.args.get("room_id", type=int)
    status = request.args.get("status", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    if member_id:
        query = query.filter(ThaiLotteryBet.user_id == member_id)
    if room_id:
        query = query.filter(ThaiLotteryPeriod.room_id == room_id)
    if status in {"pending", "win", "lose"}:
        query = query.filter(ThaiLotteryBet.status == status)
    for raw_date, operator in ((date_from, ">="), (date_to, "<")):
        if raw_date:
            try:
                parsed_date = datetime.strptime(raw_date, "%Y-%m-%d")
                if operator == "<":
                    parsed_date += timedelta(days=1)
                query = query.filter(
                    ThaiLotteryBet.created_at >= parsed_date if operator == ">="
                    else ThaiLotteryBet.created_at < parsed_date
                )
            except ValueError:
                pass
    return query.order_by(ThaiLotteryBet.created_at.desc())


@app.route("/partner/bets")
@partner_required
def partner_bets():
    partner = partner_owner(current_user())
    if partner.is_admin and not partner.partner_profile:
        flash("กรุณาเข้าสู่ระบบด้วยบัญชี Agent เพื่อเปิดหน้านี้", "info")
        return redirect(url_for("admin"))
    members = User.query.filter_by(partner_id=partner.id, role="member").order_by(User.created_at.desc()).all()
    rooms = active_lottery_rooms().all()
    bets = partner_bet_query(partner).limit(500).all()
    return render_template(
        "partner_manage.html", view="bets", partner=partner, members=members,
        rooms=rooms, bets=bets,
        filters={key: request.args.get(key, "") for key in ("member_id", "room_id", "status", "date_from", "date_to")},
    )


def partner_bet_snapshot(partner, status=None):
    query = partner_bet_query(partner)
    if status:
        query = query.filter(ThaiLotteryBet.status == status)
    bets = query.limit(1000).all()
    total_amount = sum(float(bet.amount) for bet in bets)
    total_payout = sum(float(bet.amount) * float(bet.rate) for bet in bets)
    return bets, total_amount, total_payout


@app.route("/partner/bets/summary")
@partner_required
def partner_bet_summary():
    partner = partner_owner(current_user())
    bets, total_amount, total_payout = partner_bet_snapshot(partner)
    return render_template("partner_manage.html", view="bet_summary", partner=partner, bets=bets,
                           total_amount=total_amount, total_payout=total_payout,
                           net_exposure=total_payout - total_amount)


@app.route("/partner/bets/member-types")
@partner_required
def partner_bet_member_types():
    partner = partner_owner(current_user())
    bets, _, _ = partner_bet_snapshot(partner)
    grouped = {}
    for bet in bets:
        key = (bet.user.full_name or bet.user.username, bet.bet_type)
        row = grouped.setdefault(key, {"count": 0, "amount": 0.0, "payout": 0.0})
        row["count"] += 1
        row["amount"] += float(bet.amount)
        row["payout"] += float(bet.amount) * float(bet.rate)
    return render_template("partner_manage.html", view="bet_member_types", partner=partner,
                           grouped=grouped)


@app.route("/partner/bets/pending")
@partner_required
def partner_pending_bets():
    partner = partner_owner(current_user())
    bets, total_amount, _ = partner_bet_snapshot(partner, "pending")
    return render_template("partner_manage.html", view="pending_bets", partner=partner,
                           bets=bets, total_amount=total_amount)


@app.route("/partner/bets/acceptance")
@partner_required
def partner_bet_acceptance():
    return redirect(url_for("partner_bet_acceptance_by_type"))


@app.route("/partner/bets/acceptance-by-type", methods=["GET", "POST"])
@partner_required
def partner_bet_acceptance_by_type():
    partner = partner_owner(current_user())
    rooms = active_lottery_rooms().all()
    room_id = request.args.get("room_id", type=int) or (rooms[0].id if rooms else None)
    periods = ThaiLotteryPeriod.query.filter_by(room_id=room_id).order_by(
        ThaiLotteryPeriod.close_time.desc()
    ).limit(10).all() if room_id else []
    period_id = request.args.get("period_id", type=int) or (periods[0].id if periods else None)
    if request.method == "POST":
        room_id = request.form.get("room_id", type=int)
        bet_type = normalize_bet_type(request.form.get("bet_type"))
        try:
            amount_limit = max(0.0, float(request.form.get("amount_limit", 0)))
        except (TypeError, ValueError):
            amount_limit = 0
        room = db.session.get(LotteryRoom, room_id) if room_id else None
        if not room or not bet_type:
            flash("กรุณาเลือกห้องและประเภทให้ถูกต้อง", "error")
        else:
            rule = PartnerAcceptanceLimit.query.filter_by(
                partner_id=partner.id, room_id=room.id, bet_type=bet_type
            ).first()
            if rule is None:
                rule = PartnerAcceptanceLimit(partner_id=partner.id, room_id=room.id, bet_type=bet_type)
                db.session.add(rule)
            rule.amount_limit = amount_limit
            db.session.commit()
            flash("บันทึกวงเงินรับของแยกตามประเภทแล้ว", "success")
        return redirect(url_for("partner_bet_acceptance_by_type", room_id=room_id, period_id=period_id))
    limits = PartnerAcceptanceLimit.query.filter_by(partner_id=partner.id).all()
    number_limits = PartnerAcceptanceNumber.query.filter_by(
        partner_id=partner.id, period_id=period_id
    ).order_by(PartnerAcceptanceNumber.bet_type, PartnerAcceptanceNumber.number).all() if period_id else []
    return render_template("partner_manage.html", view="acceptance_types", partner=partner,
                           rooms=rooms, periods=periods, selected_room_id=room_id,
                           selected_period_id=period_id, acceptance_limits=limits,
                           number_limits=number_limits)


@app.route("/partner/bets/acceptance-number", methods=["POST"])
@partner_required
def partner_bet_acceptance_number():
    partner = partner_owner(current_user())
    period_id = request.form.get("period_id", type=int)
    bet_type = normalize_bet_type(request.form.get("bet_type"))
    number = request.form.get("number", "").strip()
    try:
        amount_limit = max(0.0, float(request.form.get("amount_limit", 0)))
    except (TypeError, ValueError):
        amount_limit = 0
    period = db.session.get(ThaiLotteryPeriod, period_id) if period_id else None
    if not period or not bet_type or not number.isdigit():
        flash("กรุณากรอกงวด ประเภท เลข และวงเงินให้ถูกต้อง", "error")
    else:
        item = PartnerAcceptanceNumber.query.filter_by(
            partner_id=partner.id, period_id=period.id, bet_type=bet_type, number=number
        ).first()
        if item is None:
            item = PartnerAcceptanceNumber(partner_id=partner.id, period_id=period.id,
                                           bet_type=bet_type, number=number)
            db.session.add(item)
        item.amount_limit = amount_limit
        db.session.commit()
        flash("บันทึกวงเงินรับของรายเลขแล้ว", "success")
    return redirect(url_for("partner_bet_acceptance_by_type", room_id=period.room_id if period else None,
                            period_id=period_id))


@app.route("/partner/topup", methods=["POST"])
@partner_required
def partner_topup_member():
    partner = partner_owner(current_user())
    if not partner.is_partner:
        abort(403)

    member_id = request.form.get("member_id", type=int)
    member = db.session.get(User, member_id) if member_id else None
    try:
        amount = round(float(request.form.get("amount", 0)), 2)
    except (TypeError, ValueError):
        amount = 0

    if not member or member.role != "member" or member.partner_id != partner.id:
        flash("เลือกสมาชิกในสายของคุณเท่านั้น", "error")
    elif amount <= 0:
        flash("กรุณาระบุจำนวนเครดิตมากกว่า 0", "error")
    elif amount > partner.credit_balance:
        flash("เครดิตของ Agent ไม่พอสำหรับเติมให้สมาชิก", "error")
    else:
        reason = request.form.get("reason", "").strip() or "Agent เติมเครดิตให้สมาชิก"
        adjust_credit(partner, -amount, f"โอนเครดิตให้ {member.username}: {reason}")
        adjust_credit(member, amount, f"ได้รับเครดิตจาก Agent {partner.username}: {reason}")
        notify_user(member, "ได้รับเครดิตจาก Agent", f"เครดิตเพิ่ม {amount:,.2f} เครดิต", "wallet")
        notify_user(partner, "เติมเครดิตให้สมาชิกสำเร็จ", f"โอนให้ {member.username} จำนวน {amount:,.2f} เครดิต", "wallet")
        db.session.commit()
        flash(f"เติมเครดิตให้ {member.username} สำเร็จ {amount:,.2f} เครดิต", "success")
    return redirect(url_for("partner_dashboard"))


@app.route("/partner/agents/topup", methods=["POST"])
@partner_required
def partner_topup_agent():
    partner = partner_owner(current_user())
    agent_id = request.form.get("agent_id", type=int)
    agent = User.query.filter_by(id=agent_id, partner_id=partner.id, role="partner").first() if agent_id else None
    try:
        amount = round(float(request.form.get("amount", 0)), 2)
    except (TypeError, ValueError):
        amount = 0

    if not agent:
        flash("เลือก Agent ย่อยในสายของคุณเท่านั้น", "error")
    elif amount <= 0:
        flash("กรุณาระบุจำนวนเครดิตมากกว่า 0", "error")
    elif amount > partner.credit_balance:
        flash("เครดิตของคุณไม่พอสำหรับเติมให้ Agent ย่อย", "error")
    else:
        reason = request.form.get("reason", "").strip() or "Agent เติมเครดิตให้ Agent ย่อย"
        adjust_credit(partner, -amount, f"โอนเครดิตให้ {agent.username}: {reason}")
        adjust_credit(agent, amount, f"ได้รับเครดิตจาก Agent {partner.username}: {reason}")
        notify_user(agent, "ได้รับเครดิตจาก Agent", f"เครดิตเพิ่ม {amount:,.2f} เครดิต", "wallet")
        notify_user(partner, "เติมเครดิตให้ Agent ย่อยสำเร็จ", f"โอนให้ {agent.username} จำนวน {amount:,.2f} เครดิต", "wallet")
        db.session.commit()
        flash(f"เติมเครดิตให้ {agent.username} สำเร็จ {amount:,.2f} เครดิต", "success")
    return redirect(url_for("partner_agents"))


@app.route("/partner/members", methods=["GET", "POST"])
@partner_required
def partner_members():
    partner = partner_owner(current_user())
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        if len(username) < 4 or not re.fullmatch(r"[A-Za-z0-9]+", username):
            flash("ชื่อผู้ใช้ต้องเป็นภาษาอังกฤษหรือตัวเลขอย่างน้อย 4 ตัว", "error")
        elif len(password) < 6 or not re.fullmatch(r"[A-Za-z0-9]+", password):
            flash("รหัสผ่านต้องเป็นภาษาอังกฤษและตัวเลขอย่างน้อย 6 ตัว", "error")
        elif not full_name:
            flash("กรุณากรอกชื่อสมาชิก", "error")
        elif User.query.filter_by(username=username).first():
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        else:
            member = User(username=username, full_name=full_name, phone=phone,
                          role="member", partner_id=partner.id, points=0, credit_balance=0.0)
            member.set_password(password)
            db.session.add(member)
            db.session.commit()
            flash(f"เพิ่มสมาชิก {username} แล้ว", "success")
        return redirect(url_for("partner_members"))

    members = User.query.filter_by(partner_id=partner.id).order_by(User.created_at.desc()).all()
    rooms = active_lottery_rooms().all()
    return render_template("partner_manage.html", view="members", partner=partner,
                           members=members, rooms=rooms)


@app.route("/partner/members/<int:member_id>/limits", methods=["POST"])
@partner_required
def partner_member_limits(member_id):
    partner = partner_owner(current_user())
    member = User.query.filter_by(id=member_id, partner_id=partner.id, role="member").first()
    if not member:
        abort(404)
    try:
        min_bet = max(0.0, float(request.form.get("min_bet", 0)))
        max_bet = max(0.0, float(request.form.get("max_bet", 1000000)))
        max_number_bet = max(0.0, float(request.form.get("max_number_bet", 1000000)))
    except (TypeError, ValueError):
        flash("วงเงินไม่ถูกต้อง", "error")
        return redirect(url_for("partner_members"))
    if max_bet < min_bet or max_number_bet < min_bet:
        flash("วงเงินสูงสุดต้องไม่น้อยกว่าวงเงินขั้นต่ำ", "error")
        return redirect(url_for("partner_members"))
    limit = PartnerMemberLimit.query.filter_by(partner_id=partner.id, member_id=member.id).first()
    if limit is None:
        limit = PartnerMemberLimit(partner_id=partner.id, member_id=member.id)
        db.session.add(limit)
    limit.min_bet, limit.max_bet, limit.max_number_bet = min_bet, max_bet, max_number_bet
    db.session.commit()
    flash(f"บันทึกวงเงินของ {member.username} แล้ว", "success")
    return redirect(url_for("partner_members"))


@app.route("/partner/members/<int:member_id>/rate", methods=["POST"])
@partner_required
def partner_member_rate(member_id):
    partner = partner_owner(current_user())
    member = User.query.filter_by(id=member_id, partner_id=partner.id, role="member").first()
    if not member:
        abort(404)
    bet_type = normalize_bet_type(request.form.get("bet_type"))
    if not bet_type:
        flash("ประเภทเดิมพันไม่ถูกต้อง", "error")
        return redirect(url_for("partner_members"))
    try:
        payout_multiplier = float(request.form.get("payout_multiplier", 0))
    except (TypeError, ValueError):
        payout_multiplier = 0
    rate = PartnerMemberRate.query.filter_by(
        partner_id=partner.id, member_id=member.id, bet_type=bet_type
    ).first()
    if payout_multiplier <= 0:
        if rate:
            db.session.delete(rate)
            db.session.commit()
            flash(f"ยกเลิกเรทพิเศษของ {member.username} ({bet_type}) แล้ว", "success")
        return redirect(url_for("partner_members"))
    if rate is None:
        rate = PartnerMemberRate(partner_id=partner.id, member_id=member.id, bet_type=bet_type)
        db.session.add(rate)
    rate.payout_multiplier = payout_multiplier
    db.session.commit()
    flash(f"บันทึกเรทพิเศษของ {member.username} ({bet_type} = {payout_multiplier:g}) แล้ว", "success")
    return redirect(url_for("partner_members"))


@app.route("/partner/members/<int:member_id>/stock", methods=["POST"])
@partner_required
def partner_member_stock(member_id):
    partner = partner_owner(current_user())
    member = User.query.filter_by(id=member_id, partner_id=partner.id, role="member").first()
    if not member:
        abort(404)
    room_id = request.form.get("room_id", type=int)
    room = db.session.get(LotteryRoom, room_id) if room_id else None
    if not room:
        abort(404)
    try:
        hold_percent = float(request.form.get("hold_percent", 0))
    except (TypeError, ValueError):
        hold_percent = 0
    hold_percent = max(0.0, min(100.0, hold_percent))
    share = PartnerMemberStockShare.query.filter_by(
        partner_id=partner.id, member_id=member.id, room_id=room.id
    ).first()
    if hold_percent <= 0:
        if share:
            db.session.delete(share)
            db.session.commit()
            flash(f"ยกเลิกถือหุ้นเฉพาะ {member.username} ห้อง {room.name} แล้ว", "success")
        return redirect(url_for("partner_members"))
    if share is None:
        share = PartnerMemberStockShare(partner_id=partner.id, member_id=member.id, room_id=room.id)
        db.session.add(share)
    share.hold_percent = hold_percent
    db.session.commit()
    flash(f"ตั้งถือหุ้นเฉพาะ {member.username} ห้อง {room.name} เป็น {hold_percent:g}% แล้ว", "success")
    return redirect(url_for("partner_members"))


@app.route("/partner/agents", methods=["GET", "POST"])
@partner_required
def partner_agents():
    """Agent เพิ่ม Agent ย่อยของตัวเองได้ — Agent ใหม่ทำงานเหมือน Agent ทุกอย่าง
    (มี PartnerProfile ของตัวเอง, สร้างสมาชิก/Agent ย่อยต่อได้อีก, มีคอมมิชชัน/หุ้น
    ของตัวเอง) เพียงแต่ partner_id ชี้มาที่ Agent ผู้สร้าง แทนที่จะมี senior_id ตรง —
    คอมมิชชัน/หุ้นของ Agent ผู้สร้างจะไหลผ่าน create_agent_upline_commissions /
    apply_agent_upline_stock_holding โดยอัตโนมัติ"""
    partner = partner_owner(current_user())
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        invite_code = request.form.get("invite_code", "").strip().upper()
        try:
            commission_rate = float(request.form.get("commission_rate", 3))
        except ValueError:
            commission_rate = 0

        if len(username) < 4 or len(password) < 6 or not full_name:
            flash("กรุณากรอกชื่อผู้ใช้ ชื่อ Agent และรหัสผ่านให้ถูกต้อง", "error")
        elif commission_rate < 0 or commission_rate > 100:
            flash("เปอร์เซ็นต์คอมต้องอยู่ระหว่าง 0 ถึง 100", "error")
        elif User.query.filter_by(username=username).first():
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        elif invite_code and PartnerProfile.query.filter_by(invite_code=invite_code).first():
            flash("รหัสแนะนำนี้ถูกใช้แล้ว", "error")
        else:
            if not invite_code:
                invite_code = f"FLEET{random.randint(10000, 99999)}"
            sub_agent = User(
                username=username, full_name=full_name, phone=phone,
                role="partner", partner_id=partner.id, points=0, credit_balance=0.0,
            )
            sub_agent.set_password(password)
            db.session.add(sub_agent)
            db.session.flush()
            db.session.add(PartnerProfile(
                user_id=sub_agent.id,
                invite_code=invite_code,
                commission_rate=commission_rate,
            ))
            audit_admin(current_user(), "create_sub_agent", "user", sub_agent.id,
                        f"invite={invite_code}, upline_agent={partner.id}")
            db.session.commit()
            flash(f"เพิ่ม Agent ย่อย {username} แล้ว รหัสแนะนำ: {invite_code}", "success")
        return redirect(url_for("partner_agents"))

    sub_agents = User.query.filter_by(partner_id=partner.id, role="partner").order_by(User.created_at.desc()).all()
    return render_template("partner_manage.html", view="agents", partner=partner, sub_agents=sub_agents)


@app.route("/partner/finance")
@partner_required
def partner_finance():
    partner = partner_owner(current_user())
    transactions = WalletTransaction.query.filter_by(user_id=partner.id).order_by(
        WalletTransaction.created_at.desc()
    ).limit(100).all()
    accounts = UserBankAccount.query.filter_by(user_id=partner.id, is_active=True).order_by(
        UserBankAccount.created_at.asc()
    ).all()
    pending_payout = db.session.query(func.coalesce(func.sum(WithdrawalRequest.amount), 0.0)).filter(
        WithdrawalRequest.user_id == partner.id,
        WithdrawalRequest.method == "commission_payout",
        WithdrawalRequest.status == "pending",
    ).scalar()
    return render_template("partner_manage.html", view="finance", partner=partner,
                           transactions=transactions, bank_accounts=accounts,
                           bank_catalog=get_bank_catalog(), pending_payout=float(pending_payout or 0))


@app.route("/partner/finance/bank-account", methods=["POST"])
@partner_required
def partner_add_bank_account():
    partner = partner_owner(current_user())
    bank_code = request.form.get("bank_code", "").strip()
    account_number = request.form.get("account_number", "").strip()
    account_name = request.form.get("account_name", "").strip()
    bank = get_bank_catalog().get(bank_code)
    if not bank or not account_number or not account_name:
        flash("กรุณากรอกข้อมูลบัญชีธนาคารให้ครบถ้วน", "error")
    elif UserBankAccount.query.filter_by(user_id=partner.id, bank_code=bank_code,
                                         account_number=account_number, is_active=True).first():
        flash("บัญชีธนาคารนี้ถูกผูกไว้แล้ว", "error")
    else:
        db.session.add(UserBankAccount(
            user_id=partner.id, bank_code=bank_code, bank_name=bank["name"],
            account_number=account_number, account_name=account_name, logo_url=bank["logo"],
        ))
        db.session.commit()
        flash("ผูกบัญชีธนาคารสำหรับรับคอมมิชชันแล้ว", "success")
    return redirect(url_for("partner_finance"))


@app.route("/partner/finance/payout", methods=["POST"])
@partner_required
def partner_request_payout():
    partner = partner_owner(current_user())
    account_id = request.form.get("bank_account_id", type=int)
    account = db.session.get(UserBankAccount, account_id) if account_id else None
    try:
        amount = round(float(request.form.get("amount", 0)), 2)
    except (TypeError, ValueError):
        amount = 0
    pending = db.session.query(func.coalesce(func.sum(WithdrawalRequest.amount), 0.0)).filter(
        WithdrawalRequest.user_id == partner.id,
        WithdrawalRequest.method == "commission_payout",
        WithdrawalRequest.status == "pending",
    ).scalar() or 0
    available = round(float(partner.partner_profile.commission_balance) - float(pending), 2)
    if not account or account.user_id != partner.id or not account.is_active:
        flash("กรุณาเลือกบัญชีธนาคารของ Agent เท่านั้น", "error")
    elif amount <= 0 or amount > available:
        flash(f"คอมมิชชันที่เบิกได้คงเหลือ {available:,.2f}", "error")
    else:
        db.session.add(WithdrawalRequest(
            user_id=partner.id, amount=amount, method="commission_payout",
            payout_account=f"{account.bank_name} {account.account_number} ({account.account_name})",
            note=request.form.get("note", "").strip(),
        ))
        notify_user(partner, "ส่งคำขอเบิกคอมมิชชันแล้ว", f"รอตรวจสอบจำนวน {amount:,.2f}", "wallet")
        db.session.commit()
        flash("ส่งคำขอเบิกคอมมิชชันแล้ว", "success")
    return redirect(url_for("partner_finance"))


@app.route("/partner/reports")
@partner_required
def partner_reports():
    partner = partner_owner(current_user())
    bets = ThaiLotteryBet.query.join(User, ThaiLotteryBet.user_id == User.id).filter(
        User.partner_id == partner.id
    ).order_by(ThaiLotteryBet.created_at.desc()).limit(200).all()
    summary = {
        "total": len(bets),
        "amount": sum(float(bet.amount) for bet in bets),
        "wins": sum(1 for bet in bets if bet.status == "win"),
        "losses": sum(1 for bet in bets if bet.status == "lose"),
        "commission": sum(float(entry.commission_amount) for entry in CommissionLedger.query.filter_by(partner_id=partner.id).all()),
    }
    return render_template("partner_manage.html", view="reports", partner=partner,
                           bets=bets, summary=summary)


@app.route("/partner/reports/chart")
@partner_required
def partner_reports_chart():
    partner = partner_owner(current_user())
    return render_template("partner_manage.html", view="reports_chart", partner=partner)


@app.route("/partner/reports/chart-data")
@partner_required
def partner_reports_chart_data():
    partner = partner_owner(current_user())
    since = app_now() - timedelta(days=13)
    bets = ThaiLotteryBet.query.join(User, ThaiLotteryBet.user_id == User.id).filter(
        User.partner_id == partner.id, ThaiLotteryBet.created_at >= since
    ).all()
    days = [(since + timedelta(days=i)).date() for i in range(14)]
    bet_by_day = {d.isoformat(): 0.0 for d in days}
    win_by_day = {d.isoformat(): 0.0 for d in days}
    for bet in bets:
        key = bet.created_at.date().isoformat()
        if key not in bet_by_day:
            continue
        bet_by_day[key] += float(bet.amount)
        if bet.status == "win":
            win_by_day[key] += float(bet.reward_amount)
    labels = list(bet_by_day.keys())
    return jsonify({
        "labels": labels,
        "bet_amounts": [round(bet_by_day[d], 2) for d in labels],
        "win_amounts": [round(win_by_day[d], 2) for d in labels],
        "net": [round(bet_by_day[d] - win_by_day[d], 2) for d in labels],
    })


@app.route("/partner/reports/winners")
@partner_required
def partner_reports_winners():
    partner = partner_owner(current_user())
    bets = ThaiLotteryBet.query.join(User, ThaiLotteryBet.user_id == User.id).filter(
        User.partner_id == partner.id, ThaiLotteryBet.status == "win"
    ).order_by(ThaiLotteryBet.created_at.desc()).limit(200).all()
    return render_template("partner_manage.html", view="reports_winners", partner=partner, bets=bets)


@app.route("/partner/results")
@partner_required
def partner_results():
    partner = partner_owner(current_user())
    rooms = active_lottery_rooms().all()
    latest_periods = {}
    grouped_results = {}
    for room in rooms:
        period = ThaiLotteryPeriod.query.filter_by(room_id=room.id).order_by(ThaiLotteryPeriod.id.desc()).first()
        latest_periods[room.id] = period
        category_name = room.category_ref.name if room.category_ref else (room.category or "อื่นๆ")
        grouped_results.setdefault(category_name, []).append(room)
    return render_template("partner_manage.html", view="results", partner=partner,
                           grouped_results=grouped_results, latest_periods=latest_periods)


def online_agents_for(agent_ids):
    """Agent ที่เข้าสู่ระบบภายใน 10 นาทีล่าสุด (ดูจากประวัติการเข้าสู่ระบบ)"""
    if not agent_ids:
        return []
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    recent = {
        row.user_id: row.created_at
        for row in LoginHistory.query.filter(LoginHistory.user_id.in_(agent_ids), LoginHistory.created_at >= cutoff)
        .order_by(LoginHistory.id.asc())
    }
    agents = User.query.filter(User.id.in_(list(recent) or [-1])).order_by(User.username).all()
    return [{"user": agent, "last": (recent[agent.id] + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")} for agent in agents]


@app.route("/partner/online")
@partner_required
def partner_online_members():
    partner = partner_owner(current_user())
    online_cutoff = datetime.now() - timedelta(minutes=10)
    presence = PartnerPresence.query.filter_by(partner_id=partner.id).filter(
        PartnerPresence.last_seen_at >= online_cutoff
    ).order_by(PartnerPresence.last_seen_at.desc()).all()
    sub_agent_ids = [row.id for row in User.query.filter_by(partner_id=partner.id, role="partner").with_entities(User.id).all()]
    return render_template("partner_manage.html", view="online", partner=partner,
                           online_members=[item.member for item in presence],
                           online_agents=online_agents_for(sub_agent_ids))


@app.route("/partner/deposit", methods=["GET", "POST"])
@partner_required
def partner_deposit():
    partner = partner_owner(current_user())
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0
        note = request.form.get("note", "").strip()
        reference = request.form.get("reference", "").strip()
        if amount <= 0:
            flash("กรุณากรอกจำนวนเงินให้ถูกต้อง", "error")
        else:
            db.session.add(DepositRequest(
                user_id=partner.id, amount=amount, method="partner_topup",
                reference=reference, note=note,
            ))
            db.session.commit()
            flash("ส่งคำขอเติมเงินแล้ว รอแอดมินตรวจสอบ", "success")
        return redirect(url_for("partner_deposit"))

    requests_history = DepositRequest.query.filter_by(user_id=partner.id).order_by(
        DepositRequest.created_at.desc()
    ).limit(50).all()
    return render_template("partner_manage.html", view="deposit", partner=partner,
                           deposit_requests=requests_history)


@app.route("/partner/login-history")
@partner_required
def partner_login_history():
    partner = partner_owner(current_user())
    history = LoginHistory.query.filter_by(user_id=partner.id).order_by(LoginHistory.created_at.desc()).limit(100).all()
    return render_template("partner_manage.html", view="login_history", partner=partner, history=history)


@app.route("/partner/settings", methods=["GET", "POST"])
@partner_required
def partner_settings():
    partner = partner_owner(current_user())
    if request.method == "POST" and request.form.get("action") == "rate":
        bet_type = normalize_bet_type(request.form.get("bet_type"))
        try:
            payout = float(request.form.get("payout_multiplier", 0))
        except (TypeError, ValueError):
            payout = 0
        if not bet_type or payout <= 0:
            flash("กรุณากรอกอัตราจ่ายให้ถูกต้อง", "error")
        else:
            rule = PartnerPayoutRule.query.filter_by(partner_id=partner.id, bet_type=bet_type).first()
            if rule is None:
                rule = PartnerPayoutRule(partner_id=partner.id, bet_type=bet_type)
                db.session.add(rule)
            rule.payout_multiplier = payout
            db.session.commit()
            flash(f"บันทึกอัตราจ่าย {bet_type} เฉพาะสายแล้ว", "success")
        return redirect(url_for("partner_settings"))

    if request.method == "POST":
        room_id = request.form.get("room_id", type=int)
        room = db.session.get(LotteryRoom, room_id) if room_id else None
        if not room:
            abort(404)
        setting = PartnerRoomSetting.query.filter_by(partner_id=partner.id, room_id=room.id).first()
        if setting is None:
            setting = PartnerRoomSetting(partner_id=partner.id, room_id=room.id)
            db.session.add(setting)
        setting.is_enabled = request.form.get("is_enabled") == "1"
        db.session.commit()
        flash(f"อัปเดตสถานะห้อง {room.name} แล้ว", "success")
        return redirect(url_for("partner_settings"))

    rooms = active_lottery_rooms().all()
    settings = {item.room_id: item for item in PartnerRoomSetting.query.filter_by(partner_id=partner.id).all()}
    return render_template("partner_manage.html", view="settings", partner=partner,
                           rooms=rooms, room_settings=settings, rates=get_lottery_rates(),
                           partner_rates=get_partner_payout_rates(partner))


@app.route("/partner/stock", methods=["GET", "POST"])
@partner_required
def partner_stock():
    partner = partner_owner(current_user())
    if request.method == "POST":
        room_id = request.form.get("room_id", type=int)
        room = db.session.get(LotteryRoom, room_id) if room_id else None
        if not room:
            abort(404)
        try:
            hold_percent = float(request.form.get("hold_percent", 0))
        except (TypeError, ValueError):
            hold_percent = 0
        hold_percent = max(0.0, min(100.0, hold_percent))
        share = PartnerStockShare.query.filter_by(partner_id=partner.id, room_id=room.id).first()
        if share is None:
            share = PartnerStockShare(partner_id=partner.id, room_id=room.id)
            db.session.add(share)
        share.hold_percent = hold_percent
        db.session.commit()
        flash(f"ตั้งค่าถือหุ้นห้อง {room.name} เป็น {hold_percent:g}% แล้ว", "success")
        return redirect(url_for("partner_stock"))

    rooms = active_lottery_rooms().all()
    shares = {item.room_id: item for item in PartnerStockShare.query.filter_by(partner_id=partner.id).all()}
    return render_template("partner_manage.html", view="stock", partner=partner,
                           rooms=rooms, stock_shares=shares,
                           stock_balance=partner.partner_profile.stock_balance if partner.partner_profile else 0.0)


@app.route("/partner/blocked", methods=["GET", "POST"])
@partner_required
def partner_blocked_numbers():
    partner = partner_owner(current_user())
    if request.method == "POST":
        room_id = request.form.get("room_id", type=int)
        bet_type = normalize_bet_type(request.form.get("bet_type"))
        number = request.form.get("number", "").strip()
        try:
            payout = float(request.form.get("payout_multiplier", 0))
        except (TypeError, ValueError):
            payout = 0
        if not db.session.get(LotteryRoom, room_id) or not bet_type or not number.isdigit() or payout <= 0:
            flash("กรุณากรอกข้อมูลเลขอั้นให้ถูกต้อง", "error")
        else:
            item = PartnerBlockedNumber.query.filter_by(
                partner_id=partner.id, room_id=room_id, bet_type=bet_type, number=number
            ).first()
            if item is None:
                item = PartnerBlockedNumber(partner_id=partner.id, room_id=room_id,
                                            bet_type=bet_type, number=number)
                db.session.add(item)
            item.payout_multiplier = payout
            db.session.commit()
            flash("บันทึกเลขอั้นเฉพาะสายแล้ว", "success")
        return redirect(url_for("partner_blocked_numbers"))

    rooms = active_lottery_rooms().all()
    blocked = PartnerBlockedNumber.query.filter_by(partner_id=partner.id).order_by(
        PartnerBlockedNumber.created_at.desc()
    ).all()
    return render_template("partner_manage.html", view="blocked", partner=partner,
                           rooms=rooms, blocked_numbers=blocked)


@app.route("/partner/blocked/<int:item_id>/delete", methods=["POST"])
@partner_required
def partner_delete_blocked_number(item_id):
    partner = partner_owner(current_user())
    item = PartnerBlockedNumber.query.filter_by(id=item_id, partner_id=partner.id).first()
    if item:
        db.session.delete(item)
        db.session.commit()
        flash("ลบเลขอั้นเฉพาะสายแล้ว", "success")
    return redirect(url_for("partner_blocked_numbers"))


# ==========================================================
# ระบบหลังบ้าน Senior — ดูแล Agent หลายคน ไม่ได้ดูแล Member โดยตรง
# ==========================================================
def senior_owner(user=None):
    """Return the owning Senior for either an owner or assistant account
    (mirrors partner_owner)."""
    user = user or current_user()
    assistant = SeniorAssistant.query.filter_by(
        assistant_user_id=user.id, is_active=True
    ).first() if user else None
    return assistant.senior if assistant else user


def senior_agent_ids(senior):
    """สาย Agent ของ Senior คนนี้ทั้งหมด รวม Agent ย่อยที่ถูกสร้างซ้อนกันไม่จำกัดชั้น
    (Agent1 มี senior_id ตรง, Agent2/Agent3/... ที่ Agent1 สร้างเพิ่มจะมีแค่
    partner_id ชี้มาที่ Agent1 เท่านั้น ไม่มี senior_id ของตัวเอง) — เดินลงทีละชั้น
    (BFS) จนกว่าจะไม่เจอ Agent ย่อยเพิ่ม กันวนซ้ำด้วย seen set"""
    top_level = [row.id for row in User.query.filter_by(senior_id=senior.id, role="partner").with_entities(User.id).all()]
    all_ids = list(top_level)
    seen = set(all_ids)
    frontier = list(top_level)
    while frontier:
        children = [
            row.id for row in User.query.filter(
                User.partner_id.in_(frontier), User.role == "partner"
            ).with_entities(User.id).all()
            if row.id not in seen
        ]
        if not children:
            break
        all_ids.extend(children)
        seen.update(children)
        frontier = children
    return all_ids


def senior_bet_query(senior, agent_ids):
    """เหมือน partner_bet_query แต่ไล่ทุก Agent ในสาย Senior คนนี้ + กรองเพิ่มด้วย agent_id ได้"""
    query = ThaiLotteryBet.query.join(User, ThaiLotteryBet.user_id == User.id).join(
        ThaiLotteryPeriod, ThaiLotteryBet.period_id == ThaiLotteryPeriod.id
    ).filter(User.partner_id.in_(agent_ids)) if agent_ids else ThaiLotteryBet.query.filter(db.false())
    agent_id = request.args.get("agent_id", type=int)
    member_id = request.args.get("member_id", type=int)
    room_id = request.args.get("room_id", type=int)
    status = request.args.get("status", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    if agent_id:
        query = query.filter(User.partner_id == agent_id)
    if member_id:
        query = query.filter(ThaiLotteryBet.user_id == member_id)
    if room_id:
        query = query.filter(ThaiLotteryPeriod.room_id == room_id)
    if status in {"pending", "win", "lose"}:
        query = query.filter(ThaiLotteryBet.status == status)
    for raw_date, operator in ((date_from, ">="), (date_to, "<")):
        if raw_date:
            try:
                parsed_date = datetime.strptime(raw_date, "%Y-%m-%d")
                if operator == "<":
                    parsed_date += timedelta(days=1)
                query = query.filter(
                    ThaiLotteryBet.created_at >= parsed_date if operator == ">="
                    else ThaiLotteryBet.created_at < parsed_date
                )
            except ValueError:
                pass
    return query.order_by(ThaiLotteryBet.created_at.desc())


def senior_bet_snapshot(senior, agent_ids, status=None):
    """เหมือน partner_bet_snapshot แต่ไล่ทุก Agent ในสาย Senior"""
    query = senior_bet_query(senior, agent_ids)
    if status:
        query = query.filter(ThaiLotteryBet.status == status)
    bets = query.limit(1000).all()
    total_amount = sum(float(bet.amount) for bet in bets)
    total_payout = sum(float(bet.amount) * float(bet.rate) for bet in bets)
    return bets, total_amount, total_payout


@app.route("/senior")
@senior_required
def senior_dashboard():
    user = current_user()
    if user.is_admin and not user.senior_profile:
        flash("กรุณาเข้าสู่ระบบด้วยบัญชี Senior เพื่อเปิดหน้านี้", "info")
        return redirect(url_for("admin"))
    senior = senior_owner(user)
    agent_ids = senior_agent_ids(senior)
    agents = User.query.filter(User.id.in_(agent_ids)).order_by(User.created_at.desc()).all() if agent_ids else []
    member_count = User.query.filter(User.partner_id.in_(agent_ids)).count() if agent_ids else 0
    entries = SeniorCommissionLedger.query.filter_by(senior_id=senior.id).order_by(
        SeniorCommissionLedger.created_at.desc()
    ).limit(50).all()
    profile = senior.senior_profile
    return render_template("senior_dashboard.html", senior=senior, profile=profile,
                           agents=agents, member_count=member_count, entries=entries)


@app.route("/senior/agents", methods=["GET", "POST"])
@senior_required
def senior_agents():
    """Senior เพิ่ม Agent ระดับบนสุดเข้าสายตัวเองได้โดยตรง (เหมือนที่ Agent เพิ่ม
    Agent ย่อยของตัวเองได้) — Agent ที่สร้างจากตรงนี้จะมี senior_id ชี้มาที่ Senior
    คนนี้ทันที (partner_id เป็น None เพราะเป็น Agent ระดับบนสุด ไม่ใช่ Agent ย่อย)"""
    senior = senior_owner(current_user())
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        invite_code = request.form.get("invite_code", "").strip().upper()
        try:
            commission_rate = float(request.form.get("commission_rate", 3))
        except ValueError:
            commission_rate = 0

        if len(username) < 4 or len(password) < 6 or not full_name:
            flash("กรุณากรอกชื่อผู้ใช้ ชื่อ Agent และรหัสผ่านให้ถูกต้อง", "error")
        elif commission_rate < 0 or commission_rate > 100:
            flash("เปอร์เซ็นต์คอมต้องอยู่ระหว่าง 0 ถึง 100", "error")
        elif User.query.filter_by(username=username).first():
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        elif invite_code and PartnerProfile.query.filter_by(invite_code=invite_code).first():
            flash("รหัสแนะนำนี้ถูกใช้แล้ว", "error")
        else:
            if not invite_code:
                invite_code = f"FLEET{random.randint(10000, 99999)}"
            agent = User(
                username=username, full_name=full_name, phone=phone,
                role="partner", senior_id=senior.id, points=0, credit_balance=0.0,
            )
            agent.set_password(password)
            db.session.add(agent)
            db.session.flush()
            db.session.add(PartnerProfile(
                user_id=agent.id,
                invite_code=invite_code,
                commission_rate=commission_rate,
            ))
            audit_admin(current_user(), "senior_create_agent", "user", agent.id,
                        f"invite={invite_code}, senior_id={senior.id}")
            db.session.commit()
            flash(f"เพิ่ม Agent {username} เข้าสายแล้ว รหัสแนะนำ: {invite_code}", "success")
        return redirect(url_for("senior_agents"))

    agent_ids = senior_agent_ids(senior)
    agents = User.query.filter(User.id.in_(agent_ids)).order_by(User.created_at.desc()).all() if agent_ids else []
    rooms = active_lottery_rooms().all()
    agent_stock_shares = {}
    if agent_ids:
        for item in PartnerStockShare.query.filter(PartnerStockShare.partner_id.in_(agent_ids)).all():
            agent_stock_shares.setdefault(item.partner_id, {})[item.room_id] = item.hold_percent
    return render_template("senior_manage.html", view="agents", senior=senior, agents=agents,
                           rooms=rooms, agent_stock_shares=agent_stock_shares)


@app.route("/senior/agents/<int:agent_id>/update", methods=["POST"])
@senior_required
def senior_update_agent(agent_id):
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    agent = User.query.filter(User.id == agent_id, User.id.in_(agent_ids), User.role == "partner").first() if agent_ids else None
    if not agent or not agent.partner_profile:
        abort(404)
    try:
        rate = float(request.form.get("commission_rate", agent.partner_profile.commission_rate))
    except ValueError:
        rate = -1
    if not 0 <= rate <= 100:
        flash("เปอร์เซ็นต์คอมต้องอยู่ระหว่าง 0 ถึง 100", "error")
    else:
        status = request.form.get("status", "active")
        if status not in {"active", "suspended"}:
            status = "active"
        agent.partner_profile.commission_rate = rate
        agent.partner_profile.status = status
        audit_admin(current_user(), "senior_update_agent", "user", agent.id, f"rate={rate}, status={status}")
        db.session.commit()
        flash(f"อัปเดต Agent {agent.username} แล้ว", "success")
    return redirect(url_for("senior_agents"))


@app.route("/senior/assistants", methods=["GET", "POST"])
@senior_required
def senior_assistants():
    owner = current_user()
    senior = senior_owner(owner)
    if owner.id != senior.id:
        abort(403)
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        if len(username) < 4 or not re.fullmatch(r"[A-Za-z0-9]+", username):
            flash("ชื่อผู้ช่วยต้องเป็นภาษาอังกฤษหรือตัวเลขอย่างน้อย 4 ตัว", "error")
        elif len(password) < 6 or not re.fullmatch(r"[A-Za-z0-9]+", password):
            flash("รหัสผ่านต้องเป็นภาษาอังกฤษและตัวเลขอย่างน้อย 6 ตัว", "error")
        elif not full_name:
            flash("กรุณากรอกชื่อผู้ช่วย", "error")
        elif User.query.filter_by(username=username).first():
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        else:
            assistant_user = User(
                username=username, full_name=full_name, phone=phone,
                role="senior", points=0, credit_balance=0.0,
            )
            assistant_user.set_password(password)
            db.session.add(assistant_user)
            db.session.flush()
            db.session.add(SeniorProfile(
                user_id=assistant_user.id,
                invite_code=f"ASST{random.randint(10000, 99999)}",
                commission_rate=0,
            ))
            db.session.add(SeniorAssistant(
                senior_id=senior.id, assistant_user_id=assistant_user.id,
                permissions=selected_permissions(request.form) or "members,bets,reports",
            ))
            db.session.commit()
            flash(f"สร้างผู้ช่วย {username} สำเร็จ", "success")
        return redirect(url_for("senior_assistants"))
    assistants = SeniorAssistant.query.filter_by(senior_id=senior.id).order_by(
        SeniorAssistant.created_at.desc()
    ).all()
    return render_template("senior_manage.html", view="assistants", senior=senior, assistants=assistants,
                           perm_labels=ASSISTANT_PERMISSION_LABELS)


@app.route("/senior/assistants/<int:assistant_id>/permissions", methods=["POST"])
@senior_required
def senior_assistant_permissions(assistant_id):
    owner = current_user()
    senior = senior_owner(owner)
    if owner.id != senior.id:
        abort(403)
    item = SeniorAssistant.query.filter_by(id=assistant_id, senior_id=senior.id).first_or_404()
    item.permissions = selected_permissions(request.form)
    db.session.commit()
    flash(f"บันทึกสิทธิ์ของผู้ช่วย {item.assistant.username} แล้ว", "success")
    return redirect(url_for("senior_assistants"))


@app.route("/senior/assistants/<int:assistant_id>/toggle", methods=["POST"])
@senior_required
def senior_toggle_assistant(assistant_id):
    owner = current_user()
    senior = senior_owner(owner)
    if owner.id != senior.id:
        abort(403)
    assistant = SeniorAssistant.query.filter_by(id=assistant_id, senior_id=senior.id).first()
    if assistant:
        assistant.is_active = not assistant.is_active
        assistant.assistant.is_active = assistant.is_active
        db.session.commit()
        flash("อัปเดตสถานะผู้ช่วยแล้ว", "success")
    return redirect(url_for("senior_assistants"))


@app.route("/senior/bets")
@senior_required
def senior_bets():
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    agents = User.query.filter(User.id.in_(agent_ids)).order_by(User.username.asc()).all() if agent_ids else []
    rooms = active_lottery_rooms().all()
    bets = senior_bet_query(senior, agent_ids).limit(500).all()
    return render_template(
        "senior_manage.html", view="bets", senior=senior, agents=agents,
        rooms=rooms, bets=bets,
        filters={key: request.args.get(key, "") for key in ("agent_id", "member_id", "room_id", "status", "date_from", "date_to")},
    )


@app.route("/senior/bets/summary")
@senior_required
def senior_bet_summary():
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    bets, total_amount, total_payout = senior_bet_snapshot(senior, agent_ids)
    return render_template("senior_manage.html", view="bet_summary", senior=senior, bets=bets,
                           total_amount=total_amount, total_payout=total_payout,
                           net_exposure=total_payout - total_amount)


@app.route("/senior/bets/member-types")
@senior_required
def senior_bet_member_types():
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    bets, _, _ = senior_bet_snapshot(senior, agent_ids)
    grouped = {}
    for bet in bets:
        key = (bet.user.full_name or bet.user.username, bet.bet_type)
        row = grouped.setdefault(key, {"count": 0, "amount": 0.0, "payout": 0.0})
        row["count"] += 1
        row["amount"] += float(bet.amount)
        row["payout"] += float(bet.amount) * float(bet.rate)
    return render_template("senior_manage.html", view="bet_member_types", senior=senior, grouped=grouped)


@app.route("/senior/reports")
@senior_required
def senior_reports():
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    bets = senior_bet_query(senior, agent_ids).limit(200).all()
    summary = {
        "total": len(bets),
        "amount": sum(float(bet.amount) for bet in bets),
        "wins": sum(1 for bet in bets if bet.status == "win"),
        "losses": sum(1 for bet in bets if bet.status == "lose"),
        "commission": sum(float(entry.commission_amount) for entry in SeniorCommissionLedger.query.filter_by(senior_id=senior.id).all()),
    }
    by_category = {}
    for bet in bets:
        room = bet.period.room if bet.period else None
        category = (room.category_ref.name if room and room.category_ref else (room.category if room else None)) or "อื่นๆ"
        row = by_category.setdefault(category, {"count": 0, "amount": 0.0})
        row["count"] += 1
        row["amount"] += float(bet.amount)
    return render_template("senior_manage.html", view="reports", senior=senior,
                           bets=bets, summary=summary, by_category=by_category)


@app.route("/senior/reports/chart")
@senior_required
def senior_reports_chart():
    senior = senior_owner(current_user())
    return render_template("senior_manage.html", view="reports_chart", senior=senior)


@app.route("/senior/reports/chart-data")
@senior_required
def senior_reports_chart_data():
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    since = app_now() - timedelta(days=13)
    bets = ThaiLotteryBet.query.join(User, ThaiLotteryBet.user_id == User.id).filter(
        User.partner_id.in_(agent_ids), ThaiLotteryBet.created_at >= since
    ).all() if agent_ids else []
    days = [(since + timedelta(days=i)).date() for i in range(14)]
    bet_by_day = {d.isoformat(): 0.0 for d in days}
    win_by_day = {d.isoformat(): 0.0 for d in days}
    for bet in bets:
        key = bet.created_at.date().isoformat()
        if key not in bet_by_day:
            continue
        bet_by_day[key] += float(bet.amount)
        if bet.status == "win":
            win_by_day[key] += float(bet.reward_amount)
    labels = list(bet_by_day.keys())
    return jsonify({
        "labels": labels,
        "bet_amounts": [round(bet_by_day[d], 2) for d in labels],
        "win_amounts": [round(win_by_day[d], 2) for d in labels],
        "net": [round(bet_by_day[d] - win_by_day[d], 2) for d in labels],
    })


@app.route("/senior/reports/winners")
@senior_required
def senior_reports_winners():
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    bets = ThaiLotteryBet.query.join(User, ThaiLotteryBet.user_id == User.id).filter(
        User.partner_id.in_(agent_ids), ThaiLotteryBet.status == "win"
    ).order_by(ThaiLotteryBet.created_at.desc()).limit(200).all() if agent_ids else []
    return render_template("senior_manage.html", view="reports_winners", senior=senior, bets=bets)


@app.route("/senior/finance")
@senior_required
def senior_finance():
    senior = senior_owner(current_user())
    transactions = WalletTransaction.query.filter_by(user_id=senior.id).order_by(
        WalletTransaction.created_at.desc()
    ).limit(100).all()
    accounts = UserBankAccount.query.filter_by(user_id=senior.id, is_active=True).order_by(
        UserBankAccount.created_at.asc()
    ).all()
    pending_payout = db.session.query(func.coalesce(func.sum(WithdrawalRequest.amount), 0.0)).filter(
        WithdrawalRequest.user_id == senior.id,
        WithdrawalRequest.method == "commission_payout",
        WithdrawalRequest.status == "pending",
    ).scalar()
    return render_template("senior_manage.html", view="finance", senior=senior,
                           transactions=transactions, bank_accounts=accounts,
                           bank_catalog=get_bank_catalog(), pending_payout=float(pending_payout or 0))


@app.route("/senior/finance/bank-account", methods=["POST"])
@senior_required
def senior_add_bank_account():
    senior = senior_owner(current_user())
    bank_code = request.form.get("bank_code", "").strip()
    account_number = request.form.get("account_number", "").strip()
    account_name = request.form.get("account_name", "").strip()
    bank = get_bank_catalog().get(bank_code)
    if not bank or not account_number or not account_name:
        flash("กรุณากรอกข้อมูลบัญชีธนาคารให้ครบถ้วน", "error")
    elif UserBankAccount.query.filter_by(user_id=senior.id, bank_code=bank_code,
                                         account_number=account_number, is_active=True).first():
        flash("บัญชีธนาคารนี้ถูกผูกไว้แล้ว", "error")
    else:
        db.session.add(UserBankAccount(
            user_id=senior.id, bank_code=bank_code, bank_name=bank["name"],
            account_number=account_number, account_name=account_name, logo_url=bank["logo"],
        ))
        db.session.commit()
        flash("ผูกบัญชีธนาคารสำหรับรับคอมมิชชันแล้ว", "success")
    return redirect(url_for("senior_finance"))


@app.route("/senior/finance/payout", methods=["POST"])
@senior_required
def senior_request_payout():
    senior = senior_owner(current_user())
    account_id = request.form.get("bank_account_id", type=int)
    account = db.session.get(UserBankAccount, account_id) if account_id else None
    try:
        amount = round(float(request.form.get("amount", 0)), 2)
    except (TypeError, ValueError):
        amount = 0
    pending = db.session.query(func.coalesce(func.sum(WithdrawalRequest.amount), 0.0)).filter(
        WithdrawalRequest.user_id == senior.id,
        WithdrawalRequest.method == "commission_payout",
        WithdrawalRequest.status == "pending",
    ).scalar() or 0
    available = round(float(senior.senior_profile.commission_balance) - float(pending), 2)
    if not account or account.user_id != senior.id or not account.is_active:
        flash("กรุณาเลือกบัญชีธนาคารของ Senior เท่านั้น", "error")
    elif amount <= 0 or amount > available:
        flash(f"คอมมิชชันที่เบิกได้คงเหลือ {available:,.2f}", "error")
    else:
        db.session.add(WithdrawalRequest(
            user_id=senior.id, amount=amount, method="commission_payout",
            payout_account=f"{account.bank_name} {account.account_number} ({account.account_name})",
            note=request.form.get("note", "").strip(),
        ))
        notify_user(senior, "ส่งคำขอเบิกคอมมิชชันแล้ว", f"รอตรวจสอบจำนวน {amount:,.2f}", "wallet")
        db.session.commit()
        flash("ส่งคำขอเบิกคอมมิชชันแล้ว", "success")
    return redirect(url_for("senior_finance"))


@app.route("/senior/results")
@senior_required
def senior_results():
    senior = senior_owner(current_user())
    rooms = active_lottery_rooms().all()
    latest_periods = {}
    grouped_results = {}
    for room in rooms:
        period = ThaiLotteryPeriod.query.filter_by(room_id=room.id).order_by(ThaiLotteryPeriod.id.desc()).first()
        latest_periods[room.id] = period
        category_name = room.category_ref.name if room.category_ref else (room.category or "อื่นๆ")
        grouped_results.setdefault(category_name, []).append(room)
    return render_template("senior_manage.html", view="results", senior=senior,
                           grouped_results=grouped_results, latest_periods=latest_periods)


@app.route("/senior/online")
@senior_required
def senior_online_members():
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    online_cutoff = datetime.now() - timedelta(minutes=10)
    presence = PartnerPresence.query.filter(PartnerPresence.partner_id.in_(agent_ids)).filter(
        PartnerPresence.last_seen_at >= online_cutoff
    ).order_by(PartnerPresence.last_seen_at.desc()).all() if agent_ids else []
    return render_template("senior_manage.html", view="online", senior=senior,
                           online_members=[item.member for item in presence],
                           online_agents=online_agents_for(agent_ids))


@app.route("/senior/deposit", methods=["GET", "POST"])
@senior_required
def senior_deposit():
    senior = senior_owner(current_user())
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0
        note = request.form.get("note", "").strip()
        reference = request.form.get("reference", "").strip()
        if amount <= 0:
            flash("กรุณากรอกจำนวนเงินให้ถูกต้อง", "error")
        else:
            db.session.add(DepositRequest(
                user_id=senior.id, amount=amount, method="senior_topup",
                reference=reference, note=note,
            ))
            db.session.commit()
            flash("ส่งคำขอเติมเงินแล้ว รอแอดมินตรวจสอบ", "success")
        return redirect(url_for("senior_deposit"))

    requests_history = DepositRequest.query.filter_by(user_id=senior.id).order_by(
        DepositRequest.created_at.desc()
    ).limit(50).all()
    return render_template("senior_manage.html", view="deposit", senior=senior,
                           deposit_requests=requests_history)


@app.route("/senior/login-history")
@senior_required
def senior_login_history():
    senior = senior_owner(current_user())
    history = LoginHistory.query.filter_by(user_id=senior.id).order_by(LoginHistory.created_at.desc()).limit(100).all()
    return render_template("senior_manage.html", view="login_history", senior=senior, history=history)


@app.route("/senior/stock", methods=["GET", "POST"])
@senior_required
def senior_stock():
    senior = senior_owner(current_user())
    if request.method == "POST":
        room_id = request.form.get("room_id", type=int)
        room = db.session.get(LotteryRoom, room_id) if room_id else None
        if not room:
            abort(404)
        try:
            hold_percent = float(request.form.get("hold_percent", 0))
        except (TypeError, ValueError):
            hold_percent = 0
        hold_percent = max(0.0, min(100.0, hold_percent))
        share = SeniorStockShare.query.filter_by(senior_id=senior.id, room_id=room.id).first()
        if share is None:
            share = SeniorStockShare(senior_id=senior.id, room_id=room.id)
            db.session.add(share)
        share.hold_percent = hold_percent
        db.session.commit()
        flash(f"ตั้งค่าถือหุ้นห้อง {room.name} เป็น {hold_percent:g}% แล้ว", "success")
        return redirect(url_for("senior_stock"))

    rooms = active_lottery_rooms().all()
    shares = {item.room_id: item for item in SeniorStockShare.query.filter_by(senior_id=senior.id).all()}
    return render_template("senior_manage.html", view="stock", senior=senior,
                           rooms=rooms, stock_shares=shares,
                           stock_balance=senior.senior_profile.stock_balance if senior.senior_profile else 0.0)


@app.route("/senior/pending")
@senior_required
def senior_pending_bets():
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    bets = senior_bet_query(senior, agent_ids).filter(ThaiLotteryBet.status == "pending").limit(1000).all() if agent_ids else []
    total_amount = sum(float(bet.amount) for bet in bets)
    return render_template("senior_manage.html", view="pending_bets", senior=senior,
                           bets=bets, total_amount=total_amount)


@app.route("/senior/members", methods=["GET", "POST"])
@senior_required
def senior_members():
    """Senior เพิ่มสมาชิกได้ แต่ต้องเลือกว่าสมาชิกคนนี้อยู่ใต้ Agent คนไหนในสาย
    (เพราะ partner_id ของสมาชิกต้องชี้ไปที่ Agent เสมอ ให้ commission/stock
    cascade ทำงานถูกต้อง — Senior ไม่ใช่ Agent เลยรับสมาชิกตรงๆ ไม่ได้)"""
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    agents = User.query.filter(User.id.in_(agent_ids)).order_by(User.username.asc()).all() if agent_ids else []
    if request.method == "POST":
        agent_id = request.form.get("agent_id", type=int)
        agent = User.query.filter(User.id == agent_id, User.id.in_(agent_ids), User.role == "partner").first() if agent_id and agent_ids else None
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        if not agent:
            flash("กรุณาเลือก Agent ในสายของคุณให้สมาชิกคนนี้", "error")
        elif len(username) < 4 or not re.fullmatch(r"[A-Za-z0-9]+", username):
            flash("ชื่อผู้ใช้ต้องเป็นภาษาอังกฤษหรือตัวเลขอย่างน้อย 4 ตัว", "error")
        elif len(password) < 6 or not re.fullmatch(r"[A-Za-z0-9]+", password):
            flash("รหัสผ่านต้องเป็นภาษาอังกฤษและตัวเลขอย่างน้อย 6 ตัว", "error")
        elif not full_name:
            flash("กรุณากรอกชื่อสมาชิก", "error")
        elif User.query.filter_by(username=username).first():
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        else:
            member = User(username=username, full_name=full_name, phone=phone,
                          role="member", partner_id=agent.id, points=0, credit_balance=0.0)
            member.set_password(password)
            db.session.add(member)
            db.session.commit()
            flash(f"เพิ่มสมาชิก {username} เข้าสาย Agent {agent.username} แล้ว", "success")
        return redirect(url_for("senior_members"))

    agent_ids = [a.id for a in agents]
    members = User.query.filter(User.partner_id.in_(agent_ids)).order_by(
        User.created_at.desc()
    ).all() if agent_ids else []
    rooms = active_lottery_rooms().all()
    return render_template("senior_manage.html", view="members", senior=senior,
                           agents=agents, members=members, rooms=rooms)


@app.route("/senior/members/<int:member_id>/limits", methods=["POST"])
@senior_required
def senior_member_limits(member_id):
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    member = User.query.filter(User.id == member_id, User.role == "member",
                               User.partner_id.in_(agent_ids)).first() if agent_ids else None
    if not member:
        abort(404)
    try:
        min_bet = max(0.0, float(request.form.get("min_bet", 0)))
        max_bet = max(0.0, float(request.form.get("max_bet", 1000000)))
        max_number_bet = max(0.0, float(request.form.get("max_number_bet", 1000000)))
    except (TypeError, ValueError):
        flash("วงเงินไม่ถูกต้อง", "error")
        return redirect(url_for("senior_members"))
    if max_bet < min_bet or max_number_bet < min_bet:
        flash("วงเงินสูงสุดต้องไม่น้อยกว่าวงเงินขั้นต่ำ", "error")
        return redirect(url_for("senior_members"))
    limit = SeniorMemberLimit.query.filter_by(senior_id=senior.id, member_id=member.id).first()
    if limit is None:
        limit = SeniorMemberLimit(senior_id=senior.id, member_id=member.id)
        db.session.add(limit)
    limit.min_bet, limit.max_bet, limit.max_number_bet = min_bet, max_bet, max_number_bet
    db.session.commit()
    flash(f"บันทึกวงเงินของ {member.username} แล้ว", "success")
    return redirect(url_for("senior_members"))


@app.route("/senior/members/<int:member_id>/rate", methods=["POST"])
@senior_required
def senior_member_rate(member_id):
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    member = User.query.filter(User.id == member_id, User.role == "member",
                               User.partner_id.in_(agent_ids)).first() if agent_ids else None
    if not member:
        abort(404)
    bet_type = normalize_bet_type(request.form.get("bet_type"))
    if not bet_type:
        flash("ประเภทเดิมพันไม่ถูกต้อง", "error")
        return redirect(url_for("senior_members"))
    try:
        payout_multiplier = float(request.form.get("payout_multiplier", 0))
    except (TypeError, ValueError):
        payout_multiplier = 0
    rate = SeniorMemberRate.query.filter_by(
        senior_id=senior.id, member_id=member.id, bet_type=bet_type
    ).first()
    if payout_multiplier <= 0:
        if rate:
            db.session.delete(rate)
            db.session.commit()
            flash(f"ยกเลิกเรทพิเศษของ {member.username} ({bet_type}) แล้ว", "success")
        return redirect(url_for("senior_members"))
    if rate is None:
        rate = SeniorMemberRate(senior_id=senior.id, member_id=member.id, bet_type=bet_type)
        db.session.add(rate)
    rate.payout_multiplier = payout_multiplier
    db.session.commit()
    flash(f"บันทึกเรทพิเศษของ {member.username} ({bet_type} = {payout_multiplier:g}) แล้ว", "success")
    return redirect(url_for("senior_members"))


@app.route("/senior/members/<int:member_id>/stock", methods=["POST"])
@senior_required
def senior_member_stock(member_id):
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    member = User.query.filter(User.id == member_id, User.role == "member",
                               User.partner_id.in_(agent_ids)).first() if agent_ids else None
    if not member:
        abort(404)
    room_id = request.form.get("room_id", type=int)
    room = db.session.get(LotteryRoom, room_id) if room_id else None
    if not room:
        abort(404)
    try:
        hold_percent = float(request.form.get("hold_percent", 0))
    except (TypeError, ValueError):
        hold_percent = 0
    hold_percent = max(0.0, min(100.0, hold_percent))
    share = SeniorMemberStockShare.query.filter_by(
        senior_id=senior.id, member_id=member.id, room_id=room.id
    ).first()
    if hold_percent <= 0:
        if share:
            db.session.delete(share)
            db.session.commit()
            flash(f"ยกเลิกถือหุ้นเฉพาะ {member.username} ห้อง {room.name} แล้ว", "success")
        return redirect(url_for("senior_members"))
    if share is None:
        share = SeniorMemberStockShare(senior_id=senior.id, member_id=member.id, room_id=room.id)
        db.session.add(share)
    share.hold_percent = hold_percent
    db.session.commit()
    flash(f"ตั้งถือหุ้นเฉพาะ {member.username} ห้อง {room.name} เป็น {hold_percent:g}% แล้ว", "success")
    return redirect(url_for("senior_members"))


@app.route("/senior/agents/<int:agent_id>/stock", methods=["POST"])
@senior_required
def senior_agent_stock(agent_id):
    """Senior ดู/แก้ % ถือหุ้นของ Agent แต่ละคนในสายได้โดยตรง — เขียนลงตาราง
    PartnerStockShare ตัวเดียวกับที่ Agent ใช้ self-service เอง (เป็นค่าเดียวกัน
    จุดเดียวกัน ไม่ใช่ค่าซ้อนทับใหม่) เพื่อให้ Senior ช่วยตั้งแทน Agent ที่ตั้งไม่เป็นได้"""
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    agent = User.query.filter(User.id == agent_id, User.id.in_(agent_ids), User.role == "partner").first() if agent_ids else None
    if not agent:
        abort(404)
    room_id = request.form.get("room_id", type=int)
    room = db.session.get(LotteryRoom, room_id) if room_id else None
    if not room:
        abort(404)
    try:
        hold_percent = float(request.form.get("hold_percent", 0))
    except (TypeError, ValueError):
        hold_percent = 0
    hold_percent = max(0.0, min(100.0, hold_percent))
    share = PartnerStockShare.query.filter_by(partner_id=agent.id, room_id=room.id).first()
    if share is None:
        share = PartnerStockShare(partner_id=agent.id, room_id=room.id)
        db.session.add(share)
    share.hold_percent = hold_percent
    db.session.commit()
    flash(f"ตั้งค่าถือหุ้นของ {agent.username} ห้อง {room.name} เป็น {hold_percent:g}% แล้ว", "success")
    return redirect(url_for("senior_agents"))


@app.route("/senior/topup", methods=["POST"])
@senior_required
def senior_topup_member():
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    member_id = request.form.get("member_id", type=int)
    member = User.query.filter(User.id == member_id, User.role == "member",
                               User.partner_id.in_(agent_ids)).first() if member_id and agent_ids else None
    try:
        amount = round(float(request.form.get("amount", 0)), 2)
    except (TypeError, ValueError):
        amount = 0

    if not member:
        flash("เลือกสมาชิกในสายของคุณเท่านั้น", "error")
    elif amount <= 0:
        flash("กรุณาระบุจำนวนเครดิตมากกว่า 0", "error")
    elif amount > senior.credit_balance:
        flash("เครดิตของ Senior ไม่พอสำหรับเติมให้สมาชิก", "error")
    else:
        reason = request.form.get("reason", "").strip() or "Senior เติมเครดิตให้สมาชิก"
        adjust_credit(senior, -amount, f"โอนเครดิตให้ {member.username}: {reason}")
        adjust_credit(member, amount, f"ได้รับเครดิตจาก Senior {senior.username}: {reason}")
        notify_user(member, "ได้รับเครดิตจาก Senior", f"เครดิตเพิ่ม {amount:,.2f} เครดิต", "wallet")
        notify_user(senior, "เติมเครดิตให้สมาชิกสำเร็จ", f"โอนให้ {member.username} จำนวน {amount:,.2f} เครดิต", "wallet")
        db.session.commit()
        flash(f"เติมเครดิตให้ {member.username} แล้ว", "success")
    return redirect(url_for("senior_members"))


@app.route("/senior/agents/topup", methods=["POST"])
@senior_required
def senior_topup_agent():
    senior = senior_owner(current_user())
    agent_ids = senior_agent_ids(senior)
    agent_id = request.form.get("agent_id", type=int)
    agent = User.query.filter(User.id == agent_id, User.id.in_(agent_ids), User.role == "partner").first() if agent_id and agent_ids else None
    try:
        amount = round(float(request.form.get("amount", 0)), 2)
    except (TypeError, ValueError):
        amount = 0

    if not agent:
        flash("เลือก Agent ในสายของคุณเท่านั้น", "error")
    elif amount <= 0:
        flash("กรุณาระบุจำนวนเครดิตมากกว่า 0", "error")
    elif amount > senior.credit_balance:
        flash("เครดิตของ Senior ไม่พอสำหรับเติมให้ Agent", "error")
    else:
        reason = request.form.get("reason", "").strip() or "Senior เติมเครดิตให้ Agent"
        adjust_credit(senior, -amount, f"โอนเครดิตให้ {agent.username}: {reason}")
        adjust_credit(agent, amount, f"ได้รับเครดิตจาก Senior {senior.username}: {reason}")
        notify_user(agent, "ได้รับเครดิตจาก Senior", f"เครดิตเพิ่ม {amount:,.2f} เครดิต", "wallet")
        notify_user(senior, "เติมเครดิตให้ Agent สำเร็จ", f"โอนให้ {agent.username} จำนวน {amount:,.2f} เครดิต", "wallet")
        db.session.commit()
        flash(f"เติมเครดิตให้ {agent.username} สำเร็จ {amount:,.2f} เครดิต", "success")
    return redirect(url_for("senior_agents"))


@app.route("/senior/settings", methods=["GET", "POST"])
@senior_required
def senior_settings():
    senior = senior_owner(current_user())
    if request.method == "POST" and request.form.get("action") == "rate":
        bet_type = normalize_bet_type(request.form.get("bet_type"))
        try:
            payout = float(request.form.get("payout_multiplier", 0))
        except (TypeError, ValueError):
            payout = 0
        if not bet_type or payout <= 0:
            flash("กรุณากรอกอัตราจ่ายให้ถูกต้อง", "error")
        else:
            rule = SeniorPayoutRule.query.filter_by(senior_id=senior.id, bet_type=bet_type).first()
            if rule is None:
                rule = SeniorPayoutRule(senior_id=senior.id, bet_type=bet_type)
                db.session.add(rule)
            rule.payout_multiplier = payout
            db.session.commit()
            flash(f"บันทึกอัตราจ่าย {bet_type} ทั้งสายแล้ว", "success")
        return redirect(url_for("senior_settings"))

    if request.method == "POST":
        room_id = request.form.get("room_id", type=int)
        room = db.session.get(LotteryRoom, room_id) if room_id else None
        if not room:
            abort(404)
        setting = SeniorRoomSetting.query.filter_by(senior_id=senior.id, room_id=room.id).first()
        if setting is None:
            setting = SeniorRoomSetting(senior_id=senior.id, room_id=room.id)
            db.session.add(setting)
        setting.is_enabled = request.form.get("is_enabled") == "1"
        db.session.commit()
        flash(f"อัปเดตสถานะห้อง {room.name} แล้ว", "success")
        return redirect(url_for("senior_settings"))

    rooms = active_lottery_rooms().all()
    settings = {item.room_id: item for item in SeniorRoomSetting.query.filter_by(senior_id=senior.id).all()}
    return render_template("senior_manage.html", view="settings", senior=senior,
                           rooms=rooms, room_settings=settings, rates=get_lottery_rates(),
                           senior_rates=get_senior_payout_rates(senior))


@app.route("/senior/bets/acceptance-by-type", methods=["GET", "POST"])
@senior_required
def senior_bet_acceptance_by_type():
    senior = senior_owner(current_user())
    rooms = active_lottery_rooms().all()
    room_id = request.args.get("room_id", type=int) or (rooms[0].id if rooms else None)
    periods = ThaiLotteryPeriod.query.filter_by(room_id=room_id).order_by(
        ThaiLotteryPeriod.close_time.desc()
    ).limit(10).all() if room_id else []
    period_id = request.args.get("period_id", type=int) or (periods[0].id if periods else None)
    if request.method == "POST":
        room_id = request.form.get("room_id", type=int)
        bet_type = normalize_bet_type(request.form.get("bet_type"))
        try:
            amount_limit = max(0.0, float(request.form.get("amount_limit", 0)))
        except (TypeError, ValueError):
            amount_limit = 0
        room = db.session.get(LotteryRoom, room_id) if room_id else None
        if not room or not bet_type:
            flash("กรุณาเลือกห้องและประเภทให้ถูกต้อง", "error")
        else:
            rule = SeniorAcceptanceLimit.query.filter_by(
                senior_id=senior.id, room_id=room.id, bet_type=bet_type
            ).first()
            if rule is None:
                rule = SeniorAcceptanceLimit(senior_id=senior.id, room_id=room.id, bet_type=bet_type)
                db.session.add(rule)
            rule.amount_limit = amount_limit
            db.session.commit()
            flash("บันทึกวงเงินรับของแยกตามประเภทแล้ว", "success")
        return redirect(url_for("senior_bet_acceptance_by_type", room_id=room_id, period_id=period_id))
    limits = SeniorAcceptanceLimit.query.filter_by(senior_id=senior.id).all()
    number_limits = SeniorAcceptanceNumber.query.filter_by(
        senior_id=senior.id, period_id=period_id
    ).order_by(SeniorAcceptanceNumber.bet_type, SeniorAcceptanceNumber.number).all() if period_id else []
    return render_template("senior_manage.html", view="acceptance_types", senior=senior,
                           rooms=rooms, periods=periods, selected_room_id=room_id,
                           selected_period_id=period_id, acceptance_limits=limits,
                           number_limits=number_limits)


@app.route("/senior/bets/acceptance-number", methods=["POST"])
@senior_required
def senior_bet_acceptance_number():
    senior = senior_owner(current_user())
    period_id = request.form.get("period_id", type=int)
    bet_type = normalize_bet_type(request.form.get("bet_type"))
    number = request.form.get("number", "").strip()
    try:
        amount_limit = max(0.0, float(request.form.get("amount_limit", 0)))
    except (TypeError, ValueError):
        amount_limit = 0
    period = db.session.get(ThaiLotteryPeriod, period_id) if period_id else None
    if not period or not bet_type or not number.isdigit():
        flash("กรุณากรอกงวด ประเภท เลข และวงเงินให้ถูกต้อง", "error")
    else:
        item = SeniorAcceptanceNumber.query.filter_by(
            senior_id=senior.id, period_id=period.id, bet_type=bet_type, number=number
        ).first()
        if item is None:
            item = SeniorAcceptanceNumber(senior_id=senior.id, period_id=period.id,
                                          bet_type=bet_type, number=number)
            db.session.add(item)
        item.amount_limit = amount_limit
        db.session.commit()
        flash("บันทึกวงเงินรับของรายเลขแล้ว", "success")
    return redirect(url_for("senior_bet_acceptance_by_type", room_id=period.room_id if period else None,
                            period_id=period_id))


@app.route("/senior/blocked", methods=["GET", "POST"])
@senior_required
def senior_blocked_numbers():
    senior = senior_owner(current_user())
    if request.method == "POST":
        room_id = request.form.get("room_id", type=int)
        bet_type = normalize_bet_type(request.form.get("bet_type"))
        number = request.form.get("number", "").strip()
        try:
            payout = float(request.form.get("payout_multiplier", 0))
        except (TypeError, ValueError):
            payout = 0
        if not db.session.get(LotteryRoom, room_id) or not bet_type or not number.isdigit() or payout <= 0:
            flash("กรุณากรอกข้อมูลเลขอั้นให้ถูกต้อง", "error")
        else:
            item = SeniorBlockedNumber.query.filter_by(
                senior_id=senior.id, room_id=room_id, bet_type=bet_type, number=number
            ).first()
            if item is None:
                item = SeniorBlockedNumber(senior_id=senior.id, room_id=room_id,
                                           bet_type=bet_type, number=number)
                db.session.add(item)
            item.payout_multiplier = payout
            db.session.commit()
            flash("บันทึกเลขอั้นทั้งสายแล้ว", "success")
        return redirect(url_for("senior_blocked_numbers"))

    rooms = active_lottery_rooms().all()
    blocked = SeniorBlockedNumber.query.filter_by(senior_id=senior.id).order_by(
        SeniorBlockedNumber.created_at.desc()
    ).all()
    return render_template("senior_manage.html", view="blocked", senior=senior,
                           rooms=rooms, blocked_numbers=blocked)


@app.route("/senior/blocked/<int:item_id>/delete", methods=["POST"])
@senior_required
def senior_delete_blocked_number(item_id):
    senior = senior_owner(current_user())
    item = SeniorBlockedNumber.query.filter_by(id=item_id, senior_id=senior.id).first()
    if item:
        db.session.delete(item)
        db.session.commit()
        flash("ลบเลขอั้นทั้งสายแล้ว", "success")
    return redirect(url_for("senior_blocked_numbers"))


@app.route("/admin/credits/<int:user_id>", methods=["POST"])
@admin_required
def admin_adjust_credits(user_id):
    admin_user = current_user()
    username = request.form.get("username", "").strip()
    admin_page = lambda: redirect(url_for("admin", username=username, _anchor="member-management") if username else url_for("admin", _anchor="member-management"))
    target = db.session.get(User, user_id)
    if not target:
        flash("ไม่พบสมาชิก", "error")
        return admin_page()

    try:
        amount = float(request.form.get("amount", 0))
    except ValueError:
        flash("จำนวนเครดิตไม่ถูกต้อง", "error")
        return admin_page()

    action = request.form.get("action", "add")
    reason = request.form.get("reason", "").strip() or "ปรับเครดิตโดยแอดมิน"
    if amount <= 0:
        flash("กรุณาระบุจำนวนเครดิตมากกว่า 0", "error")
        return admin_page()

    change = amount if action == "add" else -amount
    adjust_credit(target, change, reason, admin=admin_user)
    audit_admin(admin_user, "adjust_credit", "user", target.id, f"{change:+,.2f} เครดิต: {reason}")
    db.session.commit()

    flash(f"ปรับเครดิต {target.username} แล้ว ({change:+,.2f}) คงเหลือ {target.credit_balance:,.2f}", "success")
    return admin_page()


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(user_id):
    target = db.session.get(User, user_id)
    if target and target.id != current_user().id:
        target.is_active = not target.is_active
        db.session.commit()
        flash(f"อัปเดตสถานะ {target.username} แล้ว", "success")
    return redirect(url_for("admin"))


@app.route("/admin/rewards/add", methods=["POST"])
@admin_required
@points_rewards_required
def admin_add_reward():
    name = request.form.get("name", "").strip()
    if not name:
        flash("กรุณากรอกชื่อของรางวัล", "error")
        return redirect(url_for("admin"))

    reward = Reward(
        name=name,
        description=request.form.get("description", "").strip(),
        image_url=request.form.get("image_url", "").strip(),
        points_required=int(request.form.get("points_required") or 100),
        stock=int(request.form.get("stock") or 0),
        is_active=True,
    )
    db.session.add(reward)
    db.session.commit()
    flash(f"เพิ่มของรางวัล '{reward.name}' แล้ว", "success")
    return redirect(url_for("admin"))


@app.route("/admin/rewards/<int:reward_id>/edit", methods=["GET", "POST"])
@admin_required
@points_rewards_required
def admin_edit_reward(reward_id):
    reward = db.session.get(Reward, reward_id)
    if not reward:
        flash("ไม่พบของรางวัล", "error")
        return redirect(url_for("admin"))

    if request.method == "POST":
        reward.name = request.form.get("name", "").strip()
        reward.description = request.form.get("description", "").strip()
        reward.image_url = request.form.get("image_url", "").strip()
        reward.points_required = int(request.form.get("points_required") or 0)
        reward.stock = int(request.form.get("stock") or 0)
        reward.is_active = request.form.get("is_active") == "on"
        db.session.commit()
        flash("บันทึกการแก้ไขแล้ว", "success")
        return redirect(url_for("admin"))

    return render_template("admin_reward_edit.html", reward=reward)


@app.route("/admin/rewards/<int:reward_id>/delete", methods=["POST"])
@admin_required
@points_rewards_required
def admin_delete_reward(reward_id):
    reward = db.session.get(Reward, reward_id)
    if reward:
        reward.is_active = not reward.is_active
        db.session.commit()
        status_text = "เปิดการแสดงผล" if reward.is_active else "ปิดการแสดงผล"
        flash(f"เปลี่ยนสถานะของรางวัล '{reward.name}' เป็น{status_text}เรียบร้อย", "success")
    return redirect(url_for("admin"))


@app.route("/admin/rewards/<int:reward_id>/purge", methods=["POST"])
@admin_required
@points_rewards_required
def admin_purge_reward(reward_id):
    reward = db.session.get(Reward, reward_id)
    if not reward:
        flash("ไม่พบของรางวัล", "error")
        return redirect(url_for("admin"))

    if reward.redemptions:
        reward.is_active = False
        db.session.commit()
        flash("รางวัลนี้มีประวัติการแลกแล้ว จึงปิดการแสดงผลแทนการลบถาวร", "warning")
    else:
        name = reward.name
        db.session.delete(reward)
        db.session.commit()
        flash(f"ลบของรางวัล '{name}' ถาวรแล้ว", "success")
    return redirect(url_for("admin"))


@app.route("/admin/redemptions/<int:record_id>/status", methods=["POST"])
@admin_required
def admin_update_redemption(record_id):
    record = db.session.get(RedemptionHistory, record_id)
    if not record:
        return redirect(url_for("admin"))

    new_status = request.form.get("status")
    if new_status == "cancelled" and record.status != "cancelled":
        user = db.session.get(User, record.user_id)
        adjust_points(user, record.points_used, f"ยกเลิกการแลก {record.redeem_code} (คืนแต้ม)", admin=current_user())
        if record.reward:
            record.reward.stock += 1

    record.status = new_status
    db.session.commit()
    flash("อัปเดตสถานะเรียบร้อย", "success")
    return redirect(url_for("admin"))


# ==========================================================
# ROUTES — Admin Lottery Management (หวยรัฐบาลไทย)
# ==========================================================
@app.route("/admin/thai-lottery", methods=["GET", "POST"])
@admin_required
def admin_thai_lottery():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "sync_api":
            date_value = request.form.get("api_date", "").strip() or None
            try:
                summary = sync_lottery_api_results(date_value)
            except Exception as exc:
                db.session.rollback()
                flash(f"ดึงข้อมูล API ไม่สำเร็จ: {exc}", "error")
                return redirect(url_for("admin_thai_lottery"))
            flash(
                f"ซิงก์ API วันที่ {summary['date']} แล้ว: เพิ่มห้อง {summary['rooms']} ห้อง, "
                f"เพิ่มงวด {summary['periods']} งวด, อัปเดตผล {summary['results']} งวด "
                "ระบบตรวจสอบและจ่ายรางวัลอัตโนมัติแล้ว",
                "success",
            )
            return redirect(url_for("admin_thai_lottery"))

        if action == "confirm_api_results":
            pending_periods = ThaiLotteryPeriod.query.filter(
                ThaiLotteryPeriod.api_status == "success",
                ThaiLotteryPeriod.is_checked.is_(False),
                ThaiLotteryPeriod.result_3up.is_not(None),
                ThaiLotteryPeriod.result_2down.is_not(None),
            ).all()
            confirmed = 0
            skipped = 0
            for period in pending_periods:
                settled = settle_lottery_period(period, current_user())
                if settled is None:
                    skipped += 1
                else:
                    confirmed += 1
            db.session.commit()
            flash(
                f"ยืนยันผลหวยทั้งหมดแล้ว {confirmed} งวด"
                + (f" (ข้าม {skipped} งวดที่ผลไม่ครบ)" if skipped else ""),
                "success" if confirmed else "warning",
            )
            return redirect(url_for("admin_thai_lottery"))

        if action == "reverse_result":
            period_id = request.form.get("period_id", type=int)
            period = db.session.get(ThaiLotteryPeriod, period_id)
            if not period or not period.is_checked:
                flash("ไม่พบงวดที่ตรวจผลแล้วสำหรับการย้อนผล", "error")
                return redirect(url_for("admin_thai_lottery"))

            winning_bets = ThaiLotteryBet.query.filter_by(
                period_id=period.id, status="win"
            ).all()
            for bet in winning_bets:
                if bet.user.credit_balance < bet.reward_amount:
                    flash(f"ไม่สามารถย้อนผลได้: เครดิตของ {bet.user.username} ถูกใช้ไปแล้ว", "error")
                    return redirect(url_for("admin_thai_lottery"))

            for bet in winning_bets:
                adjust_credit(
                    bet.user,
                    -bet.reward_amount,
                    f"ย้อนคืนรางวัลหวยงวด {period.period_date} ({bet.bet_type}: {bet.number})",
                    admin=current_user(),
                )
                bet.status = "pending"
                notify_user(
                    bet.user,
                    "มีการย้อนผลหวย",
                    f"รางวัลเลข {bet.number} ถูกย้อนกลับเพื่อให้แอดมินตรวจสอบใหม่",
                    "system",
                )

            period.is_checked = False
            period.result_3up = None
            period.result_2down = None
            period.result_3front = None
            period.result_3back = None
            audit_admin(current_user(), "reverse_lottery_period", "lottery_period", period.id)
            db.session.commit()
            flash("ย้อนผลหวยและคืนโพยที่ถูกรางวัลเป็นสถานะรอตรวจสอบแล้ว", "success")
            return redirect(url_for("admin_thai_lottery"))

        if action == "set_rule":
            bet_type = normalize_bet_type(request.form.get("bet_type"))
            multiplier = request.form.get("payout_multiplier", "").strip()
            if not bet_type:
                flash("กรุณาเลือกประเภทการแทงให้ถูกต้อง", "error")
                return redirect(url_for("admin_thai_lottery"))
            try:
                value = float(multiplier)
            except ValueError:
                flash("ค่าจ่ายแต่ละแทงต้องเป็นตัวเลข", "error")
                return redirect(url_for("admin_thai_lottery"))

            rule = LotteryPayoutRule.query.filter_by(bet_type=bet_type).first()
            if rule is None:
                rule = LotteryPayoutRule(bet_type=bet_type, description=f"อัตราจ่าย {bet_type}")
                db.session.add(rule)
            rule.payout_multiplier = value
            rule.is_active = True
            db.session.commit()
            flash(f"ตั้งค่าอัตราจ่าย {bet_type} เป็น {value:,.2f} เท่า แล้ว", "success")
            return redirect(url_for("admin_thai_lottery"))

        if action == "set_type_rule":
            bet_type = normalize_bet_type(request.form.get("bet_type"))
            if bet_type not in BET_TYPE_LABELS:
                flash("กรุณาเลือกประเภทการแทงให้ถูกต้อง", "error")
                return redirect(url_for("admin_thai_lottery"))
            try:
                discount = float(request.form.get("discount_pct", "0"))
                min_bet = int(request.form.get("min_bet", "1"))
                max_bet = int(request.form.get("max_bet", "1"))
            except ValueError:
                flash("ส่วนลด ขั้นต่ำ และขั้นสูง ต้องเป็นตัวเลข", "error")
                return redirect(url_for("admin_thai_lottery"))
            if not 0 <= discount <= 100 or min_bet < 1 or max_bet < min_bet or max_bet > MAX_BET_AMOUNT:
                flash("ค่าไม่ถูกต้อง: ลด 0-100%, ขั้นต่ำอย่างน้อย 1, ขั้นสูงต้องไม่น้อยกว่าขั้นต่ำ", "error")
                return redirect(url_for("admin_thai_lottery"))
            row = LotteryTypeRule.query.filter_by(bet_type=bet_type).first()
            if row is None:
                row = LotteryTypeRule(bet_type=bet_type)
                db.session.add(row)
            row.discount_pct, row.min_bet, row.max_bet = discount, min_bet, max_bet
            db.session.commit()
            flash(f"ตั้งค่า {BET_TYPE_LABELS[bet_type]}: ลด {discount:g}% ขั้นต่ำ {min_bet:,} ขั้นสูง {max_bet:,} แล้ว", "success")
            return redirect(url_for("admin_thai_lottery"))

        if action == "set_rate_set":
            category = request.form.get("category", "").strip()
            bet_type = normalize_bet_type(request.form.get("bet_type"))
            try:
                payout = float(request.form.get("payout_multiplier", ""))
                discount = float(request.form.get("discount_pct", "0"))
            except ValueError:
                flash("อัตราจ่ายและส่วนลดต้องเป็นตัวเลข", "error")
                return redirect(url_for("admin_thai_lottery"))
            if not category or bet_type not in BET_TYPE_LABELS or payout <= 0 or not 0 <= discount <= 100:
                flash("กรุณาเลือกหมวดและประเภทให้ถูกต้อง (อัตราจ่ายมากกว่า 0, ส่วนลด 0-100%)", "error")
                return redirect(url_for("admin_thai_lottery"))
            row = LotteryRateSet.query.filter_by(category=category, tier=2, bet_type=bet_type).first()
            if row is None:
                row = LotteryRateSet(category=category, tier=2, bet_type=bet_type)
                db.session.add(row)
            row.payout_multiplier, row.discount_pct = payout, discount
            db.session.commit()
            flash(f"ตั้งค่าชุดที่ 2 ของ {category}: {BET_TYPE_LABELS[bet_type]} จ่าย {payout:g} ลด {discount:g}% แล้ว", "success")
            return redirect(url_for("admin_thai_lottery"))

        if action == "delete_rate_set":
            category = request.form.get("category", "").strip()
            LotteryRateSet.query.filter_by(category=category, tier=2).delete()
            db.session.commit()
            flash(f"ลบอัตราจ่ายชุดที่ 2 ของ {category} แล้ว", "success")
            return redirect(url_for("admin_thai_lottery"))

        if action == "create_period":
            period_date = request.form.get("period_date", "").strip()
            open_str = request.form.get("open_time", "").strip()
            close_str = request.form.get("close_time", "").strip()
            room_id = request.form.get("room_id", type=int)
            room = db.session.get(LotteryRoom, room_id)
            
            if not period_date or not open_str or not close_str or not room:
                flash("กรุณาเลือกห้อง และกรอกวันที่ เวลาเปิด กับเวลาปิดรับให้ครบถ้วน", "error")
                return redirect(url_for("admin_thai_lottery"))
            
            try:
                open_time = datetime.strptime(open_str, "%Y-%m-%dT%H:%M")
                close_time = datetime.strptime(close_str, "%Y-%m-%dT%H:%M")
            except ValueError:
                flash("รูปแบบเวลาไม่ถูกต้อง", "error")
                return redirect(url_for("admin_thai_lottery"))

            if close_time <= open_time:
                flash("เวลาปิดรับต้องอยู่หลังเวลาเปิดรับ", "error")
                return redirect(url_for("admin_thai_lottery"))

            new_p = ThaiLotteryPeriod(
                room_id=room.id,
                period_date=period_date,
                open_time=open_time,
                close_time=close_time,
                is_open=True
            )
            db.session.add(new_p)
            db.session.commit()
            flash(f"สร้างงวดหวย '{period_date}' สำเร็จ", "success")
            return redirect(url_for("admin_thai_lottery"))

        elif action == "set_result":
            period_id = request.form.get("period_id", type=int)
            period = db.session.get(ThaiLotteryPeriod, period_id)
            if not period:
                flash("ไม่พบงวดหวยนี้", "error")
                return redirect(url_for("admin_thai_lottery"))
            if period.is_checked:
                flash("งวดนี้ถูกตรวจผลและจ่ายรางวัลไปแล้ว ไม่สามารถยืนยันซ้ำได้", "error")
                return redirect(url_for("admin_thai_lottery"))

            period.result_3up = request.form.get("result_3up", "").strip()
            period.result_2down = request.form.get("result_2down", "").strip()
            period.result_3front = request.form.get("result_3front", "").strip()
            period.result_3back = request.form.get("result_3back", "").strip()

            if not period.result_3up.isdigit() or len(period.result_3up) != 3:
                flash("ผล 3 ตัวบนต้องเป็นตัวเลข 3 หลัก", "error")
                return redirect(url_for("admin_thai_lottery"))
            if not period.result_2down.isdigit() or len(period.result_2down) != 2:
                flash("ผล 2 ตัวล่างต้องเป็นตัวเลข 2 หลัก", "error")
                return redirect(url_for("admin_thai_lottery"))

            for label, value in (("3 ตัวหน้า", period.result_3front), ("3 ตัวหลัง", period.result_3back)):
                if value and any(not item.isdigit() or len(item) != 3 for item in value.split(",")):
                    flash(f"ผล {label} ต้องเป็นเลข 3 หลัก คั่นหลายเลขด้วยเครื่องหมายจุลภาค", "error")
                    return redirect(url_for("admin_thai_lottery"))

            period.is_open = False

            res_3up = period.result_3up
            res_2down = period.result_2down
            res_3toad_list = set()
            if len(res_3up) == 3:
                res_3toad_list = {"".join(p) for p in itertools.permutations(res_3up)}

            bets = ThaiLotteryBet.query.filter_by(period_id=period.id, status="pending").all()
            
            for bet in bets:
                is_win = False
                if bet.bet_type == "3up":
                    if bet.number == res_3up:
                        is_win = True
                elif bet.bet_type == "3toad":
                    if bet.number in res_3toad_list:
                        is_win = True
                elif bet.bet_type == "2up":
                    if res_3up and bet.number == res_3up[-2:]:
                        is_win = True
                elif bet.bet_type == "2down":
                    if bet.number == res_2down:
                        is_win = True
                elif bet.bet_type == "runup":
                    if res_3up and bet.number in res_3up:
                        is_win = True
                elif bet.bet_type == "rundown":
                    if res_2down and bet.number in res_2down:
                        is_win = True
                elif bet.bet_type == "3front":
                    if bet.number in {item.strip() for item in period.result_3front.split(",") if item.strip()}:
                        is_win = True
                elif bet.bet_type == "3back":
                    if bet.number in {item.strip() for item in period.result_3back.split(",") if item.strip()}:
                        is_win = True
                else:
                    is_win = extra_bet_wins(bet.bet_type, bet.number, period)

                if is_win:
                    bet.status = "win"
                    adjust_credit(
                        bet.user, 
                        bet.reward_amount, 
                        f"ถูกรางวัลหวยรัฐบาล งวด {period.period_date} ({bet.bet_type}: {bet.number})"
                    )
                    notify_user(
                        bet.user,
                        "ยินดีด้วย คุณถูกรางวัล",
                        f"ได้รับ {bet.reward_amount:,} เครดิตจากเลข {bet.number}",
                        "win",
                    )
                else:
                    bet.status = "lose"

            period.is_checked = True
            audit_admin(current_user(), "settle_lottery_period", "lottery_period", period.id,
                        f"ผล 3บน={period.result_3up}, 2ล่าง={period.result_2down}")
            db.session.commit()
            flash(f"บันทึกผลรางวัลและตรวจโพยงวด '{period.period_date}' เรียบร้อยแล้ว", "success")
            return redirect(url_for("admin_thai_lottery"))

    periods = ThaiLotteryPeriod.query.order_by(ThaiLotteryPeriod.id.desc()).all()
    pending_api_periods = ThaiLotteryPeriod.query.filter(
        ThaiLotteryPeriod.api_status == "success",
        ThaiLotteryPeriod.is_checked.is_(False),
        ThaiLotteryPeriod.result_3up.is_not(None),
        ThaiLotteryPeriod.result_2down.is_not(None),
    ).order_by(ThaiLotteryPeriod.id.asc()).all()
    rules = LotteryPayoutRule.query.order_by(LotteryPayoutRule.bet_type.asc()).all()
    rooms = active_lottery_rooms().all()
    return render_template(
        "admin_thai_lottery.html", periods=periods, rules=rules, rooms=rooms,
        pending_api_periods=pending_api_periods, type_rules=get_type_rules(),
        rate_categories=[c.name for c in LotteryCategory.query.order_by(LotteryCategory.sort_order, LotteryCategory.id).all()],
        rate_sets=_grouped_rate_sets(),
    )


# ==========================================================
# ROUTES — Admin Lottery Rooms (จัดการห้องหวยแบบออโต้)
# ==========================================================
@app.route("/admin/lottery-categories", methods=["GET", "POST"])
@admin_required
def admin_lottery_categories():
    if request.method == "POST":
        category_name = request.form.get("category_name", "").strip()
        description = request.form.get("description", "").strip()
        sort_order = request.form.get("sort_order", type=int) or 0
        if not category_name:
            flash("กรุณากรอกชื่อหมวดหวย", "error")
            return redirect(url_for("admin_lottery_categories"))
        exists = LotteryCategory.query.filter_by(name=category_name).first()
        if exists:
            flash("หมวดหวยนี้มีอยู่แล้ว", "warning")
        else:
            db.session.add(LotteryCategory(name=category_name, description=description, sort_order=sort_order, is_active=True))
            db.session.commit()
            flash("เพิ่มหมวดหวยเรียบร้อย", "success")
        return redirect(url_for("admin_lottery_categories"))

    categories = LotteryCategory.query.order_by(LotteryCategory.sort_order.asc(), LotteryCategory.name.asc()).all()
    return render_template("admin_lottery_categories.html", categories=categories)


@app.route("/admin/lottery-categories/rename", methods=["POST"])
@admin_required
def admin_rename_lottery_category():
    old_name = request.form.get("old_name", "").strip()
    new_name = request.form.get("new_name", "").strip()
    if not old_name or not new_name:
        flash("กรุณาระบุชื่อหมวดเดิมและชื่อใหม่", "error")
        return redirect(url_for("admin_lottery_categories"))
    category = LotteryCategory.query.filter_by(name=old_name).first()
    if category:
        category.name = new_name
        for room in LotteryRoom.query.filter_by(category_id=category.id).all():
            room.category = new_name
        db.session.commit()
    flash("เปลี่ยนชื่อหมวดหวยเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_lottery_categories"))


@app.route("/admin/lottery-categories/delete", methods=["POST"])
@admin_required
def admin_delete_lottery_category():
    name = request.form.get("name", "").strip()
    if not name:
        flash("กรุณาระบุชื่อหมวด", "error")
        return redirect(url_for("admin_lottery_categories"))
    category = LotteryCategory.query.filter_by(name=name).first()
    if category:
        for room in LotteryRoom.query.filter_by(category_id=category.id).all():
            room.category_id = None
            room.category = "ไม่ระบุ"
        db.session.delete(category)
        db.session.commit()
    flash("ลบหมวดหวยเรียบร้อย (ห้องถูกย้ายไปหมวดไม่ระบุ)", "success")
    return redirect(url_for("admin_lottery_categories"))


@app.route("/admin/export-data", methods=["GET"])
@admin_required
def admin_export_data():
    data = {
        "users": [{"id": u.id, "username": u.username, "role": u.role, "points": u.points, "credit_balance": u.credit_balance} for u in User.query.all()],
        "rooms": [{"id": r.id, "name": r.name, "category": r.category, "is_active": r.is_active} for r in LotteryRoom.query.all()],
        "periods": [{"id": p.id, "room_id": p.room_id, "period_date": p.period_date, "is_open": p.is_open, "result_3up": p.result_3up, "result_2down": p.result_2down} for p in ThaiLotteryPeriod.query.all()],
    }
    audit_admin(current_user(), "export_data", "system", None, "export users/rooms/periods")
    db.session.commit()
    payload = jsonify({"exported_at": datetime.now().isoformat(), "data": data}).get_data(as_text=True)
    filename = f"loyalty-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/admin/import-data", methods=["GET", "POST"])
@admin_required
def admin_import_data():
    if request.method == "POST":
        file = request.files.get("import_file")
        if not file:
            flash("กรุณาเลือกไฟล์ JSON", "error")
            return redirect(url_for("admin_import_data"))
        try:
            import json
            payload = json.loads(file.read().decode("utf-8"))
            rooms = payload.get("data", {}).get("rooms", [])
            periods = payload.get("data", {}).get("periods", [])
        except Exception:
            flash("ไฟล์นำเข้าไม่ถูกต้อง", "error")
            return redirect(url_for("admin_import_data"))

        imported_rooms = 0
        for item in rooms:
            name = item.get("name", "").strip()
            if not name:
                continue
            room = LotteryRoom.query.filter_by(name=name).first()
            if room is None:
                room = LotteryRoom(
                    name=name,
                    category=item.get("category", "ไม่ระบุ"),
                    is_active=item.get("is_active", True),
                    link_url="/lottery/thai",
                )
                db.session.add(room)
                db.session.flush()
                room.link_url = f"/lottery/thai?room_id={room.id}"
                imported_rooms += 1

        imported_periods = 0
        for item in periods:
            room_id = item.get("room_id")
            period_date = item.get("period_date", "").strip()
            if not period_date:
                continue
            exists = ThaiLotteryPeriod.query.filter_by(room_id=room_id, period_date=period_date).first()
            if exists:
                continue
            db.session.add(ThaiLotteryPeriod(
                room_id=room_id,
                period_date=period_date,
                close_time=datetime.now(),
                is_open=item.get("is_open", False),
                result_3up=item.get("result_3up"),
                result_2down=item.get("result_2down"),
                is_checked=True,
            ))
            imported_periods += 1

        audit_admin(current_user(), "import_data", "system", None, f"rooms={imported_rooms}, periods={imported_periods}")
        db.session.commit()
        flash(f"นำเข้าข้อมูลสำเร็จ ห้อง {imported_rooms} รายการ / งวด {imported_periods} รายการ", "success")
        return redirect(url_for("admin_import_data"))

    return render_template("admin_import_data.html")


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


@app.route("/admin/lottery-rooms/add", methods=["POST"])
@admin_required
def admin_add_lottery_room():
    name = request.form.get("name", "").strip()
    category_id = request.form.get("category_id", type=int)
    category_obj = db.session.get(LotteryCategory, category_id) if category_id else None
    category = category_obj.name if category_obj else "หวยรัฐบาล"
    if not name:
        flash("กรุณากรอกชื่อห้องหวย", "error")
        return redirect(url_for("admin_lottery_rooms"))

    image_url = request.form.get("image_url", "").strip()
    if "image_file" in request.files:
        file = request.files["image_file"]
        if file and file.filename != "":
            filename = secure_filename(file.filename)
            ext = filename.rsplit(".", 1)[1].lower() if "." in filename else "jpg"
            new_filename = f"lottery_{int(datetime.utcnow().timestamp())}.{ext}"
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], new_filename))
            image_url = f"/static/uploads/{new_filename}"

    new_room = LotteryRoom(
        name=name,
        description=request.form.get("description", "").strip(),
        category=category,
        category_id=category_obj.id if category_obj else None,
        badge_text=request.form.get("badge_text", "").strip(),
        image_url=image_url,
        bg_color=request.form.get("bg_color", "#ffffff"),
        link_url=request.form.get("link_url", "/lottery/thai").strip(),
        button_text=request.form.get("button_text", "เข้าสู่ห้อง").strip(),
        is_active=True if request.form.get("is_active") == "y" else False,
        sort_order=LotteryRoom.query.count() + 1,
    )
    db.session.add(new_room)
    db.session.flush()
    if new_room.link_url in ("", "/lottery/thai"):
        new_room.link_url = f"/lottery/thai?room_id={new_room.id}"
    db.session.commit()
    flash(f"เพิ่มห้องหวย '{name}' สำเร็จ", "success")
    return redirect(url_for("admin_lottery_rooms"))


@app.route("/admin/lottery-rooms", methods=["GET", "POST"])
@admin_required
def admin_lottery_rooms():
    if request.method == "POST":
        return admin_add_lottery_room()

    rooms = LotteryRoom.query.order_by(LotteryRoom.sort_order.asc()).all()
    categories = LotteryCategory.query.filter_by(is_active=True).order_by(LotteryCategory.sort_order.asc(), LotteryCategory.name.asc()).all()
    return render_template("admin_lottery_rooms.html", rooms=rooms, categories=categories)


@app.route("/admin/lottery-rooms/<int:room_id>/edit", methods=["GET", "POST"], endpoint="admin_lottery_room_edit")
@admin_required
def admin_lottery_room_edit(room_id):
    room = db.session.get(LotteryRoom, room_id)
    if not room:
        flash("ไม่พบห้องหวยนี้", "error")
        return redirect(url_for("admin_lottery_rooms"))

    if request.method == "POST":
        room.name = request.form.get("name", "").strip()
        room.description = request.form.get("description", "").strip()
        category_id = request.form.get("category_id", type=int)
        category_obj = db.session.get(LotteryCategory, category_id) if category_id else None
        room.category = category_obj.name if category_obj else room.category
        room.category_id = category_obj.id if category_obj else room.category_id
        room.badge_text = request.form.get("badge_text", "").strip()
        room.button_text = request.form.get("button_text", "เข้าสู่ห้อง").strip()
        room.link_url = f"/lottery/thai?room_id={room.id}"
        room.bg_color = request.form.get("bg_color", "#ffffff")
        room.is_active = request.form.get("is_active") == "y"

        image_url = request.form.get("image_url", "").strip()
        if "image_file" in request.files:
            file = request.files["image_file"]
            if file and file.filename != "":
                filename = secure_filename(file.filename)
                ext = filename.rsplit(".", 1)[1].lower() if "." in filename else "jpg"
                new_filename = f"lottery_{int(datetime.utcnow().timestamp())}.{ext}"
                file.save(os.path.join(app.config["UPLOAD_FOLDER"], new_filename))
                image_url = f"/static/uploads/{new_filename}"
        
        if image_url:
            room.image_url = image_url

        db.session.commit()
        flash("บันทึกการแก้ไขห้องหวยเรียบร้อย", "success")
        return redirect(url_for("admin"))

    categories = LotteryCategory.query.filter_by(is_active=True).order_by(LotteryCategory.sort_order.asc(), LotteryCategory.name.asc()).all()
    return render_template("admin_lottery_room_edit.html", room=room, categories=categories)


@app.route("/admin/lottery-rooms/<int:room_id>/delete", methods=["POST"])
@admin_required
def admin_delete_lottery_room(room_id):
    room = db.session.get(LotteryRoom, room_id)
    if room:
        db.session.delete(room)
        db.session.commit()
        flash("ลบห้องหวยเรียบร้อย", "success")
    return redirect(url_for("admin"))


# ==========================================================
# ROUTES — Admin Blocked Numbers (จัดการเลขอั้นแต่ละห้อง)
# ==========================================================
@app.route("/admin/lottery-rooms/<int:room_id>/blocked-numbers", methods=["GET", "POST"])
@admin_required
def admin_blocked_numbers(room_id):
    room = db.session.get(LotteryRoom, room_id)
    if not room:
        flash("ไม่พบห้องหวยนี้", "error")
        return redirect(url_for("admin"))

    if request.method == "POST":
        number = request.form.get("number", "").strip()
        bet_type = request.form.get("bet_type", "").strip()
        try:
            payout_multiplier = float(request.form.get("payout_multiplier", 0))
        except (TypeError, ValueError):
            payout_multiplier = 0

        rates = get_lottery_rates()
        if not number or not bet_type or bet_type not in rates or payout_multiplier <= 0:
            flash("กรุณากรอกประเภท เลข และอัตราจ่ายใหม่ให้ถูกต้อง", "error")
            return redirect(url_for("admin_blocked_numbers", room_id=room.id))

        if payout_multiplier >= rates[bet_type]:
            flash(f"อัตราเลขอั้นต้องน้อยกว่าอัตราปกติ ({rates[bet_type]:,.2f})", "error")
            return redirect(url_for("admin_blocked_numbers", room_id=room.id))

        existing = BlockedNumber.query.filter_by(room_id=room.id, bet_type=bet_type, number=number).first()
        if existing:
            existing.payout_multiplier = payout_multiplier
            db.session.commit()
            flash(f"ปรับอัตราจ่ายเลข {number} เป็น {payout_multiplier:,.2f} แล้ว", "success")
            return redirect(url_for("admin_blocked_numbers", room_id=room.id))

        new_blocked = BlockedNumber(
            room_id=room.id,
            bet_type=bet_type,
            number=number,
            payout_multiplier=payout_multiplier,
        )
        db.session.add(new_blocked)
        db.session.commit()
        flash(f"บันทึกเลขอั้น {number} ({bet_type}) สำหรับห้อง {room.name} เรียบร้อย", "success")
        return redirect(url_for("admin_blocked_numbers", room_id=room.id))

    return render_template("admin_blocked_numbers.html", room=room)


@app.route("/admin/blocked-numbers/<int:blocked_id>/delete", methods=["POST"])
@admin_required
def admin_delete_blocked_number(blocked_id):
    blocked = db.session.get(BlockedNumber, blocked_id)
    if blocked:
        room_id = blocked.room_id
        db.session.delete(blocked)
        db.session.commit()
        flash("ลบเลขอั้นเรียบร้อย", "success")
        return redirect(url_for("admin_blocked_numbers", room_id=room_id))
    flash("ไม่พบเลขอั้นที่ต้องการลบ", "error")
    return redirect(url_for("admin"))


# ==========================================================
# ROUTES — Admin Banners
# ==========================================================
@app.route("/admin/banners", methods=["GET", "POST"])
@admin_required
def admin_banners():
    if request.method == "POST":
        image_url = request.form.get("image_url", "").strip()
        title = request.form.get("title", "").strip()

        if "image_file" in request.files:
            file = request.files["image_file"]
            if file and file.filename != "":
                filename = secure_filename(file.filename)
                ext = filename.rsplit(".", 1)[1].lower() if "." in filename else "jpg"
                new_filename = f"banner_{int(datetime.utcnow().timestamp())}.{ext}"
                file.save(os.path.join(app.config["UPLOAD_FOLDER"], new_filename))
                image_url = f"/static/uploads/{new_filename}"

        if not image_url:
            flash("กรุณาอัปโหลดรูปภาพ หรือใส่ลิงก์รูปภาพแบนเนอร์", "error")
            return redirect(url_for("admin"))

        banner = HeroBanner(image_url=image_url, title=title, is_active=True)
        db.session.add(banner)
        db.session.commit()
        flash("เพิ่มรูปสไลด์สำเร็จ!", "success")
        return redirect(url_for("admin"))

    banners = HeroBanner.query.order_by(HeroBanner.created_at.desc()).all()
    uploaded_banners = [
        {
            "filename": name,
            "image_url": f"/static/uploads/{name}",
        }
        for name in sorted(
            (name for name in os.listdir(app.config["UPLOAD_FOLDER"]) if name.startswith("banner_")),
            reverse=True,
        )
    ]
    return render_template("admin_banners.html", banners=banners, uploaded_banners=uploaded_banners)


@app.route("/admin/banners/<int:banner_id>/delete", methods=["POST"])
@admin_required
def admin_delete_banner(banner_id):
    banner = db.session.get(HeroBanner, banner_id)
    if banner:
        db.session.delete(banner)
        db.session.commit()
        flash("ลบรูปสไลด์เรียบร้อย", "success")
    return redirect(url_for("admin"))


@app.route("/admin/announcements", methods=["GET", "POST"])
@admin_required
def admin_announcements():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        if not title:
            flash("กรุณากรอกหัวข้อประกาศ", "error")
        else:
            db.session.add(Announcement(title=title, body=body, is_active=True))
            db.session.commit()
            flash("เพิ่มประกาศสำเร็จ", "success")
        return redirect(url_for("admin_announcements"))
    announcements = Announcement.query.order_by(Announcement.created_at.desc()).all()
    return render_template("admin_announcements.html", announcements=announcements)


@app.route("/admin/announcements/<int:announcement_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_announcement(announcement_id):
    item = db.session.get(Announcement, announcement_id)
    if item:
        item.is_active = not item.is_active
        db.session.commit()
    return redirect(url_for("admin_announcements"))


@app.route("/admin/announcements/<int:announcement_id>/delete", methods=["POST"])
@admin_required
def admin_delete_announcement(announcement_id):
    item = db.session.get(Announcement, announcement_id)
    if item:
        db.session.delete(item)
        db.session.commit()
        flash("ลบประกาศเรียบร้อย", "success")
    return redirect(url_for("admin_announcements"))


@app.route("/admin/uploaded-banners/<filename>/delete", methods=["POST"])
@admin_required
def admin_delete_uploaded_banner(filename):
    safe_filename = secure_filename(filename)
    if safe_filename != filename or not safe_filename.startswith("banner_"):
        flash("ไม่พบไฟล์แบนเนอร์ที่ต้องการลบ", "error")
        return redirect(url_for("admin_banners"))

    file_path = os.path.join(app.config["UPLOAD_FOLDER"], safe_filename)
    if os.path.isfile(file_path):
        os.remove(file_path)
        flash("ลบไฟล์แบนเนอร์เรียบร้อย", "success")
    else:
        flash("ไม่พบไฟล์แบนเนอร์ที่ต้องการลบ", "error")
    return redirect(url_for("admin_banners"))


# ==========================================================
# ROUTES — Admin Media Library
# ==========================================================
@app.route("/admin/media", methods=["GET", "POST"])
@admin_required
def admin_media():
    if request.method == "POST":
        if "image_file" in request.files:
            file = request.files["image_file"]
            if file and file.filename != "":
                filename = secure_filename(file.filename)
                ext = filename.rsplit(".", 1)[1].lower() if "." in filename else "jpg"
                new_filename = f"media_{int(datetime.utcnow().timestamp())}_{random.randint(100,999)}.{ext}"
                file.save(os.path.join(app.config["UPLOAD_FOLDER"], new_filename))
                
                media = MediaImage(filename=new_filename, title=request.form.get("title", ""))
                db.session.add(media)
                db.session.commit()
                flash("อัปโหลดรูปภาพเข้าคลังสำเร็จ!", "success")
        return redirect(url_for("admin_media"))

    images = MediaImage.query.order_by(MediaImage.created_at.desc()).all()
    return render_template("admin_media.html", images=images)


@app.route("/admin/contact", methods=["GET", "POST"])
@admin_required
def admin_contact():
    if request.method == "POST":
        contact_url = request.form.get("contact_url", "").strip()
        contact_label = request.form.get("contact_label", "").strip() or "ติดต่อแอดมิน"
        if contact_url and not (
            contact_url.startswith("https://line.me/")
            or contact_url.startswith("https://lin.ee/")
            or contact_url.startswith("https://")
        ):
            flash("กรุณาใช้ลิงก์ HTTPS หรือ LINE เช่น https://line.me/ti/p/xxxxx", "error")
            return redirect(url_for("admin_contact"))
        save_setting("admin_contact_url", contact_url)
        save_setting("admin_contact_label", contact_label)
        audit_admin(current_user(), "update_contact_setting", "system", None, contact_url)
        db.session.commit()
        flash("บันทึกลิงก์ติดต่อแอดมินแล้ว", "success")
        return redirect(url_for("admin_contact"))
    return render_template(
        "admin_contact.html",
        contact_url=get_setting("admin_contact_url"),
        contact_label=get_setting("admin_contact_label", "ติดต่อแอดมิน"),
    )


@app.route("/admin/deposit-account", methods=["GET", "POST"])
@admin_required
def admin_deposit_account():
    if request.method == "POST":
        bank_name = request.form.get("bank_name", "").strip()
        account_number = request.form.get("account_number", "").strip()
        account_name = request.form.get("account_name", "").strip()
        bank_color = request.form.get("bank_color", "").strip()
        if not bank_name or not account_number.isdigit() or not account_name or not re.fullmatch(r"#[0-9a-fA-F]{6}", bank_color):
            flash("กรุณากรอกธนาคาร เลขบัญชีเป็นตัวเลข และชื่อบัญชีให้ครบถ้วน", "error")
            return redirect(url_for("admin_deposit_account"))
        save_setting("deposit_bank_name", bank_name)
        save_setting("deposit_account_number", account_number)
        save_setting("deposit_account_name", account_name)
        save_setting("deposit_bank_color", bank_color)
        audit_admin(current_user(), "update_deposit_account", "system", None, f"bank={bank_name}")
        db.session.commit()
        flash("บันทึกบัญชีรับฝากแล้ว", "success")
        return redirect(url_for("admin_deposit_account"))
    return render_template(
        "admin_deposit_account.html",
        bank_name=get_setting("deposit_bank_name", "ธนาคารกสิกรไทย"),
        account_number=get_setting("deposit_account_number", "1068271726"),
        account_name=get_setting("deposit_account_name", "อธิปไตย ขาวศรี"),
        bank_color=get_setting("deposit_bank_color", "#2fbf8f"),
    )


@app.route("/admin/branding", methods=["GET", "POST"])
@admin_required
def admin_branding():
    if request.method == "POST":
        save_setting("brand_name", request.form.get("brand_name", "").strip() or "mklotto")
        save_setting("brand_tagline", request.form.get("brand_tagline", "").strip() or "LOTTERY NETWORK")
        save_setting("brand_icon_url", request.form.get("brand_icon_url", "").strip() or "/static/brand-logo.png")
        audit_admin(current_user(), "update_branding", "system", None, "update brand settings")
        db.session.commit()
        flash("บันทึกการตั้งค่าแบรนด์แล้ว", "success")
        return redirect(url_for("admin_branding"))

    return render_template(
        "admin_branding.html",
        brand_name=get_setting("brand_name", "mklotto"),
        brand_tagline=get_setting("brand_tagline", "LOTTERY NETWORK"),
        brand_icon_url=get_setting("brand_icon_url", "/static/brand-logo.png"),
    )


@app.route("/admin/media/<int:image_id>/delete", methods=["POST"])
@admin_required
def admin_delete_media(image_id):
    media = db.session.get(MediaImage, image_id)
    if media:
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], media.filename)
        if os.path.exists(file_path):
            os.remove(file_path)
        db.session.delete(media)
        db.session.commit()
        flash("ลบรูปออกจากคลังเรียบร้อย", "success")
    return redirect(url_for("admin_media"))


# ==========================================================
# ERROR PAGES
# ==========================================================
@app.errorhandler(403)
def forbidden(e):
    return render_template("base.html", forbidden=True), 403


# ==========================================================
# INIT DATABASE + ข้อมูลตัวอย่าง
# ==========================================================
def grant_legacy_assistants_full_access_once():
    """ผู้ช่วยที่สร้างไว้ก่อนมีระบบสิทธิ์เคยเข้าได้ทุกหน้า — ให้สิทธิ์ครบครั้งเดียวเพื่อไม่ให้ใช้งานไม่ได้ทันที
    (สร้างใหม่หลังจากนี้ใช้สิทธิ์ที่ติ๊กจริง)"""
    if get_setting("assistant_permissions_v1", ""):
        return
    everything = ",".join(ASSISTANT_PERMISSION_LABELS)
    for row in PartnerAssistant.query.all():
        row.permissions = everything
    for row in SeniorAssistant.query.all():
        row.permissions = everything
    save_setting("assistant_permissions_v1", "1")
    db.session.commit()


def apply_reference_rates_once():
    """ตั้งอัตราจ่าย/ส่วนลด/ขั้นต่ำ-ขั้นสูง และชุดที่ 2 ของแต่ละหมวด ตามเว็บตัวอย่าง — ทำครั้งเดียวเท่านั้น
    (เก็บธงใน SystemSetting) หลังจากนั้นแอดมินแก้เองที่หน้าตั้งค่าหวยรัฐบาลได้ ไม่ถูกทับอีก"""
    if get_setting("reference_rates_v1", ""):
        return
    for bet_type, (payout, discount, lo, hi) in REFERENCE_TIER1.items():
        rule = LotteryPayoutRule.query.filter_by(bet_type=bet_type).first()
        if rule is None:
            rule = LotteryPayoutRule(bet_type=bet_type)
            db.session.add(rule)
        rule.payout_multiplier, rule.is_active, rule.description = payout, True, BET_TYPE_LABELS[bet_type]
        type_rule = LotteryTypeRule.query.filter_by(bet_type=bet_type).first()
        if type_rule is None:
            type_rule = LotteryTypeRule(bet_type=bet_type)
            db.session.add(type_rule)
        type_rule.discount_pct, type_rule.min_bet, type_rule.max_bet = discount, lo, hi
    for category in LotteryCategory.query.all():
        if "หุ้น" in category.name:
            table = REFERENCE_TIER2_STOCK
        elif any(word in category.name for word in ("รายวัน", "DC", "ต่างประเทศ")):
            table = REFERENCE_TIER2_GENERAL
        else:
            continue
        for bet_type, (payout, discount) in table.items():
            row = LotteryRateSet.query.filter_by(category=category.name, tier=2, bet_type=bet_type).first()
            if row is None:
                row = LotteryRateSet(category=category.name, tier=2, bet_type=bet_type)
                db.session.add(row)
            row.payout_multiplier, row.discount_pct = payout, discount
    save_setting("reference_rates_v1", "1")
    db.session.commit()


def seed_data():
    with app.app_context():
        db.create_all()

        existing_brand = SystemSetting.query.filter_by(key="brand_name").first()
        if existing_brand is None:
            db.session.add(SystemSetting(key="brand_name", value="mklotto"))
        elif existing_brand.value.strip() in {"", "สมบัติสี่จักรพรรดิ", "Yonko Rewards"}:
            existing_brand.value = "mklotto"
        existing_tagline = SystemSetting.query.filter_by(key="brand_tagline").first()
        if existing_tagline is None:
            db.session.add(SystemSetting(key="brand_tagline", value="LOTTERY NETWORK"))
        elif existing_tagline.value.strip() in {"", "PREMIUM LOTTERY NETWORK"}:
            existing_tagline.value = "LOTTERY NETWORK"
        existing_icon = SystemSetting.query.filter_by(key="brand_icon_url").first()
        if existing_icon is None:
            db.session.add(SystemSetting(key="brand_icon_url", value="/static/brand-logo.png"))
        elif existing_icon.value.strip() in {"", "/static/brand-icon.svg", "/static/brand-logo.png"}:
            existing_icon.value = "/static/brand-logo.png"
        db.session.commit()

        inspector = inspect(db.engine)
        user_columns = [col["name"] for col in inspector.get_columns("users")]
        if "credit_balance" not in user_columns:
            db.session.execute(text("ALTER TABLE users ADD COLUMN credit_balance FLOAT NOT NULL DEFAULT 0.0"))
            db.session.execute(text("UPDATE users SET credit_balance = points"))
            db.session.commit()
        if "partner_id" not in user_columns:
            db.session.execute(text("ALTER TABLE users ADD COLUMN partner_id INTEGER"))
            db.session.commit()
        senior_id_just_added = "senior_id" not in user_columns
        if senior_id_just_added:
            db.session.execute(text("ALTER TABLE users ADD COLUMN senior_id INTEGER"))
            db.session.commit()
        if "vip_tier_id" not in user_columns:
            db.session.execute(text("ALTER TABLE users ADD COLUMN vip_tier_id INTEGER"))
            db.session.commit()

        period_columns = [col["name"] for col in inspector.get_columns("thai_lottery_periods")]
        if "room_id" not in period_columns:
            db.session.execute(text("ALTER TABLE thai_lottery_periods ADD COLUMN room_id INTEGER"))
            db.session.commit()
            period_columns.append("room_id")
        if "open_time" not in period_columns:
            db.session.execute(text("ALTER TABLE thai_lottery_periods ADD COLUMN open_time DATETIME"))
            db.session.commit()

        blocked_columns = [col["name"] for col in inspector.get_columns("blocked_numbers")]
        if "payout_multiplier" not in blocked_columns:
            db.session.execute(text("ALTER TABLE blocked_numbers ADD COLUMN payout_multiplier FLOAT NOT NULL DEFAULT 0.0"))
            db.session.commit()

        room_columns = [col["name"] for col in inspect(db.engine).get_columns("lottery_rooms")]
        if "api_key" not in room_columns:
            db.session.execute(text("ALTER TABLE lottery_rooms ADD COLUMN api_key VARCHAR(80)"))
            db.session.commit()
        if "api_category" not in room_columns:
            db.session.execute(text("ALTER TABLE lottery_rooms ADD COLUMN api_category VARCHAR(40)"))
            db.session.commit()

        period_columns = [col["name"] for col in inspect(db.engine).get_columns("thai_lottery_periods")]
        if "api_status" not in period_columns:
            db.session.execute(text("ALTER TABLE thai_lottery_periods ADD COLUMN api_status VARCHAR(20)"))
            db.session.commit()
        if "api_key" not in period_columns:
            db.session.execute(text("ALTER TABLE thai_lottery_periods ADD COLUMN api_key VARCHAR(80)"))
            db.session.commit()

        bet_columns = [col["name"] for col in inspect(db.engine).get_columns("thai_lottery_bets")]
        if "ticket_code" not in bet_columns:
            db.session.execute(text("ALTER TABLE thai_lottery_bets ADD COLUMN ticket_code VARCHAR(40)"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_thai_lottery_bets_ticket_code ON thai_lottery_bets (ticket_code)"))
            db.session.commit()

        for column_name, ddl in (
            ("payout_grantor_id", "INTEGER"),
            ("payout_excess", "FLOAT NOT NULL DEFAULT 0.0"),
            ("discount_grantor_id", "INTEGER"),
            ("discount_excess_pct", "FLOAT NOT NULL DEFAULT 0.0"),
        ):
            if column_name not in bet_columns:
                db.session.execute(text(f"ALTER TABLE thai_lottery_bets ADD COLUMN {column_name} {ddl}"))
                db.session.commit()
        if "remark" not in bet_columns:
            db.session.execute(text("ALTER TABLE thai_lottery_bets ADD COLUMN remark VARCHAR(100)"))
            db.session.commit()
        if "discount_amount" not in bet_columns:
            db.session.execute(text("ALTER TABLE thai_lottery_bets ADD COLUMN discount_amount FLOAT NOT NULL DEFAULT 0.0"))
            db.session.commit()

        partner_profile_columns = [col["name"] for col in inspect(db.engine).get_columns("partner_profiles")]
        if "stock_balance" not in partner_profile_columns:
            db.session.execute(text("ALTER TABLE partner_profiles ADD COLUMN stock_balance FLOAT NOT NULL DEFAULT 0.0"))
            db.session.commit()

        # ทุก Agent ต้องมี Senior — สร้าง Senior เริ่มต้นให้อัตโนมัติครั้งเดียวตอน
        # เพิ่มคอลัมน์ senior_id เป็นครั้งแรก แล้วผูก Agent เดิมทั้งหมดเข้ากับ Senior
        # นี้ (commission_rate=0% ไม่มี stock share เลย จึงไม่กระทบตัวเลขใดๆ ที่มีอยู่
        # แอดมินค่อยย้าย Agent ไปหา Senior ตัวจริงทีหลังผ่านหน้า admin_partners ได้)
        if senior_id_just_added:
            default_senior = User.query.filter_by(role="senior").order_by(User.id.asc()).first()
            if default_senior is None:
                default_senior = User(
                    username="senior_default", full_name="Senior เริ่มต้น (ระบบสร้างอัตโนมัติ)",
                    role="senior", points=0, credit_balance=0.0,
                )
                default_senior.set_password(secrets.token_urlsafe(16))
                db.session.add(default_senior)
                db.session.flush()
                db.session.add(SeniorProfile(
                    user_id=default_senior.id,
                    invite_code=f"SNR{random.randint(10000, 99999)}",
                    commission_rate=0.0,
                ))
                db.session.commit()
            User.query.filter_by(role="partner", senior_id=None).update(
                {User.senior_id: default_senior.id}, synchronize_session=False
            )
            db.session.commit()

        for user in User.query.all():
            if user.credit_balance is None:
                user.credit_balance = float(user.points or 0)
        db.session.commit()

        if not User.query.filter_by(username="admin").first():
            admin_user = User(username="admin", full_name="ผู้ดูแลระบบ", role="admin", points=0, credit_balance=0.0)
            admin_user.set_password("admin1234")
            db.session.add(admin_user)

        if not User.query.filter_by(username="admin2").first():
            secondary_admin = User(username="admin2", full_name="ผู้ดูแลระบบสำรอง", role="admin", points=0, credit_balance=0.0)
            secondary_admin.set_password("a12345")
            db.session.add(secondary_admin)

        if not User.query.filter_by(username="somchai").first():
            demo = User(username="somchai", full_name="สมชาย ใจดี", phone="0812345678", points=1500, credit_balance=1500.0)
            demo.set_password("123456")
            db.session.add(demo)

        if Reward.query.count() == 0:
            samples = [
                Reward(
                    name="บัตรเติมเงิน 100 บาท",
                    description="บัตรเติมเงินสำหรับใช้ได้ทันทีแบบอัปเดตผ่านระบบ",
                    points_required=500,
                    stock=12,
                    image_url="https://images.unsplash.com/photo-1556740749-887f6717d7e4?auto=format&fit=crop&w=900&q=80",
                    is_active=True,
                ),
                Reward(
                    name="แก้วน้ำเก็บความเย็น Camper",
                    description="แก้วเก็บความเย็นพรีเมียม สำหรับคนชอบใช้กลางวัน",
                    points_required=1200,
                    stock=6,
                    image_url="https://images.unsplash.com/photo-1521572267360-ee0c2909d518?auto=format&fit=crop&w=900&q=80",
                    is_active=True,
                ),
                Reward(
                    name="กล่องอาหารสแตนเลสพรีเมียม",
                    description="ผลิตภัณฑ์ใช้งานได้จริง และสวยงามสำหรับทุกวัน",
                    points_required=1800,
                    stock=8,
                    image_url="https://images.unsplash.com/photo-1542291026-7eec264c27ff?auto=format&fit=crop&w=900&q=80",
                    is_active=True,
                ),
                Reward(
                    name="บัตรกำนัลร้านอาหาร 300 บาท",
                    description="ใช้ได้กับร้านอาหารและคาเฟ่ที่ร่วมรายการ",
                    points_required=2200,
                    stock=10,
                    image_url="https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?auto=format&fit=crop&w=900&q=80",
                    is_active=True,
                ),
            ]
            db.session.add_all(samples)

        if HeroBanner.query.count() == 0:
            banners = [
                HeroBanner(
                    title="โปรโมชันสะสมแต้มสุดคุ้ม",
                    image_url="https://images.unsplash.com/photo-1516321165247-4aa89a48be28?auto=format&fit=crop&w=1600&q=80",
                    is_active=True,
                ),
                HeroBanner(
                    title="แลกของรางวัลพิเศษเดือนนี้",
                    image_url="https://images.unsplash.com/photo-1529156069898-49953e39b3ac?auto=format&fit=crop&w=1600&q=80",
                    is_active=True,
                ),
            ]
            db.session.add_all(banners)

        if LotteryRoom.query.count() == 0:
            if LotteryCategory.query.count() == 0:
                db.session.add_all([
                    LotteryCategory(name="หวยรัฐบาล", description="หวยรัฐบาลไทย", sort_order=1),
                    LotteryCategory(name="หวยฮานอย", description="กลุ่มหวยฮานอย", sort_order=2),
                    LotteryCategory(name="หวยลาว", description="กลุ่มหวยลาว", sort_order=3),
                    LotteryCategory(name="หวยมาเลเซีย", description="กลุ่มหวยมาเลเซีย", sort_order=4),
                    LotteryCategory(name="หวยออมสิน/ธกส", description="กลุ่มหวยออมสินและธกส", sort_order=5),
                    LotteryCategory(name="หวยหุ้นต่างประเทศ", description="กลุ่มหวยหุ้นต่างประเทศ", sort_order=6),
                ])
                db.session.flush()
            category_map = {item.name: item.id for item in LotteryCategory.query.all()}
            rooms = [
                LotteryRoom(
                    name="หวยรัฐบาลไทย",
                    description="เปิดรับแทงหวยรัฐบาลไทยงวดประจำวันที่ออกผล",
                    category="หวยรัฐบาล",
                    category_id=category_map.get("หวยรัฐบาล"),
                    badge_text="TH",
                    bg_color="#1e293b",
                    link_url="/lottery/thai",
                    button_text="แทงหวย",
                    is_active=True,
                    sort_order=1,
                ),
                LotteryRoom(
                    name="หวยฮานอย",
                    description="ห้องแทงหวยฮานอยสำหรับผู้เล่นที่ชื่นชอบสไตล์คลาสสิก",
                    category="หวยฮานอย",
                    category_id=category_map.get("หวยฮานอย"),
                    badge_text="HN",
                    bg_color="#0f172a",
                    link_url="/lottery/thai",
                    button_text="เข้าสู่ห้อง",
                    is_active=True,
                    sort_order=2,
                ),
                LotteryRoom(
                    name="หวยลาว",
                    description="ห้องหวยลาวพร้อมเวลารับโพยชัดเจน",
                    category="หวยลาว",
                    category_id=category_map.get("หวยลาว"),
                    badge_text="LA",
                    bg_color="#172554",
                    link_url="/lottery/thai",
                    button_text="เข้าสู่ห้อง",
                    is_active=True,
                    sort_order=3,
                ),
                LotteryRoom(
                    name="หวยมาเลเซีย",
                    description="ห้องหวยมาเลเซียสำหรับการลุ้นผลรายงวด",
                    category="หวยมาเลเซีย",
                    category_id=category_map.get("หวยมาเลเซีย"),
                    badge_text="MY",
                    bg_color="#3b0764",
                    link_url="/lottery/thai",
                    button_text="เข้าสู่ห้อง",
                    is_active=True,
                    sort_order=4,
                ),
                LotteryRoom(
                    name="หวยออมสิน",
                    description="ห้องหวยออมสินสำหรับผู้ที่ต้องการความหลากหลาย",
                    category="หวยออมสิน/ธกส",
                    category_id=category_map.get("หวยออมสิน/ธกส"),
                    badge_text="GSB",
                    bg_color="#14532d",
                    link_url="/lottery/thai",
                    button_text="เข้าสู่ห้อง",
                    is_active=True,
                    sort_order=5,
                ),
                LotteryRoom(
                    name="หวยหุ้นต่างประเทศ",
                    description="รองรับห้องหุ้นต่างประเทศแบบหลายตลาด",
                    category="หวยหุ้นต่างประเทศ",
                    category_id=category_map.get("หวยหุ้นต่างประเทศ"),
                    badge_text="STK",
                    bg_color="#1e1b4b",
                    link_url="/lottery/thai",
                    button_text="เข้าสู่ห้อง",
                    is_active=True,
                    sort_order=6,
                ),
            ]
            db.session.add_all(rooms)

        db.session.flush()
        default_room = LotteryRoom.query.filter_by(is_active=True).order_by(LotteryRoom.sort_order.asc()).first()
        if default_room:
            ThaiLotteryPeriod.query.filter(ThaiLotteryPeriod.room_id.is_(None)).update(
                {ThaiLotteryPeriod.room_id: default_room.id}, synchronize_session=False
            )
        for room in LotteryRoom.query.all():
            if room.link_url in ("", "/lottery/thai"):
                room.link_url = f"/lottery/thai?room_id={room.id}"
            if room.api_category == "liw" or room.category == "หวย LIW":
                room.is_active = False
        for category in LotteryCategory.query.filter(
            (LotteryCategory.name == "หวย LIW") | (LotteryCategory.name.ilike("%LIW%"))
        ).all():
            category.is_active = False

        if ThaiLotteryPeriod.query.count() == 0:
            now = datetime.now()
            for room in LotteryRoom.query.order_by(LotteryRoom.sort_order.asc()).all():
                db.session.add(ThaiLotteryPeriod(
                    room_id=room.id,
                    period_date="งวดตัวอย่าง",
                    open_time=now - timedelta(days=2),
                    close_time=now - timedelta(days=1),
                    is_open=False,
                    result_3up="123",
                    result_2down="45",
                    is_checked=True,
                ))

        if LotteryPayoutRule.query.count() == 0:
            default_rules = [
                LotteryPayoutRule(bet_type="3up", payout_multiplier=900, description="3 ตัวบน", is_active=True),
                LotteryPayoutRule(bet_type="3toad", payout_multiplier=150, description="3 ตัวโต๊ด", is_active=True),
                LotteryPayoutRule(bet_type="2up", payout_multiplier=90, description="2 ตัวบน", is_active=True),
                LotteryPayoutRule(bet_type="2down", payout_multiplier=90, description="2 ตัวล่าง", is_active=True),
                LotteryPayoutRule(bet_type="runup", payout_multiplier=3, description="วิ่งบน", is_active=True),
                LotteryPayoutRule(bet_type="rundown", payout_multiplier=4, description="วิ่งล่าง", is_active=True),
            ]
            db.session.add_all(default_rules)

        if VipTier.query.count() == 0:
            db.session.add_all([
                VipTier(name="ลูกเรือฝึกหัด", rank=1, min_points=0, badge_text="CREW", benefits="เริ่มต้นเส้นทางสะสมแต้ม"),
                VipTier(name="นักเดินเรือ", rank=2, min_points=1000, badge_text="SAILOR", benefits="ปลดล็อกสิทธิ์แลกรางวัลพิเศษบางรายการ"),
                VipTier(name="กัปตัน", rank=3, min_points=5000, badge_text="CAPTAIN", benefits="สิทธิ์แลกของรางวัลระดับกัปตัน"),
                VipTier(name="จักรพรรดิแห่งสมบัติ", rank=4, min_points=15000, badge_text="EMPEROR", benefits="สิทธิ์กิจกรรมและของรางวัลระดับสูง"),
            ])

        db.session.commit()
        apply_reference_rates_once()
        grant_legacy_assistants_full_access_once()
        for user in User.query.all():
            refresh_vip_status(user)
        db.session.commit()


import backoffice_reports  # noqa: E402,F401  (ดูของรวม/รายเลข/แพ้-ชนะ 3 ฝ่าย ของ Agent/Senior)
import backoffice_settings  # noqa: E402,F401  (ลงทะเบียนหน้าตั้งค่ารายกลุ่มหวยของ Agent/Senior)


if __name__ == "__main__":
    backup_database_before_startup()
    seed_data()
    try:
        with app.app_context():
            sync_summary = sync_lottery_api_results()
        print(f"Lottery API sync: {sync_summary}")
    except Exception as exc:
        # The local lottery rooms remain available when the public API is offline.
        print(f"Lottery API sync skipped: {exc}")
    app.run(debug=True, port=5000)