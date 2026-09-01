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

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Nomes dos campos que carregam segredo. A unica fonte desta lista.
# Usada pela redacao de log e pelo teste que garante que nada vaza.
SECRET_FIELDS = frozenset(
    {"api_service_token", "anthropic_api_key", "openai_api_key"}
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
