import os
import sys
import time
import json
import signal
import logging
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ---------- Config ----------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TEXTSET_DIR = DATA_DIR / "textset"
ARCHIVES_DIR = TEXTSET_DIR / "archives"

FALCON_PARSE_SCRIPT = Path(__file__).parent / "falcon_parse.py"

# Ensure dirs
TEXTSET_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------- Global state ----------
CANCEL_EVENT = threading.Event()
RUNNING_LOCK = threading.Lock()
RUNNING_JOB = None  # dict with metadata about current job


# ---------- Helpers ----------
def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_size_mb(bytes_val: int) -> str:
    if bytes_val < 1024:
        return f"{bytes_val} B"
    if bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    return f"{bytes_val / (1024 * 1024):.1f} MB"


# ---------- Falcon runner ----------
def run_falcon(
    term: str,
    mode: str,
    limit: int | None = None,
    update: Update | None = None,
    context: ContextTypes.DEFAULT_TYPE | None = None,
):
    """
    Run falcon_parse.py in 'ulp' mode (full parse).
    """
    global RUNNING_JOB

    args = [
        str(FALCON_PARSE_SCRIPT),
        "--source", str(TEXTSET_DIR),
        "--out", str(ARCHIVES_DIR),
        "--mode", "ulp",
        "--term", term,
    ]
    if limit is not None:
        args += ["--limit", str(limit)]

    logger.info(f"[{now_ts()}] Starting falcon_parse: {' '.join(args)}")

    proc = None
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        last_status = None
        last_progress_msg = None
        msg_id = None
        chat_id = None

        if update and update.effective_message:
            chat_id = update.effective_message.chat_id
            # Initial message
            sent = update.effective_message.reply_text(
                f"🚀 Running: {term}\n⏳ Preparing...",
                quote=True,
            )
            msg_id = sent.message_id

        # Watcher thread to update progress
        def watcher():
            nonlocal last_status, last_progress_msg, msg_id, chat_id
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue

                    # Expecting JSON status lines from falcon_parse
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    status = data.get("status")
                    processed = data.get("processed")
                    total = data.get("total")
                    mb = data.get("mb_processed")
                    total_mb = data.get("total_mb")

                    if status:
                        last_status = status

                    if processed is not None and total is not None:
                        pct = int(100 * processed / total) if total > 0 else 0
                        progress_text = (
                            f"🚀 Running: {term}\n"
                            f"📊 {processed:,} / {total:,} files ({pct}%)\n"
                            f"💾 {format_size_mb(mb)} / {format_size_mb(total_mb)}\n"
                            f"⚙️ Mode: {mode.upper()}"
                        )
                        if progress_text != last_progress_msg and chat_id and msg_id:
                            try:
                                context.bot.edit_message_text(
                                    text=progress_text,
                                    chat_id=chat_id,
                                    message_id=msg_id,
                                )
                                last_progress_msg = progress_text
                            except Exception:
                                pass
            except Exception as e:
                logger.exception(f"Watcher error: {e}")

        watcher_thread = threading.Thread(target=watcher, daemon=True)
        watcher_thread.start()

        # Wait for process to finish
        retcode = proc.wait()

        # Normal completion path
        with RUNNING_LOCK:
            RUNNING_JOB = None

        if chat_id and msg_id:
            final_text = (
                f"✅ Done: {term}\n"
                f"⚙️ Mode: {mode.upper()}\n"
                f"Returned code: {retcode}"
            )
            try:
                context.bot.edit_message_text(
                    text=final_text,
                    chat_id=chat_id,
                    message_id=msg_id,
                )
            except Exception:
                pass

        logger.info(f"[{now_ts()}] falcon_parse finished with code {retcode}")

    except Exception as e:
        logger.exception(f"run_falcon error: {e}")
        with RUNNING_LOCK:
            RUNNING_JOB = None
        if update and update.effective_message:
            update.effective_message.reply_text(
                f"❌ Error running job for {term}:\n{e}",
                quote=True,
            )
    finally:
        # Safety: ensure RUNNING_JOB is cleared even on exception
        with RUNNING_LOCK:
            RUNNING_JOB = None


