import pytest
from datetime import datetime, timedelta

from app import app, db, User, LotteryRoom, BlockedNumber, ThaiLotteryPeriod, ThaiLotteryBet, add_thai_lottery_bets


@pytest.fixture
def client():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://')
    with app.app_context():
        db.drop_all()
        db.create_all()

        admin = User(username='admin', full_name='Admin', role='admin', points=0)
        admin.set_password('admin1234')
        db.session.add(admin)
        db.session.commit()

        room = LotteryRoom(
            name='หวยรัฐบาลไทย',
            description='test room',
            link_url='/lottery/thai',
            button_text='แทงหวย',
            is_active=True,
            sort_order=1,
        )
        db.session.add(room)
        db.session.commit()

    with app.test_client() as client:
        with client.session_transaction() as session:
            session['user_id'] = 1
        yield client


def test_register_accepts_special_characters_in_password():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://')
    with app.app_context():
        db.drop_all()
        db.create_all()

    with app.test_client() as client:
        response = client.post('/register', data={
            'username': 'newuser',
            'password': 'abc123!@',
            'confirm_password': 'abc123!@',
            'full_name': 'New User',
            'phone': '0812345678',
        }, follow_redirects=False)

        assert response.status_code == 302

        with app.app_context():
            user = User.query.filter_by(username='newuser').first()
            assert user is not None
            assert user.check_password('abc123!@')


def test_register_rejects_non_account_characters():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://')
    with app.app_context():
        db.drop_all()
        db.create_all()

    with app.test_client() as client:
        response = client.post('/register', data={
            'username': 'ชื่อผู้ใช้',
            'password': 'abc123!@',
            'confirm_password': 'abc123!@',
        }, follow_redirects=False)

        assert response.status_code == 200
        assert 'ชื่อผู้ใช้ต้องมีอย่างน้อย 4 ตัว' in response.data.decode('utf-8')


def test_register_rejects_duplicate_username_case_insensitively():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://')
    with app.app_context():
        db.drop_all()
        db.create_all()
        existing = User(username='ExistingUser', role='member')
        existing.set_password('abc123!@')
        db.session.add(existing)
        db.session.commit()

    with app.test_client() as client:
        response = client.post('/register', data={
            'username': 'existinguser',
            'password': 'abc123!@',
            'confirm_password': 'abc123!@',
        }, follow_redirects=False)

        assert response.status_code == 200
        assert 'ชื่อผู้ใช้นี้ถูกใช้แล้ว' in response.data.decode('utf-8')


def test_register_rejects_mismatched_passwords():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://')
    with app.app_context():
        db.drop_all()
        db.create_all()

    with app.test_client() as client:
        response = client.post('/register', data={
            'username': 'mismatchuser',
            'password': 'abc123!@',
            'confirm_password': 'abc123!#',
        }, follow_redirects=False)

        assert response.status_code == 200
        assert 'รหัสผ่านทั้งสองช่องไม่ตรงกัน' in response.data.decode('utf-8')


def test_admin_login_keeps_admin_route_accessible():
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://')
    with app.app_context():
        db.drop_all()
        db.create_all()

        admin = User(username='admin', full_name='Admin', role='admin', points=0)
        admin.set_password('admin1234')
        db.session.add(admin)
        db.session.commit()

    with app.test_client() as client:
        response = client.post('/login', data={
            'username': 'admin',
            'password': 'admin1234',
        }, follow_redirects=False)

        assert response.status_code == 302
        assert response.headers.get('Location') == '/admin'

        admin_response = client.get('/admin', follow_redirects=False)
        assert admin_response.status_code == 200
        assert 'ศูนย์ควบคุมระบบ' in admin_response.get_data(as_text=True)


