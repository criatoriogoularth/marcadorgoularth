import os
import re
import time
import random
import string
import threading

from flask import Flask, request, jsonify

app = Flask(__name__)

# ════════════════════════════════════════════════════════════════════
# MOTOR DA SALA (tudo em memória — sem banco de dados)
# ════════════════════════════════════════════════════════════════════
# Cada "sala" é um dicionário isolado, identificado por um código curto.
# O organizador manda o cadastro (que mora no localStorage do navegador
# dele) pra virar a "prova" viva aqui; os celulares linkam nos pássaros
# dessa prova e mandam o tempo em tempo real; o organizador fica lendo
# (polling) pra atualizar a tela dele. Se o servidor reiniciar, a sala
# se perde — mas o cadastro e os resultados salvos continuam no
# navegador do organizador.

salas = {}
salas_lock = threading.Lock()

DURACAO_PADRAO = {"eliminatorias": 600, "final": 900}   # 10 min / 15 min, fixos


def novo_codigo_sala():
    alfabeto = string.ascii_uppercase + string.digits
    while True:
        codigo = ''.join(random.choice(alfabeto) for _ in range(6))
        with salas_lock:
            if codigo not in salas:
                return codigo


def nova_prova(tipo):
    return {
        "duracao": DURACAO_PADRAO[tipo],
        "ativa": False,
        "finalizada": False,
        "iniciada_em": None,
        "itens": {},   # item_id -> {"nome", "esp32_id", "tempo_texto", "tempo_segundos"}
    }


def nova_sala():
    return {
        "criada_em": time.time(),
        "ultimo_uso": time.time(),
        "lock": threading.Lock(),
        "quantidade_classificados": 15,
        "proximo_id": 1,
        "provas": {
            "eliminatorias": nova_prova("eliminatorias"),
            "final": nova_prova("final"),
        },
        # esp32_id -> {"fila_saida": [...], "ultimo_tick": ts, "vinculo": (tipo, item_id)|None}
        "conexoes": {},
    }


def obter_sala(codigo):
    with salas_lock:
        sala = salas.get((codigo or "").upper())
        if sala:
            sala["ultimo_uso"] = time.time()
        return sala


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


def tempo_restante(prova):
    if not prova["iniciada_em"]:
        return prova["duracao"]
    decorrido = time.time() - prova["iniciada_em"]
    return max(0, prova["duracao"] - decorrido)


def empurrar_comando(sala, esp32_id, comando):
    conexao = sala["conexoes"].get(esp32_id)
    if conexao:
        conexao["fila_saida"].append(comando)


def obter_ou_criar_conexao(sala, esp32_id):
    return sala["conexoes"].setdefault(
        esp32_id, {"fila_saida": [], "ultimo_tick": time.time(), "vinculo": None}
    )


# ════════════════════════════════════════════════════════════════════
# API — SALA
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/criar', methods=['POST'])
def api_criar_sala():
    codigo = novo_codigo_sala()
    with salas_lock:
        salas[codigo] = nova_sala()
    return jsonify({"ok": True, "codigo": codigo})


@app.route('/api/sala/<codigo>/existe')
def api_sala_existe(codigo):
    return jsonify({"ok": True, "existe": obter_sala(codigo) is not None})


# ════════════════════════════════════════════════════════════════════
# API — CADASTRO -> PROVA
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/<codigo>/cadastrar_prova', methods=['POST'])
def api_cadastrar_prova(codigo):
    sala = obter_sala(codigo)
    if not sala:
        return jsonify({"ok": False, "erro": "sala não encontrada"}), 404
    dados = request.get_json(force=True) or {}
    tipo = dados.get('tipo')
    passaros = dados.get('passaros', [])
    if tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False, "erro": "tipo inválido"}), 400
    with sala['lock']:
        prova = sala['provas'][tipo]
        for p in passaros:
            nome = str(p.get('nome', '')).strip()[:40]
            if not nome:
                continue
            item_id = str(sala['proximo_id'])
            sala['proximo_id'] += 1
            prova['itens'][item_id] = {
                "nome": nome,
                "esp32_id": None,
                "tempo_texto": "00:00:000",
                "tempo_segundos": 0.0,
            }
    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════════════
# API — PROVA (tela do organizador)
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/<codigo>/prova/<tipo>')
def api_ver_prova(codigo, tipo):
    sala = obter_sala(codigo)
    if not sala or tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    with sala['lock']:
        prova = sala['provas'][tipo]
        itens = [
            {
                "id": iid,
                "nome": item["nome"],
                "vinculado": item["esp32_id"] is not None,
                "tempo_texto": item["tempo_texto"],
                "tempo_segundos": item["tempo_segundos"],
            }
            for iid, item in prova['itens'].items()
        ]
        itens.sort(key=lambda x: x['tempo_segundos'], reverse=True)
        resp = {
            "ok": True,
            "ativa": prova['ativa'],
            "finalizada": prova['finalizada'],
            "duracao": prova['duracao'],
            "tempo_restante": tempo_restante(prova) if prova['ativa'] else prova['duracao'],
            "itens": itens,
        }
    return jsonify(resp)


