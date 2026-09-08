# Rotação de chaves de integração

O dono da chave ou um administrador pode substituí-la usando sua sessão humana e **um novo TOTP**, com proteção contra reutilização do código entre sessões. Uma chave de integração não pode rotacionar, revogar ou listar outras chaves, mesmo enviada junto com cookie de uma sessão humana.

A sucessora mantém o mesmo titular, nome, escopos, origens e vencimento original. Rotação não concede permissões adicionais, não reativa usuário ou fonte e não renova os 90 dias da emissão inicial. Os limites agregados do titular continuam abrangendo todas as suas chaves.

## Transição e encadeamento

O padrão é 900 segundos de transição; a API aceita `grace_seconds` inteiro de 0 a 86.400. Zero desabilita imediatamente a chave anterior. Durante a transição, ambas funcionam até o menor prazo entre a graça escolhida e o vencimento original. No instante do término, a anterior já é recusada. Se o vencimento original chegar antes, a sucessora também expira: rotação não estende a validade.

Cada versão preserva seu identificador público e registra `rotation_root_id`, `predecessor_id`/`successor_id`, `created_at`, `rotated_at`, `retire_at`, `original_expires_at` e `expires_at`, conforme aplicável. A mesma versão não produz duas sucessoras. Nova rotação deve partir da sucessora atual. O histórico de auditoria registra ator, IDs da cadeia, parâmetros de transição, datas e fingerprint da operação, sem código OTP ou segredo da chave.

O prazo efetivo da anterior é gravado no próprio `expires_at`, mantendo o original em campo separado. Assim, os consumidores existentes — incluindo importações que verificam a chave a cada entrada — respeitam a transição sem depender de um job futuro de revogação. Uma importação vinculada à chave anterior não assume silenciosamente a identidade ou as permissões da sucessora.

**Revogar qualquer versão revoga toda a cadeia**, incluindo anteriores em transição e sucessoras. A listagem/painel usa a expressão “Revogar cadeia” e exige confirmação dessa ação. Desabilitar o usuário ou recuperar/redefinir seu fator conforme o fluxo existente também corta suas chaves e as respostas pendentes de rotação. Reativar o usuário não reativa as credenciais.

## API

| Operação | Contrato |
|---|---|
| `GET /api/v1/admin/api-keys` | Sessão humana; usuário normal recebe somente suas chaves, administrador recebe as chaves sob administração. Nunca retorna segredos, hashes de autenticação ou recibos cifrados. |
| `POST /api/v1/admin/api-keys/{id}/rotate` | Sessão humana do dono/admin, CSRF, `Idempotency-Key`, novo OTP e transição. Retorna 201 com a sucessora e seu segredo. |
| `PATCH /api/v1/admin/api-keys/{id}` | Sessão humana do dono/admin e CSRF. Revoga toda a cadeia e retorna seus IDs públicos revogados. |

O segmento `{id}` é o identificador público da chave, não seu segredo. O namespace administrativo foi mantido para compatibilidade com a aplicação; autorização de proprietário é conferida em cada operação. A emissão inicial continua reservada ao administrador e conserva o OTP que já exigia.

Primeira tentativa, com valores ilustrativos:

```http
POST /api/v1/admin/api-keys/{id}/rotate
Content-Type: application/json
X-CSRF-Token: <da sessão humana>
Idempotency-Key: <identificador único da operação>

{"otp":"123456","grace_seconds":900}
```

A resposta contém `id`, `key`, `expires_at`, `predecessor_id`, `predecessor_valid_until`, `grace_seconds`, `rotation_root_id` e `response_recoverable_until`. O segredo anterior nunca é retornado. A nova chave não pode ser consultada na listagem posteriormente.

Propriedades como `scopes`, `sources`, `user_id` e `expires_at` não são aceitas no pedido de rotação. Alterar permissões e emitir nova validade são operações administrativas diferentes, não efeitos implícitos de renovar o segredo.

