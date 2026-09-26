"""หลังบ้านแอดมิน: จัดการผู้ใช้ทุกระดับ · บัญชีแอดมิน · ยกเลิกโพย · จัดการงวด · รายงานทั้งระบบ · ภาพรวมสายงาน

ทุกหน้าเป็นของแอดมินเท่านั้น (admin_required) และคำนวณจากรายการแทงกับสมุดบัญชีที่ระบบบันทึกไว้จริง
ไฟล์นี้ถูก import ท้าย app.py"""
import re
import secrets
import string
from collections import defaultdict
from datetime import datetime, timedelta

from flask import abort, flash, redirect, render_template, request, session, url_for
from sqlalchemy import func

from app import (
    ACCOUNT_TEXT_PATTERN, ADMIN_STAFF_PERMISSION_LABELS, admin_staff_permissions, BET_TYPE_LABELS, EXTRA_BET_TYPES, RATE_TABLE_ORDER, _bet_history_range, active_lottery_rooms,
    adjust_credit, admin_required, agent_upline_chain, agent_upline_senior, app, app_now, audit_admin, cancel_ticket_bets,
    current_user, db, get_lottery_rates, notify_user, record_wallet_transaction, room_category_name, senior_agent_ids,
)
from models import (
    AdminAuditLog, AdminStaff, CommissionLedger, LoginHistory, LotteryRoom, MemberGroupAccess, MemberGroupLimit, MemberGroupRate,
    MemberGroupStock, PartnerAcceptanceLimit, PartnerAssistant, PartnerPresence, PartnerProfile, PartnerStockLedger,
    PartnerStockShare, SeniorAcceptanceLimit, SeniorAssistant, SeniorCommissionLedger, SeniorProfile, SeniorStockLedger,
    SeniorStockShare, ThaiLotteryBet, ThaiLotteryPeriod, User, WalletTransaction,
)

SHIFT = timedelta(hours=7)  # เวลาไทยที่ใช้แสดงผล (ฐานข้อมูลเก็บ UTC)
PAGE_SIZE = 100
ROLE_LABELS = {"member": "สมาชิก", "partner": "Agent", "senior": "Senior", "admin": "แอดมิน"}
STATUS_LABELS = {"pending": "รอผล", "win": "ถูกรางวัล", "lose": "ไม่ถูก", "cancelled": "ยกเลิก"}


# ----------------------------------------------------------------- helpers
def valid_username(value):
    return len(value) >= 4 and re.fullmatch(ACCOUNT_TEXT_PATTERN, value) is not None


def valid_password(value):
    return len(value) >= 6 and re.fullmatch(ACCOUNT_TEXT_PATTERN, value) is not None


def username_taken(value, ignore_id=None):
    query = User.query.filter(func.lower(User.username) == value.lower())
    if ignore_id:
        query = query.filter(User.id != ignore_id)
    return query.first() is not None


def generate_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def assistant_user_ids():
    """บัญชีผู้ช่วยของ Agent/Senior (เก็บเป็น role partner/senior เหมือนเจ้าของ แต่ไม่ใช่ Agent/Senior ตัวจริง)"""
    ids = {row[0] for row in db.session.query(PartnerAssistant.assistant_user_id).all()}
    ids |= {row[0] for row in db.session.query(SeniorAssistant.assistant_user_id).all()}
    return ids


def assistant_owner_map():
    """{ผู้ช่วย: เจ้าของบัญชี}"""
    result = {}
    for row in PartnerAssistant.query.all():
        result[row.assistant_user_id] = row.partner_id
    for row in SeniorAssistant.query.all():
        result[row.assistant_user_id] = row.senior_id
    return result


def acting_as_staff():
    """ผู้ที่กำลังใช้งานเป็นทีมงาน (ไม่ใช่แอดมินเต็มสิทธิ์) ไหม"""
    return admin_staff_permissions(current_user()) is not None


def guard_admin_target(target):
    """ทีมงานห้ามแตะบัญชีแอดมิน/ทีมงานด้วยกัน (ระงับ รีเซ็ตรหัส ปรับเครดิต ฯลฯ)"""
    if target.role == "admin" and acting_as_staff():
        abort(403)


def staff_user_ids():
    return {row[0] for row in db.session.query(AdminStaff.user_id).all()}


def role_label(user, assistant_ids):
    if user.role == "admin" and user.id in staff_user_ids():
        return "ทีมงานแอดมิน"
    if user.id in assistant_ids:
        return "ผู้ช่วย Agent" if user.role == "partner" else "ผู้ช่วย Senior"
    return ROLE_LABELS.get(user.role, user.role)


def local_time(value):
    return (value + SHIFT).strftime("%d/%m/%Y %H:%M") if value else "-"


def real_agents():
    """Agent ตัวจริงที่ใช้งานได้ (ไม่รวมผู้ช่วย)"""
    assistants = assistant_user_ids()
    agents = User.query.filter_by(role="partner", is_active=True).order_by(User.username.asc()).all()
    return [a for a in agents if a.id not in assistants and a.partner_profile and a.partner_profile.status == "active"]


def real_seniors():
    assistants = assistant_user_ids()
    seniors = User.query.filter_by(role="senior", is_active=True).order_by(User.username.asc()).all()
    return [s for s in seniors if s.id not in assistants and s.senior_profile and s.senior_profile.status == "active"]


def downline_agent_ids(agent):
    """Agent ย่อยทั้งหมดที่อยู่ใต้ Agent คนนี้ (ซ้อนชั้นได้) ไม่รวมตัวเอง"""
    found, frontier = set(), [agent.id]
    while frontier:
        rows = User.query.filter(User.partner_id.in_(frontier), User.role == "partner").with_entities(User.id).all()
        frontier = [r.id for r in rows if r.id not in found and r.id != agent.id]
        found.update(frontier)
    return found


def upline_text(user, users_by_id=None):
    """ข้อความบอกว่าผู้ใช้คนนี้สังกัดใคร"""
    def get(uid):
        if users_by_id is not None and uid in users_by_id:
            return users_by_id[uid]
        return db.session.get(User, uid)

    if user.role == "member":
        agent = get(user.partner_id) if user.partner_id else None
        return f"Agent {agent.username}" if agent else "ไม่มีสังกัด"
    if user.role == "partner":
        if user.partner_id:
            parent = get(user.partner_id)
            return f"Agent {parent.username}" if parent else "-"
        senior = get(user.senior_id) if user.senior_id else None
        return f"Senior {senior.username}" if senior else "ยังไม่มี Senior"
    return "-"


def back_to(default_endpoint, **values):
    """กลับหน้าที่กดมา (เฉพาะหน้าในหลังบ้านแอดมิน) ไม่งั้นไปหน้าที่กำหนด"""
    target = request.form.get("next", "")
    if target.startswith("/admin/") and "//" not in target:
        return redirect(target)
    return redirect(url_for(default_endpoint, **values))


def profile_of(user):
    if user.role == "partner":
        return user.partner_profile
    if user.role == "senior":
        return user.senior_profile
    return None


