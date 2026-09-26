"""ตัวรับผลหวยจาก huayapp.com (149 หวยในคำขอเดียว) — แปลงเป็นห้อง/งวด/ผลของระบบเรา

huayapp ให้ "ผลล่าสุดของแต่ละหวย" กับ "เวลาออกผล" เท่านั้น ไม่มีเวลาเปิด-ปิดรับ/สถานะงวด/หมวดหมู่ ระบบจึง
  • แบ่งหมวดจากชื่อหวย
  • สร้างงวดถัดไปเองจากเวลาออกผลประจำวัน (ปิดรับก่อนออกผล N นาที ค่าเริ่มต้น 15)
  • จับคู่ผลกับงวดด้วย "วันที่ของผล" เท่านั้น (หวยที่ยังไม่ออกจะโชว์ผลเก่าของวันก่อน ห้ามเอามาตรวจงวดใหม่)
  • ห้องที่นำเข้าใหม่ "ปิดไว้ก่อน" ให้แอดมินเลือกเปิด
ไฟล์นี้ถูก import ท้าย app.py"""
import hmac
import json
import os
import re
import secrets
import time as time_module
from collections import Counter
from datetime import datetime, time, timedelta

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

from app import (
    BET_TYPE_LABELS, BOTTOM_TWO_BET_TYPES, LotteryCategory, LotteryRoom, ThaiLotteryPeriod, User, admin_required, app,
    app_now, audit_admin, current_user, db, get_setting, room_disabled_types, save_setting, settle_lottery_period,
)
from lottery_api import fetch_huayapp_all, fetch_huayapp_ip

PROVIDER_KEY = "hy:"                      # api_key ของห้องที่มาจาก huayapp ขึ้นต้นด้วยข้อความนี้
SKIP_IDS = {"100", "101", "102",           # รัฐบาล/ออมสิน/ธกส — ออกไม่ทุกวัน ยังใช้ผู้ให้บริการเดิม/ตั้งงวดเอง
            "800", "801", "802"}           # หวยชุดเลข 4 หลัก — รูปแบบผลไม่เข้ากับระบบ
DORMANT_DAYS = 4                            # ผลล่าสุดเก่ากว่านี้ = หวยหยุดออกแล้ว ไม่นำเข้า/ไม่สร้างงวด
DEFAULT_CLOSE_MINUTES = 15
STUCK_HOURS = 3                             # เลยเวลาออกผลเกินนี้แล้วยังไม่มีผล = งวดค้าง ให้แอดมินดู
CATEGORY_NAMES = {"lao": "หวยลาว", "hanoi": "หวยฮานอย", "stock": "หวยหุ้น", "other": "หวยอื่นๆ"}
CATEGORY_COLORS = {"lao": "#164e63", "hanoi": "#172554", "stock": "#3f2d1f", "other": "#14532d"}
STOCK_WORDS = ("นิเคอิ", "นิคเคอิ", "จีน", "ฮั่งเส็ง", "ดาวโจนส์", "เยอรมัน", "รัสเซีย", "อังกฤษ", "สิงคโปร์", "อินเดีย",
               "ไต้หวัน", "เกาหลี", "ยูโร", "โรมัน", "ออสเตรเลีย", "อียิปต์")
LAO_WORDS = ("ลาว", "สาละวัน", "หลวงพระบาง", "เวียงจันทน์")
HANOI_WORDS = ("ฮานอย", "เวียดนาม")
MIN_TICK_INTERVAL = 20                      # วินาที — กัน cron ยิงถี่เกินไป
_tick_state = {"last": 0.0}


def categorize(name):
    """ชื่อหวย → (slug หมวด, ชื่อหมวด)"""
    name = name or ""
    if name.startswith("หุ้น"):
        slug = "stock"
    elif any(word in name for word in LAO_WORDS):
        slug = "lao"
    elif any(word in name for word in HANOI_WORDS):
        slug = "hanoi"
    elif any(word in name for word in STOCK_WORDS):
        slug = "stock"
    else:
        slug = "other"
    return slug, CATEGORY_NAMES[slug]


def _digits(value, length):
    text = str(value).strip() if value is not None else ""
    return text if text.isdigit() and len(text) == length else None


