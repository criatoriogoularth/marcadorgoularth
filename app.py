import os
import time
import random
import string
import threading

from flask import Flask, request, jsonify

import db

app = Flask(__name__)

# ════════════════════════════════════════════════════════════════════
# ESTADO EFÊMERO (só isso continua em memória — não precisa sobreviver
# a reinício do servidor, porque o vínculo em si já está salvo no
# banco). São só os comandos "pendentes de entrega" pro celular
# (NOME:/PROVA:/RESET/FINALIZAR) até a próxima vez que ele der um tick.
# ════════════════════════════════════════════════════════════════════

conexoes_lock = threading.Lock()
conexoes = {}   # esp32_id -> {"fila_saida": [...], "ultimo_tick": ts}


def empurrar_comandos(esp32_id, comandos):
    if not esp32_id or not comandos:
        return
    with conexoes_lock:
        c = conexoes.setdefault(esp32_id, {"fila_saida": [], "ultimo_tick": time.time()})
        c["fila_saida"].extend(comandos)


def novo_codigo_sala():
    alfabeto = string.ascii_uppercase + string.digits
    while True:
        codigo = ''.join(random.choice(alfabeto) for _ in range(6))
        if not db.sala_existe(codigo):
            return codigo


# ════════════════════════════════════════════════════════════════════
# API — SALA
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/criar', methods=['POST'])
def api_criar_sala():
    codigo = novo_codigo_sala()
    db.criar_sala(codigo)
    return jsonify({"ok": True, "codigo": codigo})


@app.route('/api/sala/<codigo>/existe')
def api_sala_existe(codigo):
    return jsonify({"ok": True, "existe": db.sala_existe(codigo.upper())})


# ════════════════════════════════════════════════════════════════════
# API — CADASTRO -> PROVA
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/<codigo>/cadastrar_prova', methods=['POST'])
def api_cadastrar_prova(codigo):
    codigo = codigo.upper()
    if not db.sala_existe(codigo):
        return jsonify({"ok": False, "erro": "sala não encontrada"}), 404
    dados = request.get_json(force=True) or {}
    tipo = dados.get('tipo')
    passaros = dados.get('passaros', [])
    if tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False, "erro": "tipo inválido"}), 400
    db.cadastrar_prova(codigo, tipo, passaros)
    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════════════
# API — PROVA (tela do organizador)
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/<codigo>/prova/<tipo>')
def api_ver_prova(codigo, tipo):
    codigo = codigo.upper()
    if tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    resultado = db.ver_prova(codigo, tipo)
    if resultado is None:
        return jsonify({"ok": False}), 404
    prova = resultado['prova']
    itens = [
        {
            "id": it['id'],
            "nome": it['nome'],
            "anilha": it['anilha'],
            "proprietario": it['proprietario'],
            "vinculado": it['esp32_id'] is not None,
            "tempo_texto": it['tempo_texto'],
            "tempo_segundos": it['tempo_segundos'],
        }
        for it in resultado['itens']
    ]
    return jsonify({
        "ok": True,
        "ativa": prova['ativa'],
        "finalizada": prova['finalizada'],
        "duracao": prova['duracao'],
        "tempo_restante": db.tempo_restante_segundos(prova),
        "itens": itens,
    })


@app.route('/api/sala/<codigo>/prova/<tipo>/iniciar', methods=['POST'])
def api_iniciar_prova(codigo, tipo):
    codigo = codigo.upper()
    if tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    resultado = db.iniciar_prova(codigo, tipo)
    if resultado is None:
        return jsonify({"ok": False, "erro": "nenhum pássaro nessa prova"}), 400
    restante_txt = db.formatar_tempo(resultado['duracao'])
    for esp32_id in resultado['vinculados']:
        empurrar_comandos(esp32_id, [f"PROVA:{restante_txt}"])
    return jsonify({"ok": True})


@app.route('/api/sala/<codigo>/prova/<tipo>/finalizar', methods=['POST'])
def api_finalizar_prova(codigo, tipo):
    codigo = codigo.upper()
    if tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    vinculados = db.finalizar_prova(codigo, tipo)
    for esp32_id in vinculados:
        empurrar_comandos(esp32_id, ["FINALIZAR"])
    return jsonify({"ok": True})


