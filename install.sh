#!/usr/bin/env bash
# install.sh — Claude Code Telegram bot — interactive installer/wizard
#
# Satu perintah, beres: deteksi distro → install dependency → clone → venv →
# WIZARD setup (validasi token Telegram live, auto-detect owner ID) → systemd.
# Idempotent: aman dijalankan ulang (update).
#
# Pakai:
#   curl -fsSL https://raw.githubusercontent.com/zesbe/claude-code-telegram-bot/main/install.sh | bash
#   # atau setelah clone:  ./install.sh
#
# Non-interaktif / CI (lewati wizard):
#   CCTG_TOKEN=123:abc CCTG_OWNER=111 ASSUME_YES=1 SKIP_SYSTEMD=1 ./install.sh
#
# Env override:
#   INSTALL_DIR=$HOME/.cc-tg   REPO_URL=...   BRANCH=main
#   CCTG_TOKEN=  CCTG_OWNER=  CCTG_MODEL=opus  CCTG_WORKDIR=$HOME
#   ASSUME_YES=1  SKIP_SYSTEMD=1

set -eu

INSTALL_DIR="${INSTALL_DIR:-$HOME/.cc-tg}"
REPO_URL="${REPO_URL:-https://github.com/zesbe/claude-code-telegram-bot.git}"
BRANCH="${BRANCH:-main}"
ASSUME_YES="${ASSUME_YES:-0}"
SKIP_SYSTEMD="${SKIP_SYSTEMD:-0}"
# Pre-seed (opsional) — kalau diisi, wizard lewati prompt-nya tapi tetap validasi.
CCTG_TOKEN="${CCTG_TOKEN:-}"
CCTG_OWNER="${CCTG_OWNER:-}"
CCTG_MODEL="${CCTG_MODEL:-}"
CCTG_WORKDIR="${CCTG_WORKDIR:-}"

# ── terminal yang benar (penting saat `curl | bash`: stdin sudah dipakai pipe) ─
# Semua input wizard dibaca dari /dev/tty langsung, bukan stdin.
if [ -r /dev/tty ] && [ -w /dev/tty ]; then TTY=/dev/tty; else TTY=""; fi

# ── pretty helpers ────────────────────────────────────────────────────────────
if [ -t 1 ] || [ -t 2 ]; then
    C_RED=$(printf '\033[31m'); C_GRN=$(printf '\033[32m'); C_YLW=$(printf '\033[33m')
    C_BLU=$(printf '\033[36m'); C_DIM=$(printf '\033[2m');  C_BLD=$(printf '\033[1m')
    C_RST=$(printf '\033[0m')
else
    C_RED=; C_GRN=; C_YLW=; C_BLU=; C_DIM=; C_BLD=; C_RST=
fi
hr()   { printf '%s\n' "${C_DIM}────────────────────────────────────────────────────────${C_RST}" >&2; }
say()  { printf '%s\n' "${C_BLU}▶${C_RST} $*" >&2; }
ok()   { printf '%s\n' "${C_GRN}✓${C_RST} $*" >&2; }
warn() { printf '%s\n' "${C_YLW}!${C_RST} $*" >&2; }
die()  { printf '%s\n' "${C_RED}✗ $*${C_RST}" >&2; exit 1; }
step() { hr; printf '%s\n' "${C_BLD}${C_BLU}$*${C_RST}" >&2; }

# Prompt teks biasa.  ask "Pertanyaan" "default" → echo jawaban (ke stdout).
ask() {
    _q="$1"; _def="${2:-}"
    if [ "$ASSUME_YES" = "1" ] && [ -n "$_def" ]; then printf '%s\n' "$_def"; return; fi
    if [ -z "$TTY" ]; then
        # Tak ada terminal & tak ada default → tak bisa tanya.
        [ -n "$_def" ] && { printf '%s\n' "$_def"; return; }
        die "Butuh input '$_q' tapi tidak ada terminal. Set lewat env (CCTG_*) atau jalankan interaktif."
    fi
    if [ -n "$_def" ]; then printf '%s [%s%s%s] ' "$_q" "$C_DIM" "$_def" "$C_RST" >&2
    else printf '%s: ' "$_q" >&2; fi
    IFS= read -r _ans < "$TTY" || _ans=""
    [ -z "$_ans" ] && _ans="$_def"
    printf '%s\n' "$_ans"
}