@app.route('/api/sala/<codigo>/prova/<tipo>/iniciar', methods=['POST'])
def api_iniciar_prova(codigo, tipo):
    sala = obter_sala(codigo)
    if not sala or tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    with sala['lock']:
        prova = sala['provas'][tipo]
        if not prova['itens']:
            return jsonify({"ok": False, "erro": "nenhum pássaro nessa prova"}), 400
        prova['ativa'] = True
        prova['finalizada'] = False
        prova['iniciada_em'] = time.time()
        restante_txt = formatar_tempo(prova['duracao'])
        for item in prova['itens'].values():
            if item['esp32_id']:
                empurrar_comando(sala, item['esp32_id'], f"PROVA:{restante_txt}")
    return jsonify({"ok": True})


@app.route('/api/sala/<codigo>/prova/<tipo>/finalizar', methods=['POST'])
def api_finalizar_prova(codigo, tipo):
    sala = obter_sala(codigo)
    if not sala or tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    with sala['lock']:
        prova = sala['provas'][tipo]
        prova['ativa'] = False
        prova['finalizada'] = True
        for item in prova['itens'].values():
            if item['esp32_id']:
                empurrar_comando(sala, item['esp32_id'], "FINALIZAR")
    return jsonify({"ok": True})


@app.route('/api/sala/<codigo>/prova/<tipo>/limpar', methods=['POST'])
def api_limpar_prova(codigo, tipo):
    sala = obter_sala(codigo)
    if not sala or tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    with sala['lock']:
        prova = sala['provas'][tipo]
        for item in prova['itens'].values():
            if item['esp32_id']:
                empurrar_comando(sala, item['esp32_id'], "RESET")
                conexao = sala['conexoes'].get(item['esp32_id'])
                if conexao:
                    conexao['vinculo'] = None
        sala['provas'][tipo] = nova_prova(tipo)
    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════════════
# API — CLASSIFICAR PARA FINAL
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/<codigo>/classificar', methods=['POST'])
def api_classificar(codigo):
    sala = obter_sala(codigo)
    if not sala:
        return jsonify({"ok": False}), 404
    dados = request.get_json(force=True) or {}
    with sala['lock']:
        try:
            qtd = int(dados.get('quantidade', sala['quantidade_classificados']))
        except (TypeError, ValueError):
            qtd = sala['quantidade_classificados']
        qtd = max(0, qtd)
        sala['quantidade_classificados'] = qtd

        elim = sala['provas']['eliminatorias']
        ranking = sorted(elim['itens'].values(), key=lambda x: x['tempo_segundos'], reverse=True)
        classificados = ranking[:qtd]

        final = sala['provas']['final']
        for item in classificados:
            item_id = str(sala['proximo_id'])
            sala['proximo_id'] += 1
            final['itens'][item_id] = {
                "nome": item['nome'],
                "esp32_id": None,
                "tempo_texto": "00:00:000",
                "tempo_segundos": 0.0,
            }
    return jsonify({"ok": True, "classificados": len(classificados)})


# ════════════════════════════════════════════════════════════════════
# API — CELULAR (vincular / desvincular / tick)
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/<codigo>/passaros_livres/<tipo>')
def api_passaros_livres(codigo, tipo):
    sala = obter_sala(codigo)
    if not sala or tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    with sala['lock']:
        prova = sala['provas'][tipo]
        livres = [
            {"id": iid, "nome": item['nome']}
            for iid, item in prova['itens'].items() if not item['esp32_id']
        ]
    return jsonify({"ok": True, "itens": livres})


@app.route('/api/sala/<codigo>/vincular', methods=['POST'])
def api_vincular(codigo):
    sala = obter_sala(codigo)
    if not sala:
        return jsonify({"ok": False, "erro": "sala não encontrada"}), 404
    dados = request.get_json(force=True) or {}
    tipo = dados.get('tipo')
    item_id = str(dados.get('item_id', ''))
    esp32_id = dados.get('esp32_id')
    if tipo not in ('eliminatorias', 'final') or not item_id or not esp32_id:
        return jsonify({"ok": False, "erro": "parâmetros inválidos"}), 400
    with sala['lock']:
        prova = sala['provas'][tipo]
        item = prova['itens'].get(item_id)
        if not item:
            return jsonify({"ok": False, "erro": "pássaro não encontrado"}), 404
        if item['esp32_id']:
            return jsonify({"ok": False, "erro": "esse pássaro já está vinculado a outro celular"}), 409

        conexao = obter_ou_criar_conexao(sala, esp32_id)
        if conexao['vinculo']:
            v_tipo, v_item = conexao['vinculo']
            outro = sala['provas'][v_tipo]['itens'].get(v_item)
            if outro:
                outro['esp32_id'] = None

        item['esp32_id'] = esp32_id
        conexao['vinculo'] = (tipo, item_id)
        conexao['fila_saida'].append(f"NOME:{item['nome'][:16]}")
        if prova['ativa']:
            conexao['fila_saida'].append(f"PROVA:{formatar_tempo(tempo_restante(prova))}")
    return jsonify({"ok": True})


