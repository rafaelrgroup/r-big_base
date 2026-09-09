# Enriquecimento HTTP canônico sintético

Contrato `canonical-http-fields-2026-09-08.1`. O endpoint `POST /api/v1/canonical/{people|companies}/enrich` grava uma entidade por transação no PostgreSQL 18 sintético. A ativação requer `CanonicalReads(..., writes_enabled=True)` e as mesmas verificações de socket privado, porta 18769, banco `bigbase_test`, ambiente e UUID de implantação das leituras. O padrão é desabilitado. Nenhum schema é criado pela API; o fixture do navegador habilita somente seu schema descartável.

Autenticação, catálogos de fontes e limites continuam no SQLite local de processo único. O cadastro escrito, a operação auditável, suas observações e a outbox ficam exclusivamente no PostgreSQL. Isso não constitui implantação do banco de produção ou migração real. Não há cópia dupla dos cadastros.

## Contrato e concorrência

Exemplo exclusivamente fictício, com cabeçalho `Idempotency-Key` próprio da operação:

```json
{
  "source_id": "manual",
  "source_record_id": "pessoa-ficticia-1",
  "expected_version": 0,
  "items": [{
    "kind": "phone",
    "key": "contato-1",
    "fields": [{
      "path": "number",
      "value": "telefone fictício",
      "observed_at": "2026-01-01T00:00:00Z",
      "flags": {"is_whatsapp": {"value": null}}
    }]
  }]
}
```

`expected_version=0` exige identidade ainda inexistente. Uma identidade existente exige a versão lida; `entity_id` opcional deve corresponder à identidade de origem/documento fornecida. A versão e a atribuição de identidade são verificadas dentro da mesma transação, sob os mesmos locks ordenados usados pela importação. Duas alterações concorrentes na mesma versão não sobrescrevem uma à outra. Não há exclusão cadastral neste contrato.

`source_id` deve constar no catálogo ativo e, para API keys, nas fontes autorizadas da chave. O ator vem da sessão/chave autenticada; o identificador público da chave também fica nos metadados. Origem e ator não são aceitos dentro dos campos. A permissão `enrich` é obrigatória; qualquer flag requer também `validate`. CSRF, OTP concluído, revogação, usuário ativo, escopos e limites são reavaliados pela autenticação compartilhada.

A chave idempotente tem até 200 caracteres e escopo por usuário no namespace de enriquecimento canônico. Corpo, coleção, chave API e precondições participam da comparação. Repetição idêntica recupera o recibo da operação imutável, inclusive após reinício ou outras escritas; conteúdo diferente retorna 409. A resposta contém `id`, `operation_id`, `record_version`, `observations_created`, `replayed`, ambiente sintético e `production_connected=false`. A versão é a da operação confirmada, não uma leitura posterior. Após timeout/503, repetir com a mesma chave e o mesmo corpo; não presumir cancelamento.

O recibo, a auditoria por campo e a outbox derivam da mesma transação PostgreSQL. Não se exige um segundo commit SQLite após a escrita, evitando que uma falha nessa segunda gravação torne o recibo inconsistente. Erros de banco/destino são sanitizados. Respostas usam `no-store`.

## Campos, estrutura e confirmações

Até 100 itens, 100 campos por item, 1.000 átomos no total e corpo de 2 MiB. Grupos: identificação, documento, telefone, email, endereço, username, atividade e campo adicional. Relações aguardam contrato próprio. `kind` e `key` identificam um item da entidade; referências distintas permitem múltiplos contatos/usernames. Este recorte não deduplica contatos automaticamente por normalização. Repetir o mesmo campo-alvo na operação é recusado.

Cada campo inclui `path`, `value` e, opcionalmente, `source_updated_at`, `observed_at`, `reason` e `flags`. Datas explícitas exigem fuso horário; datas ausentes não são preenchidas com a recepção. Precedência por campo/flag preserva eventos atrasados, sem data e futuros no histórico. Campos ausentes e arrays vazios não apagam os campos já existentes.

Null, false, zero, texto vazio, objetos/listas vazios, inteiros e decimais exatos permanecem distintos. Esta rota lê números com Decimal diretamente dos bytes JSON e responde sem conversão para float. As rotas locais mantêm sua política anterior. Duplicatas de chaves JSON e números não finitos são recusados.

Valores estruturados são decompostos recursivamente até 24 níveis. Caminho JSON Pointer da entrada, posições, tipos e comprimentos dos containers são preservados; nenhum subobjeto completo é copiado como átomo. Nomes de campo não aceitam `/` ou `~`, reservados à composição sem ambiguidades dos caminhos filhos. Conteúdo desconhecido permanece acessível, sem alegação de que está indexado. O novo recorte conserva os valores dos contatos sem inferir normalização, sintaxe, validade, titularidade ou WhatsApp.

O documento opcional de identidade `{type,country,value}` exige CPF/CNPJ brasileiro válido e compatível com pessoa/empresa. A normalização usa o validador existente; entrada textual original e regra ficam nas observações. Documentos incompatíveis são recusados integralmente. Conflitos entre aliases/documentos retornam 409 e exigem resolução explícita, sem fusão automática ou remoção do histórico. Um documento cadastrado como simples campo não cria por si só uma identidade documental.

Flags aceitam somente true/false/null e as dimensões existentes. Cada flag pode ter suas próprias datas, `checked_at`, `expires_at`, método e referência; expiração exige verificação anterior. `confirmed_value` opcional explicita o valor ao qual a evidência pertence; o padrão é o valor escalar recebido. Uma flag do valor antigo não confirma o valor novo. Estruturas com flags são recusadas: a confirmação deve identificar um campo escalar. Metadados de vencimento são preservados. Validação dedicada e apresentação de revalidação foram acrescentadas no contrato [VALIDACOES-CANONICAS-HTTP.md](VALIDACOES-CANONICAS-HTTP.md).

## Painel e limites da entrega

A consulta canônica mostra o editor apenas quando o serviço habilita escrita sintética. É possível criar pessoa/empresa por identidade de origem, acrescentar múltiplos itens/campos, escolher tipo de valor exato e registrar confirmações. Após a escrita, a ficha e o histórico são reabertos em novo corte. Uma tentativa sem resposta mantém a chave idempotente enquanto corpo/precondições não mudarem. Troca de contexto descarta respostas visuais antigas.

Permanecem pendentes: integração com o contrato local de enriquecimento/importação e seus normalizadores/catálogos; PATCH dedicado de valores por item/campo; deduplicação automática de contatos; atualização do registro de identidade documental; relações; exportação canônica; autenticação/catálogos PostgreSQL; ES real e ordenação definitiva; implantação, piloto, reconciliação, migração, carga e disponibilidade. Nenhum teste sintético autoriza conexão dos serviços atuais à produção.

Evidência da rodada: `validacao-escritas-canonicas.json`. Os resultados e hashes devem ser lidos nesse arquivo, sem atribuir a este código as contagens históricas de outras rodadas.
