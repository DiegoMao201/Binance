from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def update_env_value(content: str, key: str, value: str) -> str:
    lines = content.splitlines()
    updated_lines: list[str] = []
    found = False

    for line in lines:
        if line.startswith(f"{key}="):
            updated_lines.append(f"{key}={value}")
            found = True
        else:
            updated_lines.append(line)

    if not found:
        updated_lines.append(f"{key}={value}")

    return "\n".join(updated_lines) + "\n"


def main() -> None:
    content = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""

    print("Configuración segura de credenciales para OptiFerre-Trader")
    binance_key = input("Binance API Key: ").strip()
    binance_secret = input("Binance API Secret: ").strip()
    openrouter_key = input("OpenRouter API Key: ").strip()

    content = update_env_value(content, "BINANCE_API_KEY", binance_key)
    content = update_env_value(content, "BINANCE_API_SECRET", binance_secret)
    content = update_env_value(content, "OPENROUTER_API_KEY", openrouter_key)

    ENV_PATH.write_text(content, encoding="utf-8")
    print(f"Credenciales guardadas en {ENV_PATH}")


if __name__ == "__main__":
    main()