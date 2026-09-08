"""A quarentena do forward. Incremento 19, §8.5 e ADR 0034.

**Nenhuma candidata entra no forward da 0C.** A decisao e a D38, e a razao e
que o Portao B rejeitou a unica candidata que existia - §14.4: *"A abordagem
esta descartada. Reprojetar."*

O que roda aqui e o **B3 como controle negativo do proprio encanamento**: uma
regra congelada que ninguem afirma ser edge, e que a 0A ja mediu perdendo. Se a
maquinaria promover o B3, a maquinaria esta errada, e e para isso que ele esta
ali.

## Os dois lados, e a separacao e a garantia

    admitir   o caminho de PRODUCAO. Recusa em 0C, com o motivo escrito
    avaliar   PURO: recebe dados e devolve veredito

O caminho positivo - o de promover - e exercitado por **controle positivo
sintetico, so na suite**, sobre dados artificiais que satisfazem os limiares
congelados. Ele nao produz evidencia, nao consome credito, nao entra em familia
e nunca aparece como candidata.

**E nenhum caminho de producao constroi candidato admitido.** Ha guarda varrendo
`app/` para que continue assim - porque a alternativa seria afrouxar a regua
para o B3 passar, e um afrouxamento feito "para ver o pipeline funcionar" fica.
"""
