# Busca na implantação canônica

A aplicação dispõe de um leitor de busca separado do consumidor de indexação.
O leitor usa uma chave somente de leitura, restrita ao índice canônico, e recebe
um recibo de conferência que fixa banco, versão da projeção, índice e mapeamento.
A ativação não representa conclusão da migração: a interface identifica a
cobertura dos cadastros já indexados e mantém o andamento da carga separado.

O painel oferece filtros combinados por todos/qualquer critério ou pelo mesmo
telefone, endereço ou item. Texto completo/parcial, documento, telefone, email,
endereço, usernames e atividades usam os campos suportados pela projeção.
Validade e WhatsApp preservam `true`, `false` e `null`. Paginação mantém a mesma
consulta e autorização; encerrar a pesquisa libera seu cursor. Números extensos
seguem como literais exatos, sem passar por arredondamento JavaScript.

A chave dos cursores é persistente. Reiniciar a aplicação permite continuar uma
consulta enquanto o ponto de leitura do Elasticsearch continuar válido. Troca
de identidade, permissões, configuração, credenciais ou recibo invalida o acesso;
o cliente não recebe as chaves nem acesso às fontes restauradas.

Limites atuais: a ordem canônica é por ID crescente; ordenação múltipla, seleção
visual de campos adicionais indexados e consulta em massa com XLSX ainda estão
pendentes de integração. Não interpretar a presença de uma definição no catálogo
como confirmação de que ela já pode ser pesquisada.

Validação desta candidata: 311 testes de backend, 12 testes com PostgreSQL real
em banco separado e dados fictícios, build e 12 grupos de navegador com API
simulada, sem falhas nas execuções finais. A publicação ainda exige conferir a
projeção real e seu recibo. Os ensaios encontraram e corrigiram uma condição de
corrida na primeira consulta após autenticar e rótulos acessíveis dos filtros.

A regressão integral posterior confirmou **1.540 testes aprovados**, sem falhas,
erros ou testes ignorados, usando bancos fictícios isolados e Redis privado.
Essa prova amplia a cobertura funcional; não homologa o volume real nem a carga
de 50 requisições por segundo.
