# Implantação separada do desenvolvimento

A factory `bigbase.production:create_app` usa uma configuração explícita de implantação, PostgreSQL canônico já provisionado e um diretório privado de controle. Ela não inicializa esquema nem cria usuários, senha ou chave de criptografia. Os guardas das factories e fixtures sintéticas continuam ativos.

A implantação inicial usa ambiente `staging` no PostgreSQL real. `runtime=deployed`, `synthetic=false` e `storage_mode=canonical` identificam esse modo; `production_connected` só é verdadeiro se o ambiente durável do banco for `production`. Não existe conversão implícita de ambiente. O andamento da migração retorna desconhecido até receber uma fonte de progresso durável; estar conectado ao banco não comprova migração concluída.

A configuração privada, informada por `BIGBASE_DEPLOYMENT_FILE`, exige `environment`, `canonical` (`dsn_file`, `deployment_id`, `database`, `schema`, `port`), `control_dir`, `redis_url_file`, `allowed_hosts` e `writes_enabled`. A variável `BIGBASE_ENV` deve coincidir com o ambiente. A conexão exige PostgreSQL 18, porta dedicada 15432 e endereço loopback, com UUID, banco e esquema fixados; todas as transações revalidam esses metadados e o hash do esquema. Nenhuma DSN de fixture é aceita. Catálogo PostgreSQL exige extensão previamente provisionada e metadados de versão/hash da DDL.

O controle SQLite contém exclusivamente contas, sessões, desafios OTP, convites, chaves de API e configuração de fontes/catálogos. `control.sqlite3` e `encryption.key` devem ser preparados juntos em diretório privado; preservar os registros administrativos e a chave existente mantém as credenciais e OTP. O processo recusa dados pessoais, trabalhos de importação/exportação ou outros tipos no controle. Os históricos originais permanecem no backup; o controle não serve como destino de migração. Triggers também impedem gravar entidades por SQL direto. Há um lock exclusivo e somente um processo Uvicorn.

Redis é obrigatório, por socket Unix ou loopback; o namespace inclui a identidade de implantação. Se Redis falhar, o controle de acesso retorna indisponibilidade. A sessão exige OTP e usa cookie Secure, HttpOnly e SameSite=Strict. Mutações com sessão exigem CSRF; operações administrativas mantêm confirmação OTP recente. Não definir `BIGBASE_LOCAL_HTTP=1`, nem usar `testing=True` ou o script de desenvolvimento para publicação.

O processo de publicação deve usar uma cópia estável do código, dependências próprias e frontend compilado na mesma versão, com manifesto comparado antes/depois da cópia. O código final deve ser imutável para o serviço. O serviço pode executar `scripts/run-deployed.sh` com configuração privada, porta loopback própria e permissão de escrita apenas no controle. O proxy deve encaminhar o `/api` do painel para essa mesma origem e o domínio de API para os mesmos endpoints. Assim não é necessário compartilhar cookies entre domínios nem abrir CORS. Os cabeçalhos de proxy são confiados apenas de loopback.

Nesta etapa são habilitados autenticação/administração, consulta por identidade, ficha/histórico, enriquecimento, validação, edição escalar e catálogo canônico conforme capacidades informadas pela API. As rotas de pessoas/empresas, importação/exportação e pesquisas salvas do adaptador SQLite retornam indisponibilidade depois de autenticar. O painel não as oferece no modo implantado. Pesquisa canônica por filtros depende do leitor de busca, ainda não configurado na factory inicial; consulta por documento/origem continua disponível. Isso não atesta carga integral, desempenho de produção ou conclusão dos demais requisitos.

Validação da mudança: testes de autenticação e OTP, Secure/CSRF, rate limit, host permitido, isolamento do controle, trava de um processo, recusa de fixture/configuração divergente e regressão do adaptador existente. Testes PostgreSQL devem usar banco de validação separado, jamais inserir dados sintéticos no destino da migração.


O campo opcional `status_file` recebe uma publicação operacional saneada do
andamento. Ele não é um guardião de migração: o publicador precisa vincular e
verificar as provas antes de informar conclusão. A API expõe a data dessa
publicação; não converte percentual de restauração em migração concluída.

A identidade de conexão fixa UUID da implantação, banco, esquema, endereço,
porta, versão e hash da DDL; ainda não fixa `system_identifier` do cluster.
Transações da implantação usam REPEATABLE READ para compatibilidade com cortes
estáveis do histórico. Concorrência pode produzir falha transitória de
serialização; o cliente deve repetir a mesma chave idempotente. Capacidade e
políticas de repetição sob carga precisam de homologação própria.

## Validade do relatório de andamento na MAIN

O loader recalcula a idade de `updated_at` a cada health, sem usar o mtime do
arquivo. Até 120 segundos a publicação é `fresh`; acima disso é `stale`.
Datas mais de 30 segundos no futuro geram `clock_skew`. Nos dois últimos casos,
`status` e `phase` passam a `needs_attention`, `migration_complete` fica null e
os campos `reported_status`/`reported_phase` preservam o estado informado.
Contagens, percentual e data continuam como última observação, sem inferir
avanço. O painel identifica as contagens antigas e oculta a barra de progresso.
Falhas e pausas têm prioridade sobre a fase informativa. Nova publicação válida
recupera o estado automaticamente na próxima leitura (painel consulta a cada
30 segundos e oferece atualização manual).

A expiração também se aplica a um relatório de conclusão: a prova histórica
não é apagada, mas o serviço deixa de atestar um estado atual sem publicação
recente. Relatórios ausentes, inacessíveis ou malformados retornam andamento
desconhecido; isso não transforma uma conexão canônica saudável em falha do
banco. O contrato mantém uma lista explícita de campos públicos, sem repassar
metadados privados. Esses campos informativos não autorizam promover um piloto.

Validação sintética desta revisão: 55 testes direcionados de backend, build e
oito grupos de navegador com API simulada. Evidência em
`validacao-runtime-freshness.json`. Mudança preparada na MAIN, sem atualização
da release ativa, do publicador, das unidades ou do runtime do piloto.
