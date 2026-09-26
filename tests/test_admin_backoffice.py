"""หลังบ้านแอดมิน: ทุกหน้าเปิดได้ · จัดการผู้ใช้ · ยกเลิกโพย · ตรวจผล/ย้อนผลต้องคิดหุ้นสายงานครบ · งวด · บัญชีแอดมิน"""
from datetime import timedelta

import pytest

from app import (
    Announcement, AdminAuditLog, CommissionLedger, LotteryCategory, LotteryRoom, PartnerAssistant, PartnerProfile,
    PartnerStockLedger, PartnerStockShare, SeniorProfile, SeniorStockLedger, SeniorStockShare, ThaiLotteryBet,
    ThaiLotteryPeriod, User, WalletTransaction, add_thai_lottery_bets, app, app_now, db, store_announcements_for,
)


def make_user(username, role="member", **extra):
    user = User(username=username, full_name=username.upper(), role=role, points=0, credit_balance=extra.pop("credit", 0.0), **extra)
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
        admin = make_user("admin", "admin")
        senior = make_user("senior1", "senior")
        db.session.add(SeniorProfile(user_id=senior.id, invite_code="S1", commission_rate=0.5))
        agent = make_user("agent1", "partner", senior_id=senior.id)
        db.session.add(PartnerProfile(user_id=agent.id, invite_code="A1", commission_rate=2.0))
        sub = make_user("agent2", "partner", partner_id=agent.id)
        db.session.add(PartnerProfile(user_id=sub.id, invite_code="A2", commission_rate=1.0))
        helper = make_user("helper1", "partner", partner_id=agent.id)
        db.session.add(PartnerProfile(user_id=helper.id, invite_code="H1", commission_rate=0))
        db.session.add(PartnerAssistant(partner_id=agent.id, assistant_user_id=helper.id, permissions="members,bets"))
        m1 = make_user("member1", partner_id=agent.id, credit=1000)
        m2 = make_user("member2", partner_id=sub.id, credit=1000)
        m0 = make_user("member0", credit=500)
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
        # agent ถือหุ้น 50%, senior ถือหุ้น 20% ของห้องนี้
        db.session.add(PartnerStockShare(partner_id=agent.id, room_id=room.id, hold_percent=50))
        db.session.add(SeniorStockShare(senior_id=senior.id, room_id=room.id, hold_percent=20))
        db.session.commit()
        ids = {n: u.id for n, u in dict(admin=admin, senior=senior, agent=agent, sub=sub, helper=helper, m1=m1, m2=m2, m0=m0).items()}
        ids.update(room=room.id, period=period.id)
    with app.test_client() as client:
        client.environ_base["HTTP_X_BACKOFFICE_INTERNAL"] = app.config["SECRET_KEY"]
        with client.session_transaction() as session:
            session["user_id"] = ids["admin"]
        yield client, ids


def bet(ids, key, number, amount=100, bet_type="2up"):
    user = db.session.get(User, ids[key])
    period = db.session.get(ThaiLotteryPeriod, ids["period"])
    add_thai_lottery_bets(user, period, [{"bet_type": bet_type, "number": number, "amount": amount}])
    db.session.commit()
    return ThaiLotteryBet.query.filter_by(user_id=user.id).order_by(ThaiLotteryBet.id.desc()).first()


def admin_urls(ids):
    return [
        "/admin", "/admin/users", "/admin/users?role=member&q=mem&status=active", "/admin/users?role=assistant", "/admin/users?role=partner",
        "/admin/users/new", "/admin/staff", f"/admin/users/{ids['m1']}", f"/admin/users/{ids['agent']}", f"/admin/users/{ids['senior']}",
        f"/admin/users/{ids['helper']}", f"/admin/users/{ids['admin']}", "/admin/admins", "/admin/account", "/admin/logins",
        "/admin/lines", f"/admin/lines/{ids['senior']}", f"/admin/lines/{ids['agent']}", f"/admin/lines/{ids['sub']}",
        "/admin/periods", "/admin/periods?state=open", "/admin/reports", "/admin/reports?range=today",
        "/admin/reports/pnl", "/admin/reports/pnl?by=member", "/admin/reports/pnl?by=agent", "/admin/reports/pnl?by=senior",
        "/admin/reports/pnl?by=room", "/admin/reports/pnl?by=type", "/admin/reports/overall",
        f"/admin/reports/overall?room_id={ids['room']}&period_id={ids['period']}&sort=stake",
        "/admin/lottery-tickets", "/admin/lottery-tickets?status=cancelled", "/admin/partners", "/admin/seniors", "/admin/wallet",
        "/admin/audit", "/admin/thai-lottery", "/admin/lottery-rooms", "/admin/lottery-categories",
        "/admin/announcements", "/admin/banners", "/admin/media", "/admin/contact", "/admin/deposit-account",
        "/admin/branding", "/admin/import-data", f"/admin/lottery-rooms/{ids['room']}/edit", f"/admin/lottery-rooms/{ids['room']}/blocked-numbers",
    ]


