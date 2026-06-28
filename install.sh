#!/usr/bin/env bash
# install.sh — Claude Code Telegram bot installer
#
# Auto-detect distro (Fedora, Ubuntu/Debian/Mint, Arch/Manjaro, openSUSE, Alpine),
# install python3+venv+pip, set up app dir + venv + deps, prompt for config,
# (optionally) install systemd unit. Idempotent: aman dijalankan ulang.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/<user>/claude-code-telegram-bot/main/install.sh | bash
#   # atau setelah clone:
#   ./install.sh
#
# Flags (env):
#   INSTALL_DIR=$HOME/.cc-tg   # lokasi instalasi (default)
#   SKIP_SYSTEMD=1             # jangan setup systemd (mis. tanpa root)
#   ASSUME_YES=1               # jawab "y" semua prompt
#   REPO_URL=...               # override repo (kalau fork)
#   BRANCH=main                # branch yang di-clone

set -eu

INSTALL_DIR="${INSTALL_DIR:-$HOME/.cc-tg}"
REPO_URL="${REPO_URL:-https://github.com/zesbe/claude-code-telegram-bot.git}"
BRANCH="${BRANCH:-main}"
ASSUME_YES="${ASSUME_YES:-0}"
SKIP_SYSTEMD="${SKIP_SYSTEMD:-0}"

# ── pretty helpers ──────────────────────────────────────────────────────────
if [ -t 1 ]; then
    C_RED=$(printf '\033[31m'); C_GRN=$(printf '\033[32m'); C_YLW=$(printf '\033[33m')
    C_BLU=$(printf '\033[34m'); C_BLD=$(printf '\033[1m');  C_RST=$(printf '\033[0m')
else
    C_RED=; C_GRN=; C_YLW=; C_BLU=; C_BLD=; C_RST=
fi
say()  { printf '%s\n' "${C_BLU}▶${C_RST} $*"; }
ok()   { printf '%s\n' "${C_GRN}✓${C_RST} $*"; }
warn() { printf '%s\n' "${C_YLW}!${C_RST} $*" >&2; }
die()  { printf '%s\n' "${C_RED}✗${C_RST} $*" >&2; exit 1; }
ask()  {
    # ask "Question" "default" → echoes answer
    if [ "$ASSUME_YES" = "1" ]; then printf '%s\n' "$2"; return; fi
    printf '%s [%s] ' "$1" "$2" >&2
    read -r _ans
    [ -z "${_ans:-}" ] && _ans="$2"
    printf '%s\n' "$_ans"
}

# ── 0. preflight ────────────────────────────────────────────────────────────
say "Claude Code Telegram bot installer"

# Hindari install sebagai root: bot ini interaktif & nyimpan state di $HOME.
if [ "$(id -u)" = "0" ]; then
    die "Jangan jalankan sebagai root. Run sebagai user biasa; script akan minta sudo kalau perlu."
fi

# ── 1. detect distro ────────────────────────────────────────────────────────
DISTRO_ID=""; DISTRO_LIKE=""
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-}"
    DISTRO_LIKE="${ID_LIKE:-}"
fi
ok "Distro: ${DISTRO_ID:-unknown} (like: ${DISTRO_LIKE:-—})"

