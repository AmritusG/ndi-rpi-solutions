#!/usr/bin/env python3
"""NDI Network Monitor — uses ndi_ctypes (no ndi-python needed)."""

import argparse, json, os, signal, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ndi_ctypes as ndi

running = True
def _sig(s, f): global running; running = False
signal.signal(signal.SIGINT, _sig); signal.signal(signal.SIGTERM, _sig)


def main():
    p = argparse.ArgumentParser(description="NDI Network Monitor")
    p.add_argument("--interval", type=int, default=5)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    if not ndi.initialize():
        print("[ERR] NDI init failed."); sys.exit(1)

    if not a.json:
        print("[i] NDI Network Monitor — Ctrl+C to stop.\n")

    known, scan = set(), 0
    while running:
        sources = ndi.discover_sources(timeout_ms=a.interval * 1000)
        scan += 1
        current = {s["name"] for s in sources}
        ts = time.strftime("%H:%M:%S")

        if a.json:
            print(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                              "scan": scan, "count": len(sources),
                              "sources": [{"name": s["name"]} for s in sources]}), flush=True)
        else:
            for name in current - known: print(f"  [{ts}] + NEW: {name}")
            for name in known - current: print(f"  [{ts}] - LOST: {name}")
            if scan == 1 or scan % 12 == 0:
                print(f"\n  [{ts}] Active ({len(sources)}):")
                for s in sources: print(f"    * {s['name']}")
                if not sources: print("    (none)")
                print()
        known = current
        time.sleep(max(0.1, a.interval - 1))

    ndi.destroy()
    if not a.json: print("\n[OK] Monitor stopped.")

if __name__ == "__main__": main()
