# NDI Video Wall — Architecture Design

## Overview

A distributed video wall system where N Raspberry Pis each drive one display, together forming a tiled wall from a single NDI source. One Pi acts as **controller** with a central web UI to configure and manage all wall nodes.

---

## System Components

### Roles

| Role | Description |
|------|-------------|
| **Controller** | One Pi runs the web UI, coordinates all nodes, monitors status |
| **Worker** | Each Pi (including controller) receives NDI, crops its region, outputs to HDMI |

The controller is also a worker — it drives one display while managing the others.

### Per-Node Software

```
ndi_wall.py          — Wall worker: receive → crop → display
ndi_wall_controller.py — Controller: REST API for wall management
ndi_web.py           — Extended web panel with Video Wall section
```

---

## Architecture: Phase A (Current Target)

**Each Pi receives the full NDI stream and crops locally.**

```
                    ┌─────────────┐
                    │  NDI Source  │
                    │  (Switcher)  │
                    └──────┬──────┘
                           │ NDI stream (unicast)
                    ┌──────┴──────┐
                    │   Gigabit   │
                    │   Switch    │
                    └──┬──┬──┬──┬─┘
                       │  │  │  │  Full stream to each
                    ┌──┘  │  │  └──┐
                    ▼     ▼  ▼     ▼
                  ┌───┐ ┌───┐ ┌───┐ ┌───┐
                  │Pi0│ │Pi1│ │Pi2│ │Pi3│
                  │CTL│ │   │ │   │ │   │
                  └─┬─┘ └─┬─┘ └─┬─┘ └─┬─┘
                    │     │     │     │
                  ┌─┴─┐ ┌─┴─┐ ┌─┴─┐ ┌─┴─┐
                  │ TV │ │ TV │ │ TV │ │ TV │
                  │0,0 │ │1,0 │ │0,1 │ │1,1 │
                  └───┘ └───┘ └───┘ └───┘
```

### Bandwidth Budget

| Source | Stream BW | 2×2 Wall | 3×3 Wall |
|--------|-----------|----------|----------|
| 1080p30 SpeedHQ | ~125 Mbps | 500 Mbps ✓ | 1125 Mbps ✗ |
| 1080p30 H.264 | ~15 Mbps | 60 Mbps ✓ | 135 Mbps ✓ |
| 4K30 SpeedHQ | ~500 Mbps | 2000 Mbps ✗ | — |

**Gigabit limit:** 2×2 with SpeedHQ 1080p, or larger with H.264/HX.

### Future Phases

**Phase B — Master re-sends cropped regions:**
- An N100 mini-PC receives full stream, crops N regions, sends N smaller NDI streams
- Each Pi receives only its crop → bandwidth = stream_bw / N per Pi
- Enables 4K sources and large walls on gigabit

**Phase C — IGMP Multicast:**
- NDI supports multicast mode — switch replicates packets at hardware level
- Network sees only 1× bandwidth regardless of Pi count
- Requires managed switch with IGMP snooping
- Compatible with Phase A architecture (just enable multicast on source)

---

## Synchronization

### The Problem

Four Pis receiving the same NDI stream will have slightly different network arrival times (0.5–5ms jitter). Without sync, seams between displays show mismatched frames — visually unacceptable.

### Solution: Timecode-Based Frame Sync

Every NDI frame carries a **timecode** (int64, 100ns ticks since epoch). All Pis displaying the same timecode at the same wall-clock moment = perfect sync.

```
Timeline:
  NDI Source:     Frame T=100  Frame T=101  Frame T=102
                       │            │            │
  Network jitter: ─────┼────────────┼────────────┼─────
                       │            │            │
  Pi0 receives:   ─────╤──(+1ms)───╤──(+0ms)───╤──
  Pi1 receives:   ─────╤──(+3ms)───╤──(+2ms)───╤──
  Pi2 receives:   ─────╤──(+2ms)───╤──(+5ms)───╤──
  Pi3 receives:   ─────╤──(+1ms)───╤──(+1ms)───╤──
                       │            │            │
  Display target: ─────┼──(+40ms)──┼──(+40ms)──┼──  ← all show T at T+40ms
```

### Implementation

1. **NTP sync** — All Pis sync to the same NTP server (the controller Pi runs `chrony`). Achieves <1ms wall-clock agreement on a LAN.

2. **Frame buffer** — Each worker buffers 2–3 frames in a ring buffer.

3. **Display clock** — A dedicated display thread runs at the source frame rate (e.g., 30fps = 33.3ms). On each tick:
   - Calculate: `target_timecode = latest_timecode - buffer_delay`
   - Find the frame in the buffer with the closest timecode
   - Write it to DRM/framebuffer

4. **Buffer delay** — Configurable (default: 2 frames = 66ms at 30fps). Absorbs network jitter. Higher = more stable sync, more latency.