def _parse_dt(value):
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def parse_huayapp(payload):
    """แปลงคำตอบ lotto_id=all เป็นรายการที่ใช้ง่าย — ข้ามหวยที่ไม่รองรับ/ข้อมูลไม่ครบ"""
    items = []
    for raw in (payload or {}).get("data") or []:
        lotto_id = str(raw.get("lotto_id", "")).strip()
        if not lotto_id or lotto_id in SKIP_IDS:
            continue
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(raw.get("result_time") or "").strip())
        result_date = _parse_dt(raw.get("result_date"))
        if not match or result_date is None:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        by_code, first_ts = {}, None
        for row in raw.get("lotto_results") or []:
            by_code[str(row.get("lotto_type_code"))] = row.get("number")
            first_ts = first_ts or _parse_dt(row.get("timestamp"))
        top3 = _digits(by_code.get("6"), 3) or _digits(by_code.get("5"), 3)  # บางตลาดมีแต่เลข 3 หลักในช่อง "รางวัลที่ 1"
        if top3 is None and "5" not in by_code:
            continue  # ไม่มีเลขให้ตรวจ (เช่นหวยชุด)
        day = result_date.date()
        # หวยข้ามคืน (ดาวโจนส์ ฯลฯ): "วันของผล" เป็นวันงวด แต่เวลาออกจริงคือหลังเที่ยงคืนของวันถัดไป
        next_day = hour < 6 and first_ts is not None and first_ts.date() == day + timedelta(days=1)
        last_draw = datetime.combine(day + timedelta(days=1 if next_day else 0), time(hour, minute))
        name = str(raw.get("lotto_name") or "").strip()
        items.append({
            "id": lotto_id, "name": name, "hhmm": f"{hour:02d}:{minute:02d}", "result_date": day, "top3": top3,
            "bottom2": _digits(by_code.get("7"), 2), "next_day": next_day, "last_draw": last_draw, "ts": first_ts,
        })
    return items


def close_minutes_for(room):
    if room.close_minutes is not None:
        return room.close_minutes
    raw = get_setting("lottery_close_minutes", "")
    return int(raw) if raw.isdigit() else DEFAULT_CLOSE_MINUTES


def initial_disabled_types(item):
    """ประเภทที่ตรวจไม่ได้เพราะผู้ให้บริการไม่ส่งเลขมา: 3 ตัวล่างของหวยต่างประเทศ + (ถ้ามี) ประเภทที่ต้องใช้ 2 ตัวล่าง"""
    disabled = ["3down"]
    if item["bottom2"] is None:
        disabled += list(BOTTOM_TWO_BET_TYPES)
    return disabled


def ensure_category(slug):
    name = CATEGORY_NAMES[slug]
    category = LotteryCategory.query.filter_by(name=name).first()
    if category is None:
        category = LotteryCategory(name=name, description="นำเข้าจาก huayapp")
        db.session.add(category)
        db.session.flush()
    return category


def ensure_period(room, now):
    """สร้างงวดถัดไปของห้องที่เปิดใช้งาน (ถ้ายังไม่มีงวดที่เปิดรับอยู่) — คืน True ถ้าสร้าง"""
    if not room.draw_time:
        return False
    hour, minute = (int(part) for part in room.draw_time.split(":"))
    minutes = close_minutes_for(room)
    draw = datetime.combine(now.date(), time(hour, minute))
    if draw - timedelta(minutes=minutes) <= now:
        draw += timedelta(days=1)
    period_date = (draw.date() - timedelta(days=1)) if room.draw_next_day else draw.date()
    open_now = ThaiLotteryPeriod.query.filter(
        ThaiLotteryPeriod.room_id == room.id, ThaiLotteryPeriod.is_open.is_(True), ThaiLotteryPeriod.close_time > now
    ).first()
    if open_now is not None:
        return False
    if ThaiLotteryPeriod.query.filter_by(room_id=room.id, period_date=period_date.isoformat()).first() is not None:
        return False
    db.session.add(ThaiLotteryPeriod(
        room_id=room.id, period_date=period_date.isoformat(), open_time=draw - timedelta(days=1),
        close_time=draw - timedelta(minutes=minutes), draw_time=draw, is_open=True, api_key=room.api_key,
    ))
    return True


