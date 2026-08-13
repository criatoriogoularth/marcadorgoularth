import os
import re
import threading
import datetime

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

# ════════════════════════════════════════════════════════════════════
# CAMADA DE BANCO DE DADOS (Neon / Postgres)
# ════════════════════════════════════════════════════════════════════
# Substitui os dicionários em memória por tabelas de verdade, pra sala
# sobreviver ao servidor "dormir" e acordar (comum no plano grátis do
# Render). Continua sem login: o código da sala é a única "chave".
#
# Variável de ambiente esperada: DATABASE_URL (a connection string que
# o Neon te dá, algo como postgresql://usuario:senha@host/banco).
# ════════════════════════════════════════════════════════════════════

DATABASE_URL = os.environ.get('DATABASE_URL', '')

_pool = None
_pool_lock = threading.Lock()


def _obter_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                if not DATABASE_URL:
                    raise RuntimeError(
                        "DATABASE_URL não configurada. Defina essa variável de "
                        "ambiente com a connection string do Neon antes de rodar."
                    )
                # pool maior (20) ajuda a evitar fila de espera por conexão
                # quando várias pessoas estão marcando ao mesmo tempo — isso
                # era uma das causas do delay entre o celular e a tela do
                # organizador.
                _pool = ThreadedConnectionPool(1, 20, DATABASE_URL, sslmode='require')
    return _pool


class conexao:
    """Context manager: pega uma conexão do pool, comita se deu tudo
    certo, devolve pro pool sempre. Uso: `with conexao() as cur: ...`"""

    def __enter__(self):
        self._conn = _obter_pool().getconn()
        self._cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return self._cur

    def __exit__(self, tipo_exc, valor_exc, tb):
        try:
            if tipo_exc is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._cur.close()
            _obter_pool().putconn(self._conn)
        return False


