#!/usr/bin/env bash
# claude-terminal — launcher menu untuk provider yang dikelola lewat BOT TELEGRAM.
# Sumber provider: ~/.cc-tg/providers.json (edit lewat /provider add di bot Telegram).
# Mirror dari claude-deep, tapi sumbernya bot — bukan Claude Hub.
#
# Pakai:
#   claude-terminal              -> menu pilih provider
#   claude-terminal omni         -> langsung provider 'omni' (skip menu)
#   claude-terminal omni -p "hi" -> headless ke 'omni'
#   claude-terminal 3            -> langsung provider nomor 3
set -uo pipefail

CFG="$HOME/.cc-tg/providers.json"
CLAUDE="$HOME/.local/bin/claude"

[ -f "$CFG" ] || { echo "providers.json bot tidak ada: $CFG" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq belum terinstall" >&2; exit 1; }
[ -x "$CLAUDE" ] || CLAUDE="$(command -v claude)"

# Daftar provider dari bot (urut), 'claude' native selalu di atas.
mapfile -t NAMES < <( { echo "claude"; jq -r '.providers | keys[]' "$CFG" 2>/dev/null | grep -v '^claude$'; } )

# Warna (kalau ke terminal)
if [ -t 1 ]; then
  C0=$'\e[0m'; CB=$'\e[1m'; CG=$'\e[32m'; CD=$'\e[2m'; CY=$'\e[33m'; CC=$'\e[36m'
else C0=; CB=; CG=; CD=; CY=; CC=; fi

# Argumen langsung? (nama provider atau nomor)
sel="${1:-}"
choice=""
if [ -n "$sel" ]; then
  if [[ "$sel" =~ ^[0-9]+$ ]]; then choice="$sel"; shift
  else
    # cari index dari nama
    for idx in "${!NAMES[@]}"; do
      [ "${NAMES[$idx]}" = "$sel" ] && { choice=$((idx+1)); shift; break; }
    done
    [ -z "$choice" ] && { echo "${CY}Provider '$sel' tidak ada di bot.${C0}" >&2; }
  fi
fi

if [ -z "$choice" ]; then
  echo
  echo "  ${CB}${CG}⚡ Claude Terminal${C0}  ${CD}— provider dari bot Telegram${C0}"
  echo "  ${CD}(kelola: /provider add di bot · file: ~/.cc-tg/providers.json)${C0}"
  echo "  ${CD}──────────────────────────────────────────────${C0}"
  i=1
  for name in "${NAMES[@]}"; do
    if [ "$name" = "claude" ]; then
      printf "  ${CB}${CY}%2d${C0})  ${CB}${CC}claude${C0}  ${CD}(native Anthropic)${C0}\n" "$i"
    else
      base=$(jq -r ".providers[\"$name\"].base_url // \"-\"" "$CFG")
      opus=$(jq -r ".providers[\"$name\"].opus // \"-\"" "$CFG")
      sonnet=$(jq -r ".providers[\"$name\"].sonnet // \"-\"" "$CFG")
      haiku=$(jq -r ".providers[\"$name\"].haiku // \"-\"" "$CFG")
      printf "  ${CB}${CY}%2d${C0})  ${CB}${CC}%s${C0}\n" "$i" "$name"
      printf "       ${CD}%s${C0}\n" "$base"
      printf "       ${CD}opus=${C0}%s  ${CD}sonnet=${C0}%s  ${CD}haiku=${C0}%s\n" "$opus" "$sonnet" "$haiku"
    fi
    i=$((i+1))
  done
  echo "  ${CD}──────────────────────────────────────────────${C0}"
  printf "  ${CD} q)  keluar${C0}\n\n"
  printf "  Pilih [1-%d]: " "${#NAMES[@]}"
  read -r choice || exit 0
fi

case "$choice" in q|Q) echo "bye 👋"; exit 0;; esac
if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#NAMES[@]}" ]; then
  echo "${CY}Pilihan tidak valid: '$choice'${C0}" >&2; exit 1
fi

NAME="${NAMES[$((choice-1))]}"

# Bersihkan env provider lama biar tak bocor antar-run
unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_DEFAULT_OPUS_MODEL \
      ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL \
      ANTHROPIC_SMALL_FAST_MODEL ANTHROPIC_MODEL

if [ "$NAME" = "claude" ]; then
  echo "${CG}→ claude (native Anthropic)${C0}"
else
  # Inject env PERSIS seperti _provider_env bot — nama model ASLI, tanpa translate.
  base=$(jq -r ".providers[\"$NAME\"].base_url // empty" "$CFG")
  tok=$(jq -r ".providers[\"$NAME\"].token // empty" "$CFG")
  opus=$(jq -r ".providers[\"$NAME\"].opus // empty" "$CFG")
  sonnet=$(jq -r ".providers[\"$NAME\"].sonnet // empty" "$CFG")
  haiku=$(jq -r ".providers[\"$NAME\"].haiku // empty" "$CFG")
  [ -n "$base" ] && export ANTHROPIC_BASE_URL="$base"
  [ -n "$tok" ]  && export ANTHROPIC_AUTH_TOKEN="$tok"
  [ -n "$opus" ]   && export ANTHROPIC_DEFAULT_OPUS_MODEL="$opus"
  [ -n "$sonnet" ] && export ANTHROPIC_DEFAULT_SONNET_MODEL="$sonnet"
  [ -n "$haiku" ]  && { export ANTHROPIC_DEFAULT_HAIKU_MODEL="$haiku"; export ANTHROPIC_SMALL_FAST_MODEL="$haiku"; }
  echo "${CG}→ claude-${NAME}${C0} ${CD}(${base})${C0}"
fi

exec "$CLAUDE" --dangerously-skip-permissions "$@"
