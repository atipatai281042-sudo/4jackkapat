"""Production entrypoint for Railway/Gunicorn."""
from werkzeug.serving import run_simple

from app import app, backup_database_before_startup, seed_data, sync_lottery_api_results
from backoffice_app import backoffice_app
from host_routing import build_application


with app.app_context():
    backup_database_before_startup()
    seed_data()
    try:
        sync_lottery_api_results()
    except Exception as exc:
        print(f"Lottery API sync skipped: {exc}")


# ไม่ตั้ง MAIN_HOSTS/BACKOFFICE_HOST = หลังบ้านอยู่ที่ /backoffice เหมือนเดิม (ดู host_routing.py)
application = build_application(app, backoffice_app)


if __name__ == "__main__":
    run_simple("0.0.0.0", 5000, application)
