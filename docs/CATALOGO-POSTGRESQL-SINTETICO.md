# Catálogo transacional de campos no PostgreSQL sintético

O catálogo optativo de campos adicionais persiste no mesmo PostgreSQL das
observações canônicas de ensaio. O contrato
`canonical-postgresql-field-2026-09-09.1` distingue essas definições das cópias
locais vinculadas pelo contrato `canonical-custom-field-2026-09-09.1`.
Um mesmo ID pode existir nos dois catálogos sem alterar seu significado ou
converter observações anteriores. A autenticação permanece no adaptador SQLite
local, de processo único; este bloco não implanta o banco de produção.

## API e painel

| Rota | Contrato |
|---|---|
| `GET /api/v1/canonical/fields` | Permissão read; `after` textual e `limit` de 1 a 100, continuação em `next_after` |
| `GET /api/v1/canonical/fields/{field_id}/history` | Permissão read; versões crescentes, `after` numérico e `limit` de 1 a 100 |
| `POST /api/v1/canonical/fields` | Criar definição tipada, ID opcional; `Idempotency-Key` obrigatório |
| `PATCH /api/v1/canonical/fields/{field_id}` | Atualização explícita com `If-Match` e `Idempotency-Key`; conflito de versão retorna 409 |

Mutações exigem permissão administrativa, sessão humana, CSRF e OTP recente.
Chaves de API não substituem a sessão humana para administrar o catálogo.
As políticas existentes de autenticação e limites também se aplicam às rotas.
O recibo contém operação, definição, versão, SHA256 e indicador de replay.
Uma chave repetida com outro conteúdo retorna conflito; uma resposta perdida
pode ser recuperada com a mesma chave e conteúdo, inclusive após outras versões.

Na Consulta canônica, o painel cria definições e permite renomear,
inativar/reativar e consultar versões com seus hashes. Solicita OTP recente
quando necessário e preserva a chave idempotente na repetição. O editor de
enriquecimento permite selecionar explicitamente o catálogo, a definição e a
versão. Nenhuma falha de PostgreSQL usa automaticamente uma definição SQLite.

## Transações e preservação

Criação/alteração grava estado atual, versão imutável e recibo em uma transação.
Locks por operação serializam repetições; locks por campo serializam alterações
concorrentes e abrangem também IDs ainda desconhecidos. Versão divergente,
definição inválida ou alteração incompatível não gera migração implícita.
Histórico e recibos rejeitam UPDATE, DELETE e TRUNCATE no banco.

No enriquecimento, a leitura/validação da definição ocorre na transação das
observações/outbox. O lock permanece até commit ou rollback, impedindo que uma
inativação concorra entre validação e gravação. Cada observação guarda snapshot,
SHA256, versão e identidade do catálogo. O histórico não depende da definição
atual. Replays já confirmados são resolvidos antes de exigir a versão atual.

Tipos, escopo, referências canônicas, precisão numérica, null/false/zero,
alternativas e conteúdos desconhecidos seguem o contrato de campos adicionais.
Um item já vinculado não muda silenciosamente de catálogo ou definição:
reclassificação exige outra referência e preserva as observações anteriores.

## Isolamento e limites

A extensão `infra/sql/002_synthetic_catalog.sql` é inicializada explicitamente
pelo fixture, após o schema canônico, e vinculada ao deployment e SHA256 do DDL.
As rotas não executam DDL. São aceitos somente ambiente synthetic,
`bigbase_test`, socket privado e porta 18769. Contas existentes e armazenamento
principal não são usados pelos testes de autenticação ou navegador.

A listagem administrativa usa páginas por chave, sem snapshot congelado entre
páginas; alterações concorrentes podem exigir recarregar o catálogo. A versão
informada continua obrigatória na escrita. Indexação dos campos permanece
pendente, assim como autenticação PostgreSQL, demais catálogos, exportações e
importadores canônicos, papéis de produção, piloto, migração e homologação de
carga/recuperação. Nenhum dado real foi migrado por este bloco.

Provas: [validacao-catalogo-postgresql.json](validacao-catalogo-postgresql.json).
Foram aprovadas 406 regressões de backend e 33 grupos no navegador, além do
build. Os 44 testes específicos aprovados na revisão da transferência têm
escopo e hashes próprios, reconferidos sem repetir a execução. As tentativas
com falha e as correções de nomes/seletores estão preservadas na evidência.
