# Разработка

Python 3.10+, Git. Runtime не имеет сторонних зависимостей.

```sh
git clone https://github.com/denden121/gv.git
cd gv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
python -m pip wheel --no-deps . -w dist
```

Сборка пакета требует setuptools. Тесты не требуют PAT и не должны менять пользовательские репозитории. Live-проверки выполняйте отдельно в специально разрешённых тестовых репозиториях.

Новые команды возвращают JSON, сохраняют разделение авторизации, сообщают о неполных результатах и не повторяют запросы записи автоматически. Проверяйте контракт API и добавляйте тесты успешных, ошибочных и неполных ответов.

Не публикуйте токены, cookies, HAR, приватные ответы API и журналы. Присылайте issues и PR в этот GitHub-репозиторий; ошибки платформы — в поддержку GitVerse.

## GitHub Actions

Готовая конфигурация: `docs/github-actions.yml.example` (Linux, Python 3.10 и 3.13). Для включения скопируйте её в `.github/workflows/ci.yml` и отправьте через авторизацию с правом `workflow`. При первой публикации это право отсутствовало, поэтому Actions ещё не включён.
