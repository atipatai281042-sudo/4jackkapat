"""แยกเว็บสมาชิกกับหลังบ้านตามชื่อโดเมน

ตั้งค่าด้วยตัวแปรสภาพแวดล้อม (ไม่ตั้ง = ทำงานเหมือนเดิมทุกอย่าง หลังบ้านอยู่ที่ /backoffice):
  MAIN_HOSTS       ชื่อโดเมนของเว็บสมาชิก คั่นหลายชื่อด้วยจุลภาค เช่น www.mklotto.club,mklotto.club
  BACKOFFICE_HOST  ชื่อโดเมนของหลังบ้าน เช่น bo.mklotto.club

พฤติกรรมเมื่อตั้งค่าแล้ว
  - โดเมนหลังบ้าน  → ได้หลังบ้านที่พาธราก (ไม่ต้องมี /backoffice) เว็บสมาชิกเปิดไม่ได้
  - โดเมนสมาชิก    → ได้เว็บสมาชิกอย่างเดียว ทางเข้า /backoffice เดิมส่งต่อไปโดเมนหลังบ้าน
  - โดเมนอื่น (เช่น xxx.up.railway.app) → เหมือนเดิม: เว็บสมาชิกที่ราก + หลังบ้านที่ /backoffice
คุกกี้ล็อกอินเป็น host-only จึงแยกกันเองตามโดเมน"""
import os
from urllib.parse import quote

from werkzeug.middleware.dispatcher import DispatcherMiddleware

BACKOFFICE_PREFIX = "/backoffice"


def parse_hosts(raw):
    return {item.strip().lower() for item in (raw or "").split(",") if item.strip()}


def request_host(environ):
    """ชื่อโดเมนที่ผู้ใช้เปิดเข้ามา (ตัดพอร์ตออก, ตัวพิมพ์เล็ก)"""
    host = environ.get("HTTP_X_FORWARDED_HOST") or environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or ""
    return host.split(",")[0].strip().split(":")[0].lower()


def strip_backoffice_prefix(path):
    """/backoffice/login → /login, /backoffice → / (ไม่ใช่พาธ /backoffice... ให้คืน None)"""
    if path == BACKOFFICE_PREFIX:
        return "/"
    if path.startswith(BACKOFFICE_PREFIX + "/"):
        return path[len(BACKOFFICE_PREFIX):]
    return None


class HostDispatcher:
    def __init__(self, main_app, backoffice_app, main_hosts=None, backoffice_host=None):
        self.main_app = main_app
        self.backoffice_app = backoffice_app
        self.main_hosts = set(main_hosts or [])
        self.backoffice_host = (backoffice_host or "").lower()
        # โดเมนอื่น ๆ (เช่น *.up.railway.app หรือ localhost) ใช้แบบเดิม
        self.legacy = DispatcherMiddleware(main_app, {BACKOFFICE_PREFIX: backoffice_app})

    def _redirect(self, environ, start_response, host, path):
        query = environ.get("QUERY_STRING", "")
        location = f"https://{host}{quote(path)}" + (f"?{query}" if query else "")
        start_response("302 Found", [("Location", location), ("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", "0")])
        return [b""]

    def __call__(self, environ, start_response):
        host = request_host(environ)
        path = environ.get("PATH_INFO", "") or "/"

        if self.backoffice_host and host == self.backoffice_host:
            rest = strip_backoffice_prefix(path)
            if rest is not None:  # ลิงก์เก่าแบบ bo.../backoffice/xxx → ตัดคำนำหน้าออก
                return self._redirect(environ, start_response, host, rest)

            def add_headers(status, headers, exc_info=None):
                headers = [h for h in headers if h[0].lower() != "x-robots-tag"] + [("X-Robots-Tag", "noindex, nofollow")]
                return start_response(status, headers, exc_info)

            return self.backoffice_app(environ, add_headers)

        if host in self.main_hosts:
            rest = strip_backoffice_prefix(path)
            if rest is not None and self.backoffice_host:
                return self._redirect(environ, start_response, self.backoffice_host, rest)
            if rest is not None:  # ไม่ได้ตั้งโดเมนหลังบ้าน → โดเมนสมาชิกไม่เปิด /backoffice
                start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
                return [b"Not Found"]
            return self.main_app(environ, start_response)

        return self.legacy(environ, start_response)


def build_application(main_app, backoffice_app):
    main_hosts = parse_hosts(os.environ.get("MAIN_HOSTS") or os.environ.get("MAIN_HOST"))
    backoffice_host = (os.environ.get("BACKOFFICE_HOST") or "").strip().lower()
    if not main_hosts and not backoffice_host:
        return DispatcherMiddleware(main_app, {BACKOFFICE_PREFIX: backoffice_app})
    return HostDispatcher(main_app, backoffice_app, main_hosts, backoffice_host)
