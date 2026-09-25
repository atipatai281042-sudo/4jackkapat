"""รายงานและหน้าดูของของ Agent/Senior (โค้ดชุดเดียวกัน) ตามหลังบ้านของเว็บตัวอย่าง

  /<role>/overall         ดูของรวม/คาดคะเนได้เสีย: ซื้อ/คอม/รับ/จ่ายสูงสุด ต่อประเภท + รายเลข + ตั้งสู้
  /<role>/takelist        รายการเก็บของตามสมาชิก (ต่อตลาด/งวด)
  /<role>/pnl/<kind>      แพ้-ชนะ แยกตามสมาชิก / ประเภท / วัน — แบ่ง 3 ฝ่าย สมาชิก · คุณ · บริษัท

ตัวเลขทุกตัวคำนวณจากรายการแทงและสมุดบัญชีที่ระบบบันทึกไว้จริง (ThaiLotteryBet + Commission/StockLedger)
ไฟล์นี้ถูก import ท้าย app.py"""
from collections import defaultdict
from datetime import datetime, timedelta

from flask import abort, flash, redirect, render_template, request, session, url_for

from app import (
    BET_TYPE_LABELS, EXTRA_BET_TYPES, RATE_TABLE_ORDER, _bet_history_range, app, active_lottery_rooms,
    app_now, current_user, db, group_visible, get_lottery_rates, hold_cap_for, normalize_bet_type, partner_owner, partner_required,
    room_category_name, senior_agent_ids, senior_owner, senior_required,
)
from models import (
    CommissionLedger, LotteryRoom, MemberGroupStock, PartnerAcceptanceLimit, PartnerMemberStockShare,
    PartnerStockLedger, PartnerStockShare, SeniorAcceptanceLimit, SeniorCommissionLedger,
    SeniorMemberStockShare, SeniorStockLedger, SeniorStockShare, ThaiLotteryBet, ThaiLotteryPeriod, User,
)

ROLES = {
    "partner": {"decorator": partner_required, "owner": partner_owner, "shell": "backoffice_shell_partner.html"},
    "senior": {"decorator": senior_required, "owner": senior_owner, "shell": "backoffice_shell_senior.html"},
}
PNL_TITLES = {"member": "แพ้-ชนะ สมาชิก/ประเภท", "date": "แพ้-ชนะ สุทธิ"}


def room_visible(room):
    """ตลาดนี้ผู้ใช้ปัจจุบัน (รวมผู้ช่วยที่ถูกจำกัดสิทธิ์กลุ่มหวยพิเศษ) มองเห็นไหม"""
    return group_visible(room_category_name(room) or "อื่นๆ")


def bet_types_list():
    rates = get_lottery_rates()
    return list(RATE_TABLE_ORDER) + [t for t in EXTRA_BET_TYPES if rates.get(t)]


def scope_query(role, owner):
    """รายการแทงของสมาชิกในสายของผู้ดูแลคนนี้ (ไม่รวมที่ยกเลิก)"""
    query = ThaiLotteryBet.query.join(User, ThaiLotteryBet.user_id == User.id).filter(
        ThaiLotteryBet.status != "cancelled"
    )
    if role == "partner":
        return query.filter(User.partner_id == owner.id)
    return query.filter(User.partner_id.in_(senior_agent_ids(owner) or [-1]))


def owner_models(role):
    if role == "partner":
        return {
            "commission": CommissionLedger, "commission_owner": CommissionLedger.partner_id,
            "stock": PartnerStockLedger, "stock_owner": PartnerStockLedger.partner_id,
        }
    return {
        "commission": SeniorCommissionLedger, "commission_owner": SeniorCommissionLedger.senior_id,
        "stock": SeniorStockLedger, "stock_owner": SeniorStockLedger.senior_id,
    }


def ledger_maps(role, owner, bet_ids):
    """{bet_id: ค่าคอมของผู้ดูแล}, {bet_id: กำไร/ขาดทุนจากหุ้นของผู้ดูแล}"""
    models = owner_models(role)
    commission, stock = defaultdict(float), defaultdict(float)
    if not bet_ids:
        return commission, stock
    for chunk_start in range(0, len(bet_ids), 500):
        chunk = bet_ids[chunk_start:chunk_start + 500]
        for row in models["commission"].query.filter(models["commission_owner"] == owner.id, models["commission"].bet_id.in_(chunk)):
            commission[row.bet_id] += float(row.commission_amount)
        for row in models["stock"].query.filter(models["stock_owner"] == owner.id, models["stock"].bet_id.in_(chunk)):
            stock[row.bet_id] += float(row.pnl_amount)
    return commission, stock


