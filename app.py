import os
import time
import random
import string
import threading
import base64
from io import BytesIO

from flask import Flask, request, jsonify

import db

app = Flask(__name__)

# ════════════════════════════════════════════════════════════════════
# ESTADO EFÊMERO (só isso continua em memória)
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
# API — ADMIN
# ════════════════════════════════════════════════════════════════════

@app.route('/api/admin/estatisticas')
def api_admin_estatisticas():
    """Retorna estatísticas do sistema."""
    senha = request.headers.get('X-Admin-Password', '')
    if not db.verificar_senha_admin(senha):
        return jsonify({"ok": False, "erro": "senha inválida"}), 401
    
    stats = db.obter_estatisticas()
    stats['ok'] = True
    return jsonify(stats)


@app.route('/api/admin/logs')
def api_admin_logs():
    """Retorna os logs mais recentes."""
    senha = request.headers.get('X-Admin-Password', '')
    if not db.verificar_senha_admin(senha):
        return jsonify({"ok": False, "erro": "senha inválida"}), 401
    
    limite = request.args.get('limite', 50, type=int)
    logs = db.obter_logs_recentes(limite)
    return jsonify({"ok": True, "logs": logs})


@app.route('/api/admin/imagem', methods=['GET', 'POST'])
def api_admin_imagem():
    """Obtém ou atualiza a imagem do botão."""
    if request.method == 'GET':
        imagem = db.obter_imagem_botao()
        return jsonify({"ok": True, "imagem": imagem})
    
    # POST - atualizar imagem
    senha = request.headers.get('X-Admin-Password', '')
    if not db.verificar_senha_admin(senha):
        return jsonify({"ok": False, "erro": "senha inválida"}), 401
    
    dados = request.get_json(force=True) or {}
    imagem = dados.get('imagem', '')
    db.definir_config_admin("imagem_botao", imagem)
    return jsonify({"ok": True})


@app.route('/api/admin/upload_imagem', methods=['POST'])
def api_admin_upload_imagem():
    """Faz upload de uma imagem para usar como botão."""
    senha = request.headers.get('X-Admin-Password', '')
    if not db.verificar_senha_admin(senha):
        return jsonify({"ok": False, "erro": "senha inválida"}), 401
    
    dados = request.get_json(force=True) or {}
    imagem_base64 = dados.get('imagem', '')
    
    if not imagem_base64:
        return jsonify({"ok": False, "erro": "nenhuma imagem enviada"}), 400
    
    try:
        # Remover prefixo data:image/...;base64, se existir
        if ',' in imagem_base64:
            imagem_base64 = imagem_base64.split(',')[1]
        
        # Decodificar a imagem
        imagem_bytes = base64.b64decode(imagem_base64)
        
        # Verificar se é uma imagem válida
        try:
            from PIL import Image
            img = Image.open(BytesIO(imagem_bytes))
            
            # Redimensionar para não ficar muito grande (max 300px de altura)
            if img.height > 300:
                ratio = 300 / img.height
                novo_tamanho = (int(img.width * ratio), 300)
                img = img.resize(novo_tamanho, Image.Resampling.LANCZOS)
            
            # Converter para base64 novamente
            buffer = BytesIO()
            img.save(buffer, format='PNG')
            imagem_otimizada = base64.b64encode(buffer.getvalue()).decode('utf-8')
            imagem_final = f"data:image/png;base64,{imagem_otimizada}"
        except:
            # Se não tiver PIL, salva a imagem original
            imagem_final = f"data:image/png;base64,{imagem_base64}"
        
        # Salvar no banco
        db.definir_config_admin("imagem_botao", imagem_final)
        
        return jsonify({"ok": True, "mensagem": "Imagem salva com sucesso!"})
    except Exception as e:
        return jsonify({"ok": False, "erro": f"Erro ao processar imagem: {str(e)}"}), 400


@app.route('/api/admin/senha', methods=['POST'])
def api_admin_alterar_senha():
    """Altera a senha do admin."""
    dados = request.get_json(force=True) or {}
    senha_antiga = dados.get('senha_antiga', '')
    senha_nova = dados.get('senha_nova', '')
    
    if not senha_nova or len(senha_nova) < 4:
        return jsonify({"ok": False, "erro": "senha nova deve ter pelo menos 4 caracteres"}), 400
    
    if db.alterar_senha_admin(senha_antiga, senha_nova):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "erro": "senha antiga incorreta"}), 401


