# Pesquisa canônica com PIT e cursor protegido

Bloco implementado em 08/09/2026: `backend/bigbase/canonical_search_reader.py`, com 41 testes em `backend/tests/test_canonical_search_reader.py`. A validação usou exclusivamente `httpx.MockTransport`, dados sintéticos e relógio simulado. Não há conexão Elasticsearch/PostgreSQL real, API pública integrada ou serviço iniciado por este módulo.

## Uso pelo servidor da aplicação

`CanonicalSearchReader` exige cliente HTTP explícito, URL, alias, UUID esperado do cluster, UUID esperado do índice e chave Fernet persistente fornecida pelo chamador. Não gera nem salva uma chave automaticamente. Todas as instâncias que precisam retomar os mesmos cursores usam a mesma chave protegida do ambiente de destino. Uma chave efêmera por processo impediria a retomada após reinício e não atende ao contrato.

O transporte reaproveita as proteções de `ElasticProjection`: HTTPS fora do loopback, recusa da porta legada 9200, nenhuma credencial em URL, UUIDs e mapping versionado conferidos antes de cada página, respostas lidas em fluxo com limite e sem redirecionamento. O teto de resposta deste leitor é 2 MiB por padrão, configurável nos limites do transporte. Só usa consultas, abertura e fechamento de PIT; não escreve documentos nem cria índices.

`search(criteria, principal_id=..., authorization=..., page_size=50, entity_type=None, include_pending=False, include_invalid=False, sort=None, cursor=None)` recebe os mesmos critérios de `build_query`. O contexto de autorização deve ser produzido pelo servidor após autenticação e reavaliação dos acessos, nunca copiado do corpo enviado pelo usuário. Campos obrigatórios:

```json
{
  "active": true,
  "can_search": true,
  "revision": "revisao-das-permissoes",
  "scopes": ["search"],
  "source_revision": "revisao-do-escopo-de-fontes"
}
```

Todo o objeto participa do vínculo criptográfico. `source_revision` exemplifica contexto adicional que o chamador deve fornecer quando ele afeta a autorização. Usuário/chave desativado ou sem permissão de busca é recusado antes de qualquer requisição. Mudanças de principal, revisão, escopos, filtros, inclusão de pendências/invalidados, tipo de entidade ou tamanho de página invalidam a continuação antiga. O módulo não autentica senhas/chaves nem substitui a aplicação das políticas de acesso pela futura API.

## Continuação e consistência

