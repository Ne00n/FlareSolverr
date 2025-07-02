import json
import logging
import os
import sys
from urllib.parse import urlparse
import socket
import random
import re
from email.utils import format_datetime
from datetime import datetime

import certifi
from bottle import run, response, Bottle, request, ServerAdapter

from bottle_plugins.error_plugin import error_plugin
from bottle_plugins.logger_plugin import logger_plugin
from bottle_plugins import prometheus_plugin
from dtos import V1RequestBase
import flaresolverr_service
import utils


class JSONErrorBottle(Bottle):
    """
    Handle 404 errors
    """
    def default_error_handler(self, res):
        response.content_type = 'application/json'
        return json.dumps(dict(error=res.body, status_code=res.status_code))


app = JSONErrorBottle()

# Global proxy pool
PROXY_POOL = []
# Per-domain proxy assignment
DOMAIN_PROXIES = {}


def discover_proxies():
    """
    Scan 10.0.0.1:8888 to 10.0.119.1:8888 and add working proxies to PROXY_POOL.
    """
    global PROXY_POOL
    proxies = []
    for x in range(120):
        host = f"10.0.{x}.1"
        port = 8888
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            proxies.append(f"http://{host}:{port}")
        except Exception:
            pass
        finally:
            s.close()
    PROXY_POOL = proxies
    logging.info(f"Discovered {len(PROXY_POOL)} proxies: {PROXY_POOL}")


@app.route('<path:path>', method=['GET'])
def controller_v1(path):
    """
    Controller v1S
    """
    logging.debug(f"request.json type: {type(request.json)}, value: {request.json}")
    payload = request.json or {}
    session_id, domain = None, None
    try:
        domain = urlparse(request.url).netloc
    except Exception as e:
        logging.warning(f"Failed to parse URL for session: {url}, error: {e}")
    if domain:
        session_id = 'flaresolverr-default-session'
    payload['session'] = domain

    # --- Per-domain proxy assignment ---
    if domain not in DOMAIN_PROXIES:
        if len(PROXY_POOL) >= 10:
            DOMAIN_PROXIES[domain] = random.sample(PROXY_POOL, 10)
        else:
            DOMAIN_PROXIES[domain] = PROXY_POOL.copy()
    # Pick a random proxy for this request
    proxy = random.choice(DOMAIN_PROXIES[domain]) if DOMAIN_PROXIES[domain] else None
    # Modify sessionID to be unique per domain and proxy
    session_id = f"flaresolverr-{domain.replace('.', '_')}-{proxy.replace('.', '_').replace('http://','')}"
    payload['session'] = session_id
    payload['proxy'] = proxy
    # --- End per-domain proxy assignment ---

    logging.info(f"Selected proxy {proxy} for session {session_id} (domain: {domain})")

    req = V1RequestBase(payload)
    logging.debug(f"Constructed V1RequestBase: {req.__dict__}")
    req.url = request.url.replace("http","https")
    res = flaresolverr_service.controller_v1_endpoint(req)
    if res.__error_500__:
        response.status = 500
        response.content_type = 'text/plain'
        return res.message or "Internal Server Error"
    # If solution and HTML response exist, return it as HTML
    if hasattr(res, 'solution') and res.solution and hasattr(res.solution, 'response') and res.solution.response:
        response.content_type = 'text/html'
        #rewrite to http
        html = res.solution.response.replace("https://","http://")
        # --- Remove ThankedByBox elements if present ---
        html = re.sub(r'<div[^>]*class=["\"][^"\"]*ThankedByBox[^"\"]*["\"][^>]*>.*?</div>', '', html, flags=re.DOTALL)
        # --- Remove Box BoxInThisDiscussion elements if present ---
        html = re.sub(r'<div[^>]*class=["\"][^"\"]*Box BoxInThisDiscussion[^"\"]*["\"][^>]*>.*?</div>', '', html, flags=re.DOTALL)
        # --- Extract <time title="..."> and set Last-Modified header ---
        match = re.search(r'rel="nofollow"><time title="([^"]+)"', html)
        if match:
            date_str = match.group(1)
            try:
                # Try parsing the date string (e.g., 'February 17, 2025 1:20AM')
                dt = datetime.strptime(date_str, '%B %d, %Y %I:%M%p')
                # Format as RFC 1123 for Last-Modified
                http_date = format_datetime(dt)
                response.set_header('Last-Modified', http_date)
                logging.info(f"Set Last-Modified header to: {http_date}")
            except Exception as e:
                logging.warning(f"Failed to parse date '{date_str}' for Last-Modified header: {e}")
        # --- End Last-Modified logic ---
        return html
    # Fallback: return JSON (should not happen in normal flow)
    response.content_type = 'application/json'
    return utils.object_to_dict(res)


if __name__ == "__main__":
    # Discover proxies on startup
    discover_proxies()
    # check python version
    if sys.version_info < (3, 9):
        raise Exception("The Python version is less than 3.9, a version equal to or higher is required.")

    # fix for HEADLESS=false in Windows binary
    # https://stackoverflow.com/a/27694505
    if os.name == 'nt':
        import multiprocessing
        multiprocessing.freeze_support()

    # fix ssl certificates for compiled binaries
    # https://github.com/pyinstaller/pyinstaller/issues/7229
    # https://stackoverflow.com/questions/55736855/how-to-change-the-cafile-argument-in-the-ssl-module-in-python3
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    os.environ["SSL_CERT_FILE"] = certifi.where()

    # validate configuration
    log_level = os.environ.get('LOG_LEVEL', 'info').upper()
    log_html = utils.get_config_log_html()
    headless = utils.get_config_headless()
    server_host = os.environ.get('HOST', '0.0.0.0')
    server_port = int(os.environ.get('PORT', 80))

    # configure logger
    logger_format = '%(asctime)s %(levelname)-8s %(message)s'
    if log_level == 'DEBUG':
        logger_format = '%(asctime)s %(levelname)-8s ReqId %(thread)s %(message)s'
    logging.basicConfig(
        format=logger_format,
        level=log_level,
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )
    # disable warning traces from urllib3
    logging.getLogger('urllib3').setLevel(logging.ERROR)
    logging.getLogger('selenium.webdriver.remote.remote_connection').setLevel(logging.WARNING)
    logging.getLogger('undetected_chromedriver').setLevel(logging.WARNING)

    logging.info(f'FlareSolverr {utils.get_flaresolverr_version()}')
    logging.debug('Debug log enabled')

    # Get current OS for global variable
    utils.get_current_platform()

    # test browser installation
    flaresolverr_service.test_browser_installation()

    # start bootle plugins
    # plugin order is important
    app.install(logger_plugin)
    app.install(error_plugin)
    prometheus_plugin.setup()
    app.install(prometheus_plugin.prometheus_plugin)

    # start webserver
    # default server 'wsgiref' does not support concurrent requests
    # https://github.com/FlareSolverr/FlareSolverr/issues/680
    # https://github.com/Pylons/waitress/issues/31
    class WaitressServerPoll(ServerAdapter):
        def run(self, handler):
            from waitress import serve
            serve(handler, host=self.host, port=self.port, asyncore_use_poll=True)
    run(app, host=server_host, port=server_port, quiet=True, server=WaitressServerPoll)
