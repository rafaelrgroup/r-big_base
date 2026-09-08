# Adaptadores das bases legadas

Estado: implementação pura validada com dados sintéticos. Não representa uma
migração executada, um mapeamento semanticamente homologado dos dados reais ou um
teste de capacidade dos índices completos. Nenhuma API ou índice atual foi lido
pelo adaptador.

## Inventário utilizado

O adaptador considera as fontes distintas `pessoas` e `pessoas_serasa`. Foram
lidos somente o inventário de campos e os metadados do manifesto do backup:

| Artefato | Cobertura / SHA-256 |
|---|---|
| `docs/mapeamento-inicial-campos.csv` | 107 caminhos em pessoas, 33 em pessoas_serasa; `e185842ac045cf7516af99c8880d36abae7377d40143fca6545da7579b2ba945` |
| Manifesto operacional privado (`backup-manifesto.json`) | Mappings e formatos de datas utilizados para definir os adaptadores; o manifesto, seus identificadores e a evidência original permanecem fora do repositório público. |

Campos de objeto do inventário têm um registro de estrutura. Cada folha abaixo
deles tem seu próprio fato. Campos novos, fora desses 140 caminhos, também são
preservados; não existe uma lista de descarte.

## Contrato do módulo

`backend/bigbase/source_adapters.py` expõe:

- `map_record(source_id, external_id, record, source_version=None,
  adapter_version=ADAPTER_VERSION)`: transforma apenas um registro e não faz I/O.
- `ExactDecimal`: usar no leitor JSON como `parse_float=ExactDecimal`.
- `exact_json(value)`: serialização JSON ASCII, sem converter Decimal em float.
- `hash_value(value)`: hash tipado que distingue false, zero, inteiro, decimal,
  texto e formatos de coleção.
- `reconcile_coverage(record, mapped)`: compara caminhos, valores, tipos, literais
  disponíveis, hashes e estruturas de coleções.

A saída contém origem, ID e versão da origem, hash do registro, versões das
regras, candidato de identidade opcional, fatos, estruturas de containers e
relatório de cobertura. Não contém uma cópia integral permanente adicional do
registro. Cada fato contém:

- ID determinístico, origem/registro/versão e JSON Pointer do valor recebido;
- `input_value`, `input_type`, `input_json`, `input_encoding`, `input_hash` e
  indicador de disponibilidade do lexema decimal;
- valor normalizado, tipo e JSON desse valor;
- `target_kind`, `target_path`, `item_key`, atributos derivados do item e
  metadados da normalização;
- estado `mapped` ou `pending`, motivo, notas e versões das regras;
- datas de observação/atualização desconhecidas como null e `flags={}`.

`mapped` indica interpretação da representação, não confirmação de realidade,
titularidade, validade ou atualidade. Metadados como `syntax_valid` são distintos
das flags cadastrais.

IDs dos fatos incluem origem, registro, versão/hash da entrada, versão do
adaptador, versão do normalizador e caminho. Repetir a mesma entrada com as mesmas
versões produz os mesmos IDs. Uma versão nova produz fatos novos; o armazenamento
deve acrescentá-los e conservar os anteriores. IDs/chaves de itens agrupam
componentes do mesmo valor e contexto, independentemente da versão de regra.

## O que foi interpretado

| Entrada | Destino efetivamente implementado |
|---|---|
| CPF e `pessoas.doc.CPF` | Documento CPF textual, normalização e verificador; candidato apenas quando todas as declarações não vazias são válidas e concordantes |
| NOME | Nome principal, preservando entrada e normalizando espaços exteriores/NFC; coleção de nomes permanece pendente |
| SEXO | Texto informado, sem inferir sexo a partir do nome e sem impor códigos novos |
| `pessoas.DT_NASCIMENTO`, DT_OBITO; `pessoas_serasa.NASC` | Data civil ISO quando inequívoca; epoch_millis apenas no campo cujo mapping declara esse formato |
| Datas SERASA declaradas e DT_INCLUSAO_SERV_PB | Representação ISO/epoch interpretada, mas significado/alcance temporal do campo permanece pendente; não data outros fatos |
| NOME_MAE/NOME_PAI e variantes explicitamente listadas; SERASA_nome_conjuge | Relação por nome, sempre pendente de resolução da identidade; nenhum CPF/ID de parente é inventado |
| CELULAR1–5, TEL_FIXO1–5; TELEFONES | Contatos individuais ou elementos escalares de arrays; normalizador telefônico versionado, classificação pelo número e não pelo rótulo da coluna |
| EMAIL; EMAILS | Endereço individual/elemento de array, domínio normalizado; listas textuais ou sintaxe não reconhecida permanecem pendentes |
| BAIRRO, CEP, CIDADE, COMPLEMENTO, LOGRADOURO, NUMERO, TIPO_ENDERECO, UF | Componentes de um endereço plano em pessoas |
| ENDERECOS_JSON e seus componentes declarados | Cada objeto/endereço da coleção recebe uma chave própria; cidade, CEP, número etc. não são cruzados entre objetos |
| RG/TITULO_ELEITOR e variantes explicitamente listadas | Documento textual com tipo; validação específica pendente |

