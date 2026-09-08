# Matriz de implementação e validação

Atualizada em 08/09/2026. Restauração do backup original aprovada em ambiente isolado; provas operacionais preservadas fora do Git. A migração real ainda exige servidor de destino, integração do banco definitivo e piloto. **Não é entrega integral e os testes sintéticos abaixo não homologam a carga real.**

| Requisito | Implementação verificável | Evidência / restante |
|---|---|---|
| Pessoas e empresas, múltiplos itens | API e painel; identidade, documentos, contatos, endereços, usernames, atividades e campos adicionais | Integração local e navegador; catálogo completo empresarial/estabelecimentos ainda pendente |
| Origem e história por campo e flag | Entrada original inclusive null/false/zero, valor anterior, fonte, data da fonte, observação, recepção, ator e operação | `test_flows.py`: idempotência, eventos atrasados, datas ausentes/futuras e concorrência; edição recursiva independente de objetos arbitrários pendente |
| Confirmações | true/false/null; validade, WhatsApp, titularidade, entregabilidade e residência | Métodos, referências, vencimento e histórico; evidências de valores atrasados não confirmam o valor atual; vencimento não fabrica false nem altera outra flag |
| Normalização | CPF, CNPJ numérico/alfanumérico, telefone, email, CEP | Vetor alfanumérico oficial, zeros, regra histórica Anatel 8/9 nos 67 DDDs, conflitos/ramais/rádio e precisão numérica; revisão operacional de normalização e piloto real ainda pendentes |
| Deduplicação | Itens normalizados, documento consistente e ID por fonte | Conflitos bloqueados; observação futura não associa identidade; fusão/desfusão e registro definitivo de titularidade pendentes |
| Vínculos | Pessoa/pessoa, pessoa/empresa, empresa/empresa, identificação pendente e relação inversa derivada | API + aba Vínculos; teste impede autocorrelação e confirma que relação inversa preserva evento original |
| Pesquisa | AND/OR recursivo, mesmo item, idade/CEP, ordenação múltipla local e inclusão explícita de invalidados | Backend + navegador sintético: prioridades, min/max, ausentes, datas, Unicode, legado e desempates; leitor canônico PIT com cursor protegido implementado e testado por HTTP simulado; ligação à API, ordenação múltipla no canônico e índices reais pendentes |
| Pesquisas salvas | Critérios, tipo, ordenação múltipla e inclusão de invalidados por usuário; arquivamento preserva critérios e pesquisas antigas mantêm contrato legado | API + navegador: salvar, reabrir, executar e exportar com os mesmos critérios; conversão do legado exige edição explícita |
| Consulta em massa | Texto, listas CSV/XLSX, filtros e ordenação compartilhada; fila, percentual, cancelamento e download autenticado | Navegador e API: IDs na mesma ordem da pesquisa/XLSX, inclusive divisão em volumes e retomada no corte local |
| XLSX completo | Colunas escalares, abas relacionadas, estado, história, confirmações, entradas e correspondências; fragmentos de texto e valores estruturados | Reabertura, contagem e conteúdo numérico/booleano; números inseguros como texto exato; definições versionadas até o corte; divisões por linhas/colunas e volumes ZIP com hashes testadas |
| Processamento de exportação | Estado persistido, corte local consistente, reinício de pendentes; arquivos só disponíveis após verificação | Checkpoints incrementais, streaming em escala, limite de memória por trabalho ainda pendentes; limpeza de arquivos expirados implementada/testada, sem remover dados cadastrais |
| Importação assíncrona | JSON/JSONL pelo painel e API; resultados por entrada, checkpoint atômico, cancelamento e retomada | 1.000 entradas/2 MiB no adaptador local; transporte JSONL/PIT separado para canônico, limite de expansão, URL/UUID fixados e retomada testados; integração dos importadores HTTP ao canônico e sistemas reais pendentes |
| Convite e OTP | Convite de 24 h, senha própria, primeiro OTP obrigatório, Argon2id, TOTP cifrado, replay bloqueado | Backend e navegador; convite só funciona uma vez e usuário normal não vê administração |
| Recuperação e revogação | Códigos de recuperação, novo OTP, revogação de sessões/chaves; desabilitar usuário revoga acessos; TOTP recente por sessão para administrar usuários | Step-up incluído na rodada integrada aprovada; conta administrativa existente preservada; redefinição administrativa e autenticação reforçada nos outros catálogos pendentes |
| Administração | Usuários, convites, fontes, campos, API keys e auditoria | Campos tipados/versionados com validação estrita, alternativas, escopo e inativação preservados; edição granular de permissões, quotas e outros catálogos ainda pendentes |
| Chaves API | Hash, escopos, origem permitida, expiração, revogação e OTP na emissão | Rotação com OTP recente, período de transição, recuperação idempotente de resposta e revogação de toda a cadeia implementadas e testadas em backend/navegador; políticas administrativas completas pendentes |
| Rate limit | Adaptador local ou Redis opcional, cota compartilhada por usuário/chave, IP e conta; falha Redis retorna 503 | Integração com Redis real isolado, atomicidade com dois clientes e falha sem liberação testadas; failover, quotas por custo/admin e teste distribuído prolongado pendentes |
| Interface/PWA | Português, tema, desktop/móvel, manifesto, service worker apenas estático | Navegador confirma fluxos e ausência de API no cache; instalação em dispositivos, atualização controlada, auditoria completa de acessibilidade e virtualização pendentes |
| Migração | Adaptadores puros para os140 caminhos inventariados das duas fontes; átomos/containers, normalização conservadora e semântica desconhecida preservada; leitor JSONL/PIT; CLI com guardião de destino/capacidade; piloto limitado | Novos contratos em `ADAPTADORES-MIGRACAO.md`, `EXECUTOR-MIGRACAO.md` e `PREFLIGHT-MIGRACAO.md`; pipeline exercitado no PostgreSQL sintético. Conferência por releitura do destino implementada e testada; finalização depende de prova vinculada ao destino/fonte/versões. Nenhum registro real migrado; resolução de conflitos/coleções codificadas/registros grandes, piloto representativo e capacidade real pendentes |
| Banco definitivo / outbox / busca | Repositório PostgreSQL18 com64 partições, operações/observações imutáveis, identidade documental, outbox/leases e checkpoint transacional; projeção ES versionada com filtros correlacionados; paginação do histórico por corte de versão | Testes PG reais com dados fictícios e testes HTTP simulados da busca; contratos em `CANONICAL-STORE.md` e `PROJECAO-BUSCA.md`. API/painel continuam no SQLite local; integração dos serviços ao canônico, ES real, papéis de produção, esquema de atualizações e homologação de escala ainda pendentes |
| Backup/restauração | Backup original e restauração isolada aprovados | Manifestos, contagens, mapeamentos, consultas e configuração conferidos; provas no pacote privado. O novo estado administrativo/projeto requer cópia complementar conforme RETOMADA.md |
| Desempenho e disponibilidade | Metas no plano | 50 req/s, 1 h/8 h, replicação, failover, RPO/RTO e reconciliação integral não medidos |

## Evidências e limites

A rodada integrada de 08/09/2026 passou com **896 testes de backend, 14 testes de precisão, nove de entrada de importação, seis de compatibilidade de ordenação e 22 grupos no navegador**, além do build TypeScript/Vite. Nenhum teste backend foi ignorado. Veja [VALIDACAO.md](VALIDACAO.md) e o manifesto público de hashes. O código permaneceu igual antes/depois dos testes. Logs completos, tentativas com falha, capturas e arquivos gerados permanecem no pacote privado.

PostgreSQL 18.6 real isolado, com dados fictícios e socket Unix privado, validou persistência, paginação, reconciliação e migração sintética. A busca Elasticsearch usa testes HTTP simulados e ainda precisa de integração com cluster real no novo ambiente. Redis foi exercitado em processo isolado; isso não transforma o adaptador HTTP SQLite em banco definitivo ou permite múltiplos processos de produção.

Nenhuma migração real ou alteração dos índices atuais foi executada. Publicação no Git, aprovação dos testes e ativação do painel local não equivalem a homologação de produção.

## Próximos blocos obrigatórios

1. Completar indexação dos campos administráveis, rastreabilidade recursiva, revisão de identidade/fusão/desfusão, mapeamento/importação dos sistemas reais e revisão operacional da normalização. Definições tipadas/versionadas e regra histórica conservadora já estão implementadas.
2. Integrar PostgreSQL/outbox, projeções de busca, filas com checkpoints, cursor e respostas/exportações em fluxo no novo ambiente.
3. Validar instalação/atualização PWA, acessibilidade, isolamento completo, autenticação reforçada e operação administrativa.
4. Após backup validado e destino disponível: piloto, reconciliação por campo, dimensionamento, migração e carga/falhas/recuperação antes de troca de tráfego.

O plano completo continua obrigatório; esta matriz registra o estado real, sem reclassificar itens obrigatórios pendentes como opcionais.

## Transferência em preparação

O código será mantido no repositório indicado pelo proprietário. O novo servidor está pendente de disponibilização. Seguir [RETOMADA.md](RETOMADA.md), [INFRAESTRUTURA.md](INFRAESTRUTURA.md) e o [plano completo](PLANO-PROJETO-COMPLETO.md). Ao concluir todos os requisitos e a migração, lembrar o proprietário de tornar o repositório privado.

A meta inicial de capacidade foi revisada pelo proprietário para **50 requisições por segundo**, sem reduzir preservação de dados, testes de falha, rastreabilidade ou completude. O perfil inicial econômico está em INFRAESTRUTURA.md; a capacidade final permanece dependente do piloto. Isso atualiza o planejamento, não declara um novo limite já homologado no serviço em execução.
