from datetime import timedelta

import pytest

from app import (
    LotteryCategory, LotteryRoom, ThaiLotteryBet, ThaiLotteryPeriod, User, app, app_now, db,
)

JSON = {"Accept": "application/json"}


@pytest.fixture
def world():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite://")
    with app.app_context():
        db.drop_all()
        db.create_all()
        member = User(username="mem", full_name="M", role="member", points=0, credit_balance=500.0)
        member.set_password("x123456")
        db.session.add(member)
        cat = LotteryCategory(name="หวยหุ้น")
        db.session.add(cat)
        db.session.commit()
        room = LotteryRoom(name="ดาวโจนส์", category="หวยหุ้น", category_id=cat.id, link_url="/x", is_active=True)
        db.session.add(room)
        db.session.commit()
        now = app_now()
        db.session.add(ThaiLotteryPeriod(
            room_id=room.id, period_date="d", open_time=now - timedelta(hours=1),
            close_time=now + timedelta(hours=1), is_open=True,
        ))
        db.session.commit()
        ids = {"member": member.id, "room": room.id}
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user_id"] = ids["member"]
        yield client, ids


def payload(room_id, amounts, bet_type="2up", numbers=None):
    numbers = numbers or ["12"] * len(amounts)
    return {
        "room_id": room_id, "rate_tier": 1,
        "bet_types[]": [bet_type] * len(amounts), "numbers[]": numbers, "amounts[]": [str(a) for a in amounts],
    }


def test_success_returns_json_and_balance(world):
    client, ids = world
    response = client.post(f"/lottery/thai?room_id={ids['room']}", data=payload(ids["room"], [100]), headers=JSON)
    body = response.get_json()
    assert response.status_code == 200 and body["ok"] is True and body["created"] == 1
    assert body["balance"] == pytest.approx(400)  # ค่าเริ่มต้นในโค้ดไม่มีส่วนลด (ส่วนลดมาจาก seed อ้างอิงบนระบบจริง)
    with app.app_context():
        assert ThaiLotteryBet.query.count() == 1


def test_failure_returns_json_and_creates_nothing(world):
    client, ids = world
    response = client.post(f"/lottery/thai?room_id={ids['room']}", data=payload(ids["room"], [2000, 2000]), headers=JSON)
    body = response.get_json()
    assert response.status_code == 400 and body["ok"] is False
    assert "เครดิต" in body["message"]
    with app.app_context():
        assert ThaiLotteryBet.query.count() == 0
        assert db.session.get(User, ids["member"]).credit_balance == 500.0


def test_without_json_header_still_redirects_with_flash(world):
    client, ids = world
    response = client.post(f"/lottery/thai?room_id={ids['room']}", data=payload(ids["room"], [10]))
    assert response.status_code == 302
