from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from .auth import active_session, LoginLimits, login, logout
from .errors import ApiError
from .storage import Storage
from .service import Service
from .api import router, json_body
from .run_api import router as run_router
from .runs import Runs
from fastapi.responses import JSONResponse
from .config import Settings
from .contracts import CONTRACT_VERSION, SCHEMA_SHA256
from .database import make_engine, probe_database
from .diagnostics import failure_message, write_failure


log = logging.getLogger(__name__)


def create_app(settings=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = make_engine(settings)
        app.state.service = Service(app.state.engine, Storage(settings.storage_root))
        app.state.runs = Runs(app.state.service, settings)
        await run_in_threadpool(app.state.service.reconcile_files)
        yield
        app.state.engine.dispose()

    app = FastAPI(title="AIST workspace", version="0.3.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.login_limits = LoginLimits()

    def error_response(request, error, stage, exception=None):
        if error.status >= 500:
            route = request.scope.get('route')
            operation = 'active_session' if stage == 'auth' else getattr(route, 'path', 'unmatched')
            request.state.api_failure = failure_message(
                'api_failure', stage, exception, operation=operation, request_id=error.body['request_id'],
                status=error.status, code=error.body['error']['code'], run_id=error.body['error']['run_id'])
        return JSONResponse(error.body,status_code=error.status)

    @app.middleware('http')
    async def secure_api(request: Request, call_next):
        stage = 'auth'
        try:
            if request.url.path.startswith('/v1/') and not (request.method == 'POST' and request.url.path == '/v1/auth/login'):
                headers = request.headers.getlist('authorization')
                session = await run_in_threadpool(active_session,app.state.engine,
                    headers[0] if len(headers)==1 else None)
                request.state.actor = session.user_id
                request.state.session_id = session.id
            stage = 'handler'
            response = await call_next(request)
        except ApiError as error:
            response = error_response(request, error, stage)
        except Exception as exception:
            response = error_response(request, ApiError('temporarily_unavailable'), stage, exception)
        write_failure(log, getattr(request.state, 'api_failure', None))
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.exception_handler(ApiError)
    async def api_error(request, error):
        return error_response(request, error, 'handler')

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        safe = ApiError('resource_not_found' if error.status_code==404 else 'validation_error')
        return JSONResponse(safe.body,status_code=safe.status)

    app.include_router(router)
    app.include_router(run_router)
    from .navigation import router as navigation_router
    app.include_router(navigation_router)

    @app.post('/v1/auth/login')
    async def password_login(request: Request):
        if request.query_params:
            raise ApiError('validation_error')
        payload = await json_body(request, 'LoginRequest')
        return await run_in_threadpool(login, app.state.engine, app.state.login_limits, payload)

    @app.post('/v1/auth/logout')
    async def session_logout(request: Request):
        if request.query_params:
            raise ApiError('validation_error')
        await json_body(request, 'LogoutRequest')
        return await run_in_threadpool(logout, app.state.engine, request.state.session_id)

    @app.get("/healthz")
    def health():
        return {"status": "alive", "service": "aist-backend", "contract_version": CONTRACT_VERSION,
                "schema_sha256": SCHEMA_SHA256, "live_models_enabled": settings.live_models_enabled}

    @app.get("/readyz")
    def ready():
        details = probe_database(app.state.engine)
        is_ready = details["reason"] is None
        return JSONResponse({"status": "ready" if is_ready else "not_ready",
                             "contract_version": CONTRACT_VERSION, "schema_sha256": SCHEMA_SHA256,
                             **details}, status_code=200 if is_ready else 503)

    return app


app = create_app()
