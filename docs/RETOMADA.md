# Retomada do projeto em outro servidor

O repositório público contém código, testes e documentação técnica. Ele não substitui o backup dos bancos nem a cópia privada do estado atual do painel. A migração real e a homologação de capacidade continuam dependentes do destino provisionado; consulte a matriz em `docs/IMPLEMENTACAO.md`.

O ponto de retomada em 08/09/2026 é a rodada com **896 testes de backend e 22 grupos de navegador aprovados**, com evidências identificadas na matriz. O painel de desenvolvimento foi atualizado preservando a conta existente. **Zero registros reais foram migrados para o novo cadastro.** O próximo bloco de implementação é conectar os serviços HTTP de consulta e atualização ao repositório PostgreSQL canônico, à projeção de busca e às filas duráveis, mantendo os contratos e o histórico já testados. O painel/API ainda usam o armazenamento local de desenvolvimento; a fundação canônica e os ensaios sintéticos não significam integração concluída nem homologação do volume real.

## Cópias necessárias

| Cópia | Conteúdo | Comprovação necessária |
|---|---|---|
| Repositório público | Código, dependências fixadas, testes, esquema canônico e documentação | Revisão de conteúdo e commit identificado |
| Snapshot original, no armazenamento pessoal | Índices de origem e configurações anteriores à consolidação | Manifesto do snapshot e relatório da restauração isolada aprovada |
| Pacote privado do projeto | Código atual, planos completos, scripts operacionais, checkpoints, provas de teste, cópia SQLite consistente, chave de cifragem, primeiro acesso e configurações de serviço | SHA256 do arquivo cifrado, descriptografia e conferência de todos os arquivos do manifesto |

O pacote privado usa GPG com AES256, permissões restritas e senha solicitada no Ubuntu. Não há senha em argumentos, relatórios ou no repositório. O recibo compartilhável contém somente versão do formato, hashes, contagens, data e resultado. Esse recibo registra a verificação executada no Ubuntu; ele não substitui o arquivo cifrado nem sua senha, e não atesta uma migração de dados.

## Captura privada no Ubuntu

O operador fornece o apelido SSH já configurado e uma pasta pessoal para `baixar-handoff-ubuntu.sh`. O procedimento copia o verificador, confere primeiro a leitura dos arquivos no servidor e aguarda a criação e repetição da senha no terminal. Só depois recebe um fluxo por SSH, cifra localmente e confere automaticamente cada arquivo sem extrair seu conteúdo. A senha trafega para o cifrador somente por um canal local de memória, separado do conteúdo do pacote; não entra em argumentos, variáveis de ambiente ou arquivo de senha. Somente após todas as verificações grava o recibo e o envia ao servidor. A instrução operacional com os caminhos da instalação fica fora do repositório público.

O emissor faz uma cópia online consistente do SQLite e confere sua estrutura; a chave de cifragem e o arquivo de primeiro acesso acompanham essa cópia somente no pacote privado. Não redefine usuários, senhas ou OTP. Os arquivos estáticos são conferidos antes e depois da captura; uma alteração concorrente exige repetir o procedimento. O pacote representa esse instante: alterações posteriores exigem nova captura.

São excluídos o diretório `.git` de trabalho, dependências reinstaláveis, caches, runtimes de ensaio, banco PostgreSQL sintético, workspace temporário do navegador, credenciais SSH/Codex e os bancos/snapshot de origem. Quando preparado e auditado, um arquivo Git bundle separado acompanha os documentos operacionais para preservar o commit e seu histórico portátil. Esse bundle não comprova publicação remota; a publicação deve ser confirmada separadamente. O build atual do painel e as evidências selecionadas são incluídos. Novos arquivos privados fora da seleção exigem revisão explícita do inventário. O limite atual é 128 MiB por pacote e 32 MiB por arquivo; excedê-lo interrompe o processo sem omitir dados silenciosamente.

Preserve juntos o arquivo `.tar.gz.gpg`, `SHA256SUMS`, `recibo.json`, `verificacao.json` e `pacote-handoff.py`. Guarde a senha em local separado. Faça uma segunda cópia do conjunto em outro armazenamento. Não envie o pacote privado, arquivos extraídos ou sua senha ao Git público.

## Restauração do projeto

1. Confira o SHA256 do arquivo cifrado. Descriptografe por fluxo para o verificador antes de extrair. A senha correta deve permitir conferir todos os arquivos do manifesto.
2. Extraia somente em uma pasta privada vazia, com diretórios `0700` e arquivos `0600`, fora do checkout público. Confira o manifesto para localizar `projeto/`, `privado/painel/`, `privado/validacao/`, `privado/levantamento-db/` e `privado/systemd/`.
3. Reinstale dependências a partir dos arquivos fixados. Leia as instruções e pendências antes de provisionar PostgreSQL, busca e filas no destino. Os runtimes e bancos sintéticos não foram transportados e podem ser recriados pelos scripts de ensaio.
4. Para retomar o painel local, restaure conjuntamente o SQLite e sua chave na pasta privada configurada para a aplicação. Preserve também o arquivo de primeiro acesso. Nunca inicialize o sistema por cima desse estado nem execute redefinição administrativa como parte da restauração.
5. Trate as unidades de serviço antigas como referência. Ajuste caminhos, usuário, rede, destinos e limites de recursos antes de qualquer ativação. **O monitor e seu agendador devem permanecer desativados no novo servidor até configuração explícita**, mesmo que um checkpoint antigo indique autorização para continuar.
6. Execute os testes sintéticos e confira login/OTP e as funcionalidades do painel no destino isolado. Registre o commit, o hash do pacote e as novas evidências. Testes pequenos não homologam o volume real.
7. Reconfirme o snapshot original e sua restauração, a identidade e capacidade do novo destino, o piloto e a reconciliação por campo antes de migrar dados ou trocar tráfego. Preserve a operação anterior enquanto esses requisitos não forem satisfeitos.

Nenhuma restauração deve ativar serviços automaticamente, reutilizar endereços antigos por suposição ou tratar um checkpoint copiado como prova de que uma execução está ativa no novo servidor.

## Continuidade da tarefa

A aplicação pode transferir a mesma tarefa para outro host conectado que tenha o mesmo repositório Git com o trabalho salvo. Essa ação é feita pelo usuário no seletor de localização da tarefa, após preparar o destino; não é uma transferência automática dos bancos ou arquivos privados. Consulte o [procedimento oficial de transferência entre hosts](https://learn.chatgpt.com/docs/remote-connections#hand-off-a-chat-between-hosts).

Também é possível abrir uma nova tarefa no checkout do novo servidor. Ela deve ler `AGENTS.md`, o plano completo, `docs/IMPLEMENTACAO.md` e este documento, conferir os recibos e checkpoints privados disponíveis e continuar a partir das pendências verificadas. A retomada não deve depender da memória desta conversa, presumir uma migração concluída ou ativar o monitor automaticamente.
