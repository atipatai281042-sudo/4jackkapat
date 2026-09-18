"""
models.py — โครงสร้างฐานข้อมูลทั้งหมดของระบบ
"""

from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class SystemSetting(db.Model):
    __tablename__ = "system_settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, default="", nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ==========================================================
# 1) ตารางสมาชิก
# ==========================================================
class User(db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name     = db.Column(db.String(120), default="")
    phone         = db.Column(db.String(20), default="")
    points        = db.Column(db.Integer, default=0, nullable=False)
    credit_balance = db.Column(db.Float, default=0.0, nullable=False)
    role          = db.Column(db.String(20), default="member", nullable=False)  # member | partner | senior | admin
    partner_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)  # member -> agent (partner)
    senior_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)  # agent (partner) -> senior
    vip_tier_id   = db.Column(db.Integer, db.ForeignKey("vip_tiers.id"), nullable=True)
    is_active     = db.Column(db.Boolean, default=True, nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    redemptions = db.relationship(
        "RedemptionHistory", backref="user",
        lazy=True, cascade="all, delete-orphan"
    )
    point_logs = db.relationship(
        "PointLog", backref="user", lazy=True,
        cascade="all, delete-orphan", foreign_keys="PointLog.user_id"
    )
    partner_profile = db.relationship(
        "PartnerProfile", backref="user", uselist=False,
        cascade="all, delete-orphan", foreign_keys="PartnerProfile.user_id"
    )
    senior_profile = db.relationship(
        "SeniorProfile", backref="user", uselist=False,
        cascade="all, delete-orphan", foreign_keys="SeniorProfile.user_id"
    )
    referred_members = db.relationship(
        "User", backref=db.backref("partner", remote_side=[id]),
        foreign_keys=[partner_id], lazy=True
    )
    managed_agents = db.relationship(
        "User", backref=db.backref("senior", remote_side=[id]),
        foreign_keys=[senior_id], lazy=True
    )
    partner_limits = db.relationship(
        "PartnerMemberLimit", backref="member", lazy=True,
        foreign_keys="PartnerMemberLimit.member_id", cascade="all, delete-orphan"
    )
    vip_tier = db.relationship("VipTier", backref="members", foreign_keys=[vip_tier_id])

    # ---- จัดการรหัสผ่าน (เข้ารหัสเสมอ ไม่เก็บ plain text) ----
    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_partner(self) -> bool:
        return self.role == "partner"

    @property
    def is_senior(self) -> bool:
        return self.role == "senior"

    def __repr__(self):
        return f"<User {self.username} (points={self.points}, credit={self.credit_balance})>"


# ==========================================================
# 2) ตารางของรางวัล
# ==========================================================
class Reward(db.Model):
    __tablename__ = "rewards"

    id              = db.Column(db.Integer, primary_key=True)
    name            = db.Column(db.String(150), nullable=False)
    description     = db.Column(db.Text, default="")
    image_url       = db.Column(db.String(500), default="")
    points_required = db.Column(db.Integer, nullable=False, default=100)
    stock           = db.Column(db.Integer, nullable=False, default=0)
    is_active       = db.Column(db.Boolean, default=True, nullable=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    redemptions = db.relationship("RedemptionHistory", backref="reward", lazy=True)

    @property
    def in_stock(self) -> bool:
        return self.stock > 0

    def __repr__(self):
        return f"<Reward {self.name}>"


# ==========================================================
# 3) ตารางประวัติการแลกของรางวัล
# ==========================================================
class RedemptionHistory(db.Model):
    __tablename__ = "redemption_history"

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reward_id    = db.Column(db.Integer, db.ForeignKey("rewards.id"), nullable=True)
    reward_name  = db.Column(db.String(150), nullable=False)   # เก็บสำเนาชื่อไว้ กันของรางวัลถูกลบ
    points_used  = db.Column(db.Integer, nullable=False)
    redeem_code  = db.Column(db.String(20), unique=True, nullable=False)
    status       = db.Column(db.String(20), default="pending", nullable=False)  # pending | completed | cancelled
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Redemption {self.redeem_code}>"


# ==========================================================
# 4) ตารางบันทึกการเพิ่ม/ลดแต้ม (Audit Log)
# ==========================================================
class PointLog(db.Model):
    __tablename__ = "point_logs"

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    admin_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    change       = db.Column(db.Integer, nullable=False)       # +100 หรือ -50
    balance_after = db.Column(db.Integer, nullable=False)
    reason       = db.Column(db.String(255), default="")
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    admin = db.relationship("User", foreign_keys=[admin_id])

    def __repr__(self):
        return f"<PointLog {self.change:+d}>"


class WalletTransaction(db.Model):
    __tablename__ = "wallet_transactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    wallet_type = db.Column(db.String(20), nullable=False)  # points | credit | commission
    change = db.Column(db.Float, nullable=False)
    balance_after = db.Column(db.Float, nullable=False)
    reference_type = db.Column(db.String(40), default="")
    reference_id = db.Column(db.Integer, nullable=True)
    reason = db.Column(db.String(255), default="")
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="wallet_transactions")
    admin = db.relationship("User", foreign_keys=[admin_id])


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(150), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    category = db.Column(db.String(30), default="system")
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="notifications")


