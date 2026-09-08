# Contratos implementados no ambiente isolado

Base `/api/v1`; contrato interativo em `/api/docs`. Os exemplos contêm somente dados sintéticos. Envie `X-API-Key` em integrações; o painel usa sessão HttpOnly e `X-CSRF-Token`. A chave limita escopos e origens. `Idempotency-Key` é obrigatório em enriquecimento, consulta em massa e pesquisa salva.

## Agregar dados

`POST /people/enrich` (`/companies/enrich` com `entity_type: company` para empresas):

```json
{
  "entity_type": "person",
  "source_id": "manual",
  "external_id": "pessoa-sintetica-001",
  "source_updated_at": "2026-09-01T12:00:00Z",
  "observed_at": "2026-09-02T10:00:00Z",
  "reason": "Teste de integração com cadastro sintético",
  "items": [
    {"kind": "identity", "value": {"name": "Pessoa Sintética Exemplo", "birth_date": "1990-01-01"}},
    {"kind": "email", "value": {"email": "exemplo@example.invalid"}, "flags": {"valid": null}},
    {"kind": "username", "value": {"platform": "instagram", "username": "perfil_sintetico"}, "flags": {"ownership_confirmed": null}}
  ]
}
```

Envie `entity_id` para agregar diretamente ao cadastro. Documento consistente ou par `source_id/external_id` pode localizar a entidade existente. Conflitos retornam 409; nome igual não funde pessoas. Repetir a mesma chave e corpo retorna a resposta anterior; outro corpo com a mesma chave retorna 409.

## Confirmar/infirmar uma propriedade

`PATCH /people/{id}/items/{item_id}`, com `If-Match: versão_do_item`:

```json
{
  "source_id": "manual",
  "reason": "Resultado de verificação sintética",
  "flag_evidence": {
    "is_whatsapp": {
      "value": false,
      "checked_at": "2026-09-01T12:00:00Z",
      "expires_at": "2026-10-01T12:00:00Z",
      "method": "verificacao_informada",
      "reference": "evento-sintetico-123"
    }
  }
}
```

Alternativa simples: `flags: {"is_whatsapp": true}` com `observed_at`. Não duplique a mesma propriedade em `flags` e `flag_evidence`. Null é desconhecido; false é negativo explícito. Vencimento apenas marca `flag_details.<flag>.stale`, preservando o resultado e seu histórico. Nenhuma integração externa de WhatsApp é acionada por esse endpoint; ele registra a evidência recebida.

Novo número/email/endereço entra por enriquecimento; confirmação do valor antigo não é transferida. Uma confirmação recebida junto de um valor atrasado fica vinculada a esse valor: se ele não é o valor corrente, a evidência fica no histórico com `applied: false` e `pending_reason: value_mismatch`. Nem mesmo uma data de confirmação mais recente autoriza aplicá-la a outro valor. PATCH ausente não altera outras flags. Versão ausente retorna 428, divergente retorna 409.

## Consulta combinada e em massa

`POST /people/search` aceita o mesmo `filters` da consulta em massa. No adaptador local, `limit` é até 100 e `offset` até 10.000; cursor e busca indexada ainda serão integrados.

`POST /bulk-queries`, com chave idempotente:

```json
{
  "name": "Consulta sintética",
  "entity_type": "person",
  "text": "Pessoa Sintética Alfa, Pessoa Sintética Beta",
  "input_field": "name",
  "match_mode": "eq",
  "filters": {
    "and": [
      {"field": "age", "op": "range", "value": [18, 65]},
      {"item": {"and": [
        {"field": "city", "op": "eq", "value": "Cidade de Teste"},
        {"field": "postal_code", "op": "range", "value": ["01000000", "01999999"]}
      ]}}
    ]
  }
}
```

Para documento use `input_field: document`; a modalidade textual usa CPF por padrão; envie `document_type: CNPJ` para CNPJ. Entradas estruturadas aceitam `document_type` por entrada; listas enviadas aceitam esse parâmetro no trabalho. Não use números JSON para documentos, pois zeros já removidos pelo cliente não podem ser recuperados.

Upload: `POST /bulk-queries/uploads`, multipart `file` CSV UTF-8/XLSX. Cada célula não vazia é uma entrada, sem cabeçalho; devolve `id`, contagem e amostra. Envie `upload_id` no trabalho. Limites de desenvolvimento: 2 MiB e 100.000 entradas. CSV com zeros deve preservá-los como texto; XLSX deve ter células textuais para documentos.

