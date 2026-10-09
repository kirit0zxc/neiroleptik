"""Вход оператора, восстановление CSRF после refresh и отзыв сессии."""

from fastapi import APIRouter, Request, Response

from honeyforge.api.dependencies import Configuration, Database, Reader
from honeyforge.api.schemas import AuthRead, LoginInput, UserRead
from honeyforge.auth.service import AuthService

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=AuthRead)
def login(
    payload: LoginInput, request: Request, response: Response, db: Database, settings: Configuration
) -> AuthRead:
    service = AuthService(db, settings)
    service.verify_origin(request.headers.get("origin"))
    ip = request.client.host if request.client else "unknown"
    user, token = service.login(payload.username, payload.password.get_secret_value(), ip)
    response.set_cookie(
        "hf_session",
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_hours * 3600,
        path="/",
    )
    return AuthRead(
        user=UserRead.model_validate(user), csrf_token=service.security.csrf_token(token)
    )


@router.get("/me", response_model=AuthRead)
def me(request: Request, user: Reader, db: Database, settings: Configuration) -> AuthRead:
    service = AuthService(db, settings)
    return AuthRead(
        user=UserRead.model_validate(user),
        csrf_token=service.security.csrf_token(request.cookies["hf_session"]),
    )


@router.post("/logout", status_code=204)
def logout(
    request: Request, response: Response, user: Reader, db: Database, settings: Configuration
) -> None:
    AuthService(db, settings).logout(request.cookies["hf_session"], user)
    response.delete_cookie(
        "hf_session", path="/", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )
