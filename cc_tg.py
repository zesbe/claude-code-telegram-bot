#!/usr/bin/env python3
"""CC-TG — Claude Code Telegram bot (multi-provider).

All tools, agents, skills, MCP, compact are handled by Claude Code.
This bot just relays Telegram messages to the native `claude` binary
(provider via env injection from providers.json) and sends output back.

Usage: python3 cc_tg.py
"""
import json, os, sys, time, subprocess, re, uuid, sqlite3, traceback, threading, unicodedata
from pathlib import Path
import httpx
import telegramify_markdown

# ── Config ──────────────────────────────────────────────────────────────────
BOT_DIR = Path(__file__).parent
CFG = json.loads((BOT_DIR / "config.json").read_text())

# ── Claude Code folder auto-detect ──────────────────────────────────────────
# `~/.claude` lokasinya bisa di-override (Claude Code menerima env CLAUDE_CONFIG_DIR
# dari user/install non-standar). Slug folder project di `~/.claude/projects/`
# saat ini = workdir.replace("/","-"), tapi format bisa berubah saat update — jadi
# kita scan slug-slug yang ada & cari yang isi-nya cocok dgn workdir kita
# (fallback berurutan, tahan-update).
import os as _os
def _claude_home() -> Path:
    """Folder Claude Code (default ~/.claude, override via CLAUDE_CONFIG_DIR)."""
    return Path(_os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))

def _claude_projects_dir() -> Path:
    return _claude_home() / "projects"

def _find_claude_bin() -> str:
    """Cari binary `claude`. Urutan: config → PATH → kandidat umum."""
    cfg_bin = CFG.get("claude_bin")
    if cfg_bin and Path(cfg_bin).exists():
        return cfg_bin
    import shutil as _sh
    found = _sh.which("claude")
    if found:
        return found
    for c in (Path.home()/".local/bin/claude", Path("/usr/local/bin/claude"),
              Path("/usr/bin/claude"), Path.home()/".npm-global/bin/claude",
              Path.home()/".bun/bin/claude"):
        if c.exists():
            return str(c)
    return cfg_bin or "claude"   # last resort: lewat PATH saat dipanggil

TG_TOKEN    = CFG["telegram_token"]
OWNER_IDS   = set(CFG.get("owner_ids", []))
CLAUDE_BIN  = _find_claude_bin()
WORKDIR     = CFG.get("default_workdir", str(Path.home()))
CLAUDE_TIMEOUT = CFG.get("claude_timeout", 600)
MODEL_SLOT  = CFG.get("model_slot", "opus")
# Claude Code --effort levels (depth of thinking). Mirrors terminal /effort.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Claude runs headless (-p) relayed through Telegram. There is NO interactive
# UI here: the user cannot press space/arrows to pick options like in the
# terminal. Tell Claude so it never blocks on un-clickable choice prompts.
TELE_SYSTEM_PROMPT = CFG.get("system_prompt", (
    "Kamu dijalankan lewat bot Telegram, BUKAN terminal interaktif. "
    "User TIDAK BISA menekan space/panah atau klik prompt ala-terminal — "
    "mereka hanya bisa mengetik balasan teks ATAU menekan tombol yang bot sediakan.\n\n"
    "Kalau keputusan bisa kamu ambil sendiri dari konteks dengan default yang wajar, "
    "langsung kerjakan tanpa bertanya.\n\n"
    "Kalau BENAR-BENAR perlu user memilih, tulis pilihan dalam blok khusus ini "
    "(bot akan ubah jadi tombol yang bisa diklik):\n"
    "[[PICK]]\n"
    "1. opsi pertama\n"
    "2. opsi kedua\n"
    "3. opsi ketiga\n"
    "[[/PICK]]\n"
    "Aturan blok PICK: taruh di AKHIR pesan, satu opsi per baris diawali angka, "
    "teks opsi singkat (maks ~50 char), maksimal 8 opsi. "
    "Tulis pertanyaan/penjelasan SEBELUM blok. Jangan pakai blok ini kalau tidak menanyakan pilihan.\n\n"
    "ATURAN PENTING — PILIH PICK vs MULTIPICK:\n"
    "• Kalau jawaban yang benar HANYA SATU (mutually exclusive, mis. 'pilih bahasa', "
    "'pakai opsi A atau B') → pakai [[PICK]].\n"
    "• Kalau user BOLEH memilih LEBIH DARI SATU (mis. 'fitur apa saja yang mau', "
    "'centang semua yang relevan', 'pilih beberapa', daftar checklist, scope multi-item) "
    "→ WAJIB pakai [[MULTIPICK]], JANGAN [[PICK]]. Ini default untuk pertanyaan "
    "'mana saja' / 'fitur apa aja' / 'centang' / 'boleh banyak'.\n"
    "[[MULTIPICK]]\n"
    "1. fitur A\n"
    "2. fitur B\n"
    "3. fitur C\n"
    "[[/MULTIPICK]]\n"
    "Aturan MULTIPICK: sama dengan PICK, tapi user bisa toggle on/off banyak opsi "
    "lalu tekan tombol ✅ Selesai untuk konfirmasi. Maksimal 8 opsi. "
    "Kalau ragu antara PICK/MULTIPICK dan pertanyaannya soal 'pilih fitur/scope', "
    "pilih MULTIPICK.\n\n"
    "PRESENTASI — data terstruktur: untuk perbandingan/daftar berkolom/rekap/angka, "
    "GUNAKAN tabel markdown (| kolom | kolom | lalu baris pemisah |---|). Bot "
    "merender tabel otomatis jadi kotak rapi (monospace) di Telegram. Beri baris "
    "kosong sebelum & sesudah tabel. Cukup 1 kolom → pakai daftar bullet, bukan tabel. "
    "Kolom singkat; kalau teks cell panjang, tabel jadi tinggi — pecah jadi bullet."))

TG = f"https://api.telegram.org/bot{TG_TOKEN}"
SESS_DIR  = BOT_DIR / "sessions"
LOG_PATH  = BOT_DIR / "logs" / "bot.log"

# Per-chat state
_busy: set[int] = set()          # chats currently running Claude Code
_current_chat_id: int = 0        # set before run_claude, used by send_to_telegram.sh
_pending_rename: dict[int, str] = {}  # cid -> session_id waiting for a new title
_pending_provider: dict = {}     # cid -> {"step": str, "data": {...}} add-provider wizard
_running_procs: dict = {}        # lock_key -> Popen (for /stop interrupt)
_cancelled: set = set()          # lock_keys user asked to cancel
_usage_log: dict = {}            # provider -> {"tokens": int, "cost": float, "calls": int}

# Global concurrency cap: how many Claude Code processes may run AT ONCE across
# all chats/topics. Each Claude proc can eat 0.5–5GB RAM; without a cap, 10+
# topics firing together can OOM the box (the real cause of "tiba-tiba mati").
# Topics beyond the cap aren't rejected — they wait for a free slot.
import threading as _thr_mod
MAX_CONCURRENT = CFG.get("max_concurrent", 3)
_claude_slots = _thr_mod.Semaphore(MAX_CONCURRENT)


# Add-provider wizard steps (in order)
_PV_STEPS = [
    ("name",    "1️⃣ *Nama provider* (huruf kecil/angka/dash, mis. `groq`)"),
    ("base_url","2️⃣ *Base URL* endpoint Anthropic-compatible\nmis. `https://api.groq.com/anthropic`"),
    ("token",   "3️⃣ *API token / key*"),
    ("opus",    "4️⃣ *Model untuk slot Opus* (model paling pinter)\nmis. `llama-3.3-70b`"),
    ("sonnet",  "5️⃣ *Model untuk slot Sonnet* (ketik `-` untuk samakan dgn Opus)"),
    ("haiku",   "6️⃣ *Model untuk slot Haiku* (ketik `-` untuk samakan dgn Sonnet)"),
]

# ── Providers (loaded from Claude Hub DB) ───────────────────────────────────
# ── Providers (self-contained — no Claude Hub dependency) ────────────────────
# Providers disimpan di providers.json milik bot sendiri. run_claude men-set
# ANTHROPIC_BASE_URL / AUTH_TOKEN / model env LANGSUNG ke binary `claude` native
# (tanpa wrapper, tanpa proxy Hub). Token HANYA ada di providers.json (gitignored)
# dan di-inject ke env subprocess saat spawn — tidak pernah ditulis ke wrapper.
PROVIDERS_FILE = BOT_DIR / "providers.json"
# Bot pakai wrapper bernama 'claude-telegram' (passthrough tipis ke claude native).
# Folder ~/.claude TETAP SAMA dgn terminal (sesi bisa diakses dari terminal juga);
# nama beda cuma biar gampang dikenali & gak ketuker sama claude-deep dll.
# Fallback ke claude native kalau wrapper belum dibuat.
_BOT_WRAPPER   = str(Path.home() / ".local" / "bin" / "claude-telegram")
NATIVE_CLAUDE  = _BOT_WRAPPER if Path(_BOT_WRAPPER).exists() else CLAUDE_BIN
HUB_DB = CFG.get("hub_db", str(Path.home() / ".claude-hub" / "profiles.db"))
DEFAULT_PROVIDER = CFG.get("default_provider", "claude")  # immutable: default utk window BARU (anti cross-chat contamination)
PROVIDER = DEFAULT_PROVIDER  # global "current": cuma utk display & fallback; disetel ulang tiap user switch

def _migrate_from_hub() -> dict:
    """Sekali jalan: kalau providers.json belum ada tapi Hub DB ada, tarik semua
    profile Hub ke providers.json supaya setup lama tidak hilang."""
    out = {}
    if not Path(HUB_DB).exists():
        return out
    try:
        conn = sqlite3.connect(HUB_DB); conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT * FROM profiles"):
            d = dict(r)
            out[d["name"]] = {
                "base_url": d.get("base_url", ""),
                "token":    d.get("auth_token", ""),
                "opus":     d.get("opus_model", ""),
                "sonnet":   d.get("sonnet_model", "") or d.get("opus_model", ""),
                "haiku":    d.get("haiku_model", "") or d.get("sonnet_model", "") or d.get("opus_model", ""),
            }
        conn.close()
    except Exception as e:
        print(f"[providers] hub migrate failed: {e}", flush=True)
    return out

def _read_providers_file() -> dict:
    try:
        return json.loads(PROVIDERS_FILE.read_text()).get("providers", {})
    except Exception:
        return {}

def _write_providers_file(provs: dict):
    PROVIDERS_FILE.write_text(json.dumps({"providers": provs}, ensure_ascii=False, indent=2))

def _load_providers() -> dict:
    """Returns {name: {base_url, token, opus, sonnet, haiku}}.
    'claude' selalu ada = native Anthropic (tanpa override env)."""
    provs = _read_providers_file()
    if not provs:
        # bootstrap dari Hub sekali, lalu simpan ke file bot
        provs = _migrate_from_hub()
        if provs:
            _write_providers_file(provs)
            print(f"[providers] migrated {len(provs)} dari Hub -> providers.json", flush=True)
    provs.setdefault("claude", {})  # native; {} = pakai auth claude sendiri
    return provs

PROVIDERS = _load_providers()

def reload_providers():
    global PROVIDERS
    PROVIDERS = _load_providers()
    return PROVIDERS

def get_claude_bin(provider: str = None) -> str:
    """Selalu binary native. Routing provider via env (lihat _provider_env)."""
    return NATIVE_CLAUDE if Path(NATIVE_CLAUDE).exists() else CLAUDE_BIN

def _provider_env(env: dict, provider: str = None):
    """Inject base_url/token/model env untuk provider ke dict env subprocess.
    Provider 'claude' (atau tak dikenal) = tanpa override (pakai auth native)."""
    name = provider or PROVIDER
    cfg = PROVIDERS.get(name) or {}
    if not cfg.get("base_url"):
        # native / no-override: bersihkan sisa env provider biar tak bocor
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                  "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                  "ANTHROPIC_MODEL"):
            env.pop(k, None)
        return
    env["ANTHROPIC_BASE_URL"]  = cfg["base_url"]
    if cfg.get("token"):
        env["ANTHROPIC_AUTH_TOKEN"] = cfg["token"]
    if cfg.get("opus"):   env["ANTHROPIC_DEFAULT_OPUS_MODEL"]   = cfg["opus"]
    if cfg.get("sonnet"): env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = cfg["sonnet"]
    if cfg.get("haiku"):
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = cfg["haiku"]
        env["ANTHROPIC_SMALL_FAST_MODEL"]    = cfg["haiku"]
    env.pop("ANTHROPIC_MODEL", None)

def _provider_save(name, base_url, token, opus, sonnet, haiku) -> tuple:
    """Tambah/edit provider di providers.json. Returns (ok, msg)."""
    if not re.fullmatch(r"[a-z0-9_-]{1,30}", name or ""):
        return False, "nama tidak valid (huruf kecil/angka/dash)"
    provs = _read_providers_file()
    existing = provs.get(name, {})
    provs[name] = {
        "base_url": base_url if base_url is not None else existing.get("base_url", ""),
        "token":    token    if token    is not None else existing.get("token", ""),
        "opus":     opus     if opus     is not None else existing.get("opus", ""),
        "sonnet":  (sonnet   if sonnet   is not None else existing.get("sonnet", "")) or (opus if opus is not None else existing.get("opus","")),
        "haiku":   (haiku    if haiku    is not None else existing.get("haiku", "")),
    }
    if not provs[name]["haiku"]:
        provs[name]["haiku"] = provs[name]["sonnet"]
    try:
        _write_providers_file(provs); reload_providers()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def _provider_delete(name: str) -> bool:
    provs = _read_providers_file()
    if name not in provs:
        return False
    provs.pop(name, None)
    try:
        _write_providers_file(provs); reload_providers()
        return True
    except Exception as e:
        log(f"provider delete {name}: {e}"); return False

def _provider_info(name: str) -> dict:
    return _read_providers_file().get(name, {})

def _provider_rename(old: str, new: str) -> tuple:
    """Ganti nama provider. Returns (ok, msg)."""
    if not re.fullmatch(r"[a-z0-9_-]{1,30}", new or ""):
        return False, "nama baru tidak valid (huruf kecil/angka/dash)"
    provs = _read_providers_file()
    if old not in provs:
        return False, f"`{old}` tidak ada"
    if new in provs:
        return False, f"`{new}` sudah dipakai"
    provs[new] = provs.pop(old)
    try:
        _write_providers_file(provs); reload_providers()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# Parser "paste sekaligus": terima blob bebas (env-style, key:value, JSON-ish, atau
# baris URL/token telanjang) → {name, base_url, token, opus, sonnet, haiku}.
_PASTE_KEYMAP = {
    "name": "name", "nama": "name", "provider": "name",
    "base_url": "base_url", "baseurl": "base_url", "url": "base_url", "base": "base_url",
    "endpoint": "base_url", "anthropic_base_url": "base_url",
    "token": "token", "key": "token", "api_key": "token", "apikey": "token", "api": "token",
    "auth": "token", "auth_token": "token", "anthropic_auth_token": "token",
    "anthropic_api_key": "token", "x_api_key": "token",
    "opus": "opus", "sonnet": "sonnet", "haiku": "haiku", "model": "model", "models": "model",
}

def _parse_provider_paste(blob: str) -> dict:
    d = {}
    for raw in (blob or "").splitlines():
        line = raw.strip().strip(",").strip()
        if not line or line in ("{", "}"):
            continue
        # URL telanjang dulu (sebelum regex, karena 'https://' punya ':' yg bikin
        # salah-parse jadi key=https).
        low = line.lower()
        if (low.startswith("http://") or low.startswith("https://")) and "base_url" not in d:
            d["base_url"] = line.split()[0].strip('"\'')
            continue
        m = re.match(r'^["\']?([A-Za-z_][\w .\-]*?)["\']?\s*[:=]\s*["\']?(.+?)["\']?,?$', line)
        if m:
            k = m.group(1).strip().lower().replace(" ", "_").replace("-", "_")
            v = m.group(2).strip().strip('"\'').strip()
            if k in _PASTE_KEYMAP and v:
                d[_PASTE_KEYMAP[k]] = v
    # 'model' tunggal → isi semua slot yang kosong
    if d.get("model"):
        for s in ("opus", "sonnet", "haiku"):
            d.setdefault(s, d["model"])
    return d

def _derive_provider_name(base_url: str) -> str:
    """Tebak nama provider dari host base_url (mis. api.deepseek.com → deepseek)."""
    try:
        from urllib.parse import urlparse
        host = urlparse(base_url).netloc.split(":")[0]
    except Exception:
        return ""
    skip = {"api", "www", "com", "net", "io", "ai", "dev", "org", "co", "id", "app", "cloud", "v1"}
    labels = [re.sub(r"[^a-z0-9_-]", "", l.lower()) for l in host.split(".")]
    cand = [l for l in labels if l and l not in skip]
    return (cand[0] if cand else (labels[0] if labels else ""))[:30]

def _provider_ingest_paste(cid: int, blob: str):
    """Parse blob paste → test → auto-load model bila perlu → simpan. Kirim status
    ke Telegram. Dipakai /provider paste & tombol 📋 Paste config."""
    d = _parse_provider_paste(blob)
    base, token = d.get("base_url"), d.get("token")
    if not base or not token:
        send_msg(cid, "❌ Paste kurang lengkap — minimal butuh *base_url* & *token*.\n"
                      "Format: `base_url=...`, `token=...` (opsional `name=`, `opus=`, dll).")
        return
    name = d.get("name") or _derive_provider_name(base)
    if not re.fullmatch(r"[a-z0-9_-]{1,30}", name or ""):
        send_msg(cid, "❌ Nama provider gak kebaca dari paste. Tambah baris `name=<nama>`.")
        return
    send_msg(cid, f"🔌 Tes `{name}` & ambil daftar model…")
    ok, ids, info = _fetch_models(base, token)
    if not ok:
        send_msg(cid, f"❌ Endpoint gagal: *{info}*. Provider TIDAK disimpan (biar gak sia-sia).")
        return
    opus = d.get("opus") or ids[0]
    sonnet = d.get("sonnet") or opus
    haiku = d.get("haiku") or sonnet
    miss = [m for m in dict.fromkeys([opus, sonnet, haiku]) if m not in ids]
    sok, smsg = _provider_save(name, base, token, opus, sonnet, haiku)
    if not sok:
        send_msg(cid, f"❌ Gagal simpan: {smsg}")
        return
    warn = (f"\n⚠️ Model ini nggak ada di daftar endpoint (cek ejaan?): "
            f"{', '.join(miss)}") if miss else ""
    send_msg(cid, f"✅ Provider `{name}` tersimpan & konek ({info})\n"
                  f"🧠 opus=`{opus}` sonnet=`{sonnet}` haiku=`{haiku}`{warn}\n\n"
                  f"Pakai: `/provider {name}`  ·  ganti nama: `/provider rename {name} <baru>`")

def _models_url(base_url: str) -> str:
    """Bangun URL /v1/models dari base_url provider (handle base yg sudah /v1)."""
    b = (base_url or "").rstrip("/")
    return (b + "/models") if b.endswith("/v1") else (b + "/v1/models")

def _fetch_models(base_url: str, token: str, timeout: int = 15) -> tuple:
    """GET <base>/v1/models → (ok, [model_id...], info). Kirim Bearer + x-api-key
    + anthropic-version biar kompatibel lintas provider. Dipakai untuk auto-load
    model SEKALIGUS test koneksi (nol biaya token). Fallback ke root domain kalau
    path /anthropic 404 (mis. DeepSeek listing model di root /v1/models)."""
    import httpx as _hx
    from urllib.parse import urlparse
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["x-api-key"] = token
    # Kandidat URL: utama (base/v1/models), lalu root domain (scheme://host/v1/models)
    urls = [_models_url(base_url)]
    try:
        p = urlparse(base_url)
        root = f"{p.scheme}://{p.netloc}/v1/models"
        if root not in urls:
            urls.append(root)
    except Exception:
        pass
    last = "tak bisa konek"
    for url in urls:
        try:
            r = _hx.get(url, headers=headers, timeout=_hx.Timeout(timeout, connect=8))
        except Exception as e:
            last = f"tak bisa konek: {str(e)[:100]}"; continue
        if r.status_code in (401, 403):
            return False, [], f"token ditolak (HTTP {r.status_code})"
        if r.status_code >= 400:
            last = f"HTTP {r.status_code}"; continue
        try:
            data = r.json()
        except Exception:
            last = "respons bukan JSON"; continue
        items = data.get("data") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        ids = []
        for it in (items or []):
            if isinstance(it, dict) and it.get("id"):
                ids.append(str(it["id"]))
            elif isinstance(it, str):
                ids.append(it)
        if ids:
            return True, ids, f"{len(ids)} model"
        last = "konek OK tapi daftar model kosong"
    return False, [], last

def _test_endpoint(base_url: str, token: str) -> tuple:
    """Cek endpoint konek + token valid via /v1/models. Returns (ok, msg)."""
    ok, ids, info = _fetch_models(base_url, token)
    if ok:
        return True, f"konek ✓ — {info} (mis. {', '.join(ids[:3])})"
    return False, info

# ── httpx ───────────────────────────────────────────────────────────────────
tg_http = httpx.Client(timeout=httpx.Timeout(90, connect=10))

# ── Logging ─────────────────────────────────────────────────────────────────
# Token Telegram (format bot<id>:<hash>) sering nyangkut di string error httpx,
# mis. "Client error '409 Conflict' for url 'https://api.telegram.org/bot<TOKEN>/getUpdates'".
# Kalau dilewatkan mentah, token tertulis plaintext ke logs/bot.log + journald.
# _redact() dipasang di log() — satu corong untuk SEMUA pesan (Poll error,
# traceback, cron, tg_api, dst) — jadi tak ada jalur log yang lolos.
# Cocokkan pola UMUM (bukan cuma nilai TG_TOKEN) biar tetap aman walau token
# diganti / ada token lain.
_REDACT_TG_RE = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")

def _redact(s) -> str:
    if not isinstance(s, str):
        s = str(s)
    if TG_TOKEN:
        s = s.replace(TG_TOKEN, "bot<REDACTED>")
    return _REDACT_TG_RE.sub("bot<REDACTED>", s)

def log(msg: str):
    msg = _redact(msg)  # JANGAN pernah tulis token/secret ke log maupun stdout
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)