class LoginHistory(db.Model):
    __tablename__ = "login_history"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    ip_address = db.Column(db.String(64), default="")
    user_agent = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="login_history")


class Announcement(db.Model):
    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    body = db.Column(db.Text, default="")
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AdminAuditLog(db.Model):
    __tablename__ = "admin_audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(80), nullable=False)
    target_type = db.Column(db.String(40), default="")
    target_id = db.Column(db.Integer, nullable=True)
    details = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    admin = db.relationship("User", backref="admin_audit_logs")


class ResponsiblePlayProfile(db.Model):
    __tablename__ = "responsible_play_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    daily_bet_limit = db.Column(db.Float, nullable=True)
    self_excluded_until = db.Column(db.DateTime, nullable=True)
    age_verified = db.Column(db.Boolean, default=False, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("responsible_play", uselist=False))


class DepositRequest(db.Model):
    __tablename__ = "deposit_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(40), default="manual_transfer")
    reference = db.Column(db.String(120), default="")
    proof_url = db.Column(db.String(500), default="")
    note = db.Column(db.String(500), default="")
    status = db.Column(db.String(20), default="pending", nullable=False)
    admin_note = db.Column(db.String(500), default="")
    processed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", foreign_keys=[user_id], backref="deposit_requests")
    admin = db.relationship("User", foreign_keys=[processed_by])


class WithdrawalRequest(db.Model):
    __tablename__ = "withdrawal_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(40), default="manual_transfer")
    payout_account = db.Column(db.String(120), nullable=False)
    note = db.Column(db.String(500), default="")
    status = db.Column(db.String(20), default="pending", nullable=False)
    admin_note = db.Column(db.String(500), default="")
    processed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", foreign_keys=[user_id], backref="withdrawal_requests")
    admin = db.relationship("User", foreign_keys=[processed_by])


class UserBankAccount(db.Model):
    __tablename__ = "user_bank_accounts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    bank_code = db.Column(db.String(40), nullable=False)
    bank_name = db.Column(db.String(120), nullable=False)
    account_number = db.Column(db.String(30), nullable=False)
    account_name = db.Column(db.String(160), nullable=False)
    logo_url = db.Column(db.String(500), default="")
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("bank_accounts", cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<UserBankAccount {self.bank_code}:{self.account_number}>"


class PartnerProfile(db.Model):
    __tablename__ = "partner_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    invite_code = db.Column(db.String(30), unique=True, nullable=False, index=True)
    commission_rate = db.Column(db.Float, nullable=False, default=3.0)
    status = db.Column(db.String(20), nullable=False, default="active")
    commission_balance = db.Column(db.Float, nullable=False, default=0.0)
    stock_balance = db.Column(db.Float, nullable=False, default=0.0)
    notes = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SeniorProfile(db.Model):
    __tablename__ = "senior_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    invite_code = db.Column(db.String(30), unique=True, nullable=False, index=True)
    commission_rate = db.Column(db.Float, nullable=False, default=1.0)
    status = db.Column(db.String(20), nullable=False, default="active")
    commission_balance = db.Column(db.Float, nullable=False, default=0.0)
    stock_balance = db.Column(db.Float, nullable=False, default=0.0)
    notes = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PartnerAssistant(db.Model):
    __tablename__ = "partner_assistants"
    __table_args__ = (db.UniqueConstraint("partner_id", "assistant_user_id", name="uq_partner_assistant"),)

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    assistant_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True, index=True)
    permissions = db.Column(db.String(255), nullable=False, default="members,bets,reports")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    partner = db.relationship("User", foreign_keys=[partner_id])
    assistant = db.relationship("User", foreign_keys=[assistant_user_id])


