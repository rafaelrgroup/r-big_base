# BIG BASE — plano de implementação do cadastro unificado

Versão pública de planejamento: 08/09/2026. Este documento define os requisitos completos; o estado comprovado está em [IMPLEMENTACAO.md](IMPLEMENTACAO.md) e [VALIDACAO.md](VALIDACAO.md). A restauração do backup original foi validada em ambiente isolado. A migração real depende do novo destino, da integração e homologação do banco definitivo e do piloto representativo. Manifestos e evidências operacionais pertencem ao pacote privado de retomada, fora deste repositório.

## 1. Resultado e decisões fechadas

Entregar um cadastro unificado de pessoas e empresas, consultável por painel responsivo instalável e APIs, com múltiplos contatos, documentos, endereços, usernames e relações. Cada valor tem origem, datas, validação e histórico de alterações. Não exigir armazenamento permanente de cópias integrais dos cadastros de origem. Nunca apagar informação cadastral em uma invalidação, correção ou deduplicação.

Decisões do usuário:

- Infraestrutura nova, preservando o servidor atual durante a transição.
- Meta inicial de teste: 50 requisições por segundo (revisada pelo proprietário em 08/09/2026), considerando uma mistura definida de consultas e atualizações.
- Administrador cria usuários e define permissões individuais.
- TOTP compatível com Google Authenticator obrigatório para administradores e usuários no painel, incluindo configuração no primeiro acesso.
- Usuário quer a observação mais recente prevalecendo em conflitos, com histórico.
- `is_whatsapp` aceita true, false e null para desconhecido.
- Resultados paginados com cadastros completos e exportação assíncrona do conjunto inteiro.
- Consulta em massa pelo painel e API, por listas de documentos/nomes ou filtros combinados, com fila, percentual de progresso e entrega completa em XLSX, conforme seção 7.1.
- Aplicação web responsiva e instalável (PWA); aplicativos nativos não fazem parte da primeira entrega.
- Motor e distribuição dos índices delegados ao projeto, priorizando consultas rápidas.

## 2. Arquitetura escolhida

**PostgreSQL é a única fonte oficial de dados. Elasticsearch acelera a busca e pode ser integralmente reconstruído. Redis coordena limites e filas transitórias.** Usuários e integrações escrevem somente pela API oficial; não existem cadastros concorrentes atualizados independentemente em motores diferentes.

Fluxo: painel/integração → API autenticada → transação PostgreSQL (estado atual + histórico + evento de publicação) → workers → projeções Elasticsearch. Consultas exatas por identidade usam lookup indexado; consultas combinadas usam Elasticsearch para encontrar IDs e carregam os cadastros correspondentes em lote no PostgreSQL, respeitando a ordem encontrada.

Stack de referência: Python com FastAPI, Pydantic e PostgreSQL. O repositório canônico atual usa psycopg 3 e DDL versionado; a integração HTTP, o processo de atualização de schema e os pools precisam ser homologados. SQLAlchemy/Alembic são opções de implementação, não dependências já integradas; PostgreSQL 18; Elasticsearch 9.5.3 como base inicial compatível com a restauração atual; Redis para rate limit e coordenação; React + TypeScript + Vite no painel, com componentes acessíveis; Tailwind e TanStack Query/Table eram opções de planejamento e não são requisitos de biblioteca nem módulos já integrados. Fixar versões exatas das dependências e imagens em lockfiles/digests na preparação da entrega. Não atualizar versões automaticamente em produção.

API stateless em duas ou mais instâncias; workers separados para ingestão, publicação da busca e exportação. Publicação pelo padrão outbox transacional: gravar evento na mesma transação dos dados, processar com confirmação e repetição idempotente. Redis não é a única cópia de trabalhos aceitos; estado e checkpoints persistem no PostgreSQL.

PostgreSQL primário com réplica síncrona para durabilidade das escritas confirmadas e backup contínuo/WAL fora da máquina. Failover controlado, com proteção contra dois primários. Elasticsearch com pelo menos três nós, réplica dos shards em outro nó e distribuição por domínios de falha quando disponíveis. Redis sem descarte de chaves de rate limit e com réplica/failover. Rede privada entre serviços; apenas a entrada HTTPS da aplicação é pública.

A réplica PostgreSQL não é usada para leituras que exijam retorno imediato após escrita. A busca é eventualmente consistente: resposta informa `record_version` e `indexed_version`; alvo inicial de atualização da busca é até 5 segundos p95. Recuperação de atrasos e reconstrução acontecem sem perder o cadastro oficial.

## 3. Modelo de pessoas, empresas e dados associados

Identificadores internos estáveis e opacos, expostos como UUIDs. Documentos não são chaves primárias internas, pois podem estar ausentes, conter erros ou mudar de formato.

| Grupo | Dados principais |
|---|---|
| Pessoas | Nomes e alternativas, nascimento, sexo informado, nacionalidade, documentos e outros atributos importados |
| Documentos | Tipo, valor textual, normalização, país, órgão/UF, regra de validação e sua versão |
| Empresas | CNPJ completo, raiz quando derivável, razão social, nomes fantasia, situação cadastral, natureza jurídica, abertura, porte e campos recebidos |
| Estabelecimentos | Matriz/filial identificadas pelo CNPJ completo e vinculadas à empresa/grupo quando essa relação for conhecida |
| Atividades | CNAE principal e secundários como códigos textuais, descrições oficiais versionadas e múltiplas áreas/tags de atuação |
| Telefones | Número, país/DDD, classificação técnica, uso informado, vínculo com pessoa/empresa, validação e WhatsApp |
| Emails | Email, domínio, validação sintática, entregabilidade quando informada e vínculo com titular |
| Endereços | País, UF, município/código, bairro, tipo/logradouro, número, complemento e código postal |
| Usernames | Plataforma, username, URL/ID externo opcionais, pessoa/empresa, origem, validade e datas |
| Relações pessoais | Pessoa de origem/destino, tipo, direção, datas, hipótese/validação e fonte |
| Relações empresariais | Pessoa–empresa ou empresa–empresa, papel, função/cargo, participação quando conhecida, período e validade |
| Campos adicionais | Definição tipada, valores simples/múltiplos, metadados e histórico |

