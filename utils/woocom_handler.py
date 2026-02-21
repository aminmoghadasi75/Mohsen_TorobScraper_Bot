import requests
import logging
from requests.auth import HTTPBasicAuth
import config

class GerishmallAPI:
    def __init__(self):
        self.base_url = f"{config.GERISHMALL_URL.rstrip('/')}/wp-json/wc/v3"
        self.auth = HTTPBasicAuth(config.WC_CONSUMER_KEY, config.WC_CONSUMER_SECRET)

    def get_product_by_name(self, name):
        """Finds a product ID by its exact name or close match."""
        try:
            # First attempt: search for exact name
            params = {"search": name, "per_page": 10}
            response = requests.get(f"{self.base_url}/products", auth=self.auth, params=params, timeout=15)
            response.raise_for_status()
            products = response.json()
            
            if not products:
                return None
            
            # Find the best match (exact name match preferred)
            for product in products:
                if product['name'] == name:
                    return product
            
            # If no exact match, return the first one from search if it looks similar
            return products[0]
            
        except Exception as e:
            logging.error(f"Error fetching product from API: {e}")
            return None

    def update_price(self, product_name, target_price=None, reduction=10000):
        """
        Updates the price using the WooCommerce REST API.
        If target_price is provided, it sets the Sale Price to that value.
        Otherwise, it applies the reduction logic based on current prices.
        """
        try:
            product = self.get_product_by_name(product_name)
            if not product:
                logging.error(f"Product '{product_name}' not found via API.")
                return False
            
            product_id = product['id']
            regular_price = float(product.get('regular_price', 0) or 0)
            sale_price = float(product.get('sale_price', 0) or 0)
            
            if target_price is not None:
                new_val = target_price
            else:
                if sale_price > 0:
                    new_val = sale_price - reduction
                else:
                    new_val = regular_price - reduction
            
            # Ensure price is integer for better display/logic
            new_val = int(new_val)
            
            # WooCommerce API update data
            data = {
                "sale_price": str(new_val)
            }
            
            # Update the product
            response = requests.put(f"{self.base_url}/products/{product_id}", auth=self.auth, json=data, timeout=15)
            response.raise_for_status()
            
            logging.info(f"✅ API SUCCESS: Product '{product_name}' (ID: {product_id}) updated to {new_val}")
            return True
            
        except Exception as e:
            logging.error(f"Error updating price via API: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logging.error(f"API Response: {e.response.text}")
            return False
