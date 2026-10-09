# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request, Response

COOKIE_NAME = "huiyiku_session"


def cookie_value(access_token: str) -> str:
    return hmac.new(b"huiyiku-session", access_token.encode("utf-8"), hashlib.sha256).hexdigest()


def tokens_match(expected: str, provided: str) -> bool:
    if not expected or not provided:
        return False
    exp = expected.encode("utf-8")
    got = provided.encode("utf-8")
    if len(exp) != len(got):
        # compare_digest requires equal length; still do a dummy compare
        hmac.compare_digest(exp, exp)
        return False
    return hmac.compare_digest(exp, got)


def set_session_cookie(response: Response, access_token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        cookie_value(access_token),
        httponly=True,
        samesite="strict",
        path="/",
        # 无 max_age 是会话 Cookie，WebView2/浏览器退出即丢，令牌要每次启动重输
        max_age=30 * 24 * 3600,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def authorized(request: Request, access_token: str) -> bool:
    if not access_token:
        return True
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
        if tokens_match(access_token, provided):
            return True
    cookie = request.cookies.get(COOKIE_NAME) or ""
    return tokens_match(cookie_value(access_token), cookie)


PUBLIC_PREFIXES = (
    "/api/health",
    "/api/session",
)


def require_auth(request: Request, access_token: str) -> None:
    path = request.url.path
    if path in {"/api/health"} or path.startswith("/assets") or path == "/":
        return
    if path == "/api/session" and request.method in {"POST", "DELETE"}:
        return
    if path.startswith("/api/") or "/audio" in path or path.startswith("/api/export"):
        if not authorized(request, access_token):
            raise HTTPException(status_code=401, detail="unauthorized")
