from flask import Flask, jsonify, request, render_template, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_bcrypt import Bcrypt
import sqlite3
import requests
import json
import logging
from datetime import datetime, timedelta
import os
import tempfile
import re
import html as ihtml
from scraper_camara import obter_itens_pauta  # Importar o scraper

# --------------------------------------------------------------------------
# CONFIGURAÇÕES DE LOGGING
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler()  # Garante que os logs apareçam no console
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sua-chave-secreta-aqui'
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'usuarios.login'  # usa o blueprint externo

# 🔹 Importa e registra o módulo de usuários (Blueprint)
from usuarios import usuarios_bp, Usuario, buscar_usuario_por_id
app.register_blueprint(usuarios_bp)

@login_manager.user_loader
def load_user(user_id):
    return buscar_usuario_por_id(user_id)


# Cache em memória
pauta_cache = {}
CACHE_DURATION = timedelta(minutes=5)

# --------------------------------------------------------------------------
# BANCO DE DADOS
# --------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS notas (
        item_key TEXT PRIMARY KEY,
        evento_id INTEGER,
        ordem TEXT,
        resumo_materia TEXT,
        orientacao TEXT,
        resumo_parecer TEXT
    )''')
    conn.commit()
    users = [
        ('admin', bcrypt.generate_password_hash('123').decode('utf-8'), 'Admin'),
        ('assessor_plenario', bcrypt.generate_password_hash('123').decode('utf-8'), 'Assessor Plenário'),
        ('assessor', bcrypt.generate_password_hash('123').decode('utf-8'), 'Assessor')
    ]
    for user in users:
        try:
            c.execute('INSERT INTO users (username, password, role) VALUES (?, ?, ?)', user)
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()

def init_pauta_cache_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS pauta_cache_db (
                    evento_id INTEGER PRIMARY KEY,
                    json_pauta TEXT,
                    last_updated TEXT
                )''')
    conn.commit()
    try:
        c.execute("SELECT last_updated FROM pauta_cache_db WHERE 1=0")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE pauta_cache_db ADD COLUMN last_updated TEXT")
        logger.info("Coluna last_updated adicionada à tabela pauta_cache_db")
    conn.commit()
    conn.close()

def load_notas():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    try:
        c.execute('SELECT item_key, resumo_materia, orientacao, resumo_parecer FROM notas')
        notas = {
            row[0]: {'resumo_materia': row[1] or '', 'orientacao': row[2] or '', 'resumo_parecer': row[3] or ''}
            for row in c.fetchall()
        }
    except Exception as e:
        logger.warning(f"Erro ao carregar notas: {e}")
        init_db()
        notas = {}
    finally:
        conn.close()
    return notas

# --------------------------------------------------------------------------
# AUXILIARES
# --------------------------------------------------------------------------
def _clean_html(raw):
    if raw is None:
        return ''
    s = re.sub(r'<[^>]+>', '', raw, flags=re.S | re.I)
    s = ihtml.unescape(s)
    s = re.sub(r'\s+', ' ', s, flags=re.S).strip()
    return s

def obter_destaques(id_proposicao):
    url = f"https://www.camara.leg.br/pplen/destaques.html?codOrgao=180&codProposicao={id_proposicao}"
    destaques = []
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        html = r.text
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, flags=re.S | re.I)
        for row in rows:
            cols = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, flags=re.S | re.I)
            if len(cols) < 5:
                continue
            numero_raw = _clean_html(cols[0])
            autoria_raw = _clean_html(cols[1])
            descricao_raw = _clean_html(cols[2])
            tipo_raw = _clean_html(cols[3])
            situacao_raw = _clean_html(cols[4])
            if 'DTQ' not in numero_raw.upper():
                continue
            if situacao_raw.strip().lower() != 'em tramitação':
                continue
            destaques.append({
                'numero': numero_raw,
                'autoria': autoria_raw,
                'descricao': descricao_raw,
                'tipo_destaque': tipo_raw,
                'situacao': situacao_raw,
                'resumo_nota': ''
            })
        notas_local = load_notas()
        for d in destaques:
            d_key = f"DSTQ_{id_proposicao}_{d['numero']}"
            if d_key in notas_local:
                d['resumo_nota'] = notas_local[d_key].get('resumo_materia', '')
        return destaques
    except Exception as e:
        logger.warning(f"Falha ao obter destaques de {id_proposicao}: {e}")
        return []

