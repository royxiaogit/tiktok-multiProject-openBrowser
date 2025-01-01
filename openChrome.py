from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from typing import Optional
from playwright.sync_api import sync_playwright


class ChromeBrowser:
    def __init__(self):
        self.driver: Optional[webdriver.Chrome] = None
        self.options = Options()
    
    def initialize(self, headless: bool = False) -> None:
        """Initialize Chrome browser with specified options.
        
        Args:
            headless (bool): Whether to run Chrome in headless mode
        """
        if headless:
            self.options.add_argument('--headless')
        
        self.options.add_argument('--no-sandbox')
        self.options.add_argument('--disable-dev-shm-usage')
        
        service = Service()
        self.driver = webdriver.Chrome(service=service, options=self.options)
    
    def open_url(self, url: str) -> None:
        """Open specified URL in Chrome browser.
        
        Args:
            url (str): URL to open
        """
        if not self.driver:
            raise RuntimeError("Browser not initialized. Call initialize()")
        self.driver.get(url)
    
    def close(self) -> None:
        """Close the browser and clean up resources."""
        if self.driver:
            self.driver.quit()
            self.driver = None



def openSingleChrome(url: str = "about:blank") -> None:
    """Open a single Chrome browser instance using Playwright.
    The browser will remain open after the script exits.

    Args:
        url (str): URL to open in the browser. Defaults to about:blank.
    """
    with sync_playwright() as p:
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
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = context.new_page()
        page.goto(url)
        print(f"Browser opened successfully at {url}")
        print("Chrome instance will remain open after script exits.")
        # Detach from browser to keep it running
        context.close()  # Close context but keep browser running

if __name__ == "__main__":
    # Example usage
    browser = ChromeBrowser()
    browser.initialize()
    browser.open_url("https://www.tiktok.com")
    browser.close()


# Example usage of openSingleChrome
openSingleChrome("https://www.tiktok.com")
