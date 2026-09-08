# Reconciliação do destino por campo

`backend/bigbase/canonical_reconciliation.py` implementa uma verificação independente da escrita: relê a origem, reaplica o adaptador versionado e compara os átomos esperados com as linhas efetivamente armazenadas no PostgreSQL. Não considera suficiente encontrar o hash declarado na operação nem apenas conferir contagens de registros.

O módulo não abre conexão ao ser importado, não inicia migração e não grava dados. Cada registro usa uma transação PostgreSQL curta em REPEATABLE READ e READ ONLY. A origem deve continuar sendo uma restauração imutável ou arquivo verificado; o leitor/guardião da migração é responsável por fixar essa identidade. Nenhum registro de produção foi carregado para desenvolver este bloco.

## Contrato para o transporte

```python
from bigbase.canonical_reconciliation import reconcile_reader

# O leitor deve estar explicitamente aberto e posicionado para reler desde o início.
proof = reconcile_reader(
    reader,
    store,
    expected_records=expected_count,
    expected_actor_id=None,
    cancelled=cancel_requested,
    progress=lambda checked, divergences: report_progress(checked, divergences),
)
```

`CanonicalReconciler(store).check_record(mapped_record, expected_actor_id=None)` verifica uma entrada já preparada, para ensaios e diagnóstico. A identidade determinística de processamento e o contrato de preparação vêm de `prepare_canonical_record` no repositório; não existe outro algoritmo de IDs apenas para o verificador. Este helper é puro e contém dados da fonte em seu retorno interno: não deve ser emitido como relatório público.

`reconcile_reader` chama `reader.pages(checkpoint=None)` e verifica toda a origem, desde o início. O CLI realiza o rewind/revalidação do leitor antes da chamada. `max_records` permite uma amostra explicitamente parcial; nunca autoriza concluir o acervo. `progress(records_checked, divergences)` é chamado após cada registro conferido, sem valores pessoais. Cancelamento retorna `reason='cancelled'` e preserva os dados/checkpoints existentes.

Resultado estável:

| Campo | Significado |
|---|---|
| `state` | `verified`, `partial` ou `failed` |
| `complete`, `passed` | True somente com EOF, contagem esperada e nenhuma divergência/erro |
| `coverage_complete` | A origem inteira foi percorrida e a contagem corresponde; pode haver divergências |
| `records_checked`, `records_matched`, `expected_records` | Entradas conferidas, sem divergência e esperadas; não são pessoas únicas |
| `divergences`, `discrepancy_counts` | Contadores por categoria técnica, sem caminhos ou valores pessoais |
| `atoms_expected`, `observations_expected`, `observations_checked` | Folhas previstas e observações reais, incluindo flags separadas |
| `containers_expected`, `containers_checked` | Estruturas previstas e relidas |
| `evidence_sha256` | Hash agregado dos átomos/estruturas relidos e da identidade esperada de processamento |
| `source_identity_sha256` | Vinculação à identidade do leitor, sem copiar suas entradas |
| `destination_deployment_id` | Instalação canônica explicitamente utilizada |
| `adapter_version`, `normalizer_version` | Regras usadas na releitura |
| `actor_scope` | Ator explicitamente esperado ou apenas consistência interna de proveniência |
| `snapshot_scope` | `per_operation_repeatable_read`, sem alegar um snapshot global de toda a base |

Um limite, cancelamento ou leitor que termine sem evidência de EOF gera prova parcial. Falta de operações, valores diferentes, contagens divergentes ou erros operacionais impedem a aprovação. `divergences` conta diferenças/categorias verificadas, podendo haver várias na mesma entrada; não representa diretamente a quantidade de pessoas afetadas.

## O que é comparado

A operação é encontrada pelo UUID derivado de origem, ID externo, versão da fonte, hash do registro, adaptador e normalizador. São comparados os metadados reais da operação, tipo da entidade e registro de identidades. Uma versão posterior legítima ou outra origem que enriqueça a mesma pessoa não substitui o alvo da conferência.

Para cada observação, compara diretamente:

- Existência exata, sem observações extras, ausentes ou duplicadas.
- UUIDs, componente, item, caminho original, origem e operação.
- Literal de entrada, tipo e encoding, valor normalizado e metadados integrais.
- Datas da fonte/observação/recepção, ator e versões de entidade/item/sequência.
- Status, aplicação ou pendência, valor/observação anterior e vínculo da flag com o valor confirmado.

False, zero, null e vazio não são tratados como equivalentes. JSON textual preserva NUL/surrogates e o lexema decimal disponível. Objetos/arrays vazios e campos desconhecidos permanecem na conferência. Containers são comparados por caminho, tipo, tamanho e metadados, distinguindo objeto com chave `"0"` de array na posição zero.

