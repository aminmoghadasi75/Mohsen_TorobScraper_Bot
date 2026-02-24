import asyncio
import time
from rich.console import Console
from rich.panel import Panel
from concurrent.futures import ThreadPoolExecutor

import config
from utils.sheets_handler import GoogleSheetsHandler
from scrapers.torob_scraper import TorobScraper

console = Console()
executor = ThreadPoolExecutor(max_workers=1)

async def scraping_loop():
    console.print(Panel.fit(
        "[bold magenta]SERVICE: TOROB SCRAPER[/bold magenta]\n"
        "[dim]Running background Torob monitoring...[/dim]",
        border_style="magenta"
    ))

    # Initialize
    sheets = GoogleSheetsHandler(config.CREDENTIALS_FILE, config.SPREADSHEET_ID)
    sheets.ensure_headers(config.SHEET_COLUMNS)
    scraper = TorobScraper()

    # Shop URL to monitor
    SHOP_URL = "https://torob.com/shop/116426/%DA%AF%D8%B1%DB%8C%D8%B4-%D9%85%D8%A7%D9%84/%D9%85%D8%AD%D8%B5%D9%88%D9%84%D8%A7%D8%AA/"

    try:
        while True:
            console.print(f"\n[bold cyan]Cycle Started:[/bold cyan] {time.strftime('%H:%M:%S')}")
            
            # 1. Scrape Shop Page
            console.print(f"Scraping shop page: [blue]{SHOP_URL}[/blue]")
            loop = asyncio.get_running_loop()
            shop_products = await loop.run_in_executor(executor, scraper.get_shop_products, SHOP_URL)
            
            if not shop_products:
                console.print("[yellow]No products found or error scraping. Waiting 60s...[/yellow]")
                await asyncio.sleep(60)
                continue

            console.print(f"Found [green]{len(shop_products)}[/green] products on Torob.")

            # 2. Get Current Sheet Data
            records = sheets.get_all_records()
            
            sheet_map = {}
            for i, r in enumerate(records):
                name = r.get(config.COL_PRODUCT_NAME)
                if name:
                    sheet_map[name] = i + 2

            scraped_map = {p['name']: p for p in shop_products}
            
            scraped_names = set(scraped_map.keys())
            sheet_names = set(sheet_map.keys())

            # 3. Analyze Differences
            # Note: We prioritize Torob shop page as the source of truth for products
            to_update = scraped_names.intersection(sheet_names)
            to_add = scraped_names - sheet_names
            to_delete = sheet_names - scraped_names

            console.print(f"Sync Status: {len(to_update)} Update, {len(to_add)} Add, {len(to_delete)} Delete")

            # 4. Perform Updates
            for name in to_update:
                row_idx = sheet_map[name]
                p = scraped_map[name]
                current_record = records[row_idx - 2]
                
                updates = {
                    config.COL_PRODUCT_NAME: name,
                    config.COL_SHOP_PRODUCT_NAME: p.get('shop_product_name', ""),
                    config.COL_PURCHASE_COST: current_record.get(config.COL_PURCHASE_COST, ""),
                    config.COL_SITE_PRICE: p.get('shop_site_price', ""), # Populating from shop page
                    config.COL_TOROB_PRICE: p['price'],
                    config.COL_SHOP_NAME: p['shop_name'],
                    config.COL_PRODUCT_URL: sheets.format_hyperlink(p['product_url'], "لینک محصول"),
                    config.COL_IMAGE_URL: sheets.format_hyperlink(p['image_url'], "عکس محصول"),
                    config.COL_TELEGRAM_MSG_ID: current_record.get(config.COL_TELEGRAM_MSG_ID, ""),
                    config.COL_SECOND_TOROB_PRICE: p.get('second_price', ""),
                    config.COL_SECOND_SHOP_NAME: p.get('second_shop_name', "")
                }
                sheets.update_row(row_idx, updates)
                console.print(f"  [green]✔ Updated:[/green] {name} (Torob: {p['price']} T | Site: {p.get('shop_site_price')} T)")
                await asyncio.sleep(0.5)

            # 5. Perform Deletions (Bottom-to-Top to keep indices valid)
            if config.TEST_MODE_LIMIT is None:
                sorted_delete_indices = sorted([sheet_map[name] for name in to_delete], reverse=True)
                for row_idx in sorted_delete_indices:
                    sheets.delete_row(row_idx)
                    console.print(f"  [red]✘ Deleted:[/red] Row {row_idx}")
                    await asyncio.sleep(0.5)
            else:
                console.print(f"[yellow]⚠ Test Mode Active (Limit {config.TEST_MODE_LIMIT}): Skipping {len(to_delete)} deletions.[/yellow]")

            # 6. Perform Adds (Calculate row index manually to prevent overwriting)
            next_row_idx = len(records) + 2 # Header is 1, so data starts at 2. If records is 0, start at 2.
            
            for name in to_add:
                p = scraped_map[name]
                new_record = {
                    config.COL_PRODUCT_NAME: name,
                    config.COL_SHOP_PRODUCT_NAME: p.get('shop_product_name', ""),
                    config.COL_PURCHASE_COST: "", 
                    config.COL_SITE_PRICE: p.get('shop_site_price', ""),
                    config.COL_TOROB_PRICE: p['price'],
                    config.COL_SHOP_NAME: p['shop_name'],
                    config.COL_PRODUCT_URL: sheets.format_hyperlink(p['product_url'], "لینک محصول"),
                    config.COL_IMAGE_URL: sheets.format_hyperlink(p['image_url'], "عکس محصول"),
                    config.COL_TELEGRAM_MSG_ID: "",
                    config.COL_SECOND_TOROB_PRICE: p.get('second_price', ""),
                    config.COL_SECOND_SHOP_NAME: p.get('second_shop_name', "")
                }
                
                # Use update_row with a specific index instead of append_row
                sheets.update_row(next_row_idx, new_record)
                console.print(f"  [blue]+ Added to Row {next_row_idx}:[/blue] {name} (Site: {p.get('shop_site_price')} T)")
                
                next_row_idx += 1
                await asyncio.sleep(0.5)

            console.print("[dim]Cycle finished. Sleep 2 minutes...[/dim]")
            await asyncio.sleep(120)

    except KeyboardInterrupt:
        console.print("[red]Scraper service stopped.[/red]")
    except Exception as e:
        console.print(f"[bold red]Unexpected Error:[/bold red] {e}")
    finally:
        scraper.close()

if __name__ == "__main__":
    asyncio.run(scraping_loop())
