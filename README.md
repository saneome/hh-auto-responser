# hh-auto-response

Авто-отклик на вакансии hh.ru через **браузерную автоматизацию (Playwright)**.

> **Важно.** Соискательский API hh.ru **закрыт с 15 декабря 2025**: эндпоинты `POST /negotiations`, `GET /resumes/mine` и т.п. возвращают 403 для соискательских токенов. Поэтому инструмент работает не через REST, а как живой пользователь в Chromium. Никаких client_id/client_secret/токенов вам больше не понадобится — только обычный логин на hh.ru.

## Что делает

- Запускает реальный Chromium с persistent-профилем (`./user-data/`)
- При первом запуске показывает окно браузера → вы логинитесь руками (включая капчу/SMS); сессия сохраняется в профиле и в дальнейшем нужна не будет
- Есть GUI на Qt: вводишь профиль кандидата, формат работы, опыт, ЗП, hard/soft skills и настройки модели, а приложение само строит поиск и отклик
- Ходит по поисковым URL `https://hh.ru/search/vacancy?...` на основе профиля и доп. фильтров (город, опыт, формат работы, желаемая ЗП)
- Открывает каждую вакансию, читает текст страницы, включая требования, soft skills и условия, детектит стек (Python/Java/Rust/React/Vue), отбрасывает PHP/Go/1C/Senior/тимлидов
- Если настроен NIM, отправляет текст вакансии в `qwen/qwen3.5-122b-a10b` и получает сопроводительное письмо от модели; иначе использует локальный fallback
- Жмёт «Откликнуться», вставляет сгенерированное сопроводительное письмо, отправляет
- Между откликами ждёт случайное время **1.5–9 минут** + 7% шанс «человечьей» паузы 15–45 минут
- Запоминает обработанные вакансии в `applied.json`, не дублирует
- Отбрасывает вакансии с обязательным тестовым заданием
- Скриншоты при сбоях падают в `./screenshots/`

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # <-- однократно, Chromium кешируется в ~/.cache/ms-playwright/
cp config.example.yaml config.yaml
cp .env.example .env
```

Если у тебя Linux без графики — Playwright всё равно может запустить headful Chromium через xvfb, но проще первый логин сделать на машине с экраном. После логина каталог `./user-data/` можно перенести на сервер и дальше гонять headless.

## Что нужно от тебя (минимум)

| Что | Где взять |
|-----|-----------|
| Аккаунт на hh.ru | `https://hh.ru/account/login` (обычный логин по email/телефону) |
| Активное резюме | создаётся в личном кабинете hh.ru |
| Браузер Chromium | ставится автоматически через `playwright install chromium` |
| NVIDIA NIM API key | положи в `.env` как `NIM_API_KEY=...` |

Ключ модели читается из `.env` или переменных окружения процесса. Файл `.env` лежит в корне проекта и уже игнорируется git.

## GUI

Запуск GUI:

```bash
python main.py --gui
```

В окне можно ввести ФИО, город, hard/soft skills, формат работы, опыт, желаемую ЗП и контакты. По этим данным приложение генерирует поисковые запросы, фильтрует вакансии и собирает сопроводительное письмо через NIM.

Модель по умолчанию уже выставлена на `qwen/qwen3.5-122b-a10b`, но в GUI можно указать свой `NIM_MODEL` и `NIM_BASE_URL`.

## Первый запуск (логин)

```bash
python main.py --login-only
```

Откроется окно Chromium с формой логина hh.ru. Логинись как обычно (email + пароль/код из SMS). После того как страница перейдёт в твой личный кабинет, скрипт это заметит и завершится с сообщением «Логин успешен». Сессия осталась в `./user-data/` — её можно копировать между запусками.

## Боевой запуск

Сначала dry-run (показывает, что **будет** отправлено, кнопку отклика не жмёт):

```bash
python main.py --dry-run -v
```

Потом боевой:

```bash
python main.py
```

Можно запустить в headless после первого логина — поставь в `config.yaml`:
```yaml
browser:
  headless: true
```

Прервать — `Ctrl+C`. Прогресс пишется в `applied.json` после каждого отклика.

## NIM и `.env`

Создай рядом с проектом файл `.env` или скопируй `.env.example`:

```env
NIM_API_KEY=your-nim-api-key
NIM_MODEL=qwen/qwen3.5-122b-a10b
NIM_BASE_URL=https://integrate.api.nvidia.com/v1
NIM_TIMEOUT_SECONDS=60
NIM_TEMPERATURE=0.5
NIM_MAX_TOKENS=700
```

Если `.env` не задан или ключа нет, скрипт не падает и использует локальный шаблон письма.

## Профиль и поиск

В `config.yaml` теперь живут не только настройки браузера, но и профиль кандидата:

| Поле | Что регулирует |
|------|----------------|
| `profile.full_name` | ФИО кандидата |
| `profile.city` | город для поиска и подсказок модели |
| `profile.hard_skills` | хард-скиллы, из которых строятся поисковые запросы |
| `profile.soft_skills` | софт-скиллы, которые идут в промт и матчинг |
| `profile.work_format` | `remote`, `hybrid` или `office` |
| `profile.experience` | `noExperience`, `between1And3`, `between3And6`, `moreThan6` |
| `profile.desired_salary` | желаемая зарплата |
| `search.queries` | можно оставить пустым, они сгенерируются автоматически |

## Конфиг (`config.yaml`)