# ── Telegram helpers ────────────────────────────────────────────────────────
def tg_api(method: str, **kw) -> dict:
    # All topics in one group share a chat_id; live-streaming many topics at
    # once can hit Telegram's ~1 msg/sec/chat limit → HTTP 429. Honor the
    # server's retry_after and back off instead of crashing the call.
    for attempt in range(4):
        r = tg_http.post(f"{TG}/{method}", json=kw)
        if r.status_code == 429:
            try:
                wait = r.json().get("parameters", {}).get("retry_after", 1)
            except Exception:
                wait = 1
            time.sleep(min(wait, 10) + 0.1)
            continue
        r.raise_for_status()
        d = r.json()
        if not d.get("ok"):
            log(f"TG {method} NOT OK: {d}")
        return d
    log(f"TG {method} gave up after 429 retries")
    return {}

def _download_tg_file(cid: int, file_id: str, file_name: str) -> str:
    """Download a Telegram file into the active window's workdir/uploads. Returns path or ''."""
    try:
        info = tg_api("getFile", file_id=file_id)
        fp = info.get("result", {}).get("file_path")
        if not fp:
            return ""
        wd = load_sess(cid).get("workdir", WORKDIR)
        updir = Path(wd) / "uploads"
        updir.mkdir(parents=True, exist_ok=True)
        dest = updir / file_name
        url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{fp}"
        r = tg_http.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
        log(f"downloaded {file_name} ({len(r.content)}b) → {dest}")
        return str(dest)
    except Exception as e:
        log(f"download failed: {e}")
        return ""

def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from Claude Code output."""
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)

# ── Markdown tables → boxed monospace grids (Telegram MarkdownV2 has no tables) ─
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")
_MAX_BOX_WIDTH = 64  # display cols; wider tables wrap ugly in Telegram → skip boxing

def _dispw(s: str) -> int:
    """Display width: CJK/fullwidth chars count as 2 (best-effort, no emoji)."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)

def _box_pad(s: str, width: int) -> str:
    """Left-justify s to `width` display columns (right-pad with spaces)."""
    return s + " " * max(0, width - _dispw(s))

def _box_split_row(line: str) -> list:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]

def _render_box(rows: list) -> str:
    ncols = max((len(r) for r in rows), default=0)
    if ncols == 0:
        return ""
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    widths = [max(_dispw(r[c]) for r in rows) for c in range(ncols)]
    if sum(widths) + 3 * ncols + 1 > _MAX_BOX_WIDTH:
        return ""  # too wide → caller leaves markdown as-is
    def bar(l, m, r):
        return l + m.join("─" * (w + 2) for w in widths) + r
    out = [bar("┌", "┬", "┐")]
    for ri, r in enumerate(rows):
        out.append("│" + "│".join(" " + _box_pad(r[c], widths[c]) + " " for c in range(ncols)) + "│")
        if ri == 0:
            out.append(bar("├", "┼", "┤"))
    out.append(bar("└", "┴", "┘"))
    return "\n".join(out)

def _render_table_bullets(rows: list) -> str:
    """Wide-table fallback (saat box grid bakal wrap jelek): tiap baris jadi
    'kartu' vertikal — label baris bold + bullet 'kolom: nilai'. Markdown biasa
    (bukan monospace); meniru gaya tabel Hermes di Telegram."""
    if len(rows) < 2:
        return ""
    headers = rows[0]
    cards = []
    for r in rows[1:]:
        if not any((c or "").strip() for c in r):
            continue
        heading = r[0].strip() if r and r[0].strip() else "•"
        bullets = []
        for idx, val in enumerate(r[1:], start=1):
            col = headers[idx].strip() if idx < len(headers) and headers[idx].strip() else f"col{idx}"
            val = (val or "").strip()
            if val and val != heading:
                bullets.append(f"• {col}: {val}")
        cards.append(f"**{heading}**" + ("\n" + "\n".join(bullets) if bullets else ""))
    return "\n\n".join(cards)

def _boxify_tables(text: str) -> str:
    """Replace GFM markdown tables with Unicode box-drawing tables inside a
    fenced code block (monospace in Telegram). Non-table text untouched."""
    lines = text.split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        if (i + 1 < n
                and _MD_TABLE_ROW_RE.match(lines[i])
                and _MD_TABLE_SEP_RE.match(lines[i + 1])):
            rows = [_box_split_row(lines[i])]
            j = i + 2
            while j < n and _MD_TABLE_ROW_RE.match(lines[j]):
                rows.append(_box_split_row(lines[j]))
                j += 1
            box = _render_box(rows)
            if box:
                out.append("```\n" + box + "\n```")
            else:
                bullets = _render_table_bullets(rows)  # too wide → vertical cards
                out.append(bullets if bullets else "\n".join(lines[i:j]))
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)

# MarkdownV2 special chars yang wajib di-escape di luar code/link.
_MDV2_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#\+\-=|{}.!\\])')

def _esc_mdv2(t: str) -> str:
    return _MDV2_ESCAPE_RE.sub(r'\\\1', t)

def _format_mdv2(content: str) -> str:
    """Render Markdown standar → Telegram MarkdownV2 yang RAPI (gaya Hermes).
    Ganti telegramify yg bikin heading emoji-pensil + bullet '⦁' aneh.
    Code/inline/link di-'protect' lewat placeholder supaya tak ke-escape;
    heading→*bold*, **x**→*x*, *x*/_x_→_x_, bullet '- '/'* '→'• ', blockquote
    dipertahankan, sisanya di-escape presisi."""
    if not content:
        return content
    ph: dict = {}
    cnt = [0]
    def _ph(v: str) -> str:
        k = f"\x00P{cnt[0]}\x00"; cnt[0] += 1; ph[k] = v; return k
    text = content

    # 1) fenced code block — escape \ dan ` di dalamnya, lalu protect
    def _pf(m):
        raw = m.group(0)
        oe = raw.index('\n') + 1 if '\n' in raw[3:] else 3
        body = raw[oe:][:-3].replace('\\', '\\\\').replace('`', '\\`')
        return _ph(raw[:oe] + body + '```')
    text = re.sub(r'(```(?:[^\n]*\n)?[\s\S]*?```)', _pf, text)
    # 2) inline code — protect
    text = re.sub(r'(`[^`\n]+`)', lambda m: _ph(m.group(0).replace('\\', '\\\\')), text)
    # 3) link [teks](url) — escape teks, protect
    text = re.sub(r'\[([^\]]+)\]\(([^()\s]+)\)',
                  lambda m: _ph(f'[{_esc_mdv2(m.group(1))}]({m.group(2)})'), text)
    # 4) heading (## Judul) → *Judul* bold
    def _heading(m):
        # strip **bold** di dalam heading dulu (regex dipisah dari f-string biar
        # tak ada backslash dalam expression → kompatibel Python 3.10/3.11)
        inner = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1).strip())
        return _ph(f'*{_esc_mdv2(inner)}*')
    text = re.sub(r'^#{1,6}\s+(.+)$', _heading, text, flags=re.MULTILINE)
    # 5) bullet '- ' / '* ' di awal baris → '• ' (rapi, bukan '⦁'/escaped)
    text = re.sub(r'^(\s*)[-*]\s+', lambda m: m.group(1) + _ph('•') + ' ', text, flags=re.MULTILINE)
    # 6) bold **x** → *x*
    text = re.sub(r'\*\*(.+?)\*\*', lambda m: _ph(f'*{_esc_mdv2(m.group(1))}*'), text)
    # 7) italic *x* / _x_ → _x_
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', lambda m: _ph(f'_{_esc_mdv2(m.group(1))}_'), text)
    text = re.sub(r'(?<!\w)_([^_\n]+)_(?!\w)', lambda m: _ph(f'_{_esc_mdv2(m.group(1))}_'), text)
    # 8) blockquote '> ' → pertahankan '>' tak ter-escape
    text = re.sub(r'^(>{1,3}) (.+)$', lambda m: _ph(f'{m.group(1)} {_esc_mdv2(m.group(2))}'),
                  text, flags=re.MULTILINE)
    # 9) escape sisa teks biasa
    text = _esc_mdv2(text)
    # 10) restore placeholder (reverse order utk nested)
    for k in reversed(list(ph.keys())):
        text = text.replace(k, ph[k])
    return text

def _to_md(text: str) -> str:
    """Markdown → Telegram MarkdownV2 yang rapi. Tabel jadi box/bullet dulu."""
    if text and "|" in text:
        try:
            text = _boxify_tables(text)
        except Exception:
            pass
    try:
        return _format_mdv2(text)
    except Exception:
        # Fallback: escape semua sebagai plain MarkdownV2
        return _esc_mdv2(text)

def _split_chunks(text: str, limit: int = 4000) -> list:
    """Split into <=limit chunks at paragraph/line breaks, never inside ``` blocks."""
    if len(text) <= limit:
        return [text]
    chunks, buf = [], ""
    for para in text.split("\n\n"):
        candidate = (buf + "\n\n" + para) if buf else para
        if len(candidate) > limit and buf:
            chunks.append(buf)
            buf = para
            while len(buf) > limit:
                idx = buf.rfind("\n", 0, limit)
                if idx < 0:
                    idx = limit
                chunks.append(buf[:idx])
                buf = buf[idx:].lstrip("\n")
        else:
            buf = candidate
    if buf.strip():
        chunks.append(buf)
    # Repair ``` fences split across chunks
    fixed = []
    for c in chunks:
        if c.count("```") % 2 == 1:
            c = c + "\n```"
        fixed.append(c)
    return fixed

def _send_raw(chat_id: int, md: str, reply_to: int = 0, thread_id: int = 0) -> dict:
    """Send MarkdownV2 text, chunked. Falls back to plain on parse error."""
    parts = _split_chunks(md, 4000)
    res = {}
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        kw = {"chat_id": chat_id, "text": part, "parse_mode": "MarkdownV2"}
        if thread_id:
            kw["message_thread_id"] = thread_id
        if reply_to and i == 0:
            kw["reply_to_message_id"] = reply_to
        try:
            res = tg_api("sendMessage", **kw)
        except Exception:
            # Fallback: strip MarkdownV2 escapes, send plain
            plain = re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!\\])', r'\1', part)
            kw["text"] = plain[:4096]
            kw.pop("parse_mode", None)
            try:
                res = tg_api("sendMessage", **kw)
            except Exception:
                pass
    return res

def send_msg(chat_id: int, text: str, reply_to: int = 0, thread_id: int = 0) -> dict:
    """Send markdown text (AI output or command response) to Telegram."""
    return _send_raw(chat_id, _to_md(text), reply_to, thread_id)

# Backward-compat alias: command responses now use markdown too
def send_html(chat_id: int, text: str, reply_to: int = 0) -> dict:
    return _send_raw(chat_id, _to_md(text), reply_to)

# ── PICK blocks: turn Claude's choice list into clickable Telegram buttons ────
# Claude emits  [[PICK]] 1. a \n 2. b [[/PICK]]  when it needs the user to pick.
# We strip the block, render the body as inline buttons, and on click feed the
# chosen option back to Claude as a normal message (so it continues the turn).
_PICK_RE = re.compile(r"\[\[PICK\]\](.*?)\[\[/PICK\]\]", re.DOTALL | re.IGNORECASE)
# Multi-pick: user can select multiple options before confirming
_MULTIPICK_RE = re.compile(r"\[\[MULTIPICK\]\](.*?)\[\[/MULTIPICK\]\]", re.DOTALL | re.IGNORECASE)
# pick_token -> list[str] of option texts (per chat). Keeps callback_data short.
_pending_pick: dict = {}
# multipick_token -> (options, set[int]) — tracks selected indices
_pending_multipick: dict = {}

def _parse_pick(text: str):
    """Return (clean_text, [options]) if a PICK block exists, else (text, None)."""
    m = _PICK_RE.search(text or "")
    if not m:
        return text, None
    options = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        # strip leading "1." / "1)" / "- " / "* "
        line = re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line).strip()
        if line:
            options.append(line[:60])
    clean = (text[:m.start()] + text[m.end():]).strip()
    return clean, (options[:8] or None)

def _parse_multipick(text: str):
    """Return (clean_text, [options]) if a MULTIPICK block exists, else (text, None)."""
    m = _MULTIPICK_RE.search(text or "")
    if not m:
        return text, None
    options = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line).strip()
        if line:
            options.append(line[:60])
    clean = (text[:m.start()] + text[m.end():]).strip()
    return clean, (options[:8] or None)

def send_with_pick(chat_id: int, text: str, reply_to: int = 0, thread_id: int = 0) -> bool:
    """If text has a PICK/MULTIPICK block, send text + inline option buttons.
    Returns True if a pick was rendered (caller should NOT also send the raw text)."""

    # Check MULTIPICK first (multi-select with toggle + confirm button)
    clean_mp, mp_options = _parse_multipick(text)
    if mp_options:
        tok = uuid.uuid4().hex[:8]
        _pending_multipick[(chat_id, tok)] = (mp_options, set())
        # Build toggle buttons with ☐ prefix
        rows = [[{"text": f"☐ {opt[:38]}", "callback_data": f"mpick:{tok}:{i}"}]
                for i, opt in enumerate(mp_options)]
        # Add "Selesai" confirmation button
        rows.append([{"text": "✅ Selesai", "callback_data": f"mpdone:{tok}"}])
        kb = {"inline_keyboard": rows}
        body = clean_mp or "Pilih (bisa lebih dari satu):"
        kw = {"chat_id": chat_id, "text": _to_md(body), "parse_mode": "MarkdownV2",
              "reply_markup": kb}
        if thread_id:
            kw["message_thread_id"] = thread_id
        if reply_to:
            kw["reply_to_message_id"] = reply_to
        try:
            tg_api("sendMessage", **kw)
        except Exception:
            kw["text"] = re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!\\])', r'\1', kw["text"])[:4096]
            kw.pop("parse_mode", None)
            tg_api("sendMessage", **kw)
        return True

    # Single-select PICK
    clean, options = _parse_pick(text)
    if not options:
        return False
    tok = uuid.uuid4().hex[:8]
    _pending_pick[(chat_id, tok)] = options
    rows = [[{"text": f"{i+1}. {opt[:40]}", "callback_data": f"pick:{tok}:{i}"}]
            for i, opt in enumerate(options)]
    kb = {"inline_keyboard": rows}
    body = clean or "Pilih salah satu:"
    kw = {"chat_id": chat_id, "text": _to_md(body), "parse_mode": "MarkdownV2",
          "reply_markup": kb}
    if thread_id:
        kw["message_thread_id"] = thread_id
    if reply_to:
        kw["reply_to_message_id"] = reply_to
    try:
        tg_api("sendMessage", **kw)
    except Exception:
        kw["text"] = re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!\\])', r'\1', kw["text"])[:4096]
        kw.pop("parse_mode", None)
        tg_api("sendMessage", **kw)
    return True

def edit_msg(chat_id: int, mid: int, text: str) -> dict:
    md = _to_md(text)
    if len(md) > 4000:
        idx = md.rfind("\n", 0, 3900)
        md = (md[:idx] if idx > 0 else md[:3900]) + "\n\n_(dipotong)_"
    try:
        return tg_api("editMessageText", chat_id=chat_id, message_id=mid,
                      text=md, parse_mode="MarkdownV2")
    except Exception:
        plain = re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!\\])', r'\1', md)
        try:
            return tg_api("editMessageText", chat_id=chat_id, message_id=mid,
                          text=plain[:4096])
        except Exception:
            return {}


def edit_md(chat_id: int, mid: int, text: str, reply_markup=None) -> dict:
    """Edit a message with markdown text (for inline-keyboard callbacks)."""
    md = _to_md(text)
    kw = {"chat_id": chat_id, "message_id": mid, "text": md, "parse_mode": "MarkdownV2"}
    if reply_markup is not None:
        kw["reply_markup"] = reply_markup
    try:
        return tg_api("editMessageText", **kw)
    except Exception:
        return {}

def typing(chat_id: int, thread_id: int = 0):
    try:
        kw = {"chat_id": chat_id, "action": "typing"}
        if thread_id:
            kw["message_thread_id"] = thread_id
        tg_api("sendChatAction", **kw)
    except Exception:
        pass

# ── Session management (multi-window) ────────────────────────────────────────
# Structure: _store[cid] = {
#     "active": "main",
#     "windows": {
#         "main": {"session_id": "uuid", "workdir": "...", "provider": "claude"},
#         "project-x": {"session_id": "uuid", "workdir": "...", "provider": "deepseek"},
#     }
# }
_store: dict[int, dict] = {}
import threading as _threading
_store_lock = _threading.Lock()  # protects _store writes (reads are fine under GIL)

def _cc_latest_session(workdir: str) -> str | None:
    """Find the most recently modified Claude Code session UUID."""
    proj = _cc_project_dir(workdir)
    if not proj:
        return None
    files = sorted(proj.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)
    return files[0].stem if files else None

def _new_window_provider(cid: int) -> str:
    """Provider untuk window BARU (topic baru, `/w` baru, recovery window).
    Warisi dari window AKTIF chat ini (konsisten intra-chat); kalau belum ada,
    pakai DEFAULT_PROVIDER. SENGAJA bukan global `PROVIDER` yang berubah tiap
    user switch di chat MANA pun — itulah sumber 'pindah provider nyangkut'."""
    try:
        store = _store.get(cid) or {}
        w = (store.get("windows") or {}).get(store.get("active", "main")) or {}
        if w.get("provider"):
            return w["provider"]
    except Exception:
        pass
    return DEFAULT_PROVIDER

def _win_provider(cid: int) -> str:
    """Provider window AKTIF chat ini — SUMBER KEBENARAN utk display & switch.
    JANGAN pakai global `PROVIDER` (global cuma default chat baru); kalau dipakai
    utk highlight/teks, switch di satu window/topic terlihat 'ikut' ke yang lain."""
    try:
        store = _load_store(cid)
        w = (store.get("windows") or {}).get(store.get("active", "main")) or {}
        return w.get("provider") or DEFAULT_PROVIDER
    except Exception:
        return DEFAULT_PROVIDER

def _win_model(cid: int) -> str:
    """Model slot window AKTIF chat ini — sumber kebenaran display (bukan global)."""
    try:
        store = _load_store(cid)
        w = (store.get("windows") or {}).get(store.get("active", "main")) or {}
        return w.get("model") or MODEL_SLOT
    except Exception:
        return MODEL_SLOT

def _ensure_win_keys(w: dict) -> dict:
    """Pastikan window punya provider+model+session_id+workdir EKSPLISIT, supaya
    run_claude/display tak pernah jatuh ke global yg bisa terkontaminasi switch."""
    if not w.get("session_id"):
        w["session_id"] = str(uuid.uuid4())
    if not w.get("provider"):
        w["provider"] = DEFAULT_PROVIDER
    if not w.get("model"):
        w["model"] = MODEL_SLOT
    if not w.get("workdir"):
        w["workdir"] = WORKDIR
    return w

def _load_store(cid: int) -> dict:
    """Load full window store for a chat."""
    if cid in _store:
        return _store[cid]
    p = SESS_DIR / f"{cid}.json"
    if p.exists():
        try:
            data = json.loads(p.read_text())
            # Migrate old format (flat {session_id, workdir}) to window format
            if "session_id" in data and "windows" not in data:
                data = {
                    "active": "main",
                    "windows": {"main": {
                        "session_id": data["session_id"],
                        "workdir": data.get("workdir", WORKDIR),
                        "provider": DEFAULT_PROVIDER,
                    }}
                }
            for _w in (data.get("windows") or {}).values():
                _ensure_win_keys(_w)   # backfill provider/model eksplisit per window
            _store[cid] = data
            return data
        except Exception:
            pass
    # Fresh: create default window with latest Claude Code session
    latest = _cc_latest_session(WORKDIR)
    _store[cid] = {
        "active": "main",
        "windows": {"main": {
            "session_id": latest or str(uuid.uuid4()),
            "workdir": WORKDIR,
            "provider": DEFAULT_PROVIDER, "model": MODEL_SLOT,
        }}
    }
    return _store[cid]

def _save_store(cid: int):
    if cid not in _store:
        return
    p = SESS_DIR / f"{cid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    with _store_lock:
        p.write_text(json.dumps(_store[cid], ensure_ascii=False, default=str))

def load_sess(cid: int) -> dict:
    """Returns active window's session data (flat: session_id, workdir, provider)."""
    store = _load_store(cid)
    active = store.get("active", "main")
    windows = store.get("windows", {})
    if active not in windows:
        # Create missing window
        latest = _cc_latest_session(WORKDIR)
        windows[active] = {"session_id": latest or str(uuid.uuid4()), "workdir": WORKDIR, "provider": _new_window_provider(cid), "model": MODEL_SLOT}
        store["windows"] = windows
        _save_store(cid)
    return windows[active]

def save_sess(cid: int):
    _save_store(cid)

def new_session(cid: int):
    """Create fresh session in active window — PERTAHANKAN provider/model/workdir
    window (cuma reset session_id). `/new` jangan diam-diam ganti provider."""
    store = _load_store(cid)
    active = store.get("active", "main")
    old = store.get("windows", {}).get(active, {})
    store["windows"][active] = {
        "session_id": str(uuid.uuid4()),
        "workdir": old.get("workdir", WORKDIR),
        "provider": old.get("provider", DEFAULT_PROVIDER),
        "model": old.get("model", MODEL_SLOT),
    }
    _save_store(cid)

# ── Auto-compact (RESEED deterministik) ──────────────────────────────────────
# Bot drives `claude -p` (headless one-shot per pesan), yang TIDAK punya
# auto-compact loop interaktif → konteks membengkak tak terbatas.
# CATATAN: `/compact` native via `--resume` TERBUKTI tidak reliable di mode -p —
# file sesi append-only & bercabang, `--resume` sering nyasar balik ke cabang
# lama yang gemuk (investigasi 2026-06-26: cuma nyangkut 1 dari 7×). Maka dipakai
# RESEED: ringkas sesi lama → session_id BARU yang bersih (file linear, 0 cabang)
# → ringkasan jadi seed pesan berikutnya. Deterministik: tiap kali pasti turun.
#
# Context window per SLOT model (opus/sonnet/haiku). Bisa beda kalau provider
# underlying (DeepSeek/GLM dll) lebih kecil → override di config["context_windows"].
CONTEXT_WINDOWS = {"opus": 1_000_000, "sonnet": 1_000_000, "haiku": 200_000}
CONTEXT_WINDOWS.update(CFG.get("context_windows", {}))
# Fraksi window yang memicu auto-compact. 0 = nonaktif.
AUTO_COMPACT_RATIO = CFG.get("auto_compact_ratio", 0.85)