## Resposta perdida e idempotência

Para permitir falha de rede após o commit, a resposta fica cifrada por até **cinco minutos**, limitada também ao vencimento da sucessora. O recibo pertence à sessão exata que iniciou a operação e usa fingerprint do ID público e da transição. O OTP não é persistido, nem incluído nesse fingerprint.

Na mesma sessão, repetir a mesma `Idempotency-Key` e a mesma transição recupera a resposta original, inclusive se a anterior já foi desabilitada por graça zero. O OTP pode ser omitido na recuperação:

```json
{"grace_seconds":900}
```

Essa recuperação não consome outro OTP, não cria outra versão, não estende datas e não duplica o evento de rotação. Sem recibo já existente, omitir o OTP retorna `NEW_TOTP_REQUIRED`; o cliente pode reenviar a mesma operação com um novo código. Isso distingue pedido que não chegou ao servidor de resposta que se perdeu depois da gravação.

A mesma identificação com outra transição retorna `IDEMPOTENCY_CONFLICT`. Outra sessão não recupera o segredo. Depois da janela, `ROTATION_RESPONSE_EXPIRED` informa somente o ID da sucessora; o operador pode selecioná-la e fazer uma nova rotação com novo TOTP, preservando o vencimento e a cadeia. Não há endpoint genérico para revelar segredos de chaves.

Recibos guardam ciphertext e metadados. A rotina local remove o ciphertext expirado em sua próxima execução, a cada 60 segundos e também após reinício; preserva identidade, fingerprint e datas da operação. Revogação de cadeia ou usuário remove essa resposta recuperável imediatamente. O acesso à resposta é bloqueado pelo prazo mesmo antes da limpeza. Essa limpeza lógica não representa promessa de destruição física de páginas ou backups antigos do banco.

## Painel

O administrador usa Administração → Chaves de API; usuários normais usam Minhas chaves. A tela mostra validade, transição e encadeamento. “Renovar chave” oferece revogação imediata, 15 minutos, uma hora ou 24 horas e solicita um novo código do autenticador.

O código é limpo ao enviar. Se a resposta de rede falhar, a tela conserva somente o ID da operação e seus parâmetros na memória e oferece “Recuperar resultado”, sem armazenar ou exigir novamente o OTP. Uma resposta inequívoca de código inválido permite informar outro código. Durante a chamada, o formulário evita outra submissão. Fechar o resultado limpa o segredo da memória do formulário; a lista não o contém.

Rotação e recuperação são limitadas por usuário, IP e tentativas OTP da conta. Cookie, CSRF, usuário ativo, OTP habilitado e autorização do dono/admin são reavaliados. Receber o mesmo código já usado em login/step-up/outra emissão não autoriza uma nova rotação; a pessoa precisa aguardar um código posterior.

## Evidências e limites

`backend/tests/test_api_key_rotation.py` usa somente dados sintéticos e cobre transição no limite exato, graça zero, vencimento herdado, resposta cifrada e repetição sem OTP, concorrência com uma única sucessora, não bifurcação, sessão diferente, TTL/limpeza, replay global, revogação em qualquer nível, desabilitação, dono/admin/terceiros, API key com cookie, CSRF, entradas inválidas, listagem sem segredos e prazo aplicado ao worker antigo.

O teste de navegador cria uma chave sintética, confirma a rotação com novo OTP, interrompe deliberadamente a resposta **depois** do commit, recupera o mesmo segredo sem duplicar a sucessora e confere as duas chaves durante a transição e sua recusa após revogar a cadeia. A conta principal e as chaves de produção não participam dos testes.

Este é o adaptador local de processo único. A atomicidade usa a transação de escrita existente; armazenamento definitivo, operação distribuída e homologação de capacidade permanecem em seus blocos próprios. O fluxo não presume que recuperar uma resposta autoriza ler cadastros além dos escopos originais.