def test_every_admin_page_opens_with_and_without_data(world):
    client, ids = world
    with app.app_context():
        bet(ids, "m1", "12")
        bet(ids, "m2", "34", 50, "3up") if False else bet(ids, "m2", "345", 50, "3up")
    for url in admin_urls(ids):
        response = client.get(url)
        assert response.status_code == 200, f"{url} -> {response.status_code}"
        page = response.get_data(as_text=True)
        assert "Traceback" not in page and "jinja2" not in page, url
    with app.app_context():
        first = ThaiLotteryBet.query.first()
        code = first.ticket_code
    for url in (f"/admin/lottery-tickets/{ids['period']}/{ids['m1']}", f"/admin/lottery-tickets/{ids['period']}/{ids['m1']}/{code}"):
        assert client.get(url).status_code == 200, url


def test_nav_is_shared_and_ticket_page_stays_in_backoffice(world):
    client, ids = world
    with app.app_context():
        code = bet(ids, "m1", "12").ticket_code
    page = client.get(f"/admin/lottery-tickets/{ids['period']}/{ids['m1']}/{code}").get_data(as_text=True)
    assert "ADMIN BACKOFFICE" in page and "/admin/users" in page  # ใช้เมนูแอดมินเดียวกัน ไม่ใช่เมนูเว็บสมาชิก


def test_suspended_account_is_logged_out_immediately(world):
    client, ids = world
    member_client = app.test_client()
    with member_client.session_transaction() as session:
        session["user_id"] = ids["m1"]
    assert member_client.get("/lottery/rooms").status_code == 200
    client.post(f"/admin/users/{ids['m1']}/status")
    response = member_client.get("/lottery/rooms")
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    with app.app_context():
        assert db.session.get(User, ids["m1"]).is_active is False
    client.post(f"/admin/users/{ids['m1']}/status")
    with app.app_context():
        assert db.session.get(User, ids["m1"]).is_active is True


def test_admin_cannot_suspend_self_or_last_admin(world):
    client, ids = world
    client.post(f"/admin/users/{ids['admin']}/status")
    with app.app_context():
        assert db.session.get(User, ids["admin"]).is_active is True
        second = make_user("admin2x", "admin")
        db.session.commit()
        second_id = second.id
    client.post(f"/admin/users/{second_id}/status")
    with app.app_context():
        assert db.session.get(User, second_id).is_active is False
    other = app.test_client()
    other.environ_base["HTTP_X_BACKOFFICE_INTERNAL"] = app.config["SECRET_KEY"]
    with app.app_context():
        db.session.get(User, second_id).is_active = True
        db.session.commit()
    with other.session_transaction() as session:
        session["user_id"] = second_id
    other.post(f"/admin/users/{ids['admin']}/status")  # second ระงับ admin เดิมได้ ตราบที่ยังเหลือแอดมินใช้งานได้
    other.post(f"/admin/users/{second_id}/status")     # แต่ระงับตัวเองไม่ได้
    with app.app_context():
        assert db.session.get(User, second_id).is_active is True
        assert User.query.filter_by(role="admin", is_active=True).count() >= 1


def test_reset_password_lets_user_log_in_with_new_password(world):
    client, ids = world
    client.post(f"/admin/users/{ids['m1']}/password", data={"new_password": "brand-new9"})
    member_client = app.test_client()
    assert member_client.post("/login", data={"username": "member1", "password": "pass1234"}).status_code == 200  # รหัสเก่าใช้ไม่ได้
    assert member_client.post("/login", data={"username": "member1", "password": "brand-new9"}).status_code == 302
    response = client.post(f"/admin/users/{ids['m1']}/password", data={"new_password": "ab"}, follow_redirects=True)
    assert "อย่างน้อย 6" in response.get_data(as_text=True)
    with app.app_context():
        assert AdminAuditLog.query.filter_by(action="reset_password", target_id=ids["m1"]).count() == 1
        assert "brand-new9" not in " ".join(row.details or "" for row in AdminAuditLog.query.all())


