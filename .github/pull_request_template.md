## Summary

- Expand supported Pokémon card retailers.
- Discover canonical product links and honor explicit checkout URLs.
- Add automated syntax and unit tests for retailer mapping, JSON-LD parsing, stock detection, and checkout-link selection.

## Validation

- `python -m py_compile check_stock.py test_check_stock.py`
- `python -m unittest -v`

## Safety

The bot only opens retailer-provided product/cart pages. It does not submit orders, bypass queues or CAPTCHAs, or defeat purchase limits.
