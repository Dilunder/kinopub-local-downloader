"""Import only explicitly supplied cookies; never read a browser database here."""
import os
import re
import secrets

from app.config import config
from app.services.security import secure_cookie_file

ALLOWED_DOMAINS = {"kino.pub", "kino.watch", "www.kino.pub", "www.kino.watch"}
COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
MAX_BYTES = 1_048_576


def normalize_netscape(text: str) -> tuple[str, int]:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0] not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
        raise ValueError("Нужен Netscape cookies.txt с заголовком # Netscape HTTP Cookie File")
    output = ["# Netscape HTTP Cookie File"]
    for index, line in enumerate(lines[1:], 2):
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        fields = line.split("\t")
        domain = fields[0].removeprefix("#HttpOnly_").lstrip(".").lower()
        if domain not in ALLOWED_DOMAINS:
            continue
        if (len(fields) != 7 or fields[1] not in {"TRUE", "FALSE"}
                or fields[3] not in {"TRUE", "FALSE"} or not fields[4].isdigit()
                or not fields[2].startswith("/") or not COOKIE_NAME.fullmatch(fields[5])
                or any(ord(character) < 32 or ord(character) == 127 for character in fields[6])):
            raise ValueError(f"Некорректная запись в строке {index}: нужны 7 полей через TAB")
        output.append(line)
    if len(output) == 1:
        raise ValueError("В файле нет cookies kino.pub / kino.watch")
    return "\n".join(output) + "\n", len(output) - 1


def from_header(value: str, domain: str) -> tuple[str, int]:
    if domain not in {"kino.pub", "kino.watch"}:
        raise ValueError("Выберите домен kino.pub или kino.watch")
    value = value.strip()
    if value.lower().startswith("cookie:"):
        value = value[7:].strip()
    if not value or len(value.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Вставьте значение Cookie из Request Headers, не более 1 МБ")
    if any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise ValueError("Вставьте только одну строку Cookie из Request Headers")
    rows, names = ["# Netscape HTTP Cookie File"], set()
    for item in value.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("Ожидается Cookie в виде имя=значение; другое_имя=значение")
        name, cookie_value = item.split("=", 1)
        name, cookie_value = name.strip(), cookie_value.strip()
        if not COOKIE_NAME.fullmatch(name) or name.lower() in {"expires", "max-age", "domain", "path", "samesite"}:
            raise ValueError("Нужен заголовок Cookie из Request Headers; Set-Cookie не поддерживается")
        if name in names:
            raise ValueError("В Cookie повторяется имя. Используйте экспорт Netscape cookies.txt для разных путей")
        names.add(name)
        # Request headers lack expiry/path attributes. Preserve the supplied
        # values as host-only session cookies, secure and inaccessible to JS.
        rows.append(f"#HttpOnly_{domain}\tFALSE\t/\tTRUE\t0\t{name}\t{cookie_value}")
    if not names:
        raise ValueError("Вставьте значение Cookie из Request Headers")
    return "\n".join(rows) + "\n", len(names)


def save_cookie_text(text: str):
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Файл cookies слишком большой (лимит 1 МБ)")
    temporary = config.data_dir / ("cookies-" + secrets.token_hex(8) + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            secure_cookie_file(temporary)
            stream.write(text)
        os.replace(temporary, config.data_dir / "cookies.txt")
    except (OSError, ValueError):
        raise ValueError("Не удалось безопасно сохранить cookies.txt. Проверьте права папки data")
    finally:
        temporary.unlink(missing_ok=True)