def _compact_threshold(model_slot: str = None) -> int:
    """Ambang token (per slot model) pemicu auto-compact. 0 = nonaktif."""
    if not AUTO_COMPACT_RATIO:
        return 0
    win = CONTEXT_WINDOWS.get(model_slot or MODEL_SLOT,
                              CONTEXT_WINDOWS.get("opus", 1_000_000))
    return int(win * AUTO_COMPACT_RATIO)

_RESEED_SUMMARY_PROMPT = (
    "Buat RINGKASAN HANDOFF dari SELURUH percakapan ini untuk dilanjutkan di "
    "sesi baru yang kosong. Ringkasan ini akan jadi SATU-SATUNYA memori sesi "
    "berikutnya, jadi harus cukup untuk melanjutkan tanpa kehilangan konteks. "
    "Sertakan: (1) tujuan/tugas utama yang sedang dikerjakan; (2) keputusan "
    "penting beserta alasannya; (3) file/path/perintah/identifier kunci; "
    "(4) yang SUDAH selesai; (5) yang masih PENDING / langkah berikutnya; "
    "(6) detail teknis yang tak boleh hilang. Langsung tulis ringkasannya — "
    "padat tapi lengkap, tanpa kalimat pembuka/penutup."
)

def _reseed_compact(cid: int, win_name: str = None) -> tuple:
    """Compact via RESEED (deterministik): ringkas sesi lama, lalu pindah ke
    session_id BARU yang bersih; ringkasan disimpan sebagai `pending_seed` dan
    disisipkan ke pesan user berikutnya. Menghindari masalah `--resume` yang
    nyasar ke cabang lama gemuk. Sesi lama TIDAK dihapus (masih bisa di-resume
    manual). Returns (ok, info)."""
    store = _load_store(cid)
    win_name = win_name or store.get("active", "main")
    win = store.get("windows", {}).get(win_name)
    if not win:
        return False, "window tak ada"
    wd = win.get("workdir", WORKDIR)
    old_sid = win.get("session_id", "")
    if not _session_exists(wd, old_sid):
        return False, "sesi masih kosong (belum perlu dipangkas)"
    # 1) Minta Claude meringkas seluruh sesi lama (handoff summary).
    summary, _ = run_claude(_RESEED_SUMMARY_PROMPT, cid, wd, old_sid,
                            provider=win.get("provider"), model=win.get("model"))
    summary = (summary or "").strip()
    if not summary:
        return False, "ringkasan kosong (sesi lama tetap utuh)"
    if "not enough messages" in summary.lower():
        return False, "sesi masih terlalu pendek"
    if summary[0] in "❌⏰⏹⚠️":
        return False, "gagal meringkas (sesi lama tetap utuh, coba lagi)"
    # 2) Pindah ke session_id BARU (file bersih) + simpan seed sekali-pakai.
    win["session_id"] = str(uuid.uuid4())
    win["pending_seed"] = summary
    win.pop("ctx_tokens", None)
    win.pop("needs_compact", None)
    _save_store(cid)
    return True, "ok"

# ── Window management ───────────────────────────────────────────────────────
def win_list(cid: int) -> list[dict]:
    """List all windows for a chat."""
    store = _load_store(cid)
    active = store.get("active", "main")
    result = []
    for name, w in store.get("windows", {}).items():
        result.append({"name": name, "active": name == active, **w})
    return result

def win_switch(cid: int, name: str, workdir: str = None) -> dict:
    """Switch to a window (create if not exists).
    A brand-new window gets its OWN fresh session (not borrowed from another),
    so each forum topic is fully isolated."""
    store = _load_store(cid)
    if name not in store.get("windows", {}):
        store.setdefault("windows", {})[name] = {
            "session_id": str(uuid.uuid4()),   # fresh session, isolated per topic
            "workdir": workdir or WORKDIR,
            "provider": _new_window_provider(cid), "model": MODEL_SLOT,
        }
    elif workdir:
        store["windows"][name]["workdir"] = workdir
    store["active"] = name
    _save_store(cid)
    return store["windows"][name]

def win_close(cid: int, name: str) -> bool:
    """Close a window. Can't close the last one."""
    store = _load_store(cid)
    windows = store.get("windows", {})
    if name not in windows:
        return False
    if len(windows) <= 1:
        return False  # can't close last window
    del windows[name]
    if store.get("active") == name:
        store["active"] = list(windows.keys())[0]
    _save_store(cid)
    return True

# ── Forum Topics (Telegram groups) ──────────────────────────────────────────
# topic_map: {chat_id: {thread_id: window_name}}
_topic_map: dict[int, dict[int, str]] = {}

def _create_topic(chat_id: int, name: str) -> int:
    """Create a forum topic in a Telegram group. Returns thread_id."""
    try:
        r = tg_api("createForumTopic", chat_id=chat_id, name=name)
        tid = r.get("result", {}).get("message_thread_id", 0)
        if tid:
            _topic_map.setdefault(chat_id, {})[tid] = name
            # Save topic map to store
            store = _load_store(chat_id)
            store.setdefault("topic_map", {})[str(tid)] = name
            _save_store(chat_id)
        return tid
    except Exception as e:
        log(f"createForumTopic failed: {e}")
        return 0

def _load_topic_map(cid: int):
    """Load topic map from store."""
    if cid not in _topic_map:
        store = _load_store(cid)
        raw = store.get("topic_map", {})
        _topic_map[cid] = {int(k): v for k, v in raw.items()}

def _get_window_for_thread(cid: int, thread_id: int) -> str | None:
    """Get window name for a forum topic thread_id."""
    _load_topic_map(cid)
    return _topic_map.get(cid, {}).get(thread_id)

# ── Process management ──────────────────────────────────────────────────────
import signal

def _kill_process_tree(proc):
    """Kill a process and ALL its children (MCP servers, subagents, etc.)
    Equivalent to pressing ESC in the Claude Code terminal."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except Exception:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=3)
    except ProcessLookupError:
        pass  # already dead
    except Exception as e:
        # Fallback: at least try to kill the direct process
        try:
            proc.kill()
        except Exception:
            pass

# ── Claude Code CLI wrapper ─────────────────────────────────────────────────
def _session_exists(workdir: str, session_id: str) -> bool:
    """Check if a Claude Code conversation file exists for this session."""
    proj = _cc_project_dir(workdir)
    if not proj:
        return False
    return (proj / f"{session_id}.jsonl").exists()

def _repair_session(workdir: str, session_id: str) -> int:
    """Buang orphan tool_result dari .jsonl sesi (yang tool_use_id-nya tak punya
    pasangan tool_use). Mismatch ini terjadi kalau proses claude ke-kill di tengah
    tool call (via /stop, timeout, crash) → resume di provider Bedrock-based
    (mis. omni) gagal '400 toolResult exceeds toolUse'. Anthropic native memaafkan,
    Bedrock strict. Repair ini bikin sesi valid di SEMUA provider tanpa kehilangan
    konteks. Idempotent + aman (sesi sehat tak disentuh). Returns jumlah baris dibuang."""
    proj = _cc_project_dir(workdir)
    if not proj:
        return 0
    jl = proj / f"{session_id}.jsonl"
    if not jl.exists():
        return 0
    try:
        lines = jl.read_text().splitlines()
    except Exception:
        return 0
    # 1) kumpulkan semua id tool_use yang ada
    use_ids = set()
    for ln in lines:
        try:
            o = json.loads(ln)
        except Exception:
            continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    use_ids.add(b.get("id"))
    # 2) buang block tool_result yang tool_use_id-nya tak ada di use_ids;
    #    kalau pesan jadi kosong (cuma berisi orphan), buang seluruh barisnya.
    out, removed = [], 0
    for ln in lines:
        try:
            o = json.loads(ln)
        except Exception:
            out.append(ln)
            continue
        msg = o.get("message")
        c = (msg or {}).get("content")
        if not isinstance(c, list):
            out.append(ln)
            continue
        kept = [b for b in c
                if not (isinstance(b, dict) and b.get("type") == "tool_result"
                        and b.get("tool_use_id") not in use_ids)]
        if len(kept) == len(c):
            out.append(ln)                 # tak ada orphan di baris ini
            continue
        if not kept:
            removed += 1                   # seluruh baris isinya orphan → drop
            continue
        msg["content"] = kept              # sebagian orphan → sisakan yang valid
        removed += 1
        out.append(json.dumps(o, ensure_ascii=False))
    if removed:
        bak = jl.with_suffix(".jsonl.prerepair.bak")
        try:
            if not bak.exists():
                bak.write_text("\n".join(lines) + "\n")
            jl.write_text("\n".join(out) + "\n")
            log(f"repair_session {session_id[:8]}: buang {removed} orphan tool_result")
        except Exception as e:
            log(f"repair_session {session_id[:8]} gagal tulis: {e}")
            return 0
    return removed

def _set_session_id(chat_id: int, session_id: str):
    """Persist a session_id into the active window."""
    if chat_id in _store:
        store = _store[chat_id]
        active = store.get("active", "main")
        if active in store.get("windows", {}):
            store["windows"][active]["session_id"] = session_id
            save_sess(chat_id)

def _tool_label(name: str, inp: dict) -> str:
    """Human-friendly progress label for a tool_use event."""
    n = (name or "").lower()
    if n in ("bash", "shell"):
        cmd = (inp.get("command") or "")[:48]
        return f"⚙️ `{cmd}`"
    if n in ("edit", "write", "multiedit", "notebookedit"):
        f = inp.get("file_path") or inp.get("path") or ""
        return f"✏️ tulis {Path(f).name or f}"
    if n == "read":
        f = inp.get("file_path") or ""
        return f"📖 baca {Path(f).name or f}"
    if n in ("grep", "glob"):
        return f"🔍 cari {inp.get('pattern','')[:30]}"
    if n in ("webfetch", "websearch"):
        return f"🌐 {n}"
    if n == "task":
        return f"🤖 subagent: {inp.get('description','')[:40]}"
    if n == "todowrite":
        return "📋 update rencana"
    return f"🔧 {name}"

def run_claude(prompt: str, chat_id: int, workdir: str, session_id: str,
               provider: str = None, model: str = None,
               lock_key=None, on_event=None, effort: str = None) -> tuple:
    """Run Claude Code -p with stream-json output for true live streaming.

    Each NDJSON event from stdout is parsed and passed to on_event(ev) the
    instant it arrives — this is authoritative (straight from the process),
    unlike tailing the session .jsonl which can race/disappear and look "dead".
    Returns (result_text, usage_dict)."""
    env = os.environ.copy()
    env["TELEGRAM_BOT_TOKEN"] = TG_TOKEN
    env["TELEGRAM_CHAT_ID"] = str(chat_id)
    bin_path = get_claude_bin(provider)
    _provider_env(env, provider)  # inject base_url/token/model for this provider
    model_slot = model or MODEL_SLOT

    def _cmd(sid, resume):
        flag = "--resume" if resume else "--session-id"
        base = [bin_path, "-p", flag, sid, "--model", model_slot,
                "--output-format", "stream-json", "--verbose",
                "--include-partial-messages",  # token-by-token deltas (FITUR 1)
                "--append-system-prompt", TELE_SYSTEM_PROMPT]
        if effort and effort in EFFORT_LEVELS:
            base += ["--effort", effort]
        base += ["--dangerously-skip-permissions", prompt]
        return base

    resume = _session_exists(workdir, session_id)
    if resume:
        # Auto-repair: buang orphan tool_result sebelum resume, supaya sesi yg
        # sempat korup (proses ke-kill mid-tool) tetap valid di provider Bedrock.
        try:
            _repair_session(workdir, session_id)
        except Exception:
            pass

    for attempt in range(2):
        result_text, usage, err_text = "", {}, ""
        try:
            # start_new_session: own process group so we can kill ALL children
            # (MCP servers, subagents, etc.) — like ESC in terminal.
            proc = subprocess.Popen(_cmd(session_id, resume), cwd=workdir, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
            if lock_key is not None:
                _running_procs[lock_key] = proc

            import threading as _t
            # stderr drained in background (avoids ~64KB pipe-buffer deadlock)
            _err_buf = []
            def _drain_err():
                try:
                    for chunk in iter(lambda: proc.stderr.read(65536), b""):
                        _err_buf.append(chunk)
                except Exception:
                    pass
            t_err = _t.Thread(target=_drain_err, daemon=True); t_err.start()

            # Read stdout line-by-line (NDJSON). Each line → parse → on_event.
            # Runs in a thread so the main loop can enforce cancel/timeout.
            holder = {"result": "", "usage": {}}
            _done = _t.Event()
            def _reader():
                def _emit(e):
                    """Forward raw/synthetic event to on_event; swallow errors."""
                    if on_event:
                        try:
                            on_event(e)
                        except Exception:
                            pass
                try:
                    for raw in proc.stdout:           # blocks per-line; ends at EOF
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue
                        if ev.get("type") == "assistant":
                            # Track true context size (input + cache) — used for
                            # auto-compact. Result-event usage sometimes omits
                            # cache fields, so we keep the max seen as fallback.
                            mu = ev.get("message", {}).get("usage", {}) or {}
                            c = (mu.get("input_tokens", 0)
                                 + mu.get("cache_read_input_tokens", 0)
                                 + mu.get("cache_creation_input_tokens", 0))
                            if c:
                                holder["ctx_seen"] = max(holder.get("ctx_seen", 0), c)
                        if ev.get("type") == "result":
                            holder["result"] = (ev.get("result") or "").strip()
                            u = ev.get("usage", {}) or {}
                            cr = u.get("cache_read_input_tokens", 0)
                            cc = u.get("cache_creation_input_tokens", 0)
                            it = u.get("input_tokens", 0)
                            ctx = it + cr + cc
                            holder["usage"] = {
                                "tokens": (it + u.get("output_tokens", 0)),
                                # true prompt size sent this turn (what drives cost)
                                "context": max(ctx, holder.get("ctx_seen", 0)),
                                "cache_read": cr,
                                "cost": ev.get("total_cost_usd", 0) or 0,
                                "turns": ev.get("num_turns", 0),
                                "ms": ev.get("duration_ms", 0),
                            }
                        # ── Partial-message streaming: unwrap stream_event
                        # deltas into synthetic events so the live feed renders
                        # token-by-token. (Deltas only arrive because of
                        # --include-partial-messages on the claude call.)
                        if ev.get("type") == "stream_event":
                            se = ev.get("event", {}) or {}
                            et = se.get("type")
                            if et == "message_start":
                                # new turn → reset buffers, stream each turn fresh
                                holder["text_buf"] = ""; holder["text_sent"] = 0
                                holder["think_buf"] = ""; holder["think_sent"] = 0
                                _emit({"type": "stream_text", "text": ""})
                                _emit({"type": "stream_think", "text": ""})
                            elif et == "content_block_delta":
                                d = se.get("delta", {}) or {}
                                if d.get("type") == "text_delta":
                                    # Emit SETIAP delta (tanpa gate char): biar
                                    # self.text selalu paling baru. Throttle Telegram
                                    # diurus flusher LiveStream — gate di sini cuma
                                    # bikin token pertama telat. (lebih instan)
                                    holder["text_buf"] = holder.get("text_buf", "") + (d.get("text", "") or "")
                                    _emit({"type": "stream_text", "text": holder["text_buf"]})
                                elif d.get("type") == "thinking_delta":
                                    holder["think_buf"] = holder.get("think_buf", "") + (d.get("thinking", "") or "")
                                    _emit({"type": "stream_think", "text": holder["think_buf"]})
                            elif et == "content_block_stop":
                                # flush the tail so the last partial chunk shows
                                if len(holder.get("text_buf", "")) - holder.get("text_sent", 0) > 0:
                                    holder["text_sent"] = len(holder["text_buf"])
                                    _emit({"type": "stream_text", "text": holder["text_buf"]})
                                if len(holder.get("think_buf", "")) - holder.get("think_sent", 0) > 0:
                                    holder["think_sent"] = len(holder["think_buf"])
                                    _emit({"type": "stream_think", "text": holder["think_buf"]})
                        _emit(ev)
                finally:
                    _done.set()
            t_out = _t.Thread(target=_reader, daemon=True); t_out.start()

            start = time.time()
            # Wait for the reader to finish (process EOF) with cancel + timeout
            while not _done.is_set():
                if lock_key is not None and lock_key in _cancelled:
                    _kill_process_tree(proc)
                    return "⏹ Dibatalkan.", {}
                if time.time() - start > CLAUDE_TIMEOUT:
                    _kill_process_tree(proc)
                    return f"⏰ Timeout (>{CLAUDE_TIMEOUT//60} menit). Coba /reset.", {}
                _done.wait(0.5)
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            t_err.join(timeout=5)
            result_text = _strip_ansi(holder["result"])
            usage = holder["usage"]
            err_text = _strip_ansi((b"".join(_err_buf)).decode("utf-8", "replace").strip())
        except FileNotFoundError:
            return f"❌ Binary tidak ditemukan: {bin_path}", {}
        except Exception as e:
            return f"❌ Error: {e}", {}
        finally:
            if lock_key is not None:
                _running_procs.pop(lock_key, None)

        if result_text:
            if usage:
                p = provider or PROVIDER
                slot = _usage_log.setdefault(p, {"tokens": 0, "cost": 0.0, "calls": 0})
                slot["tokens"] += usage["tokens"]
                slot["cost"] += usage["cost"]
                slot["calls"] += 1
            return result_text, usage

        # Recover from session errors
        if attempt == 0:
            low = err_text.lower()
            if "already in use" in low:
                session_id = str(uuid.uuid4()); resume = False
                _set_session_id(chat_id, session_id); continue
            if "no conversation found" in low or "not found" in low:
                resume = False; continue
        if err_text:
            low = err_text.lower()
            # Overload upstream (mis. z.ai 529) = sisi server provider, bukan
            # bot/config. Surface pesan jelas + arahkan ke /retry, jangan dump
            # error mentah. HANYA di stderr (bukan result_text) supaya jawaban
            # Claude yang kebetulan menyebut "529"/"overloaded" tak salah ganti.
            if ("529" in low or "overloaded" in low
                    or "service may be temporarily" in low):
                return ("⚠️ Provider lagi overload sesaat (HTTP 529 — sisi server "
                        "provider, bukan bot/config kamu). Endpoint-nya sehat "
                        "(barusan dites). Ketik /retry untuk ulang, atau ganti "
                        "model/provider sebentar."), {}
            return f"⚠️ {err_text[:400]}", {}
        return "(kosong — Claude Code tidak mengembalikan output)", {}
    return "❌ Gagal menjalankan Claude Code.", {}

# ── Commands ────────────────────────────────────────────────────────────────
# NOTE: command responses use raw HTML and are sent via send_html() (no md conversion)
START_MSG = """👋 **Halo! Aku Claude Code di Telegram.**

Pakai **tombol di bawah** 👇 untuk navigasi cepat:

💬 **Sesi** — pilih/lanjut percakapan
🪟 **Project** — ganti folder kerja
🔌 **Provider** — ganti AI (claude/zai/deepseek)
🧠 **Model** — ganti model (opus/sonnet/haiku)
📋 **Menu** — aksi cepat (files, git, search…)
🆕 **Sesi Baru** — mulai dari awal

*Atau langsung ketik pesan untuk mulai coding.*
🖥 Provider di sini juga bisa dipakai di terminal: ketik `claude-terminal`.
Ketik /help untuk daftar command lengkap."""

HELP = """📖 **Panduan Lengkap CC-TG**

**💬 Sesi (percakapan Claude Code)**
• `/resume` — pilih sesi via tombol (judul asli)
• `/resume <id>` — lanjut sesi by ID
• `/resume <provider> <id>` — lanjut + ganti AI
• `/reset` — mulai sesi baru (fresh)
• `/exit` — keluar dari sesi
Di list sesi: ✏️ rename · 🗑️ hapus · 🧹 hapus kosong

**📂 Project / Folder**
• `/cd /path/project` — buka folder (auto jadi project)
• `/w` — daftar semua project
• `/w <nama>` — buat/pindah project
• `/pwd` — lihat folder aktif
Di grup pakai Topics: tiap topic = project terpisah

**🔌 Provider & Model** (per-project, gak bentrok)
• `/provider` — pilih AI via tombol
• `/provider <nama>` — claude/zai/deepseek/dll
• `/provider add <nama> <url> <token> <opus> [sonnet] [haiku]` — tambah provider baru
• `/provider del <nama>` — hapus provider
• `/provider info <nama>` — detail provider
• `/provider reload` — refresh dari Claude Hub
• `/model` / `/model <slot>` — ganti model (opus/sonnet/haiku)

**⏱ Saat Claude bekerja**
• Progress live tiap step (bash, tulis file, cari…)
• ⏹ Stop / `/stop` — batalin task berjalan
• Notif otomatis kalau task >2 menit

**📎 Kirim file**
• Kirim foto/dokumen → otomatis dianalisa Claude
• Caption jadi instruksi

**💰 Pemakaian & Otomasi**
• `/cost` — token & biaya per provider
• `/cron` — daftar jadwal otomatis
• `/cron add 07:00 <tugas>` — jadwal harian
• `/cron del <n>` — hapus jadwal

**🆕 Sesi & window**
• `/new [nama]` — window/sesi baru (fresh context)
• `/title <judul>` — beri judul sesi sekarang
• `/agents` (`/tasks`) — lihat window/task yang lagi jalan

**⏯ Kontrol saat kerja**
• `/queue <prompt>` (`/q`) — antri; jalan setelah task sekarang kelar
• `/queue` — lihat isi antrian
• `/background <prompt>` (`/bg`) — jalan paralel di window terpisah
• `/retry` — ulang pesan terakhir di window ini
• `/verbose` — toggle tampil semua step (teks+thinking) live
• `/stop` — batalin task berjalan

**ℹ️ Info & sistem**
• `/usage` — pemakaian token/biaya (alias `/cost`)
• `/whoami` — cek level akses kamu
• `/version` (`/v`) — versi Claude Code
• `/yolo` — status mode YOLO
• `/update` — update bot ke versi terbaru dari GitHub + restart
• `/restart` — restart bot (auto-up via systemd)

	**🧠 Reasoning & konteks**
	• `/effort [level]` — atur kedalaman mikir (low→max) · `/verbose` — tampil thinking live
	• `/compress` (`/compact`) `[here N]` — ringkas konteks percakapan (hemat token)
	• `/undo [N]` — mundur N turn terakhir (default 1)
	• `/clear` — bersihkan layar, sesi baru
	• `/effort <level>` — low/medium/high/xhigh/max

**⚙️ Lainnya**
• `/menu` — tombol aksi cepat (files/git/dll)
• `/status` — info sesi/provider/model sekarang
• `/start` — panduan singkat + tombol

