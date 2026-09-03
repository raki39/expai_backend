"""O pre-registro da secao 8.2, e o que dele o modelo tem permissao de dizer.

Na 0A a intencao era uma frase: *"espero entre 3 e 8 operacoes e desempenho
abaixo do buy-and-hold"*. Ela cumpria a regra 17 - declarada antes da execucao,
imutavel, com confianca ao lado - e nao cumpria mais nada. Julgar se ela se
realizou exigia leitura humana, e foi por isso que a avaliacao posterior da 0A
fechou com `veredito_da_expectativa = None`.

A secao 8.2 pede outra coisa. O pre-registro tem dez campos, e dois deles -
`efeito_minimo` e `condicoes_falseamento` - so existem para tornar o veredito
uma CONTA. Este modulo e a metade que o modelo preenche.

## O que o modelo declara, e o que ele nao declara

| Campo da secao 8.2 | Quem preenche | Por que |
|---|---|---|
| `id`, `timestamp_registro` | nos | fato do registro |
| `agente_origem` | nos | identidade nao se autodeclara |
| `enunciado` | **modelo** | a afirmacao falsificavel e dele |
| `metrica_primaria` | **modelo**, de enum fechado | uma so, antes do teste |
| `efeito_minimo` | **modelo** | o que importa economicamente e juizo dele |
| `n_minimo` | **calculado** | R34: por poder estatistico, nunca escolhido |
| `criterio_parada` | **modelo**, de enum fechado | |
| `condicoes_validade` | nos, da config | deixa-lo carimbar a propria procedencia seria o oposto de procedencia |
| `condicoes_falseamento` | **modelo** | so ele pode dizer o que refutaria a propria ideia |

`n_minimo` sair do modelo seria o buraco inteiro: ele escolheria a amostra que
o favorece. Ele declara o Sharpe que espera - uma afirmacao falsificavel, que
custa caro se for otimista, porque Sharpe alto exige MENOS amostra e portanto
promete mais - e `poder.n_minimo` faz a conta.

## Enum fechado de metrica, e nao texto

Metrica livre por hipotese tornaria a familia estatistica incoerente: BY ordena
p-valores que precisam medir a mesma coisa. Alem disso, "supera o baseline" e
"tem Sharpe alto" nao sao comparaveis, e uma familia que mistura as duas nao e
uma familia - e duas, cada uma com sua propria multiplicidade.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Metricas do enum fechado. Todas em inteiros de precisao fixa: as monetarias
# por causa da regra 5, e `idas_e_voltas` porque contagem nao tem fracao.
#
# `idas_e_voltas` e a mesma quantidade que o resto do sistema conta, vinda da
# mesma funcao. A setima ocorrencia do padrao neste projeto foi exatamente uma
# tabela que punha `execucoes` do agente ao lado de `operacoes` do controle
# sob um rotulo so.
Metrica = Literal[
    "patrimonio_final_cents",
    "excesso_sobre_b1_p50_cents",
    "excesso_sobre_b2_cents",
    "excesso_sobre_b3_cents",
    "idas_e_voltas",
]

METRICAS: tuple[str, ...] = (
    "patrimonio_final_cents",
    "excesso_sobre_b1_p50_cents",
    "excesso_sobre_b2_cents",
    "excesso_sobre_b3_cents",
    "idas_e_voltas",
)

# Metricas que sao FATO, e nao estimativa.
#
# A distincao decide quando a amostra e exigida. Secao 14.4: "Rejeitado: um
# criterio foi ESTATISTICAMENTE rejeitado com amostra suficiente." Um excesso
# em centavos e uma estimativa ruidosa de uma vantagem - dizer que ela nao
# existe exige amostra. Quantas vezes a regra comprou e vendeu nao e
# estimativa de nada: e uma contagem, e ela e o que e com uma execucao ou com
# mil.
#
# Sem esta separacao o veredito quebra de um jeito que nao aparece: a clausula
# obrigatoria sobre a metrica primaria e sempre "efeito < minimo", entao ela
# dispararia antes da conferencia de amostra e `inconclusiva` viraria um ramo
# INALCANCAVEL - matando exatamente a distincao que a R51 existe para manter.
METRICAS_FACTUAIS: frozenset[str] = frozenset({"idas_e_voltas"})

CriterioParada = Literal[
    "fim_da_janela",
    "n_minimo_alcancado",
    "falseamento_observado",
]

CRITERIOS_PARADA: tuple[str, ...] = (
    "fim_da_janela",
    "n_minimo_alcancado",
    "falseamento_observado",
)

# Dois comparadores, e so dois. Igualdade exata sobre um inteiro de centavos
# nunca dispararia, e uma condicao de falseamento que nao pode disparar e
# pior que nenhuma: ela parece proteger.
Comparador = Literal["menor_que", "maior_que"]

MAX_CHARS_ENUNCIADO = 1_200
MAX_CLAUSULAS = 6

# Faixa do Sharpe declaravel, em milesimos. O teto existe porque a conta de
# poder e uma DIVISAO por Sharpe ao quadrado: declarar Sharpe 50 pediria
# quatorze barras de amostra e aprovaria qualquer coisa. Secao 8.3 lista
# Sharpe 3,0 como ja exigindo so cinco meses; acima de 5,0 nao ha hipotese
# honesta em mercado liquido, ha erro de unidade.
SHARPE_MIN_MILESIMOS = 100      # 0,10
SHARPE_MAX_MILESIMOS = 5_000    # 5,00


class ClausulaFalseamento(BaseModel):
    """Uma condicao que, se observada, considera a hipotese falsa.

    Secao 8.2: "O campo de falseamento e obrigatorio. Uma hipotese que nao
    pode ser refutada nao entra no sistema."

    Estruturada, e nao em prosa, porque e sobre ela que o veredito e
    calculado. Em prosa ela seria exatamente o que a 0A ja tinha.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    metrica: Metrica
    comparador: Comparador
    valor: int

    def disparou(self, observado: int) -> bool:
        if self.comparador == "menor_que":
            return observado < self.valor
        return observado > self.valor

    def como_texto(self) -> str:
        sinal = "<" if self.comparador == "menor_que" else ">"
        return f"{self.metrica} {sinal} {self.valor}"


