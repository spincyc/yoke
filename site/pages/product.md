<header class="product-intro"><p class="eyebrow">USB / HDMI / IR</p><h1>Yoke hardware.</h1>
<p class="hero-lead">Small boxes connect your equipment over Ethernet. USB is the first prototype; HDMI and IR hardware are also planned.</p></header>

## USB

A USB device plugs into one Yoke box. A second Yoke box connects by USB to
your computer. Both boxes connect to your Ethernet switch. Their pairing
stays saved until you change or remove it.

The prototype uses **two Radxa ZERO 3E boards** and targets **USB 2 first**.
The computer is separate from both boxes and Yoke runs inside the boxes. Firmware development and hardware testing are ongoing.

[See the USB boxes and wiring](usb.md) ·
[Read the USB hardware design](../../doc/design/USB-ENDPOINT.md)

## HDMI

An HDMI source plugs into a Yoke HDMI box. Another box connects to the
screen. Both use Ethernet. Pair the screen's box with the source you want
to watch.

Dedicated HDMI hardware is planned. Earlier bandwidth and hardware studies
are kept in the [engineering documentation](engineering/index.md).

## IR

An IR box connects to Ethernet and sits beside the equipment it controls.
A 3.5 mm jack connects an IR emitter cable. A planned two-jack version
would receive and forward signals from an existing control system, so Yoke
could be added alongside it.

IR transmission, learning, and that pass-through mode still need to be
implemented and tested.

## Management is optional

The boxes keep their own saved settings. Use the command line, Thermite,
or another manager to change them. Closing the manager does not remove
the pairing.

[How pairing works](how-it-works.md) · [Development status](docs/project-status.md)