# Prompt rahasia (token) — input tidak ditampilkan di layar.
ask_secret() {
    _q="$1"
    [ -z "$TTY" ] && die "Butuh input rahasia '$_q' tapi tidak ada terminal."
    printf '%s: ' "$_q" >&2
    stty -echo < "$TTY" 2>/dev/null || true
    IFS= read -r _ans < "$TTY" || _ans=""
    stty echo < "$TTY" 2>/dev/null || true
    printf '\n' >&2
    printf '%s\n' "$_ans"
}

# yes/no → return 0 (yes) / 1 (no)
confirm() {
    _q="$1"; _def="${2:-N}"
    _a=$(ask "$_q [$( [ "$_def" = Y ] && echo 'Y/n' || echo 'y/N' )]" "$_def")
    case "$_a" in y|Y|yes|YES|ya|Ya) return 0 ;; *) return 1 ;; esac
}

# ── Telegram API helpers ──────────────────────────────────────────────────────
# Validasi token via getMe → echo username bot kalau valid, return!=0 kalau gagal.
tg_validate() {
    _t="$1"
    _resp=$(curl -fsS --max-time 15 "https://api.telegram.org/bot${_t}/getMe" 2>/dev/null) || return 1
    printf '%s' "$_resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not d.get("ok"):
    sys.exit(1)
print(d["result"].get("username", "?"))
' 2>/dev/null
}

# Auto-detect owner ID: minta user kirim pesan ke bot, poll getUpdates ~90s.
# echo "<user_id> <first_name>" kalau ketemu; return!=0 kalau timeout.
tg_detect_owner() {
    _t="$1"
    _deadline=$(( $(date +%s) + 90 ))
    while [ "$(date +%s)" -lt "$_deadline" ]; do
        _resp=$(curl -fsS --max-time 30 \
            "https://api.telegram.org/bot${_t}/getUpdates?offset=-1&timeout=20&allowed_updates=%5B%22message%22%5D" \
            2>/dev/null) || { sleep 2; continue; }
        _out=$(printf '%s' "$_resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not d.get("ok") or not d.get("result"):
    sys.exit(1)
for u in reversed(d["result"]):
    m = u.get("message") or u.get("edited_message")
    if m and m.get("from"):
        f = m["from"]
        print(f["id"], f.get("first_name", "") or f.get("username", ""))
        sys.exit(0)
sys.exit(1)
' 2>/dev/null) && { printf '%s\n' "$_out"; return 0; }
        printf '.' >&2
        sleep 1
    done
    return 1
}

# ── banner ────────────────────────────────────────────────────────────────────
clear 2>/dev/null || true
printf '%s\n' "${C_BLD}${C_BLU}"  >&2
cat >&2 <<'BANNER'
   ____ ____      _____ ____
  / ___/ ___|    |_   _/ ___|   Claude Code  ·  Telegram
 | |  | |   _____  | || |  _    ───────────────────────────
 | |__| |__|_____| | || |_| |   chat → Claude Code ngerjain
  \____\____|      |_| \____|
BANNER
printf '%s\n' "${C_RST}" >&2

# ── 0. preflight ────────────────────────────────────────────────────────────
if [ "$(id -u)" = "0" ]; then
    die "Jangan jalankan sebagai root. Pakai user biasa; script minta sudo saat perlu."
fi

# ── 1. detect distro ──────────────────────────────────────────────────────────
step "1/6  Deteksi sistem"
DISTRO_ID=""; DISTRO_LIKE=""; DISTRO_NAME=""
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-}"; DISTRO_LIKE="${ID_LIKE:-}"; DISTRO_NAME="${PRETTY_NAME:-$ID}"
fi
ok "OS: ${DISTRO_NAME:-tidak dikenal}"

