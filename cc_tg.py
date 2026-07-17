#!/usr/bin/env python3
"""CC-TG — Claude Code Telegram bot (multi-provider).

All tools, agents, skills, MCP, compact are handled by Claude Code.
This bot just relays Telegram messages to the native `claude` binary
(provider via env injection from providers.json) and sends output back.

Usage: python3 cc_tg.py
"""
import json, os, sys, time, subprocess, re, uuid, sqlite3, traceback, threading, unicodedata, queue
from pathlib import Path
from datetime import datetime, timedelta
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

def _load_claude_env() -> list:
    """Muat ~/.claude/.env ke os.environ — di situlah API key MCP disimpan
    (CONTEXT7/EXA/Z_AI/MINIMAX…). Tanpa ini `${VAR}` di .mcp.json tak pernah
    ter-resolve → server MCP-nya gagal start di sesi bot (systemd unit tak
    baca shell profile). Var yang SUDAH ada di environ TIDAK ditimpa.
    Return: daftar NAMA var yang dimuat (nilai tak pernah di-log)."""
    f = _claude_home() / ".env"
    if not f.exists():
        return []
    loaded = []
    try:
        for ln in f.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            m = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", ln)
            if not m:
                continue
            k, v = m.group(1), m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if v and not os.environ.get(k):
                os.environ[k] = v
                loaded.append(k)
    except Exception:
        return loaded
    return loaded

_ENV_LOADED = _load_claude_env()

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
    "FORM MULTI-PERTANYAAN: kalau kamu perlu menanyakan BEBERAPA hal sekaligus "
    "(wizard/setup, mis. 'game apa + perangkat mana + fallback gimana'), JANGAN "
    "kirim pertanyaan satu-satu — pakai SATU blok FORM (bot merender wizard "
    "bertahap dengan navigasi ⬅️ ➡️ + tombol ✅ Kirim, persis AskUserQuestion "
    "di terminal):\n"
    "[[FORM]]\n"
    "[Q:multi] Game apa saja yang mau diarahkan ke ISP2?\n"
    "1. Mobile Legends\n"
    "2. Free Fire\n"
    "3. PUBG Mobile\n"
    "[Q:single] Aturan berlaku untuk perangkat mana?\n"
    "1. Semua perangkat LAN\n"
    "2. Device tertentu saja\n"
    "[[/FORM]]\n"
    "Aturan FORM: maksimal 4 pertanyaan × 8 opsi; [Q:single]=pilih satu, "
    "[Q:multi]=boleh banyak; pertanyaan singkat & opsi maks ~50 char; taruh blok "
    "di AKHIR pesan, penjelasan sebelumnya. User juga bisa MENGETIK jawaban bebas "
    "per pertanyaan (tombol ✍️), jadi tak perlu opsi 'lainnya'. "
    "Kalau cuma SATU pertanyaan → tetap pakai PICK/MULTIPICK biasa.\n\n"
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
_pending_cron: dict = {}         # cid -> {"step": str, "data": {...}} add-cron wizard
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
# Gerbang antrian GLOBAL per chat. Telegram jatah ~1 msg/detik/chat; tanpa
# gerbang, thread-thread (flusher edit + kirim chunk + notif) nyerbu barengan →
# badai 429 yg penaltinya makin panjang (bukti log 2026-07-02 11:42–11:54:
# sendMessage pun tewas → chunk jawaban HILANG). Gerbang juga MENYEBARKAN
# retry_after: sekali Telegram bilang "tunggu 35s", SEMUA thread chat itu nahan.
_tg_gate_lock = threading.Lock()
_tg_next_ok: dict = {}          # chat_id → epoch akhir PENALTI 429 (retry_after)
_tg_bucket: dict = {}           # chat_id → (sisa_token, ts_refill_terakhir)
_TG_RATE = 1.05                 # detik per token (rata aman ~1 pesan/detik/chat)
_TG_BURST = 3                   # burst: 3 pesan boleh keluar beruntun instan

def _tg_gate_wait(chat_id, abort=None):
    # Token bucket per chat: rangkaian pendek (narasi + kartu tool + jawaban)
    # keluar BERUNTUN tanpa jeda — kerasa cepat kayak Hermes — sementara laju
    # rata tetap 1/detik jadi anti-429 tetap utuh. Penalti retry_after dari
    # Telegram tetap dihormati penuh & berlaku utk semua thread chat ini.
    if not chat_id:
        return True              # getUpdates dkk (tanpa chat_id) bebas
    while True:
        if abort and abort():
            return False         # caller batal (mis. stream sudah difinalize)
        with _tg_gate_lock:
            now = time.time()
            tokens, last = _tg_bucket.get(chat_id, (float(_TG_BURST), now))
            tokens = min(float(_TG_BURST), tokens + (now - last) / _TG_RATE)
            nxt = _tg_next_ok.get(chat_id, 0.0)
            if now >= nxt and tokens >= 1.0:
                _tg_bucket[chat_id] = (tokens - 1.0, now)
                return True
            _tg_bucket[chat_id] = (tokens, now)
            wait = max(nxt - now, (1.0 - tokens) * _TG_RATE)
        time.sleep(min(max(wait, 0.05), 5.0))

def _tg_gate_penalize(chat_id, seconds: float):
    if not chat_id:
        return
    with _tg_gate_lock:
        _tg_next_ok[chat_id] = max(_tg_next_ok.get(chat_id, 0.0),
                                   time.time() + seconds)

def tg_api(method: str, **kw) -> dict:
    # sendMessage = jalur JAWABAN → paling gigih (pesan hilang itu fatal);
    # edit dkk boleh nyerah lebih cepat (flush berikutnya menimpa).
    # retry_after DIHORMATI penuh — dulu dipangkas max 10s, jadi nge-poke pas
    # penalti belum kelar → Telegram perpanjang penalti → 429 gak kelar-kelar.
    # `_abort` (callable→bool, opsional): caller bisa membatalkan call yg masih
    # ngantri/retry — dipakai flusher supaya edit BASI (teks streaming lama) yg
    # ketahan penalti 429 tidak menimpa kartu status setelah finalize.
    abort = kw.pop("_abort", None)
    chat_id = kw.get("chat_id")
    persistent = method == "sendMessage"
    attempts = 8 if persistent else 4
    max_wait = 120.0 if persistent else 45.0
    for attempt in range(attempts):
        if not _tg_gate_wait(chat_id, abort):
            return {}
        if abort and abort():
            return {}
        r = tg_http.post(f"{TG}/{method}", json=kw)
        if r.status_code == 429:
            try:
                wait = float(r.json().get("parameters", {}).get("retry_after", 1))
            except Exception:
                wait = 1.0
            wait = min(wait, max_wait) + 0.1
            _tg_gate_penalize(chat_id, wait)   # semua thread chat ini ikut nunggu
            time.sleep(wait)
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

# ── Lampiran foto/file: niru drag-drop terminal (path absolut quoted) ──────────
# Claude Code di terminal pas di-drag file dapet path absolut & baca via Read.
# Kita tiru persis: kasih instruksi + daftar path quoted. Bukan base64 (hindari
# token-bloat + risiko di-mangle proxy provider).
TG_FILE_LIMIT = 20 * 1024 * 1024   # Bot API getFile mentok 20MB

def _attach_prompt(caption: str, paths: list) -> str:
    quoted = " ".join(f"'{p}'" for p in paths)
    instr = (caption or "").strip() or "Tolong lihat/analisa file berikut."
    # Baris path dipisah biar tegas — mitigasi Claude skip Read.
    return f"{instr}\n\nBaca file berikut: {quoted}"

def _ensure_uploads_gitignore(workdir: str):
    """Kalau workdir repo git, pastikan uploads/ di-ignore biar gambar tak
    ke-push ke GitHub. Idempotent; bikin .gitignore kalau belum ada."""
    try:
        wd = Path(workdir)
        if not (wd / ".git").exists():
            return
        gi = wd / ".gitignore"
        lines = gi.read_text().splitlines() if gi.exists() else []
        if any(l.strip().rstrip("/") == "uploads" for l in lines):
            return
        with gi.open("a") as f:
            if lines and lines[-1].strip():
                f.write("\n")
            f.write("# CC-TG: lampiran dari Telegram\nuploads/\n")
        log(f"gitignore: + uploads/ → {gi}")
    except Exception as e:
        log(f"gitignore skip: {e}")

# Album buffer: Telegram kirim tiap foto album sebagai update terpisah (ikat
# media_group_id sama). Kita kumpulin + debounce, lalu proses SEKALI.
_album_buf: dict = {}
_album_lock = threading.Lock()
# Telegram kirim tiap foto album sbg update TERPISAH (media_group_id sama),
# datang berdekatan (~<2s). TAPI download tiap file makan waktu beda2 (file
# besar / koneksi lambat bisa 9s+, terbukti di log). BUG lama: _album_add baru
# dipanggil SETELAH download → debounce 1.5s keburu habis sebelum file kedua
# selesai → album pecah jadi banyak run "1 file".
# FIX: daftar item SAAT update masuk (SEBELUM download) via _album_note →
# tahu berapa "expected". Flush cuma ketika collect-window habis DAN semua
# download beres (received>=expected), atau hard-timeout (safety).
_ALBUM_COLLECT = 2.5    # detik tanpa update album baru = update album lengkap
_ALBUM_HARD_MAX = 120   # batas keras nunggu semua download (anti-hang)

def _album_note(key, ctx: dict):
    """Register 1 item album SEBELUM download — cuma naikin 'expected' & reset
    collect-timer. Dipanggil segera saat update foto/doc album masuk."""
    with _album_lock:
        b = _album_buf.get(key)
        if not b:
            b = {"paths": [], "caption": "", "expected": 0, "received": 0,
                 "collect_done": False, "collect_timer": None, "hard_timer": None,
                 **ctx}
            _album_buf[key] = b
            ht = threading.Timer(_ALBUM_HARD_MAX, _album_flush, args=(key, True))
            ht.daemon = True
            b["hard_timer"] = ht
            ht.start()
        b["expected"] += 1
        if b["collect_timer"]:
            b["collect_timer"].cancel()
        ct = threading.Timer(_ALBUM_COLLECT, _album_collect_done, args=(key,))
        ct.daemon = True
        b["collect_timer"] = ct
        ct.start()

def _album_deposit(key, path: str, caption: str):
    """Setoran hasil download 1 item album (path kosong = download gagal, tetap
    dihitung biar flush gak nunggu selamanya). Flush kalau semua sudah beres."""
    ready = False
    with _album_lock:
        b = _album_buf.get(key)
        if not b:
            return
        b["received"] += 1
        if path:
            b["paths"].append(path)
        if caption and not b["caption"]:
            b["caption"] = caption   # caption album biasanya nempel di 1 foto
        if b["collect_done"] and b["received"] >= b["expected"]:
            ready = True
    if ready:
        _album_flush(key)

def _album_collect_done(key):
    """Collect-window habis: update album dianggap lengkap. Flush kalau semua
    download sudah beres; kalau belum, biar _album_deposit terakhir yg flush."""
    ready = False
    with _album_lock:
        b = _album_buf.get(key)
        if not b:
            return
        b["collect_done"] = True
        if b["received"] >= b["expected"] and b["expected"] > 0:
            ready = True
    if ready:
        _album_flush(key)

def _album_flush(key, hard=False):
    with _album_lock:
        b = _album_buf.pop(key, None)
        if b:
            for tk in ("collect_timer", "hard_timer"):
                if b.get(tk):
                    b[tk].cancel()
    if not b or not b["paths"]:
        return
    text = _attach_prompt(b["caption"], b["paths"])
    m = {"chat": {"id": b["cid"], "type": b["chat_type"]},
         "from": {"id": b["uid"]}, "message_id": b["mid"], "text": text}
    if b.get("thread_id"):
        m["message_thread_id"] = b["thread_id"]
    log(f"album flush{' (hard-timeout)' if hard else ''}: {len(b['paths'])}/"
        f"{b['expected']} file → run")
    _process_safe({"message": m})   # re-masuk pipeline normal (1 run, semua path)

def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from Claude Code output."""
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)

# ── Markdown tables → boxed monospace grids (Telegram MarkdownV2 has no tables) ─
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")
_MAX_BOX_WIDTH = 120  # display cols; user pilih tabel lebar drpd dipotong ("gpp makan tempat")
_MIN_COL_W = 6        # lantai lebar kolom saat menyusutkan tabel super lebar
# Karakter box-drawing → deteksi tabel yg digambar model langsung (bukan pipe table)
_BOX_CHARS = set("┌┬┐├┼┤└┴┘─│╔╦╗╠╬╣╚╩╝═║╭╮╰╯┏┳┓┣╋┫┗┻┛━┃")

def _is_boxline(ln: str) -> bool:
    return sum(1 for c in ln if c in _BOX_CHARS) >= 2

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

def _wrap_cell(s: str, width: int) -> list:
    """Word-wrap isi sel ke `width` kolom display; token raksasa (URL/path
    tanpa spasi) dipotong keras. Konten TIDAK pernah dibuang, cuma turun baris."""
    out, cur = [], ""
    for tok in s.split():
        while _dispw(tok) > width:
            if cur:
                out.append(cur)
                cur = ""
            head, w = "", 0
            for ch in tok:
                cw = _dispw(ch)
                if w + cw > width:
                    break
                head += ch
                w += cw
            out.append(head)
            tok = tok[len(head):]
        if not tok:
            continue
        cand = (cur + " " + tok) if cur else tok
        if _dispw(cand) > width and cur:
            out.append(cur)
            cur = tok
        else:
            cur = cand
    if cur or not out:
        out.append(cur)
    return out

def _render_box(rows: list) -> str:
    ncols = max((len(r) for r in rows), default=0)
    if ncols == 0:
        return ""
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    widths = [max(_dispw(r[c]) for r in rows) for c in range(ncols)]
    # Kelebaran? Susutkan kolom terlebar selangkah demi selangkah — isi sel
    # di-WRAP ke beberapa baris, bukan dibuang (tabel wajib tampil utuh).
    while (sum(widths) + 3 * ncols + 1) > _MAX_BOX_WIDTH and max(widths) > _MIN_COL_W:
        widths[widths.index(max(widths))] -= 1
    def bar(l, m, r):
        return l + m.join("─" * (w + 2) for w in widths) + r
    out = [bar("┌", "┬", "┐")]
    for ri, r in enumerate(rows):
        cells = [_wrap_cell(r[c], widths[c]) for c in range(ncols)]
        tall = max(len(cl) for cl in cells)
        for cl in cells:
            cl.extend([""] * (tall - len(cl)))
        for k in range(tall):
            out.append("│" + "│".join(
                " " + _box_pad(cells[c][k], widths[c]) + " " for c in range(ncols)) + "│")
        if ri == 0:
            out.append(bar("├", "┼", "┤"))
    out.append(bar("└", "┴", "┘"))
    return "\n".join(out)

def _boxify_tables(text: str) -> str:
    """Rapikan tabel utk Telegram: pipe table (GFM) di-render ulang jadi box
    grid, tabel box-drawing yg digambar model langsung cukup dibungkus fence —
    dua-duanya jadi monospace (kolom sejajar + tap-to-copy). Isi ``` fence
    yg sudah ada TIDAK disentuh (idempotent, gak dobel-bungkus)."""
    parts = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    return "".join(p if p.startswith("```") else _boxify_plain(p) for p in parts)

def _boxify_plain(text: str) -> str:
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
            out.append("```\n" + box + "\n```" if box else "\n".join(lines[i:j]))
            i = j
        elif _is_boxline(lines[i]):
            # Tabel box-drawing yg digambar model sendiri: tanpa fence tampil
            # font proporsional (kolom mencong). Bungkus fence apa adanya.
            j = i
            while j < n and _is_boxline(lines[j]):
                j += 1
            if j - i >= 3:
                out.append("```\n" + "\n".join(lines[i:j]) + "\n```")
            else:
                out.extend(lines[i:j])
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
    """Markdown → Telegram MarkdownV2 yang rapi. Tabel jadi box monospace dulu.
    Gate cek juga garis vertikal box-drawing (│║┃) — tabel yg digambar model
    langsung gak punya '|' ASCII sama sekali."""
    if text and any(ch in text for ch in ("|", "│", "║", "┃")):
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
        else:
            buf = candidate
        # Pecah buf yg kepanjangan (termasuk satu paragraf raksasa tanpa \n\n).
        while len(buf) > limit:
            idx = buf.rfind("\n", 0, limit)
            if idx <= 0:
                idx = buf.rfind(" ", 0, limit)   # gak ada newline → potong di spasi
            if idx <= 0:
                idx = limit                      # gak ada spasi → potong keras
            chunks.append(buf[:idx])
            buf = buf[idx:].lstrip("\n ")
    if buf.strip():
        chunks.append(buf)
    # Repair ``` fences split across chunks
    fixed = []
    for c in chunks:
        if c.count("```") % 2 == 1:
            c = c + "\n```"
        fixed.append(c)
    return fixed

def _smart_chunks(text: str, hard_limit: int = 3500,
                  target: int = 450, min_total: int = 450,
                  max_paras: int = 1, max_msgs: int = 14) -> list:
    """Pecah jawaban gaya potongan ✂️/Hermes: default SATU PARAGRAF = SATU
    PESAN (permintaan user: long-press → Copy dapet persis bagian itu).
    Aturan:
      • Blok kode (``` … ```) SELALU jadi pesan sendiri (gampang copy kode).
      • max_paras=1 → tiap paragraf pesan sendiri; heading (# / **bold**)
        juga selalu berdiri sendiri. List ber-\\n tunggal tetap satu blok.
      • Jawaban pendek (≤ min_total) TETAP 1 pesan — biar nggak lebay.
      • Jawaban super panjang: paragraf digabung adaptif supaya total pesan
        ≤ ~max_msgs (bucket macu burst 3 lalu ~1 pesan/detik; 30 pesan = 30s
        nunggu — kebanyakan potongan malah bikin lambat).
    Tiap potongan tetap aman < hard_limit (limit Telegram)."""
    body = (text or "").strip()
    if len(body) <= min_total and "```" not in body:
        return [body]
    # Pisah jadi blok: code-fence vs teks biasa (regex tangkap ```...```).
    parts = re.split(r'(```.*?```)', body, flags=re.DOTALL)
    chunks, buf, n_para = [], "", 0

    def _flush():
        nonlocal buf, n_para
        if buf.strip():
            chunks.append(buf.strip())
        buf, n_para = "", 0

    def _is_heading(p: str) -> bool:
        first = p.split("\n", 1)[0].strip()
        return bool(re.match(r"^#{1,6}\s", first)
                    or re.fullmatch(r"\*\*[^*\n]{1,80}\*\*:?", first))

    for part in parts:
        if not part.strip():
            continue
        if part.startswith("```"):
            _flush()                       # tutup teks sebelumnya
            chunks.append(part.strip())    # code block = pesan sendiri
            continue
        for para in part.split("\n\n"):
            para = para.strip()
            if not para:
                continue
            if buf and (_is_heading(para) or n_para >= max_paras
                        or len(buf) + 2 + len(para) > target):
                _flush()
            buf = (buf + "\n\n" + para) if buf else para
            n_para += 1
    _flush()
    # Jaga tiap chunk < hard_limit (kalau ada paragraf/code raksasa).
    safe = []
    for c in chunks:
        if len(c) <= hard_limit:
            safe.append(c)
        else:
            safe.extend(_split_chunks(c, hard_limit))
    safe = [c for c in safe if c.strip()]
    # Rem jumlah pesan: kebanyakan potongan = lambat (bucket ~1 pesan/detik
    # setelah burst). Gabung pasangan bertetangga TERKECIL berulang sampai
    # total ≤ max_msgs — potongan kecil2 nyatu, paragraf gede tetap sendiri.
    def _is_code(c):
        return c.startswith("```")
    while len(safe) > max_msgs:
        best, bi = None, -1
        for i in range(len(safe) - 1):
            if _is_code(safe[i]) or _is_code(safe[i + 1]):
                continue
            tot = len(safe[i]) + len(safe[i + 1])
            if tot <= hard_limit and (best is None or tot < best):
                best, bi = tot, i
        if bi < 0:
            break                      # sisa code block semua / mentok limit
        safe[bi] = safe[bi] + "\n\n" + safe.pop(bi + 1)
    return safe

