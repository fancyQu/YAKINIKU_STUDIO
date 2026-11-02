#!/usr/bin/env python3
"""Run only on loopback; capture audio from an Edge tab via the local UI."""
import argparse
import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="直播烤肉台")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run("yakiniku.server:app", host="127.0.0.1", port=args.port, access_log=False)
