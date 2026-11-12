"""Relational workspace API/UI. Run migrations and a separate worker first."""
import argparse
import os
import secrets
import uuid
from fastapi import FastAPI
from fastapi.responses import RedirectResponse, JSONResponse
from tollgate.platform_api import install
from tollgate.services.platform import Platform
from tollgate.settings import Settings
from tollgate.storage.database import Database


def create_app(settings=None, database=None, local_token=None):
    settings = settings or Settings.from_env()
    if settings.mode == "hosted" and local_token:
        raise ValueError("Hosted mode prohibits local operator credentials")
    database = database or Database(settings.database_url)
    if not database.ready():
        raise RuntimeError("Run python -m alembic upgrade head before starting the workspace API")
    token = (local_token or os.environ.get("TOLLGATE_OPERATOR_TOKEN") or secrets.token_urlsafe(32)) if settings.mode == "local" else None
    app = FastAPI(title="Tollgate workspace sandbox", docs_url=None, redoc_url=None)
    app.state.local_token = token
    platform = Platform(database, settings, token)
    install(app, platform)

    @app.middleware("http")
    async def boundaries(request, call_next):
        from tollgate.observability import correlation_id, operational_event
        correlation = uuid.uuid4().hex
        correlation_id.set(correlation)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            local_origin = settings.mode == "local" and request.url.hostname in {"127.0.0.1", "localhost", "::1"} and origin == str(request.base_url).rstrip("/")
            if origin and origin != settings.public_origin and not local_origin:
                return JSONResponse({"error": {"code": "origin_denied"}}, status_code=403)
            size = 0
            # Bound chunked requests as well as Content-Length.
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_request_bytes:
                    return JSONResponse({"error": {"code": "request_too_large"}}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        try:
            response = await call_next(request)
        except Exception:
            return JSONResponse({"error": {"code": "internal_error", "correlation_id": correlation}}, status_code=500)
        route = request.scope.get("route")
        operational_event("http_request", method=request.method, route=getattr(route,"path","unmatched"), status=response.status_code)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "X-Correlation-ID": correlation,
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @app.get("/")
    def root():
        return RedirectResponse("/workspace")

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    settings = Settings.from_env()
    if settings.mode == "local" and args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Shared local credentials require a loopback binding; use hosted identity for a public listener")
    app = create_app(settings)
    if app.state.local_token:
        print(f"Open http://127.0.0.1:{args.port}/workspace#token={app.state.local_token}", flush=True)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
