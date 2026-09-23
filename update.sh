#!/usr/bin/env bash
# update.sh — update CC-TG ke versi terbaru dari GitHub, lalu restart.
# Aman: config.json/providers.json/sessions/ di-gitignore → TIDAK tersentuh.
# File kode (cc_tg.py dll) di-reset ke versi origin/main (buang edit lokal).
#
# Pakai:
#   ~/.cc-tg/update.sh             # update + restart (kalau service) / minta start manual
#   curl -fsSL https://raw.githubusercontent.com/zesbe/claude-code-telegram-bot/main/update.sh | bash
#
# Seluruh isi dibungkus main(): bash mem-parse SEMUANYA dulu sebelum jalan.
# Tanpa ini, `git reset` di tengah jalan mengganti file skrip ini sendiri —
# bash membaca skrip per-offset, jadi eksekusi bisa loncat ke baris acak.
set -eu

if [ -t 1 ]; then G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; X=$'\e[0m'
else G=; Y=; R=; B=; X=; fi
say(){ printf '%s\n' "${G}▶${X} $*"; }
warn(){ printf '%s\n' "${Y}!${X} $*" >&2; }
die(){ printf '%s\n' "${R}✗ $*${X}" >&2; exit 1; }

main() {
    DIR="${INSTALL_DIR:-$HOME/.cc-tg}"
    BRANCH="${BRANCH:-main}"

    [ -d "$DIR/.git" ] || die "$DIR bukan git repo. Install dulu lewat install.sh."
    cd "$DIR"

    say "Cek versi sekarang…"
    OLD=$(git rev-parse --short HEAD 2>/dev/null || echo "?")

    say "Ambil update dari origin/$BRANCH…"
    git fetch --quiet origin "$BRANCH" || die "git fetch gagal (cek koneksi)."
    NEW=$(git rev-parse --short "origin/$BRANCH")

    # PENGAMAN (2026-08-05): commit lokal yang BELUM di-push HILANG kalau
    # di-reset --hard (kejadian nyata: /update menghapus 11 commit fitur).
    # Ada commit lokal / perubahan file tracked belum di-commit → STOP.
    # (File untracked tak dihitung: reset --hard tidak menyentuhnya.)
    AHEAD=$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 0)
    DIRTY=$(git status --porcelain --untracked-files=no 2>/dev/null | head -20)
    if [ "${AHEAD:-0}" -gt 0 ] || [ -n "$DIRTY" ]; then
        warn "Update DIBATALKAN demi keamanan:"
        if [ "${AHEAD:-0}" -gt 0 ]; then
            warn "  • ada $AHEAD commit lokal yang belum di-push ke GitHub"
        fi
        if [ -n "$DIRTY" ]; then
            warn "  • ada perubahan file yang belum di-commit"
        fi
        warn "  Reset ke versi GitHub akan MENGHAPUS pekerjaan itu."
        warn "  Backup dulu:  cd $DIR && git bundle create ~/cctg-backup.bundle --all"
        warn "  Lalu push:    git push origin $BRANCH"
        warn "  Atau paksa (SADAR akan kehilangan): FORCE_UPDATE=1 $DIR/update.sh"
        [ "${FORCE_UPDATE:-0}" = "1" ] || die "Ada pekerjaan lokal — update dihentikan."
        warn "FORCE_UPDATE=1 → lanjut reset (pekerjaan lokal dibuang)."
    fi

    if [ "$OLD" = "$NEW" ]; then
        say "${B}Sudah versi terbaru${X} ($OLD). Tidak ada update."
        return 0                     # tak ada yang berubah → tak perlu restart
    fi

    # Titik balik sebelum reset (selain reflog) — pulihkan: git reset --hard <tag>
    git tag -f "pre-update-$(date +%Y%m%d-%H%M%S)" HEAD >/dev/null 2>&1 || true
    say "Update $OLD → $NEW (reset file kode ke versi GitHub)…"
    git reset --hard --quiet "origin/$BRANCH" || die "git reset gagal."

    # Sinkron dependency (requirements bisa berubah bersama kode)
    if [ -x ".venv/bin/python" ] && [ -f requirements.txt ]; then
        say "Sinkron dependency…"
        .venv/bin/python -m pip install --quiet -r requirements.txt 2>/dev/null \
            || warn "pip sync dilewati"
    fi

    # Dipanggil dari /update bot (CCTG_FROM_BOT=1): JANGAN restart dari sini —
    # `systemctl restart` dari dalam cgroup bot membunuh skrip ini + proses bot
    # yang sedang menunggu hasilnya (hasil tak pernah terlapor), dan di mesin
    # yang sudo-nya minta password malah gagal. Bot restart sendiri setelah lapor.
    if [ "${CCTG_FROM_BOT:-0}" = "1" ]; then
        say "${B}✓ Kode terupdate${X} → $NEW. Bot restart sendiri."
        return 0
    fi

    # Dijalankan manual dari terminal → restart service di sini
    if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled --quiet cc-tg 2>/dev/null; then
        say "Restart service cc-tg…"
        if sudo systemctl restart cc-tg; then
            sleep 3
            if systemctl is-active --quiet cc-tg; then
                say "${B}✓ Update selesai${X} → $NEW, bot jalan lagi."
            else
                warn "Service tidak aktif. Cek: journalctl -u cc-tg -n 50"
            fi
        else
            warn "Gagal restart. Jalankan manual: sudo systemctl restart cc-tg"
        fi
    else
        say "${B}✓ Kode terupdate${X} → $NEW."
        warn "Bukan systemd service. Restart manual: matikan bot lalu $DIR/start.sh"
    fi
}

main "$@"; exit $?
