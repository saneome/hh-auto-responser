"""Минималистичный GUI — редактор config.yaml + запуск run-both.sh.

Всё остальное (embedded browser, логи, отдельные режимы) удалено.
Остаётся только: заполнить настройки → Сохранить → Старт.
"""
from __future__ import annotations

import logging
import os
import platform
import signal
import subprocess
import time
from subprocess import TimeoutExpired
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .app_config import load_config, save_config
from .profile import EXPERIENCE_LABELS, FORMAT_LABELS

log = logging.getLogger("hh_auto.gui")

# ── log reader thread ──────────────────────────────────────────────


class LogReaderThread(QThread):
    line_ready = Signal(str)

    def __init__(self, stream, parent=None):
        super().__init__(parent)
        self._stream = stream
        self._running = True

    def run(self):
        try:
            while self._running:
                raw = self._stream.readline()
                if not raw:
                    break
                self.line_ready.emit(raw.decode("utf-8", errors="replace").rstrip("\n"))
        except Exception:
            pass

    def stop(self):
        self._running = False


# ── helpers ──────────────────────────────────────────────────────────


def _str(val: object) -> str:
    return "" if val is None else str(val)


def _bool(val: object) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    return str(val).lower() in ("true", "1", "yes", "on")


def _int(val: object, default: int = 0) -> int:
    if isinstance(val, (int, float)):
        return int(val)
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return default


def _float(val: object, default: float = 0.0) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return default