Acompanhe `GET /bulk-queries/{id}`: pending/preparing/completed/failed/cancelled, fase e percentual. Baixe `GET /bulk-queries/{id}/files/result` após completed. `artifact` informa xlsx ou zip. ZIP contém volumes XLSX e manifesto com hashes. Cada item/campo tem coluna, ou referência a abas de valores estruturados/fragmentos para excedentes; não há corte silencioso. Cabeçalhos e conteúdo são texto quando apropriado, nunca fórmulas recebidas do cliente. Números além da precisão segura do Excel são entregues como texto exato, identificados na aba `Numeros exatos` por célula, tipo original e representação JSON. O arquivo é reaberto para verificar os valores numéricos/booleanos e contagens antes de liberar o download. Metadados dos itens, versões/classificação dos campos e definições versionadas até o instante de corte também acompanham o arquivo, incluindo definições renomeadas ou desativadas.

`include_invalid: true` permite que dados invalidados participem da seleção. Fichas exportadas incluem seus dados e histórico completos no instante de corte local. Ainda faltam streaming e checkpoints para escala de produção.

## Importação assíncrona de cadastros

`POST /imports`, com `Idempotency-Key`, recebe `{"name":"Atualização sintética","entries":[PAYLOAD_DE_ENRIQUECIMENTO,...]}` e retorna 202 com o ID do trabalho. Cada entrada usa o mesmo contrato de `POST /people/enrich` ou `/companies/enrich`, incluindo `entity_type`, `source_id`, datas, itens e flags. O painel aceita arquivo JSON com uma lista ou JSONL, um payload por linha.

No adaptador local, cada pedido permite até 1.000 entradas e 2 MiB. A fila compartilha os limites de trabalhos pesados: dois por usuário e dez no ambiente, incluindo exportações. Esses limites não representam homologação da importação em escala de produção.

`GET /imports` lista os trabalhos do usuário; uma chave de integração acessa apenas seus próprios trabalhos. `GET /imports/{id}?offset=0&limit=25` entrega `job`, `results`, `total_results` e `complete`. A página mostra resultado individual, ID da entidade e erro seguro, sem copiar o conteúdo recebido para logs ou respostas de progresso. As permissões de enriquecimento também autorizam acompanhar o próprio trabalho, sem exigir escopo de consulta cadastral.

Estados: `pending`, `processing`, `completed`, `completed_with_errors`, `failed` ou `cancelled`. Contadores: `total`, `processed`, `succeeded`, `failed_count` e `progress_percent`. Uma entrada com erro não impede as demais; a transação da entrada inválida é desfeita, e o resultado do erro fica registrado. O progresso em 100% significa que todas foram processadas; confira `failed_count` para saber se houve rejeições.

Cadastro, resultado, checkpoint e evento da entrada são gravados juntos. Uma execução interrompida com estado `pending/processing` é retomada na inicialização, a partir da próxima entrada; não repete contribuições já confirmadas. O trabalhador reavalia usuário ativo, OTP, permissão, chave, expiração e origem a cada entrada. Revogar acesso interrompe o processamento restante.

`POST /imports/{id}/cancel` interrompe entre entradas sem desfazer registros já incorporados. Para trabalho `failed/cancelled` com entradas restantes, `POST /imports/{id}/resume`, corpo `{}` e chave idempotente, retoma o mesmo checkpoint e conserva o histórico da interrupção. A autorização da chave original continua valendo; a retomada não troca o principal nem altera as entradas. Checkpoint inconsistente exige revisão. Entradas já rejeitadas permanecem no resultado e não são reenviadas automaticamente.

As entradas e os resultados são preservados no armazenamento local do trabalho para revisão/reconciliação. Transporte em fluxo, armazenamento temporário dimensionado, mapeamento dos sistemas reais e integração PostgreSQL/filas definitivas continuam etapas próprias da implantação.

## Campos adicionais definidos pelo administrador

`POST /admin/fields` aceita uma definição tipada. O ID é estável e pode ser informado pelo administrador:

```json
{
  "id": "preferencia_contato",
  "name": "Preferência de contato",
  "type": "enum",
  "scope": "both",
  "multiple": false,
  "options": ["email", "telefone"]
}
```

