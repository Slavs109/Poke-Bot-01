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
            '<script type="application/ld+json">' + json.dumps(payload) + "</script>",
            "html.parser",
        )
        name, price, in_stock, product_url = check_stock.parse_jsonld(
            soup, "https://www.target.com/search"
        )
        self.assertEqual(name, "Pokemon Test Elite Trainer Box")
        self.assertEqual(price, 49.99)
        self.assertTrue(in_stock)
        self.assertEqual(product_url, "https://www.target.com/p/pokemon-test/123")

    @patch("check_stock.build_session")
    def test_inspect_uses_discovered_product_url(self, mock_build_session: Mock) -> None:
        html = """
        <html><head>
          <link rel="canonical" href="https://www.target.com/p/pokemon-test/-/A-123">
          <script type="application/ld+json">
          {"@type":"Product","name":"Pokemon Test Elite Trainer Box",
           "url":"https://www.target.com/p/pokemon-test/-/A-123",
           "offers":{"price":"49.99","availability":"https://schema.org/InStock"}}
          </script>
        </head><body>Add to cart</body></html>
        """
        response = Mock(status_code=200, text=html, url="https://www.target.com/p/pokemon-test/-/A-123")
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response
        mock_build_session.return_value = session

        result = check_stock.inspect({
            "name": "Pokemon Test ETB",
            "url": response.url,
            "max_price": 59.99,
        })

        self.assertEqual(result.retailer, "Target")
        self.assertEqual(result.price, 49.99)
        self.assertTrue(result.in_stock)
        self.assertEqual(result.checkout_url, response.url)

    @patch("check_stock.build_session")
    def test_explicit_checkout_url_wins(self, mock_build_session: Mock) -> None:
        response = Mock(
            status_code=200,
            text='<html><body><span>$39.99</span><button>Add to cart</button></body></html>',
            url="https://www.acehardware.com/product/pokemon-cards",
        )
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response
        mock_build_session.return_value = session

        result = check_stock.inspect({
            "name": "Pokemon cards",
            "url": response.url,
            "checkout_url": "https://www.acehardware.com/cart",
            "max_price": 59.99,
        })
        self.assertEqual(result.checkout_url, "https://www.acehardware.com/cart")
        self.assertEqual(result.price, 39.99)
        self.assertTrue(result.in_stock)

    @patch("check_stock.requests.Session")
    def test_pokemon_center_session_warms_homepage(self, mock_session_class: Mock) -> None:
        session = Mock()
        mock_session_class.return_value = session
        check_stock.build_session(
            "https://www.pokemoncenter.com/search/pokemon-151-trainer-box", {}
        )
        session.get.assert_called_once_with(
            "https://www.pokemoncenter.com/", timeout=15, allow_redirects=True
        )


if __name__ == "__main__":
    unittest.main()
