import asyncio, hashlib, hmac, json, logging, os, secrets, tempfile, time
from pathlib import Path
from urllib.parse import urlparse, parse_qsl
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import yt_dlp
import uvicorn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("smart-media")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "45"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
ALLOWED_USERS = {x.strip() for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}
BASE = Path(tempfile.gettempdir()) / "smart-media"
BASE.mkdir(exist_ok=True)
app = FastAPI(title="Smart Media Downloader")
bot_app = None

def check_user(user_id):
    return not ALLOWED_USERS or str(user_id) in ALLOWED_USERS

def verify_init_data(init_data: str):
    if not BOT_TOKEN or not init_data:
        return None
    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True))
        received = values.pop("hash", "")
        auth_date = int(values.get("auth_date", "0"))
        if not received or time.time() - auth_date > 86400:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received):
            return None
        return json.loads(values.get("user", "{}"))
    except Exception:
        return None

def validate_url(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "الرابط غير صالح")
    host = p.hostname.lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith((".local", ".internal")):
        raise HTTPException(400, "هذا الرابط غير مدعوم")
    return url

class AnalyzeRequest(BaseModel):
    url: HttpUrl
    init_data: str = ""

class DownloadRequest(BaseModel):
    url: HttpUrl
    format: str = "video"
    quality: str = "best"
    init_data: str = ""

def get_user(init_data, fallback_id=None):
    user = verify_init_data(init_data)
    if user:
        if not check_user(user.get("id")): raise HTTPException(403, "غير مصرح لك باستخدام الخدمة")
        return user
    if fallback_id and check_user(fallback_id):
        return {"id": fallback_id}
    raise HTTPException(401, "افتح التطبيق من خلال بوت تليجرام أولاً")

def extract_info(url):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "socket_timeout": 15}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        log.warning("extract failed: %s", e)
        raise HTTPException(422, "تعذر تحليل الرابط. تأكد أنه عام ومدعوم.")

@app.get("/health")
async def health(): return {"status": "ok"}

@app.get("/")
async def home(): return FileResponse("static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.post("/api/analyze")
async def analyze(body: AnalyzeRequest):
    get_user(body.init_data)
    info = await asyncio.to_thread(extract_info, validate_url(str(body.url)))
    formats = []
    for f in info.get("formats", []):
        if f.get("vcodec") != "none" and f.get("height"):
            formats.append({"height": f["height"], "format_id": f.get("format_id"), "ext": f.get("ext")})
    heights = sorted({f["height"] for f in formats}, reverse=True)
    return {"title": info.get("title", "Media"), "thumbnail": info.get("thumbnail"), "duration": info.get("duration"), "uploader": info.get("uploader"), "qualities": heights[:8], "audio": True}

@app.post("/api/download")
async def download(body: DownloadRequest):
    user = get_user(body.init_data)
    url = validate_url(str(body.url))
    uid = str(user["id"])
    folder = BASE / uid
    folder.mkdir(exist_ok=True)
    for old in folder.iterdir():
        try:
            if time.time() - old.stat().st_mtime > 3600: old.unlink()
        except Exception: pass
    if body.format not in ("video", "audio"): raise HTTPException(400, "نوع الملف غير صالح")
    quality = body.quality if body.quality in ("best", "1080", "720", "480", "360") else "best"
    selector = "bestaudio/best" if body.format == "audio" else (f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best" if quality != "best" else "bestvideo+bestaudio/best")
    outtmpl = str(folder / f"%(id)s.%(ext)s")
    opts = {"format": selector, "outtmpl": outtmpl, "noplaylist": True, "quiet": True, "no_warnings": True, "socket_timeout": 20, "max_filesize": MAX_FILE_BYTES, "merge_output_format": "mp4"}
    if body.format == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    try:
        def run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info
        info = await asyncio.to_thread(run)
        candidates = sorted(folder.glob(f"{info.get('id','*')}.*"), key=lambda p:p.stat().st_mtime, reverse=True)
        if not candidates: raise RuntimeError("No output file")
        file = candidates[0]
        if file.stat().st_size > MAX_FILE_BYTES:
            file.unlink(missing_ok=True)
            raise HTTPException(413, f"الملف أكبر من الحد المسموح ({MAX_FILE_MB} MB)")
        return FileResponse(file, filename=file.name, media_type="audio/mpeg" if body.format=="audio" else "video/mp4", background=None)
    except HTTPException: raise
    except Exception as e:
        log.exception("download failed")
        raise HTTPException(422, "فشل التحميل. جرّب جودة أقل أو رابطاً آخر.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update.effective_user.id):
        await update.message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return
    if WEBAPP_URL:
        kb = ReplyKeyboardMarkup([[KeyboardButton("🚀 افتح Smart Media", web_app=WebAppInfo(url=WEBAPP_URL))]], resize_keyboard=True)
        await update.message.reply_text("أهلاً بك في Smart Media! أرسل رابطاً أو افتح التطبيق لاختيار الجودة والصيغة.", reply_markup=kb)
    else:
        await update.message.reply_text("البوت جاهز تقريباً. أضف WEBAPP_URL في Railway بعد نشر الواجهة.")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update.effective_user.id):
        await update.message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return
    url = next((w for w in update.message.text.split() if w.startswith(("https://","http://"))), None)
    if not url:
        await update.message.reply_text("ابعت رابط فيديو عام من منصة مدعومة.")
        return
    await update.message.reply_text("⏳ جاري تحليل الرابط...")
    try:
        info = await asyncio.to_thread(extract_info, validate_url(url))
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 فيديو", callback_data="video"), InlineKeyboardButton("🎵 صوت", callback_data="audio")]])
        context.user_data["last_url"] = url
        await update.message.reply_text(f"📹 {info.get('title','Media')}\nالمدة: {info.get('duration') or 'غير معروفة'} ثانية\nاختار نوع التحميل من التطبيق للحصول على الجودة المناسبة.", reply_markup=kb)
    except Exception:
        await update.message.reply_text("لم أتمكن من تحليل الرابط. تأكد أنه عام ومدعوم.")

async def post_init(application):
    if WEBAPP_URL:
        await application.bot.set_chat_menu_button(menu_button={"type":"web_app","text":"Smart Media","web_app":{"url":WEBAPP_URL}})

async def run_bot():
    global bot_app
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN is not configured; API will run without Telegram bot.")
        return
    bot_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

async def main():
    await run_bot()
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT","8000")), log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
