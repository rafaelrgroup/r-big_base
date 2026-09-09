# Matriz de implementação e validação

Atualizada em 09/09/2026. Restauração do backup original aprovada em ambiente isolado; provas operacionais preservadas fora do Git. O novo servidor recebeu uma cópia validada do projeto. O destino agora dispõe de PostgreSQL dedicado, Redis privado e uma implantação separada do painel/API em ambiente canônico `staging`. As cinco fontes do backup foram restauradas na Linode, incluindo 476.401.269 entradas das duas fontes cadastrais. Em 09/09/2026 às 12:19 UTC, um novo piloto concluiu 10.000 entradas com reconciliação por campo, preservando as 6.910 conferidas na tentativa anterior. O destino contém 16.910 entidades neste corte; essa contagem não estima pessoas únicas na origem inteira. A projeção real de busca está sendo preenchida e sua ativação depende de comparação completa com o PostgreSQL. A migração integral continua condicionada ao piloto representativo, reconciliação por campo e validação de capacidade. **Não é entrega integral e os testes sintéticos abaixo não homologam a carga real.**

Bloco anterior validado na Linode: catálogo PostgreSQL sintético transacional,
com **406 regressões de backend e 33 grupos de navegador** nesta rodada. Os
**44 testes específicos do catálogo** aprovados na revisão da transferência
permanecem vinculados ao mesmo backend/DDL, sem reexecução. Build aprovado;
detalhes e tentativas preservadas no bloco de catálogo ao final desta matriz.


Bloco atual: implantação explícita separada da árvore de desenvolvimento, com
contas/OTP preservados, PostgreSQL canônico, Redis obrigatório, cookie Secure,
CSRF, verificação do destino por transação e controle SQLite sem dados pessoais.
A candidata passou em **38 testes dedicados de runtime/PostgreSQL**, **373
regressões locais**, build e **seis grupos de interface**. O serviço implantado
passou nas verificações HTTP e de navegador sem sessão em desktop/celular; as
rotas protegidas responderam 401. Consulta por identidade e escrita canônica
estão disponíveis; busca por filtros e operações em massa canônicas continuam
explicitamente desabilitadas. Contrato: [DEPLOYMENT-RUNTIME.md](DEPLOYMENT-RUNTIME.md).
A integração da árvore principal preserva o bloco anterior e tem prova própria
na seção de integração abaixo; essas contagens não são uma nova suíte integral.

