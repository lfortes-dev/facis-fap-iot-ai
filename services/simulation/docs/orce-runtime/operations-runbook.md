# ORCE-native Simulation Runtime — Operations Runbook

Day-to-day ops procedures. For migration steps see
[`migration-guide.md`](./migration-guide.md). For the architecture overview
see [`architecture.md`](./architecture.md).

## Deploy / upgrade

```bash
# 1. Sync the canonical flow JSON into the chart's files/ directory.
cd services/simulation/helm/facis-simulation
./sync-flows.sh

# 2. Ensure the ORCE admin token Secret exists.
#    Key: token; Value: bearer token accepted by ORCE Admin API.
kubectl -n facis create secret generic facis-orce-admin \
  --from-literal=token="$(cat /tmp/orce-admin-token)" \
  --dry-run=client -o yaml | kubectl apply -f -

# 3. Helm install / upgrade.
helm upgrade --install facis-simulation . \
  --namespace facis \
  --create-namespace \
  --set compatibilityMode=orce

# 4. Verify the post-install Job deployed flows.
kubectl -n facis logs job/facis-simulation-orce-flow-deploy --tail=30
# Expected last lines:
#   [orce-flow-deploy] POST http://facis-orce.orce.svc.cluster.local:1880/flows ...
#   [orce-flow-deploy] deploy ok
```

## Lifecycle control

```bash
# Port-forward ORCE so you can drive the REST control plane locally.
kubectl -n orce port-forward svc/facis-orce 1880:1880 &

curl -X POST http://localhost:1880/api/v1/simulation/start
curl http://localhost:1880/api/v1/simulation/status
curl -X POST http://localhost:1880/api/v1/simulation/pause
curl -X POST http://localhost:1880/api/v1/simulation/reset \
  -H 'Content-Type: application/json' \
  -d '{"seed": 12345, "start_time": "2024-06-15T08:00:00Z"}'
```

## Read snapshots

```bash
curl http://localhost:1880/api/v1/meters | jq
curl http://localhost:1880/api/v1/pv | jq
curl http://localhost:1880/api/v1/weather | jq
curl http://localhost:1880/api/v1/prices | jq
curl http://localhost:1880/api/v1/loads | jq
curl http://localhost:1880/api/v1/health | jq
```

## Verify Kafka publishing

```bash
KAFKA_BROKERS="217.160.222.177:32413,217.160.219.79:31053,213.165.77.54:31860"
KAFKA_CERTS="-X security.protocol=ssl \
  -X ssl.ca.location=/path/to/ca.crt \
  -X ssl.certificate.location=/path/to/tls.crt \
  -X ssl.key.location=/path/to/tls.key"

kcat -b "${KAFKA_BROKERS}" ${KAFKA_CERTS} \
  -t sim.smart_energy.meter -C -c 5 -e | jq
```

## Verify MQTT publishing

```bash
mosquitto_sub -h facis-mqtt -p 1883 -t 'facis/#' -C 10
```

## Verify Modbus

```bash
python3 - <<'PY'
import asyncio
from pymodbus.client import AsyncModbusTcpClient

async def main():
    c = AsyncModbusTcpClient('localhost', port=5020)
    await c.connect()
    rsp = await c.read_holding_registers(address=19000, count=2, slave=1)
    high, low = rsp.registers
    import struct
    val = struct.unpack('>f', struct.pack('>HH', high, low))[0]
    print(f'Active power L1: {val:.1f} W')
    c.close()

asyncio.run(main())
PY
```

(`kubectl -n orce port-forward svc/facis-orce 5020:5020` first.)

## Observability

```bash
# Prometheus scrape
curl http://localhost:1880/metrics | head -40

# Stdout JSON logs
kubectl -n orce logs deploy/facis-orce -c orce | head -20

# Tail dead-letter topic
kcat -b "${KAFKA_BROKERS}" ${KAFKA_CERTS} -t sim.dead_letter -C -e
```

Counters emitted:

- `facis_sim_ticks_total{runtime="orce-native"}`
- `facis_kafka_messages_sent_total{topic=...}`
- `facis_mqtt_messages_sent_total{topic=...}`
- `facis_modbus_requests_total{register_type=FCx}`

## Switch modes

### Hand off to legacy (Python) runtime

```bash
helm upgrade facis-simulation services/simulation/helm/facis-simulation/ \
  --namespace facis --reuse-values \
  --set compatibilityMode=legacy
```

The Python deployment scales back to 1; the post-install Job is **not** torn
down (idempotent re-import). The flow runtime tabs (`engine`, `tick`, `feeds`,
`modbus`, `rest`) keep running in ORCE but the engine FSM stays
`initialized` because the Python pod now drives the envelope into the
adapters via `POST /api/sim/tick`.

### Hand back to ORCE

```bash
helm upgrade facis-simulation services/simulation/helm/facis-simulation/ \
  --namespace facis --reuse-values \
  --set compatibilityMode=orce
```

## Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `orce-flow-deploy` Job CrashLoopBackoff | ORCE pod not ready or admin token wrong | `kubectl -n facis logs job/...-orce-flow-deploy`; check ORCE readiness; rotate `facis-orce-admin` Secret |
| `/api/v1/meters` returns `[]` | Engine in `initialized`, no tick has fired | `POST /api/v1/simulation/start`; wait `1 / speed_factor` minutes |
| Modbus client times out | `node-red-contrib-modbus` not loaded in PVC | `kubectl -n orce exec deploy/facis-orce -- ls /data/node_modules/node-red-contrib-modbus` — if missing, re-run entrypoint by deleting the pod |
| Kafka publishing silent | Cert paths invalid | `kubectl -n orce describe pod` → check `kafka-tls-certs` mount |
| Determinism drift between two runs | seed not reset before second run | `POST /api/v1/simulation/reset {"seed": N, "start_time": ...}` first |