def _send_raw(chat_id: int, md: str, reply_to: int = 0, thread_id: int = 0) -> dict:
    """Send MarkdownV2 text, chunked. Falls back to plain on parse error.
    Cek `d.get("ok")` eksplisit — tg_api BISA nyerah stlh retry 429 exhaust &
    balikin {} TANPA raise exception, jadi cuma andalin try/except gak cukup
    (kelihatan "sukses" padahal diam2 gagal). Kalau tetap gagal, SATU retry
    lagi setelah jeda + logging — jawaban tak boleh hilang diam-diam."""
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
        plain_kw = dict(kw)
        plain_kw["text"] = re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!\\])', r'\1', part)[:4096]
        plain_kw.pop("parse_mode", None)

        def _try(payload):
            try:
                d = tg_api("sendMessage", **payload)
                return d, bool(d.get("ok"))
            except Exception:
                return {}, False

        res, ok = _try(kw)
        if not ok:
            res, ok = _try(plain_kw)          # mungkin parse error MarkdownV2
        if not ok:
            log(f"_send_raw: gagal kirim ke {chat_id} (part {i+1}/{len(parts)}), retry sekali…")
            time.sleep(2.5)
            res, ok = _try(plain_kw)          # retry terakhir, plain (paling aman)
            if not ok:
                log(f"_send_raw: retry terakhir JUGA gagal — pesan HILANG (chat_id={chat_id})")
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
# multipick_token -> (options, set[int], [version]) — selected indices + versi
# toggle (utk gugurkan edit keyboard BASI saat user nge-tap cepat beruntun)
_pending_multipick: dict = {}
# split_token -> body jawaban penuh (tombol "✂️ Pecah buat copy")
_pending_split: dict = {}

def _cap_pending(d: dict, cap: int = 200):
    """Jaga dict pending gak tumbuh tanpa batas — entry tertua dianggap
    kadaluarsa (dict Python 3.7+ terurut sesuai insersi)."""
    while len(d) > cap:
        d.pop(next(iter(d)))

def _split_pieces(body: str, max_pieces: int = 40) -> list:
    """Pecah jawaban jadi potongan kecil per paragraf / code-block untuk mode
    "✂️ Pecah buat copy" — tiap potongan dikirim sebagai pesan sendiri biar
    long-press → Copy dapet PERSIS bagian itu aja, gak perlu copy semuanya."""
    parts = re.split(r"(```.*?```)", body or "", flags=re.DOTALL)
    pieces = []
    for p in parts:
        if not p.strip():
            continue
        if p.startswith("```"):
            pieces.append(p.strip())
            continue
        for para in p.split("\n\n"):
            para = para.strip()
            if para:
                pieces.append(para)
    return pieces[:max_pieces]

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

# ── FORM multi-pertanyaan (wizard ala AskUserQuestion di terminal) ───────────
# Claude emit [[FORM]] [Q:multi] tanya? \n 1. opsi … [[/FORM]] → bot render
# wizard 1 pesan: pertanyaan tampil satu-satu, ⬅️ ➡️ navigasi, ☐/☑ toggle,
# ✍️ jawab ketik, ✅ Kirim menyusun semua jawaban jadi satu pesan balik.
_FORM_RE = re.compile(r"\[\[FORM\]\](.*?)\[\[/FORM\]\]", re.DOTALL | re.IGNORECASE)
_pending_form: dict = {}   # (cid, tok) -> state wizard
_form_await: dict = {}     # cid -> (tok, mid): nunggu jawaban KETIK utk form

def _parse_form(text: str):
    """Return (clean_text, questions|None); questions=[{q,multi,opts}] maks 4×8."""
    m = _FORM_RE.search(text or "")
    if not m:
        return text, None
    qs, cur = [], None
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        qm = re.match(r"^\[Q(?::(single|multi))?\]\s*(.+)$", line, re.IGNORECASE)
        if qm:
            if cur and cur["opts"]:
                qs.append(cur)
            cur = {"q": qm.group(2).strip()[:200],
                   "multi": (qm.group(1) or "single").lower() == "multi",
                   "opts": []}
            continue
        if cur is None:
            continue
        opt = re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line).strip()
        if opt and len(cur["opts"]) < 8:
            cur["opts"].append(opt[:60])
    if cur and cur["opts"]:
        qs.append(cur)
    clean = (text[:m.start()] + text[m.end():]).strip()
    return clean, (qs[:4] or None)

def _form_answer_of(st: dict, i: int):
    """Jawaban pertanyaan ke-i sebagai teks; None kalau belum dijawab."""
    if st["typed"][i]:
        return st["typed"][i]
    if st["sel"][i]:
        return ", ".join(st["qs"][i]["opts"][j] for j in sorted(st["sel"][i]))
    return None

def _form_text(st: dict) -> str:
    i, n = st["idx"], len(st["qs"])
    q = st["qs"][i]
    lines = []
    if st.get("intro"):
        lines += [st["intro"], ""]
    done = sum(1 for k in range(n) if _form_answer_of(st, k))
    lines.append(f"📋 *Pertanyaan {i+1}/{n}* — {done}/{n} terjawab")
    note = st.pop("note", None)   # peringatan sekali-tampil (mis. belum lengkap)
    if note:
        lines.append(note)
    lines.append(f"\n*{q['q']}*")
    if st.get("typing"):
        lines.append("✍️ *MODE KETIK AKTIF* — pesan teks berikutnya yang kamu "
                     "kirim = jawaban pertanyaan ini (tidak dikirim ke Claude).")
    else:
        lines.append("_boleh pilih lebih dari satu_" if q["multi"]
                     else "_pilih salah satu_")
    answered = [f"✔ {st['qs'][k]['q'][:34]} → *{_form_answer_of(st, k)[:42]}*"
                for k in range(n) if k != i and _form_answer_of(st, k)]
    if answered:
        lines.append("")
        lines += answered
    return "\n".join(lines)

def _form_kb(tok: str, st: dict) -> dict:
    i, n = st["idx"], len(st["qs"])
    q = st["qs"][i]
    rows = []
    for oi, opt in enumerate(q["opts"]):
        on = oi in st["sel"][i]
        pre = ("☑" if on else "☐") if q["multi"] else ("🔘" if on else "⚪")
        rows.append([{"text": f"{pre} {opt[:36]}",
                      "callback_data": f"fm:t:{tok}:{i}:{oi}"}])
    nav = []
    if i > 0:
        nav.append({"text": "⬅️ Kembali", "callback_data": f"fm:p:{tok}"})
    if i < n - 1:
        nav.append({"text": "➡️ Lanjut", "callback_data": f"fm:n:{tok}"})
    nav.append({"text": "✅ Kirim", "callback_data": f"fm:s:{tok}"})
    rows.append(nav)
    if st.get("typing"):
        rows.append([{"text": "✖️ Batal mode ketik",
                      "callback_data": f"fm:x:{tok}"}])
    else:
        rows.append([
            {"text": "✍️ Ketik jawaban", "callback_data": f"fm:w:{tok}"},
            {"text": "💬 Bahas dulu", "callback_data": f"fm:c:{tok}"},
        ])
    return {"inline_keyboard": rows}

def _form_render(cid: int, mid: int, tok: str):
    st = _pending_form.get((cid, tok))
    if not st:
        return
    try:
        tg_api("editMessageText", chat_id=cid, message_id=mid,
               text=_to_md(_form_text(st)), parse_mode="MarkdownV2",
               reply_markup=_form_kb(tok, st))
    except Exception:
        pass

def send_with_pick(chat_id: int, text: str, reply_to: int = 0, thread_id: int = 0) -> bool:
    """If text has a FORM/PICK/MULTIPICK block, send text + inline buttons.
    Returns True if a pick was rendered (caller should NOT also send the raw text)."""

    # FORM multi-pertanyaan lebih dulu (wizard bertahap)
    clean_f, form_qs = _parse_form(text)
    if form_qs:
        tok = uuid.uuid4().hex[:8]
        st = {"qs": form_qs, "sel": [set() for _ in form_qs],
              "typed": [None] * len(form_qs), "idx": 0,
              "intro": (clean_f or "").strip()[:1500]}
        _pending_form[(chat_id, tok)] = st
        _cap_pending(_pending_form)
        kw = {"chat_id": chat_id, "text": _to_md(_form_text(st)),
              "parse_mode": "MarkdownV2", "reply_markup": _form_kb(tok, st)}
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

    # Check MULTIPICK first (multi-select with toggle + confirm button)
    clean_mp, mp_options = _parse_multipick(text)
    if mp_options:
        tok = uuid.uuid4().hex[:8]
        _pending_multipick[(chat_id, tok)] = (mp_options, set(), [0])
        _cap_pending(_pending_multipick)
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
    _cap_pending(_pending_pick)
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
    _prewarm_async(cid)   # siapkan proses claude di muka → pesan pertama ~1 dtk

# ── Context window per SLOT model ─────────────────────────────────────────────
# Dipakai cuma buat NAMPILIN footer "ctx Nk/limitk" (info). Compaction sendiri
# 100% diurus NATIVE Claude Code (client-side, jalan di mode -p juga) — bot TIDAK
# lagi pakai RESEED. Investigasi 2026-07-01: isCompactSummary native terbukti
# muncul di sesi bot. Override per provider via config["context_windows"].
CONTEXT_WINDOWS = {"opus": 1_000_000, "sonnet": 1_000_000, "haiku": 200_000,
                   "fable": 1_000_000}   # probed 2026-07-13: claude-fable-5 = 1M

# Slot model per provider. "fable" HANYA ada di claude native: provider
# pihak-ketiga cuma punya mapping opus/sonnet/haiku (providers.json), jadi
# --model fable ke proxy = model tak dikenal.
MODEL_SLOTS_NATIVE = ("opus", "sonnet", "haiku", "fable")
MODEL_SLOTS_PROXY = ("opus", "sonnet", "haiku")

def _model_slots_for(provider) -> tuple:
    return MODEL_SLOTS_NATIVE if (provider or "claude") == "claude" else MODEL_SLOTS_PROXY

def _guard_model_after_provider_switch(sess) -> str:
    """Dipanggil SETELAH sess['provider'] diganti: kalau model window masih
    'fable' padahal provider baru bukan claude native → reset ke opus.
    Return catatan utk ditempel di pesan konfirmasi ('' kalau tak ada)."""
    if sess.get("model") == "fable" and sess.get("provider") != "claude":
        sess["model"] = "opus"
        return "\n⚠️ Model `fable` cuma ada di claude native → model di-reset ke `opus`."
    return ""
CONTEXT_WINDOWS.update(CFG.get("context_windows", {}))

def _ctx_limit_from_model_usage(model_usage) -> int:
    """Ukuran context window ASLI yg dilaporkan CLI (result.modelUsage[*]
    .contextWindow) — bukan tebakan statis CONTEXT_WINDOWS. Multi-model
    (subagent bisa pakai model lain) → pilih model pemegang konteks terbesar,
    itu model utama percakapan. Return 0 kalau CLI belum lapor (versi lama)."""
    best_used, lim = -1, 0
    for m in (model_usage or {}).values():
        try:
            used = (int(m.get("inputTokens", 0) or 0)
                    + int(m.get("cacheReadInputTokens", 0) or 0)
                    + int(m.get("cacheCreationInputTokens", 0) or 0))
            cw = int(m.get("contextWindow", 0) or 0)
        except Exception:
            continue
        if cw and used > best_used:
            best_used = used
            lim = cw
    return lim

# ── Window management ───────────────────────────────────────────────────────
def win_list(cid: int) -> list[dict]:
    """List all windows for a chat."""
    store = _load_store(cid)
    active = store.get("active", "main")
    result = []
    for name, w in store.get("windows", {}).items():
        result.append({"name": name, "active": name == active, **w})
    return result


# ── Agent View (ala `claude agents` di terminal) ────────────────────────────
# Di terminal: panah atas/bawah pilih sesi, Enter masuk. Di Telegram tak ada
# panah, jadi padanannya: tiap window jadi TOMBOL inline yang di-tap untuk
# pindah (set active). Window = "agent/sesi" paralel; yang lagi jalan = _busy.

def _av_titles(wins: dict) -> dict:
    """Map session_id → judul (dari file sesi, sumber sama dgn /resume)."""
    titles = {}
    for wd in {w.get("workdir", WORKDIR) for w in wins.values()}:
        try:
            for s in _cc_sessions(wd):
                t = (s.get("title") or s.get("summary") or "").strip()
                if t:
                    titles[s["id"]] = t
        except Exception:
            pass
    return titles

def _agentview_text(cid: int) -> str:
    store = _load_store(cid)
    active = store.get("active", "main")
    wins = store.get("windows", {})
    running = {w for (ci, w) in _busy if ci == cid}
    if not wins:
        return "🤖 *Agent View*\n\nBelum ada sesi. Kirim pesan untuk memulai."
    titles = _av_titles(wins)
    n_run = len(running & set(wins))
    home = str(Path.home())
    lines = [f"🤖 *Agent View* — {len(wins)} sesi · {n_run} lagi kerja\n"]
    now = time.time()
    any_busy = False
    for name, w in wins.items():
        sid = w.get("session_id", "")
        act = _ACT.get((cid, name)) or {}
        extra = []
        if name in running:
            any_busy = True
            state = f"🟢 lagi kerja — {_fmt_dur(now - act.get('start', now))}"
            if act.get("label"):
                extra.append(f"   ⚙️ {act['label'][:52]}")
            ags = list((act.get("agents") or {}).values())
            if ags:
                extra.append("   🤖 *Subagent:*")
                for a in ags[-4:]:
                    tp = f" [{a['type']}]" if a.get("type") else ""
                    if a["done"]:
                        extra.append(f"      ✅ {a['desc'][:30]}{tp} "
                                     f"({_fmt_dur(a['dur'])})")
                    else:
                        extra.append(f"      🟢 {a['desc'][:30]}{tp} — "
                                     f"{_fmt_dur(now - a['since'])}")
        elif _WARM.is_warm(sid):
            state = "🔥 siap — respon ~1 dtk"
        else:
            state = "⚪ idle"
        here = "  ← *kamu di sini*" if name == active else ""
        title = titles.get(sid, "")
        head = f"*{name}*" + (f" — _{title[:36]}_" if title else "")
        wd = w.get("workdir", WORKDIR).replace(home, "~")
        info = (f"   {state}{here}\n"
                f"   `{w.get('provider', PROVIDER)}/{w.get('model', MODEL_SLOT)}`"
                f" · 📂 {wd}")
        qn = len(w.get("queue") or [])
        if qn:
            info += f" · 📥 antri {qn}"
        block = f"{head}\n{info}"
        if extra:
            block += "\n" + "\n".join(extra)
        lines.append(block)
    slots = MAX_CONCURRENT - _claude_slots._value
    lines.append(f"\n⚡ Kapasitas paralel: {slots} jalan dari maks "
                 f"{MAX_CONCURRENT} task bareng")
    if any_busy:
        lines.append("🔄 _Panel update sendiri tiap ±4 dtk._")
    else:
        lines.append("🔄 _Panel bangun sendiri begitu ada task masuk "
                     "(±5 mnt sejak dibuka)._")
    lines.append("💡 *Cara pakai:* tap nama sesi = pindah ke sana · "
                 "⏹ = hentikan · 🆕 = sesi baru · 🔌 = kelola MCP")
    return "\n".join(lines)


def _agentview_kb(cid: int) -> dict:
    store = _load_store(cid)
    active = store.get("active", "main")
    wins = store.get("windows", {})
    running = {w for (ci, w) in _busy if ci == cid}
    rows = []
    for name, w in wins.items():
        is_act = name == active
        if name in running:
            mark = "🟢"
        elif _WARM.is_warm(w.get("session_id", "")):
            mark = "🔥"
        else:
            mark = "🎯" if is_act else "⚪"
        label = f"{mark} {name[:28]}" + (" ←" if is_act else "")
        row = [{"text": label, "callback_data": f"av:sw:{name[:40]}"}]
        # sesi yang lagi kerja → tombol ⏹ stop di sebelahnya
        if name in running:
            row.append({"text": "⏹ Stop", "callback_data": f"av:st:{name[:40]}"})
        rows.append(row)
    rows.append([
        {"text": "🆕 Sesi baru", "callback_data": "av:new"},
        {"text": "🔄 Segarkan", "callback_data": "av:rf"},
    ])
    rows.append([
        {"text": "🔌 MCP servers", "callback_data": "mcp:open"},
        {"text": "✖️ Tutup", "callback_data": "m_close"},
    ])
    return {"inline_keyboard": rows}


def _agentview_refresh(cid: int, mid: int):
    """Perbarui panel Agent View di tempat (setelah pindah/stop/baru)."""
    try:
        tg_api("editMessageText", chat_id=cid, message_id=mid,
               text=_to_md(_agentview_text(cid)), parse_mode="MarkdownV2",
               reply_markup=_agentview_kb(cid))
    except Exception:
        pass


_av_live: dict = {}   # (cid, mid) -> token: panel yang lagi auto-refresh