# ════════════════════════════════════════════════════════════════════
# API — SALA
# ════════════════════════════════════════════════════════════════════

@app.route('/api/sala/criar', methods=['POST'])
def api_criar_sala():
    codigo = novo_codigo_sala()
    db.criar_sala(codigo)
    db.registrar_acesso(codigo, 'api', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
    return jsonify({"ok": True, "codigo": codigo})


@app.route('/api/sala/<codigo>/existe')
def api_sala_existe(codigo):
    codigo = codigo.upper()
    existe = db.sala_existe(codigo)
    if existe:
        db.registrar_acesso(codigo, 'api', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
    return jsonify({"ok": True, "existe": existe})


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
    db.registrar_acesso(codigo, 'api', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
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
    db.registrar_acesso(codigo, 'api', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
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
    db.registrar_acesso(codigo, 'api', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
    return jsonify({"ok": True})


@app.route('/api/sala/<codigo>/prova/<tipo>/finalizar', methods=['POST'])
def api_finalizar_prova(codigo, tipo):
    codigo = codigo.upper()
    if tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    vinculados = db.finalizar_prova(codigo, tipo)
    for esp32_id in vinculados:
        empurrar_comandos(esp32_id, ["FINALIZAR"])
    db.registrar_acesso(codigo, 'api', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
    return jsonify({"ok": True})


@app.route('/api/sala/<codigo>/prova/<tipo>/limpar', methods=['POST'])
def api_limpar_prova(codigo, tipo):
    codigo = codigo.upper()
    if tipo not in ('eliminatorias', 'final'):
        return jsonify({"ok": False}), 404
    vinculados = db.limpar_prova(codigo, tipo)
    for esp32_id in vinculados:
        empurrar_comandos(esp32_id, ["RESET"])
    db.registrar_acesso(codigo, 'api', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
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
    db.registrar_acesso(codigo, 'api', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
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
    db.registrar_acesso(codigo, 'api', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
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
    
    db.registrar_acesso(codigo, 'api', esp32_id=esp32_id, ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
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
    db.registrar_acesso(codigo, 'api', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
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
    
    db.registrar_acesso(codigo, 'api', esp32_id=esp32_id, ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
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
    db.registrar_acesso(codigo, 'api', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
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

        # limpa conexões efêmeras sem tick há muito tempo
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


# ════════════════════════════════════════════════════════════════════
# ESTILO BASE (compartilhado entre todas as telas)
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
input[type=text], input[type=number], input[type=password] {
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
.estatistica-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:10px; margin-top:10px; }
.estatistica-item { background:#0f1830; border-radius:8px; padding:12px; text-align:center; }
.estatistica-item .valor { font-size:28px; font-weight:bold; color:#F0C030; }
.estatistica-item .label { font-size:11px; color:#93a4c3; margin-top:4px; }
"""


# ════════════════════════════════════════════════════════════════════
# TELA HOME
# ════════════════════════════════════════════════════════════════════

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
  
  <div style="text-align:center; margin-top:20px;">
    <a href="/admin" style="color:#5a6d94; font-size:12px; text-decoration:none;">⚙️ Admin</a>
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


# ════════════════════════════════════════════════════════════════════
# TELA ORGANIZADOR (COMPLETA)
# ════════════════════════════════════════════════════════════════════

HTML_ORGANIZADOR = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Marcador Digital Goularth - Organizador</title>
<style>""" + ESTILO_BASE + """
.faixa-codigo { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; }
.relogio { text-align:center; margin:6px 0 16px; }
.relogio-numero { font-family:monospace; font-size:48px; color:#F0C030; font-weight:bold; letter-spacing:2px; }
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

  <!-- ═══════ ELIMINATÓRIA / FINAL ═══════ -->
  <div id="telaProva" class="card" style="display:none">
    <h2 style="margin-top:0" id="tituloProva">Eliminatória</h2>

    <div class="relogio" id="blocoRelogio" style="display:none">
      <div class="relogio-numero" id="relogioNumero">00:00:000</div>
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

// ═══════ FORMATAR TEMPO COM MILÉSIMOS ═══════
function formatarMMSSmmm(segundos) {
  segundos = Math.max(0, segundos);
  const mm = String(Math.floor(segundos / 60)).padStart(2, '0');
  const ss = String(Math.floor(segundos % 60)).padStart(2, '0');
  const mmm = String(Math.floor((segundos - Math.floor(segundos)) * 1000)).padStart(3, '0');
  return `${mm}:${ss}:${mmm}`;
}

// ═══════ NAVEGAÇÃO ═══════
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

// ═══════ PROVA ═══════
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
      document.getElementById('relogioNumero').textContent = formatarMMSSmmm(data.tempo_restante);
      document.getElementById('relogioLegenda').textContent = 'tempo restante da prova (finaliza sozinho)';
    } else if (data.finalizada) {
      blocoRelogio.style.display = 'block';
      blocoRelogio.classList.remove('acabando');
      document.getElementById('relogioNumero').textContent = '00:00:000';
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

// ═══════ RESULTADO GERAL ═══════
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

// ═══════ GERAR IMAGEM ═══════
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

  ctx.fillStyle = '#0B1629';
  ctx.fillRect(0, 0, largura, altura);

  ctx.fillStyle = '#F0C030';
  ctx.font = 'bold 24px Arial';
  ctx.fillText('🐦 Marcador Digital Goularth', 20, 38);
  ctx.fillStyle = '#93a4c3';
  ctx.font = '13px Arial';
  ctx.fillText('Resultado Geral — sala ' + codigo, 20, 60);
  ctx.fillText('Colocação pelo tempo da Final', 20, 78);

  const colunas = [
    { titulo: '#',            x: 20,  largura: 34 },
    { titulo: 'Pássaro',      x: 56,  largura: 150 },
    { titulo: 'Anilha',       x: 210, largura: 90 },
    { titulo: 'Proprietário', x: 304, largura: 150 },
    { titulo: 'Eliminatória', x: 458, largura: 110 },
    { titulo: 'Final',        x: 572, largura: 120 },
  ];

  let y = cabecalhoAltura;

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
      } catch (e) { /* cancelado */ }
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

// ═══════ SINCRONIZAÇÃO ═══════
async function sincronizarFundo() {
  try {
    const resp = await fetch(`/api/sala/${codigo}/ranking_geral`);
    const data = await resp.json();
    if (data.ok) salvarResultadosLocais(data.ranking);
  } catch (e) { /* servidor fora do ar */ }
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
    codigo = codigo.upper()
    db.registrar_acesso(codigo, 'organizador', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
    return HTML_ORGANIZADOR


# ════════════════════════════════════════════════════════════════════
# TELA CELULAR
# ════════════════════════════════════════════════════════════════════

HTML_CELULAR = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, user-scalable=no">
<title>Celular como ESP32</title>
<style>""" + ESTILO_BASE + """
  body { user-select:none; background:#0a0f1f; }
  .status-bar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; margin-bottom:4px; }
  .device-id { font-size:11px; color:#5a6d94; font-family:monospace; background:#0f1830; padding:4px 10px; border-radius:6px; border:1px solid #1a2a4a; }
  .cat-titulo { color:#F0C030; font-weight:bold; font-size:14px; margin:14px 0 8px; }
  .bolinha { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; background:#ef4444; transition:0.3s; }
  .bolinha.ok { background:#10b981; box-shadow:0 0 10px #10b98166; }
  .lcd { background:linear-gradient(145deg, #0a0a1a, #1a1a2e); border-radius:16px; padding:20px 16px; 
         margin-bottom:22px; border:2px solid #2a3a63; box-shadow:inset 0 0 30px rgba(0,0,0,0.8), 0 4px 20px rgba(0,0,0,0.4); }
  .lcd-linha0 { color:#F0C030; font-family:monospace; font-size:18px; text-align:center;
                white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-height:24px; text-shadow:0 0 10px rgba(240,192,48,0.2); }
  .lcd-linha1 { color:#3ddc84; font-family:monospace; font-size:38px; text-align:center; 
                margin-top:4px; letter-spacing:2px; text-shadow:0 0 20px #3ddc8444; font-weight:bold; }
  .botao-canto { 
    width:100%; padding:16px; border-radius:24px; border:none; 
    font-weight:bold; font-size:22px; color:white; background:#1558B0; 
    box-shadow:0 6px 0 #0d3a7c, 0 8px 20px rgba(0,0,0,0.5); 
    touch-action:none; overflow:hidden; position:relative; transition:all 0.05s;
    cursor:pointer; text-align:center;
  }
  .botao-canto.pressionado { background:#177A38; box-shadow:0 2px 0 #0d3a7c; transform:scale(0.97); }
  .botao-canto:disabled { background:#374158; box-shadow:0 6px 0 #232a3a; color:#7c88a6; cursor:default; opacity:0.6; }
  .botao-canto img { max-width:100%; max-height:120px; border-radius:12px; display:block; margin:0 auto; }
  .passaro-card { 
    padding:16px; margin:6px 0; background:#16213d; border-radius:10px; 
    cursor:pointer; transition:all 0.2s; border-left:3px solid #C9980E;
    font-size:15px; font-weight:500;
  }
  .passaro-card:active { transform:scale(0.97); background:#1a2a5a; }
  .passaro-card .sub-info { font-size:11px; color:#93a4c3; font-weight:normal; }
  .vinculado-atual { 
    background:linear-gradient(145deg, #0f1830, #16213d); border-radius:10px; padding:10px 14px; margin:8px 0 14px;
    border:1px solid #2a3a63; color:#93a4c3; font-size:13px; text-align:center;
  }
  .vinculado-atual strong { color:#F0C030; }
  .btn-trocar { background:#6025A8; color:white; padding:12px; border-radius:10px; border:none; font-weight:bold; width:100%; margin-top:8px; cursor:pointer; }
  .btn-trocar:active { transform:scale(0.97); }
</style>
</head>
<body>
<div class="wrap">
  <div class="status-bar">
    <div>
      <h1 style="margin-bottom:2px">📟 ESP32 Virtual</h1>
      <div class="sub" style="margin-bottom:0;">sala <span class="codigo-sala" id="codigoTopo" style="font-size:16px;">------</span></div>
    </div>
    <span class="device-id" id="deviceIdDisplay">---</span>
  </div>
  <div class="sub" style="margin-top:2px;">
    <span class="bolinha" id="bolinhaStatus"></span><span id="textoStatus">conectando...</span>
  </div>

  <div id="telaVincular">
    <div class="cat-titulo">🔵 ELIMINATÓRIA</div>
    <div id="listaElim"><div class="vazio">carregando...</div></div>
    <div class="cat-titulo">🔴 FINAL</div>
    <div id="listaFinal"><div class="vazio">carregando...</div></div>
  </div>

  <div id="telaMarcador" style="display:none;">
    <div class="vinculado-atual">
      📌 Vinculado: <strong id="nomeVinculado">-</strong>
    </div>
    <div class="lcd">
      <div class="lcd-linha0" id="lcdLinha0">-</div>
      <div class="lcd-linha1" id="lcdLinha1">00:00:000</div>
    </div>
    <button class="botao-canto" id="botaoCanto">
      <span id="textoBotao">AGUARDANDO</span>
    </button>
    <button class="btn-trocar" onclick="trocarPassaro()">🔄 Trocar pássaro vinculado</button>
  </div>
</div>

<video id="videoNoSleep" muted playsinline loop
       style="position:fixed; top:0; left:0; width:1px; height:1px; opacity:0.01; pointer-events:none;"></video>

<script>
const codigo = location.pathname.split('/').pop().toUpperCase();
document.getElementById('codigoTopo').textContent = codigo;

let deviceId = localStorage.getItem('celular_esp32_id');
if (!deviceId) {
  deviceId = 'CEL-' + Math.random().toString(36).slice(2,8).toUpperCase();
  localStorage.setItem('celular_esp32_id', deviceId);
}
document.getElementById('deviceIdDisplay').textContent = deviceId;

let passaroAtual = null;
let conectado = false;
let faseAtual = 0;
let botaoBloqueado = true;
let provaIniciada = false;
let isRunning = false;
let startTime = 0;
let totalTime = 0;
let nomePassaroAtual = '';
let syncAtiva = false;
let syncTempoRestanteMs = 0;
let syncRecebidoEm = 0;
let imagemBotao = '';

function formatarTempo(msTotal) {
  if (msTotal < 0) msTotal = 0;
  const ms = Math.floor(msTotal % 1000);
  const totalSeg = Math.floor(msTotal / 1000);
  const seg = totalSeg % 60;
  const min = Math.floor(totalSeg / 60);
  return `${String(min).padStart(2,'0')}:${String(seg).padStart(2,'0')}:${String(ms).padStart(3,'0')}`;
}

function atualizarStatusConexao() {
  document.getElementById('bolinhaStatus').classList.toggle('ok', conectado);
  document.getElementById('textoStatus').textContent = conectado
    ? 'conectado' : 'sem conexão — tentando...';
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
    document.getElementById('nomeVinculado').textContent = nomePassaroAtual;
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
    document.getElementById('lcdLinha0').textContent = '🏁 PROVA FINALIZADA';
    document.getElementById('lcdLinha1').textContent = 'FINALIZADA';
  }
  atualizarBotao();
}

function parseTempoParaMs(str) {
  const partes = str.split(':');
  if (partes.length !== 3) return 0;
  return (parseInt(partes[0])||0) * 60000 + (parseInt(partes[1])||0) * 1000 + (parseInt(partes[2])||0);
}

function atualizarBotao() {
  const btn = document.getElementById('botaoCanto');
  const texto = document.getElementById('textoBotao');
  btn.disabled = botaoBloqueado;
  
  if (faseAtual === 2) {
    texto.textContent = '🏁 FINALIZADA';
  } else if (botaoBloqueado) {
    texto.textContent = '⏳ AGUARDANDO';
  } else {
    texto.textContent = '🔴 SEGURE PARA CANTAR';
  }
}

// Carregar imagem do botão
async function carregarImagemBotao() {
  try {
    const resp = await fetch('/api/admin/imagem');
    const data = await resp.json();
    if (data.ok && data.imagem) {
      imagemBotao = data.imagem;
      const btn = document.getElementById('botaoCanto');
      const texto = document.getElementById('textoBotao');
      if (imagemBotao) {
        btn.innerHTML = `<img src="${imagemBotao}" alt="Botão">`;
      } else {
        btn.innerHTML = '<span id="textoBotao">AGUARDANDO</span>';
      }
      atualizarBotao();
    }
  } catch (e) {}
}
carregarImagemBotao();

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
let ultimoTick = 0;

setInterval(() => {
  let displayTime = totalTime;
  if (isRunning) displayTime += Date.now() - startTime;
  const bufLocal = formatarTempo(displayTime);
  document.getElementById('lcdLinha1').textContent = bufLocal;

  if (faseAtual === 1 && syncAtiva) {
    const decorrido = Date.now() - syncRecebidoEm;
    let restante = syncTempoRestanteMs - decorrido;
    if (restante < 0) restante = 0;
    document.getElementById('lcdLinha0').textContent = '⏱️ ' + formatarTempo(restante);
  }

  const agora = Date.now();
  if (!tickEmAndamento && (agora - ultimoTick > 200)) {
    ultimoTick = agora;
    tickEmAndamento = true;
    enviarTick(provaIniciada ? bufLocal : '').finally(() => { tickEmAndamento = false; });
  }
}, 50);

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
    const div = document.createElement('div');
    div.className = 'passaro-card';
    div.innerHTML = `${p.nome} <span class="sub-info">(clique para vincular)</span>`;
    div.onclick = () => vincularPassaro(tipo, p.id);
    el.appendChild(div);
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

ativarProtecaoDeTela();
document.addEventListener('touchstart', ativarProtecaoDeTela, { once: true });
document.addEventListener('click', ativarProtecaoDeTela, { once: true });
setInterval(ativarProtecaoDeTela, 20000);

carregarPassaros();
setInterval(carregarPassaros, 5000);
</script>
</body>
</html>"""


@app.route('/celular/<codigo>')
def tela_celular(codigo):
    codigo = codigo.upper()
    db.registrar_acesso(codigo, 'celular', ip=request.remote_addr, user_agent=request.headers.get('User-Agent'))
    return HTML_CELULAR


# ════════════════════════════════════════════════════════════════════
# TELA ADMIN
# ════════════════════════════════════════════════════════════════════

HTML_ADMIN = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin - Marcador Digital Goularth</title>
<style>""" + ESTILO_BASE + """
.estatistica-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:10px; margin-top:10px; }
.estatistica-item { background:#0f1830; border-radius:8px; padding:14px; text-align:center; border:1px solid #1a2a4a; }
.estatistica-item .valor { font-size:32px; font-weight:bold; color:#F0C030; }
.estatistica-item .label { font-size:11px; color:#93a4c3; margin-top:4px; }
.estatistica-item .sub-valor { font-size:14px; color:#3ddc84; margin-top:2px; }
.log-tabela { font-size:12px; }
.log-tabela td { padding:4px 6px; font-size:11px; }
.log-tabela .timestamp { color:#5a6d94; white-space:nowrap; }
.preview-img { max-width:200px; max-height:150px; border-radius:8px; border:1px solid #2a3a63; }
</style>
</head>
<body>
<div class="wrap">
  <div class="faixa-codigo">
    <div>
      <h1>⚙️ Painel Admin</h1>
      <div class="sub">Estatísticas e configurações do sistema</div>
    </div>
    <a href="/" class="btn-cinza" style="text-decoration:none; padding:8px 12px; border-radius:8px;">← Voltar</a>
  </div>

  <!-- Login -->
  <div id="telaLogin" class="card">
    <h2 style="margin-top:0">🔐 Acesso Restrito</h2>
    <div class="sub">Digite a senha para acessar o painel administrativo</div>
    <input type="password" id="senhaLogin" placeholder="Senha" style="margin-bottom:8px;">
    <button class="btn-ouro" onclick="fazerLogin()" style="width:100%">Entrar</button>
    <div id="erroLogin" style="color:#B0271A; margin-top:8px; display:none;">Senha incorreta!</div>
  </div>

  <!-- Conteúdo Admin -->
  <div id="telaAdmin" style="display:none;">
    <!-- Estatísticas -->
    <div class="card">
      <h2 style="margin-top:0">📊 Estatísticas em Tempo Real</h2>
      <div class="estatistica-grid" id="gridStats">
        <div class="estatistica-item"><div class="valor" id="statSalas">-</div><div class="label">Total de Salas</div></div>
        <div class="estatistica-item"><div class="valor" id="statSalasAtivas">-</div><div class="label">Salas Ativas (24h)</div></div>
        <div class="estatistica-item"><div class="valor" id="statAcessos">-</div><div class="label">Total de Acessos</div></div>
        <div class="estatistica-item"><div class="valor" id="statAcessos24h">-</div><div class="label">Acessos (24h)</div></div>
        <div class="estatistica-item"><div class="valor" id="statDispositivos">-</div><div class="label">Dispositivos Únicos</div></div>
        <div class="estatistica-item"><div class="valor" id="statVinculados">-</div><div class="label">Vinculados Agora</div></div>
        <div class="estatistica-item"><div class="valor" id="statProvas">-</div><div class="label">Provas em Andamento</div></div>
        <div class="estatistica-item"><div class="valor" id="statPassaros">-</div><div class="label">Total de Pássaros</div></div>
      </div>
      <div class="linha-botoes">
        <button class="btn-azul" onclick="atualizarEstatisticas()" style="flex:1">🔄 Atualizar</button>
      </div>
    </div>

    <!-- Top Salas -->
    <div class="card">
      <h2 style="margin-top:0">🏆 Salas Mais Acessadas</h2>
      <table id="tabelaTopSalas">
        <thead><tr><th>Código</th><th>Acessos</th><th>Último Uso</th></tr></thead>
        <tbody><tr><td class="vazio" colspan="3">carregando...</td></tr></tbody>
      </table>
    </div>

    <!-- Configurações -->
    <div class="card">
      <h2 style="margin-top:0">⚙️ Configurações</h2>
      
      <div style="margin-bottom:16px;">
        <label style="display:block; color:#93a4c3; font-size:12px; margin-bottom:4px;">Imagem do Botão</label>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px;">
          <input type="file" id="inputFileImagem" accept="image/*" style="display:none;">
          <button class="btn-azul" onclick="document.getElementById('inputFileImagem').click()">📁 Escolher imagem da galeria</button>
          <button class="btn-verde" onclick="enviarImagem()">📤 Enviar imagem</button>
          <button class="btn-vermelho" onclick="removerImagem()">🗑️ Remover imagem</button>
        </div>
        <div id="previewImagem" style="margin-top:8px; display:none;">
          <img id="previewImg" class="preview-img">
        </div>
        <div id="msgUpload" style="margin-top:6px; font-size:13px;"></div>
      </div>

      <div style="border-top:1px solid #1a2a4a; padding-top:16px;">
        <label style="display:block; color:#93a4c3; font-size:12px; margin-bottom:4px;">Alterar Senha Admin</label>
        <input type="password" id="senhaAntiga" placeholder="Senha atual">
        <input type="password" id="senhaNova" placeholder="Nova senha (mínimo 4 caracteres)">
        <div class="linha-botoes">
          <button class="btn-ouro" onclick="alterarSenha()">🔑 Alterar Senha</button>
        </div>
        <div id="msgSenha" style="margin-top:6px; font-size:13px;"></div>
      </div>
    </div>

    <!-- Logs Recentes -->
    <div class="card">
      <h2 style="margin-top:0">📋 Logs Recentes</h2>
      <div class="sub">Últimos 50 acessos</div>
      <table class="log-tabela" id="tabelaLogs">
        <thead><tr><th>Data/Hora</th><th>Sala</th><th>Tipo</th><th>Dispositivo</th></tr></thead>
        <tbody><tr><td class="vazio" colspan="4">carregando...</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<script>
let token = '';

function fazerLogin() {
  const senha = document.getElementById('senhaLogin').value;
  if (!senha) return;
  
  fetch('/api/admin/estatisticas', {
    headers: { 'X-Admin-Password': senha }
  })
  .then(r => r.json())
  .then(data => {
    if (data.ok) {
      token = senha;
      localStorage.setItem('admin_token', senha);
      document.getElementById('telaLogin').style.display = 'none';
      document.getElementById('telaAdmin').style.display = 'block';
      document.getElementById('erroLogin').style.display = 'none';
      carregarTudo();
    } else {
      document.getElementById('erroLogin').style.display = 'block';
    }
  })
  .catch(() => {
    document.getElementById('erroLogin').style.display = 'block';
  });
}

function getHeaders() {
  return { 'X-Admin-Password': token };
}

async function carregarTudo() {
  await atualizarEstatisticas();
  await carregarLogs();
  await carregarImagemBotao();
}

async function atualizarEstatisticas() {
  try {
    const resp = await fetch('/api/admin/estatisticas', { headers: getHeaders() });
    const data = await resp.json();
    if (!data.ok) return;
    
    document.getElementById('statSalas').textContent = data.total_salas;
    document.getElementById('statSalasAtivas').textContent = data.salas_ativas_24h;
    document.getElementById('statAcessos').textContent = data.total_acessos;
    document.getElementById('statAcessos24h').textContent = data.acessos_24h;
    document.getElementById('statDispositivos').textContent = data.dispositivos_unicos_24h;
    document.getElementById('statVinculados').textContent = data.vinculados_agora;
    document.getElementById('statProvas').textContent = data.provas_ativas;
    document.getElementById('statPassaros').textContent = data.total_passaros;
    
    const tbody = document.querySelector('#tabelaTopSalas tbody');
    if (data.salas_top && data.salas_top.length > 0) {
      tbody.innerHTML = '';
      data.salas_top.forEach(s => {
        const tr = document.createElement('tr');
        const ultimo = new Date(s.ultimo_uso).toLocaleString('pt-BR');
        tr.innerHTML = `<td><strong style="color:#F0C030;">${s.codigo}</strong></td><td>${s.acessos_total}</td><td>${ultimo}</td>`;
        tbody.appendChild(tr);
      });
    } else {
      tbody.innerHTML = '<tr><td class="vazio" colspan="3">nenhuma sala com acessos</td></tr>';
    }
  } catch (e) {
    console.error('Erro ao carregar estatísticas:', e);
  }
}

async function carregarLogs() {
  try {
    const resp = await fetch('/api/admin/logs', { headers: getHeaders() });
    const data = await resp.json();
    if (!data.ok) return;
    
    const tbody = document.querySelector('#tabelaLogs tbody');
    if (data.logs && data.logs.length > 0) {
      tbody.innerHTML = '';
      data.logs.forEach(log => {
        const tr = document.createElement('tr');
        const dataHora = new Date(log.data_hora).toLocaleString('pt-BR');
        tr.innerHTML = `<td class="timestamp">${dataHora}</td>
          <td><span style="color:#F0C030;">${log.sala_codigo || '-'}</span></td>
          <td><span class="tag ${log.tipo_acesso === 'organizador' ? 'tag-ok' : log.tipo_acesso === 'celular' ? 'tag-ok' : 'tag-nao'}">${log.tipo_acesso}</span></td>
          <td style="font-size:10px; font-family:monospace;">${log.esp32_id || log.ip || '-'}</td>`;
        tbody.appendChild(tr);
      });
    } else {
      tbody.innerHTML = '<tr><td class="vazio" colspan="4">nenhum log registrado</td></tr>';
    }
  } catch (e) {
    console.error('Erro ao carregar logs:', e);
  }
}

async function carregarImagemBotao() {
  try {
    const resp = await fetch('/api/admin/imagem', { headers: getHeaders() });
    const data = await resp.json();
    if (data.ok && data.imagem) {
      document.getElementById('previewImg').src = data.imagem;
      document.getElementById('previewImagem').style.display = 'block';
    } else {
      document.getElementById('previewImagem').style.display = 'none';
    }
  } catch (e) {}
}

document.getElementById('inputFileImagem').addEventListener('change', function(e) {
  const file = e.target.files[0];
  if (file) {
    const reader = new FileReader();
    reader.onload = function(event) {
      document.getElementById('previewImg').src = event.target.result;
      document.getElementById('previewImagem').style.display = 'block';
      document.getElementById('msgUpload').textContent = '✅ Imagem selecionada. Clique em "Enviar imagem" para salvar.';
      document.getElementById('msgUpload').style.color = '#3ddc84';
    };
    reader.readAsDataURL(file);
  }
});

async function enviarImagem() {
  const img = document.getElementById('previewImg');
  if (!img.src || img.src === '') {
    document.getElementById('msgUpload').textContent = '❌ Selecione uma imagem primeiro!';
    document.getElementById('msgUpload').style.color = '#B0271A';
    return;
  }
  
  try {
    const resp = await fetch('/api/admin/upload_imagem', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...getHeaders() },
      body: JSON.stringify({ imagem: img.src })
    });
    const data = await resp.json();
    if (data.ok) {
      document.getElementById('msgUpload').textContent = '✅ ' + data.mensagem;
      document.getElementById('msgUpload').style.color = '#3ddc84';
    } else {
      document.getElementById('msgUpload').textContent = '❌ ' + (data.erro || 'Erro ao salvar imagem');
      document.getElementById('msgUpload').style.color = '#B0271A';
    }
  } catch (e) {
    document.getElementById('msgUpload').textContent = '❌ Erro de rede.';
    document.getElementById('msgUpload').style.color = '#B0271A';
  }
}

async function removerImagem() {
  if (!confirm('Remover a imagem do botão?')) return;
  try {
    const resp = await fetch('/api/admin/imagem', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...getHeaders() },
      body: JSON.stringify({ imagem: '' })
    });
    const data = await resp.json();
    if (data.ok) {
      document.getElementById('previewImagem').style.display = 'none';
      document.getElementById('inputFileImagem').value = '';
      document.getElementById('msgUpload').textContent = '✅ Imagem removida!';
      document.getElementById('msgUpload').style.color = '#3ddc84';
    } else {
      document.getElementById('msgUpload').textContent = '❌ ' + (data.erro || 'Erro ao remover imagem');
      document.getElementById('msgUpload').style.color = '#B0271A';
    }
  } catch (e) {
    document.getElementById('msgUpload').textContent = '❌ Erro de rede.';
    document.getElementById('msgUpload').style.color = '#B0271A';
  }
}

async function alterarSenha() {
  const senhaAntiga = document.getElementById('senhaAntiga').value;
  const senhaNova = document.getElementById('senhaNova').value;
  const msgEl = document.getElementById('msgSenha');
  
  if (!senhaNova || senhaNova.length < 4) {
    msgEl.style.color = '#B0271A';
    msgEl.textContent = '❌ A nova senha deve ter pelo menos 4 caracteres.';
    return;
  }
  
  try {
    const resp = await fetch('/api/admin/senha', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...getHeaders() },
      body: JSON.stringify({ senha_antiga: senhaAntiga, senha_nova: senhaNova })
    });
    const data = await resp.json();
    if (data.ok) {
      msgEl.style.color = '#3ddc84';
      msgEl.textContent = '✅ Senha alterada com sucesso!';
      token = senhaNova;
      localStorage.setItem('admin_token', senhaNova);
      document.getElementById('senhaAntiga').value = '';
      document.getElementById('senhaNova').value = '';
    } else {
      msgEl.style.color = '#B0271A';
      msgEl.textContent = '❌ ' + (data.erro || 'Erro ao alterar senha.');
    }
  } catch (e) {
    msgEl.style.color = '#B0271A';
    msgEl.textContent = '❌ Erro de rede.';
  }
}

setInterval(() => {
  if (token) {
    atualizarEstatisticas();
  }
}, 10000);

if (localStorage.getItem('admin_token')) {
  document.getElementById('senhaLogin').value = localStorage.getItem('admin_token');
  fazerLogin();
}
</script>
</body>
</html>"""


@app.route('/admin')
def tela_admin():
    return HTML_ADMIN


# ════════════════════════════════════════════════════════════════════
# INICIALIZAÇÃO E EXECUÇÃO
# ════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    porta = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=porta, debug=False)