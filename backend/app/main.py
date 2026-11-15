import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import handle_http_error, handle_unexpected_error, handle_validation_error
from app.api.routes.health import router as health_router
from app.api.routes.projects import router as projects_router
from app.api.routes.transcriptions import router as transcriptions_router
from app.api.routes.translations import router as translations_router
from app.core.config import get_settings


settings = get_settings()

app = FastAPI(title=settings.app_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    allow_headers=["*", "Tus-Resumable", "Upload-Length", "Upload-Offset", "Upload-Metadata", "Content-Type"],
    expose_headers=["Location", "Tus-Resumable", "Tus-Version", "Tus-Extension", "Upload-Offset", "Upload-Length"],
)
app.add_exception_handler(Exception, handle_unexpected_error)
app.add_exception_handler(HTTPException, handle_http_error)
app.add_exception_handler(RequestValidationError, handle_validation_error)
app.include_router(health_router)
app.include_router(projects_router, prefix="/api/v1")
app.include_router(transcriptions_router, prefix="/api/v1")
app.include_router(translations_router, prefix="/api/v1")


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=True,
    )
