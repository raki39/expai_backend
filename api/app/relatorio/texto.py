"""Renderiza o relatorio em Markdown. Nenhum numero nasce aqui.

Este modulo **so formata**. Toda conta e toda conferencia vieram de
`montar.montar`, que leu o banco. A separacao existe para que uma unica
pergunta tenha resposta obvia: "de onde veio esse numero?". Se houvesse uma
soma neste arquivo, o relatorio em Markdown e o JSON da rota poderiam divergir
- e o Markdown e justamente a versao que alguem le e acredita.

O criterio 1 pede afirmacao com dados do banco. Um formatador que calculasse
seria a maneira mais discreta de burlar isso.
"""

from __future__ import annotations

import json
from typing import Any


def _usd(cents: Any) -> str:
    if cents is None:
        return "indisponivel"
    return f"US$ {int(cents) / 100:,.2f}".replace(",", "_").replace(".", ",").replace(
        "_", "."
    )


def _brl(cents: Any) -> str:
    if cents is None:
        return "indisponivel"
    return f"R$ {int(cents) / 100:,.2f}".replace(",", "_").replace(".", ",").replace(
        "_", "."
    )


def _curto(texto: Any, n: int = 16) -> str:
    """Prefixo de um hash. As reticencias aparecem so quando cortou de fato.

    Um "..." sempre presente afirma que ha mais texto adiante - e num
    documento cujo assunto e nao afirmar o que nao se sabe, isso incomoda.
    """
    if not texto:
        return "indisponivel"
    s = str(texto)
    return s if len(s) <= n else s[:n] + "..."


def _sim_nao(valor: Any) -> str:
    if valor is True:
        return "sim"
    if valor is False:
        return "NAO"
    return "nao se aplica"