# ----------------------------------------------------------------- ผู้ใช้ทั้งหมด
@app.route("/admin/users")
@admin_required
def admin_users():
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "")
    status = request.args.get("status", "")
    agent_id = request.args.get("agent_id", type=int)
    page = max(1, request.args.get("page", 1, type=int))
    assistants = assistant_user_ids()

    query = User.query
    if q:
        like = f"%{q}%"
        query = query.filter(User.username.ilike(like) | User.full_name.ilike(like) | User.phone.ilike(like))
    if role == "assistant":
        query = query.filter(User.id.in_(assistants or [-1]))
    elif role in ("partner", "senior"):
        query = query.filter(User.role == role)
        if assistants:
            query = query.filter(~User.id.in_(assistants))
    elif role in ("member", "admin"):
        query = query.filter(User.role == role)
    if agent_id:
        query = query.filter(User.partner_id == agent_id)
    if status == "active":
        query = query.filter(User.is_active.is_(True))
    elif status == "suspended":
        query = query.filter(User.is_active.is_(False))

    total = query.count()
    users = query.order_by(User.created_at.desc(), User.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    related_ids = {u.partner_id for u in users if u.partner_id} | {u.senior_id for u in users if u.senior_id}
    users_by_id = {u.id: u for u in User.query.filter(User.id.in_(related_ids or [-1])).all()}
    last_login = dict(
        db.session.query(LoginHistory.user_id, func.max(LoginHistory.created_at))
        .filter(LoginHistory.user_id.in_([u.id for u in users] or [-1])).group_by(LoginHistory.user_id).all()
    )
    rows = [{
        "user": u, "role_label": role_label(u, assistants), "upline": upline_text(u, users_by_id),
        "last_login": local_time(last_login.get(u.id)),
        "created": (u.created_at + SHIFT).strftime("%d/%m/%Y") if u.created_at else "-",
    } for u in users]
    counts = {
        "member": User.query.filter_by(role="member").count(),
        "partner": User.query.filter_by(role="partner").count() - PartnerAssistant.query.count(),
        "senior": User.query.filter_by(role="senior").count() - SeniorAssistant.query.count(),
        "admin": User.query.filter_by(role="admin").count(),
        "assistant": len(assistants),
    }
    return render_template(
        "admin_users.html", rows=rows, q=q, role=role, status=status, agent_id=agent_id, page=page,
        total=total, pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE), counts=counts, agents=real_agents(),
    )


@app.route("/admin/users/new", methods=["GET", "POST"])
@admin_required
def admin_user_new():
    agents = real_agents()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        agent_id = request.form.get("agent_id", type=int)
        agent = next((a for a in agents if a.id == agent_id), None) if agent_id else None
        try:
            credit = float(request.form.get("credit") or 0)
        except ValueError:
            credit = -1
        generated = not password
        if generated:
            password = generate_password()
        if not valid_username(username):
            flash("ชื่อผู้ใช้ต้องมีอย่างน้อย 4 ตัว และใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษเท่านั้น", "error")
        elif not valid_password(password):
            flash("รหัสผ่านต้องมีอย่างน้อย 6 ตัว และใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษเท่านั้น", "error")
        elif username_taken(username):
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        elif agent_id and agent is None:
            flash("ไม่พบ Agent ที่เลือก หรือ Agent ถูกพักการใช้งาน", "error")
        elif credit < 0:
            flash("เครดิตเริ่มต้นต้องเป็นตัวเลขไม่ติดลบ", "error")
        else:
            member = User(
                username=username, full_name=full_name, phone=phone, role="member",
                partner_id=agent.id if agent else None, points=0, credit_balance=0.0,
            )
            member.set_password(password)
            db.session.add(member)
            db.session.flush()
            if credit > 0:
                adjust_credit(member, credit, "เครดิตเริ่มต้นตอนสร้างบัญชีโดยแอดมิน", admin=current_user())
            audit_admin(current_user(), "create_member", "user", member.id,
                        f"agent_id={agent.id if agent else None}, credit={credit:g}")
            db.session.commit()
            note = f" รหัสผ่านที่ระบบสร้างให้: {password}" if generated else ""
            flash(f"สร้างสมาชิก {username} แล้ว{note}", "success")
            return redirect(url_for("admin_user_detail", user_id=member.id))
    return render_template("admin_user_new.html", agents=agents)


