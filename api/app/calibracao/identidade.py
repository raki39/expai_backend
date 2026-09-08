"""A identidade EXECUTAVEL de um run. Uma definicao, num lugar so.

## O que mudou, e por que isto precisou existir

Ate o ADR 0033, `config_hash` identificava o comportamento: mesma config,
mesmo preco. O perfil de calibracao passou a viver **fora do payload** -
numa coluna, para nao mexer no hash das versoes ja gravadas -, e a consequencia
e que **`config_hash` sozinho parou de identificar o que executa**.

Medido na demonstracao da parte 2c corrigida: as versoes 1 e 2 tem o MESMO
`config_hash` (`ebf441d4db65`), e executam com precos diferentes -
`vol_baixa` a 1,674 bps numa, a 1,000 na outra.

**A identidade efetiva e o PAR:**

    config_hash + calibracao_perfil_hash

E ela precisa de uma definicao unica. Duas copias desta regra em dois modulos
divergiriam no primeiro que alguem esquecesse de atualizar - e este projeto
conta essa historia em `baselines.condicoes` contra
`contrato.condicoes_da_config`, e em `agente_estado` contra
`/api/baselines/curva`.

## A representacao canonica de "sem perfil"

`SEM_PERFIL` e um token literal, e nao `None` nem string vazia. Tres razoes:

- a string de identidade fica sempre bem formada, e comparar duas identidades
  nunca precisa de caso especial;
- `"None"` colidiria com um perfil que por acaso se chamasse assim - improvavel
  para um sha256, mas "improvavel" nao e "impossivel" e o custo de evitar e
  zero;
- e ele DIZ o que significa. Um campo vazio num relatorio e lido como "faltou
  preencher"; `sem-perfil` e lido como "este run rodou so com a base".
"""

from __future__ import annotations

# O token canonico. Nao e `None`, nao e "", e nao e "null".
SEM_PERFIL = "sem-perfil"

SEPARADOR = "+"


def identidade_executavel(
    config_hash: str, calibracao_perfil_hash: str | None
) -> str:
    """`config_hash + perfil_hash`, com o token canonico quando nao ha perfil.

    Esta e a string que responde "duas execucoes tem o mesmo comportamento
    esperado?". Ela NAO substitui o digest, que responde "e o resultado foi
    o mesmo?" - sao perguntas diferentes, e a segunda so tem sentido depois
    de a primeira dar sim.
    """
    return f"{config_hash}{SEPARADOR}{calibracao_perfil_hash or SEM_PERFIL}"


def do_run(conn, run_id: int) -> str:
    """A identidade executavel de um run, lida do que ficou GRAVADO.

    Do run, e nao da config vigente: um run antigo continua identificado pelo
    que ele usou, e nao pelo que passou a valer depois. E a mesma disciplina de
    `condicoes_do_run`, que le a config sob a qual o run foi ABERTO.
    """
    linha = conn.execute(
        "SELECT c.config_hash AS config_hash,"
        "       r.calibracao_perfil_hash AS perfil"
        "  FROM run r JOIN config_version c ON c.id = r.config_version_id"
        " WHERE r.id = ?",
        (run_id,),
    ).fetchone()
    if linha is None:
        raise ValueError(f"run {run_id} nao existe")
    return identidade_executavel(
        str(linha["config_hash"]), linha["perfil"]
    )


def perfil_da_config_version(conn, config_version_id: int) -> str | None:
    """O perfil que uma `config_version` carrega, ou `None`.

    Existe para que `abrir_run` grave no run o perfil VIGENTE naquela versao,
    sem precisar importar `perfil.py` - que traria o modulo de calibracao
    inteiro para dentro do ledger, e a fronteira entre eles vale mais que a
    conveniencia.
    """
    linha = conn.execute(
        "SELECT calibracao_perfil_hash FROM config_version WHERE id = ?",
        (config_version_id,),
    ).fetchone()
    return None if linha is None else linha["calibracao_perfil_hash"]
