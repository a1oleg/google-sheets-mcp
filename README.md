# Google Sheets MCP

MCP-сервер для детерминированной работы AI-агентов с конкретными таблицами,
листами и диапазонами Google Sheets. Реализован на Python + FastMCP, использует
stdio, Google Sheets API v4 и Google Drive API v3 через service account.

В отличие от UI-автоматизации и высокоуровневых коннекторов, сервер предоставляет
явный контракт `spreadsheetId + sheet/range`: агент может сослаться на конкретный
A1-диапазон, выполнить пакетное изменение и получить проверяемый результат.

## Установка (Windows, Python 3.12+)

```powershell
uv venv --python 3.12
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
Copy-Item .env.example .env
```

Если `.env` уже существует, не перезаписывайте его. Заполните поля GOOGLE_* из
учётных данных service account. В GOOGLE_PRIVATE_KEY допустимы экранированные
переносы `\n`. Не отправляйте ключи в чат или Git. `.env` исключён из Git.

В Google Cloud должны быть включены Sheets API и Drive API. Предоставьте доступ
к нужной таблице адресу GOOGLE_CLIENT_EMAIL: просмотр для чтения, редактирование
для записи. Запрошенные области доступа: spreadsheets и drive.readonly.

Для production-подобного использования задайте `GOOGLE_ALLOWED_SPREADSHEET_IDS`
как список разрешённых ID через запятую. Ограничения размера изменений задаются
через `GOOGLE_MAX_WRITE_CELLS` и `GOOGLE_MAX_INSERT_ROWS`.
При включённом allowlist discovery возвращает только перечисленные таблицы и не
раскрывает названия остальных документов, доступных service account.

## Проверка и запуск

```powershell
.venv/Scripts/python.exe server.py --check
.venv/Scripts/python.exe server.py
```

`--check` читает список доступных таблиц, ничего не меняет. Обычный запуск
обслуживает MCP по stdio. В VS Code откройте эту папку или добавьте её в workspace:
конфигурация находится в `.vscode/mcp.json`. Подтвердите доверие серверу при запросе.
На Linux/macOS используйте `.venv/bin/python` в командах и конфигурации MCP.

## Инструменты

- check_connection, list_spreadsheets, list_sheets, get_spreadsheet_info;
- get_sheet_data, get_sheet_data_by_notation, resolve_chat_notation;
- update_cells, batch_update_cells, insert_rows — изменяют таблицы.

Везде передаётся spreadsheet_id. Поддерживаются стандартные диапазоны A1
и две формы короткой нотации:

- `!A1:G5` — первый лист, `!!A1:G5` — второй. Это удобно в диалоге, но зависит
  от текущего порядка вкладок;
- `@123456!A1:G5` — лист с постоянным Google `sheetId=123456`. Эта форма устойчива
  к переименованию и перестановке вкладок.

Инструменты записи принимают данные в JSON; индексы вставки строк начинаются с
нуля. У всех мутаций есть `dry_run`, а необязательный `request_id` связывает
операцию с внешним trace. Аудит мутаций пишется в stderr и не нарушает stdio MCP.

Это не read-only сервер. MCP-клиент должен запрашивать подтверждение изменений.
Сервисный аккаунт видит только предоставленные ему ресурсы, а allowlist позволяет
дополнительно ограничить доступ внутри этого набора.

## Проверки

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

Тесты не требуют доступа к Google и проверяют разрешение адресов, стабильную
нотацию `sheetId`, allowlist и лимиты мутаций.

## Архитектура и границы

- Сервер не хранит содержимое таблиц и учётные данные вне `.env`.
- stdout зарезервирован для MCP; аудит изменений отправляется в stderr.
- A1-диапазон передаётся Google Sheets API без интерпретации сервером.
- Позиционная `!`-нотация предназначена для удобства, а `@sheetId` — для
  воспроизводимых автоматизаций.