def _agentview_autorefresh(cid: int, mid: int):
    """Panel /agents jadi LIVE selama ±5 menit sejak dibuka/di-tap:
    - ada window kerja → refresh tiap 4 dtk (durasi/aktivitas bergerak);
    - lagi idle → PANTAU tiap 2 dtk, dan begitu ada task masuk / selesai
      (transisi kerja↔idle) panel langsung di-refresh. (Dulu loop berhenti
      kalau dibuka saat idle → panel 'mati' walau task masuk — itu bugnya.)
    Token registry cegah 2 loop di panel yang sama; loop lama gugur sendiri."""
    if not mid:
        return
    tok = uuid.uuid4().hex[:6]
    _av_live[(cid, mid)] = tok
    def _loop():
        try:
            was_busy = any(ci == cid for (ci, _w) in _busy)
            t_end = time.time() + 300
            while time.time() < t_end:
                time.sleep(4 if was_busy else 2)
                if _av_live.get((cid, mid)) != tok:
                    return   # ditutup / diganti panel MCP / loop baru
                busy = any(ci == cid for (ci, _w) in _busy)
                if busy or busy != was_busy:
                    _agentview_refresh(cid, mid)
                was_busy = busy
        finally:
            if _av_live.get((cid, mid)) == tok:
                _av_live.pop((cid, mid), None)
    threading.Thread(target=_loop, daemon=True).start()


# ── MCP manager (self-contained; mekanisme sama dgn terminal) ────────────────
# Status = `claude mcp list` (health-check CLI ASLI, bukan tebakan).
# On/off  = edit enabled/disabledMcpjsonServers di ~/.claude.json per project —
#           field PERSIS yang dipakai Claude Code (CLI tak punya subcommand
#           enable/disable). Backup + tulis atomic sebelum ubah.
_MCP_LINE_RE = re.compile(r"^(.+?):\s+(.+?)\s+-\s+(✔|✘|!)\s+(.+)$")
_MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,60}$")
_MCP_CACHE = {"at": 0.0, "servers": None}   # cache 60 dtk (health-check ±10 dtk)

def _mcp_json_names() -> set:
    """KATALOG server di ~/.mcp.json — daftar yang bisa di-on/off dari bot
    (connector claude.ai & server plugin dikelola di tempat lain).
    CATATAN 2026-07-13: .mcp.json (project scope) TIDAK pernah diaktifkan
    Claude Code di home dir — dia cuma jadi katalog. Yang benar-benar dimuat
    (terminal DAN bot) = USER SCOPE: `mcpServers` di ~/.claude.json."""
    try:
        data = json.loads((Path.home() / ".mcp.json").read_text(encoding="utf-8"))
        return set((data.get("mcpServers") or {}).keys())
    except Exception:
        return set()

def _mcp_user_scope() -> dict:
    """Server user-scope di ~/.claude.json — INI yang dimuat semua sesi."""
    try:
        data = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        return data.get("mcpServers") or {}
    except Exception:
        return {}

def _mcp_missing_env(name: str) -> list:
    """Var ${...} yang direferensikan config server tapi kosong di env bot.
    Server begini PASTI gagal start → tidak ikut dimuat, panel kasih 🟠."""
    try:
        data = json.loads((Path.home() / ".mcp.json").read_text(encoding="utf-8"))
        cfg = (data.get("mcpServers") or {}).get(name) or {}
        refs = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", json.dumps(cfg)))
        return sorted(v for v in refs if not os.environ.get(v))
    except Exception:
        return []

def _mcp_enabled_names(project: str = "") -> set:
    """Server katalog yang AKTIF = yang ada di user-scope ~/.claude.json.
    Dimuat otomatis oleh SEMUA sesi (terminal & bot) tanpa flag apa pun —
    makanya bot tak perlu --mcp-config lagi."""
    return _mcp_json_names() & set(_mcp_user_scope().keys())

def _mcp_extra_names() -> set:
    """Server user-scope DI LUAR katalog (mis. serena, telegram) — bawaan,
    tidak diutak-atik bot (permintaan user: 'MCP bawaan Claude biarin')."""
    return set(_mcp_user_scope().keys()) - _mcp_json_names()

