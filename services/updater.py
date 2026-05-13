"""Auto-updater. Проверяет GitHub Releases на новую версию,
качает .exe инсталлятор в %TEMP%, запускает его в /SILENT режиме.

Inno Setup с тем же AppId (см. installer.iss) автоматически apgreid'ит
текущую установку поверх, и [Run]-секция перезапускает Knox.exe."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import subprocess
from typing import Callable, Optional

import requests

from core.version import VERSION

GITHUB_REPO = "sseconddeath/Knox"
LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Лимит на скачивание установщика. Сейчас .exe ~65 МБ; ставим запас
# с явной защитой от «бесконечного» ответа (повреждённый CDN, MITM).
MAX_INSTALLER_BYTES = 300 * 1024 * 1024  # 300 МБ


def _parse_version(tag: str) -> Optional[tuple[int, ...]]:
    """`v1.0.2` / `1.0.2` → `(1, 0, 2)`. None если формат не подходит —
    тогда сравнение версий просто пропустим."""
    m = re.match(r"v?(\d+(?:\.\d+){1,3})", tag.strip())
    if not m:
        return None
    try:
        return tuple(int(x) for x in m.group(1).split("."))
    except ValueError:
        return None


def _is_newer(remote: str, local: str) -> bool:
    r = _parse_version(remote)
    l = _parse_version(local)
    if not r or not l:
        return False
    return r > l


def check_for_update(timeout: float = 10.0) -> Optional[dict]:
    """Запрашивает /releases/latest у GitHub. Возвращает dict если
    есть новее текущей, иначе None. Никогда не бросает — все ошибки
    глотаются (нет интернета, GitHub timeout и т.п. — это не повод
    падать приложению на старте).

    Структура ответа:
        {
            "version": "1.0.3",        # tag без префикса v
            "name": "Knox v1.0.3",     # display title релиза
            "url": "https://...exe",   # прямая ссылка на .exe asset
            "size": 67890123,          # размер asset в байтах
            "notes": "...",            # release notes (markdown)
        }
    """
    try:
        r = requests.get(LATEST_URL, timeout=timeout,
                         headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None

    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None
    if not _is_newer(tag, VERSION):
        return None

    # Ищем .exe в assets — берём первый совпавший Knox_Setup_*.exe.
    installer = None
    for asset in data.get("assets", []):
        name = asset.get("name") or ""
        if name.lower().endswith(".exe") and "setup" in name.lower():
            installer = asset
            break
    if not installer:
        return None

    return {
        "version": tag.lstrip("v"),
        "name": data.get("name") or f"Knox {tag}",
        "url": installer.get("browser_download_url") or "",
        "size": int(installer.get("size") or 0),
        "notes": data.get("body") or "",
    }


def download_installer(
    url: str,
    on_progress: Optional[Callable[[int, int], None]] = None,
    timeout: float = 60.0,
) -> Optional[str]:
    """Качает .exe в %TEMP%\\Knox_Update_<version>.exe. Стримит
    кусками, чтобы UI мог показывать прогресс. Возвращает путь к
    файлу или None при ошибке.

    on_progress(received_bytes, total_bytes) — total может быть 0
    если сервер не вернул Content-Length."""
    if not url.startswith(("http://", "https://")):
        return None
    try:
        r = requests.get(url, stream=True, timeout=timeout,
                         allow_redirects=True)
        if r.status_code != 200:
            return None
        total = int(r.headers.get("Content-Length") or 0)
        if total and total > MAX_INSTALLER_BYTES:
            return None

        # Имя файла: пытаемся вытащить из URL, иначе fallback.
        fname = os.path.basename(url.split("?", 1)[0]) or "Knox_Update.exe"
        dest = os.path.join(tempfile.gettempdir(), fname)

        received = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                received += len(chunk)
                if received > MAX_INSTALLER_BYTES:
                    # ответ оказался длиннее заявленного — рвём.
                    try:
                        f.close()
                        os.remove(dest)
                    except OSError:
                        pass
                    return None
                f.write(chunk)
                if on_progress:
                    on_progress(received, total)
        return dest
    except Exception:
        try:
            if "dest" in locals() and os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
        return None


def launch_installer_and_quit(installer_path: str) -> bool:
    """Запускает скачанный инсталлятор в silent-режиме и завершает
    текущий процесс приложения. Inno Setup с CloseApplications=force
    закроет/убьёт открытый Knox.exe и обновит файлы поверх, после чего
    [Run]-секция запустит обновлённый exe.

    Возвращает True только если subprocess стартовал — но реально
    в этом случае мы тут же зовём sys.exit и юзер ответ не увидит."""
    if not installer_path or not os.path.exists(installer_path):
        return False
    try:
        # /SILENT показывает мини-прогресс (без визарда),
        # /SUPPRESSMSGBOXES — без подтверждений,
        # /NORESTART — нам не нужна перезагрузка системы.
        # Запускаем detached, чтобы инсталлятор пережил наш sys.exit.
        flags = 0
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [installer_path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            creationflags=flags,
            close_fds=True,
        )
    except Exception:
        return False
    # Даём инсталлятору момент стартовать, потом выходим.
    # Сам инсталлятор подождёт пока Knox.exe освободит файлы.
    sys.exit(0)