def effective_hold_percent(role, owner, member, room, cache):
    """% ถือหุ้นของผู้ดูแลกับสมาชิกคนนี้ในห้องนี้ — ลำดับเดียวกับตอนตรวจรางวัล
    (เฉพาะสมาชิก+ห้อง > เฉพาะสมาชิก+กลุ่มหวย > ค่าระดับห้อง)"""
    key = (member.id, room.id)
    if key in cache:
        return cache[key]
    if role == "partner":
        member_share, room_share, id_field = PartnerMemberStockShare, PartnerStockShare, "partner_id"
    else:
        member_share, room_share, id_field = SeniorMemberStockShare, SeniorStockShare, "senior_id"
    row = member_share.query.filter_by(**{id_field: owner.id, "member_id": member.id, "room_id": room.id}).first()
    if row is not None:
        percent = row.hold_percent
    else:
        group = MemberGroupStock.query.filter_by(owner_id=owner.id, member_id=member.id, category=room_category_name(room)).first()
        if group is not None:
            percent = group.hold_percent
        else:
            share = room_share.query.filter_by(**{id_field: owner.id, "room_id": room.id}).first()
            percent = share.hold_percent if share else 0.0
    cache[key] = percent
    return percent


def market_and_period():
    """ตัวเลือกตลาด (ห้อง) + งวด ที่ใช้ร่วมกันทุกหน้า — ค่าเริ่มต้น: ห้องแรกที่มีงวดเปิดอยู่ ไม่งั้นห้องแรก"""
    rooms = active_lottery_rooms().all()
    rooms = [r for r in rooms if room_visible(r)]
    room_id = request.values.get("room_id", type=int)
    remembered = False
    if room_id is None:
        room_id = session.get("bo_room_id")
        remembered = True
    room = next((r for r in rooms if r.id == room_id), None)
    if room is not None and not remembered:
        session["bo_room_id"] = room.id
    if room is None:
        now = app_now()
        open_room_ids = {
            p.room_id for p in ThaiLotteryPeriod.query.filter(
                ThaiLotteryPeriod.is_open.is_(True), ThaiLotteryPeriod.close_time > now
            )
        }
        room = next((r for r in rooms if r.id in open_room_ids), rooms[0] if rooms else None)
    periods = ThaiLotteryPeriod.query.filter_by(room_id=room.id).order_by(ThaiLotteryPeriod.id.desc()).limit(15).all() if room else []
    period_id = request.values.get("period_id", type=int)
    date_value = (request.values.get("date") or "").strip()
    period = next((p for p in periods if p.id == period_id), None)
    if period is None and date_value:
        period = next((p for p in periods if p.period_date == date_value), None)
        if period is not None:
            session["bo_date"] = date_value
    if period is None and not request.values.get("room_id") and session.get("bo_date"):
        period = next((p for p in periods if p.period_date == session.get("bo_date")), None)
    if period is None:
        period = periods[0] if periods else None
    return rooms, room, periods, period