@app.route('/api/sala/<codigo>/prova/<tipo>/limpar', methods=['POST'])
def api_limpar_prova(codigo, tipo):
    codigo = codigo.upper()
    if tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    vinculados = db.limpar_prova(codigo, tipo)
    for esp32_id in vinculados:
        empurrar_comandos(esp32_id, ["RESET"])
    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════════════
# API — CLASSIFICAR PARA FINAL
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/<codigo>/classificar', methods=['POST'])
def api_classificar(codigo):
    codigo = codigo.upper()
    if not db.sala_existe(codigo):
        return jsonify({"ok": False}), 404
    dados = request.get_json(force=True) or {}
    try:
        qtd = int(dados.get('quantidade', db.obter_quantidade_classificados(codigo)))
    except (TypeError, ValueError):
        qtd = db.obter_quantidade_classificados(codigo)
    qtd = max(0, qtd)
    total = db.classificar(codigo, qtd)
    return jsonify({"ok": True, "classificados": total})


# ════════════════════════════════════════════════════════════════════
# API — CELULAR (vincular / desvincular / tick)
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/<codigo>/passaros_livres/<tipo>')
def api_passaros_livres(codigo, tipo):
    codigo = codigo.upper()
    if tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    itens = db.passaros_livres(codigo, tipo)
    return jsonify({"ok": True, "itens": [{"id": it['id'], "nome": it['nome']} for it in itens]})


@app.route('/api/sala/<codigo>/vincular', methods=['POST'])
def api_vincular(codigo):
    codigo = codigo.upper()
    if not db.sala_existe(codigo):
        return jsonify({"ok": False, "erro": "sala não encontrada"}), 404
    dados = request.get_json(force=True) or {}
    tipo = dados.get('tipo')
    esp32_id = dados.get('esp32_id')
    try:
        item_id = int(dados.get('item_id'))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "erro": "parâmetros inválidos"}), 400
    if tipo not in ('eliminatorias', 'final') or not esp32_id:
        return jsonify({"ok": False, "erro": "parâmetros inválidos"}), 400

    resultado = db.vincular(codigo, tipo, item_id, esp32_id)
    if not resultado['ok']:
        status = 409 if 'já está vinculado' in resultado.get('erro', '') else 404
        return jsonify(resultado), status

    with conexoes_lock:
        conexoes[esp32_id] = {"fila_saida": list(resultado['comandos']), "ultimo_tick": time.time()}
    return jsonify({"ok": True})


@app.route('/api/sala/<codigo>/desvincular', methods=['POST'])
def api_desvincular(codigo):
    codigo = codigo.upper()
    if not db.sala_existe(codigo):
        return jsonify({"ok": False}), 404
    dados = request.get_json(force=True) or {}
    tipo = dados.get('tipo')
    try:
        item_id = int(dados.get('item_id'))
    except (TypeError, ValueError):
        return jsonify({"ok": False}), 400
    if tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 400
    db.desvincular(codigo, tipo, item_id)
    return jsonify({"ok": True})


@app.route('/api/sala/<codigo>/tick', methods=['POST'])
def api_tick(codigo):
    codigo = codigo.upper()
    if not db.sala_existe(codigo):
        return jsonify({"ok": False, "erro": "sala não encontrada"}), 404
    dados = request.get_json(force=True) or {}
    esp32_id = dados.get('esp32_id')
    tempo_str = dados.get('t', '')
    if not esp32_id:
        return jsonify({"ok": False}), 400

    tempo_valido = db.validar_tempo(tempo_str)
    tempo_seg = db.tempo_para_segundos(tempo_str) if tempo_valido else 0.0
    db.tick(codigo, esp32_id, tempo_str, tempo_valido, tempo_seg)

    with conexoes_lock:
        c = conexoes.setdefault(esp32_id, {"fila_saida": [], "ultimo_tick": time.time()})
        c["ultimo_tick"] = time.time()
        comandos = c["fila_saida"]
        c["fila_saida"] = []
    return jsonify({"ok": True, "comandos": comandos})


# ════════════════════════════════════════════════════════════════════
# API — RESULTADO GERAL
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/<codigo>/ranking_geral')
def api_ranking_geral(codigo):
    codigo = codigo.upper()
    if not db.sala_existe(codigo):
        return jsonify({"ok": False}), 404
    ranking = db.ranking_geral(codigo)
    return jsonify({"ok": True, "ranking": ranking})


