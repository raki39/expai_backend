"""Testes do coletor de book (ADR 0028).

Varios deles existem porque uma versao anterior do desenho estava errada, e o
teste e o que impede a volta. Estao nomeados de forma a dizer isso.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from coletor import relogio
from coletor.amostra import (
    TOLERANCIA_MS,
    Cotacao,
    Estado,
    amostrar,
    da_mensagem,
)
from coletor.arquivo import (
    Diario,
    conferir_destino,
    dia_utc,
    integridade,
    ler,
)

S = 1_000_000_000  # ns


def cot(u: int, recebido_ns: int) -> Cotacao:
    return Cotacao(u=u, bid="60000.00", bid_qty="1.5",
                   ask="60000.10", ask_qty="2.0", received_at_ns=recebido_ns)


# ---------------------------------------------------------------- o payload

def test_o_bookticker_spot_nao_tem_carimbo_da_exchange():
    """O payload tem SEIS campos, e nenhum e horario.

    Uma versao anterior do ADR dizia que gravariamos "o carimbo local E o E/T
    da exchange". O `bookTicker` SPOT nao tem `E` nem `T` - quem tem e o de
    futuros. Este teste fixa que o parser nao inventa o campo.
    """
    payload = {"u": 400900217, "s": "BTCUSDT", "b": "60000.00",
               "B": "1.5", "a": "60000.10", "A": "2.0"}
    c = da_mensagem(payload, recebido_ns := 123 * S)
    assert c.received_at_ns == recebido_ns
    assert not hasattr(c, "exchange_ts")
    assert not hasattr(c, "event_time")
    # E os campos que existem sao exatamente os seis, mais a hora local.
    assert {f for f in c.__dataclass_fields__} == {
        "u", "bid", "bid_qty", "ask", "ask_qty", "received_at_ns"
    }


# --------------------------------------------------- validade temporal

def test_amostra_fresca_e_disponivel():
    e = Estado(ultima=cot(1, 10 * S), conectado=True)
    a = amostrar(e, 10 * S + 500_000_000)  # 500 ms depois
    assert a.disponivel and a.motivo is None
    assert a.idade_ms == 500
    assert a.bid == "60000.00"


def test_amostra_defasada_alem_da_tolerancia_e_indisponivel():
    e = Estado(ultima=cot(1, 10 * S), conectado=True)
    a = amostrar(e, 10 * S + (TOLERANCIA_MS + 1) * 1_000_000)
    assert not a.disponivel
    assert a.motivo == "defasada"


def test_desconectado_e_indisponivel_mesmo_com_cotacao_fresca():
    """A conexao caida invalida a amostra ainda que o slot esteja recente.

    Sem isto, uma queda logo apos uma mensagem produziria amostras "frescas"
    por ate `TOLERANCIA_MS` - descrevendo um mercado que ja nao estamos vendo.
    """
    e = Estado(ultima=cot(1, 10 * S), conectado=False)
    a = amostrar(e, 10 * S + 100_000_000)
    assert not a.disponivel
    assert a.motivo == "desconectado"


def test_sem_mensagem_tem_motivo_proprio():
    """"Nao ha dado" e "o dado esta velho" sao coisas diferentes."""
    a = amostrar(Estado(conectado=True), 10 * S)
    assert not a.disponivel and a.motivo == "sem_mensagem"
    assert a.received_at_ns is None and a.idade_ms is None


def test_amostra_indisponivel_NAO_repete_a_anterior():
    """Sem interpolacao, sem repetir - a lacuna fica declarada.

    E a mesma regra da subsecao 3b do ADR 0026 e do item 3 do ADR 0027: o que
    nao se observou fica ausente, e nao preenchido com o vizinho.
    """
    e = Estado(ultima=cot(7, 10 * S), conectado=True)
    assert amostrar(e, 10 * S).bid == "60000.00"          # fresca: sai preco
    velha = amostrar(e, 10 * S + 5 * S)                    # defasada
    assert velha.bid is None and velha.ask is None
    assert velha.u is None
    assert velha.bid_qty is None and velha.ask_qty is None


# --------------------------------------------------------- o papel do `u`

def test_u_parado_NAO_torna_a_amostra_indisponivel():
    """O topo pode simplesmente nao ter mudado.

    Uma versao anterior propunha `u` parado como prova de buffer atrasado.
    Nao e: `u` estatico e compativel com mercado calmo E com stream travado, e
    tratar os dois como o mesmo caso inventaria indisponibilidade onde ha so
    quietude. Este teste fixa a correcao.
    """
    e = Estado(ultima=cot(42, 10 * S), conectado=True)
    a1 = amostrar(e, 10 * S + 100_000_000)
    # Mesma cotacao, mesmo `u`, um segundo depois - e ainda dentro da tolerancia.
    e.ultima = cot(42, 10 * S + 900_000_000)
    a2 = amostrar(e, 10 * S + 1_000_000_000)

    assert a1.disponivel and a2.disponivel, "u parado nao invalida amostra"
    assert a2.u_duplicado is True, "mas a duplicacao e REGISTRADA"
    assert a2.delta_u == 0
    assert e.duplicadas == 1


def test_u_regressivo_e_registrado_e_tambem_nao_decide():
    """`u` para tras indica reconexao mal costurada. Observacao, nao veredito."""
    e = Estado(ultima=cot(100, 10 * S), conectado=True)
    amostrar(e, 10 * S)
    e.ultima = cot(90, 11 * S)
    a = amostrar(e, 11 * S)
    assert a.u_regrediu is True and a.delta_u == -10
    assert e.regressoes == 1
    assert a.disponivel, "regressao de u nao invalida a amostra sozinha"


def test_salto_de_u_e_normal_e_nao_vira_alarme():
    """Milhares de atualizacoes por segundo: salto entre tiques e o esperado."""
    e = Estado(ultima=cot(1_000, 10 * S), conectado=True)
    amostrar(e, 10 * S)
    e.ultima = cot(95_000, 11 * S)
    a = amostrar(e, 11 * S)
    assert a.delta_u == 94_000
    assert not a.u_duplicado and not a.u_regrediu
    assert a.disponivel


# ------------------------------------------------------------------ relogio

def test_offset_e_rtt_saem_da_ida_e_volta():
    """t0=1000,00 t1=1000,40 S=1000200ms -> rtt=400ms, offset=0ms."""
    tempos = iter([1000.00, 1000.40])
    m = relogio.medir(
        agora_s=lambda: next(tempos),
        agora_ns=lambda: 42,
        buscar=lambda _u, _t: json.dumps({"serverTime": 1_000_200}).encode(),
    )
    assert m.rtt_ms == pytest.approx(400.0)
    assert m.offset_ms == pytest.approx(0.0)
    assert m.incerteza_bruta_ms() == pytest.approx(200.0)
    assert m.incerteza_residual_ms() == pytest.approx(200.0)


def test_offset_positivo_quer_dizer_exchange_adiante():
    tempos = iter([1000.00, 1000.10])
    m = relogio.medir(
        agora_s=lambda: next(tempos), agora_ns=lambda: 0,
        buscar=lambda _u, _t: json.dumps({"serverTime": 1_000_350}).encode(),
    )
    assert m.offset_ms == pytest.approx(300.0)


def test_sonda_que_falha_nao_inventa_valor():
    """Relogio nao medido e relogio nao medido - nunca um numero plausivel."""
    def explode(_u: str, _t: float) -> bytes:
        raise OSError("rede fora")

    with pytest.raises(relogio.FalhaNaSonda):
        relogio.medir(agora_s=lambda: 1.0, agora_ns=lambda: 0, buscar=explode)


def test_resposta_sem_servertime_e_falha_e_nao_zero():
    with pytest.raises(relogio.FalhaNaSonda):
        relogio.medir(agora_s=lambda: 1.0, agora_ns=lambda: 0,
                      buscar=lambda _u, _t: b'{"outra":1}')


# ------------------------------------------------------------------ arquivo

def test_rotaciona_por_dia_utc(tmp_path: Path):
    d = Diario(tmp_path, "bookticker-btcusdt")
    ns_dia1 = int(1_756_000_000 * 1e9)          # um instante qualquer
    ns_dia2 = ns_dia1 + 86_400 * S
    d.escrever({"a": 1}, ns=ns_dia1)
    d.escrever({"a": 2}, ns=ns_dia2)
    d.fechar()

    nomes = sorted(p.name for p in tmp_path.glob("*.jsonl.gz"))
    assert len(nomes) == 2, nomes
    assert nomes[0] == f"bookticker-btcusdt-{dia_utc(ns_dia1)}.jsonl.gz"


def test_arquivo_fechado_e_legivel_ate_a_ultima_linha(tmp_path: Path):
    """gzip guarda estado: processo morto sem fechar perde o fim do arquivo."""
    ns = int(1_756_000_000 * 1e9)
    with Diario(tmp_path, "p") as d:
        for i in range(150):
            d.escrever({"i": i}, ns=ns)
    arq = next(tmp_path.glob("*.jsonl.gz"))
    linhas = gzip.decompress(arq.read_bytes()).decode().strip().split("\n")
    assert len(linhas) == 150
    assert json.loads(linhas[-1]) == {"i": 149}


def test_recusa_gravar_no_volume_do_experimento(tmp_path: Path):
    """A D32 nomeou a propriedade exclusiva de escrita do SQLite como bloqueio.

    Na Railway os volumes sao distintos e a colisao nao acontece; num compose
    local mal configurado, acontece. Falhar no boot e melhor que descobrir
    depois.
    """
    banco = tmp_path / "data" / "fase0a.sqlite3"
    banco.parent.mkdir(parents=True)
    with pytest.raises(SystemExit) as e:
        conferir_destino(tmp_path / "data", str(banco))
    assert "COLETOR_DIR" in str(e.value)


def test_destino_separado_e_aceito(tmp_path: Path):
    banco = tmp_path / "data" / "fase0a.sqlite3"
    banco.parent.mkdir(parents=True)
    conferir_destino(tmp_path / "dados", str(banco))  # nao levanta


def test_sem_db_path_nao_ha_o_que_conferir(tmp_path: Path):
    conferir_destino(tmp_path, None)


# ------------------------------------------- a lista curta de dependencias

def test_o_coletor_nao_importa_o_cerebro_nem_provedor():
    """R83: ele nao decide nada, e a lista curta torna isso verificavel.

    Nao e teste de estilo. Se um dia alguem importar um adaptador de LLM aqui,
    a afirmacao "o coletor nao participa de nenhuma decisao" para de ser
    verdade sem que nenhum outro teste perceba.
    """
    raiz = Path(__file__).resolve().parents[1] / "coletor"
    proibidos = ("langgraph", "anthropic", "openai", "fastapi", "sqlite3")
    for py in raiz.rglob("*.py"):
        texto = py.read_text(encoding="utf-8")
        for termo in proibidos:
            assert f"import {termo}" not in texto, f"{py.name} importa {termo}"


# ------------------------------------ o que o primeiro teste de fumaca revelou

def test_offset_grande_e_corrigivel_o_residual_e_que_nao_e():
    """A distincao que da valor a medicao.

    Medido em maquina real: offset de -2.460 ms contra a Binance, RTT de 315.
    A incerteza BRUTA (2.618 ms) passa da tolerancia inteira de 2.000 ms do
    ADR 0027; a RESIDUAL, depois de corrigir pelo offset, e 158 ms.

    Sem medir, carregaria-se o offset como se fosse zero - e o alinhamento
    estaria errado por mais que o orcamento inteiro, em silencio.
    """
    tempos = iter([1000.000, 1000.315])
    m = relogio.medir(
        agora_s=lambda: next(tempos), agora_ns=lambda: 0,
        buscar=lambda _u, _t: json.dumps(
            {"serverTime": int((1000.000 + 1000.315) / 2 * 1000) - 2460}
        ).encode(),
    )
    assert m.offset_ms == pytest.approx(-2460.0, abs=1.0)
    assert m.incerteza_bruta_ms() > 2000, "sem corrigir, estoura a tolerancia"
    assert m.incerteza_residual_ms() < 200, "corrigido, cabe com folga"


def test_arquivo_truncado_entrega_tudo_ate_o_corte(tmp_path: Path):
    """SIGKILL existe, e a Railway derruba container em redeploy.

    `gzip.decompress` recusa o arquivo INTEIRO quando o membro nao fecha - nao
    a ultima linha, o dia todo. O leitor precisa aguentar, porque a escrita nao
    tem como prevenir.
    """
    ns = int(1_756_000_000 * 1e9)
    d = Diario(tmp_path, "p", segundos_por_flush=0.0)  # flush a cada linha
    for i in range(50):
        d.escrever({"i": i}, ns=ns)
    d.flush()
    arq = next(tmp_path.glob("*.jsonl.gz"))
    bruto = arq.read_bytes()

    # Corta o fim, como um SIGKILL faria.
    arq.write_bytes(bruto[: int(len(bruto) * 0.8)])

    recuperadas = list(ler(arq))
    assert len(recuperadas) > 0, "um corte no fim nao pode zerar o arquivo"
    assert recuperadas[0] == {"i": 0}
    # Sequencia intacta ate onde chegou: nada e inventado nem reordenado.
    assert [r["i"] for r in recuperadas] == list(range(len(recuperadas)))
    assert integridade(arq)["truncado"] is True


def test_sem_flush_o_leitor_nao_recupera_NADA(tmp_path: Path):
    """O defeito exato que o teste de fumaca produziu, fixado como teste.

    Com `segundos_por_flush` alto, nenhuma fronteira Z_SYNC_FLUSH e criada, e
    um truncamento leva o arquivo inteiro junto. E por isso que o intervalo de
    descarga e curto, e por isso que ele e medido em vez de chutado.
    """
    ns = int(1_756_000_000 * 1e9)
    d = Diario(tmp_path, "p", segundos_por_flush=10_000.0)
    for i in range(50):
        d.escrever({"i": i}, ns=ns)
    # NAO fecha nem descarrega: e o processo levando SIGKILL.
    arq = next(tmp_path.glob("*.jsonl.gz"))
    # Ainda mais forte que "ilegivel": o `BufferedWriter` do GzipFile segura
    # tudo, entao NEM O CABECALHO chega ao disco. Cinquenta amostras escritas,
    # zero bytes gravados.
    assert arq.stat().st_size == 0
    assert list(ler(arq)) == []


def test_flush_por_TEMPO_e_nao_por_contagem(tmp_path: Path):
    """Se a taxa de amostragem mudar, uma regra por contagem mentiria."""
    ns = int(1_756_000_000 * 1e9)
    agora = [0.0]
    d = Diario(tmp_path, "p", segundos_por_flush=10.0, agora=lambda: agora[0])
    for _ in range(1000):
        d.escrever({"x": 1}, ns=ns)
    assert d.flushes == 0, "mil linhas em zero segundos nao descarregam"
    agora[0] = 11.0
    d.escrever({"x": 1}, ns=ns)
    assert d.flushes == 1, "onze segundos descarregam, com uma linha so"
