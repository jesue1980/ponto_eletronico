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
import shutil
import sqlite3
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
ANEXO_DIR = os.path.join(BASE_DIR, "anexos_ajustes")
DEFAULT_SECRET_KEY = "ponto-eletronico-repp-dev"
PASSWORD_HASH_PREFIXES = ("pbkdf2:", "scrypt:")
ALLOWED_ANEXO_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".doc", ".docx", ".xls", ".xlsx", ".txt"
}

app = Flask(__name__)
app.config.from_object(get_config())
app.secret_key = app.config.get("SECRET_KEY", DEFAULT_SECRET_KEY)
DATABASE_URL = app.config.get("DATABASE_URL", DATABASE_URL)
DB_PATH = app.config.get("SQLITE_DB_PATH", DB_PATH)
if app.config.get("BEHIND_PROXY"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


def ensure_runtime_dirs():
    for path in (SELFIE_DIR, ANEXO_DIR, BACKUP_DIR, app.config["LOG_DIR"]):
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
    return value if value else None


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
            local_id INTEGER,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            raio_metros INTEGER NOT NULL DEFAULT 100,
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL,
            FOREIGN KEY (local_id) REFERENCES locais_trabalho(id)
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
        CREATE INDEX IF NOT EXISTS idx_compensacoes_funcionario_data ON compensacoes (funcionario_id, data);
        CREATE INDEX IF NOT EXISTS idx_usuarios_funcionario ON usuarios (funcionario_id);
        CREATE INDEX IF NOT EXISTS idx_locais_rh_ativo ON locais_trabalho (rh_local_id, ativo);
        CREATE INDEX IF NOT EXISTS idx_funcionario_locais_func ON funcionario_locais_autorizados (funcionario_id, ativo);
        CREATE INDEX IF NOT EXISTS idx_funcionario_locais_local ON funcionario_locais_autorizados (local_id, ativo);
        CREATE INDEX IF NOT EXISTS idx_totens_ativo ON totens (ativo);
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
        add_column("funcionarios", "rh_local_id", "INTEGER")
        add_column("funcionarios", "chefia_id", "INTEGER")
        add_column("funcionarios", "secretario_id", "INTEGER")
        add_column("funcionarios", "foto_base_path", "TEXT")
        add_column("funcionarios", "reconhecimento_facial_ativo", "INTEGER NOT NULL DEFAULT 0")
        add_column("funcionarios", "permite_totem_facial", "INTEGER NOT NULL DEFAULT 0")
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
        add_column("ajustes_ponto", "anexo_path", "TEXT")
        add_column("ajustes_ponto", "chefia_id", "INTEGER")

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
    diferenca = abs(time_to_minutes(batido) - time_to_minutes(previsto))
    return diferenca > int(jornada["tolerancia_minutos"] or 0), previsto, diferenca


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
        atraso = max(0, time_to_minutes(marks["entrada"]) - (time_to_minutes(jornada["entrada"]) + jornada["tolerancia_minutos"]))
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


def save_selfie(data_url, funcionario_id):
    if not data_url or "," not in data_url:
        return None
    header, encoded = data_url.split(",", 1)
    if "image" not in header:
        return None
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
    filename = f"base_{funcionario_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}{extension}"
    path = os.path.join(SELFIE_DIR, filename)
    file_storage.save(path)
    return os.path.join("selfies", filename)


def save_foto_base_data_url(data_url, funcionario_id):
    if not data_url or "," not in data_url:
        return None
    header, encoded = data_url.split(",", 1)
    if "image" not in header:
        return None
    filename = f"base_{funcionario_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"
    path = os.path.join(SELFIE_DIR, filename)
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(encoded))
    return os.path.join("selfies", filename)


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


