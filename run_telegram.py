import asyncio
import time
from rich.console import Console
from rich.panel import Panel

import config
from utils.sheets_handler import GoogleSheetsHandler
from utils.telegram_bot import PriceUpdateBot
from scrapers.torob_scraper import TorobScraper # needed just for the Class type if needed

from concurrent.futures import ThreadPoolExecutor
from telegram.request import HTTPXRequest

console = Console()
executor = ThreadPoolExecutor(max_workers=1)

async def monitoring_job(bot_instance):
    """
    Checks the sheet for differences between Torob price and Site price.
    Uses the centralized logic in PriceUpdateBot.
    """
    while True:
        try:
            console.print("[dim]Starting scheduled market scan...[/dim]")
            await bot_instance.run_monitoring_scan()
            
            # Dynamic sleep based on settings (interval is in minutes)
            interval = bot_instance.settings.get('scan_interval', 1)
            sleep_time = max(15, interval * 60)
            await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            break
        except Exception as e:
            console.print(f"[red]Error in monitoring_job: {e}[/red]")
            await asyncio.sleep(10)

async def main():
    console.print(Panel.fit(
        "[bold green]SERVICE: TELEGRAM BOT & MONITOR[/bold green]\n"
        "[dim]Handling admin alerts and site updates...[/dim]",
        border_style="green"
    ))

    # Initialize
    sheets = GoogleSheetsHandler(config.CREDENTIALS_FILE, config.SPREADSHEET_ID)
    
    # Ensure all required columns exist (Async-safe)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(executor, sheets.ensure_headers, config.SHEET_COLUMNS)
    
    bot = PriceUpdateBot(config.TELEGRAM_BOT_TOKEN, sheets, None)

    # Start Background Task
    monitor_task = asyncio.create_task(monitoring_job(bot))

    try:
        # Manual Lifecycle Management
        await bot.app.initialize()
        await bot.app.start()
        await bot.app.updater.start_polling()
        
        console.print("[bold blue]Bot is now active and polling...[/bold blue]")
        
        while True:
            await asyncio.sleep(1)
            
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("[yellow]Initiating graceful shutdown...[/yellow]")
    except Exception as e:
        console.print(f"[bold red]Critical Error in main loop: {e}[/bold red]")
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
            
        if bot.app.updater and bot.app.updater.running:
            await bot.app.updater.stop()
        if bot.app.running:
            await bot.app.stop()
        await bot.app.shutdown()
        
        console.print("[red]Bot service stopped successfully.[/red]")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
