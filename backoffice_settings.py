"""หน้าตั้งค่ารายกลุ่มหวยต่อสมาชิก ของ Agent และ Senior — ใช้โค้ดชุดเดียวกัน

โครงตามหลังบ้านของเว็บตัวอย่าง: เลือก "กลุ่มหวย" (หมวดหมู่) แล้วตั้งค่าให้สมาชิกทีละคน
  rates  — อัตราจ่าย / ส่วนลด (คอมมิชชั่น) แยกตามชุดอัตราจ่าย
  limits — ขั้นต่ำ / สูงสุด / สูงสุดต่อเลข
  access — เปิด-ปิดกลุ่มหวย และชุดอัตราจ่าย
  stock  — ข้อมูลทั่วไป / รหัสผ่าน / % ถือหุ้น
ค่าที่ตั้งถูกบังคับใช้ตอนสมาชิกส่งโพยใน add_thai_lottery_bets (ดู member_group_* ใน app.py)

ไฟล์นี้ถูก import ท้าย app.py เพื่อลงทะเบียนเส้นทาง /partner/... และ /senior/..."""
import re

from flask import abort, flash, redirect, render_template, request, url_for

from app import (
    BET_TYPE_LABELS, EXTRA_BET_TYPES, MAX_BET_AMOUNT, RATE_TABLE_ORDER, RATE_TIER_NAMES, app,
    active_lottery_rooms, agent_upline_senior, available_rate_tiers, current_user, db,
    get_lottery_rates, get_rate_set, get_senior_payout_rates, get_type_rules, partner_owner,
    partner_required, room_category_name, senior_agent_ids, senior_owner, senior_required,
)
from models import (
    LoginHistory, MemberGroupAccess, MemberGroupLimit, MemberGroupRate, MemberGroupStock, User,
)

SECTIONS = {
    "rates": "อัตราจ่าย/คอมมิชชั่น",
    "limits": "ขั้นต่ำ/สูงสุด/สูงสุดต่อเลข",
    "access": "เปิด-ปิด อัตราจ่าย/กลุ่มหวย",
    "stock": "ข้อมูลทั่วไป/เก็บของ/แบ่งหุ้น",
}
SUBTABS = {
    "rates": [("payout", "อัตราจ่าย"), ("discount", "คอมมิชชั่น (ลด %)")],
    "limits": [("min", "ขั้นต่ำ"), ("max", "สูงสุด"), ("num", "สูงสุดต่อเลข")],
    "access": [("all", "เปิด/ปิด กลุ่มหวยและอัตราจ่าย")],
    "stock": [("general", "ข้อมูลทั่วไป"), ("password", "รหัสผ่าน"), ("stock", "แบ่งหุ้น/เก็บของ")],
}
MAX_ROWS = 300

ROLES = {
    "partner": {"decorator": partner_required, "owner": partner_owner, "shell": "backoffice_shell_partner.html"},
    "senior": {"decorator": senior_required, "owner": senior_owner, "shell": "backoffice_shell_senior.html"},
}


def scoped_members(role, owner):
    """สมาชิกที่ผู้ดูแลคนนี้ตั้งค่าให้ได้: Agent = สมาชิกที่ดูแลตรง, Senior = สมาชิกของทุก Agent ในสาย"""
    if role == "partner":
        query = User.query.filter_by(partner_id=owner.id, role="member")
    else:
        agent_ids = senior_agent_ids(owner)
        query = User.query.filter(User.partner_id.in_(agent_ids or [-1]), User.role == "member")
    return query


def lottery_groups():
    names = []
    for room in active_lottery_rooms().all():
        name = room_category_name(room) or "อื่นๆ"
        if name not in names:
            names.append(name)
    return names


def bet_types_for_tables():
    rates = get_lottery_rates()
    return list(RATE_TABLE_ORDER) + [t for t in EXTRA_BET_TYPES if rates.get(t)]


