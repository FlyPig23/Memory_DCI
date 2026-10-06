#!/usr/bin/env python3
"""Synthetic text and image requests through the local Codex inference facade."""
import base64
import json
from pathlib import Path
import struct
import sys
import zlib
import httpx
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import atomic_json
from experiment.benchmarks.wildclaw_bench.src.inference_access import inference_client


def blue_png():
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff)
    pixels = b"".join(b"\0" + b"\0\0\xff" * 32 for _ in range(32))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", 32, 32, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


def main():
    results = []
    prompts = [
        ("text", [{"role": "user", "content": "Return only JSON with the field answer equal to 4. What is 2+2?"}], 4),
        ("vision", [{"role": "user", "content": [
            {"type": "text", "text": "Identify the dominant color in the attached image. Return only JSON with field answer containing its uppercase English name."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(blue_png()).decode()}}]}], "BLUE")]
    for kind, messages, expected in prompts:
        with inference_client(ROOT, "synthetic-gateway-" + kind) as client:
            response = httpx.post("http://api.hangxiao.internal/v1/chat/completions",
                json={"model": "synthetic-requested-judge", "messages": messages, "response_format": {"type": "json_object"}},
                headers={"Authorization": "Bearer " + client.api_key}, proxy="http://127.0.0.1:18081",
                trust_env=False, timeout=660)
        body = response.json()
        result = {"kind": kind, "http_status": response.status_code, "response": body, "passed": False}
        if response.status_code == 200:
            content = json.loads(body["choices"][0]["message"]["content"])
            result["passed"] = content.get("answer") == expected
        results.append(result)
        atomic_json(ROOT / "experiment/benchmarks/wildclaw_bench/manifests/inference_gateway_smoke.json", {"results": results, "access_mode": "registered-private-client", "passed": len(results) == 2 and all(r["passed"] for r in results)})
        print(json.dumps({"kind": kind, "status": response.status_code, "passed": result["passed"]}), flush=True)
        if not result["passed"]:
            raise RuntimeError("Synthetic gateway request failed; inspect the saved private report")


if __name__ == "__main__":
    main()
