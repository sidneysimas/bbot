import os
import ssl
import shutil
import pytest
import asyncio
import logging
from pathlib import Path
from contextlib import suppress
from omegaconf import OmegaConf
from pytest_httpserver import HTTPServer

from bbot.core import CORE
from bbot.core.helpers.misc import execute_sync_or_async
from bbot.core.helpers.interactsh import server_list as interactsh_servers


test_config = OmegaConf.load(Path(__file__).parent / "test.conf")
if test_config.get("debug", False):
    os.environ["BBOT_DEBUG"] = "True"

if test_config.get("debug", False):
    logging.getLogger("bbot").setLevel(logging.DEBUG)
else:
    # silence stdout + trace
    root_logger = logging.getLogger()
    for h in root_logger.handlers:
        h.addFilter(lambda x: x.levelname not in ("STDOUT", "TRACE"))

CORE.merge_default(test_config)


@pytest.fixture
def assert_all_responses_were_requested() -> bool:
    return False


@pytest.fixture
def bbot_httpserver():
    server = HTTPServer(host="127.0.0.1", port=8888, threaded=True)
    server.start()

    yield server

    server.clear()
    if server.is_running():
        server.stop()

    # this is to check if the client has made any request where no
    # `assert_request` was called on it from the test

    server.check_assertions()
    server.clear()


@pytest.fixture
def bbot_httpserver_ssl():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    current_dir = Path(__file__).parent
    keyfile = str(current_dir / "testsslkey.pem")
    certfile = str(current_dir / "testsslcert.pem")
    context.load_cert_chain(certfile, keyfile)
    server = HTTPServer(host="127.0.0.1", port=9999, ssl_context=context, threaded=True)
    server.start()

    yield server

    server.clear()
    if server.is_running():
        server.stop()

    # this is to check if the client has made any request where no
    # `assert_request` was called on it from the test

    server.check_assertions()
    server.clear()


@pytest.fixture
def non_mocked_hosts() -> list:
    return ["127.0.0.1", "localhost", "raw.githubusercontent.com"] + interactsh_servers


@pytest.fixture
def bbot_httpserver_allinterfaces():
    server = HTTPServer(host="0.0.0.0", port=5556, threaded=True)
    server.start()

    yield server

    server.clear()
    if server.is_running():
        server.stop()
    server.check_assertions()
    server.clear()


class Interactsh_mock:
    def __init__(self, name):
        self.name = name
        self.log = logging.getLogger(f"bbot.interactsh.{self.name}")
        self.interactions = []
        self.correlation_id = "deadbeef-dead-beef-dead-beefdeadbeef"
        self.stop = False
        self.poll_task = None

    def mock_interaction(self, subdomain_tag, msg=None):
        self.log.info(f"Mocking interaction to subdomain tag: {subdomain_tag}")
        if msg is not None:
            self.log.info(msg)
        self.interactions.append(subdomain_tag)

    async def register(self, callback=None):
        if callable(callback):
            self.poll_task = asyncio.create_task(self.poll_loop(callback))
        return "fakedomain.fakeinteractsh.com"

    async def deregister(self, callback=None):
        self.stop = True
        if self.poll_task is not None:
            self.poll_task.cancel()
            with suppress(BaseException):
                await self.poll_task

    async def poll_loop(self, callback=None):
        while not self.stop:
            data_list = await self.poll(callback)
            if not data_list:
                await asyncio.sleep(1)
                continue

    async def poll(self, callback=None):
        poll_results = []
        for subdomain_tag in self.interactions:
            result = {"full-id": f"{subdomain_tag}.fakedomain.fakeinteractsh.com", "protocol": "HTTP"}
            poll_results.append(result)
            if callback is not None:
                await execute_sync_or_async(callback, result)
        self.interactions = []
        return poll_results


import threading
import http.server
import socketserver
import urllib.request


class Proxy(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "Proxy"
    urls = []

    def do_GET(self):
        self.urls.append(self.path)

        # Extract host and port from path
        netloc = urllib.parse.urlparse(self.path).netloc
        host, _, port = netloc.partition(":")

        # Fetch the content
        conn = http.client.HTTPConnection(host, port if port else 80)
        conn.request("GET", self.path, headers=self.headers)
        response = conn.getresponse()

        # Send the response back to the client
        self.send_response(response.status)
        for header, value in response.getheaders():
            self.send_header(header, value)
        self.end_headers()
        self.copyfile(response, self.wfile)

        response.close()
        conn.close()


@pytest.fixture
def proxy_server():
    # Set up an HTTP server that acts as a simple proxy.
    server = socketserver.ThreadingTCPServer(("localhost", 0), Proxy)

    # Start the server in a new thread.
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    yield server

    # Stop the server.
    server.shutdown()
    server_thread.join()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    RED = "\033[1;31m"
    GREEN = "\033[1;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[1;34m"
    CYAN = "\033[1;36m"
    RESET = "\033[0m"
    stats = terminalreporter.stats
    total_tests = len(terminalreporter._session.items)
    passed = len(stats.get("passed", []))
    skipped = len(stats.get("skipped", []))
    errors = len(stats.get("error", []))
    failed = stats.get("failed", [])

    print("\nTest Session Summary:")
    print(f"Total tests run: {total_tests}")
    print(
        f"{GREEN}Passed: {passed}{RESET}, {RED}Failed: {len(failed)}{RESET}, {YELLOW}Skipped: {skipped}{RESET}, Errors: {errors}"
    )

    if failed:
        print(f"\n{RED}Detailed failed test report:{RESET}")
        for item in failed:
            test_name = item.nodeid.split("::")[-1] if "::" in item.nodeid else item.nodeid
            file_and_line = f"{item.location[0]}:{item.location[1]}"  # File path and line number
            print(f"{BLUE}Test Name: {test_name}{RESET} {CYAN}({file_and_line}){RESET}")
            print(f"{RED}Location: {item.nodeid} at {item.location[0]}:{item.location[1]}{RESET}")
            print(f"{RED}Failure details:\n{item.longreprtext}{RESET}")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_sessionfinish(session, exitstatus):
    # Remove handlers from all loggers to prevent logging errors at exit
    loggers = [logging.getLogger("bbot")] + list(logging.Logger.manager.loggerDict.values())
    for logger in loggers:
        handlers = getattr(logger, "handlers", [])
        for handler in handlers:
            logger.removeHandler(handler)

    # Wipe out BBOT home dir
    shutil.rmtree("/tmp/.bbot_test", ignore_errors=True)

    yield
