"""As seis famílias de defeito de §14.4, uma constante por família.

> "**Determinístico** | Acesso explícito ao futuro; duplicação disfarçada de
> hipótese; operação que só lucra quando custos são ignorados; violação
> conhecida do embargo; preço impossível no nível de fidelidade declarado;
> adulteração proposital do ledger | Existe um **defeito** no pipeline |
> Zero" — §14.4

A lista é **fechada e citada**, e não uma escolha nossa: são as seis que o
documento nomeia, na ordem em que ele as nomeia. Acrescentar uma sétima seria
inventar critério; deixar uma de fora seria deixar de testar uma família de
defeito que o documento manda testar. Há teste conferindo os dois lados.

## Estrutural e estatístico não são a mesma prova

Cada controle declara o que se espera dele:

- **estrutural** — a injeção tem de ser **recusada**, por CHECK, gatilho ou
  fronteira. `barrado: true` é a prova positiva de que a guarda existe e
  disparou; um controle estrutural que atravessa é informação grave mesmo sem
  ser promovido.
- **estatístico** — a injeção **completa**, e o que não pode acontecer é a
  hipótese ser promovida. Aqui `barrado: false` é o esperado.

O portão de §14.4 é literal e é um só: **uma única promoção reprova a fase**.
Não estendemos o portão para "controle estrutural que não foi barrado" —
apertar um critério por conta própria é a quinta pergunta do teste de escopo
olhando para nós. O relatório mostra a divergência; ele não a transforma em
reprovação.
"""

from __future__ import annotations

from dataclasses import dataclass

ESTRUTURAL = "estrutural"
ESTATISTICO = "estatistico"


@dataclass(frozen=True)
class Familia:
    chave: str
    #: A frase de §14.4, literal. É o que torna a lista conferível contra o
    #: documento em vez de contra a nossa memória dela.
    familia_de_defeito: str
    o_que_injeta: str
    guarda_esperada: str
    tipo: str

    def como_dict(self) -> dict:
        return {
            "chave": self.chave,
            "familia_de_defeito": self.familia_de_defeito,
            "o_que_injeta": self.o_que_injeta,
            "guarda_esperada": self.guarda_esperada,
            "tipo": self.tipo,
        }


FAMILIAS: tuple[Familia, ...] = (
    Familia(
        chave="acesso_ao_futuro",
        familia_de_defeito="acesso explícito ao futuro",
        o_que_injeta=(
            "duas tentativas: ler walk-forward pelo caminho do agente, e"
            " gravar uma execução na MESMA barra da decisão"
        ),
        guarda_esperada=(
            "split.exigir_do_agente (a finalidade não é do agente) e o CHECK"
            " execution_bar_ms > decision_bar_ms, que torna a latência"
            " estrutural"
        ),
        tipo=ESTRUTURAL,
    ),
    Familia(
        chave="duplicacao_disfarcada",
        familia_de_defeito="duplicação disfarçada de hipótese",
        o_que_injeta=(
            "a mesma afirmação testável de uma hipótese já registrada, com o"
            " enunciado reescrito"
        ),
        guarda_esperada=(
            "o teto da família e o contador global contam a linha: a"
            " multiplicidade não é subestimada, e é ela que BY corrige"
        ),
        tipo=ESTATISTICO,
    ),
    Familia(
        chave="lucro_so_sem_custos",
        familia_de_defeito="operação que só lucra quando custos são ignorados",
        o_que_injeta=(
            "duas tentativas: declarar métrica primária sem custo, e executar"
            " giro alto para comparar o bruto com o líquido"
        ),
        guarda_esperada=(
            "o enum fechado de métrica não tem métrica bruta, e todas saem do"
            " ledger, que é líquido por construção"
        ),
        tipo=ESTATISTICO,
    ),
    # ------------------------------------------------------------------
    # LIMITE DECLARADO desta família, encontrado ao escrever o controle.
    #
    # Uma terceira forma do mesmo defeito NÃO é coberta aqui: `executor.rodar`
    # recebe o objeto `config` de quem o chama, e nada o amarra à
    # `config_version` do run. Em produção os dois vêm juntos de
    # `config_service.versao_atual`, mas estruturalmente é possível executar
    # com taxas zeradas sob um run cujo `config_version` diz que elas não são
    # zero — e o resultado ficaria comparável a baselines que pagaram custo.
    #
    # Não foi fechado neste incremento, e a razão está escrita para não virar
    # dívida invisível: a guarda natural é comparar o `config_hash` do objeto
    # com o da versão do run, e ela recusaria toda a suíte que hoje passa uma
    # config alterada em memória (`max_llm_calls_per_run=0`, por exemplo) sob a
    # `config_version` 1. Fechá-la exige decidir **quais** campos amarram — os
    # do modelo de custo, provavelmente — e isso é decisão sobre o experimento,
    # não detalhe de implementação.
    #
    # O que o controle cobre hoje: não dá para DECLARAR um alvo sem custo, e o
    # bruto e o líquido do run ficam medidos lado a lado, saindo da própria
    # decomposição do ledger.
    # ------------------------------------------------------------------
    Familia(
        chave="violacao_do_embargo",
        familia_de_defeito="violação conhecida do embargo",
        o_que_injeta=(
            "duas janelas de walk-forward inválidas: uma com purga zero, e"
            " outra declarando purga maior que o intervalo que remove"
        ),
        guarda_esperada=(
            "os gatilhos purga_zero_nao_e_purga e"
            " janela_respeita_a_purga_declarada, da migração 15"
        ),
        tipo=ESTRUTURAL,
    ),
    Familia(
        chave="preco_impossivel",
        familia_de_defeito="preço impossível no nível de fidelidade declarado",
        o_que_injeta=(
            "uma execução preenchida MELHOR que a referência adversa — o"
            " preenchimento maker que a fidelidade 1 não pode afirmar"
        ),
        guarda_esperada=(
            "o CHECK 'nunca favorável' da migração 4: na compra o executado"
            " não fica abaixo da referência; na venda, não fica acima"
        ),
        tipo=ESTRUTURAL,
    ),
    Familia(
        chave="ledger_adulterado",
        familia_de_defeito="adulteração proposital do ledger",
        o_que_injeta=(
            "duas tentativas: alterar um lançamento já gravado, e fechar uma"
            " transação com as partidas desequilibradas"
        ),
        guarda_esperada=(
            "os gatilhos de imutabilidade e de partidas dobradas da migração"
            " 3, que valem por livro"
        ),
        tipo=ESTRUTURAL,
    ),
)

#: O total é o da D25: seis dos 48 lugares da família fechada, um por família
#: de defeito. Não é coincidência de contagem — a D25 dimensionou a família a
#: partir desta lista.
QUANTAS = len(FAMILIAS)

POR_CHAVE: dict[str, Familia] = {f.chave: f for f in FAMILIAS}
