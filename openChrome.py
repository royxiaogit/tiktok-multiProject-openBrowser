from playwright.sync_api import sync_playwright


def openSingleChrome(url: str = "about:blank") -> None:
    """Open a single Chrome browser instance using Playwright.
    The browser will remain open after the script exits.

    Args:
        url (str): URL to open in the browser. Defaults to about:blank.
    """
    p = sync_playwright().start()
    print("Launching Chrome browser with port 9901...")
    browser = p.chromium.launch(
        headless=False,
        args=[
            '--remote-debugging-port=9901',
            '--no-sandbox',
            '--start-maximized',
        ]
    )
    # Set up context with realistic browser behavior
    context = browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
    )
    page = context.new_page()
    page.goto(url)
    print(f"Browser opened successfully at {url}")
    print("Chrome instance will remain open after script exits.")
    # Do not close context or call p.stop() to keep browser running


if __name__ == "__main__":
    # Example usage of openSingleChrome
    openSingleChrome("https://www.tiktok.com")
