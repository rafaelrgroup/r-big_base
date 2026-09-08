# Confirmação recente para administrar usuários

As escritas `POST /api/v1/admin/invitations`, `POST /api/v1/admin/users` e `PATCH /api/v1/admin/users/{id}` exigem permissão administrativa, sessão humana válida, CSRF e TOTP confirmado nessa sessão nos últimos cinco minutos. A ativação inicial por convite continua permitindo que o convidado defina a senha e configure seu próprio autenticador; ela não exige uma sessão administrativa do convidado.

O TOTP confirmado no login já inicia essa janela. Sessões criadas antes desta implementação continuam válidas para as operações existentes, mas precisam confirmar o autenticador antes dessas três escritas. Ler a listagem de usuários não consome uma nova confirmação. Usar a sessão, consultar dados ou executar ações administrativas não renova a janela. Datas ausentes, inválidas, sem fuso ou futuras não concedem confirmação recente.

## Contrato da confirmação

Quando a sessão precisa de nova confirmação, a ação retorna HTTP 403 com:

```json
{
  "detail": {
    "code": "RECENT_TOTP_REQUIRED",
    "message": "Confirme com seu autenticador para continuar esta ação.",
    "max_age_seconds": 300
  }
}
```

O cliente envia `POST /api/v1/auth/step-up` com `{ "code": "123456" }`, cookie da sessão e `X-CSRF-Token`. O exemplo é ilustrativo. O corpo exige texto ASCII com exatamente seis dígitos e rejeita propriedades extras, inclusive tentativa de indicar outro usuário. A resposta aprovada contém `confirmed: true`, `valid_until` e `max_age_seconds: 300`. O prazo é conferido novamente pelo servidor em cada ação; `valid_until` no cliente não constitui autorização.

`totp_verified_at` fica no objeto da sessão, sem guardar o código. A confirmação de uma sessão não libera outra sessão do mesmo usuário. `last_otp_step` continua pertencendo ao usuário: códigos aceitos no login, na emissão existente de chave ou no step-up não podem ser reutilizados em outra sessão. São mantidos os períodos de 30 segundos, a tolerância existente de um período anterior/posterior e a atualização atômica do contador e da sessão. Um código futuro aceito pela tolerância também avança o contador; o usuário deve aguardar um código posterior para uma nova confirmação.

Código inválido ou já utilizado retorna HTTP 401 com `TOTP_INVALID_OR_REPLAYED`; isso não transforma a confirmação em logout nem renova a janela. Uma chave de integração retorna HTTP 403 com `HUMAN_SESSION_REQUIRED`, inclusive quando enviada junto com cookie válido ou código TOTP. A chave não pode satisfazer o fator humano. O endpoint de confirmação também exige permissão administrativa.

Rate limit existente por IP de autenticação, limite agregado do usuário e limite de tentativas OTP da conta cobrem o novo endpoint. Falha no coordenador Redis continua bloqueando a operação. A auditoria grava `admin_step_up`, ator, destino e data; não registra o código, segredo TOTP, cookie ou token de sessão.

## Comportamento no painel

O painel tenta a ação solicitada. Ao receber `RECENT_TOTP_REQUIRED`, mostra a ação exata e solicita um novo código do autenticador. O nome do convidado ou a alteração de estado ficam apenas na memória da tela, permitindo retomar o pedido após a confirmação. O código é limpo ao enviar, errar, cancelar ou trocar a sessão e não é gravado em armazenamento local ou relatórios.

Cancelar retorna ao formulário sem executar a ação. Mesmo se o cancelamento ocorrer durante a chamada de confirmação, sua resposta não retoma o pedido cancelado. Mudança de sessão/CSRF ou descarte da tela invalida a ação pendente. Só a resposta específica de confirmação obrigatória provoca retomada; falhas de rede não repetem automaticamente a criação de um usuário.

Em Administração → Usuários, o administrador também pode confirmar a desativação ou ativação de outro usuário. Desativação revoga sessões, convites/desafios e chaves conforme o mecanismo já existente. Reativar não ressuscita sessões nem chaves revogadas. Convite ainda pendente não pode ser ativado por esse atalho e o administrador não pode desativar seu próprio acesso. Os dados cadastrais e sua rastreabilidade permanecem intactos.

## Validação e escopo

Os testes de `backend/tests/test_admin_stepup.py` usam contas sintéticas em pastas temporárias. Cobrem limite exato de cinco minutos, expiração, data futura/inválida, ausência de renovação por uso, replay de login/step-up entre duas sessões, revogação, usuário normal, API key com cookie, CSRF, vida útil da sessão, formatos estritos e auditoria sem segredos. As regressões de convites e criação direta existentes continuam usando o TOTP recente do login.

O navegador usa somente o fixture `var/browser-test` em 18767. O ensaio expira deliberadamente apenas a confirmação do administrador sintético, sem redefinir seu contador TOTP. Confere cancelamento sem criar usuário, rejeição do código já utilizado, retomada com exatamente um convite e ativação/desativação sem restaurar sessão revogada. Nenhum teste altera a conta principal, seu OTP ou seus dados.

Este bloco cobre administração de usuários no adaptador local. A aplicação continua identificada como desenvolvimento isolado; o armazenamento definitivo e a validação distribuída permanecem nos seus próprios blocos. Estender autenticação reforçada a outros catálogos, permissões, redefinições administrativas e demais operações sensíveis requer implementação e evidências específicas. A emissão de API keys conserva o requisito de novo OTP que já possuía; não foi convertida silenciosamente para a janela de cinco minutos.