def obter_autores_proposicao(id_proposicao):
    try:
        r = requests.get(f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_proposicao}/autores", timeout=10)
        r.raise_for_status()
        dados = r.json().get('dados', [])
        autores = [a.get('nome', 'Desconhecido') for a in dados[:3]]
        return {'autores': ", ".join(autores) + (" e outros" if len(dados) > 3 else ""), 'tem_mais_autores': len(dados) > 3}
    except Exception as e:
        logger.error(f"Erro ao obter autores da proposição {id_proposicao}: {e}")
        return {'autores': [], 'tem_mais_autores': False}

def obter_situacao_proposicao(id_proposicao):
    try:
        url = f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_proposicao}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        dados = r.json().get("dados", {})
        return dados.get("statusProposicao", {}).get("descricaoSituacao", "N/D")
    except Exception as e:
        logger.warning(f"Falha ao obter situação da proposição {id_proposicao}: {e}")
        return "N/D"

def fetch_eventos_por_data(data):
    url = f"https://dadosabertos.camara.leg.br/api/v2/eventos?idOrgao=180&dataInicio={data}&dataFim={data}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        dados = response.json().get('dados', [])
        logger.info(f"Eventos encontrados para a data {data}: {len(dados)}")
        return [
            {
                'id': str(e.get('id')),
                'descricao': e.get('descricao', 'Sem descrição'),
                'dataHoraInicio': e.get('dataHoraInicio', 'N/D'),
                'local': e.get('localCamara', {}).get('nome', 'N/D')
                if isinstance(e.get('localCamara'), dict)
                else e.get('localCamara', 'N/D'),
                'situacao': e.get('situacao', 'N/D')
            }
            for e in dados if e.get('descricaoTipo') == "Sessão Deliberativa"
        ]
    except Exception as e:
        logger.error(f"Erro ao acessar API de eventos: {e}")
        return []

def fetch_evento_por_id(evento_id):
    url = f"https://dadosabertos.camara.leg.br/api/v2/eventos/{evento_id}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        e = response.json().get('dados', {})
        logger.info(f"Dados do evento {evento_id} obtidos com sucesso")
        return {
            'id': str(e.get('id', evento_id)),
            'descricao': e.get('descricao', 'Sessão Deliberativa'),
            'dataHoraInicio': e.get('dataHoraInicio', 'N/D'),
            'local': e.get('localCamara', {}).get('nome', 'N/D')
                if isinstance(e.get('localCamara'), dict)
                else e.get('localCamara', 'N/D'),
            'situacao': e.get('situacao', 'N/D')
        }
    except Exception as e:
        logger.error(f"Erro ao obter dados do evento {evento_id}: {e}")
        return {
            'id': str(evento_id),
            'descricao': 'Sessão Deliberativa',
            'dataHoraInicio': 'N/D',
            'local': 'N/D',
            'situacao': 'N/D'
        }

