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



def openSingleChrome(url: str = "about:blank", headless: bool = False) -> None:
    """Open a single Chrome browser instance using Playwright.

    Args:
        url (str): URL to open in the browser. Defaults to about:blank.
        headless (bool): Whether to run browser in headless mode. Defaults to False.
    """
    with sync_playwright() as p:
        print(f"Launching Chrome browser{'in headless mode ' if headless else ' '}with port 9901...")
        browser = p.chromium.launch(
            headless=headless,
            args=[
                '--remote-debugging-port=9901',
                '--no-sandbox',
            ]
        )
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)
        # Keep the browser open
        print(f"Browser opened successfully at {url}")
        input("Press Enter to close the browser...")
        browser.close()
        print("Browser closed successfully")

if __name__ == "__main__":
    # Example usage
    browser = ChromeBrowser()
    browser.initialize()
    browser.open_url("https://www.tiktok.com")
    browser.close()


# Example usage of openSingleChrome
openSingleChrome("https://www.tiktok.com")