# Pilih pkg manager + nama paket
PKG=""; PKG_INSTALL=""; PKG_UPDATE=""; PKG_LIST="python3 python3-venv python3-pip git curl"
case "$DISTRO_ID $DISTRO_LIKE" in
    *fedora*|*rhel*|*centos*|*rocky*|*almalinux*|*amzn*)
        if command -v dnf >/dev/null 2>&1; then PKG="dnf"; else PKG="yum"; fi
        PKG_INSTALL="sudo $PKG install -y"
        PKG_UPDATE="sudo $PKG -y makecache"
        PKG_LIST="python3 python3-pip git curl"   # venv ada di python3 stdlib di Fedora/RHEL
        ;;
    *ubuntu*|*debian*|*linuxmint*|*pop*|*elementary*|*kali*|*raspbian*|*"linux mint"*)
        PKG="apt-get"
        PKG_INSTALL="sudo DEBIAN_FRONTEND=noninteractive apt-get install -y"
        PKG_UPDATE="sudo apt-get update -y"
        ;;
    *arch*|*manjaro*|*endeavouros*|*garuda*)
        PKG="pacman"
        PKG_INSTALL="sudo pacman -S --noconfirm --needed"
        PKG_UPDATE="sudo pacman -Sy"
        PKG_LIST="python python-pip git curl"
        ;;
    *suse*|*opensuse*|*sles*)
        PKG="zypper"
        PKG_INSTALL="sudo zypper -n install"
        PKG_UPDATE="sudo zypper -n refresh"
        PKG_LIST="python3 python3-pip git curl"
        ;;
    *alpine*)
        PKG="apk"
        PKG_INSTALL="sudo apk add --no-cache"
        PKG_UPDATE="sudo apk update"
        PKG_LIST="python3 py3-pip git curl"
        ;;
    *void*)
        PKG="xbps-install"
        PKG_INSTALL="sudo xbps-install -Sy"
        PKG_UPDATE=""
        PKG_LIST="python3 python3-pip git curl"
        ;;
    *)
        warn "Distro tak dikenal: '$DISTRO_ID'. Skip instalasi paket sistem — pastikan python3, pip, git, curl sudah ada."
        ;;
esac

need_install=""
for c in python3 git curl; do
    command -v "$c" >/dev/null 2>&1 || need_install="$need_install $c"
done
# python3 punya venv module?
if command -v python3 >/dev/null 2>&1; then
    python3 -c "import venv" 2>/dev/null || need_install="$need_install python3-venv"
fi

if [ -n "$need_install" ] && [ -n "$PKG_INSTALL" ]; then
    say "Install paket sistem: $PKG_LIST"
    # shellcheck disable=SC2086
    $PKG_UPDATE >/dev/null 2>&1 || true
    # shellcheck disable=SC2086
    $PKG_INSTALL $PKG_LIST || die "Gagal install paket sistem via $PKG"
    ok "Paket sistem terpasang"
elif [ -n "$need_install" ]; then
    die "Paket berikut belum ada dan tak ada pkg manager dikenali:$need_install"
else
    ok "python3, pip, git, curl: tersedia"
fi

# ── 2. clone / update repo ──────────────────────────────────────────────────
mkdir -p "$(dirname "$INSTALL_DIR")"

if [ -d "$INSTALL_DIR/.git" ]; then
    say "Repo sudah ada di $INSTALL_DIR — update dari $BRANCH"
    git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH" || die "git fetch gagal"
    # Pertahankan config.json/providers.json lokal (ada di .gitignore, aman).
    git -C "$INSTALL_DIR" checkout --quiet "$BRANCH" || true
    git -C "$INSTALL_DIR" reset --hard --quiet "origin/$BRANCH"
    ok "Repo di-update"
