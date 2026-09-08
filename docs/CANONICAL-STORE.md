# Fundação do cadastro canônico em PostgreSQL

Implementada em 08/09/2026 em `backend/bigbase/canonical_store.py`, com DDL explícito em `infra/sql/001_canonical.sql`. É um repositório separado do adaptador SQLite que atende ao painel de desenvolvimento. Não altera API, serviços, Elasticsearch de origem ou dados reais. O resultado não homologa a migração dos 476 milhões de registros nem a capacidade de produção.

## Contrato de armazenamento

`CanonicalStore(dsn, schema='bigbase_canonical')` exige conexão explícita. Importar o módulo não abre conexão nem cria tabelas. `initialize(environment='synthetic')` provisiona uma instalação vazia de PostgreSQL 18 em uma transação. Registra UUID de implantação, ambiente, versão e SHA-256 do DDL. Uma instalação existente deve corresponder ao mesmo ambiente e DDL; mudar o argumento não converte dados sintéticos em produção. Schema desconhecido e ocupado é recusado. Alterações posteriores exigem uma migração explícita.

`deployment_info()` retorna `deployment_id`, `environment`, `schema_version`, `schema_sha256`, `created_at`, `schema`, `database`, `database_user`, `server_version_num`, `server_address` e `server_port`. Não retorna DSN/senha. Esses dados identificam a instalação; não comprovam espaço, aprovação de piloto, backup ou isolamento de infraestrutura. O guardião de migração verifica esses requisitos separadamente e rejeita o ambiente `synthetic` para dados reais.

O contrato de entrada é o resultado de `source_adapters.map_record`: `source_id`, `source_record_id`, `source_version`, `record_hash`, `adapter_version`, `normalizer_version`, `entity_type`, `identity_candidate`, `facts` e `containers`. As regras de validação documental e de normalização ficam no adaptador. O repositório recebe átomos já preparados, sem autenticação/HTTP, leitura de origem ou normalização implícita.

Cada fato identifica uma folha por `id` e `source_path`, um item por `target_kind` e `item_key`, e um componente por `target_path`. Exige `input_value`, `input_type` e `normalized_value`, inclusive quando são null, false, zero, string vazia, objeto vazio ou array vazio. IDs de fatos e caminhos de folhas repetidos dentro de uma entrada são recusados. Um átomo de entrada não aceita objeto/array não vazio: a fonte deve ser decomposta por folhas antes da escrita.

`item_attributes`, `normalization`, motivo de pendência, notas, versões de definições e demais metadados do fato ficam preservados na observação e acompanham a projeção daquele componente. Não se atribuem metadados da nova cidade à rua ou ao CEP. Atributos derivados da mesma folha não são apresentados como folhas originais novas.

## Estrutura física e identidade

| Tabela | Unidade armazenada |
|---|---|
| `canonical_meta` | Instalação imutável, ambiente e versão do schema |
| `entities` | UUID de pessoa/empresa, versão e datas |
| `identity_keys` | Chave única de documento ou de origem/ID externo e titular |
| `operations` | Identidade de processamento, hashes, versões, ator e recepção |
| `items` | Item múltiplo de uma entidade, chave contextual, versão e datas |
| `observations` | Evento de um componente ou de uma flag, entrada original, novo valor, estado anterior e proveniência |
| `field_state` | Projeção atual de cada componente ou flag, referenciando sua observação |
| `source_containers` | Caminho, tipo e tamanho de objetos/arrays, sem conteúdo do documento |
| `outbox` | Evento para publicação da versão da entidade, lease e confirmação |
| `migration_jobs` | Estado durável, cursor, checkpoint, contadores e configuração fixada |
| `migration_batches` | Recibo imutável de lote e identidade idempotente |
| `read_cursors` | Contexto temporário privado de leitura em corte estável; somente hash do token |