**🖥 Pakai di Terminal juga**
Provider yang kamu tambah di sini bisa langsung dipakai di terminal!
• Jalankan: `claude-terminal` → muncul menu pilih provider
• `claude-terminal <nama>` — langsung (mis. `claude-terminal omni`)
• Sumbernya sama (`~/.cc-tg/providers.json`) — tambah di bot, otomatis muncul di terminal.

💡 *Tombol di bar bawah = akses cepat tanpa ngetik.*
💡 *Ketik pesan biasa = langsung ke Claude Code.*"""

# ── Reply Keyboard (persistent bottom bar) ───────────────────────────────────
# Reply keyboard (bar bawah layar, selalu kelihatan) — tombol-first UX.
# Label diterjemahkan ke command via QUICK_BTN di process().
REPLY_KB = {
    "keyboard": [
        [{"text": "💬 Sesi"}, {"text": "🪟 Project"}],
        [{"text": "🔌 Provider"}, {"text": "🧠 Model"}, {"text": "🎯 Effort"}],
        [{"text": "📋 Menu"}, {"text": "⏹ Stop"}],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": False,
    "is_persistent": True,
}

# Map label tombol reply keyboard → command yang dijalankan
QUICK_BTN = {
    "💬 Sesi": "/resume",
    "🪟 Project": "/w",
    "🔌 Provider": "/provider",
    "🧠 Model": "_MODELKB_",
    "🎯 Effort": "_EFFORTKB_",
    "📋 Menu": "/menu",
    "⏹ Stop": "/stop",
}

# ── Menu (inline keyboard) ──────────────────────────────────────────────────
# Fokus: aksi cepat coding + pengaturan. Navigasi sesi/project/provider ada di
# reply keyboard (bar bawah).
MENU_KB = {"inline_keyboard": [
    [{"text": "📊 Disk", "callback_data": "m_disk"},
     {"text": "📊 Status", "callback_data": "m_status"}],
    [{"text": "💰 Usage", "callback_data": "m_usage"},
     {"text": "🤖 Agents", "callback_data": "m_agents"},
     {"text": "⏰ Cron", "callback_data": "m_cron"}],
    [{"text": "🆕 Sesi Baru", "callback_data": "m_reset"},
     {"text": "⬇️ Update", "callback_data": "m_update"}],
    [{"text": "🚪 Exit", "callback_data": "m_exit"},
     {"text": "❓ Bantuan", "callback_data": "m_help"},
     {"text": "✖️ Tutup", "callback_data": "m_close"}],
]}

MODEL_KB = {"inline_keyboard": [
    [{"text": "🔥 Opus (heavy)", "callback_data": "set_opus"},
     {"text": "⚡ Sonnet (balanced)", "callback_data": "set_sonnet"},
     {"text": "💨 Haiku (fast)", "callback_data": "set_haiku"}],
    [{"text": "← Back", "callback_data": "m_back"},
     {"text": "✖️ Tutup", "callback_data": "m_close"}],
]}

EFFORT_KB = {"inline_keyboard": [
    [{"text": "💨 Low", "callback_data": "eff_low"},
     {"text": "⚖️ Medium", "callback_data": "eff_medium"},
     {"text": "🔥 High", "callback_data": "eff_high"}],
    [{"text": "🚀 XHigh", "callback_data": "eff_xhigh"},
     {"text": "🧠 Max", "callback_data": "eff_max"},
     {"text": "♻️ Default", "callback_data": "eff_default"}],
    [{"text": "✖️ Tutup", "callback_data": "m_close"}],
]}

def _build_provider_kb(cid: int) -> dict:
    """Build inline keyboard for provider selection."""
    rows = []
    row = []
    cur = _win_provider(cid)   # highlight = provider window AKTIF, bukan global
    # "claude" = native default (selalu pertama). PROVIDERS juga punya 'claude'
    # via setdefault (line ~189) → exclude dari sorted biar tombolnya TIDAK dobel.
    for name in ["claude"] + sorted(n for n in PROVIDERS.keys() if n != "claude"):
        marker = "✅ " if name == cur else "🔌 "
        row.append({"text": f"{marker}{name}", "callback_data": f"pvmgr:{name}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "➕ Tambah (wizard)", "callback_data": "pv_add_start"},
                 {"text": "📋 Paste sekaligus", "callback_data": "pv_paste_start"}])
    rows.append([{"text": "← Back", "callback_data": "m_back"},
                 {"text": "✖️ Tutup", "callback_data": "m_close"}])
    return {"inline_keyboard": rows}

# cache daftar model per (cid, provider) untuk edit-via-tombol
_pv_model_cache: dict = {}

def _provider_card(cid: int, name: str) -> tuple:
    """(teks, keyboard) kartu kelola satu provider — info + tombol aksi."""
    cur = _win_provider(cid)   # highlight per-window aktif, bukan global PROVIDER
    if name == "claude":
        text = "🔌 *claude* — native Anthropic\nLogin sendiri (OAuth), tanpa override."
        kb = {"inline_keyboard": [
            [{"text": ("✅ Aktif" if cur == name else "▶️ Pakai"), "callback_data": f"pv_{name}"}],
            [{"text": "← Provider", "callback_data": "m_provider"}, {"text": "✖️ Tutup", "callback_data": "m_close"}],
        ]}
        return text, kb
    inf = _provider_info(name)
    tok = inf.get("token", "")
    tokm = (tok[:5] + "…" + tok[-4:]) if len(tok) > 12 else "***"
    active = " ✅ *AKTIF*" if cur == name else ""
    text = (f"🔌 *{name}*{active}\n"
            f"📡 `{inf.get('base_url','?')}`\n"
            f"🔑 `{tokm}`\n"
            f"🧠 opus=`{inf.get('opus','?')}`\n"
            f"      sonnet=`{inf.get('sonnet','?')}`\n"
            f"      haiku=`{inf.get('haiku','?')}`")
    kb = {"inline_keyboard": [
        [{"text": ("✅ Aktif" if PROVIDER == name else "▶️ Pakai"), "callback_data": f"pv_{name}"},
         {"text": "🧪 Test", "callback_data": f"pvtest:{name}"}],
        [{"text": "✏️ Edit", "callback_data": f"pvedit:{name}"},
         {"text": "📋 List model", "callback_data": f"pvmodels:{name}"}],
        [{"text": "🔑 Lihat key", "callback_data": f"pvkey:{name}"},
         {"text": "✏️ Ganti nama", "callback_data": f"pvren:{name}"}],
        [{"text": "🗑️ Hapus", "callback_data": f"pvdel:{name}"}],
        [{"text": "← Provider", "callback_data": "m_provider"}, {"text": "✖️ Tutup", "callback_data": "m_close"}],
    ]}
    return text, kb

def _pv_ask(cid: int, mid: int = None):
    """Ask the current wizard step's question."""
    st = _pending_provider.get(cid)
    if not st:
        return
    step_idx = st["step"]
    if step_idx >= len(_PV_STEPS):
        return
    key, question = _PV_STEPS[step_idx]
    # Untuk slot model, kalau auto-load berhasil, tampilkan daftar model bernomor.
    models = st.get("models") or []
    if key in ("opus", "sonnet", "haiku") and models:
        listing = "\n".join(f"`{i+1}.` {m}" for i, m in enumerate(models[:40]))
        more = f"\n_…dan {len(models)-40} lagi_" if len(models) > 40 else ""
        hint = ("ketik *nomor* dari daftar, atau nama model langsung"
                + ("" if key == "opus" else ", atau `-` samakan slot sebelumnya"))
        question = f"{question.splitlines()[0]}\n\n📋 *Model tersedia:*\n{listing}{more}\n\n_{hint}_"
    text = (f"➕ *Tambah Provider* ({step_idx+1}/{len(_PV_STEPS)})\n\n{question}\n\n"
            f"_Ketik jawaban, atau tekan Batal._")
    cancel_kb = {"inline_keyboard": [[{"text": "✖️ Batal", "callback_data": "pv_cancel"}]]}
    if mid:
        edit_md(cid, mid, text, reply_markup=cancel_kb)
    else:
        tg_api("sendMessage", chat_id=cid, text=_to_md(text),
               parse_mode="MarkdownV2", reply_markup=cancel_kb)

MENU_PROMPTS = {
    # Tombol cepat → jalankan command shell LANGSUNG di bot (tanpa Claude).
    # Lebih cepat + anti-error "Invalid tool use format" dari provider Bedrock.
    "m_disk":    ("📊 Disk",  "df -h . ; echo ; echo '— 10 folder terbesar —' ; du -sh ./* 2>/dev/null | sort -rh | head -10"),
}

def handle_callback(cb: dict):
    """Handle inline keyboard button press."""
    global MODEL_SLOT, PROVIDER
    cid = cb["message"]["chat"]["id"]
    uid = cb["from"]["id"]
    data = cb.get("data", "")
    cb_id = cb["id"]
    mid = cb["message"]["message_id"]

    if OWNER_IDS and uid not in OWNER_IDS:
        try:
            tg_api("answerCallbackQuery", callback_query_id=cb_id, text="🚫 Unauthorized")
        except Exception:
            pass
        return

    try:
        tg_api("answerCallbackQuery", callback_query_id=cb_id)
    except Exception:
        pass  # callback expired (bot was offline)

    # Resolve forum-topic → window FIRST (sama seperti jalur pesan teks). Tanpa
    # ini, tombol /provider /model /effort di dalam topic nulis ke window AKTIF
    # (mis. 'main'/bot utama) bukan window topic → "provider topic ikut bot
    # utama". Set active window sesuai topic sebelum branch mana pun pakai
    # load_sess(cid) (yang baca window aktif).
    _cb_thread = cb["message"].get("message_thread_id", 0)
    if _cb_thread:
        _cb_win = _get_window_for_thread(cid, _cb_thread)
        if not _cb_win:
            _cb_win = f"topic-{_cb_thread}"
            _topic_map.setdefault(cid, {})[_cb_thread] = _cb_win
            _cb_store = _load_store(cid)
            _cb_store.setdefault("topic_map", {})[str(_cb_thread)] = _cb_win
            _save_store(cid)
        win_switch(cid, _cb_win)

    # Stop a running task (#2 interrupt)
    if data.startswith("stop:"):
        win_name = data[5:]
        lock_key = (cid, win_name)
        _cancelled.add(lock_key)
        proc = _running_procs.get(lock_key)
        if proc:
            _kill_process_tree(proc)
        # Immediately free the lock so user can send new messages
        _busy.discard(lock_key)
        try:
            tg_api("editMessageText", chat_id=cid, message_id=mid,
                   text="⏹ Task dihentikan. Kirim pesan baru untuk lanjut.",
                   parse_mode="")
        except Exception:
            pass
        return

    # MULTIPICK: toggle an option on/off
    if data.startswith("mpick:"):
        try:
            _, tok, idx_s = data.split(":", 2)
            idx = int(idx_s)
        except ValueError:
            return
        entry = _pending_multipick.get((cid, tok))
        if not entry:
            return
        options, selected = entry
        if not (0 <= idx < len(options)):
            return
        # Toggle
        if idx in selected:
            selected.discard(idx)
        else:
            selected.add(idx)
        # Rebuild keyboard with updated toggles
        rows = [[{"text": f"{'☑️' if j in selected else '☐'} {opt[:38]}",
                  "callback_data": f"mpick:{tok}:{j}"}]
                for j, opt in enumerate(options)]
        rows.append([{"text": f"✅ Selesai ({len(selected)} dipilih)",
                      "callback_data": f"mpdone:{tok}"}])
        kb = {"inline_keyboard": rows}
        try:
            tg_api("editMessageReplyMarkup", chat_id=cid, message_id=mid,
                   reply_markup=kb)
        except Exception:
            pass
        return

    # MULTIPICK: confirm and submit all selected options
    if data.startswith("mpdone:"):
        _, tok = data.split(":", 1)
        entry = _pending_multipick.pop((cid, tok), None)
        if not entry:
            try:
                tg_api("editMessageReplyMarkup", chat_id=cid, message_id=mid,
                       reply_markup={"inline_keyboard": []})
            except Exception:
                pass
            return
        options, selected = entry
        if not selected:
            # User clicked done but nothing selected
            try:
                tg_api("answerCallbackQuery", callback_query_id=cb_id,
                       text="Pilih dulu minimal satu opsi!", show_alert=True)
            except Exception:
                pass
            return
        # Build comma-separated selection
        chosen = [options[i] for i in sorted(selected)]
        choice_text = ", ".join(chosen)
        thread_id = cb["message"].get("message_thread_id", 0)
        try:
            tg_api("editMessageText", chat_id=cid, message_id=mid,
                   text=_to_md(f"✅ Dipilih: *{choice_text}*"),
                   parse_mode="MarkdownV2")
        except Exception:
            pass
        synth = {"message": {"chat": {"id": cid, "type": cb["message"]["chat"].get("type", "private")},
                             "from": {"id": uid}, "message_id": mid,
                             "text": choice_text}}
        if thread_id:
            synth["message"]["message_thread_id"] = thread_id
        import threading as _t
        _t.Thread(target=_process_safe, args=(synth,), daemon=True).start()
        return

    # User clicked a PICK option → feed the choice back to Claude as a message
    if data.startswith("pick:"):
        try:
            _, tok, idx_s = data.split(":", 2)
            idx = int(idx_s)
        except ValueError:
            return
        options = _pending_pick.pop((cid, tok), None)
        if not options or not (0 <= idx < len(options)):
            try:
                tg_api("editMessageReplyMarkup", chat_id=cid, message_id=mid, reply_markup={"inline_keyboard": []})
            except Exception:
                pass
            send_msg(cid, "⚠️ Pilihan kadaluarsa. Ketik jawabanmu langsung.")
            return
        choice = options[idx]
        thread_id = cb["message"].get("message_thread_id", 0)
        # Lock the chosen option into the message (remove buttons)
        try:
            tg_api("editMessageText", chat_id=cid, message_id=mid,
                   text=_to_md(f"✅ Kamu pilih: *{choice}*"), parse_mode="MarkdownV2")
        except Exception:
            pass
        # Dispatch the choice as if the user typed it (reuses full pipeline)
        synth = {"message": {"chat": {"id": cid, "type": cb["message"]["chat"].get("type", "private")},
                             "from": {"id": uid}, "message_id": mid,
                             "text": choice}}
        if thread_id:
            synth["message"]["message_thread_id"] = thread_id
        import threading as _t
        _t.Thread(target=_process_safe, args=(synth,), daemon=True).start()
        return

    # ── Provider management cards (lihat/edit/test/hapus/rename) ──────────────
    if data.startswith("pvmgr:"):
        name = data[6:]
        if name != "claude" and name not in PROVIDERS:
            edit_md(cid, mid, f"❌ Provider `{name}` tidak ada."); return
        t, kb = _provider_card(cid, name)
        edit_md(cid, mid, t, reply_markup=kb); return
    if data.startswith("pvtest:"):
        name = data[7:]; inf = _provider_info(name)
        def _t():
            ok, msg = _test_endpoint(inf.get("base_url",""), inf.get("token",""))
            send_msg(cid, f"{'✅' if ok else '❌'} *{name}*: {msg}")
        threading.Thread(target=_t, daemon=True).start()
        return
    if data.startswith("pvmodels:"):
        name = data[9:]; inf = _provider_info(name)
        def _m():
            ok, ids, info = _fetch_models(inf.get("base_url",""), inf.get("token",""))
            if not ok:
                send_msg(cid, f"❌ *{name}*: {info}"); return
            lst = "\n".join(f"• `{x}`" for x in ids[:50])
            send_msg(cid, f"📋 *{len(ids)} model di {name}:*\n{lst}")
        threading.Thread(target=_m, daemon=True).start()
        return
    if data.startswith("pvedit:"):
        name = data[7:]
        kb = {"inline_keyboard": [
            [{"text": "📡 URL/endpoint", "callback_data": f"pvfield:{name}:base_url"},
             {"text": "🔑 Token/API key", "callback_data": f"pvfield:{name}:token"}],
            [{"text": "🧠 opus", "callback_data": f"pvslot:{name}:opus"},
             {"text": "⚖️ sonnet", "callback_data": f"pvslot:{name}:sonnet"},
             {"text": "💨 haiku", "callback_data": f"pvslot:{name}:haiku"}],
            [{"text": "← Kembali", "callback_data": f"pvmgr:{name}"}],
        ]}
        edit_md(cid, mid, f"✏️ *Edit {name}* — pilih yang mau diganti:\n"
                          f"_URL & token: ketik nilai baru. Model: pilih dari daftar._", reply_markup=kb)
        return
    if data.startswith("pvfield:"):
        try: _, name, field = data.split(":", 2)
        except ValueError: return
        if field not in ("base_url", "token"):
            return
        _pending_provider[cid] = {"mode": "field", "name": name, "field": field}
        label = "URL/endpoint" if field == "base_url" else "token/API key"
        edit_md(cid, mid, f"✏️ Ketik *{label}* baru untuk `{name}`.\n/batal buat batal.")
        return
    if data.startswith("pvkey:"):
        name = data[6:]
        inf = _provider_info(name)
        tok = inf.get("token", "")
        if not tok:
            send_msg(cid, f"`{name}` nggak punya token (native/kosong).")
            return
        # kirim full key di code-block biar gampang di-copy (bot owner-only)
        tg_api("sendMessage", chat_id=cid,
               text=_to_md(f"🔑 *API key {name}* (tap buat copy):\n`{tok}`"),
               parse_mode="MarkdownV2")
        return
    if data.startswith("pvslot:"):
        try: _, name, slot = data.split(":", 2)
        except ValueError: return
        inf = _provider_info(name)
        send_msg(cid, f"📋 Ambil daftar model {name}…")
        def _s():
            ok, ids, info = _fetch_models(inf.get("base_url",""), inf.get("token",""))
            if not ok:
                send_msg(cid, f"❌ {info}"); return
            _pv_model_cache[(cid, name)] = ids
            rows, row = [], []
            for i, mdl in enumerate(ids[:30]):
                row.append({"text": mdl[:24], "callback_data": f"pvset:{name}:{slot}:{i}"})
                if len(row) == 2: rows.append(row); row = []
            if row: rows.append(row)
            rows.append([{"text": "← Batal", "callback_data": f"pvmgr:{name}"}])
            tg_api("sendMessage", chat_id=cid,
                   text=_to_md(f"Pilih model untuk *{slot}* di `{name}`:"),
                   parse_mode="MarkdownV2", reply_markup={"inline_keyboard": rows})
        threading.Thread(target=_s, daemon=True).start()
        return
    if data.startswith("pvset:"):
        try: _, name, slot, idx_s = data.split(":", 3); idx = int(idx_s)
        except ValueError: return
        ids = _pv_model_cache.get((cid, name)) or []
        if not (0 <= idx < len(ids)):
            edit_md(cid, mid, "⚠️ Daftar kadaluarsa, buka Edit lagi."); return
        chosen = ids[idx]
        ok, msg = _provider_save(name, None, None, chosen if slot=="opus" else None,
                                 chosen if slot=="sonnet" else None,
                                 chosen if slot=="haiku" else None)
        if ok:
            t, kb = _provider_card(cid, name)
            edit_md(cid, mid, f"✅ {slot} → `{chosen}`\n\n{t}", reply_markup=kb)
        else:
            edit_md(cid, mid, f"❌ Gagal: {msg}")
        return
    if data.startswith("pvdel:"):
        name = data[6:]
        if name == "claude":
            edit_md(cid, mid, "❌ Provider native `claude` gak bisa dihapus."); return
        kb = {"inline_keyboard": [
            [{"text": "🗑️ Ya, hapus", "callback_data": f"pvdelok:{name}"},
             {"text": "← Batal", "callback_data": f"pvmgr:{name}"}]]}
        edit_md(cid, mid, f"🗑️ Hapus provider `{name}`? Yakin?", reply_markup=kb)
        return
    if data.startswith("pvdelok:"):
        name = data[8:]
        ok = _provider_delete(name)
        edit_md(cid, mid, f"🗑️ Provider `{name}` dihapus." if ok else f"❌ Gagal hapus `{name}`.",
                reply_markup=_build_provider_kb(cid) if ok else None)
        return
    if data.startswith("pvren:"):
        name = data[6:]
        if name == "claude":
            edit_md(cid, mid, "❌ Native `claude` gak bisa di-rename."); return
        _pending_provider[cid] = {"mode": "rename", "old": name}
        edit_md(cid, mid, f"✏️ Ketik *nama baru* untuk `{name}` (huruf kecil/angka/dash). /batal buat batal.")
        return

    # Actions that go through Claude Code
    if data in MENU_PROMPTS:
        label, shell_cmd = MENU_PROMPTS[data]
        sess = load_sess(cid)
        wd = sess["workdir"]
        try:
            r = subprocess.run(["bash", "-lc", shell_cmd], cwd=wd,
                               capture_output=True, text=True, timeout=20)
            out = _strip_ansi((r.stdout or "") + (r.stderr or "")).rstrip()
        except Exception as e:
            out = f"(gagal: {str(e)[:120]})"
        if not out:
            out = "(kosong)"
        if len(out) > 3500:
            out = out[:3500] + "\n…(dipotong)"
        body = f"{label} · `{wd}`\n\n```\n{out}\n```"
        try:
            tg_api("deleteMessage", chat_id=cid, message_id=mid)
        except Exception:
            pass
        send_msg(cid, body)
        return

    # Local actions
    if data == "m_status":
        sess = load_sess(cid)
        send_msg(cid, (
            f"📊 **Status**\n\n"
            f"Provider: `{sess.get('provider', DEFAULT_PROVIDER)}`\n"
            f"Model: `{sess.get('model', MODEL_SLOT)}`\n"
            f"Folder: `{sess['workdir']}`\n"
            f"Sesi: `{sess['session_id'][:8]}`"
        ))
    elif data == "m_usage":
        send_msg(cid, cmd(cid, "/usage", None) or "💰 Belum ada pemakaian.")
    elif data == "m_agents":
        send_msg(cid, cmd(cid, "/agents", None) or "🤖 Tidak ada task berjalan.")
    elif data == "m_cron":
        send_msg(cid, cmd(cid, "/cron", None) or "⏰ Belum ada jadwal.")
    elif data == "m_model":
        edit_md(cid, mid, "⚙️ Pilih model:", reply_markup=MODEL_KB)
    elif data == "m_effort":
        cur = load_sess(cid).get("effort") or "default"
        edit_md(cid, mid, f"🎯 Effort level (aktif: `{cur}`)\nMakin tinggi = mikir lebih dalam, lebih lama/mahal.",
                reply_markup=EFFORT_KB)
    elif data in ("set_opus", "set_sonnet", "set_haiku"):
        slot = data.replace("set_", "")
        sess = load_sess(cid)
        sess["model"] = slot           # per-window ONLY — jangan sentuh global MODEL_SLOT
        save_sess(cid)
        win = _load_store(cid).get("active", "main")
        edit_md(cid, mid, f"🔄 Model window **{win}** → `{slot}`\n\nSesi tetap sama. Kirim pesan untuk lanjut.")
    elif data.startswith("eff_"):
        lvl = data[4:]
        sess = load_sess(cid)
        win = _load_store(cid).get("active", "main")
        if lvl == "default":
            sess.pop("effort", None)
            save_sess(cid)
            edit_md(cid, mid, f"🎯 Effort window **{win}** → *default* (Claude Code yang atur).\n\nKirim pesan untuk lanjut.")
        elif lvl in EFFORT_LEVELS:
            sess["effort"] = lvl
            save_sess(cid)
            edit_md(cid, mid, f"🎯 Effort window **{win}** → `{lvl}`\n\n(makin tinggi = mikir lebih dalam, lebih lama/mahal)\nKirim pesan untuk lanjut.")
    elif data == "m_provider":
        sess = load_sess(cid)
        edit_md(cid, mid, f"🔌 Pilih provider (window ini: `{sess.get('provider', DEFAULT_PROVIDER)}`):",
                reply_markup=_build_provider_kb(cid))
    elif data == "pv_add_start":
        # Start the add-provider wizard
        _pending_provider[cid] = {"step": 0, "data": {}}
        _pv_ask(cid, mid)
    elif data == "pv_paste_start":
        # Mode paste: pesan berikutnya di-parse sebagai config provider sekaligus
        _pending_provider[cid] = {"mode": "paste"}
        edit_md(cid, mid,
                "📋 *Tempel config provider sekarang* (boleh multi-baris):\n\n"
                "`name=zai`\n`base_url=https://api.z.ai/api/anthropic`\n`token=sk-xxx`\n"
                "`opus=glm-4.6`\n`sonnet=glm-4.6`\n`haiku=glm-4.5-air`\n\n"
                "_Model boleh dikosongin — bot auto-ambil dari endpoint. Ketik /batal buat batal._")
    elif data == "pv_cancel":
        _pending_provider.pop(cid, None)
        edit_md(cid, mid, "↩️ Tambah provider dibatalkan.")
    elif data.startswith("pv_") and len(data) > 3:
        name = data[3:]
        if name == "claude" or name in PROVIDERS:
            sess = load_sess(cid)
            sess["provider"] = name    # per-window ONLY — jangan sentuh global PROVIDER
            save_sess(cid)
            win = _load_store(cid).get("active", "main")
            edit_md(cid, mid, f"✅ Provider window **{win}** → `{name}`\n\nKirim pesan untuk lanjut.")
        else:
            edit_md(cid, mid, f"❌ Provider `{name}` tidak ada")
    elif data.startswith("rs_"):
        # Resume a session by cached index
        try:
            idx = int(data[3:])
        except ValueError:
            return
        sessions = _resume_sessions(cid)
        if 0 <= idx < len(sessions):
            s = sessions[idx]
            sess = load_sess(cid)
            sess["session_id"] = s["id"]
            save_sess(cid)
            label = s["summary"] if s["summary"] else "(tanpa judul)"
            hist = _session_recent_history(sess["workdir"], s["id"], n_pairs=3)
            msg = (f"✅ *Lanjut sesi*\n\n"
                   f"💬 {label}\n"
                   f"🕐 {_rel_time(s.get('mtime', 0))} · `{s['id'][:8]}` · `{PROVIDER}`\n")
            if hist:
                msg += f"\n*📜 Obrolan terakhir:*\n{hist}\n"
            msg += "\nKirim pesan untuk lanjut."
            edit_md(cid, mid, msg)
        else:
            edit_md(cid, mid, "❌ Sesi tidak ditemukan (mungkin sudah refresh). Ketik /resume lagi.")
    elif data.startswith("rspage_"):
        # Pagination for resume list
        try:
            page = int(data[7:])
        except ValueError:
            return
        sessions = _resume_sessions(cid)
        sess = load_sess(cid)
        kb = _build_resume_kb(cid, sessions, sess["session_id"], page=page)
        try:
            tg_api("editMessageReplyMarkup", chat_id=cid, message_id=mid,
                   reply_markup=_clean_kb(kb))
        except Exception:
            pass
    elif data.startswith("rsren_"):
        # Start rename flow — wait for the next text message as the new title
        try:
            idx = int(data[6:])
        except ValueError:
            return
        sessions = _resume_sessions(cid)
        if not (0 <= idx < len(sessions)):
            edit_md(cid, mid, "❌ Sesi tidak ditemukan. Ketik /resume lagi.")
            return
        s = sessions[idx]
        _pending_rename[cid] = s["id"]
        cur = (s.get("title") or s.get("summary") or "(tanpa judul)").strip()
        edit_md(cid, mid,
                f"✏️ *Ganti nama sesi*\n\n"
                f"Nama sekarang: {cur}\n"
                f"`{s['id'][:8]}`\n\n"
                f"Ketik *nama baru* untuk sesi ini 👇\n"
                f"(atau ketik /batal untuk membatalkan)")
    elif data.startswith("rsdel_"):
        # Ask confirmation before deleting a session
        try:
            idx = int(data[6:])
        except ValueError:
            return
        sessions = _resume_sessions(cid)
        if not (0 <= idx < len(sessions)):
            edit_md(cid, mid, "❌ Sesi tidak ditemukan. Ketik /resume lagi.")
            return
        s = sessions[idx]
        label = (s.get("title") or s.get("summary") or s["id"][:8]).strip()
        confirm_kb = {"inline_keyboard": [[
            {"text": "🗑️ Ya, hapus permanen", "callback_data": f"rsdelok_{idx}"},
            {"text": "↩️ Batal", "callback_data": "rsdelno"},
        ]]}
        edit_md(cid, mid,
                f"⚠️ *Hapus sesi ini?*\n\n"
                f"💬 {label}\n"
                f"🕐 {_rel_time(s.get('mtime', 0))} · `{s['id'][:8]}`\n\n"
                f"File sesi akan *dihapus permanen* dan tidak bisa dikembalikan.",
                reply_markup=confirm_kb)
    elif data.startswith("rsdelok_"):
        # Execute deletion
        try:
            idx = int(data[8:])
        except ValueError:
            return
        sessions = _resume_sessions(cid)
        if not (0 <= idx < len(sessions)):
            edit_md(cid, mid, "❌ Sesi tidak ditemukan. Ketik /resume lagi.")
            return
        s = sessions[idx]
        sess = load_sess(cid)
        ok, freed = _delete_session(sess["workdir"], s["id"])
        label = (s.get("title") or s.get("summary") or s["id"][:8]).strip()
        if ok:
            # If we deleted the active session, start a fresh one
            if s["id"] == sess["session_id"]:
                new_session(cid)
            # Refresh list & show updated keyboard
            fresh = _cc_sessions(sess["workdir"])
            if fresh:
                edit_md(cid, mid,
                        f"🗑️ *Sesi dihapus* ({_fmt_size(freed)} dibebaskan)\n\n"
                        f"💬 {label}\n\n" + _resume_msg_text(cid, fresh),
                        reply_markup=_clean_kb(_build_resume_kb(cid, fresh, sess["session_id"], 0)))
            else:
                edit_md(cid, mid, f"🗑️ Sesi dihapus. Tidak ada sesi lagi di folder ini.")
        else:
            edit_md(cid, mid, f"❌ Gagal hapus sesi `{s['id'][:8]}`.")
    elif data == "rsdelno":
        # Cancel deletion — back to resume list
        sess = load_sess(cid)
        sessions = _resume_sessions(cid)
        edit_md(cid, mid, _resume_msg_text(cid, sessions),
                reply_markup=_clean_kb(_build_resume_kb(cid, sessions, sess["session_id"], 0)))
    elif data == "rsnop":
        pass  # page indicator button — do nothing
    elif data == "rscleanup":
        sess = load_sess(cid)
        n, freed = _cleanup_empty_sessions(sess["workdir"])
        sessions = _cc_sessions(sess["workdir"])
        if n:
            head = f"🧹 *{n} sesi kosong dihapus* ({_fmt_size(freed)} dibebaskan)\n\n"
        else:
            head = "✨ Tidak ada sesi kosong.\n\n"
        if sessions:
            edit_md(cid, mid, head + _resume_msg_text(cid, sessions),
                    reply_markup=_clean_kb(_build_resume_kb(cid, sessions, sess["session_id"], 0)))
        else:
            edit_md(cid, mid, head + "Tidak ada sesi lagi.")
    elif data == "m_reset":
        new_session(cid)
        edit_md(cid, mid, "🆕 Sesi baru dimulai (fresh context).\nKirim pesan untuk mulai.")
    elif data == "m_exit":
        _store.pop(cid, None)
        p = SESS_DIR / f"{cid}.json"
        p.unlink(missing_ok=True)
        edit_md(cid, mid, "🚪 Keluar dari sesi.\nKetik pesan atau /start untuk mulai lagi.")
    elif data == "m_help":
        send_msg(cid, HELP)
    elif data == "m_update":
        # Tombol Update → jalankan update.sh (pull GitHub + restart), async.
        def _do_update_btn():
            try:
                up = BOT_DIR / "update.sh"
                if not up.exists():
                    send_msg(cid, "❌ update.sh tidak ada. Manual: `cd ~/.cc-tg && git pull`")
                    return
                r = subprocess.run(["bash", str(up)], cwd=str(BOT_DIR),
                                   capture_output=True, text=True, timeout=180)
                out = _strip_ansi((r.stdout or "") + (r.stderr or "")).strip()
                tail = "\n".join(out.splitlines()[-8:])[:1200]
                if "Sudah versi terbaru" in out:
                    send_msg(cid, f"✅ *Sudah versi terbaru.*\n\n```\n{tail}\n```")
                    return
                send_msg(cid, f"🔄 *Update selesai* — bot restart pakai versi baru.\n\n```\n{tail}\n```")
                time.sleep(1); os._exit(0)
            except subprocess.TimeoutExpired:
                send_msg(cid, "⏰ Update timeout (>3 menit).")
            except Exception as e:
                send_msg(cid, f"❌ Update gagal: {str(e)[:200]}")
        try:
            tg_api("deleteMessage", chat_id=cid, message_id=mid)
        except Exception:
            pass
        send_msg(cid, "⬇️ Mengambil update dari GitHub… (tunggu ~10-30 detik)")
        log("Update requested via MENU button")
        threading.Thread(target=_do_update_btn, daemon=True).start()
    elif data == "m_back":
        edit_md(cid, mid, "⚡ **Aksi cepat** — pilih di bawah:", reply_markup=MENU_KB)
    elif data in ("m_close", "close"):
        # Universal "tutup" — hapus pesan menu biar chat bersih
        try:
            tg_api("deleteMessage", chat_id=cid, message_id=mid)
        except Exception:
            # Kalau gagal hapus (mis. terlalu lama), minimal buang tombol
            try:
                edit_md(cid, mid, "✖️ Ditutup.")
            except Exception:
                pass