| Requisito | Implementação verificável | Evidência / restante |
|---|---|---|
| Pessoas e empresas, múltiplos itens | API e painel; identidade, documentos, contatos, endereços, usernames, atividades e campos adicionais | Integração local e navegador; catálogo completo empresarial/estabelecimentos ainda pendente |
| Origem e história por campo e flag | Entrada original inclusive null/false/zero, valor anterior, fonte, data da fonte, observação, recepção, ator e operação | `test_flows.py`: idempotência, eventos atrasados, datas ausentes/futuras e concorrência; PATCH de folhas escalares existentes implementado; reorganização recursiva de objetos/coleções pendente |
| Confirmações | true/false/null; validade, WhatsApp, titularidade, entregabilidade e residência | Métodos, referências, vencimento e histórico; evidências de valores atrasados não confirmam o valor atual; vencimento não fabrica false nem altera outra flag; PATCH canônico dedicado de flags validado em PostgreSQL sintético e navegador |
| Normalização | CPF, CNPJ numérico/alfanumérico, telefone, email, CEP e plataforma de username | Vetor alfanumérico oficial, zeros, regra histórica Anatel 8/9 nos 67 DDDs, conflitos/ramais/rádio e precisão numérica; integração optativa de telefones no enriquecimento canônico HTTP/painel testada; normalização de emails, CEP/código postal e plataforma de username implementada com provas próprias abaixo; revisão operacional de normalização e piloto real ainda pendentes |
| Deduplicação | Itens normalizados, documento consistente e ID por fonte | Conflitos bloqueados; observação futura não associa identidade; fusão/desfusão e registro definitivo de titularidade pendentes |
| Vínculos | Pessoa/pessoa, pessoa/empresa, empresa/empresa, identificação pendente e relação inversa derivada | API + aba Vínculos; teste impede autocorrelação e confirma que relação inversa preserva evento original |
| Pesquisa | AND/OR recursivo, mesmo item, idade/CEP, ordenação múltipla local e inclusão explícita de invalidados | Backend + navegador sintético: prioridades, min/max, ausentes, datas, Unicode, legado e desempates; leitor canônico PIT com cursor protegido implementado e testado por HTTP simulado; ligação HTTP sintética com versões do cadastro/índice implementada; ordenação múltipla no canônico e índices reais pendentes |
| Pesquisas salvas | Critérios, tipo, ordenação múltipla e inclusão de invalidados por usuário; arquivamento preserva critérios e pesquisas antigas mantêm contrato legado | API + navegador: salvar, reabrir, executar e exportar com os mesmos critérios; conversão do legado exige edição explícita |
| Consulta em massa | No adaptador local: texto, listas CSV/XLSX, filtros e ordenação compartilhada; fila, percentual, cancelamento e download autenticado. Rotas locais bloqueadas na implantação canônica | Navegador e API: IDs na mesma ordem da pesquisa/XLSX, inclusive divisão em volumes e retomada no corte local |
| XLSX completo | Colunas escalares, abas relacionadas, estado, história, confirmações, entradas e correspondências; fragmentos de texto e valores estruturados | Reabertura, contagem e conteúdo numérico/booleano; números inseguros como texto exato; definições versionadas até o corte; divisões por linhas/colunas e volumes ZIP com hashes testadas |
| Processamento de exportação | Estado persistido, corte local consistente, reinício de pendentes; arquivos só disponíveis após verificação | Checkpoints incrementais, streaming em escala, limite de memória por trabalho ainda pendentes; limpeza de arquivos expirados implementada/testada, sem remover dados cadastrais |
| Importação assíncrona | No adaptador local: JSON/JSONL pelo painel e API, resultados por entrada, checkpoint atômico, cancelamento e retomada. Implantação canônica não usa essas filas locais | 1.000 entradas/2 MiB no adaptador local; transporte JSONL/PIT separado para canônico, limite de expansão, URL/UUID fixados e retomada testados; integração dos importadores HTTP ao canônico e sistemas reais pendentes |
| Convite e OTP | Convite de 24 h, senha própria, primeiro OTP obrigatório, Argon2id, TOTP cifrado, replay bloqueado | Backend e navegador; convite só funciona uma vez e usuário normal não vê administração |
| Recuperação e revogação | Códigos de recuperação, novo OTP, revogação de sessões/chaves; desabilitar usuário revoga acessos; TOTP recente por sessão para administrar usuários | Step-up incluído na rodada integrada aprovada; conta administrativa existente preservada; redefinição administrativa e autenticação reforçada nos outros catálogos pendentes |
| Administração | Usuários, convites, fontes, campos, API keys e auditoria | Campos tipados/versionados com validação estrita, alternativas, escopo e inativação preservados; catálogo PostgreSQL sintético transacional integrado ao enriquecimento, com versão, idempotência, histórico e OTP recente; provas e limites no bloco de catálogo abaixo. Indexação, edição granular de permissões, quotas e outros catálogos ainda pendentes |
| Chaves API | Hash, escopos, origem permitida, expiração, revogação e OTP na emissão | Rotação com OTP recente, período de transição, recuperação idempotente de resposta e revogação de toda a cadeia implementadas e testadas em backend/navegador; políticas administrativas completas pendentes |
| Rate limit | Redis opcional no desenvolvimento e obrigatório na implantação; cota por usuário/chave, IP e conta, namespace por implantação e falha Redis retorna 503 | Integração com Redis real isolado, atomicidade com dois clientes e falha sem liberação testadas; failover, quotas por custo/admin e teste distribuído prolongado pendentes |
| Interface/PWA | Português, tema, desktop/móvel, manifesto, service worker apenas estático | Navegador confirma fluxos e ausência de API no cache; instalação em dispositivos, atualização controlada, auditoria completa de acessibilidade e virtualização pendentes |
| Migração | Adaptadores puros para os140 caminhos inventariados das duas fontes; átomos/containers, normalização conservadora e semântica desconhecida preservada; leitor JSONL/PIT; CLI com guardião de destino/capacidade; piloto limitado | Novos contratos em `ADAPTADORES-MIGRACAO.md`, `EXECUTOR-MIGRACAO.md` e `PREFLIGHT-MIGRACAO.md`; pipeline exercitado no PostgreSQL sintético. Conferência por releitura do destino implementada e testada; finalização depende de prova vinculada ao destino/fonte/versões. Novo piloto real de 10 mil entradas concluído e reconciliado em 09/09/2026 às 12:19 UTC; 6.910 entradas anteriores preservadas. Transporte por sequência retoma após interrupção sem depender do PIT anterior. A carga integral não começou: espaço e vazão do modelo atual são inadequados à escala das fontes. Resolução de conflitos/coleções codificadas/registros grandes, piloto representativo de pelo menos 1 milhão e capacidade real pendentes |
| Banco definitivo / outbox / busca | Repositório PostgreSQL18 com64 partições, operações/observações imutáveis, identidade documental, outbox/leases e checkpoint transacional; projeção ES versionada com filtros correlacionados; paginação do histórico por corte de versão | Testes PG reais com dados fictícios e testes HTTP simulados da busca; contratos em `CANONICAL-STORE.md` e `PROJECAO-BUSCA.md`. O modo de desenvolvimento mantém o adaptador SQLite e fixture sintético. A implantação separada usa SQLite só para controle/autenticação e PostgreSQL real para ficha, catálogo, leituras e enriquecimento por campo em transação PostgreSQL/outbox, com origem/ator autorizados, idempotência e versão. Exportações/importadores canônicos, integração dos normalizadores/catálogos, reorganização de coleções, autenticação PostgreSQL, ES real, papéis de produção e homologação de escala ainda pendentes |
| Backup/restauração | Backup original e restauração isolada aprovados | Manifestos, contagens, mapeamentos, consultas e configuração conferidos; provas no pacote privado. O novo estado administrativo/projeto requer cópia complementar conforme RETOMADA.md |
| Desempenho e disponibilidade | Metas no plano | 50 req/s, 1 h/8 h, replicação, failover, RPO/RTO e reconciliação integral não medidos |

## Evidências e limites

A rodada integrada de 08/09/2026 passou com **896 testes de backend, 14 testes de precisão, nove de entrada de importação, seis de compatibilidade de ordenação e 22 grupos no navegador**, além do build TypeScript/Vite. Nenhum teste backend foi ignorado. Veja [VALIDACAO.md](VALIDACAO.md) e o manifesto público de hashes. O código permaneceu igual antes/depois dos testes. Logs completos, tentativas com falha, capturas e arquivos gerados permanecem no pacote privado.

PostgreSQL 18.6 real isolado, com dados fictícios e socket Unix privado, validou persistência, paginação, reconciliação e migração sintética. A busca Elasticsearch usa testes HTTP simulados e ainda precisa de integração com cluster real no novo ambiente. Redis foi exercitado em processo isolado; isso não transforma o adaptador HTTP SQLite em banco definitivo ou permite múltiplos processos de produção.

Na rodada histórica integrada de 08/09/2026 não houve migração real ou alteração dos índices originais. O avanço operacional posterior é registrado por provas privadas; publicar o código ou ativar o painel não equivale a concluir a migração ou homologar a capacidade de produção.

## Próximos blocos obrigatórios

1. Validar integração real da projeção dos campos administráveis (contrato sintético descrito abaixo), rastreabilidade recursiva, revisão de identidade/fusão/desfusão, mapeamento/importação dos sistemas reais e revisão operacional da normalização. Definições tipadas/versionadas e regra histórica conservadora já estão implementadas.
2. Integrar PostgreSQL/outbox, projeções de busca, filas com checkpoints, cursor e respostas/exportações em fluxo no novo ambiente.
3. Validar instalação/atualização PWA, acessibilidade, isolamento completo, autenticação reforçada e operação administrativa.
4. Após backup validado e destino disponível: piloto, reconciliação por campo, dimensionamento, migração e carga/falhas/recuperação antes de troca de tráfego.

O plano completo continua obrigatório; esta matriz registra o estado real, sem reclassificar itens obrigatórios pendentes como opcionais.

## Disponibilidade dos filtros canônicos por versão — MAIN, 09/09/2026

Implementado e testado contrato puro de disponibilidade/seleção dos campos
adicionais: definição histórica, implantação, cluster/índice, versão da projeção,
corte e recibo de reconciliação com validade máxima de 120 segundos. Provas
ausentes, vencidas, incompletas ou incompatíveis impedem a seleção. Preserva
triestado, precisão e correlação de fonte/flags; não habilita ordenação.
**198 testes sintéticos aprovados**, incluindo 47 novos, sem falhas/erros/skips
finais. Detalhes e limites em [BUSCA-CAMPOS-CANONICOS.md](BUSCA-CAMPOS-CANONICOS.md)
e [validacao-disponibilidade-busca.json](validacao-disponibilidade-busca.json).

Ainda pendentes: produtor/verificador confiável das provas, persistência e
integração HTTP/PIT/painel, PostgreSQL e Elasticsearch reais. O contrato não
autentica uma declaração fornecida pelo cliente nem constitui prova operacional.
Busca ampla publicada permanece desativada. Piloto observado nesta rodada ainda
em `needs_attention`, 6.910/10.000, sem conclusão verificada. Migração integral,
capacidade, 50 req/s e matriz completa continuam não homologadas.

## Disponibilidade histórica via HTTP — MAIN, 09/09/2026

Rota autenticada por campo/versão, com leitura histórica/hash, dependência interna
optativa de provas, reavaliação de vencimento e resposta sem cache. Recusa contexto
do cliente e ambiente não sintético. **286 testes aprovados, 33 novos**, sem
falhas/erros/skips finais. Catálogo/provedor simulados; autenticação/OTP/escopos e
revogação exercitados na aplicação com contas sintéticas. Prova e limitações em
[validacao-disponibilidade-http.json](validacao-disponibilidade-http.json) e
[BUSCA-CAMPOS-CANONICOS.md](BUSCA-CAMPOS-CANONICOS.md).

Não habilita execução de filtros ou busca pública. Provedor confiável/persistência,
PIT, seletor visual e integração PostgreSQL/Elasticsearch continuam pendentes.
Nenhuma mudança de frontend, release ou piloto; não houve novo build/navegador.
Última observação operacional nesta rodada: `needs_attention`, 6.910/10.000,
conclusão não verificada. Migração integral e homologação permanecem pendentes.

## Transferência em preparação

O código está publicado no repositório indicado pelo proprietário. O novo servidor recebeu o projeto, o histórico Git e uma cópia privada conferida por hashes, preservando as contas existentes. A árvore de desenvolvimento continua isolada. Uma cópia de código validada publica painel/API autenticados com dados canônicos no PostgreSQL; progresso de restauração/migração é informativo e não prova conclusão. Seguir [RETOMADA.md](RETOMADA.md), [INFRAESTRUTURA.md](INFRAESTRUTURA.md) e o [plano completo](PLANO-PROJETO-COMPLETO.md). Ao concluir todos os requisitos e a migração, lembrar o proprietário de tornar o repositório privado.

