"""O QUE o certificado certifica — e não é só o agente.

> *"O certificado não pode ter como alvo somente a `identidade_executavel` do
> agente. O Portão A também certifica o laboratório."* — o usuário, 2026-09-09

`identidade_executavel` responde *"duas execuções têm o mesmo comportamento
esperado?"* sobre a **configuração**. Ela não diz nada sobre o código que
julga: trocar `app/estatistica/fdr.py` deixa a identidade intacta e muda o que
o Portão A significa. O alvo de certificação fecha esse vão.

## Os sete componentes, e por que cada um

| componente | o que muda se ele mudar |
|---|---|
| `identidade_executavel` | o comportamento esperado da execução (config + perfil + contrato do agente) |
| `schema` | um gatilho novo muda **o que barra** — e são as guardas do banco que reprovam 4 dos 6 controles do A1a |
| `metodologia_estatistica` | a v1 media o MERCADO e a v2 mede a estratégia. O mesmo controle, dois vereditos |
| `suite_de_controles` | mudar o que se injeta muda o que se certifica |
| `fonte_de_dados` | certificar sobre outro dado é outra certificação |
| `implementacao` | o **laboratório**: validador, ledger, simulador, estatística, hipótese |
| `ambiente_de_execucao` | a mesma fórmula em outro Python ou outro SQLite pode dar outro número |

`build_do_backend` fica **fora do hash** e dentro do manifesto, como
rastreamento: senão uma vírgula no README invalidaria o Portão A.

## A implementação é o FECHAMENTO TRANSITIVO, e não uma lista

> *"O hash da implementação deve cobrir o fechamento transitivo dos módulos
> realmente usados pelo caminho, não apenas uma lista manual dos cinco
> pacotes."*

Uma lista manual é a forma do defeito que este projeto conta: alguém acrescenta
um módulo ao caminho, esquece a lista, e o hash passa a descrever menos código
do que o certificado afirma. Aqui o conjunto é **derivado** dos `import` a
partir das entradas, seguindo só o que é `app.*` — e um módulo novo entra
sozinho.

`ast`, e não `importlib`: percorrer os imports estaticamente não executa
código, e o resultado não depende de qual ramo rodou.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
import sqlite3
import sys

#: As ENTRADAS do caminho de certificação. Não é a lista dos módulos — é a
#: lista de por onde a certificação começa. O resto é derivado.
ENTRADAS = (
    "app.a1a.braco",
    "app.a1b.braco",
    "app.b4.braco",
    "app.maos_rapidas.baselines",
    "app.relatorio.portao_a",
    # O PROPRIO ARNES da certificacao. 2026-09-09.
    #
    # Medido: duas certificacoes da cv9 sairam com o MESMO `alvo_hash` tendo o
    # arnes mudado entre elas - a correcao de `bytes_em_disco` foi para o ar no
    # meio, e o alvo nao se moveu. Um certificado produzido por um arnes
    # diferente e um certificado diferente, e sem isto a reutilizacao por
    # identidade devolveria o certificado antigo para um mecanismo novo.
    #
    # Nao ha auto-referencia: o hash e sobre o CONTEUDO dos arquivos, e nenhum
    # arquivo contem o proprio hash.
    "app.certificacao.suite",
    "app.certificacao.laboratorio",
)

#: A raiz do pacote, para resolver `app.x.y` em arquivo.
_RAIZ = pathlib.Path(__file__).resolve().parent.parent.parent


def _arquivo_do_modulo(nome: str) -> pathlib.Path | None:
    base = _RAIZ / pathlib.Path(*nome.split("."))
    for candidato in (base.with_suffix(".py"), base / "__init__.py"):
        if candidato.is_file():
            return candidato
    return None


def _importados(arquivo: pathlib.Path, pacote: str) -> set[str]:
    """Os módulos `app.*` que este arquivo importa, absolutos ou relativos."""
    achados: set[str] = set()
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            for alias in no.names:
                if alias.name.startswith("app."):
                    achados.add(alias.name)
        elif isinstance(no, ast.ImportFrom):
            if no.level:
                # Relativo: sobe `level - 1` a partir do pacote do arquivo.
                partes = pacote.split(".")
                base = partes[: len(partes) - (no.level - 1)] or ["app"]
                prefixo = ".".join(base)
                alvo_mod = f"{prefixo}.{no.module}" if no.module else prefixo
            elif no.module and no.module.startswith("app."):
                alvo_mod = no.module
            else:
                continue
            if not alvo_mod.startswith("app"):
                continue
            achados.add(alvo_mod)
            # `from x import y` pode importar um SUBMODULO, e não um nome.
            for alias in no.names:
                achados.add(f"{alvo_mod}.{alias.name}")
    return achados


def fechamento_transitivo(entradas: tuple[str, ...] = ENTRADAS) -> list[str]:
    """Todo módulo `app.*` alcançável por import a partir das entradas.

    Derivado, e é isso que impede a lista de envelhecer. Nomes que não
    resolvem em arquivo são descartados — `from x import NOME` produz
    candidatos que são constantes, não módulos.
    """
    vistos: set[str] = set()
    fila = list(entradas)
    while fila:
        nome = fila.pop()
        if nome in vistos:
            continue
        arquivo = _arquivo_do_modulo(nome)
        if arquivo is None:
            continue
        vistos.add(nome)
        pacote = nome.rsplit(".", 1)[0] if "." in nome else nome
        fila.extend(_importados(arquivo, pacote) - vistos)
    return sorted(vistos)


#: A versão da política de canonicalização, **dentro do hash**.
#:
#: Sem ela, mudar a política produziria alvos diferentes sem que ninguém
#: soubesse **por quê** — e dois certificados incomparáveis pareceriam
#: descrever laboratórios diferentes quando o que mudou foi a régua. Com ela,
#: a mudança de política é visível no próprio número.
POLITICA_CANONICA = "canonico@1"

#: As extensões tratadas como TEXTO. O que não estiver aqui entra em bytes
#: crus — um `.py` normalizado e um `.png` normalizado são coisas muito
#: diferentes, e normalizar bytes de imagem corromperia o hash de um arquivo
#: que ninguém edita em editor de texto.
#:
#: Hoje o fechamento é 100% `.py`. A lista existe para o dia em que não for.
EXTENSOES_DE_TEXTO = frozenset({".py", ".pyi", ".txt", ".md", ".json", ".toml",
                                ".cfg", ".ini", ".sql", ".yml", ".yaml"})


def caminho_canonico(p: pathlib.Path) -> str:
    """O caminho relativo em POSIX. É ele que entra no hash **e** na ordem.

    `str(Path)` devolve `app\\a1a\\braco.py` no Windows e `app/a1a/braco.py`
    no Linux, e os separadores ordenam diferente: `\\` é 0x5C e `/` é 0x2F, os
    dois em relação a `.` (0x2E). Um conjunto que tivesse `app/x.py` **e**
    `app/x/algo.py` sairia em ordens diferentes nos dois sistemas.

    **Medido:** no fechamento de hoje as duas ordens coincidem — mas por acaso
    do conjunto, e não por construção (um pacote não coexiste com um módulo de
    mesmo nome). Ordenar pela mesma string que entra no hash tira a sorte da
    conta.
    """
    return p.relative_to(_RAIZ).as_posix()


def conteudo_canonico(p: pathlib.Path) -> bytes:
    """Os bytes que representam o conteúdo LÓGICO deste arquivo.

    ## Texto

    1. **BOM UTF-8 removido.** Um editor que grava BOM não muda o programa —
       Python ignora o BOM ao compilar —, e deixá-lo no hash faria "abri o
       arquivo no editor errado" virar recertificação;
    2. **CRLF e CR viram LF.** `\r\n` primeiro e `\r` solto depois: fazer o
       contrário transformaria `\r\n` em `\n\n` e mudaria o conteúdo em vez de
       normalizá-lo. O CR solto é o fim de linha do Mac clássico, e um arquivo
       assim é raro — mas raro não é ausente, e a ordem das duas substituições é
       de graça.

    ## Binário

    **Bytes crus.** Normalizar bytes de imagem, fonte ou `.pyc` corromperia o
    hash de um arquivo que ninguém edita em editor de texto: `0x0D0A` dentro de
    um PNG é dado, e não fim de linha.
    """
    bruto = p.read_bytes()
    if p.suffix.lower() not in EXTENSOES_DE_TEXTO:
        return bruto
    if bruto.startswith(b"\xef\xbb\xbf"):
        bruto = bruto[3:]
    return bruto.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _hash_de_arquivos(caminhos) -> str:
    """Sha256 do conteúdo LÓGICO, em ordem canônica e com o caminho dentro.

    ## O defeito que isto conserta — OP-2, medido em 2026-09-10

    O mesmo commit produzia alvos diferentes conforme o sistema de arquivos:

    ```
    local (Windows, 39 dos 69 com CRLF)   56d4a443ee51398b
    producao (Linux, LF)                  8bfbef7e1914003a
    ```

    A função lia `read_bytes()` cru, então ela media o **checkout** e não o
    conteúdo — e um certificado emitido no Linux não conferia no Windows sob o
    mesmo commit.

    ## Por que o caminho entra no hash

    Para que **renomear** um módulo mude o alvo: dois arquivos com o mesmo
    conteúdo em lugares diferentes são laboratórios diferentes. E ele entra em
    POSIX, que é a mesma string usada para ordenar — ver `caminho_canonico`.

    ## E por que a POLÍTICA entra também

    `POLITICA_CANONICA` vai no início do hash. Mudar a régua de canonicalização
    passa a ser visível **no número**, em vez de produzir dois certificados
    incomparáveis que parecem descrever laboratórios diferentes.
    """
    h = hashlib.sha256()
    h.update(POLITICA_CANONICA.encode("utf-8"))
    h.update(b"\x00")
    # Ordena pela MESMA string que entra no hash, e nao por `str(Path)`.
    pares = sorted(
        ((caminho_canonico(pathlib.Path(c)), pathlib.Path(c)) for c in caminhos),
        key=lambda par: par[0],
    )
    for relativo, p in pares:
        h.update(relativo.encode("utf-8"))
        h.update(b"\x00")
        h.update(conteudo_canonico(p))
        h.update(b"\x00")
    return h.hexdigest()


def hash_da_implementacao() -> tuple[str, list[str]]:
    """O laboratório, pelo fechamento transitivo. Devolve `(hash, modulos)`."""
    modulos = fechamento_transitivo()
    arquivos = [
        a for a in (_arquivo_do_modulo(m) for m in modulos) if a is not None
    ]
    return _hash_de_arquivos(arquivos), modulos


def hash_do_ambiente() -> dict:
    """Dockerfile, lock de dependências, e as versões que fazem a conta.

    **As estatísticas deste projeto não têm dependência de terceiros**: `math`,
    `statistics`, `fractions` e `random` vêm do interpretador. Então a versão
    do Python **é** a versão da biblioteca estatística, e é por isso que ela
    entra aqui em vez de uma linha de `requirements`.
    """
    componentes = {
        "python": sys.version.split()[0],
        # `sqlite_version` e a da BIBLIOTECA, que e a que faz a conta.
        # `sqlite3.version` (a do modulo) esta deprecada e sai no Python
        # 3.14 - por um campo desses o hash de ambiente quebraria no dia
        # da atualizacao, e ele existe para o contrario disso.
        "sqlite": sqlite3.sqlite_version,
        "biblioteca_estatistica": (
            "stdlib: math, statistics, fractions, random - sem dependencia de"
            " terceiros, entao a versao do Python E a versao dela"
        ),
    }
    # Os arquivos que definem o ambiente, e QUAIS DELES EXISTEM.
    #
    # Medido em producao em 2026-09-09: o `Dockerfile` **nao esta na imagem** -
    # ele copia `app/`, `requirements.txt`, `pytest.ini` e `start-backend.sh`,
    # e nao a si mesmo. A primeira versao desta funcao simplesmente pulava o
    # arquivo ausente, e o hash passava a cobrir menos em producao do que em
    # desenvolvimento **sem dizer**. Um componente que degrada em silencio e a
    # forma exata do padrao que este projeto conta.
    #
    # Agora a ausencia entra no hash: um ambiente onde o Dockerfile existe e um
    # onde ele nao existe sao ambientes diferentes, e o manifesto diz qual e.
    esperados = ("Dockerfile", "requirements.txt", "requirements-dev.txt")
    arquivos, ausentes = [], []
    for nome in esperados:
        alvo_p = _RAIZ / nome
        if alvo_p.is_file():
            arquivos.append(alvo_p)
            componentes[nome] = hashlib.sha256(
                alvo_p.read_bytes()
            ).hexdigest()[:16]
        else:
            ausentes.append(nome)
            componentes[nome] = None
    componentes["arquivos_ausentes"] = ausentes
    if ausentes:
        componentes["por_que_ausentes"] = (
            "estes arquivos nao estao no ambiente que executa. Na imagem da"
            " Railway isso e esperado para o `Dockerfile` e para o"
            " `requirements-dev.txt`: o build copia `app/`,"
            " `requirements.txt`, `pytest.ini` e `start-backend.sh`, e nao a"
            " si mesmo. A ausencia ENTRA no hash - um ambiente sem eles e"
            " outro ambiente, e o certificado nao pode fingir que cobriu o que"
            " nao viu"
        )
    h = hashlib.sha256()
    h.update(componentes["python"].encode())
    h.update(componentes["sqlite"].encode())
    h.update(("|".join(ausentes)).encode())
    if arquivos:
        h.update(_hash_de_arquivos(arquivos).encode())
    return {"hash": h.hexdigest(), "componentes": componentes}


def fonte_de_dados(
    *, dataset_hash: str | None = None, snapshot_hash: str | None = None
) -> dict:
    """União MARCADA: `{tipo, hash}`. Nunca id isolado, nunca XOR.

    > *"a fonte de dados deve ser uma união marcada `{tipo: dataset, hash}` ou
    > `{tipo: snapshot, hash}`, nunca ID isolado nem XOR aritmético."*

    O XOR estava no relatório da quarentena e é uma escolha ruim por dois
    motivos: ele **perde a informação de qual dos dois** é a fonte, e um XOR de
    hashes não é um hash — colisões deixam de ser improváveis e passam a ser
    construtíveis. Um id isolado é pior ainda: aponta para uma linha que pode
    ter sido reescrita ao redor.
    """
    if (dataset_hash is None) == (snapshot_hash is None):
        raise ValueError(
            "a fonte e um dataset OU um snapshot, e exatamente um dos dois:"
            " recebido dataset_hash="
            f"{dataset_hash!r}, snapshot_hash={snapshot_hash!r}"
        )
    if dataset_hash is not None:
        return {"tipo": "dataset", "hash": dataset_hash}
    return {"tipo": "snapshot", "hash": snapshot_hash}


def hash_do_schema(conn: sqlite3.Connection) -> dict:
    """A versão e o SQL efetivo das tabelas e gatilhos que existem.

    Do `sqlite_master`, e não do texto das migrações: o que barra a injeção é o
    gatilho **que está no banco**, e uma migração que falhou pela metade
    deixaria as duas coisas discordando.
    """
    from ..store import versao_schema

    linhas = [
        f"{r['type']}:{r['name']}:{r['sql'] or ''}"
        for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_master"
            " WHERE type IN ('table', 'trigger', 'index')"
            "   AND name NOT LIKE 'sqlite_%'"
            " ORDER BY type, name"
        )
    ]
    h = hashlib.sha256()
    for linha in linhas:
        h.update(linha.encode("utf-8"))
        h.update(b"\x00")
    return {
        "versao": versao_schema(conn),
        "hash": h.hexdigest(),
        "objetos": len(linhas),
    }


def hash_da_suite() -> dict:
    """O catálogo de controles e as injeções — o que a certificação injeta."""
    from ..a1a import catalogo

    arquivos = [
        _arquivo_do_modulo(m)
        for m in ("app.a1a.catalogo", "app.a1a.injecoes", "app.a1b.registro")
    ]
    return {
        "familias": [f.chave for f in catalogo.FAMILIAS],
        "quantas": catalogo.QUANTAS,
        "hash": _hash_de_arquivos([a for a in arquivos if a is not None]),
    }


def montar(
    conn: sqlite3.Connection,
    *,
    dataset_hash: str | None = None,
    snapshot_hash: str | None = None,
) -> dict:
    """O alvo inteiro, com os componentes publicados ao lado do hash.

    O hash sozinho é opaco: dois alvos diferentes só são úteis se quem lê
    consegue ver **qual** componente divergiu. É a mesma razão pela qual
    `lote_congelado` publica o diff material campo a campo em vez de só dizer
    `vigente_nao_certificada`.
    """
    from ..calibracao import identidade
    from ..config import service as config_service
    from ..validador import promocao

    atual = config_service.versao_atual(conn)
    if atual is None:
        raise ValueError("sem config vigente: nao ha identidade a certificar")

    impl_hash, modulos = hash_da_implementacao()
    componentes = {
        "identidade_executavel": identidade.identidade_executavel(
            atual.config_hash,
            identidade.perfil_da_config_version(conn, atual.id),
        ),
        "schema": hash_do_schema(conn),
        "metodologia_estatistica": {
            "versao": promocao.METODOLOGIA_VIGENTE["versao"],
            "estado": promocao.METODOLOGIA_VIGENTE["estado"],
            "serie": promocao.METODOLOGIA_VIGENTE["serie"],
        },
        "suite_de_controles": hash_da_suite(),
        "fonte_de_dados": fonte_de_dados(
            dataset_hash=dataset_hash, snapshot_hash=snapshot_hash
        ),
        "implementacao": {
            "hash": impl_hash,
            "modulos": modulos,
            "quantos": len(modulos),
            "como": (
                "fechamento transitivo dos imports `app.*` a partir de"
                f" {len(ENTRADAS)} entradas, por AST - derivado, e nao uma"
                " lista mantida a mao"
            ),
        },
        "ambiente_de_execucao": hash_do_ambiente(),
    }

    # A ORDEM entra no hash, e ela e a das chaves ordenadas: um dicionario
    # reordenado nao pode produzir alvo diferente.
    h = hashlib.sha256()
    for chave in sorted(componentes):
        h.update(chave.encode())
        h.update(b"\x00")
        h.update(_estavel(componentes[chave]).encode())
        h.update(b"\x00")
    return {
        "alvo_de_certificacao_hash": h.hexdigest(),
        "componentes": componentes,
        "o_que_NAO_entra_no_hash": {
            "build_do_backend": (
                "fica no manifesto como RASTREAMENTO. Se entrasse, uma virgula"
                " no README invalidaria o Portao A"
            )
        },
    }


def _estavel(valor) -> str:
    """Serialização determinística. `json.dumps` com `sort_keys` não basta.

    Ele ordena as chaves e **não** ordena listas, e uma lista de módulos que
    chegasse em outra ordem produziria outro hash sobre o mesmo laboratório.
    Aqui as listas de string são ordenadas.
    """
    import json

    def normalizar(v):
        if isinstance(v, dict):
            return {k: normalizar(v[k]) for k in sorted(v)}
        if isinstance(v, (list, tuple)):
            itens = [normalizar(x) for x in v]
            if all(isinstance(x, str) for x in itens):
                return sorted(itens)
            return itens
        return v

    return json.dumps(
        normalizar(valor), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
