# Leituras canônicas sintéticas por HTTP e painel

O bloco conecta lookup, metadados e coleções paginadas do PostgreSQL à autenticação HTTP existente. O painel possui uma entrada separada **Consulta canônica** para abrir pessoa/empresa por ID canônico, identidade da fonte ou documento exato. Não normaliza um documento na consulta nem confunde os IDs dos dois bancos.

**Estado:** implementação de leitura em ensaio sintético; escrita, exportação e produção canônicas continuam pendentes. Autenticação, auditoria de acesso e os fluxos locais existentes ainda usam SQLite de desenvolvimento, de processo único. Não habilitar múltiplos workers de produção com esta configuração.

## Dependências explícitas

`create_app(..., canonical_reads=CanonicalReads(repository, expected_deployment_id=...))` recebe o repositório explicitamente. Não procura DSN em variáveis do serviço, não inicializa o schema e não faz fallback para o cadastro SQLite. A configuração HTTP atual aceita somente banco `bigbase_test`, socket Unix explícito e porta 18769; verifica PostgreSQL 18, `environment=synthetic` e o UUID da implantação na construção e a cada requisição. Recusa DSN implícito, TCP, `hostaddr`, serviço libpq e redirecionamento por `PGHOSTADDR`/`PGSERVICE` antes de conectar.

Sem a dependência, `/canonical/status` autenticado informa `enabled=false` e a ficha explica que o ensaio não está configurado. Isso permite instalar o código preservando as contas e o armazenamento do serviço existente. Não aponta o painel principal automaticamente para um fixture de teste.

A pesquisa aceita um `CanonicalSearchReader` opcional, configurado pelo operador com URL, alias e UUIDs fixados e chave persistente de cursor. Não há cliente ES implícito; o leitor recusa a porta 9200. A prova desta rodada usa transporte HTTP simulado, sem cluster ES real.

## Contrato `/api/v1`

| Rota | Entrada / resultado |
|---|---|
| `GET /canonical/status` | Disponibilidade, ambiente sintético, armazenamento de autenticação local e estado da pesquisa |
| `POST /canonical/{people|companies}/lookup` | Exatamente identidade `{source_id,source_record_id}` ou `{country,document_type,value}`; retorna metadados e cursores iniciais |
| `GET /canonical/{people|companies}/{id}` | ID, tipo, versão e referências explícitas às coleções, sem hidratação ilimitada |
| `POST /canonical/{people|companies}/{id}/items` | `limit` 1–200, `order` asc/desc, `kind` opcional e `cursor`; resumos e cursores de campos/histórico por item |
| `POST /canonical/{people|companies}/{id}/fields` | Página de valores/flags no corte; filtros opcionais `item_id`, `field_path`, `source_id`, `dimension` |
| `POST /canonical/{people|companies}/{id}/history` | Mesmos filtros, observações imutáveis inclusive eventos não aplicados |
| `POST /canonical/{people|companies}/search` | `filters` no contrato do compilador canônico, `sort` somente ID asc, `page_size` 1–20, `include_pending`, `include_invalid`, `cursor` |
| `POST /canonical-search/close` | `release_cursor` para liberação explícita do PIT |

As páginas usam POST para manter cursores e critérios fora da URL. Exigem CSRF nas sessões humanas; integrações usam `X-API-Key`. Campos não suportados são recusados com 422, incluindo `authorization` fornecida pelo cliente. Uma chave limitada por origem não pode realizar lookup usando outra origem. Como nas leituras locais existentes, a permissão `read` permite ler o cadastro consolidado; `sources` da chave controla atribuição/lookup por origem, **não é uma política de ocultação de contribuições de outras fontes na ficha**. Restringir leitura por fonte requer política de dados própria antes de homologação.