A meta inicial de capacidade foi revisada pelo proprietário para **50 requisições por segundo**, sem reduzir preservação de dados, testes de falha, rastreabilidade ou completude. O perfil inicial econômico está em INFRAESTRUTURA.md; a capacidade final permanece dependente do piloto. Isso atualiza o planejamento, não declara um novo limite já homologado no serviço em execução.

## Validação da instalação no novo servidor

Em 08/09/2026, a instalação Ubuntu 26.04 / Python 3.14 / PostgreSQL 18.6 passou com **923 testes de backend sem falhas ou testes ignorados**, build, 14 verificações de precisão, nove de entrada de importação, seis de compatibilidade de ordenação e 22 grupos no navegador. Os hashes do código permaneceram iguais durante os testes e foram conferidos na origem. Veja [validacao-linode.json](validacao-linode.json). O novo modo explícito de PostgreSQL instalado mantém o fixture sintético privado e acrescenta 27 verificações de isolamento. Essa prova valida a instalação; a integração ao banco canônico, o piloto, a carga real e a migração continuam pendentes.

## Leitura canônica HTTP e ficha de ensaio

Bloco de leitura implementado com dependência PostgreSQL explícita, ambiente sintético verificado, autenticação/permissões existentes, cursores ligados ao usuário/contexto, serialização numérica exata e ficha paginada de pessoa/empresa. Busca opcional devolve versões e metadados canônicos com continuação explícita das coleções. Contrato, limites e ensaio: [LEITURAS-CANONICAS-HTTP.md](LEITURAS-CANONICAS-HTTP.md). Nesse checkpoint histórico, a escrita ainda estava pendente; o recorte posterior está descrito abaixo. Exportação canônica e serviço definitivo seguem pendentes. As evidências específicas registram a validação desta versão; não reutilizar as contagens da instalação anterior.

Validação deste bloco na Linode: **941 testes backend sem falhas/skips**, build, **14** verificações de precisão, **nove** de entrada de importação, **seis** de ordenação e **23 grupos de navegador**. A primeira tentativa do navegador falhou no nome acessível de um seletor; a correção de interface foi validada por novo build/navegador com os hashes de backend inalterados. Veja [validacao-leituras-canonicas.json](validacao-leituras-canonicas.json). Esse relatório preserva o estado histórico da execução automática, incluindo a indisponibilidade de escrita nos metadados Git e de reinício do serviço na origem. A revisão manual subsequente acrescentou manutenção em lotes dos cursores expirados, com verificação do destino a cada lote e tentativas após falhas; a evidência complementar registra os arquivos e os testes afetados. O registro Git e a ativação têm comprovação operacional própria, sem alterar retroativamente a prova dos 941 testes.

Verificação complementar da manutenção: **26 testes direcionados** na Linode (18 de leitura HTTP e oito do ciclo de manutenção), sem falhas, erros ou testes ignorados. Somente `api.py` e `canonical_http.py` mudaram no backend desde a suíte completa anterior, além dos novos testes de manutenção; o navegador permaneceu inalterado. Veja [validacao-manutencao-cursores.json](validacao-manutencao-cursores.json). Não houve nova execução da suíte completa neste complemento.

## Enriquecimento canônico HTTP sintético

Recorte de pessoa/empresa por identidade de origem e documento brasileiro validado, com múltiplos itens, campos recursivos, datas por campo/flag, precisão exata, confirmação vinculada ao valor, autorização e recibo idempotente persistido na transação PostgreSQL/outbox. O painel de ensaio permite criar e acrescentar observações sob controle de versão. Escrita desabilitada por padrão; somente o fixture sintético a habilita nesta rodada. Contrato e pendências: [ESCRITAS-CANONICAS-HTTP.md](ESCRITAS-CANONICAS-HTTP.md). Resultados e hashes próprios: [validacao-escritas-canonicas.json](validacao-escritas-canonicas.json). O bloco não habilita produção, migração real, importadores/exportações canônicas ou deduplicação automática de contatos.

Validação específica em 09/09/2026 na Linode: **975 testes backend sem falhas, erros ou skips**, build, **14** verificações de precisão, **nove** de importação, **seis** de ordenação e **24 grupos de navegador**. Duas tentativas de navegador falharam em seletores (nome acessível e ambiguidade entre zero/UUID), foram corrigidas e preservadas. A última alteração afetou somente o teste de navegador; backend e aplicação mantêm os hashes da aprovação de 975 testes. A reexecução final do navegador passou com fontes estáveis. Ativação na Linode aprovada; reinício da origem e commit/publicação desta revisão ficam pendentes pelas restrições registradas na evidência, sem bloquear os próximos blocos sintéticos.

## Validação canônica dedicada de flags