elif [ -d "$INSTALL_DIR" ] && [ "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    die "$INSTALL_DIR sudah ada tapi bukan git repo. Pindahkan/hapus dulu."
else
    say "Clone $REPO_URL → $INSTALL_DIR"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" || die "git clone gagal"
    ok "Repo ter-clone"
fi

# ── 3. venv + deps ──────────────────────────────────────────────────────────
cd "$INSTALL_DIR"
if [ ! -x ".venv/bin/python" ]; then
    say "Buat virtualenv di .venv"
    python3 -m venv .venv || die "venv gagal — paket python3-venv mungkin belum terpasang"
    ok "venv siap"
fi
say "Install Python dependencies"
.venv/bin/python -m pip install --quiet --upgrade pip >/dev/null
.venv/bin/python -m pip install --quiet -r requirements.txt || die "pip install gagal"
ok "Deps terpasang ($(.venv/bin/python -c 'import httpx,telegramify_markdown;print("httpx",httpx.__version__)'))"

# ── 4. config interaktif ────────────────────────────────────────────────────
if [ ! -f "config.json" ]; then
    say "Setup config.json"
    TOKEN=$(ask "Telegram bot token (dari @BotFather)" "")
    [ -z "$TOKEN" ] && die "Token Telegram wajib diisi"
    OWNER=$(ask "Telegram user ID kamu (dari @userinfobot)" "")
    [ -z "$OWNER" ] && die "Owner ID wajib diisi (siapa pun yang chat selain ID ini akan diblokir)"

    # Validasi ringan
    case "$TOKEN" in
        *:*) : ;;
        *) die "Format token aneh — harus '<digits>:<string>'" ;;
    esac
    case "$OWNER" in
        ''|*[!0-9]*) die "Owner ID harus angka" ;;
    esac

    cat > config.json <<EOF
{
  "telegram_token": "$TOKEN",
  "owner_ids": [$OWNER],
  "default_provider": "claude",
  "model_slot": "opus",
  "claude_timeout": 1800,
  "max_concurrent": 3,
  "auto_compact_ratio": 0
}
EOF
    chmod 600 config.json
    ok "config.json dibuat (mode 600)"
else
    ok "config.json sudah ada — tidak diubah"
fi

# Pastikan providers.json minimal ada (template kosong, biar bot tidak crash)
if [ ! -f "providers.json" ]; then
    printf '{"providers": {}}\n' > providers.json
    chmod 600 providers.json
    ok "providers.json kosong dibuat (tambah lewat /provider di Telegram)"
fi

# ── 5. cek Claude CLI ───────────────────────────────────────────────────────
if ! command -v claude >/dev/null 2>&1; then
    warn "Binary 'claude' tidak ada di PATH. Bot butuh Claude Code CLI."
    warn "Install: https://docs.claude.com/en/docs/claude-code"
    warn "Atau kalau sudah ada, tambahkan ke PATH (sering di ~/.local/bin)."
else
    ok "claude CLI: $(command -v claude)"
fi

# ── 6. systemd unit (opsional) ──────────────────────────────────────────────
SETUP_SYSTEMD=0
if [ "$SKIP_SYSTEMD" != "1" ] && command -v systemctl >/dev/null 2>&1 \
   && [ -d /etc/systemd/system ]; then
    A=$(ask "Setup sebagai systemd service (auto-start saat boot)? [y/N]" "N")
    case "$A" in y|Y|yes|YES) SETUP_SYSTEMD=1 ;; esac
fi

if [ "$SETUP_SYSTEMD" = "1" ]; then
    SVC=/etc/systemd/system/cc-tg.service
    say "Generate $SVC dari template"
    sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$INSTALL_DIR|g" \
        cc-tg.service.template | sudo tee "$SVC" >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now cc-tg.service
    sleep 1
    if systemctl is-active --quiet cc-tg.service; then
        ok "cc-tg.service aktif. Cek: 'systemctl status cc-tg' / 'journalctl -u cc-tg -f'"
    else
        warn "Service tidak aktif. Cek: 'journalctl -u cc-tg -n 50'"
    fi
else
    ok "Skip systemd. Jalankan manual: '$INSTALL_DIR/start.sh'"
fi

# ── done ────────────────────────────────────────────────────────────────────
echo
ok "${C_BLD}Installasi selesai!${C_RST}"
echo "   Folder    : $INSTALL_DIR"
echo "   Config    : $INSTALL_DIR/config.json  (mode 600)"
echo "   Start     : $INSTALL_DIR/start.sh"
[ "$SETUP_SYSTEMD" = "1" ] && echo "   Service   : systemctl status cc-tg"
echo
echo "Langkah berikutnya:"
echo "  1. Chat ke bot Telegram-mu — ketik /start"
echo "  2. (opsional) /provider add  → tambah provider non-default"
echo "  3. /help  → lihat semua command"