| Поле | Что регулирует |
|------|----------------|
| `browser.user_data_dir` | папка с профилем Chromium (куки/localStorage) |
| `browser.headless` | `false` — видимое окно (нужно при первом логине), `true` — фон |
| `browser.slow_mo_ms` | задержка между Playwright-шагами для «человечности» |
| `search.area` | регион: `113` = Россия, `1` = Москва, `2` = СПб; может подставиться из `profile.city` |
| `search.experience` | список опыта hh, обычно берётся из `profile.experience` |
| `search.queries` | список поисковых строк; если пустой, генерируется из профиля |
| `search.max_pages` | сколько страниц результатов листать на каждый запрос |
| `cover_letter.pretend_experience` | `true` — добавить фразу про 1–2 года опыта (для ATS); `false` — честно |
| `NIM_API_KEY` | ключ NVIDIA NIM в `.env` или в переменных окружения |
| `rate_limit.min_seconds`/`max_seconds` | диапазон паузы между откликами (по дефолту 90–540 с) |
| `rate_limit.long_break_chance` | вероятность длинной паузы 15–45 мин |
| `max_per_run` | предел откликов за запуск (`0` — без лимита) |
| `dry_run` | `true` — ничего не отправлять, только показать |

## Про опыт работы

Флаг `cover_letter.pretend_experience`:
- `false` (дефолт) — в письме только pet-проекты + хакатоны, без слов про коммерческий опыт. Минус: жёсткие ATS могут отсеять
- `true` — добавится фраза про «порядка 1–2 лет на бэкенд-задачах». Не пиши конкретные крупные компании в самом резюме — это вскроется на собесе

Резюме на hh.ru правишь сам — этот инструмент его не трогает.

## Поведение и ограничения

- Антибот hh.ru может прогнать через капчу (особенно после серии действий). Скрипт капчу не решает — он её увидит и упрётся в таймаут. Решение: запусти headful, реши капчу руками, после этого hh обычно не трогает несколько часов
- Логин может потребовать SMS-код — это разовая операция, делается тоже руками в окне браузера
- Если NIM недоступен или ключ не задан, письмо соберётся локальным шаблоном без обращения к модели
- GUI и CLI используют один и тот же `config.yaml` и один и тот же `.env`
- Селекторы привязаны к `data-qa` атрибутам hh.ru. Они исторически стабильны, но если hh поменяет вёрстку — увидишь в `./screenshots/` что не так и нужно будет поправить селекторы в `hh_auto/browser.py`
- **Не** запускай несколько копий скрипта параллельно с одним и тем же `user_data_dir` — Chromium ругнётся

## Сборка desktop-приложения

Проект можно собрать в standalone-приложение через PyInstaller. Результат — папка с исполняемым файлом, которая не требует Python.

### Подготовка

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### Сборка

**Linux / macOS:**
```bash
./build.sh
```

**Windows:**
```
build.bat
```

**macOS .app bundle (дополнительно):**
```bash
./build-mac.sh
```

Результат появится в `dist/hh-auto-response/`.

### Что внутри

| Файл | Описание |
|------|----------|
| `hh-auto-response` / `.exe` | Исполняемый файл |
| `_internal/` | Библиотеки, PySide6, Playwright driver |
| `_internal/config.example.yaml` | Шаблон конфига |
| `_internal/.env.example` | Шаблон .env |

### После сборки

1. Скопируй `dist/hh-auto-response/` куда нужно
2. Создай `config.yaml` рядом с исполняемым файлом (или укажи `--config /path/to/config.yaml`)
3. Создай `.env` с `NIM_API_KEY`
4. Chromium для Playwright ставится отдельно: `playwright install chromium` — он ляжет в `~/.cache/ms-playwright/` и будет найден автоматически
5. Запуск: `./hh-auto-response --gui`

### Кросс-компиляция

PyInstaller собирает под текущую платформу. Для распространения на все три ОС нужно собрать на каждой:

| Цель | Где собирать |
|------|-------------|
| Linux x86_64 | Любой Linux |
| Windows x86_64 | Windows |
| macOS (Intel + ARM) | macOS |

## Структура

```
.
├── .env.example
├── main.py                    # точка входа
├── config.example.yaml
├── requirements.txt
├── build.sh                   # сборка Linux
├── build.bat                  # сборка Windows
├── build-mac.sh               # сборка macOS .app
├── hh-auto-response.spec      # PyInstaller spec
├── icon.svg / .png / .ico     # иконка приложения
└── hh_auto/
    ├── app_config.py          # общие helpers для YAML/.env
    ├── browser.py             # Playwright: open/login/search/apply
    ├── cover_letter.py        # генератор сопроводительного + prompt для NIM
    ├── filters.py             # детект стека, стоп-слов
    ├── gui.py                 # Qt GUI
    ├── nim_client.py          # OpenAI-compatible клиент NVIDIA NIM
    ├── profile.py             # профиль кандидата и матчинг вакансий
    ├── runner.py              # главный цикл
    ├── storage.py             # applied.json
    └── _runtime_hook.py       # PyInstaller runtime patch для Playwright
```

## Дисклеймер

Используй ответственно. Браузерная автоматизация формально не запрещена hh.ru ToS, но массовый спам и попытки обхода защиты — да. Дефолтные паузы (1.5–9 мин) специально подобраны, чтобы не выглядеть как робот. Не делай интервалы короче.