# ----------------------------- ดูของรวม -----------------------------
def make_overall(role):
    cfg = ROLES[role]
    endpoint = f"{role}_overall"

    @cfg["decorator"]
    def view():
        owner = cfg["owner"](current_user())
        rooms, room, periods, period = market_and_period()
        types = bet_types_list()
        limit_model = PartnerAcceptanceLimit if role == "partner" else SeniorAcceptanceLimit
        owner_field = "partner_id" if role == "partner" else "senior_id"

        if request.method == "POST" and room:
            for bet_type in types:
                raw = (request.form.get(f"cap_{bet_type}") or "").strip()
                row = limit_model.query.filter_by(**{owner_field: owner.id, "room_id": room.id, "bet_type": bet_type}).first()
                if raw == "":
                    if row:
                        db.session.delete(row)
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    flash("ตั้งสู้ต้องเป็นตัวเลข", "error")
                    return redirect(url_for(endpoint, room_id=room.id, period_id=period.id if period else None))
                if value < 0:
                    flash("ตั้งสู้ต้องไม่ติดลบ", "error")
                    return redirect(url_for(endpoint, room_id=room.id, period_id=period.id if period else None))
                if row is None:
                    row = limit_model(**{owner_field: owner.id, "room_id": room.id, "bet_type": bet_type})
                    db.session.add(row)
                row.amount_limit = value
            db.session.commit()
            flash(f"บันทึกตั้งสู้ของ {room.name} แล้ว", "success")
            return redirect(url_for(endpoint, room_id=room.id, period_id=period.id if period else None))

        totals = {key: defaultdict(float) for key in ("buy", "comm", "held", "max")}
        numbers = defaultdict(lambda: {"stake": 0.0, "max": 0.0, "members": set()})
        bets = []
        if period:
            bets = scope_query(role, owner).filter(ThaiLotteryBet.period_id == period.id).all()
        commission, _ = ledger_maps(role, owner, [b.id for b in bets])
        cache = {}
        caps = {
            row.bet_type: row.amount_limit for row in limit_model.query.filter_by(**{owner_field: owner.id, "room_id": room.id})
        } if room else {}
        prior_held = defaultdict(float)  # ตั้งสู้ต่อเลข: นับสะสมตามลำดับโพย เหมือนตอนตรวจรางวัลจริง
        cap_cache = {}
        for bet in sorted(bets, key=lambda b: b.id):
            percent = effective_hold_percent(role, owner, bet.user, room, cache)
            stake, payout = float(bet.amount), float(bet.amount) * float(bet.rate)
            held = stake * percent / 100
            cap = cap_cache.get((bet.bet_type, bet.number), "?")
            if cap == "?":
                cap = cap_cache[(bet.bet_type, bet.number)] = hold_cap_for(role, owner.id, room.id, period.id, bet.bet_type, bet.number)
            if cap is not None and percent > 0:
                held = min(held, max(0.0, float(cap) - prior_held[(bet.bet_type, bet.number)]))
            prior_held[(bet.bet_type, bet.number)] += held
            totals["buy"][bet.bet_type] += stake
            totals["comm"][bet.bet_type] += commission.get(bet.id, 0.0)
            totals["held"][bet.bet_type] += held
            totals["max"][bet.bet_type] += payout * (held / stake if stake else 0.0)
            entry = numbers[(bet.bet_type, bet.number)]
            entry["stake"] += stake
            entry["max"] += payout
            entry["members"].add(bet.user_id)

        view_filter = request.args.get("type") or ""
        sort = request.args.get("sort") or "max"
        rows = [
            {"type": t, "number": n, "stake": v["stake"], "max": v["max"], "members": len(v["members"]),
             "over": bool(caps.get(t) is not None and v["stake"] > caps[t])}
            for (t, n), v in numbers.items() if not view_filter or t == view_filter
        ]
        rows.sort(key=lambda r: (-r["max"], r["number"]) if sort == "max" else ((-r["stake"], r["number"]) if sort == "stake" else (r["type"], r["number"])))
        summary = {key: sum(totals[key].values()) for key in totals}
        return render_template(
            "bo_overall.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            rooms=rooms, room=room, periods=periods, period=period, types=types, labels=BET_TYPE_LABELS,
            show_market_bar=True, totals=totals, summary=summary, caps=caps, rows=rows[:300], view_filter=view_filter, sort=sort,
            bet_count=len(bets),
        )

    view.__name__ = endpoint
    return endpoint, view


# ------------------------- รายการเก็บของตามสมาชิก -------------------------
def make_takelist(role):
    cfg = ROLES[role]
    endpoint = f"{role}_takelist"

    @cfg["decorator"]
    def view():
        owner = cfg["owner"](current_user())
        rooms, room, periods, period = market_and_period()
        types = bet_types_list()
        table = defaultdict(lambda: {"stake": defaultdict(float), "held": defaultdict(float), "count": 0})
        members = {}
        cache = {}
        if period:
            limit_model = PartnerAcceptanceLimit if role == "partner" else SeniorAcceptanceLimit
            owner_field = "partner_id" if role == "partner" else "senior_id"
            caps = {
                row.bet_type: row.amount_limit for row in limit_model.query.filter_by(**{owner_field: owner.id, "room_id": room.id})
            }
            prior_held = defaultdict(float)
            cap_cache = {}
            for bet in sorted(scope_query(role, owner).filter(ThaiLotteryBet.period_id == period.id).all(), key=lambda b: b.id):
                members[bet.user_id] = bet.user
                percent = effective_hold_percent(role, owner, bet.user, room, cache)
                held = float(bet.amount) * percent / 100
                cap = cap_cache.get((bet.bet_type, bet.number), "?")
                if cap == "?":
                    cap = cap_cache[(bet.bet_type, bet.number)] = hold_cap_for(role, owner.id, room.id, period.id, bet.bet_type, bet.number)
                if cap is not None and percent > 0:
                    held = min(held, max(0.0, float(cap) - prior_held[(bet.bet_type, bet.number)]))
                prior_held[(bet.bet_type, bet.number)] += held
                row = table[bet.user_id]
                row["stake"][bet.bet_type] += float(bet.amount)
                row["held"][bet.bet_type] += held
                row["count"] += 1
        rows = sorted(
            ({"member": members[mid], **data, "total": sum(data["stake"].values()), "held_total": sum(data["held"].values())}
             for mid, data in table.items()),
            key=lambda r: -r["total"],
        )
        return render_template(
            "bo_takelist.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            rooms=rooms, room=room, periods=periods, period=period, types=types, labels=BET_TYPE_LABELS, rows=rows,
            show_market_bar=True,
        )

    view.__name__ = endpoint
    return endpoint, view