def test_admin_can_add_lottery_room(client):
    response = client.post(
        '/admin/lottery-rooms/add',
        data={
            'name': 'หวยฮานอย',
            'description': 'test hanoi',
            'link_url': '/lottery/hanoi',
            'button_text': 'เข้าห้อง',
            'badge_text': 'HN',
            'bg_color': '#000000',
            'is_active': 'y',
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert LotteryRoom.query.filter_by(name='หวยฮานอย').count() == 1


def test_admin_can_manage_blocked_numbers(client):
    with app.app_context():
        room = LotteryRoom.query.filter_by(name='หวยรัฐบาลไทย').first()
        assert room is not None

        response = client.post(
            f'/admin/lottery-rooms/{room.id}/blocked-numbers',
            data={'bet_type': '3up', 'number': '123', 'payout_multiplier': '100'},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert BlockedNumber.query.filter_by(room_id=room.id, bet_type='3up', number='123').count() == 1

        blocked = BlockedNumber.query.filter_by(room_id=room.id).first()
        assert blocked is not None
        response = client.post(f'/admin/blocked-numbers/{blocked.id}/delete', follow_redirects=False)

        assert response.status_code == 302
        assert BlockedNumber.query.filter_by(id=blocked.id).count() == 0


def test_lottery_rejects_invalid_number_before_charging_credit(client):
    with app.app_context():
        user = User(username='member-invalid', full_name='Member', credit_balance=100)
        user.set_password('test-password')
        db.session.add(user)
        room = LotteryRoom.query.filter_by(name='หวยรัฐบาลไทย').first()
        period = ThaiLotteryPeriod(
            room_id=room.id,
            period_date='2026-09-16',
            open_time=datetime.now() - timedelta(minutes=1),
            close_time=datetime.now() + timedelta(minutes=30),
            is_open=True,
        )
        db.session.add(period)
        db.session.commit()

        with pytest.raises(ValueError):
            add_thai_lottery_bets(user, period, [{'bet_type': '3up', 'number': '12x', 'amount': 10}])

        assert user.credit_balance == 100
        assert ThaiLotteryBet.query.filter_by(user_id=user.id).count() == 0


def test_lottery_batch_checks_total_credit_before_charging(client):
    with app.app_context():
        user = User(username='member-batch', full_name='Member', credit_balance=10)
        user.set_password('test-password')
        db.session.add(user)
        room = LotteryRoom.query.filter_by(name='หวยรัฐบาลไทย').first()
        period = ThaiLotteryPeriod(
            room_id=room.id,
            period_date='2026-09-16',
            open_time=datetime.now() - timedelta(minutes=1),
            close_time=datetime.now() + timedelta(minutes=30),
            is_open=True,
        )
        db.session.add(period)
        db.session.commit()

        with pytest.raises(ValueError):
            add_thai_lottery_bets(user, period, [
                {'bet_type': '3up', 'number': '123', 'amount': 6},
                {'bet_type': '2down', 'number': '45', 'amount': 6},
            ])

        assert user.credit_balance == 10
        assert ThaiLotteryBet.query.filter_by(user_id=user.id).count() == 0


def test_lottery_rejects_bet_before_open_time(client):
    with app.app_context():
        user = User(username='member-early', full_name='Member', credit_balance=100)
        user.set_password('test-password')
        db.session.add(user)
        room = LotteryRoom.query.filter_by(name='หวยรัฐบาลไทย').first()
        period = ThaiLotteryPeriod(
            room_id=room.id,
            period_date='2026-09-16',
            open_time=datetime.now() + timedelta(minutes=30),
            close_time=datetime.now() + timedelta(hours=1),
            is_open=True,
        )
        db.session.add(period)
        db.session.commit()

        with pytest.raises(ValueError):
            add_thai_lottery_bets(user, period, [{'bet_type': '3up', 'number': '123', 'amount': 10}])

        assert user.credit_balance == 100


def test_lottery_preserves_duplicate_entries_in_one_slip(client):
    with app.app_context():
        user = User(username='member-duplicate', full_name='Member', credit_balance=100)
        user.set_password('test-password')
        db.session.add(user)
        room = LotteryRoom.query.filter_by(name='หวยรัฐบาลไทย').first()
        period = ThaiLotteryPeriod(
            room_id=room.id,
            period_date='2026-09-16',
            open_time=datetime.now() - timedelta(minutes=1),
            close_time=datetime.now() + timedelta(minutes=30),
            is_open=True,
        )
        db.session.add(period)
        db.session.commit()

        created = add_thai_lottery_bets(user, period, [
            {'bet_type': '3up', 'number': '123', 'amount': 10},
            {'bet_type': '3up', 'number': '123', 'amount': 10},
        ])

        assert created == 2
        assert user.credit_balance == 80
        bets = ThaiLotteryBet.query.filter_by(user_id=user.id, period_id=period.id).all()
        assert len(bets) == 2
        assert bets[0].ticket_code == bets[1].ticket_code


def test_settlement_pays_credit_and_cannot_repeat(client):
    with app.app_context():
        user = User(username='member-win', full_name='Member', credit_balance=100)
        user.set_password('test-password')
        db.session.add(user)
        room = LotteryRoom.query.filter_by(name='หวยรัฐบาลไทย').first()
        period = ThaiLotteryPeriod(
            room_id=room.id,
            period_date='2026-09-16',
            open_time=datetime.now() - timedelta(minutes=1),
            close_time=datetime.now() + timedelta(minutes=30),
            is_open=True,
        )
        db.session.add(period)
        db.session.commit()
        add_thai_lottery_bets(user, period, [{'bet_type': '3up', 'number': '123', 'amount': 10}])
        assert user.credit_balance == 90

        response = client.post('/admin/thai-lottery', data={
            'action': 'set_result',
            'period_id': period.id,
            'result_3up': '123',
            'result_2down': '45',
            'result_3front': '',
            'result_3back': '',
        }, follow_redirects=False)

        assert response.status_code == 302
        db.session.refresh(user)
        assert user.credit_balance == 9090

        client.post('/admin/thai-lottery', data={
            'action': 'reverse_result',
            'period_id': period.id,
        }, follow_redirects=False)
        db.session.refresh(user)
        db.session.refresh(period)
        assert user.credit_balance == 90
        assert period.is_checked is False
        assert ThaiLotteryBet.query.filter_by(period_id=period.id, status='pending').count() == 1

        client.post('/admin/thai-lottery', data={
            'action': 'set_result',
            'period_id': period.id,
            'result_3up': '123',
            'result_2down': '99',
        }, follow_redirects=False)
        db.session.refresh(user)
        assert user.credit_balance == 9090

        client.post('/admin/thai-lottery', data={
            'action': 'set_result',
            'period_id': period.id,
            'result_3up': '999',
            'result_2down': '99',
        }, follow_redirects=False)
        db.session.refresh(user)
        assert user.credit_balance == 9090


def test_admin_can_confirm_all_api_results_once(client):
    with app.app_context():
        user = User(username='member-api-win', full_name='API Member', credit_balance=0)
        user.set_password('test-password')
        room = LotteryRoom.query.filter_by(name='หวยรัฐบาลไทย').first()
        period = ThaiLotteryPeriod(
            room_id=room.id,
            period_date='2026-09-16',
            open_time=datetime.now() - timedelta(hours=2),
            close_time=datetime.now() - timedelta(minutes=30),
            is_open=False,
            result_3up='123',
            result_2down='45',
            api_status='success',
            api_key='thailotto',
        )
        db.session.add_all([user, period])
        db.session.commit()
        bet = ThaiLotteryBet(
            user_id=user.id, period_id=period.id, bet_type='3up',
            number='123', amount=10, rate=900, reward_amount=9000,
            status='pending',
        )
        db.session.add(bet)
        db.session.commit()

        response = client.post('/admin/thai-lottery', data={
            'action': 'confirm_api_results',
        }, follow_redirects=False)

        assert response.status_code == 302
        db.session.refresh(user)
        db.session.refresh(period)
        assert user.credit_balance == 9000
        assert period.is_checked is True
        assert bet.status == 'win'

        assert ThaiLotteryBet.query.filter_by(period_id=period.id, status='win').count() == 1


def test_lottery_ticket_is_limited_to_owner(client):
    with app.app_context():
        user = User(username='member-ticket', full_name='Member', credit_balance=100)
        user.set_password('test-password')
        db.session.add(user)
        room = LotteryRoom.query.filter_by(name='หวยรัฐบาลไทย').first()
        period = ThaiLotteryPeriod(
            room_id=room.id,
            period_date='2026-09-16',
            open_time=datetime.now() - timedelta(minutes=1),
            close_time=datetime.now() + timedelta(minutes=30),
            is_open=True,
        )
        db.session.add(period)
        db.session.commit()
        add_thai_lottery_bets(user, period, [{'bet_type': '2down', 'number': '45', 'amount': 10}])

        with client.session_transaction() as session:
            session['user_id'] = user.id
        response = client.get(f'/lottery/ticket/{period.id}')
        assert response.status_code == 200
        assert 'ใบโพยหวย' in response.get_data(as_text=True)

        with client.session_transaction() as session:
            session['user_id'] = 1
        assert client.get(f'/lottery/ticket/{period.id}').status_code == 404


def test_admin_can_review_member_lottery_tickets(client):
    with app.app_context():
        user = User(username='member-admin-view', full_name='Member', credit_balance=100)
        user.set_password('test-password')
        db.session.add(user)
        room = LotteryRoom.query.filter_by(name='หวยรัฐบาลไทย').first()
        period = ThaiLotteryPeriod(
            room_id=room.id,
            period_date='2026-09-16',
            open_time=datetime.now() - timedelta(minutes=1),
            close_time=datetime.now() + timedelta(minutes=30),
            is_open=True,
        )
        db.session.add(period)
        db.session.commit()
        add_thai_lottery_bets(user, period, [{'bet_type': '3up', 'number': '123', 'amount': 10}])

        response = client.get('/admin/lottery-tickets')
        assert response.status_code == 200
        assert 'member-admin-view' in response.get_data(as_text=True)

        response = client.get(f'/admin/lottery-tickets/{period.id}/{user.id}')
        assert response.status_code == 200
        assert 'สมาชิก member-admin-view' in response.get_data(as_text=True)


def test_treasure_chest_demo_is_disabled(client):
    with app.app_context():
        user = User(username='member-chest', full_name='Member', credit_balance=100)
        user.set_password('test-password')
        db.session.add(user)
        db.session.commit()
        with client.session_transaction() as session:
            session['user_id'] = user.id

        assert client.get('/games/treasure-chest').status_code == 404
        assert client.post('/games/treasure-chest/open', json={'chest': 1}).status_code == 404
        db.session.refresh(user)
        assert user.credit_balance == 100
