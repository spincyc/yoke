# Pair configured endpoints

Use this guide after installing Yoke and commissioning a producer and
consumer with compatible backends. The
[deployment guide](../../../doc/design/DEPLOYMENT.md) covers identities,
site keys, interface selection, persistent state, and service setup.
The [backend reference](../reference/backends.md) helps you choose the
right integration.

The examples below use the identities in the repository’s Just Add Power
configs. Substitute the identities and local interface address from your
own test network.

## Find the endpoints

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk status
```

Both endpoint agents must be running on the same control network and
use the same site key. On a machine with multiple interfaces, the address
must belong to the local host. An empty result means no accepted
announcements were collected; it does not create or start endpoints.

## Select a source

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk pair cam-garage tv-den
```

The order is **producer, then consumer**. The consumer saves the pairing
before it acknowledges the request. Exit code zero confirms that saved
choice. The driver may still be connecting.

## Check the result

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk status --json
```

| Announced state | What to do |
|---|---|
| `idle` | Check whether the consumer is paired |
| `pending` | Check that the selected producer is online and the backend is ready |
| `active` | The backend confirms attachment; check the actual display or USB application |
| `error` | Read the driver detail and inspect the backend |

To inspect the control conversation as it happens, run this in another
terminal before pairing:

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk snoop
```

The observer does not change pairings. Stop it with Ctrl-C.
Its view covers traffic it received, not a complete historical log.

## Change or remove the connection

Pair the consumer to another compatible producer to replace its route.
Explicitly unpair it to clear the saved choice:

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk unpair tv-den
```

Closing the CLI or monitor preserves the pairing. A missing producer also
preserves it; the consumer waits for that source to return. Orderly agent
shutdown quiesces the local driver and retains the route for restart.

See [Deployment’s failure playbook](../../../doc/design/DEPLOYMENT.md#failure-playbook)
for authentication drops, missing multicast traffic, and device failures.
