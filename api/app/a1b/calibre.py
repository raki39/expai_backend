"""A1b: os dois desenhos de §14.4, em execuções repetidas (R46, D29).

> "**Nula global** | Somente hipóteses nulas | Proporção de **execuções** que
> produzem ao menos uma promoção | Compatível com o nível de FDR
> pré-registrado, dentro do IC definido antes do teste
>
> **Nulas com sinal sintético** | Nulas mais sinais implantados de efeito
> conhecido | Em cada execução, `V / max(R,1)`; depois a **média entre
> execuções** | Média dentro do limite pré-registrado" — §14.4

## Onde estas execuções rodam, e por quê

**Fora da família fechada**, em lotes próprios. Não é atalho: é a D25, literal
— *"as execuções repetidas de A1b (D29) rodam fora desta família, em lotes
próprios: elas medem o calibre do procedimento, não produzem conhecimento"*.
Registrá-las como hipóteses estouraria o teto de 48 quatrocentas vezes.

## A decisão é a MESMA do lote real

Cada execução chama `validador.lote.decidir`, que é a função que o lote de
produção usa: BY sobre a família e depois o DSR sobre quem sobrevive. Uma cópia
do procedimento aqui mediria o calibre de um procedimento que não é o que
promove — e a divergência seria invisível.

## Duas magnitudes de sinal, e as duas são derivadas

Medir poder exige efeito de magnitude conhecida. **Qual** magnitude não é
detalhe: implantar um efeito que o procedimento provavelmente não detecta mede
o horizonte, não o protocolo. Então são duas, e o contraste entre elas é o
resultado:

| Magnitude | De onde sai | O que responde |
|---|---|---|
| `piso_testavel` | §8.3: o Sharpe que faz `n_minimo` caber no horizonte (`t = 2`) | o protocolo detecta o que o próprio pré-registro considera testável? |
| `detectavel_por_by` | o limiar de BY na primeira posição, invertido pela normal | qual Sharpe seria preciso para BY promover? |

Elas **não coincidem**, e a distância entre as duas é uma propriedade do nosso
protocolo que nenhum outro número mostra: o planejamento de amostra é
calibrado em `t = 2` e a correção de multiplicidade exige `t` bem maior.

## Duas barreiras, e as duas são reportadas

O caminho de promoção tem duas: o veredito por hipótese exige
`n_efetivo >= n_minimo` (R51), e o lote exige BY e DSR. Reportar só uma
responderia meia pergunta — e §14.4 já manda registrar os dois números quando
manda registrar FDR e poder juntos.

## O que estas execuções NÃO exercitam

Elas entram no pipeline **estatístico**. Não passam pelo simulador, pela
avaliação de regra nem pelo ledger — 400 execuções sobre o mercado inteiro
custariam horas. Quem cobre esse lado é A1a, injetado pelo mesmo caminho das
reais. A limitação viaja junto do número, e não numa nota de rodapé.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import time
from dataclasses import dataclass

from ..config.schema import ExperimentConfig
from ..estatistica import fdr as fdr_mod
from ..estatistica import intervalo as intervalo_mod
from ..estatistica import pvalor as pvalor_mod
from ..estatistica import sharpe as sharpe_mod
from ..hipotese import poder
from ..validador import lote as lote_mod
from . import series

log = logging.getLogger(__name__)

NULA_GLOBAL = "nula_global"
COM_SINAL = "nulas_com_sinal_sintetico"
DESENHOS = (NULA_GLOBAL, COM_SINAL)

PISO = "piso_testavel"
DETECTAVEL = "detectavel_por_by"


@dataclass(frozen=True)
class Magnitudes:
    piso_milesimos: int
    detectavel_milesimos: int
    limiar_by_ppm: int
    z_do_limiar: float

    def como_dict(self) -> dict:
        return {
            "piso_testavel_milesimos": self.piso_milesimos,
            "detectavel_por_by_milesimos": self.detectavel_milesimos,
            "limiar_by_primeira_posicao_ppm": self.limiar_by_ppm,
            "z_do_limiar": round(self.z_do_limiar, 4),
            "por_que_duas": (
                "o planejamento de amostra de §8.3 e calibrado em t = 2, e o"
                " limiar de BY na primeira posicao exige t bem maior. Implantar"
                " so o piso mediria o horizonte; implantar so o detectavel"
                " esconderia que o piso nao passa"
            ),
        }


def magnitudes(
    *, config: ExperimentConfig, duracao_barra_ms: int, n_barras: int
) -> Magnitudes:
    """As duas magnitudes de sinal, **derivadas** e nunca configuradas.

    `detectavel_por_by` inverte o caminho do p-valor: BY na primeira posição
    aceita `p <= alfa / (H(m) * m)`, e o Sharpe anualizado que produz esse
    p-valor sobre este horizonte é `z / sqrt(anos)`.
    """
    piso = poder.sharpe_minimo_testavel(
        duracao_barra_ms=duracao_barra_ms, horizonte_barras=n_barras
    )
    m = config.a1b_lote
    base_ppm = fdr_mod.limiar_efetivo_ppm(
        procedimento=config.fdr_procedimento, m=m, alfa_bps=config.fdr_alvo_bps
    )
    # Primeira posicao: (1/m) * alfa_efetivo. E a mais exigente das m, e e a
    # que decide se ALGUMA coisa e promovida num lote em que quase tudo e
    # nulo - que e exatamente o caso do desenho 1.
    limiar_ppm = max(1, base_ppm // m)
    z = pvalor_mod.phi_inversa(1 - limiar_ppm / 1_000_000)
    anos = n_barras / poder.barras_por_ano(duracao_barra_ms)
    detectavel = math.ceil(z / math.sqrt(anos) * 1_000) if anos > 0 else piso
    return Magnitudes(
        piso_milesimos=piso,
        detectavel_milesimos=detectavel,
        limiar_by_ppm=limiar_ppm,
        z_do_limiar=z,
    )


@dataclass(frozen=True)
class Uma:
    """O resultado de uma execução repetida. É a linha que fica gravada."""

    desenho: str
    indice: int
    #: Promoções pelo lote (BY + DSR), e quantas delas eram nulas.
    r_lote: int
    v_lote: int
    #: O mesmo, depois do portão de amostra do pré-registro.
    r_com_portao: int
    v_com_portao: int
    #: Por magnitude: quantos implantados e quantos promovidos.
    sinais_piso: int
    promovidos_piso: int
    sinais_detectavel: int
    promovidos_detectavel: int

    @property
    def teve_promocao(self) -> bool:
        return self.r_lote > 0

    @property
    def razao_lote(self) -> float:
        return self.v_lote / max(self.r_lote, 1)

    def como_dict(self) -> dict:
        return {
            "desenho": self.desenho,
            "indice": self.indice,
            "r_lote": self.r_lote,
            "v_lote": self.v_lote,
            "r_com_portao": self.r_com_portao,
            "v_com_portao": self.v_com_portao,
            "sinais_piso": self.sinais_piso,
            "promovidos_piso": self.promovidos_piso,
            "sinais_detectavel": self.sinais_detectavel,
            "promovidos_detectavel": self.promovidos_detectavel,
        }


def _semente_da_execucao(base: int, desenho: str, indice: int) -> int:
    """Semente por hash, e não `base + indice`.

    Sementes vizinhas correlacionam no início da sequência: com `base + i` as
    primeiras execuções sairiam parecidas entre si, e a proporção medida
    descreveria a correlação das sementes em vez do calibre. O mesmo motivo de
    `baselines.derivar_semente` e de `b4.busca._semente_de`.
    """
    digest = hashlib.sha256(f"a1b:{base}:{desenho}:{indice}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def uma(
    *,
    indice: int,
    desenho: str,
    base_bps: list[int],
    config: ExperimentConfig,
    duracao_barra_ms: int,
    n_barras: int,
    tentativas_globais: int,
    semente: int,
    mags: Magnitudes,
) -> Uma:
    """Uma execução repetida: monta o lote, decide, conta.

    Reprodutível por `(semente, desenho, indice)`: rodar a execução 7 de novo
    produz a mesma linha. É o que permite gravá-las em pedaços sem que o
    conjunto deixe de ser um experimento só.
    """
    rng = random.Random(_semente_da_execucao(semente, desenho, indice))
    barras_ano = poder.barras_por_ano(duracao_barra_ms)
    quantos = config.a1b_sinais_implantados if desenho == COM_SINAL else 0
    # Metade em cada magnitude. Ímpar sobra para o piso, que é o caso menos
    # favorável: errar para o lado de medir menos poder.
    no_detectavel = quantos // 2
    no_piso = quantos - no_detectavel

    # O `n_minimo` que o pre-registro declararia. Na producao da 0B ele sai do
    # Sharpe declarado, e o declarado e o piso testavel do horizonte (D33) - o
    # mesmo numero que o prompt do agente e o pre-registro de B4 usam. Supor
    # outro aqui mediria um portao que ninguem enfrenta.
    n_min = poder.n_minimo(
        sharpe_milesimos=mags.piso_milesimos, duracao_barra_ms=duracao_barra_ms
    )

    entradas: list[lote_mod.Entrada] = []
    rotulo: dict[str, str | None] = {}
    alcanca: dict[str, bool] = {}

    for i in range(config.a1b_lote):
        if i < no_piso:
            tipo, sharpe = PISO, mags.piso_milesimos
        elif i < no_piso + no_detectavel:
            tipo, sharpe = DETECTAVEL, mags.detectavel_milesimos
        else:
            tipo, sharpe = None, 0

        serie = (
            series.nula(rng, base_bps, n_barras)
            if tipo is None
            else series.com_sinal(
                rng, base_bps, n_barras,
                sharpe_milesimos=sharpe, barras_por_ano=barras_ano,
            )
        )
        chave = f"{desenho}-{indice}-{i}"
        rotulo[chave] = tipo

        m = sharpe_mod.momentos(serie)
        efetivo = poder.efetivo_de_bruto(serie, len(serie))
        teste = pvalor_mod.de_sharpe(
            sharpe_anualizado=m.sharpe_anualizado(duracao_barra_ms),
            n_efetivo=efetivo.efetivo,
            barras_por_ano_=barras_ano,
        )
        alcanca[chave] = efetivo.efetivo >= n_min
        entradas.append(
            lote_mod.Entrada(
                chave=chave,
                p_valor_ppm=teste.p_valor_ppm,
                momentos=m.como_dict(duracao_barra_ms),
            )
        )

    promovidas = lote_mod.decidir(
        entradas,
        procedimento=config.fdr_procedimento,
        alfa_bps=config.fdr_alvo_bps,
        m=config.a1b_lote,
        dsr_minimo_milesimos=config.dsr_minimo_milesimos,
        tentativas=tentativas_globais,
    ).sobreviventes
    com_portao = [c for c in promovidas if alcanca[c]]
    return Uma(
        desenho=desenho,
        indice=indice,
        r_lote=len(promovidas),
        v_lote=sum(1 for c in promovidas if rotulo[c] is None),
        r_com_portao=len(com_portao),
        v_com_portao=sum(1 for c in com_portao if rotulo[c] is None),
        sinais_piso=no_piso,
        promovidos_piso=sum(1 for c in promovidas if rotulo[c] == PISO),
        sinais_detectavel=no_detectavel,
        promovidos_detectavel=sum(
            1 for c in promovidas if rotulo[c] == DETECTAVEL
        ),
    )


def agregar(
    execucoes: list[Uma], *, desenho: str, config: ExperimentConfig
) -> dict:
    """As estatísticas de um desenho, a partir das execuções gravadas.

    `None` com motivo quando não há execução nenhuma — zero execuções não é
    "proporção zero", e devolver um intervalo sobre nada afirmaria que se
    mediu.
    """
    do_desenho = [e for e in execucoes if e.desenho == desenho]
    alvo_ppm = config.fdr_alvo_bps * 100
    if not do_desenho:
        return {
            "desenho": desenho,
            "execucoes": 0,
            "completo": False,
            "por_que_sem_numero": (
                "nenhuma execucao gravada; zero execucoes nao e proporcao"
                " zero, e um intervalo sobre nada afirmaria que se mediu"
            ),
        }

    n = len(do_desenho)
    com_promocao = sum(1 for e in do_desenho if e.teve_promocao)
    com_portao = sum(1 for e in do_desenho if e.r_com_portao > 0)
    ic_portao = intervalo_mod.wilson(
        sucessos=com_portao, n=n, confianca_bps=config.a1b_ic_bps
    )

    if desenho == NULA_GLOBAL:
        ic = intervalo_mod.wilson(
            sucessos=com_promocao, n=n, confianca_bps=config.a1b_ic_bps
        )
        principal = {
            "o_que_mede": (
                "proporcao de execucoes que produzem ao menos uma promocao,"
                " num lote so de nulas"
            ),
            "execucoes_com_promocao": com_promocao,
            "intervalo": ic.como_dict(),
            # OS DOIS criterios, e a divergencia entre eles esta declarada no
            # relatorio. Escolher um por conta propria seria decidir a regua.
            "ic_contem_o_alvo": ic.contem_ppm(alvo_ppm),
            "limite_superior_ate_o_alvo": ic.alto_ppm <= alvo_ppm,
        }
    else:
        razoes = [e.razao_lote for e in do_desenho]
        ic = intervalo_mod.bootstrap_da_media(
            razoes, semente=config.default_seed, confianca_bps=config.a1b_ic_bps
        )
        piso_n = sum(e.sinais_piso for e in do_desenho)
        piso_ok = sum(e.promovidos_piso for e in do_desenho)
        det_n = sum(e.sinais_detectavel for e in do_desenho)
        det_ok = sum(e.promovidos_detectavel for e in do_desenho)
        principal = {
            "o_que_mede": (
                "media de V / max(R,1) entre execucoes, num lote de nulas mais"
                " sinais implantados"
            ),
            "intervalo": ic.como_dict(),
            "limite_superior_ate_o_alvo": ic.alto_ppm <= alvo_ppm,
            # §14.4: "ambos os numeros sao registrados". Um protocolo que
            # rejeita tudo tem FDR perfeito e e inutil, e sem o poder ao lado
            # o primeiro numero sozinho parece otimo.
            "poder": {
                PISO: {
                    "implantados": piso_n,
                    "promovidos": piso_ok,
                    "fracao_ppm": (
                        piso_ok * 1_000_000 // piso_n if piso_n else None
                    ),
                },
                DETECTAVEL: {
                    "implantados": det_n,
                    "promovidos": det_ok,
                    "fracao_ppm": (
                        det_ok * 1_000_000 // det_n if det_n else None
                    ),
                },
                "por_que_importa": (
                    "um sistema que rejeita ruido perfeitamente mas tambem"
                    " rejeita efeitos verdadeiros implantados nao esta"
                    " calibrado, esta apenas surdo (§14.4)"
                ),
            },
        }

    return {
        "desenho": desenho,
        "execucoes": n,
        "execucoes_pedidas": config.a1b_execucoes,
        "completo": n >= config.a1b_execucoes,
        "fdr_alvo_ppm": alvo_ppm,
        "promocao_do_lote": principal,
        "com_o_portao_de_amostra": {
            "o_que_mede": (
                "o mesmo lote, depois do n_minimo que o pre-registro declara."
                " E a barreira que de fato impede promocao na 0B, e ela e mais"
                " restritiva que BY"
            ),
            "execucoes_com_promocao": com_portao,
            "intervalo": ic_portao.como_dict(),
        },
    }


def rodar(
    *,
    base_bps: list[int],
    config: ExperimentConfig,
    duracao_barra_ms: int,
    n_barras: int,
    tentativas_globais: int,
    indices: dict[str, list[int]],
    semente: int | None = None,
) -> tuple[list[Uma], Magnitudes, int]:
    """Roda os índices pedidos por desenho. Devolve as linhas e o tempo de CPU.

    **Em pedaços de propósito.** São 400 execuções (D29) a ~0,85 s cada, e uma
    requisição HTTP de seis minutos não é um desenho, é uma aposta no timeout.
    Cada execução é reprodutível por `(semente, desenho, indice)`, então rodar
    em pedaços e gravar produz exatamente o mesmo conjunto que rodar tudo de
    uma vez — e o registro fica append-only, como o resto do sistema.
    """
    if len(base_bps) < series.MINIMO_DE_BARRAS:
        raise ValueError(
            f"a serie base tem {len(base_bps)} retornos, e o quarto momento"
            f" exige ao menos {series.MINIMO_DE_BARRAS}"
        )
    base = config.default_seed if semente is None else semente
    mags = magnitudes(
        config=config, duracao_barra_ms=duracao_barra_ms, n_barras=n_barras
    )
    comeco = time.perf_counter_ns()
    feitas = [
        uma(
            indice=i,
            desenho=desenho,
            base_bps=base_bps,
            config=config,
            duracao_barra_ms=duracao_barra_ms,
            n_barras=n_barras,
            tentativas_globais=tentativas_globais,
            semente=base,
            mags=mags,
        )
        for desenho in DESENHOS
        for i in indices.get(desenho, [])
    ]
    cpu = (time.perf_counter_ns() - comeco) // 1_000
    log.info(
        "a1b.pedaco",
        extra={"execucoes": len(feitas), "cpu_micros": cpu},
    )
    return feitas, mags, cpu