# --------------------------------------------------------------------------
# PAUTA (com cache persistente e proteção contra sobrescrita)
# --------------------------------------------------------------------------
def fetch_pauta(evento_id, force_reload=False):
    now = datetime.now()
    cache_key = str(evento_id)
    notas = load_notas()

    if not force_reload and cache_key in pauta_cache:
        cached = pauta_cache[cache_key]
        if now - cached['timestamp'] < CACHE_DURATION:
            logger.info(f"🟢 Pauta {evento_id} carregada do cache em memória.")
            return cached['itens'], False

    logger.info(f"🔍 Buscando pauta do evento {evento_id} via scraping...")
    conn = sqlite3.connect('users.db')
    c = conn.cursor()

    if not force_reload:
        try:
            c.execute("SELECT json_pauta, last_updated FROM pauta_cache_db WHERE evento_id = ?", (evento_id,))
            cached = c.fetchone()
            if cached:
                try:
                    itens = json.loads(cached[0])
                    last_updated = cached[1]
                    logger.info(f"📦 Carregado do cache persistente para evento {evento_id}, última atualização: {last_updated}")
                    pauta_cache[cache_key] = {'timestamp': now, 'itens': itens}
                    conn.close()
                    return itens, True
                except json.JSONDecodeError:
                    logger.warning(f"Cache inválido para evento {evento_id}")
        except sqlite3.OperationalError:
            logger.warning(f"Coluna last_updated não encontrada para evento {evento_id}. Tentando sem last_updated...")
            c.execute("SELECT json_pauta FROM pauta_cache_db WHERE evento_id = ?", (evento_id,))
            cached = c.fetchone()
            if cached:
                try:
                    itens = json.loads(cached[0])
                    logger.info(f"📦 Carregado do cache persistente para evento {evento_id}, sem last_updated")
                    pauta_cache[cache_key] = {'timestamp': now, 'itens': itens}
                    conn.close()
                    return itens, True
                except json.JSONDecodeError:
                    logger.warning(f"Cache inválido para evento {evento_id}")

    try:
        itens = obter_itens_pauta(evento_id)
        if not itens:
            raise ValueError("Scraper não retornou itens")

        itens_processados = []
        vistos = set()
        for ordem, item in enumerate(itens, start=1):
            id_principal = item.get('id_principal')
            if not id_principal or id_principal in vistos:
                continue
            vistos.add(id_principal)

            autores = item.get('autores', 'N/D')
            destaques = obter_destaques(id_principal)
            item_key = f"PROP_{id_principal}"

            # Carregar notas apenas para resumo_materia, orientacao e resumo_parecer
            nota = notas.get(item_key, {})
            resumo_materia = nota.get('resumo_materia', '')
            orientacao = nota.get('orientacao', '')
            resumo_parecer = nota.get('resumo_parecer', '')
            secao = item.get('secao', 'N/D')

            # Status é SEMPRE o valor da seção do scraper
            status = secao
            logger.info(f"Item {item_key} do evento {evento_id} (seção: {secao}) classificado como '{status}'")

            item_data = {
                'ordem': str(ordem),
                'id_principal': id_principal,
                'projeto': item['codigo'],
                'ementa': item['ementa'],
                'autor': autores,
                'relator': item.get('relator', 'Não atribuído'),
                'situacao': item.get('situacao', 'N/D'),
                'secao': secao,
                'resumo_materia': resumo_materia,
                'orientacao': orientacao,
                'resumo_parecer': resumo_parecer,
                'destaques_emendas': destaques,
                'status': status
            }
            itens_processados.append(item_data)

        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('''INSERT OR REPLACE INTO pauta_cache_db (evento_id, json_pauta, last_updated)
                     VALUES (?, ?, ?)''', (evento_id, json.dumps(itens_processados), current_time))
        conn.commit()

        pauta_cache[cache_key] = {'timestamp': now, 'itens': itens_processados}
        logger.info(f"✅ Pauta {evento_id} carregada via scraping com {len(itens_processados)} itens.")
        conn.close()
        return itens_processados, False

    except Exception as e:
        logger.warning(f"⚠️ Falha ao buscar via scraping ({e}). Tentando cache persistente...")
        c.execute("SELECT json_pauta FROM pauta_cache_db WHERE evento_id = ?", (evento_id,))
        cached = c.fetchone()
        conn.close()
        if cached:
            try:
                itens = json.loads(cached[0])
                logger.info(f"📦 Usando cache persistente para {evento_id}.")
                pauta_cache[cache_key] = {'timestamp': now, 'itens': itens}
                return itens, True
            except json.JSONDecodeError:
                logger.warning(f"Cache inválido para evento {evento_id}")
        logger.warning(f"❌ Nenhum dado de cache disponível para {evento_id}.")
        return [], True

