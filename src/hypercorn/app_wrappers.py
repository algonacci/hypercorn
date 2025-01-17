from __future__ import annotations

from io import BytesIO
from typing import Callable, List, Optional, Tuple

from .typing import (
    ASGIFramework,
    ASGIReceiveCallable,
    ASGISendCallable,
    HTTPScope,
    Scope,
    WSGIFramework,
)


class InvalidPathError(Exception):
    pass


class ASGIWrapper:
    def __init__(self, app: ASGIFramework) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
        sync_spawn: Callable,
    ) -> None:
        await self.app(scope, receive, send)


class WSGIWrapper:
    def __init__(self, app: WSGIFramework, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(
        self,
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
        sync_spawn: Callable,
    ) -> None:
        if scope["type"] == "http":
            status_code, headers, body = await self.handle_http(scope, receive, send, sync_spawn)
            await send({"type": "http.response.start", "status": status_code, "headers": headers})  # type: ignore # noqa: E501
            await send({"type": "http.response.body", "body": body})  # type: ignore
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close"})  # type: ignore
        elif scope["type"] == "lifespan":
            return
        else:
            raise Exception(f"Unknown scope type, {scope['type']}")

    async def handle_http(
        self,
        scope: HTTPScope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
        sync_spawn: Callable,
    ) -> Tuple[int, list, bytes]:
        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))  # type: ignore
            if len(body) > self.max_body_size:
                return 400, [], b""
            if not message.get("more_body"):
                break

        try:
            environ = _build_environ(scope, body)
        except InvalidPathError:
            return 404, [], b""
        else:
            return await sync_spawn(self.run_app, environ)

    def run_app(self, environ: dict) -> Tuple[int, list, bytes]:
        headers: List[Tuple[bytes, bytes]]
        status_code: Optional[int] = None

        def start_response(
            status: str,
            response_headers: List[Tuple[str, str]],
            exc_info: Optional[Exception] = None,
        ) -> None:
            nonlocal headers, status_code

            raw, _ = status.split(" ", 1)
            status_code = int(raw)
            headers = [
                (name.lower().encode("ascii"), value.encode("ascii"))
                for name, value in response_headers
            ]

        body = bytearray()
        for output in self.app(environ, start_response):
            body.extend(output)
        return status_code, headers, body


def _build_environ(scope: HTTPScope, body: bytes) -> dict:
    server = scope.get("server") or ("localhost", 80)
    path = scope["path"]
    script_name = scope.get("root_path", "")
    if path.startswith(script_name):
        path = path[len(script_name) :]
        path = path if path != "" else "/"
    else:
        raise InvalidPathError()

    environ = {
        "REQUEST_METHOD": scope["method"],
        "SCRIPT_NAME": script_name.encode("utf8").decode("latin1"),
        "PATH_INFO": path.encode("utf8").decode("latin1"),
        "QUERY_STRING": scope["query_string"].decode("ascii"),
        "SERVER_NAME": server[0],
        "SERVER_PORT": server[1],
        "SERVER_PROTOCOL": "HTTP/%s" % scope["http_version"],
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scope.get("scheme", "http"),
        "wsgi.input": BytesIO(body),
        "wsgi.errors": BytesIO(),
        "wsgi.multithread": True,
        "wsgi.multiprocess": True,
        "wsgi.run_once": False,
    }

    if "client" in scope:
        environ["REMOTE_ADDR"] = scope["client"][0]

    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin1")
        if name == "content-length":
            corrected_name = "CONTENT_LENGTH"
        elif name == "content-type":
            corrected_name = "CONTENT_TYPE"
        else:
            corrected_name = "HTTP_%s" % name.upper().replace("-", "_")
        # HTTPbis say only ASCII chars are allowed in headers, but we latin1 just in case
        value = raw_value.decode("latin1")
        if corrected_name in environ:
            value = environ[corrected_name] + "," + value  # type: ignore
        environ[corrected_name] = value
    return environ
