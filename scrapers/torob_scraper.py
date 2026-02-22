import re
import time
import urllib.parse
import tldextract
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
from selenium_stealth import stealth
from concurrent.futures import ThreadPoolExecutor
import threading
import random

from scrapers.base_scraper import BaseScraper
import config
import pickle
import os

class TorobScraper(BaseScraper):
    def __init__(self, captcha_callback=None):
        self.captcha_callback = captcha_callback
        self.driver = self._init_driver()
        self.wait = WebDriverWait(self.driver, config.WAIT_TIMEOUT)
        self.cookies_file = "torob_cookies.pkl"
        self._load_cookies()

    def _save_cookies(self):
        try:
            pickle.dump(self.driver.get_cookies(), open(self.cookies_file, "wb"))
            print("🍪 Cookies saved successfully.")
        except Exception as e:
            print(f"⚠️ Could not save cookies: {e}")

    def _load_cookies(self):
        if not os.path.exists(self.cookies_file):
            return
        
        try:
            # Domain must match before adding cookies
            if "torob.com" not in self.driver.current_url:
                self.driver.get("https://torob.com")
            
            cookies = pickle.load(open(self.cookies_file, "rb"))
            for cookie in cookies:
                try:
                    self.driver.add_cookie(cookie)
                except: pass
            print("🍪 Cookies loaded.")
            # Skip refresh if possible, or do it quickly
            self.driver.refresh()
        except Exception as e:
            print(f"⚠️ Could not load cookies: {e}")

    def _init_driver(self, headless=None):
        chrome_options = Options()
        chrome_options.page_load_strategy = config.PAGE_LOAD_STRATEGY
        
        # Use provided headless arg, or fallback to config
        is_headless = headless if headless is not None else config.HEADLESS
        if is_headless:
            chrome_options.add_argument("--headless=new") # Using newer headless mode
            
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-notifications")
        chrome_options.add_argument("--mute-audio")
        chrome_options.add_argument("--start-maximized")
        
        # Disable images and heavy features
        prefs = {
            "profile.managed_default_content_settings.images": 2, # 2 = Block images
            "profile.default_content_setting_values.notifications": 2,
            "profile.managed_default_content_settings.stylesheets": 2, # Optional: Block CSS? User asked for "heavy CSS", blocking all CSS might break some JS-heavy sites though.
            "profile.default_content_setting_values.geolocation": 2,
        }
        # Human-like User-Agents list
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"
        ]
        chrome_options.add_argument(f"user-agent={random.choice(user_agents)}")
        
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        # Resource blocking via CDP (more aggressive for site speed)
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": [
            "*.png", "*.jpg", "*.jpeg", "*.gif", "*.woff", "*.woff2", "*.ttf", "*.svg",
            "facebook.net", "google-analytics.com", "googletagmanager.com"
        ]})
        driver.execute_cdp_cmd("Network.enable", {})

        stealth(driver,
                languages=["fa-IR", "fa", "en-US", "en"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True,
                run_on_insecure_origins=False
        )
        return driver

    def restart_driver(self, headless=True):
        print(f"🔄 Restarting driver (Headless={headless})...")
        if self.driver:
            current_cookies = self.driver.get_cookies() # Try to keep cookies in memory too
            try:
                self.driver.quit()
            except: pass
        
        self.driver = self._init_driver(headless=headless)
        self.wait = WebDriverWait(self.driver, config.WAIT_TIMEOUT)
        
        # Restore cookies if we have them
        self.driver.get("https://torob.com") # Domain must match
        try:
            for c in current_cookies:
                self.driver.add_cookie(c)
        except: pass
        self._load_cookies() # Load from file as backup

    def normalize_digits(self, text):
        mapping = {
            '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4',
            '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9',
            '٫': '', ',': ''
        }
        for k, v in mapping.items():
            text = text.replace(k, v)
        return text

    def search_product(self, product_name, retries=3):
        for attempt in range(retries):
            try:
                query = urllib.parse.quote(product_name)
                url = f"https://torob.com/search/?query={query}&available=true&stock_status=new"
                self.driver.get(url)

                # Wait for results or captcha
                try:
                    self.wait.until(
                        lambda d: "captcha" in d.current_url or "ربات" in d.page_source or d.find_elements(By.CSS_SELECTOR, "h2")
                    )
                except: pass

                if "captcha" in self.driver.current_url or "ربات" in self.driver.page_source:
                    self._handle_captcha(url)

                # Find organic products
                self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "h2")))
                cards = self.driver.find_elements(By.CSS_SELECTOR, "div[class*='ProductCard_desktop_card__'], div[class*='ProductCard_container__']")
                if not cards:
                    cards = self.driver.find_elements(By.XPATH, "//div[.//h2]")
                
                target_card = None
                for card in cards:
                    if "آگهی" not in card.text:
                        target_card = card.find_element(By.CSS_SELECTOR, "h2")
                        break
                
                if not target_card:
                    if attempt < retries - 1:
                        print(f"⚠️ Project {product_name} not found, retrying... ({attempt+1}/{retries})")
                        time.sleep(5)
                        continue
                    return None
                
                # If we reached here, we found the product
                break 
            except Exception as e:
                if attempt < retries - 1:
                    print(f"⚠️ Error searching {product_name}, retrying... ({attempt+1}/{retries}): {e}")
                    time.sleep(5)
                else:
                    return None
        
        try:

            self.driver.execute_script("arguments[0].click();", target_card)
            
            # Extract Image
            image_url = None
            img_selectors = ["[class*='imageGallery_singleImageContainer'] img", "[class*='Showcase_gallery'] img", "div[class*='ProductPage'] picture img"]
            for sel in img_selectors:
                try:
                    img_el = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                    image_url = img_el.get_attribute("src")
                    if image_url: break
                except: continue

            # Handle "All Iran" filter for best price
            try:
                iran_badge = self.driver.find_element(By.XPATH, "//*[contains(text(), 'تمام ایران')]/ancestor::div[contains(@class, 'FilterButton_filterBadge')]")
                self.driver.execute_script("arguments[0].click();", iran_badge)
                time.sleep(1)
            except: pass

            # --- Seller Extraction with "Show More" handling ---
            def extract_sellers_data():
                found_sellers = []
                local_shop_name = None
                
                cards_list = self.driver.find_elements(By.CSS_SELECTOR, ".shop-card.seller-element") or \
                             self.driver.find_elements(By.XPATH, "//div[contains(@class, 'shop-card')]")
                
                for card in cards_list:
                    try:
                        if "آگهی" in card.text: continue
                        s_name = card.find_element(By.CSS_SELECTOR, ".shop-name, [class*='shop-name']").text.strip()
                        
                        # Extract shop-specific product name if this is our shop
                        if s_name == config.MY_SHOP_NAME:
                            try:
                                p_name_el = card.find_element(By.CSS_SELECTOR, ".product-name, [class*='product-name']")
                                local_shop_name = p_name_el.text.strip()
                            except: pass

                        p_txt = card.find_element(By.CSS_SELECTOR, ".price, [class*='price']").text
                        p_val = int(re.search(r'(\d+)', self.normalize_digits(p_txt)).group(1))
                        try:
                            b_btn = card.find_element(By.XPATH, ".//a[contains(@href, 'redirect')]")
                        except:
                            b_btn = card.find_element(By.CSS_SELECTOR, "a[href*='redirect']")

                        found_sellers.append({'name': s_name, 'price': p_val, 'btn': b_btn})
                    except: continue
                return found_sellers, local_shop_name

            # Initial extraction
            sellers, shop_product_name = extract_sellers_data()

            # If shop name not found or we need to ensure we see everyone, click "Show More"
            try:
                # Scroll to bottom to trigger button visibility
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1)
                
                show_more = self.driver.find_elements(By.CSS_SELECTOR, ".show-more-btn, [class*='show-more-btn']")
                if show_more:
                    print(f"🔗 Clicking 'Show More Sellers' to find {config.MY_SHOP_NAME}...")
                    self.driver.execute_script("arguments[0].click();", show_more[0])
                    time.sleep(2)
                    # Re-extract if we clicked
                    sellers, shop_product_name = extract_sellers_data()
            except Exception as e:
                print(f"⚠️ Note: Could not expand sellers list: {e}")

            if not sellers:
                return None

            # Sort by price
            sellers.sort(key=lambda x: x['price'])
            cheapest = sellers[0]
            second_cheapest = sellers[1] if len(sellers) > 1 else None
            
            # Skip the slow follow_redirect for speed
            # final_url = self._follow_redirect(cheapest['btn'])
            # second_final_url = self._follow_redirect(second_cheapest['btn']) if second_cheapest else None
            # Just use the redirect URL directly or torob link
            final_url = cheapest['btn'].get_attribute("href") if cheapest.get('btn') else ""
            second_final_url = second_cheapest['btn'].get_attribute("href") if second_cheapest and second_cheapest.get('btn') else ""

            return {
                'price': cheapest['price'],
                'shop_name': cheapest['name'],
                'shop_product_name': shop_product_name,
                'second_price': second_cheapest['price'] if second_cheapest else None,
                'second_shop_name': second_cheapest['name'] if second_cheapest else None,
                'product_url': final_url,
                'second_product_url': second_final_url,
                'image_url': image_url
            }

        except Exception as e:
            return None



    def _follow_redirect(self, btn_element):
        main_window = self.driver.current_window_handle
        redirect_url = btn_element.get_attribute("href")
        
        if redirect_url:
            self.driver.execute_script(f"window.open('{redirect_url}', '_blank');")
        else:
            self.driver.execute_script("arguments[0].click();", btn_element)
        
        final_url = None
        start_time = time.time()
        while time.time() - start_time < config.REDIRECT_TIMEOUT:
            if len(self.driver.window_handles) > 1:
                for handle in self.driver.window_handles:
                    if handle != main_window:
                        self.driver.switch_to.window(handle)
                        break
            
            curr = self.driver.current_url
            if curr and "torob.com" not in curr and curr != "about:blank":
                final_url = curr
                break
            time.sleep(0.5)
        
        # Close the new window and switch back
        if len(self.driver.window_handles) > 1:
            self.driver.close()
            self.driver.switch_to.window(main_window)
        
        return final_url or self.driver.current_url

    def _handle_captcha(self, target_url=None):
        try:
            # Refined detection to avoid false positives
            curr_url = self.driver.current_url
            page_title = self.driver.title
            has_checkbox = len(self.driver.find_elements(By.ID, "input-checkbox")) > 0
            
            is_captcha_page = (
                "/captcha/" in curr_url or 
                "آیا شما یک ربات هستید" in page_title or
                (has_checkbox and "ربات" in self.driver.page_source)
            )

            if is_captcha_page:
                print(f"⚠️ CAPTCHA detected! Reason: {'URL' if '/captcha/' in curr_url else 'Title' if 'آیا شما یک ربات هستید' in page_title else 'Checkbox found'}")
                
                # Double check: if some products are already visible, it might be a false positive
                if len(self.driver.find_elements(By.CSS_SELECTOR, "a[href^='/p/'], h2")) > 5:
                    print("🔍 False positive detected (found products on page). Skipping captcha handler.")
                    return

                
                # IF HEADLESS: Open Visible Window for Manual Solve
                # We assume if the window is headless, we can't solve it.
                # Since we don't store internal headless state easily, we check if we need to switch.
                # Assuming config.HEADLESS is the default intent.
                
                # Check directly if we are likely headless (running in background)
                # Actually, let's just force a switch to Visible if we are here and config says we started Headless.
                if config.HEADLESS: 
                    print("🤖 Headless mode detected. Switching to VISIBLE mode for manual solution...")
                    self.restart_driver(headless=False)
                    if target_url:
                        self.driver.get(target_url)
                        time.sleep(3)
                
                # 1. Try to click the checkbox if present
                try:
                    checkbox = self.driver.find_element(By.ID, "input-checkbox")
                    self.driver.execute_script("arguments[0].click();", checkbox)
                    print("✅ Clicked 'I am not a robot' checkbox.")
                    time.sleep(2)
                except: 
                    pass # Checkbox might not be present or already clicked

                # 2. Check for Puzzle (Slider) or persistent captcha
                # Re-check page source after potential checkbox click
                if "با کشیدن فلش" in self.driver.page_source or "puzzle" in self.driver.page_source or "captcha" in self.driver.current_url:
                    if self.captcha_callback:
                        self.captcha_callback()

                    print("\n" + "="*50)
                    print("🧩 PUZZLE DETECTED! SCRIPT PAUSED.")
                    print("👉 Please SOLVE THE PUZZLE MANUALLY in the browser window.")
                    print("👉 The script will resume automatically once you pass the captcha.")
                    print("="*50 + "\n")
                    
                    # Wait loop until we are redirected away from captcha page
                    max_wait = 300 # 5 minutes max wait
                    start_time = time.time()
                    
                    while True:
                        if time.time() - start_time > max_wait:
                            print("❌ Timed out waiting for manual captcha solution.")
                            break
                            
                        # Check if we are out of the captcha page
                        curr_url = self.driver.current_url
                        page_src = self.driver.page_source
                        
                        if "captcha" not in curr_url and "ربات" not in page_src and "با کشیدن فلش" not in page_src:
                            print("✅ Captcha solved! Resuming...")
                            time.sleep(3) # Let the new page load fully
                            self._save_cookies() # Save cookies to avoid this next time
                            
                            # Switch BACK to Headless if configured
                            if config.HEADLESS:
                                print("🙈 Switching back to HEADLESS mode...")
                                self.restart_driver(headless=True)
                                if target_url:
                                    self.driver.get(target_url)
                            break
                        
                        time.sleep(1)
                else:
                    # just a simple checkbox, wait a bit
                    print("✅ Probably solved via checkbox.")
                    time.sleep(3)
                    self._save_cookies()
                    # If we switched to visible, switch back
                    if config.HEADLESS:
                        self.restart_driver(headless=True)
                        if target_url:
                            self.driver.get(target_url)

        except Exception as e:
            print(f"⚠️ Error in captcha handler: {e}")

    def close(self):
        if self.driver:
            self.driver.quit()

    def get_shop_products(self, shop_url, progress_callback=None, captcha_callback=None):
        """
        Scrapes all products with a callback for progress updates.
        Ensures all products (e.g., 39) are loaded via infinite scroll.
        """
        if captcha_callback:
            self.captcha_callback = captcha_callback
            
        try:
            self.driver.get(shop_url)
            time.sleep(3)
            self._handle_captcha(shop_url)

            # 🛠 Precise Scrolling Logic (Targeting product container)
            max_scroll_retries = 5
            scroll_retry = 0
            
            while True:
                cards = self.driver.find_elements(By.CSS_SELECTOR, "a[href^='/p/']")
                previous_count = len(cards)
                print(f"📊 Current product count: {previous_count}")
                
                if previous_count > 0:
                    # Scroll the LAST product card into view to trigger lazy loading
                    try:
                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", cards[-1])
                        time.sleep(1.5)
                        # Also scroll slightly more to ensure trigger
                        self.driver.execute_script("window.scrollBy(0, 300);")
                        time.sleep(1)
                    except:
                        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                else:
                    # Initial load might be slow or blocked
                    print(f"⚠️ No products found yet. Scroll retry {scroll_retry+1}/{max_scroll_retries}...")
                    self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(3)
                    scroll_retry += 1
                    if scroll_retry < max_scroll_retries:
                        continue
                    else:
                        # Check if it's a captcha
                        if "captcha" in self.driver.current_url or "ربات" in self.driver.page_source:
                            print("❌ Stuck on CAPTCHA page. Cannot find products.")
                        break
                
                # Check if new products loaded
                current_count = len(self.driver.find_elements(By.CSS_SELECTOR, "a[href^='/p/']"))
                print(f"📊 New product count: {current_count}")
                
                if current_count > previous_count:
                    print(f"🔄 {current_count - previous_count} new products loaded. Continuing scroll...")
                    scroll_retry = 0 # Reset retry if we made progress
                    continue
                else:
                    # Try one last deep scroll just in case
                    self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(2)
                    final_count = len(self.driver.find_elements(By.CSS_SELECTOR, "a[href^='/p/']"))
                    if final_count > current_count:
                        continue
                    print("🏁 All products loaded.")
                    break
            all_cards = self.driver.find_elements(By.CSS_SELECTOR, "a[href^='/p/']")
            product_links = []
            seen_urls = set()
            for card in all_cards:
                try:
                    url = card.get_attribute("href")
                    if not url or url in seen_urls: continue
                    seen_urls.add(url)
                    title = card.find_element(By.TAG_NAME, "h2").text.strip()
                    try:
                        price_els = card.find_elements(By.XPATH, ".//*[contains(text(), 'تومان')]")
                        shop_price = int(re.search(r'(\d+)', self.normalize_digits(price_els[-1].text)).group(1)) if price_els else 0
                    except: shop_price = 0
                    product_links.append({'name': title, 'url': url, 'shop_site_price': shop_price})
                except: continue

            if not product_links:
                print("❌ No product links extracted. Scraper possibly blocked.")
                return None

            # --- PARALLEL PROCESSING ---
            results = []
            total = len(product_links)
            completed_count = 0
            count_lock = threading.Lock()

            def worker_task(chunk):
                nonlocal completed_count
                worker_scraper = TorobScraper(captcha_callback=self.captcha_callback)
                worker_results = []
                try:
                    for item in chunk:
                        res = worker_scraper.scrape_single_product_details(item)
                        if res:
                            worker_results.append(res)
                        
                        with count_lock:
                            completed_count += 1
                            if progress_callback:
                                progress_callback(completed_count, total, item['name'])
                finally:
                    worker_scraper.close()
                return worker_results

            # Split tasks into chunks
            num_workers = min(config.SCRAPER_WORKERS, total) if total > 0 else 1
            chunks = [product_links[i::num_workers] for i in range(num_workers)]
            
            print(f"🚀 Starting parallel scrape for {total} products with {num_workers} workers...")
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(worker_task, chunk) for chunk in chunks]
                for future in futures:
                    results.extend(future.result())

            return results
        except Exception as e:
            print(f"Error extracting shop products: {e}")
            return None

    def scrape_single_product_details(self, item, retries=3):
        """Scrapes details for a single product. Extracted for parallel use."""
        attempts = 0
        while attempts < retries:
            try:
                # 🛡 Human-like Jitter
                time.sleep(random.uniform(0.5, 2.5))
                
                self.driver.get(item['url'])
                
                if "captcha" in self.driver.current_url or "ربات" in self.driver.page_source:
                    self._handle_captcha(item['url'])
                    
                try: 
                    self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "h1, h2")))
                except: 
                    pass

                # Image Extraction
                image_url = None
                img_selectors = [
                    "[class*='imageGallery_singleImageContainer'] img", 
                    "[class*='Showcase_gallery'] img", 
                    "div[class*='ProductPage'] picture img"
                ]
                for sel in img_selectors:
                    try:
                        img_el = self.driver.find_element(By.CSS_SELECTOR, sel)
                        image_url = img_el.get_attribute("src")
                        if image_url: break
                    except: continue

                # All Iran filter
                try:
                    badge = self.driver.find_element(By.XPATH, "//*[contains(text(), 'تمام ایران')]/ancestor::*[contains(@class, 'FilterButton_filterBadge')]")
                    self.driver.execute_script("arguments[0].click();", badge)
                    time.sleep(1)
                except: pass

                # --- Seller Extraction with "Show More" handling ---
                def extract_sellers_data():
                    found_sellers = []
                    local_shop_name = None
                    
                    cards_list = self.driver.find_elements(By.CSS_SELECTOR, ".shop-card.seller-element") or \
                                 self.driver.find_elements(By.XPATH, "//div[contains(@class, 'shop-card')]")
                    
                    for card in cards_list:
                        try:
                            if "آگهی" in card.text: continue
                            s_name = card.find_element(By.CSS_SELECTOR, ".shop-name, [class*='shop-name']").text.strip()
                            
                            # Extract shop-specific product name if this is our shop
                            if s_name == config.MY_SHOP_NAME:
                                try:
                                    p_name_el = card.find_element(By.CSS_SELECTOR, ".product-name, [class*='product-name']")
                                    local_shop_name = p_name_el.text.strip()
                                except: pass

                            p_txt = card.find_element(By.CSS_SELECTOR, ".price, [class*='price']").text
                            p_val = int(re.search(r'(\d+)', self.normalize_digits(p_txt)).group(1))
                            try: 
                                b_btn = card.find_element(By.XPATH, ".//a[contains(@href, 'redirect')]")
                            except: 
                                b_btn = card.find_element(By.CSS_SELECTOR, "a[href*='redirect']")
                            found_sellers.append({'name': s_name, 'price': p_val, 'btn': b_btn})
                        except: continue
                    return found_sellers, local_shop_name

                # Initial extraction
                sellers, shop_product_name = extract_sellers_data()

                # If shop name not found or we need to ensure we see everyone, click "Show More"
                try:
                    # Scroll to bottom to trigger button visibility
                    self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(1)
                    
                    show_more = self.driver.find_elements(By.CSS_SELECTOR, ".show-more-btn, [class*='show-more-btn']")
                    if show_more:
                        print(f"🔗 Clicking 'Show More Sellers' to find {config.MY_SHOP_NAME}...")
                        self.driver.execute_script("arguments[0].click();", show_more[0])
                        time.sleep(2)
                        # Re-extract
                        sellers, shop_product_name = extract_sellers_data()
                except Exception as e:
                    print(f"⚠️ Note: Could not expand sellers list: {e}")

                if sellers:
                    sellers.sort(key=lambda x: x['price'])
                    cheapest = sellers[0]
                    second_cheapest = sellers[1] if len(sellers) > 1 else None
                    
                    # Optimized: Skip the highly slow follow_redirect to significantly speed up processing
                    # final_url = self._follow_redirect(cheapest['btn'])
                    # second_final_url = self._follow_redirect(second_cheapest['btn']) if second_cheapest else None
                    final_url = cheapest['btn'].get_attribute("href") if cheapest.get('btn') else ""
                    second_final_url = second_cheapest['btn'].get_attribute("href") if second_cheapest and second_cheapest.get('btn') else ""

                    return {
                        'name': item['name'], 
                        'shop_product_name': shop_product_name,
                        'shop_site_price': item['shop_site_price'],
                        'price': cheapest['price'], 
                        'shop_name': cheapest['name'],
                        'second_price': second_cheapest['price'] if second_cheapest else None,
                        'second_shop_name': second_cheapest['name'] if second_cheapest else None,
                        'product_url': final_url, 
                        'second_product_url': second_final_url,
                        'image_url': image_url
                    }
                return None
            except (WebDriverException, Exception) as de:
                attempts += 1
                err_msg = str(de).lower()
                if any(x in err_msg for x in ["connection", "refused", "target", "window"]):
                    self.restart_driver(headless=config.HEADLESS)
                if attempts >= retries:
                    return None
                time.sleep(2)
        return None
