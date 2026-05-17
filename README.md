# hh-auto-response

Автоматизация поиска работы и переписки на hh.ru через браузерную автоматизацию (Playwright).

> **Важно.** Соискательский API hh.ru закрыт для прямых откликов (возвращает 403).
> Этот инструмент работает как реальный пользователь в Chromium — логин, поиск, отклики и переписка.

## Возможности

- **Поиск вакансий** — ходит по `hh.ru/search/vacancy`, читает карточки, фильтрует
- **Авто-отклики** — генерирует сопроводительное письмо через LLM и отправляет
- **Авто-ответы работодателям** — сканирует переписку, генерирует ответы через LLM
- **GUI-редактор конфига** — меняй все настройки без лазанья в yaml
- **Запуск через `./run-both.sh`** — search + responder параллельно, две Chrome-сессии
- **Прогресс поиска** — запоминает проверенные вакансии в `search_progress.json`, не перепроверяет
- **Предотвращение дублей** — `applied.json` + `search_progress.json`
- **Фильтрация по запросу** — откликается только если название вакансии совпадает со словом из поискового запроса (Python → Python, не Ruby)

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # однократно
cp config.example.yaml config.yaml
cp .env.example .env
```

## Настройка

Отредактируй `config.yaml` или запусти GUI (`./auto-responser.sh`) и заполни поля.

### Обязательное

| Что | Где |
|-----|-----|
| Профиль (ФИО, навыки, город) | `config.yaml → profile` или GUI |
| LLM ключ | `.env → LLM_API_KEY` или `OPENAI_API_KEY` |

Поддерживаются любые OpenAI-compatible провайдеры (NVIDIA NIM, polza.ai, OpenAI, Anthropic и т.д.).

### LLM (.env)

```env
LLM_API_KEY=your-key
LLM_BASE_URL=https://polza.ai/api/v1
LLM_MODEL=openai/gpt-4o-mini
NIM_TIMEOUT_SECONDS=60
```

Если ключа нет — письма собираются локальным шаблоном.

## Запуск

### GUI (рекомендуется)

```bash
./auto-responser.sh
```

Скрипт активирует venv, подчищает stale Chrome-locks и запускает GUI с логами в `logs/gui.log`.
На первый запуск может потребоваться выставить права:

```bash
chmod +x auto-responser.sh run-both.sh
```

Если venv уже активирован — можно напрямую:

```bash
python main.py --gui
```

Окно с формой настроек + кнопки **Старт** / **Стоп**.
При старте:
1. Сохраняет `config.yaml`
2. Проверяет доступность LLM (ping)
3. Запускает `./run-both.sh`

Логи агентов в реальном времени отображаются в окне.

### CLI — отклики на вакансии

```bash
python main.py --dry-run -v     # посмотреть что будет, не отправлять
python main.py -v               # боевой режим
python main.py --login-only     # только логин в hh.ru (первый раз)
```

### CLI — проверка переписки

```bash
python main.py --check-negotiations --auto-reply -v   # один раз
python main.py --check-negotiations --loop --auto-reply -v   # цикл каждые 5 мин
```

### Два агента параллельно

```bash
./run-both.sh
```

- **Search agent** — ищет вакансии, откликается, спит час, повторяет
- **Responder agent** — проверяет переписку, отвечает работодателям, спит 5 мин, повторяет
- У каждого свой профиль Chrome (`user-data-search` / `user-data-responder`)
- При выходе (`Ctrl+C`, `kill`, закрытие GUI) — cleanup убивает всю группу процессов

## Фильтрация вакансий

Бот не откликается на все подряд. Цепочка фильтров:

1. **Уже откликались** (`applied.json`) — skip
2. **Уже проверяли и не подошли** (`search_progress.json`) — skip
3. **Не вакансия разработчика** (`_is_dev_title`) — skip
4. **Название не совпадает с поисковым запросом** — если искал `Python разработчик`, а попалась `Ruby разработчик` (hh.ru иногда подсовывает) — skip
5. **Нет хард-скиллов / стек не подходит** (`match_vacancy`, `_is_relevant`) — skip
6. **ML / Data Science / Аналитика** — явно отбрасываются

Причина skip записывается в `search_progress.json` — при следующем запуске вакансия пропускается мгновенно.

## Повторный запуск (resume)

При перезапуске:
- Вакансии из `applied.json` и `search_progress.json` пропускаются
- Необработанные из-за `max_per_run` **не** записываются в progress — при следующем запуске продолжим с них
- hh.ru сортирует по свежести, новые вакансии всегда первые

## Конфиг (`config.yaml`)

| Поле | Что регулирует |
|------|----------------|
| `profile.full_name` | ФИО кандидата |
| `profile.city` | Город (родительный падеж для письма генерирует LLM) |
| `profile.hard_skills` | Хард-скиллы для матчинга и сопроводительного |
| `profile.soft_skills` | Софт-скиллы для матчинга |
| `profile.work_format` | `remote` / `hybrid` / `office` |
| `profile.experience` | `noExperience`, `between1And3`, `between3And6`, `moreThan6` |
| `profile.desired_salary` | Желаемая ЗП |
| `search.queries` | Список поисковых строк (если пусто — генерируются из hard_skills) |
| `search.area` | Регион (`113` = Россия) |
| `search.experience` | Опыт для поиска (можно указать несколько) |
| `search.max_pages` | Сколько страниц листать на запрос |
| `rate_limit.min_seconds` / `max_seconds` | Пауза между откликами (сек) |
| `rate_limit.long_break_chance` | Шанс «длинной» паузы |
| `cover_letter.pretend_experience` | `true` — добавить фразу про опыт 1–2 года в письмо |
| `responder.chance` | Вероятность проверки переписки после отклика |
| `responder.auto_reply` | Автоотправка ответов работодателям |
| `max_per_run` | Лимит откликов за запуск (`0` = без лимита) |
| `dry_run` | `true` — не нажимать кнопки, только показать |

## LLM (нейронка)

Используется для:
- Генерации сопроводительного письма (по тексту вакансии + профилю кандидата)
- Ответов работодателям в переписке (с учётом контекста диалога)

Провайдер настраивается через `.env`:
- `LLM_API_KEY` — ключ
- `LLM_BASE_URL` — endpoint (OpenAI-compatible)
- `LLM_MODEL` — модель

Если LLM недоступен — fallback на локальный шаблон (менее персонализированный).

## Структура

```
.
├── main.py                    # точка входа (CLI + GUI)
├── config.yaml                # активный конфиг
├── config.example.yaml        # шаблон
├── .env                       # ключи (не в git)
├── run-both.sh                # запуск двух агентов параллельно
├── auto-responser.sh          # convenience: venv + GUI
├── applied.json               # ID вакансий, на которые откликнулись
├── search_progress.json       # ID вакансий, которые проверили и откинули
├── negotiations.json          # переписки с работодателями
├── logs/                      # stdout/stderr агентов
├── screenshots/               # скриншоты при ошибках
└── hh_auto/
    ├── gui.py                 # PySide6 GUI
    ├── runner.py              # главный цикл поиска + откликов
    ├── responder.py           # автоответы в переписке
    ├── browser.py             # Playwright: браузер, логин, поиск, отклик
    ├── cover_letter.py        # генератор сопроводительного (LLM + fallback)
    ├── profile.py             # профиль кандидата, матчинг вакансий
    ├── filters.py             # детект стека, стоп-слова, отрицательные паттерны
    ├── negotiations.py        # сканирование переписки
    ├── storage.py             # applied.json, search_progress.json
    ├── app_config.py          # YAML helpers
    └── nim_client.py          # OpenAI-compatible LLM клиент
```

## Ограничения и предостережения

- **Капча** — скрипт не решает капчу. Если hh.ru покажет — нужно решить руками в открытом окне браузера. После раза-двух обычно перестают трогать на часы
- **SMS-код** — первый логин может потребовать код, вводится руками
- **Селекторы** — привязаны к `data-qa` атрибутам hh.ru. Если hh поменяет вёрстку — нужно обновить селекторы в `browser.py`
- **Не запускай несколько копий с одним `user_data_dir`** — Chrome упадёт с `SingletonLock`
- **Паузы настроены на «человечность»** — не уменьшай `rate_limit` до роботоподобных значений
- **Массовый спам запрещён ToS hh.ru** — инструмент для тех, кто ищет работу, а не для флуда

## Сборка

```bash
./build.sh      # Linux
./build.bat     # Windows
./build-mac.sh  # macOS .app
```

Результат: `dist/hh-auto-response/` — standalone, Python не нужен.

## Дисклеймер

Используй ответственно. Дефолтные паузы (минимум 15–60 сек) специально подобраны, чтобы не выглядеть как робот. Не сокращай.
