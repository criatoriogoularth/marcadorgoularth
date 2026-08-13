# Marcador Digital Goularth — versão Web

Site que substitui o programa de mesa (Tkinter): cronômetro de provas com
celular funcionando como "ESP32 virtual". Sem login — o código da sala é a
única "chave" de acesso. Os dados de cada sala ficam guardados no banco
(Neon/Postgres) por até 48 horas e são apagados automaticamente depois disso.

## 1. Criar o banco de dados grátis no Neon

1. Cria uma conta em [neon.tech](https://neon.tech) (sem cartão de crédito).
2. Cria um projeto novo. O Neon já cria um banco padrão pra você.
3. No painel do projeto, copia a **Connection String** (algo como
   `postgresql://usuario:senha@ep-xxxxx.aws.neon.tech/neondb?sslmode=require`).
4. Isso vai virar a variável de ambiente `DATABASE_URL` (ver abaixo). O
   programa cria as tabelas sozinho na primeira vez que rodar — não precisa
   criar nada manualmente no Neon.

## 2. Rodar localmente (pra testar antes de subir)

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://usuario:senha@ep-xxxxx.aws.neon.tech/neondb?sslmode=require"
python app.py
```

(No Windows, troca `export` por `set` no CMD, ou `$env:DATABASE_URL="..."` no
PowerShell.)

Abre em `http://localhost:5000`. Se aparecer no terminal "✅ Banco de dados
conectado", está tudo certo. Se aparecer um aviso dizendo que não achou a
`DATABASE_URL`, o site ainda abre (as telas carregam), mas nenhuma ação que
mexe em dado (criar sala, cadastrar, vincular, etc.) vai funcionar até você
configurar a variável.

## 3. Subir no Render

1. Suba esta pasta num repositório do GitHub (ou conecte direto se o Render
   permitir upload de pasta).
2. No Render, crie um **Web Service** novo apontando pro repositório.
3. Configurações:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --worker-class gthread --threads 8 --workers 1 --bind 0.0.0.0:$PORT`
4. Em **Environment**, adiciona a variável `DATABASE_URL` com a connection
   string do Neon (a mesma do passo 1). Opcionalmente, adiciona também
   `SECRET_KEY` (qualquer texto aleatório longo) — sem ela o site gera uma
   sozinho, mas aí toda vez que o Render reiniciar o servidor sua sessão
   de admin (`/admin`) vai pedir senha de novo.
5. Plano gratuito do Render serve bem pra isso (o app é leve). O servidor
   ainda "dorme" depois de um tempo sem acesso — mas agora isso não é mais
   problema: os dados moram no Neon, não na memória do programa, então
   sobrevivem ao servidor dormir e acordar. (O Neon também "dorme" depois de
   5 minutos parado, mas acorda sozinho na primeira consulta seguinte, em
   menos de 1 segundo — sem perder nenhum dado.)

## Como usar

1. Organizador acessa o site → **Criar nova sala** → recebe um código
   (ex: `AB12CD`). A sala fica guardada por 48 horas a partir do último uso;
   depois disso é apagada automaticamente do banco.
2. Na tela do organizador: aba **Cadastro** pra cadastrar os pássaros (nome,
   anilha, proprietário), depois **Adicionar todos à prova** manda pra
   Eliminatória ou Final (depende de qual aba você entrou no Cadastro).
3. Clica em **📟 Link do celular** pra copiar o link e mandar pra quem vai
   usar o celular como marcador. Cada pessoa abre o link, escolhe o pássaro
   que vai marcar, e o celular vira um "ESP32 virtual" (segura o botão pra
   marcar o canto).
4. Organizador clica em **Iniciar Prova** quando estiver pronto. Um relógio
   grande mostra o tempo restante; quando bate zero, a prova finaliza
   sozinha e os celulares travam automaticamente.
5. Depois de rodar a Eliminatória: **Finalizar Prova** → **Classificar para
   Final** (escolhe quantos pássaros passam).
6. Aba **Resultado Geral** mostra uma linha por pássaro com o tempo da
   Eliminatória e da Final lado a lado — a colocação é sempre pelo tempo da
   Final (a Eliminatória é só classificatória). Botão **Gerar imagem pra
   compartilhar** cria um PNG pronto pra mandar no WhatsApp.

## Painel do administrador (`/admin`)

Existe uma aba separada, em `SEU-SITE.onrender.com/admin`, só pra você (dono
do site) — não é a mesma coisa que o código de sala. Ela foi feita de
propósito bem simples e leve (quase sem JavaScript, sem ficar atualizando
várias coisas o tempo todo) pra não pesar no resto do site.

- **Senha padrão: `123456`**. Troca ela assim que entrar pela primeira vez,
  na seção "Trocar senha" — fica salvo no banco, não precisa mexer em nada
  no Neon ou no Render.
- Mostra 4 números: acessos totais (desde sempre), salas criadas (desde
  sempre), pássaros cadastrados (desde sempre) e celulares conectados agora.
- Os três primeiros só atualizam quando você recarrega a página (F5) — de
  propósito, pra não gerar tráfego extra no site. Só o de "celulares
  conectados agora" atualiza sozinho, a cada 10 segundos, e só enquanto essa
  aba do admin estiver aberta — não afeta a tela do organizador nem a do
  celular.

## O que mudou nesta versão (última atualização)

- **Delay entre celular e site bem menor**: antes, cada "tick" do celular
  (10x por segundo) fazia até 3 idas ao banco antes de gravar o tempo
  (checar se a sala existe, "tocar" ela, e um SELECT+JOIN pra descobrir de
  quem é o vínculo). Agora o servidor guarda isso em memória (cache) assim
  que o celular vincula/a prova inicia, e o tick vira só 1 UPDATE quando
  está contando, ou nenhuma consulta ao banco quando está parado — só volta
  a consultar o banco se esse cache estiver vazio (primeiro tick depois de
  conectar, ou logo após o servidor reiniciar).
- **Botão "Trocar pássaro vinculado" mudou de lugar**: agora fica em cima,
  antes do relógio — longe de onde o polegar fica apertando "SEGURE PARA
  CANTAR" lá embaixo — e ficou visualmente menor/discreto, pra reduzir
  chance de apertar sem querer.
- **Botão "SEGURE PARA CANTAR" ficou bem maior**, ocupando boa parte do
  resto da tela — além de mais fácil de acertar, não sobra espaço vazio
  embaixo dele pra acabar encostando em outra coisa.

## Versão anterior

- **Contadores reais** (acessos, salas criadas, pássaros cadastrados,
  celulares conectados agora) — visíveis em `/admin`, de forma leve.
- **Delay reduzido entre celular e organizador**: o celular só grava um novo
  tempo no banco enquanto está realmente contando (antes gravava a cada
  100ms mesmo parado), e o pool de conexões com o banco dobrou de tamanho.
  Se o delay ainda incomodar com muitos celulares ao mesmo tempo, vale
  aumentar `--threads` no Start Command do Render (ex: de 8 pra 16).
- **Milissegundos no cronômetro grande** da Eliminatória/Final (organizador),
  além de minutos e segundos — atualiza suavemente sem precisar bater no
  servidor a cada 100ms (calcula localmente entre uma sincronização e outra).
- **Bug corrigido**: na tela de vínculo do celular, a lista de pássaros de
  uma categoria (Eliminatória ou Final) some assim que aquela prova é
  finalizada — antes as duas listas ficavam aparecendo juntas mesmo depois
  da Eliminatória já ter acabado.

## Estrutura

- `app.py` — servidor Flask (rotas da API + as 4 telas embutidas em HTML:
  home, organizador, celular e admin).
- `db.py` — camada de banco de dados (Neon/Postgres): tabelas, consultas,
  finalização automática por tempo, limpeza de salas com mais de 48h,
  config (senha do admin / imagem do botão) e estatísticas.
- `requirements.txt` — dependências (Flask, gunicorn, psycopg2).

## Nota sobre o que testei

Sem acesso a um banco Neon de verdade neste ambiente, não consegui rodar o
`db.py` contra um Postgres real ponta a ponta. O que foi validado:
- O app Flask sobe e todas as páginas carregam normalmente mesmo sem banco
  configurado (mostra um aviso claro no lugar de travar).
- A lógica das consultas mais complexas (classificar, ranking geral com
  JOIN) foi conferida contra um banco SQLite equivalente e bateu certinho
  com o resultado esperado.
- Revisei a sintaxe SQL específica do Postgres à mão (tipos, `RETURNING`,
  conversão de intervalo de tempo) e corrigi um erro de tipo que só
  apareceria rodando contra o Postgres de verdade.

Ainda assim, recomendo testar localmente primeiro (passo 2 acima) com sua
connection string do Neon antes de ir direto pro Render, só pra garantir que
tudo conversa direito com o banco de verdade.