def apply_result(room, item, now, admin_user):
    """ผลของหวยนี้ตรงกับงวด (ห้อง + วันที่ของผล) ที่ยังไม่ตรวจไหม — ตรวจโพยและจ่ายรางวัล คืน True ถ้าตรวจแล้ว"""
    if item["top3"] is None:
        return False
    period = ThaiLotteryPeriod.query.filter_by(room_id=room.id, period_date=item["result_date"].isoformat()).first()
    if period is None or period.is_checked:
        return False
    reference = period.draw_time or period.close_time
    if now < reference - timedelta(minutes=1):
        return False  # ยังไม่ถึงเวลาออกผลของงวดนี้ — ผลที่เห็นเป็นของงวดก่อน
    if item["ts"] is not None and item["ts"] < reference - timedelta(minutes=30):
        return False  # เวลาประกาศผลเก่ากว่างวดนี้มาก = ผลเก่า
    if item["bottom2"] is None and not set(BOTTOM_TWO_BET_TYPES) <= room_disabled_types(room):
        return False
    period.result_3up, period.result_2down = item["top3"], item["bottom2"] or ""
    period.result_3front = period.result_3back = None
    if settle_lottery_period(period, admin_user) is None:
        period.result_3up = period.result_2down = None
        return False
    period.api_status = "success"
    return True


def sync_huayapp(payload, now=None):
    now = now or app_now()
    items = parse_huayapp(payload)
    admin_user = User.query.filter_by(role="admin").order_by(User.id.asc()).first()
    stats = Counter()
    sort_base = (db.session.query(db.func.coalesce(db.func.max(LotteryRoom.sort_order), 0)).scalar() or 0)
    for item in items:
        key = f"{PROVIDER_KEY}{item['id']}"
        room = LotteryRoom.query.filter_by(api_key=key).first()
        dormant = item["last_draw"] < now - timedelta(days=DORMANT_DAYS)
        if room is None:
            if dormant:
                stats["dormant"] += 1
                continue
            slug, category_name = categorize(item["name"])
            category = ensure_category(slug)
            sort_base += 1
            room = LotteryRoom(
                name=item["name"], description="", category=category_name, category_id=category.id,
                badge_text=f"HY{item['id']}", link_url="/lottery/thai", button_text="เข้าสู่ห้อง", is_active=False,
                sort_order=sort_base, api_key=key, api_category=f"hy_{slug}", bg_color=CATEGORY_COLORS[slug],
                disabled_bet_types=",".join(initial_disabled_types(item)),
            )
            db.session.add(room)
            db.session.flush()
            room.link_url = f"/lottery/thai?room_id={room.id}"
            stats["created"] += 1
        room.draw_time, room.draw_next_day = item["hhmm"], item["next_day"]
        if item["bottom2"] is None and not set(BOTTOM_TWO_BET_TYPES) <= room_disabled_types(room):
            room.disabled_bet_types = ",".join(sorted(room_disabled_types(room) | set(BOTTOM_TWO_BET_TYPES)))
        if room.is_active and not dormant and ensure_period(room, now):
            stats["periods"] += 1
        if apply_result(room, item, now, admin_user):
            stats["settled"] += 1
    db.session.commit()
    stats["items"] = len(items)
    return dict(stats)


def stuck_period_count(now=None):
    now = now or app_now()
    return ThaiLotteryPeriod.query.filter(
        ThaiLotteryPeriod.api_key.like(f"{PROVIDER_KEY}%"), ThaiLotteryPeriod.is_checked.is_(False),
        ThaiLotteryPeriod.draw_time < now - timedelta(hours=STUCK_HOURS),
    ).count()


HUAYAPP_ERRORS = {
    300: "API key ไม่ถูกต้อง", 329: "API key หมดอายุ", 429: "เรียกถี่เกินกำหนด (Rate limit)",
    600: "IP ของเซิร์ฟเวอร์ยังไม่ได้ลงทะเบียนกับ huayapp — เข้า huayapp.com/developer กด \"รีเซ็ต IP ด้วยตัวเอง\"",
    999: "เซิร์ฟเวอร์ huayapp ขัดข้อง",
}


