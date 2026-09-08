# Normalização auditada de telefones

Regra `br-anatel-2026-09-08.1`, implementada em `backend/bigbase/phone_rules.py`. A versão da biblioteca também é registrada em cada decisão. Ensaios usam somente números sintéticos e não consultam operadoras, titulares ou WhatsApp.

## Decisões e fontes oficiais

A transformação histórica aceita número local de oito dígitos começando com **8 ou 9**: a redação original do art. 19 da Resolução Anatel 301/2002 destinava esses identificadores ao SMP. A faixa com os dois dígitos seguintes iguais a `00` era reservada e foi excluída. Essa resolução foi revogada posteriormente; é usada como evidência **histórica**, não como norma atual. [Resolução 301/2002, texto original e alterações](https://informacoes.anatel.gov.br/legislacao/component/content/article/17-resolucoes/2002/90-resolucao-301).

A inclusão acrescenta o 9 à esquerda do número local; o DDD não muda. O cronograma é nacional e ocorreu de 2012 a 2016. A Resolução 553/2010 documenta a mudança do formato SMP e diferencia STFC e SME; também é uma referência histórica revogada. [Resolução 553/2010](https://informacoes.anatel.gov.br/legislacao/resolucoes/25-2010/16-resolucao-553), [cronograma publicado pela Anatel](https://www.gov.br/anatel/pt-br/regulado/perguntas-frequentes).

| Data da implantação | DDDs |
|---|---|
| 2012-07-29 | 11 |
| 2013-08-25 | 12, 13, 14, 15, 16, 17, 18, 19 |
| 2013-10-27 | 21, 22, 24, 27, 28 |
| 2014-11-02 | 91, 92, 93, 94, 95, 96, 97, 98, 99 |
| 2015-05-31 | 81, 82, 83, 84, 85, 86, 87, 88, 89 |
| 2015-10-11 | 31, 32, 33, 34, 35, 37, 38, 71, 73, 74, 75, 77, 79 |
| 2016-05-29 | 61, 62, 63, 64, 65, 66, 67, 68, 69 |
| 2016-11-06 | 41, 42, 43, 44, 45, 46, 47, 48, 49, 51, 53, 54, 55 |

O catálogo acima contém 67 DDDs. Suas datas são datas da regra de numeração, **não datas de atualização ou observação do cadastro**. Uma fonte sem data continua sem data. A tabela é uma transcrição do [cronograma da Anatel](https://www.gov.br/anatel/pt-br/regulado/perguntas-frequentes).

O prefixo local 7 pertence ao serviço especializado/rádio e fica classificado como `other`; não recebe 9. Prefixos 2–5 são próprios da telefonia fixa, mas a classificação final continua exigindo formato aceito pela biblioteca. A classificação `fixed` não atribui uso residencial. [Perguntas de numeração da Anatel](https://www.gov.br/anatel/pt-br/regulado/numeracao/perguntas-frequentes).

## Condições implementadas

Uma conversão histórica exige cumulativamente:

1. País Brasil explícito em `country: "BR"` ou prefixo internacional `+55`, sem conflito entre ambos.
2. DDD presente no catálogo auditado, incluído no número ou fornecido separadamente em `ddd` como texto.
3. Número local de oito dígitos, começando com 8/9 e fora da faixa reservada mencionada.
4. Candidato com 9 acrescentado aceito como celular brasileiro pela versão fixada do libphonenumber.

Ausência de país explícito mantém a alternativa em revisão. O contexto padrão Brasil da aplicação continua disponível para interpretar formatos atuais; ele aparece como `application_default_BR` na decisão e **não habilita a inclusão histórica**.

Prefixos 6 ficam pendentes porque não existe, nesta versão, tabela histórica específica que elimine suas ambiguidades. Números sem DDD, DDD inválido, conflito de país/DDD, letras, formatos especiais não interpretados e ramais divergentes são preservados. Não há conversão cega de letras para dígitos. Números internacionais e especiais seguem a interpretação da biblioteca, sem receber a regra brasileira do nono dígito.

Cada valor contém `phone_normalization`: versão, versão da biblioteca, componentes recebidos, decisão, motivo, regra, fontes, número anterior, saída, alteração de dígitos e eventuais candidatos. `canonical_number` é null quando o formato continua pendente; `number` mantém a entrada. Quando resolvido, `number` e `canonical_number` contêm E.164, com ramal separado. Não existe chamada externa para executar essa normalização.

## Confirmações, idempotência e correção

`syntax_valid` informa apenas conformidade de formato. O normalizador não cria `valid`, `is_whatsapp`, confirmação de titularidade ou de uso residencial.

Uma confirmação enviada junto ao número antigo de oito dígitos permanece vinculada àqueles dígitos. Após a transformação, sua observação é preservada com `applied: false` e `pending_reason: "phone_number_transformed"`; ela não confirma automaticamente o número novo. Uma atualização posterior direcionada ao contato atual, com a versão exigida pela API, pode confirmá-lo ou invalidá-lo independentemente.

Reprocessar o número canônico não acrescenta outro 9 e mantém a chave de deduplicação. `original_phone_components()` recupera os componentes recebidos para uma revisão sem alterar o estado salvo. A API preserva `input_value` no evento de alteração do número. O fluxo operacional de desfazer/revisar uma normalização em dados definitivos ainda depende do módulo de revisão e da migração; esta função não executa remoção, fusão nem atualização retroativa.

## Cobertura e limites

`backend/tests/test_phone_rules.py` cobre ambos os prefixos em todos os 67 DDDs, cronograma, ramais com zeros, países e DDDs conflitantes, linhas fixas, rádio, formatos internacionais/especiais, letras, faixas reservadas, idempotência, recuperação da entrada e ausência de transferência de flags. Isso valida a regra implementada, não a precisão de todos os contatos das bases reais.

Antes da migração integral ainda será necessário medir em piloto quantos valores foram normalizados, quantos permaneceram pendentes, conflitos e impacto da deduplicação. Prefixos históricos adicionais só podem ser incorporados em nova versão, com evidência oficial específica, testes e preservação das decisões anteriores.
