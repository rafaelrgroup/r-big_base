# Campos adicionais na projeção canônica

Contrato `canonical-search-2026-09-09.1`, preparado na MAIN em 09/09/2026.
Busca ampla permanece desativada na aplicação publicada. Este recorte não cria
índices nem altera catálogo, release, outbox real ou serviços de migração.

`build_projection` aceita itens `custom` no caminho `value`, com snapshot da
definição e SHA256 conferido contra os metadados da observação. A chave indexada
combina ID, versão, hash, tipo, contrato e implantação do catálogo PostgreSQL.
`custom_selector(metadata)` devolve esse seletor; não consulta o catálogo atual.
Renomear ou desativar um campo não reclassifica suas observações anteriores.
Definições locais de ensaio e PostgreSQL têm espaços separados.

O filtro exige `kind=custom`, `field=value` e `custom` contendo exatamente
`field_id`, `version`, `sha256`, `type`, `contract` e `deployment_id`.
O último campo é UUID para contrato PostgreSQL e null para o contrato local.
O seletor deve ser obtido da definição histórica autorizada. Para pesquisar
várias versões, o chamador usa um grupo `any` explícito com os seletores;
nenhuma versão é incluída implicitamente pelo nome atual do campo.

Exemplo no contrato Python:

```python
selector = custom_selector(observation['metadata'])
criteria = {
    'kind': 'custom', 'field': 'value', 'custom': selector,
    'op': 'eq', 'value': False, 'flags': {'valid': True},
}
query = build_query(criteria)
```

Seletor, valor, fonte e flags ficam no mesmo `items.fields` nested; itens
alternativos mantêm IDs separados. AND/OR, `same_item`, invalidação, pendências
e vencimento conservam o contrato geral. Mapping estrito possui propriedades
fixas; um novo nome de campo não cria uma propriedade Elasticsearch.

Texto, inteiro, decimal, booleano, data, enum e URL têm igualdade tipada.
Null, false e zero são distintos. Decimal textual aceito pelo catálogo gera
um token numérico exato, igual ao de Decimal/integer equivalente, sem float
ou arredondamento pelo contexto Decimal; a observação original não muda.
Texto/enum/URL permitem os operadores textuais existentes e seus limites.
Datas adicionais aceitam intervalos de datas civis ISO. Intervalos numéricos,
ordenação e referências ainda não têm suporte neste recorte e são recusados.
A referência continua integral no PostgreSQL e incrementa `unsupported_values`.

Definição ausente sem vínculo parcial incrementa `unbound_custom_fields`, sem fabricar prontidão.
Caminhos desconhecidos, texto inseguro ou extenso são contabilizados pelos
contadores de omissão. Vínculo/hash inválido interrompe a projeção inteira;
o consumidor não confirma o evento que falhou. Limites existentes de bytes e
nested continuam aplicáveis. A publicação não atualiza `search_state` ou
`canonical_search_state`: continuam pendentes até prova operacional específica.

Para ativar este contrato será necessário um novo índice versionado, reconstrução,
reconciliação e troca controlada, com a mesma versão em escritor/leitor. O índice
anterior não deve receber esta projeção. O leitor PIT vincula cursores à versão
do contrato e às identidades do destino. Integração Elasticsearch real, catálogo
de disponibilidade por versão, seletor visual e ordenação permanecem pendentes.

Provas desta rodada: `validacao-busca-campos-canonicos.json`. Testes sintéticos de
contrato não homologam busca real, migração, 50 req/s ou conclusão do projeto.

Validação desta recuperação: 151 testes sintéticos de projeção, filtros e
protocolo PIT, além do build. O teste de outbox comprova que vínculo incompleto
não chega ao transporte nem é confirmado; mudança de versão no seletor invalida
a continuação antes da rede. Elasticsearch é simulado nesses testes. O arquivo
`test_canonical_custom_search_postgres.py` foi preservado, mas sua integração
ainda não foi aprovada: o fixture está bloqueado pela verificação de propriedade
do runtime dentro do sandbox. Nenhum gate foi reduzido para executar o ensaio.

## Disponibilidade e seleção por versão — contrato preparado

`canonical_search_availability.py` acrescenta funções puras, sem ligação HTTP ou
ativação, para avaliar uma definição histórica autorizada e selecionar seu filtro.
O contrato `canonical-search-availability-2026-09-09.1` exige contexto explícito:
implantação do catálogo, UUID do cluster e índice, versão da projeção e SHA256 do
corte de cobertura solicitado. Estado pronto de um catálogo mutável não serve
como prova. Renomear ou inativar não apaga a possibilidade de consulta histórica.

