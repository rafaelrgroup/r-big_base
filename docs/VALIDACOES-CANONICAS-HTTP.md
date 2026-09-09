# Validação canônica dedicada de flags

Contrato `canonical-http-flags-2026-09-09.1`. Acrescenta evidências de confirmação/invalidação de campos escalares existentes no PostgreSQL sintético. A operação não cria observação de valor, item, identidade de origem nem trabalho de migração. Escrita desabilitada por padrão, com as verificações de destino de [ESCRITAS-CANONICAS-HTTP.md](ESCRITAS-CANONICAS-HTTP.md). Autenticação e fontes continuam no SQLite local, de processo único; os cadastros e a outbox ficam no PostgreSQL. Nenhuma produção foi conectada.

## Requisição e associação ao valor

`PATCH /api/v1/canonical/{people|companies}/{owner_id}/items/{item_id}/flags`, com `Idempotency-Key` até 200 caracteres:

```json
{
  "source_id": "manual",
  "expected_version": 3,
  "field_path": "number",
  "value_observation_id": "00000000-0000-0000-0000-000000000001",
  "flags": {
    "valid": {
      "value": false,
      "reason": "Invalidação fictícia do ensaio",
      "observed_at": "2026-03-01T00:00:00Z",
      "checked_at": "2026-03-01T00:00:00Z",
      "expires_at": "2026-04-01T00:00:00Z",
      "method": "Ensaio sintético",
      "reference": "evidencia-ficticia-1"
    }
  }
}
```

O UUID do exemplo é ilustrativo; a requisição real de ensaio precisa apontar para uma observação existente de dimensão `value`, obtida nos campos ou no histórico. Entidade, item, caminho e dimensão são conferidos dentro da transação. O servidor recupera o literal normalizado exato dessa observação e registra sua associação em cada flag. Não aceita substituição do valor pelo corpo da validação. Null, false, zero e decimais permanecem distintos, sem precisar reenviar números pelo navegador.

É permitido selecionar uma observação histórica, inclusive atrasada. Sua evidência permanece no histórico; se o valor divergir do valor atual, a flag fica não aplicada com `value_mismatch`. Uma observação de outro item, campo, entidade, coleção ou dimensão é recusada integralmente. Containers vazios também são recusados: confirme um componente escalar. Caminhos recursivos já existentes são aceitos exatamente como devolvidos pela leitura.

`flags` exige ao menos uma dimensão conhecida: `valid`, `is_whatsapp`, `ownership_confirmed`, `deliverable` ou `residence_confirmed`. Cada resultado exige true, false ou null explícito. Flags ausentes não mudam. `valid=false` exige motivo não vazio; reativação usa nova evidência `valid=true`, preservando a anterior. Cada flag possui suas próprias datas, motivo, método e referência. Não há inferência de titularidade, WhatsApp ou validade.

Datas exigem fuso horário. `checked_at` é usado como `observed_at` somente quando este último está ausente; null explícito é preservado. Datas da fonte e da observação não são herdadas de outra flag ou do valor. Vencimento exige resultado conhecido e data de verificação anterior. Eventos atrasados, futuros ou sem data seguem a precedência existente e são preservados, mesmo quando não alteram o estado aplicável.

## Autorização, transação e repetição

Exige `validate`, usuário ativo e OTP concluído. Sessões exigem CSRF; API keys exigem escopo e origem ativa autorizada. Não exige `enrich` para apenas validar. Ator e ID público da chave vêm da autenticação, nunca do corpo. Limites e revogação são reavaliados inclusive em repetições. Corpo limitado a 2 MiB; motivo a 2.000 caracteres, método a 160 e referência a 1.000.

A versão positiva da entidade é obrigatória. A transação bloqueia a entidade, confere a referência, grava operação, flags e estado aplicável, incrementa versões de entidade/item uma vez e acrescenta a outbox. Nenhuma confirmação depende de um segundo commit de auditoria no SQLite. Falha após inserir uma observação desfaz toda a operação.

Idempotência possui namespace próprio por usuário. Corpo original, entidade, item, coleção e ID público da chave integram o hash de comparação. Repetição idêntica recupera o recibo imutável antes de comparar a versão atual, inclusive após outras escritas e reinício. Corpo diferente retorna `409 CANONICAL_IDEMPOTENCY_CONFLICT`; versão concorrente retorna `409 CANONICAL_VERSION_CONFLICT`. Resposta inclui `id`, `operation_id`, `record_version`, `observations_created`, `replayed`, ambiente sintético e `production_connected=false`, com `no-store`. Uma resposta perdida pode ser repetida com o mesmo corpo e chave.

Referência/contrato inválido retorna 422; serviço desabilitado ou destino indisponível/divergente retorna 503 sanitizado. Autorização mantém 401/403 e origem desconhecida mantém 422. Nenhuma exclusão de histórico ocorre para corrigir, invalidar ou reativar.

## Vencimento e painel

A página de campos acrescenta `checked_at`, `expires_at`, `stale` e `freshness_evaluated_at` às flags. O corte cadastral permanece fixo pelo cursor; a atualidade é avaliada no momento da leitura e não cria eventos, altera a data de verificação ou renova a evidência por replay. `stale=true` sinaliza vencimento e preserva o resultado conhecido, inclusive false. `applicable` continua informando, independentemente, se a evidência pertence ao valor corrente do corte.

Metadados de expiração não interpretáveis preservados por adaptadores antigos não impedem a leitura: retornam `stale=null` e permanecem no histórico, sinalizados como pendentes de classificação. A apresentação não afirma que esse vencimento está válido.

O painel oferece “Validar este valor” nos campos escalares e no histórico quando a escrita e a permissão estão habilitadas. Mostra o valor escolhido e a versão; permite resultado, origem, motivo e datas independentes, método e referência. Após confirmação reabre ficha/campos em novo corte. Repetir após erro de transporte conserva a chave enquanto corpo e contexto forem iguais. Mudança de contexto descarta respostas antigas. A confirmação vencida recebe o texto “Confirmação desatualizada”, conservando seu resultado.

## Evidências e restante

Evidência desta revisão: [validacao-flags-canonicas.json](validacao-flags-canonicas.json), com 1.005 testes backend, build, 14/9/6 verificações auxiliares e 25 grupos de navegador aprovados. A falha inicial do seletor, sua correção e o novo build/navegador estão registrados separadamente; os hashes do backend permaneceram iguais. A suíte inclui associação exata, flags históricas, datas independentes, resultados true/false/null, motivo de invalidação, replay, concorrência, rollback, autorização, fontes e vencimento. O navegador usa seu próprio armazenamento de autenticação e schema PostgreSQL descartável na porta 18767.

Permanecem obrigatórios: PATCH de valores por item/campo, integração de normalizadores/catálogos, relações, importadores/exportações canônicas, autenticação PostgreSQL, ES real, implantação definitiva, piloto, reconciliação, migração, carga e disponibilidade. Este bloco sintético não é entrega integral.
