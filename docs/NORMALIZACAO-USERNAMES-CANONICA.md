# Usernames e plataformas no enriquecimento canônico

`POST /api/v1/canonical/{people|companies}/enrich` aceita, por item `username`, o contrato optativo `username_normalization: {"contract": "canonical-username-2026-09-09.1"}`. A escrita continua desabilitada por padrão e o ensaio usa PostgreSQL privado com dados sintéticos. O serviço local SQLite continua identificado como adaptador de desenvolvimento, de processo único.

```json
{
  "kind": "username",
  "key": "conta-ficticia-instagram-1",
  "username_normalization": {"contract": "canonical-username-2026-09-09.1"},
  "fields": [
    {"path": "username", "value": "@Synthetic.Case", "observed_at": "2026-02-01T00:00:00Z"},
    {"path": "platform", "value": "Instagram", "observed_at": "2026-01-01T00:00:00Z"},
    {"path": "url", "value": "https://example.invalid/@Synthetic.Case"},
    {"path": "external_id", "value": "000000000000000001"}
  ]
}
```

O contrato exige `username` e `platform` explícitos na operação, textuais e com até 2048 caracteres. Plataforma vazia ou só com espaços gera 422. Username vazio permanece vazio; nenhum formato de conta é certificado. Valores não textuais nesses dois campos geram 422 atômico; o contrato literal existente continua disponível para preservá-los, incluindo null, false, zero e estruturas desconhecidas. A operação não busca silenciosamente a plataforma previamente armazenada quando ela está ausente.

Reutiliza `normalize('username', ...)` e sua versão existente: apenas a plataforma passa por `casefold`. Não remove espaços, não une aliases como X/Twitter e não cria regras específicas de provedor. A mesma regra de capitalização vale para plataformas personalizadas; isso não constitui catálogo ou verificação de existência de plataforma. Username mantém maiúsculas, @, pontos, espaços, Unicode e URLs apresentados como username. URL, ID externo e demais campos permanecem literais, inclusive tipos desconhecidos; nenhuma equivalência com telefone ou identificação de conta é inferida.

As observações de username e plataforma registram entrada, saída, regra, versão, decisão, contexto e datas originais de ambos os componentes. Cada observação continua com origem, ator, recepção e data efetiva próprios. A plataforma usa estado `normalized` e regra `platform-casefold`; username usa `unknown` e `username-preserve`, com decisão `literal_no_rule`. Verificação sintática permanece desconhecida. Campos ausentes não geram observações; URL/ID externo enviados mantêm suas próprias datas e origens. Eventos atrasados ou futuros e datas ausentes seguem a precedência canônica existente.

Referências de item distintas mantêm contas distintas, inclusive com o mesmo username em outra plataforma ou várias contas na mesma plataforma. Não existe deduplicação automática de contas neste contrato. O vínculo entre um item que aderiu ao contrato e sua plataforma é fixado pela primeira observação normalizada, inclusive quando ela é atrasada ou futura. Outra plataforma exige outra referência de item. Escrita literal e PATCH escalar não podem alterar esse vínculo. A adesão de um item legado exige que todo seu histórico de plataforma seja textual e corresponda por `casefold`; conflitos são rejeitados sem apagar histórico. Correção de plataforma com preservação/fusão explícita de itens permanece pendente.

Flags permanecem ligadas ao campo e valor correto. Uma confirmação de plataforma original com capitalização diferente não é transferida ao valor normalizado sem evidência explícita. Uma confirmação de username permanece associada àquele literal, e mudança de username não a torna aplicável ao novo valor. A proteção de plataforma impede transferir essas confirmações para outra conta por reutilização do item. Não há confirmação automática de titularidade, validade, WhatsApp ou telefone.

Origem ativa autorizada, permissões `enrich` e `validate` para flags, versão, Idempotency-Key e transação PostgreSQL/outbox seguem o contrato existente. Replay recupera o recibo sem criar observações ou renovar datas; conflito e erro de contexto revertem toda a escrita. PATCH de username, URL e ID externo permanece literal. PATCH de plataforma preserva a restrição do item que aderiu ao contrato.

O painel oferece “Padronizar plataforma com histórico” no campo `username`, grupo Username. O controle auxiliar de plataforma cria observação sem data conhecida; para informar data própria, usar outra observação `platform` no mesmo item e deixar o controle auxiliar vazio. Campos e histórico exibem decisão, plataforma, regra e versão. Referências distintas de item são necessárias para múltiplas contas.

Provas: `test_canonical_username.py`, regressões de armazenamento/leitura/enriquecimento/flags/edição/normalização e fluxo de pessoa/empresa em `frontend/browser-test.mjs`. Resultados exatos e hashes em `validacao-usernames-canonicos.json`. Testes sintéticos não homologam produção ou carga real.

Permanecem obrigatórios catálogos administráveis e sua indexação, identidade/fusão/desfusão, reorganização recursiva, vínculos, importadores/exportações canônicas, autenticação PostgreSQL, Elasticsearch real, implantação definitiva, piloto/reconciliação, migração, carga e disponibilidade. Nenhum registro real foi migrado.
