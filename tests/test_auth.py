"""Auth layer: login flow, next sanitization, HX 401s, CSRF, tokens, rate limit."""
import time

import pytest

import security
from tests.conftest import TEST_PASSWORD, login


# ── Disabled mode (no portal password) ───────────────────────────────────────

class TestAuthDisabled:
    def test_pages_open_without_session(self, client, mock_central, stub_db):
        assert client.get("/").status_code == 200

    def test_login_page_redirects_home(self, client):
        r = client.get("/login")
        assert r.status_code == 303
        assert r.headers["location"] == "/"

    def test_login_post_redirects_home(self, client):
        r = client.post("/login", data={"password": "anything"})
        assert r.status_code == 303
        assert r.headers["location"] == "/"

    def test_whoami_reports_disabled(self, client):
        body = client.get("/auth/whoami").json()
        assert body == {"authenticated": True, "auth_disabled": True}

    def test_verify_password_always_false_when_disabled(self, client):
        assert security.verify_password("anything") is False


# ── Login flow (auth enabled) ────────────────────────────────────────────────

class TestLoginFlow:
    def test_unauthenticated_page_redirects_to_login_with_next(self, auth_client):
        r = auth_client.get("/devices/?page=2")
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=%2Fdevices%2F%3Fpage%3D2"

    def test_login_page_renders_form(self, auth_client):
        r = auth_client.get("/login")
        assert r.status_code == 200
        assert '<form method="post" action="/login"' in r.text

    def test_wrong_password_returns_401_with_error(self, auth_client):
        r = login(auth_client, password="wrong")
        assert r.status_code == 401
        assert "Invalid password" in r.text
        assert "set-cookie" not in r.headers

    def test_correct_password_sets_cookie_and_redirects(self, auth_client,
                                                        mock_central, stub_db):
        r = login(auth_client, next_path="/devices/")
        assert r.status_code == 303
        assert r.headers["location"] == "/devices/"
        assert security.SESSION_COOKIE in auth_client.cookies
        # Session now grants access.
        assert auth_client.get("/devices/").status_code == 200

    def test_login_page_redirects_home_when_already_authenticated(self, auth_client):
        login(auth_client)
        r = auth_client.get("/login")
        assert r.status_code == 303
        assert r.headers["location"] == "/"

    def test_logout_clears_session(self, auth_client):
        login(auth_client)
        r = auth_client.post("/logout", headers={"origin": "http://testserver"})
        assert r.status_code == 303
        assert r.headers["location"] == "/login"
        # Cookie deleted -> back to redirecting.
        assert auth_client.get("/devices/").status_code == 303

    def test_whoami_enabled(self, auth_client):
        assert auth_client.get("/auth/whoami").json() == {
            "authenticated": False, "auth_disabled": False}
        login(auth_client)
        assert auth_client.get("/auth/whoami").json() == {
            "authenticated": True, "auth_disabled": False}

    def test_exempt_paths_do_not_require_auth(self, auth_client, stub_db):
        assert auth_client.get("/health").status_code == 200
        assert auth_client.get("/healthz").status_code == 200
        assert auth_client.get("/auth/whoami").status_code == 200


# ── next= sanitization ───────────────────────────────────────────────────────

class TestNextSanitization:
    @pytest.mark.parametrize("evil", [
        "//evil.com", "//evil.com/path", "http://evil.com", "https://evil.com",
        "javascript:alert(1)", "\\\\evil.com", "/ok\r\nSet-Cookie: x=1",
        "/ok\\..", "", None, 123,
    ])
    def test_sanitize_next_collapses_to_root(self, evil):
        assert security.sanitize_next(evil) == "/"

    @pytest.mark.parametrize("ok", ["/", "/devices/", "/devices/X?page=2&per_page=10"])
    def test_sanitize_next_allows_local_paths(self, ok):
        assert security.sanitize_next(ok) == ok

    def test_login_post_ignores_offsite_next(self, auth_client):
        r = login(auth_client, next_path="//evil.com")
        assert r.status_code == 303
        assert r.headers["location"] == "/"

    def test_login_post_keeps_local_next(self, auth_client):
        r = login(auth_client, next_path="/clients/")
        assert r.headers["location"] == "/clients/"

    def test_login_page_sanitizes_next_param(self, auth_client):
        r = auth_client.get("/login?next=http://evil.com")
        assert r.status_code == 200
        assert "http://evil.com" not in r.text


# ── HTMX / API callers ───────────────────────────────────────────────────────

class TestJsonCallers:
    def test_hx_request_gets_401_json_with_redirect_header(self, auth_client):
        r = auth_client.get("/devices/", headers={"HX-Request": "true"})
        assert r.status_code == 401
        assert r.headers["HX-Redirect"] == "/login"
        assert r.json() == {"ok": False, "error": "Authentication required"}

    def test_json_accept_gets_401_json(self, auth_client):
        r = auth_client.get("/devices/", headers={"accept": "application/json"})
        assert r.status_code == 401
        assert r.json()["ok"] is False


# ── CSRF (Origin / Referer same-host check) ──────────────────────────────────

