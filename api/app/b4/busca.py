"""B4: o gerador de hipoteses NAO COGNITIVO (secao 14.3).

Busca aleatoria e varredura de parametro sobre o mesmo catalogo fechado de
tres familias (D5). Nenhuma chamada de modelo, nenhum token: so CPU.

## Por que este modulo decide o que decide

Toda hipotese precisa de pre-registro (secao 8.2), e o pre-registro tem
campos que o agente **escolhe pensando** - metrica primaria, efeito minimo,
Sharpe esperado, condicoes de falseamento. B4 nao pensa. Entao cada um desses
campos precisa de uma regra deterministica, e cada regra e uma decisao que
afeta a comparacao entre os bracos.

As decisoes estao abaixo, uma por uma, com o motivo. **Nenhuma delas foi
escolhida depois de ver resultado** - a quinta pergunta do teste de escopo da
0B vale para nos, e escolher a regua do controle olhando o placar e
exatamente o que ela proibe.

## A assimetria deliberada

O agente ESCOLHE a metrica primaria; B4 nao. Isso e de proposito:

- escolher um alvo falsificavel bom e trabalho cognitivo, e e parte do que a
  fase esta medindo;
- e uma metrica variavel deixaria B4 comprar sobrevivencia trocando de regua,
  que e o modo mais barato de o controle parecer bom.

O que B4 recebe de graca e o que o agente tambem recebe: o **piso de Sharpe
testavel** do horizonte, que a D33 pos no prompt dele. Simetrico.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass

from ..config.schema import ExperimentConfig
from ..hipotese.poder import sharpe_minimo_testavel
from ..hipotese.schema import (
    SHARPE_MAX_MILESIMOS,
    ClausulaFalseamento,
    PreRegistroBruto,
)
from ..regra.schema import (
    BandaDesvio,
    BreakoutCanal,
    CruzamentoMedias,
    Regra,
    condicoes_da_config,
)

# ---------------------------------------------------------------------------
# As decisoes deterministicas
# ---------------------------------------------------------------------------

#: A metrica primaria de TODA hipotese de B4. Fixa.
#:
#: **Nao e `excesso_sobre_b1_p50_cents`, e a razao MUDOU no incremento 13.**
#:
#: Ate o incremento 12 a razao era um defeito: o validador nao enxergava o B1
#: casado, porque nada ligava o run do controle ao run que ele casa. O
#: incremento 13 ligou (`run.casa_run_id`, migracao 14) e o defeito acabou.
#:
#: A metrica de B4 continua sendo o excesso sobre B3, agora por dois motivos
#: que nao sao defeito:
#:
#:   1. **B4 nao produz B1 casado.** Casar o acaso com o giro de cada uma das
#:      16 hipoteses custaria 16 distribuicoes de 1.000 repeticoes, e nenhum
#:      criterio de §14.4 pede isso do controle: o criterio do B1 e sobre o
#:      resultado do agente, e a comparacao com B4 e "por credito consumido".
#:   2. **Trocar a regua do controle agora seria escolher a regua depois de
#:      ver o placar.** As 16 hipoteses de B4 ja rodaram em producao sob esta
#:      metrica. A quinta pergunta do teste de escopo da 0B vale para nos.
#:
#: `excesso_sobre_b3_cents` cumpre a regra 14 - desempenho sempre como excesso
#: sobre baseline -, o validador a avalia de ponta a ponta, e e a mesma que o
#: agente declarou no run 30: os dois bracos ficam comparaveis na mesma regua,
#: o que e melhor que comparaveis em reguas diferentes.
METRICA = "excesso_sobre_b3_cents"

#: `efeito_minimo` como fracao do capital semente, em bps. 500 bps = 5%.
#:
#: Derivado do capital, e nao constante em centavos: um efeito minimo fixo
#: passaria a significar outra coisa se o capital semente mudasse, que e o
#: defeito de valor-que-para-de-descrever registrado doze vezes neste projeto.
#:
#: 5% e modesto de proposito. B4 e controle: um efeito minimo alto o faria
#: falhar por ambicao, e um baixo o faria "alcancar" qualquer coisa. Nenhum dos
#: dois mede busca de parametro contra reflexao.
EFEITO_MINIMO_BPS_DO_CAPITAL = 500

#: Quantas hipoteses, e como se dividem. O total e da D25 (16 para B4).
#:
#: Meio a meio porque §14.3 lista as duas tecnicas - "busca aleatoria ou
#: varredura de parametros" - e escolher uma seria decidir qual controle o
#: agente enfrenta.
QUANTAS_ALEATORIAS = 8
QUANTAS_VARREDURA = 8
QUANTAS = QUANTAS_ALEATORIAS + QUANTAS_VARREDURA

#: A grade da varredura, por familia. Valores nos extremos e no meio de cada
#: faixa que o catalogo aceita - nao numeros "bonitos" escolhidos a mao, que
#: seriam palpite disfarcado de sistema.
GRADE: tuple[tuple[str, dict], ...] = (
    ("cruzamento_medias", {"rapida": 10, "lenta": 30}),
    ("cruzamento_medias", {"rapida": 20, "lenta": 100}),
    ("cruzamento_medias", {"rapida": 50, "lenta": 200}),
    ("cruzamento_medias", {"rapida": 100, "lenta": 400}),
    ("banda_desvio", {"periodo": 20, "desvios_milesimos": 1_500}),
    ("banda_desvio", {"periodo": 100, "desvios_milesimos": 2_500}),
    ("breakout_canal", {"periodo": 20}),
    ("breakout_canal", {"periodo": 100}),
)


@dataclass(frozen=True)
class Candidata:
    """Uma hipotese de B4: a regra, o pre-registro e de onde ela veio."""

    indice: int
    tecnica: str
    regra: Regra
    pre_registro: PreRegistroBruto

    def como_dict(self) -> dict:
        return {
            "indice": self.indice,
            "tecnica": self.tecnica,
            "familia": self.regra.params.familia,
            "params": self.regra.params.model_dump(mode="json"),
            "position_fraction_bps": self.regra.position_fraction_bps,
            "pre_registro": self.pre_registro.model_dump(mode="json"),
        }


def _semente_de(base: int, indice: int) -> int:
    """SHA-256, e nao `base + i`.

    O mesmo motivo de `baselines.derivar_semente`: sementes vizinhas
    correlacionam no inicio da sequencia, e aqui isso faria as primeiras
    candidatas aleatorias sairem parecidas entre si - estreitando o espaco de
    busca sem que nada acusasse.
    """
    digest = hashlib.sha256(f"b4:{base}:{indice}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _params_aleatorios(rng: random.Random):
    """Um ponto ao acaso do catalogo fechado. Sem preferencia de familia."""
    familia = rng.choice(("cruzamento_medias", "banda_desvio", "breakout_canal"))
    if familia == "cruzamento_medias":
        # A ordem que o modelo impoe: rapida < lenta. Sortear as duas
        # independentemente produziria pares invalidos, e descartar-los
        # enviesaria a distribuicao para lentas altas.
        rapida = rng.randint(2, 200)
        lenta = rng.randint(max(rapida + 1, 3), 400)
        return CruzamentoMedias(rapida=rapida, lenta=lenta)
    if familia == "banda_desvio":
        return BandaDesvio(
            periodo=rng.randint(5, 200),
            desvios_milesimos=rng.randint(100, 10_000),
        )
    return BreakoutCanal(periodo=rng.randint(5, 200))


def _enunciado(tecnica: str, params, fracao_bps: int, sharpe_milesimos: int) -> str:
    """O enunciado de B4 diz O QUE ELE E, e nao finge raciocinio.

    Esta e a linha mais facil de errar do modulo. O campo se chama
    `enunciado` e no braco do agente carrega a leitura de mercado dele; se B4
    escrevesse aqui uma frase plausivel sobre deriva e volatilidade, o registro
    ficaria com duas afirmacoes indistinguiveis, uma pensada e uma gerada - e a
    comparacao da fase perderia o sentido no proprio dado que a sustenta.

    Entao ele declara a procedencia primeiro, em maiusculas, e depois os
    parametros. Quem ler o registro sabe de qual braco a linha veio sem
    consultar `agente_origem`.
    """
    descricao = ", ".join(
        f"{k}={v}" for k, v in params.model_dump(mode="json").items()
        if k != "familia"
    )
    return (
        f"CONTROLE NAO COGNITIVO (B4, secao 14.3), por {tecnica}."
        f" Nenhuma reflexao produziu esta hipotese: os parametros vieram de"
        f" {'sorteio no catalogo fechado' if tecnica == 'busca_aleatoria' else 'uma grade fixa'}."
        f" Familia {params.familia} com {descricao},"
        f" {fracao_bps / 100:.0f}% do caixa por operacao."
        f" Afirmacao testada: o excesso sobre o B3 supera o efeito minimo"
        f" declarado, com Sharpe de"
        f" {sharpe_milesimos / 1000:.2f} - que e o piso testavel do horizonte,"
        f" e nao uma expectativa."
    )


def _pre_registro(
    tecnica: str,
    params,
    *,
    fracao_bps: int,
    config: ExperimentConfig,
    duracao_barra_ms: int,
    horizonte_barras: int,
) -> PreRegistroBruto:
    """O pre-registro de §8.2 montado sem nenhuma escolha de julgamento."""
    efeito = (
        config.seed_capital_usd_cents * EFEITO_MINIMO_BPS_DO_CAPITAL // 10_000
    )
    # O MESMO numero que a D33 pos no prompt do agente. Declarar o piso e a
    # unica escolha honesta para quem nao tem visao: acima dele seria ambicao
    # inventada, e abaixo produziria `testavel = 0` de saida.
    piso = sharpe_minimo_testavel(
        duracao_barra_ms=duracao_barra_ms,
        horizonte_barras=max(1, horizonte_barras),
    )
    # **O piso pode estourar o teto do schema, e isso e resultado.**
    #
    # `SHARPE_MAX_MILESIMOS` e 5,00 porque acima disso, em mercado liquido,
    # nao ha hipotese honesta - ha erro de unidade. Numa janela curta o piso
    # testavel passa disso (12,48 numa de 3.000 barras), e entao NENHUMA
    # hipotese e testavel ali: e §8.3 funcionando, e a D33 ja decidiu o que
    # fazer - registrar sem promover, com veredito `inconclusiva` por
    # construcao.
    #
    # O agente enfrenta o mesmo teto: o schema recusa acima de 5,00, entao
    # nem ele pode declarar o piso quando o piso e alto. Grudar no teto e o
    # mais perto que os dois chegam, e o `testavel = 0` que sai disso e a
    # informacao, e nao a falha.
    sharpe = min(piso, SHARPE_MAX_MILESIMOS)
    return PreRegistroBruto(
        enunciado=_enunciado(tecnica, params, fracao_bps, sharpe),
        metrica_primaria=METRICA,
        efeito_minimo=efeito,
        sharpe_esperado_milesimos=sharpe,
        # O unico criterio que B4 pode honrar: parar por falseamento ou por
        # `n_minimo` exigiria decidir durante a execucao, e B4 nao decide.
        criterio_parada="fim_da_janela",
        condicoes_falseamento=[
            # A obrigatoria: sobre a metrica primaria, `menor_que`, com valor
            # igual ao efeito minimo. E o schema que exige essa forma, e nao
            # uma escolha - ficar abaixo do proprio efeito minimo declarado
            # tem de refutar.
            ClausulaFalseamento(
                metrica=METRICA, comparador="menor_que", valor=efeito
            ),
            # Uma FACTUAL, que refuta sem depender de amostra: um giro
            # absurdo denuncia parametro degenerado, e essa e a falha que a
            # busca aleatoria produz de verdade.
            ClausulaFalseamento(
                metrica="idas_e_voltas", comparador="maior_que", valor=2_000
            ),
        ],
    )


def gerar(
    *,
    config: ExperimentConfig,
    duracao_barra_ms: int,
    horizonte_barras: int,
    semente: int | None = None,
) -> list[Candidata]:
    """As 16 candidatas de B4. Deterministico: mesma semente, mesma lista.

    A varredura vem PRIMEIRO e a busca aleatoria depois, e a ordem e fixa
    porque `test_credit_entry` e `hypothesis` sao append-only: a ordem de
    gravacao entra no digest do braco, e uma ordem que dependesse de dicionario
    faria o digest variar entre execucoes identicas.
    """
    base = config.default_seed if semente is None else semente
    fracao = 10_000  # todo o caixa - o mesmo default de `Regra`, sem escolha

    def monta(indice: int, tecnica: str, params) -> Candidata:
        return Candidata(
            indice=indice,
            tecnica=tecnica,
            regra=Regra(
                params=params,
                position_fraction_bps=fracao,
                # A MESMA procedencia que o agente carimba, da mesma
                # funcao: uma regra de B4 nao vale sob condicoes
                # diferentes por ter vindo de sorteio.
                condicoes_validade=condicoes_da_config(config),
            ),
            pre_registro=_pre_registro(
                tecnica,
                params,
                fracao_bps=fracao,
                config=config,
                duracao_barra_ms=duracao_barra_ms,
                horizonte_barras=horizonte_barras,
            ),
        )

    saida: list[Candidata] = []
    for i, (familia, kwargs) in enumerate(GRADE[:QUANTAS_VARREDURA]):
        classe = {
            "cruzamento_medias": CruzamentoMedias,
            "banda_desvio": BandaDesvio,
            "breakout_canal": BreakoutCanal,
        }[familia]
        saida.append(monta(i, "varredura_de_parametro", classe(**kwargs)))

    for j in range(QUANTAS_ALEATORIAS):
        rng = random.Random(_semente_de(base, j))
        saida.append(
            monta(QUANTAS_VARREDURA + j, "busca_aleatoria", _params_aleatorios(rng))
        )
    return saida


def digest(candidatas: list[Candidata]) -> str:
    """Hash do CONJUNTO gerado, para a prova de reprodutibilidade (R12).

    Cobre a regra e o pre-registro de cada candidata, na ordem. Nao cobre
    resultado nenhum: este digest responde "a busca produziu as mesmas
    hipoteses?", e nao "elas renderam o mesmo dinheiro?" - essa segunda
    pergunta e do `digest_do_run`, que ja existe e sai dos lancamentos.
    """
    cru = json.dumps(
        [c.como_dict() for c in candidatas],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(cru.encode("utf-8")).hexdigest()