Implementada rota PATCH de flags para pessoa/empresa, associada a uma observação escalar explícita, com origem/ator autorizados, metadados independentes, motivo de invalidação, versão e recibo idempotente na transação PostgreSQL/outbox. Nenhuma observação de valor é criada por uma validação. Campos e histórico do painel permitem escolher a evidência; vencimento aparece separadamente do resultado. Contrato: [VALIDACOES-CANONICAS-HTTP.md](VALIDACOES-CANONICAS-HTTP.md). Validação própria na Linode: **1.005 testes backend sem falhas, erros ou skips**, build, **14** verificações de precisão, **nove** de importação, **seis** de ordenação e **25 grupos no navegador**. A tentativa integrada falhou no nome acessível do novo seletor; somente o componente de validação mudou depois do backend aprovado, seguido de novo build/navegador aprovados com hashes estáveis. Veja [validacao-flags-canonicas.json](validacao-flags-canonicas.json). Linode ativada; ativação da origem e commit/publicação continuam pendentes pelas restrições do executor, sem bloquear o próximo bloco independente. Zero registros reais migrados.


## Edição escalar canônica direcionada

Implementada rota PATCH por pessoa/empresa, item existente e caminho escalar completo, inclusive folha recursiva, sem reapresentar identidade externa. Exige origem/ator autorizados, permissão enrich, versão e chave idempotente; acrescenta valor/histórico/outbox atomicamente, mantém outros campos e não transporta flags para valor diferente. Campos de documento não alteram o registro de identidade. O painel edita literal exato e permite recuperar resposta perdida por replay. Contrato e limites: [EDICAO-ESCALAR-CANONICA-HTTP.md](EDICAO-ESCALAR-CANONICA-HTTP.md). Validação na Linode: **1043 testes backend sem falhas, erros ou skips**, build, **14** verificações de precisão, **nove** de importação, **seis** de ordenação e **26 grupos no navegador**, com hashes estáveis em cada etapa. A tentativa integrada falhou na localização do rótulo da textarea preenchida; apenas o rótulo acessível do editor mudou, seguido de novo build/navegador aprovados e backend inalterado. Os **38 testes novos** também passaram em execução direcionada. Veja [validacao-edicao-escalar-canonica.json](validacao-edicao-escalar-canonica.json). Nenhum registro real migrado; não é entrega integral.


## Normalização canônica optativa de telefones

Contrato explícito por item, reutilizando as regras versionadas existentes. Preserva número recebido, contexto, decisão/regra/versão, candidatos e histórico; só transforma a observação do número. Componentes ausentes não recebem novas datas/origens. Múltiplos telefones permanecem separados, sem fusão de identidade ou confirmação inventada; flags continuam vinculadas ao valor correto. O PATCH escalar mantém o contrato literal e permite reversão por nova observação. Contrato: [NORMALIZACAO-TELEFONES-CANONICA.md](NORMALIZACAO-TELEFONES-CANONICA.md).

Validação proporcional na Linode: **451 testes backend, sem falhas, erros ou skips**, incluindo **40 novos**, build e **27 grupos de navegador** aprovados. A suíte backend completa e os scripts auxiliares não foram reexecutados. As tentativas anteriores estão preservadas: uma falha intermitente no fluxo existente de inteiro zero (causa não isolada), uma expectativa incorreta sobre reinício do formulário, um bloqueio transitório da porta antes de iniciar o navegador e um rótulo instável de textarea. Expectativa e rótulo foram corrigidos; build/navegador finais passaram com fontes estáveis e backend inalterado desde a aprovação. Evidência: [validacao-telefones-canonicos.json](validacao-telefones-canonicos.json). Linode ativada; origem e Git seguem com as restrições registradas. Demais normalizadores/catálogos, importadores/exportações, implantação definitiva, piloto, migração e carga continuam pendentes. Nenhum dado real migrado; não é entrega integral.


## Normalização canônica optativa de emails

Integrado contrato explícito de email no enriquecimento HTTP/painel de pessoa/empresa. Reutiliza a regra existente de domínio em minúsculas, preserva parte local, entrada, saída, regra/versão e literais de datas; formatos não reconhecidos ficam pendentes. Não aplica regras particulares de provedor nem confirma entregabilidade/titularidade. Múltiplos itens, componentes ausentes, origem e flags por valor continuam independentes. PATCH escalar permanece literal e permite revisão por nova observação. Contrato: [NORMALIZACAO-EMAILS-CANONICA.md](NORMALIZACAO-EMAILS-CANONICA.md).

Validação proporcional na Linode em 09/09/2026: **301 testes backend, sem falhas, erros ou skips**, incluindo **36 novos**, build e **28 grupos de navegador** aprovados na primeira execução, com fontes estáveis. Backend emitiu duas advertências de depreciação; suíte completa e scripts auxiliares não foram reexecutados. Prova cobre PostgreSQL sintético, pessoa/empresa, múltiplos emails, histórico, datas, autorização/fontes, triestado, eventos atrasados/futuros, conflito, rollback e replay após resposta perdida. Evidência: [validacao-emails-canonicos.json](validacao-emails-canonicos.json). Linode ativada e fixtures encerrados; origem e commit/publicação continuam pendentes pelas restrições do executor. Demais normalizadores/catálogos, integração definitiva, piloto, migração e carga permanecem pendentes. Nenhum registro real migrado; não é entrega integral.