def _mcp_servers(force: bool = False) -> list:
    """Jalankan `claude mcp list` + parse. Hasil di-cache 60 dtk supaya tombol
    toggle/refresh nggak nunggu health-check 10 dtk tiap tap."""
    now = time.time()
    if (not force and _MCP_CACHE["servers"] is not None
            and now - _MCP_CACHE["at"] < 60):
        return _MCP_CACHE["servers"]
    out = ""
    try:
        r = subprocess.run([get_claude_bin(None), "mcp", "list"], cwd=WORKDIR,
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            out = r.stdout
    except Exception:
        pass
    servers = []
    for line in out.splitlines():
        m = _MCP_LINE_RE.match(line.strip())
        if not m:
            continue
        name, target, icon, st = m.groups()
        if name.strip() in _mcp_json_names():
            continue   # server .mcp.json dirender dari state enable/init
        servers.append({"name": name.strip(), "target": target.strip(),
                        "icon": icon, "status": st.strip()})
    _MCP_CACHE.update(at=now, servers=servers)
    return servers

def _mcp_toggle(name: str, enabled: bool, project: str = "") -> bool:
    """Nyalakan/matikan server DI USER SCOPE (~/.claude.json `mcpServers`) —
    satu-satunya scope yang benar-benar dimuat Claude Code di sini. Efeknya
    kena SEMUA sesi: bot DAN terminal (sesi yang sudah jalan perlu restart).
    Nyala  = salin definisi dari katalog ~/.mcp.json ke user-scope.
    Mati   = buang dari user-scope (definisi tetap aman di katalog).
    Backup + tulis atomic. Return False kalau gagal."""
    cj = Path.home() / ".claude.json"
    try:
        data = json.loads(cj.read_text(encoding="utf-8")) if cj.exists() else {}
        catalog = json.loads(
            (Path.home() / ".mcp.json").read_text(encoding="utf-8"))
        catalog = catalog.get("mcpServers") or {}
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    srv = data.setdefault("mcpServers", {})
    if enabled:
        if name not in catalog:
            return False
        srv[name] = catalog[name]      # definisi apa adanya (${VAR}, bukan nilai)
    else:
        srv.pop(name, None)
    try:
        (cj.parent / f".claude.json.bak.{int(time.time())}").write_text(
            cj.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    try:
        tmp = cj.parent / f".claude.json.tmp.{os.getpid()}"
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, cj)
        return True
    except Exception:
        return False

def _mcp_panel_text(cid: int) -> str:
    enabled = _mcp_enabled_names()
    sess_state = _MCP_SESSION_STATE["servers"]
    names = sorted(_mcp_json_names())
    lines = [f"🔌 *MCP Servers* — {len(enabled)}/{len(names)} nyala",
             "_Berlaku di bot **dan** terminal (satu pengaturan)._\n"]

    for n in names:
        miss = _mcp_missing_env(n)
        if n not in enabled:
            lines.append(f"⚫ *{n}* — mati")
        elif miss:
            lines.append(f"🟠 *{n}* — butuh API key `{', '.join(miss)}`")
        elif sess_state.get(n) in ("connected", "pending"):
            lines.append(f"🟢 *{n}* — nyala")
        elif sess_state.get(n) == "failed":
            lines.append(f"🔴 *{n}* — gagal konek")
        else:
            lines.append(f"🔵 *{n}* — nyala (aktif di pesan berikutnya)")

    extra = sorted(_mcp_extra_names())
    if extra:
        lines.append(f"\n*Bawaan* (tak diutak-atik bot): "
                     f"{', '.join(f'`{e}`' for e in extra)}")
    others = [s for s in _mcp_servers()
              if s["name"] not in _mcp_user_scope()]
    if others:
        ok = sum(1 for s in others if s["icon"] == "✔")
        lines.append(f"*Connector/plugin akun:* {ok}/{len(others)} nyambung "
                     f"(kelola di claude.ai / terminal)")

    lines.append("\n💡 *Cara pakai:* tap ⏸ utk matikan · ▶️ utk nyalakan. "
                 "Bot langsung ikut; sesi terminal yang lagi buka perlu "
                 "dijalankan ulang.")
    lines.append("_Makin banyak nyala = start sesi makin berat — matikan yang "
                 "tak dipakai biar ngebut._")
    return "\n".join(lines)

def _mcp_panel_kb(cid: int) -> dict:
    enabled = _mcp_enabled_names()
    rows, pair = [], []
    for name in sorted(_mcp_json_names()):
        if not _MCP_NAME_RE.fullmatch(name):
            continue
        on = name in enabled
        label = f"{'⏸' if on else '▶️'} {name[:24]}"
        pair.append({"text": label, "callback_data": f"mcp:t:{name[:40]}"})
        if len(pair) == 2:
            rows.append(pair); pair = []
    if pair:
        rows.append(pair)
    rows.append([
        {"text": "🔄 Cek ulang", "callback_data": "mcp:hard"},
        {"text": "🤖 Agent View", "callback_data": "av:rf"},
    ])
    rows.append([{"text": "✖️ Tutup", "callback_data": "m_close"}])
    return {"inline_keyboard": rows}

def _mcp_panel_refresh(cid: int, mid: int):
    try:
        tg_api("editMessageText", chat_id=cid, message_id=mid,
               text=_to_md(_mcp_panel_text(cid)), parse_mode="MarkdownV2",
               reply_markup=_mcp_panel_kb(cid))
    except Exception:
        pass

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
    _prewarm_async(cid)   # siapkan proses claude di muka → pesan pertama ~1 dtk
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

# Mode Hermes: narasi antar-langkah & tool call dikirim REAL-TIME sebagai
# pesan2 kecil (kayak bot Hermes 137 di SS user) — bukan nunggu task kelar.
HERMES_MODE = bool(CFG.get("hermes_mode", True))

def _tool_line(name: str, inp: dict):
    """Satu baris utk bubble progress tool gaya Hermes. Return (jenis, teks):
    jenis 'bash' dirender fenced ```cmd``` di bawah header '💻 terminal'
    (header di-dedup utk command beruntun — persis gateway Hermes), sisanya
    baris label biasa. SEMUA tool masuk bubble — gak spam krn 1 bubble/batch."""
    n = (name or "").lower()
    inp = inp or {}
    if n in ("bash", "shell"):
        cmd = (inp.get("command") or "").strip()
        return ("bash", cmd[:280]) if cmd else ("label", "💻 terminal")
    return ("label", _tool_label(name, inp))

# ── Pelacak aktivitas per window (bahan panel /agents LIVE) ──────────────────
# Diisi dari wrapper on_event di run_claude: aktivitas terkini (tool/mikir/
# nulis), subagent Task yang di-spawn + status selesai, dan waktu mulai turn.
_ACT: dict = {}   # lock_key (cid, win) -> {start, at, label, agents{id:{...}}}

def _fmt_dur(s) -> str:
    s = max(0, int(s))
    return f"{s//60}m{s%60:02d}s" if s >= 60 else f"{s}s"

def _act_track(lock_key, ev: dict):
    """Update _ACT dari satu event stream — dipanggil tiap event saat turn
    berjalan. Murah (dict ops saja), aman dipanggil dari thread reader."""
    ent = _ACT.get(lock_key)
    if ent is None:
        return
    t = ev.get("type")
    now = time.time()
    if t == "stream_think":
        ent["label"] = "🧠 berpikir…"
    elif t == "stream_text" and ev.get("text"):
        ent["label"] = "💬 menulis jawaban…"
    elif t == "assistant":
        for b in ev.get("message", {}).get("content", []) or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = (b.get("name") or "")
            inp = b.get("input") or {}
            if name.lower() == "task":
                aid = b.get("id") or uuid.uuid4().hex[:6]
                desc = (inp.get("description")
                        or (inp.get("prompt") or "")[:40] or "subagent")
                ent["agents"][aid] = {"desc": desc[:60],
                                      "type": inp.get("subagent_type") or "",
                                      "since": now, "done": False, "dur": 0}
                while len(ent["agents"]) > 8:
                    ent["agents"].pop(next(iter(ent["agents"])))
                ent["label"] = f"🤖 delegasi ke subagent: {desc[:34]}"
            else:
                ent["label"] = _tool_label(name, inp)
    elif t == "user":
        # tool_result Task balik = subagent kelar
        for b in ev.get("message", {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                a = ent["agents"].get(b.get("tool_use_id"))
                if a and not a["done"]:
                    a["done"] = True
                    a["dur"] = now - a["since"]
    ent["at"] = now

# ── Warm pool: proses `claude` PERSISTENT per sesi ───────────────────────────
# Tiap pesan yang spawn proses baru bayar startup penuh (load MCP + CLAUDE.md
# + tool discovery ≈ 3-9 dtk). Dengan --input-format stream-json prosesnya
# hidup terus: pesan berikutnya masuk via stdin → first-token ~1 dtk.
# Pola sama persis dgn warm_pool.py di cc-tg-web (sudah jalan produksi).
WARM_ENABLED = bool(CFG.get("warm_pool", True))
WARM_TTL = int(CFG.get("warm_ttl", 900))   # dtk idle sebelum proses dimatikan
WARM_MAX = int(CFG.get("warm_max", 3))     # maks proses hidup (≈300MB RAM/proses)

class _WarmCancelled(Exception):
    pass

class _WarmTimeout(Exception):
    pass

# Status MCP NYATA per sesi — diisi dari event init tiap spawn claude
# (ground truth: server yang benar2 dimuat + connected/failed).
_MCP_SESSION_STATE = {"at": 0.0, "servers": {}}

def _handle_stream_ev(ev: dict, holder: dict, _emit):
    """Proses SATU event NDJSON dari claude CLI: sinkronkan holder (result,
    usage, buffer teks) + emit event sintetis stream_text/stream_think.
    Dipakai jalur cold DAN warm supaya rendering live-nya identik."""
    if ev.get("type") == "system" and ev.get("subtype") == "init":
        try:
            _MCP_SESSION_STATE["at"] = time.time()
            _MCP_SESSION_STATE["servers"] = {
                m.get("name"): m.get("status", "?")
                for m in (ev.get("mcp_servers") or [])}
        except Exception:
            pass
    if ev.get("type") == "assistant":
        # ⚠️ URUTAN CLI (terbukti probe 2026-07-02): event
        # `assistant [text]` keluar SEBELUM content_block_stop
        # turn yg sama → flush stop yg telat bisa ngisi ulang
        # buffer & bikin preview segmen BARU pasca-seal (pesan
        # dobel + kursor nyangkut). Kosongkan buffer di sini
        # (thread sama, ordered) supaya flush telat = no-op.
        for _b in ev.get("message", {}).get("content", []) or []:
            if isinstance(_b, dict) and _b.get("type") == "text":
                holder["text_buf"] = ""
                holder["text_sent"] = 0
                break
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
            # window ASLI dari CLI (0 = gak dilaporkan)
            "ctx_limit": _ctx_limit_from_model_usage(
                ev.get("modelUsage")),
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

class _WarmProc:
    """Satu proses claude persistent untuk satu sesi."""
    def __init__(self, chat_id, workdir, session_id, provider, model, effort):
        self.chat_id = chat_id
        self.workdir = workdir
        self.session_id = session_id
        self.provider = provider
        self.model = model
        self.effort = effort
        self.last_used = time.time()
        self.turn_lock = threading.Lock()   # 1 turn pada satu waktu per proses
        self.proc = self._spawn()

    def _spawn(self):
        env = os.environ.copy()
        env["TELEGRAM_BOT_TOKEN"] = TG_TOKEN
        env["TELEGRAM_CHAT_ID"] = str(self.chat_id)
        _provider_env(env, self.provider)
        if _session_exists(self.workdir, self.session_id):
            try:
                _repair_session(self.workdir, self.session_id)
            except Exception:
                pass
            flag = "--resume"
        else:
            flag = "--session-id"
        cmd = [get_claude_bin(self.provider), "-p", flag, self.session_id,
               "--model", self.model,
               "--output-format", "stream-json",
               "--input-format", "stream-json",
               "--verbose", "--include-partial-messages",
               "--append-system-prompt", TELE_SYSTEM_PROMPT]
        if self.effort and self.effort in EFFORT_LEVELS:
            cmd += ["--effort", self.effort]
        cmd.append("--dangerously-skip-permissions")
        # stderr → DEVNULL: kalau turn warm gagal, fallback cold yang
        # nangkep detail errornya. start_new_session → kill 1 pohon (MCP dkk).
        return subprocess.Popen(cmd, cwd=self.workdir, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)

    def alive(self):
        return self.proc.poll() is None

    def matches(self, workdir, provider, model, effort):
        return (self.workdir == workdir and self.provider == provider
                and self.model == model and self.effort == effort)

    def kill(self):
        try:
            _kill_process_tree(self.proc)
        except Exception:
            pass

    def interrupt(self) -> bool:
        """Setop turn berjalan TANPA bunuh proses (setara ESC di terminal).
        CLI balas control_response + result subtype=error_during_execution,
        proses tetap hidup — VERIFIED CLI 2.1.206. Return False = stdin putus."""
        try:
            self.proc.stdin.write((json.dumps(
                {"type": "control_request",
                 "request_id": f"intr-{int(time.time()*1000)}",
                 "request": {"subtype": "interrupt"}}) + "\n").encode())
            self.proc.stdin.flush()
            return True
        except Exception:
            return False

    def run_turn(self, prompt, on_event, lock_key):
        """Kirim 1 pesan via stdin, baca event sampai `result` (proses TETAP
        hidup buat turn berikutnya). Raise: _WarmCancelled (di-/stop),
        _WarmTimeout, RuntimeError (proses/stdin mati → caller fallback cold)."""
        # /stop bisa lepas _busy lebih dulu → pesan baru masuk saat turn lama
        # masih beres-beres pasca-interrupt. Lock cegah 2 reader di stdout sama.
        if not self.turn_lock.acquire(timeout=15):
            self.kill()
            raise RuntimeError("warm proc masih sibuk turn sebelumnya")
        try:
            return self._run_turn_locked(prompt, on_event, lock_key)
        finally:
            self.turn_lock.release()

    def _run_turn_locked(self, prompt, on_event, lock_key):
        def _emit(e):
            if on_event:
                try:
                    on_event(e)
                except Exception:
                    pass
        msg = {"type": "user", "message": {"role": "user",
               "content": [{"type": "text", "text": prompt}]}}
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            self.proc.stdin.flush()
        except Exception as e:
            raise RuntimeError(f"warm stdin putus: {e}")
        self.last_used = time.time()

        holder = {"result": "", "usage": {}}
        done = threading.Event()
        got = {"result": False}

        def _reader():
            try:
                for raw in self.proc.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    _handle_stream_ev(ev, holder, _emit)
                    if ev.get("type") == "result":
                        got["result"] = True
                        return      # SISAKAN stdout untuk turn berikutnya
            finally:
                done.set()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        start = time.time()
        intr_at = 0.0
        while not done.is_set():
            if lock_key is not None and lock_key in _cancelled:
                # GRACEFUL: interrupt dulu (turn berhenti, proses+MCP tetap
                # hidup → pesan berikutnya tetap warm ~1 dtk). Kill hanya
                # kalau interrupt gak mempan 10 dtk / stdin sudah putus.
                if not intr_at:
                    intr_at = time.time()
                    if not self.interrupt():
                        self.kill()
                elif time.time() - intr_at > 10:
                    self.kill()
            if time.time() - start > CLAUDE_TIMEOUT:
                self.kill()
                raise _WarmTimeout()
            done.wait(0.3)
        # Handler /stop lama bisa kill proses DULUAN (via _running_procs)
        # sebelum loop lihat _cancelled → reader EOF. Cek lagi di sini supaya
        # cancel TIDAK dianggap crash (crash = fallback cold = prompt jalan 2x).
        if lock_key is not None and lock_key in _cancelled:
            raise _WarmCancelled()
        if not got["result"]:
            raise RuntimeError("warm proc berakhir tanpa result")
        self.last_used = time.time()
        return _strip_ansi(holder["result"]), holder["usage"]

class _WarmPool:
    def __init__(self):
        self._procs = {}                 # session_id -> _WarmProc
        self._lock = threading.Lock()
        threading.Thread(target=self._janitor, daemon=True).start()

    def get(self, chat_id, workdir, session_id, provider, model, effort):
        with self._lock:
            wp = self._procs.get(session_id)
            if wp and (not wp.alive()
                       or not wp.matches(workdir, provider, model, effort)):
                wp.kill()                # model/provider ganti → respawn
                self._procs.pop(session_id, None)
                wp = None
            if wp is None:
                while len(self._procs) >= WARM_MAX:
                    oldest = min(self._procs.values(), key=lambda w: w.last_used)
                    oldest.kill()
                    self._procs.pop(oldest.session_id, None)
                wp = _WarmProc(chat_id, workdir, session_id,
                               provider, model, effort)
                self._procs[session_id] = wp
            return wp

    def discard(self, session_id):
        with self._lock:
            wp = self._procs.pop(session_id, None)
        if wp:
            wp.kill()

    def owns(self, proc) -> bool:
        """Proc ini milik pool? Dipakai handler /stop: warm → interrupt
        (jangan kill), cold → kill pohon seperti biasa."""
        with self._lock:
            return any(w.proc is proc for w in self._procs.values())

    def discard_all(self):
        """Buang SEMUA proses — dipanggil saat config MCP berubah (proses
        lama masih bawa set MCP lama). Respawn otomatis di pesan berikutnya."""
        with self._lock:
            procs = list(self._procs.values())
            self._procs.clear()
        for w in procs:
            w.kill()

    def is_warm(self, session_id) -> bool:
        """Sesi punya proses hidup siap pakai? (utk status 🔥 di Agent View)"""
        with self._lock:
            wp = self._procs.get(session_id)
            return bool(wp and wp.alive())

    def _janitor(self):
        while True:
            time.sleep(60)
            now = time.time()
            with self._lock:
                stale = [sid for sid, w in self._procs.items()
                         if (not w.alive()) or now - w.last_used > WARM_TTL]
                for sid in stale:
                    self._procs[sid].kill()
                    self._procs.pop(sid, None)

_WARM = _WarmPool()

def _prewarm_async(cid: int):
    """PRE-WARM: spawn proses claude utk window AKTIF chat ini di background.
    Dipanggil saat pindah window / sesi baru / resume — pas user selesai
    ngetik pesan pertama, prosesnya sudah siap → turn-1 pun ~1 dtk, bukan
    cold 3-9 dtk. Gagal pun tak apa (jalur biasa tetap jalan)."""
    if not WARM_ENABLED:
        return
    def _go():
        try:
            store = _load_store(cid)
            active = store.get("active", "main")
            if (cid, active) in _busy:
                return               # ada task jalan — jangan dobel proses
            w = store.get("windows", {}).get(active) or {}
            sid = w.get("session_id")
            if not sid:
                return
            eff = w.get("effort")
            _WARM.get(cid, w.get("workdir", WORKDIR), sid,
                      w.get("provider", DEFAULT_PROVIDER),
                      w.get("model", MODEL_SLOT),
                      eff if eff in EFFORT_LEVELS else None)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()

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

    # ⚡ Ukur first-token latency (muncul di journal — bukti cepat/lambat nyata)
    _t0 = time.time()
    _ft = {"seen": False, "path": "cold"}
    if lock_key is not None:
        # daftar aktivitas utk panel /agents live
        _ACT[lock_key] = {"start": _t0, "at": _t0,
                          "label": "🚀 menyiapkan…", "agents": {}}
        _cap_pending(_ACT, 100)
    _user_ev = on_event
    def on_event(e, __oe=_user_ev):
        if (not _ft["seen"]) and (
                (e.get("type") in ("stream_text", "stream_think") and e.get("text"))
                or e.get("type") == "assistant"):
            _ft["seen"] = True
            log(f"⚡ first-token {time.time()-_t0:.1f}s ({_ft['path']}) sesi {session_id[:8]}")
        if lock_key is not None:
            try:
                _act_track(lock_key, e)
            except Exception:
                pass
        if __oe:
            __oe(e)

    # ── JALUR WARM: proses persistent (MCP load 1x → first-token ~1 dtk) ────
    # Slash-command (mis. /compact) tetap cold: dia memutasi jsonl sesi, jadi
    # proses warm sesi itu dibuang dulu biar konteks in-memory-nya gak basi.
    if (prompt or "").lstrip().startswith("/"):
        _WARM.discard(session_id)
    elif WARM_ENABLED:
        wp = None
        try:
            wp = _WARM.get(chat_id, workdir, session_id, provider, model_slot,
                           effort if effort in EFFORT_LEVELS else None)
        except Exception as e:
            log(f"warm spawn gagal → cold: {e}")
        if wp is not None:
            if lock_key is not None:
                _running_procs[lock_key] = wp.proc
            _ft["path"] = "warm"
            try:
                result_text, usage = wp.run_turn(prompt, on_event, lock_key)
                if result_text:
                    if usage:
                        p = provider or PROVIDER
                        slot = _usage_log.setdefault(p, {"tokens": 0, "cost": 0.0, "calls": 0})
                        slot["tokens"] += usage.get("tokens", 0)
                        slot["cost"] += usage.get("cost", 0)
                        slot["calls"] += 1
                    return result_text, usage
                _WARM.discard(session_id)   # result kosong → coba jalur cold
            except _WarmCancelled:
                # Interrupt graceful: proses biasanya MASIH hidup → simpan di
                # pool biar pesan berikutnya tetap ~1 dtk. Buang cuma yg mati.
                if not wp.alive():
                    _WARM.discard(session_id)
                return "⏹ Dibatalkan.", {}
            except _WarmTimeout:
                _WARM.discard(session_id)
                return f"⏰ Timeout (>{CLAUDE_TIMEOUT//60} menit). Coba /reset.", {}
            except Exception as e:
                log(f"warm turn gagal → fallback cold: {e}")
                _WARM.discard(session_id)
            finally:
                if lock_key is not None:
                    _running_procs.pop(lock_key, None)
            _ft["path"] = "cold"

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
                        # parsing result/usage/deltas: SATU sumber, sama dgn warm
                        _handle_stream_ev(ev, holder, _emit)
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
🧠 **Model** — ganti model (opus/sonnet/haiku/fable*)
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
• `/model` / `/model <slot>` — ganti model (opus/sonnet/haiku; `fable` khusus claude native)

**⏱ Saat Claude bekerja**
• Progress live tiap step (bash, tulis file, cari…)
• ⏹ Stop / `/stop` — batalin task berjalan
• Notif otomatis kalau task >2 menit

**📎 Kirim file**
• Kirim foto/dokumen → otomatis dianalisa Claude
• Caption jadi instruksi

**💰 Pemakaian & Otomasi**
• `/cost` — token & biaya per provider
• `/cron` — jadwal otomatis (tombol ⏰ Cron = wizard lengkap: harian/mingguan/interval/sekali, pause, run-now, edit)

**🆕 Sesi & window**
• `/new [nama]` — window/sesi baru (fresh context)
• `/title <judul>` — beri judul sesi sekarang
• `/agents` (`/tasks`) — Agent View: daftar sesi, pindah, stop
• `/mcp` — status MCP servers + nyalakan/matikan per server

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
	• `/undo [N]` — mundur N turn terakhir (default 1)
	• `/compact` — ringkas konteks sesi sekarang (native, kayak di terminal)
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
        [{"text": "⏰ Cron"}, {"text": "📋 Menu"}, {"text": "⏹ Stop"}],
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
    "⏰ Cron": "_CRONKB_",
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
     {"text": "🤖 Agents", "callback_data": "m_agents"}],
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
    [{"text": "✨ Fable 5 (khusus claude native)", "callback_data": "set_fable"}],
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
        if proc and not _WARM.owns(proc):
            # cold → kill pohon. Warm → JANGAN kill: run_turn kirim interrupt
            # ≤0.3 dtk (turn berhenti, proses+MCP selamat, tetap cepat).
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

    # ── Agent View: pindah sesi / stop / baru / refresh ──────────────────────
    if data.startswith("av:"):
        sub = data[3:]
        if sub.startswith("sw:"):           # pindah (switch) ke window
            name = sub[3:]
            if name in _load_store(cid).get("windows", {}):
                win_switch(cid, name)
            _agentview_refresh(cid, mid)
        elif sub.startswith("st:"):         # stop task di window itu
            name = sub[3:]
            lk = (cid, name)
            _cancelled.add(lk)
            p = _running_procs.get(lk)
            if p and not _WARM.owns(p):     # warm → interrupt via run_turn
                _kill_process_tree(p)
            _busy.discard(lk)
            _agentview_refresh(cid, mid)
        elif sub == "new":                  # sesi baru di window aktif
            new_session(cid)
            _agentview_refresh(cid, mid)
        elif sub == "rf":
            _agentview_refresh(cid, mid)
        _agentview_autorefresh(cid, mid)   # panel LIVE selama ada yang kerja
        return

    # ── Panel MCP: on/off server / cek ulang ────────────────────────────────
    if data.startswith("mcp:"):
        _av_live.pop((cid, mid), None)     # panel ini bukan Agent View lagi
        sub = data[4:]
        if sub in ("open", "hard"):
            # health-check bisa ±10 dtk → kasih tanda loading dulu
            try:
                tg_api("editMessageText", chat_id=cid, message_id=mid,
                       text="🔌 Cek status MCP… (±10 dtk)", parse_mode="")
            except Exception:
                pass
            _mcp_servers(force=(sub == "hard"))
            _mcp_panel_refresh(cid, mid)
        elif sub.startswith("t:"):
            name = sub[2:]
            if name in _mcp_json_names() and _MCP_NAME_RE.fullmatch(name):
                turn_on = name not in _mcp_enabled_names()
                if _mcp_toggle(name, enabled=turn_on):
                    # proses warm masih bawa set MCP lama → buang; respawn
                    # otomatis dgn config baru di pesan berikutnya
                    _WARM.discard_all()
                    _MCP_CACHE["servers"] = None   # paksa health-check ulang
            _mcp_panel_refresh(cid, mid)
        return

    # "✂️ Pecah buat copy": kirim ulang jawaban sbg pesan-pesan kecil per
    # paragraf/code-block — long-press → Copy dapet persis bagian itu aja.
    if data.startswith("split:"):
        sp_tok = data.split(":", 1)[1]
        body = _pending_split.pop(sp_tok, None)
        sp_thread = cb["message"].get("message_thread_id", 0)
        try:
            # tombol dilepas biar gak dobel-tap (sekali pakai)
            tg_api("editMessageReplyMarkup", chat_id=cid, message_id=mid,
                   reply_markup={"inline_keyboard": []})
        except Exception:
            pass
        if not body:
            try:
                tg_api("sendMessage", chat_id=cid, parse_mode="",
                       text="⌛ Jawaban ini sudah kadaluarsa dari memori (bot restart / kelamaan).")
            except Exception:
                pass
            return
        def _send_pieces():
            for pc in _split_pieces(body):
                for chunk in _split_chunks(pc, 4000):
                    _send_raw(cid, _to_md(chunk), 0, sp_thread)
        threading.Thread(target=_send_pieces, daemon=True).start()
        return

    # MULTIPICK: toggle an option on/off
    # ── FORM wizard: toggle opsi / navigasi / ketik / kirim ──────────────────
    if data.startswith("fm:"):
        parts = data.split(":")
        act = parts[1] if len(parts) > 1 else ""
        tok = parts[2] if len(parts) > 2 else ""
        st = _pending_form.get((cid, tok))
        if not st:
            try:
                tg_api("editMessageReplyMarkup", chat_id=cid, message_id=mid,
                       reply_markup={"inline_keyboard": []})
                tg_api("sendMessage", chat_id=cid, parse_mode="",
                       text="⌛ Form kadaluarsa (bot sempat restart). Ketik jawabannya manual ya.")
            except Exception:
                pass
            return
        n = len(st["qs"])
        if act == "t" and len(parts) == 5:
            try:
                qi, oi = int(parts[3]), int(parts[4])
            except ValueError:
                return
            if 0 <= qi < n and 0 <= oi < len(st["qs"][qi]["opts"]):
                if st["qs"][qi]["multi"]:
                    if oi in st["sel"][qi]:
                        st["sel"][qi].discard(oi)
                    else:
                        st["sel"][qi].add(oi)
                else:
                    st["sel"][qi] = {oi}
                    st["typed"][qi] = None
                    if qi < n - 1:
                        st["idx"] = qi + 1   # pilih-satu → otomatis lanjut
            _form_render(cid, mid, tok)
        elif act == "p":
            st["idx"] = max(0, st["idx"] - 1)
            _form_render(cid, mid, tok)
        elif act == "n":
            st["idx"] = min(n - 1, st["idx"] + 1)
            _form_render(cid, mid, tok)
        elif act == "w":
            # JANGAN pakai answerCallbackQuery alert: callback sudah dijawab
            # generik di atas handler → alert kedua ditolak Telegram diam2.
            # Feedback lewat perubahan panel yang PASTI kelihatan.
            st["typing"] = True
            _form_await[cid] = (tok, mid)
            _form_render(cid, mid, tok)
        elif act == "x":
            st["typing"] = False
            _form_await.pop(cid, None)
            _form_render(cid, mid, tok)
        elif act == "c":
            thread_id = cb["message"].get("message_thread_id", 0)
            send_msg(cid, "💬 Silakan langsung ketik pertanyaan/diskusimu ke "
                          "Claude — form di atas tetap bisa diisi kapan saja.",
                     thread_id=thread_id or 0)
        elif act == "s":
            blank = [k for k in range(n) if not _form_answer_of(st, k)]
            if blank:
                st["idx"] = blank[0]
                st["note"] = (f"⚠️ *Masih {len(blank)} pertanyaan belum "
                              f"dijawab* — kubuka yang kosong di bawah.")
                _form_render(cid, mid, tok)
                return
            _pending_form.pop((cid, tok), None)
            _form_await.pop(cid, None)
            rows = [f"• {st['qs'][k]['q']} → {_form_answer_of(st, k)}"
                    for k in range(n)]
            thread_id = cb["message"].get("message_thread_id", 0)
            try:
                tg_api("editMessageText", chat_id=cid, message_id=mid,
                       text=_to_md("✅ *Form terkirim:*\n" + "\n".join(rows)),
                       parse_mode="MarkdownV2")
            except Exception:
                pass
            synth = {"message": {"chat": {"id": cid, "type": cb["message"]["chat"].get("type", "private")},
                                 "from": {"id": uid}, "message_id": mid,
                                 "text": "Jawaban form:\n" + "\n".join(rows)}}
            if thread_id:
                synth["message"]["message_thread_id"] = thread_id
            import threading as _t
            _t.Thread(target=_process_safe, args=(synth,), daemon=True).start()
        return

    if data.startswith("mpick:"):
        try:
            _, tok, idx_s = data.split(":", 2)
            idx = int(idx_s)
        except ValueError:
            return
        entry = _pending_multipick.get((cid, tok))
        if not entry:
            # Token kadaluarsa (bot sempat restart) — kasih tahu, jangan diam.
            try:
                tg_api("editMessageReplyMarkup", chat_id=cid, message_id=mid,
                       reply_markup={"inline_keyboard": []})
                tg_api("sendMessage", chat_id=cid, parse_mode="",
                       text="⌛ Pilihan ini kadaluarsa (bot sempat restart). Ketik jawabannya manual ya.")
            except Exception:
                pass
            return
        options, selected, ver = entry
        if not (0 <= idx < len(options)):
            return
        # Toggle
        if idx in selected:
            selected.discard(idx)
        else:
            selected.add(idx)
        # Rebuild keyboard. Versi naik tiap tap: kalau user nge-tap beruntun
        # cepat, edit2 LAMA yg masih ngantri gerbang digugurkan (_abort) — cuma
        # keadaan TERAKHIR yg dirender → toggle kerasa responsif, gak antre 1-1.
        ver[0] += 1
        my_ver = ver[0]
        rows = [[{"text": f"{'☑️' if j in selected else '☐'} {opt[:38]}",
                  "callback_data": f"mpick:{tok}:{j}"}]
                for j, opt in enumerate(options)]
        rows.append([{"text": f"✅ Selesai ({len(selected)} dipilih)",
                      "callback_data": f"mpdone:{tok}"}])
        kb = {"inline_keyboard": rows}
        try:
            tg_api("editMessageReplyMarkup", chat_id=cid, message_id=mid,
                   reply_markup=kb, _abort=lambda: ver[0] != my_ver)
        except Exception:
            pass
        return

    # MULTIPICK: confirm and submit all selected options
    if data.startswith("mpdone:"):
        _, tok = data.split(":", 1)
        entry = _pending_multipick.get((cid, tok))
        if not entry:
            try:
                tg_api("editMessageReplyMarkup", chat_id=cid, message_id=mid,
                       reply_markup={"inline_keyboard": []})
                tg_api("sendMessage", chat_id=cid, parse_mode="",
                       text="⌛ Pilihan ini kadaluarsa (bot sempat restart). Ketik jawabannya manual ya.")
            except Exception:
                pass
            return
        options, selected = entry[0], entry[1]
        if not selected:
            # Belum milih apa-apa → alert, dan entry JANGAN di-pop dulu —
            # dulu keburu di-pop di sini, jadi habis alert tombol2nya mati semua.
            try:
                tg_api("answerCallbackQuery", callback_query_id=cb_id,
                       text="Pilih dulu minimal satu opsi!", show_alert=True)
            except Exception:
                pass
            return
        _pending_multipick.pop((cid, tok), None)   # submit beneran → baru dilepas
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
        edit_md(cid, mid, _cron_panel_text(cid), reply_markup=_cron_panel_kb(cid))
    elif data == "m_model":
        edit_md(cid, mid, "⚙️ Pilih model:", reply_markup=MODEL_KB)
    elif data == "m_effort":
        cur = load_sess(cid).get("effort") or "default"
        edit_md(cid, mid, f"🎯 Effort level (aktif: `{cur}`)\nMakin tinggi = mikir lebih dalam, lebih lama/mahal.",
                reply_markup=EFFORT_KB)
    elif data in ("set_opus", "set_sonnet", "set_haiku", "set_fable"):
        slot = data.replace("set_", "")
        sess = load_sess(cid)
        if slot not in _model_slots_for(sess.get("provider")):
            # alert callback gak bisa (sudah dijawab generik di atas) → pesan biasa
            send_msg(cid, "✨ `fable` cuma ada di provider *claude* (native).\n"
                          "Pindah dulu: `/provider claude`, lalu pilih Fable lagi.")
            return
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
    # ── Cron callbacks ────────────────────────────────────────────────────
    elif data == "cron_panel":
        edit_md(cid, mid, _cron_panel_text(cid), reply_markup=_cron_panel_kb(cid))
    elif data == "cron_add":
        st = {"step": "type", "data": {}}
        if _cb_thread:
            st["thread_id"] = _cb_thread
        _pending_cron[cid] = st
        _cron_start_wizard(cid, mid)
    elif data == "cron_cancel":
        _pending_cron.pop(cid, None)
        edit_md(cid, mid, _cron_panel_text(cid), reply_markup=_cron_panel_kb(cid))
    elif data.startswith("crontype:"):
        t = data.split(":", 1)[1]
        st = _pending_cron.get(cid)
        if not st:
            edit_md(cid, mid, "↩️ Wizard kadaluarsa.", reply_markup=_cron_panel_kb(cid))
        else:
            st["data"]["type"] = t
            if t == "daily":
                st["step"] = "time"; _cron_ask_time(cid, mid)
            elif t == "weekly":
                st["step"] = "days"; st["data"]["days"] = []
                edit_md(cid, mid, "➕ *Tambah Jadwal* — Mingguan\n\nPilih *hari* (boleh lebih dari satu):",
                        reply_markup=_cron_days_kb([]))
            elif t == "interval":
                st["step"] = "interval"
                edit_md(cid, mid, "➕ *Tambah Jadwal* — Interval\n\nJalankan *tiap berapa jam*?",
                        reply_markup=_CRON_INTERVAL_KB)
            elif t == "once":
                st["step"] = "date"; _cron_ask_date(cid, mid)
    elif data.startswith("cronday:"):
        st = _pending_cron.get(cid)
        if st and st.get("step") == "days":
            d = int(data.split(":", 1)[1])
            sel = st["data"].setdefault("days", [])
            sel.remove(d) if d in sel else sel.append(d)
            edit_md(cid, mid, "➕ *Tambah Jadwal* — Mingguan\n\nPilih *hari* (boleh lebih dari satu):",
                    reply_markup=_cron_days_kb(sel))
    elif data == "cronday_done":
        st = _pending_cron.get(cid)
        if st and st["data"].get("days"):
            st["step"] = "time"; _cron_ask_time(cid, mid)
        else:
            tg_api("answerCallbackQuery", callback_query_id=cb_id, text="Pilih minimal 1 hari")
    elif data.startswith("cronival:"):
        st = _pending_cron.get(cid)
        if st:
            st["data"]["interval_h"] = int(data.split(":", 1)[1])
            st["step"] = "win"; _cron_ask_win(cid, mid)
    elif data.startswith("crondate:"):
        st = _pending_cron.get(cid)
        if st:
            st["data"]["date"] = data.split(":", 1)[1]
            st["step"] = "time"; _cron_ask_time(cid, mid)
    elif data.startswith("cronhh:"):
        st = _pending_cron.get(cid)
        if st:
            hh = int(data.split(":", 1)[1])
            st["data"]["_hh"] = hh
            edit_md(cid, mid, f"➕ *Tambah Jadwal* — Menit\n\nJam *{hh:02d}* — pilih *menit*:",
                    reply_markup=_cron_min_kb(hh))
    elif data == "cronhh_back":
        _cron_ask_time(cid, mid)
    elif data.startswith("cronmm:"):
        st = _pending_cron.get(cid)
        if st:
            hh = st["data"].get("_hh", 7)
            mm = int(data.split(":", 1)[1])
            st["data"]["time"] = f"{hh:02d}:{mm:02d}"
            st["data"].pop("_hh", None)
            st["step"] = "win"; _cron_ask_win(cid, mid)
    elif data.startswith("cronsid:"):
        st = _pending_cron.get(cid)
        if st:
            arg = data.split(":", 1)[1]
            if arg == "sep":
                st["data"]["target_win"] = "cron"
                st["data"].pop("target_sid", None)
            else:
                try:
                    s = _sess_cache.get(cid, [])[int(arg)]
                    st["data"]["target_win"] = "session"
                    st["data"]["target_sid"] = s["id"]
                    st["data"]["target_title"] = (s.get("title") or s.get("summary") or s["id"][:8])[:40]
                except Exception:
                    st["data"]["target_win"] = "cron"
            st["step"] = "prompt"; _cron_ask_prompt(cid, mid)
    elif data.startswith("cronview:"):
        j = _job_by_id(data.split(":", 1)[1])
        if j:
            edit_md(cid, mid, _cron_job_text(j), reply_markup=_cron_job_kb(j))
        else:
            edit_md(cid, mid, _cron_panel_text(cid), reply_markup=_cron_panel_kb(cid))
    elif data.startswith("cronrun:"):
        jid = data.split(":", 1)[1]
        j = _job_by_id(jid)
        if j:
            tg_api("answerCallbackQuery", callback_query_id=cb_id, text="🚀 Dijalankan…")
            _job_update(jid, run_count=j.get("run_count", 0) + 1,
                        last_run=datetime.now().isoformat(timespec="seconds"))
            threading.Thread(target=_run_cron_job, args=(dict(j),), daemon=True).start()
            j2 = _job_by_id(jid)
            edit_md(cid, mid, _cron_job_text(j2), reply_markup=_cron_job_kb(j2))
    elif data.startswith("cronpause:"):
        jid = data.split(":", 1)[1]
        _job_update(jid, enabled=False)
        j = _job_by_id(jid)
        edit_md(cid, mid, _cron_job_text(j), reply_markup=_cron_job_kb(j))
    elif data.startswith("cronresume:"):
        jid = data.split(":", 1)[1]
        _job_update(jid, enabled=True)
        j = _job_by_id(jid)
        edit_md(cid, mid, _cron_job_text(j), reply_markup=_cron_job_kb(j))
    elif data.startswith("croneditprompt:"):
        jid = data.split(":", 1)[1]
        _pending_cron[cid] = {"step": "editprompt", "jid": jid}
        edit_md(cid, mid, "✏️ Ketik *tugas baru* untuk jadwal ini (ganti prompt lama).",
                reply_markup={"inline_keyboard": [[{"text": "✖️ Batal", "callback_data": f"cronview:{jid}"}]]})
    elif data.startswith("crondel:"):
        jid = data.split(":", 1)[1]
        j = _job_by_id(jid)
        lbl = (j.get("title") or j.get("prompt", ""))[:40] if j else jid
        edit_md(cid, mid, f"🗑️ Hapus jadwal ini?\n\n*{_sched_label(j) if j else ''}*\n`{lbl}`",
                reply_markup={"inline_keyboard": [
                    [{"text": "✅ Ya, hapus", "callback_data": f"crondelok:{jid}"},
                     {"text": "↩️ Batal", "callback_data": f"cronview:{jid}"}]]})
    elif data.startswith("crondelok:"):
        _job_delete(data.split(":", 1)[1])
        edit_md(cid, mid, "🗑️ Jadwal dihapus.\n\n" + _cron_panel_text(cid),
                reply_markup=_cron_panel_kb(cid))

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
            note = _guard_model_after_provider_switch(sess)
            save_sess(cid)
            win = _load_store(cid).get("active", "main")
            edit_md(cid, mid, f"✅ Provider window **{win}** → `{name}`{note}\n\nKirim pesan untuk lanjut.")
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
            _prewarm_async(cid)   # proses siap duluan → pesan pertama ~1 dtk
            label = s["summary"] if s["summary"] else "(tanpa judul)"
            hist = _session_recent_history(sess["workdir"], s["id"], n_pairs=5)
            msg = (f"✅ *Lanjut sesi*\n\n"
                   f"💬 {label}\n"
                   f"🕐 {_rel_time(s.get('mtime', 0))} · `{s['id'][:8]}` · `{PROVIDER}`\n")
            if hist:
                msg += f"\n━━━ *📜 Obrolan terakhir* ━━━\n\n{hist}\n"
            msg += "\n_Kirim pesan untuk lanjut._"
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
        _av_live.pop((cid, mid), None)   # stop auto-refresh Agent View
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

def _session_recent_history(workdir: str, session_id: str, n_pairs: int = 5) -> str:
    """Return the last few user/assistant exchanges of a session, formatted
    for a Telegram preview. Reads only the file tail (fast even for huge files).
    Catatan: ini cuma TAMPILAN biar user inget — Claude tetap resume FULL sesi."""
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

    def _clip(s: str, limit: int) -> str:
        """Potong di batas kata (bukan tengah kata), tambah … kalau kepotong."""
        s = re.sub(r'\s+', ' ', s).strip()
        if len(s) <= limit:
            return s
        cut = s[:limit]
        sp = cut.rfind(' ')
        if sp > limit * 0.6:        # ada spasi yg masuk akal → potong di situ
            cut = cut[:sp]
        return cut.rstrip() + " …"

    PER_MSG = 600        # batas per pesan (naik dari 140)
    TOTAL = 3000         # batas total biar aman dari limit Telegram (4096)
    lines, used = [], 0
    for role, txt in msgs:
        who = "👤 *Kamu:*" if role == "user" else "🤖 *Claude:*"
        body = _clip(txt, PER_MSG)
        block = f"{who}\n{body}"
        if used + len(block) > TOTAL:
            lines.append("…_(lebih lama dipotong — Claude tetap ingat semua)_")
            break
        lines.append(block)
        used += len(block)
    return "\n\n".join(lines)

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
        slots = _model_slots_for(sess.get("provider"))
        if not a:
            return (f"Model window ini: `{cur}`\n"
                    f"Pilihan: {', '.join(slots)}"
                    + ("" if "fable" in slots else
                       "\n_(fable cuma di provider claude native)_"))
        if a in slots:
            sess["model"] = a          # per-window ONLY — jangan sentuh global MODEL_SLOT
            save_sess(cid)
            return f"🔄 Model window **{_load_store(cid).get('active','main')}** → `{a}`"
        if a == "fable":
            return ("❌ `fable` cuma ada di provider claude (native).\n"
                    "Pindah dulu: `/provider claude`, lalu `/model fable`.")
        return f"❌ Tidak dikenal: `{a}`\nPilihan: {', '.join(slots)}"
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
            _guard_model_after_provider_switch(sess)
            save_sess(cid)
        # If a specific session ID is provided, switch to it
        if target_sid and len(target_sid) >= 8:
            all_sess = _cc_sessions(wd)
            match = [s for s in all_sess if s["id"].startswith(target_sid.strip())]
            if match:
                new_id = match[0]["id"]
                sess["session_id"] = new_id
                save_sess(cid)
                _prewarm_async(cid)   # proses siap duluan → pesan pertama ~1 dtk
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
            note = _guard_model_after_provider_switch(sess)
            save_sess(cid)
            return (f"🔄 Provider window **{_load_store(cid).get('active','main')}** "
                    f"→ `{action}`{note}")
        return (f"❌ Tidak ada: `{action}`\n\n"
                f"Provider: {', '.join(sorted(PROVIDERS.keys()))}\n"
                f"Kelola: `/provider add|edit|del|info|test|models|reload`")
    if c == "/cost":
        # Konteks per window (ukuran ASLI termasuk cache) — penunjuk read-only.
        # Compaction otomatis ditangani NATIVE Claude Code.
        store = _load_store(cid)
        ctx_lines = []
        for name, w in store.get("windows", {}).items():
            ct = w.get("ctx_tokens", 0)
            if not ct:
                continue
            mdl = w.get("model", MODEL_SLOT)
            lim_real = w.get("ctx_limit")   # window asli dari CLI (kalau pernah kebaca)
            lim = lim_real or CONTEXT_WINDOWS.get(mdl, CONTEXT_WINDOWS.get("opus", 1_000_000))
            pct = ct / lim if lim else 0
            bar = "🟢" if pct < 0.6 else ("🟡" if pct < 0.85 else "🔴")
            mark = "" if lim_real else "~"   # '~' = perkiraan statis, tanpa '~' = asli
            ctx_lines.append(f"• **{name}** (`{mdl}`): {bar} {ct//1000}k /{mark}{lim//1000}k token")
        out = []
        if ctx_lines:
            out.append("🧮 **Konteks aktif** (auto-compact native saat mendekati limit)")
            out += ctx_lines
            out.append("_Mulai bersih: /new._")
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
                if p and not _WARM.owns(p):
                    try: _kill_process_tree(p)
                    except Exception: pass
            return ("⏹ Tidak ada task aktif. (State sudah dibersihkan.)"
                    if stray else "Tidak ada task yang sedang jalan.")
        killed = []
        for lk in targets:
            _cancelled.add(lk)              # warm: run_turn kirim interrupt ≤0.3s
            p = _running_procs.pop(lk, None)
            if p and not _WARM.owns(p):
                try: _kill_process_tree(p)  # cold: SIGTERM→SIGKILL se-process-group
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
    if c == "/mcp":
        # Panel MCP: health-check CLI ±10 dtk → kirim placeholder dulu,
        # isi panel dari thread biar chat nggak keblokir.
        thread_id = (msg or {}).get("message_thread_id", 0)
        kw = {"chat_id": cid, "text": "🔌 Cek status MCP… (±10 dtk)"}
        if thread_id:
            kw["message_thread_id"] = thread_id
        try:
            d = tg_api("sendMessage", **kw)
            pmid = (d.get("result") or {}).get("message_id")
        except Exception:
            return "❌ Gagal kirim panel MCP."
        if pmid:
            def _fill():
                try:
                    _mcp_servers(force=True)
                    _mcp_panel_refresh(cid, pmid)
                except Exception as e:
                    log(f"/mcp fill error: {e}")
            threading.Thread(target=_fill, daemon=True).start()
        return None
    if c in ("/agents", "/tasks"):
        # Agent View interaktif: tiap window/sesi jadi tombol yang bisa di-tap
        # untuk PINDAH ke sana (padanan panah atas/bawah di terminal Claude).
        thread_id = (msg or {}).get("message_thread_id", 0)
        kw = {"chat_id": cid, "text": _to_md(_agentview_text(cid)),
              "parse_mode": "MarkdownV2", "reply_markup": _agentview_kb(cid)}
        if thread_id:
            kw["message_thread_id"] = thread_id
        try:
            d = tg_api("sendMessage", **kw)
        except Exception:
            kw["text"] = re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!\\])', r'\1', kw["text"])[:4096]
            kw.pop("parse_mode", None)
            d = tg_api("sendMessage", **kw)
        # panel LIVE: update sendiri selama ada task jalan
        _agentview_autorefresh(cid, (d.get("result") or {}).get("message_id") or 0)
        return None
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
        store = _load_store(cid)
        win_name = store.get("active", "main")
        win = win_switch(cid, win_name)
        if not a.strip():
            q = win.get("queue", [])
            if not q:
                return "📭 Antrian kosong. Pakai: `/queue <prompt>`"
            lines = ["📋 *Antrian:*"]
            for i, item in enumerate(q, 1):
                lines.append(f"{i}. _{item[:50]}_")
            return "\n".join(lines)
        lock_key = (cid, win_name)
        prompt = a.strip()
        if lock_key in _busy:
            q = win.setdefault("queue", [])
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
        # /compact MANUAL = jalankan slash command NATIVE Claude Code via
        # `-p "/compact" --resume <sid>` (BUKAN reseed lama yg bikin loop — itu
        # sudah dicabut). Terbukti 2026-07-02: headless compact nulis marker
        # isCompactSummary ke file sesi & memori percakapan tetap nyambung.
        # Auto-compact native tetap jalan sendiri; ini cuma buat trigger manual.
        store = _load_store(cid)
        win_name = store.get("active", "main")
        if (cid, win_name) in _busy:
            return "⏳ Window ini masih kerja. Tunggu selesai (atau /stop) sebelum /compact."
        sess = load_sess(cid)
        sid = sess.get("session_id", "")
        wd = sess.get("workdir", WORKDIR)
        if not sid or not _session_exists(wd, sid):
            return "ℹ️ Sesi window ini masih kosong — belum ada yang bisa di-compact. (/new buat mulai bersih)"
        old_k = int(sess.get("ctx_tokens", 0)) // 1000
        lock_key = (cid, win_name)

        def _count_compact_markers() -> int:
            try:
                proj = _cc_project_dir(wd)
                f = proj / f"{sid}.jsonl" if proj else None
                if not f or not f.is_file():
                    return -1
                n = 0
                with open(f, errors="replace") as fh:
                    for ln in fh:
                        if '"isCompactSummary":true' in ln or '"isCompactSummary": true' in ln:
                            n += 1
                return n
            except Exception:
                return -1

        def _run_compact():
            _busy.add(lock_key)
            t0 = time.time()
            try:
                before = _count_compact_markers()
                run_claude("/compact", cid, wd, sid,
                           provider=sess.get("provider"), model=sess.get("model"),
                           lock_key=lock_key)
                if lock_key in _cancelled:
                    _cancelled.discard(lock_key)
                    txt = "⏹ Compact dibatalkan."
                elif before >= 0 and _count_compact_markers() > before:
                    # Marker BARU muncul → compact beneran kejadian, bukan asumsi.
                    sess["ctx_tokens"] = 0      # ukuran baru kebaca di pesan berikutnya
                    save_sess(cid)
                    was = f" (tadinya ~{old_k}k)" if old_k else ""
                    txt = (f"🧹 Compact selesai · {int(time.time()-t0)}s. Sesi diringkas "
                           f"native{was} — ukuran baru muncul di footer pesan berikutnya.")
                else:
                    txt = ("⚠️ Compact jalan tapi marker ringkasan tidak nambah — "
                           "kemungkinan sesi masih terlalu kecil buat diringkas. Cek /cost.")
            except Exception as e:
                txt = f"⚠️ Compact gagal: {e}"
            finally:
                _busy.discard(lock_key)
            try:
                tg_api("sendMessage", chat_id=cid, text=txt, parse_mode="")
            except Exception:
                pass
        threading.Thread(target=_run_compact, daemon=True).start()
        return ("🧹 Compact native jalan… CLI lagi meringkas sesi (~10–60s "
                "tergantung ukuran). Kukabari begitu selesai — window ini "
                "kekunci dulu biar gak tabrakan.")
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
    _HEARTBEAT = 0.7          # detak hidup: edit walau TANPA event baru (mis. di
                              # dalam Agent/subagent yg diam) → spinner+timer jalan.
                              # 0.7dtk = muter lebih cepat, masih aman (backoff
                              # adaptif mundur sendiri kalau Telegram mulai flood).
    _SPINNER = "🌑🌒🌓🌔🌕🌖🌗🌘"  # bulan muter — jelas & kebaca di HP
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
        self._spin = 0           # indeks frame spinner (muter tiap heartbeat)
        self._agents = {}        # id → (desc, start_ts) subagent yg SEDANG kerja
        self._lock = threading.Lock()
        self._dirty = False
        self._last_sent = None
        self._last_edit = 0.0
        self._interval = self._BASE_INTERVAL
        self._strikes = 0
        self._stop = threading.Event()
        self._thread = None
        self._typing_thread = None
        # Mode Hermes v2 (hasil reverse-eng gateway Hermes): teks diketik LIVE
        # ke pesan per-SEGMEN (kirim ≥24 char, edit berkala + kursor ▉, disegel
        # rich MarkdownV2 di batas tool/akhir); tool numpuk di SATU bubble
        # per batch yg di-edit nambah baris.
        self.hermes = HERMES_MODE
        self.reply_mid = 0            # message_id prompt user → quote header
        self._outq = queue.Queue()    # antrian kirim progresif (urutan terjaga)
        self._sender = None
        self._drained = False
        self._seg_id = None           # message_id segmen teks yg lagi diketik
        self._seg_ver = 0             # versi segmen (gugurkan edit basi pas seal)
        self._seg_shown = None        # teks preview terakhir yg beneran tampil
        self._seg_last = 0.0          # ts edit segmen terakhir
        self._last_seal = None        # teks segel terakhir (dedup vs result)
        # Mode Hermes: TANPA bubble spinner sama sekali (permintaan user) —
        # chat murni isi pesan Claude; liveness dari "typing…" + narasi/kartu
        # real-time. Bonus: nol edit bubble = nol rebutan jatah kirim.
        if self.hermes:
            self.st_id = 0
        else:
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
        # Spinner + timer di HEADER (atas). Sengaja TIDAK di ujung teks: kalau
        # nempel di badan teks, render streaming yg dipotong (…ekor) bisa nyangkut
        # jadi jawaban akhir & teks keliatan kepotong. Di header = badan teks aman.
        spin = self._SPINNER[self._spin % len(self._SPINNER)]
        return (f"{spin} {self.win} · {self.provider}/{self.model} · "
                f"⏱ {int(time.time()-self.started)}s")

    def _render(self):
        lines = [self._header()]
        if self.note_line:
            lines += ["", self.note_line]
        if self.feed and not self.hermes:   # hermes: tool tampil sbg kartu pesan
            lines += [""] + self.feed[-8:]
        # Tanda subagent SEDANG kerja (bisa lama & diam) — di zona status, bukan
        # di badan teks jawaban, jadi tak pernah motong teks. Spinner + timer per
        # agent = bukti Claude masih aktif lewat Agent.
        if self._agents:
            spin = self._SPINNER[self._spin % len(self._SPINNER)]
            now = time.time()
            for desc, ts in list(self._agents.values()):
                d = desc or "kerja"
                lines += ["", f"{spin} 🤖 Agent: {d} · ⏱ {int(now-ts)}s"]
        if self.verbose and self.think:
            tp = " ".join(self.think.split())
            if tp:
                lines += ["", f"💭 {tp[-200:]}"]
        if self.text and not self.hermes:   # hermes: narasi terkirim real-time
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
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and self.hermes:
                    # Turn text KOMPLIT → SEGEL segmen live-nya: preview yg
                    # lagi diketik di-upgrade jadi MarkdownV2 rapi (tanpa
                    # kursor) & dikunci. Swap state di bawah lock supaya
                    # flusher segmen berhenti nyentuh pesan yg disegel.
                    txt = (b.get("text") or "").strip()
                    if txt:
                        with self._lock:
                            sid = self._seg_id
                            self._seg_id = None
                            self._seg_ver += 1
                            self._seg_shown = None
                            self.text = ""
                        self._enqueue("seal", (sid, txt))
                if b.get("type") == "tool_use":
                    if self.hermes:
                        self._enqueue("tool", _tool_line(b.get("name"),
                                                         b.get("input", {})))
                    self._push(_tool_label(b.get("name"), b.get("input", {})))
                    # Task/subagent → tandai SEDANG kerja (bisa lama & diam).
                    if (b.get("name") or "").lower() == "task":
                        desc = (b.get("input", {}) or {}).get("description", "")[:40]
                        with self._lock:
                            self._agents[b.get("id")] = (desc, time.time())
                            self._dirty = True
        elif t == "user":
            for b in ev.get("message", {}).get("content", []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    # Subagent selesai → lepas tanda "sedang kerja".
                    tuid = b.get("tool_use_id")
                    if tuid in self._agents:
                        with self._lock:
                            self._agents.pop(tuid, None)
                            self._dirty = True
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
        if self.hermes:
            # Tanpa bubble: status transient (mis. antri slot) tetap harus
            # keliatan → kirim sbg pesan kecil sekali (dedup teks sama).
            if text and text != self.note_line:
                self.note_line = text
                self._enqueue("msg", text)
            return
        with self._lock:
            self.note_line = text
            self._dirty = True
        self._flush(force=True)

    # ---- Hermes v2: segmen live-typed (reverse-eng gateway Hermes) ----
    _CURSOR = " ▉"            # kursor ngetik (persis Hermes)
    _SEG_MIN = 24             # ambang char kirim pertama (persis Hermes)
    _SEG_IVAL = 1.0           # cadence edit segmen (Hermes 0.8; bucket kita 1.05)

    @staticmethod
    def _strip_pick_live(t: str) -> str:
        """Preview live dipotong di tag PICK/MULTIPICK — tag mentah jangan
        pernah tampil ke user; blok pilihan dirender caller via tombol."""
        up = (t or "").upper()
        cuts = [i for i in (up.find("[[PICK"), up.find("[[MULTIPICK"),
                            up.find("[[FORM")) if i >= 0]
        return t[:min(cuts)] if cuts else t

    def _seg_loop(self):
        while not self._stop.is_set():
            try:
                self._seg_tick()
            except Exception:
                pass
            self._stop.wait(0.15)

    def _seg_tick(self):
        with self._lock:
            buf = self._strip_pick_live(self.text or "").strip()
            sid, ver = self._seg_id, self._seg_ver
        if len(buf) < self._SEG_MIN:
            return
        if time.time() - self._seg_last < self._SEG_IVAL:
            return
        show = buf if len(buf) <= 3400 else "…" + buf[-3400:]
        if show == self._seg_shown:
            return
        if sid is None:
            kw = {"chat_id": self.cid, "text": show + self._CURSOR, "parse_mode": "",
                  # keburu di-seal/stop saat masih ngantri gerbang → batal total
                  "_abort": lambda: self._stop.is_set() or self._seg_ver != ver}
            if self.thread_id:
                kw["message_thread_id"] = self.thread_id
            if self.reply_mid:
                kw["reply_to_message_id"] = self.reply_mid
            d = tg_api("sendMessage", **kw)
            new_id = (d or {}).get("result", {}).get("message_id")
            self._seg_last = time.time()
            if new_id:
                stray = False
                with self._lock:
                    if self._seg_ver == ver:
                        self._seg_id = new_id
                        self._seg_shown = show
                    else:
                        stray = True   # keburu di-seal saat send in-flight
                if stray:
                    log(f"hermes: preview basi (mid={new_id}) dihapus — seal duluan")
                    try:
                        tg_api("deleteMessage", chat_id=self.cid, message_id=new_id)
                    except Exception:
                        pass
        else:
            d = tg_api("editMessageText", chat_id=self.cid, message_id=sid,
                       text=show + self._CURSOR, parse_mode="",
                       _abort=lambda: self._stop.is_set() or self._seg_ver != ver)
            self._seg_last = time.time()
            if (d or {}).get("ok"):
                self._seg_shown = show

    def _seal_rich(self, sid, txt):
        """Segel segmen: pesan preview di-upgrade jadi MarkdownV2 rapi tanpa
        kursor (persis finalize=True Hermes). Kepanjangan → chunk pertama
        nge-edit preview, sisanya pesan lanjutan."""
        chunks = _smart_chunks(txt, hard_limit=self._SPLIT) or [txt]
        first, ok = chunks[0], False
        if sid:
            try:
                md = _to_md(first)
            except Exception:
                md = None
            if md is not None and len(md) <= self._TG_LIMIT:
                try:
                    d = tg_api("editMessageText", chat_id=self.cid, message_id=sid,
                               text=md, parse_mode="MarkdownV2")
                    ok = bool(d.get("ok"))
                except Exception:
                    ok = False
            if not ok:
                try:
                    d = tg_api("editMessageText", chat_id=self.cid, message_id=sid,
                               text=first[:self._TG_LIMIT], parse_mode="")
                    ok = bool(d.get("ok"))
                except Exception:
                    ok = False
        if not ok:
            if sid:
                # Edit gagal total → preview (plain + kursor) jangan jadi
                # bangkai dobel di chat: hapus dulu, baru kirim versi rich.
                log(f"hermes: seal edit gagal (mid={sid}) — preview dihapus, kirim fresh")
                try:
                    tg_api("deleteMessage", chat_id=self.cid, message_id=sid)
                except Exception:
                    pass
            # Segmen pendek yg belum sempat punya preview (atau edit gagal)
            _send_raw(self.cid, _to_md(first), self.reply_mid, self.thread_id)
        for ch in chunks[1:]:
            _send_raw(self.cid, _to_md(ch), 0, self.thread_id)

    def _enqueue(self, kind, payload):
        if payload is not None:
            self._outq.put((kind, payload))

    def _sender_loop(self):
        # Kirim dari antrian satu-satu (bucket anti-429 yg macu tempo). Dipisah
        # dari reader supaya parsing event gak ke-block jatah Telegram.
        # Bubble tool GRUP (persis Hermes): baris2 tool numpuk di satu pesan
        # yg di-edit; ditutup tiap narasi muncul → tool berikutnya buka
        # bubble baru di bawahnya. Header '💻 terminal' di-dedup.
        tb_id, tb_lines, last_bash = None, [], False

        def _tb_text():
            return "\n".join(tb_lines)[:3900]

        while True:
            item = self._outq.get()
            if item is None:
                self._outq.task_done()
                break
            kind, payload = item
            try:
                if kind == "tool":
                    tkind, ttxt = payload
                    if not ttxt:
                        self._outq.task_done()
                        continue
                    if tkind == "bash":
                        blk = ("" if last_bash else "💻 terminal\n") + f"```\n{ttxt}\n```"
                        last_bash = True
                    else:
                        blk = ttxt
                        last_bash = False
                    if tb_id is None or len(_tb_text()) + len(blk) > 3800:
                        tb_lines, tb_id = [blk], None
                        kw = {"chat_id": self.cid, "text": _to_md(_tb_text()),
                              "parse_mode": "MarkdownV2"}
                        if self.thread_id:
                            kw["message_thread_id"] = self.thread_id
                        d = tg_api("sendMessage", **kw)
                        tb_id = (d or {}).get("result", {}).get("message_id")
                    else:
                        tb_lines.append(blk)
                        d = tg_api("editMessageText", chat_id=self.cid,
                                   message_id=tb_id, text=_to_md(_tb_text()),
                                   parse_mode="MarkdownV2")
                        if not (d or {}).get("ok"):
                            tb_id = None   # edit mati → batch berikut pesan baru
                elif kind == "seal":
                    tb_id, tb_lines, last_bash = None, [], False   # tutup batch tool
                    sid, txt = payload
                    if (_PICK_RE.search(txt) or _MULTIPICK_RE.search(txt)
                            or _FORM_RE.search(txt)):
                        # Blok pilihan/form dirender caller (tombol/wizard).
                        # Preview yg sempat tampil dihapus biar gak dobel.
                        if sid:
                            try:
                                tg_api("deleteMessage", chat_id=self.cid, message_id=sid)
                            except Exception:
                                pass
                    else:
                        self._seal_rich(sid, txt)
                    self._last_seal = txt
                elif kind == "msg":
                    tb_id, tb_lines, last_bash = None, [], False
                    _send_raw(self.cid, _to_md(payload), 0, self.thread_id)
            except Exception:
                pass
            self._outq.task_done()

    # ---- flusher thread (adaptive throttle) ----
    def start(self):
        if self.hermes:
            # Flusher segmen: ngetik live ke pesan per-segmen (kursor ▉).
            self._thread = threading.Thread(target=self._seg_loop, daemon=True)
            self._thread.start()
            self._sender = threading.Thread(target=self._sender_loop, daemon=True)
            self._sender.start()
        else:
            # Flusher bubble status (mode lama).
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        self._typing_thread = threading.Thread(target=self._typing_loop, daemon=True)
        self._typing_thread.start()

    def _age_interval(self):
        # Makin TUA task, makin jarang edit bubble. Edit 2x/detik selama 5 menit
        # ke pesan yg sama itu pemicu badai 429 (bukti log 2026-07-02 11:42–11:54)
        # — penalti Telegram numpuk sampai kiriman jawaban ikut mati. Awal task
        # tetap rapat biar responsif; di menit-menit panjang, update pelan
        # nggak kerasa bedanya tapi jatah rate Telegram aman.
        age = time.time() - self.started
        if age < 45:
            return self._BASE_INTERVAL
        if age < 120:
            return 2.0
        if age < 240:
            return 3.5
        if age < 420:
            return 5.0
        return 7.0

    def _loop(self):
        # Flush loop: HANYA edit bubble (ringan). typing() dipindah ke thread
        # terpisah (_typing_loop) supaya HTTP sendChatAction yg lambat TIDAK
        # mem-block flush → bubble nggak freeze.
        while not self._stop.is_set():
            # First content instan: kalau ada update tapi bubble belum pernah
            # nampilin teks/feed (masih "memulai…"), flush SEGERA tanpa nunggu
            # interval → token pertama langsung muncul (mirip terminal).
            first = self._last_sent is None and self._dirty
            # Hermes: pesan asli (narasi/kartu) yg lagi ngalir = bukti hidup.
            # Spinner NGALAH — jangan rebutan jatah kirim sama pesan beneran.
            if self.hermes and not first and self._outq.unfinished_tasks:
                self._stop.wait(0.15)
                continue
            iv = max(self._interval, self._age_interval())
            if self.hermes:
                iv = max(iv, 3.0)   # bubble tinggal heartbeat — 3s cukup hidup
            # Heartbeat: walau TANPA event baru (mis. lagi di dalam Agent yg diam,
            # atau Claude mikir lama), tetap maju-in spinner + timer biar bubble
            # kelihatan HIDUP — anti "keliatan mati padahal jalan". Saat flood-
            # backoff (interval naik), heartbeat ikut mundur biar nggak picu 429.
            beat = time.time() - self._last_edit >= max(self._HEARTBEAT, iv)
            if beat:
                with self._lock:
                    self._spin += 1
                    self._dirty = True
            if first or beat or time.time() - self._last_edit >= iv:
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
        # Kalau sudah di-stop (finalize mau ambil alih bubble), JANGAN nulis lagi —
        # cegah render streaming (kepotong + spinner) nimpa jawaban final. Kecuali
        # force=True (dipakai note() sebelum stop).
        if self._stop.is_set() and not force:
            return
        with self._lock:
            if not self._dirty and not force:
                return
            self._dirty = False
            txt = self._render()
        if txt == self._last_sent:
            return
        # Cek d.get("ok") EKSPLISIT — tg_api bisa nyerah stlh 429 retry exhaust
        # & balikin {} tanpa raise, jadi try/except doang gak kedeteksi (backoff
        # gak pernah aktif pas kondisi paling parah, malah makin ngebom TG).
        ok = False
        try:
            # _abort: kalau stream keburu di-stop saat edit ini masih ngantri
            # gerbang / kena penalti 429, edit BASI dibatalkan — jangan sampai
            # nimpa kartu status yg ditulis finalize.
            d = tg_api("editMessageText", chat_id=self.cid, message_id=self.st_id,
                       text=txt, parse_mode="",
                       _abort=(None if force else self._stop.is_set))
            ok = bool(d.get("ok"))
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
        # Tunggu thread flusher benar-benar mati sebelum lanjut, supaya edit
        # streaming terakhir (yg kepotong + spinner) TIDAK jalan setelah/berbarengan
        # dgn finalize → jawaban final dijamin jadi tulisan terakhir di bubble.
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1)
        # Mode Hermes: kuras antrian narasi/kartu SAMPAI habis dulu — jawaban
        # final (finalize) wajib muncul SETELAH semua pesan progresif.
        if self._sender and not self._drained:
            self._drained = True
            self._outq.put(None)
            deadline = time.time() + 120
            while self._outq.unfinished_tasks and time.time() < deadline:
                time.sleep(0.2)

    # ---- finalize: bubble → jawaban (markdown, multi-msg, PICK passthrough) ----
    def _footer(self, usage):
        dur = int(time.time() - self.started)
        u = usage or {}
        ctx = int(u.get("context", 0))
        # Limit ASLI dari CLI (modelUsage.contextWindow); fallback ke tebakan
        # statis CONTEXT_WINDOWS cuma kalau CLI gak lapor — dan itu DITANDAI
        # '~' biar kelihatan jujur mana angka asli mana perkiraan.
        real = int(u.get("ctx_limit", 0) or 0)
        limit = real or CONTEXT_WINDOWS.get(
            self.model, CONTEXT_WINDOWS.get("opus", 1_000_000))
        approx = "" if real else "~"
        pct = f" ({ctx * 100 // limit}%)" if ctx and limit else ""
        turns = u.get("turns", 0)
        extra = (f" · 📊 {ctx // 1000}k/{approx}{limit // 1000}k{pct}"
                 + (f" · {turns} turn" if turns else ""))
        return f"✅ selesai · {dur}s · {self.provider}/{self.model}{extra}"

    def _edit_md(self, text, kb=None):
        """Edit bubble dgn MarkdownV2; kalau gagal parse / kepanjangan → plain.
        Return True HANYA kalau Telegram BENERAN konfirmasi sukses (ok=true) —
        bukan cuma "tidak exception". tg_api bisa diam2 nyerah stlh 429 retry
        exhaust & balikin {} TANPA raise, jadi try/except doang bikin caller
        (finalize) ngira sukses padahal bubble gagal total ditulis.
        kb (opsional): inline keyboard nempel di hasil edit (mis. tombol ✂️)."""
        extra = {"reply_markup": kb} if kb else {}
        md = None
        try:
            md = _to_md(text)
        except Exception:
            md = None
        if md is not None and len(md) <= self._TG_LIMIT:
            try:
                d = tg_api("editMessageText", chat_id=self.cid, message_id=self.st_id,
                           text=md, parse_mode="MarkdownV2", **extra)
                if d.get("ok"):
                    return True
            except Exception:
                pass
        try:
            d = tg_api("editMessageText", chat_id=self.cid, message_id=self.st_id,
                       text=text[:self._TG_LIMIT], parse_mode="", **extra)
            return bool(d.get("ok"))
        except Exception:
            return False

    def finalize(self, result, usage, reply_to=0):
        self.stop()
        footer = self._footer(usage)
        # PICK/MULTIPICK → caller render tombol; bubble dikecilkan jadi footer.
        # Kalau edit footer gagal (rate-limit berat), fallback pesan baru — biar
        # gak nyangkut jadi teks streaming lama selamanya (kosmetik tapi bingungin).
        if (_PICK_RE.search(result or "") or _MULTIPICK_RE.search(result or "")
                or _FORM_RE.search(result or "")):
            if self.st_id and not self._edit_md(footer):
                _send_raw(self.cid, _to_md(footer), 0, self.thread_id)
            return False
        body = (result or "").strip() or "_(kosong)_"
        # Hermes v2: jawaban final = turn text terakhir yg SUDAH disegel rich
        # oleh sender (live-typed → seal). stop() di atas sudah menguras
        # antrian, jadi _last_seal final. Kalau sama → jangan kirim ulang.
        already = (self.hermes and self._last_seal
                   and " ".join(body.split()) == " ".join(self._last_seal.split()))
        try:
            # Tabel (pipe/box) → fence monospace SEBELUM chunking, biar jadi
            # pesan sendiri: kolom sejajar + tap-to-copy tabel utuh.
            body = _boxify_tables(body)
        except Exception:
            pass
        chunks = [] if already else _smart_chunks(body, hard_limit=self._SPLIT)
        # Desain anti-terpotong: bubble TIDAK pernah di-edit jadi jawaban.
        # Bubble menyusut jadi kartu status (edit kecil — kalaupun gagal cuma
        # kosmetik), jawaban SELALU pesan baru: sendMessage jauh lebih andal
        # daripada edit besar in-place yg rawan 429 pas task lama/berat.
        # Jawaban panjang → kartu status bawa tombol "✂️ Pecah buat copy":
        # di-tap → jawaban dikirim ulang per paragraf kecil, jadi long-press →
        # Copy dapet persis bagian yg diincar (gak perlu copy semuanya).
        footer_kb = None
        if len(body) > 500:
            sp_tok = uuid.uuid4().hex[:8]
            _pending_split[sp_tok] = body
            _cap_pending(_pending_split, 30)
            footer_kb = {"inline_keyboard": [[
                {"text": "✂️ Pecah buat copy", "callback_data": f"split:{sp_tok}"}]]}
        footer_ok = False
        if self.st_id:
            footer_ok = self._edit_md(footer, kb=footer_kb)
            if not footer_ok:
                log(f"finalize: edit bubble→status gagal (cid={self.cid}, "
                    f"st_id={self.st_id}) — footer nempel di pesan terakhir")
        for i, ch in enumerate(chunks):
            piece = ch
            if i == len(chunks) - 1 and not footer_ok and self.st_id:
                piece = piece + "\n\n———\n" + footer
            # Tempo antar-chunk diurus token-bucket gerbang (burst 3 → sisanya
            # ~1/detik) — gak perlu sleep manual lagi, kerasa lebih gesit.
            _send_raw(self.cid, _to_md(piece), reply_to if i == 0 else 0,
                      self.thread_id)
        if not self.st_id:
            # Mode Hermes (tanpa bubble): kartu status ✅ + tombol ✂️ jadi
            # pesan PENUTUP sendiri — penanda selesai + info durasi/context.
            kw = {"chat_id": self.cid, "text": _to_md(footer),
                  "parse_mode": "MarkdownV2"}
            if footer_kb:
                kw["reply_markup"] = footer_kb
            if self.thread_id:
                kw["message_thread_id"] = self.thread_id
            try:
                d = tg_api("sendMessage", **kw)
                if not d.get("ok"):
                    kw["text"] = footer
                    kw.pop("parse_mode", None)
                    tg_api("sendMessage", **kw)
            except Exception:
                pass
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

    # Form wizard: user tadi tap "✍️ Ketik jawaban" → pesan teks ini masuk
    # sebagai jawaban pertanyaan aktif form, BUKAN dikirim ke Claude.
    if text and not text.startswith("/") and cid in _form_await:
        f_tok, f_mid = _form_await.pop(cid)
        f_st = _pending_form.get((cid, f_tok))
        if f_st:
            f_st["typed"][f_st["idx"]] = text.strip()[:300]
            f_st["sel"][f_st["idx"]] = set()
            f_st["typing"] = False
            if f_st["idx"] < len(f_st["qs"]) - 1:
                f_st["idx"] += 1
            _form_render(cid, f_mid, f_tok)
            send_msg(cid, "✍️ Masuk ke form. Lanjut isi, atau tekan ✅ Kirim.",
                     thread_id=thread_id or None)
            return

    # Photo / document → download to workdir, niru drag-drop terminal (#4).
    # Album (banyak foto) di-buffer dulu lewat media_group_id biar 1 run, bukan
    # nge-trigger Claude berkali-kali.
    file_id, file_name = None, None
    if msg.get("photo"):
        file_id = msg["photo"][-1]["file_id"]  # largest size
        file_name = f"tg_photo_{mid}.jpg"
    elif msg.get("document"):
        d = msg["document"]
        if d.get("file_size", 0) > TG_FILE_LIMIT:
            send_msg(cid, "❌ File >20MB — di luar batas Telegram Bot API. "
                          "Kompres dulu atau kirim lewat cara lain.",
                     thread_id=thread_id)
            return
        file_id = d["file_id"]
        file_name = d.get("file_name", f"tg_file_{mid}")
    if file_id:
        caption = (msg.get("caption", "") or "").strip()
        if is_group and "@" in caption:
            caption = re.sub(r'@\w+\s*', '', caption).strip()
        mgid = msg.get("media_group_id")
        akey = (cid, mgid) if mgid else None
        # Album: DAFTAR item SEBELUM download (download bisa lama & bikin album
        # pecah kalau didaftar setelahnya). Non-album: langsung download.
        if akey:
            _album_note(akey, {"cid": cid, "uid": uid, "mid": mid,
                               "thread_id": thread_id, "chat_type": chat_type})
        saved = _download_tg_file(cid, file_id, file_name)
        _ensure_uploads_gitignore(load_sess(cid).get("workdir", WORKDIR))
        if akey:
            if not saved:
                log(f"album item download GAGAL (cid={cid}, {file_name})")
            _album_deposit(akey, saved or "", caption)   # tetap setor (path bisa kosong)
            return
        if not saved:
            send_msg(cid, "❌ Gagal download file.", thread_id=thread_id)
            return
        text = _attach_prompt(caption, [saved])

    # Pending rename: next text message becomes the new session title
    # Add-provider wizard: collect answers step by step
    # Cron wizard: collect typed answers (time/date/prompt/editprompt)
    if cid in _pending_cron:
        ans = text.strip()
        st = _pending_cron[cid]
        if ans.lower() in ("/batal", "/cancel", "batal"):
            _pending_cron.pop(cid, None)
            send_msg(cid, "↩️ Wizard cron dibatalkan.")
            return
        step = st.get("step")
        if step == "editprompt":
            jid = st.get("jid")
            _pending_cron.pop(cid, None)
            if _job_update(jid, prompt=ans, title=ans[:40]):
                j = _job_by_id(jid)
                send_msg(cid, "✅ Tugas jadwal diperbarui.")
                tg_api("sendMessage", chat_id=cid, text=_to_md(_cron_job_text(j)),
                       parse_mode="MarkdownV2", reply_markup=_cron_job_kb(j))
            else:
                send_msg(cid, "❌ Jadwal tak ditemukan.")
            return
        if step == "prompt":
            if not ans:
                send_msg(cid, "❌ Tugas kosong. Ketik tugasnya, atau /batal.")
                return
            st["data"]["prompt"] = ans
            ok, res = _cron_finalize(cid)
            if not ok:
                send_msg(cid, f"❌ Gagal: {res}")
                return
            nr = _fmt_when(_next_run(res))
            send_msg(cid, f"✅ *Jadwal dibuat!*\n\n{_sched_label(res)}\n"
                          f"⏭ Berikutnya: *{nr}*\n`{res['prompt'][:60]}`")
            tg_api("sendMessage", chat_id=cid, text=_to_md(_cron_panel_text(cid)),
                   parse_mode="MarkdownV2", reply_markup=_cron_panel_kb(cid))
            return
        # step tak dikenal → reset aman
        _pending_cron.pop(cid, None)

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
    if text == "_CRONKB_":
        tg_api("sendMessage", chat_id=cid, text=_to_md(_cron_panel_text(cid)),
               parse_mode="MarkdownV2", reply_markup=_cron_panel_kb(cid))
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
            # Window lagi kerja → JANGAN tolak. Antrikan otomatis (seperti
            # terminal Claude Code: ketik pesan berikutnya, dikerjakan setelah
            # yang sekarang selesai). Drain-nya di akhir _handle (win["queue"]).
            q = win.setdefault("queue", [])
            q.append(text)
            save_sess(cid)
            tg_api("sendMessage", chat_id=cid,
                   text=f"➕ Diantri (posisi {len(q)}) — dikerjakan setelah task "
                        f"sekarang selesai. /stop untuk batalkan yang jalan, "
                        f"/queue lihat antrian.",
                   parse_mode="", **({"message_thread_id": thread_id} if thread_id else {}))
            return

    global _current_chat_id
    _current_chat_id = cid
    _busy.add(lock_key)
    _cancelled.discard(lock_key)

    # (RESEED dicabut 2026-07-01) Compaction kini 100% NATIVE Claude Code:
    # otomatis, client-side, jalan di mode -p — tak ada lagi trigger RESEED,
    # seed-injection, atau needs_compact di sini.

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
    ls.reply_mid = mid   # narasi Hermes nge-quote prompt user (header konteks)
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

        # Catat ukuran konteks (cuma buat tampilan /status). Compaction sendiri
        # diurus NATIVE Claude Code — bot tak lagi nge-arm apa pun.
        ctx = (usage or {}).get("context", 0)
        if ctx:
            win["ctx_tokens"] = ctx
            if (usage or {}).get("ctx_limit"):
                win["ctx_limit"] = usage["ctx_limit"]   # window asli dari CLI
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

