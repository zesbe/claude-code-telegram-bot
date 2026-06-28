<div align="center">

# 🤖 Claude Code Telegram Bot

**Kendalikan [Claude Code](https://docs.claude.com/en/docs/claude-code) dari Telegram.**
Chat dari HP, Claude Code yang ngerjain di server kamu — coding, baca/edit file, jalanin command, semuanya.

[![CI](https://github.com/zesbe/claude-code-telegram-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/zesbe/claude-code-telegram-bot/actions/workflows/ci.yml)
[![shell](https://img.shields.io/badge/install-one--liner-22c55e?logo=gnubash&logoColor=white)](#-instalasi)
[![python](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)](#prasyarat)
[![platform](https://img.shields.io/badge/linux-Fedora%20·%20Ubuntu%20·%20Mint%20·%20Arch%20·%20openSUSE-orange?logo=linux&logoColor=white)](#-instalasi)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

---

## ✨ Apa ini?

Bot Telegram yang membungkus **Claude Code CLI**. Kamu kirim pesan biasa, bot menjalankan `claude` di mesin server dan mengirim balik jawabannya **secara live (token-by-token)** — persis seperti ngetik di terminal, tapi dari mana saja lewat Telegram.

```
Kamu (Telegram)                    Server kamu
─────────────────                  ──────────────────────
"refactor fungsi login"  ───────▶  claude -p "refactor..."
                                        │ baca file, edit, test
   🔄 live streaming  ◀────────────────┤ (tool calls tampil live)
   ✅ jawaban + diff   ◀───────────────┘
```

> [!WARNING]
> Bot menjalankan Claude Code dengan `--dangerously-skip-permissions`. Siapa pun
> di `owner_ids` punya **akses penuh ke shell server** (baca file, run command,
> edit kode). **Jangan** bagikan token bot atau owner ID kamu. Hanya pasang di
> mesin yang kamu kontrol.

---

## 🎯 Fitur

| | |
|---|---|
| ⚡ **Live streaming** | Jawaban muncul token-by-token (gaya Hermes), bukan nunggu selesai |
| 🔌 **Multi-provider** | `claude` native + endpoint Anthropic-compatible (z.ai/GLM, DeepSeek, dll). Switch via `/provider` |
| 🪟 **Isolasi per-topic** | Tiap forum topic = sesi + provider + model **sendiri**, tak saling ganggu |
| 💬 **Sesi persisten** | `/resume` lihat history, lompat antar sesi, rename, hapus |
| 🔧 **Tool visibility** | Tool yang dipanggil Claude + hasilnya tampil live di bubble |
| ☑️ **PICK / MULTIPICK** | Claude bisa minta kamu pilih (single/multi) lewat tombol inline |
| ⏹ **Stop kapan saja** | Tombol Stop + `/stop` — kill seluruh process tree |
| 🗜️ **Auto-compact** | Ringkas konteks panjang → sesi baru (opsional, hemat token) |
| 📊 **Markdown rapi** | Heading→bold, bullet `•`, tabel→box monospace, code block utuh |
| ⏰ **Cron** | Jadwalkan tugas otomatis (`/cron add 07:00 cek server`) |
| 🌙 **Background & queue** | Jalankan paralel di window terpisah, atau antri berurutan |

---

## 🚀 Instalasi

**Satu perintah** — installer interaktif (wizard) yang nuntun dari nol:

```bash
curl -fsSL https://raw.githubusercontent.com/zesbe/claude-code-telegram-bot/main/install.sh | bash
```

Wizard akan:
1. 🔍 **Deteksi distro** & install dependency (python3, venv, pip, git, curl)
2. 📥 **Clone** repo + buat virtualenv + install deps
3. 🔑 **Minta token bot** → divalidasi **live** ke Telegram (nunjukin `@username` bot kamu)
4. 🪪 **Auto-detect owner ID** — kamu cukup kirim pesan ke bot, ID-mu kedeteksi otomatis
5. ⚙️ Pilih model slot + working directory
6. 🔧 (opsional) Pasang sebagai **systemd service** (auto-start saat boot)

<details>
<summary><b>📸 Tampilan wizard</b></summary>

```
   ____ ____      _____ ____
  / ___/ ___|    |_   _/ ___|   Claude Code  ·  Telegram
 | |  | |   _____  | || |  _    ───────────────────────────
 | |__| |__|_____| | || |_| |   chat → Claude Code ngerjain
  \____\____|      |_| \____|

1/6  Deteksi sistem
✓ OS: Fedora Linux 44 (Workstation Edition)
✓ python3 · pip · git · curl — sudah ada
...
4/6  Setup bot (wizard)
▶ Tempel token bot: ••••••••••••
  mengecek token ke Telegram…
✓ Token valid → bot: @namabot_kamu
▶ Auto-detect ID kamu? (aku tunggu kamu kirim pesan ke @namabot_kamu) [Y/n]
✓ Kedeteksi: Yudi (ID: 11876...)
✓ config.json tersimpan (mode 600)
```
</details>

### Distro yang didukung

`Fedora` · `RHEL` · `Rocky` · `AlmaLinux` · `Ubuntu` · `Debian` · `Linux Mint`
· `Pop!_OS` · `elementary` · `Zorin` · `KDE neon` · `Kali` · `Raspberry Pi OS`
· `Arch` · `Manjaro` · `EndeavourOS` · `Garuda` · `CachyOS` · `openSUSE` · `Alpine` · `Void`

### Instalasi manual

```bash
git clone https://github.com/zesbe/claude-code-telegram-bot.git ~/.cc-tg
cd ~/.cc-tg
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json && chmod 600 config.json
$EDITOR config.json        # isi telegram_token & owner_ids
./start.sh
```

### Instalasi non-interaktif (CI / otomatis)

```bash
CCTG_TOKEN='123:abc' CCTG_OWNER='111' ASSUME_YES=1 SKIP_SYSTEMD=1 \
  bash <(curl -fsSL https://raw.githubusercontent.com/zesbe/claude-code-telegram-bot/main/install.sh)
```

---

## Prasyarat

- **Python 3.10+**
- **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)** terinstall & sudah login (`claude` ada di PATH)
- **Bot Telegram** — bikin lewat [@BotFather](https://t.me/BotFather) (`/newbot`)
- Wizard mengurus sisanya (owner ID dideteksi otomatis)

---

## ⚙️ Konfigurasi

`config.json`:

| Field | Wajib | Default | Deskripsi |
|---|:---:|---|---|
| `telegram_token` | ✅ | — | Token bot dari BotFather |
| `owner_ids` | ✅ | — | Array Telegram user ID yang boleh pakai bot |
| `default_provider` | | `claude` | Provider default (`claude` / nama di `providers.json`) |
| `model_slot` | | `opus` | `opus` / `sonnet` / `haiku` |
| `default_workdir` | | `$HOME` | Folder kerja Claude Code |
| `claude_timeout` | | `1800` | Timeout per pesan (detik) |
| `max_concurrent` | | `3` | Maks Claude Code jalan paralel |
| `auto_compact_ratio` | | `0` | `0` = off. `0.85` = auto-compact saat konteks ≥85% window |

<details>
<summary><b>Provider tambahan (<code>providers.json</code>)</b> — opsional, bisa juga lewat <code>/provider add</code> di Telegram</summary>

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
Bot meng-inject `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/model env langsung
ke binary `claude` native — tanpa proxy.
</details>

---

## 💬 Command

| Command | Fungsi |
|---|---|
| `/start` · `/help` | Mulai / panduan lengkap |
| `/resume` | Daftar sesi (lanjut · ✏️ rename · 🗑️ hapus · 🧹 bersihkan kosong) |
| `/new [nama]` | Sesi baru di window aktif |
| `/provider [nama]` | Switch / kelola provider (per-window) |
| `/model opus\|sonnet\|haiku` | Switch model slot (per-window) |
| `/effort low\|medium\|high\|xhigh\|max` | Kedalaman reasoning |
| `/verbose` | Toggle tampil "thinking" Claude secara live |
| `/stop` | Hentikan task berjalan + lepas lock |
| `/status` | Window aktif: provider, model, sesi |
| `/compact` | Ringkas konteks → sesi baru (hemat token) |
| `/queue` · `/background` | Antri berurutan / jalan paralel di window terpisah |
| `/cd <path>` · `/pwd` | Ganti / lihat working directory |
| `/cron add HH:MM <prompt>` | Jadwalkan tugas otomatis harian |
| `/usage` | Pemakaian token & biaya |
| `/restart` | Restart bot |

Lengkapnya: ketik `/help` di dalam Telegram.

---

## 🪟 Forum topics (grup)

Aktifkan **Topics** di setting grup Telegram. Tiap topic otomatis dapat window
sendiri (`topic-<id>`) dengan **sesi + provider + model terisolasi** — cocok buat
misahin proyek/konteks berbeda dalam satu grup.

---

## 🔧 Operasional

### Service mode (systemd)

Installer otomatis menawarkan ini. Manual:

```bash
sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$HOME/.cc-tg|g" \
    cc-tg.service.template | sudo tee /etc/systemd/system/cc-tg.service
sudo systemctl daemon-reload && sudo systemctl enable --now cc-tg.service
journalctl -u cc-tg -f          # lihat log live
```

### Update

```bash
cd ~/.cc-tg && git pull && .venv/bin/pip install -r requirements.txt
sudo systemctl restart cc-tg    # kalau pakai systemd
```

Atau jalankan ulang `install.sh` (idempotent — config kamu tidak ditimpa).

---

## 🩺 Troubleshooting

<details>
<summary><b>Bot tidak membalas / <code>409 Conflict</code> di log</b></summary>

Telegram hanya mengizinkan **satu** poller per token. `409 Conflict` = ada 2
instance bot jalan dengan token sama (mis. di laptop **dan** server sekaligus).
Matikan salah satu:
```bash
pkill -f cc_tg.py          # atau: sudo systemctl stop cc-tg
```
</details>

<details>
<summary><b><code>Binary 'claude' tidak ada di PATH</code></b></summary>

Bot butuh Claude Code CLI. Install + login dari
[docs resmi](https://docs.claude.com/en/docs/claude-code), pastikan `claude`
ada di PATH (umumnya `~/.local/bin`). Cek: `command -v claude`.
</details>

<details>
<summary><b>Respons lama / Claude malah "ngubek repo"</b></summary>

Kalau `default_workdir` punya `CLAUDE.md` yang nyuruh eksplorasi ("pahami repo
dulu"), tiap pesan jadi agentic. Untuk chat cepat, set workdir ke folder netral,
atau tambahkan instruksi singkat di system prompt. Pakai `/stop` untuk
menghentikan task yang kelamaan.
</details>

<details>
<summary><b>Ganti / hapus sesi</b></summary>

`/resume` → tombol per sesi: klik untuk lanjut, ✏️ rename, 🗑️ hapus,
🧹 bersihkan sesi kosong.
</details>

---

## 🔒 Keamanan

- `config.json` & `providers.json` di-`chmod 600` dan **di-gitignore** — token tak pernah ikut ke repo.
- Hanya `owner_ids` yang bisa pakai bot; selain itu diblokir.
- Token Telegram diredaksi otomatis dari log.
- Provider token di-inject ke subprocess env, tak ditulis ke disk selain `providers.json`.

> Jalankan sebagai user biasa (bukan root). Bot hanya seaman mesin tempat ia berjalan.

---

## 📁 Struktur

```
cc_tg.py                  # bot (single-file)
install.sh                # installer/wizard interaktif
start.sh                  # launcher (pakai .venv)
requirements.txt          # httpx, telegramify-markdown
cc-tg.service.template    # template systemd (placeholder __USER__/__DIR__)
config.example.json       # template config
providers.example.json    # template provider
send_to_telegram.sh       # helper: Claude kirim file ke chat
```

---

## 📜 Lisensi

[MIT](LICENSE) © zesbe

<div align="center">
<sub>Dibangun di atas <a href="https://docs.claude.com/en/docs/claude-code">Claude Code</a>. Bukan produk resmi Anthropic.</sub>
</div>
