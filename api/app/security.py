"""Autenticacao do servico `api`.

A `api` nao tem dominio publico (ADR 0007), mas "nao ter dominio" nao e
autenticacao. Todo endpoint exige o token de servico que o `web` apresenta.

O token vive apenas no servidor Next.js e **nunca chega ao navegador**.

## Por que `HTTPBearer` e nao `Header` cru

Os dois validam a mesma coisa. A diferenca e que `HTTPBearer` e um **esquema
de seguranca**, e o FastAPI o publica no OpenAPI - que e o que faz o botao
`Authorize` existir no Swagger.

A primeira versao usava `Header(default=None)`. Funcionava, e os docs subiam
sem botao nenhum: dava para ler as rotas e nao dava para exercitar uma sequer,
que e o oposto do motivo pelo qual os docs foram ligados.

**`auto_error=False` de proposito.** Com `auto_error=True` o FastAPI responde
403 quando falta credencial, e perde as mensagens daqui. As duas importam ao
diagnosticar: `credencial ausente` significa que nenhum Bearer chegou, e
`credencial invalida` significa que chegou um e nao bateu - quase sempre o
`PANEL_TOKEN` no lugar do `API_SERVICE_TOKEN`.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .settings import Settings, get_settings

# `description` aparece na caixa do `Authorize`. E o unico lugar em que da
# para dizer QUAL dos dois tokens vai ali, para quem esta com o painel aberto
# do lado e os dois na frente.
_esquema = HTTPBearer(
    scheme_name="API_SERVICE_TOKEN",
    description=(
        "O token de SERVICO (`API_SERVICE_TOKEN`), nao o do painel"
        " (`PANEL_TOKEN`). Cole so o valor - o `Bearer ` e acrescentado pelo"
        " proprio Swagger."
    ),
    auto_error=False,
)


async def exigir_token_de_servico(
    credencial: HTTPAuthorizationCredentials | None = Depends(_esquema),
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

    if credencial is None or credencial.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="credencial ausente",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not secrets.compare_digest(credencial.credentials.strip(), esperado):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="credencial invalida",
            headers={"WWW-Authenticate": "Bearer"},
        )