## Normalização canônica optativa de CEP/código postal

Contrato explícito de endereço no enriquecimento HTTP/painel: BR precisa estar presente na operação, zeros e entrada original são preservados, códigos internacionais ou sem país permanecem literais para revisão. Apenas o CEP é transformado; componentes ausentes e datas/origens de outros campos permanecem independentes. Flags continuam associadas ao valor correto, e PATCH escalar permanece literal. Contrato e limites: [NORMALIZACAO-POSTAL-CANONICA.md](NORMALIZACAO-POSTAL-CANONICA.md). Validação proporcional na Linode: **348 testes backend, incluindo 47 novos, sem falhas, erros ou skips**, build e **29 grupos no navegador** aprovados. Dois avisos de depreciação no backend; suíte completa e scripts auxiliares não reexecutados. A revisão do teste de navegador, antes de sua execução, acrescentou a navegação à segunda página do histórico; backend permaneceu inalterado e build/navegador usaram os hashes finais. Evidência: [validacao-postal-canonica.json](validacao-postal-canonica.json). Não é entrega integral; nenhum dado real migrado.


## Usernames e plataformas canônicos

Contrato optativo no enriquecimento HTTP/painel de pessoa/empresa, reutilizando casefold apenas da plataforma. Username, URL e ID externo permanecem literais; contexto/datas/origem/regra/versão ficam no histórico de cada campo, sem confirmação inferida. Referências distintas mantêm contas distintas na mesma plataforma e em plataformas diferentes. O item aderente fica vinculado à plataforma, inclusive por observação atrasada/futura; conflitos exigem outra referência e revertem a operação. Edição escalar de identificadores permanece literal, preservando flags do valor antigo. Contrato: [NORMALIZACAO-USERNAMES-CANONICA.md](NORMALIZACAO-USERNAMES-CANONICA.md).

Validação proporcional na Linode: **401 testes backend (53 novos), zero falhas/erros/skips na execução final**, build e **30 grupos de navegador** aprovados, com fontes estáveis. Dois avisos de depreciação; suíte completa e scripts auxiliares não reexecutados. A primeira execução foi interrompida após duas falhas de expectativa na ordem de campos de objeto (71 aprovados); testes passaram a conferir caminhos/tipos. A proteção de plataforma recebeu também regressão para texto de motivo contendo o nome do contrato. Tentativa e correções preservadas. Prova: [validacao-usernames-canonicos.json](validacao-usernames-canonicos.json). Nenhum dado real migrado; banco definitivo, catálogos, integração de importadores/exportações, piloto, migração e carga seguem pendentes. Não é entrega integral.


## Campos adicionais canônicos tipados/versionados

Campos adicionais canônicos integrados em HTTP/painel: definição/versão explícitas, tipos e precisão, alternativas, histórico com snapshot/SHA256, referências PostgreSQL e proteção contra edição sem contrato. O catálogo administrativo continua local; cada observação carrega a definição aceita no snapshot de autorização, independente de renomeação/desativação posterior. Repetição confirmada precede nova validação do catálogo; desconhecidos permanecem pendentes. Referências resolvem apenas no PostgreSQL sintético, e itens livres antigos não são convertidos implicitamente. Contrato e limites: [CAMPOS-ADICIONAIS-CANONICOS.md](CAMPOS-ADICIONAIS-CANONICOS.md).

Validação proporcional na Linode: **487 testes backend (49 novos), build e 31 grupos de navegador aprovados**, zero falhas/erros/skips finais e fontes estáveis. Dois avisos de depreciação. A primeira execução backend passou em40 testes e falhou numa asserção nova que comparava o horário dinâmico de avaliação de vencimento; corrigida para conferir evidências persistidas e aplicabilidade das flags. A tentativa está preservada. A proteção de vínculo foi limitada à primeira observação do item antes da reexecução final. Suíte completa e scripts auxiliares não reexecutados. Evidência: [validacao-campos-canonicos.json](validacao-campos-canonicos.json). Catálogo PostgreSQL transacional, indexação, importadores/exportações, implantação definitiva, piloto, migração e carga/failover seguem pendentes. Nenhum dado real migrado; não é entrega integral.

## Catálogo PostgreSQL sintético transacional

Concluído o bloco incompleto recebido na transferência para a Linode. Definições
são criadas, renomeadas, inativadas/reativadas e consultadas por versão, com
recibo idempotente e histórico imutável. Administração exige sessão humana,
CSRF e OTP recente. O enriquecimento resolve e bloqueia a definição na mesma
transação PostgreSQL das observações/outbox; cada observação preserva snapshot,
SHA256, versão e identidade do catálogo. Os contratos SQLite e PostgreSQL
permanecem distintos, inclusive quando usam o mesmo ID. Contrato e limites:
[CATALOGO-POSTGRESQL-SINTETICO.md](CATALOGO-POSTGRESQL-SINTETICO.md).

