"""BH e BY sobre a família fechada (R39, R40, §8.6).

> "Procedimento: BH, ou BY se a estrutura de dependência exigir; escolhido
> antes da primeira hipótese." — §8.6

D26 escolheu **BY**, e o motivo está no próprio documento: as hipóteses do lote
são variações de parâmetro da mesma estratégia sobre a mesma série, e §8.6
chama isso de "altamente dependentes".

## Os dois procedimentos, e a única linha que os separa

Os dois ordenam os p-valores e procuram o maior `k` tal que

```
p(k) <= (k / m) * alfa          <- BH
p(k) <= (k / m) * alfa / H(m)   <- BY,  H(m) = 1 + 1/2 + ... + 1/m
```

e rejeitam as `k` hipóteses de menor p-valor. É a mesma máquina; BY apenas
divide o limiar pela soma harmônica.

**Por que isso é a escolha honesta e não a conservadora.** BH controla FDR sob
independência ou dependência positiva. Sob dependência arbitrária, ele **não
controla** — apenas parece controlar, porque continua produzindo um número.
Com `m = 48`, `H(48) = 4,4588`, e o limiar efetivo cai de 10% para **2,243%**.
Promover fica bem mais difícil, e essa dificuldade é o preço de a garantia ser
verdadeira.

## O que este módulo NÃO faz

Não lê saldo de crédito. §8.6.1 é explícita em que créditos e FDR são
**mecanismos distintos que existem em paralelo, e nenhum substitui o outro**:
"cobrar por hipótese cria incentivo econômico correto, mas não determina
matematicamente o FDR". Um procedimento estatístico que consultasse orçamento
faria da escassez uma entrada da matemática — e aí o limiar passaria a depender
de quanto o agente pode pagar, que é o oposto de controle de erro.

Há teste varrendo este arquivo por qualquer menção a crédito.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

PROCEDIMENTOS = ("BH", "BY")


@dataclass(frozen=True)
class Decisao:
    """Uma hipótese e o que o procedimento decidiu sobre ela."""

    chave: str
    p_valor_ppm: int
    posicao: int
    limiar_ppm: int
    rejeitada: bool

    def como_dict(self) -> dict:
        return {
            "chave": self.chave,
            "p_valor_ppm": self.p_valor_ppm,
            "posicao": self.posicao,
            "limiar_ppm": self.limiar_ppm,
            "rejeitada": self.rejeitada,
        }


@dataclass(frozen=True)
class Resultado:
    procedimento: str
    m: int
    alfa_bps: int
    correcao_harmonica_milesimos: int
    limiar_efetivo_ppm: int
    k: int
    decisoes: list[Decisao]

    @property
    def rejeitadas(self) -> list[str]:
        return [d.chave for d in self.decisoes if d.rejeitada]

    def como_dict(self) -> dict:
        return {
            "procedimento": self.procedimento,
            "m": self.m,
            "alfa_bps": self.alfa_bps,
            "correcao_harmonica_milesimos": self.correcao_harmonica_milesimos,
            "limiar_efetivo_ppm": self.limiar_efetivo_ppm,
            "k": self.k,
            "rejeitadas": self.rejeitadas,
            "decisoes": [d.como_dict() for d in self.decisoes],
        }


def harmonico(m: int) -> Fraction:
    """`H(m) = 1 + 1/2 + ... + 1/m`, exato.

    `Fraction`, e não ponto flutuante: este número **divide o limiar de
    promoção**. Um erro no último dígito seria a diferença entre promover e
    não promover exatamente na fronteira, e é a fronteira que decide.
    """
    if m < 1:
        raise ValueError("H(m) exige m >= 1")
    return sum((Fraction(1, i) for i in range(1, m + 1)), Fraction(0))


def limiar_efetivo_ppm(*, procedimento: str, m: int, alfa_bps: int) -> int:
    """O limiar de que se parte, em partes por milhão.

    Para BH é o próprio alfa. Para BY é `alfa / H(m)`, arredondado para
    BAIXO - um limiar maior que o exato promoveria mais que o procedimento
    autoriza, e o arredondamento que se pode errar de graça é o restritivo.
    """
    if procedimento not in PROCEDIMENTOS:
        raise ValueError(f"procedimento desconhecido: {procedimento!r}")
    alfa = Fraction(alfa_bps, 10_000)
    if procedimento == "BY":
        alfa = alfa / harmonico(m)
    return int(alfa * 1_000_000)  # trunca: para baixo


def aplicar(
    p_valores_ppm: dict[str, int],
    *,
    procedimento: str,
    alfa_bps: int,
    m: int | None = None,
) -> Resultado:
    """Roda BH ou BY sobre o lote.

    `m` é o tamanho da **família**, e por padrão é quantos p-valores
    chegaram. Poder informá-lo separado não é conveniência: §8.6 fixa o
    número máximo de hipóteses **antes de começar**, e a multiplicidade a
    descontar é esse teto, não quantas por acaso chegaram a ser testadas.
    Usar o que chegou tornaria o limiar mais generoso justamente nos lotes
    que testaram menos - e isso é escolher a régua depois de ver a amostra.
    """
    total = m if m is not None else len(p_valores_ppm)
    if total < 1:
        raise ValueError("a familia precisa ter ao menos uma hipotese")
    if len(p_valores_ppm) > total:
        raise ValueError(
            f"chegaram {len(p_valores_ppm)} p-valores para uma familia de"
            f" {total}: a familia fechada foi excedida (§8.6)"
        )

    base_ppm = limiar_efetivo_ppm(
        procedimento=procedimento, m=total, alfa_bps=alfa_bps
    )
    alfa = Fraction(alfa_bps, 10_000)
    if procedimento == "BY":
        alfa = alfa / harmonico(total)

    # Ordem estável: p-valor, e a chave como desempate. Sem o desempate, dois
    # p-valores iguais poderiam trocar de posicao entre execucoes e a
    # reprodutibilidade (R12) dependeria da ordem de um dicionario.
    ordenados = sorted(p_valores_ppm.items(), key=lambda kv: (kv[1], kv[0]))

    # O maior k que satisfaz p(k) <= (k/m) * alfa. Em Fraction, exato.
    k = 0
    limiares: list[int] = []
    for i, (_, p_ppm) in enumerate(ordenados, start=1):
        limiar = Fraction(i, total) * alfa
        limiares.append(int(limiar * 1_000_000))
        if Fraction(p_ppm, 1_000_000) <= limiar:
            k = i

    decisoes = [
        Decisao(
            chave=chave,
            p_valor_ppm=p_ppm,
            posicao=i,
            limiar_ppm=limiares[i - 1],
            # TODAS as k de menor p-valor sao rejeitadas, inclusive as que
            # individualmente NAO satisfazem a desigualdade. E o passo em que
            # BH e mais forte que Bonferroni, e o mais facil de implementar
            # errado - conferir cada uma contra o proprio limiar daria um
            # procedimento diferente, mais conservador e nao publicado.
            rejeitada=i <= k,
        )
        for i, (chave, p_ppm) in enumerate(ordenados, start=1)
    ]
    return Resultado(
        procedimento=procedimento,
        m=total,
        alfa_bps=alfa_bps,
        correcao_harmonica_milesimos=(
            int(harmonico(total) * 1_000) if procedimento == "BY" else 1_000
        ),
        limiar_efetivo_ppm=base_ppm,
        k=k,
        decisoes=decisoes,
    )
