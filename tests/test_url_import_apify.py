"""Shopee product import via Apify — NO live network.

Shopee blocks direct/headless fetches, so ``fetch_product_page`` routes
Shopee hosts through an Apify actor (src/apify_client.py, a port of
sellcenter's ApifyRunService.js).  Proves:

  - Shopee URLs never hit the static/browser path; other hosts never hit Apify
  - the actor record is mapped into the SAME result contract as the HTML path
  - CDN image URLs the actor returns still go through the SSRF-validated fetcher
  - the run's REAL cost is recorded (success and error paths) via ai_usage
  - run polling / failure / timeout / missing-token behaviour of the client
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

from src import ai_usage, apify_client, url_import
from src.url_import import UrlImportError

FIXTURE = Path(__file__).parent / "fixtures" / "apify_shopee_item.json"
SHOPEE_URL = "https://shopee.co.th/--i.1191420560.43332033245"
ACTOR = url_import.APIFY_SHOPEE_ACTOR_DEFAULT
PUBLIC_IP = "93.184.216.34"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _item() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))[0]


def _dns_map(mapping: dict[str, str]):
    orig = socket.getaddrinfo

    def fake(host, port=0, *args, **kwargs):
        if host in mapping:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port or 443))]
        return orig(host, port, *args, **kwargs)

    return fake


def _run(status="SUCCEEDED", *, run_id="run1", cost=0.00005, message=None) -> dict:
    d = {"id": run_id, "actId": "act1", "status": status, "defaultDatasetId": "ds1",
         "usageTotalUsd": cost, "startedAt": "2026-09-18T06:00:00.000Z"}
    if message:
        d["statusMessage"] = message
    return d


def _apify_transport(*, start_run, poll_runs=(), items=None, calls=None, final_run=None):
    """MockTransport for api.apify.com. ``poll_runs`` are consumed in order;
    once exhausted, GET actor-runs answers ``final_run`` (default: the last
    known run) — that is the post-dataset cost re-read."""
    polls = list(poll_runs)
    calls = calls if calls is not None else []
    last = {"run": start_run}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers.get("authorization")))
        path = request.url.path
        if request.method == "POST" and path == f"/v2/acts/{ACTOR}/runs":
            return httpx.Response(201, json={"data": start_run})
        if request.method == "GET" and path.startswith("/v2/actor-runs/"):
            if polls:
                last["run"] = polls.pop(0)
                return httpx.Response(200, json={"data": last["run"]})
            return httpx.Response(200, json={"data": final_run or last["run"]})
        if request.method == "GET" and path == "/v2/datasets/ds1/items":
            return httpx.Response(200, json=items if items is not None else [])
        return httpx.Response(404, content=b"nope")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _image_client(calls: list[str], *, status=200, ctype="image/jpeg") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, content=JPEG, headers={"content-type": ctype})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def apify_env(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "test-token")
    monkeypatch.delenv("APIFY_SHOPEE_ACTOR", raising=False)
    monkeypatch.delenv("APIFY_SHOPEE_PROXY_GROUP", raising=False)
    monkeypatch.setattr(url_import, "APIFY_RETRY_SLEEP_S", 0)
    monkeypatch.setattr(socket, "getaddrinfo", _dns_map({
        "shopee.co.th": PUBLIC_IP,
        "down-sg.img.susercontent.com": PUBLIC_IP,
    }))
    monkeypatch.setattr(url_import, "_browser_fetch",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser must not run")))


def _usage_entries() -> list[dict]:
    p = ai_usage.usage_log_path()
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,region", [
    ("https://shopee.co.th/x-i.1.2", "th"),
    ("https://www.shopee.co.th/product/1/2", "th"),
    ("https://shopee.sg/product/1/2", "sg"),
    ("https://shopee.com.my/x-i.1.2", "my"),
    ("https://example.com/shopee.co.th", None),
    ("https://notshopee.co.th/x", None),
    ("https://shopee.co.th.evil.com/x", None),
])
def test_shopee_region_detection(url, region):
    assert url_import._shopee_region(url) == region


def test_non_shopee_url_never_touches_apify(monkeypatch):
    monkeypatch.setattr(apify_client, "run_actor_and_get_items",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("apify must not run")))
    monkeypatch.setattr(socket, "getaddrinfo", _dns_map({"shop.example": PUBLIC_IP}))
    html = b"<html><head><title>T</title></head><body>" + b"<p>product text</p>" * 40 + b"</body></html>"
    page = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=html, headers={"content-type": "text/html"})))
    monkeypatch.setattr(url_import, "_make_client", lambda: page)
    result = url_import.fetch_product_page("https://shop.example/p/1")
    assert result["fetched_via"] == "static"


def test_shopee_without_token_is_a_controlled_error(monkeypatch, apify_env):
    monkeypatch.setattr(apify_client, "apify_token", lambda: None)
    monkeypatch.setattr(apify_client, "_make_client",
                        lambda: (_ for _ in ()).throw(AssertionError("no network without token")))
    with pytest.raises(UrlImportError) as ei:
        url_import.fetch_product_page(SHOPEE_URL)
    assert ei.value.reason == "apify_not_configured"
    assert "APIFY_TOKEN" in str(ei.value)


# ---------------------------------------------------------------------------
# Happy path — contract mapping
# ---------------------------------------------------------------------------

def test_shopee_import_maps_actor_record_to_result_contract(monkeypatch, apify_env):
    api_calls: list = []
    monkeypatch.setattr(apify_client, "_make_client",
                        lambda: _apify_transport(start_run=_run(), items=[_item()], calls=api_calls))
    img_calls: list[str] = []
    monkeypatch.setattr(url_import, "_make_client", lambda: _image_client(img_calls))

    result = url_import.fetch_product_page(SHOPEE_URL)

    # Actor was called exactly as sellcenter does (+ one final cost re-read),
    # with a bearer token, and the page itself was never fetched by us.
    assert [m for m, _u, _a in api_calls] == ["POST", "GET", "GET"]
    assert all(a == "Bearer test-token" for _m, _u, a in api_calls)
    assert all("token=" not in u for _m, u, _a in api_calls)
    assert all(u.startswith("https://down-sg.img.susercontent.com/") for u in img_calls)

    assert result["fetched_via"] == "apify"
    assert result["page_class"] == "single"
    assert result["page_signals"]["product_count"] == 1
    assert result["page_signals"]["multi_signals"] == []
    assert result["page_signals"]["provider"] == "apify"
    assert result["page_signals"]["actor"] == ACTOR
    assert result["original_url"] == SHOPEE_URL
    assert result["final_url"] == "https://shopee.co.th/product/1191420560/43332033245"
    assert result["canonical_url"] == result["final_url"]
    assert result["page_title"].startswith("(NewArrival) BLACK SHARK RUN")
    assert result["fetched_at"]

    text = result["text"]
    assert text.startswith("Title: (NewArrival) BLACK SHARK RUN")
    desc_line = next(l for l in text.splitlines() if l.startswith("Description: "))
    assert "=====" not in desc_line and "รบกวนลูกค้า" in desc_line
    assert "Source: https://shopee.co.th/product/1191420560/43332033245" in text
    # facts are two-cell `label | value` lines — what extract_source_facts reads
    assert "Brand | Black Shark(แบล็ค ชาร์ค)" in text
    assert "Shop | Black Shark Thailand (rating 4.89/5, 21182 ratings, 48646 followers, official shop)" in text
    assert "Ships from | จังหวัดสมุทรปราการ" in text
    assert "Category | มือถือและอุปกรณ์เสริม > อุปกรณ์สวมใส่ > สมาร์ทวอทช์และอุปกรณ์ฟิตเนส" in text
    assert "Condition | new" in text and "Availability | in stock" in text
    assert "Variants (9):" in text and "ตัวเลือกสินค้า | Orange, Black, Yellow" in text
    assert "Specifications:" in text and "Item Type | Smartwatch" in text
    # promotions expire → bullets (evidence), never pipe facts
    assert "Vouchers:" in text and "- BLAC09RUN: 400 off (min spend 2990)" in text
    assert "Product description:" in text and "[[ จุดเด่นสินค้า ]]" in text
    assert len(text) <= url_import.MAX_TEXT_CHARS

    # The deterministic fact extractor turns those lines into derived_facts.
    from src.ingestion import extract_source_facts, _fact_key
    facts = extract_source_facts(text, [{"file": "source_page.txt", "text": text}])
    assert facts["brand"] == {"label": "Brand", "value": "Black Shark(แบล็ค ชาร์ค)",
                              "source_file": "source_page.txt"}
    assert facts["item_type"]["value"] == "Smartwatch"
    assert facts[_fact_key("ตัวเลือกสินค้า")]["value"].startswith("Orange, Black, Yellow")
    assert not any("BLAC09RUN" in f["value"] for f in facts.values())

    # main image + gallery, deduped, same naming as the HTML path
    assert [i["name"] for i in result["images"]] == ["page_img_001.jpg", "page_img_002.jpg", "page_img_003.jpg"]
    assert all(i["content"] == JPEG for i in result["images"])
    assert result["images"][0]["source_url"] == _item()["mainImageUrl"]


def test_pipe_in_actor_values_cannot_forge_extra_fact_cells():
    item = _item()
    item["attributes"] = [{"name": "Weight | Net", "value": "30 g | approx"}]
    text = url_import._shopee_item_text(item, SHOPEE_URL)
    assert "Weight / Net | 30 g / approx" in text
    from src.ingestion import extract_source_facts
    facts = extract_source_facts(text, [])
    assert facts["weight_net"]["value"] == "30 g / approx"


def test_shopee_import_records_real_apify_cost(monkeypatch, apify_env):
    monkeypatch.setattr(apify_client, "_make_client",
                        lambda: _apify_transport(start_run=_run(cost=0.00005, run_id="runX"), items=[_item()]))
    monkeypatch.setattr(url_import, "_make_client", lambda: _image_client([]))

    url_import.fetch_product_page(SHOPEE_URL)

    entries = [e for e in _usage_entries() if e.get("provider") == "apify"]
    assert len(entries) == 1
    e = entries[0]
    assert e["model"] == ACTOR
    assert e["operation"] == "actor.run"
    assert e["source"] == "url_import.shopee"
    assert e["status"] == "success"
    assert e["request_id"] == "runX"
    assert e["cost_usd"] == pytest.approx(0.00005)
    assert e["units"] == {"items_fetched": 1}
    assert e["metadata"]["platform"] == "shopee"
    assert e["metadata"]["url"] == SHOPEE_URL
    assert e["raw_usage"]["usageTotalUsd"] == pytest.approx(0.00005)
    assert e["duration_ms"] >= 0


def test_actor_override_via_env(monkeypatch, apify_env):
    monkeypatch.setenv("APIFY_SHOPEE_ACTOR", "someone~other-shopee-actor")
    seen: list[str] = []

    def fake_run(actor, payload, *, timeout_s):
        seen.append(actor)
        assert payload == {
            "products": [SHOPEE_URL], "region": "th", "includeShop": True,
            "includeDescription": True, "maxItems": 1,
            # residential exit in the storefront's country by default
            "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"],
                                   "apifyProxyCountry": "TH"},
        }
        return apify_client.ApifyRunResult(items=[_item()], cost_usd=0.0, run_id="r", run=_run())

    monkeypatch.setattr(apify_client, "run_actor_and_get_items", fake_run)
    monkeypatch.setattr(url_import, "_make_client", lambda: _image_client([]))
    result = url_import.fetch_product_page(SHOPEE_URL)
    assert seen == ["someone~other-shopee-actor"]
    assert result["page_signals"]["actor"] == "someone~other-shopee-actor"


def test_proxy_group_env_overrides_and_empty_disables(monkeypatch, apify_env):
    payloads: list[dict] = []

    def fake_run(actor, payload, *, timeout_s):
        payloads.append(payload)
        return apify_client.ApifyRunResult(items=[_item()], cost_usd=0.0, run_id="r", run=_run())

    monkeypatch.setattr(apify_client, "run_actor_and_get_items", fake_run)
    monkeypatch.setattr(url_import, "_make_client", lambda: _image_client([]))

    monkeypatch.setenv("APIFY_SHOPEE_PROXY_GROUP", "SHADER")
    url_import.fetch_product_page("https://shopee.sg/product/1/2")
    assert payloads[-1]["proxyConfiguration"] == {
        "useApifyProxy": True, "apifyProxyGroups": ["SHADER"], "apifyProxyCountry": "SG"}

    monkeypatch.setenv("APIFY_SHOPEE_PROXY_GROUP", "")
    url_import.fetch_product_page(SHOPEE_URL)
    assert "proxyConfiguration" not in payloads[-1]


# ---------------------------------------------------------------------------
# Images keep the SSRF / MIME policy
# ---------------------------------------------------------------------------

def test_cdn_images_resolving_to_private_ip_are_skipped_not_fatal(monkeypatch, apify_env):
    monkeypatch.setattr(socket, "getaddrinfo", _dns_map({
        "shopee.co.th": PUBLIC_IP,
        "down-sg.img.susercontent.com": "10.0.0.5",
    }))
    monkeypatch.setattr(apify_client, "_make_client",
                        lambda: _apify_transport(start_run=_run(), items=[_item()]))
    img_calls: list[str] = []
    monkeypatch.setattr(url_import, "_make_client", lambda: _image_client(img_calls))

    result = url_import.fetch_product_page(SHOPEE_URL)
    assert result["images"] == []
    assert img_calls == []  # rejected before any connect
    assert result["text"].startswith("Title:")


def test_non_image_mime_from_cdn_is_skipped(monkeypatch, apify_env):
    monkeypatch.setattr(apify_client, "_make_client",
                        lambda: _apify_transport(start_run=_run(), items=[_item()]))
    monkeypatch.setattr(url_import, "_make_client",
                        lambda: _image_client([], ctype="text/html"))
    result = url_import.fetch_product_page(SHOPEE_URL)
    assert result["images"] == []


def test_image_count_capped(monkeypatch, apify_env):
    item = _item()
    item["imageUrls"] = [f"https://down-sg.img.susercontent.com/file/img{i}" for i in range(30)]
    monkeypatch.setattr(apify_client, "_make_client",
                        lambda: _apify_transport(start_run=_run(), items=[item]))
    monkeypatch.setattr(url_import, "_make_client", lambda: _image_client([]))
    result = url_import.fetch_product_page(SHOPEE_URL)
    assert len(result["images"]) == url_import.MAX_IMAGES


# ---------------------------------------------------------------------------
# Actor failure modes -> controlled UrlImportError + cost still logged
# ---------------------------------------------------------------------------

NOT_FOUND_ROW = {"error": "PRODUCT_NOT_FOUND",
                 "errorMessage": "Shopee returned no product data for 1191420560/43332033245 (th)",
                 "itemId": 43332033245, "shopId": 1191420560}


def test_product_not_found_row_is_retried_then_reported(monkeypatch, apify_env):
    runs: list[str] = []
    monkeypatch.setattr(apify_client, "_make_client", lambda: (
        runs.append("run") or _apify_transport(start_run=_run(run_id=f"r{len(runs)}"),
                                               items=[NOT_FOUND_ROW])))
    with pytest.raises(UrlImportError) as ei:
        url_import.fetch_product_page(SHOPEE_URL)
    assert ei.value.reason == "product_not_found"
    assert "Shopee returned no product data" in ei.value.detail
    assert len(runs) == url_import.APIFY_SHOPEE_ATTEMPTS == 3
    entries = [e for e in _usage_entries() if e.get("provider") == "apify"]
    assert [e["attempt"] for e in entries] == [1, 2, 3]
    assert all(e["units"] == {"items_fetched": 0} for e in entries)
    assert all("no product data" in e["error_message"] for e in entries)


def test_flaky_actor_succeeds_on_retry(monkeypatch, apify_env):
    """Observed live: the same listing errors on one run and succeeds seconds later."""
    runs: list[str] = []

    def factory():
        runs.append("run")
        items = [NOT_FOUND_ROW] if len(runs) == 1 else [_item()]
        return _apify_transport(start_run=_run(run_id=f"r{len(runs)}"), items=items)

    monkeypatch.setattr(apify_client, "_make_client", factory)
    monkeypatch.setattr(url_import, "_make_client", lambda: _image_client([]))
    result = url_import.fetch_product_page(SHOPEE_URL)
    assert result["page_title"].startswith("(NewArrival) BLACK SHARK RUN")
    assert len(runs) == 2
    entries = [e for e in _usage_entries() if e.get("provider") == "apify"]
    assert [(e["attempt"], e["units"]["items_fetched"], e["request_id"]) for e in entries] == [
        (1, 0, "r1"), (2, 1, "r2")]
    assert entries[1].get("error_message") is None


def test_empty_dataset_is_product_not_found(monkeypatch, apify_env):
    monkeypatch.setattr(apify_client, "_make_client",
                        lambda: _apify_transport(start_run=_run(), items=[]))
    with pytest.raises(UrlImportError) as ei:
        url_import.fetch_product_page(SHOPEE_URL)
    assert ei.value.reason == "product_not_found"


def test_failed_run_maps_to_fetch_failed_and_logs_error_cost(monkeypatch, apify_env):
    monkeypatch.setattr(apify_client, "_make_client", lambda: _apify_transport(
        start_run=_run("FAILED", cost=0.001, run_id="bad", message="actor crashed")))
    with pytest.raises(UrlImportError) as ei:
        url_import.fetch_product_page(SHOPEE_URL)
    assert ei.value.reason == "fetch_failed"
    assert "actor crashed" in ei.value.detail
    assert str(ei.value) == UrlImportError._MESSAGES["fetch_failed"]  # no internals leak

    entries = [e for e in _usage_entries() if e.get("provider") == "apify"]
    assert len(entries) == 1
    assert entries[0]["status"] == "error"
    assert entries[0]["cost_usd"] == pytest.approx(0.001)
    assert entries[0]["request_id"] == "bad"
    assert "actor crashed" in entries[0]["error_message"]


def test_http_error_from_apify_logs_http_status(monkeypatch, apify_env):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": {"message": "unauthorized"}})))
    monkeypatch.setattr(apify_client, "_make_client", lambda: client)
    with pytest.raises(UrlImportError) as ei:
        url_import.fetch_product_page(SHOPEE_URL)
    assert ei.value.reason == "fetch_failed"
    entries = [e for e in _usage_entries() if e.get("provider") == "apify"]
    assert entries and entries[0]["status"] == "error"
    assert entries[0]["http_status"] == 401
    assert "request_id" not in entries[0]


# ---------------------------------------------------------------------------
# apify_client — polling / terminal handling
# ---------------------------------------------------------------------------

def test_client_polls_until_terminal_then_reads_dataset(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "t")
    calls: list = []
    monkeypatch.setattr(apify_client, "_make_client", lambda: _apify_transport(
        start_run=_run("RUNNING"), poll_runs=[_run("RUNNING"), _run("SUCCEEDED", cost=0.002)],
        items=[{"a": 1}], calls=calls))
    res = apify_client.run_actor_and_get_items(ACTOR, {"x": 1}, timeout_s=30)
    assert res.items == [{"a": 1}]
    assert res.cost_usd == pytest.approx(0.002)
    assert res.run_id == "run1"
    paths = [httpx.URL(u).path for _m, u, _a in calls]
    assert paths == [f"/v2/acts/{ACTOR}/runs", "/v2/actor-runs/run1", "/v2/actor-runs/run1",
                     "/v2/datasets/ds1/items", "/v2/actor-runs/run1"]
    assert all("waitForFinish=60" in u for _m, u, _a in calls[:3])
    assert "clean=true" in calls[3][1]
    assert "waitForFinish" not in calls[4][1]  # final cost re-read, no wait


def test_client_reports_finalized_cost_after_dataset_read(monkeypatch):
    """Observed live: usageTotalUsd is $0.00005 when the run turns SUCCEEDED
    and $0.00405 a moment later once residential proxy bandwidth is billed."""
    monkeypatch.setenv("APIFY_TOKEN", "t")
    monkeypatch.setattr(apify_client, "_make_client", lambda: _apify_transport(
        start_run=_run(cost=0.00005), items=[{"a": 1}], final_run=_run(cost=0.00405)))
    res = apify_client.run_actor_and_get_items(ACTOR, {}, timeout_s=30)
    assert res.cost_usd == pytest.approx(0.00405)
    assert res.run["usageTotalUsd"] == pytest.approx(0.00405)


def test_client_final_cost_reread_failure_is_not_fatal(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "t")
    state = {"gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"data": _run(cost=0.001)})
        if request.url.path == "/v2/datasets/ds1/items":
            return httpx.Response(200, json=[{"a": 1}])
        state["gets"] += 1
        return httpx.Response(500, content=b"boom")

    monkeypatch.setattr(apify_client, "_make_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    res = apify_client.run_actor_and_get_items(ACTOR, {}, timeout_s=30)
    assert res.items == [{"a": 1}] and res.cost_usd == pytest.approx(0.001)
    assert state["gets"] == 1


def test_client_timeout_carries_partial_cost(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "t")
    monkeypatch.setattr(apify_client, "_make_client", lambda: _apify_transport(
        start_run=_run("RUNNING", cost=0.0004), poll_runs=[_run("RUNNING")] * 5))
    with pytest.raises(apify_client.ApifyError) as ei:
        apify_client.run_actor_and_get_items(ACTOR, {}, timeout_s=0)
    assert ei.value.reason == "timeout"
    assert ei.value.cost_usd == pytest.approx(0.0004)
    assert ei.value.run["id"] == "run1"


def test_client_missing_token(monkeypatch):
    monkeypatch.setattr(apify_client, "apify_token", lambda: None)
    with pytest.raises(apify_client.ApifyError) as ei:
        apify_client.run_actor_and_get_items(ACTOR, {})
    assert ei.value.reason == "no_token"


def test_client_transport_error(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "t")
    monkeypatch.setattr(apify_client, "_make_client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))))
    with pytest.raises(apify_client.ApifyError) as ei:
        apify_client.run_actor_and_get_items(ACTOR, {})
    assert ei.value.reason == "transport_error"
