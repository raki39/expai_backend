"""A fase corrente do experimento. **Uma fonte, e uma so.**

Este arquivo existe porque a alternativa ja falhou duas vezes.

No incremento 9 o `/api/health` foi pego declarando `"fase": "0A"` depois de a
0B abrir, e a correcao veio com um comentario dizendo *"a fase vem daqui e de
nenhum outro lugar"*. Era falso quando foi escrito: havia mais dois lugares -
o liveness em `main.py` e o `/api/exportar` -, e os tres passaram a **discordar
entre si**.

Um comentario afirmando unicidade nao produz unicidade. Um modulo importado
produz.

## E ELE FALHOU DE NOVO, de um jeito diferente - 2026-09-08

`FASE` ficou em `"0B"` da abertura da 0C (2026-09-04) ate aqui. Quatro dias,
tres incrementos e onze ADRs depois, `/api/health` continuava anunciando a fase
anterior.

**A centralizacao resolveu DIVERGENCIA e nao resolveu ENVELHECIMENTO.** Os tres
lugares concordavam perfeitamente - todos errados juntos. Um valor unico e
melhor que tres valores discordantes, e continua sendo um valor que ninguem
atualiza.

**E o teste que devia pegar era parte do problema:** ele afirmava
`fase == "0B"`, ou seja, pinava a constante contra ela mesma. Um teste assim
passa exatamente enquanto o valor estiver errado.

Hoje a fase e **DERIVADA** do maior incremento aplicado nas migracoes, e a
constante e conferida contra a derivacao. Acrescentar migracao de um incremento
de outra fase **quebra a suite** ate alguem decidir - que e o unico jeito de a
virada de fase nao depender de memoria.

## Por que isto importa mais que parecer

O campo `fase` acompanha o **aviso sobre o que pode ser afirmado**. Um numero
rotulado "0A" carrega "nenhuma conclusao estatistica"; o mesmo numero rotulado
"0B" carrega "conclusao so pelo validador independente"; e "0C" carrega
"nenhuma candidata admitida no forward". Errar o rotulo e descrever errado o
que o resultado significa - que e pior que nao rotular.
"""

from __future__ import annotations

# Os incrementos de cada fase, do plano. Explicito porque a fronteira e uma
# DECISAO registrada nos documentos de fase, e nao algo derivavel do codigo.
INCREMENTOS_POR_FASE: dict[str, range] = {
    "0A": range(0, 8),     # 0-7
    "0B": range(8, 15),    # 8-14 (mais o 11b, que cai no 11)
    "0C": range(15, 22),   # 15-21
}

FASE = "0C"

AVISO = (
    "Fase 0C. Forward continuo, e NENHUMA CANDIDATA foi admitida nele (D38,"
    " ADR 0034) - o Portao B rejeitou a unica que existia, e o B3 roda apenas"
    " como controle negativo do encanamento. O simulador e calibrado contra"
    " mercado observado, POR REGIME (ADR 0033), e resultado que atravessa"
    " regime nao calibrado fica inconclusivo quanto a fidelidade. Nenhuma"
    " aprovacao autoriza capital real."
)

# O nome do servico ficou "fase0a-api" desde o incremento 0 e NAO muda: e
# identificador de servico, nao declaracao de fase. Renomea-lo quebraria a
# correlacao de log entre os deploys das fases, que e justamente o que se quer
# olhar ao comparar as duas.
SERVICO = "fase0a-api"


def maior_incremento_aplicado() -> int:
    """O maior numero de incremento citado nas migracoes registradas.

    Le a DESCRICAO das migracoes, que e onde o incremento esta escrito. Nao e
    elegante, e e o unico ponto do codigo da `api` que muda a cada incremento
    sem ninguem precisar lembrar - o que o torna a ancora certa.
    """
    import re

    from .migrations import MIGRACOES

    numeros = [
        int(m.group(1))
        for _numero, descricao, _sql in MIGRACOES
        if (m := re.search(r"incremento (\d+)", descricao))
    ]
    return max(numeros) if numeros else 0


def fase_derivada() -> str | None:
    """A fase que o maior incremento aplicado implica. `None` se nenhuma cobre.

    `None` e informacao: significa que as migracoes chegaram a um incremento
    fora de toda faixa declarada - ou seja, uma fase nova comecou e
    `INCREMENTOS_POR_FASE` nao sabe dela.
    """
    n = maior_incremento_aplicado()
    for fase, faixa in INCREMENTOS_POR_FASE.items():
        if n in faixa:
            return fase
    return None