@app.route("/admin/users/<int:user_id>")
@admin_required
def admin_user_detail(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    assistants = assistant_user_ids()
    is_assistant = user.id in assistants
    owners = assistant_owner_map()
    profile = profile_of(user)
    bets = ThaiLotteryBet.query.filter_by(user_id=user.id).order_by(ThaiLotteryBet.id.desc()).limit(50).all()
    transactions = WalletTransaction.query.filter_by(user_id=user.id).order_by(WalletTransaction.id.desc()).limit(50).all()
    logins = LoginHistory.query.filter_by(user_id=user.id).order_by(LoginHistory.id.desc()).limit(20).all()
    logs = AdminAuditLog.query.filter_by(target_type="user", target_id=user.id).order_by(AdminAuditLog.id.desc()).limit(30).all()
    stats = {
        "bet_count": ThaiLotteryBet.query.filter(ThaiLotteryBet.user_id == user.id, ThaiLotteryBet.status != "cancelled").count(),
        "stake": float(db.session.query(func.coalesce(func.sum(ThaiLotteryBet.amount), 0)).filter(
            ThaiLotteryBet.user_id == user.id, ThaiLotteryBet.status != "cancelled").scalar() or 0),
        "won": float(db.session.query(func.coalesce(func.sum(ThaiLotteryBet.reward_amount), 0)).filter(
            ThaiLotteryBet.user_id == user.id, ThaiLotteryBet.status == "win").scalar() or 0),
        "members": User.query.filter_by(partner_id=user.id, role="member").count() if user.role == "partner" else 0,
        "agents": (len(senior_agent_ids(user)) if user.role == "senior" and not is_assistant else 0),
    }
    upline_chain = agent_upline_chain(user) if user.role == "partner" and not is_assistant else []
    move_agents = [a for a in real_agents() if a.id != user.id]
    if user.role == "partner" and not is_assistant:
        blocked = downline_agent_ids(user)
        parent_options = [a for a in move_agents if a.id not in blocked]
    else:
        parent_options = []
    return render_template(
        "admin_user_detail.html", target=user, profile=profile, is_assistant=is_assistant,
        role_label=role_label(user, assistants), upline=upline_text(user), upline_chain=upline_chain,
        owner=db.session.get(User, owners[user.id]) if user.id in owners else None,
        bets=bets, transactions=transactions, logins=logins, logs=logs, stats=stats,
        agents=move_agents, parent_options=parent_options, seniors=real_seniors(),
        shift=SHIFT, status_labels=STATUS_LABELS, labels=BET_TYPE_LABELS,
    )


@app.route("/admin/users/<int:user_id>/update", methods=["POST"])
@admin_required
def admin_user_update(user_id):
    user = db.session.get(User, user_id) or abort(404)
    guard_admin_target(user)
    full_name = request.form.get("full_name", "").strip()[:120]
    phone = request.form.get("phone", "").strip()[:20]
    user.full_name, user.phone = full_name, phone
    audit_admin(current_user(), "update_user", "user", user.id, f"name={full_name}, phone={phone}")
    db.session.commit()
    flash(f"บันทึกข้อมูลของ {user.username} แล้ว", "success")
    return back_to("admin_user_detail", user_id=user.id)


@app.route("/admin/users/<int:user_id>/status", methods=["POST"])
@admin_required
def admin_user_status(user_id):
    user = db.session.get(User, user_id) or abort(404)
    guard_admin_target(user)
    if user.id == current_user().id:
        flash("ไม่สามารถระงับบัญชีของตัวเองได้", "error")
        return back_to("admin_user_detail", user_id=user.id)
    target_active = not user.is_active
    full_admins = User.query.filter(User.role == "admin", User.is_active.is_(True), ~User.id.in_(staff_user_ids() or [-1])).count()
    if not target_active and user.role == "admin" and user.id not in staff_user_ids() and full_admins <= 1:
        flash("ต้องมีแอดมินที่ใช้งานได้อย่างน้อย 1 บัญชี", "error")
        return back_to("admin_user_detail", user_id=user.id)
    user.is_active = target_active
    profile = profile_of(user)
    if profile is not None:  # Agent/Senior: สถานะบัญชีกับสถานะสายงานต้องไปด้วยกัน
        profile.status = "active" if target_active else "suspended"
    audit_admin(current_user(), "activate_user" if target_active else "suspend_user", "user", user.id)
    db.session.commit()
    flash(f"{'เปิด' if target_active else 'ระงับ'}การใช้งาน {user.username} แล้ว", "success")
    return back_to("admin_user_detail", user_id=user.id)


@app.route("/admin/users/<int:user_id>/password", methods=["POST"])
@admin_required
def admin_user_password(user_id):
    user = db.session.get(User, user_id) or abort(404)
    guard_admin_target(user)
    if user.id == current_user().id:
        flash("เปลี่ยนรหัสผ่านของตัวเองที่เมนู \"บัญชีของฉัน\"", "info")
        return redirect(url_for("admin_account"))
    password = request.form.get("new_password", "")
    generated = not password
    if generated:
        password = generate_password()
    if not valid_password(password):
        flash("รหัสผ่านต้องมีอย่างน้อย 6 ตัว และใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษเท่านั้น", "error")
        return back_to("admin_user_detail", user_id=user.id)
    user.set_password(password)
    audit_admin(current_user(), "reset_password", "user", user.id)
    if user.role == "member":
        notify_user(user, "รหัสผ่านถูกเปลี่ยน", "แอดมินได้ตั้งรหัสผ่านใหม่ให้บัญชีของคุณ", "system")
    db.session.commit()
    flash(f"ตั้งรหัสผ่านใหม่ให้ {user.username} แล้ว" + (f" รหัสผ่านใหม่: {password}" if generated else ""), "success")
    return back_to("admin_user_detail", user_id=user.id)


@app.route("/admin/users/<int:user_id>/credit", methods=["POST"])
@admin_required
def admin_user_credit(user_id):
    user = db.session.get(User, user_id) or abort(404)
    guard_admin_target(user)
    try:
        amount = round(float(request.form.get("amount", 0)), 2)
    except ValueError:
        amount = 0
    action = request.form.get("action", "add")
    reason = request.form.get("reason", "").strip()[:200] or "ปรับเครดิตโดยแอดมิน"
    if amount <= 0:
        flash("กรุณาระบุจำนวนเครดิตมากกว่า 0", "error")
    elif action == "sub" and amount > user.credit_balance + 1e-9:
        flash(f"หักเกินยอดคงเหลือ ({user.credit_balance:,.2f})", "error")
    else:
        change = amount if action == "add" else -amount
        adjust_credit(user, change, reason, admin=current_user())
        audit_admin(current_user(), "adjust_credit", "user", user.id, f"{change:+,.2f} เครดิต: {reason}")
        db.session.commit()
        flash(f"ปรับเครดิต {user.username} แล้ว ({change:+,.2f}) คงเหลือ {user.credit_balance:,.2f}", "success")
    return back_to("admin_user_detail", user_id=user.id)


@app.route("/admin/users/<int:user_id>/move", methods=["POST"])
@admin_required
def admin_user_move(user_id):
    """ย้ายสังกัด: สมาชิก → Agent อื่น · Agent → Senior อื่น หรือเป็น Agent ย่อยใต้ Agent อื่น"""
    user = db.session.get(User, user_id) or abort(404)
    guard_admin_target(user)
    assistants = assistant_user_ids()
    if user.id in assistants or user.role not in ("member", "partner"):
        flash("ย้ายสังกัดได้เฉพาะสมาชิกและ Agent", "error")
        return back_to("admin_user_detail", user_id=user.id)
    if user.role == "member":
        agent_id = request.form.get("agent_id", type=int)
        agent = next((a for a in real_agents() if a.id == agent_id), None) if agent_id else None
        if agent_id and agent is None:
            flash("ไม่พบ Agent ที่เลือก หรือ Agent ถูกพักการใช้งาน", "error")
            return back_to("admin_user_detail", user_id=user.id)
        old = user.partner_id
        user.partner_id = agent.id if agent else None
        audit_admin(current_user(), "move_member", "user", user.id, f"{old} -> {user.partner_id}")
        db.session.commit()
        flash(f"ย้าย {user.username} ไปอยู่ใต้ {'Agent ' + agent.username if agent else 'ไม่มีสังกัด'} แล้ว "
              "(ค่าเฉพาะสมาชิกที่ Agent เดิมเคยตั้งไว้จะไม่ถูกใช้กับสายใหม่)", "success")
        return back_to("admin_user_detail", user_id=user.id)

    parent_id = request.form.get("parent_agent_id", type=int)
    senior_id = request.form.get("senior_id", type=int)
    if parent_id:
        parent = next((a for a in real_agents() if a.id == parent_id), None)
        if parent is None or parent.id == user.id or parent.id in downline_agent_ids(user):
            flash("เลือก Agent ต้นสายไม่ได้ (ไม่พบ พักการใช้งาน หรืออยู่ใต้ Agent นี้เอง)", "error")
            return back_to("admin_user_detail", user_id=user.id)
        user.partner_id, user.senior_id = parent.id, None  # Agent ย่อยสืบทอด Senior จากต้นสาย
        target_text = f"Agent {parent.username}"
    else:
        senior = next((s for s in real_seniors() if s.id == senior_id), None)
        if senior is None:
            flash("กรุณาเลือก Senior ที่ใช้งานอยู่", "error")
            return back_to("admin_user_detail", user_id=user.id)
        user.partner_id, user.senior_id = None, senior.id
        target_text = f"Senior {senior.username}"
    audit_admin(current_user(), "move_agent", "user", user.id, f"-> {target_text}")
    db.session.commit()
    flash(f"ย้าย Agent {user.username} ไปอยู่ใต้ {target_text} แล้ว", "success")
    return back_to("admin_user_detail", user_id=user.id)


# ----------------------------------------------------------------- บัญชีแอดมิน
@app.route("/admin/admins", methods=["GET", "POST"])
@admin_required
def admin_admins():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        if not valid_username(username):
            flash("ชื่อผู้ใช้ต้องมีอย่างน้อย 4 ตัว และใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษเท่านั้น", "error")
        elif not valid_password(password):
            flash("รหัสผ่านต้องมีอย่างน้อย 6 ตัว และใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษเท่านั้น", "error")
        elif username_taken(username):
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        else:
            admin_user = User(username=username, full_name=full_name or username, role="admin", points=0, credit_balance=0.0)
            admin_user.set_password(password)
            db.session.add(admin_user)
            db.session.flush()
            audit_admin(current_user(), "create_admin", "user", admin_user.id, username)
            db.session.commit()
            flash(f"สร้างบัญชีแอดมิน {username} แล้ว", "success")
        return redirect(url_for("admin_admins"))
    staff_ids = staff_user_ids()
    admins = [a for a in User.query.filter_by(role="admin").order_by(User.created_at.asc(), User.id.asc()).all() if a.id not in staff_ids]
    last_login = dict(
        db.session.query(LoginHistory.user_id, func.max(LoginHistory.created_at))
        .filter(LoginHistory.user_id.in_([a.id for a in admins] or [-1])).group_by(LoginHistory.user_id).all()
    )
    return render_template(
        "admin_admins.html", admins=admins, last_login={k: local_time(v) for k, v in last_login.items()},
        active_count=sum(1 for a in admins if a.is_active), staff_count=len(staff_ids),
    )


@app.route("/admin/account", methods=["GET", "POST"])
@admin_required
def admin_account():
    user = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            user.full_name = request.form.get("full_name", "").strip()[:120]
            audit_admin(user, "update_own_profile", "user", user.id)
            db.session.commit()
            flash("บันทึกชื่อแล้ว", "success")
        else:
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if not user.check_password(current_password):
                flash("รหัสผ่านเดิมไม่ถูกต้อง", "error")
            elif not valid_password(new_password):
                flash("รหัสผ่านใหม่ต้องมีอย่างน้อย 6 ตัว และใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษเท่านั้น", "error")
            elif new_password != confirm_password:
                flash("รหัสผ่านใหม่และการยืนยันไม่ตรงกัน", "error")
            elif new_password == current_password:
                flash("รหัสผ่านใหม่ต้องแตกต่างจากรหัสผ่านเดิม", "error")
            else:
                user.set_password(new_password)
                audit_admin(user, "change_own_password", "user", user.id)
                db.session.commit()
                flash("เปลี่ยนรหัสผ่านเรียบร้อยแล้ว", "success")
        return redirect(url_for("admin_account"))
    logins = LoginHistory.query.filter_by(user_id=user.id).order_by(LoginHistory.id.desc()).limit(10).all()
    return render_template("admin_account.html", me=user, logins=logins, shift=SHIFT)


# ----------------------------------------------------------------- ยกเลิกโพยโดยแอดมิน
@app.route("/admin/lottery-tickets/<int:period_id>/<int:user_id>/cancel", methods=["POST"])
@admin_required
def admin_cancel_ticket(period_id, user_id):
    """ยกเลิกโพยทั้งใบที่ยังรอผล (แม้ปิดรับแล้ว ตราบใดที่งวดยังไม่ตรวจผล) — คืนเครดิตที่หักจริง + คืนค่าคอม"""
    code = request.form.get("ticket_code", "").strip()
    period = db.session.get(ThaiLotteryPeriod, period_id) or abort(404)
    member = db.session.get(User, user_id) or abort(404)
    query = ThaiLotteryBet.query.filter_by(user_id=member.id, period_id=period.id, status="pending")
    query = query.filter(ThaiLotteryBet.ticket_code == code) if code else query.filter(ThaiLotteryBet.ticket_code.is_(None))
    bets = query.all()
    destination = request.form.get("next", "")
    fallback = url_for("admin_lottery_ticket", period_id=period.id, user_id=member.id, ticket_code=code or None)
    if period.is_checked or not bets:
        flash("ยกเลิกไม่ได้: โพยนี้ตรวจผลไปแล้วหรือถูกยกเลิกไปแล้ว", "error")
        return redirect(destination if destination.startswith("/admin/") else fallback)
    admin_user = current_user()
    refund = cancel_ticket_bets(bets, f"ยกเลิกโดยแอดมิน {admin_user.username}")
    adjust_credit(member, refund, f"ยกเลิกโพย {code or '-'} โดยแอดมิน", admin=admin_user)
    notify_user(member, "โพยถูกยกเลิก", f"โพย {code or '-'} ถูกยกเลิกโดยผู้ดูแลระบบ คืนเครดิต {refund:,.2f}", "wallet")
    audit_admin(admin_user, "cancel_ticket", "user", member.id, f"period={period.id} ticket={code or '-'} refund={refund:,.2f}")
    db.session.commit()
    flash(f"ยกเลิกโพย {code or '-'} ของ {member.username} แล้ว คืนเครดิต {refund:,.2f}", "success")
    return redirect(destination if destination.startswith("/admin/") else fallback)


# ----------------------------------------------------------------- จัดการงวด
def parse_local(value):
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M")
    except (ValueError, AttributeError):
        return None


@app.route("/admin/periods")
@admin_required
def admin_periods():
    room_id = request.args.get("room_id", type=int)
    state = request.args.get("state", "")
    now = app_now()
    query = ThaiLotteryPeriod.query
    if room_id:
        query = query.filter(ThaiLotteryPeriod.room_id == room_id)
    if state == "open":
        query = query.filter(ThaiLotteryPeriod.is_open.is_(True), ThaiLotteryPeriod.close_time > now)
    elif state == "waiting":
        query = query.filter(ThaiLotteryPeriod.is_checked.is_(False), (ThaiLotteryPeriod.is_open.is_(False)) | (ThaiLotteryPeriod.close_time <= now))
    elif state == "done":
        query = query.filter(ThaiLotteryPeriod.is_checked.is_(True))
    periods = query.order_by(ThaiLotteryPeriod.id.desc()).limit(200).all()
    counts = dict(
        db.session.query(ThaiLotteryBet.period_id, func.count(ThaiLotteryBet.id))
        .filter(ThaiLotteryBet.period_id.in_([p.id for p in periods] or [-1]), ThaiLotteryBet.status != "cancelled")
        .group_by(ThaiLotteryBet.period_id).all()
    )
    stakes = dict(
        db.session.query(ThaiLotteryBet.period_id, func.coalesce(func.sum(ThaiLotteryBet.amount), 0))
        .filter(ThaiLotteryBet.period_id.in_([p.id for p in periods] or [-1]), ThaiLotteryBet.status != "cancelled")
        .group_by(ThaiLotteryBet.period_id).all()
    )
    rows = []
    for p in periods:
        if p.is_checked:
            label = "ตรวจผลแล้ว"
        elif p.is_open and p.close_time and p.close_time > now:
            label = "เปิดรับ"
        else:
            label = "ปิดรับ · รอผล"
        rows.append({"p": p, "label": label, "count": counts.get(p.id, 0), "stake": float(stakes.get(p.id, 0))})
    return render_template(
        "admin_periods.html", rows=rows, rooms=active_lottery_rooms().all(), room_id=room_id, state=state, now=now,
    )


@app.route("/admin/periods/<int:period_id>/<action>", methods=["POST"])
@admin_required
def admin_period_action(period_id, action):
    period = db.session.get(ThaiLotteryPeriod, period_id) or abort(404)
    now = app_now()
    label = f"{period.room.name if period.room else '-'} {period.period_date}"
    if action == "close":
        if period.is_checked:
            flash("งวดนี้ตรวจผลแล้ว", "error")
        else:
            period.is_open = False
            period.close_time = min(period.close_time, now) if period.close_time else now
            audit_admin(current_user(), "close_period", "lottery_period", period.id)
            db.session.commit()
            flash(f"ปิดรับงวด {label} แล้ว", "success")
    elif action == "reopen":
        new_close = parse_local(request.form.get("close_time", ""))
        if period.is_checked:
            flash("งวดที่ตรวจผลแล้วเปิดรับใหม่ไม่ได้ (ต้องย้อนผลก่อน)", "error")
        elif new_close is None or new_close <= now:
            flash("กรุณาระบุเวลาปิดรับใหม่ที่อยู่หลังเวลาปัจจุบัน", "error")
        else:
            period.is_open, period.close_time = True, new_close
            audit_admin(current_user(), "reopen_period", "lottery_period", period.id, f"close={new_close}")
            db.session.commit()
            flash(f"เปิดรับงวด {label} ถึง {new_close:%d/%m/%Y %H:%M} แล้ว", "success")
    elif action == "edit":
        open_time, close_time = parse_local(request.form.get("open_time", "")), parse_local(request.form.get("close_time", ""))
        date_value = request.form.get("period_date", "").strip()[:20]
        if period.is_checked:
            flash("งวดที่ตรวจผลแล้วแก้ไขไม่ได้", "error")
        elif not date_value or open_time is None or close_time is None or close_time <= open_time:
            flash("กรุณากรอกวันที่ และเวลาปิดรับต้องอยู่หลังเวลาเปิดรับ", "error")
        else:
            period.period_date, period.open_time, period.close_time = date_value, open_time, close_time
            audit_admin(current_user(), "edit_period", "lottery_period", period.id, f"{date_value} {open_time} - {close_time}")
            db.session.commit()
            flash(f"แก้ไขงวด {label} แล้ว", "success")
    elif action == "delete":
        if ThaiLotteryBet.query.filter_by(period_id=period.id).first() is not None:
            flash("ลบไม่ได้: งวดนี้มีรายการแทงแล้ว (ปิดรับแทนได้)", "error")
        else:
            audit_admin(current_user(), "delete_period", "lottery_period", period.id, label)
            db.session.delete(period)
            db.session.commit()
            flash(f"ลบงวด {label} แล้ว", "success")
    else:
        abort(404)
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/admin/") else url_for("admin_periods"))


# ----------------------------------------------------------------- ออนไลน์ / ประวัติเข้าสู่ระบบ
@app.route("/admin/logins")
@admin_required
def admin_logins():
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "")
    page = max(1, request.args.get("page", 1, type=int))
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    online = PartnerPresence.query.filter(PartnerPresence.last_seen_at >= cutoff).order_by(PartnerPresence.last_seen_at.desc()).limit(200).all()
    query = LoginHistory.query.join(User, LoginHistory.user_id == User.id)
    if q:
        query = query.filter(User.username.ilike(f"%{q}%") | LoginHistory.ip_address.ilike(f"%{q}%"))
    if role in ROLE_LABELS:
        query = query.filter(User.role == role)
    total = query.count()
    entries = query.order_by(LoginHistory.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return render_template(
        "admin_logins.html", online=online, entries=entries, q=q, role=role, page=page, total=total,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE), shift=SHIFT, role_labels=ROLE_LABELS,
    )


