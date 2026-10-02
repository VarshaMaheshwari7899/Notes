"""
Batch-download Instagram post images through SnapInsta (snapinsta.to) and
optionally merge each post into a PDF.

Setup (one time):   uv sync
Usage:              uv run insta_batch_download.py

links.txt format, one post per line:   URL | Title of the PDF
    https://www.instagram.com/p/DdJYueXkpns | AI Roadmap 2026
(The "| Title" part is optional; without it the post's shortcode is used.)

Output: ./downloads/<Title> [<shortcode>]/<shortcode>_01.jpg ...
        and ./downloads/<Title> [<shortcode>]/<Title>.pdf  (PDF title set too)
If something fails, look in ./debug/ for a screenshot + HTML of the page.

Only use for public posts, for personal use. Respect creators' rights.
"""

import mimetypes
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ----------------------------- CONFIG ---------------------------------
LINKS_FILE = Path("links.txt")
OUT_DIR = Path("downloads")
DEBUG_DIR = Path("debug")
ALL_PDFS_DIR = Path("all_pdfs")  # every PDF is also copied here (flat folder)
FAILED_FILE = Path("failed.txt")
PROFILE_DIR = Path(".browser-profile")  # keeps cookies between runs
SNAPINSTA_URL = "https://snapinsta.to/"
MAKE_PDF = True
HEADLESS = False            # keep False: sites often block headless browsers
RESULT_TIMEOUT_MS = 60_000
PAUSE_BETWEEN_LINKS = 3
MANUAL_FALLBACK = True      # if nothing appears, let YOU click Download once
# Path to Brave. Set to None to use Playwright's bundled Chromium instead.
BRAVE_PATH = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

# ---------------------------- SELECTORS -------------------------------
INPUT_SELECTOR = 'input[type="text"], input[type="url"], input:not([type])'
SUBMIT_SELECTOR = 'button:has-text("Download")'
RESULT_TEXT = "download"    # new links whose text contains this
RESULT_HREF_HINTS = ("download", "dl.", "/dl", "cdn", "fbcdn", ".jpg", ".jpeg",
                     ".png", ".webp", ".mp4")
STABLE_SECONDS = 4
# ----------------------------------------------------------------------

BAD_HREF = ("javascript:", "#", "play.google", "apps.apple", "mailto:")
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def shortcode_from_url(url: str) -> str:
    m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else re.sub(r"\W+", "_", url)[-20:]


def clean_name(text: str, max_len: int = 100) -> str:
    """Make a string safe to use as a Windows file/folder name."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    return text[:max_len].rstrip(". ")


def read_links() -> list[tuple[str, str]]:
    """Return [(url, title), ...]. Line format: URL | Title"""
    links, seen = [], set()
    for line in LINKS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, title = line.partition("|")
        url, title = url.strip(), title.strip()
        if url and url not in seen:
            seen.add(url)
            links.append((url, title))
    return links


def close_extra_pages(context, keep):
    for p in context.pages:
        if p != keep:
            try:
                p.close()
            except Exception:
                pass


def snapshot_hrefs(page) -> set[str]:
    return set(page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)"))


def new_download_links(page, baseline: set[str]) -> list[str]:
    items = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => [e.href, (e.innerText || '').trim()])"
    )
    hrefs = []
    for href, text in items:
        if href in baseline or not href.startswith("http") or href.startswith(BAD_HREF):
            continue
        text_ok = RESULT_TEXT in text.lower()
        href_ok = any(h in href.lower() for h in RESULT_HREF_HINTS)
        if (text_ok or href_ok) and href not in hrefs:
            hrefs.append(href)
    return hrefs


def wait_for_results(page, baseline, timeout_ms) -> list[str]:
    deadline = time.time() + timeout_ms / 1000
    last, stable_since = [], None
    while time.time() < deadline:
        cur = new_download_links(page, baseline)
        if cur and cur == last:
            stable_since = stable_since or time.time()
            if time.time() - stable_since >= STABLE_SECONDS:
                return cur
        else:
            stable_since = None
        last = cur
        time.sleep(1)
    if last:
        return last
    raise RuntimeError("Timed out waiting for download links")


def save_debug(page, code: str):
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{code}.png"), full_page=True)
        (DEBUG_DIR / f"{code}.html").write_text(page.content(), encoding="utf-8")
        print(f"   debug saved -> {DEBUG_DIR / code}.png / .html")
    except Exception as e:
        print(f"   (could not save debug files: {e})")


def pick_extension(resp, href: str) -> str:
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
    ext = mimetypes.guess_extension(ctype) if ctype else None
    if ext in (".jpe", None):
        ext = Path(urlparse(href).path).suffix or ".jpg"
    return ".jpg" if ext == ".jpe" else ext


def submit_link(page, url: str):
    """Type the link like a person would so the site's JS notices it."""
    box = page.locator(INPUT_SELECTOR).first
    box.click()
    box.fill("")
    box.press_sequentially(url, delay=25)
    time.sleep(0.5)
    try:
        page.click(SUBMIT_SELECTOR, timeout=5_000)
    except PWTimeout:
        box.press("Enter")


