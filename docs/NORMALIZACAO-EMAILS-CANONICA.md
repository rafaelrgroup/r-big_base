# Normalização optativa de emails no enriquecimento canônico

Recorte sintético de pessoa/empresa em `POST /api/v1/canonical/{people|companies}/enrich`. Não habilita produção nem altera implicitamente o PATCH escalar literal. Os contratos de autenticação, origem ativa autorizada, versão e Idempotency-Key do enriquecimento permanecem obrigatórios.

Cada item `kind: "email"` pode optar por `email_normalization: {"contract": "canonical-email-2026-09-09.1"}`. A operação precisa conter o campo escalar textual `email`, com até 2.048 caracteres. Contrato desconhecido, parâmetros extras, grupo incorreto, ausência do campo ou valor não textual retornam 422, sem escrita parcial. Sem a opção, continua o contrato literal, inclusive null, false, zero, objetos e listas. Múltiplos emails usam chaves de item distintas; email igual não funde identidades.

```json
{
  "source_id": "manual",
  "source_record_id": "empresa-ficticia-email-1",
  "expected_version": 0,
  "items": [{
    "kind": "email",
    "key": "email-comercial",
    "email_normalization": {"contract": "canonical-email-2026-09-09.1"},
    "fields": [{
      "path": "email",
      "value": "First.Last+Tag@EXAMPLE.INVALID",
      "observed_at": "2026-02-01T00:00:00Z"
    }]
  }]
}
```

A regra existente `normalize('email', ...)` produz `First.Last+Tag@example.invalid`. Preserva a parte local, inclusive maiúsculas, acentos, pontos e `+`; não aplica regras particulares de provedores, remoção de espaços, IDNA ou verificação de DNS/caixa postal. A decisão sintática é o reconhecimento básico já existente, não validação completa de RFC nem prova de entregabilidade, validade cadastral ou titularidade. Formatos não reconhecidos recebem `status: pending` e decisão `review`; entrada e resultado permanecem acessíveis no histórico, sem substituir um valor resolvido. Um formato reconhecido recebe `normalized`, sem gerar flags.

A observação guarda entrada, saída, regra `email-domain-lowercase`, contrato, versão do normalizador, decisão, parte local, domínio recebido/normalizado e resultado sintático. `email_input_dates` conserva os literais e a presença/ausência das datas do campo; as colunas temporais seguem a comparação em UTC. Componentes derivados ficam nos metadados da própria observação: não criam campos nem atualizam origem/datas de campos ausentes. Campos adicionais enviados no item continuam literais e independentes. Um pedido pode combinar contratos de telefone e email, com provas próprias por observação.

Confirmações exigem permissão `validate` além de `enrich`. Por padrão, referem-se ao valor original. Se a normalização muda o valor, a confirmação original fica no histórico com `value_mismatch`, sem confirmar automaticamente a saída. Evidência explícita da saída pode usar `confirmed_value` exato. Null, false e true permanecem distintos. Eventos atrasados, futuros ou sem data não substituem indevidamente o estado atual. Replay não acrescenta observações/outbox nem renova confirmações; reutilização da chave com conteúdo diferente e versões concorrentes geram conflito.

No painel de ensaio, selecionar grupo Email e campo `email` mostra “Normalização do email”. A opção é inicialmente literal; a transformação é explícita. A ficha e o histórico exibem entrada/saída, parte local, domínios, decisão e versão. O PATCH escalar pode registrar uma revisão/reversão literal, preservando eventos e flags anteriores.

Validação própria e hashes: `validacao-emails-canonicos.json`. Testes usam somente PostgreSQL privado e contas sintéticas na Linode. Integração dos demais normalizadores/catálogos, importação/exportação canônica, implantação definitiva, piloto, migração, busca real e carga permanecem pendentes.
