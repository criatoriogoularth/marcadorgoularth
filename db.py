import os
import re
import time
import queue
import threading
import datetime

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

# ════════════════════════════════════════════════════════════════════
# CAMADA DE BANCO DE DADOS (Neon / Postgres)
# ════════════════════════════════════════════════════════════════════

DATABASE_URL = os.environ.get('DATABASE_URL', '')

_pool = None
_pool_lock = threading.Lock()

ADMIN_DEFAULT_PASSWORD = "123456"


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
                # 20 conexões dá folga pro pico de "tick" simultâneo de vários
                # celulares + o polling da tela do organizador. Se a
                # connection string do Neon tiver "-pooler" no host (pooler
                # do próprio Neon/pgbouncer), isso é ainda mais barato.
                _pool = ThreadedConnectionPool(1, 20, DATABASE_URL, sslmode='require')
    return _pool


class conexao:
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


# ════════════════════════════════════════════════════════════════════
# LOG DE ACESSO — GRAVAÇÃO ASSÍNCRONA
#
# Antes, toda chamada de API (inclusive o "tick" do celular, chamado até
# 5x por segundo, e o polling do cronômetro do organizador) esperava uma
# viagem extra até o Neon só pra gravar uma linha de log. Isso sozinho já
# dobrava/triplicava o tempo de resposta de tudo. Agora o log só entra
# numa fila em memória (instantâneo) e uma thread em segundo plano grava
# em lote de tempos em tempos — a resposta ao celular/organizador não
# espera mais por isso.
# ════════════════════════════════════════════════════════════════════

_log_queue = queue.Queue()
_log_worker_iniciado = False
_log_worker_lock = threading.Lock()


def _iniciar_log_worker():
    global _log_worker_iniciado
    if _log_worker_iniciado:
        return
    with _log_worker_lock:
        if _log_worker_iniciado:
            return
        t = threading.Thread(target=_log_worker, daemon=True)
        t.start()
        _log_worker_iniciado = True


