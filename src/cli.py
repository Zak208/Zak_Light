"""
Command line control of a running Zak_light engine (for shortcuts, scheduled tasks, hotkeys).

    Zak_light.exe --set-mode off|screen|rhythm|effect|static
    Zak_light.exe --brightness 0-100
    Zak_light.exe --status
    Zak_light.exe --quit          (clean shutdown: LEDs off)

Exit code 0 on success, 1 if the engine is not running or rejected the request. Import-light on
purpose: it must not load the engine stack.
"""

import json
import sys
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8765"
MODES = ("screen", "rhythm", "effect", "static", "off")


def _request(path: str, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE_URL + path, data=data, method="GET" if payload is None else "POST")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.load(resp)


def run_cli(args) -> int:
    try:
        if "--status" in args:
            status = _request("/api/status")
            print(f"Zak_light {status['version']}: mode={status['mode']} connected={status['connected']}")
            return 0

        if "--quit" in args:
            _request("/api/quit", {})
            print("Zak_light se esta cerrando")
            return 0

        if "--toggle" in args:
            mode = _request("/api/status")["mode"]
            _request("/api/mode", {"mode": "screen" if mode == "off" else "off"})
            print("Tira apagada" if mode != "off" else "Tira encendida")
            return 0

        if "--profile" in args:
            i = args.index("--profile")
            name = args[i + 1] if i + 1 < len(args) else ""
            _request("/api/profiles/load", {"name": name})
            print(f"Perfil: {name}")
            return 0

        if "--effect" in args:
            i = args.index("--effect")
            key = args[i + 1] if i + 1 < len(args) else ""
            _request("/api/effect", {"effect": key})
            print(f"Efecto: {key}")
            return 0

        if "--set-mode" in args:
            i = args.index("--set-mode")
            mode = args[i + 1] if i + 1 < len(args) else ""
            if mode not in MODES:
                print(f"Modo no valido: '{mode}'. Usa: {', '.join(MODES)}")
                return 1
            _request("/api/mode", {"mode": mode})
            print(f"Modo: {mode}")
            return 0

        if "--brightness" in args:
            i = args.index("--brightness")
            value = float(args[i + 1]) if i + 1 < len(args) else -1.0
            if not 0.0 <= value <= 100.0:
                print("El brillo debe estar entre 0 y 100")
                return 1
            _request("/api/settings", {"brightness": value / 100.0})
            print(f"Brillo: {value:g}%")
            return 0
    except urllib.error.HTTPError as e:
        print("Zak_light rechazo la orden (nombre de perfil o efecto no valido)" if e.code in (404, 422) else f"Error {e.code}")
        return 1
    except (urllib.error.URLError, OSError):
        print("Zak_light no esta en ejecucion")
        return 1
    except (ValueError, KeyError):
        print("Argumentos no validos")
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(run_cli(sys.argv[1:]))
