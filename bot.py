import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "").strip()
COOKIES_B64 = os.getenv("YTDLP_COOKIES_B64", "").strip()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "700"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/video-bot-downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("video_bot")

# Avoid two simultaneous downloads exhausting a small Railway instance.
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)


def valid_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def prepare_cookie_file() -> str | None:
    """Use a mounted cookies file, or decode a Base64 Netscape-format file."""
    if COOKIES_FILE:
        path = Path(COOKIES_FILE)
        if path.is_file():
            return str(path)
        logger.warning("YTDLP_COOKIES_FILE is set, but the file does not exist.")
    if COOKIES_B64:
        import base64
        path = Path("/tmp/yt-dlp-cookies.txt")
        try:
            path.write_bytes(base64.b64decode(COOKIES_B64, validate=True))
            return str(path)
        except Exception:
            logger.exception("Could not decode YTDLP_COOKIES_B64.")
    return None


def download_sync(url: str, job_dir: Path) -> dict:
    cookie_file = prepare_cookie_file()
    opts = {
        "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
        "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
        "restrictfilenames": True,
        "overwrites": True,
        "logger": YtDlpLogger(),
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        expected = Path(ydl.prepare_filename(info))
        candidates = [expected]
        # yt-dlp can change extension when merging formats.
        if expected.suffix.lower() != ".mp4":
            candidates.append(expected.with_suffix(".mp4"))
        candidates.extend(p for p in job_dir.iterdir() if p.is_file())
        filepath = next((p for p in candidates if p.is_file()), None)
        if filepath is None:
            raise RuntimeError("Загрузчик завершился, но файл не найден.")
        size = filepath.stat().st_size
        if size > MAX_DOWNLOAD_MB * 1024 * 1024:
            raise RuntimeError(f"Видео больше лимита загрузки ({MAX_DOWNLOAD_MB} МБ).")
        return {
            "filepath": str(filepath),
            "title": info.get("title") or "video",
            "duration": info.get("duration"),
            "size": size,
        }


class YtDlpLogger:
    def debug(self, msg):
        # yt-dlp sends routine progress through debug; don't log cookies or URLs.
        if msg.startswith("[debug]"):
            logger.debug("yt-dlp %s", msg[:500])

    def warning(self, msg):
        logger.warning("yt-dlp: %s", msg[:700])

    def error(self, msg):
        logger.error("yt-dlp: %s", msg[:1000])


def compress_sync(input_path: str, output_path: str) -> str:
    """Try to make a Telegram-friendly MP4; return original if compression fails."""
    if not shutil.which("ffmpeg"):
        logger.warning("FFmpeg not found; cannot compress oversized video.")
        return input_path

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", input_path,
        "-vf", "scale='min(854,iw)':-2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "29",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=900)
        if Path(output_path).is_file() and Path(output_path).stat().st_size > 0:
            return output_path
    except Exception:
        logger.exception("FFmpeg compression failed.")
    return input_path


def friendly_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "redirection detected" in message:
        return (
            "Сайт перенаправил запрос. Видео может быть удалено, требовать входа "
            "или быть недоступно с Railway. Проверь ссылку в браузере; если она "
            "доступна только после входа, настрой cookies."
        )
    if "sign in to confirm" in message or "cookies" in message and "login" in message:
        return "Сайт требует авторизацию. Для разрешённого доступа настрой cookies в Railway."
    if "private video" in message or "video unavailable" in message or "this video is unavailable" in message:
        return "Видео недоступно, удалено, приватное или ограничено владельцем."
    if "unsupported url" in message:
        return "Этот URL не поддерживается yt-dlp или ссылка имеет неверный формат."
    if "max-filesize" in message or "file is larger than" in message:
        return f"Видео превышает лимит загрузки {MAX_DOWNLOAD_MB} МБ."
    return "Не удалось скачать видео. Проверь ссылку и логи Railway для подробностей."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Привет! Пришли ссылку на видео с сайта, который поддерживает yt-dlp.\n"
        f"Максимальный размер исходного файла: {MAX_DOWNLOAD_MB} МБ.\n"
        "Скачивание работает только для контента, к которому у тебя есть доступ."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Отправь полную http/https-ссылку на видео. Если сайт требует авторизацию, "
        "можно задать cookies через переменные Railway. Не отправляй cookies или "
        "пароли в чат с ботом."
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    url = message.text.strip()
    if not valid_http_url(url):
        await message.reply_text("Пришли корректную ссылку, начинающуюся с http:// или https://.")
        return

    status = await message.reply_text("⏳ Проверяю ссылку и загружаю видео…")
    job_dir = Path(tempfile.mkdtemp(prefix="job_", dir=DOWNLOAD_DIR))
    try:
        async with DOWNLOAD_SEMAPHORE:
            await status.edit_text("📥 Загружаю видео…")
            result = await asyncio.to_thread(download_sync, url, job_dir)
            final_path = result["filepath"]

            size_mb = Path(final_path).stat().st_size / (1024 * 1024)
            if size_mb > MAX_UPLOAD_MB:
                await status.edit_text(
                    f"📦 Файл {size_mb:.1f} МБ — пытаюсь сжать для отправки…"
                )
                compressed = str(job_dir / "compressed.mp4")
                final_path = await asyncio.to_thread(compress_sync, final_path, compressed)

            final_size = Path(final_path).stat().st_size
            if final_size > MAX_UPLOAD_MB * 1024 * 1024:
                await status.edit_text(
                    f"❌ Файл всё ещё больше лимита отправки ({MAX_UPLOAD_MB} МБ). "
                    "Попробуй видео меньшего качества."
                )
                return

            await status.edit_text("📤 Отправляю видео в Telegram…")
            with open(final_path, "rb") as video:
                await message.reply_video(
                    video=video,
                    caption=(result["title"] or "Видео")[:1024],
                    supports_streaming=True,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=30,
                    pool_timeout=30,
                )
            await status.delete()
    except yt_dlp.utils.DownloadError as exc:
        logger.warning("DownloadError: %s", str(exc)[:1500])
        await status.edit_text("❌ " + friendly_error(exc))
    except Exception as exc:
        logger.exception("Request failed")
        try:
            await status.edit_text("❌ " + friendly_error(exc))
        except Exception:
            pass
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled Telegram error: %s", context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in Railway Variables.")

    logger.info("Starting bot; yt-dlp version=%s", yt_dlp.version.__version__)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
