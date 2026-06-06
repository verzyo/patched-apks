import os
import logging
from curl_cffi import requests
from curl_cffi.requests.impersonate import DEFAULT_CHROME
from github import Github

# Logging (must be configured before session creation for FlareSolverr logs)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)


class _FlareSolverrResponse:
    """Lightweight response wrapper for FlareSolverr HTML results."""
    def __init__(self, content_str, status_code, url):
        self.content = content_str.encode('utf-8')
        self.text = content_str
        self.status_code = status_code
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP Error {self.status_code}")


class FlareSolverrSession:
    """Wraps curl_cffi session with automatic FlareSolverr fallback on 403s."""
    def __init__(self):
        self._session = requests.Session(impersonate=DEFAULT_CHROME)

    def get(self, url, *args, **kwargs):
        res = self._session.get(url, *args, **kwargs)
        if res.status_code == 403 and "apkmirror.com" in url:
            logging.info(f"403 Forbidden on {url}. Retrying with FlareSolverr...")
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": 60000
            }
            try:
                fs_res = requests.post("http://localhost:8191/v1", json=payload, timeout=65)
                data = fs_res.json()
                if data.get("status") == "ok":
                    html = data.get("solution", {}).get("response", "")
                    status = data.get("solution", {}).get("status", 200)
                    return _FlareSolverrResponse(html, status, url)
                else:
                    logging.warning(f"FlareSolverr returned error: {data}")
            except Exception as e:
                logging.warning(f"FlareSolverr request failed (is it running?): {e}")

        return res


session = FlareSolverrSession()

# Env Vars
github_token = os.getenv('GITHUB_TOKEN') or os.getenv('GH_TOKEN')
repository = os.getenv('GITHUB_REPOSITORY')
endpoint_url = os.getenv('ENDPOINT_URL')
access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')
bucket_name = os.getenv('BUCKET_NAME')

# APKmirror base url
base_url = "https://www.apkmirror.com"

if github_token:
    logging.info("GitHub token detected; using authenticated GitHub API client")
    gh = Github(github_token)
else:
    if os.getenv("CI"):
        logging.warning("No GITHUB_TOKEN/GH_TOKEN detected in CI; GitHub release lookups may fail")
    else:
        logging.warning("No GitHub token detected; using anonymous GitHub API client")
    gh = Github()
