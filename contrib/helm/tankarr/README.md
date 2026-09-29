# Tankarr Helm chart

Deploys [Tankarr](https://github.com/Renzodef/tankarr), the self-hosted manga,
manhwa and comics manager for the *arr stack, on Kubernetes 1.25 or newer.

The chart runs one replica (the database is SQLite on a `ReadWriteOnce`
volume) with a `Recreate` strategy, a non-root security context, probes on
`/api/ready` and `/api/health`, PersistentVolumeClaims for `/config` and
`/library` that survive `helm uninstall`, and optionally an Ingress and a
Prometheus Operator `ServiceMonitor`.

## Install

```sh
git clone https://github.com/Renzodef/tankarr.git
helm install tankarr tankarr/contrib/helm/tankarr \
  --namespace tankarr --create-namespace \
  --set persistence.library.existingClaim=comics \
  --set ingress.enabled=true \
  --set ingress.hosts[0].host=tankarr.example.com
```

Then read the login Tankarr creates on its first start and change it under
**Settings → Security**:

```sh
kubectl logs -n tankarr deploy/tankarr | grep "created one"
```

`helm test tankarr -n tankarr` checks that the service answers.

## Values

| Value | Default | Description |
| --- | --- | --- |
| `image.repository` | `ghcr.io/renzodef/tankarr` | Image to run. |
| `image.tag` | `""` (the chart's `appVersion`) | Image tag; `latest` follows every release, `X.Y` one release line. |
| `env` | `TZ`, `TANKARR_LOG_LEVEL` | Plain environment variables, named like the [configuration reference](https://renzodef.github.io/tankarr/configuration/). |
| `secretEnv` | `{}` | Variables holding secrets (API keys, passwords, tokens); written to a Secret the chart owns and loaded with `envFrom`. |
| `existingSecret` | `""` | A Secret you manage instead, with the variable names as keys. `secretEnv` is ignored when set. |
| `service.type`, `service.port` | `ClusterIP`, `8787` | The Service in front of the pod. |
| `ingress.enabled` | `false` | Create an Ingress; `className`, `annotations`, `hosts` and `tls` as usual. Set `env.TANKARR_URL_BASE` to serve a sub-path and use the same path here. |
| `persistence.config` | 10Gi, `ReadWriteOnce` | Tankarr's own data. `existingClaim` uses a claim you created; `storageClass: "-"` disables dynamic provisioning. |
| `persistence.library` | 200Gi, `ReadWriteOnce` | The CBZ library; share it with the reader through `existingClaim`. |
| `persistence.import`, `.downloads`, `.usenet` | disabled | Read-only mounts of existing claims: files to import, qBittorrent's and SABnzbd's completed folders. |
| `extraVolumes`, `extraVolumeMounts` | `[]` | Anything else, for example a backup disk at `/backups` with `env.TANKARR_BACKUP_DIRECTORY=/backups`. |
| `podSecurityContext`, `securityContext` | non-root `1000:1000`, `fsGroup: 1000`, no capabilities | Tankarr never runs as root. |
| `resources` | 100m / 512Mi requests, 2Gi memory limit | About 400 MB alone, 1.5 GB with the managed Suwayomi engine. |
| `startupProbe`, `readinessProbe`, `livenessProbe` | `/api/ready`, `/api/ready`, `/api/health` | Health checks; they answer outside `TANKARR_URL_BASE` too. |
| `strategy`, `terminationGracePeriodSeconds` | `Recreate`, `60` | Never two pods on the same database. |
| `serviceMonitor.enabled` | `false` | Prometheus Operator scrape of `/metrics`; `apiKeySecret.name`/`.key` name the Secret holding the API key used as bearer token, `path` must include the URL base when one is set. |
| `podAnnotations`, `podLabels`, `nodeSelector`, `tolerations`, `affinity`, `imagePullSecrets`, `nameOverride`, `fullnameOverride` | empty | The usual knobs. |

The full list with comments is in [`values.yaml`](values.yaml).

## Notes

- The claims the chart creates are annotated `helm.sh/resource-policy: keep`:
  `helm uninstall` leaves the database and the library in place. Delete them
  yourself when you mean it.
- With `fsGroup: 1000` Kubernetes makes the volumes group-writable for the
  application on mount (`OnRootMismatch`, so an already correct volume is
  not re-walked). A library shared with a reader running as another user
  needs permissions both can use; see
  [File permissions](https://renzodef.github.io/tankarr/installation/#file-permissions).
- Backups live in `/config/backups` unless `TANKARR_BACKUP_DIRECTORY` points
  elsewhere; see [Upgrading](https://renzodef.github.io/tankarr/upgrading/).
