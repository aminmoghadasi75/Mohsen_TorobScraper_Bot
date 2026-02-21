# Torob & Gerishmall Price Manager Bot

یک ربات تلگرام پیشرفته برای مانیتورینگ خودکار قیمت‌های محصولات شرکت در سایت **ترب** و بروزرسانی هوشمند قیمت‌ها در سایت **گریش‌مال** بر اساس استراتژی رقابتی.

## قابلیت‌های کلیدی

- **مانیتورینگ هوشمند:** اسکن مداوم قیمت رقبا در ترب و مقایسه با قیمت سایت.
- **بروزرسانی ردیفی (Queue):** مدیریت درخواست‌ها در صف برای جلوگیری از فشار به سرور.
- **Audit Log:** ثبت دقیق تمامی تغییرات قیمت توسط ادمین‌ها با تاریخ شمسی.
- **بهینه‌سازی منابع:** استفاده از حالت Headless و مسدودسازی منابع سنگین برای اجرای سریع روی سرور.
- **سیستم Fallback:** استفاده از WooCommerce API برای سرعت بالا و سلنیوم به عنوان پشتیبان.

## نصب و راه‌اندازی

۱. **نصب پیشنیازها:**

   ```bash
   pip install -r requirements.txt
   ```

۲. **تنظیمات:**

- فایل `config.py.example` را به `config.py` تغییر نام دهید.
- اطلاعات Token ربات تلگرام و API Keyهای ووکامرس را جایگزین کنید.
- فایل `credentials.json` مربوط به Google Service Account را در پوشه اصلی قرار دهید.

۳. **اجرا:**

   ```bash
   python run_telegram.py
   ```

## ساختار پروژه

- `run_telegram.py`: فایل اصلی اجرای ربات و مانیتورینگ.
- `utils/telegram_bot.py`: منطق اصلی ربات و تسک واکر.
- `utils/sheets_handler.py`: تعامل با گوگل‌شیت.
- `utils/woocom_handler.py`: تعامل با API ووکامرس.
- `scrapers/`: شامل کدهای اسکرپر ترب و سایت اصلی.

## استک تکنولوژی

- Python 3.10+
- Selenium (Optimized Headless)
- python-telegram-bot
- Google Sheets API
- WooCommerce REST API
