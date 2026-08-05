from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

import check_stock


class StockCheckerTests(unittest.TestCase):
    def test_retailer_mapping(self) -> None:
        cases = {
            "https://www.samsclub.com/p/test": "Sam's Club",
            "https://www.costco.com/test.html": "Costco",
            "https://www.acehardware.com/test": "Ace Hardware",
            "https://www.target.com/p/test": "Target",
            "https://www.dickssportinggoods.com/p/test": "DICK'S Sporting Goods",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(check_stock.retailer_name(url), expected)

    def test_normalize_url_rejects_unsafe_schemes(self) -> None:
        self.assertIsNone(check_stock.normalize_url("https://example.com", "javascript:alert(1)"))
        self.assertEqual(
            check_stock.normalize_url("https://example.com/search", "/product/123"),
            "https://example.com/product/123",
        )

    def test_jsonld_product_discovery(self) -> None:
        payload = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Pokemon Test Elite Trainer Box",
            "url": "/p/pokemon-test/123",
            "offers": {
                "price": "49.99",
                "availability": "https://schema.org/InStock",
            },
        }
        soup = BeautifulSoup(
            '<script type="application/ld+json">'
            + json.dumps(payload)
            + "</script>",
            "html.parser",
        )
        name, price, in_stock, product_url = check_stock.parse_jsonld(
            soup, "https://www.target.com/search"
        )
        self.assertEqual(name, "Pokemon Test Elite Trainer Box")
        self.assertEqual(price, 49.99)
        self.assertTrue(in_stock)
        self.assertEqual(product_url, "https://www.target.com/p/pokemon-test/123")

    @patch("check_stock.requests.get")
    def test_inspect_uses_discovered_product_url(self, mock_get: Mock) -> None:
        html = """
        <html><head>
          <link rel="canonical" href="https://www.target.com/p/pokemon-test/-/A-123">
          <script type="application/ld+json">
          {
            "@type": "Product",
            "name": "Pokemon Test Elite Trainer Box",
            "url": "https://www.target.com/p/pokemon-test/-/A-123",
            "offers": {
              "price": "49.99",
              "availability": "https://schema.org/InStock"
            }
          }
          </script>
        </head><body>Add to cart</body></html>
        """
        response = Mock()
        response.text = html
        response.url = "https://www.target.com/s?searchTerm=pokemon+cards"
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        result = check_stock.inspect(
            {
                "name": "Pokemon Test ETB",
                "url": response.url,
                "max_price": 59.99,
            }
        )

        self.assertEqual(result.retailer, "Target")
        self.assertEqual(result.price, 49.99)
        self.assertTrue(result.in_stock)
        self.assertEqual(
            result.checkout_url,
            "https://www.target.com/p/pokemon-test/-/A-123",
        )

    @patch("check_stock.requests.get")
    def test_explicit_checkout_url_wins(self, mock_get: Mock) -> None:
        response = Mock()
        response.text = '<html><body><span>$39.99</span><button>Add to cart</button></body></html>'
        response.url = "https://www.acehardware.com/search?query=pokemon"
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        result = check_stock.inspect(
            {
                "name": "Pokemon cards",
                "url": response.url,
                "checkout_url": "https://www.acehardware.com/cart",
                "max_price": 59.99,
            }
        )
        self.assertEqual(result.checkout_url, "https://www.acehardware.com/cart")
        self.assertEqual(result.price, 39.99)
        self.assertTrue(result.in_stock)


if __name__ == "__main__":
    unittest.main()