A aplicabilidade/precedência é reconstruída a partir dos eventos aplicados anteriores à operação verificada. Para conferir o cache atual, a comparação usa os **últimos eventos aplicados no corte da transação de leitura**, incluindo alterações posteriores legítimas. Assim, ler uma operação antiga não exige que o nome ou telefone atual ainda seja igual ao valor antigo. Itens e suas versões também são conferidos contra a história.

Quando `expected_actor_id=None`, o verificador exige consistência do ator entre operação e observações e mantém o operador original de uma operação idempotente. Não afirma provar externamente quem era o operador. Fornecer `expected_actor_id` também exige correspondência com esse ator conhecido. A recepção é conferida entre operação e observações; jamais se fabrica a data de recepção a partir do registro de origem.

## Limites e resultados seguros

Processa um registro por vez, com cursor de servidor para a leitura dos átomos. Limites atuais: 20.000 folhas de entrada, 32 MiB de preparação por registro, 100.000 observações e 20.001 containers por operação, com até 64 MiB de linhas de destino em cada coleção. Exceder um limite interrompe a aprovação; não corta a coleção nem marca a prova como completa. Entradas maiores exigem um caminho operacional próprio antes da carga integral.

A comparação retém a preparação e as linhas da operação dentro desses limites; não é uma alegação de memória constante independente do maior registro. O leitor limita também bytes por página/entrada. A verificação de contadores globais, throughput e custo do segundo passe deverá ser medida no piloto. O índice de operação permite localizar observações da operação inteira, inclusive detectar atribuição indevida de titular, mas seu custo nas partições ainda deve ser dimensionado.

Relatórios contêm contadores, categorias fixas, hashes agregados e UUID de instalação. Não incluem nomes, documentos, telefones, emails, caminhos de origem, IDs externos, textos de exceções SQL, DSNs ou credenciais. Os dados completos continuam no repositório. Erros de leitura/parsing/SQL externos ao contrato são convertidos em código operacional seguro.

A prova é sobre as entradas/versões percorridas da fonte fixada. Ela não afirma que toda versão histórica de outras fontes tenha sido conferida nem que todo o PostgreSQL seja um snapshot único. O hash agregado ajuda identificar o conjunto relido; ele não substitui assinatura externa, backup ou os demais critérios de restauração/implantação.

## Conclusão durável do trabalho

Jobs criados pelo CLI usam `completion_contract='source_destination_reconciliation_v1'`. Depois de ingerir e receber EOF, passam pela releitura do destino antes de `completed`. A verificação tem progresso separado da ingestão; 100% de cópia não é confundido com aprovação dos dados.

`finish_job(..., verification=proof)` persiste a prova em `migration_jobs.verification_json`, junto ao estado terminal. Para esse contrato, recusa conclusão sem prova `verified/complete/passed`, zero divergências, contagens completas e correspondência com instalação, identidade da fonte, adaptador e normalizador. A prova permanece disponível em `get_job()['verification']`. Repetir a conclusão não substitui silenciosamente a prova anterior.

O reconciliador continua somente leitura: a escrita do estado/prova pertence ao transporte/CLI após os testes. Jobs sintéticos legados sem esse contrato mantêm compatibilidade, mas não ganham retroativamente a nova prova. Um resultado parcial/falho mantém o checkpoint de ingestão e precisa de resolução/releitura antes da aprovação.

## Evidência sintética

Na rodada inicial, 23 testes passaram no PostgreSQL 18.6 privado em 16,95 segundos, em 08/09/2026 às 20:03 UTC. Incluem fluxo de dez registros sintéticos, passagem pelo leitor/mapper/CLI/PostgreSQL, releitura de todos os campos e prova persistida. Outros casos verificam operações antigas após enriquecimento por outra origem, flags, literal decimal, null/false/zero, containers, projeção atual, ator, limite parcial, cancelamento, ausência de EOF e impedimento de rebinding da prova.

As falhas foram injetadas em uma fachada de leitura de SELECT dos testes. Nenhum trigger de histórico foi desativado e nenhum dado real foi adulterado. Mantendo o hash declarado da operação intacto, a verificação detectou folha ausente/extra, mudança de literal/tipo/normalização/metadados/status/ator/sequência, containers e cache corrente divergente.

Execução restrita ao fixture sintético:

```text
python3 scripts/run-postgres-tests.py --pytest -- -q backend/tests/test_canonical_reconciliation.py
```

O resultado valida este mecanismo em dados sintéticos. Migração real, ensaio integral do volume, operação de produção, backup contínuo e dimensionamento continuam dependentes do destino e do piloto aprovados.
