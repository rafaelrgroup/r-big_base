# Normalização de telefones no enriquecimento canônico sintético

Contrato optativo `canonical-phone-2026-09-09.1`, em `POST /api/v1/canonical/{people|companies}/enrich`. Reutiliza `domain.normalize('phone', ...)` e o catálogo existente em `phone_rules.py`; a versão completa inclui regras e biblioteca. PostgreSQL privado com dados fictícios; autenticação e aplicação principal continuam no SQLite local, de processo único. Escrita canônica desabilitada por padrão.

## Pedido e contexto por item

```json
{
  "source_id": "manual",
  "source_record_id": "exemplo-sintetico",
  "expected_version": 0,
  "items": [{
    "kind": "phone",
    "key": "telefone-1",
    "phone_normalization": {"contract": "canonical-phone-2026-09-09.1"},
    "fields": [
      {"path": "number", "value": "88765432", "observed_at": "2026-01-01T00:00:00Z"},
      {"path": "country", "value": "BR"},
      {"path": "ddd", "value": "11"}
    ]
  }]
}
```

Exige `Idempotency-Key`, permissão `enrich`, origem ativa autorizada, CSRF na sessão e a versão apropriada. Flags exigem também `validate`. Mantém os limites de 100 itens, 100 campos por item, 1.000 átomos e 2 MiB. O número e os componentes usados na normalização são textos de até 2.048 caracteres; null, false, números e estruturas nesses componentes são rejeitados com 422 sem escrita. O contrato literal sem a opção continua preservando esses valores explicitamente, sem coerção. Versões desconhecidas, opções adicionais ou opção em outro grupo são rejeitadas.

Cada item optante precisa enviar `number` nessa operação. `country`, `ddd` e `extension` são contexto apenas quando enviados no mesmo item. Não se buscam componentes antigos do cadastro nem de outro telefone para completar silenciosamente a entrada. Metadados `context_fields` preservam as datas informadas desse contexto; não afirmam que o contexto seja o estado atual consolidado. Componentes podem ter precedências diferentes.

A regra existente distingue país explícito, prefixo internacional e padrão BR da aplicação. Acrescentar nono dígito exige a evidência histórica já aprovada e o contexto suficiente; DDD ausente, prefixo ambíguo, caracteres desconhecidos ou contexto conflitante permanecem em revisão. Números atuais, fixos, especiais e internacionais seguem suas regras existentes. O recorte integra essas regras, sem alterar o catálogo histórico.

Referências distintas conservam múltiplos telefones, inclusive quando normalizados para o mesmo número. Igualdade de telefone não funde pessoas/empresas, não cria identidade documental nem comprova titularidade. Deduplicação canônica de contatos entre referências/fontes continua pendente.

## Preservação, confirmações e reversão

Só o valor da observação de `number` recebe a saída normalizada. `input_value`, caminho, origem, ator, datas e entrada completa dos componentes permanecem recuperáveis. Seus metadados incluem:

- `phone_contract`, `normalization` e `phone_normalization`: contrato, versão completa, entrada, decisão, regra, fontes técnicas, candidatos e mudança de dígitos;
- `phone_output`: número canônico, classificação técnica e componentes derivados, incluindo ramal quando reconhecido;
- `normalization_notes` e `context_fields`: explicação e contexto efetivamente enviado.

País, DDD, ramal e classificação derivados ficam vinculados à observação do número. Não criam observações com datas/origens inventadas para campos ausentes. Os componentes enviados como campos continuam literais e mantêm datas, flags e histórico próprios. A classificação técnica e a verificação sintática não são confirmações cadastrais.

Uma decisão de revisão registra `pending` com entrada original e candidatos. Não substitui um número anteriormente resolvido; fica acessível no histórico. Eventos atrasados, futuros ou sem data contra valor datado obedecem à precedência existente, sem modificar o estado atual. A versão cresce pela operação registrada mesmo quando a observação não prevalece.

Nenhuma flag é produzida pela normalização. Confirmações enviadas sem vínculo explícito referem-se ao literal recebido; se a saída difere, ficam no histórico com `value_mismatch`, inclusive quando a mudança é apenas formatação. A API permite `confirmed_value` explícito para quem possui evidência do valor normalizado. O painel de validação dedicada também permite confirmar a observação normalizada. True, false e null permanecem distintos; datas/vencimento de cada flag continuam independentes.

O PATCH escalar mantém seu contrato literal. Para reverter uma transformação, registrar uma nova observação com a entrada original, motivo e data apropriada; nenhuma observação histórica é apagada. Flags do número substituído permanecem vinculadas àquele valor. Uma nova normalização não reinsere nono dígito em número já normalizado. Replay de corpo/chave recupera o recibo imutável e não renova datas, versões, observações ou outbox; corpo alterado ou versão concorrente retornam 409. Revogação e autorização são reavaliadas no replay.

## Painel e evidência

No enriquecimento, escolher grupo Telefone, campo `number` e “Normalizar com histórico”. País, DDD e ramal opcionais produzem campos sem data conhecida; para datas próprias, deixá-los vazios nesses atalhos e acrescentar observações separadas no mesmo item. A ficha e o histórico mostram entrada, saída, decisão, regra, versão e classificação. “Revisão necessária” identifica os casos não resolvidos. As confirmações do formulário se referem ao número original.

Resultados, comandos e hashes desta rodada: [validacao-telefones-canonicos.json](validacao-telefones-canonicos.json). A evidência declara o conjunto proporcional executado, sem atribuir a esta versão a suíte completa de rodadas anteriores.

Continuam obrigatórios: normalização dos demais grupos, integração dos catálogos, deduplicação e reorganização de coleções, relações, importadores/exportações canônicas, autenticação PostgreSQL, Elasticsearch real, implantação definitiva, piloto, reconciliação, migração, carga e disponibilidade. Nenhum dado real foi migrado; o recorte não é entrega integral.
