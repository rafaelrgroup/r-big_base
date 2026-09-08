# Infraestrutura de destino do BIG BASE

Revisão: 08/09/2026. Fontes oficiais consultadas nesta data. Este documento orienta a contratação inicial e o piloto; não certifica capacidade para a carga completa. Nenhum servidor foi provisionado durante esta avaliação.

## Recomendação para contratar agora

Para a primeira máquina, recomendo **256 GB de RAM, 32–64 vCPU e SSD persistente expansível**, com **4–8 TB úteis inicialmente**. Se a contratação for de servidor físico com expansão mais difícil, prefiro **32 núcleos físicos, 256 GB ECC e aproximadamente 8 TB úteis em NVMe empresarial redundante**. Em ambos os casos, o contrato deve permitir ampliar armazenamento e separar banco, busca e trabalhadores em outras máquinas.

Essa é uma hipótese conservadora para instalar os serviços definitivos, executar o piloto representativo e medir a configuração inicial. **Não há evidência de que 4 TB, 8 TB ou esse número de processadores comporte toda a base consolidada ou alcance 100 requisições/s.** A liberação da importação integral depende das medições descritas abaixo. Contratar uma máquina única também não atende à alta disponibilidade prevista no plano.

| Item | Configuração inicial proposta | Critério de contratação |
|---|---|---|
| CPU | 32–64 vCPU x86_64; em servidor físico, preferencialmente 32 núcleos físicos | CPU sustentada, sem depender de créditos de burst; identificar núcleos e threads separadamente |
| RAM | 256 GB; ECC em servidor físico | Expansível ou máquina substituível por outra maior; 128 GB é alternativa para piloto limitado, sem compromisso de carga completa |
| Dados | 4–8 TB úteis de SSD persistente; preferência de cerca de 8 TB no físico | Informar capacidade depois da redundância, latência, IOPS, throughput e possibilidade de expansão |
| Discos físicos | Exemplo: 4 × 3,84 TB NVMe empresarial em RAID10 | Aproximadamente 7,68 TB decimais úteis antes do filesystem; exigir proteção contra perda de energia, especificação de endurance e monitoramento de desgaste |
| Sistema | Linux x86_64; Ubuntu 24.04 LTS como opção inicial | PostgreSQL 18 por repositório oficial; fixar e validar a versão Elasticsearch 9 do projeto e sua matriz de suporte |
| Volumes | Sistema separado; volumes próprios para PostgreSQL, busca e temporários; WAL isolável | Evitar que exportação ou reconstrução esgote o volume do cadastro oficial; separar volumes não cria tolerância a falha do host |
| Rede | Rede privada de 10 Gbit/s como referência entre serviços; acesso externo de pelo menos 1 Gbit/s | Medir latência e throughput reais, considerar limites compartilhados e transferência de backup |
| Backup | Destino externo à máquina, expansível, com retenção e teste de restauração | Incluir backup PostgreSQL/WAL, snapshots Elasticsearch e configurações; manter o backup anterior protegido |

