"""Production entrypoint for Railway/Gunicorn."""
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple

from app import app, backup_database_before_startup, seed_data, sync_lottery_api_results
from backoffice_app import backoffice_app


with app.app_context():
    backup_database_before_startup()
    seed_data()
    try:
        sync_lottery_api_results()
    except Exception as exc:
        print(f"Lottery API sync skipped: {exc}")


application = DispatcherMiddleware(app, {"/backoffice": backoffice_app})


if __name__ == "__main__":
    run_simple("0.0.0.0", 5000, application)
