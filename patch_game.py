#!/usr/bin/env python3
"""Standalone, standard-library installer for Yog-Sothoth's Yard RU beta QA02."""

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from ctypes import wintypes

if sys.platform == "win32":
    import winreg


PACKAGE_ROOT = Path(__file__).resolve().parent / "patch"
GAME_EXE = "Yog-Sothoth's Yard.exe"
DATA_DIR = "Yog-Sothoth's Yard_Data"
BACKUP_DIR = "YogSothothYard_RU_Backup"
ALLOWED_PATHS = {f"{DATA_DIR}/resources.assets", f"{DATA_DIR}/sharedassets0.assets"}
CHUNK_SIZE = 1024 * 1024


class PatchError(Exception):
    """A checked failure that should be displayed without a traceback."""


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(CHUNK_SIZE):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(package_root):
    path = package_root / "patch_manifest.json"
    if path.stat().st_size > 64 * 1024 * 1024:
        raise PatchError("Файл описания патча слишком велик.")
    with path.open("r", encoding="utf-8-sig") as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int or manifest.get("format_version") != 1 or manifest.get("game") != "Yog-Sothoth's Yard":
        raise PatchError("Неподдерживаемое описание патча.")
    if manifest.get("steam_build") != "24329824":
        raise PatchError("Этот установщик предназначен для Steam build 24329824.")
    payload_size = manifest.get("payload_size")
    if type(payload_size) is not int or payload_size < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("payload_sha256", ""))):
        raise PatchError("Некорректный размер или SHA-256 данных патча.")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) != 2:
        raise PatchError("Патч должен содержать ровно два игровых файла.")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise PatchError("Некорректное описание игрового файла.")
        relative = entry.get("relative_path")
        if not isinstance(relative, str) or relative not in ALLOWED_PATHS or relative in seen:
            raise PatchError("Неожиданный или повторяющийся путь в описании патча.")
        seen.add(relative)
        for field in ("original_sha256", "patched_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get(field, ""))):
                raise PatchError("Некорректный SHA-256 игрового файла.")
        for field in ("original_size", "patched_size"):
            if type(entry.get(field)) is not int or entry[field] < 0:
                raise PatchError("Некорректный размер игрового файла.")
        operations = entry.get("operations")
        if not isinstance(operations, list):
            raise PatchError("Отсутствует список операций патча.")
        total = 0
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("kind") not in ("copy", "add"):
                raise PatchError("Неизвестная операция патча.")
            offset, length = operation.get("offset"), operation.get("length")
            if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0:
                raise PatchError("Некорректный диапазон операции патча.")
            input_size = entry["original_size"] if operation["kind"] == "copy" else payload_size
            if offset > input_size or length > input_size - offset:
                raise PatchError("Операция патча выходит за границы исходного файла.")
            total += length
            if total > entry["patched_size"]:
                raise PatchError("Патч превышает ожидаемый размер результата.")
        if total != entry["patched_size"]:
            raise PatchError("Размер операций не совпадает с размером результата.")
    return manifest


def find_game(requested, non_interactive=False):
    if requested:
        return Path(requested.strip().strip('"')).expanduser().resolve(strict=True)
    # Tests and automation must always name their target explicitly.
    if non_interactive:
        raise PatchError("Для --non-interactive обязательно укажите --game ПУТЬ.")
    nearby = (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent)
    for path in nearby:
        if (path / GAME_EXE).is_file():
            print(f"Найдена игра: {path}")
            return path
    roots = set()
    for hive, key_name in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                           (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
                           (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for name in ("SteamPath", "InstallPath", "SteamExe"):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    if isinstance(value, str) and value:
                        root = Path(value)
                        if name == "SteamExe":
                            root = root.parent
                        if root.is_absolute():
                            roots.add(root)
        except OSError:
            continue
    libraries = set(roots)
    for root in roots:
        for relative in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
            vdf = root / relative
            if not vdf.is_file():
                continue
            try:
                content = vdf.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                continue
            for value in re.findall(r'(?im)^\s*"(?:path|\d+)"\s*"((?:\\.|[^"\\])*)"', content):
                library = Path(value.replace("\\\\", "\\").replace('\\"', '"'))
                if library.is_absolute():
                    libraries.add(library)
    found = sorted({(library / "steamapps/common/Yog-Sothoth's Yard").resolve()
                    for library in libraries if (library / "steamapps/common/Yog-Sothoth's Yard" / GAME_EXE).is_file()}, key=str)
    if len(found) == 1:
        print(f"Найдена игра: {found[0]}")
        return found[0]
    if found:
        print("Найдено несколько копий игры:")
        for number, path in enumerate(found, start=1):
            print(f"  {number}. {path}")
    selection = input("Введите полный путь к папке игры" + (" или номер" if found else "") + " (Enter — отмена): ").strip()
    if found and selection.isdecimal() and 1 <= int(selection) <= len(found):
        return found[int(selection) - 1]
    if not selection:
        raise PatchError("Операция отменена.")
    return Path(selection.strip('"')).expanduser().resolve(strict=True)


def ensure_game_stopped():
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        success = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while success:
            if entry.szExeFile.casefold() == GAME_EXE.casefold():
                raise PatchError("Закройте игру перед установкой или восстановлением.")
            success = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


@contextlib.contextmanager
def operation_lock(game_root):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    # No sharing; Windows removes the lock even if this process is interrupted.
    handle = kernel.CreateFileW(str(game_root / ".yog_ru_operation.lock"), 0xC0000000, 0, None, 1, 0x04000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise PatchError("Не удалось заблокировать папку игры: другая установка запущена или нет прав записи.")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def replace_game_file(pending, target, rollback=None):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.ReplaceFileW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
    kernel.ReplaceFileW.restype = wintypes.BOOL
    if not kernel.ReplaceFileW(str(target), str(pending), str(rollback) if rollback else None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def write_patched_file(source, payload_path, output, entry):
    with source.open("rb") as original, payload_path.open("rb") as payload, output.open("xb") as result:
        for operation in entry["operations"]:
            stream = original if operation["kind"] == "copy" else payload
            stream.seek(operation["offset"])
            remaining = operation["length"]
            while remaining:
                block = stream.read(min(CHUNK_SIZE, remaining))
                if not block:
                    raise PatchError("Неожиданный конец данных патча.")
                result.write(block)
                remaining -= len(block)
        result.flush()
        os.fsync(result.fileno())
    if output.stat().st_size != entry["patched_size"] or file_hash(output) != entry["patched_sha256"]:
        raise PatchError("Результат сборки не прошёл проверку SHA-256.")


def run_patch(action, game_root, package_root=PACKAGE_ROOT, replace_file=replace_game_file):
    if sys.platform != "win32":
        raise PatchError("Установщик поддерживает Windows 10/11.")
    if action not in ("install", "restore", "verify"):
        raise PatchError("Неизвестное действие.")
    game_root = Path(game_root).resolve(strict=True)
    package_root = Path(package_root).resolve(strict=True)
    if not (game_root / GAME_EXE).is_file():
        raise PatchError(f"В выбранной папке отсутствует {GAME_EXE}.")
    manifest = load_manifest(package_root)
    lock = operation_lock(game_root) if action != "verify" else contextlib.nullcontext()
    with lock:
        entries = []
        for entry in manifest["files"]:
            target = game_root / entry["relative_path"]
            if target.is_symlink() or not target.is_file() or target.resolve().parent != (game_root / DATA_DIR):
                raise PatchError(f"Недопустимый или отсутствующий файл: {entry['relative_path']}")
            current = file_hash(target)
            if current not in (entry["original_sha256"], entry["patched_sha256"]):
                raise PatchError(f"Неподдерживаемый или изменённый файл: {entry['relative_path']}. Нужен Steam build {manifest['steam_build']}.")
            expected_size = entry["original_size"] if current == entry["original_sha256"] else entry["patched_size"]
            if target.stat().st_size != expected_size:
                raise PatchError("Размер игрового файла не совпадает с ожидаемым.")
            entries.append((entry, target, current))
        if action == "verify":
            states = {entry["relative_path"]: "ru" if current == entry["patched_sha256"] else "original"
                      for entry, _, current in entries}
            for relative, state in states.items():
                print(f"{relative}: {'русский перевод' if state == 'ru' else 'оригинал'}")
            return states
        ensure_game_stopped()
        payload = package_root / "payload.bin"
        if action == "install" and (not payload.is_file() or payload.stat().st_size != manifest["payload_size"]
                                    or file_hash(payload) != manifest["payload_sha256"]):
            raise PatchError("Данные патча повреждены. Файлы игры не изменены.")
        backup_root = game_root / BACKUP_DIR
        if backup_root.is_symlink() or backup_root.resolve().parent != game_root:
            raise PatchError("Недопустимая папка резервных копий.")
        planned = []
        for entry, target, current in entries:
            wanted = entry["patched_sha256"] if action == "install" else entry["original_sha256"]
            if current == wanted:
                continue
            backup = backup_root / (target.name + ".original")
            if os.path.lexists(backup):
                if backup.is_symlink() or not backup.is_file() or file_hash(backup) != entry["original_sha256"]:
                    raise PatchError(f"Резервная копия повреждена: {backup.name}.")
            elif action == "restore":
                raise PatchError(f"Отсутствует оригинальная резервная копия: {backup.name}.")
            pending = target.with_name(target.name + ".yog_ru_pending")
            rollback = target.with_name(target.name + ".yog_ru_rollback")
            if os.path.lexists(pending) or os.path.lexists(rollback):
                raise PatchError("Найдены временные файлы предыдущей установки. Сохраните их для восстановления и устраните причину прерывания.")
            planned.append((entry, target, current, backup, pending, rollback, wanted))
        staged, committed = [], []
        try:
            # Stage and verify every output before changing any game resource.
            for entry, target, current, backup, pending, rollback, wanted in planned:
                if action == "install" and not backup.exists():
                    backup_root.mkdir(exist_ok=True)
                    with target.open("rb") as original, backup.open("xb") as copy:
                        shutil.copyfileobj(original, copy, CHUNK_SIZE)
                        copy.flush()
                        os.fsync(copy.fileno())
                    if file_hash(backup) != entry["original_sha256"]:
                        raise PatchError("Новая резервная копия не прошла проверку.")
                staged.append((pending, target, rollback, current))
                if action == "install":
                    write_patched_file(target, payload, pending, entry)
                else:
                    with backup.open("rb") as original, pending.open("xb") as copy:
                        shutil.copyfileobj(original, copy, CHUNK_SIZE)
                        copy.flush()
                        os.fsync(copy.fileno())
                    if file_hash(pending) != wanted:
                        raise PatchError("Подготовленный оригинал не прошёл проверку.")
            ensure_game_stopped()
            for pending, target, rollback, current in staged:
                if file_hash(target) != current:
                    raise PatchError("Файлы игры изменились во время подготовки. Операция отменена.")
            for pending, target, rollback, current in staged:
                replace_file(pending, target, rollback)
                committed.append((pending, target, rollback, current))
        except BaseException:
            # Use the real primitive so injected commit failures cannot block rollback.
            for _, target, rollback, current in reversed(committed):
                try:
                    replace_game_file(rollback, target)
                    if file_hash(target) != current:
                        raise PatchError("SHA-256 восстановленного файла не совпадает.")
                except (OSError, PatchError) as error:
                    print(f"ВНИМАНИЕ: автоматический откат {target.name} не завершён: {error}. Сохранены резервные копии.", file=sys.stderr)
            for pending, _, _, _ in staged:
                try:
                    pending.unlink(missing_ok=True)
                except OSError as error:
                    print(f"Не удалось удалить {pending.name}: {error}", file=sys.stderr)
            raise
        for _, _, rollback, _ in committed:
            try:
                rollback.unlink(missing_ok=True)
            except OSError as error:
                print(f"Установка завершена, но не удалось удалить {rollback.name}: {error}", file=sys.stderr)
        print("Русификатор установлен. В настройках игры выберите English. Корейский временно недоступен."
              if action == "install" else "Оригинальные файлы восстановлены. Резервные копии сохранены.")
        return {"action": action, "changed_files": len(committed)}


def main():
    parser = argparse.ArgumentParser(description="Русификатор Yog-Sothoth's Yard, публичная бета QA02.")
    parser.add_argument("action", nargs="?", default="install", choices=("install", "restore", "verify"),
                        help="install — установить; restore — восстановить; verify — проверить")
    parser.add_argument("--game", metavar="ПУТЬ", help="папка с Yog-Sothoth's Yard.exe")
    parser.add_argument("--non-interactive", action="store_true", help="без вопросов; --game обязателен")
    arguments = parser.parse_args()
    try:
        if sys.version_info < (3, 10):
            raise PatchError("Требуется Python 3.10 или новее.")
        if sys.platform != "win32":
            raise PatchError("Установщик поддерживает Windows 10/11.")
        game_root = find_game(arguments.game, arguments.non_interactive)
        run_patch(arguments.action, game_root)
        return 0
    except (PatchError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Операция отменена.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