A preferência por SSD está alinhada à orientação da Elastic: indexação combina operações sequenciais e aleatórias e costuma se beneficiar de SSD e baixa latência. O exemplo RAID10 é uma escolha para este servidor compartilhado inicial; sua capacidade útil não deve ser confundida com a soma comercial dos discos. RAID não substitui réplica em outra máquina ou backup. [Armazenamento e indexação — Elastic](https://www.elastic.co/docs/deploy-manage/production-guidance/optimize-performance/indexing-speed)

O repositório oficial PostgreSQL oferece PostgreSQL 18 para Ubuntu 24.04. A combinação exata de Elasticsearch, sistema e JDK deve ser registrada no ambiente reproduzível e conferida na matriz do fabricante antes da instalação; este documento não declara homologação dessa combinação no destino ainda inexistente. [PostgreSQL para Ubuntu](https://www.postgresql.org/download/linux/ubuntu/), [Matriz de suporte Elastic](https://www.elastic.co/support/matrix)

## Duas opções concretas na AWS

| Instância | CPU e RAM oficiais | Limite agregado EBS da instância | Uso proposto |
|---|---|---|---|
| `r7i.8xlarge` | 32 vCPU, 16 núcleos, 256 GiB | 10.000 Mbit/s; 1.250 MB/s; 40.000 IOPS | Piloto e primeira máquina priorizando memória |
| `m7i.16xlarge` | 64 vCPU, 32 núcleos, 256 GiB | 20.000 Mbit/s; 2.500 MB/s; 80.000 IOPS | Maior folga inicial para ingestão, indexação e exportações concorrentes |

As duas opções usam EBS e não incluem NVMe local de instance store. São exemplos de catálogo verificados, não afirmações de disponibilidade em determinada região ou de capacidade garantida do BIG BASE. A segunda é a referência caso se queira contratar agora uma única máquina com maior margem de processamento. Escolher a região, verificar quotas e disponibilidade antes da compra. [R7i — especificações EC2](https://docs.aws.amazon.com/ec2/latest/instancetypes/mo.html), [M7i — especificações EC2](https://docs.aws.amazon.com/ec2/latest/instancetypes/gp.html)

Para começar o piloto, usar EBS SSD expansível com orçamento total inicial de **4 TiB de dados**, separado entre PostgreSQL, busca e temporários/WAL. Aumentar conforme as medições, inclusive antes de carregar mais registros; 8 TiB pode ser uma segunda reserva, não um teto de crescimento. O volume do sistema e o backup externo são adicionais. Como hipótese de ensaio, provisionar nos volumes principais **10.000–16.000 IOPS e 500 MiB/s por volume**, medindo a latência; a soma permanece limitada pela instância. Esses parâmetros são proposta de teste, não requisito mínimo provado, e não precisam ser contratados para volumes de logs ou arquivos frios.

O gp3 oferece base de 3.000 IOPS e 125 MiB/s; desempenho adicional é provisionado separadamente. A documentação atual informa máximos de 80.000 IOPS, 2.000 MiB/s e 64 TiB por volume, sujeitos às relações entre tamanho, IOPS e throughput; Outposts tem limites diferentes. Comprar mais capacidade sem ajustar desempenho não aumenta automaticamente esses valores. [Volumes gp3 — AWS](https://docs.aws.amazon.com/ebs/latest/userguide/general-purpose.html)

Elastic Volumes permite aumentar capacidade e ajustar desempenho sem desconectar o volume em instâncias compatíveis; ainda é necessário acompanhar a operação e ampliar o filesystem. Expansão deve ocorrer com antecedência e não quando o disco já estiver cheio. [Expansão de EBS — AWS](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-modify-volume.html)

Se for escolhida outra família com NVMe instance store, esse disco é temporário: parar ou terminar a instância pode perder seus dados. Usá-lo exige desenho próprio de réplicas e recuperação; ele não deve ser a única cópia do PostgreSQL. [Persistência de instance store — AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-store-lifetime.html)

Não foram cotados preços. O custo final depende da região, período contratado, volumes, IOPS/throughput adicionais, tráfego, backup e número de réplicas.

## Divisão inicial de memória no servidor único

Uma hipótese para a máquina de 256 GiB é a seguinte. Os valores incluem os respectivos orçamentos de cache e precisam ser verificados com métricas de memória do serviço; não são reservas que devam ser inteiramente preenchidas pelos processos.

| Grupo | Orçamento de referência | Ponto inicial a testar |
|---|---|---|
| PostgreSQL | 96 GiB | `shared_buffers` de 24 GiB, pool limitado; restante para cache e operações controladas |
| Elasticsearch | 96 GiB | Heap de 24 GiB se a configuração for manual; restante para memória fora do heap e cache de arquivos |
| API, Redis e trabalhadores | 32 GiB em conjunto | Limites independentes; reduzir concorrência de exportações/importações se houver pressão |
| Sistema e margem | 32 GiB | Memória disponível para o sistema e variações; não consumi-la antecipadamente em novos heaps |

O PostgreSQL sugere 25% para `shared_buffers` em servidor dedicado, mas também depende do cache do sistema. Em host compartilhado, aplicar a referência ao orçamento reservado para PostgreSQL, não à RAM total. `work_mem` pode ser consumido várias vezes por consulta e por conexão; manutenção e autovacuum também precisam de orçamento. Pooling e limites de concorrência são parte do dimensionamento. [Memória PostgreSQL 18](https://www.postgresql.org/docs/18/runtime-config-resource.html)

A Elastic recomenda o dimensionamento automático de heap para a maioria dos ambientes. Se houver ajuste manual, `Xms` e `Xmx` devem ser iguais e no máximo 50% da memória disponível ao nó/container. O limite para manter ponteiros comprimidos varia; a documentação cita 26 GB como valor seguro na maioria dos sistemas, chegando a 30 GB em alguns. Usar 24 GiB como hipótese inicial e confirmar `jvm.using_compressed_ordinary_object_pointers`; aumentar RAM do host não significa entregar metade dela a um único heap. [Heap Elasticsearch](https://www.elastic.co/docs/reference/elasticsearch/jvm-settings)

## Por que o snapshot de 230,7 GB não dimensiona o novo banco

O dimensionamento considera **cerca de 476 milhões de entradas de origem** nas duas fontes cadastrais. Essa soma não é o número de pessoas únicas. O snapshot validado tem **aproximadamente 230,7 GB** e contém os índices antigos; não representa as futuras tabelas, índices, histórico, réplicas ou arquivos temporários. Evidência do projeto: [IMPLEMENTACAO.md](IMPLEMENTACAO.md) e relatório de restauração conservado fora do repositório, com contagens e medidas exatas.

O modelo canônico transforma entradas em entidades, documentos, itens, valores, observações, fontes, versões e operações. Deduplicar pode reduzir entidades, enquanto a preservação de cada observação e de cada valor aumenta outras tabelas. Índices PostgreSQL, TOAST, WAL e atualizações futuras também consomem espaço. Não existe, neste momento, um fator de expansão real medido que permita calcular o total a partir do snapshot.

A projeção de busca tem outra expansão: cada objeto `nested` vira um documento Lucene adicional. Uma entidade com 100 objetos nested produz 101 documentos internos. Usar nested onde é necessário correlacionar valor/status/fonte do mesmo item, controlar coleções extensas e preservar no canônico a história que não precisa ser materializada integralmente no índice de busca. Qualquer limite operacional exige preservação e acesso completos pela API, sem descarte silencioso. [Tipo nested — Elastic](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/nested)

O ponto inicial recomendado pela Elastic para shards é 10–50 GB e menos de 200 milhões de documentos por shard. Essa orientação deve ser confrontada com documentos internos nested, segmentos, atualizações e tempos de recuperação, não apenas com a contagem de pessoas da API. O número final de shards será escolhido após medir o piloto. [Dimensionamento de shards — Elastic](https://www.elastic.co/docs/deploy-manage/production-guidance/optimize-performance/size-shards)

## Estrutura para atender à alta disponibilidade do plano

O servidor único é uma etapa de implantação e medição. Para tolerar falha de máquina, separar os serviços e suas cópias em domínios de falha distintos. As quantidades de RAM/CPU abaixo são hipóteses iniciais para orçamento, a recalibrar com o piloto; armazenamento por nó será calculado por componente.

| Componente | Topologia proposta | Referência inicial por nó |
|---|---|---|
| PostgreSQL | Primário e réplica síncrona em outro host; backup contínuo externo | 16–32 vCPU, 128–256 GiB; cada cópia precisa comportar todo o canônico e seus índices |
| PostgreSQL com continuidade de escrita após perda da réplica | Primário e duas réplicas candidatas, exigindo confirmação de pelo menos uma; failover com proteção contra dois primários | Capacidade equivalente nos candidatos que possam assumir o primário |
| Elasticsearch | Pelo menos três nós elegíveis a master e com dados; uma réplica por shard em outro nó | 16 vCPU e 64–128 GiB; SSD por nó calculado pela distribuição das duas cópias e reserva de recuperação |
| API/painel | Duas instâncias atrás de entrada HTTPS redundante | 4–8 vCPU, 16 GiB; pools limitados e sem estado cadastral local |
| Trabalhadores | Ingestão, projeção e exportação em grupos separados, escaláveis | 8–16 vCPU, 32–64 GiB por nó inicial; filas e checkpoints duráveis |
| Redis | Primário/réplica e failover; três participantes Sentinel em domínios de falha distintos, ou serviço equivalente validado | 2–4 vCPU e 8–16 GiB por servidor Redis como hipótese; `noeviction` para limites |
| Backups e exportações | Armazenamento externo compartilhado/autenticado, com retenção e validação | Dimensionado separadamente por tamanho, geração diária e retenção |

A replicação síncrona PostgreSQL acrescenta espera de rede e gravação no standby. Com apenas uma réplica síncrona, sua perda pode bloquear novas escritas até recuperação ou mudança controlada da política; adicionar uma segunda candidata permite manter uma confirmação sem reduzir a garantia. A política do projeto preserva a durabilidade das escritas confirmadas e precisa ser testada em falhas reais. [Replicação PostgreSQL 18](https://www.postgresql.org/docs/18/warm-standby.html)

Para Elasticsearch, três nós elegíveis permitem eleição após a perda de um, e réplicas de shards mantêm cópias em outro nó. Ainda é necessário distribuir as requisições e dimensionar os nós restantes para a carga e recuperação; três processos na mesma máquina não resolvem a falha do host. [Resiliência de clusters pequenos — Elastic](https://www.elastic.co/docs/deploy-manage/production-guidance/availability-and-resilience/resilience-in-small-clusters)

O Redis não será a única cópia de trabalhos ou dados cadastrais. Sua replicação é assíncrona; o failover e o impacto sobre quotas precisam de teste específico. Três Sentinels são a referência mínima para um desenho robusto e devem estar distribuídos. [Alta disponibilidade Redis Sentinel](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/)

Backups PostgreSQL devem combinar cópia base e arquivamento WAL para recuperação no tempo. Réplicas online não substituem esse histórico de recuperação. O objetivo de RPO zero sob falha de um nó e a recuperação em até uma hora, definidos no plano, continuam metas sem homologação; desastre completo por backup terá tempo medido separadamente. [Backup contínuo e PITR PostgreSQL](https://www.postgresql.org/docs/18/continuous-archiving.html)

## Medições que liberam a carga completa

1. Preparar destino identificado como staging/production, com PostgreSQL 18, volumes persistentes, capacidade e limites explícitos. Ambiente sintético e SQLite de desenvolvimento não recebem os dados reais.
2. Executar uma carga limitada inicial, por exemplo 10 mil entradas, com orçamento de tempo/disco; ampliar por etapas até pelo menos **1 milhão de registros representativos** e cobrir ambas as fontes, duplicações, conflitos e cadastros com muitas coleções.
3. Medir separadamente entradas processadas, entidades únicas, valores, observações e índices. Reconciliar todos os valores de origem, incluindo os preservados com semântica pendente; nenhuma falha deve desaparecer do relatório.
4. Medir armazenamento PostgreSQL completo, somando partições reais sem contar pai/filhas duas vezes. `pg_total_relation_size` inclui índices e TOAST; medir também a base total, crescimento e espaço ocupado no volume. [Funções de tamanho PostgreSQL](https://www.postgresql.org/docs/18/functions-admin.html)
5. Medir primários e réplicas Elasticsearch separadamente, documentos nested, pico de reconstrução/merge e tempo para refazer uma projeção. Medir WAL por unidade de tempo, retenção necessária, atraso de réplicas e picos de temporários/XLSX.
6. Projetar por componente e por estrato de dados representativo, mantendo o mesmo denominador da medição. Para componentes que crescem com a carga: `bytes medidos × entradas previstas / entradas medidas`. Não extrapolar só a média de cadastros simples nem assumir expansão linear de latência.
7. Somar cópias previstas, espaço de reconstrução, temporários e retenção pertinente; acrescentar **50% de margem operacional**, conforme o plano. Mapear cada parcela ao volume que de fato a armazenará, inclusive réplicas e backups externos. WAL retido exige projeção pela taxa observada e janela de retenção, e não apenas multiplicação pelo número de pessoas.
8. Só liberar a importação integral quando capacidade projetada, destino, prova de restauração e reconciliação estiverem aprovados pelo preflight. Durante a carga, revisar projeções por lote; pausar antes de consumir a reserva, sem apagar dados ou confirmações anteriores.

O teste de capacidade funcional deve reproduzir **100 requisições/s**, com 50% de consultas exatas, 35% de buscas combinadas e 15% de enriquecimentos, pelo menos 100 clientes, exportações/importações simultâneas, cache frio/quente e cenários pouco seletivos. Metas do projeto: p95 de 300 ms para consulta exata, 1 s para primeira página combinada, 500 ms para atualização; p99 até 3 s; atraso de busca p95 até 5 s. Exigir uma hora estável e ensaio prolongado de oito horas, além de falha/recuperação. Comprar a configuração indicada não substitui esses testes.

## Decisão operacional

É possível contratar agora a máquina inicial de **256 GB e armazenamento SSD expansível**, iniciar a implantação e medir o piloto. A decisão de importar os 476 milhões de registros de origem e de trocar o tráfego será tomada com as medições reais e a topologia de disponibilidade prontas. O servidor antigo e o backup validado permanecem disponíveis durante essa transição. O sistema completo continua em implementação; a nova infraestrutura viabiliza as etapas reais ainda pendentes.