# ----------------------------- แพ้-ชนะ 3 ฝ่าย -----------------------------
def make_pnl(role):
    cfg = ROLES[role]
    endpoint = f"{role}_pnl"

    @cfg["decorator"]
    def view(kind):
        if kind not in PNL_TITLES:
            abort(404)
        owner = cfg["owner"](current_user())
        range_key, start_day, end_day = _bet_history_range(request.args)
        start_utc = datetime.combine(start_day, datetime.min.time()) - timedelta(hours=7)
        end_utc = datetime.combine(end_day + timedelta(days=1), datetime.min.time()) - timedelta(hours=7)
        rooms = active_lottery_rooms().all()
        room_id = request.args.get("room_id", type=int)
        group_by = request.args.get("by") or ("member" if kind == "member" else "type")
        if group_by not in ("member", "type", "room", "date"):
            group_by = "member"
        query = scope_query(role, owner).filter(
            ThaiLotteryBet.created_at >= start_utc, ThaiLotteryBet.created_at < end_utc,
            ThaiLotteryBet.status.in_(["win", "lose"]),
        )
        if room_id:
            query = query.join(ThaiLotteryPeriod, ThaiLotteryBet.period_id == ThaiLotteryPeriod.id).filter(ThaiLotteryPeriod.room_id == room_id)
        bets = query.all()
        commission, stock = ledger_maps(role, owner, [b.id for b in bets])
        room_names = {r.id: r.name for r in LotteryRoom.query.all()}

        def key_of(bet):
            if group_by == "member":
                return bet.user.username
            if group_by == "type":
                return BET_TYPE_LABELS.get(bet.bet_type, bet.bet_type)
            if group_by == "room":
                return room_names.get(bet.period.room_id, "-") if bet.period else "-"
            return (bet.created_at + timedelta(hours=7)).strftime("%Y-%m-%d")

        rows = defaultdict(lambda: defaultdict(float))
        for bet in bets:
            r = rows[key_of(bet)]
            stake = float(bet.amount)
            discount = float(bet.discount_amount or 0)
            win = float(bet.reward_amount) if bet.status == "win" else 0.0
            member_net = win - stake + discount  # ได้เสียของสมาชิก (บวก = สมาชิกได้)
            owner_net = commission.get(bet.id, 0.0) + stock.get(bet.id, 0.0)
            r["count"] += 1
            r["stake"] += stake
            r["m_discount"] += discount
            r["m_win"] += win
            r["m_net"] += member_net
            r["o_stock"] += stock.get(bet.id, 0.0)
            r["o_comm"] += commission.get(bet.id, 0.0)
            r["o_net"] += owner_net
            r["c_net"] += -(member_net + owner_net)
        table = [{"name": name, **dict(values)} for name, values in rows.items()]
        table.sort(key=lambda r: r["name"] if group_by == "date" else -r["stake"], reverse=(group_by == "date"))
        totals = defaultdict(float)
        for row in table:
            for key, value in row.items():
                if key != "name":
                    totals[key] += value
        return render_template(
            "bo_pnl.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            kind=kind, title=PNL_TITLES[kind], group_by=group_by, range_key=range_key, start_day=start_day,
            end_day=end_day, rooms=rooms, room_id=room_id, rows=table, totals=dict(totals),
        )

    view.__name__ = endpoint
    return endpoint, view


for _role in ROLES:
    _ep, _view = make_overall(_role)
    app.add_url_rule(f"/{_role}/overall", endpoint=_ep, view_func=_view, methods=["GET", "POST"])
    _ep, _view = make_takelist(_role)
    app.add_url_rule(f"/{_role}/takelist", endpoint=_ep, view_func=_view, methods=["GET"])
    _ep, _view = make_pnl(_role)
    app.add_url_rule(f"/{_role}/pnl/<kind>", endpoint=_ep, view_func=_view, methods=["GET"])


