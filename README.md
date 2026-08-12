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
   - **Start Command**: `gunicorn app:app --worker-class gthread --threads 16 --workers 1 --bind 0.0.0.0:$PORT`
4. Em **Environment**, adiciona a variável `DATABASE_URL` com a connection
   string do Neon (a mesma do passo 1). Se o painel do Neon oferecer duas
   versões da connection string — uma normal e uma com `-pooler` no meio do
   host (ex: `ep-xxxxx-pooler.aws.neon.tech`) — **use a com `-pooler`**: ela
   é feita pra aguentar muitas conexões curtas e rápidas (exatamente o
   padrão do "tick" dos celulares) e evita gargalo por limite de conexões.
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

## Estrutura

- `app.py` — servidor Flask (rotas da API + as 3 telas embutidas em HTML).
- `db.py` — camada de banco de dados (Neon/Postgres): tabelas, consultas,
  finalização automática por tempo, limpeza de salas com mais de 48h.
- `requirements.txt` — dependências (Flask, gunicorn, psycopg2).

## Correções de performance (delay do celular / cronômetro travando)

Se você já tinha rodado uma versão anterior e sentiu tudo lento — celular
com delay, cronômetro do organizador travando, até criar sala demorando —
o problema não era o celular nem o Wi-Fi: era o app fazendo viagens ao
banco (Neon) demais em cada ação. Resumindo o que foi corrigido:

- **Todo request gravava um log de acesso na hora**, esperando o banco
  responder antes de devolver a resposta — inclusive o "tick" do celular,
  chamado até 5x por segundo. Agora o log entra numa fila em memória e uma
  thread em segundo plano grava em lote; o celular/organizador não espera
  mais por isso.
- **Verificar se a sala existe fazia 2 viagens ao banco** (um SELECT e,
  depois, um UPDATE separado pra "tocar" a sala) em toda chamada. Agora é
  1 viagem só, e o "toque" (que só serve pra sala não expirar em 48h) é
  feito no máximo 1x a cada 30s por sala, não a cada tick.
- **O "tick" do celular fazia um SELECT com JOIN e depois um UPDATE
  separado** — 2 viagens a cada 200ms. Virou 1 UPDATE só (e nem isso
  quando não há tempo pra salvar ainda).
- **O relógio do organizador dependia de um fetch novo a cada segundo**
  pra desenhar o número — qualquer soluço de rede aparecia direto como
  travadinha. Agora o servidor é consultado a cada 2s só pra corrigir o
  valor; o número em si é desenhado localmente a cada 100ms a partir da
  última leitura, então fica liso mesmo se a rede engasgar.
- Pool de conexões do banco aumentado (10 → 20) pra aguentar melhor vários
  celulares "ticando" ao mesmo tempo.

No total, uma chamada de "tick" que antes fazia até 5 viagens sequenciais
ao banco agora faz no máximo 1–2, e criar uma sala foi de ~6 viagens pra 3.
Isso deve resolver a maior parte do delay. Se ainda sentir lentidão depois
de subir essa versão, o suspeito nº 1 passa a ser o **plano gratuito do
Render**, que "dorme" o servidor inteiro (não só o banco) depois de um
tempo sem uso — a primeira requisição depois disso pode levar dezenas de
segundos pra acordar, não tem como evitar isso sem migrar pra um plano
pago do Render.

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
