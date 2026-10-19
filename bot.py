import os
import logging
import asyncio
import subprocess
import shutil
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# ================= 配置 =================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # 从环境变量读取，不要硬编码
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# 日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= 核心功能 =================

def _download_video_sync(url: str) -> dict:
    """同步下载函数（将在异步环境中用线程调用）"""
    ydl_opts = {
        "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # 可选：限制文件大小，避免下载超大文件
        # "max_filesize": 500 * 1024 * 1024,  # 500MB
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # 获取实际下载的文件路径
        filepath = ydl.prepare_filename(info)
        # 如果合并后扩展名可能变化，尝试查找
        if not Path(filepath).exists():
            # 查找匹配的 mp4 文件
            candidates = list(DOWNLOAD_DIR.glob(f"{info['title']}*.mp4"))
            if candidates:
                filepath = str(candidates[0])
        return {
            "filepath": filepath,
            "title": info.get("title", "video"),
            "filesize": os.path.getsize(filepath) if os.path.exists(filepath) else 0,
        }


def _compress_if_needed(filepath: str) -> str:
    """如果文件超过 50MB，用 FFmpeg 压缩到 480p 左右"""
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if size_mb <= 50:
        return filepath
    
    logger.info(f"文件 {size_mb:.1f}MB，开始压缩...")
    compressed_path = filepath.rsplit(".", 1)[0] + "_compressed.mp4"
    
    # 简单压缩：缩放宽度到 854（480p），降低码率
    cmd = [
        "ffmpeg", "-i", filepath,
        "-vf", "scale=854:-2",
        "-c:v", "libx264", "-crf", "28", "-preset", "fast",
        "-c:a", "aac", "-b:a", "96k",
        "-y", compressed_path
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        # 删除原文件，返回压缩后路径
        os.remove(filepath)
        return compressed_path
    except Exception as e:
        logger.error(f"压缩失败: {e}")
        return filepath  # 压缩失败则返回原文件，让 Telegram 报错


# ================= 异步封装 =================

async def download_video(url: str) -> dict:
    """在线程中执行同步下载，避免阻塞事件循环"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_video_sync, url)


async def compress_video(filepath: str) -> str:
    """在线程中执行压缩"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _compress_if_needed, filepath)


# ================= 命令处理 =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 发送视频链接给我，我会下载并发送给你。\n"
        "支持 YouTube、Twitter、Instagram 等 yt-dlp 支持的站点。\n"
        "⚠️ 超过 50MB 的视频会自动压缩。"
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("请发送有效的链接。")
        return
    
    status_msg = await update.message.reply_text("⏳ 正在解析并下载...")
    
    try:
        # 1. 下载
        await status_msg.edit_text("📥 下载中...")
        result = await download_video(url)
        filepath = result["filepath"]
        title = result["title"]
        
        if not os.path.exists(filepath):
            await status_msg.edit_text("❌ 下载失败：文件未找到。")
            return
        
        # 2. 检查并压缩
        await status_msg.edit_text("📦 检查文件大小...")
        final_path = await compress_video(filepath)
        
        # 3. 发送
        await status_msg.edit_text("📤 上传中...")
        with open(final_path, "rb") as f:
            await update.message.reply_video(
                video=f,
                caption=title[:1024],
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
            )
        
        await status_msg.delete()
        
        # 4. 清理临时文件
        try:
            os.remove(final_path)
            if final_path != filepath and os.path.exists(filepath):
                os.remove(filepath)
        except:
            pass
            
    except Exception as e:
        logger.exception("处理失败")
        await status_msg.edit_text(f"❌ 出错：{str(e)[:200]}")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"更新 {update} 导致错误 {context.error}")


# ================= 启动 =================

def main():
    if not BOT_TOKEN:
        raise ValueError("请设置环境变量 BOT_TOKEN")
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_error_handler(error_handler)
    
    logger.info("机器人启动...")
    app.run_polling()


if __name__ == "__main__":
    main()