# ── Claude Code session discovery ────────────────────────────────────────────
def _cc_project_dir(workdir: str) -> Path | None:
    """Find Claude Code project dir for a given working directory.
    Auto-detect: cari slug yang exists di ~/.claude/projects/ (atau
    $CLAUDE_CONFIG_DIR). Coba beberapa format slug supaya tahan update format
    Claude Code (saat ini = workdir.replace('/','-')."""
    base = _claude_projects_dir()
    if not base.is_dir():
        return None
    # Format saat ini & varian umum
    candidates = [
        workdir.replace("/", "-"),
        "-" + workdir.lstrip("/").replace("/", "-"),  # leading-dash variant
        workdir.lstrip("/").replace("/", "-"),        # no leading
    ]
    seen = set()
    for slug in candidates:
        if slug in seen:
            continue
        seen.add(slug)
        p = base / slug
        if p.is_dir():
            return p
    return None

def _delete_session(workdir: str, session_id: str) -> tuple[bool, int]:
    """Permanently delete all files for a Claude Code session.
    Returns (success, bytes_freed)."""
    import shutil
    if not re.fullmatch(r"[0-9a-fA-F-]{8,}", session_id):
        return False, 0  # guard against path traversal
    proj = _cc_project_dir(workdir)
    base = _claude_home()
    targets = []
    if proj:
        targets += [proj / f"{session_id}.jsonl", proj / session_id]
    targets += [
        base / "session-env" / session_id,
        base / "tasks" / session_id,
        base / "file-history" / session_id,
    ]
    freed, ok = 0, False
    for t in targets:
        try:
            if t.is_file():
                freed += t.stat().st_size
                t.unlink()
                ok = True
            elif t.is_dir():
                freed += sum(f.stat().st_size for f in t.rglob("*") if f.is_file())
                shutil.rmtree(t)
                ok = True
        except Exception as e:
            log(f"delete session {session_id[:8]} part {t.name}: {e}")
    return ok, freed

def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"

def _rename_session(workdir: str, session_id: str, new_title: str) -> bool:
    """Rename a Claude Code session by APPENDING a custom-title record.
    Safe: never reads or rewrites the conversation — just adds one JSONL line,
    exactly like Claude Code's own /rename does."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,}", session_id):
        return False
    proj = _cc_project_dir(workdir)
    if not proj:
        return False
    f = proj / f"{session_id}.jsonl"
    if not f.exists():
        return False
    title = new_title.strip()[:100]
    if not title:
        return False
    record = json.dumps({"type": "custom-title", "customTitle": title,
                         "sessionId": session_id}, ensure_ascii=False)
    try:
        with open(f, "a", encoding="utf-8") as fh:
            # Ensure we start on a fresh line, then append the record
            fh.write(("" if _file_ends_with_newline(f) else "\n") + record + "\n")
        return True
    except Exception as e:
        log(f"rename session {session_id[:8]}: {e}")
        return False

def _file_ends_with_newline(path) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.seek(-1, 2)
            return fh.read(1) == b"\n"
    except Exception:
        return True  # assume yes if empty/unreadable

def _undo_turns(workdir: str, session_id: str, n: int = 1) -> tuple[bool, int, str]:
    """Reliably rewind the last N *real* user turns from a Claude Code session.

    Session history is an append-only NDJSON linked-list (parentUuid → uuid).
    A real user turn is a `user`-type line whose message content is a plain
    string (NOT a tool_result block, which is also role=user). We find the byte
    offset of the start of the Nth-from-last such line and truncate the file
    there — removing that user turn plus every event that descends from it.
    Because the suffix we drop is exactly the tail of the linked list, the
    remaining file stays a valid, self-consistent conversation.

    Safety: validates session_id, makes a .bak backup before truncating, and
    refuses if it can't find N turns. Returns (ok, turns_removed, message)."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,}", session_id):
        return False, 0, "session id tidak valid"
    if n < 1:
        n = 1
    proj = _cc_project_dir(workdir)
    if not proj:
        return False, 0, "project dir tidak ditemukan"
    f = proj / f"{session_id}.jsonl"
    if not f.is_file():
        return False, 0, "file sesi tidak ada"

    # Walk the file recording the byte offset at the START of each real user turn.
    user_offsets = []
    try:
        with open(f, "rb") as fh:
            offset = 0
            for raw in fh:
                line = raw.decode("utf-8", "replace").strip()
                nxt = offset + len(raw)
                if line:
                    try:
                        e = json.loads(line)
                    except Exception:
                        offset = nxt
                        continue
                    if e.get("type") == "user":
                        msg_c = (e.get("message") or {}).get("content")
                        is_tool_result = False
                        if isinstance(msg_c, list):
                            for b in msg_c:
                                if isinstance(b, dict) and b.get("type") == "tool_result":
                                    is_tool_result = True
                                    break
                        # plain-string content (or non-tool_result list) == a real turn
                        if not is_tool_result and msg_c is not None:
                            user_offsets.append(offset)
                offset = nxt
    except Exception as e:
        return False, 0, f"gagal baca sesi: {e}"

    if len(user_offsets) < 1:
        return False, 0, "belum ada turn untuk di-undo"
    if n > len(user_offsets):
        return False, 0, f"cuma ada {len(user_offsets)} turn, tidak bisa mundur {n}"

    cut_at = user_offsets[-n]
    if cut_at <= 0:
        return False, 0, "tidak bisa undo turn pertama (pakai /reset untuk fresh)"

    # Backup, then truncate at the chosen offset.
    bak = f.with_suffix(".jsonl.bak")
    try:
        import shutil
        shutil.copy2(f, bak)
        with open(f, "r+b") as fh:
            fh.truncate(cut_at)
    except Exception as e:
        log(f"undo session {session_id[:8]}: {e}")
        return False, 0, f"gagal tulis ulang sesi: {e}"
    return True, n, "ok"

