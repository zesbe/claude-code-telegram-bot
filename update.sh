#!/usr/bin/env bash
# update.sh — update CC-TG ke versi terbaru dari GitHub, lalu restart.
# Aman: config.json/providers.json/sessions/ di-gitignore → TIDAK tersentuh.
# File kode (cc_tg.py dll) di-reset ke versi origin/main (buang edit lokal).
#
# Pakai:
#   ~/.cc-tg/update.sh             # update + restart (kalau service) / minta start manual
#   curl -fsSL https://raw.githubusercontent.com/zesbe/claude-code-telegram-bot/main/update.sh | bash
set -eu

DIR="${INSTALL_DIR:-$HOME/.cc-tg}"
BRANCH="${BRANCH:-main}"

if [ -t 1 ]; then G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; B=$'\e[1m'; X=$'\e[0m'
else G=; Y=; R=; D=; B=; X=; fi
say(){ printf '%s\n' "${G}▶${X} $*"; }
warn(){ printf '%s\n' "${Y}!${X} $*" >&2; }
die(){ printf '%s\n' "${R}✗ $*${X}" >&2; exit 1; }

[ -d "$DIR/.git" ] || die "$DIR bukan git repo. Install dulu lewat install.sh."
cd "$DIR"

say "Cek versi sekarang…"
OLD=$(git rev-parse --short HEAD 2>/dev/null || echo "?")

say "Ambil update dari origin/$BRANCH…"
git fetch --quiet origin "$BRANCH" || die "git fetch gagal (cek koneksi)."

NEW=$(git rev-parse --short "origin/$BRANCH")
if [ "$OLD" = "$NEW" ]; then
    say "${B}Sudah versi terbaru${X} ($OLD). Tidak ada update."
    UPDATED=0
else
    # Reset HANYA file tracked ke versi GitHub. config/providers/sessions
    # aman (gitignored, tidak ikut tracked). Edit lokal pada kode dibuang.
    say "Update $OLD → $NEW (reset file kode ke versi GitHub)…"
    git reset --hard --quiet "origin/$BRANCH" || die "git reset gagal."
    UPDATED=1
fi

# Refresh dependency kalau requirements berubah (aman dijalankan selalu)
if [ -x ".venv/bin/python" ] && [ -f requirements.txt ]; then
    say "Sinkron dependency…"
    .venv/bin/python -m pip install --quiet -r requirements.txt 2>/dev/null || warn "pip sync skip"
fi

# Restart
if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled --quiet cc-tg 2>/dev/null; then
    say "Restart service cc-tg…"
    sudo systemctl restart cc-tg
    sleep 3
    if systemctl is-active --quiet cc-tg; then
        say "${B}✓ Update selesai${X} → $NEW, bot jalan lagi."
    else
        warn "Service tidak aktif. Cek: journalctl -u cc-tg -n 50"
    fi
else
    if [ "$UPDATED" = "1" ]; then
        say "${B}✓ Kode terupdate${X} → $NEW."
    fi
    warn "Bukan systemd service. Restart manual: matikan bot lalu $DIR/start.sh"
fi
