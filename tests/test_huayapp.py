"""ตัวรับผลหวยจาก huayapp: แปลงข้อมูล · ห้องใหม่ปิดไว้ก่อน · สร้างงวดจากเวลาออกผล · ตรวจโพยเฉพาะผลที่ตรงงวด"""
import json
import os
from datetime import datetime, timedelta

import pytest

import lottery_huayapp as hy
from app import (
    LotteryRoom, ThaiLotteryBet, ThaiLotteryPeriod, User, add_thai_lottery_bets, app, db, get_setting, save_setting,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "huayapp_all_sample.json")
NOW = datetime(2026, 9, 26, 22, 30)  # ตอนที่ดึงข้อมูลตัวอย่างมา (เวลาไทย)


def sample():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def ctx():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite://")
    with app.app_context():
        db.drop_all()
        db.create_all()
        admin = User(username="admin", full_name="A", role="admin", credit_balance=0)
        admin.set_password("x123456")
        member = User(username="mem", full_name="M", role="member", credit_balance=1000)
        member.set_password("x123456")
        db.session.add_all([admin, member])
        db.session.commit()
        yield


def room_of(lotto_id):
    return LotteryRoom.query.filter_by(api_key=f"hy:{lotto_id}").first()


def enable(*lotto_ids):
    for lotto_id in lotto_ids:
        room_of(lotto_id).is_active = True
    db.session.commit()


def add_period(room, date_text, draw, is_open=False, close_before=15):
    period = ThaiLotteryPeriod(
        room_id=room.id, period_date=date_text, open_time=draw - timedelta(days=1), close_time=draw - timedelta(minutes=close_before),
        draw_time=draw, is_open=is_open, api_key=room.api_key,
    )
    db.session.add(period)
    db.session.commit()
    return period


def add_bet(user, period, bet_type, number, amount=100, rate=90):
    bet = ThaiLotteryBet(user_id=user.id, period_id=period.id, bet_type=bet_type, number=number, amount=amount, rate=rate,
                         reward_amount=int(amount * rate), status="pending", ticket_code="T1")
    db.session.add(bet)
    db.session.commit()
    return bet


# ------------------------------------------------------------------ แปลงข้อมูล
def test_parse_skips_unsupported_and_detects_overnight_draws():
    items = {i["id"]: i for i in hy.parse_huayapp(sample())}
    assert len(sample()["data"]) == 149
    assert not ({"100", "101", "102", "800", "801", "802"} & set(items))
    assert len(items) == 143
    assert items["183"]["next_day"] and items["170"]["next_day"] and items["217"]["next_day"]  # ดาวโจนส์ออกหลังเที่ยงคืน
    assert not items["123"]["next_day"] and not items["153"]["next_day"]                          # ลาว: ออกวันเดียวกับวันงวด
    assert items["105"]["top3"] == "584" and items["105"]["bottom2"] == "04" and items["105"]["hhmm"] == "17:30"
    assert items["156"]["top3"] == "934" and items["156"]["bottom2"] is None                       # หุ้นจีนเช้า: ไม่มี 2 ตัวล่าง
    assert items["183"]["last_draw"] == datetime(2026, 9, 26, 0, 30)


@pytest.mark.parametrize("name,slug", [
    ("หุ้น ดาวโจนส์", "stock"), ("หุ้นดาวโจนส์ ST", "stock"), ("ดาวโจนส์ VISA", "stock"), ("นิเคอิ VISA เช้า", "stock"),
    ("ลาวประตูชัย", "lao"), ("สาละวัน ST", "lao"), ("ประชาชนลาว", "lao"), ("ลาว VISA", "lao"),
    ("ฮานอย VIP", "hanoi"), ("เวียดนามปกติ ออนไลน์", "hanoi"), ("ฮานอย VISA", "hanoi"),
    ("มาเลย์", "other"), ("ฮ่องกง VISA", "other"), ("มาเก๊า VIP", "other"), ("เจแปน ล็อตโต้", "other"),
])
def test_categorize(name, slug):
    assert hy.categorize(name)[0] == slug