def inicializar_schema():
    with conexao() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS salas (
                codigo TEXT PRIMARY KEY,
                criada_em TIMESTAMPTZ NOT NULL DEFAULT now(),
                ultimo_uso TIMESTAMPTZ NOT NULL DEFAULT now(),
                quantidade_classificados INTEGER NOT NULL DEFAULT 15
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS provas (
                sala_codigo TEXT NOT NULL REFERENCES salas(codigo) ON DELETE CASCADE,
                tipo TEXT NOT NULL CHECK (tipo IN ('eliminatorias','final')),
                duracao INTEGER NOT NULL,
                ativa BOOLEAN NOT NULL DEFAULT false,
                finalizada BOOLEAN NOT NULL DEFAULT false,
                iniciada_em TIMESTAMPTZ,
                PRIMARY KEY (sala_codigo, tipo)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS itens (
                id SERIAL PRIMARY KEY,
                sala_codigo TEXT NOT NULL REFERENCES salas(codigo) ON DELETE CASCADE,
                tipo TEXT NOT NULL CHECK (tipo IN ('eliminatorias','final')),
                nome TEXT NOT NULL,
                anilha TEXT NOT NULL DEFAULT '',
                proprietario TEXT NOT NULL DEFAULT '',
                esp32_id TEXT,
                tempo_texto TEXT NOT NULL DEFAULT '00:00:000',
                tempo_segundos DOUBLE PRECISION NOT NULL DEFAULT 0,
                origem_id INTEGER
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_itens_sala_tipo ON itens (sala_codigo, tipo);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_itens_esp32 ON itens (esp32_id);")

        # ── config (senha do admin, imagem do botão) e estatísticas (contadores
        # cumulativos que sobrevivem mesmo depois que as salas são apagadas) ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS config (
                chave TEXT PRIMARY KEY,
                valor TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS estatisticas (
                chave TEXT PRIMARY KEY,
                valor BIGINT NOT NULL DEFAULT 0
            );
        """)
        cur.execute("INSERT INTO estatisticas (chave, valor) VALUES ('total_acessos', 0) ON CONFLICT (chave) DO NOTHING;")
        cur.execute("INSERT INTO estatisticas (chave, valor) VALUES ('total_salas_criadas', 0) ON CONFLICT (chave) DO NOTHING;")
        cur.execute("INSERT INTO config (chave, valor) VALUES ('imagem_botao', NULL) ON CONFLICT (chave) DO NOTHING;")
        cur.execute("SELECT valor FROM config WHERE chave = 'admin_senha_hash'")
        if cur.fetchone() is None:
            from werkzeug.security import generate_password_hash
            cur.execute(
                "INSERT INTO config (chave, valor) VALUES ('admin_senha_hash', %s)",
                (generate_password_hash('123456'),)
            )


# ════════════════════════════════════════════════════════════════════
# SALA
# ════════════════════════════════════════════════════════════════════

DURACAO_PADRAO = {"eliminatorias": 600, "final": 900}   # 10 min / 15 min, fixos


def validar_tempo(txt):
    return re.match(r"^\d{2}:\d{2}:\d{3}$", txt or "") is not None


def tempo_para_segundos(txt):
    try:
        mm, ss, mmm = txt.split(":")
        return int(mm) * 60 + int(ss) + int(mmm) / 1000.0
    except Exception:
        return 0.0


def formatar_tempo(seg):
    if seg < 0:
        seg = 0
    mm = int(seg // 60)
    ss = int(seg % 60)
    mmm = int(round((seg - int(seg)) * 1000))
    return f"{mm:02d}:{ss:02d}:{mmm:03d}"


def criar_sala(codigo):
    with conexao() as cur:
        cur.execute("INSERT INTO salas (codigo) VALUES (%s)", (codigo,))
        for tipo, duracao in DURACAO_PADRAO.items():
            cur.execute(
                "INSERT INTO provas (sala_codigo, tipo, duracao) VALUES (%s, %s, %s)",
                (codigo, tipo, duracao)
            )


def sala_existe(codigo):
    with conexao() as cur:
        cur.execute("SELECT 1 FROM salas WHERE codigo = %s", (codigo,))
        existe = cur.fetchone() is not None
    if existe:
        tocar_sala(codigo)
    return existe


def tocar_sala(codigo):
    """Atualiza 'ultimo_uso' — usado pra decidir quais salas apagar depois de 48h."""
    with conexao() as cur:
        cur.execute("UPDATE salas SET ultimo_uso = now() WHERE codigo = %s", (codigo,))


def obter_quantidade_classificados(codigo):
    with conexao() as cur:
        cur.execute("SELECT quantidade_classificados FROM salas WHERE codigo = %s", (codigo,))
        linha = cur.fetchone()
    return linha['quantidade_classificados'] if linha else 15


def definir_quantidade_classificados(codigo, qtd):
    with conexao() as cur:
        cur.execute("UPDATE salas SET quantidade_classificados = %s WHERE codigo = %s", (qtd, codigo))


# ════════════════════════════════════════════════════════════════════
# CADASTRO -> PROVA
# ════════════════════════════════════════════════════════════════════

def cadastrar_prova(codigo, tipo, passaros):
    with conexao() as cur:
        for p in passaros:
            nome = str(p.get('nome', '')).strip()[:40]
            if not nome:
                continue
            cur.execute(
                """INSERT INTO itens (sala_codigo, tipo, nome, anilha, proprietario)
                   VALUES (%s, %s, %s, %s, %s)""",
                (codigo, tipo, nome,
                 str(p.get('anilha', '')).strip()[:20],
                 str(p.get('proprietario', '')).strip()[:40])
            )


# ════════════════════════════════════════════════════════════════════
# PROVA
# ════════════════════════════════════════════════════════════════════

def ver_prova(codigo, tipo):
    with conexao() as cur:
        cur.execute(
            "SELECT duracao, ativa, finalizada, iniciada_em FROM provas WHERE sala_codigo = %s AND tipo = %s",
            (codigo, tipo)
        )
        prova = cur.fetchone()
        if prova is None:
            return None
        cur.execute(
            """SELECT id, nome, anilha, proprietario, esp32_id, tempo_texto, tempo_segundos
               FROM itens WHERE sala_codigo = %s AND tipo = %s
               ORDER BY tempo_segundos DESC, id ASC""",
            (codigo, tipo)
        )
        itens = cur.fetchall()
    return {"prova": prova, "itens": itens}


def iniciar_prova(codigo, tipo):
    """Retorna None se não tem pássaro nenhum na prova (não inicia).
    Senão, retorna {"duracao": int, "vinculados": [esp32_id, ...]}."""
    with conexao() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM itens WHERE sala_codigo = %s AND tipo = %s", (codigo, tipo))
        if cur.fetchone()['n'] == 0:
            return None
        cur.execute(
            """UPDATE provas SET ativa = true, finalizada = false, iniciada_em = now()
               WHERE sala_codigo = %s AND tipo = %s
               RETURNING duracao""",
            (codigo, tipo)
        )
        duracao = cur.fetchone()['duracao']
        cur.execute(
            "SELECT esp32_id FROM itens WHERE sala_codigo = %s AND tipo = %s AND esp32_id IS NOT NULL",
            (codigo, tipo)
        )
        vinculados = [l['esp32_id'] for l in cur.fetchall()]
    return {"duracao": duracao, "vinculados": vinculados}


def finalizar_prova(codigo, tipo):
    with conexao() as cur:
        cur.execute(
            "SELECT esp32_id FROM itens WHERE sala_codigo = %s AND tipo = %s AND esp32_id IS NOT NULL",
            (codigo, tipo)
        )
        vinculados = [l['esp32_id'] for l in cur.fetchall()]
        cur.execute(
            "UPDATE provas SET ativa = false, finalizada = true WHERE sala_codigo = %s AND tipo = %s",
            (codigo, tipo)
        )
    return vinculados   # quem tava vinculado recebe o comando FINALIZAR


def limpar_prova(codigo, tipo):
    with conexao() as cur:
        cur.execute(
            "SELECT esp32_id FROM itens WHERE sala_codigo = %s AND tipo = %s AND esp32_id IS NOT NULL",
            (codigo, tipo)
        )
        vinculados = [l['esp32_id'] for l in cur.fetchall()]
        cur.execute("DELETE FROM itens WHERE sala_codigo = %s AND tipo = %s", (codigo, tipo))
        duracao = DURACAO_PADRAO[tipo]
        cur.execute(
            """UPDATE provas SET ativa = false, finalizada = false, iniciada_em = NULL, duracao = %s
               WHERE sala_codigo = %s AND tipo = %s""",
            (duracao, codigo, tipo)
        )
    return vinculados   # quem tava vinculado recebe o comando RESET


def tempo_restante_segundos(prova_row):
    if not prova_row['ativa'] or not prova_row['iniciada_em']:
        return prova_row['duracao']
    agora = datetime.datetime.now(datetime.timezone.utc)
    decorrido = (agora - prova_row['iniciada_em']).total_seconds()
    return max(0, prova_row['duracao'] - decorrido)


# ════════════════════════════════════════════════════════════════════
# CLASSIFICAR
# ════════════════════════════════════════════════════════════════════

def classificar(codigo, quantidade):
    with conexao() as cur:
        cur.execute("UPDATE salas SET quantidade_classificados = %s WHERE codigo = %s", (quantidade, codigo))
        cur.execute(
            """SELECT id, nome, anilha, proprietario FROM itens
               WHERE sala_codigo = %s AND tipo = 'eliminatorias'
               ORDER BY tempo_segundos DESC LIMIT %s""",
            (codigo, max(0, quantidade))
        )
        classificados = cur.fetchall()
        for item in classificados:
            cur.execute(
                """INSERT INTO itens (sala_codigo, tipo, nome, anilha, proprietario, origem_id)
                   VALUES (%s, 'final', %s, %s, %s, %s)""",
                (codigo, item['nome'], item['anilha'], item['proprietario'], item['id'])
            )
    return len(classificados)


# ════════════════════════════════════════════════════════════════════
# VINCULAR / DESVINCULAR / TICK
# ════════════════════════════════════════════════════════════════════

def passaros_livres(codigo, tipo):
    with conexao() as cur:
        cur.execute(
            "SELECT id, nome FROM itens WHERE sala_codigo = %s AND tipo = %s AND esp32_id IS NULL ORDER BY id",
            (codigo, tipo)
        )
        return cur.fetchall()


def vincular(codigo, tipo, item_id, esp32_id):
    with conexao() as cur:
        cur.execute(
            "SELECT id, nome, esp32_id FROM itens WHERE sala_codigo = %s AND tipo = %s AND id = %s",
            (codigo, tipo, item_id)
        )
        item = cur.fetchone()
        if item is None:
            return {"ok": False, "erro": "pássaro não encontrado"}
        if item['esp32_id']:
            return {"ok": False, "erro": "esse pássaro já está vinculado a outro celular"}

        # se esse celular já estava noutro pássaro, desvincula de lá primeiro
        cur.execute("UPDATE itens SET esp32_id = NULL WHERE sala_codigo = %s AND esp32_id = %s", (codigo, esp32_id))

        cur.execute("UPDATE itens SET esp32_id = %s WHERE id = %s", (esp32_id, item_id))
        cur.execute(
            "SELECT ativa, duracao, iniciada_em FROM provas WHERE sala_codigo = %s AND tipo = %s",
            (codigo, tipo)
        )
        prova = cur.fetchone()

    comandos = [f"NOME:{item['nome'][:16]}"]
    if prova and prova['ativa']:
        comandos.append(f"PROVA:{formatar_tempo(tempo_restante_segundos(prova))}")
    return {"ok": True, "comandos": comandos}


def desvincular(codigo, tipo, item_id):
    with conexao() as cur:
        cur.execute(
            "UPDATE itens SET esp32_id = NULL WHERE sala_codigo = %s AND tipo = %s AND id = %s",
            (codigo, tipo, item_id)
        )


def tick(codigo, esp32_id, tempo_str, tempo_valido, tempo_segundos):
    """Atualiza o tempo do pássaro vinculado a esse esp32_id (se a prova
    dele estiver ativa). Retorna (tipo, item_id) do vínculo atual, ou None."""
    with conexao() as cur:
        cur.execute(
            """SELECT i.id, i.tipo, p.ativa FROM itens i
               JOIN provas p ON p.sala_codigo = i.sala_codigo AND p.tipo = i.tipo
               WHERE i.sala_codigo = %s AND i.esp32_id = %s""",
            (codigo, esp32_id)
        )
        vinculo = cur.fetchone()
        if vinculo and tempo_valido and vinculo['ativa']:
            cur.execute(
                "UPDATE itens SET tempo_texto = %s, tempo_segundos = %s WHERE id = %s",
                (tempo_str, tempo_segundos, vinculo['id'])
            )
    if vinculo:
        return (vinculo['tipo'], vinculo['id'])
    return None


# ════════════════════════════════════════════════════════════════════
# RESULTADO GERAL
# ════════════════════════════════════════════════════════════════════

def ranking_geral(codigo):
    with conexao() as cur:
        cur.execute(
            """SELECT f.nome, f.anilha, f.proprietario,
                      f.tempo_texto AS tempo_final_texto, f.tempo_segundos AS tempo_final_segundos,
                      COALESCE(e.tempo_texto, '-') AS tempo_eliminatoria_texto
               FROM itens f
               LEFT JOIN itens e ON e.id = f.origem_id
               WHERE f.sala_codigo = %s AND f.tipo = 'final'
               ORDER BY f.tempo_segundos DESC, f.id ASC""",
            (codigo,)
        )
        linhas = cur.fetchall()
    resultado = []
    for i, r in enumerate(linhas, 1):
        d = dict(r)
        d['posicao'] = i
        resultado.append(d)
    return resultado


# ════════════════════════════════════════════════════════════════════
# FINALIZAÇÃO AUTOMÁTICA + LIMPEZA (48 horas)
# ════════════════════════════════════════════════════════════════════

def provas_para_finalizar_automaticamente():
    """Provas ativas cujo tempo já bateu zero. Finaliza e devolve, por
    sala/tipo, a lista de esp32_id vinculados (pra mandar FINALIZAR)."""
    with conexao() as cur:
        cur.execute(
            """SELECT sala_codigo, tipo, duracao, iniciada_em FROM provas
               WHERE ativa = true AND iniciada_em IS NOT NULL
                 AND iniciada_em + (duracao::text || ' seconds')::interval <= now()"""
        )
        vencidas = cur.fetchall()
        resultado = []
        for v in vencidas:
            cur.execute(
                "SELECT esp32_id FROM itens WHERE sala_codigo = %s AND tipo = %s AND esp32_id IS NOT NULL",
                (v['sala_codigo'], v['tipo'])
            )
            vinculados = [l['esp32_id'] for l in cur.fetchall()]
            cur.execute(
                "UPDATE provas SET ativa = false, finalizada = true WHERE sala_codigo = %s AND tipo = %s",
                (v['sala_codigo'], v['tipo'])
            )
            resultado.append({"sala_codigo": v['sala_codigo'], "tipo": v['tipo'], "vinculados": vinculados})
    return resultado


def apagar_salas_expiradas(horas=48):
    with conexao() as cur:
        cur.execute(
            "DELETE FROM salas WHERE ultimo_uso < now() - (%s::text || ' hours')::interval RETURNING codigo",
            (horas,)
        )
        apagadas = [l['codigo'] for l in cur.fetchall()]
    return apagadas


def status_provas(codigo):
    """{'eliminatorias': {'ativa':..,'finalizada':..}, 'final': {...}} — usado
    pelo celular pra saber quais categorias ainda podem receber vínculo."""
    with conexao() as cur:
        cur.execute(
            "SELECT tipo, ativa, finalizada FROM provas WHERE sala_codigo = %s",
            (codigo,)
        )
        linhas = cur.fetchall()
    return {l['tipo']: {"ativa": l['ativa'], "finalizada": l['finalizada']} for l in linhas}


# ════════════════════════════════════════════════════════════════════
# CONFIG (senha do admin, imagem personalizada do botão) + ESTATÍSTICAS
# ════════════════════════════════════════════════════════════════════

def obter_config(chave, padrao=None):
    with conexao() as cur:
        cur.execute("SELECT valor FROM config WHERE chave = %s", (chave,))
        linha = cur.fetchone()
    return linha['valor'] if linha else padrao


def definir_config(chave, valor):
    with conexao() as cur:
        cur.execute(
            """INSERT INTO config (chave, valor) VALUES (%s, %s)
               ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor""",
            (chave, valor)
        )


def verificar_senha_admin(senha):
    from werkzeug.security import check_password_hash
    h = obter_config('admin_senha_hash')
    if not h:
        return False
    try:
        return check_password_hash(h, senha or '')
    except Exception:
        return False


def trocar_senha_admin(nova_senha):
    from werkzeug.security import generate_password_hash
    definir_config('admin_senha_hash', generate_password_hash(nova_senha))


def incrementar_acesso():
    with conexao() as cur:
        cur.execute(
            "UPDATE estatisticas SET valor = valor + 1 WHERE chave = 'total_acessos' RETURNING valor"
        )
        linha = cur.fetchone()
    return linha['valor'] if linha else 0


def incrementar_salas_criadas():
    with conexao() as cur:
        cur.execute("UPDATE estatisticas SET valor = valor + 1 WHERE chave = 'total_salas_criadas'")


def obter_estatisticas_gerais():
    with conexao() as cur:
        cur.execute("SELECT chave, valor FROM estatisticas")
        stats = {l['chave']: l['valor'] for l in cur.fetchall()}
        cur.execute("SELECT COUNT(*) AS n FROM salas")
        salas_no_banco = cur.fetchone()['n']
        cur.execute("SELECT COUNT(*) AS n FROM salas WHERE ultimo_uso > now() - interval '15 minutes'")
        salas_ativas_agora = cur.fetchone()['n']
        cur.execute("SELECT COUNT(*) AS n FROM itens")
        total_passaros = cur.fetchone()['n']
    return {
        "total_acessos": stats.get('total_acessos', 0),
        "total_salas_criadas": stats.get('total_salas_criadas', 0),
        "salas_no_banco_agora": salas_no_banco,
        "salas_ativas_ultimos_15min": salas_ativas_agora,
        "total_passaros_cadastrados_agora": total_passaros,
    }