# --------------------------------------------------------------------------
# ROTAS
# --------------------------------------------------------------------------
@app.route('/')
@login_required
def home():
    logger.info(f"Usuário {current_user.username} acessou a página inicial")
    return redirect(url_for('selecionar_data'))

@app.template_filter('datetimeformat')
def datetimeformat(value, format='%d/%m/%Y %H:%M'):
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime(format)
    except Exception:
        return value

@app.route('/selecionar-data', methods=['GET', 'POST'])
@login_required
def selecionar_data():
    data = request.form.get('data', datetime.now().strftime('%Y-%m-%d'))
    logger.info(f"Usuário {current_user.username} selecionou a data {data}")
    eventos = fetch_eventos_por_data(data)
    return render_template('selecionar_data.html', data_selecionada=data, eventos=eventos, user_role=current_user.role)

@app.route('/pauta/<int:evento_id>/view')
@login_required
def view_pauta(evento_id):
    logger.info(f"Usuário {current_user.username} acessando pauta do evento {evento_id}")
    force_reload = request.args.get('force_reload', 'false').lower() == 'true'
    itens, from_cache = fetch_pauta(evento_id, force_reload)
    last_updated = None

    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    try:
        c.execute("SELECT last_updated FROM pauta_cache_db WHERE evento_id = ?", (evento_id,))
        row = c.fetchone()
        if row:
            last_updated = row[0]
            logger.info(f"last_updated recuperado para evento {evento_id}: {last_updated}")
    except sqlite3.OperationalError:
        logger.warning(f"Coluna last_updated não encontrada para evento {evento_id}. Usando cache sem last_updated.")
    finally:
        conn.close()

    # Buscar informações do evento dinamicamente
    evento = fetch_evento_por_id(evento_id)

    return render_template(
        'pauta.html',
        evento_id=evento_id,
        evento=evento,
        itens=itens,
        from_cache=from_cache,
        user_role=current_user.role,
        last_updated=last_updated
    )

@app.route('/save_item', methods=['POST'])
@login_required
def save_item():
    data = request.get_json()
    evento_id = data.get('evento_id')
    id_principal = data.get('id_principal')
    ordem = data.get('ordem')
    logger.info(f"Usuário {current_user.username} salvando item para evento {evento_id}, ordem {ordem}")

    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    try:
        prop_key = f"PROP_{id_principal}"
        c.execute('''INSERT OR REPLACE INTO notas 
                    (item_key, evento_id, ordem, resumo_materia, orientacao, resumo_parecer)
                    VALUES (?, ?, ?, ?, ?, ?)''',
                (prop_key, evento_id, ordem,
                data.get('resumo_materia', ''),
                data.get('orientacao', ''),
                data.get('resumo_parecer', '')))

        destaques = data.get('destaques', [])
        for d in destaques:
            numero = d.get('numero', '').strip()
            resumo = d.get('resumo', '')
            if not numero:
                continue
            d_key = f"DSTQ_{id_principal}_{numero}"
            c.execute('''INSERT OR REPLACE INTO notas 
                        (item_key, evento_id, ordem, resumo_materia, orientacao, resumo_parecer)
                        VALUES (?, ?, ?, ?, ?, ?)''',
                    (d_key, evento_id, ordem, resumo, '', ''))

        conn.commit()
        pauta_cache.clear()
        logger.info(f"Item salvo com sucesso para evento {evento_id}, ordem {ordem}")
        return jsonify({'message': 'Item e destaques salvos com sucesso!'})
    except Exception as e:
        conn.rollback()
        logger.error(f"Erro ao salvar item para evento {evento_id}, ordem {ordem}: {e}")
        return jsonify({'message': f'Erro ao salvar: {e}'})
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 🔹 ROTA ROBUSTA PARA GERAR ANÁLISE DE PL COM PDF E FALLBACK AUTOMÁTICO
# --------------------------------------------------------------------------
from openai import OpenAI
from bs4 import BeautifulSoup
import requests, re, io
from pdfminer.high_level import extract_text

