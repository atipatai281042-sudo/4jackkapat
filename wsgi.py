"""Production entrypoint for Railway/Gunicorn."""
from app import app, backup_database_before_startup, seed_data, sync_lottery_api_results


with app.app_context():
    backup_database_before_startup()
    seed_data()
    try:
        sync_lottery_api_results()
    except Exception as exc:
        print(f"Lottery API sync skipped: {exc}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
