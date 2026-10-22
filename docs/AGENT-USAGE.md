# Использование агентами

1. Прочитайте `gv --help`, `gv agent-info`, проверьте `gv auth status`. Не выводите файл с токеном.
2. Задавайте `-R OWNER/REPO` явно вне клона. Проверяйте репозиторий, PR и head SHA перед записью.
3. Для ревью: `gv pr context NUMBER -R OWNER/REPO --require-patches`. Учитывайте `coverage`, `missing_patches` и ограничения inline-обсуждений из README.
4. Передавайте полученный `head_sha` в `gv pr review --commit SHA`.
5. Многострочные тексты передавайте через `--body-file`, без конкатенации непроверенного текста в shell.
6. После timeout или сетевой ошибки записи сначала прочитайте состояние ресурса: сервер мог выполнить операцию. Не повторяйте её вслепую. Ошибка может содержать `stage`, `mutation_attempted`, ID и рекомендации восстановления.
7. Проверяйте код завершения и JSON ошибки в stderr. Пустой stdout после ошибки не равен пустому успешному ответу.
8. Не пересылайте PAT во внутренний API сайта и не извлекайте cookies из браузера автоматически.

```sh
gv api endpoints --search pulls
gv api schema '/repos/{owner}/{repo}/pulls/{pull_number}/reviews' -X POST
gv pr context 14 -R OWNER/REPO
gv pr checks 14 -R OWNER/REPO --ci-only --watch
gv pr create -R OWNER/REPO --head feature/example --base develop --title 'Пример' --dry-run
```

Схема поставляется с CLI и может отставать от сервера. HTTP 400/403/404 не доказывает отсутствия возможности на всей платформе. Не заявляйте, что прочитаны все замечания, если доступны только общие комментарии.

Для CI используйте `GITVERSE_TOKEN` из хранилища секретов с минимально достаточными правами. Не печатайте переменную и не включайте shell tracing при работе с ней.
