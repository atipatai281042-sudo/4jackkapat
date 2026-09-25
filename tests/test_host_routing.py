from werkzeug.test import Client

from host_routing import HostDispatcher, build_application, parse_hosts, request_host, strip_backoffice_prefix


def make_app(label):
    def app(environ, start_response):
        body = f"{label}:{environ.get('SCRIPT_NAME', '')}|{environ.get('PATH_INFO', '')}".encode()
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [body]
    return app


def client(main_hosts=("www.example.com",), backoffice_host="bo.example.com"):
    dispatcher = HostDispatcher(make_app("main"), make_app("bo"), set(main_hosts), backoffice_host)
    return Client(dispatcher)


def get(c, host, path, **kwargs):
    return c.get(path, headers={"Host": host}, **kwargs)


def test_helpers():
    assert parse_hosts(" WWW.a.com , a.com ,,") == {"www.a.com", "a.com"}
    assert request_host({"HTTP_HOST": "Bo.Example.com:8080"}) == "bo.example.com"
    assert strip_backoffice_prefix("/backoffice") == "/"
    assert strip_backoffice_prefix("/backoffice/login") == "/login"
    assert strip_backoffice_prefix("/backoffice2") is None


def test_backoffice_host_serves_backoffice_at_root_with_noindex():
    response = get(client(), "bo.example.com", "/login")
    assert response.get_data(as_text=True) == "bo:|/login"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"


def test_backoffice_host_never_serves_member_site():
    assert get(client(), "bo.example.com", "/lottery/rooms").get_data(as_text=True).startswith("bo:")


def test_old_prefix_on_backoffice_host_redirects_to_root():
    response = get(client(), "bo.example.com", "/backoffice/dashboard?x=1")
    assert response.status_code == 302
    assert response.headers["Location"] == "https://bo.example.com/dashboard?x=1"


def test_main_host_serves_member_site_only():
    response = get(client(), "www.example.com", "/lottery/rooms")
    assert response.get_data(as_text=True) == "main:|/lottery/rooms"


def test_main_host_forwards_backoffice_path_to_backoffice_host():
    response = get(client(), "www.example.com", "/backoffice/login")
    assert response.status_code == 302
    assert response.headers["Location"] == "https://bo.example.com/login"


def test_main_host_without_backoffice_host_hides_backoffice():
    response = get(client(backoffice_host=""), "www.example.com", "/backoffice/login")
    assert response.status_code == 404


def test_other_hosts_keep_legacy_layout():
    c = client()
    assert get(c, "web-production-491f2.up.railway.app", "/").get_data(as_text=True) == "main:|/"
    legacy = get(c, "web-production-491f2.up.railway.app", "/backoffice/login").get_data(as_text=True)
    assert legacy == "bo:/backoffice|/login"


def test_unset_env_uses_plain_mount(monkeypatch):
    monkeypatch.delenv("MAIN_HOSTS", raising=False)
    monkeypatch.delenv("MAIN_HOST", raising=False)
    monkeypatch.delenv("BACKOFFICE_HOST", raising=False)
    application = build_application(make_app("main"), make_app("bo"))
    assert not isinstance(application, HostDispatcher)
    assert get(Client(application), "anything.example.com", "/backoffice/x").get_data(as_text=True) == "bo:/backoffice|/x"


def test_env_builds_host_dispatcher(monkeypatch):
    monkeypatch.setenv("MAIN_HOSTS", "www.example.com,example.com")
    monkeypatch.setenv("BACKOFFICE_HOST", "bo.example.com")
    application = build_application(make_app("main"), make_app("bo"))
    assert isinstance(application, HostDispatcher)
    assert get(Client(application), "example.com", "/").get_data(as_text=True).startswith("main:")