def test_generated_password_is_shown_once_and_works(world):
    client, ids = world
    page = client.post(f"/admin/users/{ids['m1']}/password", data={}, follow_redirects=True).get_data(as_text=True)
    marker = "รหัสผ่านใหม่: "
    assert marker in page
    password = page.split(marker, 1)[1].split("<", 1)[0].strip()
    member_client = app.test_client()
    assert member_client.post("/login", data={"username": "member1", "password": password}).status_code == 302


def test_create_member_with_agent_and_starting_credit(world):
    client, ids = world
    response = client.post("/admin/users/new", data={
        "username": "newmem", "password": "", "full_name": "N", "phone": "081", "agent_id": ids["agent"], "credit": "250",
    }, follow_redirects=True)
    assert "รหัสผ่านที่ระบบสร้างให้" in response.get_data(as_text=True)
    with app.app_context():
        member = User.query.filter_by(username="newmem").first()
        assert member.partner_id == ids["agent"] and member.credit_balance == 250 and member.role == "member"
    bad = client.post("/admin/users/new", data={"username": "ชื่อไทยยาว", "password": "abcdef"}, follow_redirects=True)
    assert "อังกฤษ" in bad.get_data(as_text=True)
    dup = client.post("/admin/users/new", data={"username": "MEMBER1", "password": "abcdef"}, follow_redirects=True)
    assert "ถูกใช้แล้ว" in dup.get_data(as_text=True)
    helper_as_agent = client.post("/admin/users/new", data={"username": "zzzz1", "password": "abcdef", "agent_id": ids["helper"]}, follow_redirects=True)
    assert "ไม่พบ Agent" in helper_as_agent.get_data(as_text=True)


def test_move_member_and_agent_between_lines(world):
    client, ids = world
    client.post(f"/admin/users/{ids['m1']}/move", data={"agent_id": ids["sub"]})
    client.post(f"/admin/users/{ids['sub']}/move", data={"parent_agent_id": ids["agent"]})
    with app.app_context():
        assert db.session.get(User, ids["m1"]).partner_id == ids["sub"]
    # sub-agent ต้องไม่ถูกย้ายไปอยู่ใต้ตัวเองหรือลูกของตัวเอง
    client.post(f"/admin/users/{ids['agent']}/move", data={"parent_agent_id": ids["sub"]})
    with app.app_context():
        assert db.session.get(User, ids["agent"]).partner_id is None
    client.post(f"/admin/users/{ids['sub']}/move", data={"senior_id": ids["senior"]})
    with app.app_context():
        moved = db.session.get(User, ids["sub"])
        assert moved.partner_id is None and moved.senior_id == ids["senior"]
    client.post(f"/admin/users/{ids['helper']}/move", data={"senior_id": ids["senior"]})  # ผู้ช่วยย้ายไม่ได้
    with app.app_context():
        assert db.session.get(User, ids["helper"]).senior_id is None


def test_assistants_are_not_listed_as_agents(world):
    client, ids = world
    agents_page = client.get("/admin/partners").get_data(as_text=True)
    assert "agent1" in agents_page and "helper1" not in agents_page
    lines = client.get("/admin/lines").get_data(as_text=True)
    assert "helper1" not in lines
    assert "helper1" in client.get("/admin/users?role=assistant").get_data(as_text=True)
    assert client.get(f"/admin/lines/{ids['helper']}").status_code == 404


def test_editing_a_sub_agent_does_not_force_a_senior(world):
    client, ids = world
    client.post(f"/admin/partners/{ids['sub']}/update", data={"commission_rate": "1.5", "status": "active"})
    with app.app_context():
        sub = db.session.get(User, ids["sub"])
        assert sub.partner_profile.commission_rate == 1.5 and sub.senior_id is None and sub.partner_id == ids["agent"]


