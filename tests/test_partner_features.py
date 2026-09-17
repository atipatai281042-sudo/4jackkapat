from app import app


def test_partner_feature_routes_are_registered():
    routes = {rule.rule for rule in app.url_map.iter_rules()}

    assert "/partner/bets" in routes
    assert "/partner/finance" in routes
    assert "/partner/finance/bank-account" in routes
    assert "/partner/finance/payout" in routes
    assert "/partner/assistants" in routes
    assert "/partner/assistants/<int:assistant_id>/toggle" in routes


def test_partner_feature_routes_require_expected_methods():
    methods = {
        rule.rule: rule.methods
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/partner/")
    }

    assert methods["/partner/bets"] == {"GET", "HEAD", "OPTIONS"}
    assert "POST" in methods["/partner/finance/payout"]
    assert "POST" in methods["/partner/assistants"]
