#!/usr/bin/env python3
"""
drm_display.py - DRM dumb buffer display for 32bpp BGRA output.
Bypasses fbdev 16bpp limitation on Pi 5's vc4-kms-v3d driver.
Write BGRA frames directly to HDMI via DRM/KMS — zero color conversion.
"""

import ctypes, struct, fcntl, mmap, os, sys
import numpy as np

# ─── DRM ioctl numbers ───────────────────────────────────────────────────

DRM_IOCTL_MODE_GETRESOURCES  = 0xC04064A0
DRM_IOCTL_MODE_GETCONNECTOR  = 0xC05064A7
DRM_IOCTL_MODE_GETENCODER    = 0xC01464A6
DRM_IOCTL_MODE_GETCRTC       = 0xC06864A1
DRM_IOCTL_MODE_SETCRTC       = 0xC06864A2
DRM_IOCTL_MODE_ADDFB         = 0xC01C64AE
DRM_IOCTL_MODE_RMFB          = 0xC00464AF
DRM_IOCTL_MODE_CREATE_DUMB   = 0xC02064B2
DRM_IOCTL_MODE_MAP_DUMB      = 0xC01064B3
DRM_IOCTL_MODE_DESTROY_DUMB  = 0xC00464B4
DRM_IOCTL_SET_MASTER         = 0x0000641E
DRM_IOCTL_DROP_MASTER        = 0x0000641F

# struct drm_mode_modeinfo: 68 bytes
# __u32 clock;          offset 0
# __u16 hdisplay;       offset 4
# __u16 hsync_start;    offset 6
# __u16 hsync_end;      offset 8
# __u16 htotal;         offset 10
# __u16 hskew;          offset 12
# __u16 vdisplay;       offset 14
# __u16 vsync_start;    offset 16
# __u16 vsync_end;      offset 18
# __u16 vtotal;         offset 20
# __u16 vscan;          offset 22
# __u32 vrefresh;       offset 24
# __u32 flags;          offset 28
# __u32 type;           offset 32
# char name[32];        offset 36
DRM_MODE_SIZE = 68
DRM_MODE_FMT = "I10HIII32s"  # clock, 10xH, vrefresh, flags, type, name


def _parse_mode(data, offset=0):
    """Parse a drm_mode_modeinfo from bytes."""
    vals = struct.unpack_from("I", data, offset)
    clock = vals[0]
    hdisplay, hsync_start, hsync_end, htotal, hskew, \
        vdisplay, vsync_start, vsync_end, vtotal, vscan = \
        struct.unpack_from("10H", data, offset + 4)
    # offset 22 has vscan (last H), then 2 bytes padding before vrefresh
    vrefresh = struct.unpack_from("I", data, offset + 24)[0]
    flags = struct.unpack_from("I", data, offset + 28)[0]
    typ = struct.unpack_from("I", data, offset + 32)[0]
    name_raw = data[offset + 36:offset + 68]
    name = name_raw.split(b'\x00')[0].decode(errors='ignore')

    return {
        "clock": clock, "hdisplay": hdisplay, "hsync_start": hsync_start,
        "hsync_end": hsync_end, "htotal": htotal, "hskew": hskew,
        "vdisplay": vdisplay, "vsync_start": vsync_start,
        "vsync_end": vsync_end, "vtotal": vtotal, "vscan": vscan,
        "vrefresh": vrefresh, "flags": flags, "type": typ, "name": name,
    }


def _pack_mode(mode):
    """Pack a mode dict into 68 bytes."""
    buf = bytearray(DRM_MODE_SIZE)
    struct.pack_into("I", buf, 0, mode.get("clock", 0))
    struct.pack_into("10H", buf, 4,
        mode.get("hdisplay", 0), mode.get("hsync_start", 0),
        mode.get("hsync_end", 0), mode.get("htotal", 0),
        mode.get("hskew", 0), mode.get("vdisplay", 0),
        mode.get("vsync_start", 0), mode.get("vsync_end", 0),
        mode.get("vtotal", 0), mode.get("vscan", 0))
    struct.pack_into("I", buf, 24, mode.get("vrefresh", 0))
    struct.pack_into("I", buf, 28, mode.get("flags", 0))
    struct.pack_into("I", buf, 32, mode.get("type", 0))
    name = mode.get("name", "").encode()[:31]
    buf[36:36 + len(name)] = name
    return bytes(buf)


