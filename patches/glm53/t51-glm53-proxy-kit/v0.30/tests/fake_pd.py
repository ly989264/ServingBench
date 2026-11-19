#!/usr/bin/env python3
"""A fake P or D vLLM server for the proxy tests: answers /v1/chat/completions and /v1/completions and logs every
request it receives (headers that matter, body) as one JSON line to $FAKE_LOG.

Usage: FAKE_ROLE=P|D FAKE_LOG=<file> python fake_pd.py <port>

What it does is steered by the request body's "fake" object, which the proxy forwards to P and D unchanged:
  case          an id for this request (attempt counters are kept per case)
  p_status      list of HTTP statuses P returns on its 1st, 2nd, ... attempt (default 200)
  d_status      the same for D (a status >= 400 is returned before any chunk)
  d_chunks      number of content chunks D sends (default 3)
  d_chunk_ms    pause before each chunk (default 5)
  d_recompute   D's first stream for this case ends with stop_reason "recomputed" after one chunk
  d_drop_after  D breaks the connection after this many chunks (on every attempt)
"""
import asyncio
import json
import os
import sys
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

ROLE = os.environ["FAKE_ROLE"]
LOG = os.environ["FAKE_LOG"]
app = FastAPI()
attempts: dict = {}


def log(path, request: Request, body):
    row = {"role": ROLE, "port": int(sys.argv[1]), "path": path, "x_request_id": request.headers.get("x-request-id"),
           "authorization": request.headers.get("authorization"), "body": body, "t": time.time()}
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def nth(spec, case, key):
    n = attempts.get((case, key), 0)
    attempts[(case, key)] = n + 1
    return n


def usage(n_out):
    return {"prompt_tokens": 7, "completion_tokens": n_out, "total_tokens": 7 + n_out,
            "prompt_tokens_details": {"cached_tokens": 3}}


async def handle(path, request: Request):
    body = await request.json()
    log(path, request, body)
    fake = body.get("fake") or {}
    case = fake.get("case", "")
    rid = request.headers.get("x-request-id") or "none"
    if ROLE == "P":
        i = nth(fake, case, "p")
        codes = fake.get("p_status") or [200]
        code = codes[min(i, len(codes) - 1)]
        if code >= 400:
            return JSONResponse({"error": {"message": "fake P error %d" % code}}, status_code=code)
        return JSONResponse({"id": "cmpl-" + rid, "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "length"}],
            "usage": usage(1), "kv_transfer_params": {"do_remote_prefill": True, "do_remote_decode": False,
                                                    "remote_engine_id": "fakeP", "remote_block_ids": [1, 2],
                                                    "remote_host": "127.0.0.1", "remote_port": 1}})
    i = nth(fake, case, "d")
    codes = fake.get("d_status") or [200]
    code = codes[min(i, len(codes) - 1)]
    if code >= 400:
        return JSONResponse({"error": {"message": "fake D error %d" % code}}, status_code=code)
    chunks = int(fake.get("d_chunks", 3))
    pause = float(fake.get("d_chunk_ms", 5)) / 1000
    recompute = bool(fake.get("d_recompute")) and i == 0
    drop = fake.get("d_drop_after")
    stream = bool(body.get("stream"))
    chat = path.endswith("chat/completions")

    def choice(text, stop_reason=None, finish=None):
        if chat:
            c = {"index": 0, "delta": {"content": text}} if stream else {"index": 0, "message": {"role": "assistant", "content": text}}
        else:
            c = {"index": 0, "text": text}
        c["finish_reason"] = finish
        c["stop_reason"] = stop_reason
        return c

    if not stream:
        await asyncio.sleep(pause * chunks)
        if recompute:
            return JSONResponse({"id": "cmpl-" + rid, "choices": [choice("r", "recomputed", "stop")], "usage": usage(1)})
        return JSONResponse({"id": "cmpl-" + rid, "choices": [choice("y" * chunks, None, "length")], "usage": usage(chunks)})

    async def gen():
        for k in range(chunks):
            await asyncio.sleep(pause)
            if drop is not None and k >= int(drop):
                raise RuntimeError("fake D drops the connection")
            last = recompute or k == chunks - 1
            c = choice("y", "recomputed" if recompute else None, ("stop" if recompute else "length") if last else None)
            yield ("data: " + json.dumps({"id": "cmpl-" + rid, "choices": [c]}) + "\n\n").encode()
            if recompute:
                break
        opts = body.get("stream_options") or {}
        if opts.get("include_usage"):
            yield ("data: " + json.dumps({"id": "cmpl-" + rid, "choices": [], "usage": usage(chunks)}) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def chat(request: Request):
    return await handle("/v1/chat/completions", request)


@app.post("/v1/completions")
async def completions(request: Request):
    return await handle("/v1/completions", request)


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "fake-model"}]}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