Rodada `20260909T043846Z-bfcec853521b4c94b5a65f819a2c000b`: **406 regressões
backend aprovadas**, zero falhas/erros/skips e dois avisos de depreciação; build
TypeScript/Vite e **33 grupos no navegador** aprovados. Os **44 testes específicos
do catálogo**, aprovados na revisão da transferência, não foram repetidos:
backend, testes e DDL correspondentes foram reconferidos por hashes. Não houve
nova execução da suíte completa nem dos scripts auxiliares do frontend.
Evidência: [validacao-catalogo-postgresql.json](validacao-catalogo-postgresql.json).

Tentativas preservadas: colisão de nome fixo com definição de execução sintética
anterior, seletor de tipo sem nome acessível estável e asserção ambígua entre
carregamento/recibo. O teste usa nome exclusivo e seleciona o recibo pelo texto;
o componente recebeu rótulos acessíveis explícitos para tipo/escopo. Dois
reinícios foram recusados pelo guardião da porta antes de executar o navegador;
a disponibilidade foi reconferida sem encerrar processos alheios. O build final
acompanha a correção da interface; a última alteração foi apenas no seletor do
teste. Hashes do backend permaneceram estáveis e as fontes do navegador final
foram conferidas antes/depois. Schemas de teste removidos e porta 18767 encerrada.

A cópia direta do backup possui recibo SUCCESS/COMPLETE e comparação integral
aprovada, vinculados ao grant da transferência; isso não realiza migração.
Nenhum dado real migrado. Não houve ativação/reinício de serviço nesta rodada;
commit/publicação ficam pendentes porque `.git` está montado somente para leitura
no executor. Indexação de campos, demais catálogos, identidade/fusão/desfusão,
importadores/exportações canônicas, autenticação PostgreSQL, busca real, destino,
piloto/reconciliação/capacidade, deltas, migração e carga/failover seguem pendentes.
O próximo bloco independente é integrar campos adicionais à projeção e aos
filtros de busca canônica, sem anunciar indexação pronta antes de comprová-la.


## Integração da implantação na árvore principal — 09/09/2026

O delta de 20 arquivos da candidata validada foi combinado com a base de 154
arquivos, preservando alterações posteriores do executor automático. A única
sobreposição foi a interface do catálogo: o merge em três vias preservou os
rótulos acessíveis novos e a distinção entre ambiente sintético e implantado.
Nenhuma aplicação publicada, conta, senha, OTP ou configuração operacional foi
alterada por essa integração. Commit/publicação Git exigem revisão separada.

Verificação proporcional do merge: **30 testes de runtime sem falhas, erros
ou skips**, build TypeScript/Vite e **seis grupos de interface em navegador**
aprovados. Foram exercitadas as fronteiras entre o modo de desenvolvimento e o
implantado, sem tocar na aplicação publicada. Os oito testes PostgreSQL da
candidata não foram reexecutados: a evidência anterior é preservada e só se
aplica aos módulos de backend e à DDL com hashes idênticos. Não houve nova
suíte integral. Logs, patch e manifestos desta integração ficam fora do Git.
Busca, filas canônicas, carga integral, capacidade e recuperação continuam
pendentes; a presença do painel publicado não muda esses critérios de conclusão.

## Atualidade do andamento da migração — MAIN, 09/09/2026

Implementado e testado: expiração do relatório operacional a cada leitura da
health, limite de 120 segundos, tolerância de 30 segundos para relógio futuro,
validação de estruturas inválidas e preservação da última contagem. Painel
prioriza falhas/pausas sobre a fase e identifica medições desatualizadas sem
mostrar barra de progresso. Publicação nova recupera o estado. Contrato em
[DEPLOYMENT-RUNTIME.md](DEPLOYMENT-RUNTIME.md).

**55 testes de backend**, build TypeScript/Vite e **oito grupos de navegador**
com API simulada aprovados; evidência em
[validacao-runtime-freshness.json](validacao-runtime-freshness.json).
Não houve ensaio com dados pessoais nem nova suíte integral/ensaio PostgreSQL.
A release publicada e o piloto permanecem separados da MAIN; ativação desta
correção exige promoção pelo coordenador. A observabilidade completa, busca e
XLSX canônicos, migração integral, carga de 50 req/s e recuperação permanecem
pendentes. O caso Cloudflare/Python-urllib informado pelo operador continua sem
homologação ou causa comprovada nesta revisão.


## Recuperação dos campos adicionais na busca — MAIN, 09/09/2026

Retomado o trabalho parcial da rodada interrompida. Projeção versionada e filtros
ligam cada valor ao ID, versão, SHA256, tipo e implantação de sua definição
histórica. Igualdade preserva null/false/zero e números exatos; filtros de valor,
fonte e flags permanecem correlacionados. Metadados incompletos ou corrompidos
interrompem a publicação sem confirmar o outbox. Campos sem definição continuam
contabilizados como não indexados. Contrato: [BUSCA-CAMPOS-CANONICOS.md](BUSCA-CAMPOS-CANONICOS.md).

