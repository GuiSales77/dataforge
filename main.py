"""DataForge — inicia o servidor local e abre a interface no navegador."""
import sys
import threading
import time
import webbrowser

from dataforge import config
from dataforge.server import make_server


def main():
    cfg = config.load()
    srv, port = make_server(int(cfg.get("porta", 8765)))
    url = f"http://127.0.0.1:{port}/"
    print("=" * 60)
    print(f"  DataForge {config.VERSION} — automação inteligente de planilhas")
    print(f"  Interface: {url}")
    print(f"  Dados:     {config.DATA_DIR}")
    print("  Feche esta janela (ou Ctrl+C) para encerrar.")
    print("=" * 60)
    if cfg.get("abrir_navegador", True) and "--no-browser" not in sys.argv:
        threading.Thread(target=lambda: (time.sleep(1.0), webbrowser.open(url)), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