# ----------------------------- ประวัติการเงิน (statement) -----------------------------
from models import WalletTransaction  # noqa: E402

WALLET_LABELS = {"credit": "เครดิต", "commission": "คอมมิชชั่น", "stock": "ถือหุ้น"}


def make_statement(role):
    cfg = ROLES[role]
    endpoint = f"{role}_statement"

    @cfg["decorator"]
    def view():
        owner = cfg["owner"](current_user())
        range_key, start_day, end_day = _bet_history_range(request.args)
        start_utc = datetime.combine(start_day, datetime.min.time()) - timedelta(hours=7)
        end_utc = datetime.combine(end_day + timedelta(days=1), datetime.min.time()) - timedelta(hours=7)
        wallet = request.args.get("wallet") or "credit"
        if wallet not in WALLET_LABELS:
            wallet = "credit"
        rows = WalletTransaction.query.filter(
            WalletTransaction.user_id == owner.id, WalletTransaction.wallet_type == wallet,
            WalletTransaction.created_at >= start_utc, WalletTransaction.created_at < end_utc,
        ).order_by(WalletTransaction.id.desc()).limit(1000).all()
        total_out = sum(-r.change for r in rows if r.change < 0)
        total_in = sum(r.change for r in rows if r.change > 0)
        return render_template(
            "bo_statement.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            rows=rows, wallet=wallet, wallet_labels=WALLET_LABELS, range_key=range_key, start_day=start_day,
            end_day=end_day, total_out=total_out, total_in=total_in, shift=timedelta(hours=7),
        )

    view.__name__ = endpoint
    return endpoint, view


for _role in ROLES:
    _ep, _view = make_statement(_role)
    app.add_url_rule(f"/{_role}/statement", endpoint=_ep, view_func=_view, methods=["GET"])


# ----------------------------- ข้อมูลเพิ่มบนหน้าภาพรวม -----------------------------
from datetime import time as _time  # noqa: E402
from models import LoginHistory  # noqa: E402