def record_status(ok, message, **extra):
    save_setting("lottery_sync_status", json.dumps(
        {"at": app_now().strftime("%d/%m/%Y %H:%M:%S"), "ok": ok, "message": message, **extra}, ensure_ascii=False
    ))
    db.session.commit()


def sync_huayapp_from_api():
    """ดึงผลจาก huayapp ตามการตั้งค่า (ใช้ทั้งตอนเปิดหน้า ตัวตั้งเวลา และปุ่มในหลังบ้าน) — ไม่ throw"""
    api_key = get_setting("huayapp_api_key", "").strip()
    if not api_key:
        record_status(False, "ยังไม่ได้ใส่ API key ของ huayapp")
        return {"ok": False, "message": "no api key"}
    try:
        payload = fetch_huayapp_all(api_key)
    except Exception as exc:  # noqa: BLE001 — เครือข่ายล่ม/ตอบผิดรูปแบบ ต้องไม่ทำให้หน้าเว็บพัง
        db.session.rollback()
        record_status(False, f"เรียก huayapp ไม่สำเร็จ: {exc}")
        return {"ok": False, "message": str(exc)}
    code = payload.get("code") if isinstance(payload, dict) else None
    if code != 200:
        message = HUAYAPP_ERRORS.get(code, f"huayapp ตอบ code {code}: {payload.get('message') if isinstance(payload, dict) else ''}")
        record_status(False, message, code=code)
        return {"ok": False, "message": message, "code": code}
    try:
        stats = sync_huayapp(payload)
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        record_status(False, f"ประมวลผลข้อมูลไม่สำเร็จ: {exc}")
        return {"ok": False, "message": str(exc)}
    record_status(True, "สำเร็จ", **stats, stuck=stuck_period_count())
    return {"ok": True, **stats}


def run_lottery_sync():
    """ซิงก์ตามผู้ให้บริการที่เลือกไว้ (ค่าเริ่มต้น thailottoapi เหมือนเดิม)"""
    if get_setting("lottery_provider", "thailottoapi") == "huayapp":
        return sync_huayapp_from_api()
    from app import sync_lottery_api_results
    return sync_lottery_api_results()


# --------------------------------------------------------------------- ตัวตั้งเวลาภายนอก
@app.route("/tick/<token>")
def lottery_tick(token):
    """ให้บริการ cron ภายนอก (เช่น cron-job.org ทุก 1 นาที) เรียกเพื่อดึงผล/ตรวจโพยแม้ไม่มีคนเข้าเว็บ"""
    expected = get_setting("lottery_tick_token", "")
    if not expected or not hmac.compare_digest(str(token), expected):
        abort(404)
    if time_module.monotonic() - _tick_state["last"] < MIN_TICK_INTERVAL:
        return jsonify(ok=True, skipped=True)
    _tick_state["last"] = time_module.monotonic()
    result = run_lottery_sync()
    return jsonify(ok=bool(result.get("ok", True)) if isinstance(result, dict) else True)


# --------------------------------------------------------------------- หลังบ้าน: ตั้งค่า API ผลหวย
def public_base_url():
    """ที่อยู่เว็บสมาชิก (ตัวตั้งเวลาต้องเรียกเว็บหลัก ไม่ใช่หลังบ้าน) — ใช้โดเมนแรกใน MAIN_HOSTS"""
    hosts = [h.strip() for h in (os.environ.get("MAIN_HOSTS") or os.environ.get("MAIN_HOST") or "").split(",") if h.strip()]
    return f"https://{hosts[0]}" if hosts else request.host_url.rstrip("/")


def masked(value):
    return ("•" * max(0, len(value) - 4) + value[-4:]) if value else ""