# ------------------------------------------------------------------ นำเข้าห้อง
def test_sync_creates_rooms_inactive_and_skips_dormant(ctx):
    stats = hy.sync_huayapp(sample(), now=NOW)
    rooms = LotteryRoom.query.filter(LotteryRoom.api_key.like("hy:%")).all()
    assert stats["created"] == len(rooms) and all(not r.is_active for r in rooms)  # ห้องใหม่ปิดไว้ก่อน
    assert stats["dormant"] > 0 and room_of("114") is None and room_of("218") is None and room_of("199") is None  # หยุดออกนานแล้ว
    assert ThaiLotteryPeriod.query.count() == 0  # ยังไม่เปิดห้อง = ยังไม่สร้างงวด
    assert all("3down" in r.disabled_bet_types for r in rooms)  # ผู้ให้บริการไม่ส่งผล 3 ตัวล่างของหวยต่างประเทศ
    partial = room_of("156")
    assert {"2down", "rundown", "oddeven_down", "highlow_down"} <= set(partial.disabled_bet_types.split(","))
    assert "2down" not in room_of("105").disabled_bet_types
    assert room_of("183").draw_next_day is True and room_of("183").draw_time == "00:30"
    assert room_of("105").category == "หวยฮานอย" and room_of("123").category == "หวยลาว" and room_of("170").category == "หวยหุ้น"
    again = hy.sync_huayapp(sample(), now=NOW)
    assert again.get("created", 0) == 0  # รันซ้ำไม่สร้างซ้ำ


def test_admin_choices_survive_a_resync(ctx):
    hy.sync_huayapp(sample(), now=NOW)
    room = room_of("105")
    room.name, room.is_active, room.close_minutes = "ฮานอยพิเศษ (ชื่อที่แอดมินตั้ง)", True, 30
    db.session.commit()
    hy.sync_huayapp(sample(), now=NOW)
    room = room_of("105")
    assert room.name.startswith("ฮานอยพิเศษ (") and room.is_active and room.close_minutes == 30


# ------------------------------------------------------------------ สร้างงวด
def test_enabled_rooms_get_the_next_period_with_15_minute_close(ctx):
    hy.sync_huayapp(sample(), now=NOW)
    enable("105", "183")
    stats = hy.sync_huayapp(sample(), now=NOW)
    assert stats["periods"] == 2
    hanoi = ThaiLotteryPeriod.query.filter_by(room_id=room_of("105").id).one()
    assert hanoi.period_date == "2026-09-27" and hanoi.draw_time == datetime(2026, 9, 27, 17, 30)
    assert hanoi.close_time == datetime(2026, 9, 27, 17, 15) and hanoi.open_time == datetime(2026, 9, 26, 17, 30) and hanoi.is_open
    dow = ThaiLotteryPeriod.query.filter_by(room_id=room_of("183").id).one()
    assert dow.period_date == "2026-09-26"  # วันงวด (ผลจะมากับ result_date 09-26) ทั้งที่ออกจริง 00:30 ของ 27
    assert dow.draw_time == datetime(2026, 9, 27, 0, 30) and dow.close_time == datetime(2026, 9, 27, 0, 15)
    assert hy.sync_huayapp(sample(), now=NOW).get("periods", 0) == 0  # มีงวดเปิดรับอยู่แล้ว ไม่สร้างซ้ำ


def test_close_minutes_setting_and_per_room_override(ctx):
    save_setting("lottery_close_minutes", "30")
    db.session.commit()
    hy.sync_huayapp(sample(), now=NOW)
    enable("105", "106")
    room_of("106").close_minutes = 5
    db.session.commit()
    hy.sync_huayapp(sample(), now=NOW)
    assert ThaiLotteryPeriod.query.filter_by(room_id=room_of("105").id).one().close_time == datetime(2026, 9, 27, 17, 0)
    assert ThaiLotteryPeriod.query.filter_by(room_id=room_of("106").id).one().close_time == datetime(2026, 9, 27, 18, 25)


def test_inside_the_closing_window_the_next_days_period_opens(ctx):
    hy.sync_huayapp(sample(), now=NOW)
    enable("105")
    hy.sync_huayapp(sample(), now=datetime(2026, 9, 26, 17, 20))  # 10 นาทีก่อนออกผล (ปิดรับแล้ว) → เปิดงวดของพรุ่งนี้
    assert ThaiLotteryPeriod.query.filter_by(room_id=room_of("105").id).one().period_date == "2026-09-27"


