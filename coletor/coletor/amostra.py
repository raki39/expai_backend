"""A amostra de topo de livro, e a regra que decide se ela vale.

ADR 0028. Duas coisas aqui sao decisao registrada, e nao detalhe:

1. **O horario e SO o de recebimento local.** O `bookTicker` spot da Binance
   entrega seis campos - `u, s, b, B, a, A` - e NAO tem `E` nem `T`. Quem tem
   e o `bookTicker` de futuros. Gravar uma coluna `exchange_ts` sempre nula
   seria declarar um campo que nao existe, que e o defeito que este projeto ja
   registrou dezesseis vezes.

2. **`u` NAO decide disponibilidade.** Um `u` parado e compativel com mercado
   calmo E com stream travado, e tratar os dois como o mesmo caso inventaria
   indisponibilidade onde ha so quietude. Ele serve para duplicacao, regressao
   e salto - observacoes, nao vereditos. Quem decide validade e a idade da
   mensagem, o estado da conexao e as lacunas registradas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Tolerancia de frescura NO MOMENTO DA AMOSTRAGEM.
#
# Nao confundir com a tolerancia do ADR 0027, que e avaliada no momento da
# DECISAO: `t_exec - received_at <= 2 s`. Como a amostra escolhida pelo
# calibrador e sempre anterior a `t_exec`, esta checagem aqui e condicao
# NECESSARIA e nao suficiente - o calibrador refaz a conta com o seu proprio
# instante. As duas existem porque sao perguntas em momentos diferentes.
TOLERANCIA_MS = 2_000

NS_POR_MS = 1_000_000


@dataclass(frozen=True)
class Cotacao:
    """Uma mensagem do stream, com a hora em que ELA chegou aqui."""

    u: int
    bid: str
    bid_qty: str
    ask: str
    ask_qty: str
    received_at_ns: int


@dataclass
class Estado:
    """O que o amostrador carrega entre tiques.

    `ultima` e substituida a cada mensagem: o receptor nunca enfileira e nunca
    bloqueia em disco. O tique de 1 Hz le o que estiver ali.
    """

    ultima: Cotacao | None = None
    conectado: bool = False
    u_anterior: int | None = None
    duplicadas: int = 0
    regressoes: int = 0


@dataclass(frozen=True)
class Amostra:
    """Uma linha do arquivo. `indisponivel` nunca e preenchida por interpolacao."""

    sampled_at_ns: int
    received_at_ns: int | None
    idade_ms: int | None
    u: int | None
    bid: str | None
    bid_qty: str | None
    ask: str | None
    ask_qty: str | None
    disponivel: bool
    motivo: str | None
    u_duplicado: bool
    u_regrediu: bool
    delta_u: int | None

    def como_linha(self) -> dict[str, Any]:
        return {
            "sampled_at_ns": self.sampled_at_ns,
            "received_at_ns": self.received_at_ns,
            "idade_ms": self.idade_ms,
            "u": self.u,
            "bid": self.bid,
            "bid_qty": self.bid_qty,
            "ask": self.ask,
            "ask_qty": self.ask_qty,
            "disponivel": self.disponivel,
            "motivo": self.motivo,
            "u_duplicado": self.u_duplicado,
            "u_regrediu": self.u_regrediu,
            "delta_u": self.delta_u,
        }


def amostrar(estado: Estado, sampled_at_ns: int, *,
             tolerancia_ms: int = TOLERANCIA_MS) -> Amostra:
    """Produz a amostra do tique, e ATUALIZA os contadores de `u` no estado.

    Tres caminhos de indisponibilidade, cada um com motivo proprio - porque
    "nao ha dado" e "o dado esta velho" sao coisas diferentes para quem
    depois le o arquivo:

      sem_mensagem      nenhuma cotacao chegou ainda (boot, ou queda longa)
      desconectado      a conexao caiu; o que temos pode estar arbitrariamente velho
      defasada          chegou, mas ha mais tempo que a tolerancia

    Em NENHUM deles a cotacao anterior e repetida. A linha sai com os campos
    de preco nulos, e a lacuna fica declarada.
    """
    u_dup = False
    u_reg = False
    delta_u: int | None = None

    if estado.ultima is None:
        return Amostra(
            sampled_at_ns=sampled_at_ns, received_at_ns=None, idade_ms=None,
            u=None, bid=None, bid_qty=None, ask=None, ask_qty=None,
            disponivel=False, motivo="sem_mensagem",
            u_duplicado=False, u_regrediu=False, delta_u=None,
        )

    c = estado.ultima
    idade_ms = (sampled_at_ns - c.received_at_ns) // NS_POR_MS

    # ------------------------------------------------------------------ `u`
    # Observado e registrado. NAO entra na decisao de disponibilidade: o topo
    # de livro pode simplesmente nao ter mudado desde o tique anterior, e isso
    # e mercado calmo, nao defeito.
    if estado.u_anterior is not None:
        delta_u = c.u - estado.u_anterior
        if delta_u == 0:
            u_dup = True
            estado.duplicadas += 1
        elif delta_u < 0:
            # `u` andando para trás nao acontece num stream saudavel. Indica
            # reconexao mal costurada ou mensagem fora de ordem - e por isso e
            # registrado, ainda que tambem nao decida disponibilidade sozinho.
            u_reg = True
            estado.regressoes += 1
    estado.u_anterior = c.u

    # -------------------------------------------------- validade TEMPORAL
    if not estado.conectado:
        motivo = "desconectado"
    elif idade_ms > tolerancia_ms:
        motivo = "defasada"
    else:
        motivo = None

    disponivel = motivo is None
    return Amostra(
        sampled_at_ns=sampled_at_ns,
        received_at_ns=c.received_at_ns,
        idade_ms=idade_ms,
        # Preco so sai na linha quando a amostra vale. Publicar bid/ask numa
        # linha marcada indisponivel convida a leitura que ignora o campo.
        u=c.u if disponivel else None,
        bid=c.bid if disponivel else None,
        bid_qty=c.bid_qty if disponivel else None,
        ask=c.ask if disponivel else None,
        ask_qty=c.ask_qty if disponivel else None,
        disponivel=disponivel,
        motivo=motivo,
        u_duplicado=u_dup,
        u_regrediu=u_reg,
        delta_u=delta_u,
    )


def da_mensagem(payload: dict[str, Any], received_at_ns: int) -> Cotacao:
    """Le o payload do `<symbol>@bookTicker`.

    Os seis campos sao os unicos que existem. Se a Binance acrescentar um
    carimbo um dia, ele entra aqui e o ADR 0028 muda junto - mas ate la o
    contrato diz a verdade sobre o que ha.
    """
    return Cotacao(
        u=int(payload["u"]),
        bid=str(payload["b"]),
        bid_qty=str(payload["B"]),
        ask=str(payload["a"]),
        ask_qty=str(payload["A"]),
        received_at_ns=received_at_ns,
    )
