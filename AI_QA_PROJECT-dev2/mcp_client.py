import asyncio
import json
import os
import re
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

from dotenv import load_dotenv

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


load_dotenv()


def _extract_snapshot_text(result) -> str:
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) or str(result)


def _snapshot_text(result) -> str:
    return _extract_snapshot_text(result)


def _extract_json_list(result) -> list:
    text = _extract_snapshot_text(result)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for match in re.finditer(r"\[", text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return value
    return []


def _result_json(result) -> list:
    return _extract_json_list(result)


def _extract_text_value(result) -> str:
    text = _extract_snapshot_text(result).strip()
    page_match = re.search(r"Page URL:\s*(https?://[^\s]+)", text)
    if page_match:
        return page_match.group(1)
    result_match = re.search(r"### Result\s*\n\s*(.+?)(?:\n###|$)", text, re.DOTALL)
    return result_match.group(1).strip() if result_match else text


def _evaluate_text(result) -> str:
    return _extract_text_value(result)


async def _discover_navigation_routes(session) -> list[str]:
    result = await session.call_tool(
        "browser_evaluate",
        {
            "function": """async () => {
                const navigationWords = /^(home|dashboard|cart|orders?|account|profile|history|menu)$/i;
                const candidates = Array.from(document.querySelectorAll('button, [role="button"]'))
                    .filter(element => navigationWords.test(element.textContent.trim()))
                    .filter(element => !element.disabled);
                const routes = [];
                for (const element of candidates) {
                    const before = window.location.href;
                    element.click();
                    await new Promise(resolve => setTimeout(resolve, 500));
                    const after = window.location.href;
                    if (after !== before) routes.push(after);
                    if (window.location.href !== before) {
                        history.back();
                        await new Promise(resolve => setTimeout(resolve, 500));
                    }
                }
                return routes;
            }""",
        },
    )
    return _extract_json_list(result)


async def _discover_interactive_routes(session) -> list[str]:
    return await _discover_navigation_routes(session)


def _canonical_url(url: str) -> str:
    """Normalize ordinary anchors while preserving hash-based SPA routes."""
    parts = urlsplit(url)
    fragment = parts.fragment if parts.fragment.startswith("/") else ""
    path = parts.path.rstrip("/") or "/"
    if fragment and path != "/":
        path += "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, fragment))


def _same_origin(url: str, origin: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc == origin


def _in_crawl_scope(url: str, origin: str, base_path: str) -> bool:
    if not _same_origin(url, origin):
        return False
    path = urlparse(url).path.rstrip("/") or "/"
    scope = base_path.rstrip("/") or "/"
    return scope == "/" or path == scope or path.startswith(f"{scope}/")


async def _authenticate(session) -> str | None:
    email = os.getenv("TEST_EMAIL")
    password = os.getenv("TEST_PASSWORD")
    if not email or not password:
        return None

    await session.call_tool("browser_snapshot", {})
    await asyncio.sleep(1)
    email_value = json.dumps(email)
    password_value = json.dumps(password)
    result = await session.call_tool(
        "browser_evaluate",
        {
            "function": f"""async () => {{
                const email = document.querySelector('#userEmail, input[type=email], input[name=email], input[placeholder="email@example.com"]');
                const password = document.querySelector('#userPassword, input[type=password], input[name=password], input[placeholder*="passsword" i], input[placeholder*="password" i]');
                const submit = document.querySelector('#login, button[type=submit], input[type=submit], button');
                if (!email || !password || !submit) return null;
                const setValue = (element, value) => {{
                    const setter = Object.getOwnPropertyDescriptor(element.__proto__, 'value')?.set;
                    if (setter) setter.call(element, value); else element.value = value;
                    element.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    element.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }};
                setValue(email, {email_value});
                setValue(password, {password_value});
                submit.click();
                await new Promise(resolve => setTimeout(resolve, 1000));
                return window.location.href;
            }}""",
        },
    )
    location = _extract_text_value(result)
    try:
        location = json.loads(location)
    except json.JSONDecodeError:
        pass
    if isinstance(location, str) and location.startswith(("http://", "https://")):
        print(f"Authenticated discovery session: {location}")
        return _canonical_url(location)
    if location in {"null", "None", ""}:
        print("Discovery login failed: the login form was not rendered at the expected route.")
    else:
        print(f"Discovery login did not reach an authenticated page: {location}")
    return None


async def get_page_snapshot(url):

    server_params = StdioServerParameters(
        command="npx",
        args=["@playwright/mcp@latest", "--headless"]
    )

    async with stdio_client(server_params) as (read, write):

        async with ClientSession(read, write) as session:

            await session.initialize()

            print("MCP connected")

            await session.call_tool(
                "browser_navigate",
                {
                    "url": url
                }
            )

            print("Website opened")

            result = await session.call_tool(
                "browser_snapshot",
                {}
            )

            return _extract_snapshot_text(result)


async def discover_site(url: str, max_pages: int = 100) -> str:
    """Collect snapshots from every reachable same-origin page up to a safe limit."""
    configured_limit = os.getenv("DISCOVERY_MAX_PAGES")
    if configured_limit:
        try:
            max_pages = max(1, int(configured_limit))
        except ValueError:
            raise RuntimeError("DISCOVERY_MAX_PAGES must be a positive integer")
    server_params = StdioServerParameters(
        command="npx",
        args=["@playwright/mcp@latest", "--headless"],
    )
    origin = urlparse(url).netloc
    base_path = urlparse(url).path
    pending = [_canonical_url(url)]
    visited = set()
    pages = []

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("MCP connected")
            while pending and len(pages) < max_pages:
                current_url = _canonical_url(pending.pop(0))
                if current_url in visited or not _in_crawl_scope(current_url, origin, base_path):
                    continue
                visited.add(current_url)
                try:
                    await session.call_tool("browser_navigate", {"url": current_url})
                    if not visited - {current_url}:
                        login_url = current_url.rstrip("/") + "/#/auth/login"
                        await session.call_tool("browser_navigate", {"url": login_url})
                        authenticated_url = await _authenticate(session)
                        if authenticated_url and _in_crawl_scope(authenticated_url, origin, base_path):
                            current_url = authenticated_url
                            visited.add(current_url)
                    snapshot = _extract_snapshot_text(
                        await session.call_tool("browser_snapshot", {})
                    )
                    pages.append(f"PAGE URL: {current_url}\n{snapshot}")
                    print(f"Discovered page {len(pages)}/{max_pages}: {current_url}")
                    link_result = await session.call_tool(
                        "browser_evaluate",
                        {
                            "function": "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)",
                        },
                    )
                    for link in _result_json(link_result):
                        absolute = _canonical_url(urljoin(current_url, link))
                        if _in_crawl_scope(absolute, origin, base_path) and absolute not in visited and absolute not in pending:
                            pending.append(absolute)
                    for route in await _discover_interactive_routes(session):
                        absolute = _canonical_url(urljoin(current_url, route))
                        if _in_crawl_scope(absolute, origin, base_path) and absolute not in visited and absolute not in pending:
                            pending.append(absolute)
                except Exception as error:
                    pages.append(f"PAGE URL: {current_url}\nDISCOVERY ERROR: {error}")
            return "\n\n".join(pages)