Plataformas iniciais de username: WhatsApp, Instagram, Facebook, X/Twitter, Telegram, TikTok, LinkedIn e outra cadastrada pelo admin. Um mesmo texto em plataformas diferentes é tratado como contas diferentes. Vários usernames por pessoa e plataforma são permitidos. Conta com username não implica propriedade confirmada nem WhatsApp confirmado no telefone.

Relações pessoais iniciais: mãe, pai, filho/filha, irmão/irmã, cônjuge, responsável e possível parente. Pessoa A → B com tipo mãe significa “B é mãe de A”. Relações inversas são derivadas da mesma informação, sem inventar outra fonte. Alvo conhecido apenas por documento/nome pode ficar pendente de resolução, claramente identificado.

Papéis empresariais iniciais: sócio, administrador, representante, empregado, prestador, procurador, contato e outro definido pelo admin. A pessoa pode exercer vários papéis em uma empresa, em períodos distintos. Não inferir emprego atual apenas porque existiu vínculo antigo.

Dados pessoais não são associados automaticamente a empresas por coincidência de nome ou telefone. CPF e CNPJ não compartilham o mesmo espaço de identidade. Endereços e contatos compartilhados não fundem identidades.

## 4. Origem, estado atual e histórico por informação

Cada item possui `item_id`, `owner_id`, tipo, valor normalizado/componentes, `created_at`, `updated_at`, `version`, estado de validação e as origens que o informaram. Cada origem registra `source_id`, data da fonte/observação, primeira e última recepção e chave externa opcional. Um mesmo valor informado por três fontes aparece uma vez, com três origens.

Mesmo nome, nascimento, sexo, documentos e componentes de endereço têm metadados próprios. Alterar só o CEP não atribui a nova origem à rua, cidade e número. O sistema guarda o valor recebido antes de uma transformação no histórico daquele campo, sem conservar uma cópia integral do cadastro de origem.

Dimensões separadas:

- Validação cadastral: `valid = true | false | null`, com `checked_at`, origem/agente e motivo.
- Estado operacional: atual, alternativo ou substituído. Invalidado continua acessível e identificado.
- Verificação sintática: válida, inválida, ambígua ou regra ainda não suportada. Uma formatação correta não comprova a realidade do dado.
- Telefone: `is_whatsapp = true | false | null`, `whatsapp_checked_at`, `whatsapp_checked_by` e método/referência opcionais.

`is_whatsapp=false` é um resultado negativo explícito; null é desconhecido. A existência de WhatsApp no número não confirma sua ligação à pessoa. Mudança do número invalida a aplicabilidade da confirmação anterior: ela fica vinculada ao valor antigo no histórico. Verificação de endereço existente, residência da pessoa, email entregável e titularidade do email também são distinções explícitas.

**Precedência escolhida: observação mais recente.** Usar `source_updated_at`, senão `observed_at`, como data efetiva do fato. Sem nenhuma dessas datas, a observação entra no cadastro e no histórico, mas não sobrepõe automaticamente um valor já datado. Para empates, usar recepção do servidor e ID do evento como desempate estável. Registrar a regra aplicada. Datas futuras acima de cinco minutos entram em pendência, evitando que relógio incorreto domine permanentemente o campo. Operador pode registrar correção com data atual e motivo.

Precedência é por campo e por propriedade de validação: nova informação de nome não modifica validade de telefone; nova observação de um telefone não desfaz invalidação posterior. Atualização que altera o valor não herda a confirmação do valor anterior. Não fabricar confirmação apenas por duplicidade entre fontes.

Toda mutação grava estado anterior, novo estado, ator, chave de API quando aplicável, origem, horários, motivo e identificador da operação. Estado corrente, histórico e outbox são uma transação. Histórico cadastral não tem exclusão automática. Desativar um campo ou invalidar um item não remove seus valores anteriores.

## 5. Documentos e normalização de telefones

Identificadores são armazenados como texto, nunca como inteiro. Validadores são módulos versionados por tipo/país, separados do armazenamento, para aceitar novos formatos sem perder entradas que ainda não sejam compreendidas.

