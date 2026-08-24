# Installing Argo CD (component 1, one-time per cluster)

Run `./install.sh` (recommended), or the manual steps below.

## Manual install
```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server --timeout=300s
```

## Hand control to Git
```bash
kubectl apply -f argocd/projects/finbot-project.yaml   # guardrails
kubectl apply -f argocd/bootstrap/root-app.yaml        # the App-of-Apps
```

## Log in to the UI
```bash
kubectl -n argocd port-forward svc/argocd-server 8081:443
# open https://localhost:8081  (accept the self-signed cert)
# user: admin
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
```

## (Optional) CLI login
```bash
argocd login localhost:8081 --username admin --password <pw> --insecure
argocd app list
```