# ------------------------------------------------------------------ ตรวจโพย
def test_result_settles_only_the_period_with_the_same_result_date(ctx):
    hy.sync_huayapp(sample(), now=NOW)
    enable("105")
    member = User.query.filter_by(username="mem").one()
    today = add_period(room_of("105"), "2026-09-26", datetime(2026, 9, 26, 17, 30))
    tomorrow = add_period(room_of("105"), "2026-09-27", datetime(2026, 9, 27, 17, 30), is_open=True)
    win = add_bet(member, today, "2up", "84")      # ผลฮานอยพิเศษ 3 ตัวบน 584 → 2 ตัวบน 84
    lose = add_bet(member, today, "2up", "12")
    waiting = add_bet(member, tomorrow, "2up", "84")  # งวดพรุ่งนี้ยังไม่ออก ห้ามถูกตรวจด้วยผลเก่า
    stats = hy.sync_huayapp(sample(), now=NOW)
    assert stats["settled"] >= 1
    db.session.expire_all()
    assert db.session.get(ThaiLotteryBet, win.id).status == "win" and db.session.get(ThaiLotteryBet, lose.id).status == "lose"
    assert db.session.get(ThaiLotteryBet, waiting.id).status == "pending"
    assert db.session.get(ThaiLotteryPeriod, today.id).is_checked and not db.session.get(ThaiLotteryPeriod, tomorrow.id).is_checked
    assert db.session.get(User, member.id).credit_balance == 1000 + 9000
    assert hy.sync_huayapp(sample(), now=NOW).get("settled", 0) == 0  # ตรวจซ้ำไม่ได้


def test_result_is_ignored_before_the_draw_time(ctx):
    hy.sync_huayapp(sample(), now=NOW)
    enable("105")
    period = add_period(room_of("105"), "2026-09-26", datetime(2026, 9, 26, 23, 59), is_open=True)  # ยังไม่ถึงเวลาออกผล
    hy.sync_huayapp(sample(), now=NOW)
    assert not db.session.get(ThaiLotteryPeriod, period.id).is_checked


def test_stale_timestamp_result_is_ignored(ctx):
    payload = sample()
    for lottery in payload["data"]:
        if lottery["lotto_id"] == "105":
            for row in lottery["lotto_results"]:
                row["timestamp"] = "2026-09-20 17:31:00"  # เวลาประกาศเก่ากว่างวดมาก
    hy.sync_huayapp(payload, now=NOW)
    enable("105")
    period = add_period(room_of("105"), "2026-09-26", datetime(2026, 9, 26, 17, 30))
    hy.sync_huayapp(payload, now=NOW)
    assert not db.session.get(ThaiLotteryPeriod, period.id).is_checked


def test_overnight_lottery_is_settled_by_its_session_date(ctx):
    hy.sync_huayapp(sample(), now=NOW)
    enable("183")
    member = User.query.filter_by(username="mem").one()
    period = add_period(room_of("183"), "2026-09-25", datetime(2026, 9, 26, 0, 30))  # ออกจริงหลังเที่ยงคืน
    bet = add_bet(member, period, "3up", "405", rate=900)  # ผลดาวโจนส์ VIP: 3 ตัวบน 405
    hy.sync_huayapp(sample(), now=datetime(2026, 9, 26, 1, 0))
    db.session.expire_all()
    assert db.session.get(ThaiLotteryBet, bet.id).status == "win"


def test_lottery_without_two_down_still_settles_its_other_bets(ctx):
    hy.sync_huayapp(sample(), now=NOW)
    enable("156")
    member = User.query.filter_by(username="mem").one()
    period = add_period(room_of("156"), "2026-09-25", datetime(2026, 9, 25, 10, 30))
    win_up, win_two = add_bet(member, period, "3up", "934"), add_bet(member, period, "2up", "34")
    hy.sync_huayapp(sample(), now=NOW)
    db.session.expire_all()
    assert db.session.get(ThaiLotteryBet, win_up.id).status == "win" and db.session.get(ThaiLotteryBet, win_two.id).status == "win"
    assert db.session.get(ThaiLotteryPeriod, period.id).result_2down == ""