# ════════════════════════════════════════════════════════════════════
# FINALIZAÇÃO AUTOMÁTICA + LIMPEZA (roda em segundo plano)
# ════════════════════════════════════════════════════════════════════

def _reaper():
    while True:
        time.sleep(2)
        try:
            for vencida in db.provas_para_finalizar_automaticamente():
                for esp32_id in vencida['vinculados']:
                    empurrar_comandos(esp32_id, ["FINALIZAR"])
        except Exception as e:
            print(f"⚠ erro no reaper (finalização automática): {e}")

        try:
            apagadas = db.apagar_salas_expiradas(horas=48)
            if apagadas:
                print(f"🧹 salas apagadas por expiração (48h): {apagadas}")
        except Exception as e:
            print(f"⚠ erro no reaper (limpeza de salas): {e}")

        # limpa conexões efêmeras (fila de comandos) sem tick há muito tempo,
        # só pra não crescer memória à toa — não afeta o vínculo no banco
        agora = time.time()
        with conexoes_lock:
            mortas = [eid for eid, c in conexoes.items() if agora - c['ultimo_tick'] > 3600]
            for eid in mortas:
                del conexoes[eid]


# ════════════════════════════════════════════════════════════════════
# INICIALIZAÇÃO
# ════════════════════════════════════════════════════════════════════

try:
    db.inicializar_schema()
    threading.Thread(target=_reaper, daemon=True).start()
    print("✅ Banco de dados conectado e schema verificado.")
