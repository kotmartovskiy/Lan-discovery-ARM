# Lan-discovery-ARM

Веб-панель управления домашней сетью для одноплатных компьютеров (Orange Pi, X96 Max / Amlogic, generic Debian/ARM): обнаружение устройств, мониторинг, IPTV/радио/плеер, сетевые инструменты, файлы, заметки, бэкапы и клонирование eMMC → SD.

- **Панель:** `http://<ip>:8080` (Flask, Python 3, venv)
- **Сервис:** `systemctl status lan-discovery`, код — `/opt/lan-discovery/app.py`


## Лицензия

**Проприетарная** — см. [LICENSE](LICENSE). Личное некоммерческое
использование свободно; коммерческое использование, распространение
и публичные копии кода — только по письменному разрешению
правообладателя (kotmartovskiy).

## Документация

Полные инструкции — в [`docs/`](docs/) (они же — [wiki](https://github.com/kotmartovskiy/Lan-discovery-ARM/wiki)):

| Страница | О чём |
|---|---|
| [Home](docs/Home.md) | Обзор возможностей |
| [Установка](docs/Установка.md) | `install.sh`, зависимости, systemd, таймеры, деплой |
| [Обновление](docs/Обновление.md) | `update.sh`: бэкап → применение → авто-откат |
| [Восстановление](docs/Восстановление.md) | `recovery.sh`: restore кода/конфига/БД из бэкапов |
| [Архитектура](docs/Архитектура.md) | Структура кода, core/, данные, lifecycle-скрипты, тесты |
| [Модули](docs/Модули.md) | Ответственность модулей и роутов |
| [API](docs/API.md) | Каталог всех 165 роутов: метод/путь/доступ |
| [Конфигурация](docs/Конфигурация.md) | settings.json, переопределения, события/retention |
| [Безопасность](docs/Безопасность.md) | Роли, CSRF, секреты, периметр, ограничения |
| [IPTV](docs/IPTV.md) | Плейлисты, таймер 04:15, диагностика |
| [SD-клонирование](docs/SD-клонирование.md) | Копирование eMMC → SD, API, осторожности |
| [Погода](docs/Погода.md) | Open-Meteo, таймеры, weather-monitor |
| [Полезные команды](docs/Полезные-команды.md) | SSH, логи, бэкапы, типовые неполадки |

Публичная (очищенная от рабочих адресов и паролей) версия документации: **https://github.com/kotmartovskiy/Lan-discovery-docs**

## Стек

Python 3 · Flask 3 · Flask-SocketIO · Flask-WTF · bcrypt · SQLite · Jinja2 · systemd · pytest + CI

## Быстрый старт

```bash
# чистая установка (идемпотентна, есть --dry-run):
sudo ./install.sh

# обновление (бэкап + авто-откат при сбое):
sudo ./update.sh

# восстановление из бэкапов:
sudo ./recovery.sh --dry-run
```

Ручная альтернатива и регламент правок на сервере — **бэкап → правка → `py_compile` → `systemctl restart lan-discovery`** (см. [Установка](docs/Установка.md)).

Тесты: `pip install -r requirements-dev.txt && pytest` (unit — без сети, live-маркер `live` требует панель); CI гоняет unit + `py_compile` на каждый push.

## Структура репозитория

```
app.py            точка входа: Flask/CSRF, регистрация модулей, фон, main
core/             hardware (платформа/температура/диски), discovery (скан),
                  events (журнал+retention), module_loader, module_catalog
modules/          роуты и логика (auth, devices, system, network, media,
                  weather, monitoring, inventory, core) + модули-компоненты
templates/        Jinja2-шаблоны (base.html — каркас панели)
games/, static/   игры и статика
tests/            pytest: unit (без сети) + live (маркер live)
install.sh        чистая установка     update.sh   обновление с откатом
recovery.sh       restore из бэкапов   deploy/     systemd-юниты и шаблоны
docs/             документация         tools/      sanitize/demo-скрипты
.github/          CI (pytest unit + py_compile)
deploy.py         деплой на сервер: бэкап → SFTP → проверка → рестарт
```