def nav_links(css):
    links = [
        ("dashboard", "bi-grid", "Dashboard"),
        ("relatorios", "bi-bar-chart", "Relatórios"),
        ("ajustes", "bi-pencil-square", "Ajustes de Ponto"),
    ]
    user = current_user()
    if user and user["perfil"] in ("Administrador Principal", "Chefia Imediata", "Chefia imediata", "Gestor", "Secretário da Pasta", "Secretário"):
        links.append(("batidas_pendentes", "bi-clock-history", "Aprovações da Chefia"))
    if user and user["funcionario_id"]:
        links.insert(1, ("registrar_ponto", "bi-fingerprint", "Registrar ponto"))
    if user and user["perfil"] in ("Administrador Principal", "RH Local"):
        links += [
            ("funcionarios", "bi-person-vcard", "Funcionários"),
            ("locais", "bi-geo-alt", "Locais de Trabalho"),
        ]
    if user and user["perfil"] == "RH Local":
        links += [
            ("jornadas", "bi-calendar2-week", "Horários de Trabalho"),
        ]
    if user and user["perfil"] == "Administrador Principal":
        links += [
            ("jornadas", "bi-calendar2-week", "Horários de Trabalho"),
            ("chefias", "bi-person-check", "Chefias"),
            ("rh_locais", "bi-people", "RH Local"),
            ("secretarios", "bi-briefcase", "Secretários"),
            ("justificativas", "bi-chat-square-text", "Justificativas"),
            ("empresas", "bi-building", "Empresas"),
            ("totens", "bi-tablet", "Totens"),
            ("usuarios", "bi-person-gear", "Usuários"),
            ("permissoes", "bi-key", "Permissões"),
            ("configuracoes", "bi-gear", "Configurações"),
            ("auditoria", "bi-shield-lock", "Auditoria"),
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
    funcionarios_count = one("SELECT COUNT(*) total FROM funcionarios WHERE ativo = 1")["total"]
    marcacoes_hoje = one("SELECT COUNT(*) total FROM marcacoes WHERE data_hora >= ? AND data_hora < ?", (inicio_hoje, fim_hoje))["total"]
    pendentes = one("SELECT COUNT(*) total FROM ajustes_ponto WHERE status = 'pendente'")["total"]
    linhas = build_report(today.replace(day=1), today, "")
    saldo = sum(row["saldo_min"] for row in linhas)
    extras = sum(row["extras_min"] for row in linhas)
    faltas = sum(1 for row in linhas if row["falta"])
    atrasos = sum(row["atraso_min"] for row in linhas)
    return page_template('pages/dashboard.html', title="Dashboard", funcionarios_count=funcionarios_count, marcacoes_hoje=marcacoes_hoje,
    pendentes=pendentes, saldo=saldo, extras=extras, faltas=faltas, atrasos=atrasos, fmt=fmt_minutes)


def allowed_funcionarios_for_user(user):
    if user["perfil"] == "Funcionário":
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


def validar_totem_facial(funcionario, totem_id, latitude, longitude):
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
        """SELECT id, nome, matricula, foto_base_path
           FROM funcionarios
           WHERE ativo = 1
             AND permite_totem_facial = 1
             AND reconhecimento_facial_ativo = 1
             AND foto_base_path IS NOT NULL
           ORDER BY nome"""
    )
    funcionarios = []
    for row in rows:
        funcionarios.append({
            "id": row["id"],
            "nome": row["nome"],
            "matricula": row["matricula"],
            "foto_url": url_for("foto_facial_base", funcionario_id=row["id"]),
        })
    app.logger.info("Totem facial frontend: %s fotos base liberadas para face-api.js", len(funcionarios))
    return {
        "funcionarios": funcionarios,
        "total": len(funcionarios),
        "total_fotos_base_carregadas": len(funcionarios),
    }


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
    return send_from_directory(SELFIE_DIR, filename)


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
            """SELECT f.*, l.latitude, l.longitude
               FROM funcionarios f
               JOIN locais_trabalho l ON l.id = f.local_id
               WHERE f.id = ? AND f.ativo = 1 AND l.ativo = 1""",
            (funcionario_id,),
        )
        if not funcionario:
            app.logger.warning("Totem facial teste com funcionario invalido: %s", funcionario_id)
            return {"erro": "Funcionario ativo nao encontrado."}, 404
        if similaridade_facial is None:
            _func_reconhecido, similaridade_facial, diagnostico_facial = reconhecer_funcionario_por_foto(selfie)

        geolocalizacao_ok, totem, detalhes_geo = validar_totem_facial(funcionario, totem_id, latitude, longitude)
        detalhes_geo["similaridade_facial"] = similaridade_facial
        detalhes_geo["diagnostico_facial"] = diagnostico_facial
        detalhes_geo["liveness_score"] = round(liveness_score, 2)
        detalhes_geo["horario"] = now_iso()
        if not geolocalizacao_ok:
            audit_totem_geolocalizacao(funcionario["id"], False, detalhes_geo)
            app.logger.warning("Totem facial bloqueado por geolocalizacao: %s", detalhes_geo)
            return {
                "erro": "Local não autorizado para registro de ponto",
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

        tipo = next_tipo(funcionario["id"])
        nsr = datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6].upper()
        data_hora_batida = now_iso()
        selfie_path = save_selfie(selfie, funcionario["id"])
        origem = ORIGEM_TOTEM_FACIAL
        payload = {
            "nsr": nsr,
            "funcionario_id": funcionario["id"],
            "tipo": tipo,
            "data_hora": data_hora_batida,
            "origem": origem,
            "dispositivo_id": dispositivo_id,
        }
        hash_registro = create_hash(payload)

        execute(
            """INSERT INTO marcacoes
               (nsr, funcionario_id, tipo, data_hora, latitude, longitude, precisao, distancia_metros,
                dentro_cerca, selfie_path, dispositivo_id, user_agent, ip, hash_registro,
                status_aprovacao, origem, origem_normalizada, totem_id, local_validacao_id,
                geolocalizacao_status, distancia_validacao_metros, criado_em)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                nsr,
                funcionario["id"],
                tipo,
                data_hora_batida,
                latitude,
                longitude,
                request.form.get("precisao"),
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
            distancia = haversine_m(lat, lon, local["latitude"], local["longitude"])
            if distancia > local["raio_metros"]:
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
                    "latitude": lat,
                    "longitude": lon,
                    "dispositivo_id": request.form.get("dispositivo_id"),
                    "status_aprovacao": status_aprovacao,
                }
                hash_registro = create_hash(payload)
                execute(
                    """INSERT INTO marcacoes
                       (nsr, funcionario_id, tipo, data_hora, latitude, longitude, precisao, distancia_metros,
                        dentro_cerca, selfie_path, dispositivo_id, user_agent, ip, hash_registro,
                        justificativa_fora_horario, status_aprovacao, horario_previsto, origem, origem_normalizada,
                        geolocalizacao_status, distancia_validacao_metros, criado_em)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (nsr, funcionario["id"], tipo, payload["data_hora"], lat, lon, request.form.get("precisao"),
                     distancia, 1, selfie_path, request.form.get("dispositivo_id"), request.headers.get("User-Agent", ""),
                     request.remote_addr, hash_registro, justificativa_fora_horario, status_aprovacao, horario_previsto,
                     ORIGEM_MANUAL, ORIGEM_MANUAL, "aprovado", distancia, now_iso()),
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
    usuarios_rows = query("SELECT * FROM usuarios WHERE perfil = 'Gestor' AND ativo = 1 ORDER BY nome")
    if request.method == "POST":
        usuario_id = request.form.get("usuario_id") or None
        execute("INSERT INTO chefias (empresa_id, nome, email, cargo, usuario_id) VALUES (?, ?, ?, ?, ?)",
                (request.form["empresa_id"], request.form["nome"], request.form.get("email"), request.form.get("cargo"), usuario_id))
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
        execute("INSERT INTO locais_trabalho (empresa_id, rh_local_id, nome, latitude, longitude, raio_metros) VALUES (?, ?, ?, ?, ?, ?)",
                (request.form["empresa_id"], rh_local_id, request.form["nome"], request.form["latitude"], request.form["longitude"], request.form["raio_metros"]))
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
            """INSERT INTO totens (nome, local_id, latitude, longitude, raio_metros, ativo, criado_em)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                request.form["nome"],
                request.form.get("local_id") or None,
                request.form["latitude"],
                request.form["longitude"],
                request.form["raio_metros"],
                ativo,
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
        execute("""INSERT INTO jornadas
                   (nome, carga_minutos, entrada, saida_almoco, retorno_almoco, saida_final, tolerancia_minutos, tipo_escala, data_inicio_escala)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request.form["nome"], carga, request.form["entrada"], parse_time_str(request.form.get("saida_almoco")),
                 parse_time_str(request.form.get("retorno_almoco")), request.form["saida_final"],
                 request.form["tolerancia_minutos"], request.form["tipo_escala"], request.form.get("data_inicio_escala") or None))
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
        entrada = request.form.get("horario_entrada") or None
        saida_almoco = request.form.get("horario_saida_almoco") or None
        retorno_almoco = request.form.get("horario_retorno_almoco") or None
        saida_final = request.form.get("horario_saida_final") or None
        if not login_novo or not senha_nova or not confirmar_senha or not perfil_acesso:
            return page("<div class='alert alert-danger'>Informe login, senha, confirmação de senha e perfil de acesso.</div><a class='btn btn-primary' href='{{ url_for(\"funcionarios\") }}'>Voltar</a>", title="Funcionários")
        if senha_nova != confirmar_senha:
            return page("<div class='alert alert-danger'>Senha e confirmação de senha não conferem.</div><a class='btn btn-primary' href='{{ url_for(\"funcionarios\") }}'>Voltar</a>", title="Funcionários")
        if one("SELECT id FROM usuarios WHERE login = ?", (login_novo,)):
            return page("<div class='alert alert-danger'>Já existe usuário com este login.</div><a class='btn btn-primary' href='{{ url_for(\"funcionarios\") }}'>Voltar</a>", title="Funcionários")
        jornada_id = request.form["jornada_id"]
        if entrada and saida_final:
            carga = minutes_between(entrada, saida_almoco) + minutes_between(retorno_almoco, saida_final) if saida_almoco and retorno_almoco else minutes_between(entrada, saida_final)
            nome_jornada = f"{request.form['matricula']} - {request.form.get('tipo_jornada', 'Jornada personalizada')}"
            execute(
                """INSERT INTO jornadas
                   (nome, carga_minutos, entrada, saida_almoco, retorno_almoco, saida_final, tolerancia_minutos, tipo_escala, padrao)
                   VALUES (?, ?, ?, ?, ?, ?, 10, 'dias_uteis', 0)""",
                (nome_jornada, carga, entrada, saida_almoco, retorno_almoco, saida_final),
            )
            jornada_id = one("SELECT id FROM jornadas WHERE nome = ?", (nome_jornada,))["id"]
        permite_totem_facial = 1 if request.form.get("permite_totem_facial") == "1" else 0
        reconhecimento_facial_ativo = 1 if request.form.get("reconhecimento_facial_ativo") == "1" else 0
        execute("""INSERT INTO funcionarios
                   (empresa_id, local_id, jornada_id, nome, cpf, matricula, cargo, email, telefone, data_admissao,
                    rh_local_id, chefia_id, secretario_id, reconhecimento_facial_ativo, permite_totem_facial)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request.form["empresa_id"], request.form["local_id"], jornada_id, request.form["nome"],
                 request.form["cpf"], request.form["matricula"], request.form.get("cargo"), request.form.get("email"),
                 request.form.get("telefone"), request.form.get("data_admissao"), rh_local_id,
                 request.form.get("chefia_id"), request.form.get("secretario_id"), reconhecimento_facial_ativo,
                 permite_totem_facial))
        novo_funcionario = one("SELECT * FROM funcionarios WHERE matricula = ?", (request.form["matricula"],))
        foto_base_path = save_foto_base(request.files.get("foto_base"), novo_funcionario["id"])
        if foto_base_path:
            execute("UPDATE funcionarios SET foto_base_path = ? WHERE id = ?", (foto_base_path, novo_funcionario["id"]))
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
        audit("criar", "funcionarios", detalhes={"matricula": request.form["matricula"]})
        return redirect(url_for("funcionarios"))
    sql_rows = """SELECT f.*, e.nome_fantasia empresa, l.nome local, j.nome jornada,
                           j.entrada, j.saida_almoco, j.retorno_almoco, j.saida_final,
                           rh.nome rh_local, c.nome chefia, sp.pasta secretario_pasta, sp.nome secretario
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

    foto_base_path = save_foto_base_data_url(request.form.get("foto_base"), funcionario_id)
    if not foto_base_path:
        app.logger.warning("Foto facial base invalida para funcionario_id=%s", funcionario_id)
        return {"erro": "Foto facial invalida."}, 400

    execute(
        """UPDATE funcionarios
           SET foto_base_path = ?, reconhecimento_facial_ativo = 1
           WHERE id = ?""",
        (foto_base_path, funcionario_id),
    )
    audit("atualizar_foto_facial", "funcionarios", funcionario_id, {"foto_base_path": foto_base_path})
    return {"ok": True, "foto_base_path": foto_base_path, "mensagem": "Foto facial cadastrada com sucesso"}


def build_report(start, end, funcionario_id):
    sql = """SELECT f.*, j.nome jornada_nome, j.carga_minutos, j.entrada, j.saida_almoco, j.retorno_almoco,
                    j.saida_final, j.tolerancia_minutos, j.tipo_escala, j.data_inicio_escala
             FROM funcionarios f JOIN jornadas j ON j.id = f.jornada_id WHERE f.ativo = 1"""
    params = []
    user = current_user()
    if user and user["perfil"] == "Funcionário":
        sql += " AND f.id = ?"
        params.append(user["funcionario_id"])
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
    funcionario_id = request.args.get("funcionario_id", "")
    linhas = build_report(start, end, funcionario_id)
    funcionarios_rows = allowed_funcionarios_for_user(current_user())
    totals = {
        "prevista": sum(r["prevista_min"] for r in linhas),
        "trabalhada": sum(r["trabalhada_min"] for r in linhas),
        "saldo": sum(r["saldo_min"] for r in linhas),
        "extras": sum(r["extras_min"] for r in linhas),
        "atraso": sum(r["atraso_min"] for r in linhas),
        "faltas": sum(1 for r in linhas if r["falta"]),
    }
    return page_template('pages/relatorios.html', title="Relatórios", linhas=linhas, totals=totals, funcionarios_rows=funcionarios_rows,
    funcionario_id=funcionario_id, start=start, end=end, fmt=fmt_minutes,
    export_url=lambda formato: url_for("exportar", formato=formato, data_inicio=start.isoformat(), data_fim=end.isoformat(), funcionario_id=funcionario_id))


@app.route("/exportar/<formato>")
@login_required
def exportar(formato):
    start = parse_date(request.args.get("data_inicio"), date.today().replace(day=1))
    end = parse_date(request.args.get("data_fim"), date.today())
    linhas = build_report(start, end, request.args.get("funcionario_id", ""))
    if formato == "excel":
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Data", "Funcionario", "Jornada", "Prevista", "Trabalhada", "Saldo", "Extras", "Atraso", "Falta"])
        for l in linhas:
            writer.writerow([l["data"].isoformat(), l["funcionario"]["nome"], l["jornada"]["nome"], l["prevista"], l["trabalhada"], l["saldo"], l["extras"], l["atraso"], "Sim" if l["falta"] else "Não"])
        return Response(output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=relatorio_ponto.csv"})
    if formato == "afd":
        inicio, fim = period_bounds(start, end)
        rows = query("SELECT m.*, f.cpf FROM marcacoes m JOIN funcionarios f ON f.id = m.funcionario_id WHERE m.data_hora >= ? AND m.data_hora < ? ORDER BY m.data_hora", (inicio, fim))
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
        funcionario = one("SELECT * FROM funcionarios WHERE id = ?", (funcionario_id,))
        anexo_path = save_anexo(request.files.get("anexo"), funcionario_id)
        justificativa = request.form.get("justificativa", "").strip()
        justificativa_padrao = request.form.get("justificativa_padrao", "").strip()
        texto_justificativa = " - ".join([item for item in (justificativa_padrao, justificativa) if item])
        execute("""INSERT INTO ajustes_ponto
                   (funcionario_id, tipo, data_hora_solicitada, justificativa, anexo_path, chefia_id, solicitado_por, criado_em)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (funcionario_id, request.form["tipo"], request.form["data_hora_solicitada"], texto_justificativa,
                 anexo_path, funcionario["chefia_id"], user["id"], now_iso()))
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
    elif user["perfil"] in ("Chefia Imediata", "Chefia imediata", "Gestor"):
        sql += " WHERE c.usuario_id = ?"
        params.append(user["id"])
    elif user["perfil"] in ("Secretário da Pasta", "Secretário") and user["secretario_id"]:
        sql += " WHERE f.secretario_id = ?"
        params.append(user["secretario_id"])
    rows = query(sql + " ORDER BY a.criado_em DESC", params)
    return page_template('pages/ajustes.html', title="Ajustes", rows=rows, funcionarios_rows=funcionarios_rows, labels=TIPOS_LABEL,
    justificativas_rows=justificativas_rows, can_approve=user["perfil"] in ("Administrador Principal", "Chefia Imediata", "Chefia imediata", "Gestor", "Secretário da Pasta", "Secretário", "RH Local"))


@app.route("/ajustes/<int:ajuste_id>/<decisao>", methods=["POST"])
@login_required
@perfil_required("Administrador Principal", "Chefia Imediata", "Gestor", "RH Local", "Secretário da Pasta")
def decidir_ajuste(ajuste_id, decisao):
    if decisao not in ("aprovado", "rejeitado"):
        decisao = "rejeitado"
    ajuste = one("""SELECT a.*, f.local_id, f.chefia_id FROM ajustes_ponto a
                    JOIN funcionarios f ON f.id = a.funcionario_id WHERE a.id = ?""", (ajuste_id,))
    user = current_user()
    if user["perfil"] in ("Chefia Imediata", "Chefia imediata", "Gestor"):
        chefia = one("SELECT * FROM chefias WHERE id = ?", (ajuste["chefia_id"],))
        if not chefia or chefia["usuario_id"] != user["id"]:
            return page("<div class='alert alert-danger'>Acesso negado para esta chefia.</div>"), 403
    if user["perfil"] in ("Secretário da Pasta", "Secretário") and user["secretario_id"]:
        funcionario_ajuste = one("SELECT secretario_id FROM funcionarios WHERE id = ?", (ajuste["funcionario_id"],))
        if not funcionario_ajuste or funcionario_ajuste["secretario_id"] != user["secretario_id"]:
            return page("<div class='alert alert-danger'>Acesso negado para esta pasta.</div>"), 403
    execute("UPDATE ajustes_ponto SET status = ?, aprovado_por = ?, decidido_em = ? WHERE id = ?",
            (decisao, session["user_id"], now_iso(), ajuste_id))
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
    audit("decidir_ajuste", "ajustes_ponto", ajuste_id, {"decisao": decisao})
    return redirect(url_for("ajustes"))


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
        elif one("SELECT id FROM usuarios WHERE login = ?", (login,)):
            erro = "Já existe usuário com este login."
        else:
            execute(
                """INSERT INTO usuarios
                   (nome, cpf, login, senha, perfil, funcionario_id, ativo)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (nome, cpf, login, hash_password(senha), perfil, funcionario_id, ativo),
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
    app.run(host="127.0.0.1", port=5001, debug=False)