def test_disabled_bet_types_are_refused_and_hidden(ctx):
    hy.sync_huayapp(sample(), now=NOW)
    enable("105")
    member = User.query.filter_by(username="mem").one()
    period = add_period(room_of("105"), "2026-09-27", datetime.now() + timedelta(hours=3), is_open=True)
    period.open_time, period.close_time = datetime.now() - timedelta(hours=1), datetime.now() + timedelta(hours=2)
    db.session.commit()
    with pytest.raises(ValueError, match="ยังไม่เปิดรับแทง"):
        add_thai_lottery_bets(member, period, [{"bet_type": "3down", "number": "123", "amount": 10}])
    assert add_thai_lottery_bets(member, period, [{"bet_type": "2up", "number": "12", "amount": 10}]) == 1
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = member.id
    page = client.get(f"/lottery/thai?room_id={room_of('105').id}").get_data(as_text=True)
    assert "3 ตัวล่าง" not in page.split('id="bp-rate-body"')[1].split("</tbody>")[0]  # ตารางอัตราจ่ายไม่มี 3 ตัวล่าง


# ------------------------------------------------------------------ ผู้ให้บริการ / ข้อผิดพลาด
def test_provider_dispatch_and_error_messages(ctx, monkeypatch):
    called = []
    import app as app_module
    monkeypatch.setattr(app_module, "sync_lottery_api_results", lambda *a, **k: called.append("old") or {"ok": True})
    hy.run_lottery_sync()
    assert called == ["old"]  # ค่าเริ่มต้นยังเป็นผู้ให้บริการเดิม

    save_setting("lottery_provider", "huayapp")
    db.session.commit()
    assert hy.run_lottery_sync()["ok"] is False and "API key" in json.loads(get_setting("lottery_sync_status"))["message"]

    save_setting("huayapp_api_key", "test-key")
    db.session.commit()
    monkeypatch.setattr(hy, "fetch_huayapp_all", lambda key: {"code": 600, "message": "IP not registered"})
    result = hy.run_lottery_sync()
    assert result["ok"] is False and "IP" in json.loads(get_setting("lottery_sync_status"))["message"]

    monkeypatch.setattr(hy, "fetch_huayapp_all", lambda key: (_ for _ in ()).throw(OSError("timeout")))
    assert hy.run_lottery_sync()["ok"] is False and called == ["old"]  # ล้มเหลวต้องไม่ทำให้หน้าเว็บพัง และไม่ถอยไปใช้เจ้าเดิมเอง

    monkeypatch.setattr(hy, "fetch_huayapp_all", lambda key: sample())
    result = hy.run_lottery_sync()
    assert result["ok"] is True and result["created"] > 100
    status = json.loads(get_setting("lottery_sync_status"))
    assert status["ok"] and status["created"] == result["created"]


# ------------------------------------------------------------------ หลังบ้าน + ตัวตั้งเวลา
@pytest.fixture
def admin_client(ctx):
    client = app.test_client()
    client.environ_base["HTTP_X_BACKOFFICE_INTERNAL"] = app.config["SECRET_KEY"]
    with client.session_transaction() as session:
        session["user_id"] = User.query.filter_by(username="admin").one().id
    return client


def test_admin_page_saves_settings_enables_rooms_and_never_shows_the_key(admin_client, monkeypatch):
    monkeypatch.setattr(hy, "fetch_huayapp_all", lambda key: sample())
    assert admin_client.get("/admin/lottery-api").status_code == 200
    admin_client.post("/admin/lottery-api", data={"action": "save", "provider": "huayapp", "huayapp_api_key": "SECRET-KEY-1234", "close_minutes": "20"})
    assert get_setting("lottery_provider") == "huayapp" and get_setting("lottery_close_minutes") == "20"
    page = admin_client.get("/admin/lottery-api").get_data(as_text=True)
    assert "SECRET-KEY-1234" not in page and "1234" in page  # แสดงแค่ 4 ตัวท้าย
    assert "เชื่อมต่อ huayapp สำเร็จ" in admin_client.post("/admin/lottery-api", data={"action": "test"}, follow_redirects=True).get_data(as_text=True)
    admin_client.post("/admin/lottery-api", data={"action": "sync"})
    assert LotteryRoom.query.filter(LotteryRoom.api_key.like("hy:%")).count() > 100
    ids = [room_of("105").id, room_of("183").id]
    admin_client.post("/admin/lottery-api", data={"action": "enable", "room_id": ids})
    assert room_of("105").is_active and room_of("183").is_active and ThaiLotteryPeriod.query.count() == 2  # เปิดแล้วสร้างงวดทันที
    admin_client.post("/admin/lottery-api", data={"action": "disable", "room_id": ids[:1]})
    assert not room_of("105").is_active and room_of("183").is_active
    # เปลี่ยนกลับ thailottoapi ไม่ลบห้องเดิม
    admin_client.post("/admin/lottery-api", data={"action": "save", "provider": "thailottoapi", "close_minutes": "15"})
    assert room_of("183") is not None and get_setting("huayapp_api_key") == "SECRET-KEY-1234"