**151 testes sintéticos de contrato/projeção/PIT aprovados**, sem falhas, erros
ou skips, e build TypeScript/Vite aprovado. A execução PostgreSQL foi interrompida
sem resultado validado: o fixture não estava acessível e seu preparador recusou
a propriedade do runtime no sandbox. A proteção foi preservada. Integração
catálogo/HTTP/PostgreSQL, navegador com banco e Elasticsearch real ainda precisam
de prova própria; não são cobertos pelos 151 testes. Evidência:
[validacao-busca-campos-canonicos.json](validacao-busca-campos-canonicos.json).

Busca ampla continua desativada na release publicada. Catálogo de disponibilidade
por versão, seleção visual, intervalos numéricos, referências pesquisáveis e
ordenação múltipla permanecem pendentes. O piloto real foi interrompido com 6.910
entradas conferidas; não houve nova importação nesta rodada. Migração integral,
deltas, capacidade com margem, XLSX canônico, 50 req/s e recuperação continuam
não homologados. A correção de freshness já foi promovida pelo coordenador,
conforme atualização operacional; não repetir essa promoção pela nota histórica.

## Candidata de busca e estado operacional — 09/09/2026, 12:38 UTC

A candidata integra o leitor canônico autenticado ao painel e à API, com
filtros AND/OR, correlação no mesmo item, flags true/false/null e paginação
protegida. Corrige a primeira consulta após login e rótulos acessíveis dos
filtros. Evidências: 311 testes de backend, 12 testes PostgreSQL com dados
fictícios em banco separado, build e 12 grupos de navegador; execuções finais
sem falhas. O contrato está em [DEPLOYMENT-SEARCH.md](DEPLOYMENT-SEARCH.md).

A implantação anterior permanece ativa até a conferência da projeção real.
A fila está sendo processada em lotes e será comparada integralmente por
identidade, versão e conteúdo. Sua conclusão cobre somente as 16.910 entidades
já canônicas. Campos preservados no banco mas ainda sem definição pesquisável
continuam identificados como omissões da projeção; isso não autoriza afirmar
que todos os campos já são pesquisáveis.

O modelo relacional atual do piloto ainda não tem custo ou vazão adequados
para 476,4 milhões de entradas. Protótipos de codificação compacta preservam
os átomos e metadados em testes, mas não substituem medição do banco, índices,
WAL e buscas. A carga integral requer corrigir esse desenho e comprovar
capacidade em amostra maior. XLSX canônico em massa, ordenações múltiplas,
catálogos completos, fusão/desfusão, autenticação multiprocesso, recuperação
externa e teste de 50 req/s permanecem obrigatórios e pendentes.

Os blocos anteriores desta matriz são registros históricos das respectivas
rodadas. As menções à interrupção com 6.910 entradas descrevem a tentativa
anterior, não o estado do novo piloto concluído.

### Regressão integral da candidata de busca

Em 09/09/2026, a suíte completa da candidata passou com **1.540 testes**,
zero falhas, erros ou testes ignorados, em 248,17 segundos. Os ensaios usaram
PostgreSQL 18.6 por socket Unix privado e banco fictício, o banco separado de
validação da implantação e Redis isolado. A regressão não altera os cadastros
reais nem comprova capacidade de 50 req/s. O build e os 12 grupos de navegador
aprovados anteriormente continuam vinculados ao mesmo código da candidata.

### Fila canônica de exportações — extensão em desenvolvimento

A extensão `canonical_export_jobs.py` e a DDL `010_canonical_export_jobs.sql`
implementam pedidos persistentes, limites de fila, isolamento por solicitante,
idempotência, leases, checkpoints, cancelamento, expiração e recibos dos arquivos.
**13 testes passaram com PostgreSQL e dados fictícios**, incluindo retomada,
revogação entre execução e commit e preservação da história. A extensão não está
instalada no banco cadastral nem exposta por HTTP. Seleção dos resultados,
materialização consistente, geração/verificação do XLSX em fluxo e integração
ao painel/API continuam pendentes; não declarar exportação canônica pronta.

### Publicação da busca — 09/09/2026, 13:02 UTC

A candidata de busca foi publicada após comparar as **16.910 entidades** do
corte canônico por ID, versão e conteúdo, com zero faltantes, extras ou
divergências. Os dois domínios confirmaram `search_enabled=true`; as rotas
protegidas sem credenciais retornaram 401. A configuração manteve o mesmo
armazenamento administrativo, contas e OTP. O consumidor periódico foi retomado
para refletir os próximos enriquecimentos no índice.

O índice mantém 142.634 campos adicionais desse corte fora da busca por ainda
não possuírem definição pesquisável. Seus valores e metadados estão preservados
no PostgreSQL. As demais categorias de omissão do projetor foram zero.
A leitura desses dados pela ficha continua separada da disponibilidade de filtros.
A aplicação informa cobertura parcial; XLSX canônico e migração integral
permanecem desabilitados/não concluídos. O piloto canônico ocupa aproximadamente
2,5 GB no PostgreSQL e 221 MB de índice, antes de translog, WAL e backups.
Esses custos motivam a revisão de armazenamento/importação antes da carga maior.