## Backup / restore

The only persistent state is `/data/sim-state/{state.json,entities.json}` on
the ORCE PVC. Back up the PVC volume snapshot via the IONOS console; restore
by remounting and Pod-restarting ORCE. The Helm chart is otherwise stateless.

## mTLS cert rotation (Stackable)

Stackable's secret-operator issues short-lived client certs (currently
~3 months). `Secret/facis-kafka-certs` in the `orce` namespace is a static
copy created from `Credentials and configs/credentials.txt`. When the cert
gets within 14 days of expiry, `FacisKafkaCertExpiringSoon` fires (warning);
within 7 days, `FacisKafkaCertExpiringCritical` fires (page-worthy).

**Detection:** the `kafka-cert-expiry-check` CronJob runs daily at 06:00 UTC
in the `orce` namespace. It pushes
`facis_kafka_cert_days_until_expiry{cert="tls.crt"}` to the
prometheus-pushgateway and writes a structured-JSON log. Alerts route to the
existing alertmanager.

**Force-trigger for testing:**
```bash
kubectl create job --from=cronjob/kafka-cert-expiry-check \
  kafka-cert-expiry-now -n orce
kubectl logs job/kafka-cert-expiry-now -n orce
```

**Manual rotation procedure:** auto-rotation isn't implemented (we don't
have admin access to the Stackable cluster from IONOS). When the alert
fires:

1. Obtain a fresh cert bundle from the FACIS Stackable owners — they
   extract from their `kubectl -n stackable get secret <kafka-tls-secret>`
   and update `Credentials and configs/credentials.txt` in the shared
   FACIS repo. Confirm the new `tls.crt`'s `notAfter` is reasonable.
2. Extract the three PEM blocks from `credentials.txt` to local files
   (`ca.crt`, `tls.crt`, `tls.key`); validate the chain:
   ```bash
   openssl verify -CAfile ca.crt tls.crt
   # cert + key modulus match
   diff <(openssl x509 -modulus -noout -in tls.crt) \
        <(openssl rsa  -modulus -noout -in tls.key)
   ```
3. Replace the Secret:
   ```bash
   kubectl create secret generic facis-kafka-certs -n orce \
     --from-file=ca.crt=ca.crt \
     --from-file=tls.crt=tls.crt \
     --from-file=tls.key=tls.key \
     --dry-run=client -o yaml | kubectl apply -f -
   ```
4. Restart the ORCE pod so it picks up the new cert via the projected
   Secret mount:
   ```bash
   kubectl rollout restart deployment/orce -n orce
   kubectl rollout status  deployment/orce -n orce
   ```
5. Verify producers reconnect:
   ```bash
   kubectl logs -n orce deploy/orce | grep "rdkafka.*Producer ready" | tail -9
   ```
   Expect 9 `Producer ready` lines (one per topic). Confirm message
   delivery with `kcat -t sim.smart_energy.meter -C -o end -e -c 1`.

**Permanent fix follow-ups:** `cert-manager`-issued client certs (issued
on the IONOS side, accepted as a CA by Stackable), or a shared cert store
(Vault) that both clusters read from. Both are out of scope for the
current operating model and tracked separately.

## Kafka NodePort drift (Stackable)

The Stackable Kafka brokers are exposed via NodePort; the ports change on
broker restart. The architecture handles this in two layers:

**Layer 1 — Client-side metadata refresh (primary, automatic).** The
patched `node-red-contrib-rdkafka` sets `metadata.max.age.ms=30000`, so
within 30s of a NodePort change the rdkafka client re-fetches the broker
list via the stable bootstrap (`212.132.83.222:9093`, hardcoded as the
first entry of `metadata.broker.list` so it's always reachable for
metadata RPCs). No human intervention; no flow redeploy.

**Layer 2 — Source-of-truth alignment (defence-in-depth, automated).**
The `kafka-broker-watcher` CronJob runs every 15 minutes in the `orce`
namespace. It runs `kcat -L` against the bootstrap, compares the live
broker list to the one currently deployed in `kafka.json` via the ORCE
Admin API, and re-renders + redeploys if the drift is stable for 2
consecutive checks. The "stable for 2" rule prevents flapping during
Stackable rolling restarts (when each broker briefly disappears).

**Force-trigger the watcher:**
```bash
kubectl create job --from=cronjob/kafka-broker-watcher \
  kafka-broker-watcher-now -n orce
kubectl logs job/kafka-broker-watcher-now -n orce
# Expected: structured JSON line with msg="no drift" or
# msg="drift detected, awaiting confirmation next cycle".
```

**Manual rediscover (if both layers somehow miss):**
```bash
# 1. Discover current NodePorts from bootstrap
kcat -L -b 212.132.83.222:9093 \
     -X security.protocol=ssl \
     -X ssl.ca.location=/path/to/ca.crt \
     -X ssl.certificate.location=/path/to/tls.crt \
     -X ssl.key.location=/path/to/tls.key \
     -X ssl.endpoint.identification.algorithm=none \
  | grep "broker [0-9]* at"

# 2. Update KAFKA_BROKERS in services/simulation/orce/scripts/render-kafka.py
# 3. Re-render and redeploy
python3 services/simulation/orce/scripts/render-kafka.py
# (then GET-merge-POST via Admin API; see infrastructure/README.md for the
# pattern, or use the watcher as a one-shot via the kubectl create job above)
```

**Why the bootstrap stays:** the bootstrap address is reserved for the
secret-operator-managed listener and isn't part of the broker rotation.
We've held the address constant across multiple NodePort changes; if it
ever moves, every chart that consumes Kafka will break and the alert
mechanism would catch it.
