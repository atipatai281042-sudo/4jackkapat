"""หน้าแทงหวยของสมาชิก: เลขปิดรับ/เลขอั้น · โพยใหญ่ต้องเร็ว · แจ้งเตือนต่อโพย · วงเงิน · หน้าผลหวย · เมนู"""
from datetime import timedelta

import pytest
from sqlalchemy import event

import app as app_module
from app import (
    BlockedNumber, LotteryCategory, LotteryRateSet, LotteryRoom, Notification, PartnerBlockedNumber, PartnerProfile,
    SeniorBlockedNumber, SeniorProfile, ThaiLotteryBet, ThaiLotteryPeriod, User, WalletTransaction,
    add_thai_lottery_bets, app, app_now, db,
)
from models import PartnerMemberLimit, ResponsiblePlayProfile

JSON = {"Accept": "application/json"}


def make_user(username, role="member", **extra):
    user = User(username=username, full_name=username, role=role, points=0, credit_balance=extra.pop("credit", 0.0), **extra)
    user.set_password("pass1234")
    db.session.add(user)
    db.session.flush()
    return user


@pytest.fixture
def world():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite://")
    with app.app_context():
        db.drop_all()
        db.create_all()
        senior = make_user("senior1", "senior")
        db.session.add(SeniorProfile(user_id=senior.id, invite_code="S1", commission_rate=0.5))
        agent = make_user("agent1", "partner", senior_id=senior.id)
        db.session.add(PartnerProfile(user_id=agent.id, invite_code="A1", commission_rate=2.0))
        member = make_user("member1", partner_id=agent.id, credit=100000)
        loner = make_user("loner1", credit=100000)
        cat = LotteryCategory(name="หวยหุ้น")
        db.session.add(cat)
        db.session.flush()
        room = LotteryRoom(name="ดาวโจนส์", category="หวยหุ้น", category_id=cat.id, link_url="/x", is_active=True)
        db.session.add(room)
        db.session.flush()
        now = app_now()
        period = ThaiLotteryPeriod(room_id=room.id, period_date="d1", open_time=now - timedelta(hours=1),
                                   close_time=now + timedelta(hours=1), is_open=True)
        db.session.add(period)
        db.session.commit()
        ids = {"senior": senior.id, "agent": agent.id, "member": member.id, "loner": loner.id, "room": room.id, "period": period.id}
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = ids["member"]
    yield client, ids


def place(ids, who, entries, tier=1):
    user = db.session.get(User, ids[who])
    period = db.session.get(ThaiLotteryPeriod, ids["period"])
    return add_thai_lottery_bets(user, period, entries, rate_tier=tier)


def entry(number="12", amount=10, bet_type="2up"):
    return {"bet_type": bet_type, "number": number, "amount": amount}


# ------------------------------------------------------------------ เลขปิดรับ / เลขอั้น
def test_closed_number_from_admin_agent_or_senior_cannot_be_bet(world):
    client, ids = world
    with app.app_context():
        db.session.add(BlockedNumber(room_id=ids["room"], bet_type="2up", number="11", payout_multiplier=0))
        db.session.add(PartnerBlockedNumber(partner_id=ids["agent"], room_id=ids["room"], bet_type="2up", number="22", payout_multiplier=0))
        db.session.add(SeniorBlockedNumber(senior_id=ids["senior"], room_id=ids["room"], bet_type="3up", number="333", payout_multiplier=0))
        db.session.commit()
        for number, bet_type in (("11", "2up"), ("22", "2up"), ("333", "3up")):
            with pytest.raises(ValueError, match="ปิดรับ"):
                place(ids, "member", [entry("44"), entry(number, bet_type=bet_type)])
        assert ThaiLotteryBet.query.count() == 0  # โพยที่มีเลขปิดรับ ต้องไม่ถูกบันทึกเลยทั้งใบ (ไม่หักเครดิตครึ่งๆ กลางๆ)
        assert db.session.get(User, ids["member"]).credit_balance == 100000
        # สมาชิกที่ไม่อยู่ในสาย Agent ไม่โดนเลขปิดของ Agent/Senior แต่โดนของระบบ
        assert place(ids, "loner", [entry("22")]) == 1
        with pytest.raises(ValueError, match="ปิดรับ"):
            place(ids, "loner", [entry("11")])


