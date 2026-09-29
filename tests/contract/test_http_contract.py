import json
from urllib.request import Request, urlopen


def test_health_contract(live_server):
    with urlopen(f"{live_server}/health", timeout=3) as response:
        assert response.status == 200
        assert json.loads(response.read()) == {"status": "ok"}


def test_create_job_contract(live_server):
    request = Request(
        f"{live_server}/jobs",
        data=json.dumps({"fileName": "contract.mp4", "contentType": "video/mp4"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        body = json.loads(response.read())
        assert response.status == 202
        assert body["status"] == "QUEUED"
        assert body["objectKey"].startswith("media/")
