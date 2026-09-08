# Leitura, retomada e execução verificável da migração

Implementação isolada de08/09/2026. O transporte, adaptadores e armazenamento canônico foram exercitados em conjunto com entradas fictícias no PostgreSQL18. Este documento não registra carga dos cadastros reais. O destino de produção ainda precisa ser definido, preparado e homologado.

## Entradas e identidade

`JsonlSource` recebe um envelope por linha com `source_id`, `external_id`, `source_version` opcional e `record`. Nesta CLI esse caminho é exclusivo para ensaios sintéticos. SHA-256, tamanho e identidade do arquivo fixam a fonte; cursor contém posição em bytes e contador. Uma alteração no arquivo impede continuar o mesmo job. A leitura distingue null, false, zero, coleções vazias, inteiros extensos e decimais exatos. Chaves JSON repetidas e números não finitos são erros, nunca entradas silenciosamente descartadas.

`ElasticsearchPitSource` exige URL explícita, cluster UUID, índice único e index UUID. HTTP é permitido apenas em loopback; acesso remoto exige HTTPS, inclusive com um cliente HTTP injetado. Não segue redirecionamentos e limita resposta a16MiB por padrão, páginas a1.000 documentos e requisições a30segundos. A carga completa exige também índice protegido contra escrita, contagem e SHA-256 do mapping correspondentes ao relatório aprovado. Nenhum método congela a origem, cria índice ou restaura snapshot implicitamente.

PIT com `search_after` lê uma visão coerente. Uma página com shard ausente, timeout, ID incorreto, sequência fora de ordem ou resposta incompleta não é entregue para commit. Essa navegação segue o [contrato oficial de paginação do Elasticsearch](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/paginate-search-results).

O transporte não transforma PIT em snapshot permanente. Em interrupção intermediária preserva o contexto por até o `keep_alive` vigente, permitindo retomar do último cursor confirmado enquanto ele permanecer válido. EOF, encerramento normal e cancelamento liberam o contexto. Se o PIT expirou, retorna `PIT_EXPIRED_RESTART_REQUIRED`; nunca reutiliza posições `_shard_doc` em um PIT novo. Uma nova tentativa explícita recomeça a varredura e reutiliza a idempotência das operações já preservadas no PostgreSQL. Não se afirma que a retomada depois de qualquer tempo seja instantânea.

## Commit, memória e progresso

Cada página passa por `map_record` e pela reconciliação de folhas. Os fatos incluem caminhos, valores originais/literais, tipos, hashes, versão da regra e proveniência; objetos e arrays mantêm estrutura por containers. Um lote prepara incrementalmente no máximo20.000 fatos e16MiB de JSON canônico por padrão. O limite considera a expansão de metadados, além do tamanho recebido. Ao exceder, `PREPARED_PAGE_LIMIT_REDUCE_PAGE_SIZE` deixa o checkpoint anterior intacto. Reduzir `page_size` permite tentar novamente o mesmo cursor. Um registro individual acima desse limite exige tratamento explícito de registro grande; não é truncado nem omitido.

`transfer_pages` chama `CanonicalStore.apply_batch` com fatos, contador anterior/novo e cursor. Estado, histórico, recibo, outbox e checkpoint são confirmados na mesma transação. Só depois do commit ocorre a notificação de progresso. Falhar ao atualizar o relatório não desfaz uma página já durável, mas a próxima execução recupera o checkpoint do banco. O relatório é escrito por substituição atômica, com permissão0600, e contém somente contadores/IDs operacionais.

A porcentagem refere-se a **registros de origem**, não a pessoas únicas. Permanece abaixo de100% enquanto houver finalização. `completed` exige EOF, contagem esperada, checkpoint e contador de registros do PostgreSQL coincidentes, seguidos por releitura independente da origem e das observações/containers do destino com zero divergências. O segundo passe retorna ao início e, para Elasticsearch, abre outro PIT somente depois de conferir novamente a fonte imutável. Durante essa etapa, o relatório informa `state=verifying`, `records_verified` e `verification_percent`, sem apresentar apenas um99,9% parado. O contrato de conclusão fica fixado nos metadados do job; jobs antigos que tinham somente contagem não adquirem a nova prova silenciosamente. Cancelamento, contagem divergente, falha de uma folha e conflito de identidade não significam conclusão da migração.

O andamento gravado inclui `state`, `records_processed`, `expected_records`, `progress_percent`, velocidade desta tentativa, data e `completion_verified`. Em erro, a CLI registra código operacional sanitizado, preservando os checkpoints; não inclui corpo HTTP, SQL, DSN, senha ou dados de pessoas. `real_migration: true` num erro identifica o tipo de tentativa, não prova que houve commit. Para saber quanto foi efetivamente persistido deve-se consultar o job canônico.