`identity_keys` tem 64 partições por hash da chave completa. `items`, `observations` e `field_state` têm 64 partições por `owner_id`; PKs/FKs incluem a chave de partição. A escolha implementa a estrutura prevista no plano e ainda exige dimensionamento e piloto. Particionamento tem restrições próprias para chaves únicas; a chave completa e a chave de partição fazem parte do desenho. [PostgreSQL 18: particionamento](https://www.postgresql.org/docs/18/ddl-partitioning.html).

Identidade documental usa a tupla `document/country/type/value`. Identidade de origem usa `source/source_id/source_record_id`. Seus valores completos ficam codificados como JSON textual e são comparados após lookup pelo SHA-256, de modo que uma colisão detectável não atribua outro titular. `lookup_identity(...)` retorna UUID ou null por documento completo ou origem/ID, com consulta indexada. Não normaliza esses parâmetros.

UUIDs de entidades novas, operações, itens, observações e eventos derivam de chaves determinísticas UUID5. Uma entidade existente mantém seu UUID ao receber novos aliases. Um documento consistente pode agregar duas origens; coincidência de telefone/nome/endereço não funde pessoas. A chave do item inclui entidade, tipo e contexto preparado pelo adaptador.

Se as identidades apresentadas já pertencem a entidades diferentes, a transação é recusada com `IdentityConflict`. Também se recusa CPF/CNPJ brasileiro novo e divergente de outro do mesmo tipo já associado ao titular, mesmo que o novo documento ainda não exista no registro de identidades. Isso impede que um ID de origem reutilizado agregue duas pessoas. Uma origem inicialmente sem documento pode receber o primeiro documento consistente. Os valores não são descartados da fonte: o checkpoint não avança e o transporte deve registrar a pendência/resolução antes de continuar. Fusão/desfusão e titularidade documental contestada ainda requerem fluxo específico; este repositório não os executa silenciosamente.

## Precisão, valores desconhecidos e estrutura original

Entradas e valores normalizados são JSON **textual ASCII escapado**, em colunas TEXT, acompanhados de tipo e encoding. Não são um JSON único da entidade. A escolha permite guardar `\u0000` e surrogates sem introduzir NUL físico no PostgreSQL. Caminhos, IDs externos e metadados arbitrários recebem a mesma codificação. As chaves de pesquisa usam hash, sem indexar um texto arbitrariamente longo.

`input_json`/`normalized_json`, quando fornecidos, são conservados literalmente após validar que representam o valor declarado. A comparação distingue bool de número recursivamente e rejeita chaves JSON duplicadas. Tipo declarado da folha deve corresponder ao valor recebido. Inteiros arbitrariamente grandes e Decimal não passam por float. Um float binário é recusado: o transporte deve analisar JSON com `parse_float=ExactDecimal` antes de preparar os fatos.

`ExactDecimal` conserva o lexema decimal, inclusive dígitos e expoente. Para strings/inteiros o adaptador preserva valor e tipo, mas não afirma recuperar a grafia original de escapes ou de um inteiro após seu parsing. Isso está identificado por `input_encoding='canonical_value'` e `input_lexeme_available=False`. Quando há literal decimal de origem, usa `source_decimal_lexeme` e true. Não se declara igualdade byte a byte do documento inteiro.

`source_containers` distingue, por exemplo, objeto com chave `"0"` de array com elemento na posição zero. Null, coleções vazias e campos de semântica desconhecida permanecem como átomos rastreáveis. Não há coluna contendo uma cópia integral permanente do registro de origem.

## Precedência, flags e história

Datas são próprias do fato: `source_updated_at`, senão `observed_at`, define a data efetiva. O repositório **não propaga uma data do registro inteiro** para todas as folhas. Datas informadas exigem fuso; `received_at` vem do PostgreSQL e não substitui uma data ausente da fonte.

Uma observação sem data não substitui um valor datado. Eventos atrasados permanecem no histórico. Empates usam recepção e UUID de evento; datas futuras acima de cinco minutos são pendentes. Fato com semântica `pending` não substitui uma projeção já resolvida, mesmo que chegue depois: uma data ambígua não apaga uma data ISO resolvida. Seu valor e motivo ficam integralmente no histórico. Dados desconhecidos sem estado anterior continuam acessíveis na projeção com seu status de pendência.

Observações registram entrada original, valor normalizado, origem, ator, operação, datas, metadados, status, se foram aplicadas, motivo quando recusadas, observação anterior e valor anterior. `operations`, `observations`, identidades, containers, recibos e identificação da instalação possuem proteção SQL contra UPDATE/DELETE/TRUNCATE. Uma invalidação é um novo fato/flag; não é uma exclusão. Na implantação real, o papel de execução deve ter privilégios mínimos e não ser proprietário das tabelas, pois um superusuário pode remover proteções de DDL.

Uma flag preparada tem formato `{value: true|false|null, observed_at?, source_updated_at?, confirmed_value_json?, ...metadados}`. Validade e WhatsApp não são inferidos. A flag é uma observação independente, com precedência própria, vinculada ao valor que confirma. Sem binding explícito, confirma o input original; uma transformação não transfere sua evidência ao novo valor. Caso o dado corrente seja diferente, a confirmação fica pendente com `value_mismatch`, sem apagar uma confirmação corrente legítima. A confirmação explícita do canônico deve fornecer seu `confirmed_value_json`.

Ao consultar, uma flag antiga vinculada a outro valor é identificada como `applicable=false` e seu valor corrente é null. O valor histórico permanece guardado. Metadados como método, referência e vencimento podem ser preservados, mas a política de expiração, permissões e respostas públicas deve ser integrada ao serviço cadastral/API; o novo repositório não substitui automaticamente a política já existente no adaptador local.

## Lotes, checkpoint e recuperação

Métodos para o transporte:

```python
job = store.create_job(job_key, source_id, metadata={...})
receipt = store.apply_batch(
    prepared_records,
    job_id=job['id'],
    expected_checkpoint=0,
    next_checkpoint=len(prepared_records),
    next_cursor={'seen': len(prepared_records), 'offset': next_offset},
    actor_id='migration-worker',
)
job = store.get_job(job['id'])
# Somente depois de EOF e reconciliação aprovados pelo transporte:
store.finish_job(job['id'], expected_checkpoint=job['checkpoint'], status='completed')
```

Um lote tem de 1 a 1.000 registros preparados. O contador avança exatamente pelo número recebido; se o cursor contém `seen`, deve corresponder ao novo checkpoint. `next_cursor` é um objeto JSON preservado na mesma transação; cursor null serve apenas para consumidores que não precisam retomar uma leitura externa. O transporte limita também bytes/memória e fixa identidade da fonte, snapshot/arquivo e versões nas opções do job.

Estado atual, observações, versões, outbox, recibo, contadores e cursor fazem **um commit**. Uma falha em qualquer registro ou na inserção do evento desfaz o lote inteiro. `apply_batch` não captura a exceção para fingir avanço. Esse comportamento usa o controle transacional do psycopg. [Psycopg 3: transações](https://www.psycopg.org/psycopg3/docs/basic/transactions.html).

O job é bloqueado antes do checkpoint; chaves de identidade e entidades são bloqueadas em ordem estável. Locks transacionais terminam com commit/rollback. Concorrência entre dois jobs que descobrem o mesmo documento não cria dois titulares. [PostgreSQL 18: locks](https://www.postgresql.org/docs/18/explicit-locking.html).

Um replay do mesmo lote e cursor retorna o recibo com `replayed=true`. Mesmo checkpoint com outro conteúdo/cursor gera `IdempotencyConflict`. Outra leitura da mesma operação não cria novamente observações, versão ou outbox. Identidade da operação inclui origem, ID externo, versão da fonte, hash do registro, versão do adaptador e **versão do normalizador**; reprocessar com nova regra cria história nova. Reutilizar a mesma identidade de operação com fatos preparados diferentes é recusado.

`get_job` retorna estado, checkpoint, cursor, contadores e metadados de fonte fixados. Estados são pending, processing, completed, failed e cancelled. `finish_job` faz comparação do checkpoint e permite os três estados finais. Um terminal não é reaberto silenciosamente; repetição explícita pode criar outro job, mantendo idempotência das operações. EOF sozinho não conclui a migração e `apply_batch` nunca conclui o projeto.

Jobs com `completion_contract='source_destination_reconciliation_v1'` exigem também `finish_job(..., verification=proof)` com reconciliação real do destino, completa e vinculada à fonte/instalação/versões/contagem. A prova fica persistida no próprio job e é retornada por `get_job()['verification']`. O helper `prepare_canonical_record` compartilha o contrato puro de identidade e átomos com o verificador. Detalhes e limites em [RECONCILIACAO-CANONICA.md](RECONCILIACAO-CANONICA.md).

## Outbox e leitura

`claim_outbox(limit=100, lease_seconds=60)` usa locks com `SKIP LOCKED`, devolve eventos com IDs, versão da entidade e token de lease. Dois consumidores não recebem o mesmo evento durante o lease. `acknowledge_outbox(event_id, lease_token)` aceita somente o titular do lease ainda válido. Após vencimento, outro consumidor pode obter novo token; confirmação do token antigo falha.

A entrega é repetível: se o processo publicar e falhar antes do ACK, o evento pode voltar. O próximo publicador deve aplicar idempotência por evento/versão, impedir regressão de versão na busca e confirmar somente após escrita durável no destino. A busca não é publicada por este módulo.

`get_entity(owner_id)` oferece leitura consistente do estado corrente: itens, componentes, fontes, versões, atributos por componente e flags aplicáveis. Sua assinatura foi mantida para os consumidores existentes. É uma hidratação de entidades pequenas usada pelo fixture, sem limite por coleção; as leituras incrementais abaixo devem ser usadas quando não couber uma ficha inteira na memória/resposta.

## Paginação de histórico, componentes e itens

Métodos novos, separados da API HTTP e de sua autenticação:

| Método | Resultado e filtros |
|---|---|
| `page_history(owner_id, ...)` | Observações completas; filtros opcionais `item_id`, `field_path`, `source_id`, `dimension` |
| `page_fields(owner_id, ...)` | Projeção dos componentes/flags no corte; mesmos filtros |
| `page_items(owner_id, kind=None, ...)` | Resumos dos itens e cursores para seus componentes/histórico no mesmo corte |
| `cleanup_read_cursors(limit=1000)` | Remove somente cursores técnicos expirados, em lote limitado |

Os três métodos aceitam `order='asc'|'desc'`, `limit=100` (inteiro estrito, máximo 200) e `cursor=None`. Resposta: `items`, `has_more`, `next_cursor` e `snapshot` com entidade, versão de corte e prazos. Não existe offset nem parâmetro de versão de corte arbitrariamente escolhido pelo cliente. `field_path` é o caminho do componente canônico, como `birth_date` ou `number`; o caminho original também consta em cada observação.

Na primeira página, uma transação curta em REPEATABLE READ captura a versão já confirmada da entidade. Cada observação possui `entity_version`, `operation_sequence` e `item_version`, gravados atomicamente com a operação. Páginas seguintes leem somente eventos até essa fronteira. Uma importação concorrente que chegue com data antiga, nova normalização, um campo adicional ou um novo item não muda o conjunto em navegação. Não é necessário manter uma transação aberta enquanto o usuário navega.

O histórico ordena por `(entity_version, operation_sequence)`, ou seja, ordem das alterações confirmadas, independente de relógio da origem. Itens ordenam por UUID; componentes por UUID do item, hash do caminho e dimensão. Essas ordens são técnicas e estáveis; ordenação alfabética arbitrária de históricos/campos não é prometida por esses métodos. A ordem asc/desc e todos os filtros ficam vinculados ao cursor.

`page_fields` reconstrói o último evento **aplicado** de cada componente no corte, por versão e sequência de aplicação. Não entrega simplesmente o valor atual que pode ter mudado depois da primeira página. Usa chaves de componentes mantidas em `field_state` e histórico imutável para obter os valores do corte; novos componentes sem observação naquele corte são excluídos. A flag é comparada ao valor do componente **na mesma versão de corte**. `value`/`value_json` descrevem a aplicabilidade naquele corte; `normalized_value`/`normalized_json` conservam a evidência registrada, mesmo quando a flag já não se aplica.

`page_items` entrega somente resumos com versão e atualização no corte. `fields.included=false` e `history.included=false` declaram explicitamente que as coleções não estão embutidas. Cada item fornece os cursores `fields.cursor` e `history.cursor`, vinculados ao seu `item_id`, aos filtros restantes ausentes e à ordem ascendente. Assim, abrir os detalhes depois de outra atualização continua exibindo a mesma ficha versionada:

```python
page = store.page_items(owner_id, limit=25)
item = page['items'][0]
fields = store.page_fields(owner_id, item_id=item['id'], cursor=item['fields']['cursor'])
history = store.page_history(owner_id, item_id=item['id'], cursor=item['history']['cursor'])
while history['has_more']:
    history = store.page_history(owner_id, item_id=item['id'], cursor=history['next_cursor'])
```

O cursor é um token aleatório de 256 bits com formato `cb1_...`, sem documento, fonte, valor, ID ou filtro codificado visivelmente. PostgreSQL guarda somente seu hash; contexto, posição e corte ficam na tabela técnica privada. Não depende de segredo efêmero de processo nem do UUID público da instalação. Outra instância com o mesmo banco pode continuar a leitura. Repetir um cursor retorna os mesmos eventos; não avança um ponteiro destrutivo. Usar outro dono dos dados, filtro, ordem ou método gera `InvalidCursor`, assim como alterar ou expirar o token.

O prazo por continuação é de 15 minutos, renovável até uma hora a partir do início original. Esse teto não se estende ao trocar de página ou abrir uma coleção de item. Filtros serializados têm limite de 8 KiB e o token tem tamanho/formato fixos. O serviço operacional deve executar a limpeza limitada dos cursores expirados. A limpeza nunca remove entidades, observações ou itens. O dono vinculado aqui é a entidade canônica; a futura API ainda deve validar usuário/chave/permissões a cada página e vincular seu contexto de autorização.

As leituras usam cursor de servidor e no máximo 200 linhas mais uma linha de antecipação. A página tem orçamento de 8 MiB, com margem reservada para envelope, separadores e tokens de coleção, podendo devolver menos linhas com `has_more=true`. A continuação parte da última linha entregue, sem pular a linha que ultrapassou o orçamento. Se uma única linha ultrapassa esse orçamento, a chamada retorna `CanonicalRowTooLarge`, sem avançar nem omitir dados; o tratamento por artefato/stream na API permanece pendente. Não se varre o acervo inteiro para calcular um total em cada requisição: completude é indicada por `has_more`/`next_cursor`, sem inventar contagem global.

Índices dedicados começam por `owner_id` e cobrem histórico por item, campo, fonte e sequência; a projeção tem índice parcial de observações aplicadas, ordenado por versão/posição. Resumos de itens usam a última observação indexada para obter sua versão no corte, sem agregar todo o histórico. Todos permanecem nas partições do titular. Custo em titulares muito grandes e combinações seletivas ainda deve ser medido no piloto; estes testes verificam consistência, não escala.

## Validação e limites de entrega

Executar apenas no fixture PostgreSQL 18 sintético, com DSN explícito:

```text
python3 scripts/run-postgres-tests.py --pytest -- -q backend/tests/test_canonical_store.py backend/tests/test_canonical_store_pagination.py
```

Os testes exigem banco `bigbase_test`, socket Unix privado e porta 18769; criam schema aleatório próprio e o removem ao terminar. Sem `BIGBASE_TEST_PG_DSN`, a suíte de integração informa skip. Isso não é aprovação de PostgreSQL. Não há conexão por default a 5432 ou ao Elasticsearch atual.

Casos cobertos: átomos exatos, Decimal/lexema, NUL/surrogates, estrutura, tipos recursivos, metadados do mapper, unknown/null/false/zero, deduplicação, documento contraditório já existente ou inédito, nova versão do normalizador, pendência contra valor resolvido, observações atrasadas/futuras, flags por valor, alteração sem herança, bloqueio de reescrita de história, rollback de outbox e de lote ambíguo, cursor/contadores atômicos, replay, concorrência, leases/ACK e retomada por nova conexão do repositório.

Na rodada de paginação, 53 testes PostgreSQL passaram em 24,46 segundos (32 anteriores e 21 novos). Os novos casos verificam escrita concorrente entre páginas, exclusão de eventos/itens/campos posteriores, valores e flags reconstruídos no corte, sequência dentro da mesma operação, filtros, isolamento de cursores, replay/reinício, expiração/limpeza, teto de uma hora, limites estritos, orçamento de bytes sem saltos e índices por titular.

Continuam obrigatórios: implantação formal com papéis separados e migrações versionadas, integração com API e pool PostgreSQL, campos administráveis/relacionamentos/fusão, exposição autorizada das coleções paginadas, publicação e reconstrução da busca, streaming de exportação, ensaios de queda de processo/host, réplica/WAL/backup, failover, dimensionamento, piloto de dados reais reconciliado e testes de carga. O fixture pequeno com fsync ligado verifica correção; não mede 50 req/s nem escala para o acervo completo.
