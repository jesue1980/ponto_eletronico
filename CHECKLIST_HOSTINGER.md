# Checklist de publicacao na Hostinger

## Antes do envio

- Confirmar que `requirements.txt` esta atualizado.
- Gerar uma `SECRET_KEY` longa e aleatoria.
- Definir se o banco SQLite ficara no mesmo diretorio do projeto ou em caminho absoluto.
- Fazer backup local de `ponto_eletronico.db`, `selfies/`, `anexos_ajustes/` e `backups/`.

## Variaveis de ambiente

Configurar no painel da Hostinger, quando disponivel:

- `APP_ENV=production`
- `SECRET_KEY=<chave-longa-e-aleatoria>`
- `SESSION_COOKIE_SECURE=1`
- `SESSION_COOKIE_SAMESITE=Lax`
- `PREFERRED_URL_SCHEME=https`
- `MAX_CONTENT_LENGTH=16777216`
- `SQLITE_DB_PATH=<caminho-absoluto-do-banco-se-necessario>`
- `LOG_DIR=<caminho-absoluto-para-logs-se-necessario>`
- `BEHIND_PROXY=1`, se a aplicacao estiver atras de proxy/reverso.

`DATABASE_URL` fica reservado para migracao futura para PostgreSQL.

## Arquivos de entrada

- Confirmar que `passenger_wsgi.py` esta na raiz publicada.
- Confirmar que `wsgi.py` importa `app` e expoe `application`.
- No painel Python da Hostinger, apontar a aplicacao para `passenger_wsgi.py`.

## Permissoes de escrita

Garantir permissao de escrita para:

- `ponto_eletronico.db`
- `selfies/`
- `anexos_ajustes/`
- `backups/`
- `logs/`

## Dominio e HTTPS

- Apontar o dominio para a hospedagem.
- Ativar SSL/HTTPS no painel da Hostinger.
- Confirmar acesso por `https://seudominio`.
- Manter `SESSION_COOKIE_SECURE=1` somente com HTTPS ativo.

## Testes apos deploy

- Abrir `/login`.
- Entrar com um usuario valido.
- Abrir `/dashboard`.
- Abrir `/registrar` e confirmar carregamento da tela.
- Testar uma rota inexistente e confirmar pagina 404 amigavel.
- Abrir `/relatorios`.
- Abrir `/ajustes`.
- Abrir `/batidas-pendentes`.
- Confirmar que `logs/app.log` esta sendo criado.

## Backup e rollback

- Fazer backup do banco antes de cada publicacao.
- Guardar copia da versao anterior do projeto.
- Para rollback, restaurar arquivos da versao anterior e o banco salvo.
