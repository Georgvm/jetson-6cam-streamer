# jetson-6cam-streamer

Stream all 6 USB cameras on a Jetson Thor (5× Global Shutter + 1× H264) live in a browser, simultaneously, from a single USB2 bus.

The stock `uvcvideo` kernel module always picks the largest USB iso alternate setting (≈24 Mbps per cam) regardless of the negotiated resolution and frame rate, so the bus saturates after 4 cams and any 5th `VIDIOC_STREAMON` returns `ENOSPC`. This repo contains:

1. **`uvcvideo-patch/`** — a small patch to `drivers/media/usb/uvc/` that adds two module parameters: `force_altsetting=N` (used for every UVC stream) and `force_altsetting_fallback=N` (used only when the device doesn't have the requested alt). The fallback lets you mix cams with different alt-setting tables on the same bus — e.g. five GS cams at alt 7 + one H264 cam (which tops out at alt 6) at alt 4.
2. **`streamer/`** — a Python HTTP server, one port per camera (8080–8085), each serving the cam as zero-copy MJPEG so the browser sees six origins and never hits a per-origin connection cap.
3. **`udev/`** — a udev rules file that creates persistent `/dev/cam_<label>` symlinks bound to USB port paths, so the front/front-left/front-right cameras keep their identities across reboots and reconnects.

Tested on Jetson Thor running L4T 6.8.12-tegra. The patch is built from mainline Linux v6.8 source — `uvcvideo` is bus-generic, so it builds fine against the Tegra kernel headers.

## Background

Each Global Shutter camera advertises 11 iso alternate settings ranging from 1.5 Mbps to 24.5 Mbps:

| alt | wMaxPacketSize | mult | per-microframe | bandwidth |
| ---:| ---:| ---:| ---:| ---:|
| 1 | 192  | 1× | 192 B  |  1.5 Mbps |
| 2 | 384  | 1× | 384 B  |  3.1 Mbps |
| 3 | 512  | 1× | 512 B  |  4.1 Mbps |
| 4 | 640  | 1× | 640 B  |  5.1 Mbps |
| 5 | 800  | 1× | 800 B  |  6.4 Mbps |
| 6 | 944  | 1× | 944 B  |  7.6 Mbps |
| 7 | 640  | 2× | 1280 B | 10.2 Mbps |
| 8 | 800  | 2× | 1600 B | 12.8 Mbps |
| 9 | 992  | 2× | 1984 B | 15.9 Mbps |
| 10 | 960 | 3× | 2880 B | 23.0 Mbps |
| 11 | 1020 | 3× | 3060 B | 24.5 Mbps |

The standard `UVC_QUIRK_FIX_BANDWIDTH` (module param `quirks=128`) is supposed to pick the smallest alt setting that fits the negotiated `dwMaxVideoFrameSize × fps`, but on this hardware the calculation still lands on alt 11 every time — confirmed by reading `/sys/bus/usb/devices/.../bAlternateSetting` while a stream is running. The patch in this repo just unconditionally selects the operator-specified alt setting, after the default bandwidth-based choice has run.

Six cams × alt 5 = ~38 Mbps total, well under USB2's ~400 Mbps usable iso budget.

## Building the patched module

The repo includes the full uvcvideo source files for an out-of-tree build. You need the kernel headers installed (Ubuntu/L4T: `linux-headers-$(uname -r)`).

```
cd uvcvideo-patch
make
```

Output: `uvcvideo.ko`.

## Loading the patched module

The patched module depends on two in-tree modules that the stock `uvcvideo.ko` pulls in automatically via `modules.dep`. Load them first:

```
sudo modprobe -r uvcvideo
sudo modprobe uvc                  # provides uvc_format_by_guid
sudo modprobe videobuf2-vmalloc    # provides vb2_vmalloc_memops
sudo insmod uvcvideo-patch/uvcvideo.ko force_altsetting=7 force_altsetting_fallback=4 quirks=128
```

Verify:

```
cat /sys/module/uvcvideo/parameters/force_altsetting           # → 7
cat /sys/module/uvcvideo/parameters/force_altsetting_fallback  # → 4
cat /sys/module/uvcvideo/parameters/quirks             # → 128
```

While any stream is running, you can read the active alt setting per device:

```
cat /sys/bus/usb/devices/1-4.1.1.1:1.1/bAlternateSetting   # → 7  (GS cam)
cat /sys/bus/usb/devices/1-4.2:1.1/bAlternateSetting       # → 4  (H264 cam, via fallback)
```

### Persisting across reboots

```
sudo cp uvcvideo-patch/uvcvideo.ko /lib/modules/$(uname -r)/updates/
sudo depmod -a
echo 'options uvcvideo quirks=128 force_altsetting=7 force_altsetting_fallback=4' | sudo tee /etc/modprobe.d/uvcvideo.conf
```

## Running the streamer

```
python3 streamer/stream.py
```

Then open `http://<host>:8080/`. The HTML at port 8080 references the per-cam streams on ports 8080–8085. Each stream is a `multipart/x-mixed-replace` MJPEG passthrough — zero transcoding, so CPU stays near idle.

If your device nodes differ, edit the `PORTS` dict at the top of `streamer/stream.py`. The order in our setup:

| port | device | role |
|---|---|---|
| 8080 | /dev/video0 | GS, also hosts HTML |
| 8081 | /dev/video2 | GS |
| 8082 | /dev/video4 | GS |
| 8083 | /dev/video6 | GS |
| 8084 | /dev/video12 | GS |
| 8085 | /dev/video8 | H264 USB Cam (MJPG output node) |

## Choosing `force_altsetting`

The per-frame budget is roughly `bandwidth_Mbps / fps / 8` MB. If you point a cam at a high-detail bright scene and the resulting JPEG exceeds that budget, the frame is truncated and uvcvideo drops it; you see a stuck or partial image (only updating when you cover the lens, because then the JPEG compresses small enough to fit).

Tested combinations on 6× cams sharing one USB2 bus:

| force_altsetting | fps | per-frame budget | bus total | result |
|---:|---:|---:|---:|---|
| 5 | 10 | ~80 KB | ~38 Mbps | low-detail scenes only; busy scenes get stuck |
| 7 | 10 | ~128 KB | ~61 Mbps | OK at 10 fps |
| 7 | 30 | ~42 KB | ~61 Mbps | smooth on most cams, busy scenes drop frames |
| 9 | 30 | ~66 KB | ~95 Mbps | smooth across all five GS cams; H264 cam can't slot in |
| **7 + fallback 4** | **30** | **~42 KB / ~53 KB** | **~64 Mbps** | **all 6 cams (5 GS + H264) live; recommended for mixed hardware** |

Even alt 11 across six cams is only ~147 Mbps, so there's plenty of headroom — bump higher if a particular cam still hiccups on very rich scenes.

## License

The kernel patch is GPL (matching uvcvideo). The streamer is MIT.
