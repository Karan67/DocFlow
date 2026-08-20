"""Phase 6: the form-login gate in front of Flower.

Run inside the worker container:  docker compose exec worker pytest

Flower's own `--basic_auth` opens the browser's native credential dialog, and
Flower's dashboard refreshes on a timer - so the dialog gets dismissed and
redrawn while you are still typing into it. This service replaces it with a
real form and a signed session cookie.

It is an auth boundary in front of a surface that can revoke and terminate
tasks, so the redirect handling is tested as carefully as the login itself.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

from flowerauth.main import _credentials_match, _safe_next, app


@pytest.fixture
def client():
    # raise_server_exceptions keeps the proxy's own errors visible rather than
    # being turned into opaque 500s.
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def stub_flower(client):
    """Answer proxied requests without a real Flower behind them."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="flower says hello")

    client.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


def _login(client) -> None:
    response = client.post(
        "/login", data={"username": "admin", "password": "admin", "next": "/"}
    )
    assert response.status_code == 303


# --------------------------------------------------------------------------
# redirect targets
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "//evil.example",           # protocol-relative: browsers treat it as absolute
        "https://evil.example",
        "http://evil.example",
        "evil.example",
        None,
        "",
    ],
)
def test_next_cannot_become_an_open_redirect(hostile):
    """?next= is attacker-supplied, and this app sits in front of an admin UI."""
    assert _safe_next(hostile) == "/"


def test_next_does_not_bounce_back_to_the_login_page():
    assert _safe_next("/login") == "/"
    assert _safe_next("/login?next=/login") == "/"


def test_next_keeps_a_genuine_relative_path():
    assert _safe_next("/workers?json=1") == "/workers?json=1"


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------


def test_credentials_must_match_both_halves():
    assert _credentials_match("admin", "admin")
    assert not _credentials_match("admin", "wrong")
    assert not _credentials_match("wrong", "admin")
    assert not _credentials_match("", "")


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


def test_unauthenticated_request_is_sent_to_the_login_form(client):
    response = client.get("/workers")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_the_original_path_survives_the_round_trip(client):
    """Flower's own URLs carry query strings; losing them lands you on / instead."""
    response = client.get("/workers", params={"json": "1"})

    # Encoded, so Flower's query string is not parsed as parameters of /login.
    assert response.headers["location"] == "/login?next=%2Fworkers%3Fjson%3D1"


def test_login_page_renders_a_form(client):
    body = client.get("/login").text

    assert 'name="username"' in body
    assert 'name="password"' in body
    assert 'type="password"' in body


def test_hostile_next_is_escaped_into_the_form(client):
    """The value is echoed into an HTML attribute, so escaping is what matters."""
    body = client.get("/login", params={"next": '/x" onload="alert(1)'}).text

    assert 'onload="alert(1)' not in body


def test_wrong_password_is_rejected_without_a_session(client):
    response = client.post("/login", data={"username": "admin", "password": "nope"})

    assert response.status_code == 401
    assert "flower_session" not in response.cookies


def test_correct_password_starts_a_session(client):
    response = client.post("/login", data={"username": "admin", "password": "admin"})

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.cookies.get("flower_session")


def test_authenticated_requests_reach_flower(client, stub_flower):
    _login(client)

    response = client.get("/workers", params={"json": "1"})

    assert response.status_code == 200
    assert response.text == "flower says hello"
    assert str(stub_flower[0].url).endswith("/workers?json=1"), "query is forwarded"


def test_the_session_cookie_is_not_forwarded_to_flower(client, stub_flower):
    """Flower has no use for it, and it should not travel further than it must."""
    _login(client)

    client.get("/workers")

    assert "cookie" not in {k.lower() for k in stub_flower[0].headers}


def test_logout_ends_the_session(client, stub_flower):
    _login(client)

    response = client.get("/logout")
    assert response.status_code == 303

    # Back to being challenged.
    assert client.get("/workers").status_code == 303
