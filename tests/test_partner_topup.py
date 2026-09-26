import pytest

from app import app, db
from models import DepositRequest, PartnerProfile, User


@pytest.fixture
def partner_client():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite://")
    with app.app_context():
        db.drop_all()
        db.create_all()
        partner = User(username="partner", full_name="Partner", role="partner", credit_balance=100)
        partner.set_password("partner123")
        db.session.add(partner)
        db.session.flush()
        db.session.add(PartnerProfile(user_id=partner.id, invite_code="PARTNER01"))
        member = User(username="member", full_name="Member", role="member", partner_id=partner.id)
        member.set_password("member123")
        other = User(username="other", full_name="Other", role="member")
        other.set_password("other123")
        db.session.add_all([member, other])
        db.session.commit()
        ids = {"partner": partner.id, "member": member.id, "other": other.id}

    with app.test_client() as client:
        client.environ_base["HTTP_X_BACKOFFICE_INTERNAL"] = app.config["SECRET_KEY"]
        with client.session_transaction() as session:
            session["user_id"] = ids["partner"]
        yield client, ids


def test_partner_can_top_up_member_from_own_credit(partner_client):
    client, ids = partner_client

    response = client.post("/partner/topup", data={"member_id": ids["member"], "amount": "25"})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(User, ids["partner"]).credit_balance == 75
        assert db.session.get(User, ids["member"]).credit_balance == 25


def test_partner_cannot_top_up_member_outside_own_line(partner_client):
    client, ids = partner_client

    response = client.post("/partner/topup", data={"member_id": ids["other"], "amount": "25"})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(User, ids["partner"]).credit_balance == 100
        assert db.session.get(User, ids["other"]).credit_balance == 0


def test_partner_self_deposit_request_is_disabled(partner_client):
    client, ids = partner_client

    response = client.post(
        "/wallet",
        data={"action": "deposit", "amount": "50", "proof_url": "/static/uploads/proof.png"},
    )

    assert response.status_code == 404  # ระบบฝาก-ถอนปิดถาวร
    with app.app_context():
        assert DepositRequest.query.filter_by(user_id=ids["partner"]).count() == 0


def test_member_login_redirects_to_lottery_rooms(partner_client):
    _, ids = partner_client
    with app.app_context():
        member = db.session.get(User, ids["member"])
        member.set_password("member123")
        db.session.commit()

    with app.test_client() as client:
        response = client.post(
            "/login",
            data={"username": "member", "password": "member123"},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers.get("Location") == "/lottery/rooms"


def test_partner_wallet_is_hidden_and_disabled(partner_client):
    client, _ = partner_client

    response = client.get("/wallet", follow_redirects=False)

    assert response.status_code == 404