"""Autenticación JWT (Modelo B: cliente único).

- create_access_token: emite un JWT HS256 firmado con caducidad.
- require_jwt: dependencia FastAPI que extrae el Bearer del header,
  valida la firma y la caducidad, y devuelve los claims.
"""
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

# auto_error=True hace que FastAPI devuelva 403 si falta el header Authorization,
# lo que también deja la marca correcta en el OpenAPI/Swagger para "Authorize".
_bearer_scheme = HTTPBearer(auto_error=True, description="JWT obtenido en /auth/login")


def create_access_token(extra_claims: dict | None = None) -> tuple[str, int]:
    """Emite un JWT firmado con la caducidad configurada.

    Devuelve (token, segundos_hasta_expirar).
    """
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=settings.jwt_expires_hours)
    payload: dict = {
        "iss": settings.app_name,       # emisor
        "iat": int(now.timestamp()),    # emitido
        "exp": int(expires.timestamp()),# expira
        "sub": "client",                # un solo cliente (Modelo B)
    }
    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, settings.jwt_expires_hours * 3600


async def require_jwt(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> dict:
    """Valida el Bearer JWT del header. Devuelve los claims o levanta 401.

    Errores cubiertos: token mal firmado, expirado, malformado, esquema distinto
    de Bearer. Todos responden 401 con WWW-Authenticate: Bearer (estándar).
    """
    if credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication scheme must be Bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        # Cubre firma inválida, payload corrupto, alg no permitido, etc.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims