# Edição escalar canônica HTTP sintética

Contrato `canonical-http-scalar-2026-09-09.1`. O recorte acrescenta uma observação de valor a um campo existente, em PostgreSQL sintético explicitamente configurado. Autenticação e serviço principal permanecem no adaptador SQLite local, de processo único. Escrita canônica desabilitada por padrão; nenhum registro real migrado.

## Rota e autorização

`PATCH /api/v1/canonical/{people|companies}/{owner_id}/items/{item_id}/value`

Exige permissão `enrich`, origem ativa autorizada à chave quando usada, proteção CSRF de sessão, `Idempotency-Key` de até 200 caracteres e versão positiva da entidade em `expected_version`. Recepção, ator, chave de API e operação são determinados pelo servidor. A configuração/verificação do destino sintético é a mesma das [leituras](LEITURAS-CANONICAS-HTTP.md) e do [enriquecimento](ESCRITAS-CANONICAS-HTTP.md).

Exemplo exclusivamente sintético, para um campo previamente existente:

```json
{
  "source_id": "manual",
  "expected_version": 3,
  "field_path": "amount",
  "value": 12345678901234567890.12345678901234567890,
  "observed_at": "2026-05-01T00:00:00Z",
  "reason": "Correção sintética direcionada"
}
```

Obrigatórios: `source_id`, `expected_version`, `field_path` e `value`. Opcionais: `observed_at`, `source_updated_at` e `reason` não vazio de até 2.000 caracteres. Parâmetros desconhecidos são rejeitados. Não enviar identidade externa, documento de identidade, flags, ator ou metadados internos. Validar uma flag usa a [rota dedicada de flags](VALIDACOES-CANONICAS-HTTP.md).

O valor deve ser um literal JSON escalar: texto, inteiro, decimal finito, booleano ou null. String vazia, null, false e zero permanecem distintos. O middleware admite números exatos nesta rota, o parser usa Decimal e a persistência/serialização preserva o valor sem arredondamento binário. JSON duplicado, não finito ou corpo acima de 2 MiB são rejeitados. A ausência de `value` é erro; a ausência de outros campos cadastrais não os altera.

O alvo precisa existir no estado de valores do item, pertencer à entidade/coleção e ser escalar. O caminho completo identifica também folhas recursivas já existentes, como `raw/a~1b~0/0`; o atalho não cria caminhos, altera containers, remove elementos ou edita relações. Observações pendentes sem valor corrente não constituem um alvo desta rota. Reorganização de objetos/coleções e relações dependem de contratos próprios.

## Transação, histórico e identidade

A transação PostgreSQL bloqueia a operação idempotente e a entidade, confere a versão e acrescenta uma observação, uma operação e uma outbox. Incrementa a versão somente do item tocado e da entidade. Não cria job de migração, outra entidade, item ou chave de identidade. O histórico registra o valor anterior, observação anterior, origem, caminho `/value`, datas, recepção, ator, motivo, chave de API e contrato sem inferência de normalização.

A referência interna da operação aponta à observação corrente encontrada sob o bloqueio; ela não é uma nova identidade de origem. Alterar um campo de documento não atualiza a chave documental de lookup. A edição pode, portanto, deixar o documento cadastral divergente da identidade registrada, visível no histórico e explicitamente informado no formulário. Resolução e reatribuição de identidade continuam pendentes de fluxo próprio.

A precedência continua por campo: data da fonte, senão observação; atraso, ausência contra valor datado e data futura além da tolerância geram histórico sem substituir o estado atual. A operação aceita incrementa a versão mesmo quando a observação não prevalece. Datas de campos ausentes e confirmações permanecem intactas. Não é criada flag; confirmação do valor antigo fica preservada e deixa de ser aplicável a um valor diferente. O cursor anterior continua apresentando o corte anterior.

Repetição com a mesma chave e corpo/contexto recupera o recibo imutável mesmo depois de outra versão ou reinício. Corpo, alvo, coleção ou chave de API divergentes com a mesma chave idempotente geram 409. Outra operação sobre versão vencida gera 409. Autorizações e fonte são conferidas novamente antes de devolver replay. Falha de destino gera 503 sem detalhes privados; repetir com a mesma chave permite recuperar um commit cuja resposta se perdeu. Rollback abrange valor, versões, operação e outbox.

## Painel e verificação

“Editar este valor” aparece em campos escalares correntes quando escrita e `enrich` estão habilitados. O formulário mostra campo, valor e versão, recebe literal JSON exato, origem, motivo e datas independentes. Cancelamento/troca de contexto desmontam o editor; a chave idempotente é mantida para repetir o mesmo corpo após falha de transporte. Após escrita, ficha, itens e campos são reabertos em um novo corte.

A evidência específica está em [validacao-edicao-escalar-canonica.json](validacao-edicao-escalar-canonica.json), com comandos, resultados exatos e hashes. A validação aprovou 1043 testes backend sem falhas/erros/skips, build, 14/9/6 verificações auxiliares e 26 grupos de navegador. A primeira tentativa de navegador falhou no rótulo da textarea preenchida; a correção acessível foi seguida de novo build/navegador, com hashes estáveis e backend inalterado. Os 38 testes novos também passaram separadamente. Os testes dedicados incluem pessoa/empresa, precisão, null/false/zero, folhas recursivas, flags do valor antigo, cortes, datas atrasadas/futuras, fontes/permissões, rollback, concorrência, replay após reinício e identidade documental. O navegador usa autenticação separada e schema PostgreSQL descartável na porta 18767.

Continuam obrigatórios: normalizadores/catálogos canônicos, reestruturação recursiva de coleções, relações, importadores/exportações canônicas, autenticação PostgreSQL, ES real, implantação definitiva, piloto, reconciliação, migração, carga e disponibilidade. O bloco não homologa produção nem conclui a matriz.
