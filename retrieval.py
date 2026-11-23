"""Local retrieval primitives. The HTTP client never uses a network proxy."""
import json
import os
import re
from urllib.request import Request, build_opener, ProxyHandler

LOCAL = build_opener(ProxyHandler({}))
XERJ_PORT = int(os.getenv("WIKI_XERJ_PORT", "19297"))
XERJ = f"http://127.0.0.1:{XERJ_PORT}"


def json_request(url, payload=None, *, method=None, ndjson=False, timeout=120):
    data = payload if ndjson else None if payload is None else json.dumps(payload).encode()
    req = Request(url, data, method=method, headers={
        "Content-Type": "application/x-ndjson" if ndjson else "application/json"})
    with LOCAL.open(req, timeout=timeout) as response:
        return json.load(response)


def terms(query):
    words = dict.fromkeys(re.findall(r"[^\W_]+", query.casefold()))
    return " OR ".join(f'"{w}"' for w in list(words)[:80])


def create_index(index):
    props = {"content": {"type": "text"}, "vec": {"type": "dense_vector", "dims": 384,
             "index": True, "similarity": "cosine"}}
    json_request(f"{XERJ}/{index}", {"mappings": {"properties": props}}, method="PUT")


def bulk(index, rows):
    lines = []
    for key, content, vec in rows:
        lines.extend([json.dumps({"index": {"_index": index, "_id": key}}),
                      json.dumps({"content": content, "vec": vec.tolist()})])
    ack = json_request(f"{XERJ}/_bulk", ("\n".join(lines)+"\n").encode(), ndjson=True)
    if ack.get("errors") or len(ack.get("items", [])) != len(rows):
        raise RuntimeError(f"Bulk indexing failed: {str(ack)[:600]}")


def xhits(index, query, limit):
    result = json_request(f"{XERJ}/{index}/_search", {"size": limit, "_source": False, "query": query})
    return [h["_id"] for h in result["hits"]["hits"]]


def xerj_search(index, text, encoder=None, limit=5):
    depth = 30 if encoder else limit
    lexical = xhits(index, {"match": {"content": text}}, depth)
    if encoder is None:
        return lexical
    vec = encoder.encode([text], "query: ")[0]
    dense = xhits(index, {"knn": {"field": "vec", "query_vector": vec.tolist(), "k": depth}}, depth)
    scores = {}
    for ranking in (lexical, dense):
        for rank, key in enumerate(ranking, 1):
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))[:limit]