5. **Drift correction** — If a Pi's buffer fills up (receiver faster than display), drop oldest. If buffer empties (receiver slower), hold last frame.

### Sync Accuracy

| Component | Accuracy |
|-----------|----------|
| NTP on LAN (chrony) | <1ms |
| Frame timing at 30fps | 33ms per frame |
| DRM page flip | <1ms |
| **Total sync error** | **<2ms** (imperceptible) |

At 30fps, a 2ms sync error means frames are 94% overlapping in time. The human eye cannot detect this — you need >8ms offset to see tearing at display seams.

---

## Controller Web UI

### Wall Configuration

The controller's web panel gets a new **Video Wall** section:

```
┌─────────────────────────────────────────┐
│ ⠿ ● Video Wall              STOPPED  ▼ │
├─────────────────────────────────────────┤
│                                         │
│  NDI Source: [STUDIO-A (Arena - Comp) ▼]│
│                                         │
│  Wall Size:  [2] × [2]   Output: 1080p  │
│  Buffer:     [2] frames  (66ms latency) │
│                                         │
│  ┌─────────┬─────────┐                  │
│  │  Pi0    │  Pi1    │  ← drag to swap  │
│  │ 0,0 CTL │ 1,0     │                  │
│  │ ●online │ ●online │                  │
│  ├─────────┼─────────┤                  │
│  │  Pi2    │  Pi3    │                  │
│  │ 0,1     │ 1,1     │                  │
│  │ ●online │ ○offline│                  │
│  └─────────┴─────────┘                  │
│                                         │
│  Nodes:                                 │
│  ndi-wall-1.local  row:0 col:0  CTL  ● │
│  ndi-wall-2.local  row:0 col:1       ● │
│  ndi-wall-3.local  row:1 col:0       ● │
│  ndi-wall-4.local  row:1 col:1       ○ │
│                                         │
│  [▶ Start Wall]  [■ Stop]              │
│                                         │
│  Sync: 0.4ms avg | Buffer: 2/3 frames  │
│  Source: 1920×1080 @ 29.97fps           │
│                                         │
└─────────────────────────────────────────┘
```

### Node Discovery

Workers announce themselves via **mDNS** (Avahi):
- Service type: `_ndi-wall._tcp`
- TXT records: `role=worker`, `hostname=ndi-wall-2`, `version=1.0`

Controller discovers workers automatically. Manual IP entry as fallback.

### Controller REST API

The controller exposes these endpoints for worker coordination:

```
GET  /api/wall/status          — Wall state, all node status
POST /api/wall/configure       — Set wall config (source, grid, assignments)
POST /api/wall/start           — Start all workers
POST /api/wall/stop            — Stop all workers
GET  /api/wall/nodes           — List discovered nodes
POST /api/wall/node/assign     — Assign node to grid position

# Worker-to-controller endpoints
POST /api/wall/heartbeat       — Worker reports status (called every 2s)
GET  /api/wall/my-config       — Worker fetches its assignment
```

### Worker API

Each worker exposes a local API for the controller to manage:

```
GET  /api/wall/worker/status   — Local status (fps, sync, buffer)
POST /api/wall/worker/start    — Start receiving + displaying
POST /api/wall/worker/stop     — Stop
POST /api/wall/worker/config   — Set source, crop region, sync params
```

---

## Worker Process: `ndi_wall.py`

### Frame Pipeline

```
NDI Receive Thread          Frame Buffer           Display Thread
       │                        │                       │
  recv frame ──────────────► [ring buf] ◄────────── read frame
  + timecode                  3 slots               @ target TC
  + crop region                 │                   write to DRM
       │                        │                       │
   ~30fps                   lock-free              vsync-aligned
```

### Crop Calculation

Given wall config `(cols=2, rows=2, my_col=1, my_row=0)` and source `1920×1080`:

```python
crop_x = (source_w // cols) * my_col    # 960
crop_y = (source_h // rows) * my_row    # 0
crop_w = source_w // cols               # 960
crop_h = source_h // rows               # 540
```

The crop is applied with numpy slicing — zero-copy, sub-microsecond:
```python
tile = frame[crop_y:crop_y+crop_h, crop_x:crop_x+crop_w]
```

Then scaled to output resolution (e.g., 1920×1080 for each display):
```python
output = cv2.resize(tile, (output_w, output_h), interpolation=cv2.INTER_LINEAR)
```

### Bezel Compensation

Physical displays have bezels that create gaps. Without compensation, the image appears to "skip" at bezels. The fix:

```
Without compensation:        With compensation:
┌────┐ ┌────┐              ┌────┐ ┌────┐
│ABCD│ │EFGH│              │AB  │ │  GH│
│IJKL│ │MNOP│              │IJ  │ │  OP│
└────┘ └────┘              └────┘ └────┘
  ↑ seam jumps                ↑ continuous across bezel
```

