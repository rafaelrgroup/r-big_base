# Projeção de busca e publicação da outbox

Implementação de 08/09/2026 em `backend/bigbase/canonical_search.py`. Versão do contrato: `canonical-search-2026-09-08.2`. Este bloco usa somente dados sintéticos e transporte HTTP simulado nos testes. Não criou índice, serviço ou daemon e não conectou ao Elasticsearch atual.

PostgreSQL permanece a fonte oficial do cadastro completo. O Elasticsearch contém uma projeção reconstruível para encontrar IDs e versões. A ficha, o histórico e as entradas originais devem ser carregados do PostgreSQL, preservando a ordem dos IDs encontrados. Este módulo não implementa a API autenticada ou a integração HTTP da paginação/PIT (o leitor separado está em BUSCA-PAGINADA-PIT.md), exportação, a instalação do índice ou a troca de tráfego.

## Contratos entregues

| Função/classe | Contrato |
|---|---|
| `build_projection(entity)` | Recebe o resultado de `CanonicalStore.get_entity(owner_id)` e devolve documento determinístico, versão cadastral, hash e contadores de omissões. Não modifica a entrada nem consulta relógio/rede. |
| `index_definition()` | Devolve `settings` e `mappings` para provisionamento explícito posterior. Não executa criação. |
| `build_query(criteria, entity_type=None, include_pending=False, include_invalid=False)` | Compila AND/OR e filtros no mesmo item. A resposta pede apenas ID/versão, com desempate por ID. |
| `ElasticProjection(client, base_url=..., alias=..., expected_cluster_uuid=..., expected_index_uuid=...)` | Cliente `httpx.Client`, URL, alias de escrita e identidades esperadas de cluster/índice obrigatórios. A construção não faz requisições. `publish(document)` verifica o destino e publica a versão. |
| `OutboxConsumer(store, publisher).run_once(limit=100, lease_seconds=60)` | Reserva eventos, lê a versão atual do proprietário, publica e confirma a reserva somente após prova do Elasticsearch. Devolve contadores e códigos de erro sem dados cadastrais. |

`httpx` já é usado pelo transporte de migração; o cliente fornecido controla autenticação/TLS. Requisições deste módulo têm limite de 30 segundos e não seguem redirecionamentos. Respostas são lidas em fluxo com teto configurável de 1 KiB a 16 MiB, inclusive no GET usado para verificar conflitos. O cliente pede `Accept-Encoding: identity` e rejeita respostas comprimidas antes da descompressão, evitando expansão sem limite de memória. HTTP remoto é recusado; conexões remotas exigem HTTPS. A porta 9200 é recusada para proteger o serviço legado. O destino validado é usado como URL absoluta mesmo se o cliente fornecido tiver outra base configurada. O destino precisa de alias explícito e mapping com a versão esperada; cada publicação confere cluster UUID, index UUID e mapping antes da escrita. A escrita usa `require_alias=true`, portanto não cria silenciosamente um índice cujo nome foi digitado errado. Esta versão exige alias apontando para um único índice. Provisionamento, credenciais de privilégio mínimo, réplicas, retenção e monitoramento ainda pertencem à implantação do novo ambiente.

## Campos indexados

O mapping usa `dynamic: strict`. Nomes de campos recebidos não viram propriedades novas do Elasticsearch. Há uma coleção `items` do tipo `nested` e uma coleção `items.fields`, também `nested`, com nomes de propriedades fixos. Assim, cidade e rua de endereços diferentes não satisfazem um filtro que exige o mesmo endereço. A flag de um telefone também não confirma outro telefone. Esse é o uso documentado de objetos `nested` pela [referência oficial do Elasticsearch](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/nested).

| Tipo de item | Campos disponíveis nesta versão |
|---|---|
| Identidade | Nome, razão social/nome fantasia quando recebidos nesses caminhos, nascimento, óbito, abertura, sexo, nacionalidade |
| Documento | Número, tipo, país, órgão, UF, resultado sintático |
| Telefone | Número, tipo, classificação telefônica, uso, ramal, país, número nacional, código de área, resultado sintático |
| Email | Email, domínio, resultado sintático |
| Endereço | País, UF, cidade/código, bairro, rua/tipo/título, número, complemento, CEP, uso/classificação |
| Username | Username, plataforma, URL e ID externo |
| Relação | Alvo por ID/nome/documento, tipo, papel, direção, início/fim e participação |
| Atividade | Código, descrição, papel, área e indicação de atividade principal |