def _log_worker():
    while True:
        item = _log_queue.get()
        lote = [item]
        # drena o que mais já estiver esperando, pra gravar tudo de uma vez
        while len(lote) < 200:
            try:
                lote.append(_log_queue.get_nowait())
            except queue.Empty:
                break
        try:
            with conexao() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO logs_acesso (sala_codigo, tipo_acesso, esp32_id, ip, user_agent)
                       VALUES %s""",
                    lote
                )
                # conta acessos de organizador/celular por sala, em lote,
                # em vez de um UPDATE por evento
                contagem = {}
                for codigo, tipo_acesso, *_ in lote:
                    if codigo and tipo_acesso in ('organizador', 'celular'):
                        contagem[codigo] = contagem.get(codigo, 0) + 1
                for codigo, qtd in contagem.items():
                    cur.execute(
                        "UPDATE salas SET acessos_total = acessos_total + %s WHERE codigo = %s",
                        (qtd, codigo)
                    )
        except Exception as e:
            print(f"⚠ erro ao gravar logs de acesso em lote: {e}")
        time.sleep(0.5)  # evita martelar o banco em rajadas de tick


def inicializar_schema():
    with conexao() as cur:
        # Tabela de salas
        cur.execute("""
            CREATE TABLE IF NOT EXISTS salas (
                codigo TEXT PRIMARY KEY,
                criada_em TIMESTAMPTZ NOT NULL DEFAULT now(),
                ultimo_uso TIMESTAMPTZ NOT NULL DEFAULT now(),
                quantidade_classificados INTEGER NOT NULL DEFAULT 15,
                acessos_total INTEGER NOT NULL DEFAULT 0
            );
        """)
        
        # Tabela de provas
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
        
        # Tabela de itens (pássaros)
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
        
        # Tabela de logs de acesso
        cur.execute("""
            CREATE TABLE IF NOT EXISTS logs_acesso (
                id SERIAL PRIMARY KEY,
                sala_codigo TEXT REFERENCES salas(codigo) ON DELETE CASCADE,
                tipo_acesso TEXT NOT NULL CHECK (tipo_acesso IN ('organizador', 'celular', 'admin', 'api')),
                esp32_id TEXT,
                ip TEXT,
                user_agent TEXT,
                data_hora TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        
        # Tabela de configuração do admin
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_config (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            );
        """)
        
        # Índices
        cur.execute("CREATE INDEX IF NOT EXISTS idx_itens_sala_tipo ON itens (sala_codigo, tipo);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_itens_esp32 ON itens (esp32_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_sala ON logs_acesso (sala_codigo);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_data ON logs_acesso (data_hora);")
        
        # Inserir configurações padrão do admin
        cur.execute(
            "INSERT INTO admin_config (chave, valor) VALUES (%s, %s) ON CONFLICT (chave) DO NOTHING",
            ("senha_admin", ADMIN_DEFAULT_PASSWORD)
        )
        cur.execute(
            "INSERT INTO admin_config (chave, valor) VALUES (%s, %s) ON CONFLICT (chave) DO NOTHING",
            ("imagem_botao", "")
        )


# ════════════════════════════════════════════════════════════════════
# ADMIN - Configurações
# ════════════════════════════════════════════════════════════════════

def obter_config_admin(chave):
    with conexao() as cur:
        cur.execute("SELECT valor FROM admin_config WHERE chave = %s", (chave,))
        linha = cur.fetchone()
        return linha['valor'] if linha else None


def definir_config_admin(chave, valor):
    with conexao() as cur:
        cur.execute(
            "INSERT INTO admin_config (chave, valor) VALUES (%s, %s) ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor",
            (chave, valor)
        )


def verificar_senha_admin(senha):
    senha_atual = obter_config_admin("senha_admin")
    return senha_atual == senha


def alterar_senha_admin(senha_antiga, senha_nova):
    if not verificar_senha_admin(senha_antiga):
        return False
    definir_config_admin("senha_admin", senha_nova)
    return True


def obter_imagem_botao():
    return obter_config_admin("imagem_botao") or ""


# ════════════════════════════════════════════════════════════════════
# SALA
# ════════════════════════════════════════════════════════════════════

DURACAO_PADRAO = {"eliminatorias": 600, "final": 900}


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
        # as 2 provas (eliminatória + final) num INSERT só, em vez de 2
        cur.execute(
            """INSERT INTO provas (sala_codigo, tipo, duracao) VALUES
               (%s, 'eliminatorias', %s), (%s, 'final', %s)""",
            (codigo, DURACAO_PADRAO['eliminatorias'], codigo, DURACAO_PADRAO['final'])
        )


_ultimo_toque = {}
_ultimo_toque_lock = threading.Lock()
_TOQUE_INTERVALO = 30  # segundos — não precisa atualizar "ultimo_uso" a cada tick


def sala_existe(codigo):
    """Verifica se a sala existe. Antes fazia sempre 2 viagens ao banco
    (SELECT + UPDATE de ultimo_uso) em toda chamada — inclusive nos ticks do
    celular, 5x por segundo. Agora só toca ultimo_uso a cada 30s por sala
    (guardado em memória) e, quando não precisa tocar, faz só 1 SELECT leve."""
    agora = time.monotonic()
    with _ultimo_toque_lock:
        precisa_tocar = (agora - _ultimo_toque.get(codigo, 0)) > _TOQUE_INTERVALO

    if precisa_tocar:
        with conexao() as cur:
            cur.execute("UPDATE salas SET ultimo_uso = now() WHERE codigo = %s RETURNING 1", (codigo,))
            existe = cur.fetchone() is not None
        if existe:
            with _ultimo_toque_lock:
                _ultimo_toque[codigo] = agora
        return existe

    with conexao() as cur:
        cur.execute("SELECT 1 FROM salas WHERE codigo = %s", (codigo,))
        return cur.fetchone() is not None


def tocar_sala(codigo):
    with conexao() as cur:
        cur.execute("UPDATE salas SET ultimo_uso = now() WHERE codigo = %s", (codigo,))
    with _ultimo_toque_lock:
        _ultimo_toque[codigo] = time.monotonic()


def registrar_acesso(codigo, tipo_acesso, esp32_id=None, ip=None, user_agent=None):
    """Enfileira o log de acesso para gravação em segundo plano (não bloqueia
    a resposta da requisição — ver _log_worker acima)."""
    _iniciar_log_worker()
    try:
        _log_queue.put_nowait((codigo, tipo_acesso, esp32_id, ip, user_agent))
    except Exception as e:
        print(f"⚠ erro ao enfileirar log de acesso: {e}")


def obter_quantidade_classificados(codigo):
    with conexao() as cur:
        cur.execute("SELECT quantidade_classificados FROM salas WHERE codigo = %s", (codigo,))
        linha = cur.fetchone()
    return linha['quantidade_classificados'] if linha else 15


def definir_quantidade_classificados(codigo, qtd):
    with conexao() as cur:
        cur.execute("UPDATE salas SET quantidade_classificados = %s WHERE codigo = %s", (qtd, codigo))


# ════════════════════════════════════════════════════════════════════
# ESTATÍSTICAS PARA O ADMIN
# ════════════════════════════════════════════════════════════════════

def obter_estatisticas():
    with conexao() as cur:
        cur.execute("SELECT COUNT(*) AS total FROM salas")
        total_salas = cur.fetchone()['total']
        
        cur.execute(
            "SELECT COUNT(*) AS ativas FROM salas WHERE ultimo_uso > now() - interval '24 hours'"
        )
        salas_ativas = cur.fetchone()['ativas']
        
        cur.execute("SELECT COALESCE(SUM(acessos_total), 0) AS total FROM salas")
        total_acessos = cur.fetchone()['total']
        
        cur.execute(
            "SELECT COUNT(*) AS total FROM logs_acesso WHERE data_hora > now() - interval '24 hours'"
        )
        acessos_24h = cur.fetchone()['total']
        
        cur.execute(
            "SELECT COUNT(DISTINCT esp32_id) AS total FROM logs_acesso WHERE esp32_id IS NOT NULL AND data_hora > now() - interval '24 hours'"
        )
        dispositivos_24h = cur.fetchone()['total']
        
        cur.execute("SELECT COUNT(DISTINCT esp32_id) AS total FROM itens WHERE esp32_id IS NOT NULL")
        vinculados_agora = cur.fetchone()['total']
        
        cur.execute("SELECT COUNT(*) AS total FROM provas WHERE ativa = true")
        provas_ativas = cur.fetchone()['total']
        
        cur.execute("""
            SELECT tipo_acesso, COUNT(*) AS total 
            FROM logs_acesso 
            WHERE data_hora > now() - interval '24 hours'
            GROUP BY tipo_acesso
        """)
        logs_por_tipo = {row['tipo_acesso']: row['total'] for row in cur.fetchall()}
        
        cur.execute("SELECT COUNT(*) AS total FROM itens")
        total_passaros = cur.fetchone()['total']
        
        cur.execute("""
            SELECT codigo, acessos_total, ultimo_uso
            FROM salas 
            ORDER BY acessos_total DESC 
            LIMIT 10
        """)
        salas_top = [dict(row) for row in cur.fetchall()]
        
        return {
            "total_salas": total_salas,
            "salas_ativas_24h": salas_ativas,
            "total_acessos": total_acessos,
            "acessos_24h": acessos_24h,
            "dispositivos_unicos_24h": dispositivos_24h,
            "vinculados_agora": vinculados_agora,
            "provas_ativas": provas_ativas,
            "total_passaros": total_passaros,
            "acessos_organizador": logs_por_tipo.get('organizador', 0),
            "acessos_celular": logs_por_tipo.get('celular', 0),
            "acessos_api": logs_por_tipo.get('api', 0),
            "salas_top": salas_top,
        }


def obter_logs_recentes(limite=50):
    with conexao() as cur:
        cur.execute("""
            SELECT id, sala_codigo, tipo_acesso, esp32_id, ip, data_hora
            FROM logs_acesso
            ORDER BY data_hora DESC
            LIMIT %s
        """, (limite,))
        return [dict(row) for row in cur.fetchall()]


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
    return vinculados


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
    return vinculados


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
    """Chamado até 5x/segundo por celular. Antes fazia sempre um SELECT com
    JOIN e depois, condicionalmente, um UPDATE — 2 viagens ao banco em cada
    chamada. Agora é 1 UPDATE só (que já filtra prova ativa e retorna o
    vínculo), e nem isso quando não há tempo válido pra salvar (ex.: antes
    de apertar o botão pela 1ª vez)."""
    if not tempo_valido:
        return None
    with conexao() as cur:
        cur.execute(
            """UPDATE itens i SET tempo_texto = %s, tempo_segundos = %s
               FROM provas p
               WHERE p.sala_codigo = i.sala_codigo AND p.tipo = i.tipo
                 AND i.sala_codigo = %s AND i.esp32_id = %s AND p.ativa = true
               RETURNING i.id, i.tipo""",
            (tempo_str, tempo_segundos, codigo, esp32_id)
        )
        linha = cur.fetchone()
    return (linha['tipo'], linha['id']) if linha else None


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