PKG_INSTALL=""; PKG_UPDATE=""; PKG_LIST="python3 python3-venv python3-pip git curl"
case " $DISTRO_ID $DISTRO_LIKE " in
    *fedora*|*rhel*|*centos*|*rocky*|*almalinux*|*amzn*)
        if command -v dnf >/dev/null 2>&1; then _pm=dnf; else _pm=yum; fi
        PKG_INSTALL="sudo $_pm install -y"; PKG_UPDATE="sudo $_pm -y makecache"
        PKG_LIST="python3 python3-pip git curl" ;;        # venv = stdlib di Fedora/RHEL
    *ubuntu*|*debian*|*linuxmint*|*"linux mint"*|*pop*|*elementary*|*kali*|*raspbian*|*neon*|*zorin*)
        PKG_INSTALL="sudo DEBIAN_FRONTEND=noninteractive apt-get install -y"
        PKG_UPDATE="sudo apt-get update -y" ;;
    *arch*|*manjaro*|*endeavouros*|*garuda*|*cachyos*)
        PKG_INSTALL="sudo pacman -S --noconfirm --needed"; PKG_UPDATE="sudo pacman -Sy"
        PKG_LIST="python python-pip git curl" ;;
    *suse*|*sles*)
        PKG_INSTALL="sudo zypper -n install"; PKG_UPDATE="sudo zypper -n refresh"
        PKG_LIST="python3 python3-pip git curl" ;;
    *alpine*)
        PKG_INSTALL="sudo apk add --no-cache"; PKG_UPDATE="sudo apk update"
        PKG_LIST="python3 py3-pip git curl" ;;
    *void*)
        PKG_INSTALL="sudo xbps-install -Sy"; PKG_UPDATE=""
        PKG_LIST="python3 python3-pip git curl" ;;
    *)
        warn "Distro tak dikenal — lewati install paket sistem. Pastikan python3/pip/git/curl ada." ;;
esac

need=""
for c in python3 git curl; do command -v "$c" >/dev/null 2>&1 || need="$need $c"; done
command -v python3 >/dev/null 2>&1 && { python3 -c 'import venv' 2>/dev/null || need="$need python3-venv"; }

if [ -n "$need" ] && [ -n "$PKG_INSTALL" ]; then
    say "Install dependency sistem:$need"
    [ -n "$PKG_UPDATE" ] && { eval "$PKG_UPDATE" >/dev/null 2>&1 || true; }
    # shellcheck disable=SC2086
    eval "$PKG_INSTALL $PKG_LIST" || die "Gagal install paket sistem."
    ok "Dependency sistem terpasang"
elif [ -n "$need" ]; then
    die "Belum ada dan tak ada package manager dikenali:$need"
else
    ok "python3 · pip · git · curl — sudah ada"
fi

# ── 2. clone / update ─────────────────────────────────────────────────────────
step "2/6  Ambil kode"
mkdir -p "$(dirname "$INSTALL_DIR")"
if [ -d "$INSTALL_DIR/.git" ]; then
    say "Update repo yang sudah ada"
    git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH" || die "git fetch gagal"
    git -C "$INSTALL_DIR" checkout --quiet "$BRANCH" 2>/dev/null || true
    git -C "$INSTALL_DIR" reset --hard --quiet "origin/$BRANCH"
    ok "Repo di-update ke origin/$BRANCH"
