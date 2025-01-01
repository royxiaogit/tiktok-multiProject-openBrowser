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
            headless=False,  # Force non-headless mode
            channel="chrome",  # Use actual Chrome instead of Chromium
            args=[
                '--remote-debugging-port=9901',
                '--no-sandbox',
                '--start-maximized',  # Start with maximized window
                '--disable-blink-features=AutomationControlled',  # Prevent detection as automated browser
                '--disable-infobars',  # Remove "Chrome is being controlled by automated software" infobar
            ]
        )
        
        # Create a new context with viewport that matches maximized window
        context = browser.new_context(
            viewport=None,  # Allow window to be resized
            accept_downloads=True,  # Accept downloads automatically
            java_script_enabled=True,  # Ensure JavaScript is enabled
            bypass_csp=True  # Bypass Content Security Policy
        )
        
        # Create a new page
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
