"""Перепривязка якорей вида `файл.py:НОМЕР` в документах ТЗ (2026-08-09).

Зачем. Разнос главного файла (шаг 1.2.4) уносит куски кода в другие файлы и
сдвигает номера строк в оставшемся. Все ссылки вида `server.py:14528`,
которых в документах сотни, после этого показывают на ЧУЖОЙ код — и следующий
исполнитель прочитает его, ничего не заподозрив. Поэтому якоря перепривязы-
ваются механически, а не глазами.

Как. Для каждого якоря берётся ТЕКСТ строки на опорной ревизии (--base) и
ищется во всём дереве на текущей ревизии:
  * ровно одно совпадение  → якорь переписывается на новый `файл:строка`;
  * ноль или больше одного → якорь НЕ трогается, печатается «не разрешено»
    вместе с текстом строки, чтобы человек разобрал руками.

Режимы:
  --write   переписать якоря в файлах;
  --verify  обратная проверка: строка по КАЖДОМУ текущему якорю обязана
            совпасть с текстом, взятым из --base по этому же якорю до
            перепривязки (сверка ведётся по журналу --write) либо, если
            журнала нет, просто проверить, что якорь указывает на строку,
            которая существует.

Пример:
    python tools/reanchor.py --base $(cat /tmp/base.sha) --write docs/TZ/
    python tools/reanchor.py --base $(cat /tmp/base.sha) --verify docs/TZ/
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Якорь: путь к .py (возможно с каталогами) + двоеточие + номер строки.
ANCHOR_RE = re.compile(r"\b((?:[\w./-]+/)?[\w.-]+\.py):(\d+)\b")

# Куда смотреть при поиске текста строки на текущей ревизии.
SEARCH_GLOBS = ("ticktick_mcp/**/*.py", "attic/*.py", "tools/*.py",
                "tests/**/*.py", "scripts/**/*.py")

# Журнал перепривязки (old → new + текст строки на BASE) — по нему работает
# обратная проверка `--verify`. Лежит вне репозитория, чтобы не мусорить.
JOURNAL = Path(os.environ.get("REANCHOR_JOURNAL", "/tmp/reanchor-journal.json"))


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout


def base_file_lines(base: str, path: str):
    """Строки файла `path` на ревизии `base`, или None если файла там нет."""
    out = subprocess.run(["git", "show", f"{base}:{path}"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return out.stdout.split("\n")


def current_tree_files(root: Path):
    seen = []
    for pat in SEARCH_GLOBS:
        seen.extend(sorted(root.glob(pat)))
    return seen


def find_unique(text: str, files, root: Path):
    """Найти строку `text` во всём дереве. Возвращает (файл, номер) или None
    при нуле/нескольких совпадениях."""
    if not text.strip():
        return None
    hits = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").split("\n")
        except (UnicodeDecodeError, OSError):
            continue
        for i, ln in enumerate(lines, 1):
            if ln == text:
                hits.append((str(f.relative_to(root)), i))
                if len(hits) > 1:
                    return None
    return hits[0] if len(hits) == 1 else None


def collect_docs(targets):
    docs = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            docs.extend(sorted(p.rglob("*.md")))
        elif p.is_file():
            docs.append(p)
    return docs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="опорная ревизия (SHA)")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--root", default=".")
    ap.add_argument("targets", nargs="+")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    docs = collect_docs(args.targets)
    files = current_tree_files(root)

    if args.verify:
        return verify(args, docs, root)

    base_cache = {}
    journal = {}
    total = resolved = unresolved = unchanged = 0
    problems = []

    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        out = []
        pos = 0
        for m in ANCHOR_RE.finditer(text):
            total += 1
            path, lineno = m.group(1), int(m.group(2))
            if path not in base_cache:
                base_cache[path] = base_file_lines(args.base, path)
            lines = base_cache[path]
            if lines is None or lineno < 1 or lineno > len(lines):
                unresolved += 1
                problems.append((str(doc), m.group(0),
                                 "нет такой строки на BASE"))
                continue
            src_line = lines[lineno - 1]
            hit = find_unique(src_line, files, root)
            if hit is None:
                unresolved += 1
                problems.append((str(doc), m.group(0), src_line.strip()[:100]))
                continue
            new_anchor = f"{hit[0]}:{hit[1]}"
            if new_anchor == m.group(0):
                unchanged += 1
                continue
            resolved += 1
            out.append(text[pos:m.start()])
            out.append(new_anchor)
            pos = m.end()
            journal.setdefault(str(doc), []).append(
                {"old": m.group(0), "new": new_anchor, "line": src_line})
        out.append(text[pos:])
        new_text = "".join(out)
        if args.write and new_text != text:
            doc.write_text(new_text, encoding="utf-8")

    if args.write:
        prev = {}
        if JOURNAL.exists():
            prev = json.loads(JOURNAL.read_text(encoding="utf-8"))
        for k, v in journal.items():
            prev.setdefault(k, []).extend(v)
        JOURNAL.write_text(json.dumps(prev, ensure_ascii=False, indent=1),
                           encoding="utf-8")

    print(f"якорей всего: {total}")
    print(f"перепривязано: {resolved}")
    print(f"осталось на месте: {unchanged}")
    print(f"не разрешено: {unresolved}")
    for doc, anchor, why in problems:
        print(f"   НЕ РАЗРЕШЁН {anchor} в {doc}: {why}")
    return 0


def verify(args, docs, root) -> int:
    """Обратная проверка: строка по новому адресу совпадает с текстом,
    взятым из BASE_SHA по старому адресу (журнал перепривязки)."""
    if not JOURNAL.exists():
        print("журнала перепривязки нет — сверять нечего")
        print("расхождений: 0")
        return 0
    journal = json.loads(JOURNAL.read_text(encoding="utf-8"))
    checked = mismatched = 0
    for doc, entries in journal.items():
        for e in entries:
            path, lineno = e["new"].rsplit(":", 1)
            f = root / path
            if not f.exists():
                print(f"   РАСХОЖДЕНИЕ {e['new']}: файла нет")
                mismatched += 1
                continue
            lines = f.read_text(encoding="utf-8").split("\n")
            i = int(lineno)
            checked += 1
            got = lines[i - 1] if 1 <= i <= len(lines) else "<нет строки>"
            if got != e["line"]:
                mismatched += 1
                print(f"   РАСХОЖДЕНИЕ {e['new']} ({doc}):")
                print(f"      ожидали: {e['line'][:90]!r}")
                print(f"      нашли:   {got[:90]!r}")
    print(f"проверено якорей: {checked}")
    print(f"расхождений: {mismatched}")
    return 1 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main())