# ----------------------------------------------------------------- รายงานทั้งระบบ
def range_bounds(args):
    range_key, start_day, end_day = _bet_history_range(args)
    start_utc = datetime.combine(start_day, datetime.min.time()) - SHIFT
    end_utc = datetime.combine(end_day + timedelta(days=1), datetime.min.time()) - SHIFT
    return range_key, start_day, end_day, start_utc, end_utc


def ledger_totals(bet_ids):
    """{bet_id: (ค่าคอมรวมทุกสาย, กำไร/ขาดทุนหุ้นรวมทุกสาย)} — Agent + Senior รวมกัน"""
    commission, stock = defaultdict(float), defaultdict(float)
    for start in range(0, len(bet_ids), 500):
        chunk = bet_ids[start:start + 500]
        for model in (CommissionLedger, SeniorCommissionLedger):
            for bet_id, amount in db.session.query(model.bet_id, func.sum(model.commission_amount)).filter(
                model.bet_id.in_(chunk)
            ).group_by(model.bet_id):
                commission[bet_id] += float(amount or 0)
        for model in (PartnerStockLedger, SeniorStockLedger):
            for bet_id, amount in db.session.query(model.bet_id, func.sum(model.pnl_amount)).filter(
                model.bet_id.in_(chunk)
            ).group_by(model.bet_id):
                stock[bet_id] += float(amount or 0)
    return commission, stock


