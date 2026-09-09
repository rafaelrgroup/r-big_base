# Prioridade: modelo dos dados e migração integral

Atualização de 09/09/2026, conforme orientação do proprietário: priorizar o modelo,
a velocidade de consulta e a migração. Novas funções de interface ficam depois.
Preservar o painel/API já publicados e os acessos existentes. Essa ordem não
remove os requisitos funcionais da matriz de implementação.

## Situação comprovada

- Backup original restaurado: 243.344.652 entradas de `pessoas` e 233.056.617 de
  `pessoas_serasa`, total de 476.401.269 entradas, sem pressupor pessoas únicas.
- Novo piloto: 10.000 entradas conferidas por campo; 6.910 entradas anteriores
  preservadas. Corte atual: 16.910 entidades e operações canônicas.
- Busca inicial: 16.910 entidades comparadas integralmente por identidade, versão
  e conteúdo, com zero faltantes, extras ou divergências; publicada em 09/09/2026.
- Nesse corte, 142.634 campos adicionais estão preservados no PostgreSQL mas
  ainda sem definição pesquisável. Sua inclusão na busca permanece obrigatória.
- PostgreSQL atual: aproximadamente 2,505 GB; observações e estado de campos
  concentram o custo. Índice de busca: aproximadamente 221 MB, sem translog.
  O modelo atual não demonstrou viabilidade para a carga integral no disco atual.
- A carga integral não começou. Testes funcionais, backup restaurado e piloto
  concluído não equivalem a capacidade, prazo ou migração integral aprovados.

## Contratos que nenhuma otimização pode remover

1. Uma identidade canônica por pessoa/empresa quando a associação documental for
   comprovada. Identidades conflitantes ficam registradas para resolução explícita;
   não unir pessoas por nome, contato compartilhado ou parentesco presumido.
2. Múltiplos documentos, nomes, endereços, telefones, emails, usernames, atividades
   e vínculos, com IDs estáveis dos itens e valores tipados. Manter precisão,
   zeros, valores ausentes, nulos, falso, listas/objetos vazios e campos desconhecidos.
3. Cada valor e cada flag preservam origem, identificação na origem, data informada,
   observação, recebimento, ator, versão de transformação e cadeia de alterações.
   Metadados iguais podem ser referenciados; precisam continuar recuperáveis.
4. `true`, `false` e `null` permanecem diferentes. Validade, WhatsApp e demais
   confirmações possuem evidência/data própria e vínculo com o valor confirmado.
   Uma confirmação de número antigo não confirma seu substituto.
5. Dados atrasados, contraditórios, invalidados e substituídos não desaparecem.
   O estado atual é uma projeção reconstruível do histórico de campos e flags.
6. Telefone: classificação, número recebido, número normalizado e motivo da
   conversão histórica ficam preservados; ambiguidades não são corrigidas por palpite.
7. Parentesco e vínculo empresarial mantêm tipo, papel, sentido, datas e status.
   Identificação por CPF/CNPJ pendente não fabrica uma associação confirmada.
8. Consulta deve continuar possível por documento, nome completo/parcial, telefone,
   email, endereço e campos adicionais, com composição de filtros e correlação
   dentro do mesmo item. Nenhuma perda silenciosa para cumprir limites de tamanho.
9. O banco consolidado armazena dados normalizados e histórico por campo. O backup
   original é proteção externa; não usar cópias integrais de documentos originais
   como substituto do modelo consolidado solicitado.

## Implementação em ordem

### 1. Representação compacta e prova de armazenamento

Separar identidade, estado consultável e histórico. Reduzir metadados repetidos
por referências versionadas; experimentar histórico imutável em blocos limitados
de átomos tipados e ponteiros por entidade/operação. Manter valores e proveniência
completos, inclusive campos ainda não interpretados. Hashes binários, escrita por
lote e índices estritamente necessários devem ser medidos.

O experimento atual comprime átomos normalizados, não objetos originais. Em 1.000
entradas de cinco faixas por fonte, blocos de 100 entradas ocuparam em média
1.367,882 bytes por entrada de `pessoas` e 1.201,168 de `pessoas_serasa`, com
reconstrução e preparação canônica iguais. Isso mede somente a codificação em
memória: não inclui índices, estado atual, ponteiros, WAL, backups ou busca, e a
amostra não é estatisticamente representativa. Não extrapolar como capacidade.
Essas medidas pertencem ao codec de bloco 1, no commit `7c21d39`; o codec 2
acrescentou a preservação explícita da ordem das flags e precisa de nova medição
junto com os demais componentes antes do dimensionamento.

