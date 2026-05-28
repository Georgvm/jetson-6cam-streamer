# udev rules

Persistent `/dev/cam_<label>` symlinks for cameras that share the same vendor/product ID (and therefore can't be distinguished by USB serial). Each rule matches the **physical USB port path**, so the label stays bound to a port: whichever cam is plugged into the labelled port becomes that label.

To install:

```
sudo cp 99-jetson-cams.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --action=add --subsystem-match=video4linux
ls -l /dev/cam_*
```

Edit the `KERNELS==` values to match your own rig's USB topology (find them with `udevadm info --query=property /dev/video0 | grep DEVPATH`).