def _list_str(val: object) -> list[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    return [s.strip() for s in str(val).splitlines() if s.strip()]


def _join_list(items: list[str]) -> str:
    return "\n".join(items)


# ── GUI ──────────────────────────────────────────────────────────────


class SettingsWindow(QMainWindow):
    RUNBOTH_PATH = "run-both.bat" if platform.system() == "Windows" else "./run-both.sh"

    def __init__(self, config_path: str = "config.yaml"):
        super().__init__()
        self._config_path = config_path
        self.setWindowTitle("hh-auto-response — настройки")
        self.setMinimumSize(560, 700)

        self._proc: subprocess.Popen | None = None
        self._stdout_thread: LogReaderThread | None = None
        self._stderr_thread: LogReaderThread | None = None

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(2000)

        self._build_ui()
        self._load_config()

    # ── layout ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # --- toolbar ---
        toolbar = QHBoxLayout()
        self.btn_save = QPushButton("Сохранить")
        self.btn_save.setStyleSheet("font-weight: bold;")
        self.btn_save.clicked.connect(self._on_save)

        self.btn_start = QPushButton("Старт")
        self.btn_start.setStyleSheet("background: #2e7d32; color: white; font-weight: bold; padding: 6px 16px;")
        self.btn_start.clicked.connect(self._on_start)

        self.btn_stop = QPushButton("Стоп")
        self.btn_stop.setStyleSheet("background: #c62828; color: white; font-weight: bold; padding: 6px 16px;")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)

        self.lbl_status = QLabel("Остановлено")
        self.lbl_status.setStyleSheet("color: #888; padding-left: 8px;")

        toolbar.addWidget(self.btn_save)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_start)
        toolbar.addWidget(self.btn_stop)
        toolbar.addWidget(self.lbl_status)
        root.addLayout(toolbar)

        # --- scrollable form ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        form_widget = QWidget()
        form = QVBoxLayout(form_widget)
        form.setContentsMargins(4, 4, 4, 4)
        form.setSpacing(14)

        # Profile
        self._profile = self._add_section(form, "Профиль")
        self.inp_full_name = self._line(self._profile, "ФИО")
        self.inp_city = self._line(self._profile, "Город")
        self.inp_desired_role = self._line(self._profile, "Желаемая роль")
        self.inp_desired_salary = self._spin(self._profile, "Желаемая ЗП (руб)", 0, 10_000_000)
        self.inp_work_format = self._combo(self._profile, "Формат работы", list(FORMAT_LABELS.keys()))
        self.inp_experience = self._combo(self._profile, "Опыт", list(EXPERIENCE_LABELS.keys()))
        self.inp_hard_skills = self._text(self._profile, "Хард-скиллы (по строке)")
        self.inp_soft_skills = self._text(self._profile, "Софт-скиллы (по строке)")
        self.inp_telegram = self._line(self._profile, "Telegram")
        self.inp_github = self._line(self._profile, "GitHub")
        self.inp_extra_contacts = self._text(self._profile, "Доп. контакты (label: value)")
        self.inp_require_work_formats = self._text(self._profile, "Требуемые форматы работы")
        self.inp_avoid_work_formats = self._text(self._profile, "Форматы работы — избегать")
        self.inp_require_contract_types = self._text(self._profile, "Требуемые типы договора")
        self.inp_avoid_contract_types = self._text(self._profile, "Типы договора — избегать")

        # Search
        self._search = self._add_section(form, "Поиск (hh.ru)")
        self.inp_search_queries = self._text(self._search, "Поисковые запросы (по строке)")
        self.inp_search_area = self._spin(self._search, "Регион (area)", 0, 999)
        self.inp_search_experience = self._text(self._search, "Опыт фильтр (по строке: noExperience...)")
        self.inp_search_schedule = self._line(self._search, "График (remote/office/hybrid)")
        self.inp_max_pages = self._spin(self._search, "Макс. страниц", 1, 100)

        # Rate limit
        self._rate = self._add_section(form, "Задержки")
        self.inp_rate_min = self._spin(self._rate, "Мин. секунд", 0, 3600)
        self.inp_rate_max = self._spin(self._rate, "Макс. секунд", 0, 3600)
        self.inp_rate_long_chance = self._dspin(self._rate, "Шанс длинной паузы", 0.0, 1.0, 0.01)
        self.inp_rate_long_min = self._spin(self._rate, "Длинная пауза мин (сек)", 0, 3600)
        self.inp_rate_long_max = self._spin(self._rate, "Длинная пауза макс (сек)", 0, 3600)

        # Cover letter
        self._cover = self._add_section(form, "Сопроводительное письмо")
        self.inp_pretend_exp = self._check(self._cover, "Упомянуть 1–2 года опыта в письме")

        # Responder
        self._responder = self._add_section(form, "Автоответчик")
        self.inp_responder_chance = self._dspin(self._responder, "Шанс проверки откликов", 0.0, 1.0, 0.01)
        self.inp_responder_auto_reply = self._check(self._responder, "Автоответ на сообщения")

        # Browser
        self._browser = self._add_section(form, "Браузер")
        self.inp_headless = self._check(self._browser, "Headless (без окна)")
        self.inp_timeout_ms = self._spin(self._browser, "Таймаут (мс)", 1000, 300000)
        self.inp_slow_mo = self._spin(self._browser, "Slow-mo (мс)", 0, 5000)
        self.inp_locale = self._line(self._browser, "Локаль")
        self.inp_timezone = self._line(self._browser, "Часовой пояс")

        # Notifications
        self._notif = self._add_section(form, "Уведомления Telegram")
        self.inp_tg_token = self._line(self._notif, "Bot token")
        self.inp_tg_chat = self._line(self._notif, "Chat ID")
        self.inp_tg_proxy = self._line(self._notif, "Прокси (опционально)")

        # Other
        self._other = self._add_section(form, "Прочее")
        self.inp_max_per_run = self._spin(self._other, "Макс. откликов за запуск", 0, 500)
        self.inp_dry_run = self._check(self._other, "Dry-run (не отправлять)")
        self.inp_login_timeout = self._spin(self._other, "Таймаут логина (сек)", 0, 3600)
        self.inp_screenshots_dir = self._line(self._other, "Папка скриншотов")
        self.inp_applied_log = self._line(self._other, "Файл applied-log")

        form.addStretch()
        scroll.setWidget(form_widget)
        root.addWidget(scroll)

        # --- log viewer ---
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("Логи агентов появятся здесь...")
        self._log.setMaximumBlockCount(5000)
        self._log.setFixedHeight(200)
        root.addWidget(QLabel("Логи:"))
        root.addWidget(self._log)

    # ── widget helpers ────────────────────────────────────────────

    def _add_section(self, parent: QVBoxLayout, title: str) -> QVBoxLayout:
        group = QGroupBox(title)
        group.setStyleSheet("QGroupBox { font-weight: bold; margin-top: 8px; } QGroupBox::title { subcontrol-origin: margin; left: 6px; top: 2px; }")
        lay = QVBoxLayout(group)
        lay.setSpacing(6)
        lay.setContentsMargins(8, 10, 8, 8)
        parent.addWidget(group)
        return lay

    def _line(self, parent: QVBoxLayout, label: str) -> QLineEdit:
        h = QHBoxLayout()
        h.addWidget(QLabel(label), stretch=0)
        edit = QLineEdit()
        h.addWidget(edit, stretch=1)
        parent.addLayout(h)
        return edit

    def _spin(self, parent: QVBoxLayout, label: str, min_val: int, max_val: int) -> QSpinBox:
        h = QHBoxLayout()
        h.addWidget(QLabel(label), stretch=0)
        spin = QSpinBox()
        spin.setRange(min_val, max_val)
        h.addWidget(spin, stretch=1)
        parent.addLayout(h)
        return spin

    def _dspin(self, parent: QVBoxLayout, label: str, min_val: float, max_val: float, step: float) -> QDoubleSpinBox:
        h = QHBoxLayout()
        h.addWidget(QLabel(label), stretch=0)
        spin = QDoubleSpinBox()
        spin.setRange(min_val, max_val)
        spin.setSingleStep(step)
        spin.setDecimals(2)
        h.addWidget(spin, stretch=1)
        parent.addLayout(h)
        return spin

    def _combo(self, parent: QVBoxLayout, label: str, items: list[str]) -> QComboBox:
        h = QHBoxLayout()
        h.addWidget(QLabel(label), stretch=0)
        combo = QComboBox()
        combo.addItems(items)
        h.addWidget(combo, stretch=1)
        parent.addLayout(h)
        return combo

    def _check(self, parent: QVBoxLayout, label: str) -> QCheckBox:
        cb = QCheckBox(label)
        parent.addWidget(cb)
        return cb

    def _text(self, parent: QVBoxLayout, label: str) -> QPlainTextEdit:
        parent.addWidget(QLabel(label))
        edit = QPlainTextEdit()
        edit.setMaximumBlockCount(100)
        edit.setPlaceholderText("один элемент на строку")
        edit.setFixedHeight(80)
        parent.addWidget(edit)
        return edit

    # ── config ↔ UI ───────────────────────────────────────────────

    def _load_config(self) -> None:
        try:
            cfg = load_config(self._config_path)
        except FileNotFoundError:
            cfg = {}

        profile = (cfg.get("profile") or {}) if cfg.get("profile") is not None else {}
        search = (cfg.get("search") or {}) if cfg.get("search") is not None else {}
        rate = (cfg.get("rate_limit") or {}) if cfg.get("rate_limit") is not None else {}
        cover = (cfg.get("cover_letter") or {}) if cfg.get("cover_letter") is not None else {}
        responder = (cfg.get("responder") or {}) if cfg.get("responder") is not None else {}
        browser = (cfg.get("browser") or {}) if cfg.get("browser") is not None else {}
        notif = ((cfg.get("notifications") or {}).get("telegram") or {}) if cfg.get("notifications") is not None else {}

        # profile
        self.inp_full_name.setText(_str(profile.get("full_name")))
        self.inp_city.setText(_str(profile.get("city")))
        self.inp_desired_role.setText(_str(profile.get("desired_role")))
        self.inp_desired_salary.setValue(_int(profile.get("desired_salary"), 0))
        self.inp_work_format.setCurrentText(_str(profile.get("work_format", "remote")))
        self.inp_experience.setCurrentText(_str(profile.get("experience", "between1And3")))
        self.inp_hard_skills.setPlainText(_join_list(_list_str(profile.get("hard_skills"))))
        self.inp_soft_skills.setPlainText(_join_list(_list_str(profile.get("soft_skills"))))
        self.inp_telegram.setText(_str(profile.get("telegram")))
        self.inp_github.setText(_str(profile.get("github")))
        self.inp_extra_contacts.setPlainText(_join_list(
            [f"{c.get('label','')}: {c.get('value','')}" for c in (profile.get("extra_contacts") or [])]
        ))
        self.inp_require_work_formats.setPlainText(_join_list(_list_str(profile.get("require_work_formats"))))
        self.inp_avoid_work_formats.setPlainText(_join_list(_list_str(profile.get("avoid_work_formats"))))
        self.inp_require_contract_types.setPlainText(_join_list(_list_str(profile.get("require_contract_types"))))
        self.inp_avoid_contract_types.setPlainText(_join_list(_list_str(profile.get("avoid_contract_types"))))

        # search
        self.inp_search_queries.setPlainText(_join_list(_list_str(search.get("queries"))))
        self.inp_search_area.setValue(_int(search.get("area"), 113))
        self.inp_search_experience.setPlainText(_join_list(_list_str(search.get("experience"))))
        self.inp_search_schedule.setText(_str(search.get("schedule")))
        self.inp_max_pages.setValue(_int(search.get("max_pages"), 12))

        # rate_limit
        self.inp_rate_min.setValue(_int(rate.get("min_seconds"), 15))
        self.inp_rate_max.setValue(_int(rate.get("max_seconds"), 60))
        self.inp_rate_long_chance.setValue(_float(rate.get("long_break_chance"), 0.0))
        self.inp_rate_long_min.setValue(_int(rate.get("long_break_min_seconds"), 0))
        self.inp_rate_long_max.setValue(_int(rate.get("long_break_max_seconds"), 0))

        # cover_letter
        self.inp_pretend_exp.setChecked(_bool(cover.get("pretend_experience")))

        # responder
        self.inp_responder_chance.setValue(_float(responder.get("chance"), 0.0))
        self.inp_responder_auto_reply.setChecked(_bool(responder.get("auto_reply")))

        # browser
        self.inp_headless.setChecked(_bool(browser.get("headless", False)))
        self.inp_timeout_ms.setValue(_int(browser.get("default_timeout_ms"), 20000))
        self.inp_slow_mo.setValue(_int(browser.get("slow_mo_ms"), 50))
        self.inp_locale.setText(_str(browser.get("locale", "ru-RU")))
        self.inp_timezone.setText(_str(browser.get("timezone", "Europe/Moscow")))

        # notifications
        self.inp_tg_token.setText(_str(notif.get("token")))
        self.inp_tg_chat.setText(_str(notif.get("chat_id")))
        self.inp_tg_proxy.setText(_str(notif.get("proxy")))

        # other top-level
        self.inp_max_per_run.setValue(_int(cfg.get("max_per_run"), 30))
        self.inp_dry_run.setChecked(_bool(cfg.get("dry_run")))
        self.inp_login_timeout.setValue(_int(cfg.get("login_timeout_seconds"), 600))
        self.inp_screenshots_dir.setText(_str(cfg.get("screenshots_dir", "screenshots")))
        self.inp_applied_log.setText(_str(cfg.get("applied_log", "applied.json")))

    def _collect_config(self) -> dict:
        def parse_contact_lines(text: str) -> list[dict[str, str]]:
            out: list[dict[str, str]] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if ":" in line:
                    label, value = line.split(":", 1)
                    out.append({"label": label.strip(), "value": value.strip()})
                else:
                    out.append({"label": "Контакт", "value": line})
            return out

        cfg: dict = {}

        cfg["profile"] = {
            "full_name": self.inp_full_name.text().strip() or None,
            "city": self.inp_city.text().strip() or None,
            "desired_role": self.inp_desired_role.text().strip() or None,
            "desired_salary": self.inp_desired_salary.value() or None,
            "work_format": self.inp_work_format.currentText(),
            "experience": self.inp_experience.currentText(),
            "hard_skills": [s.strip() for s in self.inp_hard_skills.toPlainText().splitlines() if s.strip()] or None,
            "soft_skills": [s.strip() for s in self.inp_soft_skills.toPlainText().splitlines() if s.strip()] or None,
            "telegram": self.inp_telegram.text().strip() or None,
            "github": self.inp_github.text().strip() or None,
            "extra_contacts": parse_contact_lines(self.inp_extra_contacts.toPlainText()) or None,
            "require_work_formats": [s.strip() for s in self.inp_require_work_formats.toPlainText().splitlines() if s.strip()] or None,
            "avoid_work_formats": [s.strip() for s in self.inp_avoid_work_formats.toPlainText().splitlines() if s.strip()] or None,
            "require_contract_types": [s.strip() for s in self.inp_require_contract_types.toPlainText().splitlines() if s.strip()] or None,
            "avoid_contract_types": [s.strip() for s in self.inp_avoid_contract_types.toPlainText().splitlines() if s.strip()] or None,
        }

        cfg["search"] = {
            "queries": [s.strip() for s in self.inp_search_queries.toPlainText().splitlines() if s.strip()] or None,
            "area": self.inp_search_area.value() or None,
            "experience": [s.strip() for s in self.inp_search_experience.toPlainText().splitlines() if s.strip()] or None,
            "schedule": self.inp_search_schedule.text().strip() or None,
            "max_pages": self.inp_max_pages.value() or None,
        }

        cfg["rate_limit"] = {
            "min_seconds": self.inp_rate_min.value(),
            "max_seconds": self.inp_rate_max.value(),
            "long_break_chance": round(self.inp_rate_long_chance.value(), 2),
            "long_break_min_seconds": self.inp_rate_long_min.value(),
            "long_break_max_seconds": self.inp_rate_long_max.value(),
        }

        cfg["cover_letter"] = {
            "pretend_experience": self.inp_pretend_exp.isChecked(),
            "telegram": self.inp_telegram.text().strip() or None,
            "github": self.inp_github.text().strip() or None,
        }

        cfg["responder"] = {
            "chance": round(self.inp_responder_chance.value(), 2),
            "auto_reply": self.inp_responder_auto_reply.isChecked(),
        }

        cfg["browser"] = {
            "headless": self.inp_headless.isChecked(),
            "default_timeout_ms": self.inp_timeout_ms.value(),
            "slow_mo_ms": self.inp_slow_mo.value(),
            "locale": self.inp_locale.text().strip() or None,
            "timezone": self.inp_timezone.text().strip() or None,
        }

        cfg["notifications"] = {
            "telegram": {
                "token": self.inp_tg_token.text().strip() or None,
                "chat_id": self.inp_tg_chat.text().strip() or None,
                "proxy": self.inp_tg_proxy.text().strip() or None,
            }
        }

        cfg["max_per_run"] = self.inp_max_per_run.value()
        cfg["dry_run"] = bool(self.inp_dry_run.isChecked())
        cfg["login_timeout_seconds"] = self.inp_login_timeout.value()
        cfg["screenshots_dir"] = self.inp_screenshots_dir.text().strip() or None
        cfg["applied_log"] = self.inp_applied_log.text().strip() or None

        # strip None values for cleanliness
        cfg = self._strip_none(cfg)
        return cfg

    @staticmethod
    def _strip_none(obj: object) -> object:
        if isinstance(obj, dict):
            return {k: SettingsWindow._strip_none(v) for k, v in obj.items() if v is not None}
        if isinstance(obj, list):
            return [SettingsWindow._strip_none(v) for v in obj if v is not None]
        return obj

    # ── actions ───────────────────────────────────────────────────

    def _on_save(self) -> None:
        cfg = self._collect_config()
        save_config(self._config_path, cfg)
        QMessageBox.information(self, "Сохранено", f"Настройки записаны в {self._config_path}")

    def _on_start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            QMessageBox.warning(self, "Уже запущено", "run-both.sh уже работает.")
            return

        # autosave
        cfg = self._collect_config()
        save_config(self._config_path, cfg)

        self._log.clear()
        self._log.appendPlainText("=" * 40)
        self._log.appendPlainText("Старт агентов...")

        # LLM health check
        try:
            from .nim_client import get_nim_client, LLMClient
            client = get_nim_client()
            self._log.appendPlainText(f"LLM endpoint: {client.base_url}")
            self._log.appendPlainText(f"LLM model:    {client.model}")
            # quick ping with 10 s timeout
            ping = LLMClient(
                api_key=client.api_key,
                base_url=client.base_url,
                model=client.model,
                timeout_seconds=10,
                temperature=client.temperature,
                max_tokens=50,
                do_not_train=client.do_not_train,
            )
            reply = ping.chat_completion(system_prompt="ping", user_prompt="Привет!")
            self._log.appendPlainText(f"LLM ping OK:  {reply[:60]}...")
        except Exception as exc:
            self._log.appendPlainText(f"⚠️  LLM check failed: {exc}")

        # check script exists
        if not Path(self.RUNBOTH_PATH).exists():
            QMessageBox.critical(self, "Ошибка", f"Не найден {self.RUNBOTH_PATH}")
            return

        try:
            self._proc = subprocess.Popen(
                [self.RUNBOTH_PATH],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка запуска", str(exc))
            return

        self._stdout_thread = LogReaderThread(self._proc.stdout, self)
        self._stdout_thread.line_ready.connect(self._log.appendPlainText)
        self._stdout_thread.start()
        self._stderr_thread = LogReaderThread(self._proc.stderr, self)
        self._stderr_thread.line_ready.connect(self._log.appendPlainText)
        self._stderr_thread.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_status.setText("Работает")
        self.lbl_status.setStyleSheet("color: #2e7d32; font-weight: bold; padding-left: 8px;")

    def _on_stop(self) -> None:
        if self._proc is None:
            return

        if platform.system() == "Windows":
            try:
                self._proc.terminate()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=8)
            except TimeoutExpired:
                try:
                    self._proc.kill()
                except OSError:
                    pass
                try:
                    self._proc.wait(timeout=3)
                except TimeoutExpired:
                    pass
        else:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            else:
                try:
                    self._proc.wait(timeout=8)
                except TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
        self._proc = None
        self._stop_log_threads()

        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/F", "/FI", "IMAGENAME eq python.exe"], capture_output=True)
            subprocess.run(["taskkill", "/F", "/FI", "IMAGENAME eq chrome.exe", "/FI", "WINDOWTITLE eq *"], capture_output=True)
        else:
            # Kill Python agents (also catches grandchildren that PG signal may miss)
            subprocess.run(["pkill", "-TERM", "-f", "python main.py .*user-data-dir user-data-search"], capture_output=True)
            subprocess.run(["pkill", "-TERM", "-f", "python main.py .*check-negotiations"], capture_output=True)
            time.sleep(2)
            subprocess.run(["pkill", "-KILL", "-f", "python main.py .*user-data-dir user-data-search"], capture_output=True)
            subprocess.run(["pkill", "-KILL", "-f", "python main.py .*check-negotiations"], capture_output=True)

            # also kill lingering chrome processes
            subprocess.run(["pkill", "-9", "-f", "user-data-responder"], capture_output=True)
            subprocess.run(["pkill", "-9", "-f", "user-data-search"], capture_output=True)

        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText("Остановлено")
        self.lbl_status.setStyleSheet("color: #888; padding-left: 8px;")

    def _stop_log_threads(self) -> None:
        if self._stdout_thread is not None:
            self._stdout_thread.stop()
            self._stdout_thread.wait(2000)
            self._stdout_thread = None
        if self._stderr_thread is not None:
            self._stderr_thread.stop()
            self._stderr_thread.wait(2000)
            self._stderr_thread = None

    def _refresh_status(self) -> None:
        if self._proc is None:
            return
        rc = self._proc.poll()
        if rc is not None:
            # finished unexpectedly
            self._proc = None
            self._stop_log_threads()
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.lbl_status.setText(f"Завершился (код {rc})")
            self.lbl_status.setStyleSheet("color: #c62828; padding-left: 8px;")

    def closeEvent(self, event) -> None:
        if self._proc is not None:
            self._on_stop()
        event.accept()


# ── entrypoint ─────────────────────────────────────────────────────


def run_gui(config_path: str = "config.yaml") -> int:
    app = QApplication.instance() or QApplication([])
    win = SettingsWindow(config_path)
    win.show()
    return app.exec()
