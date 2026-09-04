"""Camadas de configuracao que vivem em variavel de ambiente.

Sao duas, e apenas duas (ADR 0008):

- **Segredos**: chaves de LLM e token de servico. Nunca vao para o banco,
  nunca aparecem em log, em /api/health ou em pagina renderizada.
- **Bootstrap**: o que e necessario antes de o banco existir.

Todo o resto - mercado, timeframe, taxas, tetos operacionais, cambio, tiers,
tabela de precos, B1, B3 - vive no banco, versionado, em `config_version`.
A secao 10.2.3 do documento exige alteracao de configuracao "versionada no
ledger, com autor, data, valor anterior e novo", e variavel de ambiente nao
faz nada disso.
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Nomes dos campos que carregam segredo. A unica fonte desta lista.
# Usada pela redacao de log e pelo teste que garante que nada vaza.
SECRET_FIELDS = frozenset(
    {"api_service_token", "anthropic_api_key", "openai_api_key",
     "rele_hmac_secret"}
)


class Settings(BaseSettings):
    """Configuracao de ambiente. Imutavel apos a carga."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # ---------------------------------------------------------- bootstrap
    app_env: Literal["local", "railway"] = "local"
    port: int = 8000
    db_path: Path = Path("./var/fase0a.sqlite3")
    data_dir: Path = Path("./var/datasets")
    log_level: str = "INFO"

    # ----------------------------------------------------------- segredos
    api_service_token: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")

    # Segredo do HMAC do rele (ADR 0029, incremento 16). SEPARADO do
    # `api_service_token` de proposito: um deixa ler o painel, o outro deixa
    # ESCREVER no fluxo. Reusar o mesmo faria o comprometimento de um dar o
    # outro.
    rele_hmac_secret: SecretStr = SecretStr("")

    # Nao e segredo: e o identificador do workspace em que a chave age. Uma
    # chave ligada a identidade (e nao ao workspace) e recusada com 400 sem
    # ele. Fica fora de `SECRET_FIELDS` de proposito - redigir um id que nao
    # e credencial so atrapalharia o diagnostico -, mas tambem nao aparece em
    # `/api/health`: nada de identificar conta de terceiro numa pagina.
    anthropic_workspace_id: str = ""

    # ------------------------------------------------- docs interativos
    #
    # DESLIGADO por padrao, inclusive em producao. Ligado, o FastAPI serve
    # `/docs` e `/openapi.json` - e o Swagger UI busca o `openapi.json` **sem
    # autenticacao**, porque e assim que ele funciona. Logo, ligar expoe a
    # LISTA DE ROTAS a quem achar o dominio da api.
    #
    # Isso nao vaza dado nem segredo: toda rota continua exigindo o token de
    # servico, e a regra 15 proibe embutir o token na pagina, entao o
    # `Authorize` do Swagger e preenchido a mao por quem esta investigando.
    # O que se expoe e a superficie, e so enquanto a chave estiver ligada.
    #
    # Foi desligado no incremento 0 com o comentario "a superficie e
    # consumida pelo painel". Aquilo era verdade com 6 rotas; hoje sao mais de
    # 30, e nao ter como exercita-las e lacuna real. A saida nao e reabrir de
    # vez: e ter um interruptor que quem opera liga, usa e desliga.
    habilitar_docs: bool = False

    @field_validator("habilitar_docs", mode="before")
    @classmethod
    def _bool_tolerante(cls, v):
        """Aceita vazio como desligado, e aceita "sim"/"nao".

        Sem isto, `HABILITAR_DOCS=` derruba o BOOT com `ValidationError` -
        e era exatamente o que o `.env.example` shipava, com o campo vazio.
        Quem copiasse o arquivo nao subiria o servico, e a mensagem falaria
        de booleano invalido em vez de "voce copiou o exemplo".

        Uma variavel de ambiente vazia significa "nao defini". Tratar isso
        como erro fatal e a forma mais cara de ser rigoroso: derruba o
        servico inteiro por causa de uma linha que ninguem preencheu.

        E aceita `sim`/`nao` porque o projeto inteiro e escrito em portugues,
        e `HABILITAR_DOCS=sim` e o que qualquer um digitaria antes de ler a
        documentacao.
        """
        if v is None:
            return False
        if isinstance(v, str):
            limpo = v.strip().lower()
            if limpo == "":
                return False
            if limpo in ("sim", "s"):
                return True
            if limpo in ("nao", "não", "n"):
                return False
        return v

    # ------------------------------------------------------------- CORS
    # Lista separada por virgula. Vazio = nenhuma origem liberada.
    #
    # Guardado como str de proposito: pydantic-settings tenta interpretar
    # campo `list` no ambiente como JSON, e uma virgula simples quebraria.
    #
    # Com o padrao de proxy atual o navegador nao chama a api direto, entao
    # isto nao e exercitado. Existe para destravar chamada direta do browser
    # sem mudanca de codigo. NUNCA use "*": a api exige credencial, e curinga
    # com credencial e invalido no protocolo e desleixado na pratica.
    cors_allowed_origins: str = ""

    # ------------------------------------------------- limite inviolavel
    # Secao 12.1: "um teto definido em variavel de configuracao e um teto que
    # um bug, um prompt malicioso ou um agente criativo pode contornar".
    # Este e o limite externo; o teto operacional fica na config versionada e
    # nao pode exceder este valor.
    llm_max_usd_absolute: Decimal = Field(default=Decimal("5.00"), gt=0)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _check_producao(self) -> "Settings":
        # Rede de seguranca contra a falha mais cara possivel: rodar na Railway
        # com APP_ENV=local. Nesse caso o servico SOBE NORMALMENTE e grava em
        # ./var, no filesystem efemero - e a perda so aparece no redeploy
        # seguinte, quando o banco some sem aviso.
        #
        # A Railway injeta RAILWAY_ENVIRONMENT em todo deploy. Se ela existe e
        # APP_ENV nao e "railway", falhamos alto no boot.
        if os.getenv("RAILWAY_ENVIRONMENT") and self.app_env != "railway":
            raise ValueError(
                "detectada execucao na Railway (RAILWAY_ENVIRONMENT presente) "
                f"com APP_ENV={self.app_env!r}. Defina APP_ENV=railway: sem "
                "isso o banco seria gravado no filesystem efemero e perdido "
                "no proximo deploy."
            )

        if self.app_env != "railway":
            return self

        # Em producao o servico recusa subir sem o token: a `api` nao tem
        # dominio publico, mas "nao ter dominio" nao e autenticacao.
        if not self.api_service_token.get_secret_value():
            raise ValueError(
                "API_SERVICE_TOKEN e obrigatorio quando APP_ENV=railway"
            )

        # O volume e montado no START do container. Caminho relativo depende
        # do diretorio de trabalho e e a forma mais facil de gravar sem
        # querer no filesystem efemero.
        for nome, caminho in (("DB_PATH", self.db_path), ("DATA_DIR", self.data_dir)):
            if not caminho.is_absolute():
                raise ValueError(
                    f"{nome} precisa ser um caminho absoluto quando "
                    f"APP_ENV=railway (recebido: {caminho})"
                )

        return self

    @property
    def cors_origins(self) -> list[str]:
        """Origens liberadas, ja limpas. Lista vazia = CORS desligado."""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    def redigir(self, texto: str) -> str:
        """Substitui qualquer valor de segredo que apareca no texto.

        Rede de seguranca, nao a defesa principal: a defesa e nunca passar
        segredo para o log. Ver `logging_setup.RedacaoFilter`.
        """
        for campo in SECRET_FIELDS:
            valor = getattr(self, campo).get_secret_value()
            if valor and valor in texto:
                texto = texto.replace(valor, "***REDIGIDO***")
        return texto


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