@app.route('/api/sala/<codigo>/desvincular', methods=['POST'])
def api_desvincular(codigo):
    sala = obter_sala(codigo)
    if not sala:
        return jsonify({"ok": False}), 404
    dados = request.get_json(force=True) or {}
    tipo = dados.get('tipo')
    item_id = str(dados.get('item_id', ''))
    if tipo not in ('eliminatorias', 'final') or not item_id:
        return jsonify({"ok": False}), 400
    with sala['lock']:
        item = sala['provas'][tipo]['itens'].get(item_id)
        if item and item['esp32_id']:
            conexao = sala['conexoes'].get(item['esp32_id'])
            if conexao:
                conexao['vinculo'] = None
            item['esp32_id'] = None
    return jsonify({"ok": True})


@app.route('/api/sala/<codigo>/tick', methods=['POST'])
def api_tick(codigo):
    sala = obter_sala(codigo)
    if not sala:
        return jsonify({"ok": False, "erro": "sala não encontrada"}), 404
    dados = request.get_json(force=True) or {}
    esp32_id = dados.get('esp32_id')
    tempo_str = dados.get('t', '')
    if not esp32_id:
        return jsonify({"ok": False}), 400
    with sala['lock']:
        conexao = obter_ou_criar_conexao(sala, esp32_id)
        conexao['ultimo_tick'] = time.time()
        if tempo_str and validar_tempo(tempo_str) and conexao['vinculo']:
            tipo, item_id = conexao['vinculo']
            prova = sala['provas'][tipo]
            item = prova['itens'].get(item_id)
            if item and prova['ativa']:
                item['tempo_texto'] = tempo_str
                item['tempo_segundos'] = tempo_para_segundos(tempo_str)
        comandos = conexao['fila_saida']
        conexao['fila_saida'] = []
    return jsonify({"ok": True, "comandos": comandos})


# ════════════════════════════════════════════════════════════════════
# API — RESULTADO GERAL
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/<codigo>/ranking_geral')
def api_ranking_geral(codigo):
    sala = obter_sala(codigo)
    if not sala:
        return jsonify({"ok": False}), 404
    with sala['lock']:
        todos = []
        for tipo in ('eliminatorias', 'final'):
            for item in sala['provas'][tipo]['itens'].values():
                todos.append({
                    "nome": item['nome'],
                    "tipo": tipo,
                    "tempo_texto": item['tempo_texto'],
                    "tempo_segundos": item['tempo_segundos'],
                })
        todos.sort(key=lambda x: x['tempo_segundos'], reverse=True)
        for i, r in enumerate(todos, 1):
            r['posicao'] = i
    return jsonify({"ok": True, "ranking": todos})


# ════════════════════════════════════════════════════════════════════
# LIMPEZA DE SALAS VELHAS (evita crescer memória pra sempre)
# ════════════════════════════════════════════════════════════════════

def _reaper():
    while True:
        time.sleep(60)
        agora = time.time()
        with salas_lock:
            expiradas = [c for c, s in salas.items() if agora - s['ultimo_uso'] > 12 * 3600]
            for c in expiradas:
                del salas[c]


threading.Thread(target=_reaper, daemon=True).start()


# ════════════════════════════════════════════════════════════════════
# TELAS (HTML embutido — sem build, sem framework de frontend)
# ════════════════════════════════════════════════════════════════════

ESTILO_BASE = """
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
body { background:#0B1629; font-family:'Trebuchet MS', Arial, sans-serif; color:#EAF1FF; min-height:100vh; }
a { color:inherit; }
.wrap { max-width:760px; margin:0 auto; padding:18px; }
h1 { color:#F0C030; font-size:20px; margin-bottom:4px; }
h2 { color:#F0C030; font-size:15px; margin:16px 0 8px; }
.sub { color:#93a4c3; font-size:12px; margin-bottom:14px; }
.card { background:#16213d; border-radius:12px; padding:16px; margin-bottom:12px; border-left:4px solid #C9980E; }
input[type=text], input[type=number] {
  width:100%; padding:12px; border-radius:8px; border:1px solid #2a3a63; background:#0f1830;
  color:#EAF1FF; font-size:15px; margin-bottom:8px;
}
button { cursor:pointer; border:none; border-radius:10px; font-weight:bold; padding:12px 16px; font-size:14px; }
.btn-ouro { background:#F0C030; color:#0B1629; }
.btn-azul { background:#1558B0; color:white; }
.btn-verde { background:#177A38; color:white; }
.btn-vermelho { background:#B0271A; color:white; }
.btn-roxo { background:#6025A8; color:white; }
.btn-cinza { background:#2a3a63; color:#c9d6f0; }
.linha-botoes { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
table { width:100%; border-collapse:collapse; margin-top:8px; }
th, td { text-align:left; padding:8px 6px; border-bottom:1px solid #223154; font-size:13px; }
th { color:#93a4c3; font-weight:normal; }
.tag { display:inline-block; padding:2px 8px; border-radius:20px; font-size:11px; font-weight:bold; }
.tag-ok { background:#177A38; color:white; }
.tag-nao { background:#2a3a63; color:#93a4c3; }
.tabs { display:flex; gap:6px; margin-bottom:14px; flex-wrap:wrap; }
.tab-btn { background:#16213d; color:#93a4c3; border-radius:8px; padding:10px 14px; font-size:13px; }
.tab-btn.ativa { background:#F0C030; color:#0B1629; }
.vazio { text-align:center; color:#5a6d94; padding:14px; font-size:13px; }
.codigo-sala { font-family:monospace; font-size:22px; letter-spacing:3px; color:#F0C030; font-weight:bold; }
"""


