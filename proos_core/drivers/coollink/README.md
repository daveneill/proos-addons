# CoolLink (CoolAutomation) — Home Assistant custom integration

A drop-in replacement for the built-in `coolmaster` integration that works with
**CoolLinkBridge / CoolLinkHub** (and CoolMasterNet), which the official
integration refuses to load.

## Why the official one fails

Both integrations talk the same protocol — the CoolMasterNet ASCII line protocol
over TCP port **10102** (the same one the working `homebridge-cool-automation`
plugin uses). The difference is purely in **how the `ls2` status lines are
parsed**:

- The official integration's library (`pycoolmasternet-async`) splits each line
  on whitespace and then **requires exactly 8 or 9 fields**, throwing
  "Unexpected status line format" otherwise.
- CoolLink hubs emit `ls2` lines with a **different count of trailing fields**
  (error / filter / demand columns differ), so the official integration rejects
  every unit and the hub won't set up.

This integration parses the first six fields **positionally**
(`<uid> <ON|OFF> <set-temp> <room-temp> <fan> <mode>`) and treats everything
after that as optional — the same tolerant approach the Homebridge plugin uses.
That's the whole fix.

It is **self-contained** (pure Python standard library, no `pip` requirements),
so it installs cleanly on Home Assistant OS / a Raspberry Pi.

## Install

1. Copy the whole `coollink` folder into your Home Assistant config directory:

   ```
   /config/custom_components/coollink/
   ```

   (On Home Assistant OS use the *File editor* / *Samba* / *SSH* add-on. The
   final path must contain `custom_components/coollink/manifest.json`.)

2. **Restart** Home Assistant.

3. Go to **Settings → Devices & Services → Add Integration**, search for
   **CoolLink (CoolAutomation)**.

4. Enter the hub address. It defaults to **192.168.1.12** (your CoolLinkBridge)
   on port **10102** — change if needed. Pick which modes to expose and the
   temperature range, then submit.

Each indoor unit becomes its own `climate.*` entity with power, mode
(cool / heat / auto / dry / fan), target temperature and fan speed
(auto / very low / low / medium / high / top).

### The "wake-up prompt" option

Leave it **off** first. If setup connects but reports *no units*, or commands
are ignored, try again with **Send wake-up prompt** ticked — some firmware needs
a newline to open the command prompt before it answers.

## Notes / tuning

The status-line column layout was matched to the Homebridge plugin, which is
proven on your hardware. If a value ever looks off (e.g. temperature or fan
reads wrong), grab one raw line from the hub and it can be calibrated exactly:

```
# from any machine on the LAN
nc 192.168.1.12 10102
ls2        # then paste one of the returned lines
```

The parser is deliberately more lenient than both the official library and the
Homebridge plugin, so this should be rare.