def received_values(role, owner, category, tier):
    """สิ่งที่ผู้ดูแลคนนี้ 'รับมา' จากด้านบน (ใช้เป็นตัวเทียบตอนตั้งค่าให้สมาชิก)"""
    rates = get_lottery_rates()
    tier_set = get_rate_set(category, tier)
    for bet_type, row in tier_set.items():
        if row.payout_multiplier > 0:
            rates[bet_type] = float(row.payout_multiplier)
    if role == "partner":
        senior = agent_upline_senior(owner)
        if senior is not None:
            rates.update(get_senior_payout_rates(senior))
    type_rules = get_type_rules()
    discounts = {}
    for bet_type in bet_types_for_tables():
        rule = type_rules.get(bet_type, {"discount": 0.0, "min": 1, "max": MAX_BET_AMOUNT})
        discounts[bet_type] = tier_set[bet_type].discount_pct if bet_type in tier_set else rule["discount"]
    return rates, discounts, type_rules


def parse_number(raw, kind=float):
    raw = (raw or "").strip()
    if raw == "":
        return None
    return kind(raw)


def make_view(role):
    cfg = ROLES[role]
    endpoint_name = f"{role}_member_settings"

    @cfg["decorator"]
    def view(section):
        if section not in SECTIONS:
            abort(404)
        owner = cfg["owner"](current_user())
        groups = lottery_groups()
        category = request.values.get("category") or (groups[0] if groups else "")
        subs = SUBTABS[section]
        sub = request.values.get("sub") or subs[0][0]
        if sub not in {key for key, _ in subs}:
            sub = subs[0][0]
        tiers = available_rate_tiers(category)
        tier = request.values.get("tier", 1, type=int)
        if tier not in tiers:
            tier = 1
        base_query = scoped_members(role, owner)
        keyword = (request.values.get("q") or "").strip()
        if keyword:
            like = f"%{keyword}%"
            base_query = base_query.filter(User.username.ilike(like) | User.full_name.ilike(like))
        members = base_query.order_by(User.username.asc()).limit(MAX_ROWS).all()
        member_ids = {m.id for m in members}
        bet_types = bet_types_for_tables()
        back = redirect(url_for(endpoint_name, section=section, category=category, sub=sub, tier=tier, q=keyword or None))

        if request.method == "POST":
            selected = {int(x) for x in request.form.getlist("sel") if x.isdigit()} & member_ids
            if section != "access" and not selected:
                flash("กรุณาเลือกสมาชิกที่ต้องการบันทึกอย่างน้อย 1 คน", "error")
                return back
            try:
                message = SAVERS[section](owner, category, tier, sub, members, selected, bet_types)
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
                return back
            db.session.commit()
            flash(message, "success")
            return back

        ctx = {
            "role": role, "shell": cfg["shell"], "owner": owner, "section": section,
            "section_title": SECTIONS[section], "sections": SECTIONS, "subs": subs, "sub": sub,
            "groups": groups, "category": category, "tiers": tiers, "tier": tier,
            "tier_names": RATE_TIER_NAMES, "members": members, "bet_types": bet_types,
            "labels": BET_TYPE_LABELS, "keyword": keyword, "endpoint": endpoint_name,
            "partner": owner, "senior": owner,
        }
        ids = list(member_ids) or [-1]
        if section == "rates":
            rates, discounts, _ = received_values(role, owner, category, tier)
            ctx.update(received_rates=rates, received_discounts=discounts)
            rows = MemberGroupRate.query.filter(
                MemberGroupRate.owner_id == owner.id, MemberGroupRate.member_id.in_(ids),
                MemberGroupRate.category == category, MemberGroupRate.tier == tier,
            ).all()
            ctx["cells"] = {(r.member_id, r.bet_type): r for r in rows}
        elif section == "limits":
            _, _, type_rules = received_values(role, owner, category, 1)
            ctx["type_rules"] = type_rules
            rows = MemberGroupLimit.query.filter(
                MemberGroupLimit.owner_id == owner.id, MemberGroupLimit.member_id.in_(ids),
                MemberGroupLimit.category == category,
            ).all()
            ctx["cells"] = {(r.member_id, r.bet_type): r for r in rows}
        elif section == "access":
            rows = MemberGroupAccess.query.filter(
                MemberGroupAccess.owner_id == owner.id, MemberGroupAccess.member_id.in_(ids),
                MemberGroupAccess.category == category,
            ).all()
            ctx["access"] = {(r.member_id, r.tier): r.is_enabled for r in rows}
        else:
            rows = MemberGroupStock.query.filter(
                MemberGroupStock.owner_id == owner.id, MemberGroupStock.member_id.in_(ids),
                MemberGroupStock.category == category,
            ).all()
            ctx["stocks"] = {r.member_id: r.hold_percent for r in rows}
            last_login = {}
            for entry in LoginHistory.query.filter(LoginHistory.user_id.in_(ids)).order_by(LoginHistory.id.asc()):
                last_login[entry.user_id] = entry
            ctx["last_login"] = last_login
        return render_template("bo_member_settings.html", **ctx)

    view.__name__ = endpoint_name
    return endpoint_name, view


