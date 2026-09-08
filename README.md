# BIG BASE

Cadastro unificado de pessoas e empresas com múltiplos documentos, contatos, endereços, usernames e vínculos. Cada informação preserva sua origem, datas, valores recebidos, normalização, validade e histórico. Inclui painel em português, API autenticada e consultas em massa com XLSX assíncrono.

## Estado verificável

O núcleo local está implementado e testado. A fundação PostgreSQL, os adaptadores de migração, a reconciliação por campo e os componentes de busca estão implementados separadamente. **O painel/API ainda usam o adaptador SQLite de desenvolvimento. A integração de produção, o piloto, a migração real e a homologação de escala permanecem pendentes.**

A rodada integrada passou com 896 testes de backend, 14 de precisão, nove de entrada de importação, seis de compatibilidade de ordenação, 22 grupos no navegador e build TypeScript/Vite. Consulte a [validação](docs/VALIDACAO.md), a [matriz de requisitos](docs/IMPLEMENTACAO.md) e o [plano completo](docs/PLANO-PROJETO-COMPLETO.md). Testes sintéticos não comprovam a capacidade da carga real.

## Instalação de desenvolvimento

Python 3.11+, ambiente virtual e Node compatível com as dependências fixadas em `frontend/package-lock.json`. Redis e navegador Chromium são necessários para a integração completa. Execute a partir da raiz do clone, como usuário normal:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
(cd frontend && npm ci && npm run build)
PYTHONPATH=backend .venv/bin/python -m bigbase.cli create-admin --username admin
./scripts/run-local.sh
```

Acesse `http://localhost:18765`; documentação API em `http://localhost:18765/api/docs`. O bootstrap solicita a senha interativamente. No primeiro login, configure OTP compatível com Google Authenticator e preserve os códigos de recuperação exibidos uma vez. Em instalação retomada, restaure o estado privado e a chave de cifragem conforme o roteiro; não recrie nem redefina o administrador existente.

O executor local usa um processo, escuta apenas em loopback e permite cookie HTTP exclusivamente nesse modo local. Não publicar esse executor como produção. Dados sintéticos opcionais podem ser criados em armazenamento vazio com `PYTHONPATH=backend .venv/bin/python -m bigbase.cli seed-demo`.

`var/` contém estado privado e não entra no Git. A chave `var/encryption.key` e a cópia consistente do armazenamento administrativo devem ser preservadas juntas em backup cifrado. Perder a chave impede recuperar os segredos OTP existentes.

## API e contratos

Integrações usam `X-API-Key`, escopos e fontes permitidas. Sessões do painel usam cookie HttpOnly e proteção CSRF. Escritas de enriquecimento e criação/retomada de importação exigem `Idempotency-Key`; alterações de itens exigem `If-Match`. Chaves podem ser rotacionadas com OTP recente, recuperação de resposta idempotente e período de transição. Ausente, null, false e zero têm significados distintos.

Veja [exemplos de API](docs/API-EXEMPLOS.md), [rotação de chaves](docs/API-KEY-ROTATION.md), [autenticação reforçada](docs/ADMIN-STEPUP.md) e [normalização de telefones](docs/NORMALIZACAO-TELEFONES.md). Importações JSON/JSONL, consultas combinadas, pesquisas salvas e exportações completas estão disponíveis no adaptador local, com limites documentados na matriz.

Para testar rate limit compartilhado, `BIGBASE_REDIS_URL` deve apontar para Redis isolado. Indisponibilidade retorna 503, sem liberar tráfego ilimitado. Redis não converte o adaptador cadastral local em armazenamento distribuído.

## Validar o projeto

```bash
.venv/bin/pytest -q
(cd frontend && npm run build)
.venv/bin/python scripts/run-browser-tests.py
```

O teste de navegador prepara e encerra seu próprio ambiente sintético. Não executá-lo contra produção. Testes PostgreSQL/Redis dependem de serviços isolados; testes ignorados significam dependência não validada. Para a rodada completa sem ignorados, preparar o fixture PostgreSQL e executar `.venv/bin/python scripts/run-verification.py`. Veja [CANONICAL-STORE.md](docs/CANONICAL-STORE.md). O instalador privado `scripts/prepare-postgres-test.sh` atualmente suporta Debian 12 amd64; outros sistemas exigem adaptação e validação, sem reaproveitar arquivos binários do host anterior.

As evidências operacionais novas são produzidas em `var/validation/` e excluídas do Git. O manifesto público é uma seleção revisada, sem dados operacionais privados.

## Banco definitivo e migração

PostgreSQL será a fonte oficial única; Elasticsearch será uma projeção de busca reconstruível. A migração registra operações, campos e containers, preserva valores ambíguos e exige reconciliação por releitura do destino antes de declarar conclusão. Nenhum dado real é carregado automaticamente ao instalar este repositório.

Consulte [cadastro canônico](docs/CANONICAL-STORE.md), [adaptadores](docs/ADAPTADORES-MIGRACAO.md), [executor](docs/EXECUTOR-MIGRACAO.md), [reconciliação](docs/RECONCILIACAO-CANONICA.md), [verificação prévia](docs/PREFLIGHT-MIGRACAO.md), [projeção de busca](docs/PROJECAO-BUSCA.md) e [cursor PIT](docs/BUSCA-PAGINADA-PIT.md).

## Novo servidor e continuidade

Siga [RETOMADA.md](docs/RETOMADA.md) e [INFRAESTRUTURA.md](docs/INFRAESTRUTURA.md). Código, plano e testes ficam no Git. O snapshot original, os segredos, a conta administrativa e as provas operacionais ficam em cópia privada separada. O backup original já validado não inclui automaticamente as alterações posteriores do projeto.

`infra/bigbase-development.service` é um exemplo para usuário dedicado `bigbase` e diretório `/opt/big-base`; adaptar e verificar antes de instalar. Ele não é configuração de produção nem instrui alterar serviços já existentes.

**Lembrete de fechamento solicitado pelo proprietário:** após concluir implementação, migração e homologação, lembrá-lo de tornar este repositório privado. Nunca publicar dados pessoais ou credenciais, independentemente da visibilidade.