# ── Cron / scheduled tasks (grade produksi) ──────────────────────────────────
# Jadwal otomatis: jalanin prompt ke Claude pada waktu tertentu. Semua waktu WIB
# (server = Asia/Jakarta). 4 tipe: daily / weekly / interval / once.
# Schema job: {
#   id, cid, thread_id, title, prompt, workdir, provider, model,
#   type: "daily"|"weekly"|"interval"|"once",
#   time: "HH:MM"            (daily/weekly/once)
#   days: [0..6]             (weekly; 0=Senin..6=Minggu)
#   interval_h: int          (interval; tiap N jam)
#   date: "YYYY-MM-DD"       (once)
#   enabled: bool, last_run: iso, run_count: int, created: iso,
#   anchor: iso              (interval; titik mulai hitung)
# }
CRON_FILE = BOT_DIR / "cron.json"
_DOW = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
_DOW_FULL = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

def _load_cron() -> list:
    try:
        jobs = json.loads(CRON_FILE.read_text())
    except Exception:
        return []
    # Migrasi job lama (cuma punya time+prompt+last_run "YYYY-MM-DD") → schema baru.
    changed = False
    for j in jobs:
        if "id" not in j:
            j["id"] = uuid.uuid4().hex[:8]; changed = True
        if "type" not in j:
            j["type"] = "daily"; changed = True
        j.setdefault("title", "")
        j.setdefault("enabled", True)
        j.setdefault("run_count", 0)
        j.setdefault("thread_id", 0)
        j.setdefault("created", "")
        j.setdefault("target_win", "cron")  # window tujuan; "cron"=terpisah (lama)
        # last_run lama formatnya "YYYY-MM-DD" (tanggal). Biarkan — _due nanganin.
    if changed:
        _save_cron(jobs)
    return jobs

