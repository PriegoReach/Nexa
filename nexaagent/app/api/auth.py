"""Endpoint de autenticación. Modelo B (cliente único)."""
import secrets

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.security import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # segundos


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest) -> TokenResponse:
    """Intercambia AUTH_PASSWORD por un JWT firmado.

    compare_digest evita timing attacks (la comparación tarda lo mismo
    sea cual sea el prefijo coincidente). Sin esto, un atacante podría
    inferir bytes de la contraseña midiendo tiempos.
    """
    if not secrets.compare_digest(payload.password, settings.auth_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, expires_in = create_access_token()
    return TokenResponse(access_token=token, expires_in=expires_in)