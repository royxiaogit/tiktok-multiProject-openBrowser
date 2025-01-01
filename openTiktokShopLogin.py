from playwright.sync_api import sync_playwright


def openTiktokShopLogin():
    """Opens a Chrome instance with Playwright and navigates to TikTok Shop login.
    The browser remains open after script completion.
    Port: 9901
    URL: https://seller-my.tiktok.com/account/login
    """
    with sync_playwright() as p:
        # Launch Chrome browser with specific port
        browser = p.chromium.launch(
            headless=False,
            args=[
                '--remote-debugging-port=9901',
                '--no-sandbox'
            ]
        )
        # Create a new context and page
        context = browser.new_context()
        page = context.new_page()
        # Navigate to TikTok Shop login page
        page.goto('https://seller-my.tiktok.com/account/login')
        # Keep the browser open by waiting indefinitely
        # This can be interrupted by Ctrl+C
        try:
            while True:
                page.wait_for_timeout(1000)  # Wait 1 second between checks
        except KeyboardInterrupt:
            pass
        finally:
            context.close()  # Close context but keep browser open