except Exception as e:
    print(f"⚠ AVISO: não consegui conectar/inicializar o banco de dados agora: {e}")
    print("   Defina a variável de ambiente DATABASE_URL com a connection string do Neon.")


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
<title>Marcador Digital Goularth - Web</title>
<style>""" + ESTILO_BASE + """</style>
</head>
<body>
<div class="wrap">
  <h1>🐦 Marcador Digital Goularth</h1>
  <div class="sub">versão web — cronômetro de provas</div>

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
<title>Marcador Digital Goularth - Organizador</title>
<style>""" + ESTILO_BASE + """
.faixa-codigo { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; }
.relogio { text-align:center; margin:6px 0 16px; }
.relogio-numero { font-family:monospace; font-size:52px; color:#F0C030; font-weight:bold; letter-spacing:2px; }
.relogio-legenda { color:#93a4c3; font-size:12px; }
.relogio.acabando .relogio-numero { color:#B0271A; }
.aviso-offline { background:#4a1414; border:1px solid #B0271A; color:#ffd0cc; padding:10px 12px;
                  border-radius:8px; font-size:12px; margin-bottom:12px; display:none; }
.aviso-offline.mostrar { display:block; }
</style>
</head>
<body>
<div class="wrap">
  <div class="faixa-codigo">
    <div>
      <h1 style="margin-bottom:2px">🐦 Marcador Digital Goularth</h1>
      <div class="sub">sala <span class="codigo-sala" id="codigoTopo">------</span></div>
    </div>
    <button class="btn-roxo" onclick="copiarLinkCelular()">📟 Link do celular</button>
  </div>

  <div class="aviso-offline" id="avisoOffline">
    ⚠ Sem conexão com o servidor da sala agora (pode ter reiniciado). Os
    resultados que já chegaram continuam salvos aqui neste navegador.
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
    <input type="text" id="anilhaNovoPassaro" placeholder="Anilha">
    <input type="text" id="proprietarioNovoPassaro" placeholder="Proprietário">
    <button class="btn-azul" onclick="adicionarPassaroCadastro()" style="width:100%">➕ Adicionar ao cadastro</button>
    <table id="tabelaCadastro">
      <thead><tr><th>Pássaro</th><th>Anilha</th><th>Proprietário</th><th></th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="linha-botoes">
      <button class="btn-ouro" onclick="adicionarTodosAProva()" style="flex:1">
        ➡️ Adicionar TODOS à prova (<span id="destinoCadastro">Eliminatória</span>)
      </button>
    </div>
  </div>

  <!-- ═══════ ELIMINATÓRIA / FINAL (mesma estrutura, tipo trocado por JS) ═══════ -->
  <div id="telaProva" class="card" style="display:none">
    <h2 style="margin-top:0" id="tituloProva">Eliminatória</h2>

    <div class="relogio" id="blocoRelogio" style="display:none">
      <div class="relogio-numero" id="relogioNumero">00:00</div>
      <div class="relogio-legenda" id="relogioLegenda">tempo restante da prova</div>
    </div>

    <div class="sub" id="statusProva">carregando...</div>
    <table id="tabelaProva">
      <thead><tr><th>Pássaro</th><th>Anilha</th><th>Proprietário</th><th>Vinculado</th><th>Tempo</th></tr></thead>
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
    <div class="sub">Colocação pelo tempo da Final (a Eliminatória é só classificatória). Salvo automaticamente neste navegador.</div>
    <table id="tabelaGeral">
      <thead><tr><th>#</th><th>Pássaro</th><th>Anilha</th><th>Proprietário</th><th>Eliminatória</th><th>Final</th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="linha-botoes">
      <button class="btn-ouro" onclick="gerarImagemResultado()" style="width:100%">🖼️ Gerar imagem pra compartilhar</button>
    </div>
  </div>
</div>
<canvas id="canvasResultado" style="display:none"></canvas>

<script>
const codigo = location.pathname.split('/').pop().toUpperCase();
document.getElementById('codigoTopo').textContent = codigo;

let telaAtual = 'cadastro';
let tipoProvaAtual = 'eliminatorias';
let origemCadastro = 'eliminatorias';

const CHAVE_CADASTRO = 'md_cadastro_' + codigo;
const CHAVE_RESULTADOS = 'md_resultados_' + codigo;

function carregarCadastro() {
  try { return JSON.parse(localStorage.getItem(CHAVE_CADASTRO)) || []; }
  catch (e) { return []; }
}
function salvarCadastro(lista) {
  localStorage.setItem(CHAVE_CADASTRO, JSON.stringify(lista));
}

// ═══════ ARQUIVO LOCAL DE RESULTADOS (sobrevive a reinício do servidor) ═══════
// Guarda direto a lista já pronta que vem do /ranking_geral (uma linha por
// pássaro da Final, com o tempo da Eliminatória e o tempo da Final juntos).
function carregarResultadosLocais() {
  try { return JSON.parse(localStorage.getItem(CHAVE_RESULTADOS)) || []; }
  catch (e) { return []; }
}
function salvarResultadosLocais(lista) {
  localStorage.setItem(CHAVE_RESULTADOS, JSON.stringify(lista));
}

function marcarOffline(offline) {
  document.getElementById('avisoOffline').classList.toggle('mostrar', offline);
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
  if (nome === 'geral') { renderGeral(); }
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
    tbody.innerHTML = '<tr><td class="vazio" colspan="4">nenhum pássaro cadastrado ainda</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  lista.forEach((p, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${p.nome}</td><td>${p.anilha || ''}</td><td>${p.proprietario || ''}</td>
      <td style="text-align:right; white-space:nowrap;">
      <button class="btn-cinza" style="padding:4px 8px; font-size:11px;" onclick="editarPassaroCadastro(${i})">✏️</button>
      <button class="btn-vermelho" style="padding:4px 8px; font-size:11px;" onclick="removerPassaroCadastro(${i})">🗑️</button>
    </td>`;
    tbody.appendChild(tr);
  });
}

function adicionarPassaroCadastro() {
  const nomeEl = document.getElementById('nomeNovoPassaro');
  const anilhaEl = document.getElementById('anilhaNovoPassaro');
  const propEl = document.getElementById('proprietarioNovoPassaro');
  const nome = nomeEl.value.trim();
  if (!nome) return;
  const lista = carregarCadastro();
  lista.push({ nome, anilha: anilhaEl.value.trim(), proprietario: propEl.value.trim() });
  salvarCadastro(lista);
  nomeEl.value = ''; anilhaEl.value = ''; propEl.value = '';
  renderCadastro();
}

function editarPassaroCadastro(i) {
  const lista = carregarCadastro();
  const p = lista[i];
  const novoNome = prompt('Nome:', p.nome);
  if (novoNome === null) return;
  const novaAnilha = prompt('Anilha:', p.anilha || '');
  if (novaAnilha === null) return;
  const novoProp = prompt('Proprietário:', p.proprietario || '');
  if (novoProp === null) return;
  lista[i] = { nome: novoNome.trim(), anilha: novaAnilha.trim(), proprietario: novoProp.trim() };
  salvarCadastro(lista);
  renderCadastro();
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
  try {
    const resp = await fetch(`/api/sala/${codigo}/cadastrar_prova`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ tipo: origemCadastro, passaros: lista })
    });
    const data = await resp.json();
    if (data.ok) {
      marcarOffline(false);
      alert(`${lista.length} pássaro(s) adicionados à ${origemCadastro === 'eliminatorias' ? 'Eliminatória' : 'Final'}.`);
      mostrarTela(origemCadastro);
    } else {
      alert('Erro: ' + (data.erro || 'desconhecido'));
    }
  } catch (e) {
    marcarOffline(true);
    alert('Sem conexão com o servidor da sala agora. Tenta de novo em alguns segundos.');
  }
}

// ═══════ PROVA (Eliminatória / Final) ═══════
function formatarMMSS(segundos) {
  segundos = Math.max(0, Math.round(segundos));
  const mm = String(Math.floor(segundos / 60)).padStart(2, '0');
  const ss = String(segundos % 60).padStart(2, '0');
  return `${mm}:${ss}`;
}

async function atualizarProva() {
  if (telaAtual !== 'eliminatorias' && telaAtual !== 'final') return;
  try {
    const resp = await fetch(`/api/sala/${codigo}/prova/${tipoProvaAtual}`);
    const data = await resp.json();
    if (!data.ok) { marcarOffline(true); return; }
    marcarOffline(false);

    const status = data.finalizada ? 'finalizada' : (data.ativa ? 'em andamento' : 'aguardando início');
    document.getElementById('statusProva').textContent = `${data.itens.length} pássaro(s) — prova ${status}`;

    const blocoRelogio = document.getElementById('blocoRelogio');
    if (data.ativa) {
      blocoRelogio.style.display = 'block';
      blocoRelogio.classList.toggle('acabando', data.tempo_restante <= 30);
      document.getElementById('relogioNumero').textContent = formatarMMSS(data.tempo_restante);
      document.getElementById('relogioLegenda').textContent = 'tempo restante da prova (finaliza sozinho)';
    } else if (data.finalizada) {
      blocoRelogio.style.display = 'block';
      blocoRelogio.classList.remove('acabando');
      document.getElementById('relogioNumero').textContent = '00:00';
      document.getElementById('relogioLegenda').textContent = 'prova finalizada';
    } else {
      blocoRelogio.style.display = 'none';
    }

    const tbody = document.querySelector('#tabelaProva tbody');
    if (data.itens.length === 0) {
      tbody.innerHTML = '<tr><td class="vazio" colspan="5">nenhum pássaro nesta prova ainda</td></tr>';
    } else {
      tbody.innerHTML = '';
      data.itens.forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${item.nome}</td><td>${item.anilha || ''}</td><td>${item.proprietario || ''}</td>
          <td><span class="tag ${item.vinculado ? 'tag-ok' : 'tag-nao'}">${item.vinculado ? 'sim' : 'não'}</span></td>
          <td style="font-family:monospace">${item.tempo_texto}</td>`;
        tbody.appendChild(tr);
      });
    }
  } catch (e) {
    marcarOffline(true);
  }
}

