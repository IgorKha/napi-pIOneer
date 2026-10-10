"""python -m pioneer [serve] — запуск core; python -m pioneer set-password — задать пароль входа."""
import argparse
import getpass
import sys

from pioneer.config import load_config


def main() -> None:
    ap = argparse.ArgumentParser(prog="pioneer")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="запустить core (по умолчанию)")
    pw = sub.add_parser("set-password", help="задать пароль входа в веб-интерфейс")
    pw.add_argument("--stdin", action="store_true", help="прочитать пароль из stdin (для скриптов)")
    pw.add_argument("--if-missing", action="store_true", help="ничего не делать, если пароль уже задан")
    args = ap.parse_args()
    cfg = load_config()

    if args.cmd == "set-password":
        from pioneer.auth import Auth
        auth = Auth(cfg.data_dir)
        if args.if_missing and auth.password_set:
            return
        if args.stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("Новый пароль: ")
            if getpass.getpass("Ещё раз: ") != password:
                sys.exit("пароли не совпадают")
        try:
            auth.set_password(password)
        except ValueError as e:
            sys.exit(str(e))
        print(f"пароль сохранён в {auth.pw_file}; все сессии сброшены")
        return

    import uvicorn

    from pioneer.api.app import create_app
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