A busca recebe somente IDs/versões do ES e lê os metadados atuais no PostgreSQL, preservando a ordem. `record_version`, `indexed_version` e `indexing_pending` mostram a diferença. Cada ficha oferece cursores para leitura completa progressiva de suas coleções. O PIT congela a seleção da busca; os metadados canônicos mostram a versão corrente no momento da hidratação, não fingem pertencer à versão antiga do índice. Versão do índice à frente do PostgreSQL é recusada. A busca não abre coleções ilimitadas nem usa conteúdo cadastral do ES como fonte oficial.

## Preservação e autorização

Metadados emitem os três cursores iniciais na mesma transação/corte. Itens emitem cursores de campos/histórico no corte da página. Continuação usa o cursor original para reconstruir campos e confirmações por observações imutáveis: escritas posteriores não entram nesse corte. Nova abertura mostra versão nova.

O HTTP envolve cada cursor opaco do repositório em um envelope cifrado com a chave persistente da aplicação. Vincula implantação, pessoa/empresa, entidade, coleção, filtros, ordem, usuário, sessão ou chave e revisão das permissões. Cursores crus do repositório, outra sessão, outra chave, mudança de permissões e contexto alterado são recusados. A duração máxima continua limitada pelo cursor PostgreSQL (15 minutos ociosos, uma hora absoluta). O tamanho da página pode ser reduzido na continuação sem alterar critérios.

A autenticação existente reavalia usuário ativo, OTP concluído, sessões, expiração/revogação, permissões, escopos e limites a cada requisição, antes da leitura. Falha Redis continua retornando 503. Auditoria registra ação e ID opaco; não inclui critérios, valores, cursores ou credenciais. Erros de banco/destino são sanitizados. Respostas usam `no-store`.

Quando a leitura canônica está configurada, a aplicação também remove os cursores técnicos expirados em lotes de até 1.000, verificando o destino antes de cada lote. Lotes completos são seguidos por nova tentativa após 250 ms; quando o lote termina ou ocorre uma falha, o intervalo é de 60 segundos. Falhas são registradas sem mensagens do driver, DSN ou valores privados, e não encerram a manutenção. A tarefa é cancelada no encerramento da aplicação. Essa limpeza não apaga entidades, observações ou histórico cadastral; sem leitura canônica configurada, ela não é iniciada.

A serialização HTTP canônica preserva Decimal como número JSON exato, além de null, false, zero, NUL e escapes Unicode; não usa o conversor float do FastAPI. O painel reutiliza o parser de precisão. O repositório limita páginas a 8 MiB; o envelope HTTP tem teto de 10 MiB. Uma linha que exceda o orçamento retorna 413 explícito, sem truncamento; o trabalho canônico de arquivo completo ainda precisa ser integrado. O contrato de entrada JSON existente recusa números que perderiam precisão; não anuncia pesquisa decimal arbitrária como concluída.

## Ensaio de navegador

`scripts/run-browser-tests.py` requer o fixture PostgreSQL privado preparado. Recebe `BIGBASE_TEST_PG_DSN` explicitamente da verificação ou lê somente `var/postgres-test/dsn.txt`. Cria um schema aleatório `cbbrowser_<UUID>` com pessoa e empresa fictícias, conserva a autenticação em `var/browser-test` e usa o servidor de teste 18767. Ao sair, encerra somente seu processo HTTP e remove somente seu schema de teste. Não inicia serviço permanente, não prepara fixture em `var/development.sqlite3` e não conecta o serviço 18765 aos dados do navegador.

O fluxo abre fichas, percorre itens/histórico, verifica origens, precisão e triestado, troca pessoa/empresa sem manter resultados antigos e confere largura móvel. Casos de backend cobrem corte após escrita, flags do valor antigo, eventos atrasados, revogação, CSRF, limites, outro titular, reinício com chave persistente, expiração, destino divergente e busca HTTP simulada com metadados PostgreSQL reais.

As evidências da rodada e os resultados exatos estão em [validacao-leituras-canonicas.json](validacao-leituras-canonicas.json); testes antigos não são atribuídos automaticamente a este código.