# ------------------------- ตัวบันทึกแต่ละหน้า -------------------------
def _number(raw, label, kind=float, low=None, high=None):
    try:
        value = parse_number(raw, kind)
    except ValueError:
        raise ValueError(f"{label} ต้องเป็นตัวเลข")
    if value is not None and ((low is not None and value < low) or (high is not None and value > high)):
        raise ValueError(f"{label} ต้องอยู่ระหว่าง {low} ถึง {high}" if high is not None else f"{label} ต้องไม่น้อยกว่า {low}")
    return value


def save_rates(owner, category, tier, sub, members, selected, bet_types):
    count = 0
    for member in members:
        if member.id not in selected:
            continue
        for bet_type in bet_types:
            label = BET_TYPE_LABELS.get(bet_type, bet_type)
            payout = _number(request.form.get(f"p_{member.id}_{bet_type}"), f"อัตราจ่าย {label}", float, low=0.0001)
            discount = _number(request.form.get(f"d_{member.id}_{bet_type}"), f"ส่วนลด {label}", float, low=0, high=100)
            row = MemberGroupRate.query.filter_by(
                owner_id=owner.id, member_id=member.id, category=category, tier=tier, bet_type=bet_type
            ).first()
            if payout is None and discount is None:
                if row:
                    db.session.delete(row)
                continue
            if row is None:
                row = MemberGroupRate(owner_id=owner.id, member_id=member.id, category=category, tier=tier, bet_type=bet_type)
                db.session.add(row)
            row.payout_multiplier, row.discount_pct = payout, discount
        count += 1
    return f"บันทึกอัตราจ่าย/คอมมิชชั่น {category} ให้สมาชิก {count} คนแล้ว"


def save_limits(owner, category, tier, sub, members, selected, bet_types):
    count = 0
    for member in members:
        if member.id not in selected:
            continue
        for bet_type in bet_types:
            label = BET_TYPE_LABELS.get(bet_type, bet_type)
            low = _number(request.form.get(f"min_{member.id}_{bet_type}"), f"ขั้นต่ำ {label}", int, low=1)
            high = _number(request.form.get(f"max_{member.id}_{bet_type}"), f"สูงสุด {label}", int, low=1)
            per_number = _number(request.form.get(f"num_{member.id}_{bet_type}"), f"สูงสุดต่อเลข {label}", int, low=1)
            if low is not None and high is not None and high < low:
                raise ValueError(f"{label}: ยอดสูงสุดต้องไม่น้อยกว่าขั้นต่ำ")
            if low is not None and per_number is not None and per_number < low:
                raise ValueError(f"{label}: สูงสุดต่อเลขต้องไม่น้อยกว่าขั้นต่ำ")
            row = MemberGroupLimit.query.filter_by(
                owner_id=owner.id, member_id=member.id, category=category, bet_type=bet_type
            ).first()
            if low is None and high is None and per_number is None:
                if row:
                    db.session.delete(row)
                continue
            if row is None:
                row = MemberGroupLimit(owner_id=owner.id, member_id=member.id, category=category, bet_type=bet_type)
                db.session.add(row)
            row.min_bet, row.max_bet, row.max_number_bet = low, high, per_number
        count += 1
    return f"บันทึกขั้นต่ำ/สูงสุด {category} ให้สมาชิก {count} คนแล้ว"


