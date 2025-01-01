from playwright.sync_api import sync_playwright
import sys

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME_PROFILE_PATH = r"C:\Users\royro\AppData\Local\Google\Chrome\User Data\Profile 10"
DEBUG_PORT = 9901

def openChromeInWindows():
    """
    Opens Chrome browser in Windows using Playwright with real user simulation.
    The browser will remain open until user inputs 'quit'.
    
    Chrome is launched with:
    - Non-headless mode
    - Remote debugging port 9901
    - Path to Chrome executable
    - Specific Chrome profile path (Profile 10)
    """
    try:
        with sync_playwright() as p:
            # Launch the browser with specific Chrome path and debugging port
            browser = p.chromium.launch_persistent_context(
                user_data_dir=CHROME_PROFILE_PATH,  # Use specific Chrome profile
                executable_path=CHROME_PATH,
                headless=False,  # Ensure browser is visible
                viewport=None,  # Use actual screen size
                accept_downloads=True,  # Allow downloads like a real user
                args=[
                    f"--remote-debugging-port={DEBUG_PORT}",
                    "--no-sandbox",
                    "--start-maximized",  # Simulate real user window size
                    "--disable-blink-features=AutomationControlled",  # Prevent detection as automated
                ],
                ignore_default_args=["--enable-automation"],  # Further prevent automation detection
                locale="zh-CN",  # Set locale for realistic behavior
                timezone_id="Asia/Shanghai",  # Set timezone for realistic behavior
            )
            
            # Open a new page
            page = browser.new_page()
            page.goto("about:blank")
            
            print(f"Chrome browser opened successfully on port {DEBUG_PORT}")
            print("Type 'quit' to close the browser:")
            
            # Wait for user input
            while True:
                user_input = input().strip().lower()
                if user_input == 'quit':
                    break
            
            # Clean up
            browser.close()
            
    except Exception as e:
        print(f"Error: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    openChromeInWindows()