# --------------------------------------------------------------
# 🔑 Configuração fixa do cliente OpenAI (uso interno)
# --------------------------------------------------------------
from openai import OpenAI
import os

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

logger.info(f"🔑 OPENAI_API_KEY detectada? {'Sim' if os.getenv('OPENAI_API_KEY') else 'Não'}")


# --------------------------------------------------------------------------
# 🔹 ROTA COMPLETA: FAZ DOWNLOAD DO PDF E ENVIA O ARQUIVO INTEIRO PARA ANÁLISE POLÍTICA
# --------------------------------------------------------------------------
@app.route('/api/analisar_pl')
@login_required
def api_analisar_pl():
    numero_pl = request.args.get('numero', '').strip()
    if not numero_pl:
        return jsonify({"erro": "Número do projeto não informado."}), 400

    try:
        headers = {"User-Agent": "Mozilla/5.0"}

        # 🔍 Aceita formatos como "PL 2768/2025", "PEC 9/2024", "PDL12/2023"
        match = re.match(r'([A-Z]{2,4})\s*\.?\s*(\d+)\s*/\s*(\d{4})', numero_pl.upper())
        if not match:
            return jsonify({"erro": "Formato inválido. Use algo como 'PL 1234/2024' ou 'PEC 9/2023'."}), 400

        tipo, numero, ano = match.groups()
        logger.info(f"🔎 Buscando projeto: tipo={tipo}, número={numero}, ano={ano}")

        # 1️⃣ Consulta principal na API de proposições
        api_url = f"https://dadosabertos.camara.leg.br/api/v2/proposicoes?siglaTipo={tipo}&numero={numero}&ano={ano}"
        r_api = requests.get(api_url, headers=headers, timeout=15)
        r_api.raise_for_status()
        dados_api = r_api.json()

        if not dados_api.get("dados"):
            logger.warning(f"❌ {tipo} {numero}/{ano} não encontrado na API.")
            return jsonify({"erro": f"{tipo} {numero}/{ano} não encontrado na API."}), 404

        id_prop = dados_api["dados"][0]["id"]
        logger.info(f"📘 ID da proposição: {id_prop}")

        # 2️⃣ Busca do inteiro teor direto da API
        url_detalhes = f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_prop}"
        r_detalhes = requests.get(url_detalhes, headers=headers, timeout=15)
        r_detalhes.raise_for_status()
        dados_prop = r_detalhes.json().get("dados", {})
        link_pdf = dados_prop.get("urlInteiroTeor")

        if not link_pdf:
            logger.warning("⚠️ Nenhum 'urlInteiroTeor' encontrado. Tentando via ficha de tramitação...")
            link_pdf = f"https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao={id_prop}"

        logger.info(f"📄 PDF do inteiro teor: {link_pdf}")

        # 3️⃣ Faz download do PDF e salva temporariamente
        pdf_bytes = requests.get(link_pdf, headers=headers, timeout=25).content
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
            temp_pdf.write(pdf_bytes)
            temp_pdf_path = temp_pdf.name

        logger.info(f"📦 PDF baixado e salvo temporariamente em {temp_pdf_path}")

        # 4️⃣ Envia o PDF completo para a OpenAI como arquivo
        with open(temp_pdf_path, "rb") as f:
            upload = client.files.create(file=f, purpose="assistants")

        os.remove(temp_pdf_path)
        logger.info(f"☁️ PDF enviado à OpenAI com file_id={upload.id}")

        # 5️⃣ Solicita a análise política ao modelo GPT-5 (endpoint responses)
        resposta = client.responses.create(
            model="gpt-5",
            input=[
                {
                    "role": "system",
                    "content": (
                        "Você é um analista político da bancada do Partido Liberal (PL) na Câmara dos Deputados. "
                        "Suas análises devem refletir a perspectiva liberal-conservadora, "
                        "valorizando liberdade econômica, responsabilidade fiscal, defesa da família e segurança pública. "
                        "Evite repetições e bullets; use parágrafos curtos e subtítulos em negrito."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"Analise o Projeto {tipo} {numero}/{ano} com base no documento em anexo, "
                                "seguindo os quatro tópicos abaixo:\n\n"
                                "1. **📘 Resumo técnico** — explique o conteúdo e objetivo do projeto.\n"
                                "2. **🟢 Pontos positivos** — sob a ótica do Partido Liberal, "
                                "3. **🔴 Pontos negativos** — sob a ótica do Partido Liberal, "
                                "considerando oposição ao governo Lula.\n"
                                "4. **⚖️ Riscos políticos e de imagem** — repercussões prováveis no debate público e redes sociais.\n"
                                "5. **↔️ Orientação sugerida** — indique o voto (favorável, contrário ou com ressalvas) e justifique."
                                "Use esses mesmos ícones listados acima nas respostas dos itens."
                            ),
                        },
                        {
                            "type": "input_file",
                            "file_id": upload.id,
                        },
                    ],
                },
            ],
            max_output_tokens=10000,  # 🟢 aumente o limite
            reasoning={"effort": "high"},  # 🧠 força o modelo a analisar profundamente
        )


                # 6️⃣ Extrai texto de forma segura (compatível com qualquer versão da API)
        try:
            texto_gerado = None

            # Novo formato moderno (OpenAI 2025)
            if hasattr(resposta, "output_text") and resposta.output_text:
                texto_gerado = resposta.output_text.strip()

            # Estrutura em lista (SDK 1.0+)
            elif hasattr(resposta, "output") and resposta.output:
                conteudo = resposta.output[0].content
                if isinstance(conteudo, list) and len(conteudo) > 0 and hasattr(conteudo[0], "text"):
                    texto_gerado = conteudo[0].text.strip()

            # Estrutura clássica (chat.completions)
            elif hasattr(resposta, "choices") and resposta.choices:
                texto_gerado = resposta.choices[0].message.content.strip()

            # Fallback de segurança
            if not texto_gerado:
                texto_gerado = json.dumps(resposta, default=str)[:1000]
                logger.warning("⚠️ Resposta inesperada — conteúdo bruto armazenado para depuração.")

        except Exception as e:
            logger.error(f"Falha ao extrair texto da resposta: {e}")
            texto_gerado = "⚠️ O modelo respondeu em formato inesperado."

        logger.info(f"🧠 Análise gerada com sucesso (via PDF completo). Prévia: {texto_gerado[:120]}")
        
        
        # 🔧 FORMATAÇÃO VISUAL DO TEXTO
                # 🔧 FORMATAÇÃO VISUAL DO TEXTO (compatível com TinyMCE)
        texto_formatado = texto_gerado.strip()

        # Substitui os negritos markdown por HTML
        texto_formatado = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", texto_formatado)

        # Quebra de linha simples após ponto e vírgula
        texto_formatado = texto_formatado.replace(";", ";<br>")

        # Adiciona <p> nos principais blocos numerados (1., 2., 3., 4.)
        texto_formatado = re.sub(r"(\d+\.\s+)([A-ZÁÉÍÓÚÂÊÔÇ].+?)(?=\s)", r"<p><b>\1\2</b></p>", texto_formatado)

        # Garante espaçamento entre parágrafos
        texto_formatado = texto_formatado.replace("\n", "<br>")

        # Define o tipo de retorno HTML para renderizar formatação
        return texto_formatado, 200, {"Content-Type": "text/html; charset=utf-8"}


    except Exception as e:
        logger.error(f"⚠️ Erro ao gerar análise para {numero_pl}: {e}")
        return jsonify({"erro": f"Erro ao gerar análise: {e}"}), 500


# --------------------------------------------------------------------------
if __name__ == '__main__':
    init_db()
    init_pauta_cache_db()

    app.run(host='0.0.0.0', port=5000, debug=True)



