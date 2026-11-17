# Инструкция для оператора и администратора

Пайплайн "Playerok Клипы". Все команды выполняются на сервере в папке `/opt/clips`
от пользователя `clips`, например:

```bash
sudo -u clips /opt/clips/.venv/bin/python -m clips <команда> [--dry-run]
```

`--dry-run` ничего не публикует, ничего не пишет в базу и не шлёт сообщения: только показывает, что было бы сделано.

## 0. Первая установка сервера (один раз)

1. Пакеты и код:
   ```bash
   sudo apt update && sudo apt install -y ffmpeg python3-venv python3-pip sqlite3 fonts-dejavu-core xvfb git
   sudo useradd -m -s /bin/bash clips
   sudo git clone https://github.com/Dev-In-Crypt/BruceBanner.git /opt/clips && sudo chown -R clips:clips /opt/clips
   cd /opt/clips && sudo -u clips python3 -m venv .venv && sudo -u clips .venv/bin/pip install -r requirements.txt
   sudo -u clips cp config.example.toml config.toml && sudo -u clips cp .env.example .env && sudo chmod 600 .env
   ```
2. Заполнить `.env` (ключи: AdsPower, Apify, Telegram, Notion) и `config.toml` (аккаунты, доноры, баннеры).
   Положить файлы в `assets/`: баннеры Playerok и фон каждого аккаунта.
3. Установить AdsPower для Linux (файл .deb с сайта AdsPower), проверить путь к программе:
   `dpkg -L adspower-global | grep -i bin`. Если путь другой, поправить его в `deploy/systemd/adspower.service`.
4. Включить автозапуск:
   ```bash
   sudo cp deploy/systemd/* /etc/systemd/system/
   sudo mkdir -p /etc/systemd/journald.conf.d && sudo cp deploy/journald.conf.d/clips.conf /etc/systemd/journald.conf.d/
   sudo systemctl restart systemd-journald && sudo systemctl daemon-reload
   sudo systemctl enable --now adspower clips-bot
   sudo systemctl enable --now clips-trends.timer clips-plan.timer clips-publish.timer clips-stats.timer \
     clips-summary.timer clips-report.timer clips-notion.timer clips-maintenance.timer clips-captions.timer
   ```
5. Проверка: `systemctl list-timers 'clips-*'` показывает 9 таймеров со временем следующего запуска.
   Проверка AdsPower: `curl -s "http://127.0.0.1:50325/api/v1/browser/active?user_id=<id профиля>" -H "Authorization: Bearer $ADSPOWER_API_KEY"`
   должен вернуть `"code":0`.

Расписание (время московское, где указано):

| Что | Когда |
|-----|-------|
| Сбор трендов, отбор, скачивание | каждые 3 часа |
| Планирование и сборка роликов | каждый час |
| Публикация | каждые 10 минут |
| Статистика, "пора подавать", Notion | 06:20 |
| Ежедневная сводка | 09:40 |
| Недельный отчёт | понедельник 10:10 |
| Синхронизация Notion | каждый час |
| Обслуживание и бэкап | 04:30 |
| Сбор японских подписей | воскресенье 05:10 |

Окно публикаций (10:00-22:00) и лимиты задаются в `config.toml`, а не здесь.

## 1. Первые живые проверки (по порядку, до пилота)

1. `python -m clips build --source мем.mp4 --account <ник> --out data/tmp/t.mp4`: посмотреть ролик на телефоне
   (баннер, круг, мидролл).
2. `python -m clips health --account <ник>`: должно напечатать `ok`, скриншот ленты в `data/screenshots/`.
3. `python -m clips publish-file --account <ник> --file data/tmp/t.mp4 --banner-type voiced --caption "テスト" --dry-run`:
   скриншоты каждого шага до кнопки "Поделиться". Если какая-то кнопка не найдена, см. раздел 5.
