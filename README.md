# CaseSolve / Кейсовл

Telegram-бот для организации судов между участниками группы.

## Запуск на Render

1. Загрузите содержимое этого архива в новый GitHub-репозиторий или загрузите архив в свой проект.
2. В Render выберите **New + → Background Worker**.
3. Подключите репозиторий.
4. Render автоматически прочитает `render.yaml`.
5. В разделе Environment добавьте секрет:

   ```text
   TELEGRAM_BOT_TOKEN=токен_от_BotFather
   ```

6. Запустите Deploy.

## Важно

Добавьте бота в группу и выдайте ему права администратора:

- удаление сообщений;
- ограничение участников;
- блокировка и разблокировка участников;
- просмотр участников.

После добавления выполните в группе:

```text
/setup
```

Основные команды:

```text
/start
/lawsuit
! Суд
/setup
/status
```

Файл `casesolve.sqlite3` создаётся автоматически. Для сохранения данных между перезапусками Render подключите Persistent Disk и укажите:

```text
DATABASE_PATH=/var/data/casesolve.sqlite3
```

## Локальный запуск

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="токен"
python -m bot.main
```