A primeira chamada abre PIT no alias verificado, com `allow_partial_search_results=false`. A continuação usa `search_after` e o PIT devolvido pela última resposta. Um PIT que o Elasticsearch declarou expirado causa erro explícito de reinício necessário; o leitor não abre outro e não reaproveita posições em um conjunto diferente. A [API oficial de PIT da versão 9](https://www.elastic.co/docs/api/doc/elasticsearch/v9/operation/operation-open-point-in-time) orienta o uso do ID mais recente.

Esta versão entrega **ordenação por ID crescente**. `_shard_doc` é o desempate técnico adicional, estável dentro do PIT. A semântica desses valores é descrita na [documentação oficial de paginação](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/paginate-search-results). O parâmetro `sort`, quando fornecido, aceita somente `[{"field":"id","direction":"asc"}]`; ordenação múltipla, relevância, min/max de coleções e outras direções ainda não foram implementados no leitor e são recusados explicitamente.

O momento de avaliação das confirmações é fixado no início da pesquisa. Ranges de expiração gerados pelo compilador usam esse instante em todas as páginas. Portanto, uma flag que vence durante a navegação não altera o conjunto selecionado dentro do PIT. A data de referência aparece em `snapshot.started_at`; datas reais ou o texto literal `now` recebido como valor de cadastro não são reescritos.

Os cursores são cifrados e autenticados com Fernet. O texto visível não contém consulta, identidade do usuário, posição ou PIT em claro. A chave é responsabilidade do destino. O token vincula destino, versão de projeção, principal, autorização, critérios, opções, ordenação e tamanho de página. Alteração do token ou chave errada é rejeitada antes de acessar o serviço. A validade é de 15 minutos sem uso, prorrogada nas páginas seguintes até o máximo absoluto de uma hora desde o início da pesquisa.

Uma mudança no alias/UUID ou na versão de projeção não muda silenciosamente o índice consultado. É necessário iniciar uma pesquisa nova. Não há paginação profunda por offset.

## Resposta e limites

Cada página retorna:

- `items`: somente `{id, record_version}` da projeção, em ordem;
- `returned` e `seen`: contadores de resultados efetivamente entregues;
- `total`: valor e relação `eq`/`gte` informados pelo Elasticsearch;
- `has_more`, `next_cursor` e `release_cursor`;
- `snapshot`: início, vencimento, prazo absoluto e versão da projeção.

A ficha completa deve ser carregada e autorizada no PostgreSQL, preservando a ordem e distinguindo versão indexada de versão cadastral. Esta resposta não se apresenta como cadastro completo nem inclui históricos ou documentos originais.

São permitidos de um a 100 resultados por página, padrão 50. O leitor pede um resultado extra para determinar a continuidade e avança o cursor somente até o último resultado entregue. Isso evita perder o registro usado para detectar a próxima página. IDs duplicados, ordem regressiva, index diferente, versões booleanas/inválidas, quantidade superior à solicitada, timeout, término antecipado ou shards incompletos invalidam a página inteira.

`track_total_hits=10000` limita o custo de contagem. `eq` é uma contagem exata; `gte` é um limite inferior, nunca um total exato ou um percentual de progresso. A página final precisa ser compatível com a contagem exata ou com esse limite inferior. Não é permitido declarar esgotamento abaixo da quantidade mínima que o próprio serviço informou.

## Falha, repetição e liberação

Falha durante uma continuação não encerra deliberadamente o PIT nem avança o cursor. O chamador pode repetir o token anterior enquanto seu contexto e PIT continuarem válidos. Uma falha na primeira página, antes de existir cursor entregue, tenta liberar o PIT recém-aberto sem mascarar o erro original. Erros retornam códigos operacionais, sem corpo HTTP remoto, consulta, token ou credenciais.

O PIT não é fechado automaticamente ao devolver a última página: isso permite repetir essa página se a resposta não chegar ao consumidor. Depois de consumir o resultado, o chamador usa `close(release_cursor, principal_id=..., authorization=...)`. O token de liberação é cifrado e possui propósito diferente do cursor de busca; não pode ser usado para continuar a consulta. Falha de liberação retorna `released=false`, e o TTL continua limitando a vida do recurso. Retenção não garante que o Elasticsearch preserve um PIT após falha externa; nesses casos continua necessário reiniciar explicitamente.

**Integração ainda necessária:** API autenticada, rate limit por custo, quota de PITs abertos, liberação após consumo/abandono, rotação administrada da chave, observabilidade, ordenação múltipla e hidratação completa no PostgreSQL. Abrir pesquisas sem fechá-las pode acumular recursos até o TTL; a quota é requisito da implantação, não capacidade comprovada deste módulo.

## Evidência

Os 41 testes passaram em 0,34 segundo antes da pausa coordenada dos testes por pressão de memória causada pelo ensaio de navegador. Cobrem continuidade em nova instância com a mesma chave, atualização externa sem alterar o snapshot, última página repetida, tokens adulterados/expirados, revogação/contexto, PIT expirado sem reinício implícito, partial shards, tipos/ordem/count, resposta limitada, liberação e ausência de escrita no índice. A primeira execução encontrou somente uma chave sintética de tamanho incorreto no fixture; o fixture foi corrigido e a execução completa passou. Homologação com Elasticsearch 9 real e carga permanece pendente.
