from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError


async def handle_unexpected_error(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request, error
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "An unexpected error occurred.",
            }
        },
    )


async def handle_http_error(request: Request, error: HTTPException) -> JSONResponse:
    del request
    detail = error.detail if isinstance(error.detail, dict) else {}
    return JSONResponse(
        status_code=error.status_code,
        headers=error.headers,
        content={"error": {
            "code": detail.get("code", "request_failed"),
            "message": detail.get("message", "The request could not be completed."),
        }},
    )


async def handle_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    del request, error
    return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "Request fields or upload metadata are invalid."}})