def process_link(context, page, url: str, title: str = "") -> int:
    code = shortcode_from_url(url)
    safe_title = clean_name(title) if title else ""
    folder = f"{safe_title} [{code}]" if safe_title else code
    post_dir = OUT_DIR / folder
    post_dir.mkdir(parents=True, exist_ok=True)

    page.goto(SNAPINSTA_URL, wait_until="domcontentloaded")
    close_extra_pages(context, page)
    page.wait_for_selector(INPUT_SELECTOR, timeout=30_000)
    time.sleep(1.5)  # let the page's scripts finish loading
    baseline = snapshot_hrefs(page)

    submit_link(page, url)
    close_extra_pages(context, page)

    try:
        hrefs = wait_for_results(page, baseline, RESULT_TIMEOUT_MS)
    except RuntimeError:
        save_debug(page, code)
        if not MANUAL_FALLBACK:
            raise
        print("   No results appeared automatically.")
        print("   -> In the browser window, paste the link and click Download")
        print("      yourself (solve any CAPTCHA). When the results show, come")
        input("      back here and press Enter... ")
        hrefs = wait_for_results(page, baseline, 15_000)

    close_extra_pages(context, page)
    saved = 0
    for i, href in enumerate(hrefs, start=1):
        resp = context.request.get(href, timeout=60_000)
        if not resp.ok:
            print(f"   ! item {i}: HTTP {resp.status}")
            continue
        path = post_dir / f"{code}_{i:02d}{pick_extension(resp, href)}"
        path.write_bytes(resp.body())
        saved += 1
        print(f"   saved {path}")

    if MAKE_PDF and saved:
        make_pdf(post_dir, safe_title or code, title or code, code)
    return saved


def copy_to_all_pdfs(pdf_path: Path, code: str):
    ALL_PDFS_DIR.mkdir(exist_ok=True)
    dest = ALL_PDFS_DIR / pdf_path.name
    if dest.exists():  # same title used twice -> add the shortcode
        dest = ALL_PDFS_DIR / f"{pdf_path.stem} [{code}]{pdf_path.suffix}"
    shutil.copy2(pdf_path, dest)
    print(f"   copied -> {dest}")


def make_pdf(post_dir: Path, file_stem: str, title: str, code: str = ""):
    from PIL import Image

    imgs = sorted(p for p in post_dir.iterdir()
                  if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    if not imgs:
        return
    pages = [Image.open(p).convert("RGB") for p in imgs]
    pdf_path = post_dir / f"{file_stem}.pdf"
    pages[0].save(pdf_path, save_all=True, append_images=pages[1:], title=title)
    print(f"   PDF -> {pdf_path}")
    copy_to_all_pdfs(pdf_path, code or file_stem)


def main():
    links = read_links()
    if not links:
        print("links.txt is empty.")
        return
    OUT_DIR.mkdir(exist_ok=True)
    failed = []

    launch = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=HEADLESS,
        no_viewport=True,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    if BRAVE_PATH:
        if not Path(BRAVE_PATH).exists():
            raise SystemExit(f"Brave not found at {BRAVE_PATH}\nEdit BRAVE_PATH.")
        launch["executable_path"] = BRAVE_PATH

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(**launch)
        context.add_init_script(STEALTH_JS)
        page = context.pages[0] if context.pages else context.new_page()

        for n, (url, title) in enumerate(links, start=1):
            print(f"[{n}/{len(links)}] {title or url}")
            try:
                print(f"   done ({process_link(context, page, url, title)} files)")
            except Exception as e:
                print(f"   FAILED: {e}")
                failed.append(f"{url} | {title}" if title else url)
            time.sleep(PAUSE_BETWEEN_LINKS)

        context.close()

    if failed:
        FAILED_FILE.write_text("\n".join(failed), encoding="utf-8")
        print(f"\n{len(failed)} failed -> {FAILED_FILE}")
    else:
        print("\nAll done.")


if __name__ == "__main__":
    main()