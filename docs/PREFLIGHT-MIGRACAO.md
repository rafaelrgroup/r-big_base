# Pré-condições de piloto e migração

`backend/bigbase/migration_preflight.py` avalia **somente metadados**, sem ler arquivos, conectar bancos, alterar índices ou carregar cadastros. A função é `assess_migration(backup_manifest, restore_report, destination_info, pilot=None, source_info=None)`. O único contexto externo consultado é o relógio UTC para rejeitar datas futuras. Os dicionários de entrada permanecem intactos.

O plano operacional completo, mantido no diretório privado definido para a implantação, continua obrigatório. Consulte também a matriz pública em [IMPLEMENTACAO.md](IMPLEMENTACAO.md). `ready` aprova as pré-condições da etapa indicada; não homologa o projeto, a migração completa, a troca de tráfego, replicação/failover ou as metas de 50 requisições/s. O retorno mantém `production_cutover_approved: false`. Ausência de destino bloqueia a carga real e mantém `development_allowed: true`.

## Duas etapas sem dependência circular

`source_info.mode` aceita `full` (padrão) ou `pilot` explícito. Um modo desconhecido bloqueia a avaliação.

| Etapa | Condição adicional | Resultado permitido |
|---|---|---|
| `pilot` | Plano limitado por registros, lote, tempo e orçamento de disco observado | `ready: true`, `phase: pilot`, `ready_for_full_import: false` |
| `full` | Piloto representativo medido de pelo menos 1.000.000 de registros, reconciliação sem perda e capacidade medida com margem | `ready: true`, `phase: full`, `ready_for_full_import: true` |

O primeiro piloto pode começar, por exemplo, com 10.000 entradas e ampliar até um milhão, sem exigir um piloto anterior. Os dois modos exigem o mesmo backup restaurado e destino real identificado. O executor de carga completa deve exigir **`ready_for_full_import is True`**, nunca apenas `ready`. O executor de piloto deve impor os limites aprovados, interromper com checkpoint preservado ao atingir qualquer limite e nunca transformar o trabalho em carga completa automaticamente.

## Evidência de backup e restauração

`backup_manifest` usa o contrato existente de `backup-manifesto.json`: `snapshot`, `requested_at`, `cluster.cluster_uuid`, `indices_before` com `index`, `uuid`, `docs.count`, e `mappings` das duas fontes cadastrais. Contagens CAT admitem strings decimais canônicas ou inteiros; booleanos e floats não são contagens válidas.

O manifesto original não contém UUID do snapshot concluído. Por isso **`source_info.snapshot` é obrigatório** e recebe a resposta de metadados do snapshot consultado pelo coletor: `snapshot`, `uuid`, `state`, `failures`, `shards.total/successful/failed`, `indices`, `end_time` e/ou `end_time_in_millis`. Deve ser `SUCCESS`, sem falhas e com todos os índices do manifesto, sem duplicações. Se ambas as datas forem fornecidas, elas devem concordar.

`restore_report` usa o relatório existente `restore-validation.json`. São conferidos:

- `status: SUCCESS`, `phase: completed`, nome/UUID do mesmo snapshot, cluster de origem correspondente e cluster restaurado distinto, versão `9.5.3`.
- `repository_readonly: true`, `global_state_restored: true`, `feature_states` incluindo `security`.
- Todos os índices restaurados uma única vez, contagens iguais às do manifesto, `recovery_done: true`, `query_test_passed: true`, `failed_shards: 0` e nome do índice restaurado. Índices internos são conferidos para o backup, mas nunca entram na seleção de pessoas da migração.
- SHA-256 de todos os mappings; onde o manifesto fornece o mapping, o hash deve coincidir com JSON canônico (`sort_keys`, separadores compactos e UTF-8 sem escapar Unicode). Os índices internos sem mapping no manifesto continuam dependentes da prova de leitura/recuperação e do hash registrado pelo validador.
- Ao menos um arquivo de configurações com nome único, SHA-256 válido e as três verificações estritas: `checksum_verified`, `archive_read_verified`, `required_paths_verified`.
- Ordem temporal: solicitação ≤ fim do snapshot ≤ início da restauração ≤ conclusão ≤ relógio atual + cinco minutos. Datas exigem fuso horário. Não foi inventado prazo de expiração para um backup restaurado válido.

## Destino identificado e separado

`destination_info` deve combinar o resultado **obtido do PostgreSQL conectado** por `CanonicalStore.deployment_info()` com a identidade esperada e a inspeção de capacidade da infraestrutura. Uma declaração em arquivo não autentica sozinha um servidor; o chamador é responsável por comparar a coleta com a conexão realmente usada, imediatamente antes da operação.

| Campo | Exigência |
|---|---|
| `deployment_id`, `expected_deployment_id` | UUID não nulo e exatamente correspondente ao destino aprovado |
| `environment` | `staging` ou `production`; `synthetic` sempre bloqueia dados reais |
| `schema`, `database` | Identificadores explícitos do armazenamento conectado |
| `schema_version` | Inteiro `1`, compatível com a implementação atual |
| `server_version_num` | Inteiro entre `180000` e `189999` |
| `created_at`, `observed_at` | Datas com fuso: criação ≤ observação ≤ agora + cinco minutos |
| `dedicated_for_migration` | `true`, confirmação operacional de infraestrutura nova de destino |
| `synthetic_only`, `test_fixture` | Quando presentes, devem ser `false`; nenhuma flag `real` sobrepõe o bloqueio |
| `capacity.volumes` | Lista de `{id, free_bytes}` com IDs únicos e bytes livres inteiros não negativos, medidos na infraestrutura |

Os IDs de volume são referências internas do relatório de capacidade. Um mesmo filesystem deve ter **um só ID**, mesmo que hospede mais de um componente; do contrário seria contado duas vezes. Réplica e backup em armazenamento separado têm volumes próprios com espaço observado. O coletor precisa considerar todo destino físico da operação; o gate não consulta mounts nem verifica por conta própria isolamento de máquinas ou domínios de falha. `server_address`, `server_port` e `current_user` do diagnóstico podem permanecer na entrada privada; não são copiados para a saída.

## Origem estável para a carga

Além de `snapshot`, `source_info` contém:

```json
{
  "mode": "full",
  "observed_at": "2026-09-08T19:00:00Z",
  "cluster_uuid": "UUID-REAL-COLETADO",
  "expected_cluster_uuid": "UUID-FIXADO-PARA-A-OPERACAO",
  "indices": [{
    "source_id": "pessoas",
    "index": "pessoas",
    "index_uuid": "UUID-DO-INDICE-COLETADO",
    "expected_index_uuid": "UUID-DO-INDICE-FIXADO",
    "count": 3000000,
    "mapping_sha256": "HASH-SHA256-DO-MAPPING",
    "read_only": true
  }]
}
```

O exemplo é estrutural e sintético; os marcadores devem ser substituídos por metadados reais coletados. Somente `pessoas` e `pessoas_serasa` são selecionáveis, uma vez cada; uma operação pode selecionar uma fonte. As contagens de registros de origem **não representam pessoas únicas**.

O cluster original exige os nomes/UUIDs de índices do manifesto. Também é possível ler a cópia cuja restauração foi aprovada: o cluster deve ser exatamente `restore_report.restore_cluster_uuid`, o nome físico deve ser o `restored_index` da prova e `source_info.restored_from_snapshot_uuid` deve coincidir com o snapshot. Nessa cópia os UUIDs dos índices serão novos, mas devem estar fixados e verificados pelo coletor. Outra restauração exige sua própria prova; não é aceita por apenas declarar a mesma origem.

Contagens e hashes de mapping precisam coincidir com a prova. `read_only` deve ser booleano verdadeiro com base em bloqueio de escrita verificado operacionalmente. Abrir PIT não satisfaz esse requisito: um PIT permite uma visão consistente, mas não impede que a origem atual receba mudanças ausentes do snapshot. O gate não ativa bloqueios nem autoriza mudar produção. A preparação da origem imutável ocorre separadamente, e o transporte precisa continuar conferindo a identidade fixada e tratar expiração de PIT sem reiniciar silenciosamente em outra visão.

## Capacidade do primeiro piloto

Em modo `pilot`, o argumento `pilot` contém um plano explícito:

```json
{
  "status": "PLANNED",
  "record_limit": 10000,
  "max_batch_records": 100,
  "max_duration_seconds": 3600,
  "volume_budgets": [{"volume_id": "dados", "max_bytes": 1000000000}]
}
```