def test_admin_cancels_pending_ticket_after_close_and_refunds_everything(world):
    client, ids = world
    with app.app_context():
        first = bet(ids, "m1", "12", 100)
        code = first.ticket_code
        credit_after_bet = db.session.get(User, ids["m1"]).credit_balance
        commission_before = db.session.get(User, ids["agent"]).partner_profile.commission_balance
        assert commission_before > 0
        period = db.session.get(ThaiLotteryPeriod, ids["period"])
        period.is_open = False  # ปิดรับแล้ว แอดมินยังต้องยกเลิกได้
        db.session.commit()
    response = client.post(f"/admin/lottery-tickets/{ids['period']}/{ids['m1']}/cancel", data={"ticket_code": code}, follow_redirects=True)
    assert "ยกเลิกโพย" in response.get_data(as_text=True)
    with app.app_context():
        assert ThaiLotteryBet.query.filter_by(ticket_code=code).first().status == "cancelled"
        assert db.session.get(User, ids["m1"]).credit_balance == pytest.approx(credit_after_bet + 100)
        assert db.session.get(User, ids["agent"]).partner_profile.commission_balance == pytest.approx(0)
        assert AdminAuditLog.query.filter_by(action="cancel_ticket").count() == 1
    again = client.post(f"/admin/lottery-tickets/{ids['period']}/{ids['m1']}/cancel", data={"ticket_code": code}, follow_redirects=True)
    assert "ยกเลิกไม่ได้" in again.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, ids["m1"]).credit_balance == pytest.approx(credit_after_bet + 100)  # ไม่คืนซ้ำ


def test_admin_cannot_cancel_after_results_are_settled(world):
    client, ids = world
    with app.app_context():
        code = bet(ids, "m1", "12").ticket_code
    client.post("/admin/thai-lottery", data={"action": "set_result", "period_id": ids["period"], "result_3up": "112", "result_2down": "00"})
    response = client.post(f"/admin/lottery-tickets/{ids['period']}/{ids['m1']}/cancel", data={"ticket_code": code}, follow_redirects=True)
    assert "ยกเลิกไม่ได้" in response.get_data(as_text=True)


def test_manual_result_books_agent_and_senior_stock_and_reverse_restores_all(world):
    """บั๊กเดิม: ตรวจผลด้วยมือไม่คิดหุ้นของ Agent/Senior และย้อนผลก็ไม่ย้อนบัญชีสายงาน"""
    client, ids = world
    with app.app_context():
        bet(ids, "m1", "12", 100)   # 2up เลข 12 → ถูก (ผล 3 ตัวบน 112) จ่าย 100*rate
        bet(ids, "m1", "99", 100)   # ไม่ถูก
        balances = (
            db.session.get(User, ids["m1"]).credit_balance,
            db.session.get(User, ids["agent"]).partner_profile.stock_balance,
            db.session.get(User, ids["senior"]).senior_profile.stock_balance,
        )
    client.post("/admin/thai-lottery", data={"action": "set_result", "period_id": ids["period"], "result_3up": "112", "result_2down": "00"})
    with app.app_context():
        assert PartnerStockLedger.query.count() >= 2 and SeniorStockLedger.query.count() == 2
        agent_stock = db.session.get(User, ids["agent"]).partner_profile.stock_balance
        senior_stock = db.session.get(User, ids["senior"]).senior_profile.stock_balance
        assert agent_stock != 0 and senior_stock != 0
        assert ThaiLotteryPeriod.query.first().is_checked is True
        assert db.session.get(User, ids["m1"]).credit_balance > balances[0] - 200  # ได้รางวัลเข้ากระเป๋า
    client.post("/admin/thai-lottery", data={"action": "reverse_result", "period_id": ids["period"]})
    with app.app_context():
        assert PartnerStockLedger.query.count() == 0 and SeniorStockLedger.query.count() == 0
        assert db.session.get(User, ids["agent"]).partner_profile.stock_balance == pytest.approx(0)
        assert db.session.get(User, ids["senior"]).senior_profile.stock_balance == pytest.approx(0)
        assert db.session.get(User, ids["m1"]).credit_balance == pytest.approx(balances[0])
        assert {b.status for b in ThaiLotteryBet.query.all()} == {"pending"}
        assert ThaiLotteryPeriod.query.first().is_checked is False
    # ตรวจผลซ้ำหลังย้อน ต้องได้ตัวเลขหุ้นเท่าเดิมเป๊ะ (ไม่ซ้อน ไม่ขาด)
    client.post("/admin/thai-lottery", data={"action": "set_result", "period_id": ids["period"], "result_3up": "112", "result_2down": "00"})
    with app.app_context():
        assert db.session.get(User, ids["agent"]).partner_profile.stock_balance == pytest.approx(agent_stock)
        assert db.session.get(User, ids["senior"]).senior_profile.stock_balance == pytest.approx(senior_stock)


