# Experimento de armazenamento compacto

Protótipo isolado: não é o repositório canônico da aplicação nem está ativado
para dados reais. Preserva átomos normalizados, metadados e precisão em blocos
imutáveis; o adaptador PostgreSQL aceita somente banco fictício `bigbase_test`
na porta 18769 por socket Unix.

`block_codec.py` limita quantidade, expansão e descompressão; verifica o hash
do bloco e a reconstrução exata de cada registro. `postgres_block_experiment.py`
exercita escrita por COPY, ponteiros, recibos imutáveis, checkpoint transacional
e replay após resposta perdida. Não implementa deduplicação, estado atual por
campo, atualização de flags, fusão/desfusão ou indexação. Esses contratos devem
ser integrados antes de qualquer adoção.

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

Validação integrada em 09/09/2026: **53 testes aprovados**, zero falhas, erros
ou testes ignorados, incluindo o adaptador PostgreSQL no fixture fictício.