def dashboard_extras(role, owner):
    """การ์ดสรุปวันนี้ (ถือหุ้น/ถูกรางวัล/ค่าคอม/รวม), % ถือสู้รายห้อง, ประวัติการเข้าสู่ระบบ — ใช้บนหน้าภาพรวมของ Agent/Senior"""
    start_local = datetime.combine(app_now().date(), _time.min)
    start_utc = start_local - timedelta(hours=7)
    end_utc = start_utc + timedelta(days=1)
    bets = scope_query(role, owner).filter(
        ThaiLotteryBet.created_at >= start_utc, ThaiLotteryBet.created_at < end_utc,
        ThaiLotteryBet.status.in_(["win", "lose"]),
    ).all()
    commission, stock = ledger_maps(role, owner, [b.id for b in bets])
    cards = {
        "stock": sum(stock.values()),
        "win": sum(float(b.reward_amount) for b in bets if b.status == "win"),
        "commission": sum(commission.values()),
    }
    cards["total"] = cards["stock"] + cards["commission"]

    share_model, id_field = (PartnerStockShare, "partner_id") if role == "partner" else (SeniorStockShare, "senior_id")
    rooms = active_lottery_rooms().all()
    shares = {
        row.room_id: row.hold_percent
        for row in share_model.query.filter(getattr(share_model, id_field) == owner.id)
    }
    hold_rows = [{"room": room.name, "group": room_category_name(room) or "อื่นๆ", "percent": shares.get(room.id, 0.0)} for room in rooms]
    logins = LoginHistory.query.filter_by(user_id=owner.id).order_by(LoginHistory.id.desc()).limit(10).all()
    login_rows = [
        {"time": (entry.created_at + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S") if entry.created_at else "-",
         "ip": entry.ip_address or "-", "agent": entry.user_agent or "-"}
        for entry in logins
    ]
    return {"today_cards": cards, "hold_rows": hold_rows, "login_rows": login_rows}


# ----------------------------- รายการที่ถูกรางวัล -----------------------------
from app import adjust_credit, notify_user  # noqa: E402


def make_winners(role):
    cfg = ROLES[role]
    endpoint = f"{role}_winners"

    @cfg["decorator"]
    def view():
        owner = cfg["owner"](current_user())
        rooms, room, periods, period = market_and_period()
        query = scope_query(role, owner).filter(ThaiLotteryBet.status == "win")
        if period:
            query = query.filter(ThaiLotteryBet.period_id == period.id)
        bets = query.order_by(ThaiLotteryBet.id.asc()).limit(1000).all()
        commission, stock = ledger_maps(role, owner, [b.id for b in bets])
        rows = []
        totals = defaultdict(float)
        for bet in bets:
            stake, win = float(bet.amount), float(bet.reward_amount)
            member_net = win - stake + float(bet.discount_amount or 0)
            owner_net = stock.get(bet.id, 0.0) + commission.get(bet.id, 0.0)
            rows.append({
                "bet": bet, "member_net": member_net, "owner_net": owner_net, "company_net": -(member_net + owner_net),
                "time": (bet.created_at + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S"),
            })
            totals["stake"] += stake
            totals["win"] += win
            totals["member"] += member_net
            totals["owner"] += owner_net
            totals["company"] += -(member_net + owner_net)
        return render_template(
            "bo_winners.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            rooms=rooms, room=room, periods=periods, period=period, rows=rows, totals=dict(totals), labels=BET_TYPE_LABELS,
            show_market_bar=True,
        )

    view.__name__ = endpoint
    return endpoint, view


# ----------------------------- ตั้งค่ารับของแยกตามชนิด (ทุกตลาดในตารางเดียว) -----------------------------
def make_acceptance(role):
    cfg = ROLES[role]
    endpoint = f"{role}_acceptance"

    @cfg["decorator"]
    def view():
        owner = cfg["owner"](current_user())
        types = bet_types_list()
        limit_model = PartnerAcceptanceLimit if role == "partner" else SeniorAcceptanceLimit
        owner_field = "partner_id" if role == "partner" else "senior_id"
        rooms = active_lottery_rooms().all()
        by_id = {r.id: r for r in rooms}
        if request.method == "POST":
            selected = {int(x) for x in request.form.getlist("sel") if x.isdigit()} & set(by_id)
            if not selected:
                flash("กรุณาเลือกตลาดที่ต้องการบันทึกอย่างน้อย 1 ตลาด", "error")
                return redirect(url_for(endpoint))
            try:
                for room_id in selected:
                    for bet_type in types:
                        raw = (request.form.get(f"cap_{room_id}_{bet_type}") or "").strip()
                        row = limit_model.query.filter_by(**{owner_field: owner.id, "room_id": room_id, "bet_type": bet_type}).first()
                        if raw == "":
                            if row:
                                db.session.delete(row)
                            continue
                        value = float(raw)
                        if value < 0:
                            raise ValueError("ต้องไม่ติดลบ")
                        if row is None:
                            row = limit_model(**{owner_field: owner.id, "room_id": room_id, "bet_type": bet_type})
                            db.session.add(row)
                        row.amount_limit = value
            except ValueError:
                db.session.rollback()
                flash("ตั้งสู้ต้องเป็นตัวเลขที่ไม่ติดลบ", "error")
                return redirect(url_for(endpoint))
            db.session.commit()
            flash(f"บันทึกตั้งสู้ {len(selected)} ตลาดแล้ว", "success")
            return redirect(url_for(endpoint))
        caps = {(row.room_id, row.bet_type): row.amount_limit for row in limit_model.query.filter(getattr(limit_model, owner_field) == owner.id)}
        grouped = {}
        for room in rooms:
            grouped.setdefault(room_category_name(room) or "อื่นๆ", []).append(room)
        return render_template(
            "bo_acceptance.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            grouped=grouped, types=types, labels=BET_TYPE_LABELS, caps=caps,
        )

    view.__name__ = endpoint
    return endpoint, view


# ----------------------------- เติมเงิน / ถอนกลับ (สมาชิก) -----------------------------
def make_transfers(role):
    cfg = ROLES[role]
    endpoint = f"{role}_transfers"

    @cfg["decorator"]
    def view():
        owner = cfg["owner"](current_user())
        keyword = (request.values.get("q") or "").strip()
        query = scoped_members_for_transfers(role, owner)
        if keyword:
            like = f"%{keyword}%"
            query = query.filter(User.username.ilike(like) | User.full_name.ilike(like))
        members = query.order_by(User.username.asc()).limit(300).all()
        if request.method == "POST":
            allowed = {m.id: m for m in members}
            plan = []
            for member_id, member in allowed.items():
                raw = (request.form.get(f"amt_{member_id}") or "").strip()
                if raw == "":
                    continue
                try:
                    amount = round(float(raw), 2)
                except ValueError:
                    flash(f"จำนวนเงินของ {member.username} ไม่ถูกต้อง", "error")
                    return redirect(url_for(endpoint, q=keyword or None))
                if amount != 0:
                    plan.append((member, amount))
            deposits = sum(a for _, a in plan if a > 0)
            if deposits > owner.credit_balance + 1e-9:
                flash(f"เครดิตของคุณไม่พอ (ต้องใช้ {deposits:,.2f} มี {owner.credit_balance:,.2f})", "error")
                return redirect(url_for(endpoint, q=keyword or None))
            for member, amount in plan:
                if amount < 0 and -amount > member.credit_balance + 1e-9:
                    flash(f"{member.username} มีเครดิตไม่พอให้ถอน ({member.credit_balance:,.2f})", "error")
                    return redirect(url_for(endpoint, q=keyword or None))
            for member, amount in plan:
                if amount > 0:
                    adjust_credit(owner, -amount, f"โอนเครดิตให้ {member.username}")
                    adjust_credit(member, amount, f"ได้รับเครดิตจาก {owner.username}")
                    notify_user(member, "ได้รับเครดิต", f"เครดิตเพิ่ม {amount:,.2f}", "wallet")
                else:
                    adjust_credit(member, amount, f"ถอนเครดิตกลับโดย {owner.username}")
                    adjust_credit(owner, -amount, f"รับเครดิตคืนจาก {member.username}")
                    notify_user(member, "ถูกถอนเครดิต", f"ถอนเครดิต {-amount:,.2f}", "wallet")
            db.session.commit()
            flash(f"ทำรายการ {len(plan)} รายการเรียบร้อย" if plan else "ไม่มีรายการที่กรอกจำนวนเงิน", "success" if plan else "warning")
            return redirect(url_for(endpoint, q=keyword or None))
        return render_template(
            "bo_transfers.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            members=members, keyword=keyword,
        )

    view.__name__ = endpoint
    return endpoint, view


def scoped_members_for_transfers(role, owner):
    if role == "partner":
        return User.query.filter_by(partner_id=owner.id, role="member")
    return User.query.filter(User.partner_id.in_(senior_agent_ids(owner) or [-1]), User.role == "member")


for _role in ROLES:
    _ep, _view = make_winners(_role)
    app.add_url_rule(f"/{_role}/winners", endpoint=_ep, view_func=_view, methods=["GET"])
    _ep, _view = make_acceptance(_role)
    app.add_url_rule(f"/{_role}/acceptance", endpoint=_ep, view_func=_view, methods=["GET", "POST"])
    _ep, _view = make_transfers(_role)
    app.add_url_rule(f"/{_role}/transfers", endpoint=_ep, view_func=_view, methods=["GET", "POST"])


# ----------------------------- ยกเลิกรายการแทง (โดยผู้ดูแล) -----------------------------
from app import cancel_ticket_bets  # noqa: E402
from models import Announcement  # noqa: E402


def make_cancel(role):
    cfg = ROLES[role]
    endpoint = f"{role}_cancel_bets"

    @cfg["decorator"]
    def view(ticket_code=None):
        owner = cfg["owner"](current_user())
        now = app_now()
        base = scope_query(role, owner).join(ThaiLotteryPeriod, ThaiLotteryBet.period_id == ThaiLotteryPeriod.id).filter(
            ThaiLotteryBet.status == "pending", ThaiLotteryPeriod.is_open.is_(True), ThaiLotteryPeriod.close_time > now,
            ThaiLotteryBet.ticket_code.isnot(None),
        )
        if request.method == "POST":
            bets = base.filter(ThaiLotteryBet.ticket_code == ticket_code).all()
            if not bets:
                flash("ไม่พบโพยที่ยกเลิกได้ (อาจปิดรับแล้ว ถูกยกเลิกแล้ว หรือไม่ใช่สมาชิกในสายของคุณ)", "error")
                return redirect(url_for(endpoint))
            member = bets[0].user
            refund = cancel_ticket_bets(bets, f"ยกเลิกโดย {owner.username}")
            adjust_credit(member, refund, f"ยกเลิกโพย {ticket_code} โดย {owner.username}")
            notify_user(member, "โพยถูกยกเลิก", f"โพย {ticket_code} ถูกยกเลิก คืนเครดิต {refund:,.2f}", "wallet")
            db.session.commit()
            flash(f"ยกเลิกโพย {ticket_code} ของ {member.username} คืนเครดิต {refund:,.2f} แล้ว", "success")
            return redirect(url_for(endpoint))
        tickets = {}
        for bet in base.order_by(ThaiLotteryBet.id.desc()).limit(3000).all():
            row = tickets.setdefault(bet.ticket_code, {
                "code": bet.ticket_code, "member": bet.user, "period": bet.period, "count": 0, "stake": 0.0, "paid": 0.0,
                "time": (bet.created_at + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M"), "remark": bet.remark or "",
            })
            row["count"] += 1
            row["stake"] += float(bet.amount)
            row["paid"] += float(bet.amount) - float(bet.discount_amount or 0)
        return render_template(
            "bo_cancel.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            tickets=list(tickets.values())[:200],
        )

    view.__name__ = endpoint
    return endpoint, view


# ----------------------------- หน้าร้าน: ประกาศถึงสมาชิก -----------------------------
def make_storefront(role):
    cfg = ROLES[role]
    endpoint = f"{role}_storefront"

    @cfg["decorator"]
    def view():
        owner = cfg["owner"](current_user())
        if request.method == "POST":
            action = request.form.get("action")
            if action == "create":
                title = (request.form.get("title") or "").strip()
                body = (request.form.get("body") or "").strip()
                if not title:
                    flash("กรุณากรอกหัวข้อประกาศ", "error")
                else:
                    db.session.add(Announcement(title=title[:150], body=body[:2000], is_active=True, owner_id=owner.id))
                    db.session.commit()
                    flash("เพิ่มประกาศถึงสมาชิกแล้ว", "success")
            else:
                item = Announcement.query.filter_by(id=request.form.get("id", type=int), owner_id=owner.id).first()
                if item is None:
                    flash("ไม่พบประกาศ", "error")
                elif action == "toggle":
                    item.is_active = not item.is_active
                    db.session.commit()
                    flash("อัปเดตสถานะประกาศแล้ว", "success")
                elif action == "delete":
                    db.session.delete(item)
                    db.session.commit()
                    flash("ลบประกาศแล้ว", "success")
            return redirect(url_for(endpoint))
        items = Announcement.query.filter_by(owner_id=owner.id).order_by(Announcement.created_at.desc()).limit(100).all()
        return render_template(
            "bo_storefront.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            items=items, shift=timedelta(hours=7),
        )

    view.__name__ = endpoint
    return endpoint, view


# ----------------------------- รอผลเดิมพัน (สรุป 3 ฝ่าย) -----------------------------
def make_pending(role):
    cfg = ROLES[role]
    endpoint = f"{role}_pending_summary"

    @cfg["decorator"]
    def view():
        owner = cfg["owner"](current_user())
        rooms_by_id = {r.id: r for r in LotteryRoom.query.all()}
        bets = scope_query(role, owner).filter(ThaiLotteryBet.status == "pending").all()
        commission, _ = ledger_maps(role, owner, [b.id for b in bets])
        cache = {}
        by_room, by_type = defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(float))
        for bet in bets:
            room = rooms_by_id.get(bet.period.room_id) if bet.period else None
            percent = effective_hold_percent(role, owner, bet.user, room, cache) if room else 0.0
            stake, payout = float(bet.amount), float(bet.amount) * float(bet.rate)
            for bucket in (by_room[room.name if room else "-"], by_type[BET_TYPE_LABELS.get(bet.bet_type, bet.bet_type)]):
                bucket["count"] += 1
                bucket["stake"] += stake
                bucket["discount"] += float(bet.discount_amount or 0)
                bucket["comm"] += commission.get(bet.id, 0.0)
                bucket["held"] += stake * percent / 100
                bucket["risk"] += payout * percent / 100
        def finish(table):
            rows = [{"name": name, **dict(values)} for name, values in table.items()]
            rows.sort(key=lambda r: -r["stake"])
            total = defaultdict(float)
            for row in rows:
                for key, value in row.items():
                    if key != "name":
                        total[key] += value
            return rows, dict(total)
        room_rows, room_total = finish(by_room)
        type_rows, type_total = finish(by_type)
        return render_template(
            "bo_pending.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            room_rows=room_rows, room_total=room_total, type_rows=type_rows, type_total=type_total,
        )

    view.__name__ = endpoint
    return endpoint, view


for _role in ROLES:
    _ep, _view = make_cancel(_role)
    app.add_url_rule(f"/{_role}/cancel-bets", endpoint=_ep, view_func=_view, methods=["GET"])
    app.add_url_rule(f"/{_role}/cancel-bets/<string:ticket_code>", endpoint=_ep + "_do", view_func=_view, methods=["POST"])
    _ep, _view = make_storefront(_role)
    app.add_url_rule(f"/{_role}/storefront", endpoint=_ep, view_func=_view, methods=["GET", "POST"])
    _ep, _view = make_pending(_role)
    app.add_url_rule(f"/{_role}/pending-summary", endpoint=_ep, view_func=_view, methods=["GET"])
