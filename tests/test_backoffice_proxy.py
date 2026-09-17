from types import SimpleNamespace

import backoffice_app as backoffice_module


def test_partner_cannot_enter_admin_namespace(monkeypatch):
    partner = SimpleNamespace(is_admin=False, is_partner=True)
    monkeypatch.setattr(backoffice_module, "logged_user", lambda: partner)

    with backoffice_module.backoffice_app.test_client() as client:
        response = client.get("/main/admin/")

    assert response.status_code == 403


def test_unauthenticated_proxy_redirects_to_login(monkeypatch):
    monkeypatch.setattr(backoffice_module, "logged_user", lambda: None)

    with backoffice_module.backoffice_app.test_client() as client:
        response = client.get("/main/admin/")

    assert response.status_code == 302
    assert response.headers["Location"] == "/login"


def test_partner_dashboard_transfer_link_opens_topup_form():
    from app import app, User

    with app.app_context():
        partner = User.query.filter_by(username="partner").first()
        if partner is None:
            return
        with backoffice_module.backoffice_app.test_client() as client:
            with client.session_transaction() as session:
                session["user_id"] = partner.id
            response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'/main/partner#partner-topup' in response.data