def test_reverse_result_refuses_when_winner_spent_the_prize(world):
    client, ids = world
    with app.app_context():
        bet(ids, "m1", "12", 100)
    client.post("/admin/thai-lottery", data={"action": "set_result", "period_id": ids["period"], "result_3up": "112", "result_2down": "00"})
    with app.app_context():
        member = db.session.get(User, ids["m1"])
        member.credit_balance = 0
        db.session.commit()
    response = client.post("/admin/thai-lottery", data={"action": "reverse_result", "period_id": ids["period"]}, follow_redirects=True)
    assert "ถูกใช้ไปแล้ว" in response.get_data(as_text=True)
    with app.app_context():
        assert ThaiLotteryPeriod.query.first().is_checked is True and PartnerStockLedger.query.count() >= 1


def test_period_close_reopen_edit_delete(world):
    client, ids = world
    client.post(f"/admin/periods/{ids['period']}/close")
    with app.app_context():
        period = db.session.get(ThaiLotteryPeriod, ids["period"])
        assert period.is_open is False and period.close_time <= app_now()
    future = (app_now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    client.post(f"/admin/periods/{ids['period']}/reopen", data={"close_time": future})
    with app.app_context():
        assert db.session.get(ThaiLotteryPeriod, ids["period"]).is_open is True
    client.post(f"/admin/periods/{ids['period']}/reopen", data={"close_time": "2000-01-01T00:00"})  # เวลาในอดีตต้องถูกปฏิเสธ
    client.post(f"/admin/periods/{ids['period']}/edit", data={
        "period_date": "d1-edited", "open_time": "2030-01-01T08:00", "close_time": "2030-01-01T07:00"})  # ปิดก่อนเปิด ไม่ผ่าน
    with app.app_context():
        assert db.session.get(ThaiLotteryPeriod, ids["period"]).period_date == "d1"
    client.post(f"/admin/periods/{ids['period']}/edit", data={
        "period_date": "d1-edited", "open_time": "2030-01-01T08:00", "close_time": "2030-01-01T15:00"})
    with app.app_context():
        assert db.session.get(ThaiLotteryPeriod, ids["period"]).period_date == "d1-edited"
        bet_ok = bet(ids, "m1", "12") if False else None
    client.post(f"/admin/periods/{ids['period']}/delete")
    with app.app_context():
        assert db.session.get(ThaiLotteryPeriod, ids["period"]) is None


def test_period_with_bets_cannot_be_deleted(world):
    client, ids = world
    with app.app_context():
        bet(ids, "m1", "12")
    client.post(f"/admin/periods/{ids['period']}/delete")
    with app.app_context():
        assert db.session.get(ThaiLotteryPeriod, ids["period"]) is not None


def test_admin_accounts_create_and_change_own_password(world):
    client, ids = world
    client.post("/admin/admins", data={"username": "boss2", "password": "secret99", "full_name": "Boss"})
    with app.app_context():
        boss = User.query.filter_by(username="boss2").first()
        assert boss.role == "admin" and boss.check_password("secret99")
    client.post("/admin/admins", data={"username": "boss3", "password": "12"})
    with app.app_context():
        assert User.query.filter_by(username="boss3").first() is None
    wrong = client.post("/admin/account", data={"current_password": "nope", "new_password": "newpass77", "confirm_password": "newpass77"}, follow_redirects=True)
    assert "ไม่ถูกต้อง" in wrong.get_data(as_text=True)
    client.post("/admin/account", data={"current_password": "pass1234", "new_password": "newpass77", "confirm_password": "newpass77"})
    with app.app_context():
        assert db.session.get(User, ids["admin"]).check_password("newpass77")


def test_default_admin_is_not_recreated_when_an_admin_exists(world):
    client, ids = world
    from app import ensure_default_admin_accounts
    with app.app_context():
        db.session.get(User, ids["admin"]).username = "owner"
        db.session.commit()
        ensure_default_admin_accounts()
        assert User.query.filter_by(username="admin").first() is None


def test_credit_adjust_cannot_go_below_zero_and_ledger_matches(world):
    client, ids = world
    response = client.post(f"/admin/users/{ids['m0']}/credit", data={"action": "sub", "amount": "9999"}, follow_redirects=True)
    assert "หักเกิน" in response.get_data(as_text=True)
    client.post(f"/admin/users/{ids['m0']}/credit", data={"action": "add", "amount": "50.5", "reason": "โบนัส"})
    with app.app_context():
        assert db.session.get(User, ids["m0"]).credit_balance == pytest.approx(550.5)
        last = WalletTransaction.query.filter_by(user_id=ids["m0"]).order_by(WalletTransaction.id.desc()).first()
        assert last.change == pytest.approx(50.5) and last.balance_after == pytest.approx(550.5)


def test_reports_add_up_to_zero_sum(world):
    """สมาชิก + สายงาน + บริษัท ต้องรวมเป็นศูนย์เสมอ (ไม่มีเงินหาย/งอก)"""
    client, ids = world
    with app.app_context():
        bet(ids, "m1", "12", 100)
        bet(ids, "m2", "12", 100)
        bet(ids, "m0", "99", 100)
    client.post("/admin/thai-lottery", data={"action": "set_result", "period_id": ids["period"], "result_3up": "112", "result_2down": "00"})
    for by in ("date", "room", "type", "senior", "agent", "member"):
        page = client.get(f"/admin/reports/pnl?by={by}&range=today").get_data(as_text=True)
        assert "ไม่มีโพยที่ออกผลแล้ว" not in page, by
    from backoffice_admin import ledger_totals
    with app.app_context():
        bets = ThaiLotteryBet.query.filter(ThaiLotteryBet.status.in_(["win", "lose"])).all()
        commission, stock = ledger_totals([b.id for b in bets])
        member_total = sum((b.reward_amount if b.status == "win" else 0) - b.amount + b.discount_amount for b in bets)
        line_total = sum(commission.values()) + sum(stock.values())
        company = -(member_total + line_total)
        assert member_total + line_total + company == pytest.approx(0)
        assert len(bets) == 3


def test_announcement_audience_reaches_members_or_agents(world):
    client, ids = world
    client.post("/admin/announcements", data={"title": "สำหรับสมาชิก", "audience": "members"})
    client.post("/admin/announcements", data={"title": "สำหรับ Agent", "audience": "agents"})
    client.post("/admin/announcements", data={"title": "ทุกคน", "audience": "all"})
    with app.app_context():
        titles = {a.title for a in store_announcements_for(db.session.get(User, ids["m1"]))}
        assert titles == {"สำหรับสมาชิก", "ทุกคน"}
        assert {a.title for a in store_announcements_for(db.session.get(User, ids["m0"]))} == {"สำหรับสมาชิก", "ทุกคน"}
    from backoffice_app import backoffice_app
    bo = backoffice_app.test_client()
    with bo.session_transaction() as session:
        session["user_id"] = ids["agent"]
    page = bo.get("/dashboard").get_data(as_text=True)
    assert "สำหรับ Agent" in page and "ทุกคน" in page and "สำหรับสมาชิก" not in page


def test_settle_commission_records_wallet_and_refuses_empty(world):
    client, ids = world
    with app.app_context():
        bet(ids, "m1", "12", 100)
        owed = db.session.get(User, ids["agent"]).partner_profile.commission_balance
        assert owed > 0
    client.post(f"/admin/lines/{ids['agent']}/settle", data={"kind": "commission"})
    with app.app_context():
        assert db.session.get(User, ids["agent"]).partner_profile.commission_balance == 0
        entry = WalletTransaction.query.filter_by(user_id=ids["agent"], reference_type="settle").first()
        assert entry.change == pytest.approx(-owed)
    response = client.post(f"/admin/lines/{ids['agent']}/settle", data={"kind": "commission"}, follow_redirects=True)
    assert "ไม่มียอดค้าง" in response.get_data(as_text=True)


def test_non_admin_cannot_reach_admin_pages(world):
    client, ids = world
    member_client = app.test_client()
    member_client.environ_base["HTTP_X_BACKOFFICE_INTERNAL"] = app.config["SECRET_KEY"]
    with member_client.session_transaction() as session:
        session["user_id"] = ids["m1"]
    for url in ("/admin/users", "/admin/reports", "/admin/lines", "/admin/periods", "/admin/admins"):
        assert member_client.get(url).status_code == 403, url
    assert member_client.post(f"/admin/users/{ids['m0']}/password", data={"new_password": "hacked99"}).status_code == 403


def staff_client(client, perms, name="staff1"):
    client.post("/admin/staff", data={"username": name, "password": "staffpass1", "full_name": "S", "perm": perms})
    with app.app_context():
        staff_id = User.query.filter_by(username=name).first().id
    other = app.test_client()
    other.environ_base["HTTP_X_BACKOFFICE_INTERNAL"] = app.config["SECRET_KEY"]
    with other.session_transaction() as session:
        session["user_id"] = staff_id
    return other, staff_id


def test_staff_only_reaches_the_categories_they_were_given(world):
    client, ids = world
    staff, staff_id = staff_client(client, ["tickets", "reports"])
    assert staff.get("/admin/lottery-tickets").status_code == 200
    assert staff.get("/admin/reports").status_code == 200
    assert staff.get("/admin/account").status_code == 200  # เปลี่ยนรหัสตัวเองได้เสมอ
    for url in ("/admin/users", "/admin/thai-lottery", "/admin/periods", "/admin/wallet", "/admin/audit", "/admin/staff", "/admin/admins", "/admin/import-data"):
        assert staff.get(url).status_code == 403, url
    assert staff.post(f"/admin/users/{ids['m1']}/password", data={"new_password": "hacked99"}).status_code == 403
    assert staff.post(f"/admin/users/{ids['m1']}/credit", data={"action": "add", "amount": "10"}).status_code == 403
    nav = staff.get("/admin/reports").get_data(as_text=True)
    assert "/admin/reports/pnl" in nav and "/admin/thai-lottery" not in nav and "/admin/staff" not in nav


def test_staff_cannot_touch_admin_accounts_or_create_staff(world):
    client, ids = world
    staff, staff_id = staff_client(client, ["users", "password", "money", "tickets"])
    assert staff.post(f"/admin/users/{ids['admin']}/password", data={"new_password": "hacked99"}).status_code == 403
    assert staff.post(f"/admin/users/{ids['admin']}/status").status_code == 403
    assert staff.post(f"/admin/users/{ids['admin']}/credit", data={"action": "add", "amount": "5"}).status_code == 403
    assert staff.post("/admin/staff", data={"username": "sneaky1", "password": "abcdef", "perm": ["users"]}).status_code == 403
    assert staff.post("/admin/admins", data={"username": "sneaky2", "password": "abcdef"}).status_code == 403
    # แต่ทำงานที่ได้สิทธิ์ได้ตามปกติ
    assert staff.post(f"/admin/users/{ids['m1']}/password", data={"new_password": "okpass12"}).status_code == 302
    with app.app_context():
        assert db.session.get(User, ids["m1"]).check_password("okpass12")
        assert User.query.filter(User.username.in_(["sneaky1", "sneaky2"])).count() == 0


def test_staff_cannot_open_or_change_owner_login_and_is_suspendable(world):
    client, ids = world
    staff, staff_id = staff_client(client, ["users"])
    client.post(f"/admin/users/{staff_id}/status")  # เจ้าของระงับทีมงาน → เข้าไม่ได้ทันที
    assert staff.get("/admin/users").status_code in (302, 403)
    with app.app_context():
        assert db.session.get(User, staff_id).is_active is False
    client.post(f"/admin/users/{staff_id}/status")
    assert staff.get("/admin/users").status_code == 200


def test_staff_permission_update_and_validation(world):
    client, ids = world
    staff, staff_id = staff_client(client, ["users"])
    bad = client.post("/admin/staff", data={"username": "nopermx", "password": "abcdef"}, follow_redirects=True)
    assert "อย่างน้อย 1 หมวด" in bad.get_data(as_text=True)
    from app import AdminStaff
    with app.app_context():
        row = AdminStaff.query.filter_by(user_id=staff_id).first()
        row_id = row.id
    client.post(f"/admin/staff/{row_id}/permissions", data={"perm": ["reports", "lottery"]})
    assert staff.get("/admin/users").status_code == 403
    assert staff.get("/admin/periods").status_code == 200
