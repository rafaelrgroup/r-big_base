# Validação verificável

Rodada `20260908T201556Z-d1b260c6`, concluída em 08/09/2026. O [manifesto público](validacao-integrada.json) registra hashes de cada arquivo de execução/testes e resultados, sem credenciais, dados de produção ou localização de backup. Os hashes de código antes e depois foram idênticos. Documentação e instruções de transferência foram atualizadas depois. O exemplo `infra/bigbase-development.service` passou a usar usuário `bigbase` e diretório `/opt/big-base`; seu hash publicado corresponde ao arquivo da rodada original, preservado na prova. Essa adaptação do exemplo foi verificada separadamente e não altera o backend/frontend testados.

| Verificação | Resultado |
|---|---|
| Backend | 896 aprovados; zero falhas, erros ou ignorados |
| TypeScript e Vite | Build aprovado |
| Precisão de valores | 14 aprovados |
| Entrada de importação | 9 aprovados |
| Compatibilidade de ordenação | 6 aprovados |
| Fluxos no navegador | 22 grupos aprovados |

Os fluxos incluem login e primeiro OTP, convite de usuário, enriquecimento/histórico, campos administráveis, preservação de inteiros grandes, telefones legados, pesquisas/filtros/ordenações, importações, XLSX, responsividade, autenticação reforçada, rotação idempotente de chave, revogação de cadeia e logout com invalidação de sessão.

O ensaio utilizou PostgreSQL 18.6 real isolado e dados sintéticos. Elasticsearch foi simulado no transporte HTTP. Nenhum ensaio usa os cadastros reais. Não comprova capacidade de produção, alta disponibilidade ou migração integral. Pendências obrigatórias estão na [matriz](IMPLEMENTACAO.md).

## Reproduzir

Preparar as dependências do README e o PostgreSQL sintético seguindo [CANONICAL-STORE.md](CANONICAL-STORE.md). O executor completo é `.venv/bin/python scripts/run-verification.py`; exige fixture privado já criado e gera evidências novas com data própria. Não executá-lo em produção. PostgreSQL, Redis e navegador precisam estar disponíveis: uma execução com testes ignorados não aprova a integração completa.

Logs e artefatos completos ficam em `var/validation/`, fora do Git. Tentativas anteriores com falha são preservadas para revisão. Não se deve publicar o relatório operacional automaticamente: gerar uma versão sem informações privadas após revisão.