class PartnerMemberLimit(db.Model):
    __tablename__ = "partner_member_limits"
    __table_args__ = (db.UniqueConstraint("partner_id", "member_id", name="uq_partner_member_limit"),)

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    min_bet = db.Column(db.Float, nullable=False, default=0.0)
    max_bet = db.Column(db.Float, nullable=False, default=1000000.0)
    max_number_bet = db.Column(db.Float, nullable=False, default=1000000.0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PartnerRoomSetting(db.Model):
    __tablename__ = "partner_room_settings"
    __table_args__ = (db.UniqueConstraint("partner_id", "room_id", name="uq_partner_room_setting"),)

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    room_id = db.Column(db.Integer, db.ForeignKey("lottery_rooms.id"), nullable=False, index=True)
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PartnerStockShare(db.Model):
    __tablename__ = "partner_stock_shares"
    __table_args__ = (db.UniqueConstraint("partner_id", "room_id", name="uq_partner_stock_share"),)

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    room_id = db.Column(db.Integer, db.ForeignKey("lottery_rooms.id"), nullable=False, index=True)
    hold_percent = db.Column(db.Float, nullable=False, default=0.0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    room = db.relationship("LotteryRoom")


class PartnerStockLedger(db.Model):
    __tablename__ = "partner_stock_ledger"

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    bet_id = db.Column(db.Integer, db.ForeignKey("thai_lottery_bets.id"), nullable=True)
    room_id = db.Column(db.Integer, db.ForeignKey("lottery_rooms.id"), nullable=True)
    hold_percent = db.Column(db.Float, nullable=False, default=0.0)
    stake_amount = db.Column(db.Float, nullable=False, default=0.0)
    pnl_amount = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    partner = db.relationship("User", foreign_keys=[partner_id])
    member = db.relationship("User", foreign_keys=[member_id])


class SeniorStockShare(db.Model):
    __tablename__ = "senior_stock_shares"
    __table_args__ = (db.UniqueConstraint("senior_id", "room_id", name="uq_senior_stock_share"),)

    id = db.Column(db.Integer, primary_key=True)
    senior_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    room_id = db.Column(db.Integer, db.ForeignKey("lottery_rooms.id"), nullable=False, index=True)
    hold_percent = db.Column(db.Float, nullable=False, default=0.0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    room = db.relationship("LotteryRoom")


class SeniorStockLedger(db.Model):
    __tablename__ = "senior_stock_ledger"

    id = db.Column(db.Integer, primary_key=True)
    senior_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    agent_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    bet_id = db.Column(db.Integer, db.ForeignKey("thai_lottery_bets.id"), nullable=True)
    room_id = db.Column(db.Integer, db.ForeignKey("lottery_rooms.id"), nullable=True)
    hold_percent = db.Column(db.Float, nullable=False, default=0.0)
    stake_amount = db.Column(db.Float, nullable=False, default=0.0)
    pnl_amount = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    senior = db.relationship("User", foreign_keys=[senior_id])
    agent = db.relationship("User", foreign_keys=[agent_id])
    member = db.relationship("User", foreign_keys=[member_id])


class PartnerPayoutRule(db.Model):
    __tablename__ = "partner_payout_rules"
    __table_args__ = (db.UniqueConstraint("partner_id", "bet_type", name="uq_partner_payout_rule"),)

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    bet_type = db.Column(db.String(30), nullable=False)
    payout_multiplier = db.Column(db.Float, nullable=False, default=1.0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PartnerAcceptanceLimit(db.Model):
    __tablename__ = "partner_acceptance_limits"
    __table_args__ = (db.UniqueConstraint("partner_id", "room_id", "bet_type", name="uq_partner_acceptance_limit"),)

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    room_id = db.Column(db.Integer, db.ForeignKey("lottery_rooms.id"), nullable=False, index=True)
    bet_type = db.Column(db.String(30), nullable=False)
    amount_limit = db.Column(db.Float, nullable=False, default=0.0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    room = db.relationship("LotteryRoom")


class PartnerAcceptanceNumber(db.Model):
    __tablename__ = "partner_acceptance_numbers"
    __table_args__ = (db.UniqueConstraint("partner_id", "period_id", "bet_type", "number", name="uq_partner_acceptance_number"),)

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    period_id = db.Column(db.Integer, db.ForeignKey("thai_lottery_periods.id"), nullable=False, index=True)
    bet_type = db.Column(db.String(30), nullable=False)
    number = db.Column(db.String(10), nullable=False)
    amount_limit = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    period = db.relationship("ThaiLotteryPeriod")


class PartnerBlockedNumber(db.Model):
    __tablename__ = "partner_blocked_numbers"

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    room_id = db.Column(db.Integer, db.ForeignKey("lottery_rooms.id"), nullable=False, index=True)
    bet_type = db.Column(db.String(30), nullable=False)
    number = db.Column(db.String(10), nullable=False)
    payout_multiplier = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    room = db.relationship("LotteryRoom", backref="partner_blocked_numbers")


class PartnerPresence(db.Model):
    __tablename__ = "partner_presence"

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False, index=True)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    partner = db.relationship("User", foreign_keys=[partner_id])
    member = db.relationship("User", foreign_keys=[member_id])


class CommissionLedger(db.Model):
    __tablename__ = "commission_ledger"

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    bet_id = db.Column(db.Integer, db.ForeignKey("thai_lottery_bets.id"), nullable=True)
    base_amount = db.Column(db.Float, nullable=False, default=0.0)
    rate = db.Column(db.Float, nullable=False, default=0.0)
    commission_amount = db.Column(db.Float, nullable=False, default=0.0)
    status = db.Column(db.String(20), nullable=False, default="approved")
    reason = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    partner = db.relationship("User", foreign_keys=[partner_id], backref="commission_entries")
    member = db.relationship("User", foreign_keys=[member_id])


class SeniorCommissionLedger(db.Model):
    __tablename__ = "senior_commission_ledger"

    id = db.Column(db.Integer, primary_key=True)
    senior_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    agent_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    bet_id = db.Column(db.Integer, db.ForeignKey("thai_lottery_bets.id"), nullable=True)
    base_amount = db.Column(db.Float, nullable=False, default=0.0)
    rate = db.Column(db.Float, nullable=False, default=0.0)
    commission_amount = db.Column(db.Float, nullable=False, default=0.0)
    status = db.Column(db.String(20), nullable=False, default="approved")
    reason = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    senior = db.relationship("User", foreign_keys=[senior_id], backref="senior_commission_entries")
    agent = db.relationship("User", foreign_keys=[agent_id])
    member = db.relationship("User", foreign_keys=[member_id])


class VipTier(db.Model):
    __tablename__ = "vip_tiers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    rank = db.Column(db.Integer, nullable=False, default=1)
    min_points = db.Column(db.Integer, nullable=False, default=0)
    badge_text = db.Column(db.String(40), default="")
    benefits = db.Column(db.Text, default="")
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ==========================================================
# 5) ตารางเก็บรูปสไลด์แบนเนอร์หน้าแรก (Hero Slider)
# ==========================================================
class HeroBanner(db.Model):
    __tablename__ = "hero_banners"

    id          = db.Column(db.Integer, primary_key=True)
    image_url   = db.Column(db.String(500), nullable=False)   # ลิงก์รูปภาพ
    title       = db.Column(db.String(150), default="")         # หัวข้อ/คำอธิบาย (ถ้ามี)
    is_active   = db.Column(db.Boolean, default=True, nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<HeroBanner {self.id}>"


# ==========================================================
# 6) ตารางเก็บคลังภาพระบบ (Media Library)
# ==========================================================
class MediaImage(db.Model):
    __tablename__ = "media_images"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)   # ชื่อไฟล์รูปในโฟลเดอร์ uploads
    title = db.Column(db.String(255), nullable=True)        # ชื่อหรือคำอธิบายรูป
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<MediaImage {self.filename}>"


# ==========================================================
# 7) ตารางระบบหวยไทยและห้องหวย
# ==========================================================
class LotteryCategory(db.Model):
    __tablename__ = "lottery_categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(255), default="")
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<LotteryCategory {self.name}>"


class LotteryRoom(db.Model):
    __tablename__ = "lottery_rooms"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    category = db.Column(db.String(60), nullable=False, default="หวยรัฐบาล")
    category_id = db.Column(db.Integer, db.ForeignKey("lottery_categories.id"), nullable=True)
    badge_text = db.Column(db.String(50), nullable=True)
    image_url = db.Column(db.String(255), nullable=True)
    bg_color = db.Column(db.String(50), default="#ffffff")
    link_url = db.Column(db.String(255), nullable=False)
    button_text = db.Column(db.String(50), default="เข้าสู่ห้อง")
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    api_key = db.Column(db.String(80), nullable=True, index=True)
    api_category = db.Column(db.String(40), nullable=True)
    category_ref = db.relationship("LotteryCategory", backref=db.backref("rooms", lazy=True))

    # ความสัมพันธ์ไปยังเลขอั้นประจำห้อง (ลบห้อง เลขอั้นถูกลบตาม)
    blocked_numbers = db.relationship(
        "BlockedNumber", backref="room",
        lazy=True, cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<LotteryRoom {self.name}>"


# ==========================================================
# 8) ตารางเลขอั้น (Blocked Numbers) — เพิ่มใหม่
# ==========================================================
class BlockedNumber(db.Model):
    __tablename__ = "blocked_numbers"

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('lottery_rooms.id'), nullable=False)
    bet_type = db.Column(db.String(30), nullable=False)  # เช่น 3up, 2up, 2down ฯลฯ
    number = db.Column(db.String(10), nullable=False)     # หมายเลขที่อั้น เช่น 789, 89
    payout_multiplier = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<BlockedNumber {self.bet_type}:{self.number}>"


class LotteryPayoutRule(db.Model):
    __tablename__ = "lottery_payout_rules"

    id = db.Column(db.Integer, primary_key=True)
    bet_type = db.Column(db.String(30), unique=True, nullable=False)
    payout_multiplier = db.Column(db.Float, nullable=False, default=1.0)
    description = db.Column(db.String(100), default="")
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<LotteryPayoutRule {self.bet_type} x{self.payout_multiplier}>"


class ThaiLotteryPeriod(db.Model):
    __tablename__ = "thai_lottery_periods"

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('lottery_rooms.id'), nullable=True)
    period_date = db.Column(db.String(20), nullable=False)
    open_time = db.Column(db.DateTime, nullable=True)
    close_time = db.Column(db.DateTime, nullable=False)
    is_open = db.Column(db.Boolean, default=True)
    
    result_3up = db.Column(db.String(3), nullable=True)
    result_2down = db.Column(db.String(2), nullable=True)
    result_3front = db.Column(db.String(100), nullable=True)
    result_3back = db.Column(db.String(100), nullable=True)
    is_checked = db.Column(db.Boolean, default=False)
    api_status = db.Column(db.String(20), nullable=True)
    api_key = db.Column(db.String(80), nullable=True, index=True)

    room = db.relationship('LotteryRoom', backref=db.backref('thai_periods', lazy=True))

    def __repr__(self):
        return f"<ThaiLotteryPeriod {self.period_date}>"


class ThaiLotteryBet(db.Model):
    __tablename__ = "thai_lottery_bets"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    period_id = db.Column(db.Integer, db.ForeignKey('thai_lottery_periods.id'), nullable=False)
    
    bet_type = db.Column(db.String(30), nullable=False)
    number = db.Column(db.String(10), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    rate = db.Column(db.Float, nullable=False)
    ticket_code = db.Column(db.String(40), nullable=True, index=True)
    
    status = db.Column(db.String(20), default="pending")
    reward_amount = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('thai_bets', lazy=True))
    period = db.relationship('ThaiLotteryPeriod', backref=db.backref('bets', lazy=True))

    def __repr__(self):
        return f"<ThaiLotteryBet {self.number} amount={self.amount}>"