import logging
import asyncio
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    Application
)
from telegram.request import HTTPXRequest
from scrapers.torob_scraper import TorobScraper
from utils.woocom_handler import GerishmallAPI
import config

import json
import os
import html
import jdatetime

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)

from concurrent.futures import ThreadPoolExecutor
executor = ThreadPoolExecutor(max_workers=2)

class PriceUpdateBot:
    def __init__(self, token, sheets_handler, scraper):
        self.token = token
        self.sheets = sheets_handler
        self.scraper = scraper
        self.settings = self._load_settings()
        
        # Sync config with loaded settings
        config.PRICE_DIFF_GOAL = self.settings.get('price_diff', 100000)
        
        # Increase timeout and pool size
        request = HTTPXRequest(connection_pool_size=20, connect_timeout=60, read_timeout=60)
        self.app = ApplicationBuilder().token(token).request(request).build()
        self._setup_handlers()
        
        self.pending_updates = {}
        self.gerishmall_api = GerishmallAPI()
        self.manual_scraping_lock = False
        self.lock_owner = None
        
        # Performance Cache
        self._sheet_cache = None
        self._cache_time = 0
        self._cache_ttl = 1800 # 30 minutes
        
        # Task Queue for Price Updates
        self.update_queue = asyncio.Queue()
        asyncio.create_task(self.update_worker())
        
        # Tracking for small pinned status headers
        self._pinned_status_messages = {} # chat_id -> message_id
        
        # Start Background Scheduler for Scraper
        self.app.job_queue.run_repeating(self.background_scraper_task, interval=60) # Checks every minute if it's time

    def _load_settings(self):
        if os.path.exists(config.SETTINGS_FILE):
            with open(config.SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            
            # Migration to minutes for all time-based settings
            if settings.get('scan_interval', 0) >= 10: # Likely stored in seconds
                settings['scan_interval'] = max(1, settings['scan_interval'] // 60)
            
            # If auto_sync_interval is large, it's in seconds
            if settings.get('auto_sync_interval', 0) >= 60:
                settings['auto_sync_interval'] = settings['auto_sync_interval'] // 60
                
            return settings
            
        return {
            "admins": {str(config.SUPER_ADMIN_ID): "محمدامین"},
            "notifications": True,
            "scan_interval": 1, # Default 1 minute
            "price_diff": 100000,
            "auto_sync_interval": 60 # Default 60 minutes
        }

    def _save_settings(self):
        os.makedirs(os.path.dirname(config.SETTINGS_FILE), exist_ok=True)
        with open(config.SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.settings, f, ensure_ascii=False, indent=4)

    def is_admin(self, user_id):
        return str(user_id) in self.settings['admins']

    def is_super_admin(self, user_id):
        return user_id == config.SUPER_ADMIN_ID

    def _setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        
        # Professional Menu Handlers
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📥 آپدیت ترب"), self.menu_update_torob))
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex("🔍 بررسی مجدد قیمت‌ها"), self.menu_force_refresh))
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex("🛠 ابزارهای مدیریتی"), self.menu_management_tools))
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex("⚙️ تنظیمات"), self.menu_settings))
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex("❓ راهنما"), self.menu_help))
        
        # Submenu Return
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex("🔙 بازگشت به منوی اصلی"), self.start_command))
        
        # Management Submenu Handlers
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"📊 وضعیت کلی فروشگاه"), self.menu_market_status))
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📦 محصولات بحرانی"), self.menu_critical_products))
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📈 گزارش حاشیه سود"), self.menu_profit_margin_report))
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex("💎 محصولات انحصاری"), self.menu_sole_seller_report))
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📜 لاگ تغییرات اخیر"), self.menu_audit_logs))
        self.app.add_handler(MessageHandler(filters.TEXT & filters.Regex("👥 مدیریت دسترسی‌ها"), self.menu_admin_mgmt))
        
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def start_worker_task(self, context: ContextTypes.DEFAULT_TYPE):
        """Starts the background worker as a task in the bot's loop."""
        asyncio.create_task(self.update_worker())

    async def update_worker(self):
        """Processes price updates sequentially from the queue."""
        logging.info("🚀 Price Update Worker started.")
        while True:
            try:
                task = await self.update_queue.get()
                row_index = task['row_index']
                new_price = task['new_price']
                product_name = task['product_name']
                admin_name = task['admin_name']
                user_id = task['user_id']
                callback_query = task.get('callback_query')
                
                logging.info(f"👷 Worker processing update: {product_name} -> {new_price}")
                
                loop = asyncio.get_event_loop()
                
                # 1. Update Sheet (Async-safe)
                # Fetch old price from the task data (passed through the queue)
                old_price = task.get('old_p', 0)
                shop_product_name = task.get('shop_product_name')

                await loop.run_in_executor(
                    executor, self.sheets.update_cell, row_index, config.COL_SITE_PRICE, new_price
                )
                await loop.run_in_executor(executor, self.sheets.update_cell, row_index, config.COL_ADMIN_NAME, admin_name)

                # RECORD AUDIT LOG
                shamsi_now = jdatetime.datetime.now()
                date_str = shamsi_now.strftime("%Y/%m/%d")
                time_str = f"'{shamsi_now.strftime('%H:%M:%S')}" # Prepend ' so sheets parses as text
                await loop.run_in_executor(
                    executor, 
                    self.sheets.append_log, 
                    admin_name, date_str, time_str, product_name, old_price, new_price
                )

                # 2. Update Website with Smart Retry
                site_success = False
                attempts = 0
                max_retries = 3
                retry_delay = 5
                
                while attempts < max_retries and not site_success:
                    if attempts > 0:
                        logging.info(f"🔄 Retrying API update for {product_name} (Attempt {attempts+1}/{max_retries})...")
                        await asyncio.sleep(retry_delay)
                    
                    site_success = await loop.run_in_executor(
                        executor, self.gerishmall_api.update_price, shop_product_name or product_name, new_price
                    )
                    
                    if site_success: break
                    attempts += 1
                
                # FALLBACK: If API fails after retries, try Selenium (MySiteScraper)
                # This ensures stability as requested.
                if not site_success:
                    logging.warning(f"⚠️ API failed for {product_name}. Falling back to Selenium Scraper...")
                    from scrapers.my_site_scraper import MySiteScraper
                    site_scraper = MySiteScraper()
                    site_success = await loop.run_in_executor(
                        executor, site_scraper.update_price, shop_product_name or product_name, new_price
                    )

                # 3. Finalize UI Update
                status_icon = "🚀" if site_success else "❌"
                tech_status = "پردازش آنی API" if site_success and attempts == 0 else "پردازش با تأخیر (Retry)" if site_success else "شکست نهایی"
                
                safe_name = html.escape(product_name)
                final_text = (
                    f"🏷 <b>{safe_name}</b>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"✅ <b>تایید شد توسط:</b> {admin_name}\n"
                    f"💰 <b>قیمت نهایی:</b> <code>{new_price:,}</code> تومان\n"
                    f"{status_icon} <b>وضعیت فنی:</b> <code>{tech_status}</code>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🕒 زمان تایید: {time.strftime('%H:%M:%S')}"
                )
                
                await self._sync_admin_messages(row_index, final_text, user_id)
                # Release lock in sheet
                await loop.run_in_executor(executor, self.sheets.update_cell, row_index, config.COL_TELEGRAM_MSG_ID, "")
                self._cleanup_pending_alert(row_index)
                
                self.update_queue.task_done()
                logging.info(f"✅ Worker finished update for {product_name}")
                
            except Exception as e:
                logging.error(f"❌ Error in update_worker: {e}")
                await asyncio.sleep(10) # Prevent tight loop on error

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not self.is_admin(user_id):
            await update.message.reply_text("⛔️ شما دسترسی لازم برای استفاده از این بات را ندارید.")
            return

        # Professional Grid Layout
        menu_buttons = [
            [KeyboardButton("🔍 بررسی مجدد قیمت‌ها"), KeyboardButton("📥 آپدیت ترب")],
            [KeyboardButton("🛠 ابزارهای مدیریتی")],
            [KeyboardButton("⚙️ تنظیمات"), KeyboardButton("❓ راهنما")]
        ]
        
        reply_markup = ReplyKeyboardMarkup(menu_buttons, resize_keyboard=True)

        status_text = "✨ **پنل مدیریت هوشمند قیمت**\n"
        status_text += f"👤 کاربر: **{self.settings['admins'].get(str(user_id), 'مدیر')}**\n"
        status_text += "━━━━━━━━━━━━━━\n"
        status_text += f"✅ وضعیت سیستم: `آماده به کار 🟢`\n"
        status_text += f"🔔 ارسال خودکار پیشنهادات: `{'روشن' if self.settings['notifications'] else 'خاموش'}`\n"
        status_text += "━━━━━━━━━━━━━━\n"
        status_text += "گزینه مورد نظر را از منوی زیر انتخاب کنید:"

        await update.message.reply_text(
            status_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )


    async def menu_management_tools(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_admin(update.effective_user.id): return
        
        menu_buttons = [
            [KeyboardButton("📊 وضعیت کلی فروشگاه")],
            [KeyboardButton("📈 گزارش حاشیه سود"), KeyboardButton("💎 محصولات انحصاری")],
            [KeyboardButton("📦 محصولات بحرانی"), KeyboardButton("📜 لاگ تغییرات اخیر")],
            [KeyboardButton("👥 مدیریت دسترسی‌ها"), KeyboardButton("🔙 بازگشت به منوی اصلی")]
        ]
        
        reply_markup = ReplyKeyboardMarkup(menu_buttons, resize_keyboard=True)
        await update.message.reply_text("🛠 **ابزارهای مدیریتی و گزارش‌گیری**\nانتخاب کنید:", reply_markup=reply_markup, parse_mode='Markdown')

    async def menu_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_admin(update.effective_user.id): return
        help_text = (
            "❓ **راهنمای جامع پنل مدیریت**\n\n"
            "📊 **وضعیت فروشگاه :** گزارش در لحظه از قیمت‌های شیت و ترب.\n"
            "♻️ **اسکن و بروزرسانی:** پاکسازی تمام قفل‌ها و شروع مانیتورینگ مجدد.\n"
            "📦 **محصولات بحرانی:** کالاهایی که قیمت ترب‌شان به قیمت خرید شما نزدیک شده.\n"
            "⚙️ **تنظیمات:** مدیریت فواصل قیمتی، اعلان‌ها و سرعت اسکن.\n"
            "👥 **مدیریت دسترسی‌ها:** افزودن یا حذف دسترسی دیگران (فقط مدیر اصلی).\n\n"
            "💡 _نکته: قیمت پیشنهادی همیشه ۱۰۰ هزار تومان ارزان‌تر از ترب است مگر اینکه از قیمت خرید کمتر شود._"
        )
        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def menu_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_admin(update.effective_user.id): return
        
        if not self.is_super_admin(update.effective_user.id):
            msg_text = "🚫 دسترسی به تنظیمات فقط برای مدیر اصلی (Super Admin) مجاز است."
            if update.callback_query:
                await update.callback_query.message.edit_text(msg_text)
            else:
                await update.message.reply_text(msg_text)
            return

        notif_status = "🔔 فعال" if self.settings['notifications'] else "🔕 غیرفعال"
        
        keyboard = [
            [InlineKeyboardButton(f"اعلان لحظه‌ای: {notif_status}", callback_data="toggle_notif")],
            [InlineKeyboardButton("✏️ تغییر فاصله قیمتی ", callback_data="change_goal")],
            [InlineKeyboardButton("⏱ زمانبندی مانیتورینگ ", callback_data="change_interval")],
            [InlineKeyboardButton("📥 تنظیم آپدیت خودکار ترب", callback_data="change_auto_sync")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        settings_text = (
            f"⚙️ **تنظیمات سیستم**\n\n"
            f"💰 فاصله رقابتی: `{self.settings['price_diff']:,}` تومان\n"
            f"⏱ مانیتورینگ فروشگاه : هر `{self.settings['scan_interval']}` دقیقه\n"
            f"📥 آپدیت ترب: هر `{self.settings.get('auto_sync_interval', 60)}` دقیقه\n"
            f"🔔 وضعیت اعلان: `{notif_status}`\n\n"
            f"یکی از موارد زیر را برای تغییر انتخاب کنید:"
        )
        
        if update.callback_query:
            await update.callback_query.message.edit_text(settings_text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text(settings_text, reply_markup=reply_markup, parse_mode='Markdown')

    async def menu_admin_mgmt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not self.is_super_admin(user_id):
            await update.message.reply_text("🚫 دسترسی به مدیریت کاربران فقط برای مدیر اصلی (Super Admin) مجاز است.")
            return
            
        admin_list = "\n".join([f"• **{name}** (`{uid}`)" for uid, name in self.settings['admins'].items()])
        
        keyboard = [
            [InlineKeyboardButton("➕ افزودن ادمین جدید", callback_data="add_admin")],
            [InlineKeyboardButton("➖ حذف ادمین", callback_data="remove_admin")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"👥 **لیست ادمین‌های مجاز:**\n\n{admin_list}\n\n"
            f"برای مدیریت دسترسی‌ها از دکمه‌های زیر استفاده کنید:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )



    async def menu_force_refresh(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Clears all locks and forces a re-scan of the market with progress reporting."""
        if not self.is_admin(update.effective_user.id): return
        
        if getattr(self, 'manual_scraping_lock', False):
            await update.message.reply_text("⛔️ یک عملیات سنگین در حال اجراست. لطفا چند لحظه صبر کنید.")
            return

        self.manual_scraping_lock = True
        self.lock_owner = "force_refresh"
        
        msg = await update.message.reply_text("⏳ در حال پاکسازی هشدارهای قبلی و شروع مانیتورینگ جدید...")
        start_t = time.time()
        
        try:
            loop = asyncio.get_event_loop()
            await self._update_progress(msg, "پاکسازی قفل‌های شیت", 10, start_t)
            
            # 1. Clear locks in sheet (Locked column)
            col_idx = await loop.run_in_executor(executor, self.sheets.find_column_index, config.COL_TELEGRAM_MSG_ID)
            if col_idx:
                records = await self._get_data(force_refresh=True)
                row_count = len(records)
                if row_count > 0:
                    import gspread
                    col_letter = gspread.utils.rowcol_to_a1(1, col_idx)[:1]
                    range_to_clear = f"{col_letter}2:{col_letter}{row_count + 1}"
                    empty_values = [[""] for _ in range(row_count)]
                    await loop.run_in_executor(executor, self.sheets.sheet.update, range_to_clear, empty_values)
            
            # 2. Clear local memory
            self.pending_updates.clear()
            
            # 3. Start New Scan with Progress
            await self._update_progress(msg, "شروع اسکن مجدد", 20, start_t)
            results = await self.run_monitoring_scan(msg, start_t)
            
            await self._update_progress(msg, "بررسی مجدد قیمت‌ها", 100, start_t) # Final call with the real name
            await msg.edit_text(
                f"✅ **عملیات مانیتورینگ مجدد با موفقیت تمام شد.**\n\n"
                f"📊 تعداد کالا پردازش شده: `{results['total']}`\n"
                f"🔔 هشدارهای جدید ارسال شده: `{results['alerts']}`\n"
                f"⏱ زمان کل: `{int(time.time()-start_t)} ثانیه`",
                parse_mode='Markdown'
            )
        except Exception as e:
            logging.error(f"Force Refresh Error: {e}")
            await self._update_progress(msg, "خطا در عملیات", 0, start_t, error=True)
            await msg.edit_text(f"❌ خطا در مانیتورینگ مجدد: {e}")
        finally:
            self.manual_scraping_lock = False
            self.lock_owner = None


    async def _get_data(self, force_refresh=False):
        """Helper to get sheet data with caching to boost speed."""
        now = time.time()
        if not self._sheet_cache or force_refresh or (now - self._cache_time > self._cache_ttl):
            loop = asyncio.get_event_loop()
            self._sheet_cache = await loop.run_in_executor(executor, self.sheets.get_all_records)
            self._cache_time = now
        return self._sheet_cache


    async def menu_market_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Generates a real-time summary using caching for instant response."""
        msg = await update.message.reply_text("⏳ در حال تحلیل هوشمند داده‌ها...")
        start_t = time.time()
        
        try:
            await self._update_progress(msg, "تحلیل دیتای شیت", 30, start_t)
            records = await self._get_data() # Uses cache if available
            await self._update_progress(msg, "محاسبه آمار", 70, start_t)
            
            total = len(records)
            needs_update = 0
            below_margin = 0
            perfect_price = 0
            
            for rec in records:
                t_price = int(rec.get(config.COL_TOROB_PRICE) or 0)
                s_price = int(rec.get(config.COL_SITE_PRICE) or 0)
                p_cost = int(rec.get(config.COL_PURCHASE_COST) or 0)
                
                if t_price <= 0: continue
                ideal = max(t_price - config.PRICE_DIFF_GOAL, p_cost)
                
                if s_price == ideal: perfect_price += 1
                else: needs_update += 1
                
                if s_price > 0:
                    margin = (s_price - p_cost) / s_price
                    if margin < 0.10: below_margin += 1
            
            await self._update_progress(msg, "تکمیل گزارش", 100, start_t)
            
            current_time = time.strftime('%H:%M:%S')
            report = (
                f"📊 **گزارش تخصصی وضعیت فروشگاه**\n\n"
                f"📦 کل کالاهای پایش شده: `{total}`\n"
                f"✅ محصولات با قیمت رقابتی: `{perfect_price}`\n"
                f"⚠️ محصولات نیازمند اصلاح: `{needs_update}`\n"
                f"🔴 حاشیه سود بحرانی (<۱۰٪): `{below_margin}`\n\n"
                f"🕒 بروزرسانی در: `{current_time}`"
            )
            await msg.edit_text(report, parse_mode='Markdown')
        except Exception as e:
            await msg.edit_text(f"❌ خطا در تحلیل سریع: {e}")

    async def menu_update_torob(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manually triggers the products sync with dynamic UI."""
        if not self.is_admin(update.effective_user.id): return
        if self.manual_scraping_lock:
            owner_text = "خودکار (سیستمی)" if self.lock_owner == "background" else "دستی (توسط ادمین)"
            await update.message.reply_text(f"⚠️ سیستم در حال انجام یک عملیات سنگین {owner_text} است.\nلطفاً تا پایان آن صبر کنید یا از بخش 'بررسی مجدد قیمت‌ها' قفل را پاکسازی کنید.")
            return
            
        self.manual_scraping_lock = True
        self.lock_owner = "manual"
        msg = await update.message.reply_text("🚀 فرآیند آپدیت ترب آغاز شد...")
        start_t = time.time()
        
        try:
            # We start with a low percentage to show action, but without jumping.
            # Removed the 20% jump from here.
            
            # Execute scraper with callback
            results = await self.run_sync_logic(msg, start_t)
            
            if results is None:
                await self._update_progress(msg, "خطا در استخراج", 0, start_t, error=True)
                await msg.edit_text(
                    "❌ **خطا در استخراج محصولات از ترب.**\n\n"
                    "این خطا معمولاً به دلایل زیر رخ می‌دهد:\n"
                    "۱. شناسایی ربات توسط ترب (کپچا)\n"
                    "۲. کندی اینترنت یا قطع اتصال به سایت ترب\n\n"
                    "💡 پیشنهاد: کمی صبر کنید و مجدداً تلاش کنید.",
                    parse_mode='Markdown'
                )
                return

            await self._update_progress(msg, "آپدیت ترب", 100, start_t)
            
            summary = (
                f"✅ **آپدیت ترب با موفقیت انجام شد.**\n\n"
                f"🆕 محصولات جدید: `{results['added']}`\n"
                f"🔄 بروزرسانی شده: `{results['updated']}`\n"
                f"📂 محصولات ابقا شده (آرشیو): `{results.get('kept', 0)}`\n\n"
                f"⏱ کل زمان: `{int(time.time()-start_t)} ثانیه`"
            )
            await msg.edit_text(summary, parse_mode='Markdown')
            await self._get_data(force_refresh=True) # Refresh cache after update
        except Exception as e:
            await self._update_progress(msg, "خطا در عملیات", 0, start_t, error=True)
            await msg.edit_text(f"❌ خطا: {e}")
        finally:
            self.manual_scraping_lock = False
            self.lock_owner = None

    async def run_sync_logic(self, progress_msg=None, start_t=None):
        """Optimized sync logic with real-time granular progress tracking."""
        loop = asyncio.get_event_loop()
        scraper = TorobScraper()
        SHOP_URL = "https://torob.com/shop/116426/%DA%AF%D8%B1%DB%8C%D8%B4-%D9%85%D8%A7%D9%84/%D9%85%D8%AD%D8%B5%D9%88%D9%84%D8%A7%D8%AA/"
        
        def scraper_progress_callback(current, total, name):
            """Sync callback from scraper thread to async Telegram UI."""
            percent = int((current / total) * 100)
            status_text = f"محصول {current} از {total}: {name[:20]}..."
            asyncio.run_coroutine_threadsafe(
                self._update_progress(progress_msg, status_text, percent, start_t),
                loop
            )

        def scraper_captcha_callback():
            """Notify admin that a CAPTCHA needs manual solution."""
            if not progress_msg: return
            alert_text = (
                "🧩 **هشدار کپچا (CAPTCHA)**\n\n"
                "سایت ترب فعالیت ربات را شناسایی کرده است.\n"
                "یک پنجره مرورگر باز شده است. لطفاً کپچا را به صورت دستی حل کنید.\n"
                "پس از حل کپچا، پردازش به صورت خودکار ادامه خواهد یافت."
            )
            asyncio.run_coroutine_threadsafe(
                self.app.bot.send_message(chat_id=progress_msg.chat_id, text=alert_text, parse_mode='Markdown'),
                loop
            )

        try:
            if progress_msg:
                asyncio.run_coroutine_threadsafe(self._update_progress(progress_msg, "در حال بررسی لیست ترب...", 5, start_t), loop)
            
            # Execute scraper with callback
            shop_products = await loop.run_in_executor(
                executor, 
                scraper.get_shop_products, 
                SHOP_URL, 
                scraper_progress_callback if progress_msg else None,
                scraper_captcha_callback if progress_msg else None
            )
            
            if progress_msg:
                asyncio.run_coroutine_threadsafe(self._update_progress(progress_msg, "بررسی دیتای شیت...", 90, start_t), loop)
            
            if shop_products is None:
                if progress_msg:
                    await self._update_progress(progress_msg, "خطا در استخراج محصولات (باگ یا کپچا)", 0, start_t, error=True)
                return None
                
            records = await loop.run_in_executor(executor, self.sheets.get_all_records)
            sheet_map = {r.get(config.COL_PRODUCT_NAME): i for i, r in enumerate(records)}
            scraped_map = {p['name']: p for p in shop_products}
            
            added = updated = kept = 0
            all_rows = []
            headers = config.SHEET_COLUMNS
            red_rows = []

            # 1. Update Existing Products and Keep those not on Torob
            processed_scraped_names = set()
            
            for i, record in enumerate(records):
                name = record.get(config.COL_PRODUCT_NAME)
                if not name: continue
                
                if name in scraped_map:
                    p = scraped_map[name]
                    processed_scraped_names.add(name)
                    
                    is_my_shop = config.MY_SHOP_NAME in str(p.get('shop_name', ''))
                    is_sole_seller = is_my_shop and (not p.get('second_price') or int(p.get('second_price')) == 0)
                    target_url = p.get('second_product_url') if is_my_shop and p.get('second_product_url') else p.get('product_url')

                    row_data = [
                        name,
                        p.get('shop_product_name', ""),
                        record.get(config.COL_PURCHASE_COST, ""), # PRESERVE MANUAL VALUE
                        p.get('shop_site_price', ""),
                        p['price'],
                        p['shop_name'],
                        self.sheets.format_hyperlink(target_url, "لینک محصول"),
                        self.sheets.format_hyperlink(p['image_url'], "عکس محصول"),
                        record.get(config.COL_TELEGRAM_MSG_ID, ""),
                        record.get(config.COL_ADMIN_NAME, ""),
                        p.get('second_price', ""),
                        p.get('second_shop_name', "")
                    ]
                    updated += 1
                    if is_sole_seller:
                        red_rows.append(len(all_rows) + 1)
                else:
                    # Not found on Torob anymore, but KEEP in sheet to preserve manual data
                    row_data = [record.get(h, "") for h in headers]
                    # Optionally mark it
                    # row_data[headers.index(config.COL_SHOP_NAME)] = "⚠️ ناموجود در ترب"
                    kept += 1
                
                all_rows.append(row_data)

            # 2. Add New Products found on Torob
            for name, p in scraped_map.items():
                if name not in processed_scraped_names:
                    is_my_shop = config.MY_SHOP_NAME in str(p.get('shop_name', ''))
                    is_sole_seller = is_my_shop and (not p.get('second_price') or int(p.get('second_price')) == 0)
                    target_url = p.get('second_product_url') if is_my_shop and p.get('second_product_url') else p.get('product_url')

                    row_data = [
                        name, p.get('shop_product_name', ""), "", p.get('shop_site_price', ""), p['price'], p['shop_name'],
                        self.sheets.format_hyperlink(target_url, "لینک محصول"),
                        self.sheets.format_hyperlink(p['image_url'], "عکس محصول"), "", "",
                        p.get('second_price', ""),
                        p.get('second_shop_name', "")
                    ]
                    added += 1
                    all_rows.append(row_data)
                    if is_sole_seller:
                        red_rows.append(len(all_rows) + 1) # Already added to list, so adjust index

            if progress_msg: await self._update_progress(progress_msg, "اعمال تغییرات روی گوگل‌شیت", 95, start_t)

            # BATCH UPDATE: Overwrite the sheet data starting from row 2
            def batch_overwrite():
                import gspread
                if all_rows:
                    # 1. Update row 2 onwards FIRST (to avoid font reset)
                    end_col_letter = gspread.utils.rowcol_to_a1(1, len(headers))[:1]
                    self.sheets.sheet.update("A2", all_rows, value_input_option="USER_ENTERED")
                    
                    # 2. Apply premium styling (Font, Green Headers, Number formatting) 
                    # CRITICAL: Must be AFTER update to prevent default font override
                    self.sheets.apply_style()
                    
                    # 3. 🎨 COLOR MANAGEMENT (Exclusive Products = Red)
                    if red_rows:
                        # Reset all data rows to white/default if necessary (apply_style should handle defaults)
                        red_fmt = {"backgroundColor": {"red": 1.0, "green": 0.8, "blue": 0.8}, "textFormat": {"bold": True, "fontFamily": "Vazirmatn"}}
                        ranges_to_color = []
                        for r_idx in red_rows:
                            abs_row = r_idx + 1 # +1 because red_rows was populated based on all_rows (0-indexed list) + 1
                            ranges_to_color.append({
                                "range": f"A{abs_row}:{end_col_letter}{abs_row}",
                                "format": red_fmt
                            })
                        
                        # Apply red coloring to specific rows
                        if ranges_to_color:
                            self.sheets.sheet.batch_format(ranges_to_color)
            
            await loop.run_in_executor(executor, batch_overwrite)
            await self._get_data(force_refresh=True) # Important: Refresh cache with new styled data
            return {"added": added, "updated": updated, "deleted": 0, "kept": kept}
        finally:
            scraper.close()

    async def background_scraper_task(self, context: ContextTypes.DEFAULT_TYPE):
        """Automatic periodic sync."""
        last_sync = self.settings.get('last_auto_sync', 0)
        auto_interval = self.settings.get('auto_sync_interval', 60) # Default 1 hour (minutes)
        
        if auto_interval > 0 and (time.time() - last_sync) >= (auto_interval * 60):
            if self.manual_scraping_lock:
                logging.warning("Background Sync skipped: another heavy operation is running.")
                return
            
            logging.info("Starting Automatic Torob Sync...")
            self.manual_scraping_lock = True
            self.lock_owner = "background"
            try:
                logging.info("Executing run_sync_logic in background...")
                await self.run_sync_logic()
                self.settings['last_auto_sync'] = time.time()
                self._save_settings()
                logging.info("Automatic Sync completed.")
            except Exception as e:
                logging.error(f"Auto Sync Failed: {e}")
            finally:
                self.manual_scraping_lock = False
                self.lock_owner = None
        
    async def run_monitoring_scan(self, progress_msg=None, start_t=None):
        """High-performance monitoring scan with batch sheet locking and progress reporting."""
        loop = asyncio.get_event_loop()
        records = await self._get_data(force_refresh=True)
        total = len(records)
        alerts_count = 0
        
        if not records:
            return {"total": 0, "alerts": 0}

        # Find column indices for locking
        msg_col_idx = await loop.run_in_executor(executor, self.sheets.find_column_index, config.COL_TELEGRAM_MSG_ID)

        for idx, record in enumerate(records):
            current_row = idx + 2
            product_name = record.get(config.COL_PRODUCT_NAME, "کالای بی نام")
            
            if progress_msg and idx % 2 == 0: # Update every 2 products to avoid TG rate limits
                percent = 20 + int((idx / total) * 75)
                status = f"پایش محصول {idx+1} از {total}: {product_name[:15]}..."
                asyncio.run_coroutine_threadsafe(self._update_progress(progress_msg, status, percent, start_t), loop)

            purchase_cost = int(record.get(config.COL_PURCHASE_COST) or 0)
            site_price = int(record.get(config.COL_SITE_PRICE) or 0)
            torob_price = int(record.get(config.COL_TOROB_PRICE) or 0)
            shop_name = record.get(config.COL_SHOP_NAME, "")
            second_price = int(record.get(config.COL_SECOND_TOROB_PRICE) or 0)
            existing_msg_id = record.get(config.COL_TELEGRAM_MSG_ID)
            shop_product_name = record.get(config.COL_SHOP_PRODUCT_NAME)

            if not product_name or not torob_price or torob_price == 0:
                continue

            # NEW LOGIC: Check if we are the cheapest seller
            is_my_shop = config.MY_SHOP_NAME in str(shop_name)
            
            if is_my_shop:
                if not second_price or int(second_price) == 0:
                    # Case A: ONLY Gerishmall is selling
                    # Coloring is already handled in batch during run_sync_logic to save API quota.
                    # Just clear any old alerts.
                    if existing_msg_id:
                        await loop.run_in_executor(executor, self.sheets.update_cell_by_index, current_row, msg_col_idx, "")
                    continue
                else:
                    # Case B: Gerishmall is cheapest but others exist
                    # Benchmark is the SECOND cheapest
                    benchmark_price = second_price
                    suggestion = max(int(second_price) - config.PRICE_DIFF_GOAL, purchase_cost)
                    status_note = " (بر اساس رقیب دوم)"
            else:
                # Normal Case: Competitor is cheaper
                benchmark_price = torob_price
                suggestion = max(torob_price - config.PRICE_DIFF_GOAL, purchase_cost)
                status_note = ""

            # Skip if already correct
            if site_price == suggestion:
                if existing_msg_id:
                    await loop.run_in_executor(executor, self.sheets.update_cell_by_index, current_row, msg_col_idx, "")
                continue

            # Anti-spam: Skip if alert is active
            if existing_msg_id and str(existing_msg_id).strip():
                continue
            
            # Send Alert
            diff = suggestion - site_price
            if diff > 0:
                diff_display = f" {diff:,}⬆️ (+)"
                suggestion_icon = "🟢"
            else:
                diff_display = f" {abs(diff):,}⬇️ (-)"
                suggestion_icon = "🔴"

            alert_data = {
                'name': product_name,
                'shop_product_name': shop_product_name,
                'purchase_cost': purchase_cost,
                'torob_price': benchmark_price, # Benchmark instead of our own price
                'site_price': site_price,
                'suggestion': suggestion,
                'suggestion_icon': suggestion_icon,
                'diff_display': diff_display,
                'status_note': status_note,
                'row_index': current_row,
                'image_url': record.get(config.COL_IMAGE_URL, ""),
                'product_url': record.get(config.COL_PRODUCT_URL, "")
            }
            # Hyperlink extraction logic
            for key in ['image_url', 'product_url']:
                if "=HYPERLINK" in str(alert_data[key]):
                    import re
                    match = re.search(r'HYPERLINK\([\'"]([^\'"]+)[\'"]', alert_data[key])
                    alert_data[key] = match.group(1) if match else ""
            if not str(alert_data['product_url']).startswith("http"):
                alert_data['product_url'] = "https://torob.com"

            if self.settings.get('notifications', True):
                sent_msg_ids = await self.send_price_alert(alert_data)
                if sent_msg_ids:
                    await loop.run_in_executor(executor, self.sheets.update_cell_by_index, current_row, msg_col_idx, sent_msg_ids)
                    alerts_count += 1
        
        return {"total": total, "alerts": alerts_count}

    async def _update_pinned_header(self, chat_id, step_name, percent, finished=False, error=False):
        """Manages a separate small pinned status message for professional look."""
        try:
            # Emoji Selection Logic
            def get_emoji(name):
                name = name.lower()
                if "ترب" in name: return "📥"
                if any(x in name for x in ["بررسی", "مانیتور", "اسکن", "قیمت"]): return "🔍"
                if any(x in name for x in ["پاکسازی", "رفرش", "قفل"]): return "♻️"
                return "⚙️"

            emoji = "❌" if error else ("✅" if finished else get_emoji(step_name))
            
            if finished:
                # The user requested: فرآیند --- با موفقیت انجام شد. or specific for task
                # We try to use a more natural name if possible
                task_display = "بروزرسانی" if "ترب" in step_name else "مانیتورینگ" if "بررسی" in step_name else step_name
                text = f"{emoji} فرآیند **{task_display}** با موفقیت انجام شد."
            elif error:
                text = f"{emoji} خطا در فرآیند **{step_name}**!"
            else:
                text = f"پیشرفت: {percent}% {emoji}"

            pinned_msg_id = self._pinned_status_messages.get(chat_id)

            if not pinned_msg_id:
                # Create and pin
                msg = await self.app.bot.send_message(chat_id, text, parse_mode='Markdown')
                self._pinned_status_messages[chat_id] = msg.message_id
                try:
                    await msg.pin(disable_notification=True)
                except: pass
            else:
                # Update existing
                try:
                    await self.app.bot.edit_message_text(chat_id=chat_id, message_id=pinned_msg_id, text=text, parse_mode='Markdown')
                except Exception:
                    # Message might have been deleted manually
                    msg = await self.app.bot.send_message(chat_id, text, parse_mode='Markdown')
                    self._pinned_status_messages[chat_id] = msg.message_id
                    try: await msg.pin(disable_notification=True)
                    except: pass

            if finished or error:
                # Schedule unpin and delete after 10 seconds
                async def cleanup_pinned(context):
                    msg_id = self._pinned_status_messages.get(chat_id)
                    if msg_id:
                        try:
                            await self.app.bot.unpin_chat_message(chat_id, msg_id)
                            await self.app.bot.delete_message(chat_id, msg_id)
                        except: pass
                        self._pinned_status_messages.pop(chat_id, None)

                self.app.job_queue.run_once(cleanup_pinned, 10)
        except Exception as e:
            logging.error(f"Pinned header error: {e}")

    async def _update_progress(self, message, step_name, percent, start_time, error=False):
        """Premium UI: Updates a single message with progress bar. Also manages pinned header."""
        chat_id = message.chat.id
        finished = percent >= 100
        
        # 1. Update the small pinned header (Professional Request)
        await self._update_pinned_header(chat_id, step_name, percent, finished, error)

        # 2. Update the main detailed message
        bar_length = 10
        filled = int(bar_length * percent / 100)
        bar = "🟩" * filled + "⬜" * (bar_length - filled)
        
        # Calculate ETA
        elapsed = time.time() - start_time
        if percent > 0:
            total_est = elapsed / (percent / 100)
            eta = int(total_est - elapsed)
            eta_str = f"{eta} ثانیه" if eta > 0 else "لحظاتی دیگر"
        else:
            eta_str = "در حال محاسبه..."

        text = (
            f"🔄 **در حال عملیات: {step_name}**\n"
            f"━━━━━━━━━━━━━━\n"
            f"📊 پیشرفت: `{percent}%` [{bar}]\n"
            f"⏳ زمان تقریبی باقی‌مانده: `{eta_str}`\n"
            f"━━━━━━━━━━━━━━\n"
            f"⚡️تا پایان فرایند منتظر بمانید.\n"
        )
        try:
            # Don't pin the main message anymore (User asked for small header only)
            await message.edit_text(text, parse_mode='Markdown', disable_web_page_preview=True)
        except Exception: pass

    async def menu_profit_margin_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Shows products sorted by profit margin (Highest to Lowest)."""
        msg = await update.message.reply_text("⏳ در حال محاسبه حاشیه سود محصولات...")
        records = await self._get_data()
        
        margin_list = []
        for rec in records:
            name = rec.get(config.COL_PRODUCT_NAME)
            s_price = int(rec.get(config.COL_SITE_PRICE) or 0)
            p_cost = int(rec.get(config.COL_PURCHASE_COST) or 0)
            
            if s_price > 0 and p_cost > 0:
                profit = s_price - p_cost
                margin_pct = (profit / s_price) * 100
                margin_list.append({
                    'name': name,
                    'profit': profit,
                    'margin': margin_pct
                })
        
        if not margin_list:
            await msg.edit_text("❌ دیتای کافی برای محاسبه حاشیه سود یافت نشد.")
            return

        # Sort: Highest Margin to Lowest
        import html
        margin_list.sort(key=lambda x: x['margin'], reverse=True)
        
        report = "📈 <b>گزارش حاشیه سود (بیشترین به کمترین):</b>\n\n"
        for item in margin_list[:25]: # Show top 25
            safe_item_name = html.escape(item['name'][:25])
            report += f"🔹 {safe_item_name}...\n   💰 سود: <code>{item['profit']:,}</code> | <code>{item['margin']:.1f}%</code> \n\n"
        
        if len(margin_list) > 25:
            report += f" <i>... و {len(margin_list)-25} محصول دیگر</i>"

        await msg.edit_text(report, parse_mode='HTML')

    async def menu_sole_seller_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Lists products where Gerishmall is the ONLY seller on Torob."""
        msg = await update.message.reply_text("🔍 در حال شناسایی محصولات انحصاری گریش‌مال...")
        records = await self._get_data()
        
        exclusive_list = []
        for rec in records:
            name = rec.get(config.COL_PRODUCT_NAME)
            shop_name = str(rec.get(config.COL_SHOP_NAME, ""))
            second_price = rec.get(config.COL_SECOND_TOROB_PRICE, "")
            
            # Condition: We are the top shop AND no second shop exists
            is_my_shop = config.MY_SHOP_NAME in shop_name
            is_sole = not second_price or str(second_price).strip() == "0" or str(second_price).strip() == ""
            
            if is_my_shop and is_sole:
                exclusive_list.append(name)
        
        if not exclusive_list:
            await msg.edit_text("⚪️ هیچ محصول انحصاری یافت نشد (همه محصولات رقیب دارند).")
        else:
            import html
            report = f"💎 <b>محصولات انحصاری ({len(exclusive_list)} مورد):</b>\n"
            report += "<i>(محصولاتی که فقط گریش‌مال در ترب می‌فروشد)</i>\n\n"
            
            lines = []
            for name in exclusive_list[:25]:
                lines.append(f"✅ {html.escape(name)}")
            
            report += "\n".join(lines)
            
            if len(exclusive_list) > 25:
                report += f"\n\n... و {len(exclusive_list)-25} مورد دیگر."
            
            await msg.edit_text(report, parse_mode='HTML')

    async def menu_critical_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Shows products where Torob price is so low that we can't compete without losing money."""
        msg = await update.message.reply_text("🔍 در حال شناسایی محصولات بحرانی...")
        
        start_t = time.time()
        await self._update_progress(msg, "تحلیل حاشیه سود", 50, start_t)
        
        records = await self._get_data() # Uses cache
        critical_list = []
        for rec in records:
            s_price = int(rec.get(config.COL_SITE_PRICE) or 0)
            p_cost = int(rec.get(config.COL_PURCHASE_COST) or 0)
            
            if s_price > 0:
                margin = (s_price - p_cost) / s_price
                if margin < 0.10:
                    margin_pct = int(margin * 100)
                    critical_list.append(f"• {rec.get(config.COL_PRODUCT_NAME)} (سود: {margin_pct}%)")
        
        if not critical_list:
            await msg.edit_text("✅ هیچ محصولی با حاشیه سود کمتر از ۱۰٪ یافت نشد.")
        else:
            import html
            text = "🔴 <b>لیست محصولات با حاشیه سود بحرانی (زیر ۱۰٪):</b>\n\n"
            
            lines = []
            for item in critical_list[:15]:
                lines.append(html.escape(item))
            
            text += "\n".join(lines)
            
            if len(critical_list) > 15:
                text += f"\n\n... و {len(critical_list)-15} مورد دیگر."
            await msg.edit_text(text, parse_mode='HTML')

    async def menu_audit_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays the last 10 price change logs."""
        if not self.is_admin(update.effective_user.id): return
        
        msg = await update.message.reply_text("⏳ در حال دریافت آخرین تغییرات...")
        
        try:
            loop = asyncio.get_event_loop()
            logs = await loop.run_in_executor(executor, self.sheets.get_recent_logs, 10)
            
            if not logs:
                await msg.edit_text("⚪️ هیچ لاگی در سیستم ثبت نشده است.")
                return
            
            report = "📜 **۱۰ تغییر اخیر در سیستم:**\n\n"
            for log in logs:
                # Format: Who, Date, Time, Product, Old -> New
                admin = log.get(config.LOG_COL_ADMIN, "ناشناس")
                date = log.get(config.LOG_COL_DATE, "")
                time_val = str(log.get(config.LOG_COL_TIME, ""))
                product = log.get(config.LOG_COL_PRODUCT) or log.get("نام محصول") or "محصول"
                old_p = log.get(config.LOG_COL_OLD_PRICE, 0)
                new_p = log.get(config.LOG_COL_NEW_PRICE, 0)
                
                report += (
                    f"👤 {admin} | 📅 {date} {time_val}\n"
                    f"📦 {html.escape(str(product)[:30])}...\n"
                    f"💰 `{old_p:,}` ➔ `{new_p:,}` تومان\n"
                    f"━━━━━━━━━━━━━━\n"
                )
            
            await msg.edit_text(report, parse_mode='Markdown')
        except Exception as e:
            logging.error(f"Error fetching audit logs: {e}")
            await msg.edit_text(f"❌ خطا در دریافت لاگ‌ها: {e}")


    async def send_price_alert(self, product_data):
        """
        Broadcasting: Sends an attractive alert to ALL registered admins.
        Uses HTML mode for reliable parsing with Persian characters.
        """
        import html
        row_index = product_data['row_index']
        name_to_display = product_data.get('shop_product_name')
        if not name_to_display or not str(name_to_display).strip():
            name_to_display = product_data.get('name')
        safe_name = html.escape(str(name_to_display))
        
        text = (
            f"🏷 <b>{safe_name}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 <b>قیمت خرید:</b> <code>{product_data['purchase_cost']:,}</code> تومان\n"
            f"💻 <b>قیمت گریش مال:</b> <code>{product_data['site_price']:,}</code> تومان\n"
            f"🔍 <b>قیمت ترب:</b> <code>{product_data['torob_price']:,}</code> تومان\n\n"
            f"✨ <b>قیمت پیشنهادی:</b> {product_data.get('suggestion_icon', '🟢')} <code>{product_data['suggestion']:,}</code> تومان\n"
            f"📊 <b>مقدار تغییر:</b> <code>{product_data['diff_display']}</code>\n"
            f"━━━━━━━━━━━━━━\n"
            f"👆 <i>{product_data['status_note']} قیمت پیشنهادی بر اساس {config.PRICE_DIFF_GOAL:,} تومان ارزان‌تر از رقیب محاسبه شده است.</i>"
        )
        keyboard = [
            [
                InlineKeyboardButton("✅ تأیید و بروزرسانی", callback_data=f"approve_{row_index}"),
                InlineKeyboardButton("❌ انصراف", callback_data=f"ignore_{row_index}")
            ],
            [
                InlineKeyboardButton("✏️ تغییر قیمت دستی", callback_data=f"custom_{row_index}")
            ],
            [
                InlineKeyboardButton("🖼 عکس محصول", url=product_data.get('image_url', 'https://torob.com')),
                InlineKeyboardButton("🔍 سایت فروشنده", url=product_data.get('product_url', 'https://torob.com'))
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        sent_tracking = []
        msg_map = {}
        
        for admin_id in self.settings['admins'].keys():
            try:
                msg = await self.app.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode='HTML' # CHANGED TO HTML
                )
                if msg:
                    sent_tracking.append(f"{admin_id}:{msg.message_id}")
                    msg_map[str(admin_id)] = msg.message_id
            except Exception as e:
                logging.error(f"Failed to send alert to admin {admin_id}: {e}")

        # Store in memory for ALL admins with the map of all messages
        product_data['msg_map'] = msg_map
        for admin_id in self.settings['admins'].keys():
            self.pending_updates[f"{admin_id}_{row_index}"] = product_data

        return ",".join(sent_tracking) if sent_tracking else None

    async def _sync_admin_messages(self, row_index, text_suffix, acting_user_id):
        """
        Updates the original alert message for ALL admins using the cached msg_map.
        """
        key = f"{acting_user_id}_{row_index}"
        update_data = self.pending_updates.get(key)
        
        if not update_data or 'msg_map' not in update_data:
            logging.warning(f"Sync failed: No msg_map found for row {row_index}")
            return

        msg_map = update_data['msg_map']
        for chat_id, msg_id in msg_map.items():
            try:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text_suffix,
                    parse_mode='HTML'
                )
            except Exception as e:
                logging.debug(f"Could not edit message for admin {chat_id}: {e}")

    async def _clear_awaiting_states(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Standardizes clearing of awaiting states and deletes previous prompt messages."""
        keys_to_clear = [
            'awaiting_goal', 'awaiting_interval', 'awaiting_auto_sync', 
            'awaiting_admin_id', 'awaiting_admin_name', 'awaiting_price'
        ]
        for key in keys_to_clear:
            context.user_data.pop(key, None)
        
        # Delete previous prompt if it exists
        if 'last_prompt_id' in context.user_data:
            try:
                await self.app.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=context.user_data['last_prompt_id']
                )
            except Exception:
                pass
            context.user_data.pop('last_prompt_id', None)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = update.effective_user.id
        admin_name = self.settings['admins'].get(str(user_id), "ناشناس")
        await query.answer()
        
        logging.info(f"Callback received: {query.data} from {admin_name} ({user_id})")
        
        if not self.is_admin(user_id): return
        
        # Always clear states and previous prompts on any callback button click
        # This prevents state conflicts if user clicks a new setting while another is active
        await self._clear_awaiting_states(update, context)

        # Advanced Setting Handlers
        if query.data == "toggle_notif":
            self.settings['notifications'] = not self.settings['notifications']
            self._save_settings()
            await self.menu_settings(update, context)
            return

        if query.data == "change_goal":
            context.user_data['awaiting_goal'] = True
            msg = await query.message.reply_text("🔢 لطفاً مقدار جدید فاصله قیمتی  را به تومان وارد کنید (مثلاً ۱۰۰۰۰۰):")
            context.user_data['last_prompt_id'] = msg.message_id
            return

        if query.data == "change_interval":
            context.user_data['awaiting_interval'] = True
            msg = await query.message.reply_text("⏱ لطفاً زمان جدید بررسی شیت و مانیتورینگ فروشگاه  را به **دقیقه** وارد کنید (مثلاً ۱):")
            context.user_data['last_prompt_id'] = msg.message_id
            return

        if query.data == "change_auto_sync":
            context.user_data['awaiting_auto_sync'] = True
            msg = await query.message.reply_text("📥 لطفاً بازه زمانی آپدیت خودکار از ترب را به **دقیقه** وارد کنید (مثلاً ۶۰ برای ۱ ساعت، ۰ برای غیرفعال):")
            context.user_data['last_prompt_id'] = msg.message_id
            return

        # Admin Management Handlers (Super Admin Only)
        if query.data == "add_admin":
            if not self.is_super_admin(user_id): return
            context.user_data['awaiting_admin_id'] = True
            msg = await query.message.reply_text("🆔 لطفاً ID عددی تلگرام ادمین جدید را وارد کنید:")
            context.user_data['last_prompt_id'] = msg.message_id
            return

        if query.data == "remove_admin":
            if not self.is_super_admin(user_id): return
            keyboard = []
            for uid, name in self.settings['admins'].items():
                if int(uid) != config.SUPER_ADMIN_ID:
                    keyboard.append([InlineKeyboardButton(f"❌ حذف {name}", callback_data=f"deladmin_{uid}")])
            
            if not keyboard:
                await query.message.reply_text("⚠️ ادمین ثانویه‌ای برای حذف وجود ندارد.")
                return
                
            await query.message.reply_text("کدام ادمین حذف شود؟", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        if query.data.startswith("deladmin_"):
            if not self.is_super_admin(user_id): return
            uid_to_del = query.data.split('_')[1]
            name = self.settings['admins'].pop(uid_to_del, "Unknown")
            self._save_settings()
            await query.edit_message_text(f"✅ دسترسی **{name}** لغو شد.")
            return

        # Price Approval Handlers
        # Use rsplit to separate the action (which might have underscores) from the row_index (last part)
        data = query.data.rsplit('_', 1)
        if len(data) < 2: return
        
        action = data[0]
        try:
            row_index = int(data[1])
        except ValueError:
            return
        key = f"{user_id}_{row_index}"
        
        if key not in self.pending_updates:
            await self._recover_pending_update(user_id, row_index)

        if action == "approve":
            update_data = self.pending_updates.get(key)
            if update_data:
                # Add to Queue instead of processing immediately
                logging.info(f"➕ Adding {update_data['name']} to update queue (Suggestion: {update_data['suggestion']})")
                await self.update_queue.put({
                    'row_index': row_index,
                    'new_price': update_data['suggestion'],
                    'product_name': update_data['name'],
                    'shop_product_name': update_data.get('shop_product_name'),
                    'admin_name': admin_name,
                    'user_id': user_id,
                    'old_p': update_data['site_price']
                })
                
                # Immediate feedback to admin
                await query.edit_message_text(
                    f"⏳ **درخواست بروزرسانی محصول {html.escape(update_data['name'])} در صف قرار گرفت.**\nبه محض اعمال، نتیجه اعلام می‌شود.",
                    parse_mode='HTML'
                )

        elif action == "ignore":
            update_data = self.pending_updates.get(key)
            name = update_data['name'] if update_data else "محصول"
            safe_name = html.escape(name)
            final_text = (
                f"🏷 <b>{safe_name}</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"❌ <b>رد شد توسط:</b> {admin_name}\n"
                f"━━━━━━━━━━━━━━\n"
                f"🕒 زمان: {time.strftime('%H:%M:%S')}"
            )
            
            await self._sync_admin_messages(row_index, final_text, user_id)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(executor, self.sheets.update_cell, row_index, config.COL_TELEGRAM_MSG_ID, "")
            self._cleanup_pending_alert(row_index)

        elif action == "custom":
            context.user_data['awaiting_price'] = row_index
            msg = await self.app.bot.send_message(
                chat_id=user_id,
                text=f"🔢 لطفاً قیمت جدید برای <b>{html.escape(self.pending_updates[key]['name'])}</b> را وارد کنید:",
                parse_mode='HTML'
            )
            context.user_data['last_prompt_id'] = msg.message_id

        elif action == "custom_confirm":
            await self._process_custom_confirmation(update, context, row_index, True)
            
        elif action == "custom_cancel":
            await self._process_custom_confirmation(update, context, row_index, False)

        elif action == "custom_edit":
            context.user_data['awaiting_price'] = row_index
            msg = await query.message.reply_text(
                f"✍️ مجدداً قیمت جدید برای **{self.pending_updates[key]['name']}** را وارد کنید:",
                parse_mode='Markdown'
            )
            context.user_data['last_prompt_id'] = msg.message_id

    async def _process_custom_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE, row_index, confirmed):
        user_id = update.effective_user.id
        admin_name = self.settings['admins'].get(str(user_id), "ناشناس")
        query = update.callback_query
        
        # Get data from context
        suggested_price = context.user_data.get('temp_custom_price')
        key = f"{user_id}_{row_index}"
        
        if key not in self.pending_updates:
            await self._recover_pending_update(user_id, row_index)
        
        update_data = self.pending_updates.get(key)
        name = update_data['name'] if update_data else "محصول"

        if not confirmed:
            safe_name = html.escape(name)
            final_text = (
                f"🏷 <b>{safe_name}</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"❌ <b>تغییر دستی توسط {admin_name} لغو شد.</b>"
            )
            await self._sync_admin_messages(row_index, final_text, user_id)
            # Release lock
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(executor, self.sheets.update_cell, row_index, config.COL_TELEGRAM_MSG_ID, "")
            self._cleanup_pending_alert(row_index)
            context.user_data.pop('temp_custom_price', None)
            context.user_data.pop('awaiting_price', None)
            await query.edit_message_text("❌ فرآیند تغییر قیمت دستی متوقف شد.")
            return

        # If confirmed
        if suggested_price:
            # Add to Queue for processing
            logging.info(f"➕ Adding {name} to update queue (Custom: {suggested_price})")
            await self.update_queue.put({
                'row_index': row_index,
                'new_price': suggested_price,
                'product_name': name,
                'shop_product_name': update_data.get('shop_product_name') if update_data else None,
                'admin_name': admin_name,
                'user_id': user_id,
                'old_p': update_data['site_price'] if update_data else 0
            })
            
            # Reset state
            context.user_data.pop('temp_custom_price', None)
            context.user_data.pop('awaiting_price', None)
            
            await query.edit_message_text(f"⏳ **درخواست تغییر دستی به {suggested_price:,} تومان در صف قرار گرفت.**")
        else:
            await query.edit_message_text("❌ خطا: قیمت یافت نشد. لطفاً دوباره تلاش کنید.")

    async def _recover_pending_update(self, user_id, row_index):
        """Recover update data and message map from Google Sheet if memory is lost."""
        try:
            loop = asyncio.get_event_loop()
            records = await loop.run_in_executor(executor, self.sheets.get_all_records)
            if len(records) >= row_index - 1:
                rec = records[row_index - 2]
                t_price = int(rec.get(config.COL_TOROB_PRICE) or 0)
                p_cost = int(rec.get(config.COL_PURCHASE_COST) or 0)
                
                # Recover msg_map from COL_TELEGRAM_MSG_ID
                lock_str = str(rec.get(config.COL_TELEGRAM_MSG_ID, ""))
                msg_map = {}
                if lock_str:
                    for pair in lock_str.split(','):
                        if ':' in pair:
                            cid, mid = pair.split(':')
                            msg_map[cid] = mid
                            
                self.pending_updates[f"{user_id}_{row_index}"] = {
                    'name': rec.get(config.COL_PRODUCT_NAME),
                    'shop_product_name': rec.get(config.COL_SHOP_PRODUCT_NAME),
                    'suggestion': max(t_price - config.PRICE_DIFF_GOAL, p_cost),
                    'row_index': row_index,
                    'msg_map': msg_map
                }
        except Exception as e:
            logging.error(f"Recovery failed for row {row_index}: {e}")

    def _cleanup_pending_alert(self, row_index):
        keys_to_del = [k for k in self.pending_updates.keys() if k.endswith(f"_{row_index}")]
        for k in keys_to_del:
            self.pending_updates.pop(k, None)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        admin_name = self.settings['admins'].get(str(user_id), "ناشناس")
        if not self.is_admin(user_id): return

        # 1. Handle Goal Price Change
        if context.user_data.get('awaiting_goal'):
            try:
                new_goal = int(update.message.text.replace(',', ''))
                self.settings['price_diff'] = new_goal
                config.PRICE_DIFF_GOAL = new_goal
                self._save_settings()
                context.user_data['awaiting_goal'] = False
                await update.message.reply_text(f"✅ استراتژی روی `{new_goal:,}` تومان تنظیم شد.")
                return
            except ValueError:
                await update.message.reply_text("❌ لطفا عدد وارد کنید.")
                return

        # 2. Handle Interval Change
        if context.user_data.get('awaiting_interval'):
            try:
                new_interval = int(update.message.text)
                if new_interval < 1: raise ValueError("Too short")
                self.settings['scan_interval'] = new_interval
                self._save_settings()
                context.user_data['awaiting_interval'] = False
                await update.message.reply_text(f"✅ سرعت مانیتورینگ به هر `{new_interval}` دقیقه تغییر کرد.")
                return
            except ValueError:
                await update.message.reply_text("❌ لطفا عدد معتبر وارد کنید (حداقل ۱ دقیقه).")
                return

        # 2.5 Handle Auto Sync Change
        if context.user_data.get('awaiting_auto_sync'):
            try:
                new_minutes = int(update.message.text)
                self.settings['auto_sync_interval'] = new_minutes
                self._save_settings()
                context.user_data['awaiting_auto_sync'] = False
                status_text = f"✅ آپدیت خودکار ترب روی `{new_minutes}` دقیقه تنظیم شد." if new_minutes > 0 else "✅ آپدیت خودکار ترب غیرفعال شد."
                await update.message.reply_text(status_text)
                return
            except ValueError:
                await update.message.reply_text("❌ لطفا عدد معتبر وارد کنید.")
                return

        # 3. Handle Admin Addition (Step 1: ID)
        if context.user_data.get('awaiting_admin_id'):
            new_uid = update.message.text.strip()
            if not new_uid.isdigit():
                await update.message.reply_text("❌ لطفاً یک ID عددی معتبر وارد کنید.")
                return
            context.user_data['temp_admin_id'] = new_uid
            context.user_data['awaiting_admin_id'] = False
            context.user_data['awaiting_admin_name'] = True
            msg = await update.message.reply_text("✍️ حالا نام این ادمین را وارد کنید:")
            context.user_data['last_prompt_id'] = msg.message_id
            return

        # 4. Handle Admin Addition (Step 2: Name)
        if context.user_data.get('awaiting_admin_name'):
            new_name = update.message.text.strip()
            new_uid = context.user_data['temp_admin_id']
            self.settings['admins'][new_uid] = new_name
            self._save_settings()
            context.user_data['awaiting_admin_name'] = False
            await update.message.reply_text(f"✅ کاربر **{new_name}** به لیست ادمین‌ها اضافه شد.")
            return


        # 5. Handle Custom Product Price Approval
        row_index = context.user_data.get('awaiting_price')
        if row_index:
            try:
                new_price = int(update.message.text.replace(',', ''))
                context.user_data['temp_custom_price'] = new_price
                context.user_data['awaiting_price'] = None # Stop awaiting, move to confirm
                
                key = f"{user_id}_{row_index}"
                if key not in self.pending_updates:
                    await self._recover_pending_update(user_id, row_index)
                
                name = self.pending_updates.get(key, {}).get('name', 'محصول')
                
                keyboard = [
                    [
                        InlineKeyboardButton("✅ تایید", callback_data=f"custom_confirm_{row_index}"),
                        InlineKeyboardButton("❌ حذف فرایند", callback_data=f"custom_cancel_{row_index}")
                    ],
                    [
                        InlineKeyboardButton("✏️ ویرایش عدد", callback_data=f"custom_edit_{row_index}")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    f"❓ آیا از تغییر قیمت محصول **{name}** به عدد زیر اطمینان دارید؟\n\n"
                    f"💰 قیمت پیشنهادی: **{new_price:,} تومان**",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                
            except ValueError:
                await update.message.reply_text("❌ لطفا فقط عدد وارد کنید.")

    def run(self):
        self.app.run_polling()
