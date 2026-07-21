"""v2 kanban column creation. No real network: `_request` is monkeypatched so
we can assert the endpoint, payload shape, id2error handling, and sortOrder."""
import pytest

from ticktick_mcp.src.ticktick_v2_client import (
    COLUMN_SORT_STEP,
    TickTickV2Client,
)


@pytest.fixture
def client():
    c = TickTickV2Client(token="tok")
    # inboxId encodes the owner's numeric userId as 'inbox<userId>'.
    c.inbox_id = "inbox130208689"
    return c


def _stub_request(client, existing_columns, post_response):
    """Route GET /column/project/... to canned columns and POST /column to a
    canned batch response, recording the POST body for assertions."""
    calls = {}

    def fake(method, path, **kwargs):
        if method == "GET" and path.startswith("/column/project/"):
            return existing_columns
        if method == "POST" and path == "/column":
            calls["post"] = (path, kwargs.get("json"))
            return post_response
        raise AssertionError(f"unexpected call {method} {path}")

    client._request = fake
    return calls


def test_create_column_posts_expected_payload(client):
    calls = _stub_request(client, existing_columns=[],
                          post_response={"id2etag": {}, "id2error": {}})
    cid = client.create_column("proj1", "Doing")

    path, body = calls["post"]
    assert path == "/column"
    assert body["update"] == [] and body["delete"] == []
    (col,) = body["add"]
    assert col["id"] == cid
    assert col["name"] == "Doing"
    assert col["projectId"] == "proj1"
    assert col["userId"] == 130208689
    # first column in an empty project sits at 0
    assert col["sortOrder"] == 0


def test_create_column_appends_after_existing(client):
    calls = _stub_request(client,
                          existing_columns=[{"id": "a", "sortOrder": 0},
                                            {"id": "b", "sortOrder": COLUMN_SORT_STEP}],
                          post_response={"id2etag": {}, "id2error": {}})
    client.create_column("proj1", "Done")
    _, body = calls["post"]
    assert body["add"][0]["sortOrder"] == COLUMN_SORT_STEP * 2


def test_create_column_returns_client_generated_id(client):
    _stub_request(client, existing_columns=[],
                  post_response={"id2etag": {}, "id2error": {}})
    cid = client.create_column("proj1", "Backlog")
    assert isinstance(cid, str) and len(cid) == 24


def test_create_column_raises_on_id2error(client):
    def fake(method, path, **kwargs):
        if method == "GET":
            return []
        # Echo the failing column's own id back in id2error.
        cid = kwargs["json"]["add"][0]["id"]
        return {"id2etag": {}, "id2error": {cid: "PROJECT_NOT_FOUND"}}

    client._request = fake
    with pytest.raises(RuntimeError, match="PROJECT_NOT_FOUND"):
        client.create_column("bad", "Nope")