PNL_GROUPS = (
    ("date", "แยกตามวัน"), ("room", "แยกตามตลาด"), ("type", "แยกตามประเภท"),
    ("senior", "แยกตาม Senior"), ("agent", "แยกตาม Agent"), ("member", "แยกตามสมาชิก"),
)


@app.route("/admin/reports")
@admin_required
def admin_reports():
    """ภาพรวมทั้งระบบ: ยอดแทง/จ่าย/ค่าคอม/หุ้น/กำไรบริษัท ของช่วงวันที่ที่เลือก + ยอดคงค้างตอนนี้"""
    range_key, start_day, end_day, start_utc, end_utc = range_bounds(request.args)
    base = ThaiLotteryBet.query.filter(
        ThaiLotteryBet.created_at >= start_utc, ThaiLotteryBet.created_at < end_utc, ThaiLotteryBet.status != "cancelled"
    )
    bets = base.all()
    settled = [b for b in bets if b.status in ("win", "lose")]
    commission, stock = ledger_totals([b.id for b in settled])
    totals = defaultdict(float)
    for bet in bets:
        totals["count"] += 1
        totals["stake"] += float(bet.amount)
        totals["discount"] += float(bet.discount_amount or 0)
        if bet.status == "pending":
            totals["pending_stake"] += float(bet.amount)
    for bet in settled:
        win = float(bet.reward_amount) if bet.status == "win" else 0.0
        member_net = win - float(bet.amount) + float(bet.discount_amount or 0)
        totals["win"] += win
        totals["member_net"] += member_net
        totals["commission"] += commission.get(bet.id, 0.0)
        totals["stock"] += stock.get(bet.id, 0.0)
        totals["company"] += -(member_net + commission.get(bet.id, 0.0) + stock.get(bet.id, 0.0))
    room_rows = db.session.query(
        LotteryRoom.name, func.count(ThaiLotteryBet.id), func.coalesce(func.sum(ThaiLotteryBet.amount), 0)
    ).join(ThaiLotteryPeriod, ThaiLotteryPeriod.room_id == LotteryRoom.id).join(
        ThaiLotteryBet, ThaiLotteryBet.period_id == ThaiLotteryPeriod.id
    ).filter(
        ThaiLotteryBet.created_at >= start_utc, ThaiLotteryBet.created_at < end_utc, ThaiLotteryBet.status != "cancelled"
    ).group_by(LotteryRoom.id).order_by(func.sum(ThaiLotteryBet.amount).desc()).all()
    system = {
        "credit": float(db.session.query(func.coalesce(func.sum(User.credit_balance), 0)).scalar() or 0),
        "commission_owed": float(db.session.query(func.coalesce(func.sum(PartnerProfile.commission_balance), 0)).scalar() or 0)
        + float(db.session.query(func.coalesce(func.sum(SeniorProfile.commission_balance), 0)).scalar() or 0),
        "stock": float(db.session.query(func.coalesce(func.sum(PartnerProfile.stock_balance), 0)).scalar() or 0)
        + float(db.session.query(func.coalesce(func.sum(SeniorProfile.stock_balance), 0)).scalar() or 0),
        "pending_all": float(db.session.query(func.coalesce(func.sum(ThaiLotteryBet.amount), 0)).filter(ThaiLotteryBet.status == "pending").scalar() or 0),
    }
    return render_template(
        "admin_reports.html", totals=dict(totals), room_rows=room_rows, system=system, range_key=range_key,
        start_day=start_day, end_day=end_day,
    )


