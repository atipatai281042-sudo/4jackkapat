import pytest

import app as app_module
from app import app, db
from models import LotteryCategory, LotteryRoom, ThaiLotteryPeriod


@pytest.fixture
def api_client(monkeypatch):
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite://")
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()


def test_sync_imports_api_room_period_and_result(api_client, monkeypatch):
    payload = {
        "ok": True,
        "date": "2026-09-13",
        "categories": {
            "lottothaihot": {
                "items": [
                    {
                        "category": "lottothaihot",
                        "key": "thailotto",
                        "label": "หวยรัฐบาลไทย",
                        "status": "success",
                        "drawDate": "2026-09-01T00:00:00+07:00",
                        "top3": "212",
                        "bottom2": "04",
                        "bottom3": "257, 346",
                        "schedule": {"openTime": "01:00", "closeTime": "15:25"},
                    }
                ]
            }
        },
    }
    monkeypatch.setattr(app_module, "fetch_results", lambda date_value=None: payload)

    with app.app_context():
        summary = app_module.sync_lottery_api_results("2026-09-13")
        room = LotteryRoom.query.filter_by(api_key="thailotto").one()
        period = ThaiLotteryPeriod.query.filter_by(room_id=room.id, period_date="2026-09-01").one()

        assert summary == {"date": "2026-09-13", "rooms": 1, "periods": 1, "results": 1}
        assert room.category_ref.name == "หวยยอดฮิต"
        assert period.result_3up == "212"
        assert period.result_2down == "04"
        assert period.result_3back == "257, 346"
        assert period.is_checked is False


def test_before_request_refreshes_api_rooms_on_every_public_page_load(api_client, monkeypatch):
    payload = {
        "ok": True,
        "date": "2026-09-13",
        "categories": {
            "lottothaihot": {
                "items": [
                    {
                        "category": "lottothaihot",
                        "key": "thailotto",
                        "label": "หวยรัฐบาลไทย",
                        "status": "pending",
                        "drawDate": "2026-09-13",
                        "schedule": {"openTime": "01:00", "closeTime": "15:25"},
                    }
                ]
            }
        },
    }
    monkeypatch.setattr(app_module, "fetch_results", lambda date_value=None: payload)

    with app.app_context():
        room = LotteryRoom(
            name="หวยรัฐบาลไทย",
            description="seed room",
            category="หวยรัฐบาล",
            badge_text="TH",
            link_url="/lottery/thai",
            button_text="แทงหวย",
            is_active=True,
            sort_order=1,
        )
        db.session.add(room)
        db.session.commit()

    with app.test_client() as client:
        response = client.get("/")

    assert response.status_code == 200
    assert LotteryRoom.query.filter_by(api_key="thailotto").count() == 1


def test_sync_is_idempotent(api_client, monkeypatch):
    payload = {
        "ok": True,
        "date": "2026-09-13",
        "categories": {
            "lottothaihot": {
                "items": [
                    {
                        "key": "thailotto",
                        "label": "หวยรัฐบาลไทย",
                        "status": "pending",
                        "drawDate": "2026-09-13",
                        "schedule": {"openTime": "01:00", "closeTime": "15:25"},
                    }
                ]
            }
        },
    }
    monkeypatch.setattr(app_module, "fetch_results", lambda date_value=None: payload)

    with app.app_context():
        first = app_module.sync_lottery_api_results("2026-09-13")
        second = app_module.sync_lottery_api_results("2026-09-13")

        assert first["rooms"] == 1
        assert first["periods"] == 1
        assert second["rooms"] == 0
        assert second["periods"] == 0
        assert LotteryRoom.query.filter_by(api_key="thailotto").count() == 1
        assert ThaiLotteryPeriod.query.filter_by(period_date="2026-09-13").count() == 1
        assert LotteryCategory.query.filter_by(name="หวยยอดฮิต").count() == 1


def test_sync_keeps_multiple_same_day_rounds_separate(api_client, monkeypatch):
    payload = {
        "ok": True,
        "date": "2026-09-13",
        "categories": {
            "maekhong": {
                "items": [
                    {
                        "key": "mks",
                        "label": "แม่โขงสตาร์",
                        "status": "pending",
                        "drawDate": "2026-09-13",
                        "schedule": {"openTime": "00:00", "closeTime": "15:30"},
                    },
                    {
                        "key": "mkp",
                        "label": "แม่โขง พลัส",
                        "status": "pending",
                        "drawDate": "2026-09-13",
                        "schedule": {"openTime": "00:00", "closeTime": "16:35"},
                    },
                ]
            }
        },
    }
    monkeypatch.setattr(app_module, "fetch_results", lambda date_value=None: payload)

    with app.app_context():
        summary = app_module.sync_lottery_api_results("2026-09-13")
        periods = ThaiLotteryPeriod.query.order_by(ThaiLotteryPeriod.api_key).all()

        assert summary["periods"] == 2
        assert [period.api_key for period in periods] == ["mkp", "mks"]
        assert periods[0].close_time.strftime("%H:%M") == "16:35"
        assert periods[1].close_time.strftime("%H:%M") == "15:30"


def test_sync_skips_liw(api_client, monkeypatch):
    payload = {
        "ok": True,
        "date": "2026-09-13",
        "categories": {
            "liw": {
                "items": [{
                    "key": "liw#1600",
                    "label": "หวย LIW รอบ 16:00",
                    "status": "pending",
                    "drawDate": "2026-09-13 16:00:00",
                    "schedule": {"openTime": "00:00", "closeTime": None},
                }]
            }
        },
    }
    monkeypatch.setattr(app_module, "fetch_results", lambda date_value=None: payload)

    with app.app_context():
        summary = app_module.sync_lottery_api_results("2026-09-13")
        assert summary["rooms"] == 0
        assert summary["periods"] == 0
        assert LotteryRoom.query.filter_by(api_key="liw#1600").count() == 0
