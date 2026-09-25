import pytest

from app import (
    AdminAuditLog, SystemSetting, User, app, db, ensure_default_admin_accounts, get_setting, remove_admin2_once,
)


@pytest.fixture
def ctx():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite://")
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield


def add_admin(username):
    user = User(username=username, full_name=username, role="admin", points=0, credit_balance=0.0)
    user.set_password("x123456")
    db.session.add(user)
    db.session.commit()
    return user


def test_admin2_without_history_is_deleted(ctx):
    add_admin("admin")
    add_admin("admin2")
    remove_admin2_once()
    assert User.query.filter_by(username="admin2").first() is None
    assert User.query.filter_by(username="admin").first() is not None
    assert get_setting("admin2_removed_v1") == "1"


def test_admin2_with_audit_history_is_deactivated_not_deleted(ctx):
    add_admin("admin")
    second = add_admin("admin2")
    db.session.add(AdminAuditLog(admin_id=second.id, action="x", target_type="t", target_id=1, details=""))
    db.session.commit()
    remove_admin2_once()
    kept = User.query.filter_by(username="admin2").first()
    assert kept is not None and kept.is_active is False
    assert not kept.check_password("x123456")


def test_never_removes_the_last_admin(ctx):
    add_admin("admin2")
    remove_admin2_once()
    assert User.query.filter_by(username="admin2").first() is not None
    assert get_setting("admin2_removed_v1") == ""


def test_removal_runs_once_and_admin2_is_not_recreated(ctx):
    add_admin("admin")
    remove_admin2_once()
    ensure_default_admin_accounts()
    assert User.query.filter_by(username="admin2").first() is None
    add_admin("admin2")  # เจ้าของสร้างใหม่เองภายหลังต้องไม่ถูกลบซ้ำ
    remove_admin2_once()
    assert User.query.filter_by(username="admin2").first() is not None
