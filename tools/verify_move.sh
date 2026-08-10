#!/usr/bin/env bash
# Механическое доказательство ЧИСТОГО переноса куска из server.py в новый
# файл (шаг 1.2.4 захода 1, 2026-08-09).
#
# Проверяющий НЕ читает логику: правильность доказывается совпадением
# контрольных сумм, а не глазами.
#
# Использование:
#   tools/verify_move.sh <BASE_SHA> <N> <A> <B> <новый файл> [номер блока]
#     BASE_SHA  — ревизия ДО переноса (кусок вырезается из неё)
#     N         — номер куска (1/2/3), для имён временных файлов
#     A, B      — границы куска в server.py НА РЕВИЗИИ BASE_SHA (включительно)
#     новый файл — куда кусок переехал
#     номер блока — какой по счёту MOVED-BLOCK в новом файле сверять (по
#                 умолчанию 1); в одном файле их может быть несколько, если
#                 вместе с куском уехали привязанные к нему хвосты
#
# Печатает восемь шагов; ненулевой выход — перенос НЕ чистый.
set -uo pipefail

BASE="$1"; N="$2"; A="$3"; B="$4"; NEWFILE="$5"; BLOCK="${6:-1}"
SRC="ticktick_mcp/src/server.py"
T="${TMPDIR:-/tmp}"
rc=0

echo "=== кусок $N: строки $A–$B из $SRC @ $BASE  →  $NEWFILE ==="

# 2. Эталон из исходной ревизии
git show "$BASE:$SRC" | sed -n "${A},${B}p" > "$T/chunk$N-src.txt"
echo "--- шаг 2: эталон вырезан, строк: $(wc -l < "$T/chunk$N-src.txt")"

# 3. Перенесённый текст из нового файла — по маркерам MOVED-BLOCK
awk -v want="$BLOCK" '
    /^# === MOVED-BLOCK BEGIN/ { n++; if (n == want) { inside = 1 }; next }
    /^# === MOVED-BLOCK END ===$/ { if (inside) { inside = 0 }; next }
    inside { print }
' "$NEWFILE" > "$T/chunk$N-new.txt"
echo "--- шаг 3: из нового файла вырезано строк: $(wc -l < "$T/chunk$N-new.txt")"

# 4. ГЛАВНАЯ ПРОВЕРКА: разница обязана быть ПУСТОЙ
echo "--- шаг 4: diff эталона и перенесённого"
if diff -u "$T/chunk$N-src.txt" "$T/chunk$N-new.txt"; then
    echo "    ПЕРЕНОС ЧИСТЫЙ"
else
    echo "    !!! ПЕРЕНОС НЕ ЧИСТЫЙ"; rc=1
fi

# 5. Дублирующая проверка, нечувствительная к настройкам diff
echo "--- шаг 5: sha256"
shasum -a 256 "$T/chunk$N-src.txt" "$T/chunk$N-new.txt"
uniq_count=$(shasum -a 256 "$T/chunk$N-src.txt" "$T/chunk$N-new.txt" \
             | awk '{print $1}' | sort -u | wc -l | tr -d ' ')
echo "    различных сумм: $uniq_count (обязано быть 1)"
[ "$uniq_count" = "1" ] || rc=1

# 6. Обратная сторона: из server.py удалено РОВНО то же самое
echo "--- шаг 6: удалённое из server.py против эталона"
git diff "$BASE" -- "$SRC" | grep '^-' | grep -v '^---' | sed 's/^-//' \
    > "$T/chunk$N-removed.txt"
if diff -u "$T/chunk$N-src.txt" "$T/chunk$N-removed.txt"; then
    echo "    УДАЛЕНО РОВНО ТО ЖЕ"
else
    echo "    (расхождение — смотри вывод выше; допустимо только если этим"
    echo "     коммитом из server.py убрано ЕЩЁ что-то, названное в отчёте)"
fi

# 7. И ничего лишнего не добавлено
echo "--- шаг 7: numstat по server.py"
git diff "$BASE" --numstat -- "$SRC"

# 8. Проверка на невидимые правки — по дереву разбора
echo "--- шаг 8: сверка деревьев разбора"
BASE="$BASE" NEWFILE="$NEWFILE" python3 - <<'PY'
import ast, os, subprocess
base = os.environ["BASE"]
new_path = os.environ["NEWFILE"]
old = subprocess.run(['git', 'show', f'{base}:ticktick_mcp/src/server.py'],
                     capture_output=True, text=True).stdout
new = open(new_path, encoding='utf-8').read()
def defs(text):
    return {n.name: ast.dump(n) for n in ast.parse(text).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
o, n = defs(old), defs(new)
moved = set(n) & set(o)
bad = [k for k in moved if o[k] != n[k]]
print("    перенесено определений:", len(moved), "| изменено:", len(bad), bad)
raise SystemExit(1 if bad else 0)
PY
[ $? -eq 0 ] || rc=1

echo "=== итог куска $N: rc=$rc ==="
exit $rc