def test_half_pay_number_never_pays_more_than_the_chosen_rate_set(world):
    client, ids = world
    with app.app_context():
        db.session.add(BlockedNumber(room_id=ids["room"], bet_type="2up", number="55", payout_multiplier=60))
        db.session.add(LotteryRateSet(category="หวยหุ้น", tier=2, bet_type="2up", payout_multiplier=45, discount_pct=0))
        db.session.commit()
        place(ids, "loner", [entry("55", 10)], tier=1)
        place(ids, "loner", [entry("55", 10)], tier=2)
        rates = [b.rate for b in ThaiLotteryBet.query.filter_by(user_id=ids["loner"]).order_by(ThaiLotteryBet.id)]
        assert rates == [60.0, 45.0]  # ชุดที่ 2 จ่าย 45 อยู่แล้ว เลขอั้น 60 ต้องไม่ทำให้จ่ายสูงขึ้น


def test_admin_agent_and_senior_forms_can_close_a_number(world):
    client, ids = world
    with app.app_context():
        admin = make_user("admin", "admin")
        db.session.commit()
        admin_id = admin.id
    client.environ_base["HTTP_X_BACKOFFICE_INTERNAL"] = app.config["SECRET_KEY"]
    with client.session_transaction() as session:
        session["user_id"] = admin_id
    client.post(f"/admin/lottery-rooms/{ids['room']}/blocked-numbers", data={"bet_type": "2up", "number": "77", "close_number": "1"})
    client.post(f"/admin/lottery-rooms/{ids['room']}/blocked-numbers", data={"bet_type": "2up", "number": "78", "payout_multiplier": "45"})
    client.post(f"/admin/lottery-rooms/{ids['room']}/blocked-numbers", data={"bet_type": "2up", "number": "79", "payout_multiplier": "95"})  # สูงกว่าปกติ ต้องไม่ผ่าน
    client.post(f"/admin/lottery-rooms/{ids['room']}/blocked-numbers", data={"bet_type": "2up", "number": "9", "close_number": "1"})  # จำนวนหลักผิด
    with app.app_context():
        rows = {(r.number): r.payout_multiplier for r in BlockedNumber.query.all()}
        assert rows == {"77": 0, "78": 45}
    with client.session_transaction() as session:
        session["user_id"] = ids["agent"]
    client.post("/partner/blocked", data={"room_id": ids["room"], "bet_type": "2down", "number": "88", "close_number": "1"})
    client.post("/partner/blocked", data={"room_id": ids["room"], "bet_type": "2down", "number": "89"})  # ไม่กรอกอัตราและไม่ปิด → ไม่บันทึก
    with client.session_transaction() as session:
        session["user_id"] = ids["senior"]
    client.post("/senior/blocked", data={"room_id": ids["room"], "bet_type": "3up", "number": "123", "close_number": "1"})
    with app.app_context():
        assert PartnerBlockedNumber.query.count() == 1 and PartnerBlockedNumber.query.first().payout_multiplier == 0
        assert SeniorBlockedNumber.query.count() == 1 and SeniorBlockedNumber.query.first().payout_multiplier == 0
        with pytest.raises(ValueError, match="ปิดรับ"):
            place(ids, "member", [entry("88", bet_type="2down")])


def test_bet_page_lists_closed_and_half_pay_numbers_from_every_level(world):
    client, ids = world
    with app.app_context():
        db.session.add(BlockedNumber(room_id=ids["room"], bet_type="2up", number="11", payout_multiplier=45))
        db.session.add(PartnerBlockedNumber(partner_id=ids["agent"], room_id=ids["room"], bet_type="2up", number="22", payout_multiplier=0))
        db.session.add(SeniorBlockedNumber(senior_id=ids["senior"], room_id=ids["room"], bet_type="3up", number="333", payout_multiplier=0))
        db.session.commit()
    page = client.get(f"/lottery/thai?room_id={ids['room']}").get_data(as_text=True)
    assert "const CLOSED = " in page and '"2up": ["22"]' in page and '"3up": ["333"]' in page
    table = page.split("<th>เลขปิดรับ</th>")[1].split("</table>")[0]
    assert ">22<" in table and ">333<" in table and ">11<" in table.split("</td>")[1] + table.split("</td>")[2] or "11" in table


