"""Получение списка событий с scena24.ru через headless Chromium.

Сайт защищён ServicePipe (JS-challenge + поведенческий анализ). Прямой
``requests`` блокируется, поэтому используем реальный браузер через Playwright:
1. открываем ``https://scena24.ru/v2/`` и ждём, пока React-приложение
   отрендерит карточки событий (появление ссылок ``/v2/event/<id>``);
2. в контексте страницы делаем ``fetch('/api/events')`` — этот эндпоинт
   отдаёт JSON со списком событий.

Возвращаемый список — массив словарей с полями ``id``, ``name``,
``start_date`` (Unix-секунды), ``event_category``, ``image`` и др.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger(__name__)

SITE_URL = "https://scena24.ru/v2/"
API_URL = "https://scena24.ru/api/events"

# Запускаем Playwright синхронно — для cron это удобнее всего.
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Playwright не установлен. Выполните: pip install -r requirements.txt && playwright install chromium"
    ) from exc


@contextmanager
def _browser(headless: bool = True) -> Iterator:
    """Открывает Chromium. ``channel='chromium'`` использует установленный
    через ``playwright install chromium`` бандл; если хотите системный
    Chrome на Ubuntu — замените на ``executable_path='/usr/bin/chromium-browser'``."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",                # обязательно при запуске от root в контейнерах
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            yield browser
        finally:
            browser.close()


def fetch_events(headless: bool = True, wait_ms: int = 4000, max_attempts: int = 3) -> list[dict]:
    """Возвращает список событий или поднимает исключение.

    ServicePipe на части IP пропускает только браузер с признаками
    «живого» пользователя — иначе возвращает challenge-страницу со
    спиннером и не показывает React-приложение. Поэтому:

    * эмулируем небольшую прокрутку и движение мыши,
    * при таймауте делаем до ``max_attempts`` повторных попыток,
    * ждём дольше (``wait_ms`` применяется и как post-load слип, и
      как запас на challenge).
    """
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        log.info("Попытка %d/%d открыть сайт", attempt, max_attempts)
        try:
            events = _try_fetch_once(headless=headless, wait_ms=wait_ms)
            return events
        except RuntimeError as exc:
            last_error = exc
            log.warning("Попытка %d не удалась: %s", attempt, exc)
            # перед следующей попыткой — пауза, чтобы ServicePipe «забыл» пометку
            time.sleep(3 * attempt)

    raise RuntimeError(
        f"Не удалось получить события после {max_attempts} попыток: {last_error}"
    )


def _try_fetch_once(headless: bool, wait_ms: int) -> list[dict]:
    with _browser(headless=headless) as browser:
        # Минимальный набор параметров, поддерживаемый всеми актуальными
        # версиями Playwright. ``timezone``/``locale`` опускаем намеренно:
        # в более старых версиях они не принимаются ``new_context`` и
        # не влияют на прохождение ServicePipe.
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        # Открываем главную: именно здесь ServicePipe прогонит JS-challenge
        # и проставит нужные cookie в контекст.
        log.info("Открываю %s", SITE_URL)
        page.goto(SITE_URL, wait_until="domcontentloaded", timeout=30_000)

        # Если ServicePipe оставил challenge-страницу — на ней виден
        # спиннер ``#id_spinner``. Эмулируем поведение пользователя:
        # прокрутка + движение мышью. Это снимает поведенческий фильтр
        # на части IP-адресов.
        try:
            has_challenge = page.evaluate(
                "() => !!document.querySelector('#id_spinner') || /servicepipe/i.test(document.body.innerHTML)"
            )
        except Exception:
            has_challenge = False
        if has_challenge:
            log.info("Обнаружен challenge-спиннер, эмулирую прокрутку/мышь")
            try:
                page.mouse.move(640, 400)
                page.mouse.move(700, 420, steps=5)
                page.evaluate("window.scrollTo(0, 300)")
                page.wait_for_timeout(1500)
                page.evaluate("window.scrollTo(0, 0)")
            except Exception as exc:  # noqa: BLE001
                log.debug("Эмуляция поведения не удалась: %s", exc)

        # Дожидаемся отрисовки React-приложения. На странице должна быть
        # хотя бы одна ссылка на конкретное событие.
        try:
            page.wait_for_selector(
                "a[href*='/v2/event/']",
                state="attached",
                timeout=30_000,
            )
        except PlaywrightTimeoutError as exc:
            # Диагностируем: что именно сейчас на странице
            current_url = page.url
            try:
                content_preview = page.content()[:600]
            except Exception:  # noqa: BLE001
                content_preview = "(не удалось получить содержимое)"
            log.error(
                "Карточки событий не появились. URL=%s, фрагмент страницы: %s",
                current_url,
                content_preview,
            )
            raise RuntimeError(
                "ServicePipe не пропустил запрос или сайт изменил вёрстку: "
                "не дождались карточек событий "
                f"(текущий URL: {current_url})"
            ) from exc

        # Дополнительно даём React-приложению подтянуть данные.
        page.wait_for_timeout(wait_ms)

        # Забираем JSON событий прямо в контексте страницы — обходит любые
        # IP/cookie-проверки на этом домене.
        log.info("Запрашиваю %s через page context", API_URL)
        raw = page.evaluate(
            """async (url) => {
                const r = await fetch(url, { credentials: 'include' });
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return await r.text();
            }""",
            API_URL,
        )

        ctx.close()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Не удалось разобрать ответ API: {exc}; первые 200 символов: {raw[:200]!r}"
        ) from exc