Tipos: `text`, `integer`, `decimal`, `boolean`, `date` (YYYY-MM-DD), `enum`, `url` (http/https) e `reference` (`{"entity_type":"person","id":"ID_EXISTENTE"}`). Escopo: `person`, `company` ou `both`. A definição retorna `version`, `active`, `definition_history` e `search_state`; pendente não significa indexação pronta.

Para agregar o valor, use um item `{"kind":"custom","value":{"field_id":"preferencia_contato","value":"email"}}` no enriquecimento, com fonte/datas/flags como nos demais itens. `false`, `0` e `null` são distintos; valor desconhecido exige `value: null` explícito. Decimal também aceita texto decimal exato, como `"1234567890.123456789"`, preservando a precisão. Não há conversão automática entre texto, booleano e inteiro.

`multiple: false` orienta o uso de um valor principal; observações com outros valores são alternativas preservadas com flags independentes. A restrição nunca apaga evidências antigas. Um `field_id` desconhecido é preservado com `classification_state: pending`, aviso e versão de definição null. Uma definição conhecida com valor incompatível rejeita o enriquecimento inteiro, sem escrita parcial.

`PATCH /admin/fields/{id}`, com `If-Match: versão`, altera `name` e/ou `active`. Renomear ou desativar cria uma nova versão; dados existentes e suas confirmações permanecem acessíveis. Cada observação mantém a versão de definição usada na recepção. Alterar tipo, escopo, opções ou multiplicidade é incompatível e retorna 409; exige uma nova definição e migração explícita. Campo inativo recusa novos valores, mas permite consultar e validar os valores anteriores.

## Vínculos, pesquisas e acesso

- `GET /people/{id}/relationships` e equivalente empresarial: vínculos informados, recebidos e identificação pendente. Relação inversa referencia o mesmo evento, sem fabricar origem.
- `POST/GET /saved-searches`: filtros e ordenação guardados por usuário; `PATCH /saved-searches/{id}` arquiva preservando os critérios.
- `POST /admin/invitations`: somente admin; convite de uso único com 24 horas. `/auth/activate` define senha e retorna desafio para OTP; não libera dados antes de completar o fator.
- `/auth/recovery`: senha já validada no desafio + código de recuperação; revoga acessos e exige novo cadastro OTP.

## Preservação na entrada JSON

A API retorna 422 antes de gravar se um decimal JSON sofreria arredondamento, overflow ou underflow. Use texto exato em um campo compatível (`decimal` aceita esse formato) quando todos os dígitos forem relevantes. Inteiros JSON usam a precisão inteira do servidor, sujeitos ao limite de tamanho da entrada; documentos e identificadores permanecem sempre textuais. O painel possui editor de inteiros exatos e preserva esses números ao consultar/enviar dados.

Chaves repetidas no mesmo objeto JSON, inclusive com nomes escritos usando escapes equivalentes, também retornam 422: não existe sobrescrita silenciosa pelo último valor. NaN/Infinity são rejeitados. Erros não repetem o dado recebido na mensagem; a chave idempotente não é consumida por essas rejeições, permitindo corrigir o formato e reenviar.

## Limites e ambiente

`BIGBASE_REDIS_URL` habilita limites compartilhados, com tempo do Redis e operação atômica. Sem essa variável, o limite é local e só serve ao processo único de desenvolvimento. Redis indisponível retorna 503; cota excedida retorna 429 com Retry-After. Login é limitado por conta e IP. Configurar Redis `noeviction`, rede privada/TLS e autenticação é obrigatório para a etapa de implantação; o ensaio local não valida failover.

Referências de implementação: [cliente Python oficial](https://redis.io/docs/latest/develop/clients/redis-py/) e [execução atômica de scripts](https://redis.io/docs/latest/develop/programmability/eval-intro/).


## Ordenação múltipla local

Pesquisa, pesquisas salvas e consulta em massa aceitam `sorts`, por exemplo:

```json
{"sorts":[{"field":"city","direction":"asc","mode":"min"},{"field":"birth_date","direction":"desc"}],"include_invalid":false}
```

O retorno `applied_sort` informa modos e desempate efetivos. Não misture `sorts` com `sort`/`direction`. O catálogo de ordenação está em `/api/v1/search/catalog`, versão 2; códigos postais permanecem textuais. As regras de ausentes, contratos legados e manifesto XLSX estão em [ORDENACAO-LOCAL.md](ORDENACAO-LOCAL.md). A ativação do backend novo em 18765 está pendente de reinício administrativo; o contrato foi validado no fixture sintético 18767.
