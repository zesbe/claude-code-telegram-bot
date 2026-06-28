# claude-code-telegram-bot

Telegram wrapper untuk [Claude Code CLI](https://docs.claude.com/en/docs/claude-code).
Kamu chat di Telegram, Claude Code yang ngerjain — punya akses penuh ke
shell, file, dan tools-nya di mesin server. Cocok dipakai sebagai
remote-control coding assistant.

> ⚠️ **Bot ini menjalankan Claude Code dengan `--dangerously-skip-permissions`.**
> Siapa pun yang masuk `owner_ids` bisa minta apa pun di mesin host —
> baca file, run command, edit kode. Jangan share token atau ID-mu.

## Fitur

- **Live streaming** token-by-token (gaya Hermes) — jawaban muncul real-time.
- **Multi-provider** — `claude` native + provider Anthropic-compatible
  (z.ai/GLM, DeepSeek, dll). Switch per-window/topic via `/provider`.
- **Window per forum topic** — tiap topic = sesi + provider + model
  sendiri, sepenuhnya terisolasi.
- **Sesi persisten** — `/resume` lihat history, lompat antar sesi,
  rename, hapus, bersihkan kosong.
- **Tools tampil live** — tool yang dipanggil + hasilnya muncul di bubble.
- **PICK / MULTIPICK** — Claude bisa minta kamu pilih (single/multi)
  lewat tombol inline.
- **PIC button stop**, `/stop`, kill process tree.
- **Compact / reseed** otomatis (opsional, default off).
- **Markdown rapi** — heading→bold, bullet `•`, tabel→box monospace.

## Quick install

```bash
curl -fsSL https://raw.githubusercontent.com/zesbe/claude-code-telegram-bot/main/install.sh | bash
```

Script auto-detect distro (Fedora/RHEL, Ubuntu/Debian/Mint, Arch/Manjaro,
openSUSE, Alpine, Void), install python+venv+pip deps, prompt token Telegram
+ owner ID, dan opsi setup systemd. Idempotent — aman dijalankan ulang
untuk update.

### Manual install

```bash
git clone https://github.com/zesbe/claude-code-telegram-bot.git ~/.cc-tg
cd ~/.cc-tg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config.example.json config.json
chmod 600 config.json
# Edit config.json — isi telegram_token & owner_ids
$EDITOR config.json

./start.sh
```

## Prasyarat

- Python 3.10+
- [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) terinstall
  & login (`claude` di PATH)
- Bot Telegram (bikin di [@BotFather](https://t.me/BotFather))
- Telegram user ID kamu (cek di [@userinfobot](https://t.me/userinfobot))

## Konfigurasi

`config.json`:

| Field | Tipe | Wajib | Deskripsi |
|---|---|---|---|
| `telegram_token` | str | ✅ | Token bot dari BotFather |
| `owner_ids` | int[] | ✅ | Telegram user ID yang boleh pakai bot |
| `default_provider` | str | | Default provider (`claude` / nama di `providers.json`) |
| `model_slot` | str | | `opus` / `sonnet` / `haiku` |
| `claude_timeout` | int | | Timeout per pesan (detik), default 1800 |
| `max_concurrent` | int | | Maks Claude Code paralel, default 3 |
| `auto_compact_ratio` | float | | 0 = off. 0.85 = compact saat konteks ≥85% window |

`providers.json` (opsional, tambah lewat `/provider add` di Telegram juga
bisa):

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

## Command utama

| Command | Fungsi |
|---|---|
| `/start`, `/help` | Mulai / bantuan |
| `/resume` | Daftar sesi (lanjut · ✏️ rename · 🗑️ hapus · 🧹 bersihkan kosong) |
| `/new [nama]` | Sesi baru di window aktif |
| `/provider [nama]` | Switch / kelola provider (per-window) |
| `/model opus\|sonnet\|haiku` | Switch model slot (per-window) |
| `/effort low\|medium\|high\|xhigh\|max` | Kedalaman reasoning |
| `/verbose` | Toggle tampil "thinking" live |
| `/stop` | Hentikan task berjalan + lepas lock |
| `/status` | Window aktif: provider, model, sesi |
| `/compact` | Ringkas konteks → sesi baru |
| `/cd <path>`, `/pwd` | Ganti working directory |
| `/w <nama>` | Switch / buat window manual |
| `/cron add HH:MM <prompt>` | Jadwal otomatis |

Lengkapnya di `/help` dalam Telegram.

## Forum topics (grup)

Aktifkan **Topics** di setting grup Telegram. Tiap topic otomatis dapat
window sendiri (`topic-<thread_id>`) dengan sesi + provider + model
terisolasi.

## Service mode (systemd)

Installer interaktif menawarkan setup systemd. Manual:

```bash
sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$HOME/.cc-tg|g" \
    cc-tg.service.template | sudo tee /etc/systemd/system/cc-tg.service
sudo systemctl daemon-reload
sudo systemctl enable --now cc-tg.service
journalctl -u cc-tg -f
```

## Update

```bash
cd ~/.cc-tg && git pull && .venv/bin/pip install -r requirements.txt
sudo systemctl restart cc-tg   # kalau pakai systemd
```

Atau jalankan ulang `install.sh` (idempotent).

## Lisensi

MIT.