class TestCsrf:
    def test_cross_origin_post_rejected(self, auth_client):
        login(auth_client)
        r = auth_client.post("/logout", headers={"origin": "http://evil.com"})
        assert r.status_code == 403

    def test_null_origin_rejected(self, auth_client):
        login(auth_client)
        r = auth_client.post("/logout", headers={"origin": "null"})
        assert r.status_code == 403

    def test_matching_origin_allowed(self, auth_client):
        login(auth_client)
        r = auth_client.post("/logout", headers={"origin": "http://testserver"})
        assert r.status_code == 303

    def test_matching_referer_allowed_as_fallback(self, auth_client):
        login(auth_client)
        r = auth_client.post("/logout",
                             headers={"referer": "http://testserver/devices/"})
        assert r.status_code == 303

    def test_browser_without_origin_or_referer_rejected(self, auth_client):
        login(auth_client)
        r = auth_client.post("/logout",
                             headers={"user-agent": "Mozilla/5.0 (X11; Linux)"})
        assert r.status_code == 403

    def test_cross_origin_hx_post_gets_json_403(self, auth_client, bell_store):
        login(auth_client)
        r = auth_client.post("/notifications/api/mark-read",
                             headers={"origin": "http://evil.com",
                                      "HX-Request": "true"},
                             json={"all": True})
        assert r.status_code == 403
        assert r.json()["ok"] is False

    def test_non_browser_client_without_headers_allowed(self, auth_client):
        login(auth_client)
        # No Origin/Referer/UA hints: curl-style caller is let through.
        r = auth_client.post("/logout")
        assert r.status_code == 303

    def test_audit_recorded_for_state_change(self, auth_client):
        login(auth_client)
        auth_client.post("/logout", headers={"origin": "http://testserver"})
        assert ("POST", "/logout") in [(m, p) for m, p, _ in auth_client.audit_log]

    def test_bell_polling_not_audited(self, auth_client, bell_store):
        login(auth_client)
        auth_client.post("/notifications/api/mark-read",
                         headers={"origin": "http://testserver"},
                         json={"all": True})
        assert all(not p.startswith("/notifications/api/")
                   for _, p, _ in auth_client.audit_log)


# ── Session tokens ───────────────────────────────────────────────────────────

class TestSessionTokens:
    def test_round_trip(self, auth_client):
        token = security.create_session_token()
        assert security.verify_session_token(token) is True

    def test_tampered_signature_rejected(self, auth_client):
        token = security.create_session_token()
        head, sig = token.rsplit(".", 1)
        bad_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
        assert security.verify_session_token(f"{head}.{bad_sig}") is False

    def test_tampered_expiry_rejected(self, auth_client):
        token = security.create_session_token()
        v, exp, nonce, sig = token.split(".")
        assert security.verify_session_token(
            f"{v}.{int(exp) + 9999}.{nonce}.{sig}") is False

    def test_expired_token_rejected(self, auth_client):
        expired = int(time.time()) - 10
        payload = f"v1.{expired}.nonce123"
        token = f"{payload}.{security._sign(payload)}"
        assert security.verify_session_token(token) is False

    @pytest.mark.parametrize("garbage", [
        None, "", "v1", "v1.x.y", "v2.123.nonce.sig", "a.b.c.d",
        "v1.notanumber.nonce.sig", "x" * 600,
    ])
    def test_malformed_tokens_rejected(self, garbage, auth_client):
        assert security.verify_session_token(garbage) is False

    def test_tampered_cookie_redirects_to_login(self, auth_client):
        login(auth_client)
        good = auth_client.cookies[security.SESSION_COOKIE]
        auth_client.cookies.set(security.SESSION_COOKIE, good[:-2] + "zz")
        assert auth_client.get("/devices/").status_code == 303

    def test_expired_cookie_redirects_to_login(self, auth_client):
        expired = int(time.time()) - 5
        payload = f"v1.{expired}.nonce456"
        auth_client.cookies.set(security.SESSION_COOKIE,
                                f"{payload}.{security._sign(payload)}")
        assert auth_client.get("/devices/").status_code == 303


# ── Login rate limiting ──────────────────────────────────────────────────────

class TestRateLimit:
    def test_failed_attempts_eventually_429(self, auth_client):
        hdrs = {"x-forwarded-for": "203.0.113.77"}
        for _ in range(10):
            r = auth_client.post("/login",
                                 data={"password": "nope", "next": "/"}, headers=hdrs)
            assert r.status_code == 401
        r = auth_client.post("/login",
                             data={"password": "nope", "next": "/"}, headers=hdrs)
        assert r.status_code == 429

        # Correct password is also blocked while limited.
        r = auth_client.post(
            "/login", data={"password": TEST_PASSWORD, "next": "/"}, headers=hdrs)
        assert r.status_code == 429

        # ...but a different IP is unaffected.
        r = auth_client.post("/login", data={"password": TEST_PASSWORD, "next": "/"},
                             headers={"x-forwarded-for": "203.0.113.78"})
        assert r.status_code == 303

    def test_successful_login_resets_counter(self, auth_client):
        hdrs = {"x-forwarded-for": "203.0.113.99"}
        for _ in range(5):
            auth_client.post("/login", data={"password": "nope"}, headers=hdrs)
        assert login(auth_client).status_code == 303  # uses client IP, fine
        r = auth_client.post("/login", data={"password": TEST_PASSWORD, "next": "/"},
                             headers=hdrs)
        assert r.status_code == 303
        assert security.login_limiter.is_limited("203.0.113.99") is False

    def test_limiter_window_prunes(self):
        lim = security.LoginRateLimiter(max_attempts=2, window_seconds=60)
        lim.record_failure("1.2.3.4")
        lim.record_failure("1.2.3.4")
        assert lim.is_limited("1.2.3.4") is True
        # Age the entries out of the window.
        lim._attempts["1.2.3.4"] = [time.time() - 120, time.time() - 90]
        assert lim.is_limited("1.2.3.4") is False
