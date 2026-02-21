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

from scrapers.base_scraper import BaseScraper
import config
import pickle
import os

class TorobScraper(BaseScraper):
    def __init__(self):
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
        chrome_options.add_experimental_option("prefs", prefs)
        
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

            # Extract Sellers
            sellers = []
            seller_cards = self.driver.find_elements(By.CSS_SELECTOR, ".shop-card.seller-element")
            if not seller_cards:
                seller_cards = self.driver.find_elements(By.XPATH, "//div[contains(@class, 'shop-card')]")

            for card in seller_cards:
                try:
                    if "آگهی" in card.text: continue
                    name = card.find_element(By.CSS_SELECTOR, ".shop-name, [class*='shop-name']").text.strip()
                    price_txt = card.find_element(By.CSS_SELECTOR, ".price, [class*='price']").text
                    price_val = int(re.search(r'(\d+)', self.normalize_digits(price_txt)).group(1))
                    try:
                        buy_btn = card.find_element(By.XPATH, ".//a[contains(@href, 'redirect')]")
                    except:
                        buy_btn = card.find_element(By.CSS_SELECTOR, "a[href*='redirect']")

                    sellers.append({'name': name, 'price': price_val, 'btn': buy_btn})
                except: continue

            if not sellers:
                return None

            # Sort by price
            sellers.sort(key=lambda x: x['price'])
            cheapest = sellers[0]
            second_cheapest = sellers[1] if len(sellers) > 1 else None
            
            # Follow redirects for top 2 competitors
            final_url = self._follow_redirect(cheapest['btn'])
            second_final_url = self._follow_redirect(second_cheapest['btn']) if second_cheapest else None

            return {
                'price': cheapest['price'],
                'shop_name': cheapest['name'],
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

    def get_shop_products(self, shop_url, progress_callback=None):
        """
        Scrapes all products with a callback for progress updates.
        Ensures all products (e.g., 39) are loaded via infinite scroll.
        """
        try:
            self.driver.get(shop_url)
            time.sleep(3)
            self._handle_captcha(shop_url)

            # 🛠 Enhanced Robust Scrolling for Infinite Load
            # 🛠 Step-Scrolling Logic for Infinite Load
            for pass_idx in range(3): # Scroll to end 3 times as requested
                print(f"🔄 Starting scroll pass {pass_idx + 1}...")
                last_height = self.driver.execute_script("return document.body.scrollHeight")
                current_count = len(self.driver.find_elements(By.CSS_SELECTOR, "a[href^='/p/']"))
                
                # Step scroll down to trigger observers
                current_pos = 0
                while True:
                    current_pos += 900
                    self.driver.execute_script(f"window.scrollTo(0, {current_pos});")
                    time.sleep(0.6)
                    new_height = self.driver.execute_script("return document.body.scrollHeight")
                    if current_pos >= new_height:
                        break
                
                # Final jump to bottom and wait for render
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(4)
                
                current_count = len(self.driver.find_elements(By.CSS_SELECTOR, "a[href^='/p/']"))
                print(f"📊 Scroll pass {pass_idx + 1}: Found {current_count} products.")
                
                if current_count >= 39: break # Early exit if all 39 found
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

            results = []
            total = len(product_links)
            
            for idx, item in enumerate(product_links):
                attempts = 0
                while attempts < 3:
                    try:
                        if progress_callback:
                            progress_callback(idx + 1, total, item['name'])
                        
                        self.driver.get(item['url'])
                        try: self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "h1, h2")))
                        except: pass

                        # Image Extraction
                        image_url = None
                        for sel in ["[class*='imageGallery_singleImageContainer'] img", "[class*='Showcase_gallery'] img", "div[class*='ProductPage'] picture img"]:
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

                        # Sellers Extraction
                        sellers = []
                        seller_cards = self.driver.find_elements(By.CSS_SELECTOR, ".shop-card.seller-element") or self.driver.find_elements(By.XPATH, "//div[contains(@class, 'shop-card')]")
                        for card in seller_cards:
                            try:
                                if "آگهی" in card.text: continue
                                name = card.find_element(By.CSS_SELECTOR, ".shop-name, [class*='shop-name']").text.strip()
                                price_txt = card.find_element(By.CSS_SELECTOR, ".price, [class*='price']").text
                                price_val = int(re.search(r'(\d+)', self.normalize_digits(price_txt)).group(1))
                                try: buy_btn = card.find_element(By.XPATH, ".//a[contains(@href, 'redirect')]")
                                except: buy_btn = card.find_element(By.CSS_SELECTOR, "a[href*='redirect']")
                                sellers.append({'name': name, 'price': price_val, 'btn': buy_btn})
                            except: continue

                        if sellers:
                            # Advanced selection: top 2
                            sellers.sort(key=lambda x: x['price'])
                            cheapest = sellers[0]
                            second_cheapest = sellers[1] if len(sellers) > 1 else None
                            
                            final_url = self._follow_redirect(cheapest['btn'])
                            second_final_url = self._follow_redirect(second_cheapest['btn']) if second_cheapest else None

                            results.append({
                                'name': item['name'], 
                                'shop_site_price': item['shop_site_price'],
                                'price': cheapest['price'], 
                                'shop_name': cheapest['name'],
                                'second_price': second_cheapest['price'] if second_cheapest else None,
                                'second_shop_name': second_cheapest['name'] if second_cheapest else None,
                                'product_url': final_url, 
                                'second_product_url': second_final_url,
                                'image_url': image_url
                            })
                        break # Success
                    except (WebDriverException, Exception) as de:
                        attempts += 1
                        err_msg = str(de).lower()
                        if any(x in err_msg for x in ["connection", "refused", "target", "window"]):
                            print(f"⚠️ Browser crash on product {idx+1}/{total}. Restarting driver (Attempt {attempts})...")
                            self.restart_driver(headless=config.HEADLESS)
                        else:
                            print(f"❌ Error scraping {item['name']}: {de}")
                            if attempts >= 3: break
                        time.sleep(2)
            return results
        except Exception as e:
            print(f"Error extracting shop products: {e}")
            return []