HTML_HOME = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Marcador Digital - Web</title>
<style>""" + ESTILO_BASE + """</style>
</head>
<body>
<div class="wrap">
  <h1>🐦 Marcador Digital</h1>
  <div class="sub">Cronômetro de provas — versão web</div>

  <div class="card">
    <h2 style="margin-top:0">Criar uma sala nova</h2>
    <div class="sub">Você é o organizador: cadastro fica salvo no seu navegador.</div>
    <button class="btn-ouro" onclick="criarSala()" style="width:100%">➕ Criar nova sala</button>
  </div>

  <div class="card">
    <h2 style="margin-top:0">Já tenho uma sala</h2>
    <input type="text" id="codigoEntrar" placeholder="Código da sala (ex: AB12CD)" maxlength="6" style="text-transform:uppercase">
    <div class="linha-botoes">
      <button class="btn-azul" onclick="entrarComoOrganizador()">🧑‍💻 Entrar como organizador</button>
      <button class="btn-roxo" onclick="entrarComoCelular()">📟 Entrar como celular-ESP32</button>
    </div>
  </div>
</div>
<script>
async function criarSala() {
  const resp = await fetch('/api/sala/criar', {method:'POST'});
  const data = await resp.json();
  if (data.ok) {
    location.href = '/organizador/' + data.codigo;
  } else {
    alert('Erro ao criar sala.');
  }
}
function codigoDigitado() {
  return document.getElementById('codigoEntrar').value.trim().toUpperCase();
}
function entrarComoOrganizador() {
  const codigo = codigoDigitado();
  if (!codigo) { alert('Digite o código da sala.'); return; }
  location.href = '/organizador/' + codigo;
}
function entrarComoCelular() {
  const codigo = codigoDigitado();
  if (!codigo) { alert('Digite o código da sala.'); return; }
  location.href = '/celular/' + codigo;
}
</script>
</body>
</html>"""


@app.route('/')
def tela_home():
    return HTML_HOME


HTML_ORGANIZADOR = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Marcador Digital - Organizador</title>
<style>""" + ESTILO_BASE + """
.faixa-codigo { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="faixa-codigo">
    <div>
      <h1 style="margin-bottom:2px">🐦 Marcador Digital</h1>
      <div class="sub">sala <span class="codigo-sala" id="codigoTopo">------</span></div>
    </div>
    <button class="btn-roxo" onclick="copiarLinkCelular()">📟 Link do celular</button>
  </div>

  <div class="tabs">
    <button class="tab-btn" id="tabCadastroBtn" onclick="mostrarTela('cadastro')">📋 Cadastro</button>
    <button class="tab-btn" id="tabEliminatoriaBtn" onclick="mostrarTela('eliminatorias')">🔵 Eliminatória</button>
    <button class="tab-btn" id="tabFinalBtn" onclick="mostrarTela('final')">🔴 Final</button>
    <button class="tab-btn" id="tabGeralBtn" onclick="mostrarTela('geral')">🏆 Resultado Geral</button>
  </div>

  <!-- ═══════ CADASTRO ═══════ -->
  <div id="telaCadastro" class="card" style="display:none">
    <h2 style="margin-top:0">Cadastro de pássaros</h2>
    <div class="sub">Fica salvo neste navegador (nesta sala).</div>
    <input type="text" id="nomeNovoPassaro" placeholder="Nome do pássaro">
    <button class="btn-azul" onclick="adicionarPassaroCadastro()" style="width:100%">➕ Adicionar ao cadastro</button>
    <table id="tabelaCadastro"><tbody></tbody></table>
    <div class="linha-botoes">
      <button class="btn-ouro" onclick="adicionarTodosAProva()" style="flex:1">
        ➡️ Adicionar TODOS à prova (<span id="destinoCadastro">Eliminatória</span>)
      </button>
    </div>
  </div>

  <!-- ═══════ ELIMINATÓRIA / FINAL (mesma estrutura, tipo trocado por JS) ═══════ -->
  <div id="telaProva" class="card" style="display:none">
    <h2 style="margin-top:0" id="tituloProva">Eliminatória</h2>
    <div class="sub" id="statusProva">carregando...</div>
    <table id="tabelaProva">
      <thead><tr><th>Pássaro</th><th>Vinculado</th><th>Tempo</th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="linha-botoes">
      <button class="btn-verde" onclick="acaoProva('iniciar')">▶ Iniciar Prova</button>
      <button class="btn-cinza" onclick="irParaCadastro()">📋 Cadastro</button>
      <button class="btn-vermelho" onclick="acaoProva('finalizar')">⏹ Finalizar Prova</button>
    </div>
    <div class="linha-botoes">
      <button class="btn-ouro" id="btnClassificar" onclick="classificarParaFinal()" style="display:none">🏅 Classificar para Final</button>
      <button class="btn-cinza" onclick="acaoProva('limpar')">🧹 Limpar Prova</button>
    </div>
  </div>

  <!-- ═══════ RESULTADO GERAL ═══════ -->
  <div id="telaGeral" class="card" style="display:none">
    <h2 style="margin-top:0">🏆 Resultado Geral</h2>
    <div class="sub">Eliminatória + Final combinados, do maior tempo de canto pro menor.</div>
    <table id="tabelaGeral">
      <thead><tr><th>#</th><th>Pássaro</th><th>Fase</th><th>Tempo</th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="linha-botoes">
      <button class="btn-ouro" onclick="salvarResultadoNoNavegador()">💾 Salvar resultado no navegador</button>
    </div>
  </div>
</div>

<script>
const codigo = location.pathname.split('/').pop().toUpperCase();
document.getElementById('codigoTopo').textContent = codigo;

let telaAtual = 'cadastro';
let tipoProvaAtual = 'eliminatorias';   // controla se "telaProva" está mostrando eliminatória ou final
let origemCadastro = 'eliminatorias';   // pra onde "Adicionar à prova" manda quando estamos no Cadastro

const CHAVE_CADASTRO = 'md_cadastro_' + codigo;

function carregarCadastro() {
  try { return JSON.parse(localStorage.getItem(CHAVE_CADASTRO)) || []; }
  catch (e) { return []; }
}
function salvarCadastro(lista) {
  localStorage.setItem(CHAVE_CADASTRO, JSON.stringify(lista));
}

function copiarLinkCelular() {
  const link = location.origin + '/celular/' + codigo;
  navigator.clipboard && navigator.clipboard.writeText(link);
  alert('Link copiado:\\n' + link + '\\n\\nManda pra quem vai usar o celular como marcador.');
}

// ═══════ NAVEGAÇÃO ENTRE TELAS ═══════
function mostrarTela(nome) {
  telaAtual = nome;
  document.getElementById('telaCadastro').style.display = nome === 'cadastro' ? 'block' : 'none';
  document.getElementById('telaProva').style.display = (nome === 'eliminatorias' || nome === 'final') ? 'block' : 'none';
  document.getElementById('telaGeral').style.display = nome === 'geral' ? 'block' : 'none';

  ['tabCadastroBtn','tabEliminatoriaBtn','tabFinalBtn','tabGeralBtn'].forEach(id => document.getElementById(id).classList.remove('ativa'));
  if (nome === 'cadastro') document.getElementById('tabCadastroBtn').classList.add('ativa');
  if (nome === 'eliminatorias') document.getElementById('tabEliminatoriaBtn').classList.add('ativa');
  if (nome === 'final') document.getElementById('tabFinalBtn').classList.add('ativa');
  if (nome === 'geral') document.getElementById('tabGeralBtn').classList.add('ativa');

  if (nome === 'cadastro') { renderCadastro(); }
  if (nome === 'eliminatorias' || nome === 'final') {
    tipoProvaAtual = nome;
    document.getElementById('tituloProva').textContent = nome === 'eliminatorias' ? '🔵 Eliminatória' : '🔴 Final';
    document.getElementById('btnClassificar').style.display = nome === 'eliminatorias' ? 'inline-block' : 'none';
    atualizarProva();
  }
  if (nome === 'geral') { atualizarGeral(); }
}

function irParaCadastro() {
  origemCadastro = tipoProvaAtual;
  document.getElementById('destinoCadastro').textContent = origemCadastro === 'eliminatorias' ? 'Eliminatória' : 'Final';
  mostrarTela('cadastro');
}

// ═══════ CADASTRO ═══════
function renderCadastro() {
  document.getElementById('destinoCadastro').textContent = origemCadastro === 'eliminatorias' ? 'Eliminatória' : 'Final';
  const lista = carregarCadastro();
  const tbody = document.querySelector('#tabelaCadastro tbody');
  if (lista.length === 0) {
    tbody.innerHTML = '<tr><td class="vazio" colspan="2">nenhum pássaro cadastrado ainda</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  lista.forEach((p, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${p.nome}</td><td style="text-align:right; white-space:nowrap;">
      <button class="btn-cinza" style="padding:4px 8px; font-size:11px;" onclick="editarPassaroCadastro(${i})">✏️</button>
      <button class="btn-vermelho" style="padding:4px 8px; font-size:11px;" onclick="removerPassaroCadastro(${i})">🗑️</button>
    </td>`;
    tbody.appendChild(tr);
  });
}

function adicionarPassaroCadastro() {
  const input = document.getElementById('nomeNovoPassaro');
  const nome = input.value.trim();
  if (!nome) return;
  const lista = carregarCadastro();
  lista.push({ nome });
  salvarCadastro(lista);
  input.value = '';
  renderCadastro();
}

function editarPassaroCadastro(i) {
  const lista = carregarCadastro();
  const novoNome = prompt('Novo nome:', lista[i].nome);
  if (novoNome && novoNome.trim()) {
    lista[i].nome = novoNome.trim();
    salvarCadastro(lista);
    renderCadastro();
  }
}

function removerPassaroCadastro(i) {
  const lista = carregarCadastro();
  lista.splice(i, 1);
  salvarCadastro(lista);
  renderCadastro();
}

async function adicionarTodosAProva() {
  const lista = carregarCadastro();
  if (lista.length === 0) { alert('Cadastre pelo menos um pássaro primeiro.'); return; }
  const resp = await fetch(`/api/sala/${codigo}/cadastrar_prova`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ tipo: origemCadastro, passaros: lista })
  });
  const data = await resp.json();
  if (data.ok) {
    alert(`${lista.length} pássaro(s) adicionados à ${origemCadastro === 'eliminatorias' ? 'Eliminatória' : 'Final'}.`);
    mostrarTela(origemCadastro);
  } else {
    alert('Erro: ' + (data.erro || 'desconhecido'));
  }
}

// ═══════ PROVA (Eliminatória / Final) ═══════
async function atualizarProva() {
  if (telaAtual !== 'eliminatorias' && telaAtual !== 'final') return;
  try {
    const resp = await fetch(`/api/sala/${codigo}/prova/${tipoProvaAtual}`);
    const data = await resp.json();
    if (!data.ok) return;
    const status = data.finalizada ? 'finalizada' : (data.ativa ? 'em andamento' : 'aguardando início');
    document.getElementById('statusProva').textContent =
      `${data.itens.length} pássaro(s) — prova ${status}`;
    const tbody = document.querySelector('#tabelaProva tbody');
    if (data.itens.length === 0) {
      tbody.innerHTML = '<tr><td class="vazio" colspan="3">nenhum pássaro nesta prova ainda</td></tr>';
    } else {
      tbody.innerHTML = '';
      data.itens.forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${item.nome}</td>
          <td><span class="tag ${item.vinculado ? 'tag-ok' : 'tag-nao'}">${item.vinculado ? 'sim' : 'não'}</span></td>
          <td style="font-family:monospace">${item.tempo_texto}</td>`;
        tbody.appendChild(tr);
      });
    }
  } catch (e) { /* silencioso: só tenta de novo no próximo ciclo */ }
}

async function acaoProva(acao) {
  if (acao === 'limpar' && !confirm('Tem certeza? Isso apaga os tempos e desvincula os celulares dessa prova.')) return;
  if (acao === 'finalizar' && !confirm('Finalizar esta prova? Os celulares vinculados serão travados.')) return;
  const resp = await fetch(`/api/sala/${codigo}/prova/${tipoProvaAtual}/${acao}`, { method: 'POST' });
  const data = await resp.json();
  if (!data.ok) alert('Erro: ' + (data.erro || 'desconhecido'));
  atualizarProva();
}

async function classificarParaFinal() {
  const padrao = 15;
  const qtdStr = prompt('Quantos pássaros classificam para a Final?', padrao);
  if (qtdStr === null) return;
  const qtd = parseInt(qtdStr, 10);
  if (isNaN(qtd) || qtd < 0) { alert('Número inválido.'); return; }
  const resp = await fetch(`/api/sala/${codigo}/classificar`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ quantidade: qtd })
  });
  const data = await resp.json();
  if (data.ok) {
    alert(`${data.classificados} pássaro(s) classificados para a Final.`);
  } else {
    alert('Erro ao classificar.');
  }
}

// ═══════ RESULTADO GERAL ═══════
async function atualizarGeral() {
  try {
    const resp = await fetch(`/api/sala/${codigo}/ranking_geral`);
    const data = await resp.json();
    if (!data.ok) return;
    const tbody = document.querySelector('#tabelaGeral tbody');
    if (data.ranking.length === 0) {
      tbody.innerHTML = '<tr><td class="vazio" colspan="4">ainda sem resultados</td></tr>';
      return;
    }
    tbody.innerHTML = '';
    data.ranking.forEach(r => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${r.posicao}º</td><td>${r.nome}</td>
        <td>${r.tipo === 'eliminatorias' ? 'Eliminatória' : 'Final'}</td>
        <td style="font-family:monospace">${r.tempo_texto}</td>`;
      tbody.appendChild(tr);
    });
  } catch (e) {}
}

function salvarResultadoNoNavegador() {
  fetch(`/api/sala/${codigo}/ranking_geral`).then(r => r.json()).then(data => {
    if (!data.ok) return;
    const chave = 'md_resultado_' + codigo + '_' + new Date().toISOString().slice(0,10);
    localStorage.setItem(chave, JSON.stringify(data.ranking));
    alert('Resultado salvo neste navegador.');
  });
}

// ═══════ ATUALIZAÇÃO PERIÓDICA ═══════
setInterval(() => {
  if (telaAtual === 'eliminatorias' || telaAtual === 'final') atualizarProva();
  if (telaAtual === 'geral') atualizarGeral();
}, 1000);

mostrarTela('cadastro');
</script>
</body>
</html>"""


