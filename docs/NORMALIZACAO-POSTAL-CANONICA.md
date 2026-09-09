# Normalização postal canônica optativa

O enriquecimento de pessoa/empresa em `/api/v1/canonical/{people|companies}/enrich` aceita `postal_normalization: {"contract": "canonical-postal-2026-09-09.1"}` em itens `address`. O contrato permanece restrito ao PostgreSQL sintético explicitamente configurado; escrita desabilitada por padrão. O SQLite continua adaptador local de processo único.

Exemplo sintético de item:

```json
{
  "kind": "address",
  "key": "endereco-ficticio-1",
  "postal_normalization": {"contract": "canonical-postal-2026-09-09.1"},
  "fields": [
    {"path": "postal_code", "value": "00123-456", "observed_at": "2026-02-01T00:00:00Z"},
    {"path": "country", "value": "BR", "observed_at": "2026-01-01T00:00:00Z"}
  ]
}
```

O campo `postal_code` é obrigatório nesta operação e deve ser texto de até 2048 caracteres. País, quando enviado, deve ser texto com duas letras ASCII maiúsculas; códigos sem validador cadastrado permanecem para revisão. País ausente não assume Brasil e não reutiliza silenciosamente o país de observações anteriores. País null, vazio, não textual ou fora desse formato gera 422; para preservar essas entradas literalmente, omitir o contrato optativo. Valores não textuais de CEP também geram 422 explícito, sem escrita parcial.

Com `BR` explícito, reutiliza a remoção de espaços/hífens do normalizador existente. Só adota a saída se contiver exatamente oito dígitos ASCII; zeros são preservados. Comprimento incorreto, letras, outros separadores e dígitos não ASCII mantêm o valor literal com estado pendente. O candidato fica na auditoria, inclusive quando não adotado. Oito zeros satisfazem apenas o formato: não comprovam existência do CEP. Sem país ou com outro país, o código permanece textual literal e pendente, com validade sintática desconhecida; não há conversão numérica nem validador postal internacional neste bloco.

A observação do CEP registra entrada, saída, caminho/posição da fonte, contrato, regra, versão do normalizador, decisão, candidato, país da operação e datas originais. Metadados de contexto incluem valor/datas do país explicitamente recebido. Somente a observação de `postal_code` é transformada. Rua, cidade, número, complemento, país e demais campos enviados mantêm valores, datas, flags e origem próprios; componentes ausentes não recebem novas observações. Os componentes derivados ficam na auditoria do código postal.

Sintaxe não confirma existência de endereço, residência, titularidade ou validade real. Flags enviadas sem `confirmed_value` referem-se ao código original; se o valor foi transformado, não se tornam aplicáveis ao código novo automaticamente. O contrato de confirmação explícita e o PATCH dedicado permitem vincular evidência ao valor correto. O PATCH escalar mantém o contrato literal e permite reversão por nova observação, conservando o histórico e as confirmações antigas.

Referências distintas de item mantêm múltiplos endereços, inclusive com CEP igual. Não há fusão por CEP/endereço compartilhado. Origem ativa autorizada, permissão `enrich`, permissão `validate` para flags, identidade, versão, idempotência e transação/outbox são os mesmos do enriquecimento existente. Replay não renova datas; eventos atrasados, futuros ou pendentes não substituem indevidamente um valor resolvido. Falhas revertem toda a operação.

O painel oferece a opção no campo `postal_code` do grupo Endereço. País preenchido no controle auxiliar é uma observação sem data conhecida; datas próprias podem ser informadas como observação separada no mesmo item, deixando o controle auxiliar vazio. Campos e histórico exibem decisão, regra/versão, país informado e candidato. Confirmações no formulário continuam vinculadas ao valor original.

Evidência desta versão: `validacao-postal-canonica.json`; testes `test_canonical_postal.py`, regressões canônicas e fluxo de pessoa/empresa em `frontend/browser-test.mjs`. A evidência registra comandos, contagens, hashes e tentativas; não substitui a suíte completa ou homologação de produção.

Permanecem pendentes outros normalizadores/catálogos canônicos, deduplicação e revisão operacional, importadores/exportações, banco definitivo, Elasticsearch real, piloto/reconciliação, migração, carga e disponibilidade. Nenhum dado real foi migrado.