# ------------------------------------------------------------------ โพยใหญ่ต้องเร็ว / บันทึกต่อโพย
def test_big_slip_is_one_notification_one_credit_entry_and_few_queries(world):
    client, ids = world
    entries = [entry(f"{i:02d}", 10, t) for i in range(100) for t in ("2up", "2down")]
    counter = {"n": 0}

    def count(*args):
        counter["n"] += 1

    with app.app_context():
        event.listen(db.engine, "before_cursor_execute", count)
        try:
            created = place(ids, "member", entries)
        finally:
            event.remove(db.engine, "before_cursor_execute", count)
        assert created == 200
        assert counter["n"] < 200 * 8, counter["n"]  # เดิมประมาณ 17 คำสั่งต่อรายการ
        assert Notification.query.filter_by(user_id=ids["member"]).count() == 1
        credit_rows = WalletTransaction.query.filter_by(user_id=ids["member"], wallet_type="credit").all()
        assert len(credit_rows) == 1 and credit_rows[0].change == pytest.approx(-2000)
        assert db.session.get(User, ids["member"]).credit_balance == pytest.approx(98000)
        assert len({b.ticket_code for b in ThaiLotteryBet.query.all()}) == 1
        assert db.session.get(User, ids["agent"]).partner_profile.commission_balance == pytest.approx(40.0)  # 2% ของ 2,000


def test_any_bad_entry_rejects_the_whole_slip_without_side_effects(world):
    client, ids = world
    with app.app_context():
        with pytest.raises(ValueError):
            place(ids, "member", [entry("12"), entry("34"), entry("5x")])
        db.session.rollback()
        assert ThaiLotteryBet.query.count() == 0 and Notification.query.count() == 0
        assert db.session.get(User, ids["member"]).credit_balance == 100000
        assert db.session.get(User, ids["agent"]).partner_profile.commission_balance == 0


def test_zero_rate_type_is_not_bettable(world):
    client, ids = world
    with app.app_context():
        db.session.add(LotteryRateSet(category="หวยหุ้น", tier=2, bet_type="2up", payout_multiplier=0, discount_pct=0))
        from models import LotteryPayoutRule
        db.session.add(LotteryPayoutRule(bet_type="rundown", payout_multiplier=0, is_active=True))
        db.session.commit()
        with pytest.raises(ValueError, match="ยังไม่เปิดรับแทง"):
            place(ids, "member", [entry("1", 10, "rundown")])


# ------------------------------------------------------------------ วงเงิน
def test_legacy_per_number_limit_counts_split_entries(world):
    client, ids = world
    with app.app_context():
        db.session.add(PartnerMemberLimit(partner_id=ids["agent"], member_id=ids["member"], min_bet=1, max_bet=1000, max_number_bet=100))
        db.session.commit()
        with pytest.raises(ValueError, match="ต่อเลข"):
            place(ids, "member", [entry("12", 60), entry("12", 60)])  # แบ่งเลขเดิมเป็น 2 รายการ รวมเกิน 100
        assert place(ids, "member", [entry("12", 60), entry("13", 60)]) == 2
        with pytest.raises(ValueError, match="ต่อเลข"):
            place(ids, "member", [entry("12", 50)])  # รวมกับที่เคยแทงไว้ 60 → 110


def test_daily_limit_ignores_cancelled_bets_and_uses_thai_midnight(world):
    client, ids = world
    with app.app_context():
        member = db.session.get(User, ids["member"])
        db.session.add(ResponsiblePlayProfile(user_id=member.id, daily_bet_limit=100))
        db.session.commit()
        assert place(ids, "member", [entry("12", 100)]) == 1
        with pytest.raises(ValueError, match="วงเงินเดิมพันต่อวัน"):
            place(ids, "member", [entry("13", 10)])
        for bet in ThaiLotteryBet.query.all():
            bet.status = "cancelled"
        db.session.commit()
        assert place(ids, "member", [entry("13", 100)]) == 1  # ยกเลิกแล้วต้องไม่นับ


# ------------------------------------------------------------------ หน้าเว็บ
def test_results_pages_show_the_latest_period_that_has_a_result(world):
    client, ids = world
    with app.app_context():
        now = app_now()
        db.session.add(ThaiLotteryPeriod(room_id=ids["room"], period_date="2026-01-01", open_time=now - timedelta(days=2),
                                         close_time=now - timedelta(days=1), is_open=False, result_3up="123", result_2down="45",
                                         is_checked=True))
        db.session.add(ThaiLotteryPeriod(room_id=ids["room"], period_date="2099-01-01", open_time=now, close_time=now + timedelta(days=1), is_open=True))
        db.session.commit()
    for url in ("/results", "/"):
        page = app.test_client().get(url).get_data(as_text=True)
        assert "2026-01-01" in page and "123" in page and "45" in page, url
    detail = client.get(f"/results/{ids['room']}").get_data(as_text=True)
    assert "2026-01-01" in detail and "2099-01-01" not in detail and "d1" not in detail