O recibo interno exige seletor completo, contexto idêntico, SHA256 da prova,
reconciliação afirmativa e contagens inteiras de valores esperados/indexados,
omitidos e falhos. Contagens iguais sem reconciliação não liberam a seleção.
Omissão ou falha mantém o filtro pendente; um corte vazio pode ser reconciliado.
Datas têm fuso obrigatório, validade máxima de 120 segundos e vencimento
reavaliado em cada seleção. Prova futura, malformada, de outra versão/índice/corte
ou expirada nunca habilita o filtro. Booleano não pode representar versão inteira.

`custom_filter_availability` retorna `pending`, `blocked`, `stale`, `unsupported`
ou `ready`, com motivo, operadores, seletor e corte. `ready` refere-se somente
à cobertura informada e não atesta indexação global ou ausência de atraso.
`select_custom_filter` reavalia o recibo e passa o critério pelo compilador
existente, preservando null/false/zero, precisão, fontes e flags correlacionadas.
Ordenação permanece indisponível; referência permanece não pesquisável. Várias
versões continuam exigindo seleções explícitas combinadas em `any`.

**Limite de confiança:** o recibo só pode vir de um serviço interno que confira
o artefato de reconciliação e suas identidades. As funções não autenticam recibos,
não abrem arquivos pelo hash, não medem o índice e não aceitam a declaração do
cliente como prova operacional. Esse produtor/verificador, persistência, rotas,
autorização de catálogo, vínculo do corte ao PIT e seletor visual ainda precisam
ser integrados/testados antes de ativação. Nenhum `search_state` é modificado.

Validação desta rodada: **198 testes sintéticos**, incluindo 47 do novo contrato,
sem falhas/erros/skips finais. A tentativa inicial identificou e corrigiu igualdade
indevida entre booleano e versão inteira. Não houve mudança de frontend, build ou
navegador reexecutados, conexão ao PostgreSQL/Elasticsearch real ou alteração da
release. Prova: [validacao-disponibilidade-busca.json](validacao-disponibilidade-busca.json).

## Consulta HTTP de disponibilidade histórica — MAIN, 09/09/2026

`GET /api/v1/canonical/fields/{field_id}/versions/{version}/search-availability`
consulta uma versão imutável do catálogo sob a permissão `read` da aplicação.
Carrega definição/hash no catálogo, confere ID/versão/SHA256 e reavalia a prova
em cada requisição. Não lê a definição atual em substituição à histórica.
Parâmetros extras são recusados: cliente não fornece recibo, destino, corte ou
relógio. Respostas de disponibilidade usam `Cache-Control: no-store`.

A dependência Python `CanonicalReads(..., search_availability_provider=...)`
é optativa e interna ao ensaio sintético. Recebe somente o seletor histórico e
retorna exatamente `target`, `coverage_cut_sha256`, `receipt`. O contexto esperado
deve ser fixado independentemente da prova pelo futuro provedor confiável.
Sem provedor retorna 503; staging/production são recusados mesmo com provedor.
O construtor de implantação não configura essa dependência. Falhas do provedor
retornam código genérico, sem texto de exceção. Contas, escopos e revogação seguem
a política existente; não foram criadas permissões granulares por campo.

A resposta contém `availability` com o contrato anterior e
`search_execution_enabled: false`. Mesmo `selectable: true` é apenas avaliação
preparatória da cobertura; não autoriza execução, não emite cursor e não habilita
busca publicada. Continua obrigatório revalidar prova/autorização/corte na futura
execução e ligar a cobertura ao PIT. Não há provedor real, persistência de provas,
seletor visual ou integração PostgreSQL/Elasticsearch aprovados neste bloco.

Validação: **286 testes aprovados**, incluindo **33 novos** da rota, zero falhas,
erros ou skips finais, dois avisos de depreciação. A conexão do catálogo e o
provedor são simulados; testes adicionais usam autenticação real da aplicação
com contas sintéticas/OTP e chaves revogadas ou sem escopo. A tentativa intermediária
falhou em dois testes novos por cabeçalho incorreto e ausência de configuração
OTP no fixture; corrigidos sem alterar a política de autenticação. Frontend não
mudou e build/navegador não foram reexecutados. Evidência:
[validacao-disponibilidade-http.json](validacao-disponibilidade-http.json).
