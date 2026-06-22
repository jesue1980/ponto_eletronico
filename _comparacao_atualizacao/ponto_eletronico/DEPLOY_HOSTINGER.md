# Deploy na Hostinger

Este projeto esta preparado para WSGI via `passenger_wsgi.py`.

Variaveis recomendadas em producao:

- `APP_ENV=production`
- `SECRET_KEY`: chave longa e aleatoria. Nao use o valor de desenvolvimento.
- `SESSION_COOKIE_SECURE=1`: use quando o site estiver em HTTPS.
- `SESSION_COOKIE_SAMESITE=Lax`
- `PREFERRED_URL_SCHEME=https`
- `MAX_CONTENT_LENGTH=16777216`
- `SQLITE_DB_PATH`: caminho absoluto do banco SQLite, se necessario.
- `LOG_DIR`: caminho para gravacao de logs, se necessario.
- `LOG_LEVEL=INFO`
- `BEHIND_PROXY=1`: use quando a aplicacao estiver atras do proxy da hospedagem.
- `SERVER_NAME`: dominio da aplicacao, somente se a hospedagem exigir.
- `DATABASE_URL`: reservado para futura migracao para PostgreSQL.

Entrada WSGI:

```python
from wsgi import application
```

O banco atual permanece SQLite. A migracao real para PostgreSQL deve ser feita em uma etapa propria, com migracao de schema e dados.

Diretorios que precisam de permissao de escrita:

- `selfies/`
- `anexos_ajustes/`
- `backups/`
- `logs/`

Com HTTPS ativo no painel da Hostinger, mantenha `SESSION_COOKIE_SECURE=1`.
