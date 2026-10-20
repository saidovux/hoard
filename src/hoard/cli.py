"""CLI entrypoints. No UI code executes in background daemon."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .ipc import ensure_daemon, request


async def run_command(args: argparse.Namespace):
    if args.command == "daemon":
        from .service import run_daemon
        await run_daemon()
        return
    # shutdown/status stop are harmless if daemon exists; otherwise spawn service.
    if args.command in {"stop", "shutdown", "status"}:
        try:
            data = await request(args.command)
        except (OSError, ConnectionError, FileNotFoundError):
            data = (
                {"message": "Hoard не запущен"}
                if args.command != "status"
                else {"running": False}
            )
    else:
        await ensure_daemon()
        if args.command == "add":
            data = await request("source_add", {"url": args.url, "output": args.output,
                                                "interval": args.interval})
        elif args.command == "retry":
            data = await request("job_retry", {"id": args.id})
        else:
            data = await request(args.command)
    if args.command == "status" or args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(data.get("message", "Готово"))


def main():
    p = argparse.ArgumentParser(description="Hoard — локальный архиватор Telegram")
    sub = p.add_subparsers(dest="command")
    for name in ("ui", "daemon", "start", "stop", "scan", "status", "shutdown"):
        sub.add_parser(name)
    a = sub.add_parser("add", help="Добавить пост без запуска TUI")
    a.add_argument("url")
    a.add_argument("--output", default="~/Videos/hoard")
    a.add_argument("--interval", type=int, default=120)
    retry = sub.add_parser("retry", help="Повторить неудачное задание")
    retry.add_argument("id", type=int)
    p.add_argument("--json", action="store_true", help="Машинный вывод (для команд без TUI)")
    # argparse normally accepts parent options only before the subcommand.
    # Keep `hoard --json status` AND `hoard status --json` usable.
    for cmd in sub.choices.values():
        cmd.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                         help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.command in {None, "ui"}:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print(
                "Ошибка: интерактивный CLI требует терминала. Для скриптов: hoard status",
                file=sys.stderr,
            )
            raise SystemExit(1)
        from .console_ui import launch
        launch()
        return
    try:
        asyncio.run(run_command(args))
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
