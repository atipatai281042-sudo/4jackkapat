from models import User


def test_user_has_credit_balance_separate_from_points():
    assert hasattr(User, "credit_balance")
    assert hasattr(User, "points")
    assert User.__table__.columns["points"].name == "points"
    assert User.__table__.columns["credit_balance"].name == "credit_balance"