def _save_cron(jobs: list):
    CRON_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))

def _my_jobs(cid: int) -> list:
    return [j for j in _load_cron() if j.get("cid") == cid]

def _job_by_id(jid: str):
    for j in _load_cron():
        if j.get("id") == jid:
            return j
    return None

def _job_update(jid: str, **fields) -> bool:
    jobs = _load_cron()
    for j in jobs:
        if j.get("id") == jid:
            j.update(fields); _save_cron(jobs); return True
    return False

def _job_delete(jid: str) -> bool:
    jobs = _load_cron()
    n = len(jobs)
    jobs = [j for j in jobs if j.get("id") != jid]
    if len(jobs) != n:
        _save_cron(jobs); return True
    return False

def _next_run(j: dict, now: datetime = None) -> datetime | None:
    """Hitung kapan job jalan BERIKUTNYA (WIB). None kalau habis (once lewat)."""
    now = now or datetime.now()
    t = j.get("type", "daily")
    if t == "interval":
        ih = max(1, int(j.get("interval_h", 24)))
        try:
            anchor = datetime.fromisoformat(j["anchor"]) if j.get("anchor") else now
        except Exception:
            anchor = now
        if j.get("last_run"):
            try:
                base = datetime.fromisoformat(j["last_run"])
            except Exception:
                base = anchor
        else:
            base = anchor - timedelta(hours=ih)  # biar jalan pertama dekat anchor
        nxt = base + timedelta(hours=ih)
        while nxt < now:
            nxt += timedelta(hours=ih)
        return nxt
    # tipe berbasis jam HH:MM
    try:
        hh, mm = map(int, j.get("time", "07:00").split(":"))
    except Exception:
        hh, mm = 7, 0
    if t == "once":
        try:
            d = datetime.strptime(j.get("date", ""), "%Y-%m-%d").date()
        except Exception:
            return None
        cand = datetime(d.year, d.month, d.day, hh, mm)
        return cand if cand >= now else None
    if t == "weekly":
        days = sorted(set(j.get("days", [])))
        if not days:
            return None
        for add in range(0, 8):
            cand = (now + timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand.weekday() in days and cand >= now:
                return cand
        return None
    # daily
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand < now:
        cand += timedelta(days=1)
    return cand

def _sched_label(j: dict) -> str:
    """Deskripsi jadwal yang enak dibaca."""
    t = j.get("type", "daily")
    tm = j.get("time", "07:00")
    if t == "daily":
        return f"tiap hari {tm} WIB"
    if t == "weekly":
        ds = "/".join(_DOW[d] for d in sorted(set(j.get("days", []))))
        return f"tiap {ds} {tm} WIB"
    if t == "interval":
        return f"tiap {j.get('interval_h', 24)} jam"
    if t == "once":
        return f"sekali · {j.get('date','?')} {tm} WIB"
    return t

def _fmt_when(dt: datetime | None) -> str:
    if not dt:
        return "—"
    now = datetime.now()
    delta = dt - now
    secs = int(delta.total_seconds())
    if secs < 0:
        return "segera"
    if secs < 3600:
        rel = f"{secs//60}m lagi"
    elif secs < 86400:
        rel = f"{secs//3600}j {(secs%3600)//60}m lagi"
    else:
        rel = f"{secs//86400}h lagi"
    if dt.date() == now.date():
        return f"hari ini {dt:%H:%M} ({rel})"
    if dt.date() == (now + timedelta(days=1)).date():
        return f"besok {dt:%H:%M} ({rel})"
    return f"{dt:%a %d/%m %H:%M} ({rel})"

def _due(j: dict, now: datetime) -> bool:
    """True kalau job HARUS jalan pada menit `now` ini & belum jalan utk slot itu."""
    if not j.get("enabled", True):
        return False
    t = j.get("type", "daily")
    today = now.strftime("%Y-%m-%d")
    hm = now.strftime("%H:%M")
    if t == "interval":
        nr = _next_run(j, now)
        return bool(nr and nr <= now)
    if j.get("time") != hm:
        return False
    # match menit; cegah dobel pakai last_run
    lr = j.get("last_run", "")
    if t == "daily":
        return lr[:10] != today
    if t == "weekly":
        return now.weekday() in set(j.get("days", [])) and lr[:10] != today
    if t == "once":
        return j.get("date") == today and not lr
    return False

def _run_cron_job(j: dict):
    """Eksekusi satu job.
    - target_win == "session" → NYAMBUNG ke sesi pilihan (resume target_sid):
      jalanin prompt dgn --resume session itu → jawaban NAMBAH ke riwayat sesi
      tsb (lanjut percakapan). Anti-tabrakan: kalau ada window yg lagi kerja
      pakai session_id sama → ANTRI di window itu, bukan ditimpa.
    - target_win == "cron"    → window terpisah (isolated, tak ganggu sesi lain).
    - target_win == <nama>    → legacy: nyambung ke window bernama (pipeline)."""
    cid = j["cid"]
    thread_id = j.get("thread_id", 0) or 0
    target = j.get("target_win", "cron")
    label = j.get("title") or j.get("prompt", "")[:40]

    # ── Mode NYAMBUNG ke SESI pilihan (resume session_id) ────────────────────
    if target == "session" and j.get("target_sid"):
        sid = j["target_sid"]
        wd = j.get("workdir", WORKDIR)
        prov = j.get("provider", PROVIDER)
        mdl = j.get("model", MODEL_SLOT)
        prompt = f"[Tugas terjadwal · {label}]\n\n" + j["prompt"]
        store = _load_store(cid)
        # Kalau ADA window yg lagi busy & session_id-nya == sid → antri di situ
        for wname, w in store.get("windows", {}).items():
            if w.get("session_id") == sid and (cid, wname) in _busy:
                w.setdefault("queue", []).append(prompt)
                _save_store(cid)
                tg_api("sendMessage", chat_id=cid, parse_mode="",
                       text=f"⏰ Jadwal '{label}' diantri (sesi lagi dipakai). "
                            f"Jalan begitu selesai.")
                return
        tk = {"message_thread_id": thread_id} if thread_id else {}
        try:
            tg_api("sendMessage", chat_id=cid, parse_mode="",
                   text=f"⏰ Menjalankan jadwal di sesi «{j.get('target_title','?')}»: {label}…", **tk)
            result, _u = run_claude(prompt, cid, wd, sid, provider=prov, model=mdl)
            send_msg(cid, f"⏰ *Hasil jadwal — {label}*\n\n{result}", thread_id=thread_id)
        except Exception as e:
            log(f"cron job {j.get('id')} (session) error: {e}")
            try:
                tg_api("sendMessage", chat_id=cid, parse_mode="",
                       text=f"⚠️ Jadwal '{label}' gagal: {str(e)[:120]}", **tk)
            except Exception:
                pass
        return

    # ── Mode NYAMBUNG ke window bernama (legacy) ─────────────────────────────
    if target and target not in ("cron", "session"):
        store = _load_store(cid)
        if target not in store.get("windows", {}):
            tg_api("sendMessage", chat_id=cid, parse_mode="",
                   text=f"⚠️ Jadwal '{label}': window `{target}` tak ada lagi → jalan terpisah.")
            target = "cron"
        else:
            lock_key = (cid, target)
            prompt = (f"[Tugas terjadwal · {label}]\n\n" + j["prompt"])
            if lock_key in _busy:
                w = store["windows"][target]
                w.setdefault("queue", []).append(prompt)
                _save_store(cid)
                tg_api("sendMessage", chat_id=cid, parse_mode="",
                       text=f"⏰ Jadwal '{label}' diantri di sesi `{target}` "
                            f"(lagi kerja). Jalan begitu selesai.")
                return
            cur_active = store.get("active", "main")
            win_switch(cid, target)
            tg_api("sendMessage", chat_id=cid, parse_mode="",
                   text=f"⏰ Menjalankan jadwal di sesi `{target}`: {label}…")
            synth = {"message": {"chat": {"id": cid, "type": "private"},
                                 "from": {"id": cid},
                                 "message_id": 0, "text": prompt}}
            try:
                _process_safe(synth)
            finally:
                try:
                    st2 = _load_store(cid)
                    busy_wins = [k for (ci, k) in _busy if ci == cid]
                    if target not in busy_wins and cur_active in st2.get("windows", {}):
                        st2["active"] = cur_active
                        _save_store(cid)
                except Exception:
                    pass
            return

    # ── Mode TERPISAH (cron) ─────────────────────────────────────────────────
    win = win_switch(cid, "cron")
    win["workdir"] = j.get("workdir", WORKDIR)
    win["provider"] = j.get("provider", PROVIDER)
    win["model"] = j.get("model", MODEL_SLOT)
    save_sess(cid)
    tk = {"message_thread_id": thread_id} if thread_id else {}
    try:
        tg_api("sendMessage", chat_id=cid, parse_mode="",
               text=f"⏰ Menjalankan jadwal: {label}…", **tk)
        result, _u = run_claude(j["prompt"], cid, win["workdir"],
                                win["session_id"], provider=win["provider"],
                                model=win["model"])
        send_msg(cid, f"⏰ *Hasil jadwal — {label}*\n\n{result}", thread_id=thread_id)
        save_sess(cid)
    except Exception as e:
        log(f"cron job {j.get('id')} error: {e}")
        try:
            tg_api("sendMessage", chat_id=cid, parse_mode="",
                   text=f"⚠️ Jadwal '{label}' gagal: {str(e)[:120]}", **tk)
        except Exception:
            pass

def _cron_command(cid: int, a: str) -> str:
    """Shortcut teks (panel utama via tombol). /cron, /cron add HH:MM <tugas>."""
    jobs = _load_cron()
    parts = a.split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    if sub == "add" and len(parts) > 1:
        rest = parts[1].split(maxsplit=1)
        if len(rest) < 2 or ":" not in rest[0]:
            return "Format: `/cron add 07:00 cek server lapor ke aku`\n(atau pakai tombol ⏰ Cron untuk wizard lengkap)"
        tm, prompt = rest[0], rest[1]
        try:
            hh, mm = map(int, tm.split(":")); assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception:
            return "❌ Jam tidak valid. Format HH:MM (mis. 07:00)"
        sess = load_sess(cid)
        jobs.append({"id": uuid.uuid4().hex[:8], "cid": cid, "thread_id": 0,
                     "type": "daily", "time": f"{hh:02d}:{mm:02d}", "prompt": prompt,
                     "title": prompt[:40], "workdir": sess["workdir"],
                     "provider": sess.get("provider", PROVIDER),
                     "model": sess.get("model", MODEL_SLOT),
                     "enabled": True, "last_run": "", "run_count": 0,
                     "created": datetime.now().isoformat(timespec="seconds")})
        _save_cron(jobs)
        return f"⏰ Jadwal ditambah: *tiap hari {hh:02d}:{mm:02d} WIB*\n`{prompt[:60]}`"
    if sub in ("del", "delete", "rm") and len(parts) > 1:
        try:
            idx = int(parts[1]) - 1
        except Exception:
            return "Format: `/cron del <nomor>`"
        mine = _my_jobs(cid)
        if 0 <= idx < len(mine):
            _job_delete(mine[idx]["id"])
            return f"🗑️ Jadwal #{idx+1} dihapus."
        return "❌ Nomor tidak ada."
    # list (teks ringkas)
    mine = _my_jobs(cid)
    if not mine:
        return ("⏰ *Cron / Jadwal* (WIB)\n\nBelum ada jadwal.\n\n"
                "Pakai tombol *⏰ Cron* di menu untuk wizard lengkap, atau cepat:\n"
                "`/cron add 07:00 cek server lapor ke aku`")
    lines = ["⏰ *Jadwal Aktif* (WIB)\n"]
    for i, j in enumerate(mine, 1):
        st = "🟢" if j.get("enabled", True) else "⏸"
        nr = _fmt_when(_next_run(j)) if j.get("enabled", True) else "pause"
        lines.append(f"{i}. {st} *{_sched_label(j)}* — `{(j.get('title') or j.get('prompt',''))[:40]}`\n     ↳ berikut: {nr}")
    lines.append("\n_Kelola lengkap (edit/pause/run) lewat tombol ⏰ Cron._")
    return "\n".join(lines)

def _cron_loop():
    """Background: jalanin job yang due. Cek tiap 30 detik."""
    while True:
        try:
            now = datetime.now()
            jobs = _load_cron()
            changed = False
            for j in jobs:
                try:
                    if _due(j, now):
                        j["last_run"] = now.isoformat(timespec="seconds")
                        j["run_count"] = j.get("run_count", 0) + 1
                        if j.get("type") == "once":
                            j["enabled"] = False  # sekali jalan → matikan
                        changed = True
                        threading.Thread(target=_run_cron_job, args=(dict(j),), daemon=True).start()
                except Exception as e:
                    log(f"cron eval {j.get('id')} error: {e}")
            if changed:
                _save_cron(jobs)
        except Exception as e:
            log(f"cron loop error: {e}")
        time.sleep(30)


# ── Cron UI: panel + wizard + manajemen per-job ──────────────────────────────
def _cron_panel_kb(cid: int) -> dict:
    """Keyboard panel utama: daftar job (tiap baris 1 job) + tombol tambah."""
    rows = []
    for j in _my_jobs(cid):
        st = "🟢" if j.get("enabled", True) else "⏸"
        lbl = (j.get("title") or j.get("prompt", ""))[:24]
        rows.append([{"text": f"{st} {_sched_label(j)} · {lbl}",
                      "callback_data": f"cronview:{j['id']}"}])
    rows.append([{"text": "➕ Tambah Jadwal", "callback_data": "cron_add"}])
    rows.append([{"text": "🔄 Refresh", "callback_data": "cron_panel"},
                 {"text": "✖️ Tutup", "callback_data": "m_close"}])
    return {"inline_keyboard": rows}

def _cron_panel_text(cid: int) -> str:
    mine = _my_jobs(cid)
    if not mine:
        return ("⏰ *Cron / Jadwal Otomatis* (WIB)\n\n"
                "Belum ada jadwal. Tekan *➕ Tambah Jadwal* untuk bikin tugas "
                "yang dijalankan Claude otomatis di waktu tertentu.\n\n"
                "_Contoh: tiap pagi 07:00 cek server & lapor, atau tiap Senin "
                "ringkas progress minggu lalu._")
    active = sum(1 for j in mine if j.get("enabled", True))
    nexts = [(_next_run(j), j) for j in mine if j.get("enabled", True)]
    nexts = [(d, j) for d, j in nexts if d]
    head = [f"⏰ *Cron / Jadwal Otomatis* (WIB)",
            f"_{len(mine)} jadwal · {active} aktif_"]
    if nexts:
        d, j = min(nexts, key=lambda x: x[0])
        head.append(f"⏭ Berikutnya: *{_fmt_when(d)}* — `{(j.get('title') or j.get('prompt',''))[:30]}`")
    head.append("\nPilih jadwal untuk kelola, atau tambah baru 👇")
    return "\n".join(head)

def _cron_job_text(j: dict) -> str:
    st = "🟢 Aktif" if j.get("enabled", True) else "⏸ Pause"
    nr = _fmt_when(_next_run(j)) if j.get("enabled", True) else "—"
    last = j.get("last_run", "") or "belum pernah"
    if last and last != "belum pernah":
        last = last.replace("T", " ")[:16] + " WIB"
    tw = j.get("target_win", "cron")
    if tw == "session":
        sess_lbl = f"💬 «{j.get('target_title','?')}» (nyambung)"
    elif tw == "cron":
        sess_lbl = "🔒 terpisah (cron)"
    else:
        sess_lbl = f"🪟 {tw} (nyambung konteks)"
    lines = [
        f"⏰ *Detail Jadwal*",
        f"Status: {st}",
        f"Jadwal: *{_sched_label(j)}*",
        f"Berikutnya: {nr}",
        f"Terakhir jalan: {last}  ·  sudah {j.get('run_count', 0)}×",
        f"Sesi: {sess_lbl}",
        f"Provider/Model: `{j.get('provider','?')}/{j.get('model','?')}`",
        f"Folder: `{j.get('workdir','?')}`",
    ]
    if j.get("thread_id"):
        lines.append(f"Output ke topic: `{j['thread_id']}`")
    lines.append(f"\n*Tugas:*\n{j.get('prompt','')[:500]}")
    return "\n".join(lines)

def _cron_job_kb(j: dict) -> dict:
    toggle = ("⏸ Pause", "cronpause") if j.get("enabled", True) else ("▶️ Aktifkan", "cronresume")
    return {"inline_keyboard": [
        [{"text": "🚀 Jalankan Sekarang", "callback_data": f"cronrun:{j['id']}"}],
        [{"text": toggle[0], "callback_data": f"{toggle[1]}:{j['id']}"},
         {"text": "✏️ Edit Tugas", "callback_data": f"croneditprompt:{j['id']}"}],
        [{"text": "🗑️ Hapus", "callback_data": f"crondel:{j['id']}"}],
        [{"text": "← Daftar", "callback_data": "cron_panel"},
         {"text": "✖️ Tutup", "callback_data": "m_close"}],
    ]}

# ---- wizard tambah jadwal ----
_CRON_TYPE_KB = {"inline_keyboard": [
    [{"text": "🔁 Harian", "callback_data": "crontype:daily"},
     {"text": "📅 Mingguan", "callback_data": "crontype:weekly"}],
    [{"text": "⏱ Tiap N jam", "callback_data": "crontype:interval"},
     {"text": "1️⃣ Sekali", "callback_data": "crontype:once"}],
    [{"text": "✖️ Batal", "callback_data": "cron_cancel"}],
]}
_CRON_INTERVAL_KB = {"inline_keyboard": [
    [{"text": "1 jam", "callback_data": "cronival:1"},
     {"text": "3 jam", "callback_data": "cronival:3"},
     {"text": "6 jam", "callback_data": "cronival:6"}],
    [{"text": "12 jam", "callback_data": "cronival:12"},
     {"text": "24 jam", "callback_data": "cronival:24"}],
    [{"text": "✖️ Batal", "callback_data": "cron_cancel"}],
]}

def _cron_days_kb(sel: list) -> dict:
    rows, row = [], []
    for i, d in enumerate(_DOW):
        mark = "✅" if i in sel else "▫️"
        row.append({"text": f"{mark}{d}", "callback_data": f"cronday:{i}"})
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text": "✔️ Lanjut", "callback_data": "cronday_done"},
                 {"text": "✖️ Batal", "callback_data": "cron_cancel"}])
    return {"inline_keyboard": rows}

