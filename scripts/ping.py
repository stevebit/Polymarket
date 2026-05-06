"""Check CLOB reachability (no wallet required)."""

from polymarket_manual.clients import make_readonly_client
from polymarket_manual.config import load_settings


def main() -> None:
    settings = load_settings()
    client = make_readonly_client(settings)
    print("get_ok:", client.get_ok())
    print("server_time:", client.get_server_time())


if __name__ == "__main__":
    main()
