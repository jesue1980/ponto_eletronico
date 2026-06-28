from datetime import date, datetime, timedelta
from functools import wraps
import logging
from logging.handlers import RotatingFileHandler
import base64
import csv
import hashlib
import io
import json
import math
import os
import secrets
import subprocess
import shutil
import sqlite3
import urllib.error
import urllib.request
import urllib.parse
import uuid

from flask import (
    Flask, Response, g, redirect, render_template, render_template_string, request, session, url_for,
    send_from_directory
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from PIL import Image, ImageOps
try:
    import face_recognition
except Exception:
    face_recognition = None

from config import get_config


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
DB_PATH = os.environ.get("SQLITE_DB_PATH", os.path.join(BASE_DIR, "ponto_eletronico.db"))
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
SELFIE_DIR = os.path.join(BASE_DIR, "selfies")
FACE_VIDEO_DIR = os.path.join(SELFIE_DIR, "videos_faciais")
ANEXO_DIR = os.path.join(BASE_DIR, "anexos_ajustes")
DEFAULT_SECRET_KEY = "ponto-eletronico-repp-dev"
APP_ASSET_VERSION = "20260628.1"
GEO_VALIDATION_MIN_SECONDS = 10
GEO_VALIDATION_MAX_GAP_SECONDS = 6
GEO_VALIDATION_MIN_READINGS = 3
GEO_VALIDATION_MAX_ACCURACY_METERS = 50
GEO_BLOCK_MESSAGE = "Ponto bloqueado: a localização precisa permanecer ativa e válida durante todo o processo de verificação."
PASSWORD_HASH_PREFIXES = ("pbkdf2:", "scrypt:")
ALLOWED_ANEXO_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".doc", ".docx", ".xls", ".xlsx", ".txt"
}
LOCALHOST_NAMES = ("localhost", "127.0.0.1", "::1")
MOBILE_HTTPS_PATHS = ("/registrar", "/totem-facial", "/terminal-ponto")

app = Flask(__name__)
app.config.from_object(get_config())
app.secret_key = app.config.get("SECRET_KEY", DEFAULT_SECRET_KEY)
app.config["MAX_CONTENT_LENGTH"] = int(app.config.get("MAX_CONTENT_LENGTH") or (128 * 1024 * 1024))
DATABASE_URL = app.config.get("DATABASE_URL", DATABASE_URL)
DB_PATH = app.config.get("SQLITE_DB_PATH", DB_PATH)
if app.config.get("BEHIND_PROXY"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


def ensure_runtime_dirs():
    for path in (SELFIE_DIR, FACE_VIDEO_DIR, ANEXO_DIR, BACKUP_DIR, app.config["LOG_DIR"]):
        os.makedirs(path, exist_ok=True)


def configure_logging():
    ensure_runtime_dirs()
    log_path = os.path.join(app.config["LOG_DIR"], "app.log")
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    handler.setLevel(getattr(logging, app.config.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
    app.logger.setLevel(handler.level)
    if not any(isinstance(existing, RotatingFileHandler) and getattr(existing, "baseFilename", None) == handler.baseFilename for existing in app.logger.handlers):
        app.logger.addHandler(handler)


configure_logging()


def ensure_local_whatsapp_bridge():
    bridge_dir = os.path.join(BASE_DIR, "whatsapp_bridge")
    daemon = os.path.join(bridge_dir, "bridge_daemon.py")
    if not os.path.exists(daemon):
        return
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=1) as resp:
            if resp.status == 200:
                return
    except Exception:
        pass
    try:
        log_path = os.path.join(bridge_dir, "bridge_boot.log")
        log = open(log_path, "a", encoding="utf-8")
        kwargs = {
            "cwd": bridge_dir,
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": log,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([app.config.get("PYTHON_EXECUTABLE", os.sys.executable), "bridge_daemon.py"], **kwargs)
    except Exception as exc:
        app.logger.exception("Falha ao iniciar ponte local do WhatsApp: %s", exc)


ensure_local_whatsapp_bridge()


@app.after_request
def add_api_cors_headers(response):
    if request.path.startswith("/api/"):
        response.headers.setdefault("Access-Control-Allow-Origin", request.headers.get("Origin", "*"))
        response.headers.setdefault("Access-Control-Allow-Credentials", "true")
        response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, X-Requested-With")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return response

PERFIS = ("Administrador Principal", "RH Local", "Chefia Imediata", "Secretário da Pasta", "Funcionário")
PERFIS_CADASTRO = ("RH Local", "Chefia Imediata", "Secretário da Pasta", "Funcionário")
TIPOS_MARCACAO = ("entrada", "saida_almoco", "retorno_almoco", "saida_final")
TIPOS_LABEL = {
    "entrada": "Entrada",
    "saida_almoco": "Saída para intervalo",
    "retorno_almoco": "Retorno do intervalo",
    "saida_final": "Saída final",
}


ORIGEM_MANUAL = "MANUAL"
ORIGEM_FACIAL = "FACIAL"
ORIGEM_TOTEM_FACIAL = "TOTEM_FACIAL"
ORIGEM_BIOMETRIA = "BIOMETRIA"
ORIGEM_AJUSTE = "AJUSTE"
ORIGENS_MARCACAO = (ORIGEM_MANUAL, ORIGEM_FACIAL, ORIGEM_TOTEM_FACIAL, ORIGEM_BIOMETRIA, ORIGEM_AJUSTE)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def execute(sql, params=()):
    conn = get_db()
    conn.execute(sql, params)
    conn.commit()


def query(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def one(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def csrf_input():
    return f'<input type="hidden" name="_csrf_token" value="{csrf_token()}">'


def hash_password(password):
    return generate_password_hash(password)


def is_password_hash(value):
    return bool(value and value.startswith(PASSWORD_HASH_PREFIXES))


def verify_password(stored_password, candidate_password):
    if is_password_hash(stored_password):
        return check_password_hash(stored_password, candidate_password)
    return stored_password == candidate_password


def request_is_secure():
    return request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def request_is_mobile():
    user_agent = request.headers.get("User-Agent", "")
    return any(token in user_agent for token in ("Android", "iPhone", "iPad", "iPod", "Mobile", "Tablet"))


def request_host_without_port():
    return (request.host or "").split(":", 1)[0].strip("[]").lower()


def request_is_localhost():
    return request_host_without_port() in LOCALHOST_NAMES


def request_needs_mobile_https():
    return any(request.path == path or request.path.startswith(path + "/") for path in MOBILE_HTTPS_PATHS)


@app.before_request
def enforce_mobile_https_for_media():
    if request.method not in ("GET", "HEAD"):
        return None
    if request_is_secure() or request_is_localhost() or not request_is_mobile() or not request_needs_mobile_https():
        return None
    host = request_host_without_port()
    target_port = os.environ.get("PONTO_HTTPS_PORT", "5443")
    query = f"?{request.query_string.decode('utf-8')}" if request.query_string else ""
    return redirect(f"https://{host}:{target_port}{request.path}{query}", code=302)


@app.before_request
def validate_csrf():
    if request.method != "POST":
        return None
    sent_token = request.form.get("_csrf_token")
    expected_token = session.get("_csrf_token")
    if not sent_token or not expected_token or not secrets.compare_digest(sent_token, expected_token):
        return page("<div class='alert alert-danger'>Sessão expirada ou formulário inválido. Atualize a página e tente novamente.</div>", title="Sessão inválida"), 400
    return None


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), geolocation=(self), microphone=()")
    if request.path in ("/totem-facial", "/terminal-ponto") or request.path.startswith("/totem-facial/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.errorhandler(404)
def not_found(error):
    app.logger.info("404 em %s %s", request.method, request.path)
    return render_template("errors/404.html", title="Página não encontrada", user=current_user(), nav_links=nav_links), 404


@app.errorhandler(413)
def request_too_large(error):
    app.logger.warning("Upload acima do limite em %s %s", request.method, request.path)
    return render_template("errors/413.html", title="Arquivo muito grande", user=current_user(), nav_links=nav_links), 413


@app.errorhandler(500)
def internal_error(error):
    conn = g.get("db")
    if conn:
        conn.rollback()
    app.logger.exception("Erro interno em %s %s", request.method, request.path)
    return render_template("errors/500.html", title="Erro interno", user=current_user(), nav_links=nav_links), 500


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def parse_date(value, fallback=None):
    if not value:
        return fallback
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_time_str(value):
    return value if value and ":" in value else None


def day_bounds(day):
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    return start.isoformat(sep=" "), end.isoformat(sep=" ")


def period_bounds(start, end):
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time())
    return start_dt.isoformat(sep=" "), end_dt.isoformat(sep=" ")


def chunks(items, size=500):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def time_to_minutes(value):
    if not value:
        return None
    hour, minute = [int(part) for part in value.split(":")[:2]]
    return hour * 60 + minute


def minutes_between(start, end):
    if not start or not end:
        return 0
    start_min = time_to_minutes(start)
    end_min = time_to_minutes(end)
    if end_min < start_min:
        end_min += 24 * 60
    return end_min - start_min


def int_form(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def jornada_funcionario_from_form(form):
    tipo = form.get("tipo_jornada") or "Escala personalizada"
    entrada = parse_time_str(form.get("horario_entrada"))
    saida_almoco = parse_time_str(form.get("horario_saida_almoco"))
    retorno_almoco = parse_time_str(form.get("horario_retorno_almoco"))
    saida_final = parse_time_str(form.get("horario_saida_final"))
    if not entrada or not saida_final:
        return None, "Informe entrada e saida final."

    if tipo.startswith("8 horas"):
        if not saida_almoco or not retorno_almoco:
            return None, "Funcionario de 8 horas precisa de 4 registros: entrada, saida para intervalo, retorno e saida final."
        carga = minutes_between(entrada, saida_almoco) + minutes_between(retorno_almoco, saida_final)
    elif tipo.startswith("6 horas"):
        saida_almoco = None
        retorno_almoco = None
        carga = 360
    elif tipo.startswith("4 horas"):
        saida_almoco = None
        retorno_almoco = None
        carga = 240
    else:
        carga = minutes_between(entrada, saida_almoco) + minutes_between(retorno_almoco, saida_final) if saida_almoco and retorno_almoco else minutes_between(entrada, saida_final)

    if carga <= 0:
        return None, "Horarios da jornada invalidos."
    return {
        "tipo": tipo,
        "entrada": entrada,
        "saida_almoco": saida_almoco,
        "retorno_almoco": retorno_almoco,
        "saida_final": saida_final,
        "carga": carga,
        "tolerancia_antes": int_form(form.get("tolerancia_antes_minutos"), 0),
        "tolerancia_atraso": int_form(form.get("tolerancia_atraso_minutos"), 0),
    }, None


def fmt_minutes(minutes):
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes or 0))
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def haversine_m(lat1, lon1, lat2, lon2):
    radius = 6371000
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dp = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _geo_float(value):
    if value in (None, ""):
        raise ValueError("valor ausente")
    return float(value)


def validar_geolocalizacao_continua(form, latitude_alvo, longitude_alvo, raio_metros):
    detalhes = {
        "aprovado": False,
        "motivo_bloqueio": None,
        "latitude": None,
        "longitude": None,
        "precisao": None,
        "primeira_leitura_em": None,
        "ultima_leitura_em": None,
        "tempo_validacao_seg": 0,
        "qtd_leituras": 0,
        "falha_permissao": str(form.get("geo_falha_permissao") or "").lower() in ("1", "true", "sim", "yes"),
        "mock_suspeito": str(form.get("geo_mock_suspeito") or "").lower() in ("1", "true", "sim", "yes"),
        "leituras": [],
        "distancia_metros": None,
        "maior_distancia_metros": None,
        "maior_precisao_metros": None,
    }
    if detalhes["mock_suspeito"]:
        detalhes["motivo_bloqueio"] = "Sinal de localizacao falsa/mock location detectado"
        return False, detalhes
    if detalhes["falha_permissao"]:
        detalhes["motivo_bloqueio"] = "Permissao de localizacao perdida durante a validacao"
        return False, detalhes

    try:
        leituras_raw = json.loads(form.get("geo_leituras_json") or "[]")
    except json.JSONDecodeError:
        detalhes["motivo_bloqueio"] = "Pacote de leituras GPS invalido"
        return False, detalhes
    if not isinstance(leituras_raw, list):
        detalhes["motivo_bloqueio"] = "Pacote de leituras GPS invalido"
        return False, detalhes

    leituras = []
    for item in leituras_raw:
        if not isinstance(item, dict):
            continue
        try:
            leitura = {
                "latitude": _geo_float(item.get("latitude")),
                "longitude": _geo_float(item.get("longitude")),
                "precisao": _geo_float(item.get("precisao")),
                "capturada_em": int(float(item.get("capturada_em"))),
                "mock_suspeito": bool(item.get("mock_suspeito")),
            }
        except (TypeError, ValueError, OverflowError):
            continue
        leituras.append(leitura)

    leituras.sort(key=lambda item: item["capturada_em"])
    detalhes["qtd_leituras"] = len(leituras)
    detalhes["leituras"] = leituras
    if len(leituras) < GEO_VALIDATION_MIN_READINGS:
        detalhes["motivo_bloqueio"] = "Leituras GPS insuficientes para validacao continua"
        return False, detalhes
    if any(item["mock_suspeito"] for item in leituras):
        detalhes["mock_suspeito"] = True
        detalhes["motivo_bloqueio"] = "Sinal de localizacao falsa/mock location detectado"
        return False, detalhes

    primeira = leituras[0]
    ultima = leituras[-1]
    duracao_seg = int((ultima["capturada_em"] - primeira["capturada_em"]) / 1000)
    detalhes["tempo_validacao_seg"] = max(0, duracao_seg)
    detalhes["primeira_leitura_em"] = datetime.fromtimestamp(primeira["capturada_em"] / 1000).isoformat(sep=" ", timespec="seconds")
    detalhes["ultima_leitura_em"] = datetime.fromtimestamp(ultima["capturada_em"] / 1000).isoformat(sep=" ", timespec="seconds")
    if duracao_seg < GEO_VALIDATION_MIN_SECONDS:
        detalhes["motivo_bloqueio"] = "Localizacao ativada somente no fim ou validacao menor que o minimo"
        return False, detalhes

    intervalos = [
        (leituras[i]["capturada_em"] - leituras[i - 1]["capturada_em"]) / 1000
        for i in range(1, len(leituras))
    ]
    if intervalos and max(intervalos) > GEO_VALIDATION_MAX_GAP_SECONDS:
        detalhes["motivo_bloqueio"] = "Localizacao instavel ou interrompida durante a validacao"
        return False, detalhes

    distancias = []
    for leitura in leituras:
        if leitura["precisao"] > GEO_VALIDATION_MAX_ACCURACY_METERS:
            detalhes["motivo_bloqueio"] = "Precisao do GPS insuficiente"
            detalhes["maior_precisao_metros"] = leitura["precisao"]
            return False, detalhes
        distancia = haversine_m(leitura["latitude"], leitura["longitude"], latitude_alvo, longitude_alvo)
        distancias.append(distancia)
        if distancia > float(raio_metros):
            detalhes["motivo_bloqueio"] = "Uma ou mais leituras ficaram fora do raio autorizado"
            detalhes["distancia_metros"] = round(distancia, 2)
            detalhes["maior_distancia_metros"] = round(max(distancias), 2)
            return False, detalhes

    detalhes.update({
        "aprovado": True,
        "latitude": ultima["latitude"],
        "longitude": ultima["longitude"],
        "precisao": ultima["precisao"],
        "distancia_metros": round(distancias[-1], 2),
        "maior_distancia_metros": round(max(distancias), 2),
        "maior_precisao_metros": round(max(item["precisao"] for item in leituras), 2),
    })
    return True, detalhes


def current_user():
    if not session.get("user_id"):
        return None
    return one("SELECT * FROM usuarios WHERE id = ?", (session["user_id"],))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def perfil_required(*perfis):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            aliases = {
                "RH": "RH Local",
                "Gestor": "Chefia Imediata",
                "Chefia imediata": "Chefia Imediata",
                "Secretário": "Secretário da Pasta",
            }
            perfil = aliases.get(user["perfil"], user["perfil"])
            permitidos = {aliases.get(p, p) for p in perfis}
            if perfil not in permitidos:
                return page("<div class='alert alert-danger'>Acesso negado.</div>", title="Acesso negado"), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def audit(acao, entidade, entidade_id=None, detalhes=None):
    user_id = session.get("user_id")
    execute(
        "INSERT INTO auditoria (usuario_id, acao, entidade, entidade_id, detalhes, ip, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, acao, entidade, entidade_id, json.dumps(detalhes or {}, ensure_ascii=False), request.remote_addr if request else "", now_iso()),
    )


def add_column(table, column, definition):
    cols = [row["name"] for row in get_db().execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def is_admin(user=None):
    user = user or current_user()
    return bool(user and user["perfil"] == "Administrador Principal")


def is_rh_local(user=None):
    user = user or current_user()
    return bool(user and user["perfil"] in ("RH", "RH Local"))


def is_admin_or_rh(user=None):
    return is_admin(user) or is_rh_local(user)


def is_gestor(user=None):
    user = user or current_user()
    return bool(user and user["perfil"] in ("Chefia Imediata", "Chefia imediata", "Gestor"))


def is_secretario(user=None):
    user = user or current_user()
    return bool(user and user["perfil"] in ("Secretário da Pasta", "Secretário"))


def is_funcionario_profile(user=None):
    user = user or current_user()
    return bool(user and str(user["perfil"] or "").lower().startswith("funcion"))


def is_chefia_imediata(user=None):
    user = user or current_user()
    return bool(user and user["perfil"] in ("Chefia Imediata", "Chefia imediata") and user["chefia_id"])


@app.context_processor
def inject_asset_version():
    return {"asset_version": APP_ASSET_VERSION}


def backup_automatico():
    if not os.path.exists(DB_PATH):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    target = os.path.join(BACKUP_DIR, f"ponto_eletronico_{stamp}.db")
    if not os.path.exists(target):
        shutil.copy2(DB_PATH, target)


def init_db():
    os.makedirs(SELFIE_DIR, exist_ok=True)
    os.makedirs(FACE_VIDEO_DIR, exist_ok=True)
    os.makedirs(ANEXO_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS empresas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            razao_social TEXT NOT NULL,
            nome_fantasia TEXT,
            cnpj TEXT UNIQUE NOT NULL,
            ativa INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS locais_trabalho (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL,
            rh_local_id INTEGER,
            nome TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            raio_metros INTEGER NOT NULL DEFAULT 100,
            ativo INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (empresa_id) REFERENCES empresas(id),
            FOREIGN KEY (rh_local_id) REFERENCES rh_locais(id)
        );
        CREATE TABLE IF NOT EXISTS rh_locais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            email TEXT,
            unidade TEXT,
            ativo INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (empresa_id) REFERENCES empresas(id)
        );
        CREATE TABLE IF NOT EXISTS chefias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            email TEXT,
            cargo TEXT,
            usuario_id INTEGER,
            ativo INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (empresa_id) REFERENCES empresas(id),
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        );
        CREATE TABLE IF NOT EXISTS secretarios_pastas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL,
            pasta TEXT NOT NULL,
            nome TEXT NOT NULL,
            email TEXT,
            ativo INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (empresa_id) REFERENCES empresas(id)
        );
        CREATE TABLE IF NOT EXISTS justificativas_padrao (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            descricao TEXT NOT NULL UNIQUE,
            ativa INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jornadas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT UNIQUE NOT NULL,
            carga_minutos INTEGER NOT NULL,
            entrada TEXT NOT NULL,
            saida_almoco TEXT,
            retorno_almoco TEXT,
            saida_final TEXT NOT NULL,
            tolerancia_minutos INTEGER NOT NULL DEFAULT 0,
            tolerancia_antes_minutos INTEGER NOT NULL DEFAULT 0,
            tolerancia_atraso_minutos INTEGER NOT NULL DEFAULT 0,
            tipo_escala TEXT NOT NULL DEFAULT 'dias_uteis',
            data_inicio_escala TEXT,
            padrao INTEGER NOT NULL DEFAULT 0,
            ativa INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS funcionarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL,
            local_id INTEGER NOT NULL,
            jornada_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            cpf TEXT UNIQUE NOT NULL,
            matricula TEXT UNIQUE NOT NULL,
            cargo TEXT,
            email TEXT,
            telefone TEXT,
            data_admissao TEXT,
            rh_local_id INTEGER,
            chefia_id INTEGER,
            secretario_id INTEGER,
            ativo INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (empresa_id) REFERENCES empresas(id),
            FOREIGN KEY (local_id) REFERENCES locais_trabalho(id),
            FOREIGN KEY (jornada_id) REFERENCES jornadas(id),
            FOREIGN KEY (rh_local_id) REFERENCES rh_locais(id),
            FOREIGN KEY (chefia_id) REFERENCES chefias(id),
            FOREIGN KEY (secretario_id) REFERENCES secretarios_pastas(id)
        );
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            cpf TEXT,
            email TEXT,
            telefone TEXT,
            login TEXT UNIQUE NOT NULL,
            senha TEXT NOT NULL,
            perfil TEXT NOT NULL,
            secretaria_departamento TEXT,
            funcionario_id INTEGER,
            rh_local_id INTEGER,
            chefia_id INTEGER,
            secretario_id INTEGER,
            ativo INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id),
            FOREIGN KEY (rh_local_id) REFERENCES rh_locais(id),
            FOREIGN KEY (chefia_id) REFERENCES chefias(id),
            FOREIGN KEY (secretario_id) REFERENCES secretarios_pastas(id)
        );
        CREATE TABLE IF NOT EXISTS marcacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nsr TEXT UNIQUE NOT NULL,
            funcionario_id INTEGER NOT NULL,
            tipo TEXT NOT NULL,
            data_hora TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            precisao REAL,
            distancia_metros REAL NOT NULL,
            dentro_cerca INTEGER NOT NULL,
            selfie_path TEXT,
            dispositivo_id TEXT NOT NULL,
            user_agent TEXT,
            ip TEXT,
            hash_registro TEXT NOT NULL,
            justificativa_fora_horario TEXT,
            status_aprovacao TEXT NOT NULL DEFAULT 'normal',
            horario_previsto TEXT,
            decidido_por INTEGER,
            decidido_em TEXT,
            parecer_chefia TEXT,
            origem TEXT NOT NULL DEFAULT 'original',
            ajuste_id INTEGER,
            marcacao_original_id INTEGER,
            geo_primeira_leitura_em TEXT,
            geo_ultima_leitura_em TEXT,
            geo_tempo_validacao_seg INTEGER,
            geo_qtd_leituras INTEGER,
            geo_falha_permissao INTEGER NOT NULL DEFAULT 0,
            geo_mock_suspeito INTEGER NOT NULL DEFAULT 0,
            geo_leituras_json TEXT,
            criado_em TEXT NOT NULL,
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id),
            FOREIGN KEY (ajuste_id) REFERENCES ajustes_ponto(id),
            FOREIGN KEY (marcacao_original_id) REFERENCES marcacoes(id)
        );
        CREATE TABLE IF NOT EXISTS ajustes_ponto (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            funcionario_id INTEGER NOT NULL,
            marcacao_id INTEGER,
            tipo TEXT NOT NULL,
            data_hora_solicitada TEXT NOT NULL,
            justificativa TEXT NOT NULL,
            anexo_path TEXT,
            chefia_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pendente',
            solicitado_por INTEGER NOT NULL,
            aprovado_por INTEGER,
            parecer TEXT,
            criado_em TEXT NOT NULL,
            decidido_em TEXT,
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id),
            FOREIGN KEY (chefia_id) REFERENCES chefias(id)
        );
        CREATE TABLE IF NOT EXISTS compensacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            funcionario_id INTEGER NOT NULL,
            data TEXT NOT NULL,
            minutos INTEGER NOT NULL,
            descricao TEXT NOT NULL,
            criado_em TEXT NOT NULL,
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id)
        );
        CREATE TABLE IF NOT EXISTS funcionario_locais_autorizados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            funcionario_id INTEGER NOT NULL,
            local_id INTEGER NOT NULL,
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL,
            UNIQUE(funcionario_id, local_id),
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id),
            FOREIGN KEY (local_id) REFERENCES locais_trabalho(id)
        );
        CREATE TABLE IF NOT EXISTS totens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            descricao TEXT,
            local_id INTEGER,
            secretaria TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            raio_metros INTEGER NOT NULL DEFAULT 100,
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL,
            FOREIGN KEY (local_id) REFERENCES locais_trabalho(id)
        );
        CREATE TABLE IF NOT EXISTS biometrias_faciais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            funcionario_id INTEGER NOT NULL,
            video_path TEXT,
            foto_principal_path TEXT,
            fotos_auxiliares_json TEXT,
            embeddings_json TEXT NOT NULL,
            validacoes_json TEXT,
            motivo TEXT,
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_por INTEGER,
            criado_em TEXT NOT NULL,
            desativado_em TEXT,
            desativado_por INTEGER,
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id),
            FOREIGN KEY (criado_por) REFERENCES usuarios(id),
            FOREIGN KEY (desativado_por) REFERENCES usuarios(id)
        );
        CREATE TABLE IF NOT EXISTS dispositivos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,
            modelo TEXT,
            sistema_operacional TEXT,
            navegador TEXT,
            funcionario_id INTEGER,
            hash_dispositivo TEXT UNIQUE NOT NULL,
            ultimo_acesso TEXT,
            situacao TEXT NOT NULL DEFAULT 'ativo',
            criado_em TEXT NOT NULL,
            atualizado_em TEXT,
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id)
        );
        CREATE TABLE IF NOT EXISTS hierarquia_funcional (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            funcionario_id INTEGER NOT NULL UNIQUE,
            chefia_id INTEGER,
            secretario_id INTEGER,
            rh_local_id INTEGER,
            perfil TEXT,
            atualizado_em TEXT NOT NULL,
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id),
            FOREIGN KEY (chefia_id) REFERENCES chefias(id),
            FOREIGN KEY (secretario_id) REFERENCES secretarios_pastas(id),
            FOREIGN KEY (rh_local_id) REFERENCES rh_locais(id)
        );
        CREATE TABLE IF NOT EXISTS permissoes_modulos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            perfil TEXT NOT NULL,
            modulo TEXT NOT NULL,
            pode_visualizar INTEGER NOT NULL DEFAULT 1,
            pode_criar INTEGER NOT NULL DEFAULT 0,
            pode_editar INTEGER NOT NULL DEFAULT 0,
            pode_excluir INTEGER NOT NULL DEFAULT 0,
            atualizado_em TEXT NOT NULL,
            UNIQUE(perfil, modulo)
        );
        CREATE TABLE IF NOT EXISTS logs_sistema (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            modulo TEXT,
            acao TEXT NOT NULL,
            entidade TEXT,
            entidade_id INTEGER,
            ip TEXT,
            gps_json TEXT,
            dispositivo_hash TEXT,
            foto_path TEXT,
            video_path TEXT,
            embeddings_hash TEXT,
            alteracoes_json TEXT,
            criado_em TEXT NOT NULL,
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        );
        CREATE TABLE IF NOT EXISTS auditoria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            acao TEXT NOT NULL,
            entidade TEXT NOT NULL,
            entidade_id INTEGER,
            detalhes TEXT,
            ip TEXT,
            criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notificacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            titulo TEXT NOT NULL,
            mensagem TEXT NOT NULL,
            lida INTEGER NOT NULL DEFAULT 0,
            criado_em TEXT NOT NULL,
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        );
        CREATE TABLE IF NOT EXISTS whatsapp_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            evolution_url TEXT,
            api_key TEXT,
            instancia TEXT,
            status_conexao TEXT NOT NULL DEFAULT 'nao_configurado',
            modo_envio TEXT NOT NULL DEFAULT 'teste',
            numero_teste TEXT,
            qrcode TEXT,
            qrcode_gerado_em TEXT,
            atualizado_em TEXT
        );
        CREATE TABLE IF NOT EXISTS whatsapp_fila (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            funcionario_id INTEGER,
            chefia_id INTEGER,
            usuario_id INTEGER,
            destinatario_real TEXT,
            destinatario_usado TEXT,
            tipo TEXT NOT NULL,
            conteudo TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pendente',
            tentativas INTEGER NOT NULL DEFAULT 0,
            retorno_api TEXT,
            criado_em TEXT NOT NULL,
            enviado_em TEXT,
            ultimo_erro_em TEXT,
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id),
            FOREIGN KEY (chefia_id) REFERENCES chefias(id),
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        );
        CREATE TABLE IF NOT EXISTS aprovacao_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ajuste_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            expira_em TEXT NOT NULL,
            usado_em TEXT,
            criado_em TEXT NOT NULL,
            FOREIGN KEY (ajuste_id) REFERENCES ajustes_ponto(id)
        );
        CREATE INDEX IF NOT EXISTS idx_marcacoes_funcionario_data ON marcacoes (funcionario_id, data_hora);
        CREATE INDEX IF NOT EXISTS idx_marcacoes_status ON marcacoes (status_aprovacao);
        CREATE INDEX IF NOT EXISTS idx_marcacoes_data ON marcacoes (data_hora);
        CREATE INDEX IF NOT EXISTS idx_marcacoes_status_data ON marcacoes (status_aprovacao, data_hora);
        CREATE INDEX IF NOT EXISTS idx_marcacoes_func_status_data ON marcacoes (funcionario_id, status_aprovacao, data_hora);
        CREATE INDEX IF NOT EXISTS idx_ajustes_status ON ajustes_ponto (status);
        CREATE INDEX IF NOT EXISTS idx_ajustes_funcionario ON ajustes_ponto (funcionario_id);
        CREATE INDEX IF NOT EXISTS idx_ajustes_chefia_status ON ajustes_ponto (chefia_id, status);
        CREATE INDEX IF NOT EXISTS idx_ajustes_criado_em ON ajustes_ponto (criado_em);
        CREATE INDEX IF NOT EXISTS idx_auditoria_criado_em ON auditoria (criado_em);
        CREATE INDEX IF NOT EXISTS idx_funcionarios_rh_local ON funcionarios (rh_local_id);
        CREATE INDEX IF NOT EXISTS idx_funcionarios_chefia ON funcionarios (chefia_id);
        CREATE INDEX IF NOT EXISTS idx_funcionarios_secretario ON funcionarios (secretario_id);
        CREATE INDEX IF NOT EXISTS idx_dispositivos_funcionario ON dispositivos (funcionario_id);
        CREATE INDEX IF NOT EXISTS idx_logs_sistema_criado_em ON logs_sistema (criado_em);
        CREATE INDEX IF NOT EXISTS idx_compensacoes_funcionario_data ON compensacoes (funcionario_id, data);
        CREATE INDEX IF NOT EXISTS idx_usuarios_funcionario ON usuarios (funcionario_id);
        CREATE INDEX IF NOT EXISTS idx_locais_rh_ativo ON locais_trabalho (rh_local_id, ativo);
        CREATE INDEX IF NOT EXISTS idx_funcionario_locais_func ON funcionario_locais_autorizados (funcionario_id, ativo);
        CREATE INDEX IF NOT EXISTS idx_funcionario_locais_local ON funcionario_locais_autorizados (local_id, ativo);
        CREATE INDEX IF NOT EXISTS idx_totens_ativo ON totens (ativo);
        CREATE INDEX IF NOT EXISTS idx_biometrias_funcionario_ativo ON biometrias_faciais (funcionario_id, ativo);
        CREATE INDEX IF NOT EXISTS idx_whatsapp_fila_status ON whatsapp_fila (status, criado_em);
        CREATE INDEX IF NOT EXISTS idx_aprovacao_tokens_token ON aprovacao_tokens (token);
        """
    )
    conn.commit()
    conn.close()

    with app.app_context():
        add_column("usuarios", "funcionario_id", "INTEGER")
        add_column("usuarios", "rh_local_id", "INTEGER")
        add_column("usuarios", "chefia_id", "INTEGER")
        add_column("usuarios", "secretario_id", "INTEGER")
        add_column("usuarios", "cpf", "TEXT")
        add_column("usuarios", "email", "TEXT")
        add_column("usuarios", "telefone", "TEXT")
        add_column("usuarios", "secretaria_departamento", "TEXT")
        add_column("usuarios", "ativo", "INTEGER NOT NULL DEFAULT 1")
        add_column("locais_trabalho", "rh_local_id", "INTEGER")
        add_column("jornadas", "tipo_escala", "TEXT NOT NULL DEFAULT 'dias_uteis'")
        add_column("jornadas", "data_inicio_escala", "TEXT")
        add_column("jornadas", "tolerancia_antes_minutos", "INTEGER NOT NULL DEFAULT 0")
        add_column("jornadas", "tolerancia_atraso_minutos", "INTEGER NOT NULL DEFAULT 0")
        execute("UPDATE jornadas SET tolerancia_antes_minutos = tolerancia_minutos WHERE COALESCE(tolerancia_antes_minutos, 0) = 0 AND COALESCE(tolerancia_minutos, 0) > 0")
        execute("UPDATE jornadas SET tolerancia_atraso_minutos = tolerancia_minutos WHERE COALESCE(tolerancia_atraso_minutos, 0) = 0 AND COALESCE(tolerancia_minutos, 0) > 0")
        add_column("funcionarios", "rh_local_id", "INTEGER")
        add_column("funcionarios", "chefia_id", "INTEGER")
        add_column("funcionarios", "secretario_id", "INTEGER")
        add_column("funcionarios", "foto_base_path", "TEXT")
        add_column("funcionarios", "reconhecimento_facial_ativo", "INTEGER NOT NULL DEFAULT 0")
        add_column("funcionarios", "permite_totem_facial", "INTEGER NOT NULL DEFAULT 0")
        add_column("funcionarios", "foto_facial_cadastrada", "INTEGER NOT NULL DEFAULT 0")
        add_column("funcionarios", "mini_video_cadastrado", "INTEGER NOT NULL DEFAULT 0")
        add_column("funcionarios", "face_image_path", "TEXT")
        add_column("funcionarios", "face_embedding", "TEXT")
        add_column("funcionarios", "face_embeddings_json", "TEXT")
        add_column("funcionarios", "face_video_path", "TEXT")
        add_column("funcionarios", "permitir_totem_facial", "INTEGER NOT NULL DEFAULT 0")
        add_column("funcionarios", "updated_at", "TEXT")
        add_column("funcionarios", "whatsapp", "TEXT")
        add_column("funcionarios", "receber_whatsapp", "INTEGER NOT NULL DEFAULT 0")
        add_column("funcionarios", "papel_operacional", "TEXT NOT NULL DEFAULT 'funcionario'")
        add_column("funcionarios", "data_nascimento", "TEXT")
        add_column("funcionarios", "secretaria", "TEXT")
        add_column("funcionarios", "departamento", "TEXT")
        add_column("funcionarios", "tipo_servidor", "TEXT")
        add_column("funcionarios", "situacao", "TEXT NOT NULL DEFAULT 'ativo'")
        add_column("funcionarios", "escala", "TEXT")
        add_column("locais_trabalho", "endereco", "TEXT")
        add_column("locais_trabalho", "secretaria_responsavel", "TEXT")
        add_column("totens", "descricao", "TEXT")
        add_column("totens", "secretaria", "TEXT")
        add_column("totens", "atualizado_em", "TEXT")
        add_column("chefias", "whatsapp", "TEXT")
        add_column("chefias", "receber_solicitacoes_whatsapp", "INTEGER NOT NULL DEFAULT 0")
        add_column("marcacoes", "origem", "TEXT NOT NULL DEFAULT 'original'")
        add_column("marcacoes", "origem_normalizada", "TEXT")
        add_column("marcacoes", "totem_id", "INTEGER")
        add_column("marcacoes", "local_validacao_id", "INTEGER")
        add_column("marcacoes", "geolocalizacao_status", "TEXT")
        add_column("marcacoes", "distancia_validacao_metros", "REAL")
        add_column("marcacoes", "justificativa_fora_horario", "TEXT")
        add_column("marcacoes", "status_aprovacao", "TEXT NOT NULL DEFAULT 'normal'")
        add_column("marcacoes", "horario_previsto", "TEXT")
        add_column("marcacoes", "decidido_por", "INTEGER")
        add_column("marcacoes", "decidido_em", "TEXT")
        add_column("marcacoes", "parecer_chefia", "TEXT")
        add_column("marcacoes", "ajuste_id", "INTEGER")
        add_column("marcacoes", "marcacao_original_id", "INTEGER")
        add_column("marcacoes", "geo_primeira_leitura_em", "TEXT")
        add_column("marcacoes", "geo_ultima_leitura_em", "TEXT")
        add_column("marcacoes", "geo_tempo_validacao_seg", "INTEGER")
        add_column("marcacoes", "geo_qtd_leituras", "INTEGER")
        add_column("marcacoes", "geo_falha_permissao", "INTEGER NOT NULL DEFAULT 0")
        add_column("marcacoes", "geo_mock_suspeito", "INTEGER NOT NULL DEFAULT 0")
        add_column("marcacoes", "geo_leituras_json", "TEXT")
        add_column("ajustes_ponto", "anexo_path", "TEXT")
        add_column("ajustes_ponto", "chefia_id", "INTEGER")
        add_column("ajustes_ponto", "observacao", "TEXT")
        add_column("whatsapp_config", "qrcode", "TEXT")
        add_column("whatsapp_config", "qrcode_gerado_em", "TEXT")
        execute("INSERT OR IGNORE INTO whatsapp_config (id, modo_envio, status_conexao, atualizado_em) VALUES (1, 'teste', 'nao_configurado', ?)", (now_iso(),))

        if not one("SELECT id FROM empresas WHERE cnpj = '00.000.000/0001-00'"):
            execute(
                "INSERT INTO empresas (razao_social, nome_fantasia, cnpj, criado_em) VALUES (?, ?, ?, ?)",
                ("Empresa Demonstração REP-P", "Ponto Eletrônico", "00.000.000/0001-00", now_iso()),
            )
        empresa = one("SELECT * FROM empresas ORDER BY id LIMIT 1")
        if not one("SELECT id FROM locais_trabalho WHERE nome = 'Sede Administrativa'"):
            execute(
                "INSERT INTO locais_trabalho (empresa_id, nome, latitude, longitude, raio_metros) VALUES (?, ?, ?, ?, ?)",
                (empresa["id"], "Sede Administrativa", -15.6014, -56.0979, 300),
            )
        if not one("SELECT id FROM rh_locais WHERE nome = 'RH Local Demonstração'"):
            execute(
                "INSERT INTO rh_locais (empresa_id, nome, email, unidade) VALUES (?, ?, ?, ?)",
                (empresa["id"], "RH Local Demonstração", "rh@teste.local", "Sede Administrativa"),
            )
        if not one("SELECT id FROM chefias WHERE nome = 'Chefia Imediata Teste'"):
            execute(
                "INSERT INTO chefias (empresa_id, nome, email, cargo) VALUES (?, ?, ?, ?)",
                (empresa["id"], "Chefia Imediata Teste", "gestor@teste.local", "Coordenador"),
            )
        if not one("SELECT id FROM secretarios_pastas WHERE pasta = 'Administração'"):
            execute(
                "INSERT INTO secretarios_pastas (empresa_id, pasta, nome, email) VALUES (?, ?, ?, ?)",
                (empresa["id"], "Administração", "Secretário Teste", "secretario@teste.local"),
            )
        for desc in ("Esquecimento de marcação", "Problema técnico", "Serviço externo", "Atestado ou declaração", "Correção de horário"):
            if not one("SELECT id FROM justificativas_padrao WHERE descricao = ?", (desc,)):
                execute("INSERT INTO justificativas_padrao (descricao, criado_em) VALUES (?, ?)", (desc, now_iso()))
        jornadas = [
            ("Jornada 8 horas", 480, "08:00", "12:00", "14:00", "18:00", 10, "dias_uteis", None, 1),
            ("8 horas com intervalo", 480, "07:00", "11:00", "13:00", "17:00", 10, "dias_uteis", None, 1),
            ("Jornada 6 horas", 360, "07:00", None, None, "13:00", 10, "dias_uteis", None, 1),
            ("6 horas corridas", 360, "07:00", None, None, "13:00", 10, "dias_uteis", None, 1),
            ("Jornada 4 horas", 240, "08:00", None, None, "12:00", 10, "dias_uteis", None, 1),
            ("4 horas corridas", 240, "08:00", None, None, "12:00", 10, "dias_uteis", None, 1),
            ("Escala 12x36", 720, "07:00", None, None, "19:00", 10, "12x36", date.today().isoformat(), 1),
            ("Escala personalizada", 480, "08:00", None, None, "16:00", 10, "diaria", None, 1),
        ]
        for jornada in jornadas:
            if not one("SELECT id FROM jornadas WHERE nome = ?", (jornada[0],)):
                execute(
                    """INSERT INTO jornadas
                       (nome, carga_minutos, entrada, saida_almoco, retorno_almoco, saida_final,
                        tolerancia_minutos, tipo_escala, data_inicio_escala, padrao)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    jornada,
                )
        local = one("SELECT * FROM locais_trabalho ORDER BY id LIMIT 1")
        jornada = one("SELECT * FROM jornadas WHERE nome = 'Jornada 8 horas'")
        if not one("SELECT id FROM funcionarios WHERE matricula = 'MAT-001'"):
            execute(
                """INSERT INTO funcionarios
                   (empresa_id, local_id, jornada_id, nome, cpf, matricula, cargo, email, telefone, data_admissao)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (empresa["id"], local["id"], jornada["id"], "Funcionário Teste", "000.000.000-00", "MAT-001",
                 "Assistente Administrativo", "funcionario@teste.local", "(65) 99999-0000", date.today().isoformat()),
            )
        funcionario = one("SELECT * FROM funcionarios WHERE matricula = 'MAT-001'")
        rh_local = one("SELECT * FROM rh_locais ORDER BY id LIMIT 1")
        chefia = one("SELECT * FROM chefias ORDER BY id LIMIT 1")
        secretario = one("SELECT * FROM secretarios_pastas ORDER BY id LIMIT 1")
        execute(
            """UPDATE funcionarios
               SET rh_local_id = COALESCE(rh_local_id, ?),
                   chefia_id = COALESCE(chefia_id, ?),
                   secretario_id = COALESCE(secretario_id, ?)
               WHERE id = ?""",
            (rh_local["id"], chefia["id"], secretario["id"], funcionario["id"]),
        )
        execute(
            "UPDATE funcionarios SET permite_totem_facial = 0 WHERE matricula = 'MAT-001' AND nome LIKE '%Teste%'"
        )
        execute("UPDATE locais_trabalho SET rh_local_id = COALESCE(rh_local_id, ?) WHERE nome = 'Sede Administrativa'", (rh_local["id"],))
        users = [
            ("Administrador Principal", "admin", "Admin@2026", "Administrador Principal", None),
            ("Chefia Imediata Teste", "gestor", "Gestor@2026", "Chefia Imediata", None),
            ("RH Local Teste", "rh", "RH@2026", "RH Local", None),
            ("Funcionário Teste", "funcionario", "Funcionario@2026", "Funcionário", funcionario["id"]),
            ("Servidor Teste", "servidor", "Servidor@2026", "Funcionário", funcionario["id"]),
        ]
        for user in users:
            if not one("SELECT id FROM usuarios WHERE login = ?", (user[1],)):
                execute(
                    "INSERT INTO usuarios (nome, login, senha, perfil, funcionario_id) VALUES (?, ?, ?, ?, ?)",
                    (user[0], user[1], hash_password(user[2]), user[3], user[4]),
                )
            else:
                execute(
                    "UPDATE usuarios SET nome = ?, perfil = ?, funcionario_id = COALESCE(funcionario_id, ?) WHERE login = ?",
                    (user[0], user[3], user[4], user[1]),
                )
        execute("UPDATE usuarios SET rh_local_id = COALESCE(rh_local_id, ?) WHERE login = 'rh'", (rh_local["id"],))
        gestor_user = one("SELECT id FROM usuarios WHERE login = 'gestor'")
        if gestor_user:
            execute("UPDATE chefias SET usuario_id = COALESCE(usuario_id, ?) WHERE id = ?", (gestor_user["id"], chefia["id"]))
            execute("UPDATE usuarios SET chefia_id = COALESCE(chefia_id, ?) WHERE id = ?", (chefia["id"], gestor_user["id"]))
        execute("UPDATE usuarios SET secretario_id = COALESCE(secretario_id, ?) WHERE perfil IN ('Secretário', 'Secretário da Pasta')", (secretario["id"],))
        execute(
            """INSERT OR IGNORE INTO funcionario_locais_autorizados (funcionario_id, local_id, ativo, criado_em)
               SELECT id, local_id, 1, ? FROM funcionarios WHERE local_id IS NOT NULL""",
            (now_iso(),),
        )
        if not one("SELECT id FROM totens LIMIT 1"):
            local_totem = one("SELECT * FROM locais_trabalho WHERE ativo = 1 ORDER BY id LIMIT 1")
            if local_totem:
                execute(
                    """INSERT INTO totens (nome, local_id, latitude, longitude, raio_metros, ativo, criado_em)
                       VALUES (?, ?, ?, ?, ?, 1, ?)""",
                    (
                        "Totem Demonstração",
                        local_totem["id"],
                        local_totem["latitude"],
                        local_totem["longitude"],
                        local_totem["raio_metros"],
                        now_iso(),
                    ),
                )
    backup_automatico()


def trabalha_no_dia(jornada, day):
    if jornada["tipo_escala"] == "diaria":
        return True
    if jornada["tipo_escala"] == "12x36":
        start = parse_date(jornada["data_inicio_escala"], date.today())
        return (day - start).days % 2 == 0
    return day.weekday() < 5


def horario_previsto_por_tipo(jornada, tipo):
    mapa = {
        "entrada": jornada["entrada"],
        "saida_almoco": jornada["saida_almoco"],
        "retorno_almoco": jornada["retorno_almoco"],
        "saida_final": jornada["saida_final"],
    }
    return mapa.get(tipo)


def fora_da_tolerancia(jornada, tipo, data_hora):
    previsto = horario_previsto_por_tipo(jornada, tipo)
    if not previsto:
        return False, None, 0
    batido = data_hora[11:16] if isinstance(data_hora, str) else data_hora.strftime("%H:%M")
    diferenca_assinada = time_to_minutes(batido) - time_to_minutes(previsto)
    diferenca = abs(diferenca_assinada)
    tolerancia_antes = int(jornada["tolerancia_antes_minutos"] if "tolerancia_antes_minutos" in jornada.keys() else jornada["tolerancia_minutos"] or 0)
    tolerancia_atraso = int(jornada["tolerancia_atraso_minutos"] if "tolerancia_atraso_minutos" in jornada.keys() else jornada["tolerancia_minutos"] or 0)
    limite = tolerancia_antes if diferenca_assinada < 0 else tolerancia_atraso
    return diferenca > limite, previsto, diferenca


def calcular_dia(funcionario, jornada, day, marcacoes_por_dia=None, compensacoes_por_dia=None):
    key = (funcionario["id"], day.isoformat())
    if marcacoes_por_dia is None:
        inicio, fim = day_bounds(day)
        todas_marcacoes = query(
            "SELECT * FROM marcacoes WHERE funcionario_id = ? AND data_hora >= ? AND data_hora < ? ORDER BY data_hora",
            (funcionario["id"], inicio, fim),
        )
    else:
        todas_marcacoes = marcacoes_por_dia.get(key, [])
    rows = [
        row for row in todas_marcacoes
        if (row["status_aprovacao"] or "normal") in ("normal", "aprovado")
    ]
    marks = {row["tipo"]: row["data_hora"][11:16] for row in rows}
    previsto = jornada["carga_minutos"] if trabalha_no_dia(jornada, day) else 0
    if jornada["saida_almoco"] and jornada["retorno_almoco"]:
        trabalhado = minutes_between(marks.get("entrada"), marks.get("saida_almoco")) + minutes_between(marks.get("retorno_almoco"), marks.get("saida_final"))
    else:
        trabalhado = minutes_between(marks.get("entrada"), marks.get("saida_final"))
    atraso = 0
    if marks.get("entrada"):
        tolerancia_atraso = int(jornada["tolerancia_atraso_minutos"] if "tolerancia_atraso_minutos" in jornada.keys() else jornada["tolerancia_minutos"] or 0)
        atraso = max(0, time_to_minutes(marks["entrada"]) - (time_to_minutes(jornada["entrada"]) + tolerancia_atraso))
    falta = previsto > 0 and not rows
    saldo = trabalhado - previsto
    if falta:
        saldo = -previsto
    if compensacoes_por_dia is None:
        compensacao = one("SELECT COALESCE(SUM(minutos), 0) total FROM compensacoes WHERE funcionario_id = ? AND data = ?", (funcionario["id"], day.isoformat()))["total"]
    else:
        compensacao = compensacoes_por_dia.get(key, 0)
    saldo_com_compensacao = saldo + compensacao
    status_marcacoes = "Batida normal"
    if any(r["status_aprovacao"] == "pendente" for r in todas_marcacoes):
        status_marcacoes = "Batida fora do horário pendente"
    elif any(r["status_aprovacao"] == "reprovado" for r in todas_marcacoes):
        status_marcacoes = "Não aprovado pela chefia"
    elif any(r["status_aprovacao"] == "aprovado" for r in todas_marcacoes):
        status_marcacoes = "Aprovado pela chefia"
    origens_marcacoes = sorted({
        (row["origem_normalizada"] or row["origem"] or ORIGEM_MANUAL)
        for row in todas_marcacoes
    })
    return {
        "data": day,
        "funcionario": funcionario,
        "jornada": jornada,
        "marcacoes": todas_marcacoes,
        "marcacoes_validas": rows,
        "prevista_min": previsto,
        "trabalhada_min": trabalhado,
        "saldo_min": saldo_com_compensacao,
        "extras_min": max(0, saldo_com_compensacao),
        "atraso_min": atraso,
        "compensacao_min": compensacao,
        "falta": falta,
        "folga": previsto == 0,
        "prevista": fmt_minutes(previsto),
        "trabalhada": fmt_minutes(trabalhado),
        "saldo": fmt_minutes(saldo_com_compensacao),
        "extras": fmt_minutes(max(0, saldo_com_compensacao)),
        "atraso": fmt_minutes(atraso),
        "compensacao": fmt_minutes(compensacao),
        "status_marcacoes": status_marcacoes,
        "origens_marcacoes": ", ".join(origens_marcacoes) if origens_marcacoes else "-",
    }


def next_tipo(funcionario_id):
    funcionario = one("""SELECT f.*, j.saida_almoco, j.retorno_almoco
                         FROM funcionarios f
                         JOIN jornadas j ON j.id = f.jornada_id
                         WHERE f.id = ?""", (funcionario_id,))
    sequencia = ["entrada", "saida_final"]
    if funcionario and funcionario["saida_almoco"] and funcionario["retorno_almoco"]:
        sequencia = ["entrada", "saida_almoco", "retorno_almoco", "saida_final"]
    inicio, fim = day_bounds(date.today())
    count = one(
        "SELECT COUNT(*) total FROM marcacoes WHERE funcionario_id = ? AND data_hora >= ? AND data_hora < ?",
        (funcionario_id, inicio, fim),
    )["total"]
    return sequencia[min(count, len(sequencia) - 1)]


def _decode_data_url(data_url, expected_type=None):
    if not data_url or "," not in data_url:
        return None, None
    header, encoded = data_url.split(",", 1)
    if expected_type and expected_type not in header.lower():
        return None, None
    try:
        return header.lower(), base64.b64decode(encoded)
    except Exception:
        return None, None


def _write_compressed_image(raw_bytes, path, max_side=640, quality=78):
    try:
        image = Image.open(io.BytesIO(raw_bytes))
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        elif image.mode == "L":
            image = image.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        image.save(path, "JPEG", quality=quality, optimize=True, progressive=True)
        return True
    except Exception:
        app.logger.exception("Falha ao compactar imagem facial para %s", path)
        return False


def save_image_data_url(data_url, funcionario_id, prefix, max_side=640, quality=78):
    header, raw = _decode_data_url(data_url, "image")
    if not header or not raw:
        return None
    filename = f"{prefix}_{funcionario_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"
    path = os.path.join(SELFIE_DIR, filename)
    if not _write_compressed_image(raw, path, max_side=max_side, quality=quality):
        return None
    return os.path.join("selfies", filename)


def save_selfie(data_url, funcionario_id):
    return save_image_data_url(data_url, funcionario_id, str(funcionario_id), max_side=640, quality=74)
    filename = f"{funcionario_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"
    path = os.path.join(SELFIE_DIR, filename)
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(encoded))
    return os.path.join("selfies", filename)


def save_foto_base(file_storage, funcionario_id):
    if not file_storage or not file_storage.filename:
        return None
    extension = os.path.splitext(file_storage.filename)[1].lower()
    if extension not in (".jpg", ".jpeg", ".png", ".webp"):
        return None
    filename = f"base_{funcionario_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"
    path = os.path.join(SELFIE_DIR, filename)
    raw = file_storage.read()
    if not raw or not _write_compressed_image(raw, path, max_side=640, quality=82):
        return None
    return os.path.join("selfies", filename)


def save_video_facial_upload(file_storage, funcionario_id):
    if not file_storage or not file_storage.filename:
        return None
    extension = os.path.splitext(file_storage.filename)[1].lower()
    if extension not in (".webm", ".mp4"):
        content_type = (file_storage.mimetype or "").lower()
        if "mp4" in content_type:
            extension = ".mp4"
        elif "webm" in content_type:
            extension = ".webm"
        else:
            return None
    os.makedirs(FACE_VIDEO_DIR, exist_ok=True)
    filename = f"video_face_{funcionario_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}{extension}"
    path = os.path.join(FACE_VIDEO_DIR, filename)
    file_storage.save(path)
    return os.path.join("selfies", "videos_faciais", filename)


def save_foto_base_data_url(data_url, funcionario_id):
    return save_image_data_url(data_url, funcionario_id, "base", max_side=640, quality=82)


def save_data_url_file(data_url, funcionario_id, prefix, extension, directory=None, public_prefix="selfies"):
    if not data_url or "," not in data_url:
        return None
    header, encoded = data_url.split(",", 1)
    if "base64" not in header:
        return None
    safe_extension = extension.lower().lstrip(".")
    filename = f"{prefix}_{funcionario_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.{safe_extension}"
    target_dir = directory or SELFIE_DIR
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, filename)
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(encoded))
    return os.path.join(public_prefix, filename)


def save_foto_auxiliar_data_url(data_url, funcionario_id):
    return save_image_data_url(data_url, funcionario_id, "aux_face", max_side=640, quality=74)


def save_video_facial_data_url(data_url, funcionario_id):
    if not data_url or "," not in data_url:
        return None
    header = data_url.split(",", 1)[0].lower()
    extension = "mp4" if "mp4" in header else "webm"
    if extension not in ("webm", "mp4"):
        return None
    return save_data_url_file(
        data_url,
        funcionario_id,
        "video_face",
        extension,
        directory=FACE_VIDEO_DIR,
        public_prefix=os.path.join("selfies", "videos_faciais"),
    )


def average_embedding(embeddings):
    valid = [embedding for embedding in embeddings if isinstance(embedding, list) and len(embedding) == 128]
    if not valid:
        return None
    return [sum(values) / len(valid) for values in zip(*valid)]


def funcionario_status_payload(row):
    locais_autorizados = []
    if "locais_autorizados" in row.keys() and row["locais_autorizados"]:
        locais_autorizados = [int(item) for item in str(row["locais_autorizados"]).split(",") if item]
    return {
        "id": row["id"],
        "nome": row["nome"],
        "matricula": row["matricula"],
        "cpf": row["cpf"] if "cpf" in row.keys() else "",
        "cargo": row["cargo"] if "cargo" in row.keys() else "",
        "email": row["email"] if "email" in row.keys() else "",
        "telefone": row["telefone"] if "telefone" in row.keys() else "",
        "whatsapp": row["whatsapp"] if "whatsapp" in row.keys() else "",
        "receber_whatsapp": bool(row["receber_whatsapp"]) if "receber_whatsapp" in row.keys() else False,
        "data_admissao": row["data_admissao"] if "data_admissao" in row.keys() else "",
        "empresa_id": row["empresa_id"] if "empresa_id" in row.keys() else None,
        "local_id": row["local_id"] if "local_id" in row.keys() else None,
        "jornada_id": row["jornada_id"] if "jornada_id" in row.keys() else None,
        "rh_local_id": row["rh_local_id"] if "rh_local_id" in row.keys() else None,
        "chefia_id": row["chefia_id"] if "chefia_id" in row.keys() else None,
        "secretario_id": row["secretario_id"] if "secretario_id" in row.keys() else None,
        "papel_operacional": row["papel_operacional"] if "papel_operacional" in row.keys() else "funcionario",
        "data_nascimento": row["data_nascimento"] if "data_nascimento" in row.keys() else "",
        "secretaria": row["secretaria"] if "secretaria" in row.keys() else "",
        "departamento": row["departamento"] if "departamento" in row.keys() else "",
        "tipo_servidor": row["tipo_servidor"] if "tipo_servidor" in row.keys() else "",
        "situacao": row["situacao"] if "situacao" in row.keys() else ("ativo" if row["ativo"] else "inativo"),
        "escala": row["escala"] if "escala" in row.keys() else "",
        "login": row["login"] if "login" in row.keys() else "",
        "perfil_acesso": row["perfil_acesso"] if "perfil_acesso" in row.keys() else "",
        "ativo": bool(row["ativo"]) if "ativo" in row.keys() else True,
        "locais_autorizados": locais_autorizados,
        "horario_entrada": row["entrada"] if "entrada" in row.keys() else "",
        "horario_saida_almoco": row["saida_almoco"] if "saida_almoco" in row.keys() else "",
        "horario_retorno_almoco": row["retorno_almoco"] if "retorno_almoco" in row.keys() else "",
        "horario_saida_final": row["saida_final"] if "saida_final" in row.keys() else "",
        "tolerancia_antes_minutos": row["tolerancia_antes_minutos"] if "tolerancia_antes_minutos" in row.keys() else 0,
        "tolerancia_atraso_minutos": row["tolerancia_atraso_minutos"] if "tolerancia_atraso_minutos" in row.keys() else 0,
        "empresa": row["empresa"] if "empresa" in row.keys() else "",
        "local": row["local"] if "local" in row.keys() else "",
        "jornada": row["jornada"] if "jornada" in row.keys() else "",
        "rh_local": row["rh_local"] if "rh_local" in row.keys() else "",
        "chefia": row["chefia"] if "chefia" in row.keys() else "",
        "secretario_pasta": row["secretario_pasta"] if "secretario_pasta" in row.keys() else "",
        "secretario": row["secretario"] if "secretario" in row.keys() else "",
        "foto_facial_cadastrada": bool(row["foto_base_path"] or row["foto_facial_cadastrada"]),
        "mini_video_cadastrado": bool(row["mini_video_cadastrado"] or row["biometria_facial_id"]),
        "reconhecimento_facial_ativo": bool(row["reconhecimento_facial_ativo"]),
        "permite_totem_facial": bool(row["permite_totem_facial"] or row["permitir_totem_facial"]),
        "permitir_totem_facial": bool(row["permite_totem_facial"] or row["permitir_totem_facial"]),
        "foto_base_path": row["foto_base_path"],
        "face_image_path": row["face_image_path"] if "face_image_path" in row.keys() else row["foto_base_path"],
        "face_video_path": row["face_video_path"],
        "biometria_facial_id": row["biometria_facial_id"],
        "biometria_facial_criada_em": row["biometria_facial_criada_em"],
        "updated_at": row["updated_at"],
    }


def salvar_biometria_video_funcionario(funcionario_id, user, form):
    try:
        embeddings = json.loads(form.get("embeddings_json") or form.get("face_video_embeddings_json") or "[]")
        validacoes = json.loads(form.get("validacoes_json") or form.get("face_video_validacoes_json") or "{}")
        fotos_auxiliares = json.loads(form.get("fotos_auxiliares_json") or form.get("face_video_auxiliares_json") or "[]")
    except json.JSONDecodeError:
        return None, {"erro": "Dados biometricos invalidos."}, 400

    if len(embeddings) < 3:
        return None, {"erro": "Capture pelo menos 3 vetores faciais validos no mini video."}, 400
    if not validacoes.get("um_rosto"):
        return None, {"erro": "Cadastro bloqueado: deve haver apenas um rosto na camera."}, 400
    if not validacoes.get("nitidez_ok"):
        return None, {"erro": "Cadastro bloqueado: imagem sem nitidez suficiente."}, 400
    if not validacoes.get("iluminacao_ok"):
        return None, {"erro": "Cadastro bloqueado: iluminacao insuficiente."}, 400
    if not validacoes.get("prova_vida_ok"):
        return None, {"erro": "Cadastro bloqueado: prova de vida por movimento nao aprovada."}, 400

    video_path = save_video_facial_data_url(form.get("video_data") or form.get("face_video_data"), funcionario_id)
    if not video_path:
        video_path = save_video_facial_upload(request.files.get("video_file") or request.files.get("face_video_file"), funcionario_id)
    foto_principal_path = save_foto_base_data_url(form.get("foto_principal") or form.get("face_video_foto_principal"), funcionario_id)
    if not video_path:
        return None, {"erro": "Mini video invalido. Use formato webm ou mp4."}, 400
    if not foto_principal_path:
        return None, {"erro": "Foto principal frontal invalida."}, 400

    aux_paths = []
    for foto in fotos_auxiliares[:6]:
        path = save_foto_auxiliar_data_url(foto, funcionario_id)
        if path:
            aux_paths.append(path)

    anterior = one("SELECT * FROM biometrias_faciais WHERE funcionario_id = ? AND ativo = 1 ORDER BY id DESC LIMIT 1", (funcionario_id,))
    if anterior:
        execute(
            "UPDATE biometrias_faciais SET ativo = 0, desativado_em = ?, desativado_por = ? WHERE id = ?",
            (now_iso(), user["id"], anterior["id"]),
        )

    execute(
        """INSERT INTO biometrias_faciais
           (funcionario_id, video_path, foto_principal_path, fotos_auxiliares_json, embeddings_json,
            validacoes_json, motivo, ativo, criado_por, criado_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (
            funcionario_id,
            video_path,
            foto_principal_path,
            json.dumps(aux_paths, ensure_ascii=False),
            json.dumps(embeddings, ensure_ascii=False),
            json.dumps(validacoes, ensure_ascii=False),
            form.get("motivo") or "Cadastro facial por mini video",
            user["id"],
            now_iso(),
        ),
    )
    nova = one("SELECT id FROM biometrias_faciais WHERE funcionario_id = ? AND ativo = 1 ORDER BY id DESC LIMIT 1", (funcionario_id,))
    face_embedding = json.dumps(average_embedding(embeddings) or embeddings[0], ensure_ascii=False)
    execute(
        """UPDATE funcionarios
           SET foto_base_path = ?,
               face_image_path = ?,
               foto_facial_cadastrada = 1,
               mini_video_cadastrado = 1,
               reconhecimento_facial_ativo = 1,
               permite_totem_facial = 1,
               permitir_totem_facial = 1,
               face_embedding = ?,
               face_embeddings_json = ?,
               face_video_path = ?,
               updated_at = ?
           WHERE id = ?""",
        (foto_principal_path, foto_principal_path, face_embedding, json.dumps(embeddings, ensure_ascii=False), video_path, now_iso(), funcionario_id),
    )
    audit(
        "atualizar_biometria_facial",
        "funcionarios",
        funcionario_id,
        {
            "biometria_anterior_id": anterior["id"] if anterior else None,
            "biometria_anterior_desativada": bool(anterior),
            "biometria_nova_id": nova["id"] if nova else None,
            "usuario": user["nome"],
            "motivo": form.get("motivo") or "Cadastro facial por mini video",
            "validacoes": validacoes,
        },
    )
    return {
        "biometria_id": nova["id"] if nova else None,
        "foto_principal_path": foto_principal_path,
        "video_path": video_path,
        "embeddings": len(embeddings),
    }, None, None


def image_from_data_url(data_url):
    if not data_url or "," not in data_url:
        return None
    header, encoded = data_url.split(",", 1)
    if "image" not in header:
        return None
    return Image.open(io.BytesIO(base64.b64decode(encoded)))


def perceptual_hash(image, size=16):
    gray = ImageOps.grayscale(image).resize((size, size))
    pixels = list(gray.getdata())
    avg = sum(pixels) / len(pixels)
    return tuple(1 if px >= avg else 0 for px in pixels)


def hash_distance(left, right):
    return sum(1 for a, b in zip(left, right) if a != b)


def facial_similarity(candidate_image, base_path):
    absolute_path = os.path.join(BASE_DIR, base_path)
    if not os.path.exists(absolute_path):
        return None
    try:
        base_image = Image.open(absolute_path)
        candidate_hash = perceptual_hash(candidate_image)
        base_hash = perceptual_hash(base_image)
        distance = hash_distance(candidate_hash, base_hash)
        total = len(candidate_hash)
        return max(0, round((1 - (distance / total)) * 100, 2))
    except Exception:
        app.logger.exception("Erro ao comparar foto facial base %s", base_path)
        return None


def face_recognition_embedding_similarity(candidate_image, base_path):
    if face_recognition is None:
        return None, {
            "engine": "phash_fallback",
            "motivo": "face_recognition_nao_instalado",
            "rostos_captura": None,
            "rostos_base": None,
        }
    absolute_path = os.path.join(BASE_DIR, base_path)
    if not os.path.exists(absolute_path):
        return None, {
            "engine": "face_recognition",
            "motivo": "foto_base_nao_encontrada",
            "rostos_captura": None,
            "rostos_base": None,
        }
    try:
        candidate_rgb = candidate_image.convert("RGB")
        candidate_locations = face_recognition.face_locations(candidate_rgb)
        if not candidate_locations:
            return None, {
                "engine": "face_recognition",
                "motivo": "nenhum_rosto_na_captura",
                "rostos_captura": 0,
                "rostos_base": None,
            }
        candidate_encodings = face_recognition.face_encodings(candidate_rgb, known_face_locations=candidate_locations)
        base_image = face_recognition.load_image_file(absolute_path)
        base_locations = face_recognition.face_locations(base_image)
        if not base_locations:
            return None, {
                "engine": "face_recognition",
                "motivo": "nenhum_rosto_na_foto_base",
                "rostos_captura": len(candidate_locations),
                "rostos_base": 0,
            }
        base_encodings = face_recognition.face_encodings(base_image, known_face_locations=base_locations)
        if not candidate_encodings or not base_encodings:
            return None, {
                "engine": "face_recognition",
                "motivo": "embedding_nao_gerado",
                "rostos_captura": len(candidate_locations),
                "rostos_base": len(base_locations),
            }
        distance = float(face_recognition.face_distance([base_encodings[0]], candidate_encodings[0])[0])
        similarity = max(0, round((1 - distance) * 100, 2))
        return similarity, {
            "engine": "face_recognition",
            "motivo": "comparado",
            "distancia_embedding": round(distance, 4),
            "rostos_captura": len(candidate_locations),
            "rostos_base": len(base_locations),
        }
    except Exception:
        app.logger.exception("Erro no reconhecimento facial por embeddings para base %s", base_path)
        return None, {
            "engine": "face_recognition",
            "motivo": "erro_embedding",
            "rostos_captura": None,
            "rostos_base": None,
        }


def reconhecer_funcionario_por_foto(data_url, min_similarity=50):
    image = image_from_data_url(data_url)
    if image is None:
        app.logger.warning("Reconhecimento facial falhou: imagem recebida invalida")
        return None, 0, {"motivo": "imagem_invalida", "faces_carregadas": 0}
    candidatos = query(
        """SELECT id, nome, matricula, foto_base_path
           FROM funcionarios
           WHERE ativo = 1
             AND permite_totem_facial = 1
             AND reconhecimento_facial_ativo = 1
             AND foto_base_path IS NOT NULL
           ORDER BY nome"""
    )
    engine = "face_recognition" if face_recognition is not None else "phash_fallback"
    app.logger.info(
        "Reconhecimento facial: %s faces cadastradas carregadas; engine=%s; limite_teste=%.2f%%",
        len(candidatos),
        engine,
        min_similarity,
    )
    if face_recognition is None:
        app.logger.warning("Reconhecimento facial real indisponivel: face_recognition/dlib nao instalado. Usando fallback pHash.")
    melhor_funcionario = None
    melhor_similaridade = 0
    comparacoes = []
    melhor_meta = {}
    for funcionario in candidatos:
        similaridade, meta = face_recognition_embedding_similarity(image, funcionario["foto_base_path"])
        if similaridade is None and face_recognition is None:
            similaridade = facial_similarity(image, funcionario["foto_base_path"])
            meta = {
                "engine": "phash_fallback",
                "motivo": "comparado_sem_detector_facial_real",
                "rostos_captura": None,
                "rostos_base": None,
            }
        comparacoes.append({
            "funcionario_id": funcionario["id"],
            "nome": funcionario["nome"],
            "similaridade": similaridade,
            "engine": meta.get("engine"),
            "motivo": meta.get("motivo"),
            "rostos_captura": meta.get("rostos_captura"),
            "rostos_base": meta.get("rostos_base"),
        })
        app.logger.info(
            "Reconhecimento facial: candidato=%s funcionario_id=%s similaridade=%s%% engine=%s rostos_captura=%s rostos_base=%s motivo=%s",
            funcionario["nome"],
            funcionario["id"],
            similaridade,
            meta.get("engine"),
            meta.get("rostos_captura"),
            meta.get("rostos_base"),
            meta.get("motivo"),
        )
        if similaridade is not None and similaridade > melhor_similaridade:
            melhor_funcionario = funcionario
            melhor_similaridade = similaridade
            melhor_meta = meta
    if melhor_funcionario and melhor_similaridade >= min_similarity:
        app.logger.info(
            "Reconhecimento facial aprovado: funcionario=%s funcionario_id=%s similaridade=%.2f%% limite=%.2f%% engine=%s",
            melhor_funcionario["nome"],
            melhor_funcionario["id"],
            melhor_similaridade,
            min_similarity,
            melhor_meta.get("engine"),
        )
        return melhor_funcionario, melhor_similaridade, {
            "motivo": "reconhecido",
            "faces_carregadas": len(candidatos),
            "funcionario_encontrado": melhor_funcionario["nome"],
            "funcionario_mais_parecido": melhor_funcionario["nome"],
            "engine": melhor_meta.get("engine"),
            "rostos_captura": melhor_meta.get("rostos_captura"),
            "rostos_base": melhor_meta.get("rostos_base"),
            "comparacoes": comparacoes,
        }
    motivo = "nenhuma_face_cadastrada" if not candidatos else "similaridade_abaixo_do_limite"
    app.logger.warning(
        "Reconhecimento facial falhou: motivo=%s faces_carregadas=%s melhor_funcionario=%s melhor_similaridade=%.2f%% limite=%.2f%% engine=%s",
        motivo,
        len(candidatos),
        melhor_funcionario["nome"] if melhor_funcionario else None,
        melhor_similaridade,
        min_similarity,
        melhor_meta.get("engine"),
    )
    return None, melhor_similaridade, {
        "motivo": motivo,
        "faces_carregadas": len(candidatos),
        "funcionario_encontrado": melhor_funcionario["nome"] if melhor_funcionario else None,
        "funcionario_mais_parecido": melhor_funcionario["nome"] if melhor_funcionario else None,
        "engine": melhor_meta.get("engine"),
        "rostos_captura": melhor_meta.get("rostos_captura"),
        "rostos_base": melhor_meta.get("rostos_base"),
        "comparacoes": comparacoes,
    }


def save_anexo(file_storage, funcionario_id):
    if not file_storage or not file_storage.filename:
        return None
    safe_name = "".join(ch for ch in file_storage.filename if ch.isalnum() or ch in "._- ").strip() or "anexo"
    extension = os.path.splitext(safe_name)[1].lower()
    if extension and extension not in ALLOWED_ANEXO_EXTENSIONS:
        return None
    filename = f"{funcionario_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe_name}"
    path = os.path.join(ANEXO_DIR, filename)
    file_storage.save(path)
    return os.path.join("anexos_ajustes", filename)


def create_hash(payload):
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def whatsapp_config():
    config = one("SELECT * FROM whatsapp_config WHERE id = 1")
    if not config:
        execute("INSERT OR IGNORE INTO whatsapp_config (id, modo_envio, status_conexao, atualizado_em) VALUES (1, 'teste', 'nao_configurado', ?)", (now_iso(),))
        config = one("SELECT * FROM whatsapp_config WHERE id = 1")
    return config


def evolution_ready_config():
    config = whatsapp_config()
    if not config["evolution_url"] or not config["api_key"] or not config["instancia"]:
        return None, "Integração Evolution incompleta."
    return config, None


def evolution_ready_config_checked():
    config = whatsapp_config()
    if not config["evolution_url"] or not config["api_key"] or not config["instancia"]:
        execute("UPDATE whatsapp_config SET status_conexao = 'nao_configurado', atualizado_em = ? WHERE id = 1", (now_iso(),))
        return None, "Integracao Evolution incompleta."
    return config, None


def evolution_base_url(config):
    return config["evolution_url"].rstrip("/")


def evolution_request(path, method="GET", payload=None, timeout=12):
    config, erro = evolution_ready_config_checked()
    if erro:
        return {"ok": False, "erro": erro, "status": "nao_configurado", "data": None}
    url = f"{evolution_base_url(config)}/{path.lstrip('/')}"
    data = None
    headers = {
        "apikey": config["api_key"],
        "Accept": "application/json",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {"raw": body}
            return {"ok": True, "status": resp.status, "data": parsed}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore") if exc.fp else ""
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return {"ok": False, "status": exc.code, "data": parsed, "erro": body or str(exc)}
    except Exception as exc:
        return {"ok": False, "status": None, "data": None, "erro": str(exc)}


def evolution_instance_status():
    result = evolution_request("/instance/status", "GET")
    config = whatsapp_config()
    if not result["ok"]:
        execute("UPDATE whatsapp_config SET status_conexao = 'erro', atualizado_em = ? WHERE id = 1", (now_iso(),))
        return {"status": "erro", "detail": result.get("erro") or "Falha ao consultar status."}
    data = result.get("data") or {}
    inst = data.get("data") or {}
    connected = bool(inst.get("Connected"))
    logged = bool(inst.get("LoggedIn"))
    if connected and logged:
        status = "conectado"
    elif connected and not logged:
        status = "aguardando_qr_code"
    else:
        status = "desconectado"
    execute("UPDATE whatsapp_config SET status_conexao = ?, atualizado_em = ? WHERE id = 1", (status, now_iso()))
    return {"status": status, "detail": inst, "raw": data}


def evolution_get_qr_code():
    result = evolution_request("/instance/qr", "GET")
    if not result["ok"]:
        execute("UPDATE whatsapp_config SET status_conexao = 'erro', atualizado_em = ? WHERE id = 1", (now_iso(),))
        return result
    data = result.get("data") or {}
    qr = (data.get("data") or {}).get("Qrcode") or (data.get("data") or {}).get("qrcode")
    code = (data.get("data") or {}).get("Code") or (data.get("data") or {}).get("code")
    status = evolution_instance_status()["status"]
    execute(
        "UPDATE whatsapp_config SET status_conexao = ?, qrcode = ?, qrcode_gerado_em = ?, atualizado_em = ? WHERE id = 1",
        (status, qr or code or "", now_iso(), now_iso()),
    )
    return {"ok": True, "qrcode": qr, "code": code, "status": status, "raw": data}


def evolution_connect_instance():
    config, erro = evolution_ready_config_checked()
    if erro:
        return {"ok": False, "erro": erro}
    result = evolution_request("/instance/connect", "POST", {"immediate": True})
    if not result["ok"]:
        execute("UPDATE whatsapp_config SET status_conexao = 'erro', atualizado_em = ? WHERE id = 1", (now_iso(),))
        return {"ok": False, "erro": result.get("erro") or "Falha ao conectar a instancia.", "raw": result.get("data")}
    execute("UPDATE whatsapp_config SET status_conexao = 'aguardando_qr_code', atualizado_em = ? WHERE id = 1", (now_iso(),))
    qr_result = evolution_get_qr_code()
    if qr_result.get("status") == "conectado":
        execute("UPDATE whatsapp_config SET status_conexao = 'conectado', atualizado_em = ? WHERE id = 1", (now_iso(),))
    return qr_result


def evolution_send_text(number, text, delay=0):
    payload = {"number": number, "text": text, "delay": delay}
    result = evolution_request("/send/text", "POST", payload)
    if not result["ok"]:
        # fallback para instâncias que expõem a rota antiga
        result = evolution_request("/message/sendText", "POST", {"number": number, "text": text, "delay": delay})
    return result


def evolution_send_test_message():
    config = whatsapp_config()
    destino = normalizar_whatsapp(config["numero_teste"])
    if not destino:
        return {"ok": False, "erro": "Número de teste não configurado."}
    status = evolution_instance_status()
    if status.get("status") != "conectado":
        return {"ok": False, "erro": f"Instância não conectada. Status atual: {status.get('status')}."}
    fila_id = enfileirar_whatsapp(
        "teste_conexao",
        "Mensagem de teste do sistema de ponto.",
        destino,
        tentar_enviar=False,
    )
    if not fila_id:
        return {"ok": False, "erro": "Nao foi possivel enfileirar a mensagem."}
    ok = enviar_whatsapp_fila(fila_id)
    fila = one("SELECT * FROM whatsapp_fila WHERE id = ?", (fila_id,))
    return {"ok": ok, "fila": dict(fila) if fila else None}


def normalizar_whatsapp(numero):
    digits = "".join(ch for ch in str(numero or "") if ch.isdigit())
    if len(digits) in (10, 11):
        digits = "55" + digits
    return digits


def criar_token_aprovacao(ajuste_id):
    token = secrets.token_urlsafe(32)
    expira_em = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    execute(
        "INSERT INTO aprovacao_tokens (ajuste_id, token, expira_em, criado_em) VALUES (?, ?, ?, ?)",
        (ajuste_id, token, expira_em, now_iso()),
    )
    return token


def link_aprovacao_ajuste(ajuste_id):
    token = criar_token_aprovacao(ajuste_id)
    return url_for("aprovar_ajuste_token", token=token, _external=True)


def whatsapp_destino(numero_real):
    config = whatsapp_config()
    real = normalizar_whatsapp(numero_real)
    if config["modo_envio"] == "teste":
        usado = normalizar_whatsapp(config["numero_teste"])
    else:
        usado = real
    return real, usado


def enfileirar_whatsapp(tipo, conteudo, numero_real, funcionario_id=None, chefia_id=None, usuario_id=None, tentar_enviar=True):
    real, usado = whatsapp_destino(numero_real)
    if not usado:
        status = "erro"
        retorno = "Numero de destino nao informado."
    else:
        status = "pendente"
        retorno = None
    execute(
        """INSERT INTO whatsapp_fila
           (funcionario_id, chefia_id, usuario_id, destinatario_real, destinatario_usado, tipo, conteudo,
            status, tentativas, retorno_api, criado_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (funcionario_id, chefia_id, usuario_id, real, usado, tipo, conteudo, status, retorno, now_iso()),
    )
    fila = one("SELECT id FROM whatsapp_fila ORDER BY id DESC LIMIT 1")
    if tentar_enviar and fila and status == "pendente":
        enviar_whatsapp_fila(fila["id"])
    return fila["id"] if fila else None


def enviar_whatsapp_fila(fila_id):
    item = one("SELECT * FROM whatsapp_fila WHERE id = ?", (fila_id,))
    config = whatsapp_config()
    if not item:
        return False
    if not config["evolution_url"] or not config["api_key"] or not config["instancia"]:
        execute(
            "UPDATE whatsapp_fila SET status = 'erro', tentativas = tentativas + 1, retorno_api = ?, ultimo_erro_em = ? WHERE id = ?",
            ("Evolution API nao configurada.", now_iso(), fila_id),
        )
        execute("UPDATE whatsapp_config SET status_conexao = 'nao_configurado', atualizado_em = ? WHERE id = 1", (now_iso(),))
        return False
    status = evolution_instance_status()
    if status.get("status") != "conectado":
        execute(
            "UPDATE whatsapp_fila SET status = 'erro', tentativas = tentativas + 1, retorno_api = ?, ultimo_erro_em = ? WHERE id = ?",
            (f"Instancia {status.get('status')} para envio.", now_iso(), fila_id),
        )
        return False
    result = evolution_send_text(item["destinatario_usado"], item["conteudo"])
    if result["ok"]:
        retorno = json.dumps(result.get("data") or {}, ensure_ascii=False)[:4000]
        execute(
            "UPDATE whatsapp_fila SET status = 'enviada', tentativas = tentativas + 1, retorno_api = ?, enviado_em = ? WHERE id = ?",
            (retorno, now_iso(), fila_id),
        )
        execute("UPDATE whatsapp_config SET status_conexao = 'conectado', atualizado_em = ? WHERE id = 1", (now_iso(),))
        return True
    retorno = json.dumps(result.get("data") or {}, ensure_ascii=False)[:4000] if result.get("data") is not None else (result.get("erro") or "Falha ao enviar mensagem.")
    execute(
        "UPDATE whatsapp_fila SET status = 'erro', tentativas = tentativas + 1, retorno_api = ?, ultimo_erro_em = ? WHERE id = ?",
        (retorno, now_iso(), fila_id),
    )
    execute("UPDATE whatsapp_config SET status_conexao = 'erro', atualizado_em = ? WHERE id = 1", (now_iso(),))
    return False


def notificar_whatsapp_chefia_ajuste(ajuste_id):
    ajuste = one(
        """SELECT a.*, f.nome funcionario, f.whatsapp funcionario_whatsapp, f.foto_base_path,
                  c.nome chefia, c.whatsapp chefia_whatsapp, c.receber_solicitacoes_whatsapp, c.usuario_id
           FROM ajustes_ponto a
           JOIN funcionarios f ON f.id = a.funcionario_id
           LEFT JOIN chefias c ON c.id = a.chefia_id
           WHERE a.id = ?""",
        (ajuste_id,),
    )
    if not ajuste or not ajuste["receber_solicitacoes_whatsapp"]:
        return None
    link = link_aprovacao_ajuste(ajuste_id)
    data_hora = ajuste["data_hora_solicitada"].replace("T", " ")
    conteudo = (
        "Nova justificativa de ponto para analise.\n"
        f"Funcionario: {ajuste['funcionario']}\n"
        f"Data/hora: {data_hora}\n"
        f"Tipo: {TIPOS_LABEL.get(ajuste['tipo'], ajuste['tipo'])}\n"
        f"Justificativa: {ajuste['justificativa']}\n"
        f"Observacao: {ajuste['observacao'] or '-'}\n"
        f"Link seguro: {link}"
    )
    return enfileirar_whatsapp(
        "chefia_nova_justificativa",
        conteudo,
        ajuste["chefia_whatsapp"],
        funcionario_id=ajuste["funcionario_id"],
        chefia_id=ajuste["chefia_id"],
        usuario_id=ajuste["usuario_id"],
    )


def notificar_whatsapp_funcionario_justificativa_criada(ajuste_id):
    ajuste = one(
        """SELECT a.*, f.nome funcionario, f.whatsapp, f.receber_whatsapp, u.id usuario_id
           FROM ajustes_ponto a
           JOIN funcionarios f ON f.id = a.funcionario_id
           LEFT JOIN usuarios u ON u.funcionario_id = f.id
           WHERE a.id = ? ORDER BY u.id LIMIT 1""",
        (ajuste_id,),
    )
    if not ajuste or not ajuste["receber_whatsapp"]:
        return None
    conteudo = (
        "Justificativa de ponto registrada.\n"
        f"Funcionario: {ajuste['funcionario']}\n"
        f"Data/hora: {ajuste['data_hora_solicitada'].replace('T', ' ')}\n"
        f"Tipo: {TIPOS_LABEL.get(ajuste['tipo'], ajuste['tipo'])}\n"
        f"Justificativa: {ajuste['justificativa']}\n"
        "Status: pendente de analise."
    )
    return enfileirar_whatsapp(
        "funcionario_justificativa_criada",
        conteudo,
        ajuste["whatsapp"],
        funcionario_id=ajuste["funcionario_id"],
        usuario_id=ajuste["usuario_id"],
    )


def notificar_whatsapp_funcionario_resultado(ajuste_id, decisao, parecer):
    ajuste = one(
        """SELECT a.*, f.nome funcionario, f.whatsapp, f.receber_whatsapp, u.id usuario_id
           FROM ajustes_ponto a
           JOIN funcionarios f ON f.id = a.funcionario_id
           LEFT JOIN usuarios u ON u.funcionario_id = f.id
           WHERE a.id = ? ORDER BY u.id LIMIT 1""",
        (ajuste_id,),
    )
    if not ajuste or not ajuste["receber_whatsapp"]:
        return None
    conteudo = (
        "Resultado da justificativa de ponto.\n"
        f"Funcionario: {ajuste['funcionario']}\n"
        f"Data/hora: {ajuste['data_hora_solicitada'].replace('T', ' ')}\n"
        f"Tipo: {TIPOS_LABEL.get(ajuste['tipo'], ajuste['tipo'])}\n"
        f"Resultado: {decisao}\n"
        f"Motivo: {parecer or '-'}"
    )
    return enfileirar_whatsapp(
        f"funcionario_justificativa_{decisao}",
        conteudo,
        ajuste["whatsapp"],
        funcionario_id=ajuste["funcionario_id"],
        usuario_id=ajuste["usuario_id"],
    )


def nav_links(css):
    links = [
        ("dashboard", "bi-grid", "Dashboard"),
    ]
    user = current_user()
    if user and user["perfil"] in ("Administrador Principal", "RH Local"):
        links += [
            ("funcionarios", "bi-people", "Pessoas"),
            ("biometria_facial", "bi-person-bounding-box", "Biometria Facial"),
        ]
    if user:
        links.append(("registrar_ponto", "bi-fingerprint", "Ponto Eletrônico"))
    if user and user["perfil"] in ("Administrador Principal", "RH Local"):
        links += [
            ("locais", "bi-geo-alt", "Locais"),
            ("totens", "bi-tablet", "Totens"),
            ("dispositivos", "bi-phone", "Dispositivos"),
            ("hierarquia", "bi-diagram-3", "Hierarquia"),
        ]
    links.append(("ajustes", "bi-pencil-square", "Solicitações"))
    if user and user["perfil"] in ("Administrador Principal", "Chefia Imediata", "Chefia imediata", "Gestor", "Secretário da Pasta", "Secretário"):
        links.append(("batidas_pendentes", "bi-clock-history", "Aprovações"))
    links.append(("relatorios", "bi-bar-chart", "Relatórios"))
    if user and user["perfil"] == "Administrador Principal":
        links += [
            ("auditoria", "bi-shield-lock", "Auditoria"),
            ("configuracoes", "bi-gear", "Configurações"),
        ]
    return "".join(f'<a class="{css}" href="{url_for(endpoint)}"><i class="bi {icon}"></i> {label}</a>' for endpoint, icon, label in links)


def page(body, title=None, **ctx):
    body_html = render_template_string(
        body,
        title=title,
        user=current_user(),
        nav_links=nav_links,
        csrf_input=csrf_input,
        **ctx,
    )
    return render_template(
        "base.html",
        body=body_html,
        title=title,
        user=current_user(),
        nav_links=nav_links,
        csrf_input=csrf_input,
    )


def page_template(template_name, title=None, **ctx):
    return render_template(
        template_name,
        title=title,
        user=current_user(),
        nav_links=nav_links,
        csrf_input=csrf_input,
        **ctx,
    )


@app.route("/manifest.webmanifest")
def manifest():
    return Response(json.dumps({
        "name": "Ponto Eletrônico REP-P",
        "short_name": "Ponto REP-P",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f8fafc",
        "theme_color": "#155eef",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }, ensure_ascii=False), mimetype="application/manifest+json")


@app.route("/sw.js")
def sw():
    return Response("""
self.addEventListener('install', event => self.skipWaiting());
self.addEventListener('fetch', event => {});
""", mimetype="application/javascript")


@app.route("/icon-192.png")
@app.route("/icon-512.png")
def icon():
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    return Response(png, mimetype="image/png")


@app.route("/")
def root():
    return redirect(url_for("dashboard" if session.get("user_id") else "login"))


@app.route("/diagnostico-mobile")
def diagnostico_mobile():
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
    https_ativo = request.is_secure or forwarded_proto.lower() == "https"
    return page_template(
        "pages/diagnostico_mobile.html",
        title="Diagnostico mobile",
        https_ativo=https_ativo,
        host=request.host,
        user_agent=request.headers.get("User-Agent", ""),
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        user = one("SELECT * FROM usuarios WHERE login = ? AND ativo = 1", (request.form["login"],))
        if user and verify_password(user["senha"], request.form["senha"]):
            session["user_id"] = user["id"]
            audit("login", "usuarios", user["id"])
            return redirect(url_for("dashboard"))
        error = "Login ou senha inválidos."
    return page_template('pages/login.html', title="Login", error=error)


@app.route("/logout")
@login_required
def logout():
    audit("logout", "usuarios", session["user_id"])
    session.clear()
    return redirect(url_for("login"))


@app.route("/tema")
@login_required
def toggle_theme():
    session["theme"] = "dark" if session.get("theme", "light") == "light" else "light"
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    today = date.today()
    inicio_hoje, fim_hoje = day_bounds(today)
    user = current_user()
    filtros_funcionarios = ["f.ativo = 1"]
    params_funcionarios = []
    filtros_marcacoes = ["m.data_hora >= ?", "m.data_hora < ?"]
    params_marcacoes = [inicio_hoje, fim_hoje]
    if user["perfil"] == "RH Local" and user["rh_local_id"]:
        filtros_funcionarios.append("f.rh_local_id = ?")
        params_funcionarios.append(user["rh_local_id"])
        filtros_marcacoes.append("f.rh_local_id = ?")
        params_marcacoes.append(user["rh_local_id"])
    elif is_funcionario_profile(user) and user["funcionario_id"]:
        filtros_funcionarios.append("f.id = ?")
        params_funcionarios.append(user["funcionario_id"])
        filtros_marcacoes.append("f.id = ?")
        params_marcacoes.append(user["funcionario_id"])
    funcionarios_count = one(f"SELECT COUNT(*) total FROM funcionarios f WHERE {' AND '.join(filtros_funcionarios)}", params_funcionarios)["total"]
    marcacoes_hoje = one(
        f"""SELECT COUNT(*) total
            FROM marcacoes m
            JOIN funcionarios f ON f.id = m.funcionario_id
            WHERE {' AND '.join(filtros_marcacoes)}""",
        params_marcacoes,
    )["total"]
    funcionarios_sem_ponto_hoje = query(
        f"""SELECT f.id, f.nome, f.matricula, f.cargo, c.nome chefia_nome
           FROM funcionarios f
           LEFT JOIN (
               SELECT DISTINCT m.funcionario_id
               FROM marcacoes m
               JOIN funcionarios f ON f.id = m.funcionario_id
               WHERE {' AND '.join(filtros_marcacoes)}
           ) m ON m.funcionario_id = f.id
           LEFT JOIN chefias c ON c.id = f.chefia_id
           WHERE {' AND '.join(filtros_funcionarios)} AND m.funcionario_id IS NULL
           ORDER BY f.nome""",
        params_marcacoes + params_funcionarios,
    )
    ajustes_pendentes = one("SELECT COUNT(*) total FROM ajustes_ponto WHERE status = 'pendente'")["total"]
    batidas_pendentes = one("SELECT COUNT(*) total FROM marcacoes WHERE status_aprovacao = 'pendente'")["total"]
    pendentes = ajustes_pendentes + batidas_pendentes
    linhas = build_report(today.replace(day=1), today, "")
    saldo = sum(row["saldo_min"] for row in linhas)
    extras = sum(row["extras_min"] for row in linhas)
    faltas = sum(1 for row in linhas if row["falta"])
    atrasos = sum(row["atraso_min"] for row in linhas)
    return page_template(
        'pages/dashboard.html',
        title="Dashboard",
        funcionarios_count=funcionarios_count,
        marcacoes_hoje=marcacoes_hoje,
        funcionarios_sem_ponto_hoje=funcionarios_sem_ponto_hoje,
        funcionarios_sem_ponto_count=len(funcionarios_sem_ponto_hoje),
        pendentes=pendentes,
        saldo=saldo,
        extras=extras,
        faltas=faltas,
        atrasos=atrasos,
        fmt=fmt_minutes,
    )


def allowed_funcionarios_for_user(user):
    if is_funcionario_profile(user):
        if not user["funcionario_id"]:
            return []
        return query("SELECT * FROM funcionarios WHERE id = ? AND ativo = 1", (user["funcionario_id"],))
    if user["perfil"] in ("RH", "RH Local") and user["rh_local_id"]:
        return query("SELECT * FROM funcionarios WHERE rh_local_id = ? AND ativo = 1 ORDER BY nome", (user["rh_local_id"],))
    if user["perfil"] in ("Chefia Imediata", "Chefia imediata", "Gestor") and user["chefia_id"]:
        return query("SELECT * FROM funcionarios WHERE chefia_id = ? AND ativo = 1 ORDER BY nome", (user["chefia_id"],))
    if user["perfil"] in ("Secretário da Pasta", "Secretário") and user["secretario_id"]:
        return query("SELECT * FROM funcionarios WHERE secretario_id = ? AND ativo = 1 ORDER BY nome", (user["secretario_id"],))
    return query("SELECT * FROM funcionarios WHERE ativo = 1 ORDER BY nome")


def audit_totem_geolocalizacao(funcionario_id, aprovado, detalhes, marcacao_id=None):
    execute(
        "INSERT INTO auditoria (usuario_id, acao, entidade, entidade_id, detalhes, ip, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            None,
            "totem_facial_geolocalizacao",
            "marcacoes" if marcacao_id else "totem_facial",
            marcacao_id,
            json.dumps({
                "origem": ORIGEM_TOTEM_FACIAL,
                "funcionario_id": funcionario_id,
                "aprovado": aprovado,
                **detalhes,
            }, ensure_ascii=False),
            request.remote_addr if request else "",
            now_iso(),
        ),
    )


def notificar_chefia_batida_pendente(funcionario, tipo, horario_previsto):
    chefia = one("""SELECT c.usuario_id
                    FROM funcionarios f
                    LEFT JOIN chefias c ON c.id = f.chefia_id
                    WHERE f.id = ?""", (funcionario["id"],))
    if chefia and chefia["usuario_id"]:
        execute(
            "INSERT INTO notificacoes (usuario_id, titulo, mensagem, criado_em) VALUES (?, ?, ?, ?)",
            (
                chefia["usuario_id"],
                "Batida fora do horario pendente",
                f"{funcionario['nome']} solicitou {TIPOS_LABEL[tipo]} fora do horario previsto ({horario_previsto}).",
                now_iso(),
            ),
        )


def validar_totem_facial(funcionario, totem_id, latitude, longitude, form=None):
    detalhes = {
        "totem_id": totem_id,
        "latitude_capturada": latitude,
        "longitude_capturada": longitude,
        "local_validado_id": None,
        "local_validado": None,
        "distancia_metros": None,
        "motivo_bloqueio": None,
    }
    if not latitude or not longitude:
        detalhes["motivo_bloqueio"] = "GPS obrigatorio nao capturado"
        return False, None, detalhes
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        detalhes["motivo_bloqueio"] = "Coordenadas invalidas"
        return False, None, detalhes

    totem = one(
        """SELECT t.*, l.nome local_nome
           FROM totens t
           LEFT JOIN locais_trabalho l ON l.id = t.local_id
           WHERE t.id = ? AND t.ativo = 1""",
        (totem_id,),
    )
    if not totem:
        detalhes["motivo_bloqueio"] = "Totem inativo ou nao cadastrado"
        return False, None, detalhes

    distancia_totem = haversine_m(lat, lon, totem["latitude"], totem["longitude"])
    detalhes.update({
        "totem_id": totem["id"],
        "totem_nome": totem["nome"],
        "local_validado_id": totem["local_id"],
        "local_validado": totem["local_nome"],
        "distancia_metros": round(distancia_totem, 2),
    })
    if distancia_totem > float(totem["raio_metros"]):
        detalhes["motivo_bloqueio"] = "Local nao autorizado para registro de ponto"
        return False, totem, detalhes

    geo_ok, detalhes_continuos = validar_geolocalizacao_continua(form or request.form, totem["latitude"], totem["longitude"], totem["raio_metros"])
    detalhes["validacao_continua"] = detalhes_continuos
    if not geo_ok:
        detalhes["motivo_bloqueio"] = detalhes_continuos["motivo_bloqueio"] or GEO_BLOCK_MESSAGE
        return False, totem, detalhes
    detalhes.update({
        "latitude_capturada": detalhes_continuos["latitude"],
        "longitude_capturada": detalhes_continuos["longitude"],
        "precisao": detalhes_continuos["precisao"],
        "distancia_metros": detalhes_continuos["distancia_metros"],
        "geo_primeira_leitura_em": detalhes_continuos["primeira_leitura_em"],
        "geo_ultima_leitura_em": detalhes_continuos["ultima_leitura_em"],
        "geo_tempo_validacao_seg": detalhes_continuos["tempo_validacao_seg"],
        "geo_qtd_leituras": detalhes_continuos["qtd_leituras"],
        "geo_falha_permissao": detalhes_continuos["falha_permissao"],
        "geo_mock_suspeito": detalhes_continuos["mock_suspeito"],
    })

    if not funcionario["permite_totem_facial"]:
        detalhes["motivo_bloqueio"] = "Funcionario sem permissao para Totem Facial"
        return False, totem, detalhes

    if totem["local_id"]:
        autorizado = one(
            """SELECT id FROM funcionario_locais_autorizados
               WHERE funcionario_id = ? AND local_id = ? AND ativo = 1""",
            (funcionario["id"], totem["local_id"]),
        )
        if not autorizado:
            detalhes["motivo_bloqueio"] = "Funcionario nao autorizado para o local do totem"
            return False, totem, detalhes

    return True, totem, detalhes


@app.route("/terminal-ponto")
@app.route("/totem-facial")
def totem_facial():
    funcionarios_rows = query(
        """SELECT f.id, f.nome, f.matricula, j.nome jornada_nome
           FROM funcionarios f
           JOIN jornadas j ON j.id = f.jornada_id
           WHERE f.ativo = 1 AND f.permite_totem_facial = 1
           ORDER BY f.nome"""
    )
    totens_rows = query(
        """SELECT t.*, l.nome local_nome
           FROM totens t
           LEFT JOIN locais_trabalho l ON l.id = t.local_id
           WHERE t.ativo = 1
           ORDER BY t.nome"""
    )
    return page_template(
        "pages/totem_facial.html",
        title="Totem Facial",
        funcionarios_rows=funcionarios_rows,
        totens_rows=totens_rows,
    )


@app.route("/totem-facial/funcionarios-face")
def totem_facial_funcionarios_face():
    rows = query(
        """SELECT f.id, f.nome, f.matricula, f.foto_base_path, f.face_embedding,
                  f.updated_at, b.id biometria_id, b.embeddings_json, b.foto_principal_path, b.criado_em biometria_criada_em
           FROM funcionarios f
           LEFT JOIN biometrias_faciais b ON b.id = (
               SELECT id FROM biometrias_faciais
               WHERE funcionario_id = f.id AND ativo = 1
               ORDER BY id DESC LIMIT 1
           )
           WHERE f.ativo = 1
             AND f.permite_totem_facial = 1
             AND f.reconhecimento_facial_ativo = 1
             AND (b.id IS NOT NULL OR f.foto_base_path IS NOT NULL)
           ORDER BY f.nome"""
    )
    funcionarios = []
    for row in rows:
        embeddings = []
        if row["embeddings_json"]:
            try:
                embeddings = json.loads(row["embeddings_json"])
            except json.JSONDecodeError:
                embeddings = []
        elif row["face_embedding"]:
            try:
                embedding = json.loads(row["face_embedding"])
                if isinstance(embedding, list) and len(embedding) == 128:
                    embeddings = [embedding]
            except json.JSONDecodeError:
                embeddings = []
        funcionarios.append({
            "id": row["id"],
            "nome": row["nome"],
            "matricula": row["matricula"],
            "foto_url": url_for("foto_facial_base", funcionario_id=row["id"], v=row["updated_at"] or row["biometria_criada_em"] or row["biometria_id"] or row["id"]),
            "biometria_id": row["biometria_id"],
            "biometria_criada_em": row["biometria_criada_em"],
            "updated_at": row["updated_at"],
            "embeddings": embeddings,
        })
    app.logger.info("Totem facial frontend: %s fotos base liberadas para face-api.js", len(funcionarios))
    response = app.response_class(
        json.dumps({
        "funcionarios": funcionarios,
        "total": len(funcionarios),
        "total_fotos_base_carregadas": len(funcionarios),
        }, ensure_ascii=False),
        mimetype="application/json",
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/totem-facial/foto-base/<int:funcionario_id>")
def foto_facial_base(funcionario_id):
    funcionario = one(
        """SELECT foto_base_path FROM funcionarios
           WHERE id = ? AND ativo = 1
             AND permite_totem_facial = 1
             AND reconhecimento_facial_ativo = 1
             AND foto_base_path IS NOT NULL""",
        (funcionario_id,),
    )
    if not funcionario:
        return Response("Foto facial nao encontrada", status=404)
    foto_path = funcionario["foto_base_path"].replace("\\", "/")
    directory, filename = os.path.split(foto_path)
    if directory != "selfies":
        return Response("Caminho invalido", status=404)
    response = send_from_directory(SELFIE_DIR, filename)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/totem-facial/registrar-teste", methods=["POST"])
def totem_facial_registrar_teste():
    try:
        funcionario_id = request.form.get("funcionario_id")
        totem_id = request.form.get("totem_id")
        latitude = request.form.get("latitude")
        longitude = request.form.get("longitude")
        selfie = request.form.get("selfie")
        liveness_score = float(request.form.get("liveness_score") or 0)
        similaridade_frontend = request.form.get("similaridade_facial")
        distancia_facial = request.form.get("distancia_facial")
        reconhecimento_engine = request.form.get("reconhecimento_engine") or "face-api.js"
        dispositivo_id = request.form.get("dispositivo_id") or "totem-facial-teste"

        if not totem_id:
            app.logger.warning("Totem facial sem totem_id")
            return {"erro": "Selecione o totem autorizado."}, 400
        if not selfie:
            app.logger.warning("Totem facial teste sem imagem capturada")
            return {"erro": "Imagem da camera nao capturada."}, 400
        if liveness_score < 6:
            app.logger.warning("Totem facial bloqueado por prova de vida insuficiente: %.2f", liveness_score)
            return {"ok": False, "reconhecido": False, "erro": "Prova de vida insuficiente. Aguardando movimento real."}, 202

        similaridade_facial = float(similaridade_frontend) if similaridade_frontend not in (None, "") else None
        diagnostico_facial = {
            "engine": reconhecimento_engine,
            "distancia_facial": float(distancia_facial) if distancia_facial not in (None, "") else None,
            "motivo": "reconhecido_no_frontend" if funcionario_id else "sem_funcionario_frontend",
        }
        if not funcionario_id:
            reconhecido, similaridade_facial, diagnostico_facial = reconhecer_funcionario_por_foto(selfie)
            if not reconhecido:
                mensagem_falha = "Rosto encontrado, porem nao corresponde a nenhum funcionario cadastrado."
                if diagnostico_facial.get("motivo") == "nenhuma_face_cadastrada":
                    mensagem_falha = "Nenhuma foto facial base cadastrada para reconhecimento."
                return {
                    "ok": False,
                    "reconhecido": False,
                    "similaridade_facial": similaridade_facial,
                    "diagnostico_facial": diagnostico_facial,
                    "mensagem": mensagem_falha,
                }, 202
            funcionario_id = reconhecido["id"]

        funcionario = one(
            """SELECT f.*, l.latitude, l.longitude,
                      j.entrada, j.saida_almoco, j.retorno_almoco, j.saida_final,
                      j.tolerancia_minutos, j.tolerancia_antes_minutos, j.tolerancia_atraso_minutos
               FROM funcionarios f
               JOIN locais_trabalho l ON l.id = f.local_id
               JOIN jornadas j ON j.id = f.jornada_id
               WHERE f.id = ? AND f.ativo = 1 AND l.ativo = 1""",
            (funcionario_id,),
        )
        if not funcionario:
            app.logger.warning("Totem facial teste com funcionario invalido: %s", funcionario_id)
            return {"erro": "Funcionario ativo nao encontrado."}, 404
        if similaridade_facial is None:
            _func_reconhecido, similaridade_facial, diagnostico_facial = reconhecer_funcionario_por_foto(selfie)

        tipo = next_tipo(funcionario["id"])
        data_hora_batida = now_iso()
        fora_horario, horario_previsto, diferenca_minutos = fora_da_tolerancia(funcionario, tipo, data_hora_batida)
        if fora_horario:
            app.logger.warning(
                "Totem facial bloqueado fora do horario funcionario=%s funcionario_id=%s tipo=%s previsto=%s diferenca=%s",
                funcionario["nome"],
                funcionario["id"],
                tipo,
                horario_previsto,
                diferenca_minutos,
            )
            return {
                "ok": False,
                "erro": "Fora do horario permitido. Registre manualmente com justificativa para aprovacao da chefia.",
                "acao": "justificar_no_app",
                "fora_horario": True,
                "tipo": TIPOS_LABEL[tipo],
                "horario_previsto": horario_previsto,
                "diferenca_minutos": diferenca_minutos,
            }, 403

        geolocalizacao_ok, totem, detalhes_geo = validar_totem_facial(funcionario, totem_id, latitude, longitude, request.form)
        detalhes_geo["similaridade_facial"] = similaridade_facial
        detalhes_geo["diagnostico_facial"] = diagnostico_facial
        detalhes_geo["liveness_score"] = round(liveness_score, 2)
        detalhes_geo["horario"] = now_iso()
        if not geolocalizacao_ok:
            audit_totem_geolocalizacao(funcionario["id"], False, detalhes_geo)
            app.logger.warning("Totem facial bloqueado por geolocalizacao: %s", detalhes_geo)
            return {
                "erro": GEO_BLOCK_MESSAGE,
                "detalhes": detalhes_geo,
            }, 403

        limite_duplicidade = (datetime.now() - timedelta(minutes=2)).isoformat(sep=" ")
        duplicada = one(
            """SELECT id, nsr, data_hora FROM marcacoes
               WHERE funcionario_id = ?
                 AND origem_normalizada = ?
                 AND data_hora >= ?
               ORDER BY data_hora DESC
               LIMIT 1""",
            (funcionario["id"], ORIGEM_TOTEM_FACIAL, limite_duplicidade),
        )
        if duplicada:
            detalhes_geo["motivo_bloqueio"] = "Ponto ja registrado recentemente"
            detalhes_geo["marcacao_recente_id"] = duplicada["id"]
            detalhes_geo["marcacao_recente_data_hora"] = duplicada["data_hora"]
            audit_totem_geolocalizacao(funcionario["id"], False, detalhes_geo, duplicada["id"])
            app.logger.info(
                "Totem facial bloqueou duplicidade funcionario=%s funcionario_id=%s marcacao_id=%s",
                funcionario["nome"],
                funcionario["id"],
                duplicada["id"],
            )
            return {
                "ok": False,
                "duplicado": True,
                "mensagem": "Ponto já registrado recentemente",
                "data_hora": duplicada["data_hora"],
                "funcionario": funcionario["nome"].title(),
                "detalhes": detalhes_geo,
            }, 409

        nsr = datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6].upper()
        selfie_path = save_selfie(selfie, funcionario["id"])
        origem = ORIGEM_TOTEM_FACIAL
        payload = {
            "nsr": nsr,
            "funcionario_id": funcionario["id"],
            "tipo": tipo,
            "data_hora": data_hora_batida,
            "origem": origem,
            "dispositivo_id": dispositivo_id,
            "latitude": detalhes_geo["latitude_capturada"],
            "longitude": detalhes_geo["longitude_capturada"],
            "precisao": detalhes_geo["precisao"],
            "geo_primeira_leitura_em": detalhes_geo["geo_primeira_leitura_em"],
            "geo_ultima_leitura_em": detalhes_geo["geo_ultima_leitura_em"],
        }
        hash_registro = create_hash(payload)

        execute(
            """INSERT INTO marcacoes
               (nsr, funcionario_id, tipo, data_hora, latitude, longitude, precisao, distancia_metros,
                dentro_cerca, selfie_path, dispositivo_id, user_agent, ip, hash_registro,
                status_aprovacao, origem, origem_normalizada, totem_id, local_validacao_id,
                geolocalizacao_status, distancia_validacao_metros, geo_primeira_leitura_em,
                geo_ultima_leitura_em, geo_tempo_validacao_seg, geo_qtd_leituras,
                geo_falha_permissao, geo_mock_suspeito, geo_leituras_json, criado_em)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                nsr,
                funcionario["id"],
                tipo,
                data_hora_batida,
                detalhes_geo["latitude_capturada"],
                detalhes_geo["longitude_capturada"],
                detalhes_geo["precisao"],
                detalhes_geo["distancia_metros"],
                1,
                selfie_path,
                dispositivo_id,
                request.headers.get("User-Agent", ""),
                request.remote_addr,
                hash_registro,
                "normal",
                origem,
                origem,
                totem["id"],
                totem["local_id"],
                "aprovado",
                detalhes_geo["distancia_metros"],
                detalhes_geo["geo_primeira_leitura_em"],
                detalhes_geo["geo_ultima_leitura_em"],
                detalhes_geo["geo_tempo_validacao_seg"],
                detalhes_geo["geo_qtd_leituras"],
                1 if detalhes_geo["geo_falha_permissao"] else 0,
                1 if detalhes_geo["geo_mock_suspeito"] else 0,
                json.dumps(detalhes_geo["validacao_continua"]["leituras"], ensure_ascii=False),
                now_iso(),
            ),
        )
        marcacao = one("SELECT * FROM marcacoes WHERE nsr = ?", (nsr,))
        audit_totem_geolocalizacao(funcionario["id"], True, detalhes_geo, marcacao["id"])
        app.logger.info(
            "Totem facial registrou marcacao id=%s funcionario=%s funcionario_id=%s nsr=%s similaridade_facial=%.2f%%",
            marcacao["id"],
            funcionario["nome"],
            funcionario["id"],
            nsr,
            float(similaridade_facial or 0),
        )
        return {
            "ok": True,
            "mensagem": "Ponto registrado com sucesso",
            "data_hora": data_hora_batida,
            "nsr": nsr,
            "funcionario": funcionario["nome"].title(),
            "funcionario_id": funcionario["id"],
            "tipo": TIPOS_LABEL[tipo],
            "origem": origem,
            "similaridade_facial": similaridade_facial,
            "diagnostico_facial": diagnostico_facial,
        }
    except Exception:
        app.logger.exception("Erro ao registrar ponto de teste no Totem Facial")
        return {"erro": "Erro ao registrar ponto pelo totem."}, 500


@app.route("/registrar", methods=["GET", "POST"])
@login_required
def registrar_ponto():
    user = current_user()
    funcionario_logado = one("""SELECT f.*, j.nome jornada_nome, j.entrada, j.saida_almoco, j.retorno_almoco, j.saida_final
                                FROM funcionarios f
                                JOIN jornadas j ON j.id = f.jornada_id
                                WHERE f.id = ? AND f.ativo = 1""", (user["funcionario_id"],)) if user["funcionario_id"] else None
    proximo_tipo = next_tipo(funcionario_logado["id"]) if funcionario_logado else "entrada"
    erro = None
    if request.method == "POST":
        funcionario = one("SELECT * FROM funcionarios WHERE id = ? AND ativo = 1", (request.form["funcionario_id"],))
        if not user["funcionario_id"] or not funcionario or funcionario["id"] != user["funcionario_id"]:
            return page("<div class='alert alert-danger'>Acesso negado.</div>"), 403
        jornada = one("SELECT * FROM jornadas WHERE id = ?", (funcionario["jornada_id"],))
        tipo = next_tipo(funcionario["id"])
        if not jornada["saida_almoco"] and tipo in ("saida_almoco", "retorno_almoco"):
            erro = "A jornada vinculada ao funcionário não possui intervalo. Selecione Entrada ou Saída final."
            return page("<div class='alert alert-danger'>{{ erro }}</div><a class='btn btn-primary' href='{{ url_for(\"registrar_ponto\") }}'>Voltar</a>", title="Registrar ponto", erro=erro)
        local = one("SELECT * FROM locais_trabalho WHERE id = ? AND ativo = 1", (funcionario["local_id"],))
        lat = request.form.get("latitude")
        lon = request.form.get("longitude")
        if not lat or not lon:
            erro = "GPS obrigatório. Autorize a localização para registrar o ponto."
        elif not request.form.get("selfie"):
            erro = "Selfie obrigatória. Autorize a câmera para registrar o ponto."
        else:
            geo_ok, detalhes_geo = validar_geolocalizacao_continua(request.form, local["latitude"], local["longitude"], local["raio_metros"])
            distancia = detalhes_geo["distancia_metros"] if detalhes_geo["distancia_metros"] is not None else haversine_m(lat, lon, local["latitude"], local["longitude"])
            if not geo_ok:
                erro = GEO_BLOCK_MESSAGE
                audit("registrar_ponto_bloqueado_geo", "funcionarios", funcionario["id"], detalhes_geo)
            elif distancia > local["raio_metros"]:
                erro = f"Fora da cerca geográfica. Distância atual: {distancia:.0f} m. Raio permitido: {local['raio_metros']} m."
            else:
                nsr = datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6].upper()
                selfie_path = save_selfie(request.form.get("selfie"), funcionario["id"])
                data_hora_batida = now_iso()
                fora_horario, horario_previsto, diferenca_minutos = fora_da_tolerancia(jornada, tipo, data_hora_batida)
                justificativa_fora_horario = request.form.get("justificativa_fora_horario", "").strip()
                status_aprovacao = "pendente" if fora_horario else "normal"
                if fora_horario and not justificativa_fora_horario:
                    erro = "Batida fora do horário ou da tolerância. Informe uma justificativa para registrar."
                    return page("<div class='alert alert-danger'>{{ erro }}</div><a class='btn btn-primary' href='{{ url_for(\"registrar_ponto\") }}'>Voltar</a>", title="Registrar ponto", erro=erro)
                payload = {
                    "nsr": nsr,
                    "funcionario_id": funcionario["id"],
                    "tipo": tipo,
                    "data_hora": data_hora_batida,
                    "latitude": detalhes_geo["latitude"],
                    "longitude": detalhes_geo["longitude"],
                    "precisao": detalhes_geo["precisao"],
                    "dispositivo_id": request.form.get("dispositivo_id"),
                    "status_aprovacao": status_aprovacao,
                    "geo_primeira_leitura_em": detalhes_geo["primeira_leitura_em"],
                    "geo_ultima_leitura_em": detalhes_geo["ultima_leitura_em"],
                }
                hash_registro = create_hash(payload)
                execute(
                    """INSERT INTO marcacoes
                       (nsr, funcionario_id, tipo, data_hora, latitude, longitude, precisao, distancia_metros,
                        dentro_cerca, selfie_path, dispositivo_id, user_agent, ip, hash_registro,
                        justificativa_fora_horario, status_aprovacao, horario_previsto, origem, origem_normalizada,
                        geolocalizacao_status, distancia_validacao_metros, geo_primeira_leitura_em,
                        geo_ultima_leitura_em, geo_tempo_validacao_seg, geo_qtd_leituras,
                        geo_falha_permissao, geo_mock_suspeito, geo_leituras_json, criado_em)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (nsr, funcionario["id"], tipo, payload["data_hora"], detalhes_geo["latitude"], detalhes_geo["longitude"], detalhes_geo["precisao"],
                     distancia, 1, selfie_path, request.form.get("dispositivo_id"), request.headers.get("User-Agent", ""),
                     request.remote_addr, hash_registro, justificativa_fora_horario, status_aprovacao, horario_previsto,
                     ORIGEM_MANUAL, ORIGEM_MANUAL, "aprovado", distancia, detalhes_geo["primeira_leitura_em"],
                     detalhes_geo["ultima_leitura_em"], detalhes_geo["tempo_validacao_seg"], detalhes_geo["qtd_leituras"],
                     1 if detalhes_geo["falha_permissao"] else 0, 1 if detalhes_geo["mock_suspeito"] else 0,
                     json.dumps(detalhes_geo["leituras"], ensure_ascii=False), now_iso()),
                )
                marcacao = one("SELECT * FROM marcacoes WHERE nsr = ?", (nsr,))
                if status_aprovacao == "pendente":
                    chefia = one("""SELECT c.usuario_id FROM funcionarios f
                                    LEFT JOIN chefias c ON c.id = f.chefia_id
                                    WHERE f.id = ?""", (funcionario["id"],))
                    if chefia and chefia["usuario_id"]:
                        execute(
                            "INSERT INTO notificacoes (usuario_id, titulo, mensagem, criado_em) VALUES (?, ?, ?, ?)",
                            (chefia["usuario_id"], "Batida fora do horário pendente",
                             f"{funcionario['nome']} registrou {TIPOS_LABEL[tipo]} fora do horário previsto ({horario_previsto}).",
                             now_iso()),
                        )
                audit("registrar_ponto", "marcacoes", marcacao["id"], {"nsr": nsr, "tipo": tipo, "status": status_aprovacao, "fora_tolerancia_min": diferenca_minutos})
                return redirect(url_for("comprovante", marcacao_id=marcacao["id"]))

    return page_template('pages/registrar_ponto.html', title="Registrar ponto", funcionario=funcionario_logado, labels=TIPOS_LABEL, proximo_tipo=proximo_tipo, erro=erro)


@app.route("/api/proxima-marcacao/<int:funcionario_id>")
@login_required
def api_proxima_marcacao(funcionario_id):
    user = current_user()
    if user["perfil"] == "Funcionário" and user["funcionario_id"] != funcionario_id:
        return {"erro": "Acesso negado"}, 403
    tipo = next_tipo(funcionario_id)
    return {"tipo": tipo, "label": TIPOS_LABEL[tipo]}


@app.route("/comprovante/<int:marcacao_id>")
@login_required
def comprovante(marcacao_id):
    m = one("""SELECT m.*, f.nome, f.cpf, f.matricula FROM marcacoes m
               JOIN funcionarios f ON f.id = m.funcionario_id WHERE m.id = ?""", (marcacao_id,))
    return page_template('pages/comprovante.html', title="Comprovante", m=m, labels=TIPOS_LABEL)


@app.route("/anexos-ajustes/<path:filename>")
@login_required
def anexo_ajuste(filename):
    return send_from_directory(ANEXO_DIR, os.path.basename(filename))


@app.route("/empresas", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal")
def empresas():
    if request.method == "POST":
        execute("INSERT INTO empresas (razao_social, nome_fantasia, cnpj, criado_em) VALUES (?, ?, ?, ?)",
                (request.form["razao_social"], request.form.get("nome_fantasia"), request.form["cnpj"], now_iso()))
        audit("criar", "empresas", detalhes={"cnpj": request.form["cnpj"]})
        return redirect(url_for("empresas"))
    rows = query("SELECT * FROM empresas ORDER BY razao_social")
    return simple_crud_page("Empresas", rows, ["razao_social", "nome_fantasia", "cnpj"], ["Razão social", "Nome fantasia", "CNPJ"])


@app.route("/rh-locais", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal")
def rh_locais():
    empresas_rows = query("SELECT * FROM empresas WHERE ativa = 1 ORDER BY razao_social")
    if request.method == "POST":
        execute("INSERT INTO rh_locais (empresa_id, nome, email, unidade) VALUES (?, ?, ?, ?)",
                (request.form["empresa_id"], request.form["nome"], request.form.get("email"), request.form.get("unidade")))
        audit("criar", "rh_locais", detalhes={"nome": request.form["nome"]})
        return redirect(url_for("rh_locais"))
    rows = query("""SELECT rh.*, e.nome_fantasia empresa FROM rh_locais rh
                    JOIN empresas e ON e.id = rh.empresa_id ORDER BY rh.nome""")
    return page_template('pages/rh_locais.html', title="RH Local", rows=rows, empresas_rows=empresas_rows)


@app.route("/chefias", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal")
def chefias():
    empresas_rows = query("SELECT * FROM empresas WHERE ativa = 1 ORDER BY razao_social")
    usuarios_rows = query("SELECT * FROM usuarios WHERE perfil IN ('Gestor', 'Chefia Imediata') AND ativo = 1 ORDER BY nome")
    if request.method == "POST":
        usuario_id = request.form.get("usuario_id") or None
        execute(
            """INSERT INTO chefias
               (empresa_id, nome, email, cargo, whatsapp, receber_solicitacoes_whatsapp, usuario_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                request.form["empresa_id"],
                request.form["nome"],
                request.form.get("email"),
                request.form.get("cargo"),
                normalizar_whatsapp(request.form.get("whatsapp")),
                1 if request.form.get("receber_solicitacoes_whatsapp") == "1" else 0,
                usuario_id,
            ),
        )
        audit("criar", "chefias", detalhes={"nome": request.form["nome"]})
        return redirect(url_for("chefias"))
    rows = query("""SELECT c.*, e.nome_fantasia empresa, u.nome usuario FROM chefias c
                    JOIN empresas e ON e.id = c.empresa_id
                    LEFT JOIN usuarios u ON u.id = c.usuario_id
                    ORDER BY c.nome""")
    return page_template('pages/chefias.html', title="Chefias", rows=rows, empresas_rows=empresas_rows, usuarios_rows=usuarios_rows)


