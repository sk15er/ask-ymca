from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from ask_ymca.api import router
from ask_ymca.config import get_settings

settings = get_settings()
WEB_INDEX = Path(__file__).parent / "static" / "index.html"

app = FastAPI(
    title="Ask YMCA API",
    description="Local-first university information assistant API.",
    version="0.1.0",
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )

app.include_router(router)


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(WEB_INDEX)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)