def _set_access(owner, member, category, tier, enabled):
    row = MemberGroupAccess.query.filter_by(
        owner_id=owner.id, member_id=member.id, category=category, tier=tier
    ).first()
    if enabled:
        if row:
            db.session.delete(row)
        return
    if row is None:
        row = MemberGroupAccess(owner_id=owner.id, member_id=member.id, category=category, tier=tier)
        db.session.add(row)
    row.is_enabled = False


def save_access(owner, category, tier, sub, members, selected, bet_types):
    count = 0
    tiers = [t for t in available_rate_tiers(category) if t != 1]
    for member in members:
        if f"g_{member.id}" not in request.form:
            continue
        _set_access(owner, member, category, 0, request.form.get(f"g_{member.id}") == "1")
        for t in tiers:
            _set_access(owner, member, category, t, request.form.get(f"t{t}_{member.id}", "1") == "1")
        count += 1
    return f"บันทึกการเปิด/ปิด {category} ให้สมาชิก {count} คนแล้ว"


def save_stock(owner, category, tier, sub, members, selected, bet_types):
    count = 0
    if sub == "general":
        for member in members:
            if member.id not in selected:
                continue
            name = (request.form.get(f"name_{member.id}") or "").strip()
            if not name:
                raise ValueError(f"กรุณากรอกชื่อของ {member.username}")
            member.full_name = name[:120]
            member.phone = (request.form.get(f"phone_{member.id}") or "").strip()[:20]
            member.is_active = request.form.get(f"active_{member.id}") == "1"
            count += 1
        return f"บันทึกข้อมูลสมาชิก {count} คนแล้ว"
    if sub == "password":
        for member in members:
            if member.id not in selected:
                continue
            password = request.form.get(f"pw_{member.id}") or ""
            if password == "":
                continue
            if len(password) < 6 or not re.fullmatch(r"[A-Za-z0-9]+", password):
                raise ValueError(f"รหัสผ่านของ {member.username} ต้องเป็นภาษาอังกฤษและตัวเลขอย่างน้อย 6 ตัว")
            member.set_password(password)
            count += 1
        if count == 0:
            raise ValueError("ยังไม่ได้กรอกรหัสผ่านใหม่")
        return f"เปลี่ยนรหัสผ่านสมาชิก {count} คนแล้ว"
    for member in members:
        if member.id not in selected:
            continue
        percent = _number(request.form.get(f"hold_{member.id}"), f"% ถือหุ้นของ {member.username}", float, low=0, high=100)
        row = MemberGroupStock.query.filter_by(owner_id=owner.id, member_id=member.id, category=category).first()
        if percent is None:
            if row:
                db.session.delete(row)
        else:
            if row is None:
                row = MemberGroupStock(owner_id=owner.id, member_id=member.id, category=category)
                db.session.add(row)
            row.hold_percent = percent
        count += 1
    return f"บันทึก % ถือหุ้น {category} ให้สมาชิก {count} คนแล้ว"


SAVERS = {"rates": save_rates, "limits": save_limits, "access": save_access, "stock": save_stock}


for _role in ROLES:
    _endpoint, _view = make_view(_role)
    app.add_url_rule(f"/{_role}/member-settings/<section>", endpoint=_endpoint, view_func=_view, methods=["GET", "POST"])


# ------------------------- เพิ่มสมาชิก (ตัวช่วย 4 ขั้น) -------------------------
from app import adjust_credit, notify_user  # noqa: E402
from models import PartnerStockShare, SeniorStockShare  # noqa: E402