Implementation: Crop a slightly larger region and let the bezel "cover" the overlap:

```python
bezel_h_px = bezel_mm * (source_w / physical_wall_width_mm)
bezel_v_px = bezel_mm * (source_h / physical_wall_height_mm)

# Expand crop by half-bezel on each shared edge
if my_col > 0:        crop_x -= bezel_h_px / 2
if my_col < cols - 1: crop_w += bezel_h_px / 2
if my_row > 0:        crop_y -= bezel_v_px / 2
if my_row < rows - 1: crop_h += bezel_v_px / 2
```

Bezel width is configurable in the web UI (in mm or pixels).

---

## Configuration Flow

### Setup Sequence

```
1. Install all Pis with same setup.sh
2. Set hostnames: ndi-wall-1, ndi-wall-2, etc.
3. On controller Pi: enable "Video Wall Controller" mode
4. Workers auto-discover via mDNS
5. In web UI: set wall size, drag nodes to grid positions
6. Click "Start Wall" → controller pushes config to all workers
7. All workers start receiving, cropping, displaying
```

### Config File: `wall_config.json`

```json
{
  "source": "STUDIO-A (Arena - Composition)",
  "cols": 2,
  "rows": 2,
  "output_res": "1920x1080",
  "buffer_frames": 2,
  "bezel_mm": 12,
  "nodes": [
    {"hostname": "ndi-wall-1.local", "col": 0, "row": 0, "role": "controller"},
    {"hostname": "ndi-wall-2.local", "col": 1, "row": 0, "role": "worker"},
    {"hostname": "ndi-wall-3.local", "col": 0, "row": 1, "role": "worker"},
    {"hostname": "ndi-wall-4.local", "col": 1, "row": 1, "role": "worker"}
  ]
}
```

---

## NTP Time Sync Setup

### Controller (chrony server)

```bash
# /etc/chrony/chrony.conf additions:
local stratum 8          # Serve time even without upstream
allow 192.168.0.0/16     # Allow LAN clients
```

### Workers (chrony client)

```bash
# /etc/chrony/chrony.conf:
server ndi-wall-1.local iburst prefer minpoll 0 maxpoll 2
makestep 0.1 3
```

`minpoll 0 maxpoll 2` = poll every 1–4 seconds for tight sync.

### Verification

```bash
chronyc tracking    # Show sync accuracy
chronyc sources     # Show time sources
```

Target: <1ms offset on LAN. Typically achieves 0.1–0.5ms.

---

## File Structure

```
ndi-rpi-solutions/
├── ndi_wall.py              # Worker: receive → crop → display
├── ndi_wall_controller.py   # Controller: REST API, node management
├── ndi_web.py               # Web panel (extended with Video Wall section)
├── wall_config.json         # Wall configuration (generated by web UI)
├── templates/
│   └── index.html           # Web panel (with Video Wall section)
└── scripts/
    └── wall_ntp_setup.sh    # NTP chrony configuration helper
```

---

## Implementation Order

### Phase 1: Single-node wall worker
- `ndi_wall.py` — receive, crop, DRM display
- CLI: `python3 ndi_wall.py --source "X" --cols 2 --rows 2 --col 0 --row 0`
- Test with one Pi showing one quadrant

### Phase 2: Controller + multi-node
- `ndi_wall_controller.py` — REST API, mDNS discovery
- Worker heartbeat + remote start/stop
- Web UI Video Wall section

### Phase 3: Frame sync
- NTP setup automation
- Timecode-based frame buffer
- Sync monitoring in web UI

### Phase 4: Polish
- Bezel compensation UI
- Auto-detection of source resolution changes
- Wall presets (2×1, 2×2, 3×3)
- Autostart on boot

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Gigabit bandwidth exceeded | Wall tears/drops | Monitor BW, warn in UI, suggest multicast |
| NTP sync drift | Frame misalign at seams | chrony with tight polling, monitor offset |
| One Pi crashes | Gap in wall | Auto-restart worker, show black on timeout |
| Source resolution change | Crop mismatch | Workers re-detect resolution, recalculate crop |
| Pi 5 decode perf | Low FPS | Already proven 46fps compositor, crop is simpler |
| Network switch bottleneck | Packet loss | Recommend gigabit unmanaged or IGMP managed |

---

## Summary

Start with **Phase A** (full stream to each Pi, crop locally). This works for 2×2 walls on gigabit with SpeedHQ 1080p. The architecture cleanly upgrades to Phase B (master re-sends) or Phase C (multicast) by changing only the receive path — the crop, sync, and display logic stays identical.

The key innovations are:
1. **Timecode-based sync** — sub-millisecond frame alignment using NTP + NDI timecodes
2. **Central web UI** — one controller manages all nodes
3. **mDNS discovery** — workers announce themselves, zero manual IP config
4. **Bezel compensation** — physically accurate image continuity across display gaps
