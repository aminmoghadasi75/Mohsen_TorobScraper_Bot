from abc import ABC, abstractmethod

class BaseScraper(ABC):
    @abstractmethod
    def search_product(self, product_name):
        """
        Searches for a product and returns a dictionary with:
        - price
        - shop_name
        - product_url
        - image_url
        """
        pass

    @abstractmethod
    def close(self):
        """Cleanup resources like browser driver"""
        pass
