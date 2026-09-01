"""Logs estruturados em JSON, uma linha por evento, para stdout.

A Railway captura stdout, entao nao ha agregador. Regras:

- `run_id` acompanha todo evento de run (quando existir).
- Nenhum segredo, nem parcial (secao 10.2.4).
- Valores monetarios canonicos sao inteiros na menor unidade; campos
  decimais que aparecam aqui sao de exibicao.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# Atributos que o `logging` da stdlib coloca em todo record. Tudo que nao
# estiver aqui foi passado por `extra=` e vira campo do evento.
_PADRAO = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "stacklevel", "thread", "threadName",
        "taskName",
    }
)


class RedacaoFilter(logging.Filter):
    """Ultima linha de defesa contra segredo em log.

    A defesa principal e nunca passar segredo adiante. Este filtro existe
    porque "nunca" e uma afirmacao sobre codigo que ainda vai ser escrito.
    """

    def __init__(self, segredos: list[str]) -> None:
        super().__init__()
        self._segredos = [s for s in segredos if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._segredos:
            return True
        record.msg = self._redigir(record.msg)
        for chave, valor in list(record.__dict__.items()):
            if chave not in _PADRAO and isinstance(valor, str):
                record.__dict__[chave] = self._redigir(valor)
        return True

    def _redigir(self, valor: Any) -> Any:
        if not isinstance(valor, str):
            return valor
        for segredo in self._segredos:
            if segredo in valor:
                valor = valor.replace(segredo, "***REDIGIDO***")
        return valor


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        evento: dict[str, Any] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        for chave, valor in record.__dict__.items():
            if chave not in _PADRAO and not chave.startswith("_"):
                evento[chave] = valor
        if record.exc_info:
            evento["exc"] = self.formatException(record.exc_info)
        return json.dumps(evento, ensure_ascii=False, default=str)


def configurar_logging(nivel: str, segredos: list[str]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedacaoFilter(segredos))

    raiz = logging.getLogger()
    raiz.handlers.clear()
    raiz.addHandler(handler)
    raiz.setLevel(nivel)

    # O access log do uvicorn e texto livre e duplicaria o que ja registramos.
    logging.getLogger("uvicorn.access").disabled = True
    for nome in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(nome)
        lg.handlers.clear()
        lg.propagate = True
