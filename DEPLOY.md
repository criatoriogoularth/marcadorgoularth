# Como colocar no ar

O mesmo código roda no **Render** (grátis, mais simples) ou numa **VPS** (sempre ligado, vários sites).
Os dois usam as mesmas variáveis de ambiente (veja `.env.example`).

---

## A) Render

**Pelo Blueprint (mais rápido):** *New + → Blueprint* → escolha o repositório. O arquivo `render.yaml`
já traz tudo. Quando ele pedir, cole o `DATABASE_URL` (Neon) e defina `ADMIN_SENHA_INICIAL`.

**Manual:** *New + → Web Service* e use:
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --worker-class gthread --workers 1 --threads 16 --bind 0.0.0.0:$PORT --timeout 60`
- Variáveis: `DATABASE_URL`, `ADMIN_SENHA_INICIAL`

> No plano grátis o servidor dorme após uns minutos parado. Abra o site uns minutos antes da prova.

---

## B) VPS (vários sites no mesmo servidor)

Arquitetura: um **Caddy** na frente de tudo (HTTPS automático) + cada site/app no seu cantinho.

```
Internet ─► Caddy (80/443) ─┬─► marcador (container, porta 8000)
                            ├─► /srv/sites/site1  (arquivos estáticos)
                            ├─► /srv/sites/site2
                            └─► ...
```

### 1. DNS
Para cada domínio/subdomínio, crie um registro **A** apontando para o IP da VPS
(ex.: `marcador.seudominio.com → 203.0.113.10`). Espere propagar (minutos).

### 2. Preparar o servidor (Ubuntu/Debian)
```bash
ssh root@IP_DA_VPS
apt update && apt -y upgrade
curl -fsSL https://get.docker.com | sh          # instala Docker + Compose
apt -y install ufw fail2ban git
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw allow 443/udp && ufw --force enable
docker network create web
mkdir -p /opt/caddy /opt/marcador /srv/sites
```
Dica: depois crie um usuário comum com `sudo` e desative o login de root por senha (use chave SSH).

### 3. Caddy (porta de entrada)
Copie `deploy/vps/caddy/docker-compose.yml` e `deploy/vps/caddy/Caddyfile` para `/opt/caddy`,
edite os domínios no `Caddyfile` e suba:
```bash
cd /opt/caddy && docker compose up -d
```

### 4. Marcador
```bash
cd /opt/marcador
git clone https://github.com/SEU_USUARIO/marcadorgoularth.git app
cp app/deploy/vps/docker-compose.yml .
cp app/.env.example .env && nano .env           # preencha DATABASE_URL e ADMIN_SENHA_INICIAL
docker compose up -d --build
docker compose logs -f marcador                 # Ctrl+C pra sair
```
Abra `https://marcador.seudominio.com`.

### 5. Atualizar depois
```bash
cd /opt/marcador/app && git pull && cd .. && docker compose up -d --build
```

### 6. Adicionar mais sites
- **Estático** (HTML/CSS/JS ou pasta `dist`): copie os arquivos para `/srv/sites/siteN`
  (ex.: `scp -r dist/* root@IP:/srv/sites/site3/`), adicione um bloco no `Caddyfile` e recarregue:
  `docker compose -f /opt/caddy/docker-compose.yml exec caddy caddy reload --config /etc/caddy/Caddyfile`
- **App** (Node, Python...): rode num container na rede `web` e use `reverse_proxy nome-do-container:porta`.

### Banco de dados
- **Neon (padrão):** nada a fazer, só manter `DB_SSLMODE=require`. Backup é por conta do Neon.
- **Postgres na própria VPS (opcional):** `docker compose --profile db-local up -d`, e no `.env`:
  `DATABASE_URL=postgresql://marcador:SUASENHA@db:5432/marcador`, `DB_SSLMODE=disable`, `POSTGRES_PASSWORD=SUASENHA`.
  Aí o **backup passa a ser seu**: `docker exec marcador-db pg_dump -U marcador marcador | gzip > backup-$(date +%F).sql.gz`
  (coloque num `cron` e copie para fora da VPS).

---

## Segurança — leia

- **Troque a senha do `/admin`.** Se não definir `ADMIN_SENHA_INICIAL`, a senha inicial é `123456`.
  Depois do primeiro acesso, troque pelo próprio painel.
- O `/admin` recebe a senha pela URL (`?senha=`), que pode ficar em logs. Evite abrir o painel em
  computador público.
- Nunca suba o arquivo `.env` para o GitHub (já está no `.gitignore`).

## Importante

- **Mantenha `--workers 1`.** O tempo ao vivo fica na memória do processo; com mais de um worker
  cada um teria uma memória diferente. Para mais capacidade, aumente apenas `THREADS`.
- Reiniciar o container zera só o "ao vivo" dos últimos ~1s (o resto já foi gravado no banco).
