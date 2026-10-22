# gv — GitVerse CLI

Неофициальный CLI для GitVerse: репозитории, pull requests, ревью, issues и CI/CD из терминала. Подходит для людей, скриптов и кодовых агентов. Команды возвращают JSON; runtime использует только стандартную библиотеку Python.

Код размещён на GitHub, команды работают с **GitVerse**. Это не официальный продукт GitVerse и не полная замена `gh`.

## Установка

Нужны Python **3.10+**, Git и доступ к GitVerse. Рекомендуется [pipx](https://pipx.pypa.io/stable/installation/):

```sh
pipx install 'git+https://github.com/denden121/gv.git@v0.27.0'
gv --version
gv --help
```

Если команда не найдена, выполните `pipx ensurepath` и откройте новый терминал.

Альтернатива — виртуальное окружение:

```sh
git clone https://github.com/denden121/gv.git
cd gv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
gv --version
```

В Windows PowerShell активация: `.venv\Scripts\Activate.ps1`; для создания окружения можно использовать `py -3 -m venv .venv`. Отдельные функции с POSIX-процессами и файловыми блокировками могут требовать Linux, macOS или WSL. Конфигурация CI рассчитана на Linux; см. CONTRIBUTING.md.

Обновление до текущего `main`:

```sh
pipx install --force 'git+https://github.com/denden121/gv.git@main'
```

Для воспроизводимой установки используйте конкретный тег вместо `main`.

## Авторизация

Создайте Personal Access Token в настройках своего аккаунта GitVerse с правами для нужных репозиториев и операций. GitHub-токен не подходит. Каждый пользователь использует **свой PAT**.

Передайте токен через stdin без записи в историю команд:

```sh
python3 -c 'import getpass; print(getpass.getpass("GitVerse PAT: "))' | gv auth login --with-token
gv auth status
```

Токен проверяется и сохраняется в `~/.config/gitverse-agent-cli/auth.json` с правами `0600` (учитывается `XDG_CONFIG_HOME`). Это обычный файл с токеном, не системное хранилище паролей. `gv auth logout` удаляет локальную запись; отозвать PAT можно в GitVerse.

Для CI передавайте секрет через `GITVERSE_TOKEN` (или `GV_TOKEN`). Переменная окружения имеет приоритет над сохранённым токеном. Не включайте токены в URL, аргументы команд, YAML и репозиторий.

## Первые команды

Замените `OWNER/REPO` и номер PR своими значениями:

```sh
gv repo view -R OWNER/REPO
gv pr list -R OWNER/REPO
gv pr view 14 -R OWNER/REPO
gv pr context 14 -R OWNER/REPO
gv pr diff 14 -R OWNER/REPO
gv pr comments 14 -R OWNER/REPO
gv pr reviews 14 -R OWNER/REPO
gv pr checks 14 -R OWNER/REPO --ci-only
gv run list -R OWNER/REPO
```

В Git-клоне с одним GitVerse remote `-R` можно опустить. Вне клона указывайте его явно. Подробности селекторов PR: `gv pr view --help`.

Создание PR из **уже отправленной** ветки:

```sh
gv pr create -R OWNER/REPO --head feature/my-change --base develop \
  --title 'Описание изменения' --body-file pr-description.md
```

Комментарий и ревью:

```sh
gv pr comment 14 -R OWNER/REPO --body-file comment.md
gv pr review 14 -R OWNER/REPO --comment --commit REVIEWED_SHA --body-file review.md
```

Замените `REVIEWED_SHA` значением `head_sha` из `pr context`. Команды записи меняют GitVerse. `--dry-run` показывает план без выполнения, но не подтверждает права или успешность будущей операции.

## Возможности и ограничения

- `repo`, `org`, `team`: репозитории, организации, команды.
- `pr`, `issue`, `label`, `search`: PR, ревью, задачи, метки, поиск.
- `run`, `workflow`, `secret`, `variable`: CI/CD и настройки.
- `release`, `api`, `traffic`: релизы, API discovery, локальный анализ HAR.
- `agent-info`: машиночитаемые возможности и ограничения.

**Inline-замечания:** `pr comments` читает общие комментарии, `pr reviews` — итоги ревью. `pr context` не гарантирует полноту inline-обсуждений. `pr threads --backend web` обращается к внутреннему API с отдельными cookies; стабильность и работа сессии не гарантируются. PAT не заменяет cookies. Запрос разработчикам платформы: [TSKBG_RPRT-197](https://gitverse.ru/gitverse/bug_report/tasktracker/TSKBG_RPRT-197).

**CI:** `--ci-only` проверяет видимые CI-запуски, но не все правила защиты ветки и не разрешение на merge.

**Issues:** публичный issues API и новый веб-трекер могут показывать разные наборы задач. Пустой ответ API не доказывает отсутствие задач на сайте.

Часть web-операций экспериментальна. Поддержку конкретных флагов проверяйте через `--help` и `gv agent-info`; совместимость с GitHub/Gitea не предполагается.

## Документация

- [Агенты и автоматизация](docs/AGENT-USAGE.md)
- [Разработка и тесты](CONTRIBUTING.md)
- [Безопасность](SECURITY.md)

Тесты используют фикстуры и подмену API; они не подтверждают доступность всех операций на живом GitVerse. Частные журналы разработки и привязанные к ним live-скрипты в дистрибутив не входят.

Лицензия CLI — [MIT](LICENSE). OpenAPI GitVerse сохраняет [собственную MIT-лицензию](gitverse_cli/data/OPENAPI-LICENSE); [источник спецификации](https://gitverse.ru/gitverse/rest-api-description).