def run_falcon_lite(
    term: str,
    mode: str = "lite",
    limit: int | None = None,
    update: Update | None = None,
    context: ContextTypes.DEFAULT_TYPE | None = None,
):
    """
    Run falcon_parse.py in 'lite' mode (lighter scan).
    """
    global RUNNING_JOB

    args = [
        str(FALCON_PARSE_SCRIPT),
        "--source", str(TEXTSET_DIR),
        "--out", str(ARCHIVES_DIR),
        "--mode", "lite",
        "--term", term,
    ]
    if limit is not None:
        args += ["--limit", str(limit)]

    logger.info(f"[{now_ts()}] Starting falcon_parse (lite): {' '.join(args)}")

    proc = None
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        last_progress_msg = None
        msg_id = None
        chat_id = None

        if update and update.effective_message:
            chat_id = update.effective_message.chat_id
            sent = update.effective_message.reply_text(
                f"🚀 Running: {term}\n⏳ Preparing (lite)...",
                quote=True,
            )
            msg_id = sent.message_id

        def watcher():
            nonlocal last_progress_msg, msg_id, chat_id
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    processed = data.get("processed")
                    total = data.get("total")
                    mb = data.get("mb_processed")
                    total_mb = data.get("total_mb")

                    if processed is not None and total is not None:
                        pct = int(100 * processed / total) if total > 0 else 0
                        progress_text = (
                            f"🚀 Running: {term}\n"
                            f"📊 {processed:,} / {total:,} files ({pct}%)\n"
                            f"💾 {format_size_mb(mb)} / {format_size_mb(total_mb)}\n"
                            f"⚙️ Mode: LITE"
                        )
                        if progress_text != last_progress_msg and chat_id and msg_id:
                            try:
                                context.bot.edit_message_text(
                                    text=progress_text,
                                    chat_id=chat_id,
                                    message_id=msg_id,
                                )
                                last_progress_msg = progress_text
                            except Exception:
                                pass
            except Exception as e:
                logger.exception(f"Lite watcher error: {e}")

        watcher_thread = threading.Thread(target=watcher, daemon=True)
        watcher_thread.start()

        retcode = proc.wait()

        with RUNNING_LOCK:
            RUNNING_JOB = None

        if chat_id and msg_id:
            final_text = (
                f"✅ Done: {term}\n"
                f"⚙️ Mode: LITE\n"
                f"Returned code: {retcode}"
            )
            try:
                context.bot.edit_message_text(
                    text=final_text,
                    chat_id=chat_id,
                    message_id=msg_id,
                )
            except Exception:
                pass

        logger.info(f"[{now_ts()}] falcon_parse (lite) finished with code {retcode}")

    except Exception as e:
        logger.exception(f"run_falcon_lite error: {e}")
        with RUNNING_LOCK:
            RUNNING_JOB = None
        if update and update.effective_message:
            update.effective_message.reply_text(
                f"❌ Error running lite job for {term}:\n{e}",
                quote=True,
            )
    finally:
        with RUNNING_LOCK:
            RUNNING_JOB = None


def _kill_running_proc():
    """
    Attempt to kill the currently running falcon_parse process.
    This is called from the cancel handler.
    """
    global RUNNING_JOB
    with RUNNING_LOCK:
        job = RUNNING_JOB
    if not job or "proc" not in job:
        return

    proc = job["proc"]
    try:
        proc.kill()
        # Wait up to 10 seconds for clean exit
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Force kill again if still alive
            proc.kill()
            proc.wait()
    except Exception as e:
        logger.exception(f"Error killing process: {e}")


# ---------- Telegram handlers ----------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Falcon Bot\n\n"
        "Commands:\n"
        "/s <domain> [mode] [limit] – start a scan\n"
        "  mode: ulp (default) or lite\n"
        "  example: /s netflix.com ulp 1000\n"
        "  example: /s netflix.com lite 500\n\n"
        "Queue: /q\n"
        "Cancel: ⏹ button on running job message\n"
    )


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUNNING_JOB

    if not update.message or not update.message.text:
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "Usage: /s <domain> [mode] [limit]\n"
            "Example: /s netflix.com ulp 1000"
        )
        return

    term = parts[1]
    mode = parts[2].lower() if len(parts) > 2 else "ulp"
    limit = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None

    if mode not in ("ulp", "lite"):
        await update.message.reply_text("Mode must be 'ulp' or 'lite'.")
        return

    with RUNNING_LOCK:
        if RUNNING_JOB is not None:
            await update.message.reply_text(
                "⚠️ A job is already running. Wait for it to finish or cancel it first."
            )
            return

        # Reserve the job slot
        RUNNING_JOB = {
            "term": term,
            "mode": mode,
            "limit": limit,
            "started_at": now_ts(),
            "proc": None,  # will be set inside runner if needed
        }

    await update.message.reply_text(
        f"🚀 Starting {mode.upper()} scan for: {term}\n"
        f"Limit: {limit if limit else 'unlimited'}"
    )

    # Run in a separate thread so bot stays responsive
    def runner():
        if mode == "ulp":
            run_falcon(term, mode, limit, update, context)
        else:
            run_falcon_lite(term, mode, limit, update, context)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUNNING_JOB
    with RUNNING_LOCK:
        job = RUNNING_JOB

    if not job:
        await update.message.reply_text("✅ Queue is empty. No job running.")
        return

    text = (
        "🔄 Current job:\n"
        f"• Term: {job['term']}\n"
        f"• Mode: {job['mode'].upper()}\n"
        f"• Limit: {job['limit'] if job['limit'] else 'unlimited'}\n"
        f"• Started: {job['started_at']}"
    )
    await update.message.reply_text(text)


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle ⏹ Cancel button press.
    """
    global RUNNING_JOB

    query = update.callback_query
    if not query or not query.data or not query.data.startswith("cancel_job"):
        return

    with RUNNING_LOCK:
        job = RUNNING_JOB
        if not job:
            await query.answer("No job to cancel.", show_alert=True)
            return

    # Edit message to show cancelling
    try:
        await query.edit_message_text(
            text="⏹ Cancelling… Waiting for process to stop..",
        )
    except Exception:
        pass

    await query.answer("Cancelling job…")

    # Kill the process
    _kill_running_proc()

    # Give it a moment to die, then clear state
    time.sleep(1)

    with RUNNING_LOCK:
        RUNNING_JOB = None

    # Final message
    try:
        await query.edit_message_text(
            text="⛔ Cancelled. Job stopped.",
        )
    except Exception:
        pass


# ---------- Main ----------
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set.")
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("s", scan_command))
    app.add_handler(CommandHandler("q", queue_command))

    # Cancel button callback
    app.add_handler(MessageHandler(filters.StatusUpdate.CALLBACK_QUERY, cancel_handler))

    logger.info("Falcon bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
