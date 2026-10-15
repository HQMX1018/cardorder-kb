"""卡序问答库入口：uvicorn main:app --app-dir server --host 0.0.0.0 --port 8090"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import kb_queue
import store
import web
from mcp_kb import mcp


@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():   # streamable-http 必须在 lifespan 内
        yield


store.init()
app = FastAPI(title="卡序问答库", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")
app.mount("/mcp", mcp.streamable_http_app())


@app.middleware("http")
async def login_guard(request: Request, call_next):
    path = request.url.path
    if not path.startswith(web.PUBLIC) and not path.startswith(("/api/entries/search",)) \
            and store.get_user(request.cookies.get("uid")) is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "未登录"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=303)
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith(("/api/", "/mcp")):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    resp = web.page(request, "error.html", code=exc.status_code, detail=exc.detail)
    resp.status_code = exc.status_code
    return resp


app.include_router(kb_queue.router)
app.include_router(web.router)
