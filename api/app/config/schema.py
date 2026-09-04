"""Parametros do experimento: o que vive no banco, versionado (ADR 0008).

Nada aqui e segredo e nada aqui e bootstrap. Estes campos sao editaveis pelo
painel, toda alteracao cria uma nova `config_version` com autor, data, valor
anterior e novo, e alteracao material invalida comparacao com runs anteriores
(secao 10.2.3).

Valores monetarios em **inteiros de centavos**; taxas e precos em `Decimal`.
Nunca ponto flutuante para dinheiro (secao 5).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Campos que NAO entram no `config_hash` e nao contam como mudanca material.
# `default_seed` fica de fora porque trocar a semente e reexecucao legitima
# (secao 14.4.1): semente diferente, mesmo `config_hash`.
CAMPOS_NAO_MATERIAIS = frozenset({"default_seed", "note"})


class ModeloTier(BaseModel):
    """Um degrau do catalogo: o agente pede um tier, nao um modelo (secao 3.9)."""

    model_config = ConfigDict(frozen=True)

    provider: Literal["anthropic", "openai"]
    model: str = ""  # vazio = tier nao configurado ainda


class PrecoModelo(BaseModel):
    """Uma linha da tabela de precos.

    Secao 3.9: "Tabela de precos versionada e datada. Preco interno e dado de
    configuracao, com data de verificacao, nunca constante no codigo."
    """

    model_config = ConfigDict(frozen=True)

    provider: Literal["anthropic", "openai"]
    model: str
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    # Secao 3.3: contexto repetido custa 10% do preco de entrada.
    cache_read_usd_per_mtok: Decimal | None = None
    # Escrever no cache custa 1,25x a entrada (janela de 5 min). Ficou como
    # None ate o incremento 5, quando passou a importar: a PRIMEIRA chamada de
    # cada run grava o prefixo no cache, e sem este preco o custo dela sairia
    # incompleto - declarado como incompleto, mas incompleto.
    cache_write_usd_per_mtok: Decimal | None = None
    # None = nao verificado. "Nao sei" nao e a mesma coisa que zero.
    verified_at: str | None = None


class ExperimentConfig(BaseModel):
    """Configuracao completa do experimento da Fase 0A."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ------------------------------------------------ mercado (ADR 0001)
    market_venue: str = "binance"
    market_symbol: str = "BTCUSDT"
    market_kind: Literal["spot"] = "spot"
    position_mode: Literal["long_flat"] = "long_flat"

    # ------------------------------------ dados e janela (ADR 0002, 0006)
    timeframe: str = "15m"
    # Janela de 24 meses exatos, terminando num mes ja publicado pela origem.
    # O `data_end` e EXCLUSIVO: 2026-08-01 significa "ate o fim de julho".
    # Ver ADR 0013 - a janela anterior terminava em 2026-09-01, que a origem
    # ainda nao tinha publicado.
    data_start: str = "2024-08-01"
    data_end: str = "2026-08-01"
    # Ultimos ~20% reservados na ingestao, excluidos do loader do experimento.
    reserved_fraction: Decimal = Decimal("0.20")
    # Nivel 1 = candles OHLCV. Declarado e propagado a todo resultado
    # (secao 8.4.1.1). Nivel 2 nao cabe no volume Hobby em janela longa.
    fidelity_level: int = Field(default=1, ge=1, le=5)

    # ------------------------------- simulador pessimista (secao 8.4.1)
    # Sempre taker. Spread por cima, slippage sempre desfavoravel, atraso
    # entre decisao e execucao, penalidade adicional explicita.
    #
    # Qual preco da barra de execucao serve de REFERENCIA (ADR 0015):
    #
    #   abertura       o preco no instante da execucao. A ordem entra no
    #                  inicio da barra i+1, e e isso que ela encontra.
    #   limite_adverso maxima na compra, minima na venda. Supoe azar maximo
    #                  intrabarra em toda execucao - e exige conhecer a barra
    #                  inteira, o que e retrospectiva, ainda que contra nos.
    #
    # ESTE CAMPO PRECISA SER VERSIONADO. O modelo mora no codigo, e sem ele
    # aqui dois runs reportariam o mesmo config_hash com semanticas de
    # execucao diferentes - o que torna a comparacao entre eles invalida sem
    # que nada acuse.
    execution_reference: Literal["abertura", "limite_adverso"] = "abertura"

    taker_fee_bps: Decimal = Decimal("10")     # Binance spot: 0,10%
    spread_bps: Decimal = Decimal("1")         # spread cheio, aplicado pela metade em cada lado
    slippage_bps: Decimal = Decimal("2")
    latency_bars: int = Field(default=1, ge=1)
    penalty_bps: Decimal = Decimal("1")

    # ------------------------------ taxonomia de regimes (ADR 0026, Fase 0C)
    #
    # CONGELADOS antes do forward. Vivem aqui, e nao so como constante em
    # `app/regime`, pelo MESMO argumento que `execution_reference` carrega
    # escrito em si mesmo: dois runs com taxonomias diferentes reportariam o
    # mesmo `config_hash` com semanticas de regime diferentes, e a comparacao
    # entre eles ficaria invalida sem que nada acusasse.
    #
    # A escolha destes numeros nao foi de gosto. A proposta original era uma
    # grade direcao x volatilidade com permanencia de um dia, e a medicao
    # sobre as 70.080 barras a derrubou: ela satisfazia ">= 2 regimes" em
    # 97,3% das janelas de 30 dias, ou seja NAO FILTRAVA NADA - a "definicao
    # frouxa" que secao 19.2 alerta. E o eixo de direcao destruia a
    # persistencia do de volatilidade, porque a celula conjunta herda a
    # persistencia da dimensao MENOS persistente.
    #
    # Procedencia completa em `.docs/adr/0026-taxonomia-de-regimes.md` e nos
    # scripts de `.docs/medicoes/0026-regimes/`.
    regime_corte_inferior_mili_bps: int = Field(default=19_300, gt=0)
    regime_corte_superior_mili_bps: int = Field(default=25_300, gt=0)
    regime_janela_barras: int = Field(default=672, gt=0)        # 7 dias a 15 min
    regime_permanencia_barras: int = Field(default=672, gt=0)   # 7 dias CONSECUTIVOS
    regime_minimos_para_cobertura: int = Field(default=2, ge=1)

    # ------------------------------------------- economia (ADR 0003/0006)
    seed_capital_usd_cents: int = Field(default=100_000, gt=0)  # US$ 1.000

    # Tetos OPERACIONAIS. O limite inviolavel e LLM_MAX_USD_ABSOLUTE, no env,
    # e esta config nao pode exceder aquele valor (secao 12.1).
    max_llm_calls_per_run: int = Field(default=12, ge=0)
    max_llm_usd_per_run_cents: int = Field(default=200, ge=0)  # US$ 2,00

    # --------------------------------------- provedores (ADR 0003 e 0009)
    tiers: dict[str, ModeloTier] = Field(
        default_factory=lambda: {
            "padrao": ModeloTier(provider="anthropic", model="claude-sonnet-5"),
            "topo": ModeloTier(provider="anthropic", model="claude-opus-5"),
            # Segundo provedor exigido pela secao 3.9, escolhido em 2026-09-02.
            # Existe para PROVAR que a troca de provedor e viavel (criterio
            # 7b), nao para rodar o experimento - por isso o degrau barato.
            "padrao_alt": ModeloTier(provider="openai", model="gpt-5.6-luna"),
            # Sem modelo ainda: um tier de topo alternativo so faria sentido
            # se o experimento fosse rodar no segundo provedor, e nao vai.
            # Deixar vazio e a verdade; preencher "por simetria" seria
            # declarar um preco que ninguem conferiu.
            "topo_alt": ModeloTier(provider="openai", model=""),
        }
    )

    # Conferida na pagina de precos do provedor em 2026-09-02, inclusive a
    # coluna de escrita de cache, que faltava. ATENCAO: `price_table` e
    # MATERIAL de proposito. Parece dado administrativo, mas o custo alimenta
    # o teto de gasto, e o teto decide quantas reflexoes cabem num run - entao
    # trocar preco pode mudar o caminho de decisao. Por isso mexer aqui exige
    # nova `config_version` e invalida comparacao que atravesse a mudanca.
    price_table_version: str = "2026-09-02"
    price_table: list[PrecoModelo] = Field(
        default_factory=lambda: [
            PrecoModelo(
                provider="anthropic",
                model="claude-sonnet-5",
                input_usd_per_mtok=Decimal("2.00"),
                output_usd_per_mtok=Decimal("10.00"),
                cache_read_usd_per_mtok=Decimal("0.20"),
                cache_write_usd_per_mtok=Decimal("2.50"),
                verified_at="2026-09-02",
            ),
            PrecoModelo(
                provider="anthropic",
                model="claude-opus-5",
                input_usd_per_mtok=Decimal("5.00"),
                output_usd_per_mtok=Decimal("25.00"),
                cache_read_usd_per_mtok=Decimal("0.50"),
                cache_write_usd_per_mtok=Decimal("6.25"),
                verified_at="2026-09-02",
            ),
            PrecoModelo(
                provider="openai",
                model="gpt-5.6-luna",
                input_usd_per_mtok=Decimal("0.20"),
                output_usd_per_mtok=Decimal("1.20"),
                cache_read_usd_per_mtok=Decimal("0.02"),
                # NULO de proposito, e o par coerente: este provedor nao cobra
                # escrita de cache e nao reporta o token dela. Preencher com
                # zero afirmaria que nada foi escrito, que e coisa diferente
                # de nao haver preco - e e exatamente o caso que o criterio 7c
                # existe para testar.
                cache_write_usd_per_mtok=None,
                verified_at="2026-09-02",
            ),
        ]
    )

    # ------------------------------------- ponte de cambio (secao 5.1)
    # Fixada por periodo e registrada em cada evento, para que variacao
    # cambial nao seja confundida com desempenho do agente (secao 4.2).
    fx_brl_per_usd: Decimal = Decimal("5.40")
    fx_rate_date: str = "2026-09-01"

    # -------------------------------------------- baselines (secao 14.3)
    b1_repetitions: int = Field(default=1000, ge=1000)  # minimo do documento
    # Congelados antes do primeiro run do agente (ADR 0006). Alterar depois
    # de ver o resultado destroi o grupo de controle.
    b3_fast: int = Field(default=20, ge=1)
    b3_slow: int = Field(default=50, ge=2)

    # ---------------------------- familia fechada e FDR (secao 8.6, 0B)
    #
    # Todos MATERIAIS, e nao por formalidade: o limiar efetivo decide o que
    # e promovido, e o teto da familia decide quantas hipoteses cabem no
    # lote. Trocar qualquer um deles muda o resultado do experimento.
    #
    # "Numero maximo de hipoteses: fixado antes de comecar, NAO AJUSTAVEL
    # DURANTE" (secao 8.6). A trava vive no BANCO: um gatilho recusa a
    # hipotese de numero 49 sob a config que abriu o run. Aqui esta o valor;
    # a imposicao esta na migracao 12.
    familia_max_hipoteses: int = Field(default=48, ge=1)      # D25

    # "BH, ou BY se a estrutura de dependencia exigir; escolhido ANTES da
    # primeira hipotese" (secao 8.6). D26 escolheu BY: as hipoteses do lote
    # sao variacoes de parametro da mesma estrategia sobre a mesma serie, e
    # o documento chama isso de "altamente dependentes".
    fdr_procedimento: Literal["BH", "BY"] = "BY"             # D26

    # Alvo de 10%, literal da secao 8.6. Em bps para nao haver ponto
    # flutuante decidindo promocao.
    fdr_alvo_bps: int = Field(default=1_000, ge=1, le=10_000)

    # Limiar do Deflated Sharpe Ratio (secao 8.6, criterio B6 de 14.4).
    # "O DSR e uma PROBABILIDADE, nao um score" - 0,95 em milesimos.
    dsr_minimo_milesimos: int = Field(default=950, ge=1, le=1_000)

    # -------------------------------- creditos de teste (secao 8.6.1, D30)
    # Pesos 1/3/5/10 sao do documento e nao sao configuraveis: mexer neles
    # seria reprecificar o que a secao 8.6.1 fixa. O orcamento e nosso.
    creditos_por_braco: int = Field(default=60, ge=1)        # D30

    # ------------------------------------- A1b: o calibre (secao 14.4, D29)
    #
    # MATERIAIS, e o criterio 6 do incremento 13 exige que estejam aqui:
    # "o IC foi definido ANTES do teste e esta na config versionada, com data
    # anterior a primeira execucao. Verificavel no historico da config, nao
    # na nossa palavra."
    #
    # Um numero de execucoes ou um IC que morassem em constante de codigo
    # teriam de ser acreditados; aqui eles tem autor, data e valor anterior
    # (secao 10.2.3), e a primeira execucao de A1b e posterior a linha do
    # historico que os fixou.
    a1b_execucoes: int = Field(default=200, ge=1)            # D29
    a1b_ic_bps: int = Field(default=9_500, ge=1, le=9_999)   # D29: IC 95%

    # O tamanho do lote de cada execucao repetida. **48, o mesmo da familia
    # real** - a multiplicidade que o calibre mede precisa ser a que a fase
    # enfrenta. Um lote menor mediria BY sob uma correcao mais fraca do que a
    # que promove de verdade.
    a1b_lote: int = Field(default=48, ge=2)

    # Quantas das `a1b_lote` carregam SINAL IMPLANTADO no desenho 2. O
    # documento pede o desenho, e nao a proporcao; 12 de 48 (25%) foi fixada
    # antes da primeira execucao porque ela decide o denominador: com poucos
    # sinais, `R` e quase sempre zero e `V / max(R,1)` deixa de ser uma razao
    # para virar um indicador de "houve alguma promocao".
    #
    # As 12 sao divididas em DUAS magnitudes, e as duas sao DERIVADAS - nao ha
    # campo de magnitude aqui de proposito. Uma delas sai da conta de poder
    # (§8.3) e a outra do limiar de BY; fixar qualquer das duas como numero na
    # config a faria parar de descrever no dia em que o horizonte ou o teto da
    # familia mudassem, que e o padrao que este projeto ja registrou treze
    # vezes. Ver `a1b/calibre.magnitudes`.
    a1b_sinais_implantados: int = Field(default=12, ge=2)

    # ------------------------------------------------------ nao materiais
    default_seed: int = 42
    note: str = ""

    @model_validator(mode="after")
    def _coerencia(self) -> "ExperimentConfig":
        if self.b3_fast >= self.b3_slow:
            raise ValueError("b3_fast precisa ser menor que b3_slow")
        if not (Decimal("0") < self.reserved_fraction < Decimal("1")):
            raise ValueError("reserved_fraction precisa estar entre 0 e 1")
        if self.data_start >= self.data_end:
            raise ValueError("data_start precisa ser anterior a data_end")
        # Um tier configurado precisa ter preco datado, senao o custo por
        # decisao vira estimativa - o que a secao 5.2 proibe.
        precos = {(p.provider, p.model) for p in self.price_table}
        for nome, tier in self.tiers.items():
            if tier.model and (tier.provider, tier.model) not in precos:
                raise ValueError(
                    f"tier '{nome}' aponta para {tier.provider}/{tier.model}, "
                    "que nao tem linha na tabela de precos"
                )
        return self

    # ------------------------------------------------------------ hashing
    def payload_material(self) -> dict:
        """Campos que definem o experimento, em forma canonica."""
        bruto = self.model_dump(mode="json")
        return {k: v for k, v in bruto.items() if k not in CAMPOS_NAO_MATERIAIS}

    def config_hash(self) -> str:
        canonico = json.dumps(
            self.payload_material(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonico.encode("utf-8")).hexdigest()

    def diff(self, outra: "ExperimentConfig") -> list[tuple[str, object, object]]:
        """Campos que mudam de `self` para `outra`, em ordem estavel."""
        a = self.model_dump(mode="json")
        b = outra.model_dump(mode="json")
        return [
            (campo, a[campo], b[campo])
            for campo in sorted(set(a) | set(b))
            if a.get(campo) != b.get(campo)
        ]


def campo_material(campo: str) -> bool:
    return campo not in CAMPOS_NAO_MATERIAIS
