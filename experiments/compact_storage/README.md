# Experimento de armazenamento compacto

Protótipo isolado: não é o repositório canônico da aplicação nem está ativado
para dados reais. Preserva átomos normalizados, metadados e precisão em blocos
imutáveis; o adaptador PostgreSQL aceita somente banco fictício `bigbase_test`
na porta 18769 por socket Unix.

`block_codec.py` limita quantidade, expansão e descompressão; verifica o hash
do bloco e a reconstrução exata de cada registro. `postgres_block_experiment.py`
exercita escrita por COPY, ponteiros, recibos imutáveis, checkpoint transacional
e replay após resposta perdida. Não implementa deduplicação, persistência do
estado atual por campo, fusão/desfusão ou indexação. Esses contratos devem ser
integrados antes de qualquer adoção.

`field_replay.py` reconstrói observações e estado de campos usando a mesma regra
de precedência extraída do repositório canônico. A comparação verifica todas as
colunas do histórico e do estado, além das versões dos itens. É um ensaio em
memória para operações cuja entidade já foi determinada: ainda não constitui
registro global de identidade, catálogo ou repositório persistente completo.

O codec de bloco **2** preserva explicitamente a ordem das flags, que determina
a posição dos eventos dentro da operação. A primeira comparação independente
encontrou que o codec 1 ordenava chaves JSON e podia alterar essas posições.
Blocos antigos sem ambiguidade continuam legíveis; blocos da versão 1 com
múltiplas flags são recusados para impedir a invenção de uma ordem histórica.
O formato novo inclui essa ordem no hash do registro e do bloco. As derivadas
de metadados da versão 1 não foram redefinidas.

Os dados de `synthetic_samples.py` são fictícios. Não colocar amostras reais,
credenciais, arquivos de banco ou medições que contenham valores pessoais aqui.
As medições operacionais das fontes permanecem em diretório privado.

Execução dos testes de codificação:

```bash
PYTHONPATH=backend:experiments/compact_storage .venv/bin/python -m pytest -q experiments/compact_storage/test_block_codec.py experiments/compact_storage/test_derived_codec.py experiments/compact_storage/test_wire_codec.py
```

O ensaio PostgreSQL adicional requer `BLOCK_EXPERIMENT_DSN` apontando para um
fixture fictício explicitamente criado. O teste recusa o banco canônico. O
fixture é criado/encerrado separadamente; nenhum serviço global é instalado
por estes módulos.

A medida inicial de 600 registros fictícios confirmou a reconstrução de todos
os átomos, com crescimento de tabelas/índices de 1.270 a 8.315 bytes por registro
conforme a quantidade de campos. Isso não inclui estado atual, índices de
identidade/deduplicação, busca e backups; não constitui dimensionamento final.
Veja `docs/DADOS-E-MIGRACAO.md` para os critérios de adoção.

Validação inicial, no commit `7c21d39` e codec de bloco 1: **53 testes aprovados**,
incluindo o adaptador PostgreSQL no fixture fictício. As medidas acima pertencem
àquela versão e não dimensionam o modelo completo nem o codec posterior.

Validação do codec 2 e do mecanismo comum: **87 testes aprovados**, zero falhas,
erros ou testes ignorados. Inclui comparação independente com o repositório da
versão anterior: **126 operações e 378 observações**, em pessoas e empresas.
Esse ensaio cobre múltiplas fontes, valores exatos, dados atrasados/futuros,
status pendente, vários itens e flags com vínculo ao valor confirmado.

Para incluir a reconstrução, acrescente `backend/tests` ao `PYTHONPATH` e defina
`BIGBASE_TEST_PG_DSN` para o mesmo fixture isolado. `COMPACT_REPLAY_REFERENCE`
pode apontar ao `canonical_store.py` de uma versão anterior imutável, sem o
novo resolvedor. Isso usa apenas seu código: as conexões continuam restritas
ao PostgreSQL fictício. Não apontar os DSNs de teste ao cadastro real.
