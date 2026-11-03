<span id="pair-configured-endpoints" class="legacy-anchor" aria-hidden="true"></span>
# Pair Yoke boxes

These are the pairing commands in the current software. The USB hardware
uses two ZERO 3E boxes; its complete USB firmware is still in development.
See [USB hardware](../usb.md) for the wiring.

The examples call the device-side box `usb-device-box` and the computer-side
box `desk-usb-box`. Replace these names and the example local network
address with your configured names and address. See
[software setup](../../../doc/design/DEPLOYMENT.md) for configuration.

## Find the endpoints

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk status
```

The software on both boxes must be running on the same network and use
the same site key. On a machine with multiple interfaces, the address
must belong to the local host. An empty result means no box was heard from; the command does not start
the box software.

## Select a source

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk pair usb-device-box desk-usb-box
```

The device-side box comes first; the computer-side box comes second.
The computer-side box saves the pairing before replying. Exit code zero
confirms that it saved the choice; the USB connection can still be pending.

## Check the result

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk status --json
```

| Announced state | What to do |
|---|---|
| `idle` | Check whether the box is paired |
| `pending` | Check that the paired box is online and its software is running |
| `active` | Connection software reports ready; check the actual USB device |
| `error` | Read the error message and check both boxes |

To watch pairing and status messages, run this in another
terminal before pairing:

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk snoop
```

The observer does not change pairings. Stop it with Ctrl-C.
It shows the messages it receives, not a full history.

## Change or remove the connection

Pair the computer-side box with another device-side box to change the
connection. To clear the pairing:

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk unpair desk-usb-box
```

Closing the command-line tool or monitor keeps the pairing saved.
If the paired box goes offline, the other box waits for it to return.
Restarting the software restores its saved pairing.

See [Deployment’s failure playbook](../../../doc/design/DEPLOYMENT.md#failure-playbook)
for authentication drops, missing multicast traffic, and device failures.
