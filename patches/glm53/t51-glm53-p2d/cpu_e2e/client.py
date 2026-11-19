"""Scenario client for one vLLM OpenAI server (or the P/D proxy), standard library only.

Usage: python3 client.py <base url> <model name> <out.json>
Scenarios, run one after another (n = 1 at temperature 0, n = 2 at temperature 0.8 with seed 1234, so text is
comparable before and after the patch):
  completions / chat x stream / non-stream x n = 1 / 2; four completions at once; one streamed completion the
  client abandons after two chunks (the connection is closed, so the server aborts the request). With the simulated
  site patches (P2D_SITE_PATCHES=1), also one n = 1 and one n = 2 completion whose prompt fails the length check,
  then one normal completion.
Each result keeps the HTTP status, the text and finish_reason per choice index, usage, the number of SSE chunks
and whether [DONE] arrived.
"""

import http.client
import json
import os
import sys
import threading
import time
import urllib.parse

base, model, out_path = sys.argv[1:4]
url = urllib.parse.urlparse(base)


def connect():
    return http.client.HTTPConnection(url.hostname, url.port, timeout=300)


def body(endpoint, stream, n, max_tokens=12):
    # vLLM rejects n > 1 with greedy sampling, so n = 2 samples at temperature 0.8 with a fixed seed
    sampling = {"temperature": 0} if n == 1 else {"temperature": 0.8, "seed": 1234}
    common = {"model": model, "max_tokens": max_tokens, "n": n, "stream": stream, **sampling}
    if stream:
        common["stream_options"] = {"include_usage": True}
    if endpoint == "completions":
        return dict(common, prompt="hello world test request token")
    return dict(common, messages=[{"role": "user", "content": "hello world test"}])


def call(endpoint, payload, abort_after=None):
    conn = connect()
    conn.request("POST", f"/v1/{'completions' if endpoint == 'completions' else 'chat/completions'}",
                 json.dumps(payload), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    result = {"status": resp.status, "texts": {}, "finish": {}, "usage": None, "chunks": 0, "done": False}
    if not payload.get("stream"):
        data = json.loads(resp.read() or b"{}")
        for choice in data.get("choices", []):
            text = choice.get("text") if endpoint == "completions" else (choice.get("message") or {}).get("content")
            result["texts"][str(choice["index"])] = text
            result["finish"][str(choice["index"])] = choice.get("finish_reason")
        result["usage"] = data.get("usage")
        if resp.status != 200:
            result["error"] = data
        conn.close()
        return result
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith(b"data: "):
            continue
        payload_line = line[6:]
        if payload_line == b"[DONE]":
            result["done"] = True
            break
        chunk = json.loads(payload_line)
        result["chunks"] += 1
        for choice in chunk.get("choices", []):
            key = str(choice["index"])
            piece = choice.get("text") if endpoint == "completions" else (choice.get("delta") or {}).get("content")
            result["texts"][key] = result["texts"].get(key, "") + (piece or "")
            if choice.get("finish_reason"):
                result["finish"][key] = choice["finish_reason"]
        if chunk.get("usage"):
            result["usage"] = chunk["usage"]
        if abort_after is not None and result["chunks"] >= abort_after:
            result["aborted_by_client"] = True
            conn.sock.close()
            break
    conn.close()
    return result


results = {}
for endpoint in ("completions", "chat"):
    for stream in (False, True):
        for n in (1, 2):
            name = f"{endpoint}_{'stream' if stream else 'plain'}_n{n}"
            results[name] = call(endpoint, body(endpoint, stream, n))
concurrent = {}


def one(k):
    concurrent[k] = call("completions", body("completions", False, 1))


threads = [threading.Thread(target=one, args=(k,)) for k in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
results["completions_concurrent_4"] = {"status": sorted({r["status"] for r in concurrent.values()}),
                                       "texts": {str(k): concurrent[k]["texts"].get("0") for k in sorted(concurrent)}}
abort_payload = dict(body("completions", True, 1, max_tokens=400), ignore_eos=True)
results["completions_stream_aborted"] = call("completions", abort_payload, abort_after=2)
time.sleep(2)  # let the server notice the closed connection and abort
results["after_abort_completions_plain_n1"] = call("completions", body("completions", False, 1))
if os.environ.get("P2D_SITE_PATCHES") == "1":
    # prompts of 200 tokens, above the simulated site length check (P2D_SITE_MAX_PROMPT=64) and below max_model_len
    for n in (1, 2):
        results[f"completions_plain_n{n}_too_long"] = call(
            "completions", dict(body("completions", False, n), prompt="hello world test request token " * 40))
    results["after_reject_completions_plain_n1"] = call("completions", body("completions", False, 1))
json.dump(results, open(out_path, "w"), indent=1, ensure_ascii=False)
bad = [k for k, v in results.items() if (v["status"] if isinstance(v["status"], int) else max(v["status"])) != 200]
print("scenarios", len(results), "non-200", bad)
