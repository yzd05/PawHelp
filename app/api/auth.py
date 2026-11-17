"""Shared operator authorization for local same-origin pages and admin tokens."""

from collections.abc import Callable
import hmac
from ipaddress import ip_address
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request


def local_page_request(request: Request) -> bool:
    """Trust neither forwarded addresses nor a Host resolved through DNS."""

    try:
        if request.client is None or not ip_address(request.client.host).is_loopback:
            return False
        if any(
            name in request.headers
            for name in (
                "forwarded",
                "x-forwarded-for",
                "x-forwarded-host",
                "x-forwarded-proto",
            )
        ):
            return False
        host = urlsplit("//" + request.headers.get("host", ""))
        if (
            host.hostname not in {"localhost", "127.0.0.1", "::1"}
            or host.username
            or host.password
            or host.path
        ):
            return False
        port = host.port or (443 if request.url.scheme == "https" else 80)
        origin = request.headers.get("origin")
        if origin is not None:
            parsed = urlsplit(origin)
            if (
                parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                return False
            origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if (parsed.scheme, parsed.hostname, origin_port) != (
                request.url.scheme,
                host.hostname,
                port,
            ):
                return False
        if request.headers.get("sec-fetch-site", "none") not in {"same-origin", "none"}:
            return False
        return True
    except ValueError:
        return False


_local_page_request = local_page_request


def bind_admin_settings(settings: object | None) -> Callable[[Request], None]:
    """Attach router-scoped settings so standalone router mounts resolve ADMIN_TOKEN."""

    def _bind(request: Request) -> None:
        if settings is not None:
            request.state.admin_settings = settings

    return _bind


def _expected_admin_token(request: Request) -> str:
    settings = getattr(request.state, "admin_settings", None)
    if settings is None:
        settings = getattr(request.app.state, "settings", None)
    secret = getattr(settings, "admin_token", None) if settings is not None else None
    if secret is None:
        return ""
    if hasattr(secret, "get_secret_value"):
        return str(secret.get_secret_value())
    return str(secret)


def has_admin_credentials(request: Request, x_admin_token: str | None = None) -> bool:
    """Return True when the caller explicitly presents local-admin or token headers."""

    return bool(x_admin_token) or request.headers.get("x-pawhelp-local-admin") == "1"


def authorize(
    request: Request,
    x_admin_token: Annotated[str | None, Header()] = None,
) -> None:
    """Require either verified loopback same-origin admin access or ADMIN_TOKEN."""

    expected = _expected_admin_token(request)
    if x_admin_token is not None:
        if not expected:
            raise HTTPException(status_code=503, detail="未配置 ADMIN_TOKEN，后台接口未开放")
        if not x_admin_token or not hmac.compare_digest(
            x_admin_token.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="管理令牌无效")
        return
    if request.headers.get("x-pawhelp-local-admin") == "1":
        if not local_page_request(request):
            raise HTTPException(
                status_code=403,
                detail="后台免令牌仅支持本机同源访问，请使用 localhost 或 127.0.0.1 打开页面",
            )
        return
    if not expected:
        raise HTTPException(status_code=503, detail="未配置 ADMIN_TOKEN，后台接口未开放")
    raise HTTPException(status_code=401, detail="管理令牌无效")
