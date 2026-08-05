# Testing

Pull requests run isolated tests without calling live retailer websites or sending Discord messages.

The test suite verifies:

- Python syntax compilation
- Retailer name mapping
- Safe URL normalization
- JSON-LD product name, price, stock, and URL parsing
- Canonical product-link selection
- Explicit checkout URL priority

Scheduled and manually dispatched runs execute the live stock checks only after the test job passes.