@app.route('/organizador/<codigo>')
def tela_organizador(codigo):
    return HTML_ORGANIZADOR


HTML_CELULAR = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, user-scalable=no">
<title>Celular como ESP32</title>
<style>""" + ESTILO_BASE + """
  body { user-select:none; }
  .cat-titulo { color:#F0C030; font-weight:bold; font-size:14px; margin:14px 0 8px; }
  .bolinha { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; background:#ef4444; }
  .bolinha.ok { background:#10b981; }
  .lcd { background:#000; border-radius:14px; padding:18px 12px; margin-bottom:22px; border:3px solid #223; }
  .lcd-linha0 { color:#F0C030; font-family:monospace; font-size:20px; text-align:center;
                white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .lcd-linha1 { color:#3ddc84; font-family:monospace; font-size:34px; text-align:center; margin-top:6px; letter-spacing:1px; }
  .botao-canto { width:100%; padding:60px 0; border-radius:24px; border:none; font-weight:bold; font-size:20px;
                 color:white; background:#1558B0; box-shadow:0 4px 0 #0d3a7c; touch-action:none; }
  .botao-canto.pressionado { background:#177A38; box-shadow:0 2px 0 #0d3a7c; transform:translateY(2px); }
  .botao-canto:disabled { background:#374158; box-shadow:0 4px 0 #232a3a; color:#7c88a6; }
</style>
</head>
<body>
<div class="wrap">
  <h1>📟 Celular como ESP32</h1>
  <div class="sub">sala <span class="codigo-sala" id="codigoTopo" style="font-size:15px;">------</span></div>
  <div class="sub"><span class="bolinha" id="bolinhaStatus"></span><span id="textoStatus">conectando...</span></div>

  <div id="telaVincular">
    <div class="cat-titulo">🔵 ELIMINATÓRIA</div>
    <div id="listaElim"><div class="vazio">carregando...</div></div>
    <div class="cat-titulo">🔴 FINAL</div>
    <div id="listaFinal"><div class="vazio">carregando...</div></div>
  </div>

  <div id="telaMarcador" style="display:none">
    <div class="lcd">
      <div class="lcd-linha0" id="lcdLinha0">-</div>
      <div class="lcd-linha1" id="lcdLinha1">00:00:000</div>
    </div>
    <button class="botao-canto" id="botaoCanto">AGUARDANDO INÍCIO DA PROVA</button>
    <div class="linha-botoes">
      <button class="btn-roxo" onclick="trocarPassaro()" style="width:100%">🔄 Trocar pássaro vinculado</button>
    </div>
  </div>
</div>

<script>
const codigo = location.pathname.split('/').pop().toUpperCase();
document.getElementById('codigoTopo').textContent = codigo;

let deviceId = localStorage.getItem('celular_esp32_id');
if (!deviceId) {
  deviceId = 'CELULAR-' + Math.random().toString(36).slice(2,8).toUpperCase();
  localStorage.setItem('celular_esp32_id', deviceId);
}

let passaroAtual = null;    // {tipo, item_id}
let conectado = false;

let faseAtual = 0;          // 0=NOME 1=PROVA 2=FINALIZADO (sem ADAPT na fibra)
let botaoBloqueado = true;
let provaIniciada = false;
let isRunning = false;
let startTime = 0;
let totalTime = 0;
let nomePassaroAtual = '';
let syncAtiva = false;
let syncTempoRestanteMs = 0;
let syncRecebidoEm = 0;

function formatarTempo(msTotal) {
  if (msTotal < 0) msTotal = 0;
  const ms = Math.floor(msTotal % 1000);
  const totalSeg = Math.floor(msTotal / 1000);
  const seg = totalSeg % 60;
  const min = Math.floor(totalSeg / 60);
  const p2 = n => String(n).padStart(2, '0');
  const p3 = n => String(n).padStart(3, '0');
  return `${p2(min)}:${p2(seg)}:${p3(ms)}`;
}

function atualizarStatusConexao() {
  document.getElementById('bolinhaStatus').classList.toggle('ok', conectado);
  document.getElementById('textoStatus').textContent = conectado
    ? ('conectado como ' + deviceId) : 'sem conexão — tentando de novo...';
}

async function enviarTick(tempoStr) {
  try {
    const resp = await fetch(`/api/sala/${codigo}/tick`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ esp32_id: deviceId, t: tempoStr || '' })
    });
    const data = await resp.json();
    if (!data.ok) { conectado = false; atualizarStatusConexao(); return; }
    if (!conectado) { conectado = true; atualizarStatusConexao(); }
    (data.comandos || []).forEach(processarComando);
  } catch (e) {
    conectado = false;
    atualizarStatusConexao();
  }
}

function processarComando(msg) {
  msg = msg.trim();
  if (msg.startsWith('NOME:')) {
    nomePassaroAtual = msg.substring(5).slice(0, 16);
    faseAtual = 0; syncAtiva = false; botaoBloqueado = true;
    totalTime = 0; startTime = 0; isRunning = false; provaIniciada = false;
    document.getElementById('lcdLinha0').textContent = nomePassaroAtual;
    document.getElementById('lcdLinha1').textContent = '00:00:000';

  } else if (msg.startsWith('PROVA:')) {
    syncTempoRestanteMs = parseTempoParaMs(msg.substring(6));
    syncRecebidoEm = Date.now();
    syncAtiva = true; faseAtual = 1; botaoBloqueado = false;

  } else if (/^reset$/i.test(msg)) {
    totalTime = 0; startTime = 0; isRunning = false; provaIniciada = false;
    botaoBloqueado = true; faseAtual = 0; syncAtiva = false;
    document.getElementById('lcdLinha0').textContent = nomePassaroAtual || deviceId;
    document.getElementById('lcdLinha1').textContent = '00:00:000';

  } else if (/^finalizar$/i.test(msg)) {
    isRunning = false; botaoBloqueado = true; faseAtual = 2; syncAtiva = false;
    document.getElementById('lcdLinha0').textContent = 'PROVA FINALIZADA';
    document.getElementById('lcdLinha1').textContent = 'FINALIZADA';
  }
  atualizarBotao();
}

function parseTempoParaMs(str) {
  const partes = str.split(':');
  if (partes.length !== 3) return 0;
  const mm = parseInt(partes[0], 10) || 0;
  const ss = parseInt(partes[1], 10) || 0;
  const mmm = parseInt(partes[2], 10) || 0;
  return mm * 60000 + ss * 1000 + mmm;
}

function atualizarBotao() {
  const btn = document.getElementById('botaoCanto');
  btn.disabled = botaoBloqueado;
  btn.textContent = botaoBloqueado ? 'AGUARDANDO INÍCIO DA PROVA' : 'SEGURE PARA CANTAR';
}

const botaoEl = document.getElementById('botaoCanto');
function iniciarCanto(ev) {
  ev.preventDefault();
  if (botaoBloqueado || isRunning) return;
  isRunning = true; startTime = Date.now(); provaIniciada = true;
  botaoEl.classList.add('pressionado');
}
function pararCanto(ev) {
  ev.preventDefault();
  if (!isRunning) return;
  isRunning = false;
  totalTime += Date.now() - startTime;
  botaoEl.classList.remove('pressionado');
}
botaoEl.addEventListener('pointerdown', iniciarCanto);
botaoEl.addEventListener('pointerup', pararCanto);
botaoEl.addEventListener('pointercancel', pararCanto);
botaoEl.addEventListener('pointerleave', pararCanto);

let tickEmAndamento = false;
setInterval(() => {
  let displayTime = totalTime;
  if (isRunning) displayTime += Date.now() - startTime;
  const bufLocal = formatarTempo(displayTime);
  document.getElementById('lcdLinha1').textContent = bufLocal;

  if (faseAtual === 1 && syncAtiva) {
    const decorrido = Date.now() - syncRecebidoEm;
    let restante = syncTempoRestanteMs - decorrido;
    if (restante < 0) restante = 0;
    document.getElementById('lcdLinha0').textContent = 'PROVA ' + formatarTempo(restante);
  }

  if (!tickEmAndamento) {
    tickEmAndamento = true;
    enviarTick(provaIniciada ? bufLocal : '').finally(() => { tickEmAndamento = false; });
  }
}, 100);

async function carregarPassaros() {
  try {
    const [elim, final] = await Promise.all([
      fetch(`/api/sala/${codigo}/passaros_livres/eliminatorias`).then(r => r.json()),
      fetch(`/api/sala/${codigo}/passaros_livres/final`).then(r => r.json()),
    ]);
    montarLista('listaElim', elim.ok ? elim.itens : [], 'eliminatorias');
    montarLista('listaFinal', final.ok ? final.itens : [], 'final');
  } catch (e) {}
}

function montarLista(elId, itens, tipo) {
  const el = document.getElementById(elId);
  if (itens.length === 0) {
    el.innerHTML = '<div class="vazio">nenhum pássaro livre nesta categoria</div>';
    return;
  }
  el.innerHTML = '';
  itens.forEach(p => {
    const card = document.createElement('div');
    card.className = 'card';
    card.style.cursor = 'pointer';
    card.textContent = p.nome;
    card.onclick = () => vincularPassaro(tipo, p.id);
    el.appendChild(card);
  });
}

async function vincularPassaro(tipo, itemId) {
  try {
    const resp = await fetch(`/api/sala/${codigo}/vincular`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ tipo, item_id: itemId, esp32_id: deviceId })
    });
    const data = await resp.json();
    if (data.ok) {
      passaroAtual = { tipo, item_id: itemId };
      document.getElementById('telaVincular').style.display = 'none';
      document.getElementById('telaMarcador').style.display = 'block';
      atualizarBotao();
    } else {
      alert('Erro ao vincular: ' + (data.erro || 'desconhecido'));
      carregarPassaros();
    }
  } catch (e) {
    alert('Erro de rede ao vincular.');
  }
}

async function trocarPassaro() {
  if (passaroAtual) {
    try {
      await fetch(`/api/sala/${codigo}/desvincular`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ tipo: passaroAtual.tipo, item_id: passaroAtual.item_id })
      });
    } catch (e) {}
  }
  passaroAtual = null;
  faseAtual = 0; botaoBloqueado = true; provaIniciada = false; isRunning = false;
  totalTime = 0; startTime = 0; syncAtiva = false; nomePassaroAtual = '';
  document.getElementById('telaMarcador').style.display = 'none';
  document.getElementById('telaVincular').style.display = 'block';
  carregarPassaros();
}

let wakeLock = null;
async function pedirWakeLock() {
  try { if ('wakeLock' in navigator) wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
}
document.addEventListener('visibilitychange', async () => {
  if (wakeLock !== null && document.visibilityState === 'visible') await pedirWakeLock();
});
pedirWakeLock();

carregarPassaros();
setInterval(carregarPassaros, 5000);
</script>
</body>
</html>"""


@app.route('/celular/<codigo>')
def tela_celular(codigo):
    return HTML_CELULAR


if __name__ == '__main__':
    porta = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=porta, debug=False)
