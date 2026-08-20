"""A form-login gate in front of Flower.

Flower's own `--basic_auth` produces the browser's native credential dialog.
That interacts badly with its dashboard, which refreshes on a timer: every
refresh re-issues the unauthenticated request, and the dialog is dismissed and
redrawn under whoever is typing into it.

So Flower runs unauthenticated on the internal network and is not published to
the host at all. This service is the only way in: it serves a real login form,
sets a signed session cookie, and reverse-proxies everything else through.

Flower's UI is plain HTTP - it polls over AJAX and registers no websocket
handlers - so a straightforward request/response proxy is sufficient.
"""

from __future__ import annotations

import hmac
import logging
import os
from html import escape
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import quote

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

FLOWER_URL = os.environ.get("FLOWER_URL", "http://flower:5555").rstrip("/")
FLOWER_USER = os.environ.get("FLOWER_USER", "admin")
FLOWER_PASSWORD = os.environ.get("FLOWER_PASSWORD", "admin")
SESSION_SECRET = os.environ.get("FLOWER_SESSION_SECRET", "dev-secret-change-me")
SESSION_MAX_AGE = int(os.environ.get("FLOWER_SESSION_MAX_AGE", 8 * 60 * 60))

#: Set by the proxy, meaningless to Flower, and not ours to forward onward.
_STRIPPED_REQUEST_HEADERS = {"host", "connection", "cookie", "content-length"}
#: Hop-by-hop headers, plus ones httpx has already decoded for us.
_STRIPPED_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-encoding",
    "content-length",
}

_LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flower &middot; DocFlow</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #0b0d10; color: #e6e8eb;
    font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .card {
    width: min(92vw, 380px); padding: 32px;
    background: #14171c; border: 1px solid #23272e; border-radius: 12px;
  }
  h1 { margin: 0 0 4px; font-size: 20px; letter-spacing: -0.01em; }
  p.sub { margin: 0 0 24px; color: #8b929c; font-size: 13px; }
  label {
    display: block; margin-bottom: 6px;
    font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: #8b929c;
  }
  input {
    width: 100%; padding: 10px 12px; margin-bottom: 16px;
    background: #0e1116; color: #e6e8eb;
    border: 1px solid #2b3038; border-radius: 8px; font-size: 14px;
  }
  input:focus { outline: 2px solid #3b82f6; outline-offset: -1px; border-color: transparent; }
  button {
    width: 100%; padding: 10px 12px; border: 0; border-radius: 8px;
    background: #3b82f6; color: #fff; font-size: 14px; font-weight: 600; cursor: pointer;
  }
  button:hover { background: #2f6fd8; }
  .error {
    margin: 0 0 16px; padding: 9px 12px; border-radius: 8px; font-size: 13px;
    background: #2a1416; border: 1px solid #5b2126; color: #f2b8b5;
  }
  .foot { margin: 20px 0 0; font-size: 12px; color: #6c737d; }
</style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <h1>Flower</h1>
    <p class="sub">Worker internals for DocFlow</p>
    __ERROR__
    <input type="hidden" name="next" value="__NEXT__">
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
    <p class="foot">Flower can revoke and terminate tasks. Treat it as an admin surface.</p>
  </form>
</body>
</html>
"""


def _render_login(next_path: str, error: str = "") -> str:
    # Simple token substitution rather than str.format: the page is mostly CSS,
    # and every brace in it would otherwise need doubling.
    #
    # next_path is escaped because it lands in an HTML attribute. _safe_next
    # already restricts it to a relative path, but escaping is what actually
    # stops a crafted ?next= from breaking out of the value.
    return _LOGIN_PAGE.replace("__ERROR__", error).replace(
        "__NEXT__", escape(next_path, quote=True)
    )


def _safe_next(raw: str | None) -> str:
    """Only allow same-site relative paths, so ?next= cannot become an open redirect."""
    if (
        not raw
        or not raw.startswith("/")
        or raw.startswith("//")
        or raw.startswith("/login")
    ):
        return "/"
    return raw


def _credentials_match(username: str, password: str) -> bool:
    # compare_digest on both halves so neither is short-circuited on length.
    return hmac.compare_digest(username, FLOWER_USER) and hmac.compare_digest(
        password, FLOWER_PASSWORD
    )


async def login(request: Request) -> Response:
    if request.method == "GET":
        target = _safe_next(request.query_params.get("next"))
        if request.session.get("user"):
            return RedirectResponse(target, status_code=303)
        return HTMLResponse(_render_login(target))

    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    target = _safe_next(str(form.get("next", "/")))

    if not _credentials_match(username, password):
        logger.warning("Rejected Flower login for username %r", username)
        return HTMLResponse(
            _render_login(target, '<p class="error">Incorrect username or password.</p>'),
            status_code=401,
        )

    request.session["user"] = username
    # 303 so the browser re-issues as GET and a refresh does not repost the form.
    return RedirectResponse(target, status_code=303)


async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


async def proxy(request: Request) -> Response:
    if not request.session.get("user"):
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        # Encoded, or Flower's own query string would be parsed as extra
        # parameters of /login and silently truncate the return path.
        return RedirectResponse(
            f"/login?next={quote(target, safe='')}", status_code=303
        )

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _STRIPPED_REQUEST_HEADERS
    }

    client: httpx.AsyncClient = request.app.state.client
    upstream = await client.request(
        method=request.method,
        url=f"{FLOWER_URL}{request.url.path}",
        params=request.url.query or None,
        headers=headers,
        content=await request.body(),
    )

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in _STRIPPED_RESPONSE_HEADERS
        },
    )


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    # One client for the process, so connections to Flower are pooled rather
    # than reopened per proxied request. follow_redirects stays off: Flower's
    # redirects are relative and belong to the browser, not to us.
    app.state.client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    if SESSION_SECRET == "dev-secret-change-me":
        logger.warning(
            "FLOWER_SESSION_SECRET is unset, so sessions are signed with a "
            "known default. Set it before exposing this beyond localhost."
        )
    try:
        yield
    finally:
        await app.state.client.aclose()


app = Starlette(
    routes=[
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["GET", "POST"]),
        Route(
            "/{path:path}",
            proxy,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        ),
    ],
    middleware=[
        Middleware(
            SessionMiddleware,
            secret_key=SESSION_SECRET,
            session_cookie="flower_session",
            max_age=SESSION_MAX_AGE,
            same_site="lax",
            https_only=False,
        )
    ],
    lifespan=lifespan,
)
