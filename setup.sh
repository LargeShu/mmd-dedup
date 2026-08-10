#!/usr/bin/env bash
# Подготовка окружения: venv, зависимости, каталоги, config.json.
#
#   ./setup.sh          обычная установка
#   ./setup.sh --force  пересоздать venv с нуля
#
# Скрипт ничего не делает молча: каждый шаг сообщает, что произошло.
# Повторный запуск безопасен — уже сделанное пропускается.
set -uo pipefail
cd "$(dirname "$0")"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
info() { printf "  · %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[31m✗ %s\033[0m\n" "$*"; exit 1; }
step() { printf "\n\033[1m%s\033[0m\n" "$*"; }

VENV="scripts/.venv"

step "1. Python"
command -v python3 >/dev/null 2>&1 || die "python3 не найден. Установите Python 3.10+"
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || die "нужен Python 3.10+, найден $PYV"
ok "Python $PYV"

step "2. Виртуальное окружение"
if [ "$FORCE" = "1" ] && [ -d "$VENV" ]; then
  rm -rf "$VENV"
  info "старое окружение удалено (--force)"
fi
if [ -d "$VENV" ] && "$VENV/bin/python" -c "import sys" >/dev/null 2>&1; then
  ok "уже есть: $VENV"
else
  [ -d "$VENV" ] && { rm -rf "$VENV"; warn "прежнее окружение нерабочее, пересоздаю"; }
  python3 -m venv "$VENV" || die "не удалось создать venv"
  ok "создано: $VENV"
fi
PY="$VENV/bin/python"

step "3. Зависимости"
"$PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
if "$PY" -m pip install --quiet -r requirements.txt; then
  ok "Pillow, imagehash, pillow-heif установлены"
else
  die "не удалось установить зависимости из requirements.txt"
fi
"$PY" -m pip install --quiet ruff >/dev/null 2>&1 \
  && ok "ruff (для ./check.sh)" || warn "ruff не установлен, ./check.sh будет неполным"

step "4. Каталоги"
for d in data reports; do
  if [ -d "$d" ]; then info "$d/ уже есть"; else mkdir -p "$d" && ok "создан $d/"; fi
done

step "5. Настройка путей"
if [ -f config.json ]; then
  ok "config.json уже есть"
  "$PY" - <<'PY'
import json, os, sys
sys.path.insert(0, "scripts")
try:
    cfg = json.load(open("config.json", encoding="utf-8"))
except Exception as e:
    print(f"  ! config.json не читается: {e}"); raise SystemExit
drives = cfg.get("drives") or {}
if not drives:
    print("  ! в config.json не задано ни одного диска")
for name, path in drives.items():
    метка = "✓" if os.path.isdir(path) else "!"
    примечание = "" if os.path.isdir(path) else "  <-- НЕДОСТУПЕН СЕЙЧАС"
    print(f"    {метка} {name}: {path}{примечание}")
t = cfg.get("target")
print(f"    целевой диск: {t or '(не задан)'}")
PY
else
  cp config.example.json config.json
  warn "создан config.json из образца — ВПИШИТЕ СВОИ ПУТИ"
  info "откройте config.json и укажите каталоги с фотографиями"
fi

step "6. Дополнительно"
if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg есть — кадры из видео будут извлекаться"
else
  info "ffmpeg не найден: превью для видео недоступны (необязательно)"
  info "  macOS: brew install ffmpeg"
fi
if command -v sqlite3 >/dev/null 2>&1; then
  ok "sqlite3 есть"
else
  info "sqlite3 не найден: пригодится для проверки базы (необязательно)"
fi

step "Готово"
cat <<EOF

  Дальше:

    1. Проверьте пути в config.json
    2. Постройте карту архива:
         $PY scripts/01_inventory.py
    3. Узнайте, сколько займёт следующий шаг:
         $PY scripts/02_signatures.py --plan
    4. Снимите отпечатки:
         $PY scripts/02_signatures.py --stage all
    5. Соберите отчёт:
         $PY scripts/03_report.py
       и откройте reports/decision.html

  Подробности — в README.md
EOF
