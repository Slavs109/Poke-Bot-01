# Test status

Automated pull-request checks are configured in `.github/workflows/stock-check.yml`.

The pull-request test job installs dependencies, compiles the Python files, and runs the isolated unit test suite. Live retailer requests and Discord webhook delivery are intentionally excluded from pull-request tests because they depend on external anti-bot behavior and repository secrets.