def _cleanup_empty_sessions(workdir: str) -> tuple[int, int]:
    """Delete sessions with no real conversation (empty / no user+assistant msgs).
    Returns (count_deleted, bytes_freed)."""
    proj = _cc_project_dir(workdir)
    if not proj:
        return 0, 0
    count, freed = 0, 0
    for f in list(proj.glob("*.jsonl")):
        try:
            has_convo = False
            with open(f, errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 200:
                        break
                    # A real exchange needs an assistant reply (user-only = aborted/empty)
                    if '"type":"assistant"' in line:
                        has_convo = True
                        break
            if not has_convo:
                ok, b = _delete_session(workdir, f.stem)
                if ok:
                    count += 1
                    freed += b
        except Exception:
            continue
    return count, freed

def _tail_lines(path, n: int = 60, block: int = 65536) -> list:
    """Read last n lines of a (possibly huge) file efficiently."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                read = min(block, size)
                size -= read
                f.seek(size)
                data = f.read(read) + data
        return data.decode("utf-8", "replace").splitlines()[-n:]
    except Exception:
        return []

def _extract_title(d: dict) -> str:
    """Pull a title from a custom-title or ai-title record."""
    t = d.get("type")
    if t == "custom-title":
        return (d.get("customTitle") or d.get("title") or "").strip()
    if t == "ai-title":
        return (d.get("aiTitle") or d.get("title") or "").strip()
    return ""

def _msg_text(d: dict) -> str:
    """Extract readable text from a user/assistant JSONL record."""
    msg = d.get("message", {})
    content = msg.get("content", "") if isinstance(msg, dict) else ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                parts.append(b["text"].strip())
            elif b.get("type") == "tool_use":
                parts.append(f"[{_tool_label(b.get('name'), b.get('input', {}))}]")
        return " ".join(parts).strip()
    return ""

def _session_recent_history(workdir: str, session_id: str, n_pairs: int = 3) -> str:
    """Return the last few user/assistant exchanges of a session, formatted
    for a Telegram preview. Reads only the file tail (fast even for huge files)."""
    proj = _cc_project_dir(workdir)
    if not proj:
        return ""
    f = proj / f"{session_id}.jsonl"
    if not f.exists():
        return ""
    msgs = []  # (role, text)
    for line in _tail_lines(f, 400):
        line = line.strip()
        if '"type":"user"' not in line and '"type":"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("type")
        if t not in ("user", "assistant"):
            continue
        txt = _msg_text(d)
        # skip noise (command wrappers, empty, system reminders, pure tool calls)
        if not txt or txt.startswith("<") or "command-" in txt[:30]:
            continue
        if txt.startswith("[") and txt.endswith("]"):
            continue  # skip messages that are only tool actions
        # Merge consecutive same-role messages (assistant often split)
        if msgs and msgs[-1][0] == t:
            msgs[-1] = (t, msgs[-1][1] + " " + txt)
        else:
            msgs.append((t, txt))
    if not msgs:
        return ""
    msgs = msgs[-(n_pairs * 2):]
    lines = []
    for role, txt in msgs:
        icon = "👤" if role == "user" else "🤖"
        snippet = re.sub(r'\s+', ' ', txt)[:140]
        if len(txt) > 140:
            snippet += "…"
        lines.append(f"{icon} {snippet}")
    return "\n".join(lines)

def _cc_sessions(workdir: str) -> list[dict]:
    """List Claude Code sessions for a workdir, sorted by time.
    Title priority: custom-title (manual) > ai-title (auto) > first user message —
    same labels you see in the Claude Code terminal /resume picker."""
    proj = _cc_project_dir(workdir)
    if not proj:
        return []
    sessions = []
    for f in sorted(proj.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
        sid = f.stem
        try:
            model, custom_title, ai_title, first_msg = "?", "", "", ""
            # Pass 1: head — model + first user message (titles can be here too)
            with open(f, errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 60:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    t = d.get("type")
                    if t == "custom-title":
                        custom_title = _extract_title(d) or custom_title
                    elif t == "ai-title":
                        ai_title = _extract_title(d) or ai_title
                    elif model == "?" and t == "assistant":
                        m = d.get("message", {})
                        if isinstance(m, dict) and m.get("model"):
                            model = m["model"]
                    elif not first_msg and t == "user":
                        m = d.get("message", {})
                        content = m.get("content", "") if isinstance(m, dict) else ""
                        raw = ""
                        if isinstance(content, str) and content.strip():
                            raw = content
                        elif isinstance(content, list):
                            for b in content:
                                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                                    raw = b["text"]
                                    break
                        raw = raw.strip()
                        if raw and not raw.startswith("<") and "command-" not in raw[:30]:
                            first_msg = re.sub(r'\s+', ' ', raw)[:80]
            # Pass 2: tail — titles are usually rewritten near the end of the file
            if not custom_title:
                for line in _tail_lines(f, 60):
                    line = line.strip()
                    if '"custom-title"' not in line and '"ai-title"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ct = _extract_title(d)
                    if d.get("type") == "custom-title" and ct:
                        custom_title = ct
                    elif d.get("type") == "ai-title" and ct and not ai_title:
                        ai_title = ct
            title = custom_title or ai_title
            summary = title or first_msg
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))
            mtime = f.stat().st_mtime
            sessions.append({"id": sid, "summary": summary, "title": title,
                             "model": model, "time": ts, "mtime": mtime})
        except Exception:
            sessions.append({"id": sid, "summary": "", "title": "",
                             "model": "?", "time": "?", "mtime": 0})
    return sessions  # tampilkan semua (pagination handle scroll)

# Cache sessions per chat so callback buttons can look up UUIDs by index
_sess_cache: dict[int, list[dict]] = {}

def _resume_sessions(cid: int) -> list[dict]:
    """Daftar sesi utk handler rs_* — cache in-memory ATAU rebuild dari disk.
    `_sess_cache` hilang tiap bot restart, jadi tombol 🗑️/✏️/resume di pesan
    /resume LAMA memetakan idx ke cache kosong → 'sesi tidak ditemukan' / hapus
    gagal diam-diam. Rebuild dgn urutan SAMA spt saat keyboard dibangun
    (`_cc_sessions(workdir)` window aktif) supaya idx tombol tetap valid."""
    cached = _sess_cache.get(cid)
    if cached:
        return cached
    try:
        wd = load_sess(cid).get("workdir", WORKDIR)
        sessions = _cc_sessions(wd)
    except Exception:
        sessions = []
    _sess_cache[cid] = sessions
    return sessions

def _rel_time(mtime: float) -> str:
    """Human-friendly relative time (Indonesian)."""
    if not mtime:
        return "?"
    d = time.time() - mtime
    if d < 60:
        return "baru saja"
    if d < 3600:
        return f"{int(d//60)} menit lalu"
    if d < 86400:
        return f"{int(d//3600)} jam lalu"
    if d < 604800:
        return f"{int(d//86400)} hari lalu"
    return time.strftime("%d %b", time.localtime(mtime))

def _build_resume_kb(cid: int, sessions: list, current_sid: str, page: int = 0) -> dict:
    """Inline keyboard: one button per session (click to resume) + pagination."""
    _sess_cache[cid] = sessions
    per_page = 8
    start = page * per_page
    page_items = sessions[start:start + per_page]
    rows = []
    for idx, s in enumerate(page_items, start=start):
        active = "🟢 " if s["id"] == current_sid else ""
        title = (s.get("title") or s.get("summary") or "").strip()
        label = title[:28] if title else f"sesi {s['id'][:6]}"
        # Resume (wide) + rename + delete on the same row
        rows.append([
            {"text": f"{active}{label} · {_rel_time(s['mtime'])}", "callback_data": f"rs_{idx}"},
            {"text": "✏️", "callback_data": f"rsren_{idx}"},
            {"text": "🗑️", "callback_data": f"rsdel_{idx}"},
        ])
    # Pagination row with page indicator
    total_pages = (len(sessions) + per_page - 1) // per_page
    nav = []
    if page > 0:
        nav.append({"text": "◀️", "callback_data": f"rspage_{page-1}"})
    nav.append({"text": f"📄 {page+1}/{total_pages}", "callback_data": "rsnop"})
    if start + per_page < len(sessions):
        nav.append({"text": "▶️", "callback_data": f"rspage_{page+1}"})
    rows.append(nav)
    # Tools row: provider + cleanup empty sessions + close
    rows.append([
        {"text": "🔌 Provider", "callback_data": "m_provider"},
        {"text": "🧹 Hapus Kosong", "callback_data": "rscleanup"},
        {"text": "✖️ Tutup", "callback_data": "m_close"},
    ])
    return {"inline_keyboard": rows, "_page": page}

def _resume_msg_text(cid: int, sessions: list) -> str:
    return (f"💬 *Pilih sesi* — {len(sessions)} total\n"
            f"Provider: `{PROVIDER}`\n"
            f"✏️ ganti nama · 🗑️ hapus · 🧹 bersihkan kosong")

def _clean_kb(kb: dict) -> dict:
    """Strip internal keys (e.g. _page) before sending to Telegram."""
    return {"inline_keyboard": kb["inline_keyboard"]}

def cmd(cid: int, text: str, msg: dict = None) -> str | None:
    global MODEL_SLOT, PROVIDER
    parts = text.strip().split(maxsplit=1)
    c = parts[0].lower().split("@")[0]
    a = parts[1].strip() if len(parts) > 1 else ""
    if c == "/start":
        return START_MSG
    if c == "/help":
        return HELP
    if c == "/exit":
        _store.pop(cid, None)
        p = SESS_DIR / f"{cid}.json"
        p.unlink(missing_ok=True)
        return "👋 Exited. Ketik pesan atau /start untuk mulai."
    if c == "/reset":
        new_session(cid)
        return "🔄 Session baru (fresh context)."
    if c == "/cd":
        if not a:
            return ("Cara pakai: `/cd /folder`\n"
                    "Contoh: `/cd /home/zesbe/yudha-pay`")
        p = Path(a).expanduser()
        if not p.is_absolute():
            p = Path(load_sess(cid)["workdir"]) / a
        if not p.is_dir():
            return f"❌ Folder tidak ada: `{str(p)}`"
        store = _load_store(cid)
        active = store.get("active", "main")
        in_topic = bool((msg or {}).get("message_thread_id"))
        # In a forum topic (or non-main window): just change THIS window's folder.
        # In the default private chat: auto-create a window named after the folder.
        if in_topic or active != "main":
            sess = load_sess(cid)
            sess["workdir"] = str(p)
            save_sess(cid)
            return f"📂 Folder window **{active}** → `{str(p)}`"
        win_name = p.name.lower().replace(" ", "-").replace(".", "-")
        if win_name in ("home", "zesbe", "tmp", "root"):
            win_name = p.name
        win_switch(cid, win_name, workdir=str(p))
        return f"📂 **{win_name}** → `{str(p)}`"
    if c == "/pwd":
        return f"📂 `{load_sess(cid)['workdir']}`"
    if c == "/model":
        sess = load_sess(cid)
        cur = sess.get("model", MODEL_SLOT)
        if not a:
            return (f"Model window ini: `{cur}`\n"
                    "Pilihan: opus, sonnet, haiku")
        if a in ("opus", "sonnet", "haiku"):
            sess["model"] = a          # per-window ONLY — jangan sentuh global MODEL_SLOT
            save_sess(cid)
            return f"🔄 Model window **{_load_store(cid).get('active','main')}** → `{a}`"
        return f"❌ Tidak dikenal: `{a}`\nPilihan: opus, sonnet, haiku"
    if c == "/resume":
        sess = load_sess(cid)
        wd = sess["workdir"]
        current_sid = sess["session_id"]
        # Parse: /resume [provider] <session_id>
        target_provider = None
        target_sid = ""
        if a:
            parts_a = a.split()
            if len(parts_a) >= 2 and parts_a[0] in PROVIDERS:
                target_provider = parts_a[0]
                target_sid = parts_a[1]
            elif len(parts_a) >= 2 and parts_a[0].isdigit() and int(parts_a[0]) <= len(PROVIDERS):
                # Support: /resume 1 <id> (number-based)
                prov_list = sorted(PROVIDERS.keys())
                idx = int(parts_a[0]) - 1
                if 0 <= idx < len(prov_list):
                    target_provider = prov_list[idx]
                target_sid = parts_a[1]
            else:
                target_sid = parts_a[0]
        # Switch provider if specified — per-window ONLY (jangan sentuh global)
        if target_provider and target_provider in PROVIDERS:
            sess["provider"] = target_provider
            save_sess(cid)
        # If a specific session ID is provided, switch to it
        if target_sid and len(target_sid) >= 8:
            all_sess = _cc_sessions(wd)
            match = [s for s in all_sess if s["id"].startswith(target_sid.strip())]
            if match:
                new_id = match[0]["id"]
                sess["session_id"] = new_id
                save_sess(cid)
                prov_info = f" · provider `{target_provider}`" if target_provider else ""
                return f"🔄 Lanjut sesi `{new_id[:12]}…` ({match[0]['time']}){prov_info}"
            return f"❌ Sesi tidak ketemu: `{target_sid}`\nKetik /resume untuk lihat daftar."
        # List sessions as clickable buttons
        return "_RESUME_"
    if c == "/w":
        chat_type = (msg or {}).get("chat", {}).get("type", "private")
        is_group = chat_type in ("group", "supergroup")
        if not a:
            windows = win_list(cid)
            if not windows:
                return "📭 Ketik `/cd /folder` untuk mulai project."
            lines = ["🪟 **Project Aktif**\n"]
            for w in windows:
                marker = " ✅" if w["active"] else ""
                wd = Path(w.get("workdir", "?")).name or w.get("workdir", "?")
                lines.append(f"• **{w['name']}**{marker} — `{wd}`")
            lines.append(f"\nGanti project: `/cd /path/project`")
            if is_group:
                lines.append("Atau `/w nama` untuk topic baru.")
            return "\n".join(lines)
        # /w close <name>
        if a.startswith("close "):
            name = a[6:].strip()
            if win_close(cid, name):
                return f"🪟 Project **{name}** ditutup."
            return f"❌ Gak bisa tutup **{name}** (gak ada / project terakhir)."
        # /w <name> [dir] — switch/create window
        parts_a = a.split(maxsplit=1)
        name = parts_a[0].strip().lower().replace(" ", "-")
        dir_arg = parts_a[1].strip() if len(parts_a) > 1 else ""
        store = _load_store(cid)
        is_new = name not in store.get("windows", {})
        # Resolve workdir if a folder was given
        target_wd = None
        if dir_arg:
            p = Path(dir_arg).expanduser()
            if p.is_dir():
                target_wd = str(p)
            else:
                return f"❌ Folder tidak ada: `{dir_arg}`"
        win_switch(cid, name, workdir=target_wd)
        sess = load_sess(cid)
        # Auto-create forum topic in groups
        if is_group and is_new:
            tid = _create_topic(cid, name)
            if tid:
                wd = Path(sess.get('workdir', '?')).name or sess.get('workdir', '?')
                return (f"🪟 *Topic baru dibuat:* **{name}**\n"
                        f"📂 Folder: `{wd}` · sesi fresh\n"
                        f"Cek sidebar kiri 👈 — tiap topic = sesi terpisah.")
        wd = Path(sess.get('workdir', '?')).name or sess.get('workdir', '?')
        status = "baru" if is_new else "aktif"
        return f"🪟 Project **{name}** ({status}) · 📂 `{wd}` · sesi `{sess['session_id'][:8]}`"
    if c == "/menu":
        return "_MENU_"
    if c == "/status":
        sess = load_sess(cid)
        store = _load_store(cid)
        return (
            f"📊 **Status**\n\n"
            f"Provider: `{sess.get('provider', DEFAULT_PROVIDER)}`\n"
            f"Model: `{sess.get('model', MODEL_SLOT)}`\n"
            f"Effort: `{sess.get('effort') or 'default'}`\n"
            f"Project: **{store.get('active', 'main')}**\n"
            f"Folder: `{sess['workdir']}`\n"
            f"Sesi: `{sess['session_id'][:8]}…`"
        )
    if c == "/provider":
        if not a:
            return "_PROVIDERKB_"  # tampilkan tombol provider
        sub = a.split(maxsplit=1)
        action = sub[0].lower()
        rest = sub[1] if len(sub) > 1 else ""
        # ── reload ──
        if action == "reload":
            reload_providers()
            return f"🔄 Provider di-reload: {', '.join(sorted(PROVIDERS.keys()))}"
        # ── rename: /provider rename <lama> <baru> ──
        if action == "rename":
            rn = rest.split()
            if len(rn) != 2:
                return "Cara pakai: `/provider rename <lama> <baru>`"
            ok, msg = _provider_rename(rn[0], rn[1])
            return f"✏️ `{rn[0]}` → `{rn[1]}`" if ok else f"❌ {msg}"
        # ── paste: /provider paste <blob multi-baris> — copy-paste sekaligus ──
        if action == "paste":
            if not rest.strip():
                return ("📋 *Paste config provider sekaligus*\n\n"
                        "`/provider paste`\nlalu tempel (boleh multi-baris), contoh:\n\n"
                        "`name=zai`\n`base_url=https://api.z.ai/api/anthropic`\n"
                        "`token=sk-xxx`\n`opus=glm-4.6`\n`sonnet=glm-4.6`\n`haiku=glm-4.5-air`\n\n"
                        "_Model boleh dikosongin — bot auto-ambil dari endpoint._")
            def _bg():
                _provider_ingest_paste(cid, rest)
            threading.Thread(target=_bg, daemon=True).start()
            return None
        # ── test: /provider test <nama> — cek konek + token (nol biaya token) ──
        if action == "test":
            name = rest.strip() or PROVIDER
            inf = _provider_info(name)
            if not inf:
                return f"❌ Provider `{name}` tidak ada (atau native `claude`)."
            send_msg(cid, f"🔌 Tes `{name}`…")
            ok, msg = _test_endpoint(inf.get("base_url", ""), inf.get("token", ""))
            return f"{'✅' if ok else '❌'} `{name}`: {msg}"
        # ── models: /provider models <nama> — auto-load daftar model dari endpoint ──
        if action in ("models", "model"):
            name = rest.strip() or PROVIDER
            inf = _provider_info(name)
            if not inf:
                return f"❌ Provider `{name}` tidak ada."
            send_msg(cid, f"📋 Ambil model dari `{name}`…")
            ok, ids, info = _fetch_models(inf.get("base_url", ""), inf.get("token", ""))
            if not ok:
                return f"❌ `{name}`: {info}"
            listing = "\n".join(f"• `{m}`" for m in ids[:50])
            more = f"\n…dan {len(ids)-50} lagi" if len(ids) > 50 else ""
            return (f"📋 *{len(ids)} model di `{name}`:*\n{listing}{more}\n\n"
                    f"Pasang ke slot: `/provider edit {name} opus <model>`")
        # ── add: /provider add <nama> <base_url> <token> <opus> [sonnet] [haiku] ──
        if action == "add":
            parts = rest.split()
            if len(parts) < 4:
                return ("➕ *Tambah provider*\n\n"
                        "`/provider add <nama> <base_url> <token> <opus_model> [sonnet] [haiku]`\n\n"
                        "Contoh:\n"
                        "`/provider add groq https://api.groq.com/anthropic gsk_xxx llama-3.3-70b`\n\n"
                        "Kalau sonnet/haiku kosong, pakai model opus.")
            name, base_url, token = parts[0], parts[1], parts[2]
            opus = parts[3]
            sonnet = parts[4] if len(parts) > 4 else ""
            haiku = parts[5] if len(parts) > 5 else ""
            if not re.fullmatch(r"[a-z0-9_-]{1,30}", name):
                return "❌ Nama provider cuma boleh huruf kecil/angka/dash."
            # Tes dulu biar gak sia-sia simpan provider mati
            tok_ok, tmsg = _test_endpoint(base_url, token)
            if not tok_ok:
                return (f"❌ Endpoint gagal: *{tmsg}*\n\nProvider TIDAK disimpan. "
                        f"Cek base_url/token dulu.")
            ok, msg = _provider_save(name, base_url, token, opus, sonnet or None, haiku or None)
            if not ok:
                return f"❌ Gagal simpan: {msg}"
            inf = _provider_info(name)
            return (f"✅ Provider `{name}` ditambah & terkoneksi!\n"
                    f"📡 {base_url} ({tmsg})\n"
                    f"🧠 opus={inf.get('opus')} sonnet={inf.get('sonnet')} haiku={inf.get('haiku')}\n\n"
                    f"Pakai: `/provider {name}`")
        # ── edit: /provider edit <nama> <field> <value>  (field: base_url|token|opus|sonnet|haiku) ──
        if action == "edit":
            parts = rest.split(maxsplit=2)
            if len(parts) < 3:
                return ("✏️ *Edit provider*\n\n"
                        "`/provider edit <nama> <field> <value>`\n"
                        "field: `base_url` | `token` | `opus` | `sonnet` | `haiku`\n\n"
                        "Contoh: `/provider edit groq opus llama-3.3-70b`")
            name, field, value = parts[0], parts[1].lower(), parts[2].strip()
            if name not in _read_providers_file():
                return f"❌ Provider `{name}` tidak ada."
            if field not in ("base_url", "token", "opus", "sonnet", "haiku"):
                return "❌ Field cuma: base_url, token, opus, sonnet, haiku."
            kw = {"base_url": None, "token": None, "opus": None, "sonnet": None, "haiku": None}
            kw[field] = value
            ok, msg = _provider_save(name, kw["base_url"], kw["token"], kw["opus"], kw["sonnet"], kw["haiku"])
            return f"✅ `{name}` → {field} diupdate." if ok else f"❌ Gagal: {msg}"
        # ── del: /provider del <nama> ──
        if action in ("del", "delete", "rm"):
            name = rest.strip()
            if name not in PROVIDERS:
                return f"❌ Provider `{name}` tidak ada."
            if name in ("claude",):
                return "❌ Provider native `claude` gak bisa dihapus."
            if _provider_delete(name):
                return f"🗑️ Provider `{name}` dihapus."
            return f"❌ Gagal hapus `{name}`."
        # ── info: /provider info <nama> ──
        if action == "info":
            name = rest.strip() or PROVIDER
            inf = _provider_info(name)
            if not inf and name != "claude":
                return f"❌ Provider `{name}` tidak ada."
            if name == "claude" and not inf:
                return "🔌 *claude* — native Anthropic (auth login sendiri, tanpa override)."
            tok = inf.get("token", "")
            tok_masked = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else "***"
            return (f"🔌 *{name}*\n"
                    f"📡 `{inf.get('base_url','?')}`\n"
                    f"🔑 `{tok_masked}`\n"
                    f"🧠 opus=`{inf.get('opus','?')}`\n"
                    f"     sonnet=`{inf.get('sonnet','?')}`\n"
                    f"     haiku=`{inf.get('haiku','?')}`")
        # ── switch provider (default) ──
        # "claude" = native default Anthropic (TIDAK ada di providers.json) →
        # harus tetap valid, jika tidak `/provider claude` gagal diam-diam.
        if action == "claude" or action in PROVIDERS:
            sess = load_sess(cid)
            sess["provider"] = action  # per-window ONLY — jangan sentuh global PROVIDER
            save_sess(cid)
            return f"🔄 Provider window **{_load_store(cid).get('active','main')}** → `{action}`"
        return (f"❌ Tidak ada: `{action}`\n\n"
                f"Provider: {', '.join(sorted(PROVIDERS.keys()))}\n"
                f"Kelola: `/provider add|edit|del|info|test|models|reload`")
    if c == "/cost":
        # Konteks per window (ukuran ASLI termasuk cache) — penunjuk read-only.
        store = _load_store(cid)
        ctx_lines = []
        for name, w in store.get("windows", {}).items():
            ct = w.get("ctx_tokens", 0)
            if not ct:
                continue
            thr = _compact_threshold(w.get("model", MODEL_SLOT))
            if thr:
                bar = "🟢" if ct < thr * 0.7 else ("🟡" if ct < thr else "🔴")
                cap = f" /{thr//1000}k"
            else:
                bar, cap = "⚪", ""
            mark = " ⚠️auto-compact" if w.get("needs_compact") else ""
            ctx_lines.append(f"• **{name}** (`{w.get('model', MODEL_SLOT)}`): {bar} ~{ct//1000}k{cap} token{mark}")
        out = []
        if ctx_lines:
            ratio = f"{int(AUTO_COMPACT_RATIO*100)}%" if AUTO_COMPACT_RATIO else "off"
            out.append(f"🧮 **Konteks aktif** (auto-compact @ {ratio} limit)")
            out += ctx_lines
            out.append("_Manual: /compact (ringkas) atau /new (reset)._")
            out.append("")
        if not _usage_log:
            out.append("💰 Belum ada pemakaian tercatat sesi ini.")
            return "\n".join(out)
        out.append("💰 **Pemakaian** (sejak bot restart)\n")
        tot_tok, tot_cost, tot_calls = 0, 0.0, 0
        for prov, u in sorted(_usage_log.items(), key=lambda x: -x[1]["cost"]):
            out.append(f"• `{prov}`: {u['calls']}× · {u['tokens']:,} tok · ${u['cost']:.3f}")
            tot_tok += u["tokens"]; tot_cost += u["cost"]; tot_calls += u["calls"]
        out.append(f"\n**Total:** {tot_calls}× · {tot_tok:,} token · ${tot_cost:.3f}")
        return "\n".join(out)
    if c == "/stop":
        store = _load_store(cid)
        win_name = store.get("active", "main")
        # Sapu SEMUA window busy di chat ini (bukan cuma active) — biar task di
        # topic lain / state nyangkut juga ikut berhenti.
        targets = [k for k in list(_busy) if k[0] == cid]
        primary = (cid, win_name)
        if primary in _busy and primary not in targets:
            targets.append(primary)
        if not targets:
            # Tidak ada di _busy, tapi mungkin ada proc/cancel nyasar — bersihin juga.
            stray = [k for k in list(_running_procs) if k[0] == cid]
            for lk in stray:
                p = _running_procs.pop(lk, None)
                if p:
                    try: _kill_process_tree(p)
                    except Exception: pass
            return ("⏹ Tidak ada task aktif. (State sudah dibersihkan.)"
                    if stray else "Tidak ada task yang sedang jalan.")
        killed = []
        for lk in targets:
            _cancelled.add(lk)              # run_claude akan self-kill ≤0.5s kalau hidup
            p = _running_procs.pop(lk, None)
            if p:
                try: _kill_process_tree(p)  # SIGTERM→SIGKILL ke seluruh process group
                except Exception: pass
            _busy.discard(lk)              # PAKSA lepas lock — anti "masih kerja" nyangkut
            killed.append(lk[1])
        uniq = ", ".join(f"**{w}**" for w in dict.fromkeys(killed))
        return f"⏹ Dihentikan & lock dilepas: {uniq}\n\nKirim pesan baru kapan saja."
    if c == "/effort":
        sess = load_sess(cid)
        cur = sess.get("effort") or "default"
        if not a:
            return "_EFFORTKB_"  # tampilkan tombol effort
        lvl = a.strip().lower()
        if lvl in ("default", "auto", "reset", "off"):
            sess.pop("effort", None)
            save_sess(cid)
            return "🎯 Effort → *default* (Claude Code yang atur)."
        if lvl in EFFORT_LEVELS:
            sess["effort"] = lvl
            save_sess(cid)
            return f"🎯 Effort window **{_load_store(cid).get('active','main')}** → `{lvl}`\n\n(makin tinggi = makin dalam mikir, makin lama/mahal)"
        return f"❌ Tidak dikenal: `{lvl}`\nPilihan: {', '.join(EFFORT_LEVELS)}, default"
    # ── Hermes-style commands ────────────────────────────────────────────────
    if c in ("/new",):
        # Fresh session, optionally named (Hermes /new [name])
        new_session(cid)
        if a:
            sess = load_sess(cid)
            _rename_session(sess["workdir"], sess["session_id"], a.strip()[:80])
            return f"🆕 Sesi baru **{a.strip()[:80]}** (fresh context)."
        return "🆕 Sesi baru (fresh context)."
    if c == "/title":
        sess = load_sess(cid)
        if not a:
            return "Cara pakai: `/title nama sesi`"
        ok = _rename_session(sess["workdir"], sess["session_id"], a.strip()[:80])
        return f"🏷️ Judul sesi → *{a.strip()[:80]}*" if ok else "❌ Gagal set judul (sesi belum punya history?)."
    if c in ("/usage",):
        # Alias ke /cost (Hermes: token usage)
        return cmd(cid, "/cost", msg)
    if c == "/whoami":
        is_owner = (not OWNER_IDS) or ((msg or {}).get("from", {}).get("id") in OWNER_IDS)
        role = "👑 admin (owner)" if is_owner else "👤 user"
        return f"🪪 *Akses kamu:* {role}\nUID: `{(msg or {}).get('from',{}).get('id','?')}`"
    if c in ("/version", "/v"):
        try:
            ver = subprocess.run([get_claude_bin(load_sess(cid).get("provider")), "--version"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            ver = "?"
        return f"🤖 *CC-TG* (Claude Code wrapper)\nClaude Code: `{ver or '?'}`"
    if c == "/yolo":
        # Sudah selalu --dangerously-skip-permissions; ini cuma info
        return ("⚡ *YOLO mode* selalu AKTIF di bot ini.\n"
                "Semua command dijalankan tanpa konfirmasi izin "
                "(`--dangerously-skip-permissions`). Hati-hati ya.")
    if c == "/verbose":
        sess = load_sess(cid)
        cur = sess.get("verbose", False)
        if a.strip().lower() in ("on", "off"):
            cur = a.strip().lower() == "on"
        else:
            cur = not cur
        sess["verbose"] = cur
        save_sess(cid)
        return (f"📢 Verbose progress: *{'ON' if cur else 'OFF'}*\n"
                f"{'Semua step (teks + thinking) tampil live.' if cur else 'Cuma tool penting yang tampil.'}")
    if c in ("/agents", "/tasks"):
        # Tampilkan window/task yang lagi jalan
        running = [w for (ci, w) in _busy if ci == cid]
        store = _load_store(cid)
        lines = ["🤖 *Status agent/task*\n"]
        wins = store.get("windows", {})
        if not wins:
            return "Belum ada window aktif."
        for name in wins:
            mark = "🟢 KERJA" if name in running else "⚪ idle"
            lines.append(f"• **{name}** — {mark}")
        active_slots = MAX_CONCURRENT - _claude_slots._value
        lines.append(f"\n⚙️ Slot global: {active_slots}/{MAX_CONCURRENT} dipakai")
        return "\n".join(lines)
    if c == "/restart":
        send_msg(cid, "♻️ Merestart bot… (auto-up via systemd, ~5 detik)")
        log("Restart requested via /restart")
        threading.Thread(target=lambda: (time.sleep(1), os._exit(0)), daemon=True).start()
        return None
    if c == "/update":
        # Pull versi terbaru dari GitHub lalu restart. update.sh sudah handle:
        # reset file kode ke origin/main (config/providers aman, gitignored) +
        # sinkron deps + restart service. Dijalankan async biar /update langsung balas.
        def _do_update():
            try:
                up = BOT_DIR / "update.sh"
                if not up.exists():
                    send_msg(cid, "❌ update.sh tidak ada. Update manual: `cd ~/.cc-tg && git pull`")
                    return
                r = subprocess.run(["bash", str(up)], cwd=str(BOT_DIR),
                                   capture_output=True, text=True, timeout=180)
                out = _strip_ansi((r.stdout or "") + (r.stderr or "")).strip()
                tail = "\n".join(out.splitlines()[-8:])[:1200]
                if "Sudah versi terbaru" in out:
                    send_msg(cid, f"✅ *Sudah versi terbaru* — tidak ada update.\n\n```\n{tail}\n```")
                    return
                send_msg(cid, f"🔄 *Update selesai* — bot akan restart pakai versi baru.\n\n```\n{tail}\n```")
                # restart kalau update.sh belum (mis. bukan systemd) — exit, systemd auto-up
                time.sleep(1)
                os._exit(0)
            except subprocess.TimeoutExpired:
                send_msg(cid, "⏰ Update timeout (>3 menit). Coba manual: `cd ~/.cc-tg && ./update.sh`")
            except Exception as e:
                send_msg(cid, f"❌ Update gagal: {str(e)[:200]}")
        send_msg(cid, "⬇️ Mengambil update dari GitHub… (tunggu ~10-30 detik)")
        log("Update requested via /update")
        threading.Thread(target=_do_update, daemon=True).start()
        return None
    if c == "/retry":
        # Kirim ulang pesan terakhir user di window ini
        sess = load_sess(cid)
        last = sess.get("last_prompt")
        if not last:
            return "↻ Belum ada pesan untuk diulang."
        # Proses ulang lewat pipeline normal
        synth = {"message": {"chat": {"id": cid, "type": (msg or {}).get("chat", {}).get("type", "private")},
                             "from": {"id": (msg or {}).get("from", {}).get("id")},
                             "message_id": (msg or {}).get("message_id", 0), "text": last}}
        threading.Thread(target=_process_safe, args=(synth,), daemon=True).start()
        return f"↻ Mengulang: _{last[:60]}_"
    if c in ("/queue", "/q"):
        if not a.strip():
            sess = load_sess(cid)
            q = sess.get("queue", [])
            if not q:
                return "📭 Antrian kosong. Pakai: `/queue <prompt>`"
            lines = ["📋 *Antrian:*"]
            for i, item in enumerate(q, 1):
                lines.append(f"{i}. _{item[:50]}_")
            return "\n".join(lines)
        store = _load_store(cid)
        win_name = store.get("active", "main")
        lock_key = (cid, win_name)
        prompt = a.strip()
        if lock_key in _busy:
            sess = load_sess(cid)
            q = sess.setdefault("queue", [])
            q.append(prompt)
            save_sess(cid)
            return f"➕ Diantri (posisi {len(q)}). Jalan setelah task sekarang selesai."
        # Tidak busy → proses langsung lewat pipeline normal
        synth = {"message": {"chat": {"id": cid, "type": (msg or {}).get("chat", {}).get("type", "private")},
                             "from": {"id": (msg or {}).get("from", {}).get("id")},
                             "message_id": (msg or {}).get("message_id", 0), "text": prompt}}
        tid = (msg or {}).get("message_thread_id")
        if tid:
            synth["message"]["message_thread_id"] = tid
        threading.Thread(target=_process_safe, args=(synth,), daemon=True).start()
        return None
    if c in ("/background", "/bg", "/btw"):
        if not a.strip():
            return "Cara pakai: `/background <prompt>` — jalan di window terpisah (paralel)."
        prompt = a.strip()
        bg_name = f"bg-{int(time.time())}"
        cur_active = _load_store(cid).get("active", "main")
        synth = {"message": {"chat": {"id": cid, "type": (msg or {}).get("chat", {}).get("type", "private")},
                             "from": {"id": (msg or {}).get("from", {}).get("id")},
                             "message_id": (msg or {}).get("message_id", 0), "text": prompt}}

        def _run_bg():
            # Switch active ke window bg lalu proses; restore active sesudahnya
            win_switch(cid, bg_name)
            _process_safe(synth)
            # Kembalikan active ke window semula biar UX gak kebawa pindah
            try:
                st = _load_store(cid)
                if bg_name not in [k for (ci, k) in _busy if ci == cid] and cur_active in st.get("windows", {}):
                    st["active"] = cur_active
                    _save_store(cid)
            except Exception:
                pass
        threading.Thread(target=_run_bg, daemon=True).start()
        return f"🌙 Jalan di background (window `{bg_name}`). Hasil dikirim begitu selesai."
    if c in ("/compact", "/compress"):
        # Native /compact (manual): kirim "/compact" ke sesi → Claude Code ringkas
        # in-place & persist. Pintar, histori inti tetap, session_id sama.
        store = _load_store(cid)
        win_name = store.get("active", "main")
        if (cid, win_name) in _busy:
            return "⏳ Window ini masih kerja. /stop dulu sebelum /compact."

        def _do_compact():
            send_msg(cid, "🗜️ Meringkas konteks & menyegarkan sesi… sebentar.")
            ok, info = _reseed_compact(cid, win_name)
            if ok:
                send_msg(cid, "✅ *Konteks diringkas & sesi disegarkan.* Mulai bersih dari "
                              "ringkasan inti — token balik hemat. Lanjut ngobrol biasa.")
            else:
                send_msg(cid, f"↩️ Belum dipangkas: {info}")
        threading.Thread(target=_do_compact, daemon=True).start()
        return None
    if c == "/undo":
        # Mundurkan N turn user terakhir dari file sesi (destruktif tapi aman:
        # backup + truncate suffix linked-list). Default N=1.
        n = 1
        if a.strip():
            if a.strip().isdigit():
                n = max(1, int(a.strip()))
            else:
                return "Cara pakai: `/undo [N]` — N harus angka (default 1)."
        store = _load_store(cid)
        win_name = store.get("active", "main")
        if (cid, win_name) in _busy:
            return "⏳ Window ini masih kerja. /stop dulu sebelum /undo."
        sess = load_sess(cid)
        ok, removed, info = _undo_turns(sess["workdir"], sess["session_id"], n)
        if not ok:
            return f"↩️ Gagal undo: {info}"
        return (f"↩️ Mundur {removed} turn terakhir. Konteks sesi sudah dipangkas.\n"
                f"(backup disimpan `.jsonl.bak`) Lanjut ngobrol seperti biasa.")
    if c == "/clear":
        new_session(cid)
        return "🧹 Layar & konteks dibersihkan, sesi baru."
    if c == "/cron":
        return _cron_command(cid, a)
    # Unknown slash command → give feedback (don't silently drop, don't send to Claude)
    return f"❓ Perintah `{c}` tidak dikenal. Ketik /help untuk daftar perintah."

# ── Live streaming: single growing bubble (Hermes-style, production-grade) ─────
# Satu pesan Telegram tumbuh dari status bubble (tools/thinking + elapsed) jadi
# jawaban asisten — di-stream token-by-token sebagai PLAIN text (tak pernah gagal
# parse di markdown parsial), lalu di-FINALIZE jadi MarkdownV2 DI TEMPAT. Jawaban
# panjang dipecah jadi pesan lanjutan; jawaban PICK/MULTIPICK dilempar ke tombol.
class LiveStream:
    _BASE_INTERVAL = 0.5      # cadence edit dasar (detik) — rapat tapi aman 429
    _MAX_INTERVAL = 8.0       # cap adaptive-backoff saat flood
    _FLOOD_STRIKES = 3        # gagal edit beruntun sebelum mundur
    _TG_LIMIT = 4096
    _LIVE_BUDGET = 3600       # jaga bubble live di bawah limit (sisakan header)
    _SPLIT = 3500             # ukuran potong jawaban final (raw; sisakan utk escaping)

    def __init__(self, cid, thread_id, win_name, provider, model, verbose,
                 lock_key, started):
        self.cid, self.thread_id = cid, thread_id
        self.win, self.provider, self.model = win_name, provider, model
        self.verbose, self.lock_key, self.started = verbose, lock_key, started
        self.feed = []           # baris langkah tool
        self.text = ""           # jawaban asisten yang tumbuh
        self.think = ""          # thinking yang tumbuh (verbose)
        self.note_line = ""      # status transient (mis. antri slot)
        self._lock = threading.Lock()
        self._dirty = False
        self._last_sent = None
        self._last_edit = 0.0
        self._interval = self._BASE_INTERVAL
        self._strikes = 0
        self._stop = threading.Event()
        self._thread = None
        self._typing_thread = None
        # Bubble streaming = teks MURNI tanpa reply_markup (edit ringan, anti
        # flood-freeze). Tombol Stop ada di reply keyboard (bar bawah) — lihat
        # REPLY_KB. /stop (slash) tetap jalan sebagai alternatif.
        st = tg_api("sendMessage", chat_id=cid,
                    text=f"🔄 {win_name} · {provider}/{model}\n⏳ memulai…",
                    parse_mode="",
                    **({"message_thread_id": thread_id} if thread_id else {}))
        self.st_id = (st or {}).get("result", {}).get("message_id", 0)

    # ---- rendering (live = plain text) ----
    def _header(self):
        return (f"🔄 {self.win} · {self.provider}/{self.model} · "
                f"⏱ {int(time.time()-self.started)}s")

    def _render(self):
        lines = [self._header()]
        if self.note_line:
            lines += ["", self.note_line]
        if self.feed:
            lines += [""] + self.feed[-8:]
        if self.verbose and self.think:
            tp = " ".join(self.think.split())
            if tp:
                lines += ["", f"💭 {tp[-200:]}"]
        if self.text:
            body = self.text
            budget = self._LIVE_BUDGET - len("\n".join(lines)) - 4
            budget = max(budget, 200)
            if len(body) > budget:
                body = "…" + body[-budget:]   # windowing: tampilkan ekor
            lines += ["", body]
        return "\n".join(lines)[:self._TG_LIMIT]

    # ---- event sink (dikirim ke run_claude) ----
    def on_event(self, ev):
        t = ev.get("type")
        if t == "stream_text":
            with self._lock:
                self.text = ev.get("text", "") or ""
                self._dirty = True
            return
        if t == "stream_think":
            if self.verbose:
                with self._lock:
                    self.think = ev.get("text", "") or ""
                    self._dirty = True
            return
        if t == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    self._push(_tool_label(b.get("name"), b.get("input", {})))
        elif t == "user":
            for b in ev.get("message", {}).get("content", []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    mark = "⚠️ error" if b.get("is_error") else "✅"
                    self._push(f"   ↳ {mark} {self._preview(b.get('content',''))}".rstrip())

    def _push(self, label):
        with self._lock:
            self.feed.append(label)
            self._dirty = True

    @staticmethod
    def _preview(content, n=70):
        s = ""
        if isinstance(content, str):
            s = content
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    s = b.get("text", "") or ""; break
                if isinstance(b, str):
                    s = b; break
        s = " ".join(s.split())
        return (s[:n] + "…") if len(s) > n else s

    def note(self, text):
        with self._lock:
            self.note_line = text
            self._dirty = True
        self._flush(force=True)

    # ---- flusher thread (adaptive throttle) ----
    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._typing_thread = threading.Thread(target=self._typing_loop, daemon=True)
        self._typing_thread.start()

    def _loop(self):
        # Flush loop: HANYA edit bubble (ringan). typing() dipindah ke thread
        # terpisah (_typing_loop) supaya HTTP sendChatAction yg lambat TIDAK
        # mem-block flush → bubble nggak freeze.
        while not self._stop.is_set():
            # First content instan: kalau ada update tapi bubble belum pernah
            # nampilin teks/feed (masih "memulai…"), flush SEGERA tanpa nunggu
            # interval → token pertama langsung muncul (mirip terminal).
            first = self._last_sent is None and self._dirty
            if first or time.time() - self._last_edit >= self._interval:
                self._flush()
            self._stop.wait(0.15)

    def _typing_loop(self):
        # 'typing…' indicator, ~tiap 4s (Telegram tahan ~5s per action).
        # Dipisah dari flush biar nggak saling block.
        while not self._stop.is_set():
            typing(self.cid, self.thread_id)
            self._stop.wait(4.0)

    def _flush(self, force=False):
        if not self.st_id:
            return
        with self._lock:
            if not self._dirty and not force:
                return
            self._dirty = False
            txt = self._render()
        if txt == self._last_sent:
            return
        ok = True
        try:
            tg_api("editMessageText", chat_id=self.cid, message_id=self.st_id,
                   text=txt, parse_mode="")
        except Exception:
            ok = False
        self._last_edit = time.time()
        if ok:
            self._strikes = 0
            self._interval = self._BASE_INTERVAL
            self._last_sent = txt
        else:
            self._strikes += 1
            if self._strikes >= self._FLOOD_STRIKES:
                self._interval = min(self._interval * 2, self._MAX_INTERVAL)

    def stop(self):
        self._stop.set()

    # ---- finalize: bubble → jawaban (markdown, multi-msg, PICK passthrough) ----
    def _footer(self, usage):
        dur = int(time.time() - self.started)
        ctx_k = int((usage or {}).get("context", 0)) // 1000
        limit_k = (CONTEXT_WINDOWS.get(self.model,
                   CONTEXT_WINDOWS.get("opus", 1_000_000))) // 1000
        turns = (usage or {}).get("turns", 0)
        extra = f" · 📊 {ctx_k}k/{limit_k}k" + (f" · {turns} turn" if turns else "")
        return f"✅ {dur}s · {self.provider}/{self.model}{extra}"

    def _edit_md(self, text):
        """Edit bubble dgn MarkdownV2; kalau gagal parse / kepanjangan → plain."""
        md = None
        try:
            md = _to_md(text)
        except Exception:
            md = None
        if md is not None and len(md) <= self._TG_LIMIT:
            try:
                tg_api("editMessageText", chat_id=self.cid, message_id=self.st_id,
                       text=md, parse_mode="MarkdownV2")
                return True
            except Exception:
                pass
        try:
            tg_api("editMessageText", chat_id=self.cid, message_id=self.st_id,
                   text=text[:self._TG_LIMIT], parse_mode="")
            return True
        except Exception:
            return False

    def finalize(self, result, usage, reply_to=0):
        self.stop()
        footer = self._footer(usage)
        # PICK/MULTIPICK → caller render tombol; bubble dikecilkan jadi footer.
        if _PICK_RE.search(result or "") or _MULTIPICK_RE.search(result or ""):
            if self.st_id:
                self._edit_md(footer)
            return False
        body = (result or "").strip() or "_(kosong)_"
        chunks = _split_chunks(body, self._SPLIT)
        first = chunks[0]
        if len(chunks) == 1:
            first = first + "\n\n———\n" + footer
        if self.st_id:
            self._edit_md(first)                       # bubble → jawaban (in place)
        else:
            _send_raw(self.cid, _to_md(first), reply_to, self.thread_id)
        for i, ch in enumerate(chunks[1:], start=1):   # lanjutan → pesan baru
            piece = ch + ("\n\n———\n" + footer if i == len(chunks) - 1 else "")
            _send_raw(self.cid, _to_md(piece), 0, self.thread_id)
        return True

    def abort(self, msg=None):
        self.stop()
        if not self.st_id:
            if msg:
                tg_api("sendMessage", chat_id=self.cid, text=msg, parse_mode="",
                       **({"message_thread_id": self.thread_id} if self.thread_id else {}))
            return
        try:
            if msg:
                tg_api("editMessageText", chat_id=self.cid, message_id=self.st_id,
                       text=msg, parse_mode="")
            else:
                tg_api("deleteMessage", chat_id=self.cid, message_id=self.st_id)
        except Exception:
            pass


# ── Update processor ────────────────────────────────────────────────────────
def process(upd: dict):
    # Handle callback query (inline button press)
    cb = upd.get("callback_query")
    if cb:
        handle_callback(cb)
        return

    msg = upd.get("message")
    if not msg:
        return
    cid = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id")
    text = msg.get("text", "") or ""
    mid = msg.get("message_id", 0)
    thread_id = msg.get("message_thread_id", 0)
    chat_type = msg.get("chat", {}).get("type", "private")
    is_group = chat_type in ("group", "supergroup")

    # DIAGNOSTIK isolasi topic: lihat persis apa yg Telegram kirim utk grup.
    # Kalau thread=0 di semua topic → grup BUKAN forum (Topics OFF) atau pesan di
    # General → semua nyangkut ke window 'main' (itu sebab "topic isinya sama").
    if is_group:
        log(f"grp cid={cid} type={chat_type} thread={thread_id} "
            f"is_topic={msg.get('is_topic_message')} "
            f"reply_thread={(msg.get('reply_to_message') or {}).get('message_thread_id')}")

    # Strip @botname from text in groups
    if is_group and "@" in text:
        text = re.sub(r'@\w+\s*', '', text).strip()

    # Auto-switch window based on forum topic. Resolve SAMA seperti jalur pesan
    # (auto-create + map kalau topic belum dipetakan) — kalau cuma switch saat
    # mapping sudah ada, command /provider di topic baru nulis ke window LAIN
    # (active lama) sementara pesan jalan di window topic → switch 'nyangkut'.
    if thread_id:
        win_name = _get_window_for_thread(cid, thread_id)
        if not win_name:
            win_name = f"topic-{thread_id}"
            _topic_map.setdefault(cid, {})[thread_id] = win_name
            _store_local = _load_store(cid)
            _store_local.setdefault("topic_map", {})[str(thread_id)] = win_name
            _save_store(cid)
        win_switch(cid, win_name)

    if OWNER_IDS and uid not in OWNER_IDS:
        try:
            tg_api("sendMessage", chat_id=cid, text="🚫 Unauthorized", parse_mode="")
        except Exception:
            pass
        log(f"BLOCKED uid={uid}")
        return

    # Photo / document → download to workdir, let Claude Code analyze it (#4)
    file_id, file_name = None, None
    if msg.get("photo"):
        file_id = msg["photo"][-1]["file_id"]  # largest size
        file_name = f"tg_photo_{mid}.jpg"
    elif msg.get("document"):
        d = msg["document"]
        file_id = d["file_id"]
        file_name = d.get("file_name", f"tg_file_{mid}")
    if file_id:
        caption = (msg.get("caption", "") or "").strip()
        if is_group and "@" in caption:
            caption = re.sub(r'@\w+\s*', '', caption).strip()
        saved = _download_tg_file(cid, file_id, file_name)
        if saved:
            text = (caption or "Tolong lihat/analisa file ini.") + f"\n\n(File terlampir: {saved})"
        else:
            send_msg(cid, "❌ Gagal download file.", thread_id=thread_id)
            return

    # Pending rename: next text message becomes the new session title
    # Add-provider wizard: collect answers step by step
    if cid in _pending_provider:
        ans = text.strip()
        if ans.lower() in ("/batal", "/cancel", "batal"):
            _pending_provider.pop(cid, None)
            send_msg(cid, "↩️ Tambah provider dibatalkan.")
            return
        st = _pending_provider[cid]
        # Mode paste: seluruh pesan ini = blob config, parse sekaligus.
        if st.get("mode") == "paste":
            _pending_provider.pop(cid, None)
            threading.Thread(target=_provider_ingest_paste, args=(cid, text), daemon=True).start()
            return
        if st.get("mode") == "rename":
            _pending_provider.pop(cid, None)
            ok, msg = _provider_rename(st.get("old", ""), ans.strip())
            send_msg(cid, f"✏️ `{st.get('old')}` → `{ans.strip()}`" if ok else f"❌ {msg}")
            return
        if st.get("mode") == "field":
            _pending_provider.pop(cid, None)
            name, field, val = st.get("name"), st.get("field"), ans.strip()
            if field == "base_url" and not val.startswith("http"):
                send_msg(cid, "❌ URL harus diawali http/https. Buka Edit lagi.")
                return
            kw = {"base_url": None, "token": None}
            kw[field] = val
            ok, msg = _provider_save(name, kw["base_url"], kw["token"], None, None, None)
            if not ok:
                send_msg(cid, f"❌ Gagal: {msg}")
                return
            lbl = "URL" if field == "base_url" else "token"
            # kalau ganti URL/token, sekalian tes konek biar ketahuan valid
            tok2 = (val if field == "token" else _provider_info(name).get("token", ""))
            base2 = (val if field == "base_url" else _provider_info(name).get("base_url", ""))
            tok_ok, tmsg = _test_endpoint(base2, tok2)
            send_msg(cid, f"✅ {lbl} `{name}` diupdate. Tes: {'✅ ' if tok_ok else '❌ '}{tmsg}")
            return
        step_idx = st["step"]
        key, _ = _PV_STEPS[step_idx]
        # Validate per field
        if key == "name":
            if not re.fullmatch(r"[a-z0-9_-]{1,30}", ans):
                send_msg(cid, "❌ Nama cuma boleh huruf kecil/angka/dash. Coba lagi:")
                return
            if ans in PROVIDERS:
                send_msg(cid, f"⚠️ Provider `{ans}` sudah ada — akan di-*update*. Lanjut atau /batal.")
        elif key == "base_url":
            if not ans.startswith("http"):
                send_msg(cid, "❌ URL harus diawali http/https. Coba lagi:")
                return
        # Slot model: kalau jawaban angka & ada daftar auto-load → resolve ke model
        if key in ("opus", "sonnet", "haiku"):
            if ans == "-":
                ans = ""  # inherit slot sebelumnya
            elif ans.isdigit():
                models = st.get("models") or []
                idx = int(ans) - 1
                if 0 <= idx < len(models):
                    ans = models[idx]
                else:
                    send_msg(cid, f"❌ Nomor di luar daftar (1–{len(models)}). Coba lagi:")
                    return
        st["data"][key] = ans
        st["step"] += 1
        # Setelah token → test endpoint + auto-load daftar model (nol biaya token)
        if key == "token":
            send_msg(cid, "🔌 Tes koneksi & ambil daftar model… sebentar.")
            ok, ids, info = _fetch_models(st["data"].get("base_url", ""), st["data"].get("token", ""))
            if not ok:
                _pending_provider.pop(cid, None)
                send_msg(cid, f"❌ Endpoint gagal: *{info}*\n\nProvider tidak disimpan (biar gak sia-sia). "
                              f"Cek base_url/token, lalu buka /provider → ➕ Tambah Provider lagi.")
                return
            st["models"] = ids
            send_msg(cid, f"✅ Konek! Ketemu *{len(ids)} model*. Sekarang pilih buat tiap slot 👇")
        # More steps?
        if st["step"] < len(_PV_STEPS):
            _pv_ask(cid)
            return
        # All collected → create provider
        d = st["data"]
        _pending_provider.pop(cid, None)
        opus = d["opus"]
        sonnet = d.get("sonnet") or opus
        haiku = d.get("haiku") or sonnet
        ok, msg = _provider_save(d["name"], d["base_url"], d["token"], opus, sonnet, haiku)
        if not ok:
            send_msg(cid, f"❌ Gagal simpan provider: {msg}")
            return
        send_msg(cid,
                 f"✅ *Provider `{d['name']}` berhasil ditambah!*\n\n"
                 f"📡 `{d['base_url']}`\n"
                 f"🧠 opus=`{opus}`\n     sonnet=`{sonnet}`\n     haiku=`{haiku}`\n\n"
                 f"Pakai sekarang: `/provider {d['name']}`")
        return

    if cid in _pending_rename:
        target_sid = _pending_rename.pop(cid)
        if text.strip().lower() in ("/batal", "/cancel", "batal"):
            send_msg(cid, "↩️ Ganti nama dibatalkan.")
            return
        new_title = text.strip()
        if not new_title or text.startswith("/"):
            send_msg(cid, "❌ Nama tidak valid. Klik ✏️ lagi untuk coba lagi.")
            return
        sess = load_sess(cid)
        ok = _rename_session(sess["workdir"], target_sid, new_title)
        if ok:
            send_msg(cid, f"✅ Sesi `{target_sid[:8]}` diganti nama jadi:\n*{new_title}*\n\nKetik /resume untuk lihat.")
        else:
            send_msg(cid, f"❌ Gagal ganti nama sesi `{target_sid[:8]}`.")
        return

    # Translate reply-keyboard button label → command
    if text in QUICK_BTN:
        text = QUICK_BTN[text]

    # Show model keyboard (from reply-keyboard "🧠 Model")
    if text == "_MODELKB_":
        tg_api("sendMessage", chat_id=cid, text=_to_md(f"🧠 *Pilih model* (aktif: `{_win_model(cid)}`)"),
               parse_mode="MarkdownV2", reply_markup=MODEL_KB)
        return
    if text == "_EFFORTKB_":
        cur = load_sess(cid).get("effort") or "default"
        tg_api("sendMessage", chat_id=cid,
               text=_to_md(f"🎯 *Effort level* (aktif: `{cur}`)\nMakin tinggi = mikir lebih dalam, lebih lama/mahal."),
               parse_mode="MarkdownV2", reply_markup=EFFORT_KB)
        return

    # Handle commands
    if text.startswith("/"):
        r = cmd(cid, text, msg)
        if r == "_MENU_":
            tg_api("sendMessage", chat_id=cid,
                   text=_to_md("⚡ *Aksi cepat* — pilih di bawah:"),
                   parse_mode="MarkdownV2", reply_markup=MENU_KB)
            return
        if r == "_RESUME_":
            sess = load_sess(cid)
            sessions = _cc_sessions(sess["workdir"])
            if not sessions:
                send_msg(cid, "📭 Belum ada sesi di folder ini.\nKetik pesan untuk mulai sesi baru.")
                return
            kb = _clean_kb(_build_resume_kb(cid, sessions, sess["session_id"], page=0))
            tg_api("sendMessage", chat_id=cid, text=_to_md(_resume_msg_text(cid, sessions)),
                   parse_mode="MarkdownV2", reply_markup=kb)
            return
        if r == "_PROVIDERKB_":
            tg_api("sendMessage", chat_id=cid,
                   text=_to_md(f"🔌 *Pilih provider* (aktif: `{_win_provider(cid)}`)"),
                   parse_mode="MarkdownV2", reply_markup=_build_provider_kb(cid))
            return
        if r == "_EFFORTKB_":
            cur = load_sess(cid).get("effort") or "default"
            tg_api("sendMessage", chat_id=cid,
                   text=_to_md(f"🎯 *Effort level* (aktif: `{cur}`)\nMakin tinggi = mikir lebih dalam, lebih lama/mahal."),
                   parse_mode="MarkdownV2", reply_markup=EFFORT_KB)
            return
        if r:
            send_msg(cid, r, mid)
            # Always (re)show reply keyboard on /start
            if text.startswith("/start"):
                tg_api("sendMessage", chat_id=cid, text="⌨️ Tombol cepat aktif di bawah 👇",
                       reply_markup=REPLY_KB, parse_mode="")
            return
        # A slash command ALWAYS terminates here. If cmd() returned None it
        # handled itself (async, e.g. /compress, /queue, /restart) — never let
        # the literal "/command" text fall through and get sent to Claude.
        return

    if not text.strip():
        return

    # Resolve the window for THIS message (by topic thread, not global "active").
    # This is what keeps separate topics from clobbering each other.
    store = _load_store(cid)
    if thread_id:
        # Map this topic to its own window. Auto-create mapping if first time
        # (e.g. topic created manually, not via /w).
        win_name = _get_window_for_thread(cid, thread_id)
        if not win_name:
            win_name = f"topic-{thread_id}"
            _topic_map.setdefault(cid, {})[thread_id] = win_name
            store.setdefault("topic_map", {})[str(thread_id)] = win_name
            _save_store(cid)
        log(f"msg cid={cid} thread={thread_id} → window '{win_name}'")
    else:
        win_name = store.get("active", "main")
    win = win_switch(cid, win_name)  # ensures window exists, sets active

    # Simpan prompt terakhir per-window untuk /retry
    win["last_prompt"] = text
    save_sess(cid)

    # Per-WINDOW lock: topic A busy must not block topic B
    lock_key = (cid, win_name)
    if lock_key in _busy:
        # Anti-wedge: kalau proc tercatat TAPI sudah exit, lock-nya nyangkut
        # (finally belum/ tak jalan) → bersihin & lanjut. Hanya saat proc benar2
        # mati (poll()!=None) supaya tak balapan dgn run yg lagi antri slot.
        _p = _running_procs.get(lock_key)
        if _p is not None and _p.poll() is not None:
            log(f"stale busy lock {lock_key} (proc dead) → auto-clear")
            _busy.discard(lock_key)
            _running_procs.pop(lock_key, None)
            _cancelled.discard(lock_key)
        else:
            tg_api("sendMessage", chat_id=cid,
                   text=f"⏳ Window '{win_name}' masih kerja… kirim /stop untuk paksa berhenti.",
                   parse_mode="", **({"message_thread_id": thread_id} if thread_id else {}))
            return

    global _current_chat_id
    _current_chat_id = cid
    _busy.add(lock_key)
    _cancelled.discard(lock_key)

    # Auto-compact (RESEED): kalau turn sebelumnya konteksnya nyentuh ambang,
    # ringkas sesi & pindah ke session_id baru yang bersih DULU sebelum proses
    # pesan ini. Ganti auto-compact loop interaktif yang nggak ada di mode -p.
    # Guard `AUTO_COMPACT_RATIO`: kalau auto-compact dimatikan (ratio=0), flag
    # `needs_compact` sisa dari session lama TIDAK boleh nge-trigger. Manual
    # `/compact` tetap jalan (jalur lain).
    if AUTO_COMPACT_RATIO and win.get("needs_compact"):
        try:
            tg_api("sendMessage", chat_id=cid,
                   text="🗜️ Konteks mendekati limit — meringkas & menyegarkan sesi dulu (sekali, biar hemat)…",
                   parse_mode="", **({"message_thread_id": thread_id} if thread_id else {}))
        except Exception:
            pass
        ok, info = _reseed_compact(cid, win_name)
        win = win_switch(cid, win_name)  # reload state (sid baru + pending_seed)
        try:
            tg_api("sendMessage", chat_id=cid,
                   text=("✅ Konteks diringkas & sesi disegarkan. Lanjut normal."
                         if ok else f"↩️ Gagal menyegarkan ({info}); lanjut dgn sesi lama."),
                   parse_mode="", **({"message_thread_id": thread_id} if thread_id else {}))
        except Exception:
            pass

    # Reseed seed-injection: kalau ada ringkasan dari compact (auto/manual),
    # sisipkan ke pesan ini sebagai konteks awal sesi baru, lalu hapus (sekali pakai).
    _seed = win.get("pending_seed")
    if _seed:
        text = ("[Ringkasan konteks dari sesi sebelumnya — lanjutkan dari sini]\n\n"
                + _seed + "\n\n———\n\n" + text)
        win.pop("pending_seed", None)
        save_sess(cid)

    wd = win["workdir"]
    sid = win["session_id"]
    win_provider = win.get("provider", PROVIDER)
    win_model = win.get("model", MODEL_SLOT)
    win_effort = win.get("effort")  # None = pakai default Claude Code
    win_verbose = win.get("verbose", False)  # /verbose: tampilkan text+thinking live

    # ── Live streaming: single growing bubble (Hermes-style) ─────────────────
    started = time.time()
    ls = LiveStream(cid, thread_id, win_name, win_provider, win_model,
                    win_verbose, lock_key, started)
    ls.start()

    # Acquire a global slot (RAM guard). If none free, tell the user we're
    # queued instead of silently hanging, then block until a slot frees.
    _slot_held = False
    try:
        if not _claude_slots.acquire(blocking=False):
            ls.note(f"⏳ antri slot… (maks {MAX_CONCURRENT} jalan bareng)")
            # Wait for a slot, but honor Stop while waiting
            while not _claude_slots.acquire(timeout=1):
                if lock_key in _cancelled:
                    ls.abort("⏹ Dibatalkan (sebelum mulai).")
                    return
        _slot_held = True

        result, usage = run_claude(text, cid, wd, sid, provider=win_provider,
                                   model=win_model, lock_key=lock_key,
                                   effort=win_effort, on_event=ls.on_event)
        ls.stop()

        # Record context size + arm auto-compact for the NEXT turn if over ambang.
        ctx = (usage or {}).get("context", 0)
        if ctx:
            win["ctx_tokens"] = ctx
            thr = _compact_threshold(win_model)
            if thr and ctx > thr and not win.get("needs_compact"):
                win["needs_compact"] = True
                try:
                    tg_api("sendMessage", chat_id=cid,
                           text=f"📊 Konteks ~{ctx//1000}k token (≥{int(AUTO_COMPACT_RATIO*100)}% "
                                f"limit {win_model}). Auto-compact sebelum pesan berikut.",
                           parse_mode="", **({"message_thread_id": thread_id} if thread_id else {}))
                except Exception:
                    pass
            save_sess(cid)

        # Finalize: single growing bubble → jawaban final (multi-msg + PICK).
        if not ls.finalize(result, usage, reply_to=mid):
            # PICK/MULTIPICK → render tombol (bubble sudah jadi footer).
            if not send_with_pick(cid, result, reply_to=mid, thread_id=thread_id):
                send_msg(cid, result, mid, thread_id=thread_id)
        save_sess(cid)

        # Notify if task was long (>2 min) — user may have left
        elapsed = time.time() - started
        if elapsed > 120 and usage:
            tg_api("sendMessage", chat_id=cid,
                   text=f"🔔 Selesai ({int(elapsed)}s · {usage.get('turns',0)} turn · "
                        f"${usage.get('cost',0):.3f})",
                   parse_mode="", **({"message_thread_id": thread_id} if thread_id else {}))
    except Exception as e:
        log(f"ERROR: {e}\n{traceback.format_exc()}")
        ls.abort()
        send_msg(cid, f"❌ Error: {str(e)[:200]}", thread_id=thread_id)
    finally:
        ls.stop()
        if _slot_held:
            _claude_slots.release()
        _busy.discard(lock_key)
        _cancelled.discard(lock_key)
        # Drain antrian /queue: ambil item pertama, proses sebagai pesan baru
        try:
            queue = win.get("queue") or []
            if queue and lock_key not in _cancelled:
                next_prompt = queue.pop(0)
                save_sess(cid)
                send_msg(cid, f"▶️ Lanjut antrian: _{next_prompt[:60]}_",
                         thread_id=thread_id or None)
                synth = {"message": {"chat": {"id": cid, "type": chat_type},
                                     "from": {"id": uid},
                                     "message_id": mid, "text": next_prompt}}
                if thread_id:
                    synth["message"]["message_thread_id"] = thread_id
                threading.Thread(target=_process_safe, args=(synth,), daemon=True).start()
        except Exception as e:
            log(f"queue drain error: {e}")

# ── Cron / scheduled tasks (#9) ───────────────────────────────────────────────
CRON_FILE = BOT_DIR / "cron.json"

def _load_cron() -> list:
    try:
        return json.loads(CRON_FILE.read_text())
    except Exception:
        return []

def _save_cron(jobs: list):
    CRON_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))

def _cron_command(cid: int, a: str) -> str:
    jobs = _load_cron()
    parts = a.split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    if sub == "add" and len(parts) > 1:
        # /cron add HH:MM <prompt>
        rest = parts[1].split(maxsplit=1)
        if len(rest) < 2 or ":" not in rest[0]:
            return "Format: `/cron add 07:00 cek server lapor ke aku`"
        tm, prompt = rest[0], rest[1]
        try:
            hh, mm = map(int, tm.split(":"))
            assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception:
            return "❌ Jam tidak valid. Format HH:MM (mis. 07:00)"
        sess = load_sess(cid)
        jobs.append({"cid": cid, "time": f"{hh:02d}:{mm:02d}", "prompt": prompt,
                     "workdir": sess["workdir"], "provider": sess.get("provider", PROVIDER),
                     "model": sess.get("model", MODEL_SLOT), "last_run": ""})
        _save_cron(jobs)
        return f"⏰ Jadwal ditambah: tiap hari **{hh:02d}:{mm:02d} WIB**\n`{prompt[:60]}`"
    if sub in ("del", "delete", "rm") and len(parts) > 1:
        try:
            idx = int(parts[1]) - 1
        except Exception:
            return "Format: `/cron del <nomor>`"
        mine = [j for j in jobs if j["cid"] == cid]
        if 0 <= idx < len(mine):
            jobs.remove(mine[idx])
            _save_cron(jobs)
            return f"🗑️ Jadwal #{idx+1} dihapus."
        return "❌ Nomor tidak ada."
    # list
    mine = [j for j in jobs if j["cid"] == cid]
    if not mine:
        return ("⏰ **Cron / Jadwal** (waktu WIB)\n\nBelum ada jadwal.\n\n"
                "Tambah: `/cron add 07:00 cek server lapor ke aku`\n"
                "Hapus: `/cron del 1`\n\n"
                "_Jam pakai WIB (Asia/Jakarta). Jalan tiap hari di jam itu._")
    lines = ["⏰ **Jadwal Aktif** (WIB)\n"]
    for i, j in enumerate(mine, 1):
        lines.append(f"{i}. **{j['time']} WIB** — `{j['prompt'][:50]}`")
    lines.append("\nTambah: `/cron add HH:MM <prompt>` · Hapus: `/cron del <n>`")
    return "\n".join(lines)

def _cron_loop():
    """Background: run scheduled jobs when their time matches (once per day)."""
    import threading
    while True:
        try:
            now = time.strftime("%H:%M")
            today = time.strftime("%Y-%m-%d")
            jobs = _load_cron()
            changed = False
            for j in jobs:
                if j.get("time") == now and j.get("last_run") != today:
                    j["last_run"] = today
                    changed = True
                    cid = j["cid"]
                    win = win_switch(cid, "cron")
                    win["workdir"] = j.get("workdir", WORKDIR)
                    win["provider"] = j.get("provider", PROVIDER)
                    win["model"] = j.get("model", MODEL_SLOT)
                    save_sess(cid)
                    try:
                        tg_api("sendMessage", chat_id=cid,
                               text=f"⏰ Menjalankan jadwal {j['time']}…", parse_mode="")
                        result, _u = run_claude(j["prompt"], cid, win["workdir"],
                                                win["session_id"], provider=win["provider"],
                                                model=win["model"])
                        send_msg(cid, f"⏰ *Hasil jadwal {j['time']}*\n\n{result}")
                        save_sess(cid)
                    except Exception as e:
                        log(f"cron job error: {e}")
            if changed:
                _save_cron(jobs)
        except Exception as e:
            log(f"cron loop error: {e}")
        time.sleep(30)

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    log("CC-TG (Claude Code wrapper) starting…")
    log(f"Providers: {', '.join(sorted(PROVIDERS.keys())) or 'none'}")
    log(f"Default provider: {PROVIDER}")
    log(f"Owner IDs: {OWNER_IDS}")
    log(f"Model slot: {MODEL_SLOT}")
    log(f"Default workdir: {WORKDIR}")

    # Register commands with Telegram (/ autocomplete)
    try:
        tg_api("setMyCommands", commands=[
            {"command": "help", "description": "❓ Panduan lengkap"},
            {"command": "menu", "description": "📋 Interactive menu"},
            {"command": "new", "description": "🆕 Window/sesi baru"},
            {"command": "resume", "description": "🔄 List/switch sessions"},
            {"command": "stop", "description": "⏹ Stop task berjalan"},
            {"command": "status", "description": "ℹ️ Info sesi/provider/model"},
            {"command": "queue", "description": "📥 Antri prompt (jalan berurutan)"},
            {"command": "background", "description": "🌙 Jalan paralel di window terpisah"},
            {"command": "retry", "description": "↻ Ulang pesan terakhir"},
            {"command": "agents", "description": "🤖 Status task/window berjalan"},
            {"command": "provider", "description": "🔌 Switch provider"},
            {"command": "model", "description": "⚙️ Switch model slot"},
            {"command": "effort", "description": "🎯 Atur kedalaman mikir (low→max)"},
            {"command": "verbose", "description": "📢 Tampilkan semua step live"},
            {"command": "usage", "description": "💰 Pemakaian token/biaya"},
            {"command": "cron", "description": "⏰ Jadwal tugas otomatis"},
            {"command": "title", "description": "🏷️ Beri judul sesi"},
            {"command": "whoami", "description": "🪪 Cek akses kamu"},
            {"command": "version", "description": "🤖 Versi Claude Code"},
            {"command": "yolo", "description": "⚡ Status mode YOLO"},
            {"command": "reset", "description": "🔄 New session"},
            {"command": "exit", "description": "👋 Exit current session"},
            {"command": "cd", "description": "📂 Change workdir"},
            {"command": "pwd", "description": "📂 Show workdir"},
            {"command": "restart", "description": "♻️ Restart bot"},
            {"command": "update", "description": "⬇️ Update bot dari GitHub + restart"},
            {"command": "compact", "description": "🗜️ Ringkas + fresh context (hemat token)"},
            {"command": "undo", "description": "↩️ Mundurkan N turn terakhir"},
            {"command": "clear", "description": "🧹 Bersihkan layar & sesi baru"},
        ])
        log("Commands registered with Telegram")
    except Exception as e:
        log(f"Failed to register commands: {e}")

    # Start cron scheduler (#9)
    import threading
    threading.Thread(target=_cron_loop, daemon=True).start()
    log("Cron scheduler started")

    # Main poll loop — dispatches each update to its own thread so one
    # long-running Claude Code call doesn't block other chats.
    import threading as _thr
    offset = 0
    while True:
        try:
            r = tg_api("getUpdates", offset=offset, timeout=30,
                       allowed_updates=["message", "callback_query"])
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                _thr.Thread(target=_process_safe, args=(u,), daemon=True).start()
        except KeyboardInterrupt:
            log("Shutdown.")
            break
        except Exception as e:
            log(f"Poll error: {e}")
            time.sleep(3)

def _process_safe(u: dict):
    """Wrapper: runs process() in a thread, catches errors to log."""
    try:
        process(u)
    except Exception as e:
        log(f"process error: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    main()
