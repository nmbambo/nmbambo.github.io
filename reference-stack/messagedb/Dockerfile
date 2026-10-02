# Message DB (https://github.com/message-db/message-db) on stock Postgres 16.
# Version pin: MESSAGE_DB_VERSION below. The tag was confirmed readable on raw.githubusercontent.com
# (database/VERSION.txt = 1.3.0 on master). Release tarball checksum is NOT pinned (could not fetch the
# tarball from the build sandbox); add a sha256 check when you first build on your machine.
FROM postgres:16.10-bookworm
ARG MESSAGE_DB_VERSION=1.3.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /opt/message-db \
 && curl -fsSL "https://github.com/message-db/message-db/archive/refs/tags/v${MESSAGE_DB_VERSION}.tar.gz" \
    | tar -xz --strip-components=1 -C /opt/message-db
COPY install-message-db.sh /docker-entrypoint-initdb.d/10-install-message-db.sh
RUN chmod +x /docker-entrypoint-initdb.d/10-install-message-db.sh