def test_room_edit_can_set_disabled_types_and_close_minutes(admin_client):
    hy.sync_huayapp(sample(), now=NOW)
    room = room_of("105")
    admin_client.post(f"/admin/lottery-rooms/{room.id}/edit", data={
        "name": room.name, "category_id": room.category_id, "is_active": "y", "disabled_types": ["3up", "runup"], "close_minutes": "45",
    })
    room = room_of("105")
    assert set(room.disabled_bet_types.split(",")) == {"3up", "runup"} and room.close_minutes == 45


def test_tick_endpoint_needs_the_secret_and_runs_the_sync(admin_client, monkeypatch):
    calls = []
    monkeypatch.setattr(hy, "run_lottery_sync", lambda: calls.append(1) or {"ok": True})
    admin_client.get("/admin/lottery-api")  # สร้างโทเค็นครั้งแรก
    token = get_setting("lottery_tick_token")
    assert token
    anonymous = app.test_client()
    assert anonymous.get("/tick/wrong-token").status_code == 404
    hy._tick_state["last"] = 0.0
    assert anonymous.get(f"/tick/{token}").get_json()["ok"] is True and calls == [1]
    assert anonymous.get(f"/tick/{token}").get_json().get("skipped") is True and calls == [1]  # ถี่เกินไป → ข้าม
    admin_client.post("/admin/lottery-api", data={"action": "token"})
    assert anonymous.get(f"/tick/{token}").status_code == 404  # ลิงก์เก่าใช้ไม่ได้แล้ว


def test_staff_with_lottery_permission_can_open_the_page(admin_client):
    from models import AdminStaff
    staff = User(username="staff1", full_name="S", role="admin")
    staff.set_password("x123456")
    db.session.add(staff)
    db.session.flush()
    db.session.add(AdminStaff(user_id=staff.id, permissions="lottery"))
    other = User(username="staff2", full_name="S2", role="admin")
    other.set_password("x123456")
    db.session.add(other)
    db.session.flush()
    db.session.add(AdminStaff(user_id=other.id, permissions="reports"))
    db.session.commit()
    for user, expected in ((staff, 200), (other, 403)):
        client = app.test_client()
        client.environ_base["HTTP_X_BACKOFFICE_INTERNAL"] = app.config["SECRET_KEY"]
        with client.session_transaction() as session:
            session["user_id"] = user.id
        assert client.get("/admin/lottery-api").status_code == expected


def test_rooms_page_has_search_and_uses_few_queries(ctx):
    from sqlalchemy import event
    hy.sync_huayapp(sample(), now=NOW)
    for room in LotteryRoom.query.filter(LotteryRoom.api_key.like("hy:%")).limit(60):
        room.is_active = True
    db.session.commit()
    member = User.query.filter_by(username="mem").one()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = member.id
    counter = {"n": 0}

    def count(*args):
        counter["n"] += 1

    event.listen(db.engine, "before_cursor_execute", count)
    try:
        page = client.get("/lottery/rooms").get_data(as_text=True)
    finally:
        event.remove(db.engine, "before_cursor_execute", count)
    assert 'id="room-search"' in page
    assert counter["n"] < 60, counter["n"]  # 60 ห้อง ต้องไม่ยิงฐานข้อมูลทีละห้อง
