# OCM W-Stack baseline

The identity integration of the FAP uses the OCM W-Stack services of the
federation. This directory pins the version of each of them, so that every
installation and test run targets the same release.

- `wstack-images.lock.tsv` lists every Helm chart and container image of the
  `ocm-wstack` ApplicationSet of
  [eclipse-xfsc/deployment](https://github.com/eclipse-xfsc/deployment), with
  its sha256 digest. The first line records the `deployment` commit it was
  built from.
- `mirror-wstack-images.sh` regenerates the lock from a `deployment` commit,
  and can copy the pinned images and charts to another registry.
- `check-baseline.py` checks the lock, the registry and a running namespace.
  Its tests are in `tests/`.

Tags on the XFSC registry can move and the charts pull with
`pullPolicy: Always`, so the digest is what counts, not the tag.

## Required components

| Image | Constraint |
|---|---|
| `ocm-wstack/issuance-service` | |
| `ocm-wstack/pre-authorization-bridge` | |
| `ocm-wstack/credential-retrieval-service` | |
| `ocm-wstack/storage-service` | |
| `ocm-wstack/credential-verification-service` | 1.3.0 or later (DCQL support) |
| `ocm-wstack/status-list-service` | |
| `ocm-wstack/well-known-service` | |
| `tsa/signer` | |
| `tsa/crypto-provider-hashicorp-vault-plugin` | |

Keycloak and PCM Cloud are not part of any `deployment` ApplicationSet and are
not covered by the lock.

## Checks

```bash
python3 infrastructure/wstack/check-baseline.py                              # lock file only
python3 infrastructure/wstack/check-baseline.py --expect-deployment 55e28aa  # and its deployment commit
python3 infrastructure/wstack/check-baseline.py --registry [--strict]        # tags moved since the lock (skopeo)
python3 infrastructure/wstack/check-baseline.py --cluster <namespace>        # pods run the pinned digests (kubectl)
python3 -m unittest discover -s infrastructure/wstack/tests
```

A moved tag is a warning unless `--strict` is given. In `--cluster` mode,
images are matched on their repository path, so pods pulling from a mirror are
checked too; images that are not in the lock (NATS, Cassandra...) are only
listed.

## Updating

```bash
infrastructure/wstack/mirror-wstack-images.sh --ref <deployment commit>
python3 infrastructure/wstack/check-baseline.py --expect-deployment <deployment commit>
```

Without `--apply` the script only writes the lock. Review its diff like a
dependency upgrade. With `--apply --target <registry>/<prefix>` it also copies
the images (by digest) and the charts. Cosign signatures are not copied.

The script needs git, helm 3.8 or later, skopeo and python3 with PyYAML. The
checks only need python3 (plus skopeo or kubectl for the online modes).
