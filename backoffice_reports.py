"""รายงานและหน้าดูของของ Agent/Senior (โค้ดชุดเดียวกัน) ตามหลังบ้านของเว็บตัวอย่าง

  /<role>/overall         ดูของรวม/คาดคะเนได้เสีย: ซื้อ/คอม/รับ/จ่ายสูงสุด ต่อประเภท + รายเลข + ตั้งสู้
  /<role>/takelist        รายการเก็บของตามสมาชิก (ต่อตลาด/งวด)
  /<role>/pnl/<kind>      แพ้-ชนะ แยกตามสมาชิก / ประเภท / วัน — แบ่ง 3 ฝ่าย สมาชิก · คุณ · บริษัท

ตัวเลขทุกตัวคำนวณจากรายการแทงและสมุดบัญชีที่ระบบบันทึกไว้จริง (ThaiLotteryBet + Commission/StockLedger)
ไฟล์นี้ถูก import ท้าย app.py"""
from collections import defaultdict
from datetime import datetime, timedelta

from flask import abort, flash, redirect, render_template, request, url_for

from app import (
    BET_TYPE_LABELS, EXTRA_BET_TYPES, RATE_TABLE_ORDER, _bet_history_range, app, active_lottery_rooms,
    app_now, current_user, db, get_lottery_rates, normalize_bet_type, partner_owner, partner_required,
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
    room_id = request.values.get("room_id", type=int)
    room = next((r for r in rooms if r.id == room_id), None)
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
    period = next((p for p in periods if p.id == period_id), periods[0] if periods else None)
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
        for bet in bets:
            percent = effective_hold_percent(role, owner, bet.user, room, cache)
            stake, payout = float(bet.amount), float(bet.amount) * float(bet.rate)
            totals["buy"][bet.bet_type] += stake
            totals["comm"][bet.bet_type] += commission.get(bet.id, 0.0)
            totals["held"][bet.bet_type] += stake * percent / 100
            totals["max"][bet.bet_type] += payout * percent / 100
            entry = numbers[(bet.bet_type, bet.number)]
            entry["stake"] += stake
            entry["max"] += payout
            entry["members"].add(bet.user_id)
        caps = {
            row.bet_type: row.amount_limit for row in limit_model.query.filter_by(**{owner_field: owner.id, "room_id": room.id})
        } if room else {}

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
            totals=totals, summary=summary, caps=caps, rows=rows[:300], view_filter=view_filter, sort=sort,
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
            for bet in scope_query(role, owner).filter(ThaiLotteryBet.period_id == period.id).all():
                members[bet.user_id] = bet.user
                percent = effective_hold_percent(role, owner, bet.user, room, cache)
                row = table[bet.user_id]
                row["stake"][bet.bet_type] += float(bet.amount)
                row["held"][bet.bet_type] += float(bet.amount) * percent / 100
                row["count"] += 1
        rows = sorted(
            ({"member": members[mid], **data, "total": sum(data["stake"].values()), "held_total": sum(data["held"].values())}
             for mid, data in table.items()),
            key=lambda r: -r["total"],
        )
        return render_template(
            "bo_takelist.html", shell=cfg["shell"], role=role, endpoint=endpoint, partner=owner, senior=owner,
            rooms=rooms, room=room, periods=periods, period=period, types=types, labels=BET_TYPE_LABELS, rows=rows,
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
