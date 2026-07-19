"""NexHealth API client + shared config loaded from scheduling_rules.yaml."""

import json
import os
import urllib.parse
import urllib.request

import yaml

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(os.path.dirname(BACKEND_DIR))

RULES = yaml.safe_load(open(os.path.join(REPO_DIR, "scheduling_rules.yaml")))
PRACTICE = RULES["practice"]
CUTOFF = RULES["afternoon_cutoff"]
CUTOFF_HOUR = int(CUTOFF["time"].split(":")[0])
NEXHEALTH_API_KEY = os.environ.get("NEXHEALTH_API_KEY", "")

# ids created by nexhealth_scheduler.py (patients, appointment types)
NEX_STATE = json.load(open(os.path.join(REPO_DIR, "nexhealth_cache", "state.json")))


class NexHealth:
    """Thin authenticated client for the NexHealth Synchronizer API."""

    def __init__(self):
        self.token = None

    def request(self, method, path, params=None, body=None):
        if self.token is None and path != "/authenticates":
            self.login()
        params = {"subdomain": PRACTICE["subdomain"],
                  "location_id": PRACTICE["location_id"], **(params or {})}
        if path == "/authenticates" or "lids[]" in params:
            params.pop("location_id", None)
        url = f"https://nexhealth.info{path}?{urllib.parse.urlencode(params, doseq=True)}"
        headers = {"Nex-Api-Version": "v20240412", "Accept": "application/json",
                   "User-Agent": "scheduling-assistant/1.0"}
        headers["Authorization"] = (NEXHEALTH_API_KEY if path == "/authenticates"
                                    else f"Bearer {self.token}")
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data, headers, method=method)
        try:
            resp = json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            resp = json.loads(e.read())
        if resp.get("error"):
            raise RuntimeError(str(resp["error"]))
        return resp

    def login(self):
        self.token = self.request("POST", "/authenticates")["data"]["token"]


nex = NexHealth()
