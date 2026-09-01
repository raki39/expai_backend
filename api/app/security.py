"""Autenticacao do servico `api`.

A `api` nao tem dominio publico (ADR 0007), mas "nao ter dominio" nao e
autenticacao. Todo endpoint exige o token de servico que o `web` apresenta.

O token vive apenas no servidor Next.js e **nunca chega ao navegador**.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from .settings import Settings, get_settings


async def exigir_token_de_servico(
    authorization: str | None = Header(default=None),
) -> None:
    """Valida `Authorization: Bearer <API_SERVICE_TOKEN>`.

    Sem excecoes: `/api/health` tambem exige. Um endpoint aberto e um
    endpoint aberto, mesmo que so devolva metadados.
    """
    settings: Settings = get_settings()
    esperado = settings.api_service_token.get_secret_value()

    if not esperado:
        # Local sem token configurado. Falha fechado, e nao aberto.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API_SERVICE_TOKEN nao configurado no servidor",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="credencial ausente",
            headers={"WWW-Authenticate": "Bearer"},
        )

    recebido = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(recebido, esperado):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="credencial invalida",
            headers={"WWW-Authenticate": "Bearer"},
        )