@app.route("/secretarios", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal")
def secretarios():
    empresas_rows = query("SELECT * FROM empresas WHERE ativa = 1 ORDER BY razao_social")
    if request.method == "POST":
        execute("INSERT INTO secretarios_pastas (empresa_id, pasta, nome, email) VALUES (?, ?, ?, ?)",
                (request.form["empresa_id"], request.form["pasta"], request.form["nome"], request.form.get("email")))
        audit("criar", "secretarios_pastas", detalhes={"pasta": request.form["pasta"], "nome": request.form["nome"]})
        return redirect(url_for("secretarios"))
    rows = query("""SELECT sp.*, e.nome_fantasia empresa FROM secretarios_pastas sp
                    JOIN empresas e ON e.id = sp.empresa_id ORDER BY sp.pasta, sp.nome""")
    return page_template('pages/secretarios.html', title="Secretários", rows=rows, empresas_rows=empresas_rows)


@app.route("/justificativas", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal")
def justificativas():
    if request.method == "POST":
        execute("INSERT INTO justificativas_padrao (descricao, criado_em) VALUES (?, ?)", (request.form["descricao"], now_iso()))
        audit("criar", "justificativas_padrao", detalhes={"descricao": request.form["descricao"]})
        return redirect(url_for("justificativas"))
    rows = query("SELECT * FROM justificativas_padrao ORDER BY descricao")
    return page_template('pages/justificativas.html', title="Justificativas", rows=rows)


@app.route("/locais", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def locais():
    empresas_rows = query("SELECT * FROM empresas WHERE ativa = 1 ORDER BY razao_social")
    user = current_user()
    if request.method == "POST":
        rh_local_id = user["rh_local_id"] if user["perfil"] == "RH Local" else (request.form.get("rh_local_id") or None)
        execute(
            """INSERT INTO locais_trabalho
               (empresa_id, rh_local_id, nome, endereco, latitude, longitude, raio_metros, secretaria_responsavel)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request.form["empresa_id"],
                rh_local_id,
                request.form["nome"],
                request.form.get("endereco"),
                request.form["latitude"],
                request.form["longitude"],
                request.form["raio_metros"],
                request.form.get("secretaria_responsavel"),
            ),
        )
        audit("criar", "locais_trabalho", detalhes={"nome": request.form["nome"]})
        return redirect(url_for("locais"))
    rh_rows = query("SELECT * FROM rh_locais WHERE ativo = 1 ORDER BY nome")
    if user["perfil"] == "RH Local" and user["rh_local_id"]:
        rows = query("""SELECT l.*, e.nome_fantasia empresa, rh.nome rh_local FROM locais_trabalho l
                        JOIN empresas e ON e.id = l.empresa_id
                        LEFT JOIN rh_locais rh ON rh.id = l.rh_local_id
                        WHERE l.rh_local_id = ? ORDER BY l.nome""", (user["rh_local_id"],))
    else:
        rows = query("""SELECT l.*, e.nome_fantasia empresa, rh.nome rh_local FROM locais_trabalho l
                        JOIN empresas e ON e.id = l.empresa_id
                        LEFT JOIN rh_locais rh ON rh.id = l.rh_local_id
                        ORDER BY l.nome""")
    return page_template('pages/locais.html', title="Locais", rows=rows, empresas_rows=empresas_rows, rh_rows=rh_rows, local_user=user)


@app.route("/totens", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal")
def totens():
    locais_rows = query("SELECT * FROM locais_trabalho WHERE ativo = 1 ORDER BY nome")
    if request.method == "POST":
        ativo = 1 if request.form.get("ativo") == "1" else 0
        execute(
            """INSERT INTO totens (nome, descricao, local_id, secretaria, latitude, longitude, raio_metros, ativo, criado_em, atualizado_em)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request.form["nome"],
                request.form.get("descricao"),
                request.form.get("local_id") or None,
                request.form.get("secretaria"),
                request.form["latitude"],
                request.form["longitude"],
                request.form["raio_metros"],
                ativo,
                now_iso(),
                now_iso(),
            ),
        )
        audit("criar", "totens", detalhes={"nome": request.form["nome"], "ativo": ativo})
        return redirect(url_for("totens"))
    rows = query(
        """SELECT t.*, l.nome local_nome
           FROM totens t
           LEFT JOIN locais_trabalho l ON l.id = t.local_id
           ORDER BY t.ativo DESC, t.nome"""
    )
    return page_template("pages/totens.html", title="Totens", rows=rows, locais_rows=locais_rows)


@app.route("/dispositivos", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def dispositivos():
    user = current_user()
    if request.method == "POST":
        funcionario_id = request.form.get("funcionario_id") or None
        if funcionario_id and not funcionario_admin_row(funcionario_id, user):
            return page("<div class='alert alert-danger'>Funcionário fora do escopo permitido.</div>", title="Dispositivos"), 403
        raw_hash = request.form.get("hash_dispositivo") or f"{request.form.get('tipo','')}-{request.form.get('modelo','')}-{uuid.uuid4().hex}"
        hash_dispositivo = hashlib.sha256(raw_hash.encode("utf-8")).hexdigest()
        execute(
            """INSERT OR REPLACE INTO dispositivos
               (id, tipo, modelo, sistema_operacional, navegador, funcionario_id, hash_dispositivo, ultimo_acesso, situacao, criado_em, atualizado_em)
               VALUES ((SELECT id FROM dispositivos WHERE hash_dispositivo = ?), ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT criado_em FROM dispositivos WHERE hash_dispositivo = ?), ?), ?)""",
            (
                hash_dispositivo,
                request.form["tipo"],
                request.form.get("modelo"),
                request.form.get("sistema_operacional"),
                request.form.get("navegador"),
                funcionario_id,
                hash_dispositivo,
                request.form.get("ultimo_acesso") or now_iso(),
                request.form.get("situacao") or "ativo",
                hash_dispositivo,
                now_iso(),
                now_iso(),
            ),
        )
        audit("salvar", "dispositivos", detalhes={"tipo": request.form["tipo"], "funcionario_id": funcionario_id})
        return redirect(url_for("dispositivos"))
    funcionarios_rows = allowed_funcionarios_for_user(user)
    rows = query(
        """SELECT d.*, f.nome funcionario, f.matricula
           FROM dispositivos d
           LEFT JOIN funcionarios f ON f.id = d.funcionario_id
           ORDER BY d.ultimo_acesso DESC, d.id DESC"""
    )
    return page_template("pages/dispositivos.html", title="Dispositivos", rows=rows, funcionarios_rows=funcionarios_rows)


@app.route("/hierarquia")
@login_required
@perfil_required("Administrador Principal", "RH Local")
def hierarquia():
    user = current_user()
    params = []
    filtro = "WHERE f.ativo = 1"
    if user["perfil"] == "RH Local" and user["rh_local_id"]:
        filtro += " AND f.rh_local_id = ?"
        params.append(user["rh_local_id"])
    rows = query(
        f"""SELECT f.id, f.nome, f.matricula, f.cargo, f.papel_operacional, f.secretaria, f.departamento,
                   rh.nome rh_local, c.nome chefia, sp.nome secretario, sp.pasta secretario_pasta,
                   (SELECT COUNT(*) FROM funcionarios sub WHERE sub.chefia_id = f.chefia_id AND sub.ativo = 1 AND f.papel_operacional IN ('chefia_imediata', 'gestor')) subordinados
            FROM funcionarios f
            LEFT JOIN rh_locais rh ON rh.id = f.rh_local_id
            LEFT JOIN chefias c ON c.id = f.chefia_id
            LEFT JOIN secretarios_pastas sp ON sp.id = f.secretario_id
            {filtro}
            ORDER BY f.nome""",
        params,
    )
    return page_template("pages/hierarquia.html", title="Hierarquia", rows=rows)


@app.route("/configuracoes")
@login_required
@perfil_required("Administrador Principal")
def configuracoes():
    return page("""
    <div class="mb-4"><h3 class="fw-bold">Configurações</h3><div class="text-muted">Acessos administrativos do sistema.</div></div>
    <div class="row g-3">
      <div class="col-md-4"><a class="card text-decoration-none h-100" href="{{ url_for('empresas') }}"><div class="card-body"><h5>Empresas</h5><p class="text-muted mb-0">Dados da organização.</p></div></a></div>
      <div class="col-md-4"><a class="card text-decoration-none h-100" href="{{ url_for('locais') }}"><div class="card-body"><h5>Locais de Trabalho</h5><p class="text-muted mb-0">Geolocalização e raio autorizado.</p></div></a></div>
      <div class="col-md-4"><a class="card text-decoration-none h-100" href="{{ url_for('jornadas') }}"><div class="card-body"><h5>Horários de Trabalho</h5><p class="text-muted mb-0">Jornadas, intervalos e tolerância.</p></div></a></div>
      <div class="col-md-4"><a class="card text-decoration-none h-100" href="{{ url_for('totens') }}"><div class="card-body"><h5>Terminais de Ponto</h5><p class="text-muted mb-0">Tablets e dispositivos autorizados.</p></div></a></div>
      <div class="col-md-4"><a class="card text-decoration-none h-100" href="{{ url_for('justificativas') }}"><div class="card-body"><h5>Justificativas</h5><p class="text-muted mb-0">Motivos padrão para ajustes.</p></div></a></div>
      <div class="col-md-4"><a class="card text-decoration-none h-100" href="{{ url_for('usuarios') }}"><div class="card-body"><h5>Usuários e Permissões</h5><p class="text-muted mb-0">Login, perfil e vínculos de acesso.</p></div></a></div>
    </div>
    """, title="Configurações")


@app.route("/configuracoes/whatsapp", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal")
def whatsapp_configuracao():
    resultado = None
    if request.method == "POST":
        acao = request.form.get("acao", "salvar")
        execute(
            """UPDATE whatsapp_config
               SET evolution_url = ?, api_key = ?, instancia = ?, modo_envio = ?, numero_teste = ?,
                   atualizado_em = ?
               WHERE id = 1""",
            (
                request.form.get("evolution_url", "").strip(),
                request.form.get("api_key", "").strip(),
                request.form.get("instancia", "").strip(),
                request.form.get("modo_envio", "teste"),
                normalizar_whatsapp(request.form.get("numero_teste")),
                now_iso(),
            ),
        )
        audit("configurar_whatsapp", "whatsapp_config", 1, {"modo_envio": request.form.get("modo_envio", "teste"), "acao": acao})
        if acao == "conectar_whatsapp":
            retorno = evolution_connect_instance()
            config = whatsapp_config()
            if retorno.get("ok", True) and config["status_conexao"] == "aguardando_qr_code":
                resultado = {"classe": "info", "texto": "Instancia preparada. Leia o QR Code para concluir o pareamento."}
            elif config["status_conexao"] == "conectado":
                resultado = {"classe": "success", "texto": "WhatsApp conectado com sucesso."}
            else:
                resultado = {"classe": "danger", "texto": retorno.get("erro") or "Falha ao preparar a instancia."}
        elif acao == "verificar_status":
            retorno = evolution_instance_status()
            classe = "success" if retorno.get("status") == "conectado" else "warning" if retorno.get("status") == "aguardando_qr_code" else "danger"
            resultado = {"classe": classe, "texto": f"Status da instancia: {retorno.get('status')}"}
        elif acao == "testar_envio":
            retorno = evolution_send_test_message()
            if retorno.get("ok"):
                fila = retorno.get("fila") or {}
                resultado = {"classe": "success", "texto": f"Mensagem de teste enviada. Fila {fila.get('id', '-')}."}
            else:
                resultado = {"classe": "danger", "texto": retorno.get("erro") or "Falha ao testar envio."}
        else:
            return redirect(url_for("whatsapp_configuracao"))
    config = whatsapp_config()
    status_labels = {
        "nao_configurado": "não configurado",
        "aguardando_qr_code": "aguardando QR Code",
        "conectado": "conectado",
        "desconectado": "desconectado",
        "erro": "erro",
    }
    return page_template(
        "pages/whatsapp_config.html",
        title="WhatsApp",
        config=config,
        resultado=resultado,
        status_label=status_labels.get(config["status_conexao"], config["status_conexao"] or "não configurado"),
    )


@app.route("/central-notificacoes", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def central_notificacoes():
    if request.method == "POST":
        acao = request.form.get("acao")
        if acao == "comunicado_geral":
            mensagem = request.form.get("mensagem", "").strip()
            if mensagem:
                for f in query("SELECT id, whatsapp FROM funcionarios WHERE ativo = 1 AND receber_whatsapp = 1"):
                    enfileirar_whatsapp("rh_comunicado_geral", mensagem, f["whatsapp"], funcionario_id=f["id"])
            return redirect(url_for("central_notificacoes"))
        if acao == "aviso_mensal":
            mensagem = "Espelho de ponto e banco de horas mensal disponivel no sistema."
            for f in query("SELECT id, whatsapp FROM funcionarios WHERE ativo = 1 AND receber_whatsapp = 1"):
                enfileirar_whatsapp("espelho_banco_horas_mensal", mensagem, f["whatsapp"], funcionario_id=f["id"])
            return redirect(url_for("central_notificacoes"))
        fila_id = request.form.get("fila_id")
        if fila_id:
            execute("UPDATE whatsapp_fila SET status = 'pendente' WHERE id = ?", (fila_id,))
            enviar_whatsapp_fila(fila_id)
        return redirect(url_for("central_notificacoes"))
    sql = """SELECT wf.*, f.nome funcionario, c.nome chefia, u.nome usuario
             FROM whatsapp_fila wf
             LEFT JOIN funcionarios f ON f.id = wf.funcionario_id
             LEFT JOIN chefias c ON c.id = wf.chefia_id
             LEFT JOIN usuarios u ON u.id = wf.usuario_id
             WHERE 1 = 1"""
    params = []
    if request.args.get("status"):
        sql += " AND wf.status = ?"
        params.append(request.args["status"])
    if request.args.get("tipo"):
        sql += " AND wf.tipo LIKE ?"
        params.append(f"%{request.args['tipo']}%")
    if request.args.get("funcionario_id"):
        sql += " AND wf.funcionario_id = ?"
        params.append(request.args["funcionario_id"])
    if request.args.get("chefia_id"):
        sql += " AND wf.chefia_id = ?"
        params.append(request.args["chefia_id"])
    if request.args.get("data_inicio"):
        sql += " AND wf.criado_em >= ?"
        params.append(request.args["data_inicio"] + " 00:00:00")
    if request.args.get("data_fim"):
        sql += " AND wf.criado_em <= ?"
        params.append(request.args["data_fim"] + " 23:59:59")
    rows = query(sql + " ORDER BY wf.criado_em DESC LIMIT 500", params)
    counts = {
        "pendente": one("SELECT COUNT(*) total FROM whatsapp_fila WHERE status = 'pendente'")["total"],
        "enviada": one("SELECT COUNT(*) total FROM whatsapp_fila WHERE status = 'enviada'")["total"],
        "erro": one("SELECT COUNT(*) total FROM whatsapp_fila WHERE status = 'erro'")["total"],
    }
    return page_template(
        "pages/central_notificacoes.html",
        title="Central de Notificações",
        rows=rows,
        counts=counts,
        funcionarios_rows=query("SELECT id, nome FROM funcionarios WHERE ativo = 1 ORDER BY nome"),
        chefias_rows=query("SELECT id, nome FROM chefias WHERE ativo = 1 ORDER BY nome"),
        filtros=request.args,
    )


@app.route("/permissoes")
@login_required
@perfil_required("Administrador Principal")
def permissoes():
    return page("""
    <div class="mb-4"><h3 class="fw-bold">Permissões</h3><div class="text-muted">Perfis de acesso são gerenciados no cadastro de usuários.</div></div>
    <div class="card"><div class="card-body">
      <p>Use a tela de Usuários para definir perfil, funcionário vinculado, RH local, chefia, secretaria e status ativo/inativo.</p>
      <a class="btn btn-primary" href="{{ url_for('usuarios') }}"><i class="bi bi-person-gear"></i> Abrir Usuários</a>
    </div></div>
    """, title="Permissões")


@app.route("/jornadas", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def jornadas():
    if request.method == "POST":
        carga = int(request.form["carga_horas"]) * 60 + int(request.form.get("carga_minutos") or 0)
        tolerancia_antes = int_form(request.form.get("tolerancia_antes_minutos"), int_form(request.form.get("tolerancia_minutos"), 0))
        tolerancia_atraso = int_form(request.form.get("tolerancia_atraso_minutos"), int_form(request.form.get("tolerancia_minutos"), 0))
        tolerancia_padrao = max(tolerancia_antes, tolerancia_atraso)
        execute("""INSERT INTO jornadas
                   (nome, carga_minutos, entrada, saida_almoco, retorno_almoco, saida_final,
                    tolerancia_minutos, tolerancia_antes_minutos, tolerancia_atraso_minutos, tipo_escala, data_inicio_escala)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request.form["nome"], carga, request.form["entrada"], parse_time_str(request.form.get("saida_almoco")),
                 parse_time_str(request.form.get("retorno_almoco")), request.form["saida_final"],
                 tolerancia_padrao, tolerancia_antes, tolerancia_atraso, request.form["tipo_escala"], request.form.get("data_inicio_escala") or None))
        audit("criar", "jornadas", detalhes={"nome": request.form["nome"]})
        return redirect(url_for("jornadas"))
    rows = query("SELECT * FROM jornadas ORDER BY ativa DESC, nome")
    return page_template('pages/jornadas.html', title="Jornadas", rows=rows, fmt=fmt_minutes)


@app.route("/funcionarios", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def funcionarios():
    user = current_user()
    empresas_rows = query("SELECT * FROM empresas WHERE ativa = 1 ORDER BY razao_social")
    if user["perfil"] == "RH Local" and user["rh_local_id"]:
        locais_rows = query("SELECT * FROM locais_trabalho WHERE ativo = 1 AND rh_local_id = ? ORDER BY nome", (user["rh_local_id"],))
        rh_rows = query("SELECT * FROM rh_locais WHERE id = ? ORDER BY nome", (user["rh_local_id"],))
    else:
        locais_rows = query("SELECT * FROM locais_trabalho WHERE ativo = 1 ORDER BY nome")
        rh_rows = query("SELECT * FROM rh_locais WHERE ativo = 1 ORDER BY nome")
    jornadas_rows = query("SELECT * FROM jornadas WHERE ativa = 1 ORDER BY nome")
    chefias_rows = query("SELECT * FROM chefias WHERE ativo = 1 ORDER BY nome")
    secretarios_rows = query("SELECT * FROM secretarios_pastas WHERE ativo = 1 ORDER BY pasta, nome")
    if request.method == "POST":
        rh_local_id = user["rh_local_id"] if user["perfil"] == "RH Local" else request.form.get("rh_local_id")
        login_novo = request.form.get("login", "").strip()
        senha_nova = request.form.get("senha", "")
        confirmar_senha = request.form.get("confirmar_senha", "")
        perfil_acesso = request.form.get("perfil_acesso", "Funcionário")
        papel_operacional = request.form.get("papel_operacional") or "funcionario"
        if request.form.get("papel_operacional"):
            perfil_acesso = perfil_from_papel_operacional(papel_operacional)
        jornada_form, erro_jornada = jornada_funcionario_from_form(request.form)
        if not login_novo or not senha_nova or not confirmar_senha or not perfil_acesso:
            return page("<div class='alert alert-danger'>Informe login, senha, confirmação de senha e perfil de acesso.</div><a class='btn btn-primary' href='{{ url_for(\"funcionarios\") }}'>Voltar</a>", title="Funcionários")
        if senha_nova != confirmar_senha:
            return page("<div class='alert alert-danger'>Senha e confirmação de senha não conferem.</div><a class='btn btn-primary' href='{{ url_for(\"funcionarios\") }}'>Voltar</a>", title="Funcionários")
        if one("SELECT id FROM usuarios WHERE login = ?", (login_novo,)):
            return page("<div class='alert alert-danger'>Já existe usuário com este login.</div><a class='btn btn-primary' href='{{ url_for(\"funcionarios\") }}'>Voltar</a>", title="Funcionários")
        if erro_jornada:
            return page(f"<div class='alert alert-danger'>{erro_jornada}</div><a class='btn btn-primary' href='{{{{ url_for(\"funcionarios\") }}}}'>Voltar</a>", title="FuncionÃ¡rios")
        jornada_id = request.form["jornada_id"]
        if jornada_form:
            nome_jornada = f"{request.form['matricula']} - {jornada_form['tipo']}"
            tolerancia_padrao = max(jornada_form["tolerancia_antes"], jornada_form["tolerancia_atraso"])
            execute(
                """INSERT INTO jornadas
                   (nome, carga_minutos, entrada, saida_almoco, retorno_almoco, saida_final,
                    tolerancia_minutos, tolerancia_antes_minutos, tolerancia_atraso_minutos, tipo_escala, padrao)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'dias_uteis', 0)""",
                (
                    nome_jornada,
                    jornada_form["carga"],
                    jornada_form["entrada"],
                    jornada_form["saida_almoco"],
                    jornada_form["retorno_almoco"],
                    jornada_form["saida_final"],
                    tolerancia_padrao,
                    jornada_form["tolerancia_antes"],
                    jornada_form["tolerancia_atraso"],
                ),
            )
            jornada_id = one("SELECT id FROM jornadas WHERE nome = ?", (nome_jornada,))["id"]
        permite_totem_facial = 1 if request.form.get("permite_totem_facial") == "1" else 0
        reconhecimento_facial_ativo = 1 if request.form.get("reconhecimento_facial_ativo") == "1" else 0
        execute("""INSERT INTO funcionarios
                   (empresa_id, local_id, jornada_id, nome, cpf, matricula, cargo, email, telefone, data_admissao,
                    rh_local_id, chefia_id, secretario_id, reconhecimento_facial_ativo, permite_totem_facial,
                    permitir_totem_facial, whatsapp, receber_whatsapp, papel_operacional, data_nascimento, secretaria,
                    departamento, tipo_servidor, situacao, escala, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request.form["empresa_id"], request.form["local_id"], jornada_id, request.form["nome"],
                 request.form["cpf"], request.form["matricula"], request.form.get("cargo"), request.form.get("email"),
                 request.form.get("telefone"), request.form.get("data_admissao"), rh_local_id,
                 request.form.get("chefia_id"), request.form.get("secretario_id"), reconhecimento_facial_ativo,
                 permite_totem_facial, permite_totem_facial, normalizar_whatsapp(request.form.get("whatsapp")),
                 1 if request.form.get("receber_whatsapp") == "1" else 0, papel_operacional,
                 request.form.get("data_nascimento"), request.form.get("secretaria"), request.form.get("departamento"),
                 request.form.get("tipo_servidor"), request.form.get("situacao") or "ativo", request.form.get("escala"), now_iso()))
        novo_funcionario = one("SELECT * FROM funcionarios WHERE matricula = ?", (request.form["matricula"],))
        locais_autorizados = set(request.form.getlist("locais_autorizados"))
        locais_autorizados.add(str(request.form["local_id"]))
        for local_autorizado_id in locais_autorizados:
            execute(
                """INSERT OR IGNORE INTO funcionario_locais_autorizados (funcionario_id, local_id, ativo, criado_em)
                   VALUES (?, ?, 1, ?)""",
                (novo_funcionario["id"], local_autorizado_id, now_iso()),
            )
        execute(
            """INSERT INTO usuarios
               (nome, cpf, email, telefone, login, senha, perfil, funcionario_id, rh_local_id, chefia_id, secretario_id, ativo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (request.form["nome"], request.form["cpf"], request.form.get("email"), request.form.get("telefone"),
             login_novo, hash_password(senha_nova), perfil_acesso, novo_funcionario["id"], rh_local_id,
             request.form.get("chefia_id"), request.form.get("secretario_id")),
        )
        if perfil_acesso in ("Chefia Imediata", "Chefia imediata", "Gestor"):
            usuario_novo = one("SELECT id FROM usuarios WHERE login = ?", (login_novo,))
            sync_chefia_com_funcionario(novo_funcionario["id"], usuario_novo["id"] if usuario_novo else None)
        audit("criar", "funcionarios", detalhes={"matricula": request.form["matricula"]})
        return redirect(url_for("funcionarios"))
    sql_rows = """SELECT f.*, e.nome_fantasia empresa, l.nome local, j.nome jornada,
                           j.entrada, j.saida_almoco, j.retorno_almoco, j.saida_final,
                           rh.nome rh_local, c.nome chefia, sp.pasta secretario_pasta, sp.nome secretario,
                           (SELECT id FROM biometrias_faciais bf
                            WHERE bf.funcionario_id = f.id AND bf.ativo = 1
                            ORDER BY bf.id DESC LIMIT 1) biometria_facial_id,
                           (SELECT criado_em FROM biometrias_faciais bf
                            WHERE bf.funcionario_id = f.id AND bf.ativo = 1
                            ORDER BY bf.id DESC LIMIT 1) biometria_facial_criada_em
                    FROM funcionarios f
                    JOIN empresas e ON e.id = f.empresa_id
                    JOIN locais_trabalho l ON l.id = f.local_id
                    JOIN jornadas j ON j.id = f.jornada_id
                    LEFT JOIN rh_locais rh ON rh.id = f.rh_local_id
                    LEFT JOIN chefias c ON c.id = f.chefia_id
                    LEFT JOIN secretarios_pastas sp ON sp.id = f.secretario_id
                    """
    params_rows = []
    if user["perfil"] == "RH Local" and user["rh_local_id"]:
        sql_rows += " WHERE f.rh_local_id = ?"
        params_rows.append(user["rh_local_id"])
    rows = query(sql_rows + " ORDER BY f.nome", params_rows)
    return page_template('pages/funcionarios.html', title="Funcionários", rows=rows, empresas_rows=empresas_rows, locais_rows=locais_rows,
    jornadas_rows=jornadas_rows, rh_rows=rh_rows, chefias_rows=chefias_rows, secretarios_rows=secretarios_rows,
    fmt=fmt_minutes)


def funcionarios_api_rows():
    user = current_user()
    sql = """SELECT f.id, f.empresa_id, f.local_id, f.jornada_id, f.nome, f.cpf, f.matricula, f.cargo,
                    f.email, f.telefone, f.whatsapp, f.receber_whatsapp, f.data_admissao, f.rh_local_id, f.chefia_id, f.secretario_id,
                    f.papel_operacional, f.data_nascimento, f.secretaria, f.departamento, f.tipo_servidor, f.situacao, f.escala,
                    f.ativo, f.foto_base_path, f.foto_facial_cadastrada,
                    f.mini_video_cadastrado, f.reconhecimento_facial_ativo, f.permite_totem_facial,
                    f.permitir_totem_facial, f.face_image_path, f.face_video_path, f.updated_at,
                    j.entrada, j.saida_almoco, j.retorno_almoco, j.saida_final,
                    j.tolerancia_antes_minutos, j.tolerancia_atraso_minutos,
                    e.nome_fantasia empresa, l.nome local, j.nome jornada,
                    rh.nome rh_local, c.nome chefia, sp.pasta secretario_pasta, sp.nome secretario,
                    u.login, u.perfil perfil_acesso,
                    (SELECT group_concat(local_id)
                     FROM funcionario_locais_autorizados fla
                     WHERE fla.funcionario_id = f.id AND fla.ativo = 1) locais_autorizados,
                    (SELECT id FROM biometrias_faciais bf
                     WHERE bf.funcionario_id = f.id AND bf.ativo = 1
                     ORDER BY bf.id DESC LIMIT 1) biometria_facial_id,
                    (SELECT criado_em FROM biometrias_faciais bf
                     WHERE bf.funcionario_id = f.id AND bf.ativo = 1
                     ORDER BY bf.id DESC LIMIT 1) biometria_facial_criada_em
             FROM funcionarios f
             JOIN jornadas j ON j.id = f.jornada_id
             JOIN empresas e ON e.id = f.empresa_id
             JOIN locais_trabalho l ON l.id = f.local_id
             LEFT JOIN rh_locais rh ON rh.id = f.rh_local_id
             LEFT JOIN chefias c ON c.id = f.chefia_id
             LEFT JOIN secretarios_pastas sp ON sp.id = f.secretario_id
             LEFT JOIN usuarios u ON u.funcionario_id = f.id
             WHERE 1 = 1"""
    params = []
    if user["perfil"] == "RH Local" and user["rh_local_id"]:
        sql += " AND f.rh_local_id = ?"
        params.append(user["rh_local_id"])
    return query(sql + " ORDER BY f.nome", params)


@app.route("/api/funcionarios")
@login_required
@perfil_required("Administrador Principal", "RH Local")
def api_funcionarios():
    return {"funcionarios": [funcionario_status_payload(row) for row in funcionarios_api_rows()]}


@app.route("/biometria-facial")
@login_required
@perfil_required("Administrador Principal", "RH Local")
def biometria_facial():
    funcionarios = [funcionario_status_payload(row) for row in funcionarios_api_rows() if row["ativo"]]
    return page_template(
        "pages/biometria_facial.html",
        title="Biometria Facial",
        funcionarios_rows=funcionarios,
        funcionario_id=request.args.get("funcionario_id", ""),
    )


@app.route("/api/funcionarios/<int:funcionario_id>")
@login_required
@perfil_required("Administrador Principal", "RH Local")
def api_funcionario_detalhe(funcionario_id):
    funcionario = funcionario_api_payload(funcionario_id)
    if not funcionario:
        return {"erro": "Funcionario nao encontrado."}, 404
    return {"funcionario": funcionario}


def funcionario_api_payload(funcionario_id):
    rows = [row for row in funcionarios_api_rows() if row["id"] == funcionario_id]
    return funcionario_status_payload(rows[0]) if rows else None


def funcionario_admin_row(funcionario_id, user=None):
    user = user or current_user()
    if user["perfil"] == "RH Local" and user["rh_local_id"]:
        return one("SELECT * FROM funcionarios WHERE id = ? AND rh_local_id = ?", (funcionario_id, user["rh_local_id"]))
    return one("SELECT * FROM funcionarios WHERE id = ?", (funcionario_id,))


def snapshot_funcionario(funcionario_id):
    payload = funcionario_api_payload(funcionario_id)
    usuario = one("SELECT id, login, perfil, ativo FROM usuarios WHERE funcionario_id = ? ORDER BY id LIMIT 1", (funcionario_id,))
    if payload and usuario:
        payload["usuario"] = dict(usuario)
    return payload


def sync_locais_autorizados(funcionario_id, local_principal_id, locais_ids):
    desejados = {str(local_principal_id)}
    desejados.update(str(item) for item in locais_ids if str(item).strip())
    existentes = query("SELECT local_id FROM funcionario_locais_autorizados WHERE funcionario_id = ?", (funcionario_id,))
    existentes_ids = {str(row["local_id"]) for row in existentes}
    for local_id in desejados:
        if local_id in existentes_ids:
            execute(
                "UPDATE funcionario_locais_autorizados SET ativo = 1 WHERE funcionario_id = ? AND local_id = ?",
                (funcionario_id, local_id),
            )
        else:
            execute(
                """INSERT OR IGNORE INTO funcionario_locais_autorizados (funcionario_id, local_id, ativo, criado_em)
                   VALUES (?, ?, 1, ?)""",
                (funcionario_id, local_id, now_iso()),
            )
    for local_id in existentes_ids - desejados:
        execute(
            "UPDATE funcionario_locais_autorizados SET ativo = 0 WHERE funcionario_id = ? AND local_id = ?",
            (funcionario_id, local_id),
        )


def perfil_from_papel_operacional(papel):
    mapa = {
        "funcionario": "FuncionÃ¡rio",
        "chefia_imediata": "Chefia Imediata",
        "gestor": "Gestor",
        "rh": "RH Local",
    }
    return mapa.get((papel or "funcionario").strip(), "FuncionÃ¡rio")


def sync_chefia_com_funcionario(funcionario_id, usuario_id=None):
    funcionario = one("SELECT * FROM funcionarios WHERE id = ?", (funcionario_id,))
    if not funcionario:
        return None
    chefia = one("SELECT * FROM chefias WHERE id = ?", (funcionario["chefia_id"],)) if funcionario["chefia_id"] else None
    if not chefia:
        chefia = one(
            "SELECT * FROM chefias WHERE usuario_id = ? OR lower(email) = lower(?) OR nome = ? ORDER BY usuario_id IS NULL, id LIMIT 1",
            (usuario_id, funcionario["email"] or "", funcionario["nome"]),
        )
    if chefia:
        execute(
            """UPDATE chefias
               SET empresa_id = ?, nome = ?, email = ?, cargo = ?, whatsapp = COALESCE(NULLIF(?, ''), whatsapp),
                   usuario_id = COALESCE(?, usuario_id), ativo = 1
               WHERE id = ?""",
            (
                funcionario["empresa_id"],
                funcionario["nome"],
                funcionario["email"],
                funcionario["cargo"],
                funcionario["whatsapp"] if "whatsapp" in funcionario.keys() else None,
                usuario_id,
                chefia["id"],
            ),
        )
        chefia_id = chefia["id"]
    else:
        execute(
            "INSERT INTO chefias (empresa_id, nome, email, cargo, whatsapp, receber_solicitacoes_whatsapp, usuario_id, ativo) VALUES (?, ?, ?, ?, ?, 1, ?, 1)",
            (funcionario["empresa_id"], funcionario["nome"], funcionario["email"], funcionario["cargo"], funcionario["whatsapp"] if "whatsapp" in funcionario.keys() else None, usuario_id),
        )
        chefia_id = one("SELECT id FROM chefias WHERE usuario_id = ? ORDER BY id DESC LIMIT 1", (usuario_id,))["id"]
    execute("UPDATE funcionarios SET chefia_id = ?, updated_at = ? WHERE id = ?", (chefia_id, now_iso(), funcionario_id))
    if usuario_id:
        execute("UPDATE usuarios SET chefia_id = ?, funcionario_id = ? WHERE id = ?", (chefia_id, funcionario_id, usuario_id))
    return chefia_id


@app.route("/api/funcionarios/<int:funcionario_id>/editar", methods=["POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def api_funcionario_editar(funcionario_id):
    user = current_user()
    funcionario = funcionario_admin_row(funcionario_id, user)
    if not funcionario:
        return {"erro": "Funcionario nao encontrado."}, 404

    antigo = snapshot_funcionario(funcionario_id)
    login = request.form.get("login", "").strip()
    senha = request.form.get("senha", "")
    perfil = request.form.get("perfil_acesso", "Funcionário")
    papel_operacional = request.form.get("papel_operacional") or (funcionario["papel_operacional"] if "papel_operacional" in funcionario.keys() else "funcionario")
    if request.form.get("papel_operacional"):
        perfil = perfil_from_papel_operacional(papel_operacional)
    usuario = one("SELECT * FROM usuarios WHERE funcionario_id = ? ORDER BY id LIMIT 1", (funcionario_id,))
    if login:
        usuario_login = one("SELECT id FROM usuarios WHERE login = ? AND funcionario_id <> ?", (login, funcionario_id))
        if usuario_login:
            return {"erro": "Ja existe usuario com este login."}, 400
    elif usuario:
        login = usuario["login"]
    elif senha:
        return {"erro": "Informe um login para criar acesso com senha."}, 400

    jornada_form, erro_jornada = jornada_funcionario_from_form(request.form)
    if erro_jornada:
        return {"erro": erro_jornada}, 400
    jornada_id = request.form.get("jornada_id") or funcionario["jornada_id"]
    if jornada_form:
        nome_jornada = f"{request.form.get('matricula', funcionario['matricula'])} - {jornada_form['tipo']} {datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        tolerancia_padrao = max(jornada_form["tolerancia_antes"], jornada_form["tolerancia_atraso"])
        execute(
            """INSERT INTO jornadas
               (nome, carga_minutos, entrada, saida_almoco, retorno_almoco, saida_final,
                tolerancia_minutos, tolerancia_antes_minutos, tolerancia_atraso_minutos, tipo_escala, padrao)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'dias_uteis', 0)""",
            (
                nome_jornada,
                jornada_form["carga"],
                jornada_form["entrada"],
                jornada_form["saida_almoco"],
                jornada_form["retorno_almoco"],
                jornada_form["saida_final"],
                tolerancia_padrao,
                jornada_form["tolerancia_antes"],
                jornada_form["tolerancia_atraso"],
            ),
        )
        jornada_id = one("SELECT id FROM jornadas WHERE nome = ?", (nome_jornada,))["id"]

    rh_local_id = user["rh_local_id"] if user["perfil"] == "RH Local" else (request.form.get("rh_local_id") or None)
    reconhecimento = 1 if request.form.get("reconhecimento_facial_ativo") == "1" else int(funcionario["reconhecimento_facial_ativo"] or 0)
    permite_totem = 1 if request.form.get("permite_totem_facial") == "1" else int((funcionario["permite_totem_facial"] or funcionario["permitir_totem_facial"] or 0))
    execute(
        """UPDATE funcionarios
           SET empresa_id = ?, local_id = ?, jornada_id = ?, nome = ?, cpf = ?, matricula = ?,
               cargo = ?, email = ?, telefone = ?, data_admissao = ?, rh_local_id = ?,
               chefia_id = ?, secretario_id = ?, reconhecimento_facial_ativo = ?,
               permite_totem_facial = ?, permitir_totem_facial = ?, whatsapp = ?, receber_whatsapp = ?, papel_operacional = ?,
               data_nascimento = ?, secretaria = ?, departamento = ?, tipo_servidor = ?, situacao = ?, escala = ?, updated_at = ?
           WHERE id = ?""",
        (
            request.form["empresa_id"],
            request.form["local_id"],
            jornada_id,
            request.form["nome"],
            request.form["cpf"],
            request.form["matricula"],
            request.form.get("cargo"),
            request.form.get("email"),
            request.form.get("telefone"),
            request.form.get("data_admissao"),
            rh_local_id,
            request.form.get("chefia_id") or None,
            request.form.get("secretario_id") or None,
            reconhecimento,
            permite_totem,
            permite_totem,
            normalizar_whatsapp(request.form.get("whatsapp")),
            1 if request.form.get("receber_whatsapp") == "1" else 0,
            papel_operacional,
            request.form.get("data_nascimento"),
            request.form.get("secretaria"),
            request.form.get("departamento"),
            request.form.get("tipo_servidor"),
            request.form.get("situacao") or "ativo",
            request.form.get("escala"),
            now_iso(),
            funcionario_id,
        ),
    )
    sync_locais_autorizados(funcionario_id, request.form["local_id"], request.form.getlist("locais_autorizados"))

    if usuario:
        if senha:
            execute(
                """UPDATE usuarios
                   SET nome = ?, cpf = ?, email = ?, telefone = ?, login = ?, senha = ?, perfil = ?,
                       rh_local_id = ?, chefia_id = ?, secretario_id = ?
                   WHERE id = ?""",
                (
                    request.form["nome"], request.form["cpf"], request.form.get("email"), request.form.get("telefone"),
                    login, hash_password(senha), perfil, rh_local_id, request.form.get("chefia_id") or None,
                    request.form.get("secretario_id") or None, usuario["id"],
                ),
            )
        else:
            execute(
                """UPDATE usuarios
                   SET nome = ?, cpf = ?, email = ?, telefone = ?, login = ?, perfil = ?,
                       rh_local_id = ?, chefia_id = ?, secretario_id = ?
                   WHERE id = ?""",
                (
                    request.form["nome"], request.form["cpf"], request.form.get("email"), request.form.get("telefone"),
                    login, perfil, rh_local_id, request.form.get("chefia_id") or None,
                    request.form.get("secretario_id") or None, usuario["id"],
                ),
            )
    elif login:
        execute(
            """INSERT INTO usuarios
               (nome, cpf, email, telefone, login, senha, perfil, funcionario_id, rh_local_id, chefia_id, secretario_id, ativo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request.form["nome"], request.form["cpf"], request.form.get("email"), request.form.get("telefone"),
                login, hash_password(senha or secrets.token_urlsafe(12)), perfil, funcionario_id, rh_local_id,
                request.form.get("chefia_id") or None, request.form.get("secretario_id") or None,
                1 if funcionario["ativo"] else 0,
            ),
        )
        usuario = one("SELECT * FROM usuarios WHERE funcionario_id = ? ORDER BY id LIMIT 1", (funcionario_id,))

    if perfil in ("Chefia Imediata", "Chefia imediata", "Gestor") and usuario:
        sync_chefia_com_funcionario(funcionario_id, usuario["id"])

    novo = snapshot_funcionario(funcionario_id)
    audit("editar_funcionario", "funcionarios", funcionario_id, {"antes": antigo, "depois": novo})
    return {"ok": True, "mensagem": "Funcionario atualizado com sucesso", "funcionario": novo}


@app.route("/api/funcionarios/<int:funcionario_id>/inativar", methods=["POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def api_funcionario_inativar(funcionario_id):
    user = current_user()
    funcionario = funcionario_admin_row(funcionario_id, user)
    if not funcionario:
        return {"erro": "Funcionario nao encontrado."}, 404
    antigo = snapshot_funcionario(funcionario_id)
    execute(
        """UPDATE funcionarios
           SET ativo = 0, updated_at = ?
           WHERE id = ?""",
        (now_iso(), funcionario_id),
    )
    execute("UPDATE usuarios SET ativo = 0 WHERE funcionario_id = ?", (funcionario_id,))
    novo = snapshot_funcionario(funcionario_id)
    audit("inativar_funcionario", "funcionarios", funcionario_id, {"antes": antigo, "depois": novo})
    return {"ok": True, "mensagem": "Funcionario inativado com sucesso", "funcionario": novo}


@app.route("/api/funcionarios/<int:funcionario_id>/ativar", methods=["POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def api_funcionario_ativar(funcionario_id):
    user = current_user()
    funcionario = funcionario_admin_row(funcionario_id, user)
    if not funcionario:
        return {"erro": "Funcionario nao encontrado."}, 404
    antigo = snapshot_funcionario(funcionario_id)
    permite = 1 if funcionario["permite_totem_facial"] or funcionario["permitir_totem_facial"] else 0
    execute(
        """UPDATE funcionarios
           SET ativo = 1, permite_totem_facial = ?, permitir_totem_facial = ?, updated_at = ?
           WHERE id = ?""",
        (permite, permite, now_iso(), funcionario_id),
    )
    execute("UPDATE usuarios SET ativo = 1 WHERE funcionario_id = ?", (funcionario_id,))
    novo = snapshot_funcionario(funcionario_id)
    audit("ativar_funcionario", "funcionarios", funcionario_id, {"antes": antigo, "depois": novo})
    return {"ok": True, "mensagem": "Funcionario ativado com sucesso", "funcionario": novo}


@app.route("/api/funcionarios/<int:funcionario_id>/foto-facial-camera", methods=["POST"])
@app.route("/api/funcionarios/<int:funcionario_id>/foto-facial", methods=["POST"])
@app.route("/funcionarios/<int:funcionario_id>/foto-facial", methods=["POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def funcionario_foto_facial(funcionario_id):
    user = current_user()
    if user["perfil"] == "RH Local" and user["rh_local_id"]:
        funcionario = one("SELECT * FROM funcionarios WHERE id = ? AND rh_local_id = ?", (funcionario_id, user["rh_local_id"]))
    else:
        funcionario = one("SELECT * FROM funcionarios WHERE id = ?", (funcionario_id,))
    if not funcionario:
        return {"erro": "Funcionario nao encontrado."}, 404

    foto_base_path = save_foto_base_data_url(request.form.get("foto_base") or request.form.get("face_image_data"), funcionario_id)
    if not foto_base_path:
        foto_base_path = save_foto_base(request.files.get("foto_base") or request.files.get("face_image_file"), funcionario_id)
    if not foto_base_path:
        app.logger.warning("Foto facial base invalida para funcionario_id=%s", funcionario_id)
        return {"erro": "Foto facial invalida."}, 400
    face_embedding = request.form.get("embedding_json") or request.form.get("face_embedding") or request.form.get("face_image_embedding_json")

    execute(
        """UPDATE funcionarios
           SET foto_base_path = ?,
               face_image_path = ?,
               foto_facial_cadastrada = 1,
               reconhecimento_facial_ativo = 1,
               permite_totem_facial = 1,
               permitir_totem_facial = 1,
               face_embedding = COALESCE(?, face_embedding),
               face_embeddings_json = COALESCE(?, face_embeddings_json),
               updated_at = ?
           WHERE id = ?""",
        (foto_base_path, foto_base_path, face_embedding, face_embedding, now_iso(), funcionario_id),
    )
    audit("atualizar_foto_facial", "funcionarios", funcionario_id, {"foto_base_path": foto_base_path, "embedding": bool(face_embedding)})
    return {
        "ok": True,
        "foto_base_path": foto_base_path,
        "mensagem": "Foto facial cadastrada com sucesso",
        "funcionario": funcionario_api_payload(funcionario_id),
    }


@app.route("/api/funcionarios/<int:funcionario_id>/mini-video-facial", methods=["POST"])
@app.route("/funcionarios/<int:funcionario_id>/biometria-facial-video", methods=["POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def funcionario_biometria_facial_video(funcionario_id):
    user = current_user()
    if user["perfil"] == "RH Local" and user["rh_local_id"]:
        funcionario = one("SELECT * FROM funcionarios WHERE id = ? AND rh_local_id = ?", (funcionario_id, user["rh_local_id"]))
    else:
        funcionario = one("SELECT * FROM funcionarios WHERE id = ?", (funcionario_id,))
    if not funcionario:
        return {"erro": "Funcionario nao encontrado."}, 404

    biometria, erro, status_code = salvar_biometria_video_funcionario(funcionario_id, user, request.form)
    if erro:
        return erro, status_code
    return {
        "ok": True,
        "mensagem": "Biometria facial por mini video cadastrada com sucesso",
        "biometria_id": biometria["biometria_id"],
        "foto_principal_path": biometria["foto_principal_path"],
        "video_path": biometria["video_path"],
        "embeddings": biometria["embeddings"],
        "funcionario": funcionario_api_payload(funcionario_id),
    }


@app.route("/api/biometria-facial/teste", methods=["POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def api_biometria_facial_teste():
    inicio = datetime.now()
    selfie = request.form.get("selfie") or request.form.get("face_image_data")
    if not selfie:
        return {"ok": False, "erro": "Capture uma foto para testar o reconhecimento."}, 400
    funcionario, similaridade, diagnostico = reconhecer_funcionario_por_foto(selfie, min_similarity=45)
    tempo_ms = int((datetime.now() - inicio).total_seconds() * 1000)
    payload = {
        "ok": bool(funcionario),
        "funcionario": dict(funcionario) if funcionario else None,
        "confianca": round(float(similaridade or 0), 2),
        "liveness": "amostra_unica",
        "nitidez": None,
        "iluminacao": None,
        "tempo_ms": tempo_ms,
        "diagnostico": diagnostico,
    }
    audit("testar_reconhecimento_facial", "biometrias_faciais", detalhes={"ok": payload["ok"], "confianca": payload["confianca"], "tempo_ms": tempo_ms})
    return payload


def build_report(start, end, funcionario_id):
    sql = """SELECT f.*, j.nome jornada_nome, j.carga_minutos, j.entrada, j.saida_almoco, j.retorno_almoco,
                    j.saida_final, j.tolerancia_minutos, j.tolerancia_antes_minutos,
                    j.tolerancia_atraso_minutos, j.tipo_escala, j.data_inicio_escala
             FROM funcionarios f JOIN jornadas j ON j.id = f.jornada_id WHERE f.ativo = 1"""
    params = []
    user = current_user()
    if is_funcionario_profile(user):
        sql += " AND f.id = ?"
        params.append(user["funcionario_id"] or -1)
    elif user and user["perfil"] in ("RH", "RH Local") and user["rh_local_id"]:
        sql += " AND f.rh_local_id = ?"
        params.append(user["rh_local_id"])
    elif user and user["perfil"] in ("Chefia Imediata", "Chefia imediata", "Gestor") and user["chefia_id"]:
        sql += " AND f.chefia_id = ?"
        params.append(user["chefia_id"])
    elif user and user["perfil"] in ("Secretário da Pasta", "Secretário") and user["secretario_id"]:
        sql += " AND f.secretario_id = ?"
        params.append(user["secretario_id"])
    elif funcionario_id:
        sql += " AND f.id = ?"
        params.append(funcionario_id)
    funcionarios_rows = query(sql + " ORDER BY f.nome", params)
    funcionario_ids = [row["id"] for row in funcionarios_rows]
    marcacoes_por_dia = {}
    compensacoes_por_dia = {}
    if funcionario_ids:
        inicio, fim = period_bounds(start, end)
        for group in chunks(funcionario_ids):
            placeholders = ",".join("?" for _ in group)
            marcacoes = query(
                f"""SELECT * FROM marcacoes
                    WHERE funcionario_id IN ({placeholders})
                      AND data_hora >= ?
                      AND data_hora < ?
                    ORDER BY funcionario_id, data_hora""",
                (*group, inicio, fim),
            )
            for marcacao in marcacoes:
                key = (marcacao["funcionario_id"], marcacao["data_hora"][:10])
                marcacoes_por_dia.setdefault(key, []).append(marcacao)
            compensacoes = query(
                f"""SELECT funcionario_id, data, COALESCE(SUM(minutos), 0) total
                    FROM compensacoes
                    WHERE funcionario_id IN ({placeholders})
                      AND data >= ?
                      AND data <= ?
                    GROUP BY funcionario_id, data""",
                (*group, start.isoformat(), end.isoformat()),
            )
            for compensacao in compensacoes:
                compensacoes_por_dia[(compensacao["funcionario_id"], compensacao["data"])] = compensacao["total"]
    linhas = []
    current = start
    while current <= end:
        for f in funcionarios_rows:
            jornada = dict(f)
            jornada["nome"] = f["jornada_nome"]
            linhas.append(calcular_dia(f, jornada, current, marcacoes_por_dia, compensacoes_por_dia))
        current += timedelta(days=1)
    return linhas


@app.route("/relatorios")
@login_required
def relatorios():
    today = date.today()
    start = parse_date(request.args.get("data_inicio"), today.replace(day=1))
    end = parse_date(request.args.get("data_fim"), today)
    user = current_user()
    funcionario_id = str(user["funcionario_id"] or "") if is_funcionario_profile(user) else request.args.get("funcionario_id", "")
    linhas = build_report(start, end, funcionario_id)
    funcionarios_rows = allowed_funcionarios_for_user(user)
    totals = {
        "prevista": sum(r["prevista_min"] for r in linhas),
        "trabalhada": sum(r["trabalhada_min"] for r in linhas),
        "saldo": sum(r["saldo_min"] for r in linhas),
        "extras": sum(r["extras_min"] for r in linhas),
        "atraso": sum(r["atraso_min"] for r in linhas),
        "faltas": sum(1 for r in linhas if r["falta"]),
    }
    return page_template('pages/relatorios.html', title="Relatórios", linhas=linhas, totals=totals, funcionarios_rows=funcionarios_rows,
    funcionario_id=funcionario_id, start=start, end=end, fmt=fmt_minutes, can_choose_funcionario=not is_funcionario_profile(user),
    export_url=lambda formato: url_for("exportar", formato=formato, data_inicio=start.isoformat(), data_fim=end.isoformat(), funcionario_id=funcionario_id))


@app.route("/exportar/<formato>")
@login_required
def exportar(formato):
    start = parse_date(request.args.get("data_inicio"), date.today().replace(day=1))
    end = parse_date(request.args.get("data_fim"), date.today())
    user = current_user()
    funcionario_id = str(user["funcionario_id"] or "") if is_funcionario_profile(user) else request.args.get("funcionario_id", "")
    linhas = build_report(start, end, funcionario_id)
    if formato == "excel":
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Data", "Funcionario", "Jornada", "Prevista", "Trabalhada", "Saldo", "Extras", "Atraso", "Falta"])
        for l in linhas:
            writer.writerow([l["data"].isoformat(), l["funcionario"]["nome"], l["jornada"]["nome"], l["prevista"], l["trabalhada"], l["saldo"], l["extras"], l["atraso"], "Sim" if l["falta"] else "Não"])
        return Response(output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=relatorio_ponto.csv"})
    if formato == "afd":
        inicio, fim = period_bounds(start, end)
        funcionario_ids = sorted({l["funcionario"]["id"] for l in linhas})
        rows = []
        if funcionario_ids:
            placeholders = ",".join("?" for _ in funcionario_ids)
            rows = query(
                f"""SELECT m.*, f.cpf
                    FROM marcacoes m
                    JOIN funcionarios f ON f.id = m.funcionario_id
                    WHERE m.funcionario_id IN ({placeholders})
                      AND m.data_hora >= ?
                      AND m.data_hora < ?
                      AND COALESCE(m.status_aprovacao, 'normal') IN ('normal', 'aprovado')
                    ORDER BY m.data_hora""",
                (*funcionario_ids, inicio, fim),
            )
        text = "\n".join(f"AFD|{r['nsr']}|{r['cpf']}|{r['data_hora']}|{r['tipo']}|{r['hash_registro']}" for r in rows)
        return Response(text, mimetype="text/plain; charset=utf-8", headers={"Content-Disposition": "attachment; filename=AFD.txt"})
    if formato == "aej":
        text = "\n".join(f"AEJ|{l['data'].isoformat()}|{l['funcionario']['cpf']}|{l['prevista']}|{l['trabalhada']}|{l['saldo']}|{l['extras']}|{l['atraso']}" for l in linhas)
        return Response(text, mimetype="text/plain; charset=utf-8", headers={"Content-Disposition": "attachment; filename=AEJ.txt"})
    content = "Relatório de Ponto REP-P\n\n" + "\n".join(f"{l['data'].strftime('%d/%m/%Y')} {l['funcionario']['nome']} Prevista {l['prevista']} Trabalhada {l['trabalhada']} Saldo {l['saldo']}" for l in linhas)
    pdf = minimal_pdf(content)
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": "attachment; filename=relatorio_ponto.pdf"})


def minimal_pdf(text):
    lines = text.replace("(", "[").replace(")", "]").splitlines()[:80]
    stream = "BT /F1 10 Tf 40 800 Td " + " T* ".join(f"({line})" for line in lines) + " ET"
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj",
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj",
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj",
        "4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj",
        f"5 0 obj << /Length {len(stream.encode('latin-1', 'ignore'))} >> stream\n{stream}\nendstream endobj",
    ]
    pdf = "%PDF-1.4\n"
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf.encode("latin-1", "ignore")))
        pdf += obj + "\n"
    xref = len(pdf.encode("latin-1", "ignore"))
    pdf += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n"
    for offset in offsets[1:]:
        pdf += f"{offset:010d} 00000 n \n"
    pdf += f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    return pdf.encode("latin-1", "ignore")


@app.route("/ajustes", methods=["GET", "POST"])
@login_required
def ajustes():
    user = current_user()
    if request.method == "POST":
        funcionario_id = request.form["funcionario_id"] if user["perfil"] != "Funcionário" else user["funcionario_id"]
        funcionario = funcionario_admin_row(funcionario_id, user)
        if not funcionario:
            return page("<div class='alert alert-danger'>Acesso negado para este funcionário.</div>"), 403
        anexo_path = save_anexo(request.files.get("anexo"), funcionario_id)
        justificativa = request.form.get("justificativa", "").strip()
        justificativa_padrao = request.form.get("justificativa_padrao", "").strip()
        observacao = request.form.get("observacao", "").strip()
        texto_justificativa = " - ".join([item for item in (justificativa_padrao, justificativa) if item])
        execute("""INSERT INTO ajustes_ponto
                   (funcionario_id, tipo, data_hora_solicitada, justificativa, observacao, anexo_path, chefia_id, solicitado_por, criado_em)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (funcionario_id, request.form["tipo"], request.form["data_hora_solicitada"], texto_justificativa,
                 observacao, anexo_path, funcionario["chefia_id"], user["id"], now_iso()))
        ajuste_novo = one("SELECT id FROM ajustes_ponto WHERE funcionario_id = ? AND solicitado_por = ? ORDER BY id DESC LIMIT 1", (funcionario_id, user["id"]))
        if ajuste_novo:
            notificar_whatsapp_funcionario_justificativa_criada(ajuste_novo["id"])
            notificar_whatsapp_chefia_ajuste(ajuste_novo["id"])
        audit("solicitar_ajuste", "ajustes_ponto", detalhes={"funcionario_id": funcionario_id, "chefia_id": funcionario["chefia_id"]})
        return redirect(url_for("ajustes"))
    funcionarios_rows = allowed_funcionarios_for_user(user)
    justificativas_rows = query("SELECT * FROM justificativas_padrao WHERE ativa = 1 ORDER BY descricao")
    sql = """SELECT a.*, f.nome funcionario, u.nome solicitante, c.nome chefia, c.usuario_id chefia_usuario_id
             FROM ajustes_ponto a
             JOIN funcionarios f ON f.id = a.funcionario_id
             JOIN usuarios u ON u.id = a.solicitado_por
             LEFT JOIN chefias c ON c.id = a.chefia_id"""
    params = []
    if user["perfil"] == "Funcionário":
        sql += " WHERE a.funcionario_id = ?"
        params.append(user["funcionario_id"])
    elif user["perfil"] == "RH Local" and user["rh_local_id"]:
        sql += " WHERE f.rh_local_id = ?"
        params.append(user["rh_local_id"])
    elif user["perfil"] in ("Chefia Imediata", "Chefia imediata", "Gestor"):
        sql += " WHERE c.usuario_id = ?"
        params.append(user["id"])
    elif user["perfil"] in ("Secretário da Pasta", "Secretário") and user["secretario_id"]:
        sql += " WHERE f.secretario_id = ?"
        params.append(user["secretario_id"])
    rows = query(sql + " ORDER BY a.criado_em DESC", params)
    return page_template('pages/ajustes.html', title="Ajustes", rows=rows, funcionarios_rows=funcionarios_rows, labels=TIPOS_LABEL,
    justificativas_rows=justificativas_rows, can_approve=is_chefia_imediata(user))


@app.route("/ajustes/<int:ajuste_id>/<decisao>", methods=["POST"])
@login_required
@perfil_required("Chefia Imediata", "Chefia imediata")
def decidir_ajuste(ajuste_id, decisao):
    parecer = request.form.get("parecer") or request.form.get("motivo") or ("Aprovado pela chefia." if decisao == "aprovado" else "Rejeitado pela chefia.")
    resultado = efetivar_decisao_ajuste(ajuste_id, decisao, parecer, current_user())
    if resultado is not True:
        return resultado
    return redirect(url_for("ajustes"))


def efetivar_decisao_ajuste(ajuste_id, decisao, parecer, user):
    if decisao not in ("aprovado", "rejeitado"):
        decisao = "rejeitado"
    ajuste = one("""SELECT a.*, f.local_id, f.chefia_id FROM ajustes_ponto a
                    JOIN funcionarios f ON f.id = a.funcionario_id WHERE a.id = ?""", (ajuste_id,))
    if not ajuste:
        return page("<div class='alert alert-danger'>Solicitacao nao encontrada.</div>"), 404
    chefia = one("SELECT * FROM chefias WHERE id = ?", (ajuste["chefia_id"],))
    if not chefia or chefia["usuario_id"] != user["id"]:
        return page("<div class='alert alert-danger'>Acesso negado para esta chefia.</div>"), 403
    execute("UPDATE ajustes_ponto SET status = ?, aprovado_por = ?, parecer = ?, decidido_em = ? WHERE id = ?",
            (decisao, user["id"], parecer, now_iso(), ajuste_id))
    if decisao == "aprovado":
        local = one("SELECT * FROM locais_trabalho WHERE id = ?", (ajuste["local_id"],))
        nsr = datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6].upper()
        data_hora = ajuste["data_hora_solicitada"].replace("T", " ")
        payload = {
            "nsr": nsr,
            "funcionario_id": ajuste["funcionario_id"],
            "tipo": ajuste["tipo"],
            "data_hora": data_hora,
            "origem": "ajuste",
            "ajuste_id": ajuste_id,
        }
        execute(
            """INSERT INTO marcacoes
               (nsr, funcionario_id, tipo, data_hora, latitude, longitude, precisao, distancia_metros,
                dentro_cerca, selfie_path, dispositivo_id, user_agent, ip, hash_registro, origem, origem_normalizada,
                geolocalizacao_status, distancia_validacao_metros, ajuste_id, marcacao_original_id, criado_em)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (nsr, ajuste["funcionario_id"], ajuste["tipo"], data_hora, local["latitude"], local["longitude"],
             None, 0, 1, None, "ajuste-aprovado", "ajuste aprovado pela chefia", request.remote_addr,
             create_hash(payload), ORIGEM_AJUSTE, ORIGEM_AJUSTE, "aprovado", 0, ajuste_id, ajuste["marcacao_id"], now_iso()),
        )
    notificar_whatsapp_funcionario_resultado(ajuste_id, decisao, parecer)
    audit("decidir_ajuste", "ajustes_ponto", ajuste_id, {"decisao": decisao, "parecer": parecer})
    return True


@app.route("/aprovar-ajuste/<token>", methods=["GET", "POST"])
@login_required
@perfil_required("Chefia Imediata", "Chefia imediata")
def aprovar_ajuste_token(token):
    token_row = one("SELECT * FROM aprovacao_tokens WHERE token = ?", (token,))
    if not token_row or token_row["usado_em"]:
        return page("<div class='alert alert-danger'>Link invalido ou ja utilizado.</div>", title="Aprovação"), 403
    if datetime.strptime(token_row["expira_em"], "%Y-%m-%d %H:%M:%S") < datetime.now():
        return page("<div class='alert alert-danger'>Link expirado.</div>", title="Aprovação"), 403
    ajuste = one(
        """SELECT a.*, f.nome funcionario, f.matricula, f.cargo, f.foto_base_path, f.email, f.telefone,
                  c.nome chefia
           FROM ajustes_ponto a
           JOIN funcionarios f ON f.id = a.funcionario_id
           LEFT JOIN chefias c ON c.id = a.chefia_id
           WHERE a.id = ?""",
        (token_row["ajuste_id"],),
    )
    if not ajuste:
        return page("<div class='alert alert-danger'>Solicitacao nao encontrada.</div>", title="Aprovação"), 404
    user = current_user()
    chefia = one("SELECT * FROM chefias WHERE id = ?", (ajuste["chefia_id"],))
    if not chefia or chefia["usuario_id"] != user["id"]:
        return page("<div class='alert alert-danger'>Acesso negado para esta chefia.</div>", title="Aprovação"), 403
    if request.method == "POST":
        decisao = request.form.get("decisao")
        motivo = request.form.get("motivo", "").strip()
        if decisao not in ("aprovado", "rejeitado") or not motivo:
            return page_template("pages/aprovar_ajuste_token.html", title="Aprovação", ajuste=ajuste, labels=TIPOS_LABEL, erro="Informe o motivo da decisao.")
        resultado = efetivar_decisao_ajuste(ajuste["id"], decisao, motivo, user)
        if resultado is not True:
            return resultado
        execute("UPDATE aprovacao_tokens SET usado_em = ? WHERE id = ?", (now_iso(), token_row["id"]))
        return page("<div class='alert alert-success'>Decisao registrada com sucesso.</div><a class='btn btn-primary' href='{{ url_for(\"ajustes\") }}'>Voltar</a>", title="Aprovação")
    historico = query(
        "SELECT * FROM marcacoes WHERE funcionario_id = ? ORDER BY data_hora DESC LIMIT 10",
        (ajuste["funcionario_id"],),
    )
    return page_template("pages/aprovar_ajuste_token.html", title="Aprovação", ajuste=ajuste, labels=TIPOS_LABEL, historico=historico)


@app.route("/auditoria")
@login_required
@perfil_required("Administrador Principal")
def auditoria():
    rows = query("""SELECT a.*, u.nome usuario FROM auditoria a
                    LEFT JOIN usuarios u ON u.id = a.usuario_id
                    ORDER BY a.criado_em DESC LIMIT 300""")
    return page_template('pages/auditoria.html', title="Auditoria", rows=rows)


@app.route("/batidas-pendentes")
@login_required
@perfil_required("Administrador Principal", "Chefia Imediata", "Gestor", "Secretário da Pasta")
def batidas_pendentes():
    user = current_user()
    sql = """SELECT m.*, f.nome funcionario, f.matricula, c.usuario_id chefia_usuario_id, sp.id secretario_pasta_id
             FROM marcacoes m
             JOIN funcionarios f ON f.id = m.funcionario_id
             LEFT JOIN chefias c ON c.id = f.chefia_id
             LEFT JOIN secretarios_pastas sp ON sp.id = f.secretario_id
             WHERE m.status_aprovacao = 'pendente'"""
    params = []
    if user["perfil"] in ("Chefia Imediata", "Chefia imediata", "Gestor"):
        sql += " AND c.usuario_id = ?"
        params.append(user["id"])
    elif user["perfil"] in ("Secretário da Pasta", "Secretário") and user["secretario_id"]:
        sql += " AND sp.id = ?"
        params.append(user["secretario_id"])
    rows = query(sql + " ORDER BY m.data_hora DESC", params)
    return page_template('pages/batidas_pendentes.html', title="Batidas pendentes", rows=rows, labels=TIPOS_LABEL)


@app.route("/batidas-pendentes/<int:marcacao_id>/<decisao>", methods=["POST"])
@login_required
@perfil_required("Administrador Principal", "Chefia Imediata", "Gestor", "Secretário da Pasta")
def decidir_batida(marcacao_id, decisao):
    if decisao not in ("aprovado", "reprovado"):
        decisao = "reprovado"
    marcacao = one("""SELECT m.*, f.chefia_id, f.secretario_id, c.usuario_id chefia_usuario_id
                      FROM marcacoes m
                      JOIN funcionarios f ON f.id = m.funcionario_id
                      LEFT JOIN chefias c ON c.id = f.chefia_id
                      WHERE m.id = ?""", (marcacao_id,))
    user = current_user()
    if not marcacao:
        return page("<div class='alert alert-danger'>Batida não encontrada.</div>"), 404
    if user["perfil"] in ("Chefia Imediata", "Chefia imediata", "Gestor") and marcacao["chefia_usuario_id"] != user["id"]:
        return page("<div class='alert alert-danger'>Acesso negado para esta chefia.</div>"), 403
    if user["perfil"] in ("Secretário da Pasta", "Secretário") and user["secretario_id"] and marcacao["secretario_id"] != user["secretario_id"]:
        return page("<div class='alert alert-danger'>Acesso negado para esta pasta.</div>"), 403
    execute(
        "UPDATE marcacoes SET status_aprovacao = ?, decidido_por = ?, decidido_em = ?, parecer_chefia = ? WHERE id = ?",
        (decisao, user["id"], now_iso(), "Decisão registrada pela chefia", marcacao_id),
    )
    audit("decidir_batida_fora_horario", "marcacoes", marcacao_id, {"decisao": decisao})
    return redirect(url_for("batidas_pendentes"))


@app.route("/usuarios", methods=["GET", "POST"])
@login_required
@perfil_required("Administrador Principal", "RH Local")
def usuarios():
    user = current_user()
    erro = None
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        cpf = request.form.get("cpf", "").strip()
        login = request.form.get("login", "").strip()
        senha = request.form.get("senha", "")
        perfil = request.form.get("perfil", "")
        funcionario_id = request.form.get("funcionario_id") or None
        ativo = 1 if request.form.get("ativo") == "1" else 0

        if not nome or not cpf or not login or not senha or not perfil:
            erro = "Preencha todos os campos obrigatórios."
        elif not funcionario_id:
            erro = "Vincule o usuário a um funcionário."
        else:
            funcionario = funcionario_admin_row(funcionario_id, user)
            if not funcionario:
                erro = "Funcionário fora do escopo permitido para este RH local."
        if not erro and one("SELECT id FROM usuarios WHERE login = ?", (login,)):
            erro = "Já existe usuário com este login."
        if not erro:
            rh_local_id = funcionario["rh_local_id"] if funcionario else None
            execute(
                """INSERT INTO usuarios
                   (nome, cpf, login, senha, perfil, funcionario_id, rh_local_id, ativo)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (nome, cpf, login, hash_password(senha), perfil, funcionario_id, rh_local_id, ativo),
            )
            novo = one("SELECT id FROM usuarios WHERE login = ?", (login,))
            audit("criar_usuario_sistema", "usuarios", novo["id"], {"login": login, "perfil": perfil, "ativo": ativo})
            return redirect(url_for("usuarios"))

    if user["perfil"] == "RH Local" and user["rh_local_id"]:
        rows = query("""SELECT u.*, f.nome funcionario_nome FROM usuarios u
                        LEFT JOIN funcionarios f ON f.id = u.funcionario_id
                        WHERE f.rh_local_id = ?
                        ORDER BY u.nome""", (user["rh_local_id"],))
        funcionarios_rows = query("SELECT * FROM funcionarios WHERE ativo = 1 AND rh_local_id = ? ORDER BY nome", (user["rh_local_id"],))
    else:
        rows = query("""SELECT u.*, f.nome funcionario_nome FROM usuarios u
                        LEFT JOIN funcionarios f ON f.id = u.funcionario_id
                        ORDER BY u.nome""")
        funcionarios_rows = query("SELECT * FROM funcionarios WHERE ativo = 1 ORDER BY nome")
    return page_template('pages/usuarios.html', title="Usuários do Sistema", rows=rows, funcionarios_rows=funcionarios_rows, erro=erro)


def simple_crud_page(title, rows, fields, labels):
    inputs = "".join(f'<input class="form-control" name="{field}" placeholder="{label}" required>' for field, label in zip(fields, labels))
    headers = "".join(f"<th>{label}</th>" for label in labels)
    body = "".join("<tr>" + "".join(f"<td>{row[field] or ''}</td>" for field in fields) + "</tr>" for row in rows)
    return page(f"""
    <div class="mb-4"><h3 class="fw-bold">{title}</h3></div>
    <div class="row g-3">
      <div class="col-lg-4"><div class="card"><div class="card-body"><h5>Novo cadastro</h5><form method="post" class="d-grid gap-3">{csrf_input()}{inputs}<button class="btn btn-primary">Salvar</button></form></div></div></div>
      <div class="col-lg-8"><div class="card"><div class="table-responsive"><table class="table table-hover mb-0"><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></div></div></div>
    </div>
    """, title=title)


if __name__ == "__main__":
    init_db()
    host = os.environ.get("PONTO_HOST", "127.0.0.1")
    port = int(os.environ.get("PONTO_PORT", "5001"))
    ssl_context = "adhoc" if os.environ.get("PONTO_HTTPS", "0").lower() in ("1", "true", "yes") else None
    app.run(host=host, port=port, debug=False, ssl_context=ssl_context)