Critério para adoção: leitura e escrita completas com o novo repositório,
reconstrução do estado, precedência temporal, flags vinculadas, alterações de
coleções, deduplicação/conflitos, fusão/desfusão e histórico paginado equivalentes
ao contrato. A prova de armazenamento isolada não satisfaz esse critério.

Primeira integração concluída no código: extração da regra comum que produz a
observação imutável e decide a substituição do estado atual. A reconstrução dos
blocos usa essa regra e foi comparada ao código da implantação anterior, com
igualdade de todas as colunas de 378 observações, em 126 operações fictícias de
pessoas/empresas. A comparação identificou e corrigiu mudança da ordem das
flags no codec inicial. Os 87 testes dessa etapa passaram, sem ignorados.
Isso não ativa um novo repositório persistente nem aumenta a carga real.

Próximo resultado concreto: integrar os blocos ao registro global de identidades
e operações, persistir as referências do estado vigente, reconstruir a ficha e
paginar o histórico. A chave de operação deve impedir duplicação entre trabalhos
diferentes, e uma resposta perdida deve conservar o ator/data do primeiro commit.
Conflitos de CPF/CNPJ, tipo de entidade ou titularidade devem produzir casos
rastreáveis para resolução; associação por contato compartilhado não é válida.
Confirmar atomicidade de identidade, histórico, estado, fila de indexação e
checkpoint com falha injetada no meio do lote e reinício do processo.

### 2. Busca com custo controlado

Medir e reduzir a multiplicação de documentos internos causada por cada campo
aninhado. A projeção deve conter o necessário para filtrar/ordenar e buscar,
enquanto a ficha completa e o histórico vêm do banco canônico. Preservar filtros
no mesmo item, fontes, datas, validade e triestado. Registrar campos desconhecidos
em catálogo versionado para torná-los pesquisáveis sem interpretação inventada.

Reindexar em uma geração independente, comparar conteúdo e resultados de
consultas, então trocar o alias. O `published_at` do outbox atual é global:
não zerá-lo para reconstruir um índice nem reutilizá-lo como checkpoint de uma
nova geração. Cada reconstrução precisa de posição/prova próprias.

### 3. Importador rápido, idempotente e retomável

Leitura sequencial com limites fixados das fontes restauradas e protegidas contra
escrita; blocos/lotes de tamanho limitado; processamento paralelo somente onde
não cria conflitos de identidade. Gravação do lote, identidade, histórico,
projeção pendente e checkpoint na mesma transação. Repetir um lote após resposta
perdida deve reconhecer o commit anterior sem duplicar seus eventos.

Separar erros transitórios de registros que exigem revisão. Registrar todo caso
não processado, seu motivo e posição para correção/reprocessamento; não declarar
carga completa com registros descartados. Preservar os pilotos e seus recibos.

### 4. Piloto maior e dimensionamento

Exercitar ambas as fontes, sobreposição documental, documentos inválidos/ausentes,
contatos compartilhados, muitos itens, dados grandes, campos desconhecidos e
contradições. Amostra representativa de pelo menos um milhão, após medição menor
mostrar capacidade operacional suficiente. Não aumentar simplesmente o limite do
importador atual, nem remover a margem de disco para forçar sua execução.

Medir registros reconciliados por segundo e custo por componente: tabelas,
índices, blocos, estado atual, WAL gerado/retido, busca, merges, temporários e
backup. Determinar armazenamento necessário a partir dessas medidas. Se mesmo
o modelo otimizado exigir mais volume, fornecer o dimensionamento comprovado.

### 5. Carga integral e homologação

Executar por faixas/lotes persistentes, com monitoramento e retomada, reconciliação
de todas as entradas e tratamento de todos os conflitos. Contagens de origem,
operações, pessoas únicas, itens, observações e rejeições são métricas diferentes.
Indexar e conferir a base completa; concluir todos os dados antes de declarar
migração integral. Fazer backup externo do novo estado e restauração verificável.
Ensaiar a meta de 50 requisições por segundo, incluindo consultas pesadas, escrita
concorrente, limites de API, falhas/reinício e recuperação.

## Interface depois

Conservar o acesso atual para inspeção. Novos componentes de painel e acabamentos
aguardam o núcleo de dados/migração. XLSX em massa, ordenações múltiplas, catálogos
administrativos completos e demais funções continuam na matriz; o adiamento da
interface não elimina os contratos de API nem os requisitos de rastreabilidade.
