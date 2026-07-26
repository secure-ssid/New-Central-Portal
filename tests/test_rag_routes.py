"""Coverage for the RAG / doc-search routes — previously smoke-only.

These render local-doc search results and Q&A. The handlers do real work
(HTML/markdown stripping, truncation, error-envelope handling, graceful
degradation when the index is missing) that page-renders smoke never exercised.
"""


def test_rag_strips_html_and_markdown_from_results(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def search_docs(query, top_k=8):
        return [{
            "text": "<!-- hidden --><p>## Heading</p>\n**bold** and [a link](http://x) plain",
            "file_path": "docs/aruba/guide.md", "score": 0.912, "source": "lancedb",
        }]

    monkeypatch.setattr(cb, "search_docs", search_docs)
    r = client.post("/lab/rag", data={"query": "vlan"})
    assert r.status_code == 200
    assert "guide.md" in r.text
    # Raw markup must be gone; the readable text survives.
    assert "<!--" not in r.text and "<p>" not in r.text
    assert "**" not in r.text and "](http" not in r.text
    assert "bold" in r.text and "plain" in r.text


def test_rag_surfaces_an_error_envelope(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def search_docs(query, top_k=8):
        return [{"error": "RAG index not built"}]

    monkeypatch.setattr(cb, "search_docs", search_docs)
    r = client.post("/lab/rag", data={"query": "x"})
    assert r.status_code == 200
    assert "RAG index not built" in r.text


def test_rag_degrades_gracefully_when_search_raises(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def boom(*a, **k):
        raise RuntimeError("lancedb gone")

    monkeypatch.setattr(cb, "search_docs", boom)
    r = client.post("/lab/rag", data={"query": "x"})
    assert r.status_code == 200
    assert "unavailable" in r.text
    assert "lancedb gone" not in r.text          # internal error not leaked


def test_doc_api_skips_non_dict_rows(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def lookup_api(query, top_k=10):
        return ["junk", {"path": "/monitoring/v1/aps", "text": "AP monitoring endpoint",
                         "score": 0.8}]

    monkeypatch.setattr(cb, "lookup_api", lookup_api)
    r = client.post("/lab/doc-api", data={"query": "aps"})
    assert r.status_code == 200
    assert "/monitoring/v1/aps" in r.text


def test_doc_ask_renders_answer_and_citations(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def ask_docs(question, top_k=3, source=None):
        return {"answer": "Bind the SSID to VLAN 200.",
                "citations": [{"file_path": "docs/wlan.md"}], "mode": "lancedb"}

    monkeypatch.setattr(cb, "ask_docs", ask_docs)
    r = client.post("/lab/doc-ask", data={"question": "which vlan?"})
    assert r.status_code == 200
    assert "Bind the SSID to VLAN 200." in r.text


def test_doc_ask_empty_answer_says_not_found(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def ask_docs(question, top_k=3, source=None):
        return {"answer": "   ", "citations": [], "mode": "lancedb"}

    monkeypatch.setattr(cb, "ask_docs", ask_docs)
    r = client.post("/lab/doc-ask", data={"question": "x"})
    assert r.status_code == 200
    assert "No answer found" in r.text


def test_doc_ask_degrades_gracefully_when_it_raises(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def boom(*a, **k):
        raise RuntimeError("index error")

    monkeypatch.setattr(cb, "ask_docs", boom)
    r = client.post("/lab/doc-ask", data={"question": "x"})
    assert r.status_code == 200
    assert "unavailable" in r.text
    assert "index error" not in r.text
