# Running Tests

## Unit tests (default)

```bash
python3 -m pytest -q
```

Live forge integration tests (`@pytest.mark.forge_live`) are skipped automatically when `DEVLOOPS_FORGE_LIVE_URL` is unset.

## Live Forgejo / Gitea integration tests

Pinned versions match the minimum supported versions in `devloops_lib/forge/gitea.py` (Forgejo `>= 7.0.0`, Gitea `>= 1.21.0`):

- Forgejo: `codeberg.org/forgejo/forgejo:7.0.4`
- Gitea: `gitea/gitea:1.21.11`

```bash
docker run -d --name forge-live -p 3000:3000 \
  -e USER_UID=1000 -e USER_GID=1000 \
  -e GITEA__database__DB_TYPE=sqlite3 \
  -e GITEA__security__INSTALL_LOCK=true \
  -e GITEA__server__ROOT_URL=http://127.0.0.1:3000/ \
  -e GITEA__server__DOMAIN=127.0.0.1 \
  -e GITEA__server__HTTP_PORT=3000 \
  codeberg.org/forgejo/forgejo:7.0.4

# Wait until http://127.0.0.1:3000/api/v1/version responds, then provision a user & token:
docker exec -u git forge-live forgejo admin user create \
  --admin --username tester --password "$(openssl rand -hex 16)" \
  --email ci@example.local --must-change-password=false

export DEVLOOPS_FORGE_LIVE_URL=http://127.0.0.1:3000
export DEVLOOPS_FORGE_LIVE_TYPE=forgejo
export DEVLOOPS_FORGE_LIVE_TOKEN=$(docker exec -u git forge-live forgejo admin user generate-access-token \
  --username tester --token-name local-run --scopes all --raw | tail -n 1)

python3 -m pytest -q -m forge_live tests/test_forge_live.py
docker rm -f forge-live
```