def make_wizard(role):
    cfg = ROLES[role]
    endpoint = f"{role}_member_new"

    @cfg["decorator"]
    def view():
        owner = cfg["owner"](current_user())
        groups = lottery_groups()
        bet_types = bet_types_for_tables()
        agents = []
        if role == "senior":
            agents = User.query.filter(User.id.in_(senior_agent_ids(owner) or [-1]), User.role == "partner").order_by(User.username).all()
        tiers_by_group = {g: available_rate_tiers(g) for g in groups}

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            full_name = request.form.get("full_name", "").strip()
            phone = request.form.get("phone", "").strip()
            try:
                credit = round(float(request.form.get("credit") or 0), 2)
            except ValueError:
                credit = -1
            agent = owner
            error = None
            if role == "senior":
                agent = next((a for a in agents if a.id == request.form.get("agent_id", type=int)), None)
                if agent is None:
                    error = "กรุณาเลือก Agent ผู้ดูแลสมาชิก"
            if error is None:
                if len(username) < 4 or not re.fullmatch(r"[A-Za-z0-9]+", username):
                    error = "ชื่อผู้ใช้ต้องเป็นภาษาอังกฤษหรือตัวเลขอย่างน้อย 4 ตัว"
                elif len(password) < 6 or not re.fullmatch(r"[A-Za-z0-9]+", password):
                    error = "รหัสผ่านต้องเป็นภาษาอังกฤษและตัวเลขอย่างน้อย 6 ตัว"
                elif not full_name:
                    error = "กรุณากรอกชื่อสมาชิก"
                elif User.query.filter_by(username=username).first():
                    error = "ชื่อผู้ใช้นี้ถูกใช้แล้ว"
                elif credit < 0 or credit > owner.credit_balance:
                    error = f"เครดิตเริ่มต้นต้องอยู่ระหว่าง 0 ถึง {owner.credit_balance:,.2f} (เครดิตที่คุณมี)"
            if error is None:
                try:
                    member = User(username=username, full_name=full_name, phone=phone, role="member",
                                  partner_id=agent.id, points=0, credit_balance=0.0)
                    member.set_password(password)
                    db.session.add(member)
                    db.session.flush()
                    apply_wizard_settings(owner, member, groups, bet_types, tiers_by_group)
                    if credit > 0:
                        adjust_credit(owner, -credit, f"โอนเครดิตเริ่มต้นให้สมาชิกใหม่ {username}")
                        adjust_credit(member, credit, f"เครดิตเริ่มต้นจาก {owner.username}")
                        notify_user(member, "ได้รับเครดิตเริ่มต้น", f"เครดิต {credit:,.2f}", "wallet")
                    db.session.commit()
                except ValueError as exc:
                    db.session.rollback()
                    error = str(exc)
            if error:
                flash(error, "error")
                return redirect(url_for(endpoint))
            flash(f"เพิ่มสมาชิก {username} และตั้งค่าเริ่มต้นเรียบร้อยแล้ว", "success")
            return redirect(url_for(f"{role}_member_settings", section="stock", sub="general"))

        return render_template(
            "bo_member_new.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            groups=groups, bet_types=bet_types, labels=BET_TYPE_LABELS, tiers_by_group=tiers_by_group,
            tier_names=RATE_TIER_NAMES, agents=agents, type_rules=get_type_rules(), owner=owner,
        )

    view.__name__ = endpoint
    return endpoint, view


