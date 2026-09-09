# Campos adicionais canônicos com definição explícita

Recorte sintético de pessoa/empresa em `POST /api/v1/canonical/{people|companies}/enrich`. Escrita desabilitada por padrão; o catálogo administrativo e a autenticação continuam no SQLite local de processo único. O PostgreSQL privado recebe observações e outbox, sem dados reais. Este bloco não implanta o catálogo definitivo nem declara indexação pronta.

Um item `custom` pode aderir ao contrato:

```json
{
  "kind": "custom",
  "key": "contador-alternativa-1",
  "custom_field": {
    "contract": "canonical-custom-field-2026-09-09.1",
    "field_id": "contador-sintetico",
    "version": 1
  },
  "fields": [{"path": "value", "value": 0, "observed_at": "2026-01-01T00:00:00Z"}]
}
```

O corpo externo mantém origem, referência de origem, versão esperada e identidade opcional. Origem e permissões são autorizadas pelo servidor. Chave idempotente obrigatória; alteração concorrente retorna conflito e falha em qualquer item reverte entidade, observações, identidade e outbox.

## Tipos e versões

A validação reutiliza o catálogo existente: texto, inteiro, decimal, booleano, data, enum, URL e referência. Nenhuma conversão automática; null é explícito. Decimais numéricos usam Decimal do transporte até o armazenamento, e a forma textual exata já admitida pelo catálogo também é preservada. Booleano não é inteiro. Referências são objetos com `entity_type` e `id`, resolvidos apenas nas entidades PostgreSQL da mesma instância sintética; entidades do SQLite não satisfazem esse vínculo. Seus componentes são observações recursivas, preservando estrutura e definição.

Definição conhecida exige versão atual explícita, estado ativo e escopo compatível. Renomear/desativar/reativar preserva versões anteriores; mudança incompatível de tipo/escopo/opções requer outra definição e migração explícita conforme o catálogo existente. Não existe migração automática nesta rota.

Definição desconhecida exige `version: null`: qualquer conteúdo recebido, inclusive objetos, listas e vazios, fica preservado com classificação pendente. Uma classificação posterior exige envio explícito com a versão cadastrada; as evidências antigas não mudam. Cada referência de item recebe um valor, e referências distintas preservam alternativas mesmo para definição `multiple: false`, conforme a política existente `scalar_per_item_alternatives_preserved`.

## Rastreabilidade e repetição

Cada observação contém field_id, versão, cópia completa da definição sem seu histórico agregado, SHA256 dessa cópia, classificação, política de alternativas, datas literais recebidas e `canonical_search_state: pending`. Fonte, ator, entrada/saída, recepção e eventos atrasados seguem o contrato canônico. A cópia serve de prova independente de futuras alterações do catálogo. Confirmações enviadas junto ao valor e validações dedicadas guardam a definição do valor referenciado; evidências de outro valor não confirmam o atual.

A consulta de repetição confirmada precede a validação do catálogo mutável: uma resposta perdida continua recuperável depois de renomeação/desativação, mantendo autorização atual. Conteúdo diferente com a mesma chave retorna conflito. Definições são lidas no snapshot de autorização da requisição; uma alteração administrativa posterior vale para requisições seguintes. Não há transação distribuída de catálogo SQLite/PostgreSQL, e esta limitação deve ser removida na integração definitiva.

O item aderente permanece associado ao field_id. Escrita literal e PATCH escalar não podem contornar seu contrato; utilize enriquecimento com definição/versão explícitas. Um item livre antigo não é convertido implicitamente em campo tipado: use outra referência. Campos livres que não aderiram continuam preservados pelo contrato anterior. Reorganização de coleções recursivas permanece pendente; mudança de forma não apaga componentes antigos.

## Painel e limites

O formulário permite escolher a definição, conferir tipo/versão/escopo/estado, recarregar catálogo ou informar identificador pendente. O histórico mostra definição e versão preservadas, classificação e indexação pendente. Erros de tipo, escopo e versão orientam correção sem aplicar parcialmente o lote. A mesma chave recupera a resposta perdida.

Permanecem pendentes catálogo canônico administrativo transacional, migrações de definição, indexação/pesquisa/ordenação, importadores/exportações canônicas, reorganização recursiva, identidade/fusão/desfusão, implantação definitiva, piloto/reconciliação, migração e testes de carga/failover. Nenhum registro real migrado; não é entrega integral.
