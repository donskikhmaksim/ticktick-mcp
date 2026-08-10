"""Разрешить якорь-по-имени `файл.py::имя` в `файл:строка` (2026-08-09).

Числовые якоря (`server.py:14528`) живут до первой правки выше по файлу.
Именные (`server.py::_gate_single`) переживают и сдвиг строк, и переезд
определения в другой файл — поэтому все НОВЫЕ ссылки в документах ТЗ пишутся
именами, а этот скрипт разрешает их в адрес на текущей ревизии.

    python tools/where.py _gate_single
    python tools/where.py server.py::_gate_single
"""
import ast
import sys
from pathlib import Path

ROOTS = ("ticktick_mcp", "attic", "tools", "tests", "scripts")


def find(name: str, only_file: str = ""):
    hits = []
    for root in ROOTS:
        for f in sorted(Path(root).rglob("*.py")) if Path(root).exists() else []:
            if only_file and f.name != Path(only_file).name:
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for n in tree.body:
                got = []
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                    got = [n.name]
                elif isinstance(n, ast.Assign):
                    got = [t.id for t in n.targets if isinstance(t, ast.Name)]
                elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                    got = [n.target.id]
                if name in got:
                    hits.append(f"{f}:{n.lineno}")
    return hits


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    arg = sys.argv[1]
    only_file, name = ("", arg)
    if "::" in arg:
        only_file, name = arg.split("::", 1)
    hits = find(name, only_file)
    if not hits:
        print(f"не найдено: {arg}")
        return 1
    for h in hits:
        print(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