@app.route("/admin/reports/pnl")
@admin_required
def admin_report_pnl():
    """แพ้-ชนะทั้งระบบ แบ่ง 3 ฝ่าย: สมาชิก · สายงาน (ค่าคอม + หุ้นของ Agent/Senior ทุกคน) · บริษัท"""
    range_key, start_day, end_day, start_utc, end_utc = range_bounds(request.args)
    group_by = request.args.get("by", "date")
    if group_by not in dict(PNL_GROUPS):
        group_by = "date"
    room_id = request.args.get("room_id", type=int)
    query = ThaiLotteryBet.query.filter(
        ThaiLotteryBet.created_at >= start_utc, ThaiLotteryBet.created_at < end_utc,
        ThaiLotteryBet.status.in_(["win", "lose"]),
    )
    if room_id:
        query = query.join(ThaiLotteryPeriod, ThaiLotteryBet.period_id == ThaiLotteryPeriod.id).filter(ThaiLotteryPeriod.room_id == room_id)
    bets = query.all()
    commission, stock = ledger_totals([b.id for b in bets])
    room_names = {r.id: r.name for r in LotteryRoom.query.all()}
    users = {u.id: u for u in User.query.filter(User.id.in_({b.user_id for b in bets} or [-1])).all()}
    agent_ids = {u.partner_id for u in users.values() if u.partner_id}
    agents = {u.id: u for u in User.query.filter(User.id.in_(agent_ids or [-1])).all()}
    senior_cache = {}

    def senior_name(agent):
        if agent is None:
            return "ไม่มีสังกัด"
        if agent.id not in senior_cache:
            senior = agent_upline_senior(agent)
            senior_cache[agent.id] = senior.username if senior else "ไม่มี Senior"
        return senior_cache[agent.id]

    def key_of(bet):
        member = users.get(bet.user_id)
        if group_by == "member":
            return member.username if member else "-"
        if group_by == "type":
            return BET_TYPE_LABELS.get(bet.bet_type, bet.bet_type)
        if group_by == "room":
            return room_names.get(bet.period.room_id, "-") if bet.period else "-"
        if group_by == "agent":
            agent = agents.get(member.partner_id) if member and member.partner_id else None
            return agent.username if agent else "ไม่มีสังกัด"
        if group_by == "senior":
            return senior_name(agents.get(member.partner_id) if member and member.partner_id else None)
        return (bet.created_at + SHIFT).strftime("%Y-%m-%d")

    rows = defaultdict(lambda: defaultdict(float))
    for bet in bets:
        r = rows[key_of(bet)]
        stake, discount = float(bet.amount), float(bet.discount_amount or 0)
        win = float(bet.reward_amount) if bet.status == "win" else 0.0
        member_net = win - stake + discount
        line_comm, line_stock = commission.get(bet.id, 0.0), stock.get(bet.id, 0.0)
        r["count"] += 1
        r["stake"] += stake
        r["discount"] += discount
        r["win"] += win
        r["member_net"] += member_net
        r["comm"] += line_comm
        r["stock"] += line_stock
        r["line_net"] += line_comm + line_stock
        r["company"] += -(member_net + line_comm + line_stock)
    table = [{"name": name, **dict(values)} for name, values in rows.items()]
    table.sort(key=lambda r: r["name"], reverse=True) if group_by == "date" else table.sort(key=lambda r: -r["stake"])
    totals = defaultdict(float)
    for row in table:
        for key, value in row.items():
            if key != "name":
                totals[key] += value
    return render_template(
        "admin_report_pnl.html", rows=table, totals=dict(totals), group_by=group_by, groups=PNL_GROUPS, range_key=range_key,
        start_day=start_day, end_day=end_day, rooms=active_lottery_rooms().all(), room_id=room_id,
    )


def admin_market_and_period():
    rooms = active_lottery_rooms().all()
    room_id = request.args.get("room_id", type=int)
    room = next((r for r in rooms if r.id == room_id), None)
    if room is None:
        now = app_now()
        open_ids = {p.room_id for p in ThaiLotteryPeriod.query.filter(ThaiLotteryPeriod.is_open.is_(True), ThaiLotteryPeriod.close_time > now)}
        room = next((r for r in rooms if r.id in open_ids), rooms[0] if rooms else None)
    periods = ThaiLotteryPeriod.query.filter_by(room_id=room.id).order_by(ThaiLotteryPeriod.id.desc()).limit(20).all() if room else []
    period_id = request.args.get("period_id", type=int)
    period = next((p for p in periods if p.id == period_id), periods[0] if periods else None)
    return rooms, room, periods, period


