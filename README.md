<div align="center">

# 🤖 Claude Code Telegram Bot

**Control [Claude Code](https://docs.claude.com/en/docs/claude-code) from Telegram — fully self-hosted.**
**Kendalikan [Claude Code](https://docs.claude.com/en/docs/claude-code) dari Telegram — sepenuhnya self-hosted.**

Chat from your phone, Claude Code does the work on *your own* machine — coding, reading/editing files, running commands.
Chat dari HP, Claude Code yang mengerjakan di mesin *milikmu sendiri* — coding, baca/edit file, jalankan perintah.

[![CI](https://github.com/zesbe/claude-code-telegram-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/zesbe/claude-code-telegram-bot/actions/workflows/ci.yml)
[![self-hosted](https://img.shields.io/badge/100%25-self--hosted-22c55e)](#-privacy--security--privasi--keamanan)
[![no telemetry](https://img.shields.io/badge/telemetry-none-22c55e)](#-privacy--security--privasi--keamanan)
[![install](https://img.shields.io/badge/install-one--liner-3b82f6?logo=gnubash&logoColor=white)](#-install--instalasi)
[![python](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)](#-requirements--prasyarat)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

*🇬🇧 English & 🇮🇩 Bahasa Indonesia below*

</div>

---

## 🔐 Privacy & Security · Privasi & Keamanan

> **🇬🇧 This bot is 100% self-hosted. It does NOT connect to any author/third-party server.**
> Your bot token, your API keys, and your conversations live **only on your own machine**.
> There is no backend, no telemetry, no phone-home, no analytics, no hidden relay.
>
> **🇮🇩 Bot ini 100% self-hosted. TIDAK terhubung ke server pembuat/pihak ketiga mana pun.**
> Token bot, API key, dan percakapanmu **hanya tersimpan di mesinmu sendiri**.
> Tidak ada backend, tidak ada telemetry, tidak ada phone-home, tidak ada analytics, tidak ada relay tersembunyi.

**Where your data goes · Ke mana data kamu pergi:**

```
Your phone (Telegram)  ─┐
                        ├─►  api.telegram.org   (official Telegram — to deliver messages)
Your machine (this bot) ─┘
       │
       └─►  Anthropic API / your chosen provider   (only the LLM you configure)

         ✗ NO author server      ✗ NO telemetry server
         ✗ NO analytics           ✗ NO data collection
```

| 🇬🇧 English | 🇮🇩 Bahasa Indonesia |
|---|---|
| **Single-file, auditable** — the whole bot is one readable `cc_tg.py`. Read it yourself. | **Satu file, bisa diaudit** — seluruh bot ada di satu `cc_tg.py` yang mudah dibaca. Cek sendiri. |
| **No backend owned by anyone** — it talks only to Telegram's API + the LLM provider *you* configure. | **Tanpa backend milik siapa pun** — hanya bicara ke API Telegram + provider LLM yang *kamu* atur. |
| **Secrets stay local** — `config.json` & `providers.json` are `chmod 600` + gitignored. Never pushed, never sent anywhere. | **Rahasia tetap lokal** — `config.json` & `providers.json` di-`chmod 600` + gitignore. Tak pernah ke-push, tak dikirim ke mana pun. |
| **Owner-only** — only Telegram IDs in `owner_ids` can use it; everyone else is blocked. | **Hanya owner** — cuma ID Telegram di `owner_ids` yang bisa pakai; selain itu diblokir. |
| **Token redaction** — bot tokens are auto-stripped from logs. | **Redaksi token** — token bot otomatis dihapus dari log. |
| **No build step, no obfuscation** — pure Python, nothing compiled or hidden. | **Tanpa build, tanpa obfuscation** — Python murni, tak ada yang dikompilasi atau disembunyikan. |

> [!WARNING]
> **🇬🇧** This bot runs Claude Code with `--dangerously-skip-permissions`. Anyone in `owner_ids` gets **full shell access** to the host. Keep your bot token & owner ID private, and only install on a machine you control.
> **🇮🇩** Bot menjalankan Claude Code dengan `--dangerously-skip-permissions`. Siapa pun di `owner_ids` mendapat **akses penuh ke shell** host. Jaga token bot & owner ID, dan hanya pasang di mesin yang kamu kontrol.

---

## ✨ What is this? · Apa ini?

**🇬🇧** A Telegram bot wrapping the **Claude Code CLI**. You send a normal message, the bot runs `claude` on your server and streams the answer back **live (token-by-token)** — like typing in a terminal, but from anywhere via Telegram.

**🇮🇩** Bot Telegram yang membungkus **Claude Code CLI**. Kamu kirim pesan biasa, bot menjalankan `claude` di servermu dan mengirim balik jawabannya **secara live (token-by-token)** — seperti mengetik di terminal, tapi dari mana saja lewat Telegram.

```
Telegram (you/kamu)                Your machine / Mesinmu
─────────────────                  ──────────────────────
"refactor login()"       ───────▶  claude -p "refactor..."
                                        │ read files, edit, test
   🔄 live streaming  ◀────────────────┤ (tool calls shown live)
   ✅ answer + diff    ◀───────────────┘
```

---

## 🎯 Features · Fitur

| | 🇬🇧 | 🇮🇩 |
|---|---|---|
| ⚡ **Live streaming** | Answers appear token-by-token (Hermes-style) | Jawaban muncul token-by-token (gaya Hermes) |
| 🔌 **Multi-provider** | Native `claude` + Anthropic-compatible endpoints (z.ai/GLM, DeepSeek…). Switch via `/provider` | `claude` native + endpoint Anthropic-compatible. Ganti via `/provider` |
| 🪟 **Per-topic isolation** | Each forum topic = its **own** session/provider/model | Tiap forum topic = sesi/provider/model **sendiri** |
| 💬 **Persistent sessions** | `/resume` to browse, switch, rename, delete | `/resume` untuk lihat, pindah, rename, hapus |
| 🔧 **Tool visibility** | Tool calls + results shown live in the bubble | Tool yang dipanggil + hasilnya tampil live |
| ☑️ **PICK / MULTIPICK** | Claude can ask you to choose via inline buttons | Claude bisa minta pilih lewat tombol inline |
| ⏹ **Stop anytime** | Stop button + `/stop` — kills the whole process tree | Tombol Stop + `/stop` — kill seluruh process tree |
| 🗜️ **Auto-compact** | Summarize long context → fresh session (optional) | Ringkas konteks panjang → sesi baru (opsional) |
| 📊 **Clean markdown** | Headings, bullets, tables→boxes, code blocks | Heading, bullet, tabel→box, code block |
| ⏰ **Cron** | Schedule recurring tasks | Jadwalkan tugas berkala |
| 🌙 **Background & queue** | Run in parallel windows, or queue sequentially | Jalan paralel, atau antri berurutan |

---

## 🚀 Install · Instalasi

**🇬🇧 One command** — interactive wizard guides you from zero.
**🇮🇩 Satu perintah** — wizard interaktif menuntun dari nol.

```bash
curl -fsSL https://raw.githubusercontent.com/zesbe/claude-code-telegram-bot/main/install.sh | bash
```

**🇬🇧 The wizard will:** detect your distro & install deps → clone + venv → ask for your bot token (**validated live** against Telegram) → **auto-detect your owner ID** (just message the bot) → pick model + workdir → optional systemd service.

**🇮🇩 Wizard akan:** deteksi distro & install dependency → clone + venv → minta token bot (**divalidasi live** ke Telegram) → **auto-detect owner ID** (cukup kirim pesan ke bot) → pilih model + workdir → opsi systemd service.

<details>
<summary><b>📸 Wizard preview · Tampilan wizard</b></summary>

```
   ____ ____      _____ ____
  / ___/ ___|    |_   _/ ___|   Claude Code  ·  Telegram
 | |  | |   _____  | || |  _    ───────────────────────────
 | |__| |__|_____| | || |_| |   chat → Claude Code does it
  \____\____|      |_| \____|

1/6  System detection
✓ OS: Fedora Linux 44 (Workstation Edition)
✓ python3 · pip · git · curl — present
...
4/6  Bot setup (wizard)
▶ Paste bot token: ••••••••••••
  checking token with Telegram…
✓ Token valid → bot: @your_bot
▶ Auto-detect your ID? (I'll wait for you to message @your_bot) [Y/n]
✓ Detected: Yudi (ID: 11876...)
✓ config.json saved (mode 600)
```
</details>

### Supported distros · Distro yang didukung

`Fedora` · `RHEL` · `Rocky` · `AlmaLinux` · `Ubuntu` · `Debian` · `Linux Mint`
· `Pop!_OS` · `elementary` · `Zorin` · `KDE neon` · `Kali` · `Raspberry Pi OS`
· `Arch` · `Manjaro` · `EndeavourOS` · `Garuda` · `CachyOS` · `openSUSE` · `Alpine` · `Void`

### Manual install · Instalasi manual

```bash
git clone https://github.com/zesbe/claude-code-telegram-bot.git ~/.cc-tg
cd ~/.cc-tg
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json && chmod 600 config.json
$EDITOR config.json        # set telegram_token & owner_ids
./start.sh
```

### Non-interactive · Non-interaktif (CI / automation)

```bash
CCTG_TOKEN='123:abc' CCTG_OWNER='111' ASSUME_YES=1 SKIP_SYSTEMD=1 \
  bash <(curl -fsSL https://raw.githubusercontent.com/zesbe/claude-code-telegram-bot/main/install.sh)
```

---

## 📋 Requirements · Prasyarat

- **Python 3.10+**
- **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)** installed & logged in (`claude` in PATH) · terinstall & login
- **Telegram bot** via [@BotFather](https://t.me/BotFather) (`/newbot`)
- 🇬🇧 The wizard handles the rest (owner ID auto-detected). · 🇮🇩 Wizard mengurus sisanya.

---

## ⚙️ Configuration · Konfigurasi

`config.json`:

| Field | Req | Default | 🇬🇧 / 🇮🇩 |
|---|:---:|---|---|
| `telegram_token` | ✅ | — | Bot token from BotFather · Token bot dari BotFather |
| `owner_ids` | ✅ | — | Telegram user IDs allowed to use the bot · ID yang boleh pakai bot |
| `default_provider` | | `claude` | Default provider · Provider default |
| `model_slot` | | `opus` | `opus` / `sonnet` / `haiku` |
| `default_workdir` | | `$HOME` | Claude Code working dir · Folder kerja |
| `claude_timeout` | | `1800` | Per-message timeout (s) · Timeout per pesan (detik) |
| `max_concurrent` | | `3` | Max parallel Claude Code · Maks paralel |
| `auto_compact_ratio` | | `0` | `0` = off; `0.85` = compact at ≥85% context |

<details>
<summary><b>Extra providers · Provider tambahan (<code>providers.json</code>)</b></summary>

🇬🇧 Optional — you can also add them via `/provider add` in Telegram.
🇮🇩 Opsional — bisa juga lewat `/provider add` di Telegram.

```json
{
  "providers": {
    "zai": {
      "base_url": "https://api.z.ai/api/anthropic",
      "token": "sk-...",
      "opus": "glm-5.2",
      "sonnet": "glm-4.7",
      "haiku": "glm-4.5-air"
    }
  }
}
```
🇬🇧 The bot injects `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/model env directly into the native `claude` binary — no proxy.
🇮🇩 Bot meng-inject env langsung ke binary `claude` native — tanpa proxy.
</details>

---

## 💬 Commands · Perintah

| Command | 🇬🇧 / 🇮🇩 |
|---|---|
| `/start` · `/help` | Start / full guide · Mulai / panduan lengkap |
| `/resume` | Sessions list (resume · rename · delete · clean empty) · Daftar sesi |
| `/new [name]` | New session in active window · Sesi baru |
| `/provider [name]` | Switch / manage provider (per-window) · Ganti provider |
| `/model opus\|sonnet\|haiku` | Switch model slot (per-window) · Ganti model |
| `/effort low\|…\|max` | Reasoning depth · Kedalaman reasoning |
| `/verbose` | Toggle live "thinking" · Tampil thinking live |
| `/stop` | Stop running task + release lock · Hentikan task |
| `/status` | Active window info · Info window aktif |
| `/compact` | Summarize context → fresh session · Ringkas konteks |
| `/queue` · `/background` | Queue / parallel windows · Antri / paralel |
| `/cd <path>` · `/pwd` | Change / show workdir · Ganti / lihat workdir |
| `/cron add HH:MM <prompt>` | Schedule daily task · Jadwalkan tugas harian |
| `/usage` | Token & cost usage · Pemakaian token & biaya |
| `/restart` | Restart bot |

🇬🇧 Full list: type `/help` in Telegram. · 🇮🇩 Lengkapnya: ketik `/help` di Telegram.

---

## 🪟 Forum topics (groups) · (grup)

🇬🇧 Enable **Topics** in your Telegram group settings. Each topic gets its own window (`topic-<id>`) with an **isolated session + provider + model** — great for separating projects in one group.

🇮🇩 Aktifkan **Topics** di setting grup Telegram. Tiap topic dapat window sendiri (`topic-<id>`) dengan **sesi + provider + model terisolasi** — cocok memisahkan proyek dalam satu grup.

---

## 🔧 Operations · Operasional

### systemd service

🇬🇧 The installer offers this automatically. Manual: · 🇮🇩 Installer menawarkan otomatis. Manual:

```bash
sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$HOME/.cc-tg|g" \
    cc-tg.service.template | sudo tee /etc/systemd/system/cc-tg.service
sudo systemctl daemon-reload && sudo systemctl enable --now cc-tg.service
journalctl -u cc-tg -f          # live logs · log live
```

### Update

```bash
cd ~/.cc-tg && git pull && .venv/bin/pip install -r requirements.txt
sudo systemctl restart cc-tg    # if using systemd · kalau pakai systemd
```

🇬🇧 Or re-run `install.sh` (idempotent — your config is kept).
🇮🇩 Atau jalankan ulang `install.sh` (idempotent — config kamu tidak ditimpa).

---

## 🩺 Troubleshooting

<details>
<summary><b>Bot doesn't reply / <code>409 Conflict</code> in logs · Bot tidak membalas</b></summary>

🇬🇧 Telegram allows only **one** poller per token. `409 Conflict` = two bot instances running with the same token (e.g. laptop **and** server). Kill one.
🇮🇩 Telegram hanya mengizinkan **satu** poller per token. `409 Conflict` = dua instance jalan dengan token sama. Matikan salah satu.
```bash
pkill -f cc_tg.py          # or: sudo systemctl stop cc-tg
```
</details>

<details>
<summary><b><code>Binary 'claude' not in PATH</code></b></summary>

🇬🇧 The bot needs Claude Code CLI. Install + login from the [official docs](https://docs.claude.com/en/docs/claude-code); ensure `claude` is in PATH (usually `~/.local/bin`). Check: `command -v claude`.
🇮🇩 Bot butuh Claude Code CLI. Install + login dari [docs resmi](https://docs.claude.com/en/docs/claude-code); pastikan `claude` ada di PATH. Cek: `command -v claude`.
</details>

<details>
<summary><b>Slow response / Claude "digs through the repo" · Respons lama</b></summary>

🇬🇧 If `default_workdir` has a `CLAUDE.md` telling Claude to explore first, every message becomes agentic. For quick chat, set workdir to a neutral folder. Use `/stop` to cancel long tasks.
🇮🇩 Kalau `default_workdir` punya `CLAUDE.md` yang menyuruh eksplorasi, tiap pesan jadi agentic. Untuk chat cepat, set workdir ke folder netral. Pakai `/stop` untuk membatalkan.
</details>

---

## 📁 Project structure · Struktur

```
cc_tg.py                  # the bot (single file) · bot (satu file)
install.sh                # interactive installer/wizard
start.sh                  # launcher (uses .venv)
requirements.txt          # httpx, telegramify-markdown
cc-tg.service.template    # systemd template (__USER__/__DIR__ placeholders)
config.example.json       # config template
providers.example.json    # provider template
send_to_telegram.sh       # helper: Claude sends a file to the chat
```

---

## 🤝 Contributing · Kontribusi

🇬🇧 Issues & PRs welcome. CI runs `py_compile`, ShellCheck, and JSON validation on every push.
🇮🇩 Issue & PR dipersilakan. CI menjalankan `py_compile`, ShellCheck, dan validasi JSON tiap push.

---

## 📜 License · Lisensi

[MIT](LICENSE) © zesbe

<div align="center">
<sub>🇬🇧 Built on <a href="https://docs.claude.com/en/docs/claude-code">Claude Code</a>. Not an official Anthropic product. Self-hosted, no affiliation, no data collection.</sub><br>
<sub>🇮🇩 Dibangun di atas <a href="https://docs.claude.com/en/docs/claude-code">Claude Code</a>. Bukan produk resmi Anthropic. Self-hosted, tanpa afiliasi, tanpa pengumpulan data.</sub>

<br><br>
<b>⭐ If this is useful, star the repo — it helps others find it.</b><br>
<b>⭐ Kalau berguna, kasih star — biar makin banyak yang nemu.</b>

</div>