def _cron_hour_kb() -> dict:
    """Grid jam 00–23 (6 per baris), tinggal klik."""
    rows, row = [], []
    for h in range(24):
        row.append({"text": f"{h:02d}", "callback_data": f"cronhh:{h}"})
        if len(row) == 6:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text": "✖️ Batal", "callback_data": "cron_cancel"}])
    return {"inline_keyboard": rows}

def _cron_min_kb(hh: int) -> dict:
    """Pilihan menit (kelipatan 5) + tombol :00/:30 cepat."""
    rows, row = [], []
    for m in range(0, 60, 5):
        row.append({"text": f"{hh:02d}:{m:02d}", "callback_data": f"cronmm:{m}"})
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text": "← Ganti jam", "callback_data": "cronhh_back"},
                 {"text": "✖️ Batal", "callback_data": "cron_cancel"}])
    return {"inline_keyboard": rows}

def _cron_date_kb() -> dict:
    """14 hari ke depan sebagai tombol (Hari ini / Besok / Sen 03/07 …)."""
    now = datetime.now()
    rows, row = [], []
    for i in range(14):
        d = now + timedelta(days=i)
        if i == 0:
            lbl = "Hari ini"
        elif i == 1:
            lbl = "Besok"
        else:
            lbl = f"{_DOW[d.weekday()]} {d:%d/%m}"
        row.append({"text": lbl, "callback_data": f"crondate:{d:%Y-%m-%d}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text": "✖️ Batal", "callback_data": "cron_cancel"}])
    return {"inline_keyboard": rows}