class PreRegistroBruto(BaseModel):
    """A metade do pre-registro que vem do modelo. Ainda sem procedencia."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enunciado: str = Field(min_length=1, max_length=MAX_CHARS_ENUNCIADO)
    metrica_primaria: Metrica
    efeito_minimo: int
    sharpe_esperado_milesimos: int = Field(
        ge=SHARPE_MIN_MILESIMOS, le=SHARPE_MAX_MILESIMOS
    )
    criterio_parada: CriterioParada
    condicoes_falseamento: list[ClausulaFalseamento] = Field(
        min_length=1, max_length=MAX_CLAUSULAS
    )

    @model_validator(mode="after")
    def _falseamento_serve_para_algo(self) -> "PreRegistroBruto":
        """Uma clausula que nunca dispararia nao refuta nada.

        O caso concreto: declarar `metrica_primaria = patrimonio_final_cents`
        com `efeito_minimo = 110000` e depois falsear com
        `patrimonio_final_cents < 0`. Formalmente ha condicao de falseamento;
        na pratica so um colapso total a dispararia, e a hipotese ficou
        irrefutavel dentro da faixa que interessa.

        A conferencia possivel aqui e a de coerencia: ao menos uma clausula
        precisa tocar a metrica primaria, e ela precisa estar do lado que de
        fato contradiz o efeito minimo declarado.
        """
        na_primaria = [
            c for c in self.condicoes_falseamento
            if c.metrica == self.metrica_primaria
        ]
        if not na_primaria:
            raise ValueError(
                "ao menos uma condicao de falseamento precisa incidir sobre a"
                f" metrica primaria ('{self.metrica_primaria}'); sem isso a"
                " hipotese declara um efeito que nada observavel contradiz"
            )
        if not any(
            c.comparador == "menor_que" and c.valor >= self.efeito_minimo
            for c in na_primaria
        ):
            raise ValueError(
                "a condicao de falseamento sobre a metrica primaria precisa"
                f" ser 'menor_que' com valor >= efeito_minimo"
                f" ({self.efeito_minimo}); do contrario ficar abaixo do efeito"
                " que a propria hipotese chamou de minimo nao a refutaria"
            )
        duplicadas = {
            (c.metrica, c.comparador) for c in self.condicoes_falseamento
        }
        if len(duplicadas) != len(self.condicoes_falseamento):
            raise ValueError(
                "duas condicoes de falseamento sobre a mesma metrica e o mesmo"
                " comparador: uma delas e redundante ou contraditoria"
            )
        return self


def hash_do_conteudo(
    bruto: PreRegistroBruto, condicoes_validade: dict, agente_origem: str
) -> str:
    """Hash do CONTEUDO da hipotese, estavel a ordem de escrita.

    E o que permite reconhecer o reteste da MESMA hipotese - 1 credito contra
    3, secao 8.6.1 - sem depender de alguem lembrar. Nao entram `run_id`,
    `timestamp_registro` nem o id: duas gravacoes da mesma afirmacao em
    momentos diferentes SAO a mesma afirmacao, e e disso que a contagem de
    tentativas precisa.
    """
    payload = {
        "agente_origem": agente_origem,
        "condicoes_validade": condicoes_validade,
        "criterio_parada": bruto.criterio_parada,
        "efeito_minimo": bruto.efeito_minimo,
        "enunciado": bruto.enunciado.strip(),
        "falseamento": sorted(
            [c.metrica, c.comparador, c.valor]
            for c in bruto.condicoes_falseamento
        ),
        "metrica_primaria": bruto.metrica_primaria,
        "sharpe_esperado_milesimos": bruto.sharpe_esperado_milesimos,
    }
    cru = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(cru.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# O schema enviado ao provedor
# ---------------------------------------------------------------------------
#
# Mesma disciplina do `contrato.py` da 0A: este schema e DICA, e a validacao
# em Python e o portao. E plano onde da, porque `oneOf` com discriminador e
# onde as implementacoes de saida estruturada mais divergem entre provedores.
#
# Aqui `condicoes_falseamento` e um array de objetos, que os dois provedores
# suportam. O `minItems` vai junto e e conselho; quem recusa de verdade e o
# `min_length` do pydantic acima, e depois dele o CHECK da migracao 9.

SCHEMA_PRE_REGISTRO: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "enunciado",
        "metrica_primaria",
        "efeito_minimo",
        "sharpe_esperado_milesimos",
        "criterio_parada",
        "condicoes_falseamento",
    ],
    "properties": {
        "enunciado": {
            "type": "string",
            "maxLength": MAX_CHARS_ENUNCIADO,
            "description": (
                "A afirmacao falsificavel, em uma ou duas frases: o que esta"
                " regra deve produzir e por que. Declarada ANTES de qualquer"
                f" execucao. No maximo {MAX_CHARS_ENUNCIADO} caracteres -"
                " passar disso faz a proposta inteira ser rejeitada."
            ),
        },
        "metrica_primaria": {
            "type": "string",
            "enum": list(METRICAS),
            "description": (
                "A UNICA metrica pela qual esta hipotese sera julgada."
                " Valores monetarios em centavos de USD; 'idas_e_voltas' e"
                " contagem de compras seguidas de venda."
            ),
        },
        "efeito_minimo": {
            "type": "integer",
            "description": (
                "Tamanho de efeito minimo que IMPORTA economicamente, na"
                " unidade da metrica primaria. Nao e o resultado esperado: e"
                " o piso abaixo do qual o resultado, mesmo positivo, nao"
                " valeria o custo."
            ),
        },
        "sharpe_esperado_milesimos": {
            "type": "integer",
            "description": (
                "Sharpe anualizado esperado, em MILESIMOS (1,25 -> 1250)."
                f" Entre {SHARPE_MIN_MILESIMOS} e {SHARPE_MAX_MILESIMOS}."
                " Deste numero sai a amostra minima necessaria: Sharpe alto"
                " exige menos amostra, entao declarar alto sem base torna a"
                " hipotese facil de aprovar e facil de refutar."
            ),
        },
        "criterio_parada": {
            "type": "string",
            "enum": list(CRITERIOS_PARADA),
            "description": "Quando o teste desta hipotese termina.",
        },
        "condicoes_falseamento": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_CLAUSULAS,
            "description": (
                "O que precisaria ser OBSERVADO para considerar esta hipotese"
                " falsa. Obrigatorio: hipotese que nao pode ser refutada nao"
                " entra. Ao menos uma clausula precisa incidir sobre a metrica"
                " primaria, com comparador 'menor_que' e valor maior ou igual"
                " ao efeito minimo declarado."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["metrica", "comparador", "valor"],
                "properties": {
                    "metrica": {"type": "string", "enum": list(METRICAS)},
                    "comparador": {
                        "type": "string",
                        "enum": ["menor_que", "maior_que"],
                    },
                    "valor": {"type": "integer"},
                },
            },
        },
    },
}
