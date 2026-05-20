# ⚠️ DEPRECATED — этот каталог не используется

После pivot 20.05.2026 (см. `docs/ADR/0018-sanity-pivot.md`) мы перешли с
self-hosted Directus на existing Sanity CMS.

Этот каталог оставлен в репозитории для истории — он содержит
docker-compose stack для Directus + Postgres + Caddy который мы НЕ
поднимаем в production. Контент управляется через Sanity Studio на
icon.finance/studio.

## Когда может понадобиться

* Если когда-нибудь решим вернуться к self-hosted CMS
* Как референс кода (Directus REST clients, schema patterns)
* Если на Stage 5 для какого-то wire-бренда понадобится свой CMS

В остальных случаях — игнорируй.

См. ADR-018 в `docs/ADR/0018-sanity-pivot.md` и Master Documentation §3.