def test_ticket_pages_are_thai_and_cancelled_tickets_are_marked(world):
    client, ids = world
    with app.app_context():
        place(ids, "member", [entry("12", 50), entry("34", 25, "3up") if False else entry("340", 25, "3up")])
        code = ThaiLotteryBet.query.first().ticket_code
    page = client.get(f"/lottery/ticket/{ids['period']}/{code}").get_data(as_text=True)
    assert "2 ตัวบน" in page and "3 ตัวบน" in page and "รอผล" in page and ">2up<" not in page and ">pending<" not in page
    client.post(f"/lottery/ticket/{ids['period']}/{code}/cancel")
    page = client.get(f"/lottery/ticket/{ids['period']}/{code}").get_data(as_text=True)
    assert "ยกเลิกแล้ว" in page and "ยอดเดิมพันรวม" in page and "0.00" in page.split("ยอดเดิมพันรวม")[1][:120]
    bet_page = client.get(f"/lottery/thai?room_id={ids['room']}").get_data(as_text=True)
    assert "ยกเลิกโพยแล้ว" in bet_page and ">0 รายการ<" not in bet_page


def test_navigation_is_deduplicated_and_reaches_rules(world):
    client, ids = world
    page = client.get("/lottery/rooms").get_data(as_text=True)
    assert "แดชบอร์ด" not in page and "บัญชีของฉัน" in page
    assert page.count("/rules") >= 2 and "/change-password" in page and "/responsible-play" in page  # เมนู + ท้ายเว็บ
    assert "หน้าแรก" not in page.split('id="main-navigation"')[1].split("</nav>")[0]
    assert client.get("/dashboard").status_code == 302 and client.get("/dashboard").headers["Location"].endswith("/history")
    guest = app.test_client().get("/results").get_data(as_text=True)
    assert "หน้าแรก" in guest.split('id="main-navigation"')[1].split("</nav>")[0]
    bet_page = client.get(f"/lottery/thai?room_id={ids['room']}").get_data(as_text=True)
    assert "/wallet" not in bet_page and "ระดับ: <b>Member</b>" not in bet_page
    assert "ล้างช่องกรอก" in bet_page and "ล้างบิลทั้งหมด" in bet_page and 'id="bp-now"' not in bet_page
    assert "user-scalable=no" not in bet_page


def test_senior_badge_is_not_member(world):
    client, ids = world
    with client.session_transaction() as session:
        session["user_id"] = ids["senior"]
    page = client.get("/results").get_data(as_text=True)
    assert "SENIOR" in page and "MEMBER" not in page.split("role-badge")[1][:80]


def test_old_submit_endpoint_and_dead_templates_are_gone(world):
    client, ids = world
    assert client.post(f"/lotto/room/{ids['room']}/period/{ids['period']}/submit", data={"bet_type": "2up", "number": "12", "amount": "10"}).status_code == 404
    import os
    assert not os.path.exists(os.path.join(os.path.dirname(app_module.__file__), "templates", "lottery_thai_old.html"))


# ------------------------------------------------------------------ API sync ไม่ทำให้ทุกหน้าค้าง
def test_api_sync_runs_at_most_once_per_interval_and_only_on_page_views(world, monkeypatch):
    client, ids = world
    calls = []
    monkeypatch.setattr(app_module, "sync_lottery_api_results", lambda *a, **k: calls.append(1))
    monkeypatch.setitem(app_module._api_sync_state, "next_at", 0.0)
    app.config["TESTING"] = False
    try:
        app.testing = False
        for _ in range(5):
            client.get("/results")
        assert len(calls) == 1
        monkeypatch.setitem(app_module._api_sync_state, "next_at", 0.0)
        client.post("/register", data={"username": "x"})  # ไม่ใช่ GET → ไม่ซิงก์
        assert len(calls) == 1
        monkeypatch.setitem(app_module._api_sync_state, "next_at", 0.0)
        monkeypatch.setattr(app_module, "sync_lottery_api_results", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")))
        client.get("/results")
        assert app_module._api_sync_state["next_at"] > 0  # ล้มเหลว → เว้นช่วงก่อนลองใหม่
    finally:
        app.config["TESTING"] = True
        app.testing = True