## Comandos explícitos

`python -m bigbase.migration_cli synthetic-jsonl` exige DSN no ambiente `BIGBASE_TEST_PG_DSN`, schema, arquivo, ID do job, fonte, total esperado e arquivo do relatório. `--initialize` cria explicitamente um schema ausente com `environment=synthetic`. Não converte esse schema em produção. Os testes integram por socket Unix privado18769 e recusam qualquer outro banco.

`python -m bigbase.migration_cli preflight` avalia arquivo privado contendo `manifest`, `restore`, `destination`, `pilot` e `source`. Não lê cadastros nem altera banco. Resultado bloqueado retorna código2; erro operacional retorna1.

`python -m bigbase.migration_cli full-elasticsearch` exige DSN em `BIGBASE_MIGRATION_DSN`, destino existente, schema e identidade de implantação correspondentes ao relatório, preflight com fase `full` aprovada e fonte imutável novamente conferida. Autorização HTTP de origem, quando necessária, vem de `BIGBASE_SOURCE_AUTHORIZATION`, nunca da URL/linha de comando. A CLI processa um índice por job e compartilha o mesmo cadastro canônico entre as fontes. Não cria um destino real automaticamente, não aprova um piloto por contagem e não muda tráfego da API.

O relatório de entrada provém de coleta controlada. A CLI revalida a identidade real do banco e as identidades/contagem/mapping da fonte; a coleta e reserva de capacidade remota ainda dependem da implantação definida. Esses argumentos não constituem um certificado criptográfico fornecido por cliente externo.

## Piloto com limites

`migration_pilot.execute_pilot_source` implementa uma execução limitada por quantidade, tamanho de página, duração e orçamentos de volumes. Usa os mesmos adaptadores e commits canônicos. Um monitor de capacidade pertencente ao destino precisa fornecer consumo acumulado desde a criação do job, espaço livre e reserva real para o próximo lote em cada volume. Sem medida/reserva a escrita é recusada. O executor não inventa nem reserva espaço de um servidor remoto por conta própria.

O limite de registros deve ser divisível pelo tamanho da página. Nunca se corta uma página preservando seu cursor final, pois isso saltaria entradas. Prazo e recursos são conferidos entre leituras, antes e depois do commit; o prazo é cooperativo e uma operação de I/O em andamento continua sujeita ao seu próprio timeout. Se houver consumo inesperado acima do orçamento, a execução interrompe antes de iniciar outro lote; a implantação deve reservar o teto de expansão do lote e a margem de espaço antes de permitir escrita. Um novo processo exige uma tentativa limitada nova, reaproveitando a idempotência, para não renovar silenciosamente o orçamento de tempo antigo.

O resultado `pilot_ingested` prova somente que o lote limitado foi preservado e suas folhas reconciliadas antes da escrita. Mantém `full_migration_complete`, `representative_sample_verified` e `destination_reconciliation_complete` como false. Não conclui o job de carga integral. Seleção representativa, releitura/reconciliação do destino, reconstrução de busca e medição real dos componentes de capacidade continuam obrigatórias para produzir um piloto SUCCESS aceito pelo preflight. A execução real desse piloto ainda não está conectada a uma CLI, pois a coleta/reserva do ambiente de destino não foi definida.

## Validação e próximos passos

Os testes verificam rollback/retomada PostgreSQL, reprocessamento sem duplicar história, mesma pessoa em duas fontes, precisão decimal e Unicode, contagem divergente, progresso somente após commit, recusa de fonte mista ou alterada, limites de memória e PIT expirado. A conclusão também é recusada quando a releitura do destino encontra divergência. Os testes de Elasticsearch são de protocolo com transporte simulado; os de PostgreSQL usam uma instância18 real isolada e somente dados sintéticos. Piloto e capacidade têm testes de limites com monitores simulados, sem medida de disco real da futura implantação. Os resultados exatos desta versão constam do manifesto de integração mais recente; não atribuir uma execução antiga a mudanças posteriores.

Continuam necessários para a carga real: destino aprovado, cópia imutável correspondente ao backup, observador/reservas de capacidade, fila de resolução de identidades contraditórias e registros grandes, reconciliação de destino, medições representativas, publicação/reconstrução da busca, interrupção/retomada operacional e ensaios de escala. A CLI não altera a API em uso e a migração real continua identificada como não iniciada até haver sua própria prova.
