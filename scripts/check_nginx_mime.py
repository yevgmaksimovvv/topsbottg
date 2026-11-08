import os
import re
from collections import deque
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

BASE_URL = os.environ.get("NGINX_BASE_URL", "http://127.0.0.1:18081").rstrip("/")
MODULE_SCRIPT_RE = re.compile(r"<script\b[^>]*type=[\"']module[\"'][^>]*>", re.IGNORECASE)
SOURCE_RE = re.compile(r"\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
IMPORT_RE = re.compile(r"\bfrom\s*[\"']([^\"']+)[\"']")
SIDE_EFFECT_IMPORT_RE = re.compile(r"^\s*import\s*[\"']([^\"']+)[\"']", re.MULTILINE)
JAVASCRIPT_MIME_TYPES = {"application/javascript", "text/javascript"}


def fetch(url: str) -> tuple[str, str, str]:
    try:
        with urlopen(url, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "").split(";", maxsplit=1)[0].lower()
            nosniff = response.headers.get("X-Content-Type-Options", "").lower()
            body = response.read().decode("utf-8")
            return body, content_type, nosniff
    except (HTTPError, URLError, TimeoutError) as exc:
        raise SystemExit(f"Request failed for {url}: {exc}") from exc


def main() -> None:
    page_url = f"{BASE_URL}/miniapp/"
    html, _, _ = fetch(page_url)
    queue = deque(
        urljoin(page_url, source.group(1))
        for tag in MODULE_SCRIPT_RE.findall(html)
        if (source := SOURCE_RE.search(tag)) is not None
    )
    if not queue:
        raise SystemExit("No JavaScript module entry point found in the served Mini App")

    origin = urlparse(BASE_URL).netloc
    visited: set[str] = set()
    while queue:
        module_url = queue.popleft()
        if module_url in visited:
            continue
        if urlparse(module_url).netloc != origin:
            raise SystemExit(f"Unexpected external module import: {module_url}")

        visited.add(module_url)
        module_source, content_type, nosniff = fetch(module_url)
        if content_type not in JAVASCRIPT_MIME_TYPES:
            raise SystemExit(f"Wrong JavaScript MIME type for {module_url}: {content_type or '<missing>'}")
        if nosniff != "nosniff":
            raise SystemExit(f"X-Content-Type-Options nosniff missing for {module_url}")

        imports = IMPORT_RE.findall(module_source) + SIDE_EFFECT_IMPORT_RE.findall(module_source)
        queue.extend(urljoin(module_url, specifier) for specifier in imports)

    print(f"Verified {len(visited)} JavaScript modules through Nginx with JavaScript MIME and nosniff")


if __name__ == "__main__":
    main()
