"""Збережені сервери: збереження/заміна/видалення в settings.json, назви, ключі не виходять у діагностику."""
import json
import tempfile
from pathlib import Path

from agent import logs, servers, settings as S


def main() -> None:
    real = S.SETTINGS_FILE
    S.SETTINGS_FILE = Path(tempfile.mkdtemp()) / "settings.json"          # ніколи не чіпаємо реальні налаштування
    S._CACHE["key"] = S._CACHE["val"] = None
    try:
        assert servers.names() == [] and servers.get("x") is None and not servers.delete("x")
        n1 = servers.save({"backend": "openai", "host": "1.2.3.4", "port": "8000", "model": "Qwen", "context": "262144", "api_key": "SECRET-1"})
        n2 = servers.save({"backend": "ollama", "model": "gemma", "api_key": ""})
        assert n1 == "1.2.3.4:8000 · Qwen" and n2 == "Локально · gemma" and servers.names() == [n1, n2]
        e = servers.get(n1)
        assert e["api_key"] == "SECRET-1" and e["port"] == "8000" and e["context"] == "262144"
        servers.save({"backend": "openai", "host": "1.2.3.4", "port": "8000", "model": "Qwen", "api_key": "NEW"})   # та сама назва → заміна
        assert len(servers.names()) == 2 and servers.get(n1)["api_key"] == "NEW"
        d = S.load(); d["theme"] = "dark"; S.save(d)
        servers.save({"backend": "openai", "host": "h", "port": "", "model": "", "api_key": "k"})     # інші налаштування не губляться
        assert S.load()["theme"] == "dark" and "h · авто" in servers.names()
        assert servers.delete(n2) and n2 not in servers.names()
        # діагностика не віддає ключі збережених серверів
        import zipfile
        real_dir = logs.DIAG_DIR
        logs.DIAG_DIR = Path(tempfile.mkdtemp())
        try:
            out = logs.make_diagnostics(S.SETTINGS_FILE, net_fetch=lambda u: 200, net_resolve=lambda h: "1.2.3.4")
            blob = zipfile.ZipFile(out).read("settings.json").decode("utf-8")
        finally:
            logs.DIAG_DIR = real_dir
        assert "NEW" not in blob and "\"k\"" not in blob and "<прихований>" in blob
    finally:
        S.SETTINGS_FILE = real
        S._CACHE["key"] = S._CACHE["val"] = None
    print("OK: servers — збереження/заміна/видалення, назви, ключі не в діагностиці")


if __name__ == "__main__":
    main()