def _cron_win_kb(cid: int) -> dict:
    """Pilih SESI tujuan: daftar sesi /resume dari folder aktif (pakai judul) +
    opsi 'Terpisah'. Callback cronsid:<idx> (idx ke _sess_cache biar pendek)."""
    sess = load_sess(cid)
    sessions = _cc_sessions(sess["workdir"])
    _sess_cache[cid] = sessions  # dipakai callback lookup by index
    cur = sess.get("session_id", "")
    rows = []
    for i, s in enumerate(sessions[:12]):
        star = "⭐ " if s["id"] == cur else ""
        lbl = (s.get("title") or s.get("summary") or s["id"][:8]).strip()[:30]
        rows.append([{"text": f"{star}💬 {lbl}", "callback_data": f"cronsid:{i}"}])
    rows.append([{"text": "🔒 Sesi terpisah (default)", "callback_data": "cronsid:sep"}])
    rows.append([{"text": "✖️ Batal", "callback_data": "cron_cancel"}])
    return {"inline_keyboard": rows}

def _cron_ask_win(cid: int, mid: int):
    """Step pilih SESI tujuan — sesudah jadwal-waktu, sebelum prompt."""
    sess = load_sess(cid)
    sessions = _cc_sessions(sess["workdir"])
    if not sessions:
        # Belum ada sesi di folder ini → langsung terpisah, skip step
        st = _pending_cron.get(cid)
        if st:
            st["data"]["target_win"] = "cron"
            st["step"] = "prompt"
        _cron_ask_prompt(cid, mid)
        return
    edit_md(cid, mid,
            "➕ *Tambah Jadwal* — Sesi Tujuan\n\n"
            "Jadwal ini nyambung ke *sesi mana*?\n\n"
            "💬 *Pilih sesi* (dari /resume) → cron lanjut percakapan itu, Claude "
            "ingat konteksnya & jawabannya nambah ke sesi tsb. Kalau sesi lagi "
            "dipakai, jadwal *diantri* aman.\n"
            "🔒 *Sesi terpisah* → window khusus cron, tak ganggu sesi lain "
            "(tugas harus mandiri).",
            reply_markup=_cron_win_kb(cid))

def _cron_start_wizard(cid: int, mid: int):
    _pending_cron[cid] = {"step": "type", "data": {}}
    edit_md(cid, mid,
            "➕ *Tambah Jadwal* (1/4)\n\nPilih *jenis* jadwal:\n\n"
            "🔁 Harian — tiap hari jam tertentu\n"
            "📅 Mingguan — pilih hari (bisa banyak)\n"
            "⏱ Tiap N jam — interval berulang\n"
            "1️⃣ Sekali — satu tanggal & jam, lalu nonaktif",
            reply_markup=_CRON_TYPE_KB)

def _cron_ask_time(cid: int, mid: int, extra: str = ""):
    """Step jam: pilih JAM dulu (grid), lalu menit."""
    edit_md(cid, mid,
            f"➕ *Tambah Jadwal* — Jam (WIB){extra}\n\nPilih *jam*:",
            reply_markup=_cron_hour_kb())

def _cron_ask_date(cid: int, mid: int):
    edit_md(cid, mid,
            "➕ *Tambah Jadwal* — Tanggal\n\nPilih *tanggal* (klik):",
            reply_markup=_cron_date_kb())

def _cron_ask_prompt(cid: int, mid: int):
    edit_md(cid, mid,
            "➕ *Tambah Jadwal* (3/3)\n\nKetik *tugas* yang harus dikerjakan Claude.\n"
            "Tulis sejelas mungkin — ini dikirim sebagai prompt.\n\n"
            "_Contoh: \"Cek status systemd cc-tg, kalau mati restart & lapor. "
            "Ringkas 1 paragraf.\"_",
            reply_markup={"inline_keyboard": [[{"text": "✖️ Batal", "callback_data": "cron_cancel"}]]})

def _cron_finalize(cid: int):
    """Simpan job dari _pending_cron[cid]['data']. Returns (ok, job|err)."""
    st = _pending_cron.get(cid)
    if not st:
        return False, "wizard kadaluarsa"
    d = st["data"]
    sess = load_sess(cid)
    store = _load_store(cid)
    active = store.get("active", "main")
    # thread_id: kalau wizard dimulai dari dalam topic, simpan biar output balik ke situ
    thread_id = st.get("thread_id", 0)
    target_win = d.get("target_win", "cron")
    job = {"id": uuid.uuid4().hex[:8], "cid": cid, "thread_id": thread_id,
           "type": d["type"], "prompt": d["prompt"], "title": d["prompt"][:40],
           "target_win": target_win,
           "target_sid": d.get("target_sid", ""),
           "target_title": d.get("target_title", ""),
           "workdir": sess["workdir"], "provider": sess.get("provider", PROVIDER),
           "model": sess.get("model", MODEL_SLOT), "enabled": True,
           "last_run": "", "run_count": 0,
           "created": datetime.now().isoformat(timespec="seconds")}
    if d["type"] in ("daily", "weekly", "once"):
        job["time"] = d["time"]
    if d["type"] == "weekly":
        job["days"] = d["days"]
    if d["type"] == "interval":
        job["interval_h"] = d["interval_h"]
        job["anchor"] = datetime.now().isoformat(timespec="seconds")
    if d["type"] == "once":
        job["date"] = d["date"]
    jobs = _load_cron()
    jobs.append(job)
    _save_cron(jobs)
    _pending_cron.pop(cid, None)
    return True, job


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    log("CC-TG (Claude Code wrapper) starting…")
    log(f"Providers: {', '.join(sorted(PROVIDERS.keys())) or 'none'}")
    log(f"Default provider: {PROVIDER}")
    log(f"Owner IDs: {OWNER_IDS}")
    log(f"Model slot: {MODEL_SLOT}")
    log(f"Default workdir: {WORKDIR}")
    if _ENV_LOADED:   # NAMA key saja — nilai tidak pernah di-log
        log(f"Env dari ~/.claude/.env: {', '.join(sorted(_ENV_LOADED))}")

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
            {"command": "agents", "description": "🤖 Agent View: daftar sesi + pindah"},
            {"command": "mcp", "description": "🔌 Status & on/off MCP servers"},
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
            {"command": "undo", "description": "↩️ Mundurkan N turn terakhir"},
            {"command": "compact", "description": "🧹 Ringkas konteks sesi (native)"},
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
