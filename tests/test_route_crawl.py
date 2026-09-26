"""ไล่เปิดทุกหน้า (GET) ของทุกบทบาท ต้องไม่มีหน้าไหนพัง (5xx) — ค่า id ในลิงก์ใช้ข้อมูลจริงจากโลกจำลอง"""
import re
from datetime import timedelta

import pytest

from app import (
    LotteryCategory, LotteryRoom, PartnerAssistant, PartnerProfile, SeniorProfile, ThaiLotteryPeriod, User,
    add_thai_lottery_bets, app, app_now, db,
)

SKIP = {"static", "logout", "refresh"}
# หน้าที่เปลี่ยนสถานะด้วย GET ตั้งใจให้ข้าม
SKIP_PATH = re.compile(r"^/(logout|static)")


def make_user(username, role="member", **extra):
    user = User(username=username, full_name=username, role=role, points=0, credit_balance=extra.pop("credit", 1000.0), **extra)
    user.set_password("pass1234")
    db.session.add(user)
    db.session.flush()
    return user


@pytest.fixture
def crawl_world():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite://")
    with app.app_context():
        db.drop_all()
        db.create_all()
        admin = make_user("admin", "admin")
        senior = make_user("senior1", "senior")
        db.session.add(SeniorProfile(user_id=senior.id, invite_code="S1", commission_rate=0.5))
        agent = make_user("agent1", "partner", senior_id=senior.id)
        db.session.add(PartnerProfile(user_id=agent.id, invite_code="A1", commission_rate=2.0))
        helper = make_user("helper1", "partner", partner_id=agent.id)
        db.session.add(PartnerProfile(user_id=helper.id, invite_code="H1", commission_rate=0))
        db.session.add(PartnerAssistant(partner_id=agent.id, assistant_user_id=helper.id, permissions="members,bets,reports,transfer,cancel,storefront,takelist"))
        member = make_user("member1", partner_id=agent.id)
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
        add_thai_lottery_bets(member, period, [{"bet_type": "2up", "number": "12", "amount": 50}])
        db.session.commit()
        yield {"admin": admin.id, "senior": senior.id, "partner": agent.id, "helper": helper.id, "member": member.id,
               "room": room.id, "period": period.id}


def fill(rule, ids):
    values = {}
    for arg in rule.arguments:
        converter = rule._converters[arg]
        if arg in ("ticket_code", "filename", "path", "action", "kind", "code"):
            values[arg] = {"action": "close", "kind": "member"}.get(arg, "x")
        elif "period" in arg:
            values[arg] = ids["period"]
        elif "room" in arg:
            values[arg] = ids["room"]
        elif arg == "user_id" or "member" in arg:
            values[arg] = ids["member"]
        elif type(converter).__name__ == "IntegerConverter":
            values[arg] = 1
        else:
            values[arg] = "x"
    return values


def crawl(role_key, ids, prefixes):
    client = app.test_client()
    client.environ_base["HTTP_X_BACKOFFICE_INTERNAL"] = app.config["SECRET_KEY"]
    if role_key:
        with client.session_transaction() as session:
            session["user_id"] = ids[role_key]
    failures = []
    checked = 0
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint in SKIP:
            continue
        if not rule.rule.startswith(prefixes):
            continue
        if any(x in rule.rule for x in ("/logout", "/delete", "/toggle", "/export")):
            continue
        try:
            url = re.sub(r"\?.*", "", app.url_map.bind("localhost").build(rule.endpoint, fill(rule, ids), force_external=False))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"BUILD {rule.rule}: {exc}")
            continue
        checked += 1
        response = client.get(url)
        if response.status_code >= 500:
            failures.append(f"{response.status_code} {url}")
        elif response.status_code == 200 and "Traceback" in response.get_data(as_text=True):
            failures.append(f"TRACE {url}")
    return checked, failures


@pytest.mark.parametrize("role_key,prefixes", [
    ("admin", ("/admin",)),
    ("partner", ("/partner",)),
    ("helper", ("/partner",)),
    ("senior", ("/senior",)),
    ("member", ("/lottery", "/history", "/wallet", "/notifications", "/change-password", "/responsible-play", "/rules", "/payout", "/contact", "/deposit", "/withdraw", "/bank", "/dashboard", "/", "/games")),
    (None, ("/", "/login", "/register", "/lottery", "/rules", "/contact", "/privacy")),
])
def test_no_page_crashes(crawl_world, role_key, prefixes):
    checked, failures = crawl(role_key, crawl_world, prefixes)
    assert checked > 0
    assert not failures, "\n".join(failures)