4. То же без `--dry-run`: ролик появляется в профиле, в базе статус `published` и ссылка.
5. `python -m clips harvest-captions`, затем `python -m clips set-bio --account <ник>`.
6. `python -m clips trends --dry-run`, затем без `--dry-run`; `python -m clips plan --dry-run`.
7. Telegram: `python -m clips summary` приходит в чат; кнопки под сообщением "Пора подавать" работают.
8. Поле просмотров: на 3 наших роликах сравнить число просмотров в приложении Instagram
   с `videoPlayCount` и `videoViewCount` в последнем файле `data/raw/stats_*.json`.
   Записать сюда, какое совпало, и поставить его в `apify.views_field`:
   **Результат проверки: (не проверено)**
9. Notion: `python -m clips notion-setup --parent-page <id страницы>`, вписать выведенные ID в `.env`,
   затем `python -m clips notion-sync`.

## 2. Добавить новый аккаунт

1. В AdsPower создать профиль со своей мобильной или резидентской прокси.
2. Войти в Instagram в этом профиле вручную. Переключить аккаунт в профессиональный (автор):
   без этого не видна статистика по странам.
3. Добавить блок `[[accounts]]` в `config.toml`: ник, `adspower_id`, фон, `daily_limit` (начинать с 1),
   `caption_lang` (`japanese` или `none`).
4. Положить картинку фона по указанному пути.
5. `python -m clips sync-accounts`, `python -m clips health --account <ник>`, `python -m clips set-bio --account <ник>`.

## 3. Аккаунт на проверке (сообщение 🔴 в Telegram)

1. Открыть профиль в AdsPower на своём компьютере, пройти проверку Instagram (код, селфи, вход).
2. Закрыть профиль в AdsPower (пока он открыт, пайплайн его не трогает).
3. Нажать в Telegram кнопку "Проверку прошёл". Публикации на аккаунт возобновятся.

Пауза из-за ограничения действий снимается сама через 48 часов (`publish.action_block_pause_hours`).
Ручная смена статуса: `python -m clips account-status <ник> active|paused|check|banned`.

## 4. Подача в бота Playerok (делает человек)

1. Пришло "🟢 Пора подавать": открыть ссылку, подать ролик в бота Playerok.
2. Для роликов от 500 000: записать экран статистики с телефона и отправить команду
   `/geo <номер> <процент просмотров из России>` нашему боту.
3. Нажать "Подано". Когда Playerok ответит: "Одобрено" (бот спросит сумму) или "Отклонено" (спросит причину).
4. `/status` в боте показывает сводку в любой момент.

Опубликованные ролики и баннер в них не трогать 30 дней после оплаты (правило Playerok).

## 5. Instagram поменял интерфейс (публикация падает с `LookupError`)

1. Открыть последний скриншот и HTML в `data/screenshots/` (имя содержит ник и шаг).
2. Поправить селектор в `clips/ig_selectors.py` (и только там).
3. Проверить: `python -m clips publish-file ... --dry-run`.

## 6. Сменить баннеры (новая кампания Playerok)

1. Положить новые файлы в `assets/banners/`, поправить `[banners]` в `config.toml`.
2. Собрать пробный ролик (`build`) и проверить (`validate`), посмотреть на телефоне.

## 7. Логи

```bash
journalctl -u clips@publish -n 200        # последние публикации
journalctl -u 'clips@*' --since today     # все задачи за сегодня
journalctl -u clips-bot -f                # бот в реальном времени
```

Логи хранятся до 30 дней и не больше 2 ГБ.

## 8. Восстановить базу из бэкапа

```bash
sudo systemctl stop 'clips-*.timer' clips-bot
sudo -u clips cp /opt/clips/data/backups/clips-ГГГГММДД.db /opt/clips/data/clips.db
sudo systemctl start clips-bot && sudo systemctl start clips-trends.timer clips-plan.timer clips-publish.timer \
  clips-stats.timer clips-summary.timer clips-report.timer clips-notion.timer clips-maintenance.timer clips-captions.timer
```

## 9. Раз в месяц

```bash
cd /opt/clips && sudo -u clips .venv/bin/pip install -U yt-dlp   # Instagram ломает старые версии
sudo -u clips git pull && sudo -u clips .venv/bin/pip install -r requirements.txt
```