elif [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    die "$INSTALL_DIR sudah ada tapi bukan git repo. Pindahkan/hapus dulu."
else
    say "Clone $REPO_URL"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" || die "git clone gagal"
    ok "Repo ter-clone → $INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# ── 3. venv + deps ────────────────────────────────────────────────────────────
step "3/6  Virtualenv & dependency Python"
[ -x ".venv/bin/python" ] || { say "Buat .venv"; python3 -m venv .venv || die "venv gagal (python3-venv?)"; }
.venv/bin/python -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
.venv/bin/python -m pip install --quiet -r requirements.txt || die "pip install gagal"
ok "Deps OK ($(.venv/bin/python -c 'import httpx;print("httpx",httpx.__version__)'))"

# ── 4. WIZARD config ──────────────────────────────────────────────────────────
step "4/6  Setup bot (wizard)"
RECONFIG=1
if [ -f "config.json" ]; then
    if confirm "config.json sudah ada. Konfigurasi ulang?" "N"; then RECONFIG=1; else RECONFIG=0; fi
fi

if [ "$RECONFIG" = "1" ]; then
    printf '\n' >&2
    say "Butuh ${C_BLD}token bot Telegram${C_RST}. Cara dapat:"
    printf '   %s1.%s Buka %s@BotFather%s di Telegram\n' "$C_DIM" "$C_RST" "$C_BLU" "$C_RST" >&2
    printf '   %s2.%s Kirim %s/newbot%s → ikuti langkahnya → salin token-nya\n' "$C_DIM" "$C_RST" "$C_BLU" "$C_RST" >&2
    printf '      %s(token bentuknya: 123456789:AAH...xyz)%s\n\n' "$C_DIM" "$C_RST" >&2

    # ── token: loop sampai valid (validasi live ke Telegram) ──
    BOT_USER=""
    if [ -n "$CCTG_TOKEN" ]; then
        say "Validasi token dari env CCTG_TOKEN…"
        BOT_USER=$(tg_validate "$CCTG_TOKEN") || die "Token CCTG_TOKEN tidak valid (ditolak getMe)."
        TOKEN="$CCTG_TOKEN"
        ok "Token valid → bot: @$BOT_USER"
    else
        while :; do
            TOKEN=$(ask_secret "Tempel token bot")
            [ -z "$TOKEN" ] && { warn "Kosong. Coba lagi."; continue; }
            case "$TOKEN" in
                *:*) : ;;
                *) warn "Format salah (harus '<angka>:<huruf>'). Coba lagi."; continue ;;
            esac
            printf '%s  mengecek token ke Telegram…%s\r' "$C_DIM" "$C_RST" >&2
            if BOT_USER=$(tg_validate "$TOKEN"); then
                ok "Token valid → bot: ${C_BLD}@$BOT_USER${C_RST}"
                break
            else
                warn "Token ditolak Telegram (getMe gagal). Pastikan benar & coba lagi."
            fi
        done
    fi

    # ── owner ID: auto-detect atau manual ──
    OWNER=""
    if [ -n "$CCTG_OWNER" ]; then
        case "$CCTG_OWNER" in ''|*[!0-9]*) die "CCTG_OWNER harus angka." ;; esac
        OWNER="$CCTG_OWNER"; ok "Owner ID dari env: $OWNER"
    else
        printf '\n' >&2
        say "Sekarang tentukan ${C_BLD}owner${C_RST} (cuma ID ini yang boleh pakai bot)."
        if confirm "Auto-detect ID kamu? (aku tunggu kamu kirim pesan ke @$BOT_USER)" "Y"; then
            printf '   %s→ Buka %s@%s%s di Telegram, kirim pesan apa saja (mis. \"halo\")…%s\n' \
                   "$C_DIM" "$C_BLU" "$BOT_USER" "$C_DIM" "$C_RST" >&2
            if DET=$(tg_detect_owner "$TOKEN"); then
                OWNER=$(printf '%s' "$DET" | cut -d' ' -f1)
                ONAME=$(printf '%s' "$DET" | cut -d' ' -f2-)
                printf '\n' >&2
                ok "Kedeteksi: ${C_BLD}$ONAME${C_RST} (ID: $OWNER)"
                confirm "Pakai ID ini sebagai owner?" "Y" || OWNER=""
            else
                printf '\n' >&2
                warn "Timeout — tidak ada pesan masuk."
            fi
        fi
        # fallback manual
        while [ -z "$OWNER" ]; do
            printf '   %s(ID numerik kamu bisa dilihat dari %s@userinfobot%s)%s\n' \
                   "$C_DIM" "$C_BLU" "$C_DIM" "$C_RST" >&2
            OWNER=$(ask "Telegram user ID kamu" "")
            case "$OWNER" in ''|*[!0-9]*) warn "Harus angka."; OWNER="" ;; esac
        done
    fi

    # ── model slot ──
    MODEL="${CCTG_MODEL:-$(ask "Model slot default (opus/sonnet/haiku)" "opus")}"
    case "$MODEL" in opus|sonnet|haiku) : ;; *) warn "Slot '$MODEL' tak dikenal → pakai opus"; MODEL=opus ;; esac

    # ── workdir ──
    WDIR="${CCTG_WORKDIR:-$(ask "Working directory default Claude Code" "$HOME")}"

    # ── tulis config ──
    cat > config.json <<EOF
{
  "telegram_token": "$TOKEN",
  "owner_ids": [$OWNER],
  "default_provider": "claude",
  "model_slot": "$MODEL",
  "default_workdir": "$WDIR",
  "claude_timeout": 1800,
  "max_concurrent": 3,
  "auto_compact_ratio": 0
}
EOF
    chmod 600 config.json
    ok "config.json tersimpan (mode 600, token tidak ikut ke git)"