@app.route("/admin/lottery-api", methods=["GET", "POST"])
@admin_required
def admin_lottery_api():
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "save":
            provider = request.form.get("provider", "thailottoapi")
            if provider not in ("thailottoapi", "huayapp"):
                provider = "thailottoapi"
            key = request.form.get("huayapp_api_key", "").strip()
            minutes = request.form.get("close_minutes", "").strip()
            if key:
                save_setting("huayapp_api_key", key)
            if minutes.isdigit() and 0 <= int(minutes) <= 720:
                save_setting("lottery_close_minutes", minutes)
            save_setting("lottery_provider", provider)
            audit_admin(current_user(), "save_lottery_api", "setting", None, f"provider={provider}")
            db.session.commit()
            flash("บันทึกการตั้งค่า API ผลหวยแล้ว", "success")
        elif action == "test":
            api_key = get_setting("huayapp_api_key", "").strip()
            try:
                payload = fetch_huayapp_all(api_key) if api_key else None
            except Exception as exc:  # noqa: BLE001
                flash(f"เรียก huayapp ไม่สำเร็จ: {exc}", "error")
            else:
                if payload is None:
                    flash("ยังไม่ได้ใส่ API key", "error")
                elif payload.get("code") != 200:
                    flash(HUAYAPP_ERRORS.get(payload.get("code"), f"huayapp ตอบ code {payload.get('code')}"), "error")
                else:
                    flash(f"เชื่อมต่อ huayapp สำเร็จ — พบ {len(payload.get('data') or [])} หวย ({len(parse_huayapp(payload))} หวยที่ระบบรองรับ)", "success")
        elif action == "sync":
            result = run_lottery_sync()
            flash("ซิงก์ผลหวยแล้ว" if not isinstance(result, dict) or result.get("ok", True) else f"ซิงก์ไม่สำเร็จ: {result.get('message')}",
                  "success" if not isinstance(result, dict) or result.get("ok", True) else "error")
        elif action in ("enable", "disable"):
            ids = [int(x) for x in request.form.getlist("room_id") if x.isdigit()]
            rooms = LotteryRoom.query.filter(LotteryRoom.id.in_(ids or [-1]), LotteryRoom.api_key.like(f"{PROVIDER_KEY}%")).all()
            for room in rooms:
                room.is_active = action == "enable"
            audit_admin(current_user(), f"{action}_lottery_rooms", "lottery_room", None, f"{len(rooms)} rooms")
            db.session.commit()
            flash(f"{'เปิด' if action == 'enable' else 'ปิด'}ห้อง {len(rooms)} ห้องแล้ว" + (" — งวดจะถูกสร้างในรอบซิงก์ถัดไป" if action == "enable" else ""), "success")
            if action == "enable" and get_setting("lottery_provider", "") == "huayapp":
                run_lottery_sync()
        elif action == "token":
            save_setting("lottery_tick_token", secrets.token_urlsafe(24))
            db.session.commit()
            flash("สร้างลิงก์ตัวตั้งเวลาใหม่แล้ว (ลิงก์เดิมใช้ไม่ได้)", "success")
        return redirect(url_for("admin_lottery_api"))

    if not get_setting("lottery_tick_token", ""):
        save_setting("lottery_tick_token", secrets.token_urlsafe(24))
        db.session.commit()
    rooms = LotteryRoom.query.filter(LotteryRoom.api_key.like(f"{PROVIDER_KEY}%")).order_by(LotteryRoom.sort_order.asc()).all()
    grouped = {}
    for room in rooms:
        grouped.setdefault(room.category, []).append(room)
    try:
        status = json.loads(get_setting("lottery_sync_status", "") or "null")
    except ValueError:
        status = None
    outbound_ip = None
    if request.args.get("ip") == "1":
        try:
            outbound_ip = fetch_huayapp_ip()
        except Exception as exc:  # noqa: BLE001
            outbound_ip = f"ตรวจไม่สำเร็จ: {exc}"
    return render_template(
        "admin_lottery_api.html", provider=get_setting("lottery_provider", "thailottoapi"),
        key_masked=masked(get_setting("huayapp_api_key", "")), close_minutes=get_setting("lottery_close_minutes", str(DEFAULT_CLOSE_MINUTES)),
        grouped=grouped, status=status, stuck=stuck_period_count(), outbound_ip=outbound_ip,
        tick_url=public_base_url() + url_for("lottery_tick", token=get_setting("lottery_tick_token", "")),
        active_count=sum(1 for r in rooms if r.is_active), labels=BET_TYPE_LABELS,
    )