def _ms(valor: Any) -> str:
    """Timestamp de barra em ms -> data legivel, sem inventar precisao."""
    if valor is None:
        return "indisponivel"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(valor) / 1000, timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def markdown(r: dict) -> str:
    """O relatorio inteiro em Markdown, a partir do dict de `montar`."""
    L: list[str] = []
    add = L.append

    if not r.get("existe"):
        add("# Relatorio de fechamento da Fase 0A")
        add("")
        add(f"**Nao ha o que relatar:** {r.get('motivo')}")
        add("")
        add(f"Gerado em {r.get('gerado_em')}.")
        add("")
        add("## O que a 0A nao conclui, em nenhuma hipotese")
        add("")
        for item in r.get("nao_concluido", []):
            add(f"- {item}")
        return "\n".join(L) + "\n"

    run = r["run"]
    cfg = r["config"]
    resposta = r["resposta_da_0a"]

    add("# Relatorio de fechamento da Fase 0A")
    add("")
    add(f"Gerado em {r['gerado_em']} · run **{run['id']}** ({run['state']})")
    add(
        f" · `config_version` {cfg['version_id']}"
        f" · `config_hash` `{_curto(cfg['config_hash'])}`"
        f" · perfil `{cfg['profile_id']}`"
    )
    add("")

    # ------------------------------------------------------------- a resposta
    add("## A pergunta da 0A")
    add("")
    add(f"> {resposta['pergunta']}")
    add("")
    if resposta["fecha"]:
        add("**Sim.** As condicoes abaixo foram todas conferidas contra o banco.")
    else:
        add(f"**Nao.** {resposta['se_nao_fecha']}")
        add("")
        add("Falta:")
        for nome in resposta["faltando"]:
            add(f"- `{nome}`")
    add("")
    add("| condicao | conferida |")
    add("|---|---|")
    for nome, valor in resposta["condicoes"].items():
        add(f"| {nome.replace('_', ' ')} | {_sim_nao(valor)} |")
    add("")
    add(
        "Nenhuma linha desta tabela e digitada: cada uma e um booleano que sai"
        " de uma consulta ao banco. Uma resposta em prosa sobreviveria a"
        " qualquer regressao futura sem mudar uma letra."
    )
    add("")

    # --------------------------------------------------------------- observou
    o = r["observou"]
    add("## 1. Observou")
    add("")
    add(f"- dataset `{o['dataset_id']}`, sha256 `{_curto(o['dataset_sha256'])}`")
    add(
        f"- {o['barras_disponiveis']} barras disponiveis,"
        f" {o['barras_reservadas']} reservadas · fidelidade {o['fidelity_level']}"
    )
    add(
        f"- janela observada pelo cerebro: {_ms(o['janela_observada_de_ms'])}"
        f" a {_ms(o['janela_observada_ate_ms'])}"
    )
    sob = o.get("sobreposicao_com_a_executada") or {}
    if sob.get("sobreposicao_bps") is not None:
        add(
            f"- **sobreposicao com a janela executada:"
            f" {sob['sobreposicao_bps'] / 100:.2f}%** (D22 — resultado em amostra)"
        )
    add("")

    # --------------------------------------------------------------- refletiu
    f = r["refletiu"]
    add("## 2. Refletiu")
    add("")
    if not f["houve_cerebro"]:
        add(
            "**Zero reflexoes.** Com o teto em zero o agente e o B3 (D23): a"
            " regra padrao e o mesmo cruzamento, derivado da config. Este run"
            " nao mede cerebro nenhum."
        )
    else:
        add(f"- {f['quantas']} reflexoes")
        g = f["gasto"]
        add(
            f"- custo: {_usd(g['gasto_cents'])} no livro simulado"
            f" ({g['gasto_micro']} micros exatos) ·"
            f" {_brl(g['gasto_real_brl_cents'])} de dinheiro real"
        )
        add("")
        add("| evento | no | provedor · modelo | entrada | saida | cache leitura | cache escrita | custo |")
        add("|---|---|---|---|---|---|---|---|")
        for e in f["reflexoes"]:
            add(
                f"| {e['id']} | {e['node']} | {e['provider']} · {e['model']}"
                f" | {e['tokens_in'] if e['tokens_in'] is not None else '—'}"
                f" | {e['tokens_out'] if e['tokens_out'] is not None else '—'}"
                f" | {e['tokens_cache_read'] if e['tokens_cache_read'] is not None else '—'}"
                f" | {e['tokens_cache_write'] if e['tokens_cache_write'] is not None else '—'}"
                f" | {_usd(e['cost_usd_minor'])} |"
            )
        add("")
        add(
            "Travessao significa **nao informado pelo provedor** — nunca zero."
            ' "Nao sei" e "foi zero" sao afirmacoes diferentes (secao 5.2).'
        )
    add("")

    # ----------------------------------------------------------------- propos
    p = r["propos"]
    add("## 3. Propos regra")
    add("")
    if p["regra"]:
        add(f"- hash `{p['regra']['hash']}`")
        add(f"- familia `{p['regra']['family']}`, tipo `{p['regra']['kind']}`")
        add(f"- parametros: `{json.dumps(p['regra']['params'], sort_keys=True)}`")
        add(f"- propostas: {p['aceitas']} aceitas, {p['rejeitadas']} rejeitadas")
        ativa = p["regra_ativa"] or {}
        if ativa.get("expectation"):
            add("")
            add("**Expectativa declarada antes da execucao** (regra 17):")
            add("")
            add(f"> {ativa['expectation']}")
            if ativa.get("confidence_ppm") is not None:
                add("")
                add(f"Confianca declarada: {ativa['confidence_ppm'] / 10_000:.1f}%")
    else:
        add("Nenhuma proposta aceita: a regra executada foi a padrao (D23).")
    add("")

    # --------------------------------------------------------------- executou
    x = r["executou"]
    add("## 4. Executou")
    add("")
    add(
        f"- {x['ordens_executadas']} ordens ({x['compras']} compras,"
        f" {x['vendas']} vendas)"
    )
    add(f"- nocional girado: {_usd(x['nocional_girado_cents'])}")
    add(f"- digest do run: `{x['digest']}`")
    add("")
    add("| componente de custo | valor |")
    add("|---|---|")
    for rotulo, chave in (
        ("taxa taker", "taxa_cents"),
        ("spread", "spread_cents"),
        ("slippage", "slippage_cents"),
        ("penalidade", "penalidade_cents"),
    ):
        add(f"| {rotulo} | {_usd(x[chave])} |")
    add("")
    add(f"Condicoes de validade, derivadas da config do run: {x['condicoes_validade']}")
    add("")

    # ----------------------------------------------------------------- custos
    c = r["custos"]
    add("## 5. Registrou custos nos dois livros")
    add("")
    add(f"- patrimonio final: **{_usd(c['patrimonio_final_cents'])}**")
    add("")
    add("| livro | conta | saldo |")
    add("|---|---|---|")
    for conta, valor in c["livro_simulado_usd"].items():
        add(f"| simulado (USD) | {conta.replace('_minor', '')} | {_usd(valor)} |")
    for conta, valor in c["livro_real_brl"].items():
        add(f"| real (BRL) | {conta.replace('_minor', '')} | {_brl(valor)} |")
    add("")
    for fx in c["cambio_do_run"]:
        add(
            f"- cambio gravado na transacao: {fx['fx_rate_micro']} micros"
            f" em {fx['fx_rate_date']}"
        )
    add("")
    add(
        "Os dois livros **nunca se somam** (regra 7). A ponte entre eles e a"
        " taxa gravada em cada transacao, nao uma conversao feita na leitura."
    )
    add("")

    # -------------------------------------------------------------- comparado
    k = r["comparado"]
    add("## 6. Comparado a B1, B2 e B3")
    add("")
    if not k.get("existe"):
        add("Nenhuma comparacao rodada.")
    else:
        # Uma unidade so nesta coluna: **idas e voltas**. `execucoes` conta
        # linhas de `execution`, e uma ida e volta sao duas - por um instante
        # esta tabela pos 36 de um lado e 18 do outro sob o mesmo rotulo.
        add("| | patrimonio final | idas e voltas |")
        add("|---|---|---|")
        b1 = k.get("b1_casado_com_o_agente")
        add(
            f"| **agente** | **{_usd(c['patrimonio_final_cents'])}** |"
            f" {x['idas_e_voltas']} |"
        )
        if b1:
            add(
                f"| B1 casado p5 -> p50 -> p95 | {_usd(b1['p5'])} -> "
                f"**{_usd(b1['p50'])}** -> {_usd(b1['p95'])} |"
                f" {b1.get('operacoes_alvo')} cada |"
            )
        for marcador, rotulo in (("B2", "B2 buy and hold"), ("B3", "B3 SMA congelado")):
            bloco = k.get(marcador)
            if bloco:
                add(
                    f"| {rotulo} | {_usd(bloco.get('equity_final_cents'))} |"
                    f" {bloco.get('idas_e_voltas', 'indisponivel')} |"
                )
        add("")
        if "excesso_sobre_b1_p50_cents" in k:
            add(
                f"**Excesso sobre a mediana do acaso:"
                f" {_usd(k['excesso_sobre_b1_p50_cents'])}** · faixa"
                f" `{k.get('faixa')}`"
            )
            add("")
            add(
                "Desempenho **sempre** como excesso sobre baseline, nunca em"
                " termos absolutos (regra 14). O controle B1 casa o numero de"
                " operacoes **e o tamanho de posicao** do que ele controla"
                " (D19, secao 14.3) — casar so o giro mediria dimensionamento"
                " em vez de escolha de momento."
            )
    add("")

    # --------------------------------------------------------------- avaliou
    a = r.get("avaliou")
    add("## 7. Avaliou o resultado — evento novo, filho da decisao")
    add("")
    if a is None:
        add(
            "Nao houve expectativa declarada neste run, logo nao ha o que"
            " avaliar. Pendurar uma comparacao em quem nao afirmou nada seria"
            " invencao."
        )
    else:
        d = a["decisao"]
        comp = a["comparacao"]
        add(
            f"- decisao: evento {d['event_id']}, declarada em {d['declarada_em']}"
        )
        add(
            f"- avaliacao: evento {a['avaliacao_event_id']}, em {a['avaliada_em']}"
        )
        add(f"- faixa contra o acaso: **`{comp['faixa']}`**")
        if comp.get("contra_o_acaso"):
            ca = comp["contra_o_acaso"]
            add(
                f"- excesso sobre o p50: {_usd(ca['excesso_sobre_p50_cents'])}"
            )
        pr = comp.get("pre_registro") or {}
        veredito = pr.get("veredito")
        add(
            "- veredito sobre o pre-registro: "
            f"**{veredito if veredito is not None else 'nao emitido'}**"
        )
        add(f"  - {pr.get('motivo', 'sem motivo registrado')}")
        if pr.get("hypothesis_id") is not None:
            efeito = pr.get("efeito") or {}
            amostra = pr.get("amostra") or {}
            add(f"  - hipotese {pr['hypothesis_id']}")
            add(
                "  - efeito observado"
                f" {efeito.get('observado')} contra minimo declarado"
                f" {efeito.get('minimo_declarado')}"
            )
            add(
                "  - amostra: n_efetivo"
                f" {amostra.get('n_efetivo')} de n_minimo"
                f" {amostra.get('n_minimo')}"
                f" (bruto {amostra.get('n_bruto')})"
            )
            for c in pr.get("condicoes_falseamento") or []:
                estado = (
                    "DISPAROU"
                    if c.get("disparou")
                    else ("nao conferida" if c.get("disparou") is None else "nao")
                )
                add(
                    f"  - falseamento `{c['condicao']}`:"
                    f" observado {c.get('observado')} — {estado}"
                )
        add("")
        add(
            "A decisao original permanece **byte a byte inalterada** (R25.3):"
            " `agent_event` recusa `UPDATE` por gatilho, entao editar o passado"
            " nao e uma opcao disponivel. A avaliacao nao copia o pre-registro —"
            " ele vive na hipotese, e a decisao e o pai deste evento."
        )
    add("")

    # --------------------------------------------------------------- caminho
    add("## 8. Caminho percorrido e vinculo nos dois sentidos")
    add("")
    add("| evento | no | tipo | pai | custo |")
    add("|---|---|---|---|---|")
    for e in r["caminho"]:
        add(
            f"| {e['id']} | {e['node']} | {e['kind']}"
            f" | {e.get('parent_event_id') or '—'}"
            f" | {_usd(e.get('cost_usd_minor'))} |"
        )
    add("")
    v = r["vinculo"]
    if v.get("conferido"):
        add(
            f"**Ida e volta fecham.** Partindo da execucao {v['execution_id']}"
            f" chega-se ao evento cognitivo {v['evento_cognitivo']}"
            f" (`{v['no']}`), subindo {v['profundidade_da_cadeia']} niveis; e"
            f" desse evento se chega de volta a mesma execucao, entre as"
            f" {v['execucoes_autorizadas']} que a regra"
            f" `{_curto(v.get('regra_hash'))}` autorizou."
        )
    else:
        add(f"**Vinculo nao conferido:** {v.get('motivo')}")
    add("")

    # ------------------------------------------------------------ integridade
    i = r["integridade"]
    add("## 9. Integridade contabil")
    add("")
    add("| conferencia | resultado |")
    add("|---|---|")
    add(
        f"| partidas dobradas | {len(i['partidas_dobradas_violadas'])} violacoes |"
    )
    add(f"| saldo derivado igual ao exibido | {len(i['saldos_divergentes'])} divergencias |")
    # Iterado, e nao escrito chave por chave: com `.get(nome, [])` uma chave
    # renomeada imprimiria "0 violacoes" para sempre, e uma conferencia nova
    # simplesmente nao apareceria. Foi assim que este relatorio quase nasceu
    # com uma linha permanentemente verde.
    for nome, ids in i["vinculo_inferencia"].items():
        add(f"| {nome.replace('_', ' ')} | {len(ids)} |")
    add(
        f"| custo em centavos e o teto do exato |"
        f" {len(i['arredondamento_do_custo_divergente'])} divergencias |"
    )
    add(f"| `config_hash` ainda descreve a config | {_sim_nao(i['config_hash_ainda_descreve'])} |")
    add("")

    # ------------------------------------------------------ reprodutibilidade
    add("## 10. Reprodutibilidade (R12)")
    add("")
    prova = r.get("reprodutibilidade")
    if prova is None:
        add(
            "**Nao provada nesta base.** A prova roda tres passadas de B1 pelo"
            " ledger e compara os digests."
        )
    else:
        ms = prova["mesma_semente"]
        sd = prova["semente_diferente"]
        add(f"- mesma semente, digest A: `{ms['digest_a']}`")
        add(f"- mesma semente, digest B: `{ms['digest_b']}`")
        add(f"- semente diferente, digest C: `{sd['digest']}`")
        add("")
        add(f"- A == B: {_sim_nao(ms['iguais'])}")
        add(f"- C != A: {_sim_nao(sd['difere_do_primeiro'])}")
        add(f"- `config_hash` igual nas tres: {_sim_nao(prova['config_hash_igual_nas_tres'])}")
        add(f"- **provado: {_sim_nao(prova['provado'])}**")
        add("")
        add(
            "As duas metades importam. Sem a segunda, um digest constante"
            " passaria na primeira sempre — inclusive quando nada estivesse"
            " sendo medido."
        )
    add("")

    # ------------------------------------------------------------------ limites
    add("## O que a 0A nao conclui, em nenhuma hipotese")
    add("")
    for item in r["nao_concluido"]:
        add(f"- {item}")
    add("")
    add(
        "Esta lista e **texto fixo**, e nao derivada dos dados: derivar os"
        " limites do resultado permitiria que um dia eles encolhessem sozinhos."
    )
    add("")
    return "\n".join(L) + "\n"
