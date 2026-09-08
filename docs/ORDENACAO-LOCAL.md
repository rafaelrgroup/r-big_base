# Ordenação compartilhada no adaptador local

Contrato implementado em `backend/bigbase/sorting.py`, usado por pesquisa, pesquisas salvas e XLSX. Ambiente SQLite local, de processo único, com dados sintéticos. A seleção e a ordenação materializam registros em memória; não são índice definitivo, cursor/PIT ou prova de escala.

## Pedido novo

```json
{
  "sorts": [
    {"field": "city", "direction": "asc", "mode": "min"},
    {"field": "birth_date", "direction": "desc"}
  ],
  "include_invalid": false
}
```

`sorts` recebe de um a cinco critérios em ordem de prioridade. Cada critério exige `field` e `direction` (`asc` ou `desc`). `mode` é opcional apenas em campos múltiplos: `min` crescente e `max` decrescente por padrão. Uma escolha explícita de modo permanece ao trocar a direção.

| Campo | Valores usados | Modo |
|---|---|---|
| name | Nomes atuais dos itens identity elegíveis | min/max |
| birth_date | Nascimentos ISO `YYYY-MM-DD` válidos dos itens identity elegíveis | min/max |
| city, state, postal_code | Componentes atuais dos itens address elegíveis | min/max |
| updated_at | Instante UTC da atualização da entidade, com fuso obrigatório | Único |
| id | ID da entidade | Único |

Itens com `valid=false` não fornecem chave de ordenação, salvo `include_invalid=true`. A ficha e o XLSX continuam contendo todos os dados autorizados, com invalidados e histórico. Valores atuais null/ausentes, de tipo incompatível ou datas inválidas não fornecem chave. Uma entidade sem chave fica no fim **em ambas as direções**, sendo ordenada pelos próximos critérios. Texto vazio é texto, distinto de ausência; false e zero não são convertidos em texto para simular nomes ou códigos.

Textos usam normalização Unicode NFC e casefold, sem remover acentos; equivalentes canônicos empatam. Essa comparação é determinística e não pretende ser colação linguística pt-BR. CEPs/códigos postais continuam textuais, inclusive zeros e códigos estrangeiros. Atualizações com fusos diferentes são comparadas pelo instante UTC. A ordenação não corrige nem remove os valores recebidos.

ID crescente é acrescentado como desempate final, sem consumir os cinco critérios do usuário. Se ID for explícito, ele deve ser o último e pode ser decrescente. Campos repetidos/desconhecidos, propriedades extras no critério, modo em campo único, tipos inválidos e mistura com `sort`/`direction` retornam 422. Campos adicionais ainda não preparados retornam erro explícito.

`GET /api/v1/search/catalog` (versão 2) inclui `sorting`, com catálogo versão 1, rótulos, multiplicidade e estado `ready_local`. O painel usa esse catálogo. Pesquisa e trabalhos retornam `applied_sort`: versão, contrato, critérios efetivos (modos e desempate incluídos), comparação de texto e política de ausentes.

## Compatibilidade

Pedidos de pesquisa sem `sorts` continuam usando `sort`/`direction`, com nome crescente como padrão. O contrato `legacy` mantém exatamente a comparação anterior de `str(valor).casefold()` e o desempate por ID na mesma direção, inclusive decrescente. Nome legado usa o resumo da entidade, preservando também o comportamento anterior para ausência e invalidação.

Pesquisas salvas novas guardam `sorts`; as antigas guardam `sort`/`direction`. Ao abrir uma pesquisa antiga, o painel mostra o comportamento preservado. O botão “Editar com ordenação múltipla” muda explicitamente para o contrato novo. Salvar, executar ou exportar sem essa edição conserva o legado.

Trabalhos de exportação sem parâmetros de ordenação conservam a ordem do snapshot (`snapshot_order`), inclusive pendentes anteriores à mudança. Um trabalho novo com `sorts` ou `sort`/`direction` congela esses critérios e o contrato resolvido junto ao corte. A recuperação técnica usa esse contrato e os registros materializados, mesmo após alterações nos cadastros.

## XLSX e painel

O worker aplica a ordenação depois da seleção, antes da divisão em abas e volumes. Cada XLSX traz a aba escalar `Ordenacao`, com contrato, versão, prioridade, campo, direção, modo e políticas. O manifesto do ZIP contém `applied_sort`, critérios, corte, volumes, contagens e hashes. A verificação do arquivo reaberto compara também a sequência exata de IDs em `Cadastros` com o lote materializado antes de disponibilizar o download.

O editor em português permite adicionar/remover critérios, mover prioridades, escolher direção e min/max. Alterar a ordenação volta à primeira página e limpa a seleção. Os critérios acompanham a pesquisa salva e a ação Exportar. A consulta em massa também permite editar a ordenação e incluir invalidados na seleção.

## Compatibilidade durante ativação

O painel detecta a presença de `sorting` no catálogo do servidor. Um servidor anterior recebe o contrato legado para a consulta inicial por nome crescente; a edição múltipla fica indisponível. Pesquisas já configuradas com múltiplos critérios ou modo explícito não são reduzidas silenciosamente quando o servidor não oferece suporte. A atualização de um backend instalado segue o procedimento operacional privado da implantação. A validação funcional ocorre em fixture sintético separado; sua aprovação não equivale à atualização de outra instalação.

## Evidência e pendências

Resultados integrados e hashes dos arquivos constam em [VALIDACAO.md](VALIDACAO.md) e no [manifesto público](validacao-integrada.json). Testes usam apenas dados sintéticos; os relatórios operacionais originais permanecem privados. O leitor canônico PIT já possui implementação e testes HTTP simulados. Sua integração à API, ordenação múltipla canônica, campos adicionais indexados, integração PostgreSQL/outbox/Elasticsearch, processamento em fluxo e homologação de escala continuam pendentes, conforme [IMPLEMENTACAO.md](IMPLEMENTACAO.md).