`record_limit` deve estar entre 1 e 1.000.000; `max_batch_records`, entre 1 e 1.000; tempo e cada orçamento devem ser positivos. Todos os volumes usados precisam ter orçamento e medição de espaço. O executor monitora o consumo efetivo e respeita os limites. Cada volume precisa dispor do orçamento mais 50% de margem. Esses valores de exemplo não são uma estimativa de consumo dos cadastros reais.

## Medição exigida para carga completa

Em modo `full`, `pilot` contém:

| Grupo | Campos obrigatórios |
|---|---|
| Resultado/identidade | `status: SUCCESS`, `snapshot_uuid`, `deployment_id`, `source_ids` correspondentes à seleção |
| Volume | `measured_records >= 1000000`, `projected_records` igual à soma de contagens das fontes selecionadas, `representative: true` |
| Preservação | `reconciled: true`, `lossless_coverage: true`, `failed_records: 0`, `unpreserved_fields: 0` |
| Data | `measured_at` posterior à restauração, não futura e não posterior à observação do destino |
| Medições | `metrics.unique_entities`, `source_intersection_records`, `items`, `observations`, `nested_documents`, `elapsed_seconds`, `search_rebuild_seconds` |
| Disco | `components`: itens com `kind`, `volume_id`, `measured_bytes`, `copies` |

Pendências com valor original preservado e rastreável podem existir. `unmapped_fields` ou `pending_fields` não significam perda e não são bloqueadores: a exigência é zero campo **sem preservação**, não declarar todo dado válido ou já classificado. A representatividade deve ter prova operacional cobrindo ambiguidades, duplicações, conflitos e contatos numerosos; este gate verifica a declaração e os números, não inventa uma amostra representativa a partir de contagem.

Os seis componentes `postgres`, `search`, `wal`, `temporary`, `backup` e `replica` são obrigatórios. `measured_bytes` representa o consumo medido por uma cópia do componente no piloto; `copies` é inteiro positivo. O componente temporário admite zero explicitamente medido; os demais exigem medida positiva. Diferentes componentes no mesmo volume são somados. Cada par componente/volume aparece uma vez. Não usar bytes do snapshot antigo para substituir essas medições.

Para cada componente: `projeção = ceil(measured_bytes × projected_records / measured_records) × copies`. Para cada volume: `livre necessário = ceil(soma das projeções × 1,5)`. A conta usa inteiros exatos, inclui explicitamente WAL, temporários, réplicas e backups antes da margem e é conservadora; reduzir reservas exige nova modelagem operacional fundamentada, não ignorar componentes. Se as fontes selecionadas mudarem, exige-se medição compatível com a nova projeção. O piloto também informa expansão de itens/histórico, tempo de processamento e reconstrução; isso não substitui os ensaios prolongados de carga e recuperação anteriores à troca de tráfego.

## Saída, execução e limites

A saída contém `ready`, `phase`, `ready_for_full_import`, `development_allowed`, `production_cutover_approved`, `checked_at`, `blockers`, `checks` e `capacity`. Os bloqueadores são códigos estáticos, por exemplo `DESTINATION_DEFINED`, `RESTORE_COUNTS_MATCH`, `SOURCE_READONLY`, `PILOT_LOSSLESS_RECONCILIATION`, `CAPACITY_WITH_50_PERCENT_MARGIN`. O relatório de capacidade usa números de volumes, sem caminhos ou credenciais. Nenhum campo livre da entrada, valor cadastral, DSN ou erro do banco é repetido na saída.

A CLI `bigbase.migration_cli preflight` recebe arquivo privado com chaves `manifest`, `restore`, `destination`, `pilot`, `source`, grava o relatório solicitado e retorna código 0 quando aprovado, 2 quando bloqueado. Essa ação continua sem importar dados. O arquivo de entrada deve vir de coleta controlada e atual das identidades/contagens/espaço; JSON entregue pelo operador não é uma assinatura criptográfica. O executor mantém as mesmas identidades após o gate e rejeita alterações antes/durante a carga.

Validação local: `.venv/bin/pytest -q backend/tests/test_migration_preflight.py`. Os testes usam apenas metadados sintéticos, inclusive o caso que simula `environment: staging`; não criam um ambiente real aprovado. Cobrem ausência de provas, IDs/contagens/mappings divergentes, falhas parciais, datas, tipos adulterados, flags falsas de autorização, PIT em origem gravável, orçamento por volume, margem, ausência de piloto e a distinção entre piloto e carga completa. Nenhum teste desse módulo acessa PostgreSQL, Elasticsearch ou cadastros reais.