def apply_wizard_settings(owner, member, groups, bet_types, tiers_by_group):
    """บันทึกค่าเริ่มต้นที่กรอกในตัวช่วย — ชื่อช่องอ้างกลุ่มหวยด้วยลำดับ (0,1,2,…) ตามรายการกลุ่มตอนแสดงฟอร์ม"""
    form = request.form
    for ci, category in enumerate(groups):
        if form.get(f"acc_{ci}") == "0":
            _set_access(owner, member, category, 0, False)
        for tier in tiers_by_group[category]:
            if tier != 1 and form.get(f"acc_{ci}_t{tier}") == "0":
                _set_access(owner, member, category, tier, False)
        hold = _number(form.get(f"hold_{ci}"), f"% ถือหุ้น {category}", float, low=0, high=100)
        if hold is not None:
            db.session.add(MemberGroupStock(owner_id=owner.id, member_id=member.id, category=category, hold_percent=hold))
        for bet_type in bet_types:
            label = f"{category} {BET_TYPE_LABELS.get(bet_type, bet_type)}"
            low = _number(form.get(f"min_{ci}_{bet_type}"), f"ขั้นต่ำ {label}", int, low=1)
            high = _number(form.get(f"max_{ci}_{bet_type}"), f"สูงสุด {label}", int, low=1)
            per_number = _number(form.get(f"num_{ci}_{bet_type}"), f"สูงสุดต่อเลข {label}", int, low=1)
            if low is not None and high is not None and high < low:
                raise ValueError(f"{label}: ยอดสูงสุดต้องไม่น้อยกว่าขั้นต่ำ")
            if low is not None and per_number is not None and per_number < low:
                raise ValueError(f"{label}: สูงสุดต่อเลขต้องไม่น้อยกว่าขั้นต่ำ")
            if not (low is None and high is None and per_number is None):
                db.session.add(MemberGroupLimit(owner_id=owner.id, member_id=member.id, category=category,
                                                bet_type=bet_type, min_bet=low, max_bet=high, max_number_bet=per_number))
            for tier in tiers_by_group[category]:
                payout = _number(form.get(f"p_{ci}_{tier}_{bet_type}"), f"อัตราจ่าย {label}", float, low=0.0001)
                discount = _number(form.get(f"d_{ci}_{tier}_{bet_type}"), f"ส่วนลด {label}", float, low=0, high=100)
                if payout is not None or discount is not None:
                    db.session.add(MemberGroupRate(owner_id=owner.id, member_id=member.id, category=category, tier=tier,
                                                   bet_type=bet_type, payout_multiplier=payout, discount_pct=discount))


# ------------------------- ส่วนแบ่งที่ได้รับ -------------------------
def make_myshare(role):
    cfg = ROLES[role]
    endpoint = f"{role}_myshare"

    @cfg["decorator"]
    def view():
        owner = cfg["owner"](current_user())
        groups = lottery_groups()
        category = request.args.get("category") or (groups[0] if groups else "")
        tiers = available_rate_tiers(category)
        rooms = [r for r in active_lottery_rooms().all() if (room_category_name(r) or "อื่นๆ") == category]
        share_model, id_field = (PartnerStockShare, "partner_id") if role == "partner" else (SeniorStockShare, "senior_id")
        shares = {
            row.room_id: row.hold_percent
            for row in share_model.query.filter(getattr(share_model, id_field) == owner.id, share_model.room_id.in_([r.id for r in rooms] or [-1]))
        }
        tables = []
        for tier in tiers:
            rates, discounts, type_rules = received_values(role, owner, category, tier)
            tables.append({"tier": tier, "name": RATE_TIER_NAMES[tier], "rates": rates, "discounts": discounts})
        profile = owner.partner_profile if role == "partner" else owner.senior_profile
        return render_template(
            "bo_myshare.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            groups=groups, category=category, rooms=rooms, shares=shares, tables=tables, type_rules=get_type_rules(),
            bet_types=bet_types_for_tables(), labels=BET_TYPE_LABELS, commission_rate=profile.commission_rate if profile else 0,
        )

    view.__name__ = endpoint
    return endpoint, view


for _role in ROLES:
    _ep, _v = make_wizard(_role)
    app.add_url_rule(f"/{_role}/members/new", endpoint=_ep, view_func=_v, methods=["GET", "POST"])
    _ep, _v = make_myshare(_role)
    app.add_url_rule(f"/{_role}/myshare", endpoint=_ep, view_func=_v, methods=["GET"])