@app.route("/admin/reports/overall")
@admin_required
def admin_report_overall():
    """ดูของรวมทั้งระบบต่อตลาด/งวด: ยอดซื้อ ส่วนลด และยอดจ่ายสูงสุดถ้าเลขนั้นถูก — ใช้ดูเลขเสี่ยง"""
    rooms, room, periods, period = admin_market_and_period()
    types = list(RATE_TABLE_ORDER) + [t for t in EXTRA_BET_TYPES if get_lottery_rates().get(t)]
    bets = ThaiLotteryBet.query.filter(ThaiLotteryBet.period_id == period.id, ThaiLotteryBet.status != "cancelled").all() if period else []
    by_type = defaultdict(lambda: defaultdict(float))
    numbers = defaultdict(lambda: {"stake": 0.0, "max": 0.0, "members": set()})
    for bet in bets:
        stake = float(bet.amount)
        t = by_type[bet.bet_type]
        t["count"] += 1
        t["stake"] += stake
        t["discount"] += float(bet.discount_amount or 0)
        t["max"] += stake * float(bet.rate)
        entry = numbers[(bet.bet_type, bet.number)]
        entry["stake"] += stake
        entry["max"] += stake * float(bet.rate)
        entry["members"].add(bet.user_id)
    view_type = request.args.get("type", "")
    sort = request.args.get("sort", "max")
    rows = [
        {"type": t, "number": n, "stake": v["stake"], "max": v["max"], "members": len(v["members"])}
        for (t, n), v in numbers.items() if not view_type or t == view_type
    ]
    rows.sort(key=lambda r: (-r["stake"], r["number"]) if sort == "stake" else ((r["type"], r["number"]) if sort == "number" else (-r["max"], r["number"])))
    total = {k: sum(v[k] for v in by_type.values()) for k in ("count", "stake", "discount", "max")}
    return render_template(
        "admin_report_overall.html", rooms=rooms, room=room, periods=periods, period=period, types=types, labels=BET_TYPE_LABELS,
        by_type=by_type, total=total, rows=rows[:300], view_type=view_type, sort=sort, bet_count=len(bets),
    )


# ----------------------------------------------------------------- ภาพรวมสายงาน
@app.route("/admin/lines")
@admin_required
def admin_lines():
    """ทุก Senior/Agent ในตารางเดียว: สังกัด สมาชิก ยอดคงเหลือ ยอดแทง 30 วัน"""
    assistants = assistant_user_ids()
    since = datetime.utcnow() - timedelta(days=30)
    seniors = [s for s in User.query.filter_by(role="senior").order_by(User.username.asc()).all() if s.id not in assistants]
    agents = [a for a in User.query.filter_by(role="partner").order_by(User.username.asc()).all() if a.id not in assistants]
    member_counts = dict(
        db.session.query(User.partner_id, func.count(User.id)).filter(User.role == "member", User.partner_id.isnot(None)).group_by(User.partner_id).all()
    )
    turnover = dict(
        db.session.query(User.partner_id, func.coalesce(func.sum(ThaiLotteryBet.amount), 0))
        .join(User, ThaiLotteryBet.user_id == User.id)
        .filter(ThaiLotteryBet.created_at >= since, ThaiLotteryBet.status != "cancelled", User.partner_id.isnot(None))
        .group_by(User.partner_id).all()
    )
    online_cutoff = datetime.utcnow() - timedelta(minutes=10)
    online = dict(
        db.session.query(PartnerPresence.partner_id, func.count(PartnerPresence.id))
        .filter(PartnerPresence.last_seen_at >= online_cutoff).group_by(PartnerPresence.partner_id).all()
    )
    assistant_counts = defaultdict(int)
    for owner_id in assistant_owner_map().values():
        assistant_counts[owner_id] += 1
    agents_by_id = {a.id: a for a in agents}
    senior_of = {}
    for agent in agents:
        top = agent
        seen = {agent.id}
        while top.partner_id and top.partner_id in agents_by_id and top.partner_id not in seen:
            top = agents_by_id[top.partner_id]
            seen.add(top.id)
        senior_of[agent.id] = top.senior_id

    def agent_row(agent):
        return {
            "user": agent, "profile": agent.partner_profile, "members": member_counts.get(agent.id, 0),
            "turnover": float(turnover.get(agent.id, 0)), "online": online.get(agent.id, 0),
            "assistants": assistant_counts.get(agent.id, 0), "upline": upline_text(agent, agents_by_id),
        }

    groups = []
    for senior in seniors:
        members_of = [agent_row(a) for a in agents if senior_of.get(a.id) == senior.id]
        groups.append({
            "senior": senior, "profile": senior.senior_profile, "assistants": assistant_counts.get(senior.id, 0),
            "agents": members_of, "members": sum(r["members"] for r in members_of),
            "turnover": sum(r["turnover"] for r in members_of),
        })
    orphans = [agent_row(a) for a in agents if senior_of.get(a.id) is None]
    return render_template("admin_lines.html", groups=groups, orphans=orphans)


