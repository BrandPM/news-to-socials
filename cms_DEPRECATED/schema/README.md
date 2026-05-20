# Directus schema snapshots

On Stage 2 Day 2, after defining the 6 collections in Directus UI, run:

```bash
docker compose exec directus npx directus schema snapshot ./snapshot.yaml
docker compose cp directus:/directus/snapshot.yaml ./snapshot.yaml
```

…and commit `snapshot.yaml` to this directory. Subsequent migrations are
applied with:

```bash
docker compose exec directus npx directus schema apply ./snapshot.yaml --yes
```

Collections (per Master Doc §A.7.2):

| Collection | Purpose |
|---|---|
| `brands` | 5 brand profiles + voice + visual config |
| `sources` | RSS / Telegram / web sources |
| `topics` | Items that passed relevance + dedup |
| `posts` | Drafts/approved/published per channel |
| `channels` | Per-brand-per-platform delivery endpoints |
| `audit_log` | Append-only operational log |

Field-by-field definitions live in `IT_PROJ_NTS_032_stage2_directus_voice.md`
in the Obsidian vault.