async function acaoProva(acao) {
  if (acao === 'limpar' && !confirm('Tem certeza? Isso apaga os tempos e desvincula os celulares dessa prova.')) return;
  if (acao === 'finalizar' && !confirm('Finalizar esta prova? Os celulares vinculados serão travados.')) return;
  try {
    const resp = await fetch(`/api/sala/${codigo}/prova/${tipoProvaAtual}/${acao}`, { method: 'POST' });
    const data = await resp.json();
    if (!data.ok) alert('Erro: ' + (data.erro || 'desconhecido'));
    marcarOffline(false);
  } catch (e) {
    marcarOffline(true);
    alert('Sem conexão com o servidor da sala agora.');
  }
  atualizarProva();
}

async function classificarParaFinal() {
  const padrao = 15;
  const qtdStr = prompt('Quantos pássaros classificam para a Final?', padrao);
  if (qtdStr === null) return;
  const qtd = parseInt(qtdStr, 10);
  if (isNaN(qtd) || qtd < 0) { alert('Número inválido.'); return; }
  try {
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
  } catch (e) {
    marcarOffline(true);
    alert('Sem conexão com o servidor da sala agora.');
  }
}

// ═══════ RESULTADO GERAL (lê do arquivo local — sobrevive a reinício do
// servidor). Uma linha por pássaro da Final; a colocação é sempre pelo
// tempo da Final — a Eliminatória só aparece como referência ao lado. ═══════
function renderGeral() {
  const lista = carregarResultadosLocais();
  const tbody = document.querySelector('#tabelaGeral tbody');
  if (lista.length === 0) {
    tbody.innerHTML = '<tr><td class="vazio" colspan="6">ainda sem pássaros na Final</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  lista.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${r.posicao}º</td><td>${r.nome}</td><td>${r.anilha || ''}</td><td>${r.proprietario || ''}</td>
      <td style="font-family:monospace">${r.tempo_eliminatoria_texto}</td>
      <td style="font-family:monospace">${r.tempo_final_texto}</td>`;
    tbody.appendChild(tr);
  });
}

// ═══════ GERAR IMAGEM DO RESULTADO (leve, fácil de compartilhar no
// WhatsApp/etc.) — desenhada direto no navegador com <canvas>, não
// depende do servidor pra nada, então funciona até se a sala tiver
// caído: usa os dados que já estão salvos aqui no navegador. ═══════
function gerarImagemResultado() {
  const lista = carregarResultadosLocais();
  if (lista.length === 0) { alert('Ainda não tem resultado de Final pra gerar imagem.'); return; }

  const linhaAltura = 34;
  const cabecalhoAltura = 108;
  const rodapeAltura = 30;
  const largura = 720;
  const altura = cabecalhoAltura + (lista.length + 1) * linhaAltura + rodapeAltura;

  const canvas = document.getElementById('canvasResultado');
  canvas.width = largura;
  canvas.height = altura;
  const ctx = canvas.getContext('2d');

  // fundo
  ctx.fillStyle = '#0B1629';
  ctx.fillRect(0, 0, largura, altura);

  // cabeçalho
  ctx.fillStyle = '#F0C030';
  ctx.font = 'bold 24px Arial';
  ctx.fillText('🐦 Marcador Digital Goularth', 20, 38);
  ctx.fillStyle = '#93a4c3';
  ctx.font = '13px Arial';
  ctx.fillText('Resultado Geral — sala ' + codigo, 20, 60);
  ctx.fillText('Colocação pelo tempo da Final', 20, 78);

  // colunas
  const colunas = [
    { titulo: '#',            x: 20,  largura: 34 },
    { titulo: 'Pássaro',      x: 56,  largura: 150 },
    { titulo: 'Anilha',       x: 210, largura: 90 },
    { titulo: 'Proprietário', x: 304, largura: 150 },
    { titulo: 'Eliminatória', x: 458, largura: 110 },
    { titulo: 'Final',        x: 572, largura: 120 },
  ];

  let y = cabecalhoAltura;

  // cabeçalho da tabela
  ctx.fillStyle = '#16213d';
  ctx.fillRect(16, y - 22, largura - 32, linhaAltura);
  ctx.fillStyle = '#F0C030';
  ctx.font = 'bold 13px Arial';
  colunas.forEach(c => ctx.fillText(c.titulo, c.x, y));
  y += linhaAltura;

  ctx.font = '13px Arial';
  lista.forEach((r, i) => {
    if (i % 2 === 0) {
      ctx.fillStyle = 'rgba(255,255,255,0.03)';
      ctx.fillRect(16, y - 22, largura - 32, linhaAltura);
    }
    ctx.fillStyle = r.posicao <= 3 ? '#F0C030' : '#EAF1FF';
    ctx.fillText(r.posicao + 'º', colunas[0].x, y);
    ctx.fillStyle = '#EAF1FF';
    ctx.fillText(truncarTexto(ctx, r.nome, colunas[1].largura), colunas[1].x, y);
    ctx.fillText(truncarTexto(ctx, r.anilha || '-', colunas[2].largura), colunas[2].x, y);
    ctx.fillText(truncarTexto(ctx, r.proprietario || '-', colunas[3].largura), colunas[3].x, y);
    ctx.fillStyle = '#93a4c3';
    ctx.fillText(r.tempo_eliminatoria_texto, colunas[4].x, y);
    ctx.fillStyle = '#3ddc84';
    ctx.font = 'bold 13px Arial';
    ctx.fillText(r.tempo_final_texto, colunas[5].x, y);
    ctx.font = '13px Arial';
    y += linhaAltura;
  });

  ctx.fillStyle = '#5a6d94';
  ctx.font = '11px Arial';
  ctx.fillText('gerado em ' + new Date().toLocaleString('pt-BR'), 20, altura - 10);

  canvas.toBlob(async (blob) => {
    const arquivo = new File([blob], `resultado_${codigo}.png`, { type: 'image/png' });
    if (navigator.share && navigator.canShare && navigator.canShare({ files: [arquivo] })) {
      try {
        await navigator.share({ files: [arquivo], title: 'Resultado Geral - ' + codigo });
        return;
      } catch (e) { /* usuário cancelou o compartilhamento, cai pro download */ }
    }
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `resultado_${codigo}.png`;
    link.click();
  }, 'image/png');
}

function truncarTexto(ctx, texto, larguraMax) {
  texto = texto || '';
  while (ctx.measureText(texto).width > larguraMax - 10 && texto.length > 0) {
    texto = texto.slice(0, -1);
  }
  return texto;
}

// ═══════ SINCRONIZAÇÃO DE FUNDO (mantém o arquivo local sempre atualizado,
// mesmo que o organizador esteja numa aba diferente) ═══════
async function sincronizarFundo() {
  try {
    const resp = await fetch(`/api/sala/${codigo}/ranking_geral`);
    const data = await resp.json();
    if (data.ok) salvarResultadosLocais(data.ranking);
  } catch (e) { /* servidor fora do ar — o que já tem continua salvo */ }
  if (telaAtual === 'geral') renderGeral();
}

setInterval(() => {
  if (telaAtual === 'eliminatorias' || telaAtual === 'final') atualizarProva();
}, 1000);
setInterval(sincronizarFundo, 2000);

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

<!-- vídeo mudo de 1x1, só pra impedir a tela de apagar em navegadores que não
     têm a Screen Wake Lock API (recebe uma "gravação" ao vivo de um canvas
     parado, gerada na hora — não precisa de nenhum arquivo de vídeo) -->
<video id="videoNoSleep" muted playsinline loop
       style="position:fixed; top:0; left:0; width:1px; height:1px; opacity:0.01; pointer-events:none;"></video>

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
  ativarProtecaoDeTela();
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

// Fallback pra navegadores que não suportam a Screen Wake Lock API (ex.: iOS
// mais antigo) ou que exigem um toque do usuário antes de liberar: mantém um
// vídeo "ao vivo" (gerado na hora por um canvas parado) tocando em loop, o
// que também impede a tela de apagar em muitos navegadores.
const videoNoSleep = document.getElementById('videoNoSleep');
try {
  const canvas = document.createElement('canvas');
  canvas.width = 2; canvas.height = 2;
  const ctx = canvas.getContext('2d');
  ctx.fillRect(0, 0, 2, 2);
  if (canvas.captureStream) {
    videoNoSleep.srcObject = canvas.captureStream(1);
  }
} catch (e) {}

function iniciarNoSleepFallback() {
  if (videoNoSleep && videoNoSleep.srcObject) {
    videoNoSleep.play().catch(() => {});
  }
}

function ativarProtecaoDeTela() {
  pedirWakeLock();
  iniciarNoSleepFallback();
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') ativarProtecaoDeTela();
});

// Pede assim que a página carrega...
ativarProtecaoDeTela();
// ...e de novo no primeiro toque na tela (alguns navegadores só liberam a
// trava de tela depois de uma interação real do usuário).
document.addEventListener('touchstart', ativarProtecaoDeTela, { once: true });
document.addEventListener('click', ativarProtecaoDeTela, { once: true });
// Reforça periodicamente, caso o navegador solte a trava sozinho.
setInterval(ativarProtecaoDeTela, 20000);

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
