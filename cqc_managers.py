"""Fetch public CQC registered-manager names for provider locations.

CQC publishes registered managers on each public location profile. Results are cached
in Postgres/SQLite by the caller so normal CRM page loads do not repeatedly hit CQC.
"""
from html.parser import HTMLParser
from urllib.request import Request, urlopen


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


def fetch_registered_managers(location_id, timeout=10):
    url = f"https://www.cqc.org.uk/location/{location_id}"
    req = Request(url, headers={"User-Agent": "VeridynRecruit/1.0 CQC public-data client"})
    with urlopen(req, timeout=timeout) as response:
        html = response.read().decode("utf-8", "replace")
    parser = _TextParser()
    parser.feed(html)
    parts = parser.parts
    try:
        start = next(i for i, v in enumerate(parts) if "Who runs this service" in v)
    except StopIteration:
        start = 0
    stop = len(parts)
    for i in range(start + 1, len(parts)):
        if parts[i] in ("Similar services nearby...", "About quality of care", "Ratings"):
            stop = i
            break
    managers = []
    for i in range(start + 1, stop):
        if parts[i].strip().lower() == "registered manager":
            # The public profile renders the person's name immediately before the role.
            if i and parts[i - 1] not in managers:
                name = parts[i - 1].strip()
                if name and len(name) <= 160 and "registered manager" not in name.lower():
                    managers.append(name)
    return {"location_id": location_id, "source_url": url, "managers": managers}