@app.route("/admin/lines/<int:user_id>")
@admin_required
def admin_line_detail(user_id):
    """รายละเอียดสายของ Agent/Senior: ยอดคงเหลือ, % ถือหุ้นรายห้อง, ตั้งสู้, ผู้ช่วย, ค่าที่ตั้งให้สมาชิก, สรุปสมุดค่าคอม/หุ้น"""
    owner = db.session.get(User, user_id) or abort(404)
    if owner.role not in ("partner", "senior") or owner.id in assistant_user_ids():
        abort(404)
    is_senior = owner.role == "senior"
    profile = profile_of(owner)
    share_model = SeniorStockShare if is_senior else PartnerStockShare
    share_field = "senior_id" if is_senior else "partner_id"
    limit_model = SeniorAcceptanceLimit if is_senior else PartnerAcceptanceLimit
    shares = {s.room_id: s.hold_percent for s in share_model.query.filter(getattr(share_model, share_field) == owner.id)}
    caps = defaultdict(list)
    for row in limit_model.query.filter(getattr(limit_model, share_field) == owner.id):
        caps[row.room_id].append((BET_TYPE_LABELS.get(row.bet_type, row.bet_type), row.amount_limit))
    room_rows = [{
        "room": r, "group": room_category_name(r) or "อื่นๆ", "percent": shares.get(r.id, 0.0), "caps": caps.get(r.id, []),
    } for r in active_lottery_rooms().all() if shares.get(r.id) or caps.get(r.id)]
    assistants = (SeniorAssistant.query.filter_by(senior_id=owner.id) if is_senior else PartnerAssistant.query.filter_by(partner_id=owner.id)).all()
    group_rates = MemberGroupRate.query.filter_by(owner_id=owner.id).order_by(MemberGroupRate.member_id, MemberGroupRate.category, MemberGroupRate.tier).limit(300).all()
    member_ids = {r.member_id for r in group_rates}
    named = {u.id: u.username for u in User.query.filter(User.id.in_(member_ids or [-1])).all()}
    setting_counts = {
        "rates": MemberGroupRate.query.filter_by(owner_id=owner.id).count(),
        "limits": MemberGroupLimit.query.filter_by(owner_id=owner.id).count(),
        "access": MemberGroupAccess.query.filter_by(owner_id=owner.id).count(),
        "stock": MemberGroupStock.query.filter_by(owner_id=owner.id).count(),
    }
    commission_model, commission_owner = (SeniorCommissionLedger, SeniorCommissionLedger.senior_id) if is_senior else (CommissionLedger, CommissionLedger.partner_id)
    stock_model, stock_owner = (SeniorStockLedger, SeniorStockLedger.senior_id) if is_senior else (PartnerStockLedger, PartnerStockLedger.partner_id)
    since = datetime.utcnow() - timedelta(days=30)
    ledger = {
        "commission_all": float(db.session.query(func.coalesce(func.sum(commission_model.commission_amount), 0)).filter(commission_owner == owner.id).scalar() or 0),
        "commission_30": float(db.session.query(func.coalesce(func.sum(commission_model.commission_amount), 0)).filter(commission_owner == owner.id, commission_model.created_at >= since).scalar() or 0),
        "stock_all": float(db.session.query(func.coalesce(func.sum(stock_model.pnl_amount), 0)).filter(stock_owner == owner.id).scalar() or 0),
        "stock_30": float(db.session.query(func.coalesce(func.sum(stock_model.pnl_amount), 0)).filter(stock_owner == owner.id, stock_model.created_at >= since).scalar() or 0),
    }
    if is_senior:
        agent_ids = senior_agent_ids(owner)
        downline = User.query.filter(User.id.in_(agent_ids or [-1])).order_by(User.username.asc()).all()
        members = User.query.filter(User.partner_id.in_(agent_ids or [-1]), User.role == "member").order_by(User.username.asc()).limit(300).all()
    else:
        downline = User.query.filter_by(partner_id=owner.id, role="partner").order_by(User.username.asc()).all()
        members = User.query.filter_by(partner_id=owner.id, role="member").order_by(User.username.asc()).limit(300).all()
    return render_template(
        "admin_line_detail.html", owner=owner, profile=profile, is_senior=is_senior, room_rows=room_rows,
        assistants=assistants, group_rates=group_rates, named=named, setting_counts=setting_counts, ledger=ledger,
        downline=downline, members=members, labels=BET_TYPE_LABELS, upline=upline_text(owner),
    )


@app.route("/admin/lines/<int:user_id>/settle", methods=["POST"])
@admin_required
def admin_line_settle(user_id):
    """จ่ายค่าคอมมิชชันที่ค้าง หรือเคลียร์ยอดหุ้นสะสมของ Agent/Senior — บันทึกลงกระเป๋าและ audit ทุกครั้ง"""
    owner = db.session.get(User, user_id) or abort(404)
    profile = profile_of(owner)
    kind = request.form.get("kind", "commission")
    if profile is None or kind not in ("commission", "stock"):
        abort(404)
    is_senior = owner.role == "senior"
    balance = profile.commission_balance if kind == "commission" else profile.stock_balance
    if abs(balance) < 0.005:
        flash("ไม่มียอดค้างให้ดำเนินการ", "warning")
        return redirect(url_for("admin_line_detail", user_id=owner.id))
    label = "ค่าคอมมิชชัน" if kind == "commission" else "ยอดหุ้นสะสม"
    reason = f"แอดมินบันทึกปิดยอด{label} ({balance:,.2f})"
    if kind == "commission":
        profile.commission_balance = 0.0
        ledger_model = SeniorCommissionLedger if is_senior else CommissionLedger
        owner_col = ledger_model.senior_id if is_senior else ledger_model.partner_id
        ledger_model.query.filter(owner_col == owner.id, ledger_model.status == "approved").update(
            {ledger_model.status: "paid"}, synchronize_session=False
        )
    else:
        profile.stock_balance = 0.0
    record_wallet_transaction(owner, kind, -balance, 0.0, reason, reference_type="settle", admin=current_user())
    audit_admin(current_user(), f"settle_{kind}", "user", owner.id, f"{balance:,.2f}")
    db.session.commit()
    flash(f"ปิดยอด{label}ของ {owner.username} แล้ว ({balance:,.2f})", "success")
    return redirect(url_for("admin_line_detail", user_id=owner.id))


# ----------------------------------------------------------------- ทีมงานแอดมิน
def picked_permissions():
    return ",".join(key for key in ADMIN_STAFF_PERMISSION_LABELS if key in request.form.getlist("perm"))


@app.route("/admin/staff", methods=["GET", "POST"])
@admin_required
def admin_staff():
    """เจ้าของ (แอดมินเต็มสิทธิ์) เพิ่มทีมงานและเลือกสิทธิ์รายหมวด — ทีมงานเข้าหน้านี้ไม่ได้"""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        permissions = picked_permissions()
        if not valid_username(username):
            flash("ชื่อผู้ใช้ต้องมีอย่างน้อย 4 ตัว และใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษเท่านั้น", "error")
        elif not valid_password(password):
            flash("รหัสผ่านต้องมีอย่างน้อย 6 ตัว และใช้ภาษาอังกฤษ ตัวเลข หรืออักขระพิเศษเท่านั้น", "error")
        elif username_taken(username):
            flash("ชื่อผู้ใช้นี้ถูกใช้แล้ว", "error")
        elif not permissions:
            flash("กรุณาเลือกสิทธิ์อย่างน้อย 1 หมวด", "error")
        else:
            user = User(username=username, full_name=full_name or username, role="admin", points=0, credit_balance=0.0)
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            db.session.add(AdminStaff(user_id=user.id, permissions=permissions, created_by=current_user().id))
            audit_admin(current_user(), "create_staff", "user", user.id, permissions)
            db.session.commit()
            flash(f"เพิ่มทีมงาน {username} แล้ว", "success")
        return redirect(url_for("admin_staff"))
    rows = AdminStaff.query.order_by(AdminStaff.id.desc()).all()
    last_login = dict(
        db.session.query(LoginHistory.user_id, func.max(LoginHistory.created_at))
        .filter(LoginHistory.user_id.in_([r.user_id for r in rows] or [-1])).group_by(LoginHistory.user_id).all()
    )
    return render_template(
        "admin_staff.html", rows=rows, labels=ADMIN_STAFF_PERMISSION_LABELS,
        last_login={k: local_time(v) for k, v in last_login.items()},
    )


@app.route("/admin/staff/<int:staff_id>/permissions", methods=["POST"])
@admin_required
def admin_staff_permissions_update(staff_id):
    row = db.session.get(AdminStaff, staff_id) or abort(404)
    permissions = picked_permissions()
    if not permissions:
        flash("กรุณาเลือกสิทธิ์อย่างน้อย 1 หมวด (หรือระงับบัญชีแทน)", "error")
    else:
        row.permissions = permissions
        audit_admin(current_user(), "update_staff_permissions", "user", row.user_id, permissions)
        db.session.commit()
        flash(f"บันทึกสิทธิ์ของ {row.user.username} แล้ว", "success")
    return redirect(url_for("admin_staff"))
