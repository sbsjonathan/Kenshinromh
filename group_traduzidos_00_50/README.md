# GROUP00–GROUP50 em PT-BR

Esta pasta reúne todos os `GROUPxx.BIN` existentes entre `GROUP00.BIN` e
`GROUP50.BIN` no repositório. São 44 arquivos. Os números 28, 32, 33, 34, 36,
38 e 44 não existem nas bases recebidas e, por isso, não foram fabricados.

Os binários foram reextraídos de `zroup/mod/ZROUPxx.GRP` e comparados com a
extração anterior do branch `agent/extrair-group-bin`. As duas extrações são
idênticas byte a byte.

## Estado da tradução

- 8.885 sequências textuais plausíveis foram reabertas e auditadas.
- Não foi encontrado texto japonês residual nos pools de texto.
- Quatro sequências de prefixo/controle não UTF-8 do `GROUP02.BIN` foram
  preservadas exatamente como estavam na base funcional; elas não são texto
  japonês.
- A padronização aprovada de itens foi aplicada em 350 strings de 32 grupos.
- Nomes de itens respeitam o limite adotado de 13 caracteres visíveis.
- `Erva de couro` e `Casco` seguem as decisões de adaptação para campos curtos.

## Integridade binária

Todos os 44 arquivos mantêm o mesmo tamanho e o mesmo cabeçalho das bases
extraídas. As regiões sensíveis anteriores ao pool, as fontes locais e os dados
posteriores à fonte permanecem idênticos. Nos 32 arquivos com nomes de itens
padronizados, todas as diferenças estão confinadas ao pool de texto.

Consulte:

- `AUDITORIA.json` para o relatório geral e por arquivo;
- `MANIFESTO_SHA256.txt` para os hashes dos 44 binários;
- `docs/PADRONIZACAO_ITENS.csv` para o glossário curto aprovado;
- `docs/ALTERACOES_ITENS.csv` para as 350 substituições desta faixa;
- `docs/SISTEMA_MESTRE_ATUALIZADO_TRADUCAO_ZROUPS_SCPS-10048.md` para o
  manual completo recuperado da Biblioteca.

Esta entrega foi **auditada localmente**, mas ainda não foi testada dentro do
jogo nesta rodada. Os arquivos desta pasta são os membros internos `GROUP`; para
uso no jogo, cada um precisa ser reinserido no `ZROUPxx.GRP` correspondente por
um remontador que preserve o contêiner.
