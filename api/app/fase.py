"""A fase corrente do experimento. **Uma fonte, e uma so.**

Este arquivo existe porque a alternativa ja falhou duas vezes.

No incremento 9 o `/api/health` foi pego declarando `"fase": "0A"` depois de a
0B abrir, e a correcao veio com um comentario dizendo *"a fase vem daqui e de
nenhum outro lugar"*. Era falso quando foi escrito: havia mais dois lugares -
o liveness em `main.py` e o `/api/exportar` -, e os tres passaram a **discordar
entre si**.

Um comentario afirmando unicidade nao produz unicidade. Um modulo importado
produz.

## Por que isto importa mais que parecer

O campo `fase` acompanha o **aviso sobre o que pode ser afirmado**. Um numero
rotulado "0A" carrega "nenhuma conclusao estatistica"; o mesmo numero rotulado
"0B" carrega "conclusao so pelo validador independente". Errar o rotulo e
descrever errado o que o resultado significa - que e pior que nao rotular.
"""

from __future__ import annotations

FASE = "0B"

AVISO = (
    "Fase 0B. O Portao A e o produto da fase; conclusao estatistica so pelo"
    " validador independente, e 'inconclusivo' nunca vira 'sucesso'. Nenhuma"
    " aprovacao autoriza capital real."
)

# O nome do servico ficou "fase0a-api" desde o incremento 0 e NAO muda: e
# identificador de servico, nao declaracao de fase. Renomea-lo quebraria a
# correlacao de log entre os deploys da 0A e os da 0B, que e justamente o que
# se quer olhar ao comparar as duas.
SERVICO = "fase0a-api"