else
    ok "Pakai config.json yang ada"
fi

# providers.json minimal (provider tambahan ditambah lewat /provider di Telegram)
if [ ! -f "providers.json" ]; then
    printf '{"providers": {}}\n' > providers.json && chmod 600 providers.json
    ok "providers.json kosong dibuat"
fi

# ── 5. cek Claude CLI ─────────────────────────────────────────────────────────
step "5/6  Claude Code CLI"
if command -v claude >/dev/null 2>&1; then
    ok "claude: $(command -v claude)"
else
    warn "Binary 'claude' tidak ada di PATH. Bot WAJIB butuh Claude Code CLI."
    warn "Install + login: https://docs.claude.com/en/docs/claude-code"
    warn "Kalau sudah ada, pastikan masuk PATH (umumnya ~/.local/bin)."
fi

# ── 6. systemd (opsional) ─────────────────────────────────────────────────────
step "6/6  Service (auto-start)"
SETUP_SVC=0
if [ "$SKIP_SYSTEMD" != "1" ] && command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
    confirm "Pasang sebagai systemd service (jalan otomatis saat boot)?" "Y" && SETUP_SVC=1
fi

if [ "$SETUP_SVC" = "1" ]; then
    SVC=/etc/systemd/system/cc-tg.service
    sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$INSTALL_DIR|g" cc-tg.service.template \
        | sudo tee "$SVC" >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now cc-tg.service
    sleep 2
    if systemctl is-active --quiet cc-tg.service; then
        ok "Service aktif & jalan"
    else
        warn "Service belum aktif. Cek: journalctl -u cc-tg -n 50"
    fi
else
    ok "Tanpa systemd. Jalankan manual: $INSTALL_DIR/start.sh"
fi

# ── selesai ───────────────────────────────────────────────────────────────────
hr
printf '%s\n' "${C_GRN}${C_BLD}  ✓ Instalasi selesai!${C_RST}" >&2
printf '\n' >&2
[ -n "${BOT_USER:-}" ] && printf '   Bot      : %s@%s%s\n' "$C_BLU" "$BOT_USER" "$C_RST" >&2
printf '   Folder   : %s\n' "$INSTALL_DIR" >&2
printf '   Config   : %s/config.json %s(mode 600)%s\n' "$INSTALL_DIR" "$C_DIM" "$C_RST" >&2
if [ "$SETUP_SVC" = "1" ]; then
    printf '   Service  : %ssystemctl status cc-tg%s  ·  %sjournalctl -u cc-tg -f%s\n' \
           "$C_DIM" "$C_RST" "$C_DIM" "$C_RST" >&2
else
    printf '   Start    : %s%s/start.sh%s\n' "$C_DIM" "$INSTALL_DIR" "$C_RST" >&2
fi
printf '\n' >&2
printf '%s\n' "${C_BLD}Langkah berikutnya:${C_RST}" >&2
printf '   1. Buka chat bot %s%s\n' "${BOT_USER:+@}" "${BOT_USER:-Telegram-mu}" >&2
printf '   2. Ketik %s/start%s lalu %s/help%s\n' "$C_BLU" "$C_RST" "$C_BLU" "$C_RST" >&2
printf '   3. (opsional) %s/provider%s di Telegram untuk tambah provider lain\n' "$C_BLU" "$C_RST" >&2
hr