Campos `SERASA_NOME`, `SERASA_nome_completo`, nomes civis e partes de nomes
continuam distintos e pendentes de homologação semântica. Renda, profissão,
classificações, estados civis, identificadores internos, flags legadas e campos
desconhecidos ficam como fatos adicionais por caminho. A existência de uma flag
legada não se converte automaticamente em `valid`, `is_whatsapp` ou confirmação.

Telefones agregados em strings não são separados por delimitadores presumidos.
País Brasil não é atribuído pelo simples nome CELULAR/TEL_FIXO ou pela existência
de CPF. Número nacional atual pode ser interpretado pelo normalizador existente;
a conversão histórica de nono dígito exige o contexto explícito exigido por sua
regra, como prefixo internacional +55. Número nacional legado sem esse contexto
continua pendente, com alternativas auditadas. Ramais e demais componentes
derivados ficam associados à observação da folha original.

CPF numérico, inválido, conflitante ou ambíguo não gera candidato de identidade.
Homônimos, telefones e endereços iguais não geram candidato. Mesmo um CPF válido
é apresentado como candidato verificável; políticas de conflito/titularidade do
armazenamento continuam obrigatórias.

## Precisão e caracteres especiais

`ExactDecimal` conserva o token decimal recebido, inclusive `1.23000e+5` e
`-0.00`. Quando o leitor fornece apenas Decimal, conserva-se seu valor exato,
marcado como representação canônica do valor disponível. Não se afirma ter
preservado a grafia original de inteiros ou os escapes originais de strings.
Floats já fornecidos pelo chamador são identificados separadamente; o adaptador
não consegue recuperar precisão que o leitor anterior já perdeu.

NUL, surrogates isolados, Unicode, barras e tildes de chaves são mantidos. Valores,
metadados e caminhos precisam ser gravados pelo consumidor como **JSON ASCII em
TEXT**, com hash para identificação/indexação quando necessário. Não enviar o
valor decodificado com NUL diretamente a PostgreSQL TEXT/JSONB. `input_json` e
`normalized_json` fornecem a representação escapada; `exact_json` atende o restante
dos metadados. O adaptador não substitui caracteres por interrogação nem os remove.

Containers registram caminho, tipo objeto/array e quantidade de elementos,
inclusive quando vazios. Isso distingue `{"0": false}` de `[false]`, que teriam
folhas com caminhos iguais sem a informação estrutural. Posições repetidas em
arrays conservam observações separadas mesmo que o item canônico seja deduplicado.

## Limites e pendências

Limites por registro: 20.000 folhas, profundidade 64 e 2 MiB da representação JSON
ASCII. Registro acima do limite gera erro explícito, sem resultado truncado; o
transporte deve registrar a pendência e preservar a possibilidade de nova leitura.
Não há varredura dos bancos, amostragem real, escrita ou relógio dentro do mapper.

Faltam homologação com amostras autorizadas, regras para strings agregadas reais,
revisão de sinônimos/códigos de origem, vinculação comprovada de datas a cada fato,
resolução de identidades/relações conflitantes e testes de carga. Normalização
recursiva de novos formatos não reconhecidos deve acrescentar regras versionadas,
sem remover o destino pendente nem seus valores antigos.

## Evidência

`backend/tests/test_source_adapters.py`: **41 testes aprovados** em 08/09/2026.
Cobrem todos os caminhos do inventário com dados sintéticos, coleções vazias e
repetidas, null/false/zero, nomes distintos, CPF conflitante, datas ambíguas,
endereços múltiplos, telefones/contatos, Decimal exato, deepcopy do lexema,
NUL/surrogates, identidade determinística e detecção de perdas/alterações.

Execução: `.venv/bin/pytest -q backend/tests/test_source_adapters.py`.