CPF brasileiro: regra atual de 11 dígitos e verificadores; normalização preserva zeros. CNPJ: aceitar formato numérico existente e novo formato alfanumérico, sem remover letras. A Receita mantém o CNPJ alfanumérico com 14 posições; implementar o cálculo oficial de verificação e testes oficiais. Tipos novos ou futuros ficam registrados como pendentes se ainda não houver validador. Não inventar hoje uma regra de comprimento futuro. [Receita Federal](https://www.gov.br/receitafederal/pt-br/acesso-a-informacao/acoes-e-programas/programas-e-atividades/cnpj-alfanumerico).

Telefones: manter `input_value`, valor canônico quando resolvido, país, DDD, ramal separado, versão de normalização e decisão. Normalização internacional baseada em libphonenumber, com regras históricas brasileiras complementares e versionadas.

Classificação técnica: celular, fixo, outro ou desconhecido. Uso: residencial, comercial ou desconhecido. Um prefixo de telefone fixo não prova que seja residencial; isso será identificado separadamente no painel. Números especiais, rádio e internacionais não serão forçados às duas categorias brasileiras.

Acrescentar o nono dígito somente quando: país Brasil, DDD conhecido e válido, número local de oito dígitos e evidência histórica/tabela de prefixos de que se trata de celular legado. Prefixos ambíguos e falta de DDD vão para revisão. Não acrescentar 9 a fixos ou números já normalizados; não remover prefixos internacionais sem contexto. Toda transformação registra entrada, saída e regra, permitindo reversão. [Anatel](https://www.gov.br/anatel/pt-br/regulado/numeracao/perguntas-frequentes).

Formato E.164 é uma representação canônica dos números que se enquadram no padrão atual; o armazenamento de entrada e a API não ficam limitados à quantidade brasileira de dígitos. Letras em entrada não são descartadas cegamente: números alfabéticos e formatos não reconhecidos ficam preservados para tratamento específico. Validação de formato não comprova linha ativa, titularidade ou WhatsApp. [libphonenumber](https://github.com/google/libphonenumber/blob/master/FAQ.md).

Emails preservam o valor recebido; normalizam domínio sem aplicar regras particulares de Gmail ou remover caracteres de todos os provedores. Nomes preservam acentos e apresentação, com versões normalizadas específicas para busca. CEP brasileiro permanece texto com oito posições; código postal estrangeiro é textual. Datas ambíguas não são convertidas silenciosamente.

## 6. Deduplicação e distribuição para bilhões de itens

Deduplicar pessoa por documento validado e consistente; conflitos de atribuição vão para pendência. Similaridade de nome, nascimento, parentes ou endereço gera candidatos, não fusão automática. Empresas usam CNPJ completo, mantendo distinção matriz/filial. Fusões são eventos reversíveis e mantêm os IDs antigos como referências/redirecionamentos.

Dentro da pessoa/empresa, a chave de deduplicação de contato inclui o valor normalizado e seu contexto; não pressupõe titular exclusivo. Endereços diferentes em número/complemento permanecem distintos. Relações incluem destino, tipo e período. Reprocessamento usa chave de origem + registro/operação + versão/hash + transformação, impedindo novas duplicações a cada carga.

No PostgreSQL, grandes tabelas de valores/vínculos/histórico particionadas inicialmente em 64 partições por hash do proprietário. Usar chaves compostas que incluam o particionamento para garantir unicidade e integridade. Documentos têm registro de identidade separado, particionado pelo hash da chave completa do documento, para localizar proprietário sem consultar todas as partições. Vínculos têm índices por origem e destino. Catálogos de usuários, fontes e campos permanecem menores e não exigem esse particionamento.

No Elasticsearch, documentos compactos por pessoa/empresa, contendo apenas dados necessários a busca/ordenação, flags relevantes e versão. Histórico completo não entra nesses documentos. Arrays simples para correspondência independente; objetos `nested` compactos quando os filtros precisam combinar propriedades do mesmo telefone/endereço/relação. Não cruzar cidade de um endereço com CEP de outro nem WhatsApp de um telefone com o número de outro.

Dez telefones por pessoa podem produzir bilhões de registros associados e, com nested, bilhões de documentos internos de busca. O piloto deve medir esse multiplicador, bytes por entidade e atualização de perfis com muitos contatos. Nenhuma estimativa de disco será feita usando apenas a contagem de pessoas. [Nested](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/nested).

Distribuir por hash de ID estável, não por sexo, primeira letra do nome ou endereço mutável. A proposta de faixas não será aplicada genericamente: uma pessoa tem vários endereços e pesquisas cruzadas podem atingir todas as faixas. Filtros usam índices; partições/shards distribuem armazenamento e processamento, sem prometer eliminar todo o trabalho da consulta.

Definir número de shards antes da carga completa pela medição do piloto, visando 30–50 GiB por shard e menos de 100 milhões de documentos internos por shard, incluindo nested. Calcular o maior número exigido por tamanho, documentos e distribuição entre nós. Uma réplica por shard. Aliases versionados permitem reconstrução e troca controlada. [Dimensionamento Elastic](https://www.elastic.co/docs/deploy-manage/production-guidance/optimize-performance/size-shards).

## 7. Pesquisa, filtros, ordenação e retorno completo

Busca exata por documento, telefone, email e username; nome completo, prefixo e parcial; email completo ou parcial; combinações por sexo informado, nascimento/idade, localidade, CEP/faixa, fontes, validade, WhatsApp, plataforma, CNAE e relações. Filtros suportam AND/OR, múltiplos valores e ausência/presença. Não expor SQL ou DSL Elasticsearch arbitrários ao cliente.

Exemplo obrigatório de aceitação: pessoas com CEP entre dois limites brasileiros, sexo escolhido, idade entre limites inclusivos e nome começando com A. Converter idade em intervalo de nascimento na data de referência, com testes de aniversário e 29 de fevereiro; não gravar idade como valor que fica desatualizado. CEP em faixa deve ter mesmo país e comprimento, preservando zeros; não atribuir ordenação numérica a código postal estrangeiro.

Nome por prefixo usa campo adequado; parcial/contains usa índice próprio, sem varredura por wildcard irrestrito. Um caractere é permitido em prefixo com filtros adicionais seletivos, como no exemplo do usuário; busca parcial livre exige três caracteres. Queries excessivamente amplas podem ser executadas como trabalho assíncrono, com o mesmo critério e sem corte silencioso de resultados.

Ordenação por relevância, nome, nascimento, atualização, campos cadastrais e campos adicionais indexados, sempre com ID como desempate. Para campos múltiplos, parâmetro explícito min/max; padrão min crescente e max decrescente, documentado. Ordenação só usa campos permitidos e indexados; status de preparação aparece no painel.

Página padrão: 50 cadastros, máximo 100. Cursor opaco, validado e vinculado à consulta/permissões; Elasticsearch com point-in-time e search_after para ordenação consistente. Cursor expira em 15 minutos, prorrogado pelo uso até limite de uma hora. Mudança de filtros invalida o cursor. Não usar paginação profunda por offset.

Cada resultado pode retornar a ficha completa autorizada: campos principais, alternativas, documentos, contatos, endereços, usernames, relações, campos adicionais, origens e estados, inclusive informações invalidadas identificadas. A seleção de quem atende à busca usa por padrão dados não invalidados. Históricos ou coleções muito grandes têm contagem, indicador de continuação e endpoint/cursor explícitos; nenhum truncamento é apresentado como completo.

Limite de 10 MiB para resposta síncrona: se ultrapassado, fornecer trabalho de download completo em vez de falhar sem explicação ou omitir dados. Exportação NDJSON/JSON preserva todas as coleções e metadados. XLSX é entrega obrigatória da consulta em massa, com todas as coleções em abas relacionadas conforme seção 7.1. CSV é formato tabular complementar, acompanhado de tabelas/arquivos dos itens múltiplos; não é a única forma de exportação completa.

Exportações definem instante/versão de corte. Workers materializam seleção e versões necessárias em lotes a partir de snapshot consistente, usando histórico para preservar valores do corte. Manifesto contém critérios, contagens, versões e hashes dos arquivos. Progresso, cancelamento e retomada são disponíveis. Artefatos temporários expiram em sete dias; os dados do cadastro não são apagados. Exportação completa não inclui segredos de autenticação do sistema.

### 7.1. Consulta em massa com entrega completa em XLSX

Requisito incluído em 08/09/2026: painel e API aceitam listas de informações, filtros ou ambos, processam todos os resultados em segundo plano e entregam XLSX completo. A consulta não modifica cadastros. Compartilha o motor de seleção, o corte consistente, as permissões e os trabalhos de exportação da seção 7, sem resultados divergentes da pesquisa interativa.

**Entradas e filtros.** No painel, permitir colar documentos/nomes separados por vírgula ou quebra de linha, além de importar CSV/XLSX de entradas. Prévia mostra quantidade, tipo, correspondência e erros. Interpretar aspas CSV para nomes que contenham vírgula; não dividir nomes por espaços. Na API, aceitar entradas estruturadas com `input_id`, tipo, país/tipo de documento quando aplicável e valor, ou texto delimitado com tipo declarado. Não inferir que todo número é CPF. Identificar entradas vazias, inválidas, repetidas e não encontradas.

Permitir também os demais identificadores pesquisáveis: telefone, email e username/plataforma. Nome completo, prefixo e parcial são modos explícitos; homônimos retornam todas as correspondências, sem escolher arbitrariamente uma pessoa. Cada entrada pode ter filtros próprios. A lista representa a união das correspondências; filtros gerais restringem essa união. Dentro de uma entrada, seu critério e seus filtros se combinam com AND. Árvores AND/OR explícitas permitem outras combinações. Consultas somente por filtros também são aceitas. Pedido completamente vazio é rejeitado, exigindo seleção explícita de todos os registros para exportação integral autorizada.

Disponibilizar todos os campos e operadores do catálogo de busca, inclusive campos adicionais indexados, cidade/rua/CEP, documentos, sexo/nascimento, contatos, WhatsApp, validade, fontes, CNAE e relações. Painel e API usam o mesmo catálogo versionado. Campo sem índice pronto ou sem permissão gera erro claro, nunca é ignorado. Preservar filtros sobre o mesmo endereço/telefone. O filtro seleciona entidades; a ficha exportada contém todos os seus dados autorizados, inclusive contatos que não participaram do filtro e informações invalidadas identificadas.

**Correspondências e completude.** Materializar IDs e versões no corte do trabalho, com ordenação estável e uma entidade por ID. Pessoa encontrada por três entradas aparece uma vez no cadastro exportado; as três correspondências são preservadas em aba própria. Cada `input_id` registra valor recebido, critério, resultado (encontrado, múltiplas correspondências, não encontrado ou inválido) e IDs encontrados. Deduplicação de resultados não funde pessoas. Exportar todos os vínculos de cada entidade selecionada, sem expandir recursivamente fichas de parentes de parentes ou empresas relacionadas que não foram selecionadas.

**XLSX: um dado por coluna.** Entregar arquivo `.xlsx` com abas relacionadas, cabeçalhos claros, filtros, primeira linha congelada e IDs para relacionar os dados. Uma célula contém um valor escalar; não juntar vários telefones/emails numa célula nem serializar coleções em JSON. Estrutura mínima:

| Aba | Conteúdo por linha |
|---|---|
| Pessoas / Empresas / Estabelecimentos | Uma entidade com ID e campos principais em colunas individuais |
| Documentos | Um documento com entidade/item, tipo, país, valor e metadados |
| Telefones | Um vínculo com número, DDD, ramal, classificação, uso, validade, WhatsApp e datas de confirmação em colunas distintas |
| Emails / Usernames | Um item com plataforma quando aplicável, validade e datas |
| Endereços | Um endereço com país, UF, cidade, bairro, rua, número, complemento e CEP separados |
| Relações / Atividades | Um vínculo ou atividade com IDs, tipo/papel, período e estado |
| Campos adicionais / Valores alternativos | Um valor com entidade/item, campo, tipo e valor em coluna tipada |
| Origens / Verificações / Histórico | Uma observação, verificação ou alteração por item/componente, com fonte, datas, ator autorizado, motivo, versão e valores anteriores/novos escalares |
| Entradas / Correspondências | Entradas, inclusive inválidas/não encontradas, e cada associação entre entrada e entidade |
| Resumo / Dicionário | Critérios, corte, versões, totais, definições de colunas, tipos e significado dos valores nulos |

Uma pessoa com dez telefones terá dez linhas em Telefones vinculadas à sua linha em Pessoas. Não limitar contatos para caber numa ficha de largura fixa. Origem do CEP distinta da rua fica associada ao componente correto nas abas de observações. Todos os campos cadastrais autorizados, origens, verificações e históricos são exportados; nenhuma coleção é truncada silenciosamente. Segredos de autenticação do sistema não fazem parte do cadastro exportável.

Documentos, telefones, CEPs e IDs são células de texto para preservar zeros e evitar arredondamento. Flags true/false/desconhecido têm representação distinta e documentada. Datas usam ISO 8601, preservando fuso quando existente. Conteúdo recebido é gravado como valor literal, sem conversão automática em fórmulas ou links executáveis.

**Limites e volumes.** Excel permite 1.048.576 linhas e 16.384 colunas por aba, e 32.767 caracteres por célula. Reservar uma linha para cabeçalho. Dividir em abas e, quando necessário, volumes XLSX numerados entregues em ZIP, com manifesto, contagens e SHA-256, sem omitir resultados. Manter IDs globais e o mesmo corte em todos os volumes. Textos maiores são repartidos em aba de fragmentos com item/campo, posição e regra de recomposição. Caracteres incompatíveis com XML recebem codificação reversível documentada. Dimensionar tamanho operacional por arquivo no piloto para garantir abertura prática no Excel. Um XLSX é o padrão; o painel informa quando a divisão for necessária. [Limites oficiais do Excel](https://support.microsoft.com/en-us/excel-specifications-and-limits).

**Estados e progresso.** Persistir `pending` (Pendente), `preparing` (Preparando), `completed` (Concluído), `failed` (Falhou), `cancelled` (Cancelado) e `expired` (Arquivo expirado). Preparando informa fase: validar entradas, selecionar correspondências, materializar dados, gerar planilhas, verificar arquivos e disponibilizar download. Status inclui `progress_percent`, `progress_kind` (medido/estimado), fase, unidades processadas/total conhecido, entradas válidas/inválidas, entidades únicas encontradas, linhas por aba, arquivos e `updated_at`. Progresso de cada fase deriva de contadores reais; percentual agregado é explicitamente estimado enquanto o volume total for desconhecido. Não avançar por temporizador nem mostrar 100% antes de todos os arquivos verificados e disponíveis. Zero resultados também conclui com XLSX de resumo e entradas não encontradas.

**Painel.** Adicionar Consulta em massa na navegação e ação de exportar todos os resultados da pesquisa atual. Formulário contém listas, upload, filtros, ordenação, nome do trabalho e prévia. Central de trabalhos mostra estado, percentual, fase, totais, erros, criação, término e expiração, com detalhes, cancelamento, repetição e download de todos os volumes. Repetição explícita cria novo trabalho e corte; recuperação técnica preserva corte e checkpoints. Notificar conclusão dentro do painel e manter histórico acessível após novo login. Fechar o navegador não interrompe o trabalho.

**API.** `POST /api/v1/bulk-queries` recebe `entity_type`, entradas ou referência de upload, árvore de filtros, modo de correspondência, ordenação e `format: "xlsx"`. Exige autenticação, permissões e `Idempotency-Key`; retorna 202 com `job_id`, estado e URL de acompanhamento. `GET /api/v1/bulk-queries/{id}` retorna progresso e, ao concluir, manifesto e arquivos. `GET /api/v1/bulk-queries` lista trabalhos autorizados; `POST /api/v1/bulk-queries/{id}/cancel` solicita cancelamento; `GET /api/v1/bulk-queries/{id}/files/{file_id}` entrega download autenticado. Informar intervalo recomendado de polling. Usar os mesmos trabalhos duráveis de `/exports` e `/jobs`, com um único ID. Listas acima de 2 MiB entram por upload assíncrono; o limite síncrono de enriquecimento de 100 itens não limita o total de entradas do trabalho de consulta.

**Execução e acesso.** Workers separados fazem leitura e escrita de XLSX por fluxo/lotes com memória limitada, checkpoints e retomada idempotente. Não carregar tudo em RAM, manter transação PostgreSQL aberta durante horas ou depender de cursor Elasticsearch expirado: persistir seleção e versões. Dimensionar armazenamento temporário e de artefatos, com quotas e escalonamento justo. Um lote não contorna rate limit: contabilizar custo, concorrência de trabalhos, polling e downloads. Arquivos são privados; conferir proprietário/permissões em cada consulta e download. Revogação bloqueia acesso imediatamente; mudança de permissão que torne um artefato mais amplo que o acesso atual exige nova geração autorizada. Expirar o arquivo em sete dias remove somente o artefato temporário, preservando dados cadastrais e auditoria.

**Testes de aceitação.** Exigir equivalência painel/API/pesquisa interativa; listas com vírgulas, aspas e novas linhas; zeros em documentos; homônimos; filtros AND/OR; entradas repetidas, inválidas e não encontradas; várias entradas para uma entidade; zero resultados; todas as coleções/flags/origens/históricos e campos personalizados; atualização cadastral durante execução; retomada, cancelamento, isolamento entre usuários e revogação; fórmulas, caracteres especiais e textos longos; divisão em abas/volumes e resultado maior que uma página. Reabrir XLSX gerados para conferir tipos, contagens e integridade referencial contra o manifesto. A soma dos volumes deve corresponder ao conjunto materializado e a todas as coleções exportadas, sem lacunas ou duplicações. Medir memória, disco e impacto em consultas interativas durante exportação grande. Só marcar Concluído após essas verificações automáticas.

## 8. APIs públicas e semântica de atualização

Namespace `/api/v1`, contrato OpenAPI com exemplos e coleção de testes. Chaves passam em `X-API-Key`, nunca em query string. A chave determina usuário/integração, escopos, limites e fontes permitidas; não aceitar que uma chave se apresente como qualquer origem escolhida no corpo.

| Interface | Comportamento |
|---|---|
| `POST /people/search`, `/companies/search` | Filtros, ordenação e cursor; resposta completa paginada |
| `GET /people/{id}`, `/companies/{id}` | Cadastro consolidado autorizado |
| `POST /people/enrich`, `/companies/enrich` | Localizar/criar identidade conforme regras e agregar dados |
| `PATCH /people/{id}/items/{item_id}` | Atualizar valor/validação/propriedades específicas, com histórico |
| Rotas equivalentes de empresas | Mesmo contrato de itens e rastreabilidade |
| `POST /relationships` e `PATCH /relationships/{id}` | Criar/atualizar relação tipada |
| `GET /people/{id}/history` e equivalentes | Histórico por campo, período e fonte |
| `POST /imports`, `GET /jobs/{id}` | Lotes maiores, progresso, erros e retomada |
| `POST /exports`, `GET /exports/{id}` | Resultado completo assíncrono |
| `POST /bulk-queries`, `GET /bulk-queries`, `GET /bulk-queries/{id}` | Consulta em massa por listas/filtros, progresso e resultado XLSX completo |
| `POST /bulk-queries/{id}/cancel`, `GET /bulk-queries/{id}/files/{file_id}` | Cancelamento e download autorizado dos arquivos gerados |
| `/admin/users`, `/admin/api-keys`, `/admin/fields`, `/admin/sources` | Administração autorizada |

Corpo de escrita inclui origem, data da observação/fonte, dados e propriedades enviadas. Recepção e ator são preenchidos pelo servidor. `Idempotency-Key` obrigatório nas operações de criação/lote; reutilizar a mesma chave com corpo diferente retorna 409. Versão/If-Match obrigatório para alteração direta de item existente, retornando conflito de concorrência em vez de sobrescrever outra edição.

Ausente em PATCH = não alterar. False e null são valores explícitos. Array vazio não exclui coleções existentes; remoção cadastral é substituída por invalidação identificada. Envio apenas de telefones não altera emails. Alteração de WhatsApp não altera titularidade. Lote informa resultado por item, com sucesso, pendência ou erro e possibilidade de repetir apenas falhas. Escrita síncrona é atômica por entidade; não prometer transação única entre todas as entidades de um lote.

Limites iniciais: corpo síncrono 2 MiB; até 100 itens de enriquecimento por chamada; arquivos maiores entram por importação assíncrona. Erros estruturados com código, mensagem, campo e request_id; 401/403/409/422/429/503 consistentes. Timeout nunca equivale a cancelamento garantido: cliente consulta operação ou repete com a mesma chave idempotente.

## 9. Concorrência, limites e desempenho

Rate limit em todas as rotas, incluindo login, OTP, painel, API, polling e downloads. Proteção por IP confiável na entrada e limite distribuído por usuário/integração, chave e categoria de operação. Múltiplas chaves do mesmo usuário compartilham também o teto agregado. Validar headers de proxy somente da infraestrutura conhecida.

Padrões iniciais configuráveis pelo admin: leituras 20/s por integração com burst 40; escritas 5/s com burst 10; buscas pesadas 2/s; até dois trabalhos pesados simultâneos e cinco novas exportações por minuto. Limite agregado de proteção inicial: 50 solicitações/s, mantendo a meta de 50/s como capacidade medida, não quota garantida a cada usuário. Custos de consultas pesadas e exportações não equivalem a uma consulta exata.

Token bucket atômico no Redis; 429 inclui Retry-After. Falha do coordenador de limites retorna 503 temporário nas APIs protegidas, sem liberar tráfego ilimitado. Pool de conexões limitado globalmente, filas limitadas, backpressure, deadlines e circuit breakers evitam saturação. Tarefas CPU-intensivas e importações não bloqueiam os processos HTTP.

Metas de homologação, medidas na aplicação: p95 até 300 ms para localização exata; p95 até 1 segundo para primeira página de buscas combinadas indexadas; p99 até 3 segundos, na carga mista de 50/s definida abaixo. Atualizações simples p95 até 500 ms; busca refletindo alteração p95 até 5 s. São metas a comprovar em hardware dimensionado, não promessa de que qualquer consulta, exportação ou retorno de milhões de fichas será instantâneo.

Carga de referência: 50% consultas exatas, 35% buscas combinadas e 15% enriquecimentos/validações, com pelo menos 100 clientes simultâneos e distribuições diversas de chaves. Testar cache quente e frio, falhas de nó, dados pouco seletivos, altos números de contatos e tarefas de importação simultâneas. Dimensionar crescimento a partir de latência, throughput, filas e utilização reais.

## 10. Login, TOTP, permissões e chaves

Papéis admin e usuário normal; permissões granulares de leitura, enriquecimento, validação, exportação, API, campos/origens e administração. Negação por padrão. Admin define permissões do usuário e escopos das chaves; uma chave não pode exceder seu proprietário. Desabilitar usuário revoga sessões e chaves. Não existe cadastro público.

Primeiro admin criado por procedimento local de bootstrap de uso único. Admin cria usuário normal com convite/código de ativação único, expirando em 24 horas. No primeiro acesso, usuário define senha, configura QR TOTP, confirma código e recebe códigos de recuperação. Antes de concluir OTP, sessão permite somente esse fluxo e logout; nenhum acesso a dados ou criação de API key.

Login humano posterior exige senha + TOTP para ambos os papéis. TOTP com 6 dígitos, período de 30 segundos, tolerância de um intervalo e prevenção de reutilização do mesmo código aceito. Segredo TOTP cifrado com chave fora do banco; senha com Argon2id; dez códigos de recuperação de uso único armazenados somente como hashes. Redefinição de OTP exige recuperação ou ação administrativa auditada, revoga sessões e obriga novo cadastro do fator. Não permitir bypass silencioso.

Sessões por cookie Secure/HttpOnly/SameSite com proteção CSRF; expiração por 30 minutos de inatividade e duração máxima de 12 horas. Operações administrativas sensíveis e criação/rotação de chaves exigem autenticação reforçada recente, até cinco minutos. Limites de tentativas para senha e TOTP por conta e IP, com recuperação controlada. [OWASP MFA](https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html).

APIs de integração usam chaves próprias revogáveis, mostradas uma vez, armazenadas como hash, com escopos, origem, expiração padrão de 90 dias, quotas e rotação. Não exigir Google Authenticator a cada chamada automatizada: o OTP protege o usuário que administra as chaves. Auditar consultas, alterações, exportações e administração sem colocar senhas, tokens, segredos OTP ou payloads pessoais completos nos logs operacionais.

## 11. Painel responsivo e campos administráveis

Interface em português, navegação por Pessoas, Empresas, Pesquisas salvas, Consulta em massa, Importações, Exportações, Qualidade/Pendências e Administração. Layout adaptado a desktop/tablet/celular, modo claro/escuro, teclado, foco visível, contraste adequado, tabelas virtualizadas e estados de carregamento/erro/vazio.

Pesquisa com construtor visual AND/OR, múltiplas seleções, chips de filtros, ordenação múltipla, colunas configuráveis/reordenáveis e pesquisas salvas. Seleção por página e seleção de todos os resultados são distintas. Ações em massa exibem quantidade/escopo e prévia, exigem permissão e são executadas como trabalhos rastreáveis.

Ficha com resumo, documentos, telefones, emails, endereços, usernames, familiares, empresas/vínculos, campos extras e histórico. Cada item mostra origem, atualização, validade e verificações. Edição direcionada, motivo de invalidação, reativação e comparação de alterações. Relações abrem a ficha relacionada sem expandir recursivamente uma árvore inteira.

Administração: usuários/OTP, permissões, chaves, fontes, quotas, catálogos de tipos, campos, auditoria e saúde de importações/busca. Admin pode criar campos de texto, inteiro, decimal, booleano, data, enum, URL e referência a pessoa/empresa, simples ou múltiplos, com rótulo, escopo e regras declarativas. Sem scripts/SQL executáveis fornecidos pela UI.

Campos usam ID estável e definição versionada. Mudança incompatível de tipo cria versão e migração explícita, preservando valores anteriores. Campo desativado não perde dados. Pesquisa/ordenação exige indexação declarada: criação agenda preparação e a UI mostra estado pendente/pronto/erro. Mapping Elasticsearch usa estruturas tipadas estáveis por field_id, evitando criar um campo físico arbitrário por nome escolhido pelo admin.

PWA instalável compartilha a aplicação e API. Cache offline somente de recursos estáticos; cadastros, respostas autenticadas, exportações e credenciais não ficam no cache do service worker. Mudanças do schema/contrato têm versão e atualização controlada da aplicação.

## 12. Backup, migração, testes e entrega

O backup original concluiu e sua restauração isolada foi validada; conservar as provas no pacote privado de retomada. Não iniciar ajustes de dados antes da restauração isolada verificada. Usar 9.5.3 na restauração inicial; conferir todos os cinco índices, metadados, contagens e leitura de amostras, além da integridade do arquivo de configurações. Preservar configurações e manifesto no Ubuntu fora dos arquivos internos do repositório. Os procedimentos operacionais e a continuação condicionada estão no pacote privado descrito em [RETOMADA.md](RETOMADA.md).

Depois, mapear TODOS os campos atualmente existentes: padronizado, campo adicional ou pendência com valor preservado. Campos como SERASA_NOME e SERASA_nome_completo não serão tratados como equivalentes sem avaliação. Nenhum campo não mapeado pode desaparecer silenciosamente. Não atribuir datas inexistentes às fontes.

Piloto representativo inicial de um milhão de registros, ampliado até cobrir contatos numerosos, ambiguidades, duplicações e conflitos. Medir pessoas únicas, interseção das fontes, fator de expansão de itens/histórico/nested, bytes por entidade, disco temporário, geração de WAL e tempo de reconstrução da busca. Projetar necessidades com 50% de margem operacional além de réplicas, backups e espaço temporário; provisionar a infraestrutura antes da carga completa.

Carga completa por lotes com checkpoint e limites de I/O; originais permanecem temporariamente disponíveis durante reconciliação. Para cada entrada, registrar resultado incorporado/pendente/erro. Reexecutar lotes deve manter a mesma contagem de itens e não repetir eventos. Eventual retirada dos índices antigos é ação posterior, com backup validado e escopo explícito, não parte automática da deduplicação.

Testes obrigatórios:

- CPF/CNPJ numérico e alfanumérico, zeros, documentos ambíguos e formato futuro não suportado.
- Telefones fixos/celulares/legados/internacionais/sem DDD, nono dígito idempotente e reversão de normalização incorreta.
- `is_whatsapp` true/false/null; verificação do valor antigo não migra para número novo.
- Enriquecimento parcial, dados repetidos de várias fontes, novidade mais recente, evento atrasado e data ausente/futura.
- Invalidar/reativar sem perda, histórico por componente e fusão/desfusão de pessoas.
- Campos do mesmo endereço/telefone combinados corretamente; CEP por faixa + sexo + idade + prefixo A.
- Busca paginada/ordenada sem duplicações no snapshot, cursor inválido/expirado e exportação completa com relações/coleções.
- Consulta em massa por listas e filtros no painel/API, progresso persistido e XLSX completo com abas/volumes reconciliados, conforme seção 7.1.
- Bloqueio de acesso antes do OTP, replay de código, recuperação, permissões, chaves revogadas e isolamento de fontes.
- Limite agregado entre processos/chaves, Redis indisponível, falha de worker e retomada idempotente de trabalhos.
- Falha entre cadastro e publicação, outbox acumulado, reindexação, nó de busca indisponível e recuperação sem perda.
- Responsividade, acessibilidade e ausência de dados pessoais no cache offline.
- Carga mista de 50/s, pelo menos uma hora estável e teste prolongado de oito horas; relatório de p50/p95/p99, erros, throughput e filas.

Entregar banco/migrações, importadores, API/OpenAPI, painel/PWA, testes, ambiente reproduzível, observabilidade e runbooks de backup/restauração/failover. Alertas para disco, falha/atraso de backup, lag da busca, filas, erros e latência. Meta operacional inicial: RPO zero para escritas confirmadas sob falha de um nó com réplica síncrona saudável; objetivo de recuperação até uma hora em cenário de falha de nó, a demonstrar em ensaio. Recuperação de desastre total por centenas de GB de backup terá tempo medido separado.

Publicação gradual: ambiente de teste, validação de dados, canário de consultas, clientes API migrados e troca de tráfego. Rotas antigas podem ter adaptadores de formato por 30 dias, mas autenticação migra para cabeçalho de API key; comunicar essa mudança aos clientes, sem manter token fixo em URL como contrato novo. Após novas escritas, rollback deve voltar a versão compatível da aplicação usando o cadastro oficial, nunca apontar silenciosamente para base antiga desatualizada.

Critério final de entrega: dados reconciliados sem desaparecimento de campos, backup restaurável, fluxos de consulta/escrita/OTP funcionais no painel e API, auditoria comprovada, metas de carga atendidas na infraestrutura provisionada e documentação suficiente para operar e recuperar o serviço.

## 13. Garantias de preservação e comprovação da entrega

Reforço do usuário em 08/09/2026: após aprovação da restauração do backup, executar o plano completo, com implementação, testes, correções e validação, prosseguindo autonomamente no trabalho possível. Não encerrar a entrega em protótipo, estrutura inicial ou funcionalidades parcialmente conectadas. Requisitos já solicitados são o mínimo; tratar também os casos necessários à integridade e à operação descritos abaixo. Não declarar 100% pronto enquanto houver requisito obrigatório sem evidência de aprovação ou homologação dependente de infraestrutura ainda não realizada.

**Preservação por campo.** Cada informação recebida deve ter destino explícito: atributo padronizado, campo adicional ou valor pendente de classificação. Preservar valor original do campo, tipo recebido, caminho do campo na origem e, quando necessário, posição na coleção, além do valor normalizado, regra/versão e resultado da transformação. Isso permite reconstruir a contribuição de uma fonte por operação sem manter cópia integral permanente do registro original. Valores não reconhecidos não podem ser descartados, convertidos silenciosamente ou forçados a um tipo incorreto. Distinguir ausente, null, false, zero, string vazia e coleção vazia na interpretação do evento; uma normalização só pode aproximar essas representações mediante regra explícita e histórico preservado.

**Observações e flags independentes.** Toda informação e cada propriedade de confirmação/invalidação possuem origem, ator/operação, data da fonte quando disponível, data de observação, recepção, versão e histórico. Mudança em uma flag não atualiza indevidamente as datas ou confirmações de outras flags. Datas de importação não substituem datas desconhecidas da fonte. Horários são comparados em UTC e preservam o contexto original quando informado. Regras novas de validação ou normalização não reescrevem o passado: geram observações versionadas e permitem identificar o que foi aplicado.

**Atualidade não é confirmação automática.** Manter `last_checked_at`, `expires_at` opcional e indicador de verificação desatualizada, quando a política do tipo de dado exigir revalidação. Passagem do tempo não confirma validade/WhatsApp, não inventa data de verificação e não transforma desconhecido em false. A última evidência continua preservada; o painel e a API distinguem resultado conhecido de confirmação vencida. Se houver evidência nova, aplicar a precedência por propriedade, com histórico inclusive de eventos atrasados ou conflitantes. Reprocessar o mesmo evento não renova artificialmente a confirmação.

**Integridade de identidade e relacionamentos.** Não fundir homônimos, titulares de telefone compartilhado ou pessoas no mesmo endereço. Tratar documentos ausentes/incorretos, conflitos de titularidade, mudança de valor, múltiplos vínculos e relações pendentes. Fusões/desfusões preservam observações, vínculos e IDs anteriores; a desfusão usa a atribuição original dos eventos e encaminha alterações ambíguas posteriores à fusão para revisão, sem atribuição arbitrária. Toda operação em massa deve ter reconciliação por item e ser retomável sem duplicar contribuições.

**Prova de ausência de perda.** Na migração, manter um registro de processamento por fonte/ID/versão que relacione os caminhos e valores de entrada às observações produzidas. Comparar contagens e hashes canônicos por campo/coleção, contemplando itens repetidos e posições quando relevantes. Distinguir número de registros recebidos, entidades consolidadas, itens únicos e observações: redução por deduplicação não é, por si só, perda, e contagem global igual não prova preservação. Todo descarte aparente precisa ser explicado por consolidação rastreável; pendências mantêm seus valores e aparecem no relatório. Exigir zero entradas ou campos sem destino registrado antes da troca definitiva.

**Busca e entrega completas.** Informações atuais, alternativas, invalidadas e pendentes permanecem acessíveis conforme permissões, com filtros e estados explícitos. Campo pendente de classificação/indexação é visível e sinalizado; nunca fingir que já é pesquisável ou ignorar um filtro sobre ele. Projeções de busca são reconstruíveis a partir do cadastro oficial e sua versão/atraso é observável. Consulta, histórico e exportação XLSX devem manter os mesmos significados para valores, origens, datas e flags.

**Matriz de entrega.** Durante a implementação, manter matriz versionada ligando cada requisito das seções 1–13 às migrações/tabelas, serviços, endpoints, telas, testes e evidências correspondentes. Estados distintos: planejado, implementado, testado, validado ou dependência pendente. Evidências indicam versão do código, ambiente, conjunto de dados, comando/procedimento executado, resultado e data. Incluir testes unitários das regras, integração com serviços reais isolados, fluxos completos de painel/API, reconciliação dos importadores, restauração/recuperação, exportação completa e carga. Usar dados sintéticos no desenvolvimento; dados reais apenas nos ambientes autorizados de migração/validação.

Corrigir falhas encontradas e executar novamente as verificações afetadas. Não classificar teste não executado, simulação ou resultado obtido com volume reduzido como homologação de produção. O relatório de conclusão deve listar qualquer dependência restante, especialmente infraestrutura nova, carga integral e testes de capacidade. Até resolvê-las, a entrega não recebe o estado integralmente validado, embora o trabalho independente deva continuar.

## 14. Transferência e fechamento

O código e este plano ficam no Git. Segredos, estado administrativo, dados e provas operacionais ficam em backup privado cifrado. Seguir [RETOMADA.md](RETOMADA.md) antes de desativar o servidor anterior. Não classificar a publicação do código como migração dos dados. O novo servidor e o trabalho em outra tarefa devem retomar a matriz de requisitos, preservando os limites e as pendências registrados.

**Lembrete solicitado pelo proprietário:** ao concluir toda a implementação, migração e homologação, lembrá-lo de alterar a visibilidade deste repositório para **privado**. A publicação atual foi autorizada enquanto público; isso nunca autoriza incluir segredos ou dados pessoais no Git.
