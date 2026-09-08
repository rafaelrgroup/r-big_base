# BIG BASE

- Leia `docs/IMPLEMENTACAO.md` antes de ampliar o escopo. Em uma implantação existente, consulte também o plano operacional privado definido para esse ambiente; ele não faz parte do repositório público.
- Desenvolvimento autorizado agora; migração e alterações de produção dependem de restauração verificada e ambiente de destino definido.
- Não alterar a aplicação cadastral original, Elasticsearch em 9200, seus índices ou Nginx de uma implantação existente. Não carregar dados reais neste adaptador de desenvolvimento.
- Usar dados sintéticos e manter o adaptador SQLite identificado como local, de processo único. Não descrevê-lo como PostgreSQL ou como banco definitivo.
- Preservar rastreabilidade, triestado, eventos atrasados, idempotência e fontes. Não apagar histórico para corrigir um dado.
- Nenhuma declaração de entrega integral sem a matriz de requisitos e evidências concluída.
- Nunca versionar `var/`, segredos, OTP, senhas ou arquivos de dados pessoais. Não publicar este ambiente de teste externamente.
- Testes: `.venv/bin/pytest -q`; painel: `cd frontend && npm run build`. Fluxos de navegador: `.venv/bin/python scripts/run-browser-tests.py`; prepara e encerra seu próprio servidor 18767 com dados sintéticos. Não execute o fixture contra o armazenamento principal.

- Coordenação de implementação em ambientes que utilizam o agendador privado: `bigbase-continue-after-backup.timer` verifica as condições a cada dois minutos. Antes de uma rodada manual, confira o estado de `bigbase-continue-after-backup.service`; se houver worker de implementação ativo, encerre somente essa unidade e confira seu checkpoint antes de editar. Marque então `mode: active` no arquivo `desenvolvimento-em-andamento.json` do diretório operacional privado, preservando os demais campos e removendo o `continuation_run_id` da rodada anterior. Isso evita execução concorrente. Não encerre a API de produção nem o painel para coordenar desenvolvimento. Não crie nem altere esse agendador ou seus marcadores ao instalar uma cópia independente deste repositório.
- Rodadas automáticas usam `codex exec --ephemeral` e recebem `continuation_run_id` do agendador; preserve esse identificador ao marcar `active` e na saída. Só libere seu próprio marcador. Ao terminar e registrar testes/pendências, use `mode: armed` se há próximo bloco independente; `awaiting_destination` somente quando todo trabalho restante depende do novo destino. `armed` e o legado `awaiting_backup` autorizam uma nova rodada após backup aprovado e intervalo de segurança; nunca significam entrega integral. O monitor não retoma a sessão aberta do desktop e não cria outra tarefa persistente.
- Preserve as contas existentes de uma implantação; não redefina senha/OTP nem imprima credenciais em logs ou relatórios. Testes de autenticação usam o ambiente sintético separado.

- Lembrete solicitado pelo proprietário: após implementação, migração e homologação completas, lembrá-lo de mudar o repositório para privado. Nunca enviar segredos/dados pessoais ao Git enquanto público ou privado.