Os caminhos exatos aceitos estão na constante `FIELDS`. A projeção não inventa campos que estejam ausentes no cadastro: por exemplo, indexar razão social exige que um adaptador empresarial a tenha produzido. Atributos conhecidos da normalização podem virar componentes consultáveis com referência à observação e ao caminho que os originou. **Uma confirmação vinculada ao valor principal não é transferida para esses componentes derivados.** Componentes canônicos explícitos têm prioridade sobre atributos derivados de mesmo nome.

Cada componente inclui status, indicação de pendência, ID da observação, hash da fonte e datas efetiva/recepção. Metadados selecionados da fonte e versões são armazenados em texto JSON ASCII sem indexação. O nome original da fonte pode conter NUL ou Unicode incomum e continua íntegro nesse texto e no PostgreSQL; o filtro de fonte usa hash do valor exato.

Campos customizados e caminhos desconhecidos ainda não são pesquisáveis nesta projeção. Valores com NUL, surrogates isolados, mais de 8.192 bytes UTF-8 ou estruturas não contempladas são omitidos da busca e contabilizados; permanecem completos no PostgreSQL. Não se substitui caractere nem se trunca um valor para fingir que ele foi indexado. O documento informa `omitted`, que a operação deve monitorar durante o piloto.

## Tipos, flags e critérios

Texto possui valor exato, valor com caixa/acentos normalizados e campo textual. Existem igualdade sensível à caixa (`eq`), igualdade normalizada (`ieq`), prefixo, trecho literal (`contains`) e busca textual com todos os termos (`match`). Caracteres `*`, `?` e barra invertida recebidos em `contains` são escapados. Trechos usam campo próprio do tipo wildcard, com índice interno por n-grams, e exigem pelo menos três caracteres sem contar preenchimento nas pontas. Não usam wildcard sobre o campo keyword. A técnica está descrita na [referência oficial do campo wildcard](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/keyword). Prefixo de um caractere exige filtro adicional seletivo na mesma conjunção, como documento/telefone/email/username/CEP exato ou intervalo de datas com ambos os limites. Uma alternativa OR independente não libera esse prefixo. Ainda são necessários limites de acesso e ensaios de carga na integração da API.

`null`, `false` e zero continuam distintos. Números inteiros/Decimal são indexados como tokens decimais exatos, sem conversão para float e sem depender da precisão do contexto Decimal. A igualdade numérica permite `1`, `1.0` e `1.000` representarem a mesma quantidade; não torna `true` igual a 1. A forma original permanece no PostgreSQL. Intervalos nesta versão são restritos a datas conhecidas; intervalos numéricos e campos adicionais tipados exigem outro contrato de indexação.

As flags fixas são `valid`, `is_whatsapp`, `ownership_confirmed`, `deliverable` e `residence_confirmed`. Cada uma mantém valor booleano ou null, estado textual equivalente, aplicabilidade, origem e datas. Flags ausentes, incompatíveis com o valor atual ou pertencentes a um componente pendente têm estado desconhecido na projeção. Não são convertidas para false.

`confirmation_recorded` significa que existe evidência positiva vinculada ao componente resolvido; não substitui a avaliação de expiração. O filtro de flags compara vencimento com `now` no momento da consulta. Assim, uma evidência vencida deixa de satisfazer true/false e passa a satisfazer desconhecido sem alterar o documento nem fabricar um evento histórico. Isso mantém a projeção determinística para uma mesma versão cadastral.

Por padrão, a consulta exclui componentes pendentes e invalidações explícitas ainda aplicáveis. O chamador pode incluir ambos por opções separadas. Pedir somente `flags.valid=false` exige `include_invalid=True`. Não há propagação de flags entre componentes diferentes do mesmo item. A consulta de um atributo derivado também respeita a invalidação aplicável do valor pai por meio de parent_valid, sem copiar essa evidência para a flag própria do atributo.

Exemplo de cidade e rua no mesmo endereço:

```json
{"same_item":{"kind":"address","conditions":[
  {"field":"city","op":"ieq","value":"Cidade Sintética"},
  {"field":"street","op":"contains","value":"Rua Exemplo"}
]}}
```

Exemplo de número com WhatsApp positivo no mesmo componente:

```json
{"kind":"phone","field":"number","value":"+5511999990000","flags":{"is_whatsapp":true}}
```

Grupos usam `{"all":[...]}` e `{"any":[...]}`. A profundidade máxima é oito, com até 100 nós/condições e 50 condições em um `same_item`. Propriedades desconhecidas, scripts, operadores não suportados e coerções booleanas são recusados. Autorização, rate limit, quota por custo, auditoria e escopo de fonte do usuário ainda serão aplicados na API integradora.

## Garantia de publicação e repetição

O consumidor sempre lê a versão atual do proprietário no PostgreSQL. Não reconstrói um estado antigo a partir do evento e não publica uma versão menor que a indicada nele. A escrita leva `version=<record_version>`, `version_type=external_gte` e `wait_for_active_shards=all`. O versionamento externo faz o Elasticsearch rejeitar versões inferiores; a modalidade `external_gte` aceita também repetição da mesma versão. O contrato desta implementação exige projeção determinística e somente este escritor no índice, conforme os cuidados da [API oficial de indexação, versão 9](https://www.elastic.co/docs/api/doc/elasticsearch/v9/operation/operation-index).

Uma resposta 200/201 só libera o evento quando ID, índice físico, versão, resultado e sucesso de todos os shards esperados são coerentes (`successful == total`, `failed == 0`). Falha de rede, JSON inválido, erro de indexação, redirecionamento ou réplica com falha deixam o evento sem confirmação. Não se registra corpo de erro HTTP ou dados cadastrais no relatório.

Conflito 409 isolado não prova sucesso. O publicador lê o documento atual e verifica ID, índice, versão do contrato, versão externa e hash do conteúdo. Só considera o evento ultrapassado quando há versão superior comprovada; a mesma versão exige conteúdo idêntico. Divergência de conteúdo é erro operacional. Uma versão superior não pode ser sobrescrita por um worker que terminou atrasado.

Se o Elasticsearch confirmou a escrita, mas o lease expirou ou a confirmação PostgreSQL falhou, o evento permanece elegível para repetição. O próximo worker reenvia a projeção de modo idempotente. Reinício do processo não perde eventos aceitos no PostgreSQL. Não há confirmação antecipada, exclusão ou fila transitória como única cópia.

Alterar as regras desta projeção requer nova versão do contrato, novo índice e reconstrução controlada antes de trocar o alias. Não publicar duas regras distintas com o mesmo `record_version` no mesmo índice. O índice de busca é reconstruível; este módulo não apaga nem corrige histórico cadastral.

## Limites e evidência

O documento tem limite de 9.000 objetos nested e 8 MiB. Exceder qualquer um interrompe a publicação inteira e mantém o evento pendente; não se entrega uma ficha parcialmente indexada como se estivesse completa. O limite está explícito porque nested possui custo e limites próprios no Elasticsearch. O mapping strict também evita a expansão automática de tipos descrita na [referência oficial de dynamic mapping](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/dynamic).

O consumidor entregue processa uma reserva por vez, sequencialmente. Tamanho do lote e duração do lease devem considerar a latência de publicação; várias instâncias podem consumir a outbox com as reservas do PostgreSQL. Lotes HTTP, coalescência por proprietário, renovação de lease, fragmentação de proprietários muito grandes, rebuild em fluxo, cursor/PIT, ordenação múltipla definitiva e prova de 50 req/s não foram implementados neste bloco.

Validação local: 59 testes em `backend/tests/test_canonical_search.py` passaram na execução mais recente. O mapper possui outros 41 testes aprovados em execução anterior; a execução combinada de 95 testes antecedeu os cinco testes adicionais de seletividade/derivados. O leitor paginado ganhou 41 testes separados, documentados em BUSCA-PAGINADA-PIT.md. Cobrem ordem de eventos, duas publicações concorrentes simuladas, falha HTTP/ACK, lease, conflito não comprovado, hash divergente, triestado, Decimal extenso, Unicode/NUL, correlação no mesmo item, flags sem herança, ausência de criação de índice, mapping estrito, identidade do destino revalidada, HTTPS obrigatório fora do loopback, respostas em fluxo limitadas, recusa de compressão antes da expansão e limites. São testes de contrato e `httpx.MockTransport`; não substituem ensaio em Elasticsearch 9 e PostgreSQL no novo destino.