class DRMDisplay:
    """DRM dumb buffer display — 32bpp XRGB8888, direct HDMI output."""

    def __init__(self, card=None, preferred_w=0, preferred_h=0):
        """Open DRM device, find HDMI connector, create 32bpp buffer.
        preferred_w/h: if set, try to find a matching mode (e.g. 1920x1080).
        """
        # Try cards
        if card:
            cards = [card]
        else:
            cards = ["/dev/dri/card1", "/dev/dri/card0"]

        self.fd = None
        for c in cards:
            try:
                self.fd = os.open(c, os.O_RDWR | os.O_CLOEXEC)
                # Try to get resources — if this fails, wrong card
                buf = bytearray(64)
                fcntl.ioctl(self.fd, DRM_IOCTL_MODE_GETRESOURCES, buf)
                print(f"[OK] DRM: opened {c}")
                break
            except Exception as e:
                if self.fd is not None:
                    os.close(self.fd)
                    self.fd = None
                continue

        if self.fd is None:
            raise RuntimeError("No DRM device found")

        # Become DRM master
        try:
            fcntl.ioctl(self.fd, DRM_IOCTL_SET_MASTER)
        except:
            pass

        # Get resources
        res = self._get_resources()

        # Find connected connector with modes
        self.connector_id = None
        self.mode = None
        self.encoder_id = None
        self.crtc_id = None

        for conn_id in res["connector_ids"]:
            conn = self._get_connector(conn_id)
            if conn["connection"] == 1 and conn["modes"]:  # connected
                self.connector_id = conn_id
                self.encoder_id = conn.get("encoder_id", 0)

                # Select mode: prefer matching resolution, then highest refresh at that res
                best = None
                for m in conn["modes"]:
                    if preferred_w > 0 and preferred_h > 0:
                        if m["hdisplay"] == preferred_w and m["vdisplay"] == preferred_h:
                            if not best or m["vrefresh"] > best["vrefresh"]:
                                best = m
                    else:
                        # Default: prefer 1920x1080, or first mode
                        if m["hdisplay"] == 1920 and m["vdisplay"] == 1080:
                            if not best or m["vrefresh"] > best["vrefresh"]:
                                best = m

                if not best:
                    # Fallback: first mode with highest refresh
                    best = conn["modes"][0]

                self.mode = best
                print(f"[OK] DRM: connector {conn_id}, "
                      f"mode {self.mode['hdisplay']}x{self.mode['vdisplay']}"
                      f"@{self.mode['vrefresh']}Hz ({self.mode['name']})")
                break

        if not self.connector_id or not self.mode:
            raise RuntimeError("No connected HDMI output found")

        # Get CRTC from encoder
        if self.encoder_id:
            enc = self._get_encoder(self.encoder_id)
            self.crtc_id = enc.get("crtc_id", 0)
        if not self.crtc_id and res["crtc_ids"]:
            self.crtc_id = res["crtc_ids"][0]

        self.width = self.mode["hdisplay"]
        self.height = self.mode["vdisplay"]

        # Create 32bpp dumb buffer
        self.handle, self.pitch, self.size = self._create_dumb(
            self.width, self.height, 32)
        print(f"[OK] DRM: buffer {self.width}x{self.height} @ 32bpp, "
              f"pitch={self.pitch}, size={self.size}")

        # Create DRM framebuffer object
        self.fb_id = self._add_fb(self.width, self.height, self.pitch, 32, 24, self.handle)

        # Memory-map the buffer
        offset = self._map_dumb(self.handle)
        self.mm = mmap.mmap(self.fd, self.size, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE, offset=offset)

        # Set CRTC
        self._set_crtc()

        # Create writable numpy view of mmap for fast array writes
        self.np_buf = np.ndarray(shape=(self.height, self.pitch),
                                 dtype=np.uint8, buffer=self.mm)

        # Pre-allocate premultiply output buffer (avoids 8MB alloc per frame)
        self._premul_buf = np.empty((self.height, self.width, 4), dtype=np.uint8)
        self._premul_ch = [np.empty((self.height, self.width), dtype=np.uint8) for _ in range(4)]
        self._premul_ch[3][:] = 255

        print(f"[OK] DRM: 32bpp display active on HDMI")

    # ── DRM ioctls ──────────────────────────────────────────────────────

    def _get_resources(self):
        buf = bytearray(64)
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_GETRESOURCES, buf)
        n_fb   = struct.unpack_from("I", buf, 32)[0]
        n_crtc = struct.unpack_from("I", buf, 36)[0]
        n_conn = struct.unpack_from("I", buf, 40)[0]
        n_enc  = struct.unpack_from("I", buf, 44)[0]

        fb_arr   = (ctypes.c_uint32 * max(n_fb, 1))()
        crtc_arr = (ctypes.c_uint32 * max(n_crtc, 1))()
        conn_arr = (ctypes.c_uint32 * max(n_conn, 1))()
        enc_arr  = (ctypes.c_uint32 * max(n_enc, 1))()

        buf2 = bytearray(64)
        struct.pack_into("Q", buf2, 0,  ctypes.addressof(fb_arr))
        struct.pack_into("Q", buf2, 8,  ctypes.addressof(crtc_arr))
        struct.pack_into("Q", buf2, 16, ctypes.addressof(conn_arr))
        struct.pack_into("Q", buf2, 24, ctypes.addressof(enc_arr))
        struct.pack_into("IIII", buf2, 32, n_fb, n_crtc, n_conn, n_enc)
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_GETRESOURCES, buf2)

        return {
            "connector_ids": list(conn_arr[:n_conn]),
            "crtc_ids": list(crtc_arr[:n_crtc]),
            "encoder_ids": list(enc_arr[:n_enc]),
        }

    def _get_connector(self, conn_id):
        buf = bytearray(80)
        struct.pack_into("I", buf, 48, conn_id)
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_GETCONNECTOR, buf)

        n_modes    = struct.unpack_from("I", buf, 32)[0]
        n_props    = struct.unpack_from("I", buf, 36)[0]
        n_encoders = struct.unpack_from("I", buf, 40)[0]
        encoder_id = struct.unpack_from("I", buf, 44)[0]
        connection = struct.unpack_from("I", buf, 60)[0]

        modes_buf = bytearray(DRM_MODE_SIZE * max(n_modes, 1))
        modes_ctypes = (ctypes.c_char * len(modes_buf)).from_buffer(modes_buf)
        enc_arr = (ctypes.c_uint32 * max(n_encoders, 1))()
        prop_arr = (ctypes.c_uint32 * max(n_props, 1))()
        propval_arr = (ctypes.c_uint64 * max(n_props, 1))()

        buf2 = bytearray(80)
        struct.pack_into("Q", buf2, 0,  ctypes.addressof(enc_arr))
        struct.pack_into("Q", buf2, 8,  ctypes.addressof(modes_ctypes))
        struct.pack_into("Q", buf2, 16, ctypes.addressof(prop_arr))
        struct.pack_into("Q", buf2, 24, ctypes.addressof(propval_arr))
        struct.pack_into("III", buf2, 32, n_modes, n_props, n_encoders)
        struct.pack_into("I", buf2, 48, conn_id)
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_GETCONNECTOR, buf2)

        encoder_id = struct.unpack_from("I", buf2, 44)[0]
        connection = struct.unpack_from("I", buf2, 60)[0]

        modes = []
        for i in range(n_modes):
            modes.append(_parse_mode(modes_buf, i * DRM_MODE_SIZE))

        return {
            "connector_id": conn_id, "encoder_id": encoder_id,
            "connection": connection, "modes": modes,
        }

    def _get_encoder(self, enc_id):
        buf = bytearray(20)
        struct.pack_into("I", buf, 0, enc_id)
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_GETENCODER, buf)
        return {
            "crtc_id": struct.unpack_from("I", buf, 8)[0],
        }

    def _create_dumb(self, w, h, bpp):
        buf = bytearray(32)
        struct.pack_into("IIII", buf, 0, h, w, bpp, 0)
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_CREATE_DUMB, buf)
        handle = struct.unpack_from("I", buf, 16)[0]
        pitch = struct.unpack_from("I", buf, 20)[0]
        size = struct.unpack_from("Q", buf, 24)[0]
        return handle, pitch, size

    def _add_fb(self, w, h, pitch, bpp, depth, handle):
        buf = bytearray(28)
        struct.pack_into("IIIIIII", buf, 0, 0, w, h, pitch, bpp, depth, handle)
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_ADDFB, buf)
        return struct.unpack_from("I", buf, 0)[0]

    def _map_dumb(self, handle):
        buf = bytearray(16)
        struct.pack_into("I", buf, 0, handle)
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_MAP_DUMB, buf)
        return struct.unpack_from("Q", buf, 8)[0]

    def _set_crtc(self):
        conn_arr = (ctypes.c_uint32 * 1)(self.connector_id)
        mode_bytes = _pack_mode(self.mode)

        buf = bytearray(104)
        struct.pack_into("Q", buf, 0, ctypes.addressof(conn_arr))
        struct.pack_into("I", buf, 8, 1)        # count_connectors
        struct.pack_into("I", buf, 12, self.crtc_id)
        struct.pack_into("I", buf, 16, self.fb_id)
        struct.pack_into("II", buf, 20, 0, 0)   # x, y
        struct.pack_into("I", buf, 28, 0)        # gamma_size
        struct.pack_into("I", buf, 32, 1)        # mode_valid
        buf[36:36 + DRM_MODE_SIZE] = mode_bytes
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_SETCRTC, buf)

    # ── Public API ──────────────────────────────────────────────────────

    def _premultiply_alpha(self, bgra):
        """Composite BGRA over black — per-pixel alpha using cv2 SIMD."""
        import cv2
        h, w = bgra.shape[:2]
        if (bgra[0, 0, 3] == 255 and bgra[h-1, w-1, 3] == 255 and
            bgra[h//2, w//2, 3] == 255 and bgra[0, w-1, 3] == 255 and bgra[h-1, 0, 3] == 255):
            return bgra
        b, g, r, a = cv2.split(bgra)
        out = self._premul_buf[:h, :w]
        cv2.multiply(b, a, dst=self._premul_ch[0][:h, :w], scale=1.0/255)
        cv2.multiply(g, a, dst=self._premul_ch[1][:h, :w], scale=1.0/255)
        cv2.multiply(r, a, dst=self._premul_ch[2][:h, :w], scale=1.0/255)
        cv2.merge([self._premul_ch[0][:h, :w], self._premul_ch[1][:h, :w],
                   self._premul_ch[2][:h, :w], self._premul_ch[3][:h, :w]], dst=out)
        return out

    def write_region(self, bgra_tile, x, y, w, h):
        """Write a BGRA tile to the display buffer using numpy (C-speed)."""
        bgra_tile = self._premultiply_alpha(bgra_tile)
        row_bytes = w * 4
        if x == 0 and self.pitch == row_bytes:
            # Bulk write — single memcpy
            off = y * self.pitch
            size = h * row_bytes
            self.mm[off:off + size] = bgra_tile.data
        else:
            # Numpy view write — one C-level operation instead of Python loop
            x_bytes = x * 4
            self.np_buf[y:y + h, x_bytes:x_bytes + row_bytes] = \
                bgra_tile.reshape(h, row_bytes)

    def write_frame(self, bgra_frame):
        """Write a full BGRA frame to the display buffer."""
        bgra_frame = self._premultiply_alpha(bgra_frame)
        h, w = bgra_frame.shape[:2]
        row_bytes = w * 4
        if self.pitch == row_bytes:
            size = h * row_bytes
            self.mm[0:size] = bgra_frame.data
        else:
            for y in range(min(h, self.height)):
                off = y * self.pitch
                self.mm[off:off + row_bytes] = bgra_frame[y].data

    def clear(self):
        """Clear display to black."""
        self.mm[:self.size] = b'\x00' * self.size

    def close(self):
        """Cleanup DRM resources."""
        try:
            self.clear()
            self.mm.close()
        except:
            pass
        try:
            fcntl.ioctl(self.fd, DRM_IOCTL_DROP_MASTER)
        except:
            pass
        try:
            os.close(self.fd)
        except:
            pass


def test():
    """Quick self-test."""
    import time
    print("\n=== DRM 32bpp Display Test ===\n")
    disp = DRMDisplay(preferred_w=1920, preferred_h=1080)
    w, h = disp.width, disp.height

    # Color fill
    print(f"[Test] Color fill {w}x{h}...")
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    frame[:, :, 0] = 255  # Blue
    frame[:, :, 3] = 255
    disp.write_frame(frame)
    time.sleep(1)

    # Tile write benchmark
    print("[Test] Tile write benchmark...")
    tw, th = w // 2, h // 2
    tile = np.random.randint(0, 255, (th, tw, 4), dtype=np.uint8)
    count = 0
    start = time.monotonic()
    while time.monotonic() - start < 3.0:
        disp.write_region(tile, 0, 0, tw, th)
        count += 1
    elapsed = time.monotonic() - start
    fps = count / elapsed
    print(f"[OK] {count} tile writes in {elapsed:.1f}s = {fps:.1f}/sec "
          f"(4 tiles = ~{fps/4:.1f} FPS)")

    disp.close()
    print("[OK] Done!\n")


if __name__ == "__main__":
